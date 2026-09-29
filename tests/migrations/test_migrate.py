from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

from linguawiki import __version__, resources
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import open_temporary, open_writer
from linguawiki.db.schema import TABLE_ORDER
from linguawiki.errors import LinguaWikiError
from linguawiki.paths import WorkspacePaths, workspace_paths
from tests.support.clocks import AdvancingClock


@pytest.fixture
def target(tmp_path: Path) -> WorkspacePaths:
    return workspace_paths(tmp_path / "PolishLinguaWiki")


def _migrated(paths: WorkspacePaths, clock: AdvancingClock) -> None:
    with open_writer(paths, command="test.migrate", clock=clock, create=True) as database:
        migration_module.migrate(database)


def test_a_fresh_database_reaches_the_packaged_head(
    target: WorkspacePaths, clock: AdvancingClock
) -> None:
    with open_writer(target, command="test.migrate", clock=clock, create=True) as database:
        applied = migration_module.migrate(database)

        assert [migration.version for migration in applied] == list(
            range(1, migration_module.head_version() + 1)
        )
        assert migration_module.applied_version(database) == migration_module.head_version()
        assert set(database.table_names()) == set(TABLE_ORDER)


def test_history_records_checksum_and_application_version(
    target: WorkspacePaths, clock: AdvancingClock
) -> None:
    _migrated(target, clock)

    with open_temporary(target.database, clock=clock) as database:
        history = migration_module.applied_migrations(database)

    packaged = migration_module.migrations()
    assert [record.checksum for record in history] == [item.checksum for item in packaged]
    assert {record.application_version for record in history} == {__version__}
    assert all(record.applied_at.tzname() == "UTC" for record in history)


def test_migrating_twice_is_idempotent(target: WorkspacePaths, clock: AdvancingClock) -> None:
    _migrated(target, clock)

    with open_writer(target, command="test.migrate", clock=clock) as database:
        assert migration_module.migrate(database) == ()
        assert migration_module.pending_migrations(database) == ()


