"""Regressions for the eleventh review: ten defects in the session engine.

Each test names the state the defect produced and requires the command to refuse it. The
five that could survive in a restored file also require `db check` to report it, because
a rule enforced only at write time is a rule a restore can walk past.

The worst of them was the first: a track that had explicitly refused transcript retention
had the learner's full text stored anyway, in a staged row that outlives the session.
"""

from __future__ import annotations

from typing import Any

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from tests.conftest import PolishWorkspace

LEARNER_TEXT = "Szukam bilet do Krakowa, bardzo prosze, bo pociag odjezdza za chwile."
EVENTS = [f"evt_01ARZ3NDEKTSV4RRFFQ69G5G{suffix}" for suffix in ("A0", "A1", "A2", "A3", "A4")]


def onboard(workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(workspace.paths, declared_level="A2", clock=workspace.clock)
    onboarding_service.finalize(workspace.paths, clock=workspace.clock)
    return workspace


@pytest.fixture
def consenting(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    """The pilot fixture's track already consents to transcript retention."""

    return onboard(polish_workspace)


@pytest.fixture
def refusing(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    """A track that has explicitly refused to have its transcripts kept."""

    learner_service.update_track(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        preferences=learner_service.TrackPreferences(
            voice_available=True, transcript_retention_consent=False
        ),
        clock=polish_workspace.clock,
    )
    return onboard(polish_workspace)


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
    block: session_service.SessionBlockReport,
    *,
    event_id: str = EVENTS[0],
    occurred_at: str = "2026-01-01T09:00:00Z",
    **payload: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_type": "short-response",
        "modality": block.modality,
        "dimension": block.dimension,
        "score": 1.0,
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


def flush(
    workspace: PolishWorkspace,
    report: session_service.SessionReport,
    events: list[dict[str, Any]],
    *,
    sequence: int = 1,
    key: str = "round-eleven-1",
    block: session_service.SessionBlockReport | None = None,
) -> session_service.BatchReport:
    chosen = block or core(report)
    return session_service.log(
        workspace.paths,
        batch={
            "sequence": sequence,
            "idempotency_key": key,
            "block": chosen.block_id,
            "events": events,
        },
        session=report.session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )


def failures(workspace: PolishWorkspace) -> list[str]:
    report = database_service.check(workspace.paths, clock=workspace.clock)
    return [check.name for check in report.failures]


def staged_payloads(workspace: PolishWorkspace) -> list[str]:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return [
            str(row[0]) for row in database.query("SELECT payload_json FROM session_staged_events")
        ]


def damage(workspace: PolishWorkspace, statement: str, parameters: list[Any]) -> None:
    with (
        open_writer(workspace.paths, command="test", clock=workspace.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(statement, parameters)


# --- 1. Retention is applied before anything is staged -------------------------------


def test_a_staged_event_never_holds_more_of_the_learner_than_consent_allows(
    refusing: PolishWorkspace,
) -> None:
    """The stage's worst defect: the staged row outlives the session, text and all."""

    report = started(refusing)

    flush(refusing, report, [attempt_event(core(report), response=LEARNER_TEXT)])

    payloads = staged_payloads(refusing)
    assert payloads
    assert all(LEARNER_TEXT not in payload for payload in payloads)
    assert all('"response_visibility": "withheld"' in payload for payload in payloads)
    # The response existed and was judged: the hash is what says so.
    assert all('"response_hash"' in payload for payload in payloads)


def test_asking_to_keep_more_than_consent_allows_is_refused_at_flush_time(
    refusing: PolishWorkspace,
) -> None:
    """Refused where the text arrives, not at close, so the session stays closable.

    Before, the flush was accepted with the full text and the *close* refused it -- which
    left the retained text in place and the session stuck in `closing` with no way
    forward.
    """

    report = started(refusing)

    with pytest.raises(LinguaWikiError) as failure:
        flush(
            refusing,
            report,
            [attempt_event(core(report), response=LEARNER_TEXT, response_visibility="full")],
        )

    assert failure.value.payload.code == "transcript_consent_required"
    assert staged_payloads(refusing) == []
    assert (
        session_service.show(
            refusing.paths, session=report.session_id, track=refusing.track_id, clock=refusing.clock
        ).status
        == "active"
    ), "a refused flush leaves the session runnable"


def test_a_consenting_track_keeps_a_bounded_excerpt_and_closes(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting)

    flush(consenting, report, [attempt_event(core(report), response=LEARNER_TEXT)])
    close = session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    assert close.attempts_written == 1
    with open_reader(consenting.paths, clock=consenting.clock) as database:
        visibility, excerpt, digest = database.one(
            "SELECT response_visibility, response_excerpt, response_hash FROM attempts"
        )
    assert visibility == "excerpt"
    assert excerpt is not None
    assert digest is not None


def test_a_package_cannot_set_its_own_retention_through_free_form_details(
    refusing: PolishWorkspace,
) -> None:
    """The other half of the same defect: `details` merged after the retention filter."""

    report = started(refusing)
    package = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_details",
        "external_session_id": "call-1",
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
                        "text": LEARNER_TEXT,
                    }
                ],
            }
        ],
        "events": [
            {
                "event_id": EVENTS[0],
                "kind": "follow_up",
                "occurred_at": "2026-01-01T10:06:00Z",
                "payload": {"summary": "Practise ticket vocabulary."},
            }
        ],
    }

    session_service.ingest_package(
        refusing.paths,
        package=package,
        session=report.session_id,
        track=refusing.track_id,
        clock=refusing.clock,
    )

    payloads = staged_payloads(refusing)
    assert payloads
    assert all(LEARNER_TEXT not in payload for payload in payloads)


