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


def test_a_recording_tampered_on_disk_is_refused_rather_than_played(
    recorded: PolishWorkspace, client: Client, tmp_path: Path
) -> None:
    run_id = _start_listening(client)
    task = _serve(client, run_id)
    for path in (tmp_path / "pl-pilot-recorded" / "media").iterdir():
        path.write_bytes(path.read_bytes() + b"tampered")

    answer = client.get(f"/runs/{run_id}/tasks/{task['content_id']}/audio")

    assert answer.status != 200
    assert answer.code == "assessment_asset_changed"


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


def test_discovery_says_whether_a_judge_can_mark_the_learners_writing(
    polish_workspace: PolishWorkspace, plain: Client
) -> None:
    """The page chooses `machine+judged` from this, having no preferences of its own."""

    from linguawiki.services import learners as learner_service

    assert plain.get("/runs").data["written_offered"] is True
    learner_service.update_track(
        polish_workspace.paths,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=polish_workspace.clock,
    )
    assert plain.get("/runs").data["written_offered"] is False


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
    "/transport.js": "text/javascript; charset=utf-8",
    "/sessions.html": "text/html; charset=utf-8",
    "/sessions.js": "text/javascript; charset=utf-8",
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


def test_the_session_page_loads_its_script_as_a_module_and_inlines_nothing(plain: Client) -> None:
    page = plain.get("/sessions.html", token=None).body.decode()

    assert '<script type="module" src="/sessions.js"></script>' in page
    assert "<script>" not in page
    assert "style=" not in page


def test_the_session_page_shell_does_not_shadow_the_session_route(plain: Client) -> None:
    """`/sessions` is the discovery route, so the page lives at `/sessions.html`."""

    answer = plain.get("/sessions", token=None)

    assert answer.status == 403
    assert answer.code == "client_token_required"


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
    for name in ("index.html", "app.js", "app.css", "transport.js", "sessions.html", "sessions.js"):
        assert (static / name).is_file(), name
    assert not (polish_workspace.root / "static").exists()


# --- a whole calibration, scored by the server -------------------------------------------


def _answers_key(workspace: PolishWorkspace, run_id: str, content_id: str) -> list[str]:
    import json

    with open_reader(workspace.paths) as database:
        raw = database.scalar(
            "SELECT expected_json FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
            [run_id, content_id],
        )
    answers: list[str] = json.loads(str(raw))["answers"]
    return answers


def _response(task: dict[str, Any], answers: list[str], *, correct: bool) -> str:
    """What a learner pressing a button or typing would send, chosen by the server's scorer.

    For a choice task the response is one of the choice *values* -- the button the learner
    pressed -- never an index or the display text.
    """

    from linguawiki.placement import score_response

    choices = (task.get("presentation") or {}).get("choices") or []
    if choices:
        values = [choice["value"] for choice in choices]
        scored = {
            value: score_response(task_type=task["task_type"], answers=answers, response=value)
            for value in values
        }
        wanted = [value for value in values if (scored[value] == 1.0) == correct]
        return wanted[0] if wanted else values[0]
    return answers[0] if correct else "zupełnie nie to"


def _calibrate_over_http(
    workspace: PolishWorkspace, client: Client
) -> tuple[str, list[tuple[str, str]]]:
    run_id = _open(client, scoring="machine", modalities=["text", "audio"])
    given: list[tuple[str, str]] = []
    for step in range(200):
        screen = client.get(f"/runs/{run_id}/screen").data
        task = next((each for each in screen["outstanding"] if not each["needs_judge"]), None)
        if task is None:
            if not any(each["status"] == "open" for each in screen["dimensions"]):
                break
            client.post(f"/runs/{run_id}/tasks", {"idempotency_key": key()})
            continue
        if task["plays_audio"]:
            client.post(
                f"/runs/{run_id}/tasks/{task['content_id']}/plays", {"idempotency_key": key()}
            )
        response = _response(
            task,
            _answers_key(workspace, run_id, task["content_id"]),
            correct=step % 3 != 2,
        )
        given.append((task["content_id"], response))
        recorded = client.post(
            f"/runs/{run_id}/results",
            {"content_id": task["content_id"], "response": response, "idempotency_key": key()},
        )
        assert recorded.status == 200, recorded.payload
    else:  # pragma: no cover - a run that never stops is the failure under test
        raise AssertionError("the calibration did not finish")
    finalized = client.post(
        f"/runs/{run_id}/finalization", {"reason": "completed", "idempotency_key": key()}
    )
    assert finalized.data["status"] == "finalized"
    return run_id, given


