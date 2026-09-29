"""The session engine end to end: staging, closing once, and every crash state.

These are the stage's exit-gate scenarios against a real database and the Polish pilot
pack. The crash tests are the reason the engine looks the way it does, so they are
written as the four states the plan names -- unflushed, staged, mid-close, and
response-lost -- and each one asserts what the *next* invocation can see and do.

Where a bad state could survive in a restored file rather than only be refused at write
time, the test asserts both: the command refuses it, and `db check` names it.
"""

from __future__ import annotations

from typing import Any

import pytest

from linguawiki import session as session_policy
from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from tests.conftest import PolishWorkspace

EVENT_IDS = [f"evt_01ARZ3NDEKTSV4RRFFQ69G5F{suffix}" for suffix in ("A0", "A1", "A2", "A3", "A4")]


@pytest.fixture
def onboarded(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    """A track past onboarding, which is where a session becomes plannable."""

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    return polish_workspace


def plan(workspace: PolishWorkspace, **overrides: Any) -> session_service.SessionReport:
    options: dict[str, Any] = {"minutes": 60, "mode": "mixed"}
    options.update(overrides)
    return session_service.create(
        workspace.paths, track=workspace.track_id, clock=workspace.clock, **options
    )


def core_block(report: session_service.SessionReport) -> session_service.SessionBlockReport:
    return next(block for block in report.blocks if block.role == "core")


def attempt_event(
    block: session_service.SessionBlockReport,
    *,
    event_id: str = EVENT_IDS[0],
    score: float = 1.0,
    occurred_at: str = "2026-01-01T09:00:00Z",
    **payload: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_type": "short-response",
        "modality": block.modality,
        "dimension": block.dimension,
        "score": score,
        "claims": ["controlled-production"],
        "assessor_kind": "ai",
    }
    if block.targets:
        body["target"] = block.targets[0].content_id
    body.update(payload)
    return {
        "event_id": event_id,
        "kind": "attempt.observed",
        "occurred_at": occurred_at,
        "payload": body,
    }


def batch(
    block: session_service.SessionBlockReport,
    *,
    sequence: int = 1,
    key: str = "batch-1",
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "idempotency_key": key,
        "block": block.block_id,
        "events": events if events is not None else [attempt_event(block)],
    }


def failures(workspace: PolishWorkspace) -> list[str]:
    report = database_service.check(workspace.paths, clock=workspace.clock)
    return [check.name for check in report.failures]


# --- The plan ------------------------------------------------------------------------


def test_a_plan_records_every_block_its_reason_and_the_candidates_it_refused(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded)

    assert report.status == "planned"
    assert report.blocks[0].role == "warm-up"
    assert report.blocks[-1].role == "closure"
    assert report.planner_version == "planner.v1"
    assert all(block.rationale for block in report.blocks)
    assert all(omission.reason for omission in report.omissions)
    assert report.next_actions == ("session start",)


def test_planning_twice_with_one_key_plans_one_session(onboarded: PolishWorkspace) -> None:
    first = plan(onboarded, idempotency_key="plan-1")
    second = plan(onboarded, idempotency_key="plan-1")

    assert first.session_id == second.session_id
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM sessions")) == 1


def test_a_second_open_session_makes_a_flush_ambiguous_rather_than_arbitrary(
    onboarded: PolishWorkspace,
) -> None:
    first = plan(onboarded, idempotency_key="plan-1")
    plan(onboarded, idempotency_key="plan-2")

    with pytest.raises(LinguaWikiError) as failure:
        session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)

    assert failure.value.payload.code == "ambiguous_session"
    assert first.session_id in failure.value.payload.message


