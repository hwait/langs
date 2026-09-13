"""Regressions for the three defects round twenty-two found in Stage 5.

Finding 2 is the "every read" rule for the third time: `contained_file` was introduced last
round as the single way to read a path a row names, and two readers were left joining the
root by hand. Its real lesson is in the invariant, not here -- but the two readers are pinned
so the next one that appears has company.

The other two are about a tombstone's path being history rather than a claim, and about
`is_file` propagating the errno values it does not consider ignorable.
"""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import privacy as privacy_service
from linguawiki.services import sessions as session_service
from tests.conftest import PolishWorkspace


def write(root: Path, name: str, body: bytes) -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return f"imports/{name}", hashlib.sha256(body).hexdigest()


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    learner_service.update_track(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        preferences=learner_service.TrackPreferences(audio_retention_consent=True),
        clock=polish_workspace.clock,
    )
    session_service.create(
        polish_workspace.paths,
        minutes=60,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    session_service.start(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    return polish_workspace


# --- 1. A tombstone's path is history, not a claim on it ----------------------------


def test_recording_a_new_conversation_at_a_purged_filename_is_clean(
    running: PolishWorkspace,
) -> None:
    """This was a permanent integrity failure that nothing could clear.

    The learner purges a conversation, then records a new one and -- reasonably -- reuses
    the filename. The tombstone still named that path, so every `verify` and every privacy
    audit reported the deleted recording as having come back, for good.
    """

    relative, digest = write(running.root, "session.opus", b"OggS first" + b"\x00" * 20)
    first = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    artifact_service.purge(
        running.paths,
        artifact=first.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    assert not (running.root / relative).exists()

    again, again_digest = write(
        running.root, "session.opus", b"OggS a completely new one" + b"\x00" * 20
    )
    second = artifact_service.register(
        running.paths,
        relative_path=again,
        kind="audio",
        expected_sha256=again_digest,
        track=running.track_id,
        clock=running.clock,
    )
    assert second.artifact_id != first.artifact_id

    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.ok, "the tombstone's old path is not evidence of resurrection"
    assert verified.purged == (first.artifact_id,)
    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.retention.unkept_files_still_present == ()
    assert audited.ok


def test_a_purged_recording_really_coming_back_is_still_reported(
    running: PolishWorkspace,
) -> None:
    """The check still has to work: only a *live* row's ownership excuses the path."""

    relative, digest = write(running.root, "session.opus", b"OggS first" + b"\x00" * 20)
    first = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    artifact_service.purge(
        running.paths,
        artifact=first.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    # Restored from a backup, with nothing else registered at that path.
    (running.root / relative).write_bytes(b"OggS first" + b"\x00" * 20)

    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not verified.ok
    assert any("present again" in entry for entry in verified.altered)
    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.retention.unkept_files_still_present == (first.artifact_id,)


# --- 2. Every reader of a stored path resolves it ----------------------------------


def _escaped(running: PolishWorkspace, tmp_path: Path) -> tuple[str, Path]:
    relative, digest = write(running.root, "call.opus", b"OggS a recording" + b"\x00" * 20)
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    body = (running.root / relative).read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    elsewhere = outside / "copy.opus"
    elsewhere.write_bytes(body)
    (running.root / relative).unlink()
    (running.root / relative).symlink_to(elsewhere)
    return relative, elsewhere


def test_the_listing_does_not_call_an_escaping_path_present(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    """It contradicted `artifact verify` about the same file."""

    _escaped(running, tmp_path)
    listing = artifact_service.listing(running.paths, track=running.track_id, clock=running.clock)
    assert [entry.present for entry in listing.artifacts] == [False]
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.escaped, "and the two now agree"


def test_the_audit_does_not_call_an_outside_target_a_file_still_held(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    relative, digest = write(running.root, "gone.opus", b"OggS purgeable" + b"\x00" * 20)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    artifact_service.purge(
        running.paths,
        artifact=registered.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    elsewhere = outside / "resurrected.opus"
    elsewhere.write_bytes(b"OggS purgeable" + b"\x00" * 20)
    (running.root / relative).symlink_to(elsewhere)

    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.retention.unkept_files_still_present == ()
    assert elsewhere.is_file()


# --- 3. A permission failure is an answer, in register too -------------------------


def _denied(name: str) -> mock._patch[Any]:  # type: ignore[type-arg]
    """A permission failure with a real errno, which `Path.is_file` does not swallow."""

    real = Path.stat

    def denied(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == name:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    return mock.patch.object(Path, "stat", denied)


def test_registering_a_file_it_cannot_look_at_is_refused_by_name(
    running: PolishWorkspace,
) -> None:
    write(running.root, "locked.opus", b"OggS locked" + b"\x00" * 20)
    with _denied("locked.opus"), pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/locked.opus",
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_unreadable"


def test_a_genuinely_absent_file_is_still_reported_as_missing(
    running: PolishWorkspace,
) -> None:
    """Absent and unreadable are different facts, and the next move differs."""

    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/nothing.opus",
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_file_missing"


def test_verify_names_a_file_it_cannot_look_at_unreadable_not_escaped(
    running: PolishWorkspace,
) -> None:
    """Three kinds of "not a file here", and calling one by another's name misleads."""

    relative, digest = write(running.root, "locked.opus", b"OggS locked" + b"\x00" * 20)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    with _denied("locked.opus"):
        verified = artifact_service.verify(
            running.paths, track=running.track_id, clock=running.clock
        )
    assert not verified.ok
    assert verified.unreadable == (registered.artifact_id,)
    assert verified.escaped == (), "unreadable is not the same fact as escaping"
    assert verified.missing == (), "nor as absent"
