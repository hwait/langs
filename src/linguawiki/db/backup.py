"""Native backups, format-independent portable exports, and verified restore."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import duckdb
from pydantic import ValidationError, model_validator

from linguawiki import __version__
from linguawiki.clock import Clock, SystemClock
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import (
    Database,
    open_temporary,
    quote_identifier,
    quote_identifiers,
)
from linguawiki.db.schema import (
    TABLE_ORDER,
    assert_known_schema_version,
    assert_matches_schema,
)
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.models import ContractModel
from linguawiki.paths import (
    assert_outside,
    assert_safe_destructive_target,
    assert_within,
    find_git_root,
    resolve_path,
)
from linguawiki.versions import file_sha256

BACKUP_REASON_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
NATIVE_DIRECTORY = "native"
NATIVE_DATABASE_NAME = "linguawiki.duckdb"
PORTABLE_DIRECTORY = "portable"
METADATA_NAME = "metadata.json"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_NAME = "linguawiki.backup-manifest.v1"
MANIFEST_SCHEMA_VERSION = 1
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
PORTABLE_SCHEMA_NAME = "linguawiki.portable-export.v1"


def payload_file_name(table: str) -> str:
    """The one file name a table's payload may have."""

    return f"{table}.parquet"


class TableExport(ContractModel):
    table: str
    file: str
    row_count: int
    columns: tuple[tuple[str, str], ...]

    @model_validator(mode="after")
    def file_is_the_canonical_payload_name(self) -> TableExport:
        """Pin the payload name to the table.

        A free-form reference could name an absolute path, escape the export directory,
        or point at a file the manifest does not cover — so the restore would read
        unchecksummed data. There is exactly one legal name.
        """

        if self.file != payload_file_name(self.table):
            raise ValueError(
                f"table {self.table} must reference {payload_file_name(self.table)}, "
                f"not {self.file!r}"
            )
        if self.row_count < 0:
            raise ValueError(f"table {self.table} has a negative row count")
        return self


class PortableMetadata(ContractModel):
    """Everything needed to rebuild the database without DuckDB's storage format."""

    schema_name: Literal["linguawiki.portable-export.v1"] = "linguawiki.portable-export.v1"
    schema_version: Literal[1] = 1
    workspace_id: str | None
    application_version: str
    duckdb_version: str
    database_schema_version: int
    exported_at: str
    migrations: tuple[tuple[int, str, str], ...]
    tables: tuple[TableExport, ...]

    @model_validator(mode="after")
    def table_entries_are_unique(self) -> PortableMetadata:
        """Keep the sequence, and reject duplicates before anything maps it by name."""

        names = [export.table for export in self.tables]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate table entries: {', '.join(duplicates)}")
        files = [export.file for export in self.tables]
        if len(set(files)) != len(files):
            raise ValueError("duplicate table files")
        return self


class BackupReport(ContractModel):
    directory: str
    native_database: str | None
    portable_directory: str | None
    manifest: str
    verified: bool
    total_rows: int
    tables: tuple[TableExport, ...]
    skipped_layers: tuple[str, ...] = ()


class RestoreReport(ContractModel):
    source: str
    source_kind: str
    target: str
    database_schema_version: int
    restored_tables: tuple[TableExport, ...]
    total_rows: int
    verified: bool
    state: str = "managed"


def resolve_backup_root(
    value: str | Path, *, workspace_root: Path, allow_inside_git: bool = False
) -> Path:
    """Resolve a backup root that must sit outside the learner repository and Git.

    Native databases and portable exports hold raw learner state, so a backup root
    inside any Git repository is refused rather than merely flagged.
    """

    root = assert_safe_destructive_target(
        resolve_path(value, purpose="backup_root"), purpose="backup root"
    )
    assert_outside(root, workspace_root, purpose="backup root")
    if not allow_inside_git:
        problems = backup_root_git_problems(root, workspace_root=workspace_root)
        if problems:
            raise LinguaWikiError(
                "backup_root_invalid",
                problems[0],
                details=(
                    ErrorDetail(
                        field="backup_root",
                        reason="inside a Git repository",
                        context={"path": str(root)},
                    ),
                ),
            )
    return root


def backup_root_git_problems(root: Path, *, workspace_root: Path) -> tuple[str, ...]:
    """Report a backup root that Git history could capture."""

    git_root = find_git_root(root)
    if git_root is not None and git_root != workspace_root:
        return (
            f"backup root {root} is inside the Git repository at {git_root}; "
            "learner backups must stay outside Git",
        )
    return ()


