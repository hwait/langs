"""Database lifecycle commands: init, status, migrate, check, backup, and restore."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from linguawiki.clock import Clock, SystemClock
from linguawiki.contracts import LockManifest
from linguawiki.db import locks
from linguawiki.db import migrations as migration_module
from linguawiki.db.backup import (
    BackupReport,
    create_backup,
    portable_export,
    table_row_counts,
    verify_manifest,
    verify_portable_export,
    write_manifest,
)
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.db.integrity import IntegrityReport, check_database
from linguawiki.db.state import DatabaseState, classify_database
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths, resolve_path


class MigrationRecord(ContractModel):
    version: int
    migration_id: str
    checksum: str


class DatabaseStatusReport(ContractModel):
    database: str
    present: bool
    applied_schema_version: int
    packaged_schema_version: int
    applied: tuple[MigrationRecord, ...] = ()
    pending: tuple[MigrationRecord, ...] = ()
    writer_lock_held: bool = False
    row_counts: dict[str, int] = Field(default_factory=dict)


class MigrationReport(ContractModel):
    database: str
    dry_run: bool
    applied_schema_version: int
    packaged_schema_version: int
    applied: tuple[MigrationRecord, ...] = ()
    backup: BackupReport | None = None


class PortableExportReport(ContractModel):
    database: str
    directory: str
    manifest: str
    total_rows: int
    verified: bool


def _record(migration: migration_module.Migration) -> MigrationRecord:
    return MigrationRecord(
        version=migration.version,
        migration_id=migration.migration_id,
        checksum=migration.checksum,
    )


def _applied_records(database: Database) -> tuple[MigrationRecord, ...]:
    return tuple(
        MigrationRecord(
            version=record.version, migration_id=record.migration_id, checksum=record.checksum
        )
        for record in migration_module.applied_migrations(database)
    )


def initialize(
    paths: WorkspacePaths, *, clock: Clock | None = None, command: str = "db.init"
) -> MigrationReport:
    """Create an empty database and bring it to the packaged schema head.

    A database that already holds data is never migrated here: that path has to take a
    verified backup first, which is `db migrate`'s job.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock, create=True) as database:
        state = classify_database(database)
        pending = migration_module.pending_migrations(database)
        if pending and state is DatabaseState.MANAGED:
            raise LinguaWikiError(
                "migration_required",
                "this database already holds data; run 'linguawiki db migrate' so a verified "
                "backup is taken before the schema changes",
                details=(
                    ErrorDetail(
                        field="database",
                        reason="pending migrations on a populated database",
                        context={
                            "applied": str(migration_module.applied_version(database)),
                            "packaged": str(migration_module.head_version()),
                        },
                    ),
                ),
            )
        applied = migration_module.migrate(database)
        return MigrationReport(
            database=str(database.path),
            dry_run=False,
            applied_schema_version=migration_module.applied_version(database),
            packaged_schema_version=migration_module.head_version(),
            applied=tuple(_record(migration) for migration in applied),
        )


def migrate(
    paths: WorkspacePaths,
    *,
    backup_root: Path | None,
    clock: Clock | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Migrate an existing database, backing up first when it already holds data."""

    active_clock = clock or SystemClock()
    with open_writer(
        paths, command="db.migrate", clock=active_clock, allow_uninitialized=True
    ) as database:
        state = classify_database(database)
        pending = migration_module.pending_migrations(database)
        if dry_run or not pending:
            return MigrationReport(
                database=str(database.path),
                dry_run=dry_run,
                applied_schema_version=migration_module.applied_version(database),
                packaged_schema_version=migration_module.head_version(),
                applied=tuple(_record(migration) for migration in pending) if dry_run else (),
            )
        backup: BackupReport | None = None
        if state is DatabaseState.MANAGED:
            if backup_root is None:
                raise LinguaWikiError(
                    "backup_root_required",
                    "migrating a non-empty database requires a verified backup root",
                )
            backup = create_backup(
                database, backup_root=backup_root, reason="pre-migrate", clock=active_clock
            )
        applied = migration_module.migrate(database)
        return MigrationReport(
            database=str(database.path),
            dry_run=False,
            applied_schema_version=migration_module.applied_version(database),
            packaged_schema_version=migration_module.head_version(),
            applied=tuple(_record(migration) for migration in applied),
            backup=backup,
        )


def status(paths: WorkspacePaths, *, clock: Clock | None = None) -> DatabaseStatusReport:
    """Report schema state from a read-only connection."""

    active_clock = clock or SystemClock()
    path = paths.database
    lock_held = locks.held(path) is not None
    if not path.exists():
        return DatabaseStatusReport(
            database=str(path),
            present=False,
            applied_schema_version=0,
            packaged_schema_version=migration_module.head_version(),
            pending=tuple(_record(migration) for migration in migration_module.migrations()),
            writer_lock_held=lock_held,
        )
    with open_reader(paths, clock=active_clock) as database:
        return DatabaseStatusReport(
            database=str(database.path),
            present=True,
            applied_schema_version=migration_module.applied_version(database),
            packaged_schema_version=migration_module.head_version(),
            applied=_applied_records(database),
            pending=tuple(
                _record(migration) for migration in migration_module.pending_migrations(database)
            ),
            writer_lock_held=lock_held,
            row_counts=dict(table_row_counts(database)),
        )


def check(
    paths: WorkspacePaths, *, lock: LockManifest | None = None, clock: Clock | None = None
) -> IntegrityReport:
    """Run integrity checks from a read-only connection."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        return check_database(database, lock=lock)


def backup(
    paths: WorkspacePaths,
    *,
    backup_root: Path,
    clock: Clock | None = None,
    reason: str = "manual",
    native: bool = True,
    portable: bool = True,
) -> BackupReport:
    """Create and verify a native copy plus a portable export."""

    active_clock = clock or SystemClock()
    # A damaged database is exactly the one most worth backing up before repair.
    with open_writer(
        paths, command="db.backup", clock=active_clock, allow_damaged=True
    ) as database:
        return create_backup(
            database,
            backup_root=backup_root,
            reason=reason,
            clock=active_clock,
            native=native,
            portable=portable,
        )


def export_portable(
    paths: WorkspacePaths, *, target: str | Path, clock: Clock | None = None
) -> PortableExportReport:
    """Write a standalone portable export from a consistent snapshot."""

    active_clock = clock or SystemClock()
    directory = resolve_path(target, purpose="export target")
    if directory.exists() and any(directory.iterdir()):
        raise LinguaWikiError(
            "export_target_not_empty", f"portable export target is not empty: {directory}"
        )
    with open_writer(
        paths, command="db.export-portable", clock=active_clock, allow_damaged=True
    ) as database:
        database.checkpoint()
        metadata, metadata_path = portable_export(database, directory)
    manifest = write_manifest(directory)
    verify_manifest(directory)
    verify_portable_export(directory, clock=active_clock)
    return PortableExportReport(
        database=str(paths.database),
        directory=str(directory),
        manifest=str(manifest),
        total_rows=sum(export.row_count for export in metadata.tables),
        verified=metadata_path.is_file(),
    )


def new_correlation_id() -> EventId:
    return EventId.new()
