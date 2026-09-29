"""Regressions for the eleven defects round fifteen found in Stage 5.

Each test names the defect it pins. Where the bad state can persist in a database rather
than only be produced by a command, there are two tests: one that the command refuses it,
and one that `db check` finds it in a database that has it anyway -- because a restored
file, a hand-repaired database, or a build under a looser rule can present a state no
command would write.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import database as database_service
from linguawiki.services import errors as error_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import privacy as privacy_service
from linguawiki.services import sessions as session_service
from linguawiki.services import sources as source_service
from linguawiki.services import speaking as speaking_service
from linguawiki.services import transcripts as transcript_service
from tests.conftest import PolishWorkspace


def package(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_call_a",
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


def _recording(running: PolishWorkspace, name: str = "call.opus") -> tuple[str, str]:
    """A real file under a private root, with its real hash."""

    path = running.root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OggS" + name.encode() + b"\x00" * 48)
    return f"imports/{name}", hashlib.sha256(path.read_bytes()).hexdigest()


# --- 1. Package-declared audio is trusted without verifying that it exists -------------


def test_a_package_naming_audio_that_is_not_there_is_refused(running: PolishWorkspace) -> None:
    ghost = package(
        artifacts=[
            {
                "artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "kind": "audio",
                "relative_path": "imports/nothing.opus",
                "sha256": "a" * 64,
                "retained": True,
            }
        ],
    )
    reviewed = speaking_service.validate(
        running.paths, package=ghost, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("not in this workspace" in problem for problem in reviewed.problems)

    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=ghost, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "package_audio_unavailable"


def test_a_package_naming_a_different_file_than_it_describes_is_refused(
    running: PolishWorkspace,
) -> None:
    relative, _ = _recording(running)
    wrong = package(
        artifacts=[
            {
                "artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "kind": "audio",
                "relative_path": relative,
                "sha256": "b" * 64,
                "retained": True,
            }
        ],
    )
    reviewed = speaking_service.validate(
        running.paths, package=wrong, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("not the file the package describes" in entry for entry in reviewed.problems)


def test_a_package_naming_audio_outside_the_private_roots_is_refused(
    running: PolishWorkspace,
) -> None:
    """`inbox/` is not ignored, so a recording registered there is one Git would commit."""

    loose = running.root / "inbox" / "call.opus"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"OggS")
    outside = package(
        artifacts=[
            {
                "artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "kind": "audio",
                "relative_path": "inbox/call.opus",
                "sha256": hashlib.sha256(loose.read_bytes()).hexdigest(),
                "retained": True,
            }
        ],
    )
    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=outside, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "package_audio_unavailable"


def test_ingested_audio_becomes_an_artifact_a_purge_can_reach(
    running: PolishWorkspace,
) -> None:
    """The defect in one sentence: a confirmed claim that no purge could ever invalidate."""

    relative, digest = _recording(running)
    sound = package(
        artifacts=[
            {
                "artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "kind": "audio",
                "relative_path": relative,
                "sha256": digest,
                "retained": True,
            }
        ],
        events=[
            {
                "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB1",
                "kind": "pronunciation.assessment",
                "occurred_at": "2026-01-01T10:02:00Z",
                "payload": {
                    "status": "confirmed",
                    "utterance_id": "utt_001",
                    "audio_artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                },
            }
        ],
    )
    ingested = speaking_service.ingest(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert ingested.audio_available

    registered = artifact_service.listing(
        running.paths, track=running.track_id, clock=running.clock
    )
    assert len(registered.artifacts) == 1
    artifact_id = registered.artifacts[0].artifact_id

    closed = session_service.close(running.paths, track=running.track_id, clock=running.clock)
    assert closed.staged_consumed == 1

    shown = transcript_service.show(
        running.paths, utterance="utt_001", track=running.track_id, clock=running.clock
    )
    claims = shown.utterances[0].pronunciation
    assert [claim.status for claim in claims] == ["confirmed"]
    assert claims[0].audio_artifact_id == artifact_id

    purged = artifact_service.purge(
        running.paths,
        artifact=artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    assert purged.invalidated_observations == (claims[0].observation_id,)
    report = database_service.check(running.paths, clock=running.clock)
    assert report.ok, [check.name for check in report.failures]


def test_db_check_finds_one_producer_identifier_naming_two_recordings(
    running: PolishWorkspace,
) -> None:
    first, first_digest = _recording(running, "one.opus")
    second, second_digest = _recording(running, "two.opus")
    artifact_service.register(
        running.paths,
        relative_path=first,
        kind="audio",
        external_id="art_shared",
        expected_sha256=first_digest,
        track=running.track_id,
        clock=running.clock,
    )
    artifact_service.register(
        running.paths,
        relative_path=second,
        kind="audio",
        expected_sha256=second_digest,
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE artifacts SET external_id = 'art_shared'")
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "artifact_external_identity" in {check.name for check in report.failures}


# --- 2. A rejected spoken ingest leaves accepted events behind ------------------------


def test_a_rejected_package_stages_nothing_at_all(running: PolishWorkspace) -> None:
    mislabelled = package(
        events=[
            {
                "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB2",
                "kind": "follow_up",
                "occurred_at": "2026-01-01T10:03:00Z",
                "payload": {"summary": "Practise ticket vocabulary."},
            }
        ],
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
                        "text": "Chcialbym kupic bilety do Krakowa na jutro.",
                    }
                ],
            },
        ],
    )
    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=mislabelled, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "normalization_changed_the_words"

    staged = session_service.staged(running.paths, track=running.track_id, clock=running.clock)
    assert staged == ()
    shown = transcript_service.show(running.paths, track=running.track_id, clock=running.clock)
    assert shown.total == 0
    # And the package itself was not recorded, so ingesting the corrected export is not a
    # duplicate of a package that never landed.
    reviewed = speaking_service.validate(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    assert not reviewed.duplicate


def test_review_reports_a_mislabelled_layer_before_anyone_ingests_it(
    running: PolishWorkspace,
) -> None:
    """A reviewer told "valid" and then refused has been told the opposite of the truth."""

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
                        "text": "Chcialbym kupic bilety do Krakowa na jutro.",
                    }
                ],
            },
        ]
    )
    reviewed = speaking_service.validate(
        running.paths, package=mislabelled, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("normalization" in problem for problem in reviewed.problems)


# --- 3. Utterance identity collapses unrelated conversations --------------------------


def test_two_conversations_reusing_an_utterance_id_are_two_conversations(
    running: PolishWorkspace,
) -> None:
    """A scaffold and the segment adapter both mint `utt_001`."""

    first = package()
    second = package(package_id="pkg_call_b", external_session_id="call-b")
    second["transcript_layers"][0]["utterances"][0]["text"] = (
        "zupelnie inna rozmowa o pogodzie w Warszawie"
    )
    one = transcript_service.import_package(
        running.paths, package=first, track=running.track_id, clock=running.clock
    )
    two = transcript_service.import_package(
        running.paths, package=second, track=running.track_id, clock=running.clock
    )
    assert one.imported == 1
    assert two.imported == 1, "the second conversation was skipped as already imported"

    shown = transcript_service.show(running.paths, track=running.track_id, clock=running.clock)
    assert shown.total == 2
    assert {utterance.raw_text for utterance in shown.utterances} == {
        "chcialbym kupic bilet do Krakowa na jutro",
        "zupelnie inna rozmowa o pogodzie w Warszawie",
    }


def test_the_same_utterance_arriving_twice_is_still_one_utterance(
    running: PolishWorkspace,
) -> None:
    """A checkpoint export and the completed export of one call share their utterances."""

    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    again = transcript_service.import_package(
        running.paths,
        package=package(package_id="pkg_call_a2", mode="checkpoint"),
        track=running.track_id,
        clock=running.clock,
    )
    assert again.imported == 0
    assert again.skipped == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "cos zupelnie innego niz poprzednio"),
        ("speaker", "tutor"),
        ("started_at", "2026-01-01T10:05:00Z"),
    ],
)
def test_a_reused_identifier_whose_content_drifted_is_refused(
    running: PolishWorkspace, field: str, value: str
) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    drifted = package(package_id="pkg_call_a3")
    drifted["transcript_layers"][0]["utterances"][0][field] = value
    if field == "started_at":
        drifted["transcript_layers"][0]["utterances"][0]["ended_at"] = "2026-01-01T10:05:06Z"
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.import_package(
            running.paths, package=drifted, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "utterance_identity_reused"


# --- 4. "Not retained" and "purged" leaving the bytes on disk -------------------------


def test_registering_a_recording_as_not_kept_deletes_it(running: PolishWorkspace) -> None:
    relative, digest = _recording(running)
    absolute = running.root / relative
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        retained=False,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    assert not absolute.exists(), "the row says it was not kept"
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.ok, "an absent file is the expected state, not damage"
    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.retention.unkept_files_still_present == ()
    assert audited.ok


def test_the_audit_finds_a_recording_recorded_as_gone_whose_file_is_there(
    running: PolishWorkspace,
) -> None:
    relative, digest = _recording(running)
    absolute = running.root / relative
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
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(b"OggS back again")

    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.retention.unkept_files_still_present == (registered.artifact_id,)
    assert not audited.ok
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not verified.ok
    assert any("present again" in entry for entry in verified.altered)


def test_the_retention_policies_the_plan_requires_are_available(
    running: PolishWorkspace,
) -> None:
    """`keep`, `rolling-days`, and `delete-after-ingestion`, not a consent boolean alone."""

    relative, digest = _recording(running)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id="art_from_a_package",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    kept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert kept.policy == "keep"
    assert kept.purged == ()

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
    assert preview.purged == (registered.artifact_id,)
    assert (running.root / relative).is_file(), "a dry run deletes nothing"

    swept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert swept.purged == (registered.artifact_id,)
    assert not (running.root / relative).exists()


def test_a_rolling_window_without_a_number_of_days_is_refused(
    running: PolishWorkspace,
) -> None:
    learner_service.update_track(
        running.paths,
        track=running.track_id,
        preferences=learner_service.TrackPreferences(audio_retention_policy="rolling-days"),
        clock=running.clock,
    )
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert failure.value.payload.code == "retention_window_missing"


def test_a_sweep_keeps_a_clip_an_unfinished_target_rests_on(
    running: PolishWorkspace,
) -> None:
    """A retention window is a weaker reason than a target the learner is still working on.

    Two conditions, and each was a defect on its own. The claim has to be *about*
    something: a targetless confirmed observation held recordings back indefinitely, which
    turned `delete-after-ingestion` into "keep everything ever judged". And what is kept
    has to be a selected *clip*: holding an entire conversation because one target it
    touched is unfinished is the indefinite full-recording retention the plan forbids.
    """

    from linguawiki.services import knowledge as knowledge_service

    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    target = knowledge_service.upsert(
        running.paths,
        stable_key="sound.nasal-e",
        kind="pronunciation",
        title="Nosowe ę",
        body="The nasal vowel.",
        clock=running.clock,
    )
    relative, digest = _recording(running)
    whole = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id="art_from_a_package",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    clip_path = running.root / "artifacts" / "audio" / "moment.wav"
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(b"RIFF" + b"\x07" * 24)
    clip = artifact_service.register(
        running.paths,
        relative_path="artifacts/audio/moment.wav",
        kind="audio",
        # Carries the package's provenance, so `delete-after-ingestion` considers it and
        # the unfinished target is what actually holds it back. Without this the clip
        # survives because nothing ingested it, which proves something else.
        external_id="art_clip_from_a_package",
        clip_of=whole.artifact_id,
        clip_starts_at_ms=4_000,
        clip_ends_at_ms=7_500,
        track=running.track_id,
        clock=running.clock,
    )
    transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="confirmed",
        basis="audio",
        utterance="utt_001",
        audio=clip.artifact_id,
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
    swept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert clip.artifact_id not in swept.purged
    assert any("has not finished" in warning for warning in swept.warnings)
    assert clip_path.is_file()


# --- 5. Privacy content scanning covering only wiki Markdown --------------------------


def test_the_audit_searches_every_file_that_could_be_committed(
    running: PolishWorkspace,
) -> None:
    """AGENTS.md is on the Git-safe list, so it is *certain* to be committed."""

    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    spoken = "chcialbym kupic bilet do Krakowa na jutro"
    clean = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert clean.ok

    agents = running.root / "AGENTS.md"
    agents.write_text(
        f"{agents.read_text(encoding='utf-8')}\n\nThe learner said: {spoken}\n",
        encoding="utf-8",
    )
    leaking = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert not leaking.ok
    assert [entry.path for entry in leaking.content_leaks] == ["AGENTS.md"]


def test_the_audit_searches_configuration_as_well_as_prose(
    running: PolishWorkspace,
) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    note = running.root / "linguawiki.toml"
    note.write_text(
        f"{note.read_text(encoding='utf-8')}\n# chcialbym kupic bilet do Krakowa na jutro\n",
        encoding="utf-8",
    )
    leaking = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert not leaking.ok
    assert [entry.path for entry in leaking.content_leaks] == ["linguawiki.toml"]


# --- 6. Transcription uncertainty discarded at the ingestion boundary -----------------


def test_a_packages_reported_confidence_survives_ingestion(
    running: PolishWorkspace,
) -> None:
    uncertain = package(
        transcriber={"name": "whisper-large-v3", "version": "3", "confidence_basis": "exp"},
    )
    uncertain["transcript_layers"][0]["utterances"][0]["confidence"] = 0.21
    transcript_service.import_package(
        running.paths, package=uncertain, track=running.track_id, clock=running.clock
    )
    shown = transcript_service.show(
        running.paths, utterance="utt_001", track=running.track_id, clock=running.clock
    )
    assert shown.utterances[0].raw_confidence == pytest.approx(0.21)


def test_low_confidence_speech_cannot_become_a_learner_error(
    running: PolishWorkspace,
) -> None:
    """The plan's rule: it becomes a possible transcription error, not a confirmed one."""

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
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "confidence_too_low_to_blame"

    # `uncertain` is what it is, and it is accepted.
    recorded = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="uncertain",
        explanation="The transcriber was unsure.",
        track=running.track_id,
        clock=running.clock,
    )
    assert not recorded.counts_against_the_learner

    # A person who listened can still say so -- explicitly, and on the record.
    overridden = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
        despite_low_confidence=True,
        reviewer_kind="human",
        reviewer="the tutor",
        override_reason="I listened to the recording; the ending really is wrong.",
        track=running.track_id,
        clock=running.clock,
    )
    assert overridden.counts_against_the_learner
    assert overridden.overrode_low_confidence
    assert overridden.override_reason


