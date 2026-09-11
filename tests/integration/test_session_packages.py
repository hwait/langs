"""Ingesting an externally produced session, and detecting damage after the fact.

Two things are being tested here. First, `session ingest-package`: a package is somebody
else's account of a session, so it is validated, deduplicated by content, and *staged* --
it becomes part of the learner's model through the same close as everything else.

Second, `db check`. Every invariant the session engine enforces when it writes is also
asserted over the data, because a restored file, a hand-repaired database, or a build
under a looser rule can present a state no command would produce. Each case below
injects exactly that state and requires the named check to fail.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from tests.conftest import PolishWorkspace

PACKAGE_EVENT = "evt_01ARZ3NDEKTSV4RRFFQ69G5FC0"


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> tuple[PolishWorkspace, str]:
    """An onboarded track with one active session, ready to receive a package."""

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    report = session_service.create(
        polish_workspace.paths,
        minutes=60,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    session_service.start(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    return polish_workspace, report.session_id


def package(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_tutor_call_1",
        "external_session_id": "tutor-call-1",
        "target_language": "pl",
        "mode": "completed",
        "started_at": "2026-01-01T10:00:00Z",
        "ended_at": "2026-01-01T10:30:00Z",
        "transcript_layers": [
            {
                "kind": "raw",
                "utterances": [
                    {
                        "utterance_id": "utt_1",
                        "speaker": "learner",
                        "started_at": "2026-01-01T10:05:00Z",
                        "ended_at": "2026-01-01T10:05:04Z",
                        "text": "Szukam bilet.",
                    }
                ],
            }
        ],
        "events": [
            {
                "event_id": PACKAGE_EVENT,
                "kind": "pronunciation.assessment",
                "occurred_at": "2026-01-01T10:06:00Z",
                "payload": {"status": "uncertain", "utterance_id": "utt_1"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def failures(workspace: PolishWorkspace) -> list[str]:
    report = database_service.check(workspace.paths, clock=workspace.clock)
    return [check.name for check in report.failures]


# --- Ingestion -----------------------------------------------------------------------


def test_a_package_is_validated_and_staged_never_materialized(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running

    report = session_service.ingest_package(
        workspace.paths,
        package=package(),
        session=session_id,
        track=workspace.track_id,
        producer="test-provider",
        clock=workspace.clock,
    )

    assert report.staged_events == 1
    assert not report.duplicate
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 0
        assert int(database.scalar("SELECT count(*) FROM session_observations")) == 0
        assert database.scalar("SELECT source FROM session_event_batches") == "package"
    assert failures(workspace) == []


def test_the_same_package_content_is_ingested_once(
    running: tuple[PolishWorkspace, str],
) -> None:
    """Deduplication is by canonical content, so a re-export is the same session."""

    workspace, session_id = running
    first = session_service.ingest_package(
        workspace.paths,
        package=package(),
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )
    # Same content, different key order and a different file: still one session.
    reordered = dict(reversed(list(package().items())))
    second = session_service.ingest_package(
        workspace.paths,
        package=reordered,
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )

    assert not first.duplicate
    assert second.duplicate
    assert second.package_hash == first.package_hash
    assert second.staged_events == 0
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM session_packages")) == 1
        assert int(database.scalar("SELECT count(*) FROM session_staged_events")) == 1


def test_an_unsupported_schema_version_is_refused_with_what_to_do(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            workspace.paths,
            package=package(schema_version=2),
            session=session_id,
            track=workspace.track_id,
            clock=workspace.clock,
        )

    assert failure.value.payload.code == "unsupported_package_schema"
    assert "lingua.session.v1" in failure.value.payload.message
    assert "convert" in failure.value.payload.message


def test_a_package_naming_another_track_is_refused(
    running: tuple[PolishWorkspace, str],
) -> None:
    """An external recording is one learner's; attaching it elsewhere moves their work."""

    workspace, session_id = running

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            workspace.paths,
            package=package(track_hint="trk_01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            session=session_id,
            track=workspace.track_id,
            clock=workspace.clock,
        )

    assert failure.value.payload.code == "package_track_mismatch"