def _timestamp_slug(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def parquet_columns(database: Database, path: Path) -> tuple[tuple[str, str], ...]:
    """The logical column types DuckDB infers when reading a Parquet file.

    Physical Parquet types are lossy — VARCHAR and BLOB are both BYTE_ARRAY, TIMESTAMP
    and BIGINT are both INT64 — so comparing them let a text column become binary and
    restore as escaped bytes. DuckDB's own inference is the type that matters, because
    it is what the restore will insert.
    """

    return tuple(
        (str(name), str(kind))
        for name, kind in database.query(
            "SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM read_parquet(?))",
            [str(path)],
        )
    )


def _registered_table(name: str) -> str:
    if name not in TABLE_ORDER:
        raise LinguaWikiError("unknown_table", f"table is not part of the schema registry: {name}")
    return quote_identifier(name)


def _table_exports(database: Database, tables: tuple[str, ...]) -> tuple[TableExport, ...]:
    return tuple(
        TableExport(
            table=table,
            file=payload_file_name(table),
            row_count=database.count(table),
            columns=tuple(database.columns(table)),
        )
        for table in tables
    )


def registered_tables(database: Database) -> tuple[str, ...]:
    present = set(database.table_names())
    return tuple(table for table in TABLE_ORDER if table in present)


def write_manifest(directory: Path) -> Path:
    """Checksum every file in the backup so tampering or truncation is detectable."""

    files = sorted(
        path for path in directory.rglob("*") if path.is_file() and path.name != MANIFEST_NAME
    )
    manifest = {
        "schema_name": MANIFEST_SCHEMA_NAME,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "files": {
            path.relative_to(directory).as_posix(): {
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in files
        },
    }
    path = directory / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _manifest_entries(manifest: object, directory: Path) -> dict[str, tuple[str, int]]:
    """Validate the manifest's own structure before trusting anything it says."""

    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_name") != MANIFEST_SCHEMA_NAME
        or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
    ):
        raise LinguaWikiError(
            "backup_verification_failed",
            f"backup manifest at {directory} is not a "
            f"{MANIFEST_SCHEMA_NAME} v{MANIFEST_SCHEMA_VERSION} manifest",
        )
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise LinguaWikiError(
            "backup_verification_failed", f"backup manifest at {directory} lists no files"
        )
    entries: dict[str, tuple[str, int]] = {}
    for relative, entry in files.items():
        path = PurePosixPath(str(relative))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise LinguaWikiError(
                "backup_verification_failed",
                f"backup manifest at {directory} names an unsafe path",
                details=(ErrorDetail(field=str(relative), reason="unsafe relative path"),),
            )
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("sha256"), str)
            or not SHA256_HEX.fullmatch(entry["sha256"])
            or not isinstance(entry.get("bytes"), int)
        ):
            raise LinguaWikiError(
                "backup_verification_failed",
                f"backup manifest at {directory} has a malformed entry",
                details=(ErrorDetail(field=str(relative), reason="malformed manifest entry"),),
            )
        entries[path.as_posix()] = (entry["sha256"], entry["bytes"])
    return entries


def verify_manifest(directory: Path) -> None:
    """Re-check every recorded checksum, and that the manifest covers every file.

    Checking only the entries the manifest happens to list would let a payload file be
    removed from the manifest and then rewritten, so the two sets must match exactly.
    """

    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise LinguaWikiError(
            "backup_verification_failed", f"backup manifest is missing: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LinguaWikiError(
            "backup_verification_failed", f"backup manifest at {directory} is unreadable"
        ) from exc
    entries = _manifest_entries(manifest, directory)
    present = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    problems: list[ErrorDetail] = [
        ErrorDetail(field=relative, reason="file is not covered by the manifest")
        for relative in sorted(present - set(entries))
    ]
    for relative in sorted(entries):
        expected_hash, expected_bytes = entries[relative]
        path = directory / relative
        if not path.is_file():
            problems.append(ErrorDetail(field=relative, reason="file is missing"))
            continue
        if path.stat().st_size != expected_bytes:
            problems.append(ErrorDetail(field=relative, reason="byte count differs"))
        elif file_sha256(path) != expected_hash:
            problems.append(ErrorDetail(field=relative, reason="checksum differs"))
    if problems:
        raise LinguaWikiError(
            "backup_verification_failed",
            f"backup at {directory} failed checksum verification",
            details=tuple(problems),
        )