def test_the_adapter_carries_the_certainty_it_was_given(running: PolishWorkspace) -> None:
    adapted = speaking_service.adapt(
        {
            "model": "whisper-large-v3",
            "duration": 8.0,
            "segments": [
                {"start": 0.0, "end": 2.0, "text": " pewne slowo", "avg_logprob": -0.05},
                {"start": 2.5, "end": 5.0, "text": " niepewne slowo", "avg_logprob": -2.9},
            ],
        },
        adapter="whisper-verbose-json",
        external_session_id="call-adapted",
        target_language="pl",
        started_at=datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
        track=running.track_id,
    )
    utterances = adapted["transcript_layers"][0]["utterances"]
    assert utterances[0]["confidence"] > 0.9
    assert utterances[1]["confidence"] < 0.1
    assert adapted["transcriber"]["name"] == "whisper-large-v3"


# --- 7. An interpretation that counts against nobody's record -------------------------


def test_a_learner_error_reaches_the_error_model(running: PolishWorkspace) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_service.interpret(
            running.paths,
            utterance="utt_001",
            classification="learner-error",
            corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "error_category_required"

    filed = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet do Krakowa na jutro.",
        track=running.track_id,
        clock=running.clock,
    )
    assert filed.error_id is not None
    pattern = error_service.show(
        running.paths, error=filed.error_id, track=running.track_id, clock=running.clock
    )
    assert len(pattern.occurrences) >= 1
    report = database_service.check(running.paths, clock=running.clock)
    assert report.ok, [check.name for check in report.failures]


