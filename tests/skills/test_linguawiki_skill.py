from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

import duckdb
import pytest

from linguawiki.contracts import DELIVERY_STAGE

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents" / "skills" / "linguawiki" / "SKILL.md"


def clean_subprocess_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE")}


def test_smoke_skill_command_works_in_isolated_directory(tmp_path: Path) -> None:
    executable = shutil.which("linguawiki", path=sysconfig.get_path("scripts"))
    assert executable is not None, "the installed linguawiki console script was not on PATH"
    result = subprocess.run(
        [executable, "status", "--format", "json"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
        env=clean_subprocess_environment(),
    )

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["stage"] == DELIVERY_STAGE
    assert payload["data"]["persistence"] == "available"


def test_skill_workspace_lifecycle_runs_through_the_installed_console_script(
    tmp_path: Path,
) -> None:
    """The skill's documented Stage 1 commands must work outside the core repository."""

    executable = shutil.which("linguawiki", path=sysconfig.get_path("scripts"))
    assert executable is not None
    workspace = tmp_path / "PolishLinguaWiki"
    environment = clean_subprocess_environment()

    def run(*arguments: str) -> dict[str, object]:
        result = subprocess.run(
            [executable, *arguments, "--format", "json"],
            cwd=tmp_path,
            check=True,
            text=True,
            capture_output=True,
            env=environment,
        )
        payload: dict[str, object] = json.loads(result.stdout)
        assert payload["ok"] is True
        return payload

    initialized = run(
        "workspace",
        "init",
        str(workspace),
        "--backup-root",
        str(tmp_path / "backups"),
        "--history",
        "git-wiki",
    )
    doctor = run("workspace", "doctor", "--workspace", str(workspace))
    checked = run("db", "check", "--workspace", str(workspace))

    assert initialized["command"] == "workspace.init"
    assert doctor["data"]["ok"] is True  # type: ignore[index]
    assert checked["data"]["ok"] is True  # type: ignore[index]


def test_repository_skill_validator() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_skills.py")],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=clean_subprocess_environment(),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_skill_uses_codex_repository_discovery_location() -> None:
    assert SKILL.is_file()
    assert not (ROOT / "skills" / "linguawiki" / "SKILL.md").exists()


def test_the_release_gate_finds_uv_without_help() -> None:
    """The gate must not depend on uv happening to be on the caller's PATH."""

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_clean_environment as gate
    finally:
        sys.path.pop(0)

    discovered = gate.resolve_uv(None)

    assert discovered is not None, "uv was not discoverable from PATH or .tools"
    assert discovered.is_absolute()
    assert gate.resolve_uv("/nonexistent/uv") is None
    assert gate.resolve_uv(str(discovered)) == discovered
    environment = gate.child_environment(discovered)
    assert environment["PATH"].split(os.pathsep)[0] == str(discovered.parent)


@pytest.mark.external
def test_the_released_wheel_initializes_a_workspace_in_a_clean_environment() -> None:
    """Release gate: the built artifact must work with no source tree on the path.

    Marked `external` because it needs `uv` and a package index; run it with
    `uv run python scripts/verify.py --clean-environment` before publishing.
    """

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_clean_environment.py")],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=clean_subprocess_environment(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Clean-environment installation check passed" in result.stdout


@pytest.mark.external
def test_a_duckdb_version_change_preserves_learner_data() -> None:
    """Release gate for a `duckdb` bump: run it before changing the pin.

    Marked `external` because it installs two DuckDB versions from an index. The check
    itself is what proves cross-version compatibility; the retained Parquet fixtures
    only pin the export shape.
    """

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_duckdb_upgrade.py"),
            "--from",
            "1.4.1",
            "--to",
            duckdb.__version__,
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=clean_subprocess_environment(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "native open, portable export" in result.stdout


def test_the_upgrade_gate_seeds_every_table_of_the_current_schema() -> None:
    """The gate must judge an upgrade on learner data, not bootstrap metadata."""

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import duckdb_upgrade_support as support
    finally:
        sys.path.pop(0)

    from linguawiki.db.schema import TABLE_ORDER
    from linguawiki.services import workspace as workspace_service

    with tempfile.TemporaryDirectory(prefix="linguawiki-seed-") as raw:
        sandbox = Path(raw)
        root = sandbox / "PolishLinguaWiki"
        workspace_service.initialize(
            workspace_service.InitOptions(
                path=root, backup_root=sandbox / "backups", name="Polish LinguaWiki"
            ),
            clock=support.Clock(),
        )
        support.seed(root)
        digest = support.workspace_digest(root)

    tables = digest["tables"]
    assert set(tables) == set(TABLE_ORDER)
    empty = sorted(name for name, entry in tables.items() if entry["rows"] == 0)
    assert empty == [], f"these tables would prove nothing about an upgrade: {empty}"
    # The seed drives the real services, so identities are generated rather than fixed.
    # What the gate compares is that they exist and are singular.
    assert len(digest["identities"]["user_ids"]) == 1
    assert len(digest["identities"]["track_ids"]) == 1
    assert digest["identities"]["pack_checksums"]
    assert digest["identities"]["content_hashes"]
    assert digest["identities"]["skill_estimates"]
    assert digest["identities"]["placement_state"]


@pytest.mark.parametrize(
    "divergence",
    ["rows", "sha256", "columns", "table-set", "identities"],
)
def test_the_upgrade_gate_comparison_catches_every_divergence(divergence: str) -> None:
    """Aggregate totals hid these; the comparison must fail on each one."""

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from check_duckdb_upgrade import _compare
    finally:
        sys.path.pop(0)

    expected = {
        "tables": {
            "users": {"rows": 1, "sha256": "a" * 64, "columns": ["user_id"]},
            "jobs": {"rows": 1, "sha256": "b" * 64, "columns": ["job_id"]},
        },
        "identities": {"user_ids": ["usr_1"]},
    }
    actual = json.loads(json.dumps(expected))
    if divergence == "rows":
        actual["tables"]["users"]["rows"] = 0
    elif divergence == "sha256":
        actual["tables"]["users"]["sha256"] = "c" * 64
    elif divergence == "columns":
        actual["tables"]["users"]["columns"] = ["uid"]
    elif divergence == "table-set":
        del actual["tables"]["jobs"]
    else:
        actual["identities"]["user_ids"] = ["usr_2"]

    assert _compare("restore", expected, expected) == []
    assert _compare("restore", expected, actual) != []


def test_the_upgrade_gate_knows_which_direction_it_is_testing() -> None:
    """Only one direction is a DuckDB guarantee, so the gate has to order the releases.

    Requiring native open of a newer file by an older release made the gate pass by
    luck: with identical seeded data, DuckDB 1.4.1 opening a 1.5.5 database either
    worked or raised `INTERNAL Error: Failed to load metadata pointer`, three failures
    and two passes across five runs. A downgrade is now judged on the portable export,
    which is what a learner would actually restore from.
    """

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from check_duckdb_upgrade import release
    finally:
        sys.path.pop(0)

    assert release("1.4.1") < release("1.5.5")
    assert release("1.5.5") < release("1.6.0")
    assert not release("1.5.5") < release("1.4.1")
    # A release named with fewer or non-numeric components still orders, because the
    # version is an operator's argument and dying on it would test nothing at all.
    assert release("1.6") < release("1.6.1")
    assert release("1.6.0rc1") < release("1.6.1")