def test_a_flush_cannot_claim_it_came_from_a_package(consenting: PolishWorkspace) -> None:
    """Provenance is set by the ingestion path, never by the payload that arrives."""

    report = started(consenting)

    flush(
        consenting,
        report,
        [
            attempt_event(
                core(report),
                source="package",
                package_id="pkg_invented",
                transcript_layer="reviewed-hearing",
            )
        ],
    )

    payloads = staged_payloads(consenting)
    assert payloads
    assert all("pkg_invented" not in payload for payload in payloads)
    assert all("reviewed-hearing" not in payload for payload in payloads)


# --- 2. The event's own time is what the learner model is built from -----------------


def test_an_observation_is_dated_when_it_happened_not_when_it_was_flushed(
    consenting: PolishWorkspace,
) -> None:
    """A block worked in December and flushed in January is a December observation."""

    report = started(consenting)

    flush(
        consenting,
        report,
        [attempt_event(core(report), occurred_at="2025-12-20T18:30:00Z")],
    )
    session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    with open_reader(consenting.paths, clock=consenting.clock) as database:
        attempt_at = database.scalar("SELECT occurred_at FROM attempts")
        evidence_at = database.scalar("SELECT occurred_at FROM evidence")
        staged_at, flushed_at = database.one(
            "SELECT occurred_at, created_at FROM session_staged_events"
        )
    assert attempt_at.isoformat().startswith("2025-12-20T18:30")
    assert evidence_at.isoformat().startswith("2025-12-20T18:30")
    assert staged_at != flushed_at, "both timestamps are kept, and they are different facts"
    assert failures(consenting) == []


def test_an_attempt_dated_differently_from_its_event_is_reported(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting)
    flush(consenting, report, [attempt_event(core(report))])
    session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )
    assert failures(consenting) == []

    damage(
        consenting,
        "UPDATE session_staged_events SET occurred_at = occurred_at - INTERVAL 3 DAY "
        "WHERE session_id = ?",
        [report.session_id],
    )

    assert "session_attempt_occurrence_time" in failures(consenting)


