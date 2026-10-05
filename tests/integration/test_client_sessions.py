"""C7: session management that a browser can drive.

The services first, because every guarantee the page relies on is a service guarantee: a
retry is a replay, a close credits what was confirmed, recovery replays rather than
reporting nothing, discovery is per track. The HTTP layer follows and adds only what a
transport can break -- which body field is the key, and which track a session-named route
resolves through.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from tests.conftest import PolishWorkspace

EVENT_IDS = [f"evt_01ARZ3NDEKTSV4RRFFQ69G5F{suffix}" for suffix in ("B0", "B1", "B2", "B3", "B4")]


@pytest.fixture
def onboarded(polish_workspace: PolishWorkspace) -> PolishWorkspace:
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


def core_blocks(report: session_service.SessionReport) -> list[session_service.SessionBlockReport]:
    return [block for block in report.blocks if block.role == "core"]


def attempt_event(
    block: session_service.SessionBlockReport,
    *,
    event_id: str = EVENT_IDS[0],
    occurred_at: str = "2026-01-01T09:00:00Z",
    **payload: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_type": "short-response",
        "modality": block.modality,
        "dimension": block.dimension,
        "score": 1.0,
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


def running(workspace: PolishWorkspace, key: str = "plan-1") -> session_service.SessionReport:
    report = plan(workspace, idempotency_key=key)
    session_service.start(workspace.paths, session=report.session_id, clock=workspace.clock)
    return report


def log(workspace: PolishWorkspace, session: str, body: dict[str, Any]) -> Any:
    return session_service.log(workspace.paths, batch=body, session=session, clock=workspace.clock)


def count(workspace: PolishWorkspace, sql: str) -> int:
    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        return int(database.scalar(sql))


def failures(workspace: PolishWorkspace) -> list[str]:
    report = database_service.check(workspace.paths, clock=workspace.clock)
    return [check.name for check in report.failures]


def abandoned_with(workspace: PolishWorkspace, events: int, key: str = "plan-src") -> str:
    """A session abandoned while holding `events` staged attempts on one core block."""

    report = running(workspace, key)
    block = core_blocks(report)[0]
    log(
        workspace,
        report.session_id,
        batch(
            block,
            key=f"{key}-batch",
            events=[
                attempt_event(
                    block,
                    # Its own IDs: a recovered event keeps its identity, so it must not
                    # collide with one the target session was given directly.
                    event_id=EVENT_IDS[index + 2],
                    occurred_at=f"2026-01-01T09:0{index}:00Z",
                )
                for index in range(events)
            ],
        ),
    )
    session_service.abandon(workspace.paths, session=report.session_id, clock=workspace.clock)
    return report.session_id


def second_track(workspace: PolishWorkspace) -> str:
    user = learner_service.create_user(
        workspace.paths,
        display_name="Druga Osoba",
        timezone="Europe/Warsaw",
        native_languages=["en"],
        clock=workspace.clock,
    )
    track = learner_service.create_track(
        workspace.paths,
        user=user.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A1",
        clock=workspace.clock,
    )
    return track.track_id


# --- The staged listing --------------------------------------------------------------


def test_a_status_filtered_listing_counts_only_the_rows_it_could_return(
    onboarded: PolishWorkspace,
) -> None:
    source = abandoned_with(onboarded, 3)
    target = running(onboarded, "plan-target")
    first_id = (
        session_service.staged_listing(
            onboarded.paths, session=source, status="staged", clock=onboarded.clock
        )
        .events[0]
        .staged_event_id
    )
    session_service.recover(
        onboarded.paths,
        source=source,
        target=target.session_id,
        events=[first_id],
        clock=onboarded.clock,
    )

    remaining = session_service.staged_listing(
        onboarded.paths, session=source, status="staged", limit=1, clock=onboarded.clock
    )
    everything = session_service.staged_listing(
        onboarded.paths, session=source, clock=onboarded.clock
    )
    second_page = session_service.staged_listing(
        onboarded.paths, session=source, status="staged", limit=1, offset=1, clock=onboarded.clock
    )

    assert remaining.total == 2
    assert len(remaining.events) == 1
    assert {event.status for event in remaining.events} == {"staged"}
    assert everything.total == 3
    assert second_page.events[0].staged_event_id != remaining.events[0].staged_event_id
    rest = [remaining.events[0].staged_event_id, second_page.events[0].staged_event_id]
    recovered = session_service.recover(
        onboarded.paths,
        source=source,
        target=target.session_id,
        events=rest,
        idempotency_key="recover-rest",
        clock=onboarded.clock,
    )
    assert recovered.recovered == 2


def test_an_unknown_status_filter_is_refused_by_name(onboarded: PolishWorkspace) -> None:
    source = abandoned_with(onboarded, 1)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.staged_listing(
            onboarded.paths, session=source, status="abandoned", clock=onboarded.clock
        )

    assert failure.value.payload.code == "unknown_staged_status"


# --- Discovery and tracks ------------------------------------------------------------


def test_discovery_separates_open_from_recoverable_and_skips_the_empty(
    onboarded: PolishWorkspace,
) -> None:
    source = abandoned_with(onboarded, 1)
    empty = plan(onboarded, idempotency_key="plan-empty")
    session_service.abandon(onboarded.paths, session=empty.session_id, clock=onboarded.clock)
    open_one = running(onboarded, "plan-open")

    listing = session_service.discover(
        onboarded.paths, track=onboarded.track_id, clock=onboarded.clock
    )
    by_id = {entry.session_id: entry for entry in listing.sessions}

    assert by_id[source].recoverable and not by_id[source].open
    assert by_id[open_one.session_id].open and not by_id[open_one.session_id].recoverable
    assert empty.session_id not in by_id
    only_open = session_service.discover(
        onboarded.paths, track=onboarded.track_id, states=("open",), clock=onboarded.clock
    )
    assert [entry.session_id for entry in only_open.sessions] == [open_one.session_id]


def test_discovery_without_a_track_on_two_active_tracks_names_both(
    onboarded: PolishWorkspace,
) -> None:
    other = second_track(onboarded)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.discover(onboarded.paths, clock=onboarded.clock)

    assert failure.value.payload.code == "track_selection_required"
    named = {detail.context.get("track_id") for detail in failure.value.payload.details}
    assert {onboarded.track_id, other} <= named


def test_discovery_is_scoped_to_the_track_it_names(onboarded: PolishWorkspace) -> None:
    other = second_track(onboarded)
    running(onboarded)

    assert (
        session_service.discover(onboarded.paths, track=other, clock=onboarded.clock).sessions == ()
    )


def test_the_track_listing_carries_names_and_no_preferences(onboarded: PolishWorkspace) -> None:
    other = second_track(onboarded)

    listing = learner_service.discover_tracks(onboarded.paths, clock=onboarded.clock)
    by_id = {entry.track_id: entry for entry in listing.tracks}

    assert listing.default_track_id is None
    assert by_id[other].display_name == "Druga Osoba"
    assert by_id[onboarded.track_id].selectable
    dumped = listing.model_dump(mode="json")
    assert "preferences" not in json.dumps(dumped)
    assert "transcript_retention_consent" not in json.dumps(dumped)


def test_a_single_track_is_the_default(onboarded: PolishWorkspace) -> None:
    listing = learner_service.discover_tracks(onboarded.paths, clock=onboarded.clock)

    assert listing.default_track_id == onboarded.track_id


def test_a_session_named_by_id_resolves_its_own_track_among_two(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded, idempotency_key="plan-1")
    other = second_track(onboarded)

    started = session_service.start(
        onboarded.paths, session=report.session_id, clock=onboarded.clock
    )
    assert started.status == "active"
    with pytest.raises(LinguaWikiError) as failure:
        session_service.start(
            onboarded.paths, session=report.session_id, track=other, clock=onboarded.clock
        )
    assert failure.value.payload.code == "session_track_mismatch"


# --- Import through log --------------------------------------------------------------


def test_a_batch_naming_another_session_is_refused(onboarded: PolishWorkspace) -> None:
    first = running(onboarded, "plan-1")
    second = plan(onboarded, idempotency_key="plan-2")
    body = batch(core_blocks(first)[0]) | {"session_id": second.session_id}

    with pytest.raises(LinguaWikiError) as failure:
        log(onboarded, first.session_id, body)

    assert failure.value.payload.code == "session_batch_wrong_session"
    assert count(onboarded, "SELECT count(*) FROM session_event_batches") == 0


def test_an_import_must_declare_who_assessed_each_attempt(onboarded: PolishWorkspace) -> None:
    report = running(onboarded)
    block = core_blocks(report)[0]
    undeclared = attempt_event(block)
    del undeclared["payload"]["assessor_kind"]

    with pytest.raises(LinguaWikiError) as failure:
        session_service.log(
            onboarded.paths,
            batch=batch(block, events=[undeclared]),
            session=report.session_id,
            require_declared_assessor=True,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_import_assessor_required"
    assert EVENT_IDS[0] in failure.value.payload.message
    assert count(onboarded, "SELECT count(*) FROM session_event_batches") == 0
    # The skill's flush keeps its default.
    assert log(onboarded, report.session_id, batch(block, events=[undeclared])).staged_events == 1


def test_provenance_a_flush_may_not_claim_is_named_when_stripped(
    onboarded: PolishWorkspace,
) -> None:
    report = running(onboarded)
    block = core_blocks(report)[0]

    flushed = log(
        onboarded,
        report.session_id,
        batch(block, events=[attempt_event(block, source="package", package_id="pkg_x")]),
    )

    assert any(
        EVENT_IDS[0] in warning and "package_id" in warning and "source" in warning
        for warning in flushed.warnings
    )


def test_a_resent_batch_replays_after_the_session_has_closed(onboarded: PolishWorkspace) -> None:
    report = running(onboarded)
    body = batch(core_blocks(report)[0])
    log(onboarded, report.session_id, body)
    session_service.close(onboarded.paths, session=report.session_id, clock=onboarded.clock)

    assert log(onboarded, report.session_id, body).duplicate


# --- Close ---------------------------------------------------------------------------


def staged_close(workspace: PolishWorkspace) -> tuple[session_service.SessionReport, str]:
    report = running(workspace)
    blocks = core_blocks(report)
    log(workspace, report.session_id, batch(blocks[0]))
    digest = session_service.screen(
        workspace.paths, session=report.session_id, clock=workspace.clock
    )
    return report, digest.staging.digest


def test_a_keyed_close_replays_only_the_request_it_recorded(onboarded: PolishWorkspace) -> None:
    report, digest = staged_close(onboarded)
    arguments: dict[str, Any] = {
        "session": report.session_id,
        "actual_minutes": 40,
        "fatigue": "low",
        "summary": "Rozmowa o pracy",
        "expected_staging": digest,
        "idempotency_key": "close-1",
        "clock": onboarded.clock,
    }
    first = session_service.close(onboarded.paths, **arguments)
    again = session_service.close(onboarded.paths, **arguments)

    assert not first.replayed and again.replayed
    assert again.finalization_id == first.finalization_id
    for change in ({"actual_minutes": 41}, {"fatigue": "high"}, {"summary": "Inna"}):
        with pytest.raises(LinguaWikiError) as failure:
            session_service.close(onboarded.paths, **(arguments | change))
        assert failure.value.payload.code == "idempotency_conflict"
    with open_writer(onboarded.paths, command="test", clock=onboarded.clock) as database:
        payload = database.scalar(
            "SELECT payload_json FROM domain_events WHERE idempotency_key = 'close-1'"
        )
    assert "Rozmowa" not in str(payload)
    assert count(onboarded, "SELECT count(*) FROM attempts") == 1


def test_a_keyed_close_with_other_discarded_blocks_conflicts(onboarded: PolishWorkspace) -> None:
    report, digest = staged_close(onboarded)
    session_service.close(
        onboarded.paths,
        session=report.session_id,
        outcome="partial",
        expected_staging=digest,
        idempotency_key="close-1",
        clock=onboarded.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths,
            session=report.session_id,
            outcome="partial",
            discard_blocks=[core_blocks(report)[0].block_id],
            expected_staging=digest,
            idempotency_key="close-1",
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"


def test_a_keyless_close_retry_still_compares_only_the_outcome(onboarded: PolishWorkspace) -> None:
    report, _ = staged_close(onboarded)
    session_service.close(onboarded.paths, session=report.session_id, clock=onboarded.clock)

    again = session_service.close(
        onboarded.paths, session=report.session_id, fatigue="high", clock=onboarded.clock
    )

    assert again.replayed


def legacy(workspace: PolishWorkspace, key: str, *, payload: Any, versions: bool) -> None:
    """Rewrite a C7 finalization to the shape a pre-C7 close left behind, or a damaged one."""

    with open_writer(workspace.paths, command="test", clock=workspace.clock) as database:
        raw_versions, raw_result = database.one(
            "SELECT calculation_versions_json, result_json FROM session_finalizations "
            "WHERE idempotency_key = ?",
            [key],
        )
        stored_versions = json.loads(str(raw_versions))
        result = json.loads(str(raw_result))
        if versions:
            stored_versions.pop("close_request")
            result["calculation_versions"].pop("close_request")
        database.execute(
            "UPDATE session_finalizations SET calculation_versions_json = ?, result_json = ? "
            "WHERE idempotency_key = ?",
            [json.dumps(stored_versions), json.dumps(result), key],
        )
        if payload is None:
            database.execute("DELETE FROM domain_events WHERE idempotency_key = ?", [key])
        else:
            database.execute(
                "UPDATE domain_events SET payload_json = ? WHERE idempotency_key = ?",
                [payload if isinstance(payload, str) else json.dumps(payload), key],
            )


LEGACY_PAYLOAD = {
    "attempts": 1,
    "errors": 0,
    "evidence": 1,
    "outcome": "completed",
    "staged_consumed": 1,
}


def test_a_close_from_before_request_hashing_replays_on_its_outcome(
    onboarded: PolishWorkspace,
) -> None:
    report, _ = staged_close(onboarded)
    session_service.close(
        onboarded.paths, session=report.session_id, idempotency_key="old", clock=onboarded.clock
    )
    legacy(onboarded, "old", payload=LEGACY_PAYLOAD, versions=True)

    again = session_service.close(
        onboarded.paths,
        session=report.session_id,
        fatigue="high",
        idempotency_key="old",
        clock=onboarded.clock,
    )

    assert again.replayed
    assert any("predates" in warning for warning in again.warnings)
    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths,
            session=report.session_id,
            outcome="partial",
            idempotency_key="old",
            clock=onboarded.clock,
        )
    assert failure.value.payload.code == "session_already_finalized"


@pytest.mark.parametrize(
    ("payload", "versions"),
    [
        pytest.param({**LEGACY_PAYLOAD, "staged_consumed": 1}, False, id="c7-versions-no-hash"),
        pytest.param({**LEGACY_PAYLOAD, "extra": 1}, True, id="legacy-versions-extra-key"),
        # `payload_json` carries `json_valid`, so damage reads as JSON that is not an object.
        pytest.param("[1, 2]", True, id="not-an-object"),
        pytest.param(None, True, id="missing-event"),
    ],
)
def test_a_damaged_close_record_is_never_read_as_an_old_one(
    onboarded: PolishWorkspace, payload: Any, versions: bool
) -> None:
    report, _ = staged_close(onboarded)
    session_service.close(
        onboarded.paths, session=report.session_id, idempotency_key="old", clock=onboarded.clock
    )
    legacy(onboarded, "old", payload=payload, versions=versions)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths, session=report.session_id, idempotency_key="old", clock=onboarded.clock
        )

    assert failure.value.payload.code == "idempotency_conflict"


def test_a_legacy_close_still_checks_who_owns_the_key(onboarded: PolishWorkspace) -> None:
    report, _ = staged_close(onboarded)
    session_service.close(
        onboarded.paths, session=report.session_id, idempotency_key="old", clock=onboarded.clock
    )
    legacy(onboarded, "old", payload=LEGACY_PAYLOAD, versions=True)

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths,
            session=report.session_id,
            idempotency_key="other",
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"


def test_work_staged_after_the_confirmation_refuses_the_close(onboarded: PolishWorkspace) -> None:
    report, digest = staged_close(onboarded)
    blocks = core_blocks(report)
    log(
        onboarded,
        report.session_id,
        batch(
            blocks[0],
            sequence=2,
            key="batch-2",
            events=[
                attempt_event(blocks[0], event_id=EVENT_IDS[1], occurred_at="2026-01-01T09:05:00Z")
            ],
        ),
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths,
            session=report.session_id,
            expected_staging=digest,
            idempotency_key="close-1",
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_staging_changed"
    shown = session_service.show(onboarded.paths, session=report.session_id, clock=onboarded.clock)
    assert shown.status == "active"
    assert count(onboarded, "SELECT count(*) FROM attempts") == 0
    fresh = session_service.screen(
        onboarded.paths, session=report.session_id, clock=onboarded.clock
    )
    assert fresh.staging.count == 2
    closed = session_service.close(
        onboarded.paths,
        session=report.session_id,
        expected_staging=fresh.staging.digest,
        idempotency_key="close-2",
        clock=onboarded.clock,
    )
    assert closed.attempts_written == 2


def test_a_recovery_landing_after_the_confirmation_refuses_the_close(
    onboarded: PolishWorkspace,
) -> None:
    source = abandoned_with(onboarded, 1, key="plan-src")
    report, digest = staged_close(onboarded)
    session_service.recover(
        onboarded.paths, source=source, target=report.session_id, clock=onboarded.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(
            onboarded.paths,
            session=report.session_id,
            expected_staging=digest,
            clock=onboarded.clock,
        )

    assert failure.value.payload.code == "session_staging_changed"


def test_an_interrupted_close_finishes_under_its_original_digest(
    onboarded: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, digest = staged_close(onboarded)
    arguments: dict[str, Any] = {
        "session": report.session_id,
        "expected_staging": digest,
        "idempotency_key": "close-1",
        "clock": onboarded.clock,
    }
    original = session_service._materialize

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("power cut")

    monkeypatch.setattr(session_service, "_materialize", crash)
    with pytest.raises(RuntimeError):
        session_service.close(onboarded.paths, **arguments)
    monkeypatch.setattr(session_service, "_materialize", original)
    assert (
        session_service.show(
            onboarded.paths, session=report.session_id, clock=onboarded.clock
        ).status
        == "closing"
    )

    finished = session_service.close(onboarded.paths, **arguments)

    assert finished.attempts_written == 1
    # A finalized session's replay answers whatever its staging says now.
    assert session_service.close(
        onboarded.paths, **(arguments | {"expected_staging": digest})
    ).replayed


def test_the_screen_digest_is_the_set_the_close_consumes(onboarded: PolishWorkspace) -> None:
    report, digest = staged_close(onboarded)

    closed = session_service.close(
        onboarded.paths, session=report.session_id, expected_staging=digest, clock=onboarded.clock
    )

    assert closed.staged_consumed == 1
    after = session_service.screen(
        onboarded.paths, session=report.session_id, clock=onboarded.clock
    )
    assert after.staging.count == 0


# --- Abandon and recover -------------------------------------------------------------


def test_a_keyed_abandon_replays_after_a_lost_response(onboarded: PolishWorkspace) -> None:
    report = running(onboarded)

    first = session_service.abandon(
        onboarded.paths,
        session=report.session_id,
        reason="had to go",
        idempotency_key="abandon-1",
        clock=onboarded.clock,
    )
    again = session_service.abandon(
        onboarded.paths,
        session=report.session_id,
        reason="had to go",
        idempotency_key="abandon-1",
        clock=onboarded.clock,
    )

    assert first.status == again.status == "abandoned"
    assert again.replayed
    with pytest.raises(LinguaWikiError) as failure:
        session_service.abandon(
            onboarded.paths,
            session=report.session_id,
            reason="something else",
            idempotency_key="abandon-1",
            clock=onboarded.clock,
        )
    assert failure.value.payload.code == "idempotency_conflict"
    with pytest.raises(LinguaWikiError) as unrelated:
        session_service.abandon(
            onboarded.paths,
            session=report.session_id,
            idempotency_key="abandon-2",
            clock=onboarded.clock,
        )
    assert unrelated.value.payload.code == "invalid_session_transition"


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "all"])
def test_a_keyed_recovery_replays_from_its_snapshot(
    onboarded: PolishWorkspace, explicit: bool
) -> None:
    source = abandoned_with(onboarded, 2)
    target = running(onboarded, "plan-target")
    events = (
        [
            event.staged_event_id
            for event in session_service.staged_listing(
                onboarded.paths, session=source, status="staged", clock=onboarded.clock
            ).events
        ]
        if explicit
        else []
    )
    arguments: dict[str, Any] = {
        "source": source,
        "target": target.session_id,
        "events": events,
        "idempotency_key": "recover-1",
        "clock": onboarded.clock,
    }

    first = session_service.recover(onboarded.paths, **arguments)
    # The target closes before the retry arrives: the replay still answers.
    session_service.close(
        onboarded.paths, session=target.session_id, outcome="partial", clock=onboarded.clock
    )
    again = session_service.recover(onboarded.paths, **arguments)

    assert first.recovered == again.recovered == 2
    assert again.replayed and not first.replayed
    assert again.batch_id == first.batch_id
    assert again.recovered_events == first.recovered_events
    assert len(first.recovered_events) == 2
    with pytest.raises(LinguaWikiError) as failure:
        session_service.recover(onboarded.paths, **(arguments | {"events": events[:1] or ["x"]}))
    assert failure.value.payload.code == "idempotency_conflict"


def test_a_recovery_into_the_implicit_open_session_replays_after_it_is_gone(
    onboarded: PolishWorkspace,
) -> None:
    source = abandoned_with(onboarded, 1)
    target = running(onboarded, "plan-target")

    first = session_service.recover(
        onboarded.paths,
        source=source,
        track=onboarded.track_id,
        idempotency_key="recover-1",
        clock=onboarded.clock,
    )
    session_service.close(onboarded.paths, session=target.session_id, clock=onboarded.clock)
    again = session_service.recover(
        onboarded.paths,
        source=source,
        track=onboarded.track_id,
        idempotency_key="recover-1",
        clock=onboarded.clock,
    )

    assert again.replayed
    assert again.target_session_id == first.target_session_id == target.session_id
    assert failures(onboarded) == []


# --- The screen ----------------------------------------------------------------------


def test_the_screen_carries_the_plan_the_staging_and_what_may_happen_next(
    onboarded: PolishWorkspace,
) -> None:
    report = running(onboarded)
    blocks = core_blocks(report)
    log(onboarded, report.session_id, batch(blocks[0]))
    log(
        onboarded,
        report.session_id,
        batch(
            blocks[0],
            sequence=3,
            key="batch-3",
            events=[
                attempt_event(blocks[0], event_id=EVENT_IDS[1], occurred_at="2026-01-01T09:05:00Z")
            ],
        ),
    )

    screen = session_service.screen(
        onboarded.paths, session=report.session_id, clock=onboarded.clock
    )

    assert screen.session.session_id == report.session_id
    assert screen.staged.total == 2
    assert screen.staged_by_block == {blocks[0].block_id: 2}
    assert screen.unattributed == 0
    assert [entry.idempotency_key for entry in screen.batches] == ["batch-1", "batch-3"]
    assert screen.missing_batch_sequences == (2,)
    assert screen.staging.count == 2
    assert "session.close" in screen.actions and "session.import" in screen.actions
    assert "session.start" not in screen.actions
    assert not screen.closing_interrupted


def test_the_cli_words_and_the_operation_ids_come_from_one_table(
    onboarded: PolishWorkspace,
) -> None:
    report = plan(onboarded)

    screen = session_service.screen(
        onboarded.paths, session=report.session_id, clock=onboarded.clock
    )

    assert screen.actions == ("session.start", "session.abandon")
    assert screen.session.next_actions == ("session start",)
