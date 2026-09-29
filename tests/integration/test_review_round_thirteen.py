"""Regressions for the thirteenth review: two adjacent bugs at the same boundary.

Both are the previous round's lesson, unlearned in two more places. A vocabulary that
existed only in the generated schema validated nothing, so a nonsense value was staged
and refused at close; and a close key was compared *after* the replay it was meant to
guard, so one key could successfully identify two different closes.
"""

from __future__ import annotations

from typing import Any

import pytest

from linguawiki.contracts import (
    StagedAttemptPayload,
    StagedCorrectionPayload,
    StagedObservationPayload,
)
from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from tests.conftest import PolishWorkspace

EVENTS = [f"evt_01ARZ3NDEKTSV4RRFFQ69G5K{index:02d}" for index in range(12)]


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    return polish_workspace


def started(workspace: PolishWorkspace, **overrides: Any) -> session_service.SessionReport:
    options: dict[str, Any] = {"minutes": 60}
    options.update(overrides)
    report = session_service.create(
        workspace.paths, track=workspace.track_id, clock=workspace.clock, **options
    )
    session_service.start(
        workspace.paths,
        session=report.session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )
    return report


def core(report: session_service.SessionReport) -> session_service.SessionBlockReport:
    return next(block for block in report.blocks if block.role == "core")


def flush(
    workspace: PolishWorkspace,
    report: session_service.SessionReport,
    payload: dict[str, Any],
    *,
    kind: str = "attempt.observed",
    event_id: str = EVENTS[0],
    sequence: int = 1,
    key: str = "thirteen-1",
) -> session_service.BatchReport:
    return session_service.log(
        workspace.paths,
        batch={
            "sequence": sequence,
            "idempotency_key": key,
            "block": core(report).block_id,
            "events": [
                {
                    "event_id": event_id,
                    "kind": kind,
                    "occurred_at": "2026-01-01T09:00:00Z",
                    "payload": payload,
                }
            ],
        },
        session=report.session_id,
        track=workspace.track_id,
        clock=workspace.clock,
    )


def attempt(block: session_service.SessionBlockReport, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_type": "short-response",
        "modality": block.modality,
        "dimension": block.dimension,
        "score": 1.0,
    }
    payload.update(overrides)
    return payload


def staged_count(workspace: PolishWorkspace) -> int:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return int(database.scalar("SELECT count(*) FROM session_staged_events"))


def status(workspace: PolishWorkspace, session_id: str) -> str:
    return session_service.show(
        workspace.paths, session=session_id, track=workspace.track_id, clock=workspace.clock
    ).status


def failures(workspace: PolishWorkspace) -> list[str]:
    report = database_service.check(workspace.paths, clock=workspace.clock)
    return [check.name for check in report.failures]


# --- 1. A published vocabulary is enforced, not merely documented -------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_type", "not-a-task"),
        ("modality", "telepathy"),
        ("help_level", "generous"),
        ("correction_mode", "telepathic"),
        ("retrieval", "eventually"),
        ("assessor_kind", "oracle"),
        ("confidence", "certain"),
        ("response_visibility", "everything"),
    ],
)
def test_a_batch_naming_an_unknown_value_is_refused_where_it_arrives(
    running: PolishWorkspace, field: str, value: str
) -> None:
    """Staged and then refused at close was the failure: the session sat in `closing`."""

    report = started(running)

    with pytest.raises(LinguaWikiError) as failure:
        flush(running, report, attempt(core(report), **{field: value}))

    assert failure.value.payload.code == "invalid_session_batch"
    assert value in failure.value.payload.message
    assert staged_count(running) == 0
    assert status(running, report.session_id) == "active", "the session is still runnable"


def test_an_unknown_evidence_claim_is_refused(running: PolishWorkspace) -> None:
    """The claim caps what an observation can ever promote, so it is worth its own case."""

    report = started(running)

    with pytest.raises(LinguaWikiError) as failure:
        flush(
            running,
            report,
            attempt(core(report), claims=["telepathic-mastery"]),
        )

    assert failure.value.payload.code == "invalid_session_batch"
    assert "telepathic-mastery" in failure.value.payload.message
    assert staged_count(running) == 0


@pytest.mark.parametrize(
    ("kind", "payload", "value"),
    [
        (
            "observation.noted",
            {"category": "vibes", "note": "Seemed fine."},
            "vibes",
        ),
        (
            "observation.noted",
            {"category": "fatigue", "note": "Tired.", "salience": "enormous"},
            "enormous",
        ),
        (
            "correction.given",
            {
                "category": "case-government",
                "signature": "szukam bilet",
                "description": "Wrong case.",
                "confidence": "absolute",
            },
            "absolute",
        ),
    ],
)
def test_every_staged_kind_enforces_its_own_vocabularies(
    running: PolishWorkspace, kind: str, payload: dict[str, Any], value: str
) -> None:
    report = started(running)

    with pytest.raises(LinguaWikiError) as failure:
        flush(running, report, payload, kind=kind)

    assert failure.value.payload.code == "invalid_session_batch"
    assert value in failure.value.payload.message