def test_recovered_work_keeps_the_time_it_happened(consenting: PolishWorkspace) -> None:
    first = started(consenting, idempotency_key="one")
    flush(
        consenting,
        first,
        [attempt_event(core(first), occurred_at="2025-12-20T18:30:00Z")],
    )
    session_service.abandon(
        consenting.paths,
        session=first.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )
    second = started(consenting, idempotency_key="two")

    session_service.recover(
        consenting.paths,
        source=first.session_id,
        target=second.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    with open_reader(consenting.paths, clock=consenting.clock) as database:
        times = database.query(
            "SELECT occurred_at FROM session_staged_events WHERE session_id = ?",
            [second.session_id],
        )
    assert times[0][0].isoformat().startswith("2025-12-20T18:30")


# --- 3. A package belongs to one learner --------------------------------------------


def _package(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_shared_call",
        "external_session_id": "call-1",
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
                        "text": LEARNER_TEXT,
                    }
                ],
            }
        ],
        "events": [],
    }
    payload.update(overrides)
    return payload


def test_one_learner_s_package_is_not_a_duplicate_for_another(
    consenting: PolishWorkspace,
) -> None:
    """The duplicate lookup keyed on the hash alone, so it answered across learners.

    Learner B was told "already ingested" about a recording they have never had, and the
    report handed them learner A's session identifier.
    """

    first = started(consenting, idempotency_key="a")
    session_service.ingest_package(
        consenting.paths,
        package=_package(),
        session=first.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )
    other_user = learner_service.create_user(
        consenting.paths,
        display_name="Second Learner",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        clock=consenting.clock,
    )
    other_track = learner_service.create_track(
        consenting.paths,
        user=other_user.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        clock=consenting.clock,
    ).track_id
    theirs = session_service.create(
        consenting.paths,
        minutes=60,
        track=other_track,
        idempotency_key="b",
        clock=consenting.clock,
    )
    session_service.start(
        consenting.paths,
        session=theirs.session_id,
        track=other_track,
        clock=consenting.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            consenting.paths,
            package=_package(),
            session=theirs.session_id,
            track=other_track,
            clock=consenting.clock,
        )

    assert failure.value.payload.code == "package_track_conflict"
    assert consenting.track_id in failure.value.payload.message
    assert first.session_id not in failure.value.payload.message


