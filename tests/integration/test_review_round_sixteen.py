"""Regressions for the ten defects round sixteen found in Stage 5.

Round fifteen fixed `speaking.ingest` and left `session ingest-package` beside it, which
is the shape most of these share: a safeguard that only one caller runs, or a check that
one command performs and its sibling does not. They are pinned here at the *lower* entry
point wherever one exists, because that is where a later caller will arrive.
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
EVENT = "evt_01ARZ3NDEKTSV4RRFFQ69G5FB1"


def package(
    *,
    session: str = "call-a",
    package_id: str = "pkg_a",
    utterance: str = "utt_001",
    text: str = "chcialbym kupic bilet do Krakowa na jutro",
    artifacts: tuple[dict[str, Any], ...] = (),
    events: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": package_id,
        "external_session_id": session,
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
                        "utterance_id": utterance,
                        "speaker": "learner",
                        "started_at": "2026-01-01T10:01:00Z",
                        "ended_at": "2026-01-01T10:01:06Z",
                        "text": text,
                    }
                ],
            }
        ],
        "events": list(events),
        "artifacts": list(artifacts),
    }


def confirmed(artifact_id: str = ART, *, utterance: str = "utt_001") -> dict[str, Any]:
    return {
        "event_id": EVENT,
        "kind": "pronunciation.assessment",
        "occurred_at": "2026-01-01T10:02:00Z",
        "payload": {
            "status": "confirmed",
            "utterance_id": utterance,
            "audio_artifact_id": artifact_id,
        },
    }


def manifest(relative: str, digest: str, *, retained: bool = True) -> dict[str, Any]:
    return {
        "artifact_id": ART,
        "kind": "audio",
        "relative_path": relative,
        "sha256": digest,
        "retained": retained,
    }


def recording(root: Path, name: str = "call.opus", body: bytes = b"OggS") -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + name.encode() + b"\x00" * 32)
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


# --- 1. The lower entry point must carry the safeguards ------------------------------


def test_staging_a_package_directly_runs_the_same_refusals(running: PolishWorkspace) -> None:
    """`session ingest-package` is the lower entry point, so the checks live there."""

    ghost = package(artifacts=(manifest("imports/nothing.opus", "a" * 64),))
    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths,
            package=ghost,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "package_audio_unavailable"


def test_staging_a_package_directly_registers_its_audio(running: PolishWorkspace) -> None:
    relative, digest = recording(running.root)
    report = session_service.ingest_package(
        running.paths,
        package=package(artifacts=(manifest(relative, digest),), events=(confirmed(),)),
        track=running.track_id,
        clock=running.clock,
    )
    assert report.audio_available
    held = artifact_service.listing(running.paths, track=running.track_id, clock=running.clock)
    assert [entry.relative_path for entry in held.artifacts] == [relative]


def test_the_cli_ingest_package_command_imports_the_transcript_too(
    running: PolishWorkspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Staging events whose utterances never arrive is half an ingestion."""

    import json

    from linguawiki.cli import run

    path = tmp_path / "package.json"
    path.write_text(json.dumps(package()), encoding="utf-8")
    assert (
        run(
            [
                "session",
                "ingest-package",
                "--input",
                str(path),
                "--workspace",
                str(running.root),
                "--format",
                "json",
            ],
            clock=running.clock,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"]
    assert payload["data"]["imported_utterances"] == 1
    shown = transcript_service.show(running.paths, track=running.track_id, clock=running.clock)
    assert shown.total == 1


# --- 2. validate and ingest must refuse the same things ------------------------------


def test_review_and_ingestion_agree_about_unretained_audio(running: PolishWorkspace) -> None:
    unretained = package(
        artifacts=(manifest("imports/x.opus", "a" * 64, retained=False),),
        events=(confirmed(),),
    )
    reviewed = speaking_service.validate(
        running.paths, package=unretained, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid

    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=unretained, track=running.track_id, clock=running.clock
        )
    assert session_service.staged(running.paths, track=running.track_id, clock=running.clock) == ()


# --- 3. Audio evidence without consent to keep audio ---------------------------------


def test_a_package_cannot_confirm_from_audio_a_learner_has_not_agreed_to_keep(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    session_service.create(
        polish_workspace.paths,
        minutes=60,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    session_service.start(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    relative, digest = recording(polish_workspace.root)
    sound = package(artifacts=(manifest(relative, digest),), events=(confirmed(),))

    reviewed = speaking_service.validate(
        polish_workspace.paths,
        package=sound,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert not reviewed.valid
    assert any("has not agreed to audio being kept" in entry for entry in reviewed.problems)

    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            polish_workspace.paths,
            package=sound,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    assert failure.value.payload.code == "package_audio_unavailable"
    # Nothing happened: the recording is untouched and no row was written.
    assert (polish_workspace.root / relative).is_file()
    held = artifact_service.listing(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert held.artifacts == ()


def test_db_check_finds_a_claim_resting_on_audio_that_was_never_kept(
    running: PolishWorkspace,
) -> None:
    """Not kept is the same fact as gone, for a claim that must rest on something."""

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
        transaction.execute("UPDATE artifacts SET retained = FALSE")
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "acoustic_claim_support" in {check.name for check in report.failures}


def test_materialization_will_not_resolve_audio_that_is_not_kept(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    session_service.ingest_package(
        running.paths,
        package=package(artifacts=(manifest(relative, digest),), events=(confirmed(),)),
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        # The learner changed their mind between the ingest and the close.
        transaction.execute("UPDATE artifacts SET retained = FALSE")
    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(running.paths, track=running.track_id, clock=running.clock)
    assert failure.value.payload.code == "pronunciation_requires_audio"


# --- 4. A failed deletion must not commit the privacy claim --------------------------


def test_a_registration_that_cannot_delete_the_file_records_nothing(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    with (
        mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")),
        pytest.raises(LinguaWikiError) as failure,
    ):
        artifact_service.register(
            running.paths,
            relative_path=relative,
            kind="audio",
            retained=False,
            expected_sha256=digest,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_file_not_removed"
    # The row went with the failure. A record saying the recording is gone, beside the
    # recording, is the state this refusal exists to prevent.
    with open_reader(running.paths, clock=running.clock) as database:
        assert (
            database.one("SELECT artifact_id FROM artifacts WHERE relative_path = ?", [relative])
            is None
        )
    assert (running.root / relative).is_file()


# --- 5. A reused producer identifier must not skip verification ----------------------


def test_a_reused_artifact_identifier_naming_different_bytes_is_refused(
    running: PolishWorkspace,
) -> None:
    first, first_digest = recording(running.root, "one.opus")
    session_service.ingest_package(
        running.paths,
        package=package(artifacts=(manifest(first, first_digest),)),
        track=running.track_id,
        clock=running.clock,
    )
    second, second_digest = recording(running.root, "two.opus")
    reused = package(
        session="call-b",
        package_id="pkg_b",
        utterance="utt_002",
        text="zupelnie inna rozmowa o pogodzie",
        artifacts=(manifest(second, second_digest),),
    )
    reviewed = speaking_service.validate(
        running.paths, package=reused, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("already names a different recording" in entry for entry in reviewed.problems)

    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=reused, track=running.track_id, clock=running.clock
        )


def test_the_same_recording_under_the_same_identifier_is_accepted_again(
    running: PolishWorkspace,
) -> None:
    """A checkpoint and the completed export of one call carry the same recording."""

    relative, digest = recording(running.root)
    first = session_service.ingest_package(
        running.paths,
        package=package(artifacts=(manifest(relative, digest),)),
        track=running.track_id,
        clock=running.clock,
    )
    again = session_service.ingest_package(
        running.paths,
        package=package(artifacts=(manifest(relative, digest),)),
        track=running.track_id,
        clock=running.clock,
    )
    assert first.audio_available
    assert again.duplicate
    held = artifact_service.listing(running.paths, track=running.track_id, clock=running.clock)
    assert len(held.artifacts) == 1


# --- 6. Hash deduplication must not discard its other inputs -------------------------


def test_the_same_bytes_cannot_be_registered_as_not_kept(running: PolishWorkspace) -> None:
    relative, digest = recording(running.root, "one.opus")
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    duplicate = running.root / "imports" / "copy.opus"
    duplicate.write_bytes((running.root / relative).read_bytes())
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/copy.opus",
            kind="audio",
            retained=False,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_retention_conflict"
    assert duplicate.is_file(), "nothing was decided, so nothing was deleted"


def test_a_duplicate_copy_binds_the_identifier_and_is_removed(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root, "one.opus")
    first = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    duplicate = running.root / "imports" / "copy.opus"
    duplicate.write_bytes((running.root / relative).read_bytes())
    second = artifact_service.register(
        running.paths,
        relative_path="imports/copy.opus",
        kind="audio",
        external_id=ART,
        track=running.track_id,
        clock=running.clock,
    )
    assert second.artifact_id == first.artifact_id
    assert not duplicate.exists(), "a second copy of bytes already held is not accounted for"

    # And the binding is what lets a package naming that identifier find this row.
    package_with_audio = package(artifacts=(manifest(relative, digest),), events=(confirmed(),))
    report = session_service.ingest_package(
        running.paths,
        package=package_with_audio,
        track=running.track_id,
        clock=running.clock,
    )
    assert report.audio_available
    held = artifact_service.listing(running.paths, track=running.track_id, clock=running.clock)
    assert len(held.artifacts) == 1


# --- 7. Utterance resolution at close must use the external session ------------------


def test_two_conversations_get_their_own_acoustic_claims(running: PolishWorkspace) -> None:
    first, first_digest = recording(running.root, "one.opus")
    session_service.ingest_package(
        running.paths,
        package=package(artifacts=(manifest(first, first_digest),), events=(confirmed(),)),
        track=running.track_id,
        clock=running.clock,
    )
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    second_package = package(
        session="call-b",
        package_id="pkg_b",
        text="zupelnie inna rozmowa o pogodzie",
    )
    # The same producer identifier, in a different call: the normal case.
    second_package["events"] = [
        {
            "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB2",
            "kind": "pronunciation.assessment",
            "occurred_at": "2026-01-01T10:03:00Z",
            "payload": {"status": "observed", "utterance_id": "utt_001"},
        }
    ]
    speaking_service.ingest(
        running.paths, package=second_package, track=running.track_id, clock=running.clock
    )
    session_service.close(running.paths, track=running.track_id, clock=running.clock)

    with open_reader(running.paths, clock=running.clock) as database:
        pairs = database.query(
            "SELECT utterance.external_session_id, observation.status "
            "FROM pronunciation_observations observation "
            "JOIN utterances utterance ON utterance.utterance_id = observation.utterance_id "
            "ORDER BY observation.status"
        )
    assert {(str(session), str(status)) for session, status in pairs} == {
        ("call-a", "confirmed"),
        ("call-b", "observed"),
    }


# --- 8. The privacy audit must not report clean what it did not read -----------------


def test_the_audit_fails_on_a_file_it_could_not_read(running: PolishWorkspace) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    page = running.root / "wiki" / "notes.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"The learner said: chcialbym kupic bilet do Krakowa na jutro\n\xff")
    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert not audited.ok
    assert [entry.path for entry in audited.unscanned] == ["wiki/notes.md"]


def test_the_audit_fails_on_a_file_it_does_not_know_how_to_read(
    running: PolishWorkspace,
) -> None:
    page = running.root / "wiki" / "notes.bin"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("anything at all", encoding="utf-8")
    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert not audited.ok
    assert [entry.path for entry in audited.unscanned] == ["wiki/notes.bin"]


def test_an_ordinary_workspace_has_nothing_unscanned(running: PolishWorkspace) -> None:
    """Everything a learner workspace commits is text of a kind the scan reads."""

    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.unscanned == ()
    assert audited.ok


# --- 9. The low-confidence override must be a listener's, and on the record ----------


def test_only_someone_who_could_have_listened_may_override(running: PolishWorkspace) -> None:
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
            corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
            despite_low_confidence=True,
            reviewer_kind="ai",
            override_reason="I am sure.",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "override_requires_a_listener"


def test_an_override_without_a_reason_is_refused(running: PolishWorkspace) -> None:
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
            corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
            despite_low_confidence=True,
            reviewer_kind="human",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "override_requires_a_reason"


def test_an_override_is_stored_where_the_learner_can_read_it(
    running: PolishWorkspace,
) -> None:
    uncertain = package()
    uncertain["transcript_layers"][0]["utterances"][0]["confidence"] = 0.2
    transcript_service.import_package(
        running.paths, package=uncertain, track=running.track_id, clock=running.clock
    )
    overridden = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
        despite_low_confidence=True,
        reviewer_kind="human",
        reviewer="the tutor",
        override_reason="I listened to the recording; the ending is wrong.",
        track=running.track_id,
        clock=running.clock,
    )
    assert overridden.overrode_low_confidence
    with open_reader(running.paths, clock=running.clock) as database:
        stored = database.one(
            "SELECT overrode_low_confidence, override_reason, reviewer_kind "
            "FROM utterance_interpretations WHERE interpretation_id = ?",
            [overridden.interpretation_id],
        )
    assert stored is not None
    assert bool(stored[0])
    assert "listened" in str(stored[1])
    assert str(stored[2]) == "human"


def test_an_ordinary_interpretation_records_no_override(running: PolishWorkspace) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    ordinary = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
        despite_low_confidence=True,
        track=running.track_id,
        clock=running.clock,
    )
    assert not ordinary.overrode_low_confidence, "there was nothing to override"
    assert ordinary.override_reason is None


# --- 10. The sweep must require a target the learner has not finished ----------------


def test_a_targetless_claim_does_not_keep_a_recording_forever(
    running: PolishWorkspace,
) -> None:
    """Otherwise `delete-after-ingestion` means "keep everything ever judged"."""

    relative, digest = recording(running.root)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id="art_from_a_package",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="confirmed",
        basis="audio",
        audio=registered.artifact_id,
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
    swept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert swept.purged == (registered.artifact_id,)
    assert not (running.root / relative).exists()


def test_a_finished_target_does_not_keep_a_recording_either(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id="art_from_a_package",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    target = knowledge_service.upsert(
        running.paths,
        stable_key="sound.nasal-e",
        kind="pronunciation",
        title="Nosowe ę",
        body="The nasal vowel.",
        clock=running.clock,
    )
    transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="confirmed",
        basis="audio",
        audio=registered.artifact_id,
        target=target.content_id,
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
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "INSERT INTO track_item_state (track_id, content_id, stage, stage_source, "
            "confidence, priority, positive_evidence, negative_evidence, updated_at, "
            "aggregation_version) VALUES (?, ?, 'stable', 'evidence', 0.9, 0, 5, 0, ?, "
            "'mastery.v1')",
            [running.track_id, target.content_id, database.now()],
        )
    swept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert swept.purged == (registered.artifact_id,), "a finished target keeps nothing back"


def test_a_claim_on_a_recording_this_workspace_no_longer_keeps_is_refused(
    running: PolishWorkspace,
) -> None:
    """A row that exists is not a recording anyone can listen to.

    Found while re-reading the fix for finding 2: resolving an event's artifact by
    existence rather than by retention let review accept a package the close would refuse
    -- the same disagreement, one function along.
    """

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
    # The package no longer carries the audio either: it was ingested before the purge.
    citing = package(package_id="pkg_later", session="call-later", events=(confirmed(),))
    reviewed = speaking_service.validate(
        running.paths, package=citing, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=citing, track=running.track_id, clock=running.clock
        )
