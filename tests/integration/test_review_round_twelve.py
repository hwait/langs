"""Regressions for the twelfth review: three defects at the staging boundary.

All three are about a boundary that accepted something it could not honour later. An
observation delivered twice became two observations; a package that could never be
credited was accepted anyway; and a close refused by the database had already written the
durable `closing` marker before finding out.
"""

from __future__ import annotations

from typing import Any

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from tests.conftest import PolishWorkspace

EVENT = "evt_01ARZ3NDEKTSV4RRFFQ69G5H00"
OTHER_EVENT = "evt_01ARZ3NDEKTSV4RRFFQ69G5H01"


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    return polish_workspace


def plan(workspace: PolishWorkspace, **overrides: Any) -> session_service.SessionReport:
    options: dict[str, Any] = {"minutes": 60}
    options.update(overrides)
    return session_service.create(
        workspace.paths, track=workspace.track_id, clock=workspace.clock, **options
    )


def started(workspace: PolishWorkspace, **overrides: Any) -> session_service.SessionReport:
    report = plan(workspace, **overrides)
    session_service.start(
        workspace.paths,
        session=report.session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )
    return report


def core(report: session_service.SessionReport) -> session_service.SessionBlockReport:
    return next(block for block in report.blocks if block.role == "core")


def attempt_event(
    block: session_service.SessionBlockReport, *, event_id: str = EVENT
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_type": "short-response",
        "modality": block.modality,
        "dimension": block.dimension,
        "score": 1.0,
        "assessor_kind": "ai",
    }
    if block.targets:
        payload["target"] = block.targets[0].content_id
    return {
        "event_id": event_id,
        "kind": "attempt.observed",
        "occurred_at": "2026-01-01T09:00:00Z",
        "payload": payload,
    }


def flush(
    workspace: PolishWorkspace,
    report: session_service.SessionReport,
    events: list[dict[str, Any]],
    *,
    sequence: int = 1,
    key: str = "twelve-1",
) -> session_service.BatchReport:
    return session_service.log(
        workspace.paths,
        batch={
            "sequence": sequence,
            "idempotency_key": key,
            "block": core(report).block_id,
            "events": events,
        },
        session=report.session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )


def package(events: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_twelve",
        "external_session_id": "call-twelve",
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
                        "text": "Szukam biletu.",
                    }
                ],
            }
        ],
        "events": events,
    }
    payload.update(overrides)
    return payload


def follow_up(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": "follow_up",
        "occurred_at": "2026-01-01T10:06:00Z",
        "payload": {"summary": "Practise ticket vocabulary."},
    }


def failures(workspace: PolishWorkspace) -> list[str]:
    report = database_service.check(workspace.paths, clock=workspace.clock)
    return [check.name for check in report.failures]


def staged_count(workspace: PolishWorkspace) -> int:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return int(database.scalar("SELECT count(*) FROM session_staged_events"))


def status(workspace: PolishWorkspace, session_id: str) -> str:
    return session_service.show(
        workspace.paths, session=session_id, track=workspace.track_id, clock=workspace.clock
    ).status


# --- 1. One observation, delivered twice, is still one observation -------------------


def test_a_package_repeating_one_event_is_refused_by_the_contract(
    running: PolishWorkspace,
) -> None:
    report = started(running)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths,
            package=package([follow_up(EVENT), follow_up(EVENT)]),
            session=report.session_id,
            track=running.track_id,
            clock=running.clock,
        )

    assert failure.value.payload.code == "invalid_session_package"
    assert "unique" in failure.value.payload.message
    assert staged_count(running) == 0


def test_a_checkpoint_and_a_completed_export_cannot_stage_one_event_twice(
    running: PolishWorkspace,
) -> None:
    """Overlapping exports of one call are the normal case, not the exotic one.

    A provider writes a checkpoint mid-call and a completed export at the end, and the
    second repeats the first's events. Staging both credited the learner twice for the
    same work.
    """

    report = started(running)
    session_service.ingest_package(
        running.paths,
        package=package([follow_up(EVENT)], package_id="pkg_checkpoint", mode="checkpoint"),
        session=report.session_id,
        track=running.track_id,
        clock=running.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths,
            package=package([follow_up(EVENT), follow_up(OTHER_EVENT)], package_id="pkg_completed"),
            session=report.session_id,
            track=running.track_id,
            clock=running.clock,
        )

    assert failure.value.payload.code == "duplicate_session_event"
    assert EVENT in failure.value.payload.message
    assert staged_count(running) == 1, "the second package staged nothing at all"


