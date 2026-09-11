"""The Stage 4 command surface, and the first Polish vertical end to end.

Driven through `run()` rather than the services, because the CLI is the boundary a skill
actually uses: the envelope, the exit code, and the refusal message are the contract.
The last test in this file is the stage's own exit gate -- plan, lesson, close, dashboard
in one workspace, with no direct SQL anywhere in it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from linguawiki.cli import EXIT_ERROR, _command_name, _parser, run
from tests.conftest import PolishWorkspace


def _json(capsys: pytest.CaptureFixture[str], *, stream: str = "out") -> dict[str, Any]:
    captured = capsys.readouterr()
    payload: dict[str, Any] = json.loads(getattr(captured, stream))
    return payload


def _run(workspace: PolishWorkspace, *arguments: str) -> int:
    return run(
        [*arguments, "--workspace", str(workspace.root), "--format", "json"],
        clock=workspace.clock,
    )


def _onboard(workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(workspace, "onboard", "start", "--declared-level", "A2") == 0
    capsys.readouterr()
    assert _run(workspace, "onboard", "finalize") == 0
    capsys.readouterr()


def _batch_file(
    directory: Path,
    block: dict[str, Any],
    *,
    sequence: int = 1,
    key: str = "batch-1",
    events: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "sequence": sequence,
        "idempotency_key": key,
        "block": block["block_id"],
        "events": events
        if events is not None
        else [
            {
                "event_id": f"evt_01ARZ3NDEKTSV4RRFFQ69G5F{sequence:02d}",
                "kind": "attempt.observed",
                "occurred_at": "2026-01-01T09:00:00Z",
                "payload": {
                    "task_type": "short-response",
                    "modality": block["modality"],
                    "dimension": block["dimension"],
                    "target": (block["targets"][0]["content_id"] if block["targets"] else None),
                    "score": 1.0,
                    "response": "Jestem na dworcu.",
                    "assessor_kind": "ai",
                },
            }
        ],
    }
    path = directory / f"batch-{sequence}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["plan", "create"], "plan.create"),
        (["plan", "show"], "plan.show"),
        (["session", "start"], "session.start"),
        (["session", "log"], "session.log"),
        (["session", "staged"], "session.staged"),
        (["session", "close"], "session.close"),
        (["session", "partial-close"], "session.partial-close"),
        (["session", "abandon"], "session.abandon"),
        (["session", "resume"], "session.resume"),
        (["session", "recover"], "session.recover"),
        (["session", "ingest-package"], "session.ingest-package"),
        (["wiki", "build"], "wiki.build"),
    ],
)
def test_every_new_command_names_itself_in_the_envelope(argv: list[str], expected: str) -> None:
    assert _command_name(argv, _parser()) == expected


def test_the_stage_s_required_command_surface_exists() -> None:
    """The stage doc's command list, checked against the parser rather than the prose."""

    groups = _parser().parse_args
    required = {
        ("plan", "create"),
        ("plan", "show"),
        ("session", "start"),
        ("session", "status"),
        ("session", "log"),
        ("session", "resume"),
        ("session", "close"),
        ("session", "partial-close"),
        ("session", "abandon"),
        ("session", "ingest-package"),
        ("wiki", "build"),
    }
    for group, action in sorted(required):
        arguments = [group, action]
        if action == "create":
            arguments += ["--minutes", "60"]
        if action in ("log", "ingest-package"):
            arguments += ["--input", "payload.json"]
        if action == "recover":
            arguments += ["--from", "ses_x"]
        parsed = groups([*arguments, "--workspace", "."])
        assert parsed.group == group
        assert parsed.action == action