def test_a_transcript_only_pronunciation_observation_confirms_nothing(
    running: tuple[PolishWorkspace, str],
) -> None:
    """The rule the whole speaking model rests on, applied at the session boundary.

    A correct transcript proves nothing about how something sounded, so an unconfirmed
    pronunciation event is materialized as a note and the close says why.
    """

    workspace, session_id = running
    session_service.ingest_package(
        workspace.paths,
        package=package(),
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )

    staged = session_service.staged(
        workspace.paths, session=session_id, track=workspace.track_id, clock=workspace.clock
    )
    assert [event.evidence_basis for event in staged] == ["transcript"]

    close = session_service.close(
        workspace.paths,
        outcome="completed",
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )

    assert close.observations_written == 1
    assert close.attempts_written == 0
    assert any("rather than audio" in warning for warning in close.warnings)
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM evidence")) == 0


def test_a_confirmed_pronunciation_observation_requires_audio(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running
    broken = package(
        events=[
            {
                "event_id": PACKAGE_EVENT,
                "kind": "pronunciation.assessment",
                "occurred_at": "2026-01-01T10:06:00Z",
                # `confirmed` without an audio artifact: the contract refuses it.
                "payload": {"status": "confirmed", "utterance_id": "utt_1"},
            }
        ]
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            workspace.paths,
            package=broken,
            session=session_id,
            track=workspace.track_id,
            clock=workspace.clock,
        )

    # Named rather than raw: a malformed *input* used to surface as a generic contract
    # failure that said the output had not validated.
    assert failure.value.payload.code == "invalid_session_package"
    assert "audio" in failure.value.payload.message


def test_an_audio_backed_observation_records_its_basis(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running
    with_audio = package(
        artifacts=[
            {
                "artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "kind": "audio",
                "relative_path": "inbox/call-1.opus",
                "sha256": "a" * 64,
                "retained": True,
            }
        ],
        events=[
            {
                "event_id": PACKAGE_EVENT,
                "kind": "pronunciation.assessment",
                "occurred_at": "2026-01-01T10:06:00Z",
                "payload": {
                    "status": "confirmed",
                    "utterance_id": "utt_1",
                    "audio_artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                },
            }
        ],
    )

    report = session_service.ingest_package(
        workspace.paths,
        package=with_audio,
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )

    assert report.audio_available
    staged = session_service.staged(
        workspace.paths, session=session_id, track=workspace.track_id, clock=workspace.clock
    )
    assert [event.evidence_basis for event in staged] == ["audio"]
    close = session_service.close(
        workspace.paths,
        outcome="completed",
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )
    assert close.observations_written == 1
    assert any("linked to audio" in warning for warning in close.warnings)


def test_a_track_without_transcript_consent_keeps_no_utterance_text(
    running: tuple[PolishWorkspace, str],
) -> None:
    """Staged payloads are not a privacy loophole: the retention rule reaches them."""

    from linguawiki.services import learners as learner_service

    workspace, session_id = running
    learner_service.update_track(
        workspace.paths,
        track=workspace.track_id,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=workspace.clock,
    )

    report = session_service.ingest_package(
        workspace.paths,
        package=package(),
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )

    assert report.retention_policy == "withheld"
    assert any("not consented" in warning for warning in report.warnings)
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        payloads = database.query("SELECT payload_json FROM session_staged_events")
        manifests = database.query("SELECT manifest_json FROM session_packages")
    assert all("Szukam bilet" not in str(row[0]) for row in payloads)
    assert all("Szukam bilet" not in str(row[0]) for row in manifests)


# --- Detecting damage that no command would produce ----------------------------------


def closed(workspace: PolishWorkspace, session_id: str) -> session_service.CloseReport:
    """One flush and a close, so there is materialized state to damage."""

    report = session_service.show(
        workspace.paths, session=session_id, track=workspace.track_id, clock=workspace.clock
    )
    block = next(entry for entry in report.blocks if entry.role == "core")
    session_service.log(
        workspace.paths,
        batch={
            "sequence": 1,
            "idempotency_key": "damage-batch-1",
            "block": block.block_id,
            "events": [
                {
                    "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FD0",
                    "kind": "attempt.observed",
                    "occurred_at": "2026-01-01T09:00:00Z",
                    "payload": {
                        "task_type": "short-response",
                        "modality": block.modality,
                        "dimension": block.dimension,
                        "target": block.targets[0].content_id if block.targets else None,
                        "score": 1.0,
                        "claims": ["controlled-production"],
                    },
                }
            ],
        },
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )
    return session_service.close(
        workspace.paths,
        outcome="completed",
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )


def damage(workspace: PolishWorkspace, statement: str, parameters: list[Any]) -> None:
    """Write a state the service refuses, the way a bad restore would leave it."""

    with (
        open_writer(workspace.paths, command="test", clock=workspace.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(statement, parameters)


def test_a_closed_session_with_no_finalization_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running
    closed(workspace, session_id)
    assert failures(workspace) == []

    damage(workspace, "DELETE FROM session_finalizations WHERE session_id = ?", [session_id])

    assert "session_finalization_pairing" in failures(workspace)


def test_a_finalization_whose_outcome_disagrees_with_the_session_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running
    closed(workspace, session_id)

    damage(
        workspace,
        "UPDATE session_finalizations SET outcome = 'partial' WHERE session_id = ?",
        [session_id],
    )

    assert "session_outcome_agreement" in failures(workspace)


def test_two_staged_events_claiming_one_attempt_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    """The "exactly once" promise, stated as data rather than as a call order."""

    workspace, session_id = running
    report = closed(workspace, session_id)
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        staged = database.one(
            "SELECT staged_event_id, batch_id, materialized_id FROM session_staged_events "
            "WHERE materialized_kind = 'attempt'"
        )
    assert staged is not None
    damage(
        workspace,
        "INSERT INTO session_staged_events (staged_event_id, session_id, batch_id, sequence, "
        "kind, schema_version, payload_json, evidence_basis, status, finalization_id, "
        "materialized_kind, materialized_id, occurred_at, source_event_id, created_at) "
        "VALUES (?, ?, ?, 99, 'attempt.observed', 1, '{}', 'direct', 'materialized', ?, "
        "'attempt', ?, now(), 'evt_restored_duplicate', now())",
        [
            "sev_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            session_id,
            str(staged[1]),
            report.finalization_id,
            str(staged[2]),
        ],
    )

    assert "staged_event_materialized_once" in failures(workspace)


def test_a_materialized_event_naming_no_close_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running
    closed(workspace, session_id)

    damage(
        workspace,
        "UPDATE session_staged_events SET finalization_id = ? WHERE session_id = ?",
        ["fin_01ARZ3NDEKTSV4RRFFQ69G5FAV", session_id],
    )

    reported = failures(workspace)
    assert "staged_event_finalization" in reported
    assert "orphan_relations" in reported, (
        "the relation the schema could not declare is carried by name"
    )


def test_a_closed_session_still_holding_staged_events_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running
    closed(workspace, session_id)

    damage(
        workspace,
        "UPDATE session_staged_events SET status = 'staged', finalization_id = NULL, "
        "materialized_kind = NULL, materialized_id = NULL WHERE session_id = ?",
        [session_id],
    )

    assert "closed_session_staging_resolved" in failures(workspace)


def test_an_attempt_from_another_learner_s_session_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    """One learner's sitting must never land in another learner's model.

    Injected the only way DuckDB allows: a *new* staged row on this session pointing at
    an attempt that belongs to a second learner. The columns that would make this state
    reachable by an UPDATE -- `sessions.track_id`, `attempts.track_id` -- are foreign
    keys on referenced rows, and DuckDB refuses to update those in place, which is why
    the relation lives in a named check rather than in the schema.
    """

    from linguawiki.services import evidence as evidence_service
    from linguawiki.services import learners as learner_service

    workspace, session_id = running
    report = closed(workspace, session_id)
    other_user = learner_service.create_user(
        workspace.paths,
        display_name="Second Learner",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        clock=workspace.clock,
    )
    other_track = learner_service.create_track(
        workspace.paths,
        user=other_user.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        clock=workspace.clock,
    )
    theirs = evidence_service.record(
        workspace.paths,
        task_type="objective",
        modality="text",
        score=1.0,
        dimension="reading",
        origin="import",
        track=other_track.track_id,
        clock=workspace.clock,
    )
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        batch_id = database.scalar(
            "SELECT batch_id FROM session_event_batches WHERE session_id = ?", [session_id]
        )

    damage(
        workspace,
        "INSERT INTO session_staged_events (staged_event_id, session_id, batch_id, sequence, "
        "kind, schema_version, payload_json, evidence_basis, status, finalization_id, "
        "materialized_kind, materialized_id, occurred_at, source_event_id, created_at) "
        "VALUES (?, ?, ?, 98, 'attempt.observed', 1, '{}', 'direct', 'materialized', ?, "
        "'attempt', ?, now(), 'evt_restored_crossed', now())",
        [
            "sev_01ARZ3NDEKTSV4RRFFQ69G5FB0",
            session_id,
            str(batch_id),
            report.finalization_id,
            theirs.attempt_id,
        ],
    )

    assert "session_attempt_track" in failures(workspace)


def test_a_session_over_its_own_novelty_cap_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    """The cap is a promise about the whole session, so it is counted over the session."""

    workspace, session_id = running

    damage(
        workspace,
        "UPDATE sessions SET novel_target_cap = 0 WHERE session_id = ?",
        [session_id],
    )

    assert "session_novelty_cap" in failures(workspace)


def test_a_framing_block_carrying_new_material_is_reported(
    running: tuple[PolishWorkspace, str],
) -> None:
    workspace, session_id = running

    damage(
        workspace,
        "UPDATE session_block_targets SET novel = TRUE WHERE block_id IN "
        "(SELECT block_id FROM session_blocks WHERE session_id = ? AND role <> 'core')",
        [session_id],
    )

    reported = failures(workspace)
    assert "session_framing_blocks" in reported


def test_a_batch_sequence_gap_is_reported(running: tuple[PolishWorkspace, str]) -> None:
    """A gap means a flush was lost, and a restored file has to say so.

    Injected by adding a batch beyond the next sequence rather than by renumbering an
    existing one: `session_event_batches` carries a unique index over
    `(session_id, sequence)`, and DuckDB rewrites an update of an indexed column as a
    delete and an insert, which a row referenced by staged events refuses.
    """

    workspace, session_id = running
    closed(workspace, session_id)
    assert failures(workspace) == []

    damage(
        workspace,
        "INSERT INTO session_event_batches (batch_id, session_id, sequence, idempotency_key, "
        "content_hash, event_count, source, created_at) "
        "VALUES (?, ?, 4, 'restored-gap', ?, 0, 'skill', now())",
        ["bat_01ARZ3NDEKTSV4RRFFQ69G5FAV", session_id, "b" * 64],
    )

    assert "session_batch_sequence" in failures(workspace)


def test_one_package_ingested_twice_is_reported(running: tuple[PolishWorkspace, str]) -> None:
    """The unique index prevents it going forward; the check says so for a restore."""

    workspace, session_id = running
    session_service.ingest_package(
        workspace.paths,
        package=package(),
        session=session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        row = database.one(
            "SELECT package_hash, track_id, session_id FROM session_packages LIMIT 1"
        )
    assert row is not None
    # The unique index refuses it, which is the first line of defence; the named check
    # exists for a database that arrived from somewhere the index was not applied.
    with pytest.raises(duckdb.ConstraintException):
        damage(
            workspace,
            "INSERT INTO session_packages (ingestion_id, package_id, schema_name, "
            "schema_version, package_hash, track_id, session_id, external_session_id, mode, "
            "started_at, ended_at, ingested_at, created_at) "
            "VALUES (?, 'pkg_dup', 'lingua.session.v1', 1, ?, ?, ?, 'dup', 'completed', "
            "now(), now(), now(), now())",
            [
                "ing_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                str(row[0]),
                str(row[1]),
                str(row[2]),
            ],
        )