def test_an_overlapping_export_is_caught_even_on_a_different_session(
    running: PolishWorkspace,
) -> None:
    """The per-session index cannot see this one; the external session is the scope."""

    first = started(running, idempotency_key="one")
    session_service.ingest_package(
        running.paths,
        package=package([follow_up(EVENT)], package_id="pkg_checkpoint", mode="checkpoint"),
        session=first.session_id,
        track=running.track_id,
        clock=running.clock,
    )
    session_service.close(
        running.paths,
        outcome="completed",
        session=first.session_id,
        track=running.track_id,
        clock=running.clock,
    )
    second = started(running, idempotency_key="two")

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths,
            package=package([follow_up(EVENT)], package_id="pkg_completed"),
            session=second.session_id,
            track=running.track_id,
            clock=running.clock,
        )

    assert failure.value.payload.code == "duplicate_external_event"
    assert first.session_id in failure.value.payload.message
    assert "export only the events after it" in failure.value.payload.message


def test_the_same_event_flushed_in_two_batches_is_refused(running: PolishWorkspace) -> None:
    """The skill path had the same defect: a new key made a re-sent event a new one."""

    report = started(running)
    flush(running, report, [attempt_event(core(report))])

    with pytest.raises(LinguaWikiError) as failure:
        flush(
            running,
            report,
            [attempt_event(core(report))],
            sequence=2,
            key="twelve-2",
        )

    assert failure.value.payload.code == "duplicate_session_event"
    assert staged_count(running) == 1


def test_a_retried_flush_under_the_same_key_is_still_a_no_op(
    running: PolishWorkspace,
) -> None:
    """The duplicate check must not swallow the idempotent path it sits in front of."""

    report = started(running)
    first = flush(running, report, [attempt_event(core(report))])
    second = flush(running, report, [attempt_event(core(report))])

    assert not first.duplicate
    assert second.duplicate
    assert second.batch_id == first.batch_id
    assert staged_count(running) == 1


def test_recovered_work_keeps_its_identity_and_cannot_be_recovered_twice(
    running: PolishWorkspace,
) -> None:
    first = started(running, idempotency_key="one")
    flush(running, first, [attempt_event(core(first))])
    session_service.abandon(
        running.paths,
        session=first.session_id,
        track=running.track_id,
        clock=running.clock,
    )
    second = started(running, idempotency_key="two")

    session_service.recover(
        running.paths,
        source=first.session_id,
        target=second.session_id,
        track=running.track_id,
        clock=running.clock,
    )

    with open_reader(running.paths, clock=running.clock) as database:
        identities = database.query(
            "SELECT source_event_id FROM session_staged_events WHERE session_id = ?",
            [second.session_id],
        )
    assert [row[0] for row in identities] == [EVENT], "the observation keeps its identity"
    # And the same event cannot be flushed into the session it was recovered into.
    with pytest.raises(LinguaWikiError) as failure:
        flush(
            running,
            second,
            [attempt_event(core(second))],
            sequence=2,
            key="twelve-again",
        )
    assert failure.value.payload.code == "duplicate_session_event"


def test_one_external_event_staged_twice_in_a_restored_file_is_reported(
    running: PolishWorkspace,
) -> None:
    """The index covers one session; a restore can present the cross-session case."""

    first = started(running, idempotency_key="one")
    session_service.ingest_package(
        running.paths,
        package=package([follow_up(EVENT)], package_id="pkg_checkpoint", mode="checkpoint"),
        session=first.session_id,
        track=running.track_id,
        clock=running.clock,
    )
    second = started(running, idempotency_key="two")
    session_service.ingest_package(
        running.paths,
        package=package(
            [follow_up(OTHER_EVENT)], package_id="pkg_completed", external_session_id="call-two"
        ),
        session=second.session_id,
        track=running.track_id,
        clock=running.clock,
    )
    assert failures(running) == []

    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        # Both packages now claim one external session, and both hold the same event.
        transaction.execute(
            "UPDATE session_packages SET external_session_id = 'call-twelve' "
            "WHERE external_session_id = 'call-two'"
        )
        transaction.execute(
            "UPDATE session_staged_events SET source_event_id = ? WHERE source_event_id = ?",
            [EVENT, OTHER_EVENT],
        )

    assert "external_event_uniqueness" in failures(running)


# --- 2. A package that cannot be credited is refused where it arrives ---------------


def test_a_package_event_that_could_never_be_materialized_is_refused_at_ingestion(
    running: PolishWorkspace,
) -> None:
    """Accepted-then-unclosable was the failure: the session held work nobody could credit."""

    report = started(running)
    unusable = package(
        [
            {
                "event_id": EVENT,
                "kind": "attempt.observed",
                "occurred_at": "2026-01-01T10:06:00Z",
                "payload": {"utterance_id": "utt_1", "details": {}},
            }
        ]
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths,
            package=unusable,
            session=report.session_id,
            track=running.track_id,
            clock=running.clock,
        )

    assert failure.value.payload.code == "invalid_staged_payload"
    assert "task_type" in failure.value.payload.message
    assert EVENT in failure.value.payload.message
    assert staged_count(running) == 0
    with open_reader(running.paths, clock=running.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM session_packages")) == 0
    assert status(running, report.session_id) == "active", "the session is still runnable"
    # And the session closes normally afterwards, which is what "still runnable" means.
    session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        clock=running.clock,
    )
    assert status(running, report.session_id) == "completed"


