"""Connection, transaction, and writer-lock safety for the learner database."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.db import locks
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.paths import WorkspacePaths, assert_safe_destructive_target, assert_within

CONNECTION_CONFIGURATION = {"preserve_insertion_order": "true"}


def quote_identifier(name: str) -> str:
    """Quote an arbitrary DuckDB identifier for safe interpolation.

    Table and column names read back from the catalog are attacker-controlled data:
    anyone who can add a table can choose its name. Wrapping such a name in quotes
    without doubling the quotes it contains lets it terminate the identifier and append
    statements -- which turned `db backup`, a recovery command, into one that could drop
    a table. Every identifier that reaches SQL goes through here.
    """

    if "\x00" in name:
        raise LinguaWikiError(
            "invalid_identifier",
            "a SQL identifier cannot contain a NUL byte",
            details=(ErrorDetail(field="identifier", reason="contains a NUL byte"),),
        )
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def quote_identifiers(names: Iterable[str]) -> str:
    """Quote a comma-separated identifier list."""

    return ", ".join(quote_identifier(name) for name in names)


def resolve_database_path(paths: WorkspacePaths) -> Path:
    """Resolve the database path only from a validated workspace root."""

    root = assert_safe_destructive_target(paths.root, purpose="workspace")
    return assert_within(paths.database, root, purpose="database")


class Database:
    """Thin typed wrapper enforcing short, explicit transactions."""

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        *,
        clock: Clock,
        read_only: bool,
        path: Path,
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._read_only = read_only
        self._path = path
        self._in_transaction = False

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def path(self) -> Path:
        return self._path

    @property
    def clock(self) -> Clock:
        return self._clock

    def now(self) -> datetime:
        """Current storage-shaped (naive UTC) timestamp."""

        return naive_utc(self._clock.now())

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> None:
        self._connection.execute(sql, list(parameters))

    def query(self, sql: str, parameters: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return self._connection.execute(sql, list(parameters)).fetchall()

    def one(self, sql: str, parameters: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        return self._connection.execute(sql, list(parameters)).fetchone()

    def scalar(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        row = self.one(sql, parameters)
        return None if row is None else row[0]

    def count(self, table: str) -> int:
        """Count a table's rows, quoting the identifier safely."""

        return int(self.scalar(f"SELECT count(*) FROM {quote_identifier(table)}"))

    def columns(self, table: str) -> list[tuple[str, str]]:
        return [
            (str(name), str(data_type))
            for name, data_type in self.query(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
                [table],
            )
        ]

    def table_names(self) -> list[str]:
        return [
            str(name)
            for (name,) in self.query(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
            )
        ]

    def has_table(self, table: str) -> bool:
        return table in self.table_names()

    def checkpoint(self) -> None:
        self._connection.execute("CHECKPOINT")

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """Run one short transaction; any failure rolls the whole unit back."""

        if self._read_only:
            raise LinguaWikiError("read_only_connection", "this connection cannot write")
        if self._in_transaction:
            raise LinguaWikiError(
                "nested_transaction", "transactions must stay short and non-nested"
            )
        self._connection.execute("BEGIN TRANSACTION")
        self._in_transaction = True
        try:
            yield self
        except BaseException:
            self._connection.execute("ROLLBACK")
            self._in_transaction = False
            raise
        self._connection.execute("COMMIT")
        self._in_transaction = False


def _connect(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    try:
        return duckdb.connect(str(path), read_only=read_only, config=dict(CONNECTION_CONFIGURATION))
    except (duckdb.ConnectionException, duckdb.IOException, duckdb.PermissionException) as exc:
        raise LinguaWikiError(
            "database_busy",
            "the learner database is held by another process; retry shortly",
            retryable=True,
            details=(
                ErrorDetail(
                    field="database", reason=type(exc).__name__, context={"path": str(path)}
                ),
            ),
        ) from exc


@contextmanager
def open_writer(
    paths: WorkspacePaths,
    *,
    command: str,
    clock: Clock | None = None,
    create: bool = False,
    allow_uninitialized: bool = False,
    allow_damaged: bool = False,
) -> Iterator[Database]:
    """Open the single writer connection under an application lock.

    The writer boundary itself refuses a database LinguaWiki did not create, so no
    caller can forget to check: that is how `confirm_remote` came to write settings and
    audit rows into an unrecognized database. Commands that legitimately write into an
    empty database pass `allow_uninitialized`.
    """

    from linguawiki.db.state import DatabaseState, assert_writable

    active_clock = clock or SystemClock()
    path = resolve_database_path(paths)
    if not create and not path.exists():
        raise LinguaWikiError(
            "database_not_found",
            f"learner database does not exist: {path}",
            details=(ErrorDetail(field="database", reason="missing database"),),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with locks.writer_lock(path, clock=active_clock, command=command):
        connection = _connect(path, read_only=False)
        database = Database(connection, clock=active_clock, read_only=False, path=path)
        try:
            state = assert_writable(database, command=command, allow_damaged=allow_damaged)
            if state is DatabaseState.EMPTY and not (create or allow_uninitialized):
                raise LinguaWikiError(
                    "database_not_initialized",
                    f"{path} has no schema yet; run 'linguawiki db init' first",
                    details=(ErrorDetail(field="database", reason="no applied migrations"),),
                )
            yield database
        finally:
            database.close()


@contextmanager
def open_reader(paths: WorkspacePaths, *, clock: Clock | None = None) -> Iterator[Database]:
    """Open a read-only connection for reports, context, and diagnostics."""

    active_clock = clock or SystemClock()
    path = resolve_database_path(paths)
    if not path.exists():
        raise LinguaWikiError(
            "database_not_found",
            f"learner database does not exist: {path}",
            details=(ErrorDetail(field="database", reason="missing database"),),
        )
    connection = _connect(path, read_only=True)
    database = Database(connection, clock=active_clock, read_only=True, path=path)
    try:
        yield database
    finally:
        database.close()


@contextmanager
def open_temporary(path: Path, *, clock: Clock | None = None) -> Iterator[Database]:
    """Open an arbitrary database file, used for backups and restore targets."""

    active_clock = clock or SystemClock()
    connection = _connect(path, read_only=False)
    database = Database(connection, clock=active_clock, read_only=False, path=path)
    try:
        yield database
    finally:
        database.close()


__all__ = [
    "Database",
    "aware_utc",
    "naive_utc",
    "open_reader",
    "open_temporary",
    "open_writer",
    "quote_identifier",
    "quote_identifiers",
    "resolve_database_path",
]