def native_backup(database: Database, target: Path) -> Path:
    """Copy the checkpointed database file while this process holds the writer lock.

    DuckDB's own ``COPY FROM DATABASE`` recreates tables in catalog order and trips
    over foreign keys, so the checkpointed file is copied directly instead. The copy
    is opened and verified before it is trusted.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise LinguaWikiError("backup_target_exists", f"native backup already exists: {target}")
    database.checkpoint()
    shutil.copy2(database.path, target)
    write_ahead_log = database.path.with_name(database.path.name + ".wal")
    if write_ahead_log.exists():
        shutil.copy2(write_ahead_log, target.with_name(target.name + ".wal"))
    return target


def portable_export(database: Database, target: Path) -> tuple[PortableMetadata, Path]:
    """Write one Parquet file per typed table plus JSON metadata."""

    target.mkdir(parents=True, exist_ok=True)
    # A portable export is rebuilt by replaying this release's migrations, so a database
    # whose layout does not match its history has no valid portable form. Refusing here
    # keeps `db backup` from reporting a "verified" layer that cannot restore.
    from linguawiki.db.state import DatabaseState, classify_database, schema_divergence

    state = classify_database(database)
    if state is not DatabaseState.MANAGED:
        raise LinguaWikiError(
            "portable_export_unsupported",
            f"{database.path} cannot be exported portably: its state is {state}",
            details=(
                ErrorDetail(
                    field="database",
                    reason=str(state),
                    context={"divergence": schema_divergence(database) or ""},
                ),
            ),
        )
    schema_version = migration_module.applied_version(database)
    assert_known_schema_version(schema_version)
    tables = registered_tables(database)
    assert_matches_schema(
        {table: database.columns(table) for table in tables},
        schema_version=schema_version,
        source="this database",
        code="export_incomplete",
    )
    exports = _table_exports(database, tables)
    for export in exports:
        database.execute(
            f"COPY (SELECT * FROM {_registered_table(export.table)} ORDER BY ALL) "
            "TO ? (FORMAT PARQUET)",
            [str(target / payload_file_name(export.table))],
        )
    metadata = PortableMetadata(
        workspace_id=database.scalar("SELECT workspace_id FROM workspaces")
        if "workspaces" in tables
        else None,
        application_version=__version__,
        duckdb_version=duckdb.__version__,
        database_schema_version=schema_version,
        exported_at=database.clock.now().isoformat().replace("+00:00", "Z"),
        migrations=tuple(
            (record.version, record.migration_id, record.checksum)
            for record in migration_module.applied_migrations(database)
        ),
        tables=exports,
    )
    metadata_path = target / METADATA_NAME
    metadata_path.write_text(
        json.dumps(metadata.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata, metadata_path


class DatabaseFingerprint(ContractModel):
    """What a native copy must reproduce exactly to be a faithful backup.

    Everything here is read structurally or as text, so a database whose history table
    has drifted can still be fingerprinted — the copy of a damaged database is exactly
    the one that has to be comparable.
    """

    schema_version: int
    history: tuple[tuple[str, ...], ...]
    layout: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    row_counts: tuple[tuple[str, int], ...]


def database_fingerprint(database: Database) -> DatabaseFingerprint:
    tables = sorted(database.table_names())
    return DatabaseFingerprint(
        schema_version=migration_module.claimed_schema_version(database),
        history=migration_module.raw_history(database),
        layout=tuple((table, tuple(database.columns(table))) for table in tables),
        row_counts=tuple(
            # Any table in the catalog, including one an attacker named, so the
            # identifier must be quoted rather than merely wrapped.
            (table, database.count(table))
            for table in tables
        ),
    )


def verify_native_backup(
    path: Path, *, clock: Clock, expected: DatabaseFingerprint | None = None
) -> int:
    """Confirm a native copy opens and reproduces its source exactly.

    Fidelity, not health: a database whose migration history is damaged is exactly the
    one most worth copying before repair, so this compares the copy with the source
    rather than with the packaged schema.
    """

    try:
        with open_temporary(path, clock=clock) as backup:
            observed = database_fingerprint(backup)
    except duckdb.Error as exc:
        raise LinguaWikiError(
            "backup_verification_failed",
            f"native backup at {path} could not be opened",
            details=(ErrorDetail(field="native", reason=type(exc).__name__),),
        ) from exc
    if expected is not None and observed != expected:
        raise LinguaWikiError(
            "backup_verification_failed",
            f"native backup at {path} does not reproduce the database it copied",
            details=(
                ErrorDetail(
                    field="native",
                    reason="fingerprint differs",
                    context={
                        "expected_schema_version": str(expected.schema_version),
                        "actual_schema_version": str(observed.schema_version),
                    },
                ),
            ),
        )
    return observed.schema_version


def verify_portable_export(directory: Path, *, clock: Clock) -> PortableMetadata:
    """Re-read the export and confirm every table file and row count is present."""

    metadata = read_portable_metadata(directory)
    assert_matches_schema(
        {export.table: export.columns for export in metadata.tables},
        schema_version=metadata.database_schema_version,
        source=f"the portable export at {directory}",
        code="backup_verification_failed",
    )
    covered = manifest_coverage(directory)
    problems: list[ErrorDetail] = []
    with open_temporary(Path(":memory:"), clock=clock) as scratch:
        for export in metadata.tables:
            path = resolve_payload(directory, export, covered=covered)
            if not path.is_file():
                problems.append(ErrorDetail(field=export.table, reason="parquet file is missing"))
                continue
            actual = int(scratch.scalar("SELECT count(*) FROM read_parquet(?)", [str(path)]))
            if actual != export.row_count:
                problems.append(
                    ErrorDetail(
                        field=export.table,
                        reason="row count differs",
                        context={"expected": str(export.row_count), "actual": str(actual)},
                    )
                )
            # The file must have the columns and logical types the metadata describes,
            # not merely match the schema: otherwise a Parquet file can lose a column or
            # change a column's type without either check noticing.
            actual_columns = parquet_columns(scratch, path)
            if actual_columns != tuple(export.columns):
                problems.append(
                    ErrorDetail(
                        field=export.table,
                        reason="the parquet file does not have the described columns",
                        context={
                            "described": ", ".join(
                                f"{name} {kind}" for name, kind in export.columns
                            ),
                            "actual": ", ".join(f"{name} {kind}" for name, kind in actual_columns),
                        },
                    )
                )
    if problems:
        raise LinguaWikiError(
            "backup_verification_failed",
            f"portable export at {directory} failed verification",
            details=tuple(problems),
        )
    return metadata


def resolve_payload(directory: Path, export: TableExport, *, covered: frozenset[str]) -> Path:
    """Resolve a table's payload inside the export, requiring the manifest to cover it."""

    relative = payload_file_name(export.table)
    path = (directory / relative).resolve()
    if path.parent != directory.resolve():
        raise LinguaWikiError(
            "backup_verification_failed",
            f"the payload for {export.table} resolves outside {directory}",
            details=(ErrorDetail(field=export.table, reason="payload escapes the export"),),
        )
    if relative not in covered:
        raise LinguaWikiError(
            "backup_verification_failed",
            f"the payload for {export.table} is not covered by the backup manifest",
            details=(ErrorDetail(field=export.table, reason="payload is unchecksummed"),),
        )
    return path


