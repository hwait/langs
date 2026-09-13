"""Bringing a spoken session in, and refusing to overstate what was heard.

Every test here is one sentence of the stage's exit gate: a package re-ingests as a
no-op, transcript-only input cannot produce a confirmed pronunciation claim, and purging
audio invalidates exactly the claims that needed it and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import database as database_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from linguawiki.services import speaking as speaking_service
from linguawiki.services import transcripts as transcript_service
from tests.conftest import PolishWorkspace


def package(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_rozmowa_1",
        "external_session_id": "rozmowa-1",
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
                        "text": "chcialbym kupic bilet do Krakowa",
                    },
                    {
                        "utterance_id": "utt_002",
                        "speaker": "tutor",
                        "started_at": "2026-01-01T10:01:10Z",
                        "ended_at": "2026-01-01T10:01:14Z",
                        "text": "dokad pan jedzie i kiedy",
                    },
                ],
            }
        ],
        "events": [],
        "artifacts": [],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    """An onboarded track with one active session, ready to receive a package."""

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
    return polish_workspace


def test_a_package_is_reviewable_before_it_is_ingested(running: PolishWorkspace) -> None:
    report = speaking_service.validate(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    assert report.valid
    assert report.utterances == 2
    assert report.layers == ("raw",)
    assert not report.duplicate
    assert any("reviewed-hearing" in warning for warning in report.warnings)


def test_re_ingesting_the_same_content_stages_and_imports_nothing(
    running: PolishWorkspace,
) -> None:
    first = speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    assert first.imported_utterances == 2
    assert not first.duplicate

    again = speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    assert again.duplicate
    assert again.staged_events == 0
    assert again.imported_utterances == 0
    assert again.skipped_utterances == 2

    shown = transcript_service.show(running.paths, track=running.track_id, clock=running.clock)
    assert shown.total == 2


def test_a_reordered_export_of_the_same_conversation_is_the_same_conversation(
    running: PolishWorkspace,
) -> None:
    """Deduplication is by content, so the bytes may be arranged differently."""

    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    reordered = package()
    reordered["learning_targets"] = []
    again = speaking_service.ingest(
        running.paths, package=reordered, track=running.track_id, clock=running.clock
    )
    assert again.duplicate


def test_a_normalized_layer_that_changes_the_words_is_refused_whole(
    running: PolishWorkspace,
) -> None:
    mislabelled = package(
        transcript_layers=[
            package()["transcript_layers"][0],
            {
                "kind": "normalized",
                "derived_from": "raw",
                "utterances": [
                    {
                        "utterance_id": "utt_001",
                        "speaker": "learner",
                        "started_at": "2026-01-01T10:01:00Z",
                        "ended_at": "2026-01-01T10:01:06Z",
                        "text": "Chcialbym kupic bilety do Krakowa.",
                    }
                ],
            },
        ]
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.import_package(
            running.paths, package=mislabelled, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "normalization_changed_the_words"
    # Nothing half-written: the refusal happens before the first row.
    shown = transcript_service.show(running.paths, track=running.track_id, clock=running.clock)
    assert shown.total == 0


def test_the_layers_are_reported_rather_than_collapsed(running: PolishWorkspace) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    transcript_service.normalize(
        running.paths,
        utterance="utt_002",
        text="Dokad pan jedzie i kiedy?",
        track=running.track_id,
        clock=running.clock,
    )
    heard = transcript_service.review(
        running.paths,
        utterance="utt_002",
        text="Dokad pani jedzie i kiedy?",
        reviewer="learner",
        track=running.track_id,
        clock=running.clock,
    )
    assert heard.kind == "hearing"
    assert heard.derived_from == "normalized"

    shown = transcript_service.show(
        running.paths, utterance="utt_002", track=running.track_id, clock=running.clock
    )
    utterance = shown.utterances[0]
    assert utterance.best_layer == "reviewed-hearing"
    assert utterance.disagreement == ("normalized vs reviewed-hearing",)
    assert [revision.kind for revision in utterance.revisions] == ["normalization", "hearing"]


def test_a_normalization_that_changes_the_words_is_refused_at_the_command(
    running: PolishWorkspace,
) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.normalize(
            running.paths,
            utterance="utt_001",
            text="Chcialbym kupic bilety do Krakowa.",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "normalization_changed_the_words"


def test_db_check_finds_a_normalization_that_changed_the_words(
    running: PolishWorkspace,
) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    transcript_service.normalize(
        running.paths,
        utterance="utt_002",
        text="Dokad pan jedzie i kiedy?",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE transcript_revisions SET text = 'Dokad pani jedzie i kiedy?' "
            "WHERE layer = 'normalized'"
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "revision_honesty" in {check.name for check in report.failures}


def test_a_mishearing_is_recorded_and_counted_against_nobody(
    running: PolishWorkspace,
) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    artifact = transcript_service.interpret(
        running.paths,
        utterance="utt_002",
        classification="transcription-artifact",
        explanation="The audio clipped.",
        track=running.track_id,
        clock=running.clock,
    )
    assert not artifact.counts_against_the_learner

    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.interpret(
            running.paths,
            utterance="utt_001",
            classification="learner-error",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "correction_required"

    mistake = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet do Krakowa.",
        track=running.track_id,
        clock=running.clock,
    )
    assert mistake.counts_against_the_learner
    assert mistake.error_id is not None


def test_transcript_only_input_cannot_create_a_confirmed_pronunciation_claim(
    running: PolishWorkspace,
) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.record_pronunciation(
            running.paths,
            dimension="intelligibility",
            status="confirmed",
            basis="transcript",
            utterance="utt_001",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "pronunciation_requires_audio"

    with pytest.raises(LinguaWikiError) as prosody:
        transcript_service.record_pronunciation(
            running.paths,
            dimension="prosody",
            status="observed",
            basis="transcript",
            utterance="utt_001",
            track=running.track_id,
            clock=running.clock,
        )
    assert prosody.value.payload.code == "dimension_requires_audio"

    observed = transcript_service.record_pronunciation(
        running.paths,
        dimension="intelligibility",
        status="observed",
        basis="transcript",
        utterance="utt_001",
        track=running.track_id,
        clock=running.clock,
    )
    assert observed.status == "observed"


def test_db_check_finds_a_confirmed_claim_that_rests_on_a_transcript(
    running: PolishWorkspace,
) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    transcript_service.record_pronunciation(
        running.paths,
        dimension="intelligibility",
        status="observed",
        basis="transcript",
        utterance="utt_001",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        # A CHECK forbids this pairing, so the damage has to arrive the way it
        # actually would: a table rebuilt without the constraint, as a restore from a
        # looser build leaves one. `db check` has to find it in the data.
        transaction.execute("CREATE TABLE relaxed AS SELECT * FROM pronunciation_observations")
        transaction.execute("UPDATE relaxed SET status = 'confirmed'")
        transaction.execute("DROP TABLE pronunciation_observations")
        transaction.execute("ALTER TABLE relaxed RENAME TO pronunciation_observations")
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "acoustic_claim_basis" in {check.name for check in report.failures}


def _recording(running: PolishWorkspace) -> Any:
    """A registered recording on a track that consented to keeping audio.

    The consent matters: without it `register` records the file as *not retained*, and a
    claim cannot rest on a recording the learner did not agree to keep.
    """

    from linguawiki.services import learners as learner_service

    learner_service.update_track(
        running.paths,
        track=running.track_id,
        preferences=learner_service.TrackPreferences(audio_retention_consent=True),
        clock=running.clock,
    )
    directory = running.root / "artifacts" / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rozmowa-1.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    return artifact_service.register(
        running.paths,
        relative_path="artifacts/audio/rozmowa-1.wav",
        kind="audio",
        origin="learner-recording",
        track=running.track_id,
        clock=running.clock,
    )


def test_purging_audio_invalidates_the_claims_that_needed_it_and_nothing_else(
    running: PolishWorkspace,
) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    artifact = _recording(running)
    confirmed = transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="confirmed",
        basis="audio",
        utterance="utt_001",
        audio=artifact.artifact_id,
        track=running.track_id,
        clock=running.clock,
    )
    surviving = transcript_service.record_pronunciation(
        running.paths,
        dimension="intelligibility",
        status="observed",
        basis="transcript",
        utterance="utt_001",
        track=running.track_id,
        clock=running.clock,
    )

    preview = artifact_service.purge(
        running.paths,
        artifact=artifact.artifact_id,
        reason="learner-request",
        dry_run=True,
        track=running.track_id,
        clock=running.clock,
    )
    assert preview.invalidated_observations == (confirmed.observation_id,)
    assert (running.root / "artifacts" / "audio" / "rozmowa-1.wav").is_file()

    purged = artifact_service.purge(
        running.paths,
        artifact=artifact.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    assert purged.invalidated_observations == (confirmed.observation_id,)
    assert not (running.root / "artifacts" / "audio" / "rozmowa-1.wav").exists()

    shown = transcript_service.show(
        running.paths, utterance="utt_001", track=running.track_id, clock=running.clock
    )
    claims = {claim.observation_id: claim for claim in shown.utterances[0].pronunciation}
    assert claims[confirmed.observation_id].invalidation_reason is not None
    assert claims[surviving.observation_id].invalidation_reason is None
    # The words are untouched: what the learner said was established by the transcript.
    assert shown.utterances[0].raw_text == "chcialbym kupic bilet do Krakowa"


def test_a_new_claim_cannot_rest_on_audio_that_is_gone(running: PolishWorkspace) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    artifact = _recording(running)
    artifact_service.purge(
        running.paths,
        artifact=artifact.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.record_pronunciation(
            running.paths,
            dimension="prosody",
            status="observed",
            basis="audio",
            utterance="utt_001",
            audio=artifact.artifact_id,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "audio_not_available"


def test_db_check_finds_a_claim_still_standing_on_purged_audio(
    running: PolishWorkspace,
) -> None:
    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    artifact = _recording(running)
    transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="confirmed",
        basis="audio",
        utterance="utt_001",
        audio=artifact.artifact_id,
        track=running.track_id,
        clock=running.clock,
    )
    artifact_service.purge(
        running.paths,
        artifact=artifact.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE pronunciation_observations SET invalidated_at = NULL, "
            "invalidation_reason = NULL"
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "acoustic_claim_support" in {check.name for check in report.failures}


def test_an_adapter_maps_a_transcription_export_and_stops_there(
    running: PolishWorkspace,
) -> None:
    from datetime import UTC, datetime

    adapted = speaking_service.adapt(
        {
            "task": "transcribe",
            "language": "polish",
            "duration": 30.0,
            "segments": [
                {
                    "id": 0,
                    "start": 1.0,
                    "end": 4.5,
                    "text": " Chcialbym kupic bilet.",
                    "avg_logprob": -0.2,
                    "temperature": 0.0,
                    "no_speech_prob": 0.01,
                },
                {"id": 1, "start": 5.0, "end": 7.0, "text": " Do Krakowa prosze."},
            ],
        },
        adapter="whisper-verbose-json",
        external_session_id="rozmowa-2",
        target_language="pl",
        started_at=datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
        track=running.track_id,
    )
    assert adapted["schema_name"] == "lingua.session.v1"
    utterances = adapted["transcript_layers"][0]["utterances"]
    assert [entry["text"] for entry in utterances] == [
        "Chcialbym kupic bilet.",
        "Do Krakowa prosze.",
    ]
    # The contract's fields and nothing else: the certainty is carried because the
    # contract has a place for it, and the temperatures and logprobs are not.
    assert set(utterances[0]) == {
        "utterance_id",
        "speaker",
        "started_at",
        "ended_at",
        "text",
        "confidence",
    }
    assert set(utterances[1]) == {
        "utterance_id",
        "speaker",
        "started_at",
        "ended_at",
        "text",
    }, "a segment that reported no certainty carries none"
    assert adapted["transcriber"]["confidence_basis"]
    report = speaking_service.validate(
        running.paths, package=adapted, track=running.track_id, clock=running.clock
    )
    assert report.valid


def test_an_unknown_adapter_is_refused_rather_than_guessed(running: PolishWorkspace) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.adapt({}, adapter="some-product-v3")
    assert failure.value.payload.code == "unknown_adapter"


def test_a_scaffolded_package_is_valid_as_written(running: PolishWorkspace, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    scaffolded = speaking_service.scaffold(
        external_session_id="rozmowa-3",
        target_language="pl",
        started_at=datetime(2026, 1, 3, 18, 0, tzinfo=UTC),
        minutes=20,
        utterances=6,
        track=running.track_id,
    )
    report = speaking_service.validate(
        running.paths, package=scaffolded, track=running.track_id, clock=running.clock
    )
    assert report.valid
    assert report.utterances == 6


def test_a_package_naming_another_learners_session_is_refused(
    running: PolishWorkspace,
) -> None:
    """The track hint is optional, so the session it names has to be checked too."""

    from linguawiki.services import learners as learner_service

    second = learner_service.create_user(
        running.paths,
        display_name="Drugi Uczeń",
        timezone="Europe/Warsaw",
        native_languages=["en"],
        clock=running.clock,
    )
    other = learner_service.create_track(
        running.paths,
        user=second.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A1",
        clock=running.clock,
    )
    session_service.create(running.paths, minutes=40, track=other.track_id, clock=running.clock)
    started = session_service.start(running.paths, track=other.track_id, clock=running.clock)
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.import_package(
            running.paths,
            package=package(session_id=started.session_id, track_hint=None),
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "package_track_mismatch"


def test_a_supplied_original_is_verified_rather_than_believed(
    running: PolishWorkspace,
) -> None:
    """Otherwise the honesty check would agree with whatever the caller claimed."""

    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.normalize(
            running.paths,
            utterance="utt_001",
            text="Chcialbym kupic bilety do Krakowa.",
            original="chcialbym kupic bilety do Krakowa",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "original_text_mismatch"


def test_a_workspace_that_kept_only_a_hash_can_still_check_a_normalization(
    polish_workspace: PolishWorkspace,
) -> None:
    """The caller proposing the normalization has the words; the hash decides."""

    from linguawiki.services import learners as learner_service

    learner_service.update_track(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=polish_workspace.clock,
    )
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
    imported = transcript_service.import_package(
        polish_workspace.paths,
        package=package(),
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert imported.retention_policy == "withheld"
    shown = transcript_service.show(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert all(utterance.raw_text is None for utterance in shown.utterances)

    # Without the words, and without being told them, nothing can be checked.
    with pytest.raises(LinguaWikiError) as blind:
        transcript_service.normalize(
            polish_workspace.paths,
            utterance="utt_002",
            text="Dokad pan jedzie i kiedy?",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    assert blind.value.payload.code == "original_text_unavailable"

    # Told the real line, the hash vouches for it and the check runs as normal.
    revision = transcript_service.normalize(
        polish_workspace.paths,
        utterance="utt_002",
        text="Dokad pan jedzie i kiedy?",
        original="dokad pan jedzie i kiedy",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert revision.kind == "normalization"
    assert revision.text is None, "a withheld workspace stores the revision's words no more"

    with pytest.raises(LinguaWikiError) as dishonest:
        transcript_service.normalize(
            polish_workspace.paths,
            utterance="utt_001",
            text="Chcialbym kupic bilety do Krakowa.",
            original="chcialbym kupic bilet do Krakowa",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    assert dishonest.value.payload.code == "normalization_changed_the_words"


def test_a_pronunciation_claim_resolves_its_target_through_the_graph(
    running: PolishWorkspace,
) -> None:
    """By stable key, by content ID, and refused by name when there is no such item."""

    from linguawiki.services import knowledge as knowledge_service

    speaking_service.ingest(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    item = knowledge_service.upsert(
        running.paths,
        stable_key="sound.nasal-e",
        kind="pronunciation",
        title="Nosowe ę",
        body="The nasal vowel in 'kupię'.",
        clock=running.clock,
    )
    by_key = transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="observed",
        basis="transcript",
        utterance="utt_001",
        target="sound.nasal-e",
        track=running.track_id,
        clock=running.clock,
    )
    assert by_key.observation_id
    by_id = transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="uncertain",
        basis="transcript",
        utterance="utt_001",
        target=item.content_id,
        track=running.track_id,
        clock=running.clock,
    )
    assert by_id.observation_id

    with pytest.raises(LinguaWikiError):
        transcript_service.record_pronunciation(
            running.paths,
            dimension="phonetic-accuracy",
            status="observed",
            basis="transcript",
            utterance="utt_001",
            target="sound.nothing-like-this",
            track=running.track_id,
            clock=running.clock,
        )


def test_verify_tells_missing_altered_and_purged_apart(running: PolishWorkspace) -> None:
    """Three different facts about a file, and collapsing them loses the useful one."""

    artifact = _recording(running)
    clean = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert clean.ok
    assert clean.checked == 1
    assert clean.present == 1

    path = running.root / "artifacts" / "audio" / "rozmowa-1.wav"
    path.write_bytes(b"RIFF" + b"\xff" * 64)
    altered = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not altered.ok
    assert altered.altered == (artifact.artifact_id,)
    assert altered.missing == ()

    path.unlink()
    missing = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not missing.ok
    assert missing.missing == (artifact.artifact_id,)
    assert any("no tombstone" in warning for warning in missing.warnings)

    # A purge records the removal, and then the same absence is expected rather than damage.
    artifact_service.purge(
        running.paths,
        artifact=artifact.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    settled = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert settled.ok
    assert settled.purged == (artifact.artifact_id,)

    # A file that comes back after a purge is not a happy accident.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + b"\x00" * 64)
    returned = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not returned.ok
    assert any("file is present again" in entry for entry in returned.altered)