def test_an_explicit_mode_that_cannot_run_is_refused_with_its_reason(
    onboarded: PolishWorkspace,
) -> None:
    """A speaking session on a track with no voice channel is impossible, not silent.

    The failure mode this guards against is a plan that quietly substitutes a written
    block and reports success: the learner asked to speak, and "you have no microphone
    recorded" is the answer they need.
    """

    learner_service.update_track(
        onboarded.paths,
        track=onboarded.track_id,
        preferences=learner_service.TrackPreferences(voice_available=False),
        clock=onboarded.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        plan(onboarded, mode="speaking")

    assert failure.value.payload.code == "session_mode_unavailable"
    assert "voice" in failure.value.payload.message
    assert failure.value.payload.details


def test_a_mode_that_can_run_is_honoured_exactly(onboarded: PolishWorkspace) -> None:
    report = plan(onboarded, mode="speaking")

    core = [block for block in report.blocks if block.role == "core"]
    assert core
    assert {block.block_type for block in core} <= set(session_policy.MODES["speaking"])


# --- Staging -------------------------------------------------------------------------


def test_a_flush_is_stored_and_changes_nothing_about_the_learner(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)

    flushed = session_service.log(
        onboarded.paths, batch=batch(block), track=onboarded.track_id, clock=onboarded.clock
    )

    assert flushed.staged_events == 1
    assert not flushed.duplicate
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 0
        assert int(database.scalar("SELECT count(*) FROM evidence")) == 0
        assert int(database.scalar("SELECT count(*) FROM session_staged_events")) == 1


def test_flushing_before_the_session_starts_is_refused_with_what_to_do(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.log(
            onboarded.paths,
            batch=batch(core_block(report)),
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_not_started"
    assert "session start" in failure.value.payload.message


def test_the_same_batch_twice_is_stored_once(onboarded: PolishWorkspace) -> None:
    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    payload = batch(core_block(report))

    first = session_service.log(
        onboarded.paths, batch=payload, track=onboarded.track_id, clock=onboarded.clock
    )
    second = session_service.log(
        onboarded.paths, batch=payload, track=onboarded.track_id, clock=onboarded.clock
    )

    assert not first.duplicate
    assert second.duplicate
    assert second.batch_id == first.batch_id
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM session_staged_events")) == 1


def test_one_key_carrying_different_events_is_a_conflict_not_an_overwrite(
    onboarded: PolishWorkspace,
) -> None:
    """The second call would otherwise discard the first call's observations silently."""

    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)
    session_service.log(
        onboarded.paths, batch=batch(block), track=onboarded.track_id, clock=onboarded.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.log(
            onboarded.paths,
            batch=batch(block, events=[attempt_event(block, score=0.0)]),
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"


def test_a_batch_whose_declared_hash_disagrees_is_refused(onboarded: PolishWorkspace) -> None:
    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    payload = batch(core_block(report))
    payload["content_hash"] = "0" * 64

    with pytest.raises(LinguaWikiError) as failure:
        session_service.log(
            onboarded.paths, batch=payload, track=onboarded.track_id, clock=onboarded.clock
        )

    assert failure.value.payload.code == "batch_hash_mismatch"


def test_two_batches_cannot_claim_one_sequence(onboarded: PolishWorkspace) -> None:
    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)
    session_service.log(
        onboarded.paths, batch=batch(block), track=onboarded.track_id, clock=onboarded.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.log(
            onboarded.paths,
            batch=batch(
                block, key="batch-1-again", events=[attempt_event(block, event_id=EVENT_IDS[1])]
            ),
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "batch_sequence_taken"


def test_a_block_from_another_session_cannot_be_flushed_into_this_one(
    onboarded: PolishWorkspace,
) -> None:
    first = plan(onboarded, idempotency_key="plan-1")
    second = plan(onboarded, idempotency_key="plan-2")
    session_service.start(
        onboarded.paths, session=first.session_id, track=onboarded.track_id, clock=onboarded.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.log(
            onboarded.paths,
            batch=batch(core_block(second)),
            session=first.session_id,
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "block_not_in_session"


# --- The close -----------------------------------------------------------------------


def closed_session(
    workspace: PolishWorkspace, **overrides: Any
) -> tuple[session_service.SessionReport, session_service.CloseReport]:
    report = plan(workspace, **overrides)
    session_service.start(workspace.paths, track=workspace.track_id, clock=workspace.clock)
    block = core_block(report)
    session_service.log(
        workspace.paths,
        batch=batch(
            block,
            events=[
                attempt_event(block),
                {
                    "event_id": EVENT_IDS[1],
                    "kind": "correction.given",
                    "occurred_at": "2026-01-01T09:05:00Z",
                    "payload": {
                        "category": "case-government",
                        "signature": "szukam bilet",
                        "description": "Accusative where the verb governs the genitive.",
                        "learner_form": "szukam bilet",
                        "corrected_form": "szukam biletu",
                    },
                },
                {
                    "event_id": EVENT_IDS[2],
                    "kind": "observation.noted",
                    "occurred_at": "2026-01-01T09:07:00Z",
                    "payload": {"category": "fatigue", "note": "Flagging near the end."},
                },
                {
                    "event_id": EVENT_IDS[3],
                    "kind": "follow_up",
                    "occurred_at": "2026-01-01T09:08:00Z",
                    "payload": {"kind": "practice", "action": "Drill the genitive again."},
                },
            ],
        ),
        track=workspace.track_id,
        clock=workspace.clock,
    )
    close = session_service.close(
        workspace.paths,
        outcome="completed",
        session=report.session_id,
        track=workspace.track_id,
        actual_minutes=58,
        fatigue="medium",
        clock=workspace.clock,
    )
    return report, close


def test_a_close_materializes_every_kind_of_staged_work_once(
    onboarded: PolishWorkspace,
) -> None:
    _, close = closed_session(onboarded)

    assert close.outcome == "completed"
    assert close.staged_consumed == 4
    assert close.attempts_written == 1
    assert close.evidence_written == 1
    assert close.errors_written == 1
    assert close.observations_written == 1
    assert close.followups_written == 1
    assert close.calculation_versions["aggregation"] == "mastery.v1"
    assert close.stage_changes, "an attempt on an unseen item moves it"
    assert failures(onboarded) == []


def test_a_session_attempt_is_recorded_as_a_session_attempt(
    onboarded: PolishWorkspace,
) -> None:
    """The origin is what tells an imported observation from an observed one."""

    _, close = closed_session(onboarded)

    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        origins = database.query("SELECT DISTINCT origin FROM attempts")
        staged = database.query(
            "SELECT status, materialized_kind FROM session_staged_events ORDER BY sequence"
        )
    assert origins == [("session",)]
    assert {row[0] for row in staged} == {"materialized"}
    assert {row[1] for row in staged} == {
        "attempt",
        # The *occurrence*, not the pattern: two corrections of one error are two
        # occurrences of it.
        "error-occurrence",
        "observation",
        "followup",
    }


def test_the_derived_state_moves_exactly_once(onboarded: PolishWorkspace) -> None:
    report, first = closed_session(onboarded)

    second = session_service.close(
        onboarded.paths,
        outcome="completed",
        session=report.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )

    assert second.replayed
    assert second.finalization_id == first.finalization_id
    assert second.attempts_written == first.attempts_written
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 1
        assert int(database.scalar("SELECT count(*) FROM evidence")) == 1
        assert int(database.scalar("SELECT count(*) FROM session_finalizations")) == 1


def test_a_close_cannot_be_redone_under_another_outcome(onboarded: PolishWorkspace) -> None:
    report, _ = closed_session(onboarded)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths,
            outcome="partial",
            session=report.session_id,
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_already_finalized"
    assert "completed" in failure.value.payload.message


def test_a_close_marks_the_projection_stale(onboarded: PolishWorkspace) -> None:
    closed_session(onboarded)

    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert bool(database.scalar("SELECT stale FROM projection_state WHERE projection = 'wiki'"))


def test_a_session_with_no_staged_work_closes_and_says_so(onboarded: PolishWorkspace) -> None:
    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)

    close = session_service.close(
        onboarded.paths,
        outcome="completed",
        session=report.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )

    assert close.staged_consumed == 0
    assert close.attempts_written == 0
    assert any("no staged observations" in warning for warning in close.warnings)


def test_a_lesson_observation_cannot_be_recorded_outside_a_session(
    onboarded: PolishWorkspace,
) -> None:
    """`evidence record --origin session` is refused, and that refusal is the boundary."""

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            onboarded.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            dimension="writing",
            origin="session",
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "unknown_attempt_origin"


# --- The four crash states -----------------------------------------------------------


def test_unflushed_work_is_simply_absent_and_the_session_says_what_it_holds(
    onboarded: PolishWorkspace,
) -> None:
    """Crash before a flush: nothing was staged, and nothing pretends otherwise."""

    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)

    resumed = session_service.resume(
        onboarded.paths, session=report.session_id, track=onboarded.track_id, clock=onboarded.clock
    )

    assert resumed.status == "active"
    assert resumed.staged_events == 0
    assert resumed.batches == 0
    assert resumed.last_batch_sequence is None
    assert "session log" in resumed.next_actions


def test_staged_work_survives_and_resume_says_how_far_it_got(
    onboarded: PolishWorkspace,
) -> None:
    """Crash after a flush: the batch is durable and the session is resumable."""

    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)
    session_service.log(
        onboarded.paths, batch=batch(block), track=onboarded.track_id, clock=onboarded.clock
    )

    resumed = session_service.resume(
        onboarded.paths, session=report.session_id, track=onboarded.track_id, clock=onboarded.clock
    )

    assert resumed.status == "active"
    assert resumed.staged_events == 1
    assert resumed.last_batch_sequence == 1
    staged = session_service.staged(
        onboarded.paths, session=report.session_id, track=onboarded.track_id, clock=onboarded.clock
    )
    assert [event.status for event in staged] == ["staged"]
    assert staged[0].summary
    assert "szukam" not in staged[0].summary, "a listing never quotes the learner"


def test_a_close_interrupted_mid_transaction_leaves_a_state_that_can_be_retried(
    onboarded: PolishWorkspace,
) -> None:
    """Crash during the close: `closing` with no finalization, and nothing credited.

    The durable `closing` marker is written in its own transaction precisely so this
    state exists. Simulated by writing that marker and stopping, which is what the
    rolled-back close leaves behind.
    """

    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)
    session_service.log(
        onboarded.paths, batch=batch(block), track=onboarded.track_id, clock=onboarded.clock
    )
    with (
        open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE sessions SET status = 'closing', closing_at = ?, updated_at = ? "
            "WHERE session_id = ?",
            [transaction.now(), transaction.now(), report.session_id],
        )

    interrupted = session_service.resume(
        onboarded.paths, session=report.session_id, track=onboarded.track_id, clock=onboarded.clock
    )
    assert interrupted.status == "closing"
    assert interrupted.finalization is None
    assert any("retry the close" in warning for warning in interrupted.warnings)
    assert failures(onboarded) == [], "an interrupted close is a valid state, not damage"
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 0

    retried = session_service.close(
        onboarded.paths,
        outcome="completed",
        session=report.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )

    assert not retried.replayed
    assert retried.attempts_written == 1
    assert failures(onboarded) == []


def test_a_close_whose_response_was_lost_returns_the_original_result(
    onboarded: PolishWorkspace,
) -> None:
    """Crash after the commit: the retry answers with the first close's report."""

    report, first = closed_session(onboarded)

    replay = session_service.close(
        onboarded.paths,
        outcome="completed",
        session=report.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )

    assert replay.replayed
    assert replay.model_dump(exclude={"replayed", "warnings"}) == first.model_dump(
        exclude={"replayed", "warnings"}
    )
    shown = session_service.show(
        onboarded.paths, session=report.session_id, track=onboarded.track_id, clock=onboarded.clock
    )
    assert shown.finalization is not None
    assert shown.finalization.finalization_id == first.finalization_id


def test_a_lost_flush_stops_a_close_rather_than_crediting_a_gap(
    onboarded: PolishWorkspace,
) -> None:
    """A missing batch sequence means observations are missing. Refuse, do not guess."""

    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)
    session_service.log(
        onboarded.paths,
        batch=batch(block, sequence=1, key="batch-1"),
        track=onboarded.track_id,
        clock=onboarded.clock,
    )
    session_service.log(
        onboarded.paths,
        batch=batch(
            block,
            sequence=3,
            key="batch-3",
            events=[attempt_event(block, event_id=EVENT_IDS[1])],
        ),
        track=onboarded.track_id,
        clock=onboarded.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths,
            outcome="completed",
            session=report.session_id,
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_batch_gap"
    assert "2" in failure.value.payload.message
    # The same damage in a restored file is reported rather than trusted.
    assert "session_batch_sequence" in failures(onboarded)


# --- Partial close, abandon, recover -------------------------------------------------


def test_a_partial_close_credits_the_work_and_not_the_unreached_objectives(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded, minutes=100)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)
    session_service.log(
        onboarded.paths, batch=batch(block), track=onboarded.track_id, clock=onboarded.clock
    )

    close = session_service.partial_close(
        onboarded.paths,
        session=report.session_id,
        track=onboarded.track_id,
        actual_minutes=35,
        clock=onboarded.clock,
    )

    assert close.outcome == "partial"
    assert close.attempts_written == 1
    assert any("closed as partial" in warning for warning in close.warnings)
    shown = session_service.show(
        onboarded.paths, session=report.session_id, track=onboarded.track_id, clock=onboarded.clock
    )
    statuses = {entry.block_type: entry.status for entry in shown.blocks}
    assert statuses[block.block_type] == "completed"
    assert "skipped" in set(statuses.values()), "blocks nobody reached are not completed"
    assert failures(onboarded) == []


def test_a_reviewed_partial_close_can_exclude_a_block_and_keeps_its_reason(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded, minutes=100)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    blocks = [block for block in report.blocks if block.role == "core"]
    session_service.log(
        onboarded.paths,
        batch=batch(blocks[0], sequence=1, key="batch-1"),
        track=onboarded.track_id,
        clock=onboarded.clock,
    )
    session_service.log(
        onboarded.paths,
        batch=batch(
            blocks[1],
            sequence=2,
            key="batch-2",
            events=[attempt_event(blocks[1], event_id=EVENT_IDS[1])],
        ),
        track=onboarded.track_id,
        clock=onboarded.clock,
    )

    close = session_service.partial_close(
        onboarded.paths,
        session=report.session_id,
        track=onboarded.track_id,
        discard_blocks=[blocks[1].block_id],
        clock=onboarded.clock,
    )

    assert close.attempts_written == 1
    assert close.staged_discarded == 1
    staged = session_service.staged(
        onboarded.paths, session=report.session_id, track=onboarded.track_id, clock=onboarded.clock
    )
    discarded = [event for event in staged if event.status == "discarded"]
    assert len(discarded) == 1
    assert discarded[0].discard_reason is not None
    assert "reviewed" in discarded[0].discard_reason
    assert failures(onboarded) == []


def test_abandoning_keeps_the_staged_work_and_credits_none_of_it(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded)
    session_service.start(onboarded.paths, track=onboarded.track_id, clock=onboarded.clock)
    block = core_block(report)
    session_service.log(
        onboarded.paths, batch=batch(block), track=onboarded.track_id, clock=onboarded.clock
    )

    abandoned = session_service.abandon(
        onboarded.paths,
        session=report.session_id,
        track=onboarded.track_id,
        reason="the learner had to stop",
        clock=onboarded.clock,
    )

    assert abandoned.status == "abandoned"
    assert abandoned.staged_events == 1
    assert any("credited to nothing" in warning for warning in abandoned.warnings)
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 0
        assert int(database.scalar("SELECT count(*) FROM session_finalizations")) == 0
    assert failures(onboarded) == []


def test_staged_work_can_be_recovered_into_a_new_session_and_only_counts_once(
    onboarded: PolishWorkspace,
) -> None:
    first = plan(onboarded, idempotency_key="plan-1")
    session_service.start(
        onboarded.paths, session=first.session_id, track=onboarded.track_id, clock=onboarded.clock
    )
    block = core_block(first)
    session_service.log(
        onboarded.paths,
        batch=batch(block),
        session=first.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )
    session_service.abandon(
        onboarded.paths,
        session=first.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )
    second = plan(onboarded, idempotency_key="plan-2")
    session_service.start(
        onboarded.paths, session=second.session_id, track=onboarded.track_id, clock=onboarded.clock
    )

    recovery = session_service.recover(
        onboarded.paths,
        source=first.session_id,
        target=second.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )

    assert recovery.recovered == 1
    source_staged = session_service.staged(
        onboarded.paths, session=first.session_id, track=onboarded.track_id, clock=onboarded.clock
    )
    assert [event.status for event in source_staged] == ["discarded"]
    assert second.session_id in str(source_staged[0].discard_reason)

    close = session_service.close(
        onboarded.paths,
        outcome="completed",
        session=second.session_id,
        track=onboarded.track_id,
        clock=onboarded.clock,
    )
    assert close.attempts_written == 1
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 1
    assert failures(onboarded) == []


def test_an_open_session_cannot_be_recovered_from(onboarded: PolishWorkspace) -> None:
    first = plan(onboarded, idempotency_key="plan-1")
    second = plan(onboarded, idempotency_key="plan-2")
    session_service.start(
        onboarded.paths, session=second.session_id, track=onboarded.track_id, clock=onboarded.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.recover(
            onboarded.paths,
            source=first.session_id,
            target=second.session_id,
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_still_open"


def test_a_finished_session_cannot_be_resumed_and_says_what_remains(
    onboarded: PolishWorkspace,
) -> None:
    report, _ = closed_session(onboarded)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.resume(
            onboarded.paths,
            session=report.session_id,
            track=onboarded.track_id,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_already_finished"