@pytest.mark.parametrize("boundary", range(1, migration_module.head_version() + 1))
def test_a_failure_at_any_boundary_rolls_back_and_leaves_the_database_usable(
    boundary: int,
    target: WorkspacePaths,
    clock: AdvancingClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken_directory = tmp_path / "broken-migrations"
    broken_directory.mkdir()
    for migration in migration_module.migrations():
        destination = broken_directory / migration.path.name
        if migration.version == boundary:
            destination.write_text(
                f"{migration.sql}\nCREATE TABLE broken (id VARCHAR REFERENCES absent (id));\n",
                encoding="utf-8",
            )
        else:
            shutil.copy2(migration.path, destination)
    monkeypatch.setattr(resources, "migrations_directory", lambda: broken_directory)
    migration_module.migrations.cache_clear()

    with open_writer(target, command="test.migrate", clock=clock, create=True) as database:
        with pytest.raises(LinguaWikiError) as failure:
            migration_module.migrate(database)

        assert failure.value.payload.code == "migration_failed"
        assert migration_module.applied_version(database) == boundary - 1
        assert "broken" not in database.table_names()
        # The prior database is still usable for reads and further writes.
        assert len(migration_module.applied_migrations(database)) == boundary - 1

    monkeypatch.undo()
    migration_module.migrations.cache_clear()

    # A repaired release migrates the same database the rest of the way.
    _migrated(target, clock)
    with open_temporary(target.database, clock=clock) as database:
        assert migration_module.applied_version(database) == migration_module.head_version()


def test_an_edited_released_migration_is_refused(
    target: WorkspacePaths, clock: AdvancingClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _migrated(target, clock)
    edited_directory = tmp_path / "edited-migrations"
    edited_directory.mkdir()
    for migration in migration_module.migrations():
        destination = edited_directory / migration.path.name
        text = migration.sql
        if migration.version == 3:
            text = f"-- edited after release\n{text}"
        destination.write_text(text, encoding="utf-8")
    monkeypatch.setattr(resources, "migrations_directory", lambda: edited_directory)
    migration_module.migrations.cache_clear()

    # The writer boundary refuses the database before any command reads its history.
    with (
        pytest.raises(LinguaWikiError) as boundary,
        open_writer(target, command="test.migrate", clock=clock),
    ):
        pytest.fail("a damaged database must not be opened for writing")
    assert boundary.value.payload.code == "database_damaged"

    with (
        open_temporary(target.database, clock=clock) as database,
        pytest.raises(LinguaWikiError) as failure,
    ):
        migration_module.pending_migrations(database)

    assert failure.value.payload.code == "migration_checksum_mismatch"


def test_a_database_from_a_newer_release_is_refused(
    target: WorkspacePaths, clock: AdvancingClock
) -> None:
    _migrated(target, clock)

    with open_writer(target, command="test.migrate", clock=clock) as database:
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO schema_migrations "
                "(version, migration_id, checksum, application_version, applied_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [999, "0999_from_the_future", "f" * 64, "9.9.9", transaction.now()],
            )
        with pytest.raises(LinguaWikiError) as failure:
            migration_module.pending_migrations(database)

    assert failure.value.payload.code == "schema_ahead_of_core"


@pytest.mark.parametrize("removed", [1, 3, 6])
def test_a_hole_in_the_applied_history_is_refused(
    removed: int, target: WorkspacePaths, clock: AdvancingClock
) -> None:
    """A row-by-row check would accept a history whose migration schema never ran."""

    _migrated(target, clock)

    with open_writer(target, command="test.migrate", clock=clock) as database:
        with database.transaction() as transaction:
            transaction.execute("DELETE FROM schema_migrations WHERE version = ?", [removed])
        with pytest.raises(LinguaWikiError) as failure:
            migration_module.assert_history_matches_package(database)

    payload = failure.value.payload
    assert payload.code == "migration_history_incomplete"
    assert str(removed) not in payload.details[0].context["applied"].split(", ")


@pytest.mark.parametrize("column", ["version", "migration_id"])
def test_the_history_table_refuses_duplicate_rows(
    column: str, target: WorkspacePaths, clock: AdvancingClock
) -> None:
    """Version and identifier uniqueness are enforced by the schema itself."""

    _migrated(target, clock)
    duplicate = (
        {"version": 5, "migration_id": "0099_new_identifier"}
        if column == "version"
        else {"version": 99, "migration_id": "0005_pack_and_framework_registry"}
    )

    with (
        open_writer(target, command="test.migrate", clock=clock) as database,
        pytest.raises(duckdb.ConstraintException),
        database.transaction() as transaction,
    ):
        transaction.execute(
            "INSERT INTO schema_migrations "
            "(version, migration_id, checksum, application_version, applied_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                duplicate["version"],
                duplicate["migration_id"],
                "a" * 64,
                __version__,
                transaction.now(),
            ],
        )


def test_a_complete_history_is_accepted(target: WorkspacePaths, clock: AdvancingClock) -> None:
    _migrated(target, clock)

    with open_writer(target, command="test.migrate", clock=clock) as database:
        migration_module.assert_history_matches_package(database)
        assert migration_module.applied_version(database) == migration_module.head_version()


def test_a_partially_migrated_history_is_still_a_valid_prefix(
    target: WorkspacePaths, clock: AdvancingClock
) -> None:
    """Stopping part-way through the sequence is legitimate and stays migratable."""

    _migrated(target, clock)

    with open_writer(target, command="test.migrate", clock=clock) as database:
        with database.transaction() as transaction:
            transaction.execute("DELETE FROM schema_migrations WHERE version > 4")
        migration_module.assert_history_matches_package(database)
        assert [
            migration.version for migration in migration_module.pending_migrations(database)
        ] == list(range(5, migration_module.head_version() + 1))


def test_the_migration_history_table_is_required_before_reading_history(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    empty = tmp_path / "empty.duckdb"
    duckdb.connect(str(empty)).close()

    with open_temporary(empty, clock=clock) as database:
        assert migration_module.applied_migrations(database) == ()
        assert migration_module.applied_version(database) == 0