def manifest_coverage(directory: Path) -> frozenset[str]:
    """Payload paths the manifest checksums, relative to the directory it covers.

    The manifest may sit beside the export (a standalone `db export-portable`) or one
    level up (a `db backup` directory holding both layers).
    """

    for base, prefix in ((directory, ""), (directory.parent, f"{directory.name}/")):
        manifest_path = base / MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries = _manifest_entries(manifest, base)
        return frozenset(
            name[len(prefix) :] for name in entries if not prefix or name.startswith(prefix)
        )
    raise LinguaWikiError(
        "backup_manifest_missing",
        f"no checksum manifest covers the portable export at {directory}",
        details=(ErrorDetail(field="manifest", reason="manifest.json is missing"),),
    )


def read_portable_metadata(directory: Path) -> PortableMetadata:
    path = directory / METADATA_NAME
    if not path.is_file():
        raise LinguaWikiError(
            "portable_metadata_missing", f"portable export metadata is missing: {path}"
        )
    try:
        return PortableMetadata.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LinguaWikiError(
            "portable_metadata_invalid",
            f"portable export metadata at {path} is not a valid {PORTABLE_SCHEMA_NAME} document",
            details=(ErrorDetail(field="metadata", reason=" ".join(str(exc).split())[:300]),),
        ) from exc


