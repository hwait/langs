"""Regressions for the three defects round twenty-one found in Stage 5.

All three are one root cause wearing three faces: a registered path was read by four
different pieces of code, and each trusted it differently. The fix is `contained_file`,
which every read now goes through -- so "absent", "escaping the workspace", and "unreadable"
are one answer with three names instead of three separate oversights.

The tombstone case is the same shape a level up: the preflight asked one question of the
artifact store and registration asked another, so a purged recording was invisible to the
first and acted on by the second.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from linguawiki.services import speaking as speaking_service
from tests.conftest import PolishWorkspace

ART = "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def package(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_a",
        "external_session_id": "call-a",
        "target_language": "pl",
        "mode": "completed",
        "started_at": "2026-01-01T10:00:00Z",
        "ended_at": "2026-01-01T10:20:00Z",
        "transcript_layers": [
            {
                "kind": "raw",
                "derived_from": None,
                "utterances": [
                    {
                        "utterance_id": "utt_001",
                        "speaker": "learner",
                        "started_at": "2026-01-01T10:01:00Z",
                        "ended_at": "2026-01-01T10:01:06Z",
                        "text": "chcialbym kupic bilet do Krakowa na jutro",
                    }
                ],
            }
        ],
        "events": [],
        "artifacts": [],
    }
    payload.update(overrides)
    return payload


def entry(relative: str, digest: str, *, artifact_id: str = ART) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": "audio",
        "relative_path": relative,
        "sha256": digest,
        "retained": True,
    }


def confirmed() -> dict[str, Any]:
    return {
        "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB1",
        "kind": "pronunciation.assessment",
        "occurred_at": "2026-01-01T10:02:00Z",
        "payload": {
            "status": "confirmed",
            "utterance_id": "utt_001",
            "audio_artifact_id": ART,
        },
    }


def recording(root: Path, name: str = "call.opus") -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OggS" + name.encode() + b"\x00" * 32)
    return f"imports/{name}", hashlib.sha256(path.read_bytes()).hexdigest()


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


def _purged(running: PolishWorkspace) -> tuple[str, str]:
    """A recording the learner had deleted, and the bytes offered again afterwards."""

    relative, digest = recording(running.root)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id=ART,
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
    assert not (running.root / relative).exists()
    (running.root / relative).write_bytes(b"OggS" + b"call.opus" + b"\x00" * 32)
    return relative, digest


# --- 1. A purged recording does not come back ----------------------------------------


def test_a_package_cannot_resurrect_a_purged_recording(running: PolishWorkspace) -> None:
    """And the re-supplied file is left alone: this used to delete it."""

    relative, digest = _purged(running)
    resurrecting = package(artifacts=(entry(relative, digest),), events=(confirmed(),))

    reviewed = speaking_service.validate(
        running.paths, package=resurrecting, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("purged at the learner's request" in problem for problem in reviewed.problems)

    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=resurrecting, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "package_audio_unavailable"
    assert (running.root / relative).is_file(), "the re-supplied recording was deleted"
    with open_reader(running.paths, clock=running.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM artifacts")) == 1


def test_registering_a_purged_recording_again_is_refused_by_name(
    running: PolishWorkspace,
) -> None:
    relative, digest = _purged(running)
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path=relative,
            kind="audio",
            expected_sha256=digest,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_purged"
    assert "learner-request" in str(failure.value)
    assert (running.root / relative).is_file()


def test_a_purged_identifier_does_not_count_as_available_audio(
    running: PolishWorkspace,
) -> None:
    """A confirmed event citing it must not pass on the strength of a tombstone."""

    relative, digest = _purged(running)
    (running.root / relative).unlink()
    reviewed = speaking_service.validate(
        running.paths,
        package=package(events=(confirmed(),)),
        track=running.track_id,
        clock=running.clock,
    )
    assert not reviewed.valid


# --- 2. Containment is rechecked on every read ---------------------------------------


def _escaped(running: PolishWorkspace, tmp_path: Path) -> tuple[str, str, Path]:
    """A registered recording whose path has been replaced by an escaping symlink."""

    relative, digest = recording(running.root)
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id=ART,
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
    return relative, digest, elsewhere


def test_verify_reports_a_path_that_now_leads_out_of_the_workspace(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    relative, _, elsewhere = _escaped(running, tmp_path)
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not verified.ok
    assert verified.escaped
    assert verified.present == 0, "a file outside the workspace is not this recording"
    assert any("outside this workspace" in warning for warning in verified.warnings)
    assert elsewhere.is_file(), "and nothing out there was touched"


def test_audio_reached_through_an_escaping_path_is_not_available(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    """The preflight refuses an escaping path on the way in, so the reachable case is a
    retry of content already ingested -- which this command accepts as a no-op and then
    reports on. Reporting the recording as available would let a file outside the workspace
    stand in for the learner's."""

    relative, digest = recording(running.root)
    sound = package(artifacts=(entry(relative, digest),))
    first = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert first.audio_available

    body = (running.root / relative).read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    elsewhere = outside / "copy.opus"
    elsewhere.write_bytes(body)
    (running.root / relative).unlink()
    (running.root / relative).symlink_to(elsewhere)

    again = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert again.duplicate
    assert not again.audio_available


def test_a_genuine_copy_is_not_deleted_for_an_escaping_registered_path(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    """The outside target was treated as canonical and the real copy deleted as a duplicate."""

    relative, _, elsewhere = _escaped(running, tmp_path)
    body = elsewhere.read_bytes()
    inside = running.root / "imports" / "restored.opus"
    inside.write_bytes(body)

    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/restored.opus",
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_escaped_the_workspace"
    assert inside.is_file(), "the genuine in-workspace copy was deleted"
    assert elsewhere.is_file()

    # Putting the path back is what unblocks the recovery.
    (running.root / relative).unlink()
    recovered = artifact_service.register(
        running.paths,
        relative_path="imports/restored.opus",
        kind="audio",
        track=running.track_id,
        clock=running.clock,
    )
    assert recovered.relative_path == "imports/restored.opus"
    assert artifact_service.verify(running.paths, track=running.track_id, clock=running.clock).ok


# --- 3. Read failures are answers, in every command ---------------------------------


def _unreadable(name: str = "call.opus") -> Any:
    real = artifact_service.file_digest

    def reader(path: Path) -> str:
        if path.name == name:
            raise PermissionError("Operation not permitted")
        return real(path)

    return mock.patch.object(artifact_service, "file_digest", reader)


def test_verify_reports_an_unreadable_file_rather_than_raising(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    with _unreadable():
        verified = artifact_service.verify(
            running.paths, track=running.track_id, clock=running.clock
        )
    assert not verified.ok
    assert verified.unreadable == (registered.artifact_id,)
    assert verified.altered == (), "unreadable is not the same claim as altered"
    assert any("cannot be read" in warning for warning in verified.warnings)


def test_registering_an_unreadable_file_is_refused_by_name(
    running: PolishWorkspace,
) -> None:
    recording(running.root)
    with _unreadable(), pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/call.opus",
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_unreadable"
