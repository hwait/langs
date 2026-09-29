"""Behavioural tests for the `linguawiki-learn` skill.

The skill makes promises about what the CLI refuses. These drive the installed console
script and check that the refusals are real, because a skill whose promises are only
prose is a skill that will teach an agent to do the wrong thing confidently.

The two hardest promises: a lesson's observations cannot be recorded outside a session,
and a close credits its staged work exactly once however many times it is called.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.skills.test_learner_model_skills import Runner, executable  # noqa: F401
from tests.skills.test_linguawiki_skill import clean_subprocess_environment  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".agents" / "skills"
PILOT_PACK = ROOT / "language-packs" / "pl-pilot"
SKILL = SKILLS / "linguawiki-learn"


@pytest.fixture(scope="module")
def teaching(executable: str, tmp_path_factory: pytest.TempPathFactory) -> Runner:  # noqa: F811
    """A workspace taken to the point where a lesson can be planned."""

    sandbox = tmp_path_factory.mktemp("session-skill")
    runner = Runner(executable, sandbox)
    runner(
        "workspace",
        "init",
        str(runner.workspace),
        "--backup-root",
        str(sandbox / "backups"),
        "--name",
        "Polish LinguaWiki",
        "--timezone",
        "Europe/Warsaw",
    )
    runner.scoped("pack", "install", str(PILOT_PACK))
    runner.scoped(
        "user",
        "create",
        "--name",
        "Синтетический Учащийся",
        "--timezone",
        "Europe/Warsaw",
        "--native",
        "ru",
        "--support",
        "en",
    )
    preferences = sandbox / "preferences.json"
    preferences.write_text(
        json.dumps(
            {
                "weekly_minutes": 210,
                "correction_mode": "accuracy",
                "voice_available": True,
                "transcript_retention_consent": True,
            }
        ),
        encoding="utf-8",
    )
    runner.scoped(
        "track",
        "create",
        "--target-language",
        "pl",
        "--framework",
        "cefr",
        "--declared-level",
        "A2",
        "--target-level",
        "B1",
        "--input",
        str(preferences),
    )
    runner.scoped("onboard", "start", "--declared-level", "A2")
    runner.scoped("onboard", "finalize")
    return runner


def batch_file(runner: Runner, block: dict[str, Any], **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "sequence": 1,
        "idempotency_key": "skill-batch-1",
        "block": block["block_id"],
        "events": [
            {
                "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FF0",
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
            }
        ],
    }
    payload.update(overrides)
    path = Path(runner.workspace).parent / f"{payload['idempotency_key']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_the_documented_loop_runs_from_plan_to_dashboard(teaching: Runner) -> None:
    plan = teaching.scoped("plan", "create", "--minutes", "60", "--idempotency-key", "loop")["data"]
    teaching.scoped("session", "start")
    block = next(entry for entry in plan["blocks"] if entry["role"] == "core")

    teaching.scoped("session", "log", "--input", batch_file(teaching, block))
    staged = teaching.scoped("session", "staged")["data"]
    close = teaching.scoped("session", "close", "--outcome", "completed")["data"]
    built = teaching.scoped("wiki", "build", "--view", "dashboard")["data"]

    assert staged["total"] == 1
    assert close["attempts_written"] == 1
    assert close["evidence_written"] >= 1
    assert built["files"] == ["wiki/learner/dashboard.md"]
    assert teaching.scoped("db", "check")["data"]["ok"] is True


def test_a_lesson_observation_cannot_be_recorded_outside_a_session(teaching: Runner) -> None:
    """The skill says there is no supported way to do this. There must not be one."""

    failure = teaching.scoped(
        "evidence",
        "record",
        "--task-type",
        "short-response",
        "--modality",
        "writing",
        "--score",
        "1.0",
        "--dimension",
        "writing",
        "--origin",
        "session",
        expect=2,
    )

    assert failure["error"]["code"] in ("unknown_attempt_origin", "invalid_arguments")


def test_a_close_called_twice_credits_the_work_once(teaching: Runner) -> None:
    plan = teaching.scoped("plan", "create", "--minutes", "40", "--idempotency-key", "twice")[
        "data"
    ]
    teaching.scoped("session", "start", "--session", plan["session_id"])
    block = next(entry for entry in plan["blocks"] if entry["role"] == "core")
    teaching.scoped(
        "session",
        "log",
        "--input",
        batch_file(teaching, block, idempotency_key="skill-batch-2"),
        "--session",
        plan["session_id"],
    )
    before = teaching.scoped("evidence", "list", "--limit", "500")["data"]["total"]

    first = teaching.scoped(
        "session", "close", "--outcome", "completed", "--session", plan["session_id"]
    )["data"]
    second = teaching.scoped(
        "session", "close", "--outcome", "completed", "--session", plan["session_id"]
    )["data"]
    after = teaching.scoped("evidence", "list", "--limit", "500")["data"]["total"]

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["finalization_id"] == first["finalization_id"]
    assert after - before == first["evidence_written"]


def test_an_interrupted_session_reports_what_it_holds(teaching: Runner) -> None:
    plan = teaching.scoped("plan", "create", "--minutes", "40", "--idempotency-key", "resumed")[
        "data"
    ]
    teaching.scoped("session", "start", "--session", plan["session_id"])
    block = next(entry for entry in plan["blocks"] if entry["role"] == "core")
    teaching.scoped(
        "session",
        "log",
        "--input",
        batch_file(teaching, block, idempotency_key="skill-batch-3"),
        "--session",
        plan["session_id"],
    )

    resumed = teaching.scoped("session", "resume", "--session", plan["session_id"])["data"]

    assert resumed["status"] == "active"
    assert resumed["staged_events"] == 1
    assert resumed["last_batch_sequence"] == 1
    assert "session log" in resumed["next_actions"]
    teaching.scoped("session", "abandon", "--session", plan["session_id"])


def test_every_command_the_learning_skill_documents_exists() -> None:
    from linguawiki.cli import _parser

    groups: set[str] = set()
    parser = _parser()
    for action in parser._subparsers._group_actions:
        groups.update(action.choices)
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    named = {
        match.group(1)
        for match in re.finditer(r"^\s*linguawiki\s+([a-z-]+)\b", text, flags=re.MULTILINE)
    }

    assert named, "the learning skill documents no commands"
    assert named <= groups, f"the skill names commands the CLI lacks: {named - groups}"


def test_the_skill_documents_every_staged_event_kind() -> None:
    """A kind the skill does not document is a kind no agent will ever flush."""

    from linguawiki.services import sessions as session_service

    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    reference = (SKILL / "references" / "session-contract.md").read_text(encoding="utf-8")

    for kind in session_service.MATERIALIZED_KINDS:
        assert kind in text or kind in reference, f"{kind} is undocumented"


def test_the_umbrella_skill_routes_lessons_to_the_learning_skill() -> None:
    text = (SKILLS / "linguawiki" / "SKILL.md").read_text(encoding="utf-8")

    assert "$linguawiki-learn" in text
    assert "linguawiki plan" in text or "plan create" in text


def test_the_references_the_skill_promises_are_all_present() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")

    for match in re.finditer(r"references/([a-z-]+\.md)", text):
        assert (SKILL / "references" / match.group(1)).is_file(), match.group(1)
