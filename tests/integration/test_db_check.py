from __future__ import annotations

import tomllib
from pathlib import Path

import duckdb
import pytest

from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import open_reader, open_temporary, open_writer
from linguawiki.db.integrity import check_database
from linguawiki.errors import LinguaWikiError
from linguawiki.paths import WorkspacePaths, workspace_paths
from linguawiki.services import database as database_service
from linguawiki.services import workspace as workspace_service
from tests.conftest import SyntheticWorkspace
from tests.support.clocks import AdvancingClock

CORE_ROOT = Path(__file__).resolve().parents[2]


def _statuses(report: object) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}  # type: ignore[attr-defined]


def test_duckdb_is_pinned_exactly() -> None:
    """A DuckDB upgrade must be deliberate; it changes the on-disk storage format."""

    project = tomllib.loads((CORE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pinned = next(item for item in project["project"]["dependencies"] if item.startswith("duckdb"))
    lock = (CORE_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert pinned == f"duckdb=={duckdb.__version__}"
    assert f'name = "duckdb"\nversion = "{duckdb.__version__}"' in lock


def test_a_freshly_initialized_database_passes_every_check(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    lock = workspace_service.load_lock(synthetic_workspace.paths)

    report = database_service.check(
        synthetic_workspace.paths, lock=lock, clock=synthetic_workspace.clock
    )

    assert report.ok is True
    assert report.warnings == ()
    assert report.row_counts["workspaces"] == 1
    assert report.database_schema_version == report.packaged_schema_version


def _database_from_an_earlier_release(
    root: Path, clock: AdvancingClock, *, through: int
) -> WorkspacePaths:
    """Build a real database at an earlier schema version, holding learner data."""

    paths = workspace_paths(root)
    with open_writer(paths, command="test.seed", clock=clock, create=True) as database:
        for migration in migration_module.migrations()[:through]:
            with database.transaction() as transaction:
                transaction.execute(migration.sql)
                transaction.execute(
                    "INSERT INTO schema_migrations "
                    "(version, migration_id, checksum, application_version, applied_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        migration.version,
                        migration.migration_id,
                        migration.checksum,
                        "0.1.0",
                        transaction.now(),
                    ],
                )
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO workspaces (workspace_id, name, normalized_name, history_policy, "
                "track_policy, timezone, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "Earlier Release",
                    "earlier-release",
                    "git-wiki",
                    "single",
                    "UTC",
                    transaction.now(),
                    transaction.now(),
                ],
            )
    return paths


def test_db_init_refuses_to_migrate_a_populated_database(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """Only `db migrate` may change a populated schema, because it backs up first."""

    paths = _database_from_an_earlier_release(
        tmp_path / "Earlier", clock, through=migration_module.head_version() - 1
    )

    with pytest.raises(LinguaWikiError) as failure:
        database_service.initialize(paths, clock=clock)

    assert failure.value.payload.code == "migration_required"
    head = migration_module.head_version()
    assert failure.value.payload.details[0].context == {
        "applied": str(head - 1),
        "packaged": str(head),
    }
    assert not backup_root.exists()
    with open_reader(paths, clock=clock) as database:
        assert migration_module.applied_version(database) == head - 1
        assert int(database.scalar("SELECT count(*) FROM workspaces")) == 1


def test_db_migrate_is_the_path_that_upgrades_a_populated_database(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    paths = _database_from_an_earlier_release(
        tmp_path / "Earlier", clock, through=migration_module.head_version() - 1
    )

    report = database_service.migrate(paths, backup_root=backup_root, clock=clock)

    assert report.applied_schema_version == migration_module.head_version()
    assert [item.migration_id for item in report.applied] == [
        migration_module.migrations()[-1].migration_id
    ]
    assert report.backup is not None
    assert report.backup.verified is True
    assert "pre-migrate" in report.backup.directory
    with open_reader(paths, clock=clock) as database:
        assert int(database.scalar("SELECT count(*) FROM workspaces")) == 1


def test_db_init_refuses_a_database_linguawiki_did_not_create(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """An unrelated DuckDB file is somebody's data, not an empty slot to migrate into."""

    paths = workspace_paths(tmp_path / "Alien")
    paths.database.parent.mkdir(parents=True)
    connection = duckdb.connect(str(paths.database))
    connection.execute("CREATE TABLE somebody_elses_data(id INTEGER, note VARCHAR)")
    connection.execute("INSERT INTO somebody_elses_data VALUES (1, 'irreplaceable')")
    connection.close()

    with pytest.raises(LinguaWikiError) as failure:
        database_service.initialize(paths, clock=clock)

    assert failure.value.payload.code == "database_not_empty"
    assert "somebody_elses_data" in failure.value.payload.details[0].context["tables"]
    assert not backup_root.exists()
    with open_temporary(paths.database, clock=clock) as database:
        assert database.table_names() == ["somebody_elses_data"]
        assert database.scalar("SELECT note FROM somebody_elses_data") == "irreplaceable"


def test_db_migrate_refuses_a_database_linguawiki_did_not_create(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """`db init` and `db migrate` must agree about what counts as our database."""

    paths = workspace_paths(tmp_path / "Alien")
    paths.database.parent.mkdir(parents=True)
    connection = duckdb.connect(str(paths.database))
    connection.execute("CREATE TABLE somebody_elses_data(id INTEGER, note VARCHAR)")
    connection.execute("INSERT INTO somebody_elses_data VALUES (1, 'irreplaceable')")
    connection.close()

    with pytest.raises(LinguaWikiError) as failure:
        database_service.migrate(paths, backup_root=backup_root, clock=clock)

    assert failure.value.payload.code == "database_not_empty"
    assert not backup_root.exists()
    with open_temporary(paths.database, clock=clock) as database:
        assert database.table_names() == ["somebody_elses_data"]
        assert database.scalar("SELECT note FROM somebody_elses_data") == "irreplaceable"


def test_db_init_still_initializes_an_absent_database(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    paths = workspace_paths(tmp_path / "PolishLinguaWiki")

    report = database_service.initialize(paths, clock=clock)

    assert report.applied_schema_version == migration_module.head_version()
    assert len(report.applied) == migration_module.head_version()


def test_db_init_is_a_no_op_on_a_current_database(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = database_service.initialize(synthetic_workspace.paths, clock=synthetic_workspace.clock)

    assert report.applied == ()
    assert report.applied_schema_version == migration_module.head_version()


def test_check_detects_a_stale_projection(synthetic_workspace: SyntheticWorkspace) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            transaction.execute("UPDATE projection_state SET stale = TRUE")
        report = check_database(database)

    assert _statuses(report)["projection_state"] == "warning"
    assert report.ok is True
    assert report.warnings != ()


def test_check_detects_a_missing_projection_row(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            transaction.execute("DELETE FROM projection_state")
        report = check_database(database)

    assert report.ok is False
    assert _statuses(report)["projection_state"] == "failed"


def test_check_detects_a_missing_workspace_identity(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        # DuckDB resolves foreign keys per statement, so the child rows need their
        # own committed transaction before the parent row can go.
        with database.transaction() as transaction:
            transaction.execute("DELETE FROM workspace_versions")
        with database.transaction() as transaction:
            transaction.execute("DELETE FROM workspaces")
        report = check_database(database)

    assert report.ok is False
    assert _statuses(report)["workspace_identity"] == "failed"
    assert _statuses(report)["version_mirror"] == "failed"


def test_a_second_workspace_row_is_impossible(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The singleton column keeps a learner database from holding two workspaces."""

    with (
        open_writer(
            synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
        ) as database,
        pytest.raises(duckdb.Error),
        database.transaction() as transaction,
    ):
        transaction.execute(
            "INSERT INTO workspaces (workspace_id, name, normalized_name, history_policy, "
            "track_policy, timezone, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "Second Workspace",
                "second-workspace",
                "git-wiki",
                "single",
                "UTC",
                transaction.now(),
                transaction.now(),
            ],
        )


def test_check_detects_an_unregistered_table(synthetic_workspace: SyntheticWorkspace) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            transaction.execute("CREATE TABLE hand_made (id VARCHAR)")
        report = check_database(database)

    assert report.ok is False
    assert _statuses(report)["tables_registered"] == "failed"
    assert "hand_made" in report.checks[-1].context["unexpected"]


def test_check_detects_more_than_one_primary_track(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO users (user_id, workspace_id, display_name, timezone, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    "usr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    synthetic_workspace.report.workspace_id,
                    "Synthetic Learner",
                    "UTC",
                    now,
                    now,
                ],
            )
            for identifier, language in (
                ("trk_01ARZ3NDEKTSV4RRFFQ69G5FAV", "pl"),
                ("trk_01ARZ3NDEKTSV4RRFFQ69G5FAW", "zh"),
            ):
                transaction.execute(
                    "INSERT INTO learning_tracks (track_id, user_id, target_language, "
                    "proficiency_framework, is_primary, timezone, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        identifier,
                        "usr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                        language,
                        "cefr",
                        True,
                        "UTC",
                        now,
                        now,
                    ],
                )
        report = check_database(database)

    assert report.ok is False
    assert _statuses(report)["primary_track"] == "failed"


def test_more_than_one_active_track_warns_under_the_single_program_default(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """Two regional programs for one learner: allowed, but only one may be primary.

    Both tracks name the installed pack, because a track that names none cannot be
    resolved by any service and is reported separately by `track_pack_binding`.
    """

    synthetic_workspace = installed_pilot
    with (
        open_writer(
            synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        pack_id = str(transaction.scalar("SELECT pack_id FROM language_packs"))
        now = transaction.now()
        transaction.execute(
            "INSERT INTO users (user_id, workspace_id, display_name, timezone, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                "usr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                synthetic_workspace.report.workspace_id,
                "Synthetic Learner",
                "UTC",
                now,
                now,
            ],
        )
        for identifier, region, primary in (
            ("trk_01ARZ3NDEKTSV4RRFFQ69G5FAV", "PL", True),
            ("trk_01ARZ3NDEKTSV4RRFFQ69G5FAW", "UA", False),
        ):
            transaction.execute(
                "INSERT INTO learning_tracks (track_id, user_id, target_language, region, "
                "proficiency_framework, is_primary, timezone, pack_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    identifier,
                    "usr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "pl",
                    region,
                    "cefr",
                    primary,
                    "UTC",
                    pack_id,
                    now,
                    now,
                ],
            )

    doctor = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)
    status = workspace_service.status(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert doctor.ok is True
    assert _statuses(doctor)["single_program"] == "warning"
    assert any("one primary program" in warning for warning in status.warnings)


def test_orphan_relations_are_reported_when_constraints_are_absent(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """A restored database without its foreign keys must still be diagnosable."""

    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            transaction.execute("DROP TABLE user_languages")
            transaction.execute(
                "CREATE TABLE user_languages (user_id VARCHAR, language_tag VARCHAR, "
                "role VARCHAR, preference_order INTEGER, created_at TIMESTAMP)"
            )
            transaction.execute(
                "INSERT INTO user_languages VALUES (?, ?, ?, ?, ?)",
                ["usr_01ARZ3NDEKTSV4RRFFQ69G5FAV", "ru", "support", 1, transaction.now()],
            )
        report = check_database(database)

    assert report.ok is False
    assert _statuses(report)["orphan_relations"] == "failed"


def test_check_stops_at_a_broken_migration_history(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 2", ["a" * 64]
            )
        report = check_database(database)

    assert report.ok is False
    assert [check.name for check in report.checks] == ["migration_history"]
    assert report.row_counts == {}


def test_check_detects_a_hole_in_the_applied_migration_history(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            transaction.execute("DELETE FROM schema_migrations WHERE version = 3")
        report = check_database(database)

    assert report.ok is False
    assert [check.name for check in report.checks] == ["migration_history"]
    assert report.checks[0].context["code"] == "migration_history_incomplete"


def test_check_reports_a_database_behind_the_packaged_release(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with database.transaction() as transaction:
            transaction.execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                [migration_module.head_version()],
            )
        report = check_database(database)

    assert report.ok is False
    assert _statuses(report)["migration_head"] == "failed"


def test_check_understands_assessment_definitions_before_schema_18(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    """Behind-head inspection must use the columns that release actually had."""

    paths = _database_from_an_earlier_release(tmp_path / "Schema 17", clock, through=17)

    with open_reader(paths, clock=clock) as database:
        report = check_database(database, allow_behind_head=True)

    assert report.database_schema_version == 17
    assert _statuses(report)["migration_head"] == "warning"
    assert _statuses(report)["pack_framework_scoping"] == "ok"
