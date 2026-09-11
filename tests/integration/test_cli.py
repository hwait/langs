from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from linguawiki import resources
from linguawiki.cli import EXIT_ERROR, EXIT_REPORTED_FAILURE, _command_name, _parser, run
from linguawiki.contract_validation import validate_json_contract
from linguawiki.db import migrations as migration_module
from linguawiki.versions import skill_bundle_entries
from tests.conftest import SyntheticWorkspace
from tests.support.clocks import AdvancingClock, FixedClock

CORE_ROOT = Path(__file__).resolve().parents[2]


def _write_next_release_migration(directory: Path) -> str:
    """Add one plausible next-release migration beside the packaged sequence.

    Numbered from the current head and returning its identifier, so this fixture keeps
    working as stages are added rather than pinning whichever number happened to be next
    when it was written -- and so the expectation is read before the registry is patched.
    """

    migration_id = f"{migration_module.head_version() + 1:04d}_next_release_placeholder"
    (directory / f"{migration_id}.sql").write_text(
        "CREATE TABLE next_release_placeholder (id VARCHAR NOT NULL PRIMARY KEY);\n",
        encoding="utf-8",
    )
    return migration_id


def _json(capsys: pytest.CaptureFixture[str], *, stream: str = "out") -> dict[str, Any]:
    captured = capsys.readouterr()
    payload: dict[str, Any] = json.loads(getattr(captured, stream))
    return payload


def _initialize(root: Path, backup_root: Path, clock: AdvancingClock, *extra: str) -> int:
    return run(
        [
            "workspace",
            "init",
            str(root),
            "--backup-root",
            str(backup_root),
            "--name",
            "Polish LinguaWiki",
            "--timezone",
            "Europe/Warsaw",
            "--history",
            "git-wiki",
            "--format",
            "json",
            *extra,
        ],
        clock=clock,
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["status"], "status"),
        (["workspace", "init", "/tmp/x"], "workspace.init"),
        (["--format", "json", "db", "migrate"], "db.migrate"),
        (["skills", "check"], "skills.check"),
        (["workspace"], "workspace"),
        (["nonsense"], "unknown"),
    ],
)
def test_nested_command_names_are_reported(arguments: list[str], expected: str) -> None:
    assert _command_name(arguments, _parser()) == expected


def test_workspace_init_emits_a_valid_success_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], clock: AdvancingClock
) -> None:
    assert _initialize(tmp_path / "PolishLinguaWiki", tmp_path / "backups", clock) == 0

    payload = _json(capsys)
    validate_json_contract(
        "linguawiki.cli.success.v1", payload, schema_directory=resources.schema_directory()
    )
    assert payload["command"] == "workspace.init"
    assert payload["data"]["created"] is True
    assert payload["data"]["database_schema_version"] == migration_module.head_version()


def test_human_output_stays_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], clock: AdvancingClock
) -> None:
    run(
        [
            "workspace",
            "init",
            str(tmp_path / "PolishLinguaWiki"),
            "--backup-root",
            str(tmp_path / "backups"),
        ],
        clock=clock,
    )

    assert "initialized PolishLinguaWiki" in capsys.readouterr().out


def test_status_human_output_names_the_schema_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["status"], clock=FixedClock()) == 0

    assert f"schema v{migration_module.head_version()}" in capsys.readouterr().out


def test_workspace_doctor_and_db_check_report_success(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = synthetic_workspace.clock
    root = str(synthetic_workspace.root)

    assert run(["workspace", "doctor", "--workspace", root, "--format", "json"], clock=clock) == 0
    doctor = _json(capsys)
    assert run(["db", "check", "--workspace", root, "--format", "json"], clock=clock) == 0
    check = _json(capsys)

    assert doctor["data"]["ok"] is True
    assert check["data"]["ok"] is True
    assert any("safe to publish" in warning for warning in doctor["warnings"])


def test_reported_failures_use_exit_code_one(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = synthetic_workspace.clock
    root = str(synthetic_workspace.root)
    skill = synthetic_workspace.paths.skills / "linguawiki" / "SKILL.md"
    skill.write_text("edited", encoding="utf-8")

    doctor_status = run(
        ["workspace", "doctor", "--workspace", root, "--format", "json"], clock=clock
    )
    doctor = _json(capsys)
    skills_status = run(["skills", "check", "--workspace", root, "--format", "json"], clock=clock)
    skills = _json(capsys)

    assert doctor_status == EXIT_REPORTED_FAILURE
    assert skills_status == EXIT_REPORTED_FAILURE
    assert doctor["ok"] is True
    assert doctor["data"]["ok"] is False
    assert skills["data"]["modified"] == ["linguawiki/SKILL.md"]


def test_command_failures_use_exit_code_two_and_an_error_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], clock: AdvancingClock
) -> None:
    assert run(["workspace", "doctor", "--workspace", str(tmp_path), "--format", "json"]) == (
        EXIT_ERROR
    )

    payload = _json(capsys, stream="err")
    validate_json_contract(
        "linguawiki.cli.error.v1", payload, schema_directory=resources.schema_directory()
    )
    assert payload["command"] == "workspace.doctor"
    assert payload["error"]["code"] == "workspace_not_initialized"


