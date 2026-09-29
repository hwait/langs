"""Ordered, checksummed migrations with per-migration transactional rollback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import duckdb

from linguawiki import __version__, resources
from linguawiki.clock import aware_utc
from linguawiki.contracts import VersionPin
from linguawiki.db.connection import Database, quote_identifier
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId
from linguawiki.versions import file_sha256, tree_sha256

MIGRATION_NAME_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<slug>[a-z0-9_]+)\.sql$")
HISTORY_TABLE = "schema_migrations"


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable, released migration file."""

    version: int
    migration_id: str
    checksum: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    migration_id: str
    checksum: str
    application_version: str
    applied_at: datetime


@lru_cache(maxsize=1)
def migrations() -> tuple[Migration, ...]:
    """Load the packaged migration sequence and reject gaps or duplicates."""

    directory = resources.migrations_directory()
    loaded: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME_PATTERN.match(path.name)
        if match is None:
            raise LinguaWikiError(
                "invalid_migration_name",
                f"migration file name is not ordered and slugged: {path.name}",
            )
        loaded.append(
            Migration(
                version=int(match["version"]),
                migration_id=path.stem,
                checksum=file_sha256(path),
                path=path,
            )
        )
    expected = list(range(1, len(loaded) + 1))
    if [migration.version for migration in loaded] != expected:
        raise LinguaWikiError(
            "invalid_migration_sequence", "migration versions must be contiguous from 0001"
        )
    return tuple(loaded)


def head_version() -> int:
    return migrations()[-1].version


def database_schema_pin() -> VersionPin:
    """Pin the schema by head version plus the hash of every migration checksum."""

    return VersionPin(
        version=str(head_version()),
        sha256=tree_sha256(
            (migration.migration_id, migration.checksum) for migration in migrations()
        ),
    )


def history_table_columns() -> tuple[tuple[str, str], ...]:
    """The columns the history table must have before its rows can be decoded.

    Built from the first migration alone: the history table's shape must be knowable
    even when a later migration is broken, since that is when reading the history
    matters most.
    """

    from linguawiki.db.schema import expected_schema

    return expected_schema(1)[HISTORY_TABLE]


def recorded_migration_ids(database: Database) -> tuple[str, ...] | None:
    """Migration identifiers in the history table, read as text.

    Ownership of a database has to be detectable even when the history table's own
    layout has drifted, so this reads the one column it needs and casts it, rather than
    decoding a fully typed row. Returns None when even that is impossible.
    """

    if not database.has_table(HISTORY_TABLE):
        return None
    if "migration_id" not in {name for name, _ in database.columns(HISTORY_TABLE)}:
        return None
    try:
        rows = database.query(
            f"SELECT CAST(migration_id AS VARCHAR) FROM {HISTORY_TABLE} "
            "WHERE migration_id IS NOT NULL"
        )
    except duckdb.Error:
        return None
    return tuple(str(value) for (value,) in rows)


def claimed_schema_version(database: Database) -> int:
    """The schema version a database's raw history claims, without typed decoding."""

    recorded = recorded_migration_ids(database)
    if not recorded:
        return 0
    versions = {migration.migration_id: migration.version for migration in migrations()}
    known = sorted(versions[name] for name in recorded if name in versions)
    return known[-1] if known else 0


def raw_history(database: Database) -> tuple[tuple[str, ...], ...]:
    """The history table's rows as text, comparable even when its types have drifted."""

    if not database.has_table(HISTORY_TABLE):
        return ()
    columns = [name for name, _ in database.columns(HISTORY_TABLE)]
    if not columns:
        return ()
    # Column names come from the catalog of a possibly-drifted table, so they are data.
    projection = ", ".join(f"CAST({quote_identifier(name)} AS VARCHAR)" for name in columns)
    try:
        rows = database.query(f"SELECT {projection} FROM {HISTORY_TABLE} ORDER BY ALL")
    except duckdb.Error:
        return ()
    return tuple(tuple("" if value is None else str(value) for value in row) for row in rows)


def assert_history_table_decodable(database: Database) -> None:
    """Require the history table's own layout before decoding typed rows from it.

    Decoding first meant a dropped or retyped column in `schema_migrations` surfaced as
    an `AttributeError` and an `internal_error` envelope instead of a diagnostic.
    """

    if not database.has_table(HISTORY_TABLE):
        return
    observed = tuple(database.columns(HISTORY_TABLE))
    expected = history_table_columns()
    if observed != expected:
        raise LinguaWikiError(
            "history_table_malformed",
            f"{HISTORY_TABLE} does not have the columns this release records",
            details=(
                ErrorDetail(
                    field=HISTORY_TABLE,
                    reason="column specification differs",
                    context={
                        "expected": ", ".join(f"{name} {kind}" for name, kind in expected),
                        "actual": ", ".join(f"{name} {kind}" for name, kind in observed),
                    },
                ),
            ),
        )