def validate_backup_reason(reason: str) -> str:
    """Keep a caller-supplied reason from steering the backup out of its root."""

    if not BACKUP_REASON_PATTERN.fullmatch(reason):
        raise LinguaWikiError(
            "invalid_backup_reason",
            f"backup reason must be a short lowercase slug: {reason!r}",
            details=(
                ErrorDetail(
                    field="reason",
                    reason="must match a-z, 0-9 and hyphens",
                    context={"pattern": BACKUP_REASON_PATTERN.pattern},
                ),
            ),
        )
    return reason


def assert_exact_migration_prefix(
    recorded: Sequence[tuple[int, str, str]], schema_version: int
) -> None:
    """Require the recorded history to be exactly migrations 1..schema_version.

    Checking only the entries that happen to be listed accepted an export with no
    migration metadata at all, and would accept one that skipped a migration.
    """

    expected = [
        (migration.version, migration.migration_id, migration.checksum)
        for migration in migration_module.migrations()
        if migration.version <= schema_version
    ]
    observed = [(int(version), str(name), str(checksum)) for version, name, checksum in recorded]
    if observed != expected:
        raise LinguaWikiError(
            "restore_schema_unsupported",
            "the export's migration history is not exactly this release's prefix for "
            f"schema version {schema_version}",
            details=(
                ErrorDetail(
                    field="migrations",
                    reason="history differs from the packaged prefix",
                    context={
                        "expected": ", ".join(str(version) for version, _, _ in expected),
                        "recorded": ", ".join(str(version) for version, _, _ in observed),
                    },
                ),
            ),
        )


def portable_layer_is_possible(database: Database) -> bool:
    """Whether this database has a valid portable form at all.

    A portable export is rebuilt by replaying migrations into a fresh database, so only
    a database whose layout already matches its history can round-trip: the same
    condition `classify_database` calls MANAGED.
    """

    from linguawiki.db.state import DatabaseState, classify_database

    return classify_database(database) is DatabaseState.MANAGED


def create_backup(
    database: Database,
    *,
    backup_root: Path,
    reason: str,
    clock: Clock,
    native: bool = True,
    portable: bool = True,
) -> BackupReport:
    """Create and immediately verify the requested backup layers.

    A database whose migration history this release cannot reproduce gets a native-only
    recovery backup: the portable layer would be unrestorable, so reporting it as
    verified would be a lie. The caller is told through `skipped_layers`.
    """

    slug = validate_backup_reason(reason)
    root = backup_root.resolve()
    directory = assert_within(
        root / f"{_timestamp_slug(clock.now())}-{slug}", root, purpose="backup directory"
    )
    if directory.exists():
        raise LinguaWikiError(
            "backup_target_exists", f"backup directory already exists: {directory}"
        )
    directory.mkdir(parents=True)
    database.checkpoint()
    fingerprint = database_fingerprint(database)
    skipped: list[str] = []
    if portable and not portable_layer_is_possible(database):
        if not native:
            raise LinguaWikiError(
                "portable_export_unsupported",
                f"{database.path} has no valid portable form and no native layer was "
                "requested; take a native backup instead",
                details=(ErrorDetail(field="portable", reason="migration history is damaged"),),
            )
        portable = False
        from linguawiki.db.state import classify_database

        skipped.append(
            f"portable: this database's state is {classify_database(database)}, so a portable "
            "export could not be restored; the native layer preserves it faithfully"
        )
    native_path: Path | None = None
    portable_path: Path | None = None
    exports: tuple[TableExport, ...] = ()
    if native:
        native_path = native_backup(database, directory / NATIVE_DIRECTORY / NATIVE_DATABASE_NAME)
    if portable:
        portable_path = directory / PORTABLE_DIRECTORY
        metadata, _ = portable_export(database, portable_path)
        exports = metadata.tables
    if not exports and portable_layer_is_possible(database):
        exports = _table_exports(database, registered_tables(database))
    manifest = write_manifest(directory)
    verify_manifest(directory)
    if native_path is not None:
        verify_native_backup(native_path, clock=clock, expected=fingerprint)
    if portable_path is not None:
        verify_portable_export(portable_path, clock=clock)
    return BackupReport(
        directory=str(directory),
        native_database=None if native_path is None else str(native_path),
        portable_directory=None if portable_path is None else str(portable_path),
        manifest=str(manifest),
        verified=True,
        total_rows=sum(export.row_count for export in exports),
        tables=exports,
        skipped_layers=tuple(skipped),
    )


