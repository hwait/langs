from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from linguawiki import resources
from linguawiki.repository_policy import GENERATED_START
from linguawiki.services.privacy import (
    candidate_violation,
    check_privacy,
    missing_ignore_rules,
    required_ignore_rules,
    unsafe_top_level,
    workspace_policy,
)

TEMPLATE_IGNORE = (
    Path(__file__).resolve().parents[2] / "templates" / "learner-workspace" / ".gitignore.j2"
)


def test_required_rules_cover_databases_media_and_workspace_directories() -> None:
    rules = required_ignore_rules(workspace_policy())

    for expected in (
        "*.duckdb",
        "*.wav",
        "*.parquet",
        "backups/",
        "exports/",
        "imports/",
        "artifacts/",
        "data/",
        "drafts/",
        "**/transcripts/raw/",
    ):
        assert expected in rules


def test_the_workspace_template_satisfies_every_required_rule() -> None:
    ignore = TEMPLATE_IGNORE.read_text(encoding="utf-8")

    assert missing_ignore_rules(ignore, workspace_policy()) == ()


def test_missing_rules_and_generated_markers_are_reported() -> None:
    policy = workspace_policy()
    stripped = "\n".join(
        line
        for line in TEMPLATE_IGNORE.read_text(encoding="utf-8").splitlines()
        if line not in {"*.duckdb", "data/", GENERATED_START}
    )

    missing = missing_ignore_rules(stripped, policy)

    assert "*.duckdb" in missing
    assert "data/" in missing
    assert GENERATED_START in missing


def test_a_workspace_without_git_falls_back_to_a_filesystem_listing(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "index.md").write_text("safe", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "linguawiki.duckdb").write_bytes(b"private")

    report = check_privacy(tmp_path)

    assert report.source == "filesystem"
    assert report.violations == ()
    assert report.inspection_failure is None
    assert "not a Git repository" in report.warnings[0]
    assert ".gitignore is missing from this workspace" in report.warnings


def test_the_packaged_policy_is_the_one_used_for_workspaces() -> None:
    assert resources.privacy_policy_path().is_file()
    assert workspace_policy().version >= 1


@pytest.mark.parametrize(
    "candidate",
    [
        "wiki/index.md",
        "wiki/learner/progress.md",
        ".agents/skills/linguawiki/SKILL.md",
        "linguawiki.toml",
        "linguawiki.lock",
        "uv.lock",
        "pyproject.toml",
        "AGENTS.md",
        ".gitignore",
    ],
)
def test_generated_workspace_paths_are_git_safe(candidate: str) -> None:
    assert unsafe_top_level(PurePosixPath(candidate)) is None


@pytest.mark.parametrize(
    "candidate",
    ["learner-notes.txt", "scratch/todo.md", "secrets.env", "src/linguawiki/contracts.py"],
)
def test_unexpected_top_level_entries_are_rejected(candidate: str) -> None:
    """The allowlist is the half of the contract that catches artifacts we cannot name."""

    assert unsafe_top_level(PurePosixPath(candidate)) == "outside the Git-safe top level"
    assert candidate_violation(PurePosixPath(candidate), workspace_policy()) is not None


def test_a_private_artifact_reports_its_private_reason_first() -> None:
    """A recognised private artifact keeps its specific reason, not the generic one."""

    assert (
        candidate_violation(PurePosixPath("recording.wav"), workspace_policy())
        == "private database or media extension"
    )
