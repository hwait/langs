"""Behavioural tests for the Stage 3 skill slices.

These assert observable effects rather than wording: a skill's promises are only worth
anything if the CLI it documents actually refuses what the skill says it refuses. The two
promises tested hardest are the ones a learner would be misled by -- no mastery without
evidence, and no global level nobody measured.

They drive the installed console script, so they also prove the documented commands exist
outside the source tree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sysconfig
from pathlib import Path
from typing import Any

import pytest

from tests.skills.test_linguawiki_skill import clean_subprocess_environment

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".agents" / "skills"
PILOT_PACK = ROOT / "language-packs" / "pl-pilot"
ITEM = "pl.lex.dworzec"


@pytest.fixture(scope="module")
def executable() -> str:
    found = shutil.which("linguawiki", path=sysconfig.get_path("scripts"))
    assert found is not None, "the installed linguawiki console script was not on PATH"
    return found


class Runner:
    """Drives the console script the way a skill does, and keeps the envelope."""

    def __init__(self, executable: str, root: Path) -> None:
        self._executable = executable
        self._root = root
        self.workspace = root / "PolishLinguaWiki"

    def __call__(self, *arguments: str, expect: int = 0) -> dict[str, Any]:
        result = subprocess.run(
            [self._executable, *arguments, "--format", "json"],
            cwd=self._root,
            check=False,
            text=True,
            capture_output=True,
            env=clean_subprocess_environment(),
        )
        assert result.returncode == expect, result.stdout + result.stderr
        stream = result.stdout if expect == 0 else result.stderr
        payload: dict[str, Any] = json.loads(stream)
        return payload

    def scoped(self, *arguments: str, expect: int = 0) -> dict[str, Any]:
        return self(*arguments, "--workspace", str(self.workspace), expect=expect)


@pytest.fixture(scope="module")
def onboarded(executable: str, tmp_path_factory: pytest.TempPathFactory) -> Runner:
    """A workspace taken through the documented init sequence, once for the module."""

    sandbox = tmp_path_factory.mktemp("skills")
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
    runner.scoped("onboard", "start", "--mode", "declared-level", "--declared-level", "A2")
    return runner


def test_the_documented_init_sequence_leaves_a_consistent_database(
    onboarded: Runner,
) -> None:
    checked = onboarded.scoped("db", "check")

    assert checked["data"]["ok"] is True


def test_a_declared_level_is_a_hypothesis_and_never_a_measurement(
    onboarded: Runner,
) -> None:
    """The init skill's central promise, asserted against what the CLI actually wrote."""

    estimates = onboarded.scoped("estimate", "show")["data"]

    assert estimates["estimates"], "onboarding must seed a profile"
    for entry in estimates["estimates"]:
        assert entry["evidence_count"] == 0
        assert entry["estimate_status"] in ("provisional", "not-tested")
        assert entry["basis"] in ("declared-hypothesis", "self-report")
        assert entry["confidence_label"] in ("low", "not-tested")


def test_a_declared_level_marks_nothing_known(onboarded: Runner) -> None:
    item = onboarded.scoped("knowledge", "get", ITEM)["data"]

    assert item["state"] is None or item["state"]["stage"] == "unseen"


def test_a_global_level_is_never_offered_unasked(onboarded: Runner) -> None:
    plain = onboarded.scoped("estimate", "show")["data"]
    asked = onboarded.scoped("estimate", "show", "--summary")

    assert plain["summary_level"] is None
    # Nothing has been measured, so even an explicit request gets no number -- and says why.
    assert asked["data"]["summary_level"] is None
    assert any("no dimension has evidence" in warning for warning in asked["warnings"])


def test_recognition_evidence_cannot_produce_a_production_stage(
    onboarded: Runner,
) -> None:
    """The exit gate, through the console script a skill would call."""

    for index in range(6):
        onboarded.scoped(
            "evidence",
            "record",
            "--task-type",
            "objective",
            "--modality",
            "text",
            "--score",
            "1.0",
            "--target",
            ITEM,
            "--dimension",
            "reading",
            "--context",
            f"objective:{index}",
        )

    item = onboarded.scoped("knowledge", "get", ITEM)["data"]

    assert item["state"]["stage"] == "recognized"
    assert item["state"]["evidence_ceiling"] == "recognized"


