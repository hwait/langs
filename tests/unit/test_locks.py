from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from linguawiki.db import locks
from linguawiki.errors import LinguaWikiError
from tests.support.clocks import AdvancingClock


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "linguawiki.duckdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return path


def _hold_in_child(database: Path) -> subprocess.Popen[str]:
    """Hold the writer lock in a separate process until its stdin closes."""

    program = textwrap.dedent(
        f"""
        import sys
        from datetime import UTC, datetime
        from pathlib import Path
        from linguawiki.db import locks

        class Clock:
            def now(self):
                return datetime(2026, 1, 1, tzinfo=UTC)

        locks.acquire(Path({str(database)!r}), clock=Clock(), command="child.writer")
        print("held", flush=True)
        sys.stdin.read()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "held"
    return child


def test_lock_records_the_owner_for_diagnostics(database: Path, clock: AdvancingClock) -> None:
    holder = locks.acquire(database, clock=clock, command="db.migrate")
    payload = locks.read_holder(locks.lock_path(database))

    assert payload is not None
    assert payload["pid"] == os.getpid()
    assert payload["command"] == "db.migrate"
    assert locks.held(database) is not None

    locks.release(database, holder)
    assert locks.held(database) is None


def test_release_clears_the_payload_but_keeps_the_lock_file(
    database: Path, clock: AdvancingClock
) -> None:
    """The file is never unlinked; unlinking is what made recovery racy."""

    holder = locks.acquire(database, clock=clock, command="db.migrate")
    locks.release(database, holder)

    assert locks.lock_path(database).is_file()
    assert locks.lock_path(database).read_bytes() == b""
    assert locks.read_holder(locks.lock_path(database)) is None


def test_a_second_writer_receives_a_retryable_error(database: Path, clock: AdvancingClock) -> None:
    holder = locks.acquire(database, clock=clock, command="db.migrate")
    try:
        with pytest.raises(LinguaWikiError) as failure:
            locks.acquire(database, clock=clock, command="db.backup")
    finally:
        locks.release(database, holder)

    payload = failure.value.payload
    assert payload.code == "writer_locked"
    assert payload.retryable is True
    assert payload.details[0].context["command"] == "db.migrate"


def test_a_second_process_cannot_take_a_held_lock(database: Path, clock: AdvancingClock) -> None:
    child = _hold_in_child(database)
    try:
        with pytest.raises(LinguaWikiError) as failure:
            locks.acquire(database, clock=clock, command="db.backup")
        probed = locks.held(database)
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)

    assert failure.value.payload.code == "writer_locked"
    assert failure.value.payload.details[0].context["command"] == "child.writer"
    assert probed is not None
    assert probed["command"] == "child.writer"


def test_a_lock_left_by_a_dead_process_is_released_by_the_operating_system(
    database: Path, clock: AdvancingClock
) -> None:
    child = _hold_in_child(database)
    child.kill()
    child.wait(timeout=30)

    holder = locks.acquire(database, clock=clock, command="db.backup")

    assert locks.read_holder(locks.lock_path(database))["pid"] == os.getpid()  # type: ignore[index]
    locks.release(database, holder)


def test_a_half_written_payload_never_frees_a_held_lock(
    database: Path, clock: AdvancingClock
) -> None:
    """A torn payload is unreadable, but ownership comes from the advisory lock."""

    holder = locks.acquire(database, clock=clock, command="db.migrate")
    locks.lock_path(database).write_text('{"pid": ', encoding="utf-8")
    try:
        with pytest.raises(LinguaWikiError) as failure:
            locks.acquire(database, clock=clock, command="db.backup")
    finally:
        locks.release(database, holder)

    assert failure.value.payload.code == "writer_locked"
    assert locks.read_holder(locks.lock_path(database)) is None
    assert failure.value.payload.details[0].context == {"lock_path": str(locks.lock_path(database))}


def test_a_stale_payload_without_a_holder_does_not_block_a_writer(
    database: Path, clock: AdvancingClock
) -> None:
    """A leftover payload from a crash is diagnostics only, never a blocker."""

    locks.lock_path(database).parent.mkdir(parents=True, exist_ok=True)
    locks.lock_path(database).write_text(
        json.dumps({"pid": 2**31 - 1, "hostname": "other-host", "command": "abandoned"}),
        encoding="utf-8",
    )

    assert locks.held(database) is None
    holder = locks.acquire(database, clock=clock, command="db.backup")
    assert locks.read_holder(locks.lock_path(database))["command"] == "db.backup"  # type: ignore[index]
    locks.release(database, holder)


def test_probing_an_absent_lock_file_reports_no_holder(database: Path) -> None:
    assert locks.held(database) is None
    assert not locks.lock_path(database).exists()


def test_writer_lock_context_manager_always_releases(database: Path, clock: AdvancingClock) -> None:
    with pytest.raises(RuntimeError), locks.writer_lock(database, clock=clock, command="db.init"):
        raise RuntimeError("boom")

    assert locks.held(database) is None


def test_releasing_twice_is_harmless(database: Path, clock: AdvancingClock) -> None:
    holder = locks.acquire(database, clock=clock, command="db.init")

    locks.release(database, holder)
    locks.release(database, holder)

    assert locks.held(database) is None


def test_an_unreadable_lock_payload_is_reported_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "payload.lock"
    path.write_text("not json", encoding="utf-8")

    assert locks.read_holder(path) is None
    assert locks.read_holder(tmp_path / "absent.lock") is None