def test_a_plan_arrives_as_an_envelope_with_every_block_explained(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    _onboard(polish_workspace, capsys)

    assert _run(polish_workspace, "plan", "create", "--minutes", "60") == 0
    payload = _json(capsys)

    assert payload["ok"] is True
    assert payload["command"] == "plan.create"
    plan = payload["data"]
    assert plan["status"] == "planned"
    assert plan["planner_version"] == "planner.v1"
    roles = [block["role"] for block in plan["blocks"]]
    assert roles[0] == "warm-up"
    assert roles[-1] == "closure"
    assert all(block["rationale"] for block in plan["blocks"])
    assert all(omission["reason"] for omission in plan["omissions"])


def test_an_unrunnable_mode_is_refused_through_the_error_envelope(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    _onboard(polish_workspace, capsys)
    # A fresh track has nothing to review: `--mode review` permits only `rich-review`,
    # which has no due item yet. The refusal names that rather than substituting a mode.
    assert _run(polish_workspace, "plan", "create", "--minutes", "60", "--mode", "review") == (
        EXIT_ERROR
    )
    payload = _json(capsys, stream="err")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "session_mode_unavailable"
    assert payload["error"]["details"]


def test_a_batch_travels_as_a_file_and_is_idempotent_through_the_cli(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    _onboard(polish_workspace, capsys)
    assert _run(polish_workspace, "plan", "create", "--minutes", "60") == 0
    plan = _json(capsys)["data"]
    assert _run(polish_workspace, "session", "start") == 0
    capsys.readouterr()
    block = next(entry for entry in plan["blocks"] if entry["role"] == "core")
    path = _batch_file(tmp_path, block)

    assert _run(polish_workspace, "session", "log", "--input", path) == 0
    first = _json(capsys)["data"]
    assert _run(polish_workspace, "session", "log", "--input", path) == 0
    second = _json(capsys)["data"]

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["batch_id"] == first["batch_id"]
    assert len(first["content_hash"]) == 64


def test_a_malformed_batch_is_refused_before_anything_is_stored(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    _onboard(polish_workspace, capsys)
    assert _run(polish_workspace, "plan", "create", "--minutes", "60") == 0
    capsys.readouterr()
    assert _run(polish_workspace, "session", "start") == 0
    capsys.readouterr()
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"sequence": 1, "idempotency_key": "k", "events": []}), "utf-8")

    assert _run(polish_workspace, "session", "log", "--input", str(path)) == EXIT_ERROR
    payload = _json(capsys, stream="err")

    assert payload["ok"] is False
    assert _run(polish_workspace, "session", "staged") == 0
    assert _json(capsys)["data"]["total"] == 0


def test_the_whole_vertical_runs_through_the_cli_and_ends_with_a_dashboard(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """The stage's exit gate: plan -> lesson -> close -> dashboard, no direct SQL."""

    _onboard(polish_workspace, capsys)
    assert _run(polish_workspace, "plan", "create", "--minutes", "60") == 0
    plan = _json(capsys)["data"]
    assert _run(polish_workspace, "session", "start") == 0
    capsys.readouterr()
    block = next(entry for entry in plan["blocks"] if entry["role"] == "core")
    events = [
        {
            "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FE0",
            "kind": "attempt.observed",
            "occurred_at": "2026-01-01T09:00:00Z",
            "payload": {
                "task_type": "short-response",
                "modality": block["modality"],
                "dimension": block["dimension"],
                "target": block["targets"][0]["content_id"] if block["targets"] else None,
                "score": 1.0,
                "assessor_kind": "ai",
            },
        },
        {
            "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FE1",
            "kind": "correction.given",
            "occurred_at": "2026-01-01T09:05:00Z",
            "payload": {
                "category": "case-government",
                "signature": "szukam bilet",
                "description": "Accusative where the verb governs the genitive.",
                "learner_form": "szukam bilet",
                "corrected_form": "szukam biletu",
            },
        },
        {
            "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FE2",
            "kind": "follow_up",
            "occurred_at": "2026-01-01T09:08:00Z",
            "payload": {"kind": "practice", "action": "Drill the genitive next session."},
        },
    ]

    assert (
        _run(
            polish_workspace,
            "session",
            "log",
            "--input",
            _batch_file(tmp_path, block, events=events),
        )
        == 0
    )
    capsys.readouterr()
    assert _run(polish_workspace, "session", "status") == 0
    status = _json(capsys)["data"]
    assert status["staged_events"] == 3

    assert (
        _run(
            polish_workspace,
            "session",
            "close",
            "--outcome",
            "completed",
            "--actual-minutes",
            "58",
            "--fatigue",
            "medium",
            "--summary",
            "First vertical run",
        )
        == 0
    )
    close = _json(capsys)["data"]
    assert close["attempts_written"] == 1
    assert close["errors_written"] == 1
    assert close["followups_written"] == 1
    assert close["projection_stale"] is True

    assert _run(polish_workspace, "wiki", "build", "--view", "dashboard") == 0
    build = _json(capsys)["data"]
    assert build["files"] == ["wiki/learner/dashboard.md"]
    assert build["stale_before"] is True

    page = (polish_workspace.root / "wiki" / "learner" / "dashboard.md").read_text("utf-8")
    assert page.startswith("---\ngenerated: true")
    assert f"entity_id: {polish_workspace.track_id}" in page
    assert "projection_version: 1" in page
    assert build["source_event_id"] in page
    assert "## Today's plan" in page
    assert "## Latest session" in page
    assert "## Due work" in page
    assert "## Active errors" in page
    assert "## Skill estimates" in page
    # The dashboard shows the error *pattern*, never the occurrence history that holds
    # the learner's own words.
    assert "case-government" in page
    assert "szukam bilet" not in page

    assert _run(polish_workspace, "db", "check") == 0
    checked = _json(capsys)["data"]
    assert checked["ok"] is True


def test_a_build_records_a_hash_that_a_hand_edit_moves_away_from(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    """The projection's hash is stored so drift is detectable from the database side.

    A hand edit is not authoritative: it does not become state, it makes the files
    disagree with the recorded hash, and the next build overwrites it.
    """

    from linguawiki.services import wiki as wiki_service

    _onboard(polish_workspace, capsys)
    assert _run(polish_workspace, "wiki", "build") == 0
    first = _json(capsys)["data"]

    assert first["content_hash"] == wiki_service.projection_content_hash(polish_workspace.root)

    page = polish_workspace.root / "wiki" / "learner" / "dashboard.md"
    page.write_text(page.read_text("utf-8") + "\nThe learner is fluent.\n", encoding="utf-8")

    assert wiki_service.projection_content_hash(polish_workspace.root) != first["content_hash"], (
        "an edited projection must not still match the hash the database recorded"
    )

    assert _run(polish_workspace, "wiki", "build") == 0
    second = _json(capsys)["data"]

    assert second["stale_before"] is False, "a build clears staleness; nothing else does"
    assert "The learner is fluent." not in page.read_text("utf-8")
    assert second["content_hash"] == wiki_service.projection_content_hash(polish_workspace.root)


@pytest.mark.parametrize(
    ("minutes", "mode"),
    [(40, "mixed"), (60, "grammar"), (100, "mixed")],
)
def test_dogfooding_sessions_of_different_durations_and_modes_all_complete(
    capsys: pytest.CaptureFixture[str],
    polish_workspace: PolishWorkspace,
    tmp_path: Path,
    minutes: int,
    mode: str,
) -> None:
    """4.7's three sessions, as a test rather than as a private workspace.

    The dogfood workspace itself is a learner's own repository and cannot live here --
    this repository holds no learner state. What can live here is the property that
    matters: a declared-A2 Polish track completes the whole flow at each duration and
    mode the plan names.
    """

    _onboard(polish_workspace, capsys)
    assert _run(polish_workspace, "plan", "create", "--minutes", str(minutes), "--mode", mode) == 0
    plan = _json(capsys)["data"]
    assert plan["mode"] == mode
    assert _run(polish_workspace, "session", "start") == 0
    capsys.readouterr()
    for index, block in enumerate(
        [entry for entry in plan["blocks"] if entry["role"] == "core"], start=1
    ):
        assert (
            _run(
                polish_workspace,
                "session",
                "log",
                "--input",
                _batch_file(tmp_path, block, sequence=index, key=f"batch-{index}"),
            )
            == 0
        )
        capsys.readouterr()

    assert _run(polish_workspace, "session", "close", "--outcome", "completed") == 0
    close = _json(capsys)["data"]
    assert close["outcome"] == "completed"
    assert close["attempts_written"] >= 1
    assert _run(polish_workspace, "wiki", "build") == 0
    capsys.readouterr()
    assert _run(polish_workspace, "db", "check") == 0
    assert _json(capsys)["data"]["ok"] is True
