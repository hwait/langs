from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import duckdb
import pytest

from linguawiki.db import locks
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import open_reader, open_temporary, open_writer, resolve_database_path
from linguawiki.errors import LinguaWikiError
from linguawiki.paths import workspace_paths
from linguawiki.services import database as database_service
from tests.conftest import SyntheticWorkspace
from tests.support.clocks import AdvancingClock


def test_a_second_in_process_writer_receives_a_retryable_error(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    paths = synthetic_workspace.paths

    with (
        open_writer(paths, command="db.migrate", clock=synthetic_workspace.clock),
        pytest.raises(LinguaWikiError) as failure,
        open_writer(paths, command="db.backup", clock=synthetic_workspace.clock),
    ):
        pytest.fail("the second writer must not acquire the lock")

    assert failure.value.payload.code == "writer_locked"
    assert failure.value.payload.retryable is True
    assert failure.value.payload.details[0].context["command"] == "db.migrate"


def test_a_separate_process_sees_the_same_retryable_error(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    paths = synthetic_workspace.paths
    holder = locks.acquire(paths.database, clock=synthetic_workspace.clock, command="db.migrate")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "linguawiki",
                "db",
                "backup",
                "--workspace",
                str(paths.root),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        locks.release(paths.database, holder)

    payload = json.loads(result.stderr)
    assert result.returncode == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "writer_locked"
    assert payload["error"]["retryable"] is True


def test_deleting_the_lock_file_still_cannot_open_a_second_writer(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The application lock is the friendly error; DuckDB's own lock is the guarantee."""

    paths = synthetic_workspace.paths
    program = textwrap.dedent(
        f"""
        from datetime import UTC, datetime
        from pathlib import Path
        from linguawiki.db import locks
        from linguawiki.db.connection import open_writer
        from linguawiki.errors import LinguaWikiError
        from linguawiki.paths import workspace_paths

        class Clock:
            def now(self):
                return datetime(2026, 1, 1, tzinfo=UTC)

        paths = workspace_paths(Path({str(paths.root)!r}))
        locks.lock_path(paths.database).unlink(missing_ok=True)
        try:
            with open_writer(paths, command="second.writer", clock=Clock()):
                print("opened")
        except LinguaWikiError as error:
            print(error.payload.code, error.payload.retryable)
        """
    )

    with open_writer(paths, command="first.writer", clock=synthetic_workspace.clock):
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, check=True
        )

    assert result.stdout.strip() == "database_busy True"


def test_the_writer_lock_is_released_after_a_failed_command(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    paths = synthetic_workspace.paths

    with (
        pytest.raises(RuntimeError),
        open_writer(paths, command="db.migrate", clock=synthetic_workspace.clock),
    ):
        raise RuntimeError("command failed")

    assert locks.held(paths.database) is None
    with open_writer(paths, command="db.backup", clock=synthetic_workspace.clock) as database:
        assert migration_module.applied_version(database) == migration_module.head_version()


def test_reports_use_read_only_connections(synthetic_workspace: SyntheticWorkspace) -> None:
    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        assert database.read_only is True
        assert locks.held(synthetic_workspace.paths.database) is None
        with pytest.raises(LinguaWikiError) as failure, database.transaction():
            pytest.fail("a read-only connection must refuse to open a transaction")

    assert failure.value.payload.code == "read_only_connection"


def test_a_read_only_connection_cannot_write(synthetic_workspace: SyntheticWorkspace) -> None:
    with (
        open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database,
        pytest.raises(duckdb.Error),
    ):
        database.execute("DELETE FROM workspaces")


def test_a_failed_write_rolls_back_completely(synthetic_workspace: SyntheticWorkspace) -> None:
    paths = synthetic_workspace.paths

    with open_writer(paths, command="test.write", clock=synthetic_workspace.clock) as database:
        before = database.query("SELECT projection, stale FROM projection_state ORDER BY 1")
        with pytest.raises(RuntimeError), database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO projection_state (projection, projection_version, stale, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ["reports", 1, True, transaction.now()],
            )
            transaction.execute(
                "UPDATE projection_state SET stale = TRUE WHERE projection = 'wiki'"
            )
            raise RuntimeError("failure after partial writes")

        assert database.query("SELECT projection, stale FROM projection_state ORDER BY 1") == before


def test_a_constraint_violation_rolls_the_whole_unit_back(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    paths = synthetic_workspace.paths

    with open_writer(paths, command="test.write", clock=synthetic_workspace.clock) as database:
        with pytest.raises(duckdb.Error), database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO users (user_id, workspace_id, display_name, timezone, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    "usr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    synthetic_workspace.report.workspace_id,
                    "Synthetic Learner",
                    "UTC",
                    transaction.now(),
                    transaction.now(),
                ],
            )
            # The second user references a workspace that does not exist.
            transaction.execute(
                "INSERT INTO users (user_id, workspace_id, display_name, timezone, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    "usr_01ARZ3NDEKTSV4RRFFQ69G5FAW",
                    "wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "Orphan Learner",
                    "UTC",
                    transaction.now(),
                    transaction.now(),
                ],
            )

        assert int(database.scalar("SELECT count(*) FROM users")) == 0


def test_transactions_must_stay_short_and_non_nested(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with open_writer(
        synthetic_workspace.paths, command="test.write", clock=synthetic_workspace.clock
    ) as database:
        with (
            database.transaction(),
            pytest.raises(LinguaWikiError) as failure,
            database.transaction(),
        ):
            pytest.fail("a nested transaction must be refused")

        assert failure.value.payload.code == "nested_transaction"
        # The connection is still usable after the refusal.
        with database.transaction() as transaction:
            transaction.execute("UPDATE projection_state SET stale = stale")


def test_missing_databases_are_reported_rather_than_created(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    paths = workspace_paths(tmp_path / "PolishLinguaWiki")

    with (
        pytest.raises(LinguaWikiError) as writer,
        open_writer(paths, command="db.migrate", clock=clock),
    ):
        pytest.fail("open_writer must not create a database implicitly")
    assert writer.value.payload.code == "database_not_found"

    with pytest.raises(LinguaWikiError) as reader, open_reader(paths, clock=clock):
        pytest.fail("open_reader must not create a database")
    assert reader.value.payload.code == "database_not_found"
    assert not paths.database.exists()


def test_the_database_path_comes_only_from_the_workspace_root(tmp_path: Path) -> None:
    paths = workspace_paths(tmp_path / "PolishLinguaWiki")

    assert resolve_database_path(paths) == paths.root / "data" / "linguawiki.duckdb"


def test_a_busy_database_file_is_a_retryable_error(
    synthetic_workspace: SyntheticWorkspace, clock: AdvancingClock
) -> None:
    """DuckDB refuses a second configuration for the same file; that must be retryable."""

    with (
        open_temporary(synthetic_workspace.paths.database, clock=clock),
        pytest.raises(LinguaWikiError) as failure,
        open_reader(synthetic_workspace.paths, clock=clock),
    ):
        pytest.fail("a read-only connection must not join a writer's configuration")

    assert failure.value.payload.code == "database_busy"
    assert failure.value.payload.retryable is True


def test_db_status_reports_the_writer_lock(synthetic_workspace: SyntheticWorkspace) -> None:
    holder = locks.acquire(
        synthetic_workspace.paths.database,
        clock=synthetic_workspace.clock,
        command="db.migrate",
    )
    try:
        report = database_service.status(synthetic_workspace.paths, clock=synthetic_workspace.clock)
    finally:
        locks.release(synthetic_workspace.paths.database, holder)

    assert report.writer_lock_held is True
    assert report.applied_schema_version == migration_module.head_version()
    assert report.pending == ()


def test_db_status_reports_an_absent_database(tmp_path: Path, clock: AdvancingClock) -> None:
    report = database_service.status(workspace_paths(tmp_path / "absent"), clock=clock)

    assert report.present is False
    assert report.applied_schema_version == 0
    assert len(report.pending) == migration_module.head_version()