def test_skills_install_repairs_the_snapshot_through_the_cli(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = synthetic_workspace.clock
    root = str(synthetic_workspace.root)
    (synthetic_workspace.paths.skills / "stray.md").write_text("stray", encoding="utf-8")

    assert run(["skills", "check", "--workspace", root, "--format", "json"], clock=clock) == (
        EXIT_REPORTED_FAILURE
    )
    capsys.readouterr()
    assert run(["skills", "install", "--workspace", root, "--format", "json"], clock=clock) == 0
    installed = _json(capsys)
    assert run(["skills", "check", "--workspace", root, "--format", "json"], clock=clock) == 0

    # The snapshot is the whole pinned skill bundle, so its size moves with each stage.
    assert installed["data"]["file_count"] == len(skill_bundle_entries())
    assert not (synthetic_workspace.paths.skills / "stray.md").exists()


def test_privacy_check_reports_candidates_from_the_filesystem(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run(
        [
            "workspace",
            "privacy-check",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )
    payload = _json(capsys)

    assert status == 0
    assert payload["data"]["source"] == "filesystem"
    assert payload["data"]["violations"] == []


def test_confirm_remote_refuses_when_there_is_no_remote(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """Confirming privacy of nothing would record a confirmation that covers nothing."""

    status = run(
        [
            "workspace",
            "confirm-remote",
            "--private",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )

    assert status == EXIT_ERROR
    assert _json(capsys, stream="err")["error"]["code"] == "no_remote_configured"


def test_confirm_remote_requires_an_explicit_choice(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run(
        [
            "workspace",
            "confirm-remote",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )

    assert status == EXIT_ERROR
    assert _json(capsys, stream="err")["error"]["code"] == "invalid_arguments"


def test_confirm_remote_records_the_decision(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "--quiet"], cwd=synthetic_workspace.root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private-remote.git")],
        cwd=synthetic_workspace.root,
        check=True,
    )

    status = run(
        [
            "workspace",
            "confirm-remote",
            "--private",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )
    payload = _json(capsys)

    assert status == 0
    assert payload["data"]["workspace_id"] == synthetic_workspace.report.workspace_id


def test_workspace_status_and_db_status_agree(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = synthetic_workspace.clock
    root = str(synthetic_workspace.root)

    run(["workspace", "status", "--workspace", root, "--format", "json"], clock=clock)
    workspace = _json(capsys)
    run(["db", "status", "--workspace", root, "--format", "json"], clock=clock)
    database = _json(capsys)

    assert workspace["data"]["applied_schema_version"] == database["data"]["applied_schema_version"]
    assert workspace["data"]["row_counts"] == database["data"]["row_counts"]


def test_db_migrate_is_a_no_op_at_the_head(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run(
        [
            "db",
            "migrate",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )
    payload = _json(capsys)

    assert status == 0
    assert payload["data"]["applied"] == []
    assert payload["data"]["backup"] is None
    assert not synthetic_workspace.backup_root.exists() or not any(
        synthetic_workspace.backup_root.iterdir()
    )


def test_db_migrate_backs_up_a_non_empty_database_before_changing_it(
    synthetic_workspace: SyntheticWorkspace,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "next-release-migrations"
    directory.mkdir()
    for migration in migration_module.migrations():
        shutil.copy2(migration.path, directory / migration.path.name)
    next_migration = _write_next_release_migration(directory)
    monkeypatch.setattr(resources, "migrations_directory", lambda: directory)
    migration_module.migrations.cache_clear()

    dry_run = run(
        [
            "db",
            "migrate",
            "--dry-run",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )
    planned = _json(capsys)
    applied_status = run(
        ["db", "migrate", "--workspace", str(synthetic_workspace.root), "--format", "json"],
        clock=synthetic_workspace.clock,
    )
    applied = _json(capsys)

    assert dry_run == 0
    assert planned["data"]["dry_run"] is True
    assert [item["migration_id"] for item in planned["data"]["applied"]] == [next_migration]
    assert planned["data"]["backup"] is None
    assert applied_status == 0
    assert applied["data"]["applied_schema_version"] == migration_module.head_version()
    backup = applied["data"]["backup"]
    assert backup is not None
    assert backup["verified"] is True
    assert Path(backup["native_database"]).is_file()
    assert Path(backup["portable_directory"], "metadata.json").is_file()
    assert "pre-migrate" in backup["directory"]


def test_db_migrate_refuses_to_change_a_non_empty_database_without_a_backup_root(
    synthetic_workspace: SyntheticWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from linguawiki.errors import LinguaWikiError
    from linguawiki.services import database as database_service

    directory = tmp_path / "next-release-migrations"
    directory.mkdir()
    for migration in migration_module.migrations():
        shutil.copy2(migration.path, directory / migration.path.name)
    _write_next_release_migration(directory)
    monkeypatch.setattr(resources, "migrations_directory", lambda: directory)
    migration_module.migrations.cache_clear()

    with pytest.raises(LinguaWikiError) as failure:
        database_service.migrate(
            synthetic_workspace.paths, backup_root=None, clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "backup_root_required"


def test_db_backup_and_restore_round_trip_through_the_cli(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    clock = synthetic_workspace.clock
    root = str(synthetic_workspace.root)

    assert run(["db", "backup", "--workspace", root, "--format", "json"], clock=clock) == 0
    backup = _json(capsys)
    assert (
        run(
            [
                "db",
                "restore",
                "--workspace",
                root,
                "--from",
                backup["data"]["directory"],
                "--to",
                str(tmp_path / "restored.duckdb"),
                "--kind",
                "portable",
                "--format",
                "json",
            ],
            clock=clock,
        )
        == 0
    )
    restored = _json(capsys)

    assert backup["data"]["verified"] is True
    assert restored["data"]["verified"] is True
    assert restored["data"]["total_rows"] == backup["data"]["total_rows"]


def test_db_backup_can_produce_a_single_layer(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = synthetic_workspace.clock
    root = str(synthetic_workspace.root)

    run(["db", "backup", "--native-only", "--workspace", root, "--format", "json"], clock=clock)
    native = _json(capsys)
    run(["db", "backup", "--portable-only", "--workspace", root, "--format", "json"], clock=clock)
    portable = _json(capsys)

    assert native["data"]["portable_directory"] is None
    assert Path(native["data"]["native_database"]).is_file()
    assert portable["data"]["native_database"] is None
    assert Path(portable["data"]["portable_directory"]).is_dir()


def test_db_export_portable_writes_outside_the_workspace(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    status = run(
        [
            "db",
            "export-portable",
            "--workspace",
            str(synthetic_workspace.root),
            "--to",
            str(tmp_path / "portable-export"),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )
    payload = _json(capsys)

    assert status == 0
    assert payload["data"]["verified"] is True
    assert Path(payload["data"]["manifest"]).is_file()


def test_db_init_is_idempotent_after_workspace_init(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run(
        ["db", "init", "--workspace", str(synthetic_workspace.root), "--format", "json"],
        clock=synthetic_workspace.clock,
    )
    payload = _json(capsys)

    assert status == 0
    assert payload["data"]["applied"] == []
    assert payload["data"]["applied_schema_version"] == migration_module.head_version()


def test_lock_dependencies_surfaces_why_it_cannot_resolve_yet(
    synthetic_workspace: SyntheticWorkspace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from linguawiki.services import workspace as workspace_service

    monkeypatch.setattr(workspace_service.shutil, "which", lambda _name: None)

    status = run(
        [
            "workspace",
            "lock-dependencies",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )

    assert status == EXIT_ERROR
    assert _json(capsys, stream="err")["error"]["code"] == "uv_unavailable"


def test_init_with_uv_lock_reports_an_unresolvable_lock_as_a_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clock: AdvancingClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workspace is valid; only the optional lock step failed, so it is a warning."""

    from linguawiki.services import workspace as workspace_service

    monkeypatch.setattr(workspace_service.shutil, "which", lambda _name: None)
    target = tmp_path / "PolishLinguaWiki"

    status = _initialize(target, tmp_path / "backups", clock, "--uv-lock")
    payload = _json(capsys)

    assert status == 0
    assert payload["data"]["dependency_lock"] is False
    assert any("lock-dependencies" in warning for warning in payload["warnings"])
    assert (target / "linguawiki.toml").is_file()
    assert not (target / "uv.lock").exists()


def test_a_backup_reason_that_escapes_the_root_is_refused_by_the_cli(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run(
        [
            "db",
            "backup",
            "--reason",
            "../../../escaped",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )

    assert status == EXIT_ERROR
    assert _json(capsys, stream="err")["error"]["code"] == "invalid_backup_reason"


def test_a_malformed_dependency_shape_is_a_diagnostic_not_an_internal_error(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """A parsable-but-wrong lock must produce a failed check, never internal_error."""

    lock = (
        Path(__file__).resolve().parents[1] / "fixtures" / "workspace-uv-lock" / "uv.lock"
    ).read_text(encoding="utf-8")
    synthetic_workspace.paths.dependency_lock.write_text(
        lock.replace('dependencies = [\n    { name = "markupsafe" },\n]', "dependencies = 1", 1),
        encoding="utf-8",
    )

    status = run(
        [
            "workspace",
            "doctor",
            "--workspace",
            str(synthetic_workspace.root),
            "--format",
            "json",
        ],
        clock=synthetic_workspace.clock,
    )
    payload = _json(capsys)

    assert status == EXIT_REPORTED_FAILURE
    assert payload["ok"] is True
    checks = {check["name"]: check for check in payload["data"]["checks"]}
    assert checks["dependency_lock"]["status"] == "failed"
    assert "not a list" in checks["dependency_lock"]["message"]


def test_a_schema_damaged_database_is_reported_by_doctor(
    synthetic_workspace: SyntheticWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    from linguawiki.db.connection import open_writer

    with (
        open_writer(
            synthetic_workspace.paths, command="test", clock=synthetic_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("ALTER TABLE workspaces ADD COLUMN smuggled VARCHAR")

    status = run(
        ["workspace", "doctor", "--workspace", str(synthetic_workspace.root), "--format", "json"],
        clock=synthetic_workspace.clock,
    )
    payload = _json(capsys)
    checks = {check["name"]: check for check in payload["data"]["checks"]}

    assert status == EXIT_REPORTED_FAILURE
    assert payload["data"]["ok"] is False
    assert checks["database_state"]["status"] == "failed"
    assert "smuggled" in checks["database_state"]["context"]["divergence"]


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("dropped", "ALTER TABLE schema_migrations DROP COLUMN application_version"),
        ("retyped", "ALTER TABLE schema_migrations ALTER applied_at TYPE VARCHAR"),
    ],
)
def test_drift_inside_the_history_table_stays_diagnosable_and_recoverable(
    label: str,
    statement: str,
    synthetic_workspace: SyntheticWorkspace,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Drift in `schema_migrations` itself must not disown or crash the workspace."""

    from linguawiki.db.connection import open_writer

    with (
        open_writer(
            synthetic_workspace.paths, command="test", clock=synthetic_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(statement)
    root = str(synthetic_workspace.root)
    clock = synthetic_workspace.clock

    doctor_status = run(
        ["workspace", "doctor", "--workspace", root, "--format", "json"], clock=clock
    )
    doctor = _json(capsys)
    backup_status = run(["db", "backup", "--workspace", root, "--format", "json"], clock=clock)
    backup = _json(capsys)
    restore_status = run(
        [
            "db",
            "restore",
            "--workspace",
            root,
            "--from",
            str(backup["data"]["directory"]),
            "--to",
            str(tmp_path / f"recovered-{label}.duckdb"),
            "--format",
            "json",
        ],
        clock=clock,
    )
    restored = _json(capsys)

    # A diagnostic, not an internal error.
    assert doctor_status == EXIT_REPORTED_FAILURE
    assert doctor["ok"] is True
    checks = {check["name"]: check for check in doctor["data"]["checks"]}
    assert checks["database_state"]["status"] == "failed"
    assert "schema_migrations" in checks["database_state"]["context"]["divergence"]

    # Native-only recovery, and it restores.
    assert backup_status == 0
    assert backup["data"]["native_database"] is not None
    assert backup["data"]["portable_directory"] is None
    assert restore_status == 0
    assert restored["data"]["verified"] is True
    assert restored["data"]["state"] == "damaged"


def test_the_polish_fixture_pins_the_released_core_schema_and_skills(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The dogfood fixture is independent and pinned, not a fork of the core repository."""

    root = synthetic_workspace.root
    lock = json.loads((root / "linguawiki.lock").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert CORE_ROOT not in root.parents
    assert lock["core"]["version"] == project["project"]["dependencies"][0].split("==")[1]
    assert lock["database_schema"]["version"] == str(migration_module.head_version())
    assert set(lock) == {
        "schema_name",
        "schema_version",
        "core",
        "database_schema",
        "skill_bundle",
        "packs",
    }