def test_a_transcription_artifact_still_reaches_nobody(running: PolishWorkspace) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    noted = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="transcription-artifact",
        explanation="The audio clipped.",
        track=running.track_id,
        clock=running.clock,
    )
    assert noted.error_id is None
    assert not noted.counts_against_the_learner


# --- 8. Source progress accepting impossible numeric state ---------------------------


def test_a_source_cannot_declare_fewer_units_than_it_has(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        source_service.add(
            polish_workspace.paths,
            kind="book",
            title="Krótka",
            rights="metadata-only",
            total_units=1,
            units=[{"label": "R1"}, {"label": "R2"}],
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    assert failure.value.payload.code == "invalid_total_units"


@pytest.mark.parametrize("command", ["position", "complete-unit", "comprehension"])
def test_study_time_cannot_be_negative(polish_workspace: PolishWorkspace, command: str) -> None:
    source = source_service.add(
        polish_workspace.paths,
        kind="book",
        title="Lalka",
        rights="metadata-only",
        units=[{"label": "R1"}],
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    calls = {
        "position": lambda: source_service.position(
            polish_workspace.paths,
            source=source.source_id,
            minutes=-5,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        ),
        "complete-unit": lambda: source_service.complete_unit(
            polish_workspace.paths,
            source=source.source_id,
            unit="R1",
            minutes=-5,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        ),
        "comprehension": lambda: source_service.record_comprehension(
            polish_workspace.paths,
            source=source.source_id,
            unit="R1",
            aid="unaided",
            band="gist",
            minutes=-5,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        ),
    }
    with pytest.raises(LinguaWikiError) as failure:
        calls[command]()
    assert failure.value.payload.code == "invalid_minutes"


# --- 9. Archived material continuing to influence plans ------------------------------


def test_material_the_learner_put_down_stops_arguing_for_itself(
    polish_workspace: PolishWorkspace,
) -> None:
    from linguawiki.db.connection import open_reader
    from linguawiki.services.sessions import _source_continuity

    source = source_service.add(
        polish_workspace.paths,
        kind="podcast",
        title="W toku",
        rights="metadata-only",
        units=[{"label": "O1"}, {"label": "O2"}],
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    source_service.record_comprehension(
        polish_workspace.paths,
        source=source.source_id,
        unit="O1",
        aid="unaided",
        band="gist",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert _source_continuity(database, track_id=polish_workspace.track_id).get(
            "listening"
        ) == pytest.approx(1.0)

    source_service.set_status(
        polish_workspace.paths,
        source=source.source_id,
        status="archived",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert "listening" not in _source_continuity(database, track_id=polish_workspace.track_id)
    shown = source_service.show(
        polish_workspace.paths,
        source=source.source_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert shown.progress is not None
    assert shown.progress.status == "abandoned"

    # Picking it back up restores both.
    source_service.set_status(
        polish_workspace.paths,
        source=source.source_id,
        status="active",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert "listening" in _source_continuity(database, track_id=polish_workspace.track_id)


# --- 10. Error-history source links that are not track-scoped ------------------------


def test_a_source_cannot_link_to_another_learners_error(
    polish_workspace: PolishWorkspace,
) -> None:
    second = learner_service.create_user(
        polish_workspace.paths,
        display_name="Drugi Uczeń",
        timezone="Europe/Warsaw",
        native_languages=["en"],
        clock=polish_workspace.clock,
    )
    other = learner_service.create_track(
        polish_workspace.paths,
        user=second.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A1",
        clock=polish_workspace.clock,
    )
    foreign = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        track=other.track_id,
        clock=polish_workspace.clock,
    )
    source = source_service.add(
        polish_workspace.paths,
        kind="book",
        title="Lalka",
        rights="metadata-only",
        units=[{"label": "R1"}],
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    with pytest.raises(LinguaWikiError):
        source_service.link_item(
            polish_workspace.paths,
            source=source.source_id,
            unit="R1",
            target=foreign.error_id,
            target_kind="error-pattern",
            relation="practised-in",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )


def test_db_check_finds_a_source_link_that_crosses_learners(
    polish_workspace: PolishWorkspace,
) -> None:
    second = learner_service.create_user(
        polish_workspace.paths,
        display_name="Drugi Uczeń",
        timezone="Europe/Warsaw",
        native_languages=["en"],
        clock=polish_workspace.clock,
    )
    other = learner_service.create_track(
        polish_workspace.paths,
        user=second.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A1",
        clock=polish_workspace.clock,
    )
    foreign = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        track=other.track_id,
        clock=polish_workspace.clock,
    )
    source = source_service.add(
        polish_workspace.paths,
        kind="book",
        title="Lalka",
        rights="metadata-only",
        units=[{"label": "R1"}],
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    with (
        open_writer(polish_workspace.paths, command="test", clock=polish_workspace.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "INSERT INTO source_item_links (unit_id, target_kind, target_id, relation, "
            "created_at) VALUES (?, 'error-pattern', ?, 'practised-in', ?)",
            [source.units[0].unit_id, foreign.error_id, db.now()],
        )
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert not report.ok
    assert "source_link_track_scope" in {check.name for check in report.failures}


# --- 11. Transcript revision semantics contradicting their own contract --------------


def test_a_review_that_changed_nothing_is_a_confirmation(running: PolishWorkspace) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    confirmed = transcript_service.review(
        running.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilet do Krakowa na jutro.",
        reviewer="a person who listened",
        track=running.track_id,
        clock=running.clock,
    )
    assert confirmed.kind == "normalization", "the reviewer said the machine was right"

    heard = transcript_service.review(
        running.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilety do Krakowa na jutro.",
        reviewer="a second person",
        track=running.track_id,
        clock=running.clock,
    )
    assert heard.kind == "hearing"
    assert heard.supersedes == confirmed.revision_id


def test_a_second_review_supersedes_the_first_rather_than_deleting_it(
    running: PolishWorkspace,
) -> None:
    """Every later reading is a new row: that is the rule the layers exist for."""

    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    first = transcript_service.review(
        running.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilet do Krakowa na jutro.",
        reviewer="person-1",
        track=running.track_id,
        clock=running.clock,
    )
    second = transcript_service.review(
        running.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilety do Krakowa na jutro.",
        reviewer="person-2",
        track=running.track_id,
        clock=running.clock,
    )
    shown = transcript_service.show(
        running.paths, utterance="utt_001", track=running.track_id, clock=running.clock
    )
    revisions = {entry.revision_id: entry for entry in shown.utterances[0].revisions}
    assert first.revision_id in revisions, "the earlier hearing was deleted"
    assert revisions[first.revision_id].superseded_by == second.revision_id
    assert revisions[second.revision_id].superseded_by is None
    # Only the current reading speaks for the layer.
    assert shown.utterances[0].best_layer == "reviewed-hearing"
    report = database_service.check(running.paths, clock=running.clock)
    assert report.ok, [check.name for check in report.failures]


def test_db_check_finds_a_layer_with_two_current_readings(running: PolishWorkspace) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    transcript_service.review(
        running.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilet do Krakowa na jutro.",
        track=running.track_id,
        clock=running.clock,
    )
    transcript_service.review(
        running.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilety do Krakowa na jutro.",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE transcript_revisions SET superseded_at = NULL, superseded_by = NULL"
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "revision_supersession" in {check.name for check in report.failures}


def test_db_check_finds_a_superseded_reading_that_names_nothing(
    running: PolishWorkspace,
) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    transcript_service.review(
        running.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilet do Krakowa na jutro.",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE transcript_revisions SET superseded_at = ? WHERE superseded_at IS NULL",
            [database.now()],
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "revision_supersession_pairing" in {check.name for check in report.failures}
