"""The single classification of a database file, shared by every command that writes.

Deciding "is this ours, and is it empty?" in more than one place is how `db init` and
`db migrate` came to disagree: one refused an unrelated DuckDB file and the other wrote
seven migrations into it. Every writer now asks this module.
"""

from __future__ import annotations

from enum import StrEnum

from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database
from linguawiki.db.schema import assert_matches_schema
from linguawiki.errors import ErrorDetail, LinguaWikiError


class DatabaseState(StrEnum):
    """What a database file is, from the point of view of a command that may write.

    "Ours" and "safe to write" are not the same question: a database whose migration
    history is corrupt, or which was written by a newer release, is recognisably ours
    and must still be refused by ordinary writers so it can be inspected and backed up
    before anything changes it.
    """

    EMPTY = "empty"
    MANAGED = "managed"
    DAMAGED = "damaged"
    UNSUPPORTED = "unsupported"
    UNMANAGED = "unmanaged"


#: States an ordinary writing command may proceed against.
WRITABLE_STATES = frozenset({DatabaseState.EMPTY, DatabaseState.MANAGED})
#: States that are recognisably ours but must not be modified.
INSPECTABLE_STATES = frozenset({DatabaseState.DAMAGED, DatabaseState.UNSUPPORTED})


def _establishes_ownership(database: Database) -> bool:
    """Whether the raw history names a migration this release published.

    Read as text, so a database whose history table has itself drifted is still
    recognised as ours instead of being disowned and made unrecoverable.
    """

    recorded = migration_module.recorded_migration_ids(database)
    if not recorded:
        return False
    published = {migration.migration_id for migration in migration_module.migrations()}
    return any(name in published for name in recorded)


def classify_database(database: Database) -> DatabaseState:
    """Classify a database file: empty, ours and sound, ours and damaged, or foreign.

    The order matters. Ownership is decided from the raw history, which needs no typed
    decoding; then the complete schema — including the history table's own columns —
    decides soundness; only then is decoding the typed history safe. Decoding earlier
    meant drift *inside* `schema_migrations` either disowned the database or raised an
    unexpected error, in both cases losing the native recovery path.
    """

    tables = frozenset(database.table_names())
    if not tables:
        return DatabaseState.EMPTY
    if not _establishes_ownership(database):
        return DatabaseState.UNMANAGED
    try:
        assert_matches_schema(
            {table: database.columns(table) for table in tables},
            schema_version=migration_module.claimed_schema_version(database),
            source=str(database.path),
            code="database_damaged",
        )
    except LinguaWikiError:
        # Recognisably ours, but its layout is not what its history says it should be.
        return DatabaseState.DAMAGED
    try:
        migration_module.assert_history_matches_package(database)
    except LinguaWikiError as exc:
        return (
            DatabaseState.UNSUPPORTED
            if exc.payload.code == "schema_ahead_of_core"
            else DatabaseState.DAMAGED
        )
    return DatabaseState.MANAGED


def schema_divergence(database: Database) -> str | None:
    """Describe how a database's layout differs from what its history implies."""

    if not _establishes_ownership(database):
        return None
    try:
        assert_matches_schema(
            {table: database.columns(table) for table in database.table_names()},
            schema_version=migration_module.claimed_schema_version(database),
            source=str(database.path),
            code="database_damaged",
        )
    except LinguaWikiError as exc:
        return "; ".join(
            f"{detail.field}: {detail.reason}"
            + (f" (found {detail.context['actual']})" if "actual" in detail.context else "")
            for detail in exc.payload.details
        )
    return None


def assert_writable(
    database: Database, *, command: str, allow_damaged: bool = False
) -> DatabaseState:
    """Refuse to write into a database that is not ours, or not safe to change.

    `allow_damaged` is for commands that only read and copy — `db backup` and
    `db export-portable` — so a damaged database can still be preserved before repair.
    """

    state = classify_database(database)
    if state is DatabaseState.UNMANAGED:
        raise LinguaWikiError(
            "database_not_empty",
            f"{database.path} holds data that LinguaWiki did not create; {command} will "
            "not write into it",
            details=(
                ErrorDetail(
                    field="database",
                    reason="unrecognized database contents",
                    context={"tables": ", ".join(sorted(database.table_names())[:10])},
                ),
            ),
        )
    if state in INSPECTABLE_STATES and not allow_damaged:
        code = "database_unsupported" if state is DatabaseState.UNSUPPORTED else "database_damaged"
        reason = (
            "written by a newer release"
            if state is DatabaseState.UNSUPPORTED
            else "migration history does not match this release"
        )
        raise LinguaWikiError(
            code,
            f"{database.path} is a LinguaWiki database whose {reason}; {command} will not "
            "change it. Run 'linguawiki db check' and back it up before repairing it",
            details=(ErrorDetail(field="database", reason=reason, context={"state": str(state)}),),
        )
    return state