@pytest.fixture
def no_model(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No model credentials, and any connection off this machine fails the test."""

    import socket

    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    attempted: list[str] = []
    connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if isinstance(address, tuple) and host not in ("127.0.0.1", "::1", "localhost"):
            attempted.append(str(host))
            raise AssertionError(f"a calibration reached off this machine: {host}")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return attempted


def test_a_machine_calibration_completes_scored_by_the_server_alone(
    recorded: PolishWorkspace, client: Client, no_model: list[str]
) -> None:
    run_id, given = _calibrate_over_http(recorded, client)

    results_sent = [body for method, path, body in client.sent if path.endswith("/results")]
    assert results_sent, "nothing was answered"
    for body in results_sent:
        assert body is not None
        assert set(body) == {"content_id", "response", "idempotency_key"}, body
    with open_reader(recorded.paths) as database:
        rows = database.query(
            "SELECT assessor_kind, score_source, scoring_policy_version, play_count, "
            "task.modality FROM assessment_results result "
            "JOIN assessment_run_tasks task USING (run_id, content_id) "
            "WHERE result.run_id = ?",
            [run_id],
        )
    from linguawiki.placement import SCORING_POLICY_VERSION

    assert len(rows) == len(given)
    assert {(row[0], row[1], row[2]) for row in rows} == {
        ("deterministic", "computed", SCORING_POLICY_VERSION)
    }
    # Listening was tested, from recordings, and every listening result knows its plays.
    audio = [row for row in rows if row[4] == "audio"]
    assert audio, "no listening task was served"
    assert {row[3] for row in audio} == {1}
    assert no_model == []


def test_the_estimates_are_the_ones_the_cli_reaches_from_the_same_answers(
    recorded: PolishWorkspace, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import shutil

    from linguawiki.cli import run as cli

    # The same workspace, before anything was answered: the CLI replays the page's answers.
    twin = tmp_path / "twin"
    shutil.copytree(recorded.root, twin)
    run_id, given = _calibrate_over_http(recorded, client)
    answers = dict(given)

    def call(*arguments: str) -> dict[str, Any]:
        code = cli([*arguments, "--workspace", str(twin), "--format", "json"], clock=recorded.clock)
        out = capsys.readouterr().out
        assert code == 0, out
        document: dict[str, Any] = json.loads(out)["data"]
        return document

    twin_run = call(
        "assessment", "start", "--scoring", "machine", "--modality", "text", "--modality", "audio"
    )["run_id"]
    replayed: list[str] = []
    while True:
        served = call("assessment", "next", "--run", twin_run)
        if "content_id" not in served:
            break
        replayed.append(served["content_id"])
        call(
            "assessment",
            "record",
            "--run",
            twin_run,
            "--content",
            served["content_id"],
            "--response",
            answers[served["content_id"]],
        )
    call("assessment", "finalize", "--run", twin_run)

    assert replayed == [content_id for content_id, _ in given]
    page = client.get(f"/runs/{run_id}").data
    command_line = call("assessment", "report", "--run", twin_run)
    fields = (
        "dimension",
        "status",
        "tasks_used",
        "confidence",
        "estimated_level",
        "credible_low",
        "credible_high",
        "posterior_mean",
        "uncertainty",
        "unavailable_reason",
    )
    assert [{name: entry[name] for name in fields} for entry in page["dimensions"]] == [
        {name: entry[name] for name in fields} for entry in command_line["dimensions"]
    ]


def _counts(workspace: PolishWorkspace) -> tuple[int, ...]:
    with open_reader(workspace.paths) as database:
        return tuple(
            int(database.scalar(f"SELECT count(*) FROM {table}"))
            for table in (
                "assessment_runs",
                "assessment_run_tasks",
                "assessment_results",
                "assessment_task_plays",
            )
        )


def test_every_keyed_operation_retried_with_its_key_is_one_operation(
    recorded: PolishWorkspace, client: Client
) -> None:
    """A lost response is retried with the same key and body, and nothing is done twice."""

    def twice(path: str, body: dict[str, Any]) -> dict[str, Any]:
        first = client.post(path, body)
        before = _counts(recorded)
        again = client.post(path, body)
        assert _counts(recorded) == before, path
        assert again.status == first.status == 200, again.payload
        for volatile in ("warnings",):
            first.data.pop(volatile, None)
            again.data.pop(volatile, None)
        if "content_id" in first.data:
            # A replayed serve is the same task, said to be handed back.
            assert again.data["content_id"] == first.data["content_id"]
        else:
            assert again.data == first.data, path
        return first.data

    start = {"dimensions": ["listening"], "modalities": ["audio"], "scoring": "machine"}
    run_id = twice("/runs", {**start, "idempotency_key": key()})["run_id"]
    task = twice(f"/runs/{run_id}/tasks", {"idempotency_key": key()})
    twice(f"/runs/{run_id}/tasks/{task['content_id']}/plays", {"idempotency_key": key()})
    choices = (task["presentation"] or {}).get("choices") or [{"value": "x"}]
    twice(
        f"/runs/{run_id}/results",
        {
            "content_id": task["content_id"],
            "response": choices[0]["value"],
            "idempotency_key": key(),
        },
    )
    twice(f"/runs/{run_id}/finalization", {"reason": "completed", "idempotency_key": key()})


def test_a_replayed_serve_whose_task_was_answered_says_so(
    recorded: PolishWorkspace, client: Client
) -> None:
    """Otherwise the page would put an answered question back in front of the learner."""

    run_id = _start_listening(client)
    serve_key = key()
    task = client.post(f"/runs/{run_id}/tasks", {"idempotency_key": serve_key}).data
    choices = (task["presentation"] or {}).get("choices") or [{"value": "x"}]
    client.post(
        f"/runs/{run_id}/results",
        {
            "content_id": task["content_id"],
            "response": choices[0]["value"],
            "idempotency_key": key(),
        },
    )

    replayed = client.post(f"/runs/{run_id}/tasks", {"idempotency_key": serve_key}).data

    assert replayed["content_id"] == task["content_id"]
    assert replayed["status"] == "answered"