def test_a_package_naming_an_unknown_value_is_refused_at_ingestion(
    running: PolishWorkspace,
) -> None:
    """A package's details are free-form, so this is the path with no other guard."""

    report = started(running)
    package = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_thirteen",
        "external_session_id": "call-thirteen",
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
        "events": [
            {
                "event_id": EVENTS[1],
                "kind": "attempt.observed",
                "occurred_at": "2026-01-01T10:06:00Z",
                "payload": {
                    "utterance_id": "utt_1",
                    "details": {
                        "task_type": "not-a-task",
                        "modality": "speech",
                        "dimension": "spoken-production",
                        "score": 1.0,
                    },
                },
            }
        ],
    }

    with pytest.raises(LinguaWikiError) as failure:
        session_service.ingest_package(
            running.paths,
            package=package,
            session=report.session_id,
            track=running.track_id,
            clock=running.clock,
        )

    assert failure.value.payload.code == "invalid_staged_payload"
    assert "not-a-task" in failure.value.payload.message
    assert staged_count(running) == 0
    assert status(running, report.session_id) == "active"


def test_valid_vocabulary_still_stages_and_closes(running: PolishWorkspace) -> None:
    """The refusals above must not be a refusal of every batch."""

    report = started(running)
    block = core(report)

    flush(
        running,
        report,
        attempt(
            block,
            help_level="hinted",
            retrieval="immediate",
            assessor_kind="ai",
            confidence="low",
        ),
    )
    close = session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        clock=running.clock,
    )

    assert close.attempts_written == 1
    assert failures(running) == []


@pytest.mark.parametrize(
    "model",
    [StagedAttemptPayload, StagedCorrectionPayload, StagedObservationPayload],
)
def test_every_published_enum_is_enforced_by_the_model_itself(model: type[Any]) -> None:
    """Every declared vocabulary refuses an unknown value *and says what is allowed*.

    The declaration is read off the annotation rather than repeated here, so a field
    added later is covered without anyone remembering to cover it. The companion test in
    `tests/contracts/test_json_schemas.py` checks the other half -- that the same
    declaration reaches the published schema, and that the two agree about `null`.
    """

    from linguawiki.contracts import field_vocabulary

    published = {
        name: values
        for name, field in model.model_fields.items()
        if (values := field_vocabulary(field)) is not None
    }
    assert published, f"{model.__name__} publishes no vocabulary to enforce"

    valid: dict[str, Any] = {
        "task_type": "objective",
        "modality": "text",
        "score": 1.0,
        "dimension": "reading",
        "category": "fatigue",
        "note": "Tired.",
        "signature": "szukam bilet",
        "description": "Wrong case.",
    }
    base = {key: value for key, value in valid.items() if key in model.model_fields}
    model.model_validate(base)

    for name, values in published.items():
        offered = ["not-a-known-value"] if name != "claims" else [["not-a-known-value"]]
        with pytest.raises(Exception) as failure:
            model.model_validate({**base, name: offered[0]})
        assert "not-a-known-value" in str(failure.value)
        assert str(next(iter(values))) in str(failure.value), (
            "the refusal lists what is allowed, because a producer using the wrong word "
            "needs to know which word"
        )


# --- 2. One close key names one close, in both directions ---------------------------


def test_a_foreign_key_cannot_replay_an_already_closed_session(
    running: PolishWorkspace,
) -> None:
    """The ownership check sat *after* the replay it was meant to guard.

    Closing A under key-A and B under key-B, then retrying B under key-A, returned B's
    result and reported it as a replay -- so one key successfully identified two closes.
    """

    first = started(running, idempotency_key="plan-one")
    second = started(running, idempotency_key="plan-two", minutes=40)
    close_a = session_service.close(
        running.paths,
        outcome="completed",
        session=first.session_id,
        track=running.track_id,
        idempotency_key="key-A",
        clock=running.clock,
    )
    close_b = session_service.close(
        running.paths,
        outcome="completed",
        session=second.session_id,
        track=running.track_id,
        idempotency_key="key-B",
        clock=running.clock,
    )
    assert close_a.finalization_id != close_b.finalization_id

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            running.paths,
            outcome="completed",
            session=second.session_id,
            track=running.track_id,
            idempotency_key="key-A",
            clock=running.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"
    assert first.session_id in failure.value.payload.message


def test_a_key_that_closed_nothing_does_not_borrow_another_close_s_result(
    running: PolishWorkspace,
) -> None:
    """The other direction: a caller retrying a call they never made.

    Returning this session's result would confirm a belief that is wrong -- that their
    close, under their key, is the one that landed.
    """

    report = started(running)
    session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        idempotency_key="the-key-that-closed-it",
        clock=running.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            running.paths,
            outcome="completed",
            session=report.session_id,
            track=running.track_id,
            idempotency_key="a-key-that-closed-nothing",
            clock=running.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"
    assert "session show" in failure.value.payload.message


def test_the_right_key_still_replays_the_close_it_made(running: PolishWorkspace) -> None:
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


def test_a_close_with_no_key_still_replays(running: PolishWorkspace) -> None:
    """A caller who never supplied a key has nothing to compare, and still gets a replay."""

    report = started(running)
    first = session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        clock=running.clock,
    )

    replay = session_service.close(
        running.paths,
        outcome="completed",
        session=report.session_id,
        track=running.track_id,
        clock=running.clock,
    )

    assert replay.replayed
    assert replay.finalization_id == first.finalization_id
