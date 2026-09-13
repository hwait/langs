"""Regressions for the eight defects round seventeen found in Stage 5.

Two themes. Most of these are *ordering*: a check that ran after a write, or a write that
happened before the checks that could refuse it, so a refused package still changed the
workspace. The rest are a fact the code did not carry -- whether a file is a recording,
whether the learner still keeps it, whether it is a clip or a whole conversation -- where
the absence let a claim rest on something that could not support it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import database as database_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import privacy as privacy_service
from linguawiki.services import sessions as session_service
from linguawiki.services import speaking as speaking_service
from linguawiki.services import transcripts as transcript_service
from tests.conftest import PolishWorkspace

ART = "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "art_01ARZ3NDEKTSV4RRFFQ69G5FB9"


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


def manifest(relative: str, digest: str, *, artifact_id: str = ART, kind: str = "audio") -> Any:
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "relative_path": relative,
        "sha256": digest,
        "retained": True,
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


def follow_up() -> dict[str, Any]:
    return {
        "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB7",
        "kind": "follow_up",
        "occurred_at": "2026-01-01T10:03:00Z",
        "payload": {"summary": "Practise ticket vocabulary."},
    }


def recording(root: Path, name: str = "call.opus") -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OggS" + name.encode() + b"\x00" * 32)
    return f"imports/{name}", hashlib.sha256(path.read_bytes()).hexdigest()


def held_artifacts(workspace: PolishWorkspace) -> tuple[str, ...]:
    listing = artifact_service.listing(
        workspace.paths, track=workspace.track_id, clock=workspace.clock
    )
    return tuple(entry.artifact_id for entry in listing.artifacts)


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


# --- 1. A refused package must change nothing ----------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"target_language": "de"}, "package_language_mismatch"),
        (
            {"track_hint": "trk_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
            "package_track_mismatch",
        ),
    ],
)
def test_a_package_refused_for_any_reason_registers_no_audio(
    running: PolishWorkspace, overrides: dict[str, Any], code: str
) -> None:
    """Registration happens after every refusal, not before the ones that ran later."""

    relative, digest = recording(running.root)
    refused = package(artifacts=(manifest(relative, digest),), **overrides)
    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths, package=refused, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == code
    assert held_artifacts(running) == ()
    assert (running.root / relative).is_file(), "the learner's file was left alone"


def test_a_package_for_a_closed_session_registers_no_audio(
    running: PolishWorkspace,
) -> None:
    session_service.close(running.paths, track=running.track_id, clock=running.clock)
    relative, digest = recording(running.root)
    with pytest.raises(LinguaWikiError):
        session_service.ingest_package(
            running.paths,
            package=package(artifacts=(manifest(relative, digest),)),
            track=running.track_id,
            clock=running.clock,
        )
    assert held_artifacts(running) == ()


def test_a_package_whose_event_cannot_be_materialized_registers_no_audio(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    unmaterializable = package(
        artifacts=(manifest(relative, digest),),
        events=(
            {
                "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB3",
                "kind": "attempt.observed",
                "occurred_at": "2026-01-01T10:02:00Z",
                # Neither a target nor a dimension: an attempt that measures nothing.
                "payload": {"utterance_id": "utt_001", "details": {}},
            },
        ),
    )
    with pytest.raises(LinguaWikiError):
        session_service.ingest_package(
            running.paths, package=unmaterializable, track=running.track_id, clock=running.clock
        )
    assert held_artifacts(running) == ()


def test_re_ingesting_after_a_purge_reports_the_duplicate_rather_than_refusing(
    running: PolishWorkspace,
) -> None:
    """A retry of content already here changes nothing, so it checks nothing about audio."""

    relative, digest = recording(running.root)
    sound = package(artifacts=(manifest(relative, digest),))
    first = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert not first.duplicate
    registered = held_artifacts(running)
    assert len(registered) == 1

    artifact_service.purge(
        running.paths,
        artifact=registered[0],
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    again = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert again.duplicate, "an exact retry is a duplicate, not an audio failure"
    assert again.staged_events == 0


# --- 2. Transcript refusals belong in the shared preflight ---------------------------


def test_a_reused_utterance_identifier_is_refused_before_anything_is_staged(
    running: PolishWorkspace,
) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    drifted = package(package_id="pkg_b", events=(follow_up(),))
    drifted["transcript_layers"][0]["utterances"][0]["text"] = "zupelnie inne slowa tutaj"

    reviewed = speaking_service.validate(
        running.paths, package=drifted, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid, "review must refuse what ingestion refuses"
    assert any("already held" in problem for problem in reviewed.problems)

    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=drifted, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "utterance_identity_reused"
    assert session_service.staged(running.paths, track=running.track_id, clock=running.clock) == ()
    with open_reader(running.paths, clock=running.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM session_packages")) == 0


# --- 3. A recording the learner declined to keep stays declined ----------------------


def test_a_package_cannot_revive_a_recording_the_learner_declined_to_keep(
    running: PolishWorkspace,
) -> None:
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
    assert not (running.root / relative).exists()
    # The bytes come back -- restored from a backup, or re-exported by the producer.
    (running.root / relative).write_bytes(b"OggS" + b"call.opus" + b"\x00" * 32)

    citing = package(
        package_id="pkg_citing",
        external_session_id="call-citing",
        artifacts=(manifest(relative, digest),),
        events=(confirmed(),),
    )
    reviewed = speaking_service.validate(
        running.paths, package=citing, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("chose not to keep" in problem for problem in reviewed.problems)
    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=citing, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "package_audio_unavailable"


# --- 4. A transcript file cannot support an acoustic claim ---------------------------


def test_a_package_cannot_call_a_transcript_artifact_audio(running: PolishWorkspace) -> None:
    text = running.root / "imports" / "notes.txt"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("plain text, certainly not a recording\n", encoding="utf-8")
    digest = hashlib.sha256(text.read_bytes()).hexdigest()
    artifact_service.register(
        running.paths,
        relative_path="imports/notes.txt",
        kind="transcript",
        external_id=OTHER,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    posing = package(
        artifacts=(manifest("imports/notes.txt", digest, artifact_id=OTHER),),
        events=(confirmed(OTHER),),
    )
    reviewed = speaking_service.validate(
        running.paths, package=posing, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=posing, track=running.track_id, clock=running.clock
        )


def test_db_check_finds_a_claim_resting_on_something_that_is_not_a_recording(
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
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="confirmed",
        basis="audio",
        utterance="utt_001",
        audio=registered.artifact_id,
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE artifacts SET kind = 'transcript'")
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "acoustic_claim_is_audio" in {check.name for check in report.failures}


def test_a_pronunciation_command_will_not_rest_on_a_transcript_file(
    running: PolishWorkspace,
) -> None:
    text = running.root / "imports" / "notes.txt"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("plain text\n", encoding="utf-8")
    registered = artifact_service.register(
        running.paths,
        relative_path="imports/notes.txt",
        kind="transcript",
        track=running.track_id,
        clock=running.clock,
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.record_pronunciation(
            running.paths,
            dimension="prosody",
            status="confirmed",
            basis="audio",
            audio=registered.artifact_id,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_is_not_audio"


# --- 5. The binding and the deletion share a transaction ----------------------------


def test_a_duplicate_whose_copy_cannot_be_deleted_binds_nothing(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root, "one.opus")
    held = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    duplicate = running.root / "imports" / "copy.opus"
    duplicate.write_bytes((running.root / relative).read_bytes())
    with (
        mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")),
        pytest.raises(LinguaWikiError) as failure,
    ):
        artifact_service.register(
            running.paths,
            relative_path="imports/copy.opus",
            kind="audio",
            external_id="art_late_binding",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_file_not_removed"
    with open_reader(running.paths, clock=running.clock) as database:
        bound = database.scalar(
            "SELECT external_id FROM artifacts WHERE artifact_id = ?", [held.artifact_id]
        )
    assert bound is None, "the binding went with the failure"
    assert duplicate.is_file()


# --- 6. The audit enumerates candidates once ----------------------------------------


def test_the_audit_reports_nothing_scanned_when_it_cannot_list_the_candidates(
    running: PolishWorkspace,
) -> None:
    """Two listings meant the content scan trusted the one whose failure it discarded."""

    import linguawiki.services.privacy as privacy_module

    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    page = running.root / "wiki" / "leak.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "The learner said: chcialbym kupic bilet do Krakowa na jutro\n", encoding="utf-8"
    )
    honest = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert not honest.ok
    assert len(honest.content_leaks) == 1

    unavailable = privacy_module.CandidateListing(
        source=privacy_module.CandidateSource.UNAVAILABLE, failure="git exploded"
    )
    with mock.patch.object(privacy_module, "candidate_listing", return_value=unavailable):
        blind = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert not blind.ok, "a scan that examined nothing is not a clean workspace"
    assert blind.unscanned, "and it says so"


# --- 7. Override provenance is structural ------------------------------------------


def test_the_schema_refuses_an_override_without_a_listener_or_a_reason(
    running: PolishWorkspace,
) -> None:
    uncertain = package()
    uncertain["transcript_layers"][0]["utterances"][0]["confidence"] = 0.2
    transcript_service.import_package(
        running.paths, package=uncertain, track=running.track_id, clock=running.clock
    )
    recorded = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
        despite_low_confidence=True,
        reviewer_kind="human",
        reviewer="the tutor",
        override_reason="I listened to the recording.",
        track=running.track_id,
        clock=running.clock,
    )
    for statement in (
        "UPDATE utterance_interpretations SET reviewer_kind = 'ai'",
        "UPDATE utterance_interpretations SET override_reason = NULL",
        "UPDATE utterance_interpretations SET override_reason = '   '",
    ):
        with (
            pytest.raises(Exception, match="CHECK|Constraint"),
            open_writer(running.paths, command="test", clock=running.clock) as database,
            database.transaction() as transaction,
        ):
            transaction.execute(
                f"{statement} WHERE interpretation_id = ?", [recorded.interpretation_id]
            )


def test_a_blank_override_reason_is_refused(running: PolishWorkspace) -> None:
    uncertain = package()
    uncertain["transcript_layers"][0]["utterances"][0]["confidence"] = 0.2
    transcript_service.import_package(
        running.paths, package=uncertain, track=running.track_id, clock=running.clock
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.interpret(
            running.paths,
            utterance="utt_001",
            classification="learner-error",
            category="orthography",
            corrected_form="Chciałbym kupić bilet.",
            despite_low_confidence=True,
            reviewer_kind="human",
            override_reason="   ",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "override_requires_a_reason"


def test_db_check_finds_an_override_a_rebuild_stripped_of_its_constraint(
    running: PolishWorkspace,
) -> None:
    uncertain = package()
    uncertain["transcript_layers"][0]["utterances"][0]["confidence"] = 0.2
    transcript_service.import_package(
        running.paths, package=uncertain, track=running.track_id, clock=running.clock
    )
    transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet.",
        despite_low_confidence=True,
        reviewer_kind="human",
        reviewer="the tutor",
        override_reason="I listened.",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        # A restore from a looser build: the table is there, the CHECK is not.
        transaction.execute("CREATE TABLE relaxed AS SELECT * FROM utterance_interpretations")
        transaction.execute("UPDATE relaxed SET reviewer_kind = 'ai', override_reason = NULL")
        transaction.execute("DROP TABLE utterance_interpretations")
        transaction.execute("ALTER TABLE relaxed RENAME TO utterance_interpretations")
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "override_provenance" in {check.name for check in report.failures}


# --- 8. Selected clips, not whole conversations -------------------------------------


def _target(running: PolishWorkspace) -> str:
    item = knowledge_service.upsert(
        running.paths,
        stable_key="sound.nasal-e",
        kind="pronunciation",
        title="Nosowe ę",
        body="The nasal vowel.",
        clock=running.clock,
    )
    return item.content_id


def test_a_clip_outlives_the_conversation_it_came_from(running: PolishWorkspace) -> None:
    """The plan's rule: clips are preferred to keeping whole recordings indefinitely."""

    target = _target(running)
    relative, digest = recording(running.root)
    whole = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id=ART,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    clip_path = running.root / "imports" / "moment.opus"
    clip_path.write_bytes(b"OggS the moment" + b"\x00" * 16)
    clip = artifact_service.register(
        running.paths,
        relative_path="imports/moment.opus",
        kind="audio",
        external_id="art_clip",
        clip_of=whole.artifact_id,
        clip_starts_at_ms=12_000,
        clip_ends_at_ms=16_000,
        track=running.track_id,
        clock=running.clock,
    )
    assert clip.clip_of_artifact_id == whole.artifact_id
    assert clip.clip_starts_at_ms == 12_000

    for artifact_id in (whole.artifact_id, clip.artifact_id):
        transcript_service.record_pronunciation(
            running.paths,
            dimension="phonetic-accuracy",
            status="confirmed",
            basis="audio",
            audio=artifact_id,
            target=target,
            track=running.track_id,
            clock=running.clock,
        )
    learner_service.update_track(
        running.paths,
        track=running.track_id,
        preferences=learner_service.TrackPreferences(
            audio_retention_policy="delete-after-ingestion"
        ),
        clock=running.clock,
    )
    preview = artifact_service.sweep(
        running.paths, dry_run=True, track=running.track_id, clock=running.clock
    )
    assert whole.artifact_id in preview.purged, "a whole conversation is not kept indefinitely"
    assert clip.artifact_id not in preview.purged, "the selected moment is"
    assert preview.unclipped_recordings == (whole.artifact_id,)
    assert any("artifact clip" in warning for warning in preview.warnings)