def locate_manifest(source: Path, located: Path) -> Path | None:
    """Find the checksum manifest covering a backup, if the source carries one."""

    base = located if located.is_dir() else located.parent
    for candidate in (source, base, base.parent):
        if candidate.is_dir() and (candidate / MANIFEST_NAME).is_file():
            return candidate
    return None


def _classify_source(source: Path, *, kind: str = "auto") -> tuple[str, Path]:
    """Accept a backup directory, a portable export, or a native database file."""

    if source.is_file():
        located = ("native", source)
    elif (source / METADATA_NAME).is_file():
        located = ("portable", source)
    else:
        portable = source / PORTABLE_DIRECTORY
        native = source / NATIVE_DIRECTORY / NATIVE_DATABASE_NAME
        if kind != "native" and (portable / METADATA_NAME).is_file():
            located = ("portable", portable)
        elif native.is_file():
            located = ("native", native)
        else:
            raise LinguaWikiError(
                "restore_source_invalid",
                f"{source} is neither a native database nor a portable export",
            )
    if kind != "auto" and located[0] != kind:
        raise LinguaWikiError(
            "restore_source_invalid",
            f"{source} does not contain a {kind} backup",
            details=(ErrorDetail(field="kind", reason=f"found {located[0]}"),),
        )
    return located


def _assert_restored_integrity(database: Database, *, source: Path) -> None:
    """A restore is only complete when the result passes the same checks as a live database."""

    from linguawiki.db.integrity import check_database

    # A restore of an older export is legitimately behind this release's head; the report
    # says so as a warning and the caller runs `db migrate`.
    report = check_database(database, allow_behind_head=True)
    failures = [
        f"{check.name}: {check.message}" for check in report.checks if check.status == "failed"
    ]
    if failures:
        raise LinguaWikiError(
            "restore_verification_failed",
            f"the database restored from {source} does not pass integrity checks",
            details=tuple(ErrorDetail(field="integrity", reason=failure) for failure in failures),
        )


def _restore_native(source: Path, target: Path, *, clock: Clock) -> RestoreReport:
    """Copy a native backup faithfully, whatever state it is in.

    A native backup is a file copy, so it can reproduce a database whose migration
    history this release cannot rebuild — and a recovery backup of a damaged database
    would be pointless if it could never be restored. `verified` therefore means the
    copy reproduces its source exactly; `state` says whether the result is healthy.
    """

    from linguawiki.db.state import DatabaseState, classify_database

    with open_temporary(source, clock=clock) as backup:
        state = classify_database(backup)
        if state is DatabaseState.UNMANAGED:
            raise LinguaWikiError(
                "restore_source_invalid",
                f"native backup at {source} is not a LinguaWiki database",
                details=(ErrorDetail(field="source", reason="unrecognized database contents"),),
            )
        if state is DatabaseState.EMPTY:
            raise LinguaWikiError(
                "restore_source_invalid",
                f"native backup at {source} has no applied migrations",
                details=(ErrorDetail(field="source", reason="empty database"),),
            )
        version = migration_module.claimed_schema_version(backup)
        if state is DatabaseState.MANAGED:
            assert_matches_schema(
                {table: backup.columns(table) for table in backup.table_names()},
                schema_version=version,
                source=f"the native backup at {source}",
                code="restore_source_invalid",
            )
        exports = (
            _table_exports(backup, registered_tables(backup))
            if state is DatabaseState.MANAGED
            else ()
        )
        fingerprint = database_fingerprint(backup)
        backup.checkpoint()
    shutil.copy2(source, target)
    write_ahead_log = source.with_name(source.name + ".wal")
    if write_ahead_log.exists():
        shutil.copy2(write_ahead_log, target.with_name(target.name + ".wal"))
    verify_native_backup(target, clock=clock, expected=fingerprint)
    if state is DatabaseState.MANAGED:
        with open_temporary(target, clock=clock) as restored:
            _assert_restored_integrity(restored, source=source)
    return RestoreReport(
        source=str(source),
        source_kind="native",
        target=str(target),
        database_schema_version=version,
        restored_tables=exports,
        total_rows=sum(export.row_count for export in exports),
        verified=True,
        state=str(state),
    )