def test_the_same_learner_re_ingesting_their_own_package_is_still_a_no_op(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting)
    session_service.ingest_package(
        consenting.paths,
        package=_package(),
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    again = session_service.ingest_package(
        consenting.paths,
        package=_package(),
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    assert again.duplicate
    assert again.session_id == report.session_id
    assert again.track_id == consenting.track_id


def test_a_package_in_another_language_is_not_this_track_s_evidence(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            consenting.paths,
            package=_package(target_language="zh", package_id="pkg_other_language"),
            session=report.session_id,
            track=consenting.track_id,
            clock=consenting.clock,
        )

    assert failure.value.payload.code == "package_language_mismatch"
    assert "zh" in failure.value.payload.message


def test_a_package_attached_to_another_track_s_session_is_reported(
    consenting: PolishWorkspace,
) -> None:
    """Ingestion refuses it; a restored file has to say so too."""

    report = started(consenting)
    session_service.ingest_package(
        consenting.paths,
        package=_package(),
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )
    assert failures(consenting) == []
    other_user = learner_service.create_user(
        consenting.paths,
        display_name="Second Learner",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        clock=consenting.clock,
    )
    other_track = learner_service.create_track(
        consenting.paths,
        user=other_user.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        clock=consenting.clock,
    ).track_id

    damage(
        consenting,
        "UPDATE session_packages SET track_id = ? WHERE session_id = ?",
        [other_track, report.session_id],
    )

    assert "session_package_track" in failures(consenting)


# --- 4. A staged payload is revalidated before it is credited ------------------------


def test_a_follow_up_with_a_due_window_closes(consenting: PolishWorkspace) -> None:
    """Serialized datetimes came back as strings and crashed the close with a raw
    `AttributeError`, leaving the session in `closing` with nothing credited."""

    report = started(consenting)
    flush(
        consenting,
        report,
        [
            {
                "event_id": EVENTS[1],
                "kind": "follow_up",
                "occurred_at": "2026-01-01T09:00:00Z",
                "payload": {
                    "kind": "practice",
                    "action": "Drill the genitive after szukać.",
                    "due_from": "2026-01-02T09:00:00Z",
                    "due_by": "2026-01-09T09:00:00Z",
                },
            }
        ],
    )

    close = session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    assert close.followups_written == 1
    with open_reader(consenting.paths, clock=consenting.clock) as database:
        due_from, due_by = database.one("SELECT due_from, due_by FROM followups")
    assert due_from.isoformat().startswith("2026-01-02")
    assert due_by.isoformat().startswith("2026-01-09")
    assert failures(consenting) == []


def test_a_staged_payload_that_no_longer_validates_is_named_not_crashed_on(
    consenting: PolishWorkspace,
) -> None:
    """A payload damaged after it was staged is reported by name, with the session intact."""

    report = started(consenting)
    flush(consenting, report, [attempt_event(core(report))])
    damage(
        consenting,
        "UPDATE session_staged_events SET payload_json = ? WHERE session_id = ?",
        ['{"task_type": "short-response", "modality": "text"}', report.session_id],
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            consenting.paths,
            outcome="completed",
            session=report.session_id,
            track=consenting.track_id,
            clock=consenting.clock,
        )

    assert failure.value.payload.code == "invalid_staged_payload"
    with open_reader(consenting.paths, clock=consenting.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 0


# --- 5. Dependent events see each other ---------------------------------------------


def _correction(event_id: str, occurred_at: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": "correction.given",
        "occurred_at": occurred_at,
        "payload": {
            "category": "case-government",
            "signature": "szukam bilet",
            "description": "Accusative where the verb governs the genitive.",
        },
    }


def test_two_corrections_of_one_pattern_are_two_occurrences_of_it(
    consenting: PolishWorkspace,
) -> None:
    """Planning every event before writing any made both believe they were the first.

    Where the pattern was new that was a duplicate-key failure; where it already existed
    it was a silently wrong occurrence count, and `db check` caught the consequence.
    """

    report = started(consenting)
    flush(
        consenting,
        report,
        [
            _correction(EVENTS[1], "2026-01-01T09:00:00Z"),
            _correction(EVENTS[2], "2026-01-01T09:05:00Z"),
        ],
    )

    close = session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    assert close.errors_written == 2
    with open_reader(consenting.paths, clock=consenting.clock) as database:
        patterns = database.query("SELECT error_id, occurrence_count FROM error_patterns")
        occurrences = int(database.scalar("SELECT count(*) FROM error_occurrences"))
        materialized = database.query(
            "SELECT materialized_kind, materialized_id FROM session_staged_events "
            "WHERE status = 'materialized'"
        )
    assert len(patterns) == 1, "one pattern, not two"
    assert patterns[0][1] == 2, "and it has been seen twice"
    assert occurrences == 2
    assert {row[0] for row in materialized} == {"error-occurrence"}
    assert len({row[1] for row in materialized}) == 2, "each names its own occurrence"
    assert failures(consenting) == []


def test_two_attempts_on_one_item_are_ordered_by_when_they_happened(
    consenting: PolishWorkspace,
) -> None:
    """The second attempt is only the second if the first is folded in before it."""

    report = started(consenting)
    block = core(report)
    flush(
        consenting,
        report,
        [
            attempt_event(block, event_id=EVENTS[1], occurred_at="2026-01-01T09:00:00Z"),
            attempt_event(block, event_id=EVENTS[2], occurred_at="2026-01-01T09:20:00Z"),
        ],
    )

    session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    with open_reader(consenting.paths, clock=consenting.clock) as database:
        novelty = database.query(
            "SELECT evidence.novelty FROM evidence "
            "JOIN attempts attempt ON attempt.attempt_id = evidence.attempt_id "
            "ORDER BY attempt.occurred_at"
        )
    assert [row[0] for row in novelty] == ["novel", "repeat"], (
        "the second observation in one context is a repeat, which needs the first to "
        "have been folded in already"
    )


def test_a_materialized_event_naming_a_row_of_another_kind_is_reported(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting)
    flush(consenting, report, [_correction(EVENTS[1], "2026-01-01T09:00:00Z")])
    session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )
    assert failures(consenting) == []

    # The defect's own shape: the *pattern* recorded where the occurrence belongs.
    damage(
        consenting,
        "UPDATE session_staged_events SET materialized_id = "
        "(SELECT error_id FROM error_patterns LIMIT 1) WHERE session_id = ?",
        [report.session_id],
    )

    assert "staged_event_materialized_target" in failures(consenting)


# --- 6. A partial close is the remedy for a lost flush ------------------------------


def test_a_partial_close_credits_what_arrived_when_a_flush_was_lost(
    consenting: PolishWorkspace,
) -> None:
    """The gap check refused the very command its own message recommended.

    A completed close still refuses -- crediting a session as whole while a block's
    observations are missing is the thing worth refusing -- but the partial close now
    keeps the work that did arrive and says what did not.
    """

    report = started(consenting, minutes=100)
    blocks = [block for block in report.blocks if block.role == "core"]
    flush(consenting, report, [attempt_event(blocks[0], event_id=EVENTS[1])], block=blocks[0])
    flush(
        consenting,
        report,
        [attempt_event(blocks[1], event_id=EVENTS[2], occurred_at="2026-01-01T09:30:00Z")],
        sequence=3,
        key="round-eleven-3",
        block=blocks[1],
    )

    with pytest.raises(LinguaWikiError) as refused:
        session_service.close(
            consenting.paths,
            outcome="completed",
            session=report.session_id,
            track=consenting.track_id,
            clock=consenting.clock,
        )
    assert refused.value.payload.code == "session_batch_gap"
    assert "close as partial" in refused.value.payload.message

    close = session_service.partial_close(
        consenting.paths,
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    assert close.outcome == "partial"
    assert close.attempts_written == 2, "both batches that arrived are credited"
    assert any("never arrived" in warning for warning in close.warnings)
    assert any("2" in warning for warning in close.warnings)
    # The gap is still visible in the data afterwards, and still reported.
    assert "session_batch_sequence" in failures(consenting)


# --- 7. A plan the learner never began ----------------------------------------------


def test_a_planned_session_can_be_abandoned_without_inventing_a_start(
    consenting: PolishWorkspace,
) -> None:
    """The lifecycle permitted it and the schema refused it, with a raw constraint error."""

    report = plan(consenting)

    abandoned = session_service.abandon(
        consenting.paths,
        session=report.session_id,
        track=consenting.track_id,
        reason="the learner did not arrive",
        clock=consenting.clock,
    )

    assert abandoned.status == "abandoned"
    assert abandoned.started_at is None, "a session that never ran has no start time"
    with open_reader(consenting.paths, clock=consenting.clock) as database:
        started_at, closed_at = database.one(
            "SELECT started_at, closed_at FROM sessions WHERE session_id = ?",
            [report.session_id],
        )
    assert started_at is None
    assert closed_at is not None
    assert failures(consenting) == []


def test_an_abandoned_session_that_did_run_keeps_its_start(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting)

    abandoned = session_service.abandon(
        consenting.paths,
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    assert abandoned.started_at is not None


# --- 8. Plan idempotency is bound to the request ------------------------------------


def test_one_key_cannot_answer_two_different_plan_requests(
    consenting: PolishWorkspace,
) -> None:
    first = plan(consenting, minutes=60, mode="mixed", idempotency_key="reused")

    with pytest.raises(LinguaWikiError) as failure:
        plan(consenting, minutes=100, mode="grammar", idempotency_key="reused")

    assert failure.value.payload.code == "idempotency_conflict"
    assert first.session_id in failure.value.payload.message
    with open_reader(consenting.paths, clock=consenting.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM sessions")) == 1


def test_a_genuine_retry_still_returns_the_session_it_planned(
    consenting: PolishWorkspace,
) -> None:
    first = plan(consenting, minutes=60, mode="mixed", idempotency_key="retried")
    again = plan(consenting, minutes=60, mode="mixed", idempotency_key="retried")

    assert again.session_id == first.session_id
    with open_reader(consenting.paths, clock=consenting.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM sessions")) == 1


def test_the_same_request_on_another_track_is_a_different_request(
    consenting: PolishWorkspace,
) -> None:
    """The track is part of the fingerprint: one key cannot span two learners."""

    plan(consenting, minutes=60, idempotency_key="shared")
    other_user = learner_service.create_user(
        consenting.paths,
        display_name="Second Learner",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        clock=consenting.clock,
    )
    other_track = learner_service.create_track(
        consenting.paths,
        user=other_user.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        clock=consenting.clock,
    ).track_id

    with pytest.raises(LinguaWikiError) as failure:
        session_service.create(
            consenting.paths,
            minutes=60,
            track=other_track,
            idempotency_key="shared",
            clock=consenting.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"


# --- 9. Block and activity attribution and lifecycle --------------------------------


def test_an_event_cannot_name_a_block_and_another_block_s_activity(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting, minutes=100)
    blocks = [block for block in report.blocks if block.role == "core"]
    foreign = blocks[1].activities[0].activity_id

    with pytest.raises(LinguaWikiError) as failure:
        flush(
            consenting,
            report,
            [{**attempt_event(blocks[0], event_id=EVENTS[1]), "activity": foreign}],
            block=blocks[0],
        )

    assert failure.value.payload.code == "activity_not_in_block"
    assert blocks[1].block_id in failure.value.payload.message


def test_a_misattributed_activity_that_survived_a_restore_is_reported(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting, minutes=100)
    blocks = [block for block in report.blocks if block.role == "core"]
    flush(
        consenting,
        report,
        [
            {
                **attempt_event(blocks[0], event_id=EVENTS[1]),
                "activity": blocks[0].activities[0].activity_id,
            }
        ],
        block=blocks[0],
    )
    assert failures(consenting) == []

    damage(
        consenting,
        "UPDATE session_staged_events SET activity_id = ? WHERE session_id = ?",
        [blocks[1].activities[0].activity_id, report.session_id],
    )

    assert "session_activity_block" in failures(consenting)


def test_logging_advances_the_block_and_its_activity_and_the_close_settles_them(
    consenting: PolishWorkspace,
) -> None:
    """`resume` reads these statuses, so a plan that never moves has nothing to resume."""

    report = started(consenting, minutes=100)
    blocks = [block for block in report.blocks if block.role == "core"]
    worked = blocks[0]
    flush(
        consenting,
        report,
        [
            {
                **attempt_event(worked, event_id=EVENTS[1]),
                "activity": worked.activities[0].activity_id,
            }
        ],
        block=worked,
    )

    during = session_service.show(
        consenting.paths,
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )
    statuses = {block.block_id: block.status for block in during.blocks}
    assert statuses[worked.block_id] == "active"
    activities = {
        activity.activity_id: activity.status
        for block in during.blocks
        for activity in block.activities
    }
    assert activities[worked.activities[0].activity_id] == "active"
    assert during.resume_from is not None
    assert during.resume_from.block_id == during.blocks[0].block_id

    session_service.close(
        consenting.paths,
        outcome="completed",
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    after = session_service.show(
        consenting.paths,
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )
    settled = {activity.status for block in after.blocks for activity in block.activities}
    assert settled <= {"completed", "skipped"}, "a finished session holds no plans"
    assert after.resume_from is None
    assert failures(consenting) == []


def test_resume_names_the_next_unfinished_block_and_activity(
    consenting: PolishWorkspace,
) -> None:
    report = started(consenting, minutes=100)
    warm_up = report.blocks[0]
    flush(
        consenting,
        report,
        [
            {
                **attempt_event(warm_up, event_id=EVENTS[1]),
                "activity": warm_up.activities[0].activity_id,
            }
        ],
        block=warm_up,
    )

    resumed = session_service.resume(
        consenting.paths,
        session=report.session_id,
        track=consenting.track_id,
        clock=consenting.clock,
    )

    assert resumed.resume_from is not None
    assert resumed.resume_from.sequence == warm_up.sequence
    assert resumed.resume_from.activity_kind is not None