def test_a_clip_must_name_a_recording_it_came_from(running: PolishWorkspace) -> None:
    clip_path = running.root / "imports" / "orphan.opus"
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(b"OggS orphan")
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/orphan.opus",
            kind="audio",
            clip_starts_at_ms=0,
            clip_ends_at_ms=1_000,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "clip_source_required"


def test_a_clip_of_a_transcript_is_refused(running: PolishWorkspace) -> None:
    text = running.root / "imports" / "notes.txt"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("plain text\n", encoding="utf-8")
    source = artifact_service.register(
        running.paths,
        relative_path="imports/notes.txt",
        kind="transcript",
        track=running.track_id,
        clock=running.clock,
    )
    clip_path = running.root / "imports" / "excerpt.opus"
    clip_path.write_bytes(b"OggS excerpt")
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/excerpt.opus",
            kind="audio",
            clip_of=source.artifact_id,
            clip_starts_at_ms=0,
            clip_ends_at_ms=2_000,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "clip_source_is_not_audio"


def test_db_check_finds_a_clip_of_a_recording_nothing_records(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    whole = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    clip_path = running.root / "imports" / "moment.opus"
    clip_path.write_bytes(b"OggS the moment" + b"\x00" * 16)
    artifact_service.register(
        running.paths,
        relative_path="imports/moment.opus",
        kind="audio",
        clip_of=whole.artifact_id,
        clip_starts_at_ms=1_000,
        clip_ends_at_ms=3_000,
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE artifacts SET clip_of_artifact_id = 'art_01ARZ3NDEKTSV4RRFFQ69G5FZZ' "
            "WHERE clip_of_artifact_id IS NOT NULL"
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "clip_provenance" in {check.name for check in report.failures}