def test_a_package_attempt_with_the_fields_it_needs_is_ingested(
    running: PolishWorkspace,
) -> None:
    """The refusal above must not be a refusal of every package attempt."""

    report = started(running)
    block = core(report)
    usable = package(
        [
            {
                "event_id": EVENT,
                "kind": "attempt.observed",
                "occurred_at": "2026-01-01T10:06:00Z",
                "payload": {
                    "utterance_id": "utt_1",
                    "details": {
                        "task_type": "meaning-focused-exchange",
                        "modality": "speech",
                        "dimension": block.dimension,
                        "score": 1.0,
                        "assessor_kind": "ai",
                        "confidence": "low",
                    },
                },
            }
        ]
    )

    ingested = session_service.ingest_package(
        running.paths,
        package=usable,
        session=report.session_id,
        track=running.track_id,
        clock=running.clock,
    )
    close = session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        clock=running.clock,
    )

    assert ingested.staged_events == 1
    assert close.attempts_written == 1
    assert failures(running) == []


def test_a_payload_whose_text_is_only_whitespace_is_refused_at_flush_time(
    running: PolishWorkspace,
) -> None:
    """Found while writing this file, and it is the same defect one layer down.

    `min_length=1` accepts `"   "`. Every service that stores such a field then refuses
    it -- so the flush was accepted, the close raised `invalid_observation`, and the
    session sat in `closing` holding a note nobody could credit. The boundary that
    accepts a payload has to be the boundary that can honour it.
    """

    report = started(running)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.log(
            running.paths,
            batch={
                "sequence": 1,
                "idempotency_key": "twelve-bad",
                "block": core(report).block_id,
                "events": [
                    {
                        "event_id": EVENT,
                        "kind": "observation.noted",
                        "occurred_at": "2026-01-01T09:00:00Z",
                        # A note with no note: valid enough to arrive, impossible to credit.
                        "payload": {"category": "fatigue", "note": "   "},
                    }
                ],
            },
            session=report.session_id,
            track=running.track_id,
            clock=running.clock,
        )

    assert failure.value.payload.code == "invalid_session_batch"
    assert "note" in failure.value.payload.message
    assert "blank" in failure.value.payload.message
    assert staged_count(running) == 0
    assert status(running, report.session_id) == "active"


# --- 3. Everything the database will refuse is refused before `closing` -------------


def test_a_close_key_belonging_to_another_session_is_refused_before_the_transition(
    running: PolishWorkspace,
) -> None:
    first = started(running, idempotency_key="one")
    session_service.close(
        running.paths,
        outcome="completed",
        session=first.session_id,
        track=running.track_id,
        idempotency_key="shared-close",
        clock=running.clock,
    )
    second = started(running, idempotency_key="two")

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            running.paths,
            outcome="completed",
            session=second.session_id,
            track=running.track_id,
            idempotency_key="shared-close",
            clock=running.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"
    assert first.session_id in failure.value.payload.message
    assert status(running, second.session_id) == "active", (
        "a refused close leaves the session closable, not stuck in `closing`"
    )
    # And the session still closes under its own key.
    closed = session_service.close(
        running.paths,
        outcome="completed",
        session=second.session_id,
        track=running.track_id,
        idempotency_key="its-own-close",
        clock=running.clock,
    )
    assert closed.outcome == "completed"


def test_a_close_replayed_under_its_own_key_still_returns_the_stored_result(
    running: PolishWorkspace,
) -> None:
    """The ownership check must not break the replay it sits next to."""

    report = started(running)
    first = session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        idempotency_key="mine",
        clock=running.clock,
    )
    replay = session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        idempotency_key="mine",
        clock=running.clock,
    )

    assert replay.replayed
    assert replay.finalization_id == first.finalization_id


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("actual_minutes", -1, "invalid_actual_minutes"),
        ("summary", "x" * 5000, "invalid_session_summary"),
        ("fatigue", "exhausted", "unknown_fatigue"),
    ],
)
def test_close_metadata_is_checked_before_anything_is_written(
    running: PolishWorkspace, field: str, value: Any, code: str
) -> None:
    """Each of these was a raw `ConstraintException` that left the session in `closing`."""

    report = started(running)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            running.paths,
            outcome="completed",
            session=report.session_id,
            track=running.track_id,
            clock=running.clock,
            **{field: value},
        )

    assert failure.value.payload.code == code
    assert status(running, report.session_id) == "active"
    with open_reader(running.paths, clock=running.clock) as database:
        assert database.scalar("SELECT closing_at FROM sessions") is None
    assert failures(running) == []
