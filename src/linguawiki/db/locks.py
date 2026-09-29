"""Single-writer application locking adjacent to the learner database.

Exclusion comes from a non-blocking advisory lock held on an open file descriptor,
never from reading and unlinking a lock file. Two callers therefore cannot both
decide a lock is abandoned, a lock left by a dead process is released by the
operating system, and a half-written payload can never be mistaken for a free lock:
the payload is diagnostics only and is never consulted to decide ownership.

This lock exists to turn contention into an immediate, explanatory, retryable error.
It is not the last line of defence: DuckDB holds its own exclusive lock on the
database file, so even a caller that deletes this lock file cannot open a second
writer -- it gets `database_busy` instead. Both layers are covered by tests.
"""

from __future__ import annotations

import errno
import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from linguawiki import __version__
from linguawiki.clock import Clock
from linguawiki.errors import ErrorDetail, LinguaWikiError

LOCK_SUFFIX = ".lock"
_CONTENDED_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})

try:  # pragma: no cover - exercised by platform, not by tests
    import fcntl
except ImportError:  # pragma: no cover - only reachable off POSIX
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class LockHolder:
    """An acquired writer lock. The descriptor keeps the advisory lock alive."""

    descriptor: int
    pid: int
    hostname: str
    command: str
    application_version: str
    acquired_at: datetime

    def as_payload(self) -> dict[str, str | int]:
        return {
            "pid": self.pid,
            "hostname": self.hostname,
            "command": self.command,
            "application_version": self.application_version,
            "acquired_at": self.acquired_at.isoformat().replace("+00:00", "Z"),
        }


def lock_path(database: Path) -> Path:
    return database.with_name(database.name + LOCK_SUFFIX)


def read_holder(path: Path) -> dict[str, object] | None:
    """Read a lock payload for diagnostics; never used to decide ownership."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _require_fcntl() -> None:
    if fcntl is None:  # pragma: no cover - only reachable off POSIX
        raise LinguaWikiError(
            "locking_unsupported",
            "this platform has no advisory file locking, so single-writer safety "
            "cannot be guaranteed",
        )


def _locked_error(path: Path) -> LinguaWikiError:
    payload = read_holder(path) or {}
    context = {key: str(value) for key, value in payload.items()}
    context["lock_path"] = str(path)
    return LinguaWikiError(
        "writer_locked",
        "another LinguaWiki writer holds this workspace; retry when it finishes",
        retryable=True,
        details=(ErrorDetail(field="database", reason="writer lock held", context=context),),
    )


def _flock(descriptor: int, path: Path) -> None:
    """Take the exclusive advisory lock or fail immediately."""

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in _CONTENDED_ERRNOS:
            raise _locked_error(path) from None
        raise LinguaWikiError(
            "locking_unsupported",
            f"advisory locking is unavailable for {path}; refusing to write unprotected",
            details=(
                ErrorDetail(field="database", reason=errno.errorcode.get(exc.errno or 0, "")),
            ),
        ) from exc


def acquire(database: Path, *, clock: Clock, command: str) -> LockHolder:
    """Take the writer lock or fail immediately with a retryable error."""

    _require_fcntl()
    path = lock_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    _flock(descriptor, path)
    holder = LockHolder(
        descriptor=descriptor,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        command=command,
        application_version=__version__,
        acquired_at=clock.now(),
    )
    payload = json.dumps(holder.as_payload(), sort_keys=True).encode("utf-8")
    os.ftruncate(descriptor, 0)
    os.pwrite(descriptor, payload, 0)
    os.fsync(descriptor)
    return holder


def release(database: Path, holder: LockHolder) -> None:
    """Clear the payload and drop the advisory lock this process holds."""

    with suppress(OSError):
        os.ftruncate(holder.descriptor, 0)
        os.fsync(holder.descriptor)
        fcntl.flock(holder.descriptor, fcntl.LOCK_UN)
    with suppress(OSError):
        os.close(holder.descriptor)
    # The file itself is left in place: unlinking is what makes lock recovery racy.


def held(database: Path) -> dict[str, object] | None:
    """Probe whether a writer currently holds the lock, without taking it."""

    path = lock_path(database)
    if fcntl is None or not path.is_file():  # pragma: no cover - off-POSIX branch
        return None
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return read_holder(path) or {}
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return None
    finally:
        os.close(descriptor)


@contextmanager
def writer_lock(database: Path, *, clock: Clock, command: str) -> Iterator[LockHolder]:
    holder = acquire(database, clock=clock, command=command)
    try:
        yield holder
    finally:
        release(database, holder)
