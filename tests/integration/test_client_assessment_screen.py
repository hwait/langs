"""The assessment screen's HTTP surface: playback, plays, discovery, the shell, and scoring.

What the browser does is in `tests/browser/`. This file proves what the server owes the
page: that a recording is served only as it was snapshotted, that plays are counted where
they cannot be talked up, that a fresh page can find its run, that the shell is the only
thing reachable without a token, and that a calibration completes through this surface
with every score computed by the server and no model called.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_reader
from tests.conftest import PolishWorkspace
from tests.support.client_http import Client, key, serving
from tests.support.recordings import publish_pilot_with_recordings, recording_for


@pytest.fixture
def recorded(polish_workspace: PolishWorkspace, tmp_path: Path) -> PolishWorkspace:
    publish_pilot_with_recordings(polish_workspace, tmp_path, replay_allowance=2)
    return polish_workspace


@pytest.fixture
def client(recorded: PolishWorkspace) -> Iterator[Client]:
    with serving(recorded.paths, recorded.clock) as running:
        yield running


def _start_listening(client: Client) -> str:
    started = client.post(
        "/runs",
        {
            "dimensions": ["listening"],
            "modalities": ["audio"],
            "scoring": "machine",
            "idempotency_key": key(),
        },
    )
    return str(started.data["run_id"])


def _serve(client: Client, run_id: str) -> dict[str, Any]:
    served = client.post(f"/runs/{run_id}/tasks", {"idempotency_key": key()})
    assert "content_id" in served.data, served.data
    return served.data


def _stable_key(workspace: PolishWorkspace, content_id: str) -> str:
    with open_reader(workspace.paths) as database:
        return str(
            database.scalar(
                "SELECT stable_key FROM content_records WHERE content_id = ?", [content_id]
            )
        )


# --- playback ----------------------------------------------------------------------


def test_the_recording_is_served_as_it_was_snapshotted(
    recorded: PolishWorkspace, client: Client
) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)

    answer = client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio")

    assert answer.status == 200, answer.body[:200]
    assert answer.headers["content-type"] == "audio/wav"
    assert answer.headers["cache-control"] == "no-store"
    assert answer.body == recording_for(_stable_key(recorded, task["content_id"]))
    assert hashlib.sha256(answer.body).hexdigest() == task["asset"]["sha256"]


def test_playback_needs_the_token(client: Client) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)

    answer = client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio", token=None)

    assert answer.status == 403
    assert answer.code == "client_token_required"


def test_fetching_a_recording_is_not_a_play(client: Client) -> None:
    """The page fetches once and replays from memory, so fetching cannot be what is counted."""

    run_id = _start_listening(client)
    task = _serve(client, run_id)
    client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio")
    client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio")

    (outstanding,) = client.get(f"/runs/{run_id}/screen").data["outstanding"]
    assert outstanding["plays_used"] == 0
    assert outstanding["plays_remaining"] == 2


def test_a_task_not_served_in_this_run_has_no_recording_to_fetch(
    recorded: PolishWorkspace, client: Client
) -> None:
    run_id = _start_listening(client)
    with open_reader(recorded.paths) as database:
        stranger = database.scalar(
            "SELECT task.content_id FROM assessment_tasks task WHERE task.modality = 'audio' "
            "LIMIT 1"
        )

    answer = client.get(f"/runs/{run_id}/tasks/{stranger}/audio")

    assert answer.code == "assessment_task_not_served"


def test_a_settled_task_has_no_recording_to_fetch(client: Client) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)
    choices = (task["presentation"] or {}).get("choices") or [{"value": "x"}]
    client.post(
        f"/runs/{run_id}/results",
        {
            "content_id": task["content_id"],
            "response": choices[0]["value"],
            "idempotency_key": key(),
        },
    )

    answer = client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio")

    assert answer.code == "assessment_task_settled"


def test_a_recording_replaced_since_the_serve_is_refused_rather_than_played(
    recorded: PolishWorkspace, client: Client, tmp_path: Path
) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)
    root = tmp_path / "again"
    root.mkdir()
    from tests.support import recordings

    original = recordings.recording_for
    try:
        recordings.recording_for = lambda stable_key: original(stable_key) + b"\x00\x00"
        publish_pilot_with_recordings(recorded, root, version="9.9.0")
    finally:
        recordings.recording_for = original

    answer = client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio")

    assert answer.code == "assessment_asset_changed"


def test_a_recording_tampered_on_disk_reports_the_pack_rather_than_playing(
    recorded: PolishWorkspace, client: Client, tmp_path: Path
) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)
    for path in (tmp_path / "pl-pilot-recorded" / "media").iterdir():
        path.write_bytes(path.read_bytes() + b"tampered")

    answer = client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio")

    assert answer.status != 200
    assert answer.code == "pack_checksum_mismatch"


# --- plays ---------------------------------------------------------------------------


def test_a_play_is_recorded_through_its_own_route(client: Client) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)
    play_key = key()

    first = client.post(
        f"/runs/{run_id}/tasks/{task['content_id']}/plays", {"idempotency_key": play_key}
    )
    retried = client.post(
        f"/runs/{run_id}/tasks/{task['content_id']}/plays", {"idempotency_key": play_key}
    )

    assert first.data["plays_used"] == 1
    assert retried.data == first.data
    assert client.get(f"/runs/{run_id}/screen").data["outstanding"][0]["plays_used"] == 1


def test_a_play_without_a_key_is_refused_by_the_contract(client: Client) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)

    answer = client.post(f"/runs/{run_id}/tasks/{task['content_id']}/plays", {})

    assert answer.code == "invalid_contract"


def test_plays_past_the_allowance_are_refused(client: Client) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)
    path = f"/runs/{run_id}/tasks/{task['content_id']}/plays"
    client.post(path, {"idempotency_key": key()})
    client.post(path, {"idempotency_key": key()})

    answer = client.post(path, {"idempotency_key": key()})

    assert answer.status == 422
    assert answer.code == "assessment_replays_exhausted"


def test_the_stored_result_carries_the_count_the_rows_say(
    recorded: PolishWorkspace, client: Client
) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)
    path = f"/runs/{run_id}/tasks/{task['content_id']}/plays"
    client.post(path, {"idempotency_key": key()})
    client.post(path, {"idempotency_key": key()})
    choices = (task["presentation"] or {}).get("choices") or [{"value": "x"}]
    client.post(
        f"/runs/{run_id}/results",
        {
            "content_id": task["content_id"],
            "response": choices[0]["value"],
            "idempotency_key": key(),
        },
    )

    with open_reader(recorded.paths) as database:
        count = database.scalar(
            "SELECT play_count FROM assessment_results WHERE run_id = ?", [run_id]
        )
    assert count == 2


def test_a_caller_cannot_supply_a_play_count(client: Client) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)

    answer = client.post(
        f"/runs/{run_id}/results",
        {
            "content_id": task["content_id"],
            "response": "x",
            "play_count": 1,
            "idempotency_key": key(),
        },
    )

    assert answer.code == "invalid_contract"


# --- discovery -----------------------------------------------------------------------


@pytest.fixture
def plain(polish_workspace: PolishWorkspace) -> Iterator[Client]:
    with serving(polish_workspace.paths, polish_workspace.clock) as running:
        yield running


def _open(client: Client, **extra: Any) -> str:
    return str(client.post("/runs", {"idempotency_key": key(), **extra}).data["run_id"])


def test_a_fresh_page_finds_the_runs_it_can_resume_newest_first(plain: Client) -> None:
    older = _open(plain)
    paused = _open(plain, scoring="machine")
    plain.post(f"/runs/{paused}/status", {"status": "paused"})
    closed = _open(plain)
    plain.post(f"/runs/{closed}/status", {"status": "abandoned"})

    listed = plain.get("/runs").data

    assert [run["run_id"] for run in listed["runs"]] == [paused, older]
    assert [run["status"] for run in listed["runs"]] == ["paused", "in-progress"]
    assert listed["runs"][0]["scoring"] == "machine"
    assert listed["omitted"] == 0


def test_discovery_filters_by_status(plain: Client) -> None:
    _open(plain)
    paused = _open(plain)
    plain.post(f"/runs/{paused}/status", {"status": "paused"})

    listed = plain.get("/runs?status=paused").data

    assert [run["run_id"] for run in listed["runs"]] == [paused]


def test_discovery_lists_only_resumable_runs(plain: Client) -> None:
    answer = plain.get("/runs?status=finalized")

    assert answer.code == "invalid_contract"


def test_discovery_refuses_a_query_it_does_not_publish(plain: Client) -> None:
    answer = plain.get("/runs?limit=5")

    assert answer.code == "invalid_contract"


def test_discovery_names_the_track_it_was_asked_about(
    polish_workspace: PolishWorkspace, plain: Client
) -> None:
    _open(plain)

    mine = plain.get(f"/runs?track={polish_workspace.track_id}").data
    nobody = plain.get("/runs?track=trk_01ARZ3NDEKTSV4RRFFQ69G5FAV")

    assert len(mine["runs"]) == 1
    assert mine["track_id"] == polish_workspace.track_id
    assert nobody.status == 404


def test_a_query_string_does_not_hide_a_route(plain: Client) -> None:
    """Routing reads the path; a query on a route that takes none is refused by name."""

    run_id = _open(plain)

    assert plain.get(f"/runs/{run_id}/screen?x=1").code == "invalid_contract"
    assert plain.get("/health?x=1").status == 200


def test_the_launch_url_can_carry_a_run_reference(polish_workspace: PolishWorkspace) -> None:
    from linguawiki.client import server as server_module

    run_id = "asm_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    server = server_module.build_server(
        polish_workspace.paths, clock=polish_workspace.clock, run=run_id
    )
    try:
        assert server.launch_url.endswith(f"#token={server.token}&run={run_id}")
    finally:
        server.close()


def test_a_launch_reference_that_is_not_a_run_is_refused_before_binding(
    polish_workspace: PolishWorkspace,
) -> None:
    from linguawiki.client import server as server_module
    from linguawiki.errors import LinguaWikiError

    with pytest.raises(LinguaWikiError) as refused:
        server_module.build_server(polish_workspace.paths, run="not-a-run")
    assert refused.value.payload.code == "invalid_arguments"


# --- the public shell ------------------------------------------------------------------

SHELL = {
    "/": "text/html; charset=utf-8",
    "/app.js": "text/javascript; charset=utf-8",
    "/app.css": "text/css; charset=utf-8",
}


@pytest.mark.parametrize(("path", "media"), sorted(SHELL.items()))
def test_the_shell_answers_without_a_token(plain: Client, path: str, media: str) -> None:
    """The first navigation cannot carry a header, and the fragment never reaches us."""

    answer = plain.get(path, token=None)

    assert answer.status == 200
    assert answer.headers["content-type"] == media
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert answer.headers["referrer-policy"] == "no-referrer"
    policy = answer.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "'unsafe-inline'" not in policy


def test_the_shell_loads_its_script_as_a_module_and_inlines_nothing(plain: Client) -> None:
    page = plain.get("/", token=None).body.decode()

    assert '<script type="module" src="/app.js"></script>' in page
    assert "<script>" not in page
    assert "style=" not in page


def test_the_shell_is_still_behind_the_host_allowlist(plain: Client) -> None:
    """DNS rebinding is the same attack whether the answer is a page or a run."""

    answer = plain.get("/", token=None, host="linguawiki.example.com")

    assert answer.status == 403
    assert answer.code == "client_host_denied"


@pytest.mark.parametrize(
    "path",
    [
        "/index.html",
        "/static/app.js",
        "/../data/linguawiki.duckdb",
        "/static/../app.js",
        "/%2e%2e/data/linguawiki.duckdb",
        "/app.js/../../data",
        "/data/linguawiki.duckdb",
        "/linguawiki.toml",
        "/app.js?x=1",
        "/app.js/",
        "/APP.JS",
    ],
)
def test_nothing_else_is_reachable_without_the_token(plain: Client, path: str) -> None:
    answer = plain.get(path, token=None)

    assert answer.status == 403
    assert answer.code == "client_token_required"


def test_the_shell_is_read_only(plain: Client) -> None:
    answer = plain.post("/", {}, token=None)

    assert answer.status == 403
    assert answer.code == "client_token_required"


def test_the_shell_ships_in_the_package_not_the_workspace(
    polish_workspace: PolishWorkspace,
) -> None:
    from importlib.resources import files

    static = files("linguawiki.client") / "static"
    for name in ("index.html", "app.js", "app.css"):
        assert (static / name).is_file(), name
    assert not (polish_workspace.root / "static").exists()