def _restore_portable(source: Path, target: Path, *, clock: Clock) -> RestoreReport:
    metadata = verify_portable_export(source, clock=clock)
    covered = manifest_coverage(source)
    assert_exact_migration_prefix(metadata.migrations, metadata.database_schema_version)
    with open_temporary(target, clock=clock) as restored:
        try:
            for migration in migration_module.migrations():
                if migration.version > metadata.database_schema_version:
                    break
                with restored.transaction() as transaction:
                    transaction.execute(migration.sql)
            exports_by_table = {export.table: export for export in metadata.tables}
            with restored.transaction() as transaction:
                for table in TABLE_ORDER:
                    export = exports_by_table.get(table)
                    if export is None:
                        continue
                    if table == migration_module.HISTORY_TABLE:
                        transaction.execute(f"DELETE FROM {_registered_table(table)}")
                    columns = quote_identifiers(name for name, _ in export.columns)
                    transaction.execute(
                        f"INSERT INTO {_registered_table(table)} ({columns}) "
                        f"SELECT {columns} FROM read_parquet(?)",
                        [str(resolve_payload(source, export, covered=covered))],
                    )
        except duckdb.Error as exc:
            # A constraint or type failure means the export does not describe a database
            # this schema can hold; surface it as a restore failure, not a crash.
            raise LinguaWikiError(
                "restore_verification_failed",
                f"the portable export at {source} could not be loaded into schema "
                f"version {metadata.database_schema_version}",
                details=(ErrorDetail(field="restore", reason=type(exc).__name__),),
            ) from exc
        problems = [
            ErrorDetail(
                field=export.table,
                reason="restored row count differs",
                context={
                    "expected": str(export.row_count),
                    "actual": str(restored.count(export.table)),
                },
            )
            for export in metadata.tables
            if restored.count(export.table) != export.row_count
        ]
        if problems:
            raise LinguaWikiError(
                "restore_verification_failed",
                "restored table counts do not match the portable export",
                details=tuple(problems),
            )
        migration_module.assert_history_matches_package(restored)
        assert_matches_schema(
            {table: restored.columns(table) for table in restored.table_names()},
            schema_version=metadata.database_schema_version,
            source=f"the database restored from {source}",
            code="restore_verification_failed",
        )
        _assert_restored_integrity(restored, source=source)
    return RestoreReport(
        source=str(source),
        source_kind="portable",
        target=str(target),
        database_schema_version=metadata.database_schema_version,
        restored_tables=metadata.tables,
        total_rows=sum(export.row_count for export in metadata.tables),
        verified=True,
    )


def restore(
    source: str | Path,
    target: str | Path,
    *,
    clock: Clock | None = None,
    active_database: Path | None = None,
    kind: str = "auto",
) -> RestoreReport:
    """Restore into a new path; the active database is never overwritten."""

    active_clock = clock or SystemClock()
    resolved_source = resolve_path(source, purpose="restore source")
    resolved_target = assert_safe_destructive_target(
        resolve_path(target, purpose="restore target"), purpose="restore target"
    )
    if not resolved_source.exists():
        raise LinguaWikiError(
            "restore_source_invalid", f"restore source does not exist: {resolved_source}"
        )
    if resolved_target.exists():
        raise LinguaWikiError(
            "restore_target_exists",
            f"restore target already exists; choose a new path: {resolved_target}",
        )
    if active_database is not None and resolved_target == active_database.resolve():
        raise LinguaWikiError(
            "restore_target_active",
            "refusing to restore over the active learner database",
            details=(ErrorDetail(field="target", reason="active database path"),),
        )
    kind, located = _classify_source(resolved_source, kind=kind)
    manifest_root = locate_manifest(resolved_source, located)
    if manifest_root is not None:
        verify_manifest(manifest_root)
    elif resolved_source.is_dir():
        # Every directory this tool produces carries a manifest, so an absent one
        # means the backup was assembled or truncated outside LinguaWiki.
        raise LinguaWikiError(
            "backup_manifest_missing",
            f"backup at {resolved_source} has no checksum manifest to verify",
            details=(ErrorDetail(field="source", reason="manifest.json is missing"),),
        )
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "native":
            report = _restore_native(located, resolved_target, clock=active_clock)
        else:
            report = _restore_portable(located, resolved_target, clock=active_clock)
    except BaseException:
        resolved_target.unlink(missing_ok=True)
        raise
    return report


def table_row_counts(database: Database) -> Mapping[str, int]:
    return {table: database.count(table) for table in registered_tables(database)}
