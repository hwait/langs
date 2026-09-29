"""Regressions for the six defects round nineteen found in Stage 5.

The theme is a condition that was *nearly* right: "present" standing in for "present and
unaltered", "seen in this package" for "available to rest a claim on", "names a source" for
"is an excerpt of one". Each nearly-right condition let something through that the rule it
implemented was written to stop, and two of them destroyed a learner's recording.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import database as database_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from linguawiki.services import speaking as speaking_service
from tests.conftest import PolishWorkspace

ART = "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TWIN = "art_01ARZ3NDEKTSV4RRFFQ69G5FB9"


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


def entry(
    relative: str,
    digest: str,
    *,
    artifact_id: str = ART,
    kind: str = "audio",
    retained: bool = True,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "relative_path": relative,
        "sha256": digest,
        "retained": retained,
    }


def confirmed(artifact_id: str = ART) -> dict[str, Any]:
    return {
        "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB1",
        "kind": "pronunciation.assessment",
        "occurred_at": "2026-01-01T10:02:00Z",
        "payload": {
            "status": "confirmed",
            "utterance_id": "utt_001",
            "audio_artifact_id": artifact_id,
        },
    }


def recording(root: Path, name: str = "call.opus") -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OggS" + name.encode() + b"\x00" * 32)
    return f"imports/{name}", hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_count(workspace: PolishWorkspace) -> int:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return int(database.scalar("SELECT count(*) FROM artifacts"))


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


# --- 1. A deletion must identify what it deletes -------------------------------------


def test_a_declination_that_misidentifies_its_file_deletes_nothing(
    running: PolishWorkspace,
) -> None:
    """The file this deleted belonged to another recording entirely."""

    relative, digest = recording(running.root, "someone-elses.opus")
    malformed = package(artifacts=(entry(relative, "b" * 64, retained=False),))
    reviewed = speaking_service.validate(
        running.paths, package=malformed, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=malformed, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "package_audio_unavailable"
    assert (running.root / relative).is_file()
    assert artifact_count(running) == 0

    # Correctly identified, the same declaration is honoured.
    honest = package(
        package_id="pkg_honest",
        external_session_id="call-honest",
        artifacts=(entry(relative, digest, retained=False),),
    )
    speaking_service.ingest(
        running.paths, package=honest, track=running.track_id, clock=running.clock
    )
    assert not (running.root / relative).exists()


def test_a_declination_is_honoured_again_when_the_bytes_come_back(
    running: PolishWorkspace,
) -> None:
    """Skipping a known identifier left restored bytes sitting there."""

    relative, digest = recording(running.root)
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        retained=False,
        external_id=ART,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    (running.root / relative).write_bytes(b"OggS" + b"call.opus" + b"\x00" * 32)
    speaking_service.ingest(
        running.paths,
        package=package(artifacts=(entry(relative, digest, retained=False),)),
        track=running.track_id,
        clock=running.clock,
    )
    assert not (running.root / relative).exists()
    assert artifact_count(running) == 1, "and no second row for it"


# --- 2. Collisions are about content, whatever the retention says --------------------


def test_one_recording_declared_twice_with_different_retention_is_refused(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root, "one.opus")
    twin = running.root / "imports" / "two.opus"
    twin.write_bytes((running.root / relative).read_bytes())
    mixed = package(
        artifacts=(
            entry(relative, digest),
            entry("imports/two.opus", digest, artifact_id=TWIN, retained=False),
        )
    )
    reviewed = speaking_service.validate(
        running.paths, package=mixed, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=mixed, track=running.track_id, clock=running.clock
        )
    assert artifact_count(running) == 0, "a refused package wrote nothing"
    assert (running.root / relative).is_file()
    assert twin.is_file()


def test_a_confirmed_claim_cannot_cite_audio_the_package_says_was_not_kept(
    running: PolishWorkspace,
) -> None:
    """Caught while fixing finding 2: the collision set is not the availability set.

    Populating one structure for both questions made "we have seen these bytes" answer
    "a claim may rest on them", and a package declaring its own audio unkept satisfied a
    `confirmed` event.
    """

    relative, digest = recording(running.root)
    unkept = package(artifacts=(entry(relative, digest, retained=False),), events=(confirmed(),))
    reviewed = speaking_service.validate(
        running.paths, package=unkept, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=unkept, track=running.track_id, clock=running.clock
        )


# --- 3. "Present" is not "the file we registered" ------------------------------------


def test_a_restored_recording_replaces_one_that_is_genuinely_gone(
    running: PolishWorkspace,
) -> None:
    """Presence alone was not enough, and neither is absence of a match.

    Round nineteen fixed the case where the registered path held *altered* bytes by
    repointing the row to the restored copy, which left those bytes under a private root
    with nothing accounting for them. Round twenty refuses that case -- see
    `test_altered_bytes_at_the_registered_path_are_not_silently_abandoned` -- so what
    repointing is for is the file being *gone*.
    """

    relative, digest = recording(running.root, "original.opus")
    held = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    good = (running.root / relative).read_bytes()
    (running.root / relative).unlink()
    restored = running.root / "imports" / "restored.opus"
    restored.write_bytes(good)

    again = artifact_service.register(
        running.paths,
        relative_path="imports/restored.opus",
        kind="audio",
        track=running.track_id,
        clock=running.clock,
    )
    assert again.artifact_id == held.artifact_id
    assert restored.is_file(), "the correct recording was deleted"
    assert again.relative_path == "imports/restored.opus"
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.ok


def test_altered_bytes_at_the_registered_path_are_not_silently_abandoned(
    running: PolishWorkspace,
) -> None:
    """Round twenty: repointing left unidentifiable bytes under a private root.

    Deleting them would destroy a file the workspace cannot identify, and abandoning them
    recreates the state a not-retained declaration exists to prevent. Both are a person's
    decision, so this refuses and says what the options are.
    """

    relative, digest = recording(running.root, "original.opus")
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    good = (running.root / relative).read_bytes()
    (running.root / relative).write_bytes(b"TAMPERED" + b"\x00" * 40)
    restored = running.root / "imports" / "restored.opus"
    restored.write_bytes(good)

    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/restored.opus",
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_altered_at_registered_path"
    # Nothing moved, nothing was deleted, and `verify` says what is wrong.
    assert restored.is_file()
    assert (running.root / relative).is_file()
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not verified.ok
    assert verified.altered

    # Settling the altered file is what unblocks the recovery.
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


def test_re_registering_an_altered_file_reports_the_alteration(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root, "drifting.opus")
    first = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    (running.root / relative).write_bytes(b"DIFFERENT" + b"\x00" * 40)
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path=relative,
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_altered"
    assert artifact_count(running) == 1
    assert first.artifact_id

    # And `verify` is what reports it, as the refusal says.
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not verified.ok
    assert verified.altered


# --- 4. "Available" means playable ---------------------------------------------------


def test_audio_deleted_by_hand_is_not_available(running: PolishWorkspace) -> None:
    relative, digest = recording(running.root)
    sound = package(artifacts=(entry(relative, digest),))
    first = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert first.audio_available

    (running.root / relative).unlink()
    again = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert again.duplicate
    assert not again.audio_available


def test_audio_altered_on_disk_is_not_available(running: PolishWorkspace) -> None:
    relative, digest = recording(running.root)
    sound = package(artifacts=(entry(relative, digest),))
    session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    (running.root / relative).write_bytes(b"SOMETHING ELSE" + b"\x00" * 32)
    again = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert not again.audio_available, "a claim cannot rest on bytes nobody registered"


# --- 5. Review answers the question ingestion will be asked -------------------------


def test_review_checks_the_session_ingestion_would_use(running: PolishWorkspace) -> None:
    closed = session_service.show(
        running.paths, track=running.track_id, clock=running.clock
    ).session_id
    session_service.close(
        running.paths, session=closed, track=running.track_id, clock=running.clock
    )
    session_service.create(running.paths, minutes=60, track=running.track_id, clock=running.clock)
    session_service.start(running.paths, track=running.track_id, clock=running.clock)

    # Against the active session, this package is fine.
    assert speaking_service.validate(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    ).valid
    # Against the session ingestion was actually told to use, it is not -- and review says
    # so rather than approving a package the ingest will refuse.
    reviewed = speaking_service.validate(
        running.paths,
        package=package(),
        session=closed,
        track=running.track_id,
        clock=running.clock,
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths,
            package=package(),
            session=closed,
            track=running.track_id,
            clock=running.clock,
        )


def test_the_cli_review_takes_the_session_too() -> None:
    from linguawiki.cli import _parser

    groups = _parser()._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    actions = groups["speaking"]._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    options = {
        option for action in actions["validate"]._actions for option in action.option_strings
    }
    assert "--session" in options


# --- 6. A clip is an excerpt of something else --------------------------------------


def test_db_check_finds_a_recording_recorded_as_a_clip_of_itself(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root, "whole.opus")
    whole = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE artifacts SET clip_of_artifact_id = artifact_id, clip_starts_at_ms = 0, "
            "clip_ends_at_ms = 4000 WHERE artifact_id = ?",
            [whole.artifact_id],
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "clip_provenance" in {check.name for check in report.failures}


def test_db_check_finds_offsets_with_no_recording_to_be_offsets_into(
    running: PolishWorkspace,
) -> None:
    """A row keyed on the source was invisible to the query that looked for one."""

    relative, digest = recording(running.root, "whole.opus")
    whole = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE artifacts SET clip_starts_at_ms = 0, clip_ends_at_ms = 4000 "
            "WHERE artifact_id = ?",
            [whole.artifact_id],
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "clip_provenance" in {check.name for check in report.failures}


def test_a_clip_identical_to_its_source_is_refused(running: PolishWorkspace) -> None:
    """The reachable form at the command: dedup would have returned the source row."""

    relative, digest = recording(running.root, "whole.opus")
    whole = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    copy = running.root / "imports" / "same.opus"
    copy.write_bytes((running.root / relative).read_bytes())
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/same.opus",
            kind="audio",
            clip_of=whole.artifact_id,
            clip_starts_at_ms=0,
            clip_ends_at_ms=4_000,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "clip_of_itself"