def applied_migrations(database: Database) -> tuple[AppliedMigration, ...]:
    if not database.has_table(HISTORY_TABLE):
        return ()
    assert_history_table_decodable(database)
    rows = database.query(
        f"SELECT version, migration_id, checksum, application_version, applied_at "
        f"FROM {HISTORY_TABLE} ORDER BY version"
    )
    return tuple(
        AppliedMigration(
            version=int(row[0]),
            migration_id=str(row[1]),
            checksum=str(row[2]),
            application_version=str(row[3]),
            applied_at=aware_utc(row[4]),
        )
        for row in rows
    )


def applied_version(database: Database) -> int:
    history = applied_migrations(database)
    return history[-1].version if history else 0


def assert_history_matches_package(database: Database) -> None:
    """Refuse to touch a database whose history diverges from the packaged code.

    The applied rows must be the exact contiguous prefix of the packaged sequence:
    checking each row on its own would accept a history with a hole in it, and the
    schema of the missing migration would silently be absent from the database.
    """

    packaged = {migration.version: migration for migration in migrations()}
    records = applied_migrations(database)
    for record in records:
        migration = packaged.get(record.version)
        if migration is None:
            raise LinguaWikiError(
                "schema_ahead_of_core",
                "the database has migrations this core release does not know",
                details=(
                    ErrorDetail(
                        field="schema_migrations",
                        reason="unknown applied migration",
                        context={"version": str(record.version)},
                    ),
                ),
            )
        if migration.migration_id != record.migration_id or migration.checksum != record.checksum:
            raise LinguaWikiError(
                "migration_checksum_mismatch",
                "a released migration changed after it was applied",
                details=(
                    ErrorDetail(
                        field="schema_migrations",
                        reason="checksum or identifier differs",
                        context={
                            "version": str(record.version),
                            "applied": record.migration_id,
                            "packaged": migration.migration_id,
                        },
                    ),
                ),
            )
    expected = list(range(1, len(records) + 1))
    if [record.version for record in records] != expected:
        raise LinguaWikiError(
            "migration_history_incomplete",
            "the applied migration history is not a contiguous sequence from 0001",
            details=(
                ErrorDetail(
                    field="schema_migrations",
                    reason="missing or duplicated migration versions",
                    context={
                        "applied": ", ".join(str(record.version) for record in records),
                        "expected": ", ".join(str(version) for version in expected),
                    },
                ),
            ),
        )


def pending_migrations(database: Database) -> tuple[Migration, ...]:
    assert_history_matches_package(database)
    current = applied_version(database)
    return tuple(migration for migration in migrations() if migration.version > current)


def _apply_one(database: Database, migration: Migration) -> None:
    """Apply one migration atomically; a failure leaves the prior database usable."""

    try:
        with database.transaction() as transaction:
            transaction.execute(migration.sql)
            transaction.execute(
                f"INSERT INTO {HISTORY_TABLE} "
                "(version, migration_id, checksum, application_version, applied_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    migration.version,
                    migration.migration_id,
                    migration.checksum,
                    __version__,
                    database.now(),
                ],
            )
    except duckdb.Error as exc:
        raise LinguaWikiError(
            "migration_failed",
            f"migration {migration.migration_id} failed and was rolled back",
            details=(
                ErrorDetail(
                    field="schema_migrations",
                    reason=type(exc).__name__,
                    context={"migration_id": migration.migration_id},
                ),
            ),
        ) from exc


def migrate(database: Database) -> tuple[Migration, ...]:
    """Apply every pending migration, each in its own transaction."""

    pending = pending_migrations(database)
    for migration in pending:
        _apply_one(database, migration)
    return pending


def record_domain_event(
    database: Database,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    correlation_id: EventId,
    payload_json: str = "{}",
    idempotency_key: str | None = None,
) -> EventId:
    """Append one domain event inside the caller's transaction."""

    event_id = EventId.new()
    now = database.now()
    database.execute(
        "INSERT INTO domain_events (event_id, event_type, aggregate_type, aggregate_id, "
        "payload_json, correlation_id, idempotency_key, occurred_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            str(event_id),
            event_type,
            aggregate_type,
            aggregate_id,
            payload_json,
            str(correlation_id),
            idempotency_key,
            now,
            now,
        ],
    )
    return event_id


def record_audit_entry(
    database: Database,
    *,
    command: str,
    correlation_id: EventId,
    outcome: str,
    actor: str = "cli",
    affected_records_json: str = "[]",
    before_summary: str | None = None,
    after_summary: str | None = None,
) -> EventId:
    audit_id = EventId.new()
    database.execute(
        "INSERT INTO audit_log (audit_id, actor, command, correlation_id, outcome, "
        "affected_records_json, before_summary, after_summary, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            str(audit_id),
            actor,
            command,
            str(correlation_id),
            outcome,
            affected_records_json,
            before_summary,
            after_summary,
            database.now(),
        ],
    )
    return audit_id
