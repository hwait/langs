"""Regressions for the seven defects round eighteen found in Stage 5.

Every one of these is a consequence of an earlier round's fix: a rule added in one place
and not the matching one, or a guard whose condition was almost right. They are pinned at
the level the rule actually lives at -- the service for what a command decides, `db check`
for what a restored database can present -- because that is where the next fix will look.
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
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from linguawiki.services import speaking as speaking_service
from linguawiki.services import transcripts as transcript_service
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


def rows(workspace: PolishWorkspace) -> list[tuple[Any, ...]]:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return list(
            database.query(
                "SELECT artifact_id, external_id, relative_path, retained FROM artifacts "
                "ORDER BY created_at"
            )
        )


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


# --- 1. "retained: false" must mean the bytes are gone -------------------------------


def test_a_package_declaring_audio_not_kept_removes_the_file(running: PolishWorkspace) -> None:
    """Skipping the entry left the bytes unregistered under an ignored directory."""

    relative, digest = recording(running.root)
    declined = package(artifacts=(entry(relative, digest, retained=False),))
    report = speaking_service.ingest(
        running.paths, package=declined, track=running.track_id, clock=running.clock
    )
    assert not report.audio_available
    assert not (running.root / relative).exists(), "the declaration was honoured"
    # And the row explains the absence, which is what makes it different from a file that
    # was never there at all.
    held = rows(running)
    assert len(held) == 1
    assert held[0][1] == ART
    assert not held[0][3]


def test_registering_bytes_a_learner_declined_removes_them_again(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        retained=False,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    assert not (running.root / relative).exists()
    # Restored from a backup and offered again at the same path.
    (running.root / relative).write_bytes(b"OggS" + b"call.opus" + b"\x00" * 32)
    report = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        # Reaffirming the original decision removes bytes that came back. A request to
        # retain them is the opposite decision and is covered separately in round twenty-five.
        retained=False,
        track=running.track_id,
        clock=running.clock,
    )
    assert not report.retained
    assert not (running.root / relative).exists(), "the learner's decision still governs"
    assert any("chose not to keep" in warning for warning in report.warnings)


def test_a_not_retained_entry_outside_the_private_roots_is_refused(
    running: PolishWorkspace,
) -> None:
    """Honouring the declaration means deleting, and not from anywhere it likes."""

    loose = running.root / "wiki" / "call.opus"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"OggS")
    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths,
            package=package(artifacts=(entry("wiki/call.opus", "a" * 64, retained=False),)),
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "package_audio_unavailable"
    assert loose.is_file()


# --- 2. The preflight must see collisions by content --------------------------------


def test_two_identifiers_for_one_recording_are_refused_before_either_is_written(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root, "one.opus")
    twin = running.root / "imports" / "two.opus"
    twin.write_bytes((running.root / relative).read_bytes())
    twins = package(
        artifacts=(
            entry(relative, digest),
            entry("imports/two.opus", digest, artifact_id=TWIN),
        )
    )
    reviewed = speaking_service.validate(
        running.paths, package=twins, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("same recording declared twice" in problem for problem in reviewed.problems)

    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=twins, track=running.track_id, clock=running.clock
        )
    assert rows(running) == [], "a refused package wrote nothing"


def test_an_identifier_for_a_recording_already_held_under_another_is_refused(
    running: PolishWorkspace,
) -> None:
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
    renamed = package(artifacts=(entry(relative, digest, artifact_id=TWIN),))
    reviewed = speaking_service.validate(
        running.paths, package=renamed, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("already holds as" in problem for problem in reviewed.problems)


# --- 3. A clip has to be a clip -----------------------------------------------------


@pytest.mark.parametrize(
    ("window", "code"),
    [
        ((None, None), "clip_window_required"),
        ((0, None), "clip_window_required"),
        ((None, 4_000), "clip_window_required"),
        ((-500, 4_000), "invalid_clip_window"),
        ((4_000, 4_000), "invalid_clip_window"),
        ((4_000, 1_000), "invalid_clip_window"),
    ],
)
def test_a_clip_without_a_real_window_is_refused(
    running: PolishWorkspace, window: tuple[int | None, int | None], code: str
) -> None:
    """Clip provenance buys longer retention, so what counts as a clip must be more."""

    relative, digest = recording(running.root, "whole.opus")
    whole = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    excerpt = running.root / "imports" / "excerpt.opus"
    excerpt.write_bytes(b"OggS excerpt")
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/excerpt.opus",
            kind="audio",
            clip_of=whole.artifact_id,
            clip_starts_at_ms=window[0],
            clip_ends_at_ms=window[1],
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == code


def test_a_whole_recording_cannot_buy_clip_retention_by_naming_another(
    running: PolishWorkspace,
) -> None:
    target = knowledge_service.upsert(
        running.paths,
        stable_key="sound.nasal-e",
        kind="pronunciation",
        title="Nosowe ę",
        body="The nasal vowel.",
        clock=running.clock,
    ).content_id
    first_relative, first_digest = recording(running.root, "call-one.opus")
    first = artifact_service.register(
        running.paths,
        relative_path=first_relative,
        kind="audio",
        external_id=ART,
        expected_sha256=first_digest,
        track=running.track_id,
        clock=running.clock,
    )
    second_relative, second_digest = recording(running.root, "call-two.opus")
    posing = artifact_service.register(
        running.paths,
        relative_path=second_relative,
        kind="audio",
        external_id="art_posing",
        expected_sha256=second_digest,
        clip_of=first.artifact_id,
        clip_starts_at_ms=0,
        clip_ends_at_ms=4_000,
        track=running.track_id,
        clock=running.clock,
    )
    transcript_service.record_pronunciation(
        running.paths,
        dimension="phonetic-accuracy",
        status="confirmed",
        basis="audio",
        audio=posing.artifact_id,
        target=target,
        track=running.track_id,
        clock=running.clock,
    )
    # With a real window it *is* a clip, and it is kept -- that much is by design.
    learner_service.update_track(
        running.paths,
        track=running.track_id,
        preferences=learner_service.TrackPreferences(
            audio_retention_policy="delete-after-ingestion"
        ),
        clock=running.clock,
    )
    kept = artifact_service.sweep(
        running.paths, dry_run=True, track=running.track_id, clock=running.clock
    )
    assert posing.artifact_id not in kept.purged

    # Strip the window the way a restore or a hand-repair would, and `db check` says so.
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE artifacts SET clip_starts_at_ms = NULL, clip_ends_at_ms = NULL "
            "WHERE artifact_id = ?",
            [posing.artifact_id],
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "clip_provenance" in {check.name for check in report.failures}


def test_db_check_finds_a_clip_of_another_learners_recording(
    running: PolishWorkspace,
) -> None:
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
    theirs_relative, theirs_digest = recording(running.root, "theirs.opus")
    theirs = artifact_service.register(
        running.paths,
        relative_path=theirs_relative,
        kind="audio",
        expected_sha256=theirs_digest,
        track=other.track_id,
        clock=running.clock,
    )
    mine_relative, mine_digest = recording(running.root, "mine.opus")
    mine = artifact_service.register(
        running.paths,
        relative_path=mine_relative,
        kind="audio",
        expected_sha256=mine_digest,
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE artifacts SET clip_of_artifact_id = ?, clip_starts_at_ms = 0, "
            "clip_ends_at_ms = 1000 WHERE artifact_id = ?",
            [theirs.artifact_id, mine.artifact_id],
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "clip_provenance" in {check.name for check in report.failures}


# --- 4. Deduplication must not delete the only copy ---------------------------------


def test_restoring_a_missing_recording_elsewhere_repoints_the_row(
    running: PolishWorkspace,
) -> None:
    """Deleting the incoming file destroyed the recording and left the row pointing nowhere."""

    relative, digest = recording(running.root, "original.opus")
    held = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    body = (running.root / relative).read_bytes()
    (running.root / relative).unlink()
    restored = running.root / "imports" / "restored.opus"
    restored.write_bytes(body)

    again = artifact_service.register(
        running.paths,
        relative_path="imports/restored.opus",
        kind="audio",
        track=running.track_id,
        clock=running.clock,
    )
    assert again.artifact_id == held.artifact_id
    assert restored.is_file(), "the only surviving copy was deleted"
    assert again.relative_path == "imports/restored.opus"
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.ok, "and the claims resting on it are checkable again"


def test_a_second_copy_beside_a_present_original_is_still_removed(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root, "original.opus")
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
    artifact_service.register(
        running.paths,
        relative_path="imports/copy.opus",
        kind="audio",
        track=running.track_id,
        clock=running.clock,
    )
    assert (running.root / relative).is_file()
    assert not duplicate.exists()


# --- 5. Review and ingestion run the same checks ------------------------------------


def test_review_refuses_a_package_in_another_language(running: PolishWorkspace) -> None:
    wrong = package(target_language="de")
    reviewed = speaking_service.validate(
        running.paths, package=wrong, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=wrong, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "package_language_mismatch"


def test_review_refuses_a_package_for_a_closed_session(running: PolishWorkspace) -> None:
    session_service.close(running.paths, track=running.track_id, clock=running.clock)
    reviewed = speaking_service.validate(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=package(), track=running.track_id, clock=running.clock
        )


def test_review_calls_an_exact_retry_valid_after_its_audio_is_purged(
    running: PolishWorkspace,
) -> None:
    """Ingestion accepts it as a no-op, so review must not call it invalid."""

    relative, digest = recording(running.root)
    sound = package(artifacts=(entry(relative, digest),))
    session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    artifact_service.purge(
        running.paths,
        artifact=rows(running)[0][0],
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    reviewed = speaking_service.validate(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert reviewed.valid
    assert reviewed.duplicate
    retry = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert retry.duplicate


def test_a_refusal_keeps_the_name_its_callers_know(running: PolishWorkspace) -> None:
    """One code when the problems agree; the shared code only when they genuinely differ."""

    with pytest.raises(LinguaWikiError) as single:
        session_service.ingest_package(
            running.paths,
            package=package(artifacts=(entry("imports/nothing.opus", "a" * 64),)),
            track=running.track_id,
            clock=running.clock,
        )
    assert single.value.payload.code == "package_audio_unavailable"

    relative, digest = recording(running.root)
    twin = running.root / "imports" / "two.opus"
    twin.write_bytes((running.root / relative).read_bytes())
    with pytest.raises(LinguaWikiError) as several:
        session_service.ingest_package(
            running.paths,
            package=package(
                artifacts=(
                    entry(relative, digest),
                    entry("imports/two.opus", digest, artifact_id=TWIN),
                    entry(
                        "imports/nothing.opus",
                        "a" * 64,
                        artifact_id="art_01ARZ3NDEKTSV4RRFFQ69G5FC7",
                    ),
                )
            ),
            track=running.track_id,
            clock=running.clock,
        )
    # Still one kind of problem, however many of them there are.
    assert several.value.payload.code == "package_audio_unavailable"
    assert len(several.value.payload.details) == 2


# --- 6. audio_available must mean audio is available -------------------------------


def test_a_transcript_only_package_reports_no_audio(running: PolishWorkspace) -> None:
    text = running.root / "imports" / "notes.txt"
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("a transcript, not a recording\n", encoding="utf-8")
    digest = hashlib.sha256(text.read_bytes()).hexdigest()
    report = speaking_service.ingest(
        running.paths,
        package=package(artifacts=(entry("imports/notes.txt", digest, kind="transcript"),)),
        track=running.track_id,
        clock=running.clock,
    )
    assert not report.audio_available


def test_a_duplicate_reports_whether_its_audio_is_still_there(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    sound = package(artifacts=(entry(relative, digest),))
    first = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert first.audio_available

    again = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert again.duplicate
    assert again.audio_available, "the recording is still here"

    artifact_service.purge(
        running.paths,
        artifact=rows(running)[0][0],
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    after = session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    assert after.duplicate
    assert not after.audio_available, "and now it is not"


# --- 7. The override check mirrors both halves of its constraint -------------------


def test_db_check_finds_a_reason_recorded_against_no_override(
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
        classification="uncertain",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("CREATE TABLE relaxed AS SELECT * FROM utterance_interpretations")
        transaction.execute(
            "UPDATE relaxed SET overrode_low_confidence = FALSE, "
            "override_reason = 'a reason for an override that never happened'"
        )
        transaction.execute("DROP TABLE utterance_interpretations")
        transaction.execute("ALTER TABLE relaxed RENAME TO utterance_interpretations")
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "override_provenance" in {check.name for check in report.failures}


def test_the_schema_refuses_a_reason_without_an_override(running: PolishWorkspace) -> None:
    transcript_service.import_package(
        running.paths, package=package(), track=running.track_id, clock=running.clock
    )
    recorded = transcript_service.interpret(
        running.paths,
        utterance="utt_001",
        classification="uncertain",
        track=running.track_id,
        clock=running.clock,
    )
    with (
        pytest.raises(Exception, match="CHECK|Constraint"),
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE utterance_interpretations SET override_reason = 'invented' "
            "WHERE interpretation_id = ?",
            [recorded.interpretation_id],
        )