def test_a_skill_cannot_flag_its_way_to_a_stronger_claim(onboarded: Runner) -> None:
    """The refusal names the claims the task type can produce, so there is a next step."""

    failure = onboarded.scoped(
        "evidence",
        "record",
        "--task-type",
        "objective",
        "--modality",
        "text",
        "--score",
        "1.0",
        "--target",
        ITEM,
        "--claim",
        "spontaneous-production",
        expect=2,
    )

    assert failure["error"]["code"] == "evidence_modality_incompatible"
    assert "speech" in failure["error"]["message"]


def test_an_estimate_reports_its_range_and_its_status_together(
    onboarded: Runner,
) -> None:
    reading = next(
        entry
        for entry in onboarded.scoped("estimate", "show")["data"]["estimates"]
        if entry["dimension"] == "reading"
    )

    assert reading["estimate_status"] in ("provisional", "estimated")
    assert reading["level_low"] is not None
    assert reading["level_high"] is not None
    assert reading["confidence_label"] in ("low", "medium", "high")


def test_an_estimate_change_is_explained_by_its_own_evidence(
    onboarded: Runner,
) -> None:
    history = onboarded.scoped("estimate", "history", "--dimension", "reading")["data"]

    assert history["total"] >= 1
    snapshot = history["snapshots"][0]
    assert snapshot["reason"]
    assert snapshot["evidence_ids"]
    assert snapshot["calculation_version"]


def test_a_context_bundle_reports_its_bounds_to_the_agent_using_it(
    onboarded: Runner,
) -> None:
    bundle = onboarded.scoped("context", "session")["data"]

    assert bundle["sections"]
    assert bundle["estimated_tokens"] <= bundle["token_limit"]
    assert "omissions" in bundle
    assert bundle["provenance"]["pack_key"] == "pl-pilot"


def test_one_correct_answer_cannot_close_a_recurring_error(onboarded: Runner) -> None:
    recorded = onboarded.scoped(
        "errors",
        "record",
        "--category",
        "case-government",
        "--signature",
        "szukam bilet",
        "--description",
        "Accusative where the verb governs the genitive.",
        "--target",
        "pl.lex.bilet",
    )["data"]
    onboarded.scoped(
        "evidence",
        "record",
        "--task-type",
        "extended-productive",
        "--modality",
        "writing",
        "--score",
        "1.0",
        "--target",
        "pl.lex.bilet",
        "--dimension",
        "writing",
        "--claim",
        "spontaneous-production",
        "--context",
        "essay:one",
    )

    after = onboarded.scoped("errors", "show", recorded["error_id"])["data"]

    assert after["status"] != "resolved"
    assert after["outstanding"], "the policy must say what it is still waiting for"


@pytest.mark.parametrize(
    "skill",
    ["linguawiki", "linguawiki-init", "linguawiki-assess"],
)
def test_every_command_a_skill_documents_exists(skill: str) -> None:
    """A skill that names a command the CLI does not have is a broken skill."""

    import re

    from linguawiki.cli import _parser

    groups = set()
    parser = _parser()
    for action in parser._subparsers._group_actions:
        groups.update(action.choices)
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    named = {
        match.group(1)
        for match in re.finditer(r"^\s*linguawiki\s+([a-z-]+)\b", text, flags=re.MULTILINE)
    }

    assert named, f"{skill} documents no commands"
    assert named <= groups, f"{skill} names commands the CLI does not have: {named - groups}"


def test_the_learner_model_commands_are_documented_by_the_umbrella_skill() -> None:
    """A command nobody documents is a command no agent will find."""

    text = (SKILLS / "linguawiki" / "SKILL.md").read_text(encoding="utf-8")

    for group in ("knowledge", "evidence", "errors", "estimate", "context"):
        assert f"linguawiki {group}" in text, f"{group} is undocumented"
