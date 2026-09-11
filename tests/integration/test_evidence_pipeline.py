"""The whole path from an attempt to a stage, an estimate, and an error decision.

These are the stage's exit-gate scenarios end to end, against the Polish pilot pack and
a real database: recognition cannot buy production, a delayed failure regresses a stage
and reactivates an error, every estimate is reproducible and traceable, and an untested
dimension stays untested.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import errors as error_service
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service
from tests.conftest import PolishWorkspace

ITEM = "pl.lex.dworzec"
OTHER = "pl.lex.bilet"


def stage(workspace: PolishWorkspace, item: str = ITEM) -> str | None:
    report = knowledge_service.get(
        workspace.paths, item=item, track=workspace.track_id, clock=workspace.clock
    )
    return None if report.state is None else report.state.stage


def recognition(workspace: PolishWorkspace, *, context: str, item: str = ITEM) -> object:
    return evidence_service.record(
        workspace.paths,
        task_type="objective",
        modality="text",
        score=1.0,
        target=item,
        dimension="reading",
        context=context,
        track=workspace.track_id,
        clock=workspace.clock,
    )


def test_a_recognition_attempt_records_evidence_and_moves_the_item_off_unseen(
    polish_workspace: PolishWorkspace,
) -> None:
    report = recognition(polish_workspace, context="objective:one")

    assert report.outcome == "success"
    assert [entry.claim for entry in report.evidence] == ["recognition"]
    assert report.stage_before is None
    assert report.stage_after == "encountered"
    assert report.evidence[0].novelty == "novel"
    assert any("weakest one" in warning for warning in report.warnings)


def test_recognition_can_never_promote_spontaneous_production(
    polish_workspace: PolishWorkspace,
) -> None:
    """The exit gate, through the real recording path."""

    for index in range(12):
        recognition(polish_workspace, context=f"objective:{index}")

    item = knowledge_service.get(
        polish_workspace.paths,
        item=ITEM,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert item.state is not None
    assert item.state.stage == "recognized"
    assert item.state.evidence_ceiling == "recognized"
    assert item.state.gated_stage == "recognized"
    assert any("caps the stage" in line for line in item.state.explanation)


def test_a_claim_the_attempt_cannot_support_is_refused_with_the_alternative_named(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=ITEM,
            claims=["spontaneous-production"],
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "evidence_modality_incompatible"


def test_a_delay_that_did_not_happen_is_refused_and_names_both_remedies(
    polish_workspace: PolishWorkspace,
) -> None:
    """Without this, every attempt could be labelled delayed and reach `stable` at once."""

    recognition(polish_workspace, context="objective:first")

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            target=ITEM,
            claims=["delayed-transfer"],
            retrieval="delayed",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "retrieval_not_delayed"
    assert "--delay-hours" in failure.value.payload.message
    assert "same-session" in failure.value.payload.message


def test_a_declared_delay_shorter_than_the_minimum_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            target=ITEM,
            claims=["delayed-transfer"],
            retrieval="delayed",
            delay_hours=2.0,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "retrieval_not_delayed"


def test_a_real_delay_reaches_stable_and_a_delayed_failure_takes_it_back(
    polish_workspace: PolishWorkspace,
) -> None:
    """The exit gate's other half, end to end."""

    for context in ("reading:travel", "listening:travel"):
        evidence_service.record(
            polish_workspace.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            target=ITEM,
            dimension="writing",
            claims=["delayed-transfer"],
            retrieval="delayed",
            delay_hours=48.0,
            context=context,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert stage(polish_workspace) == "stable"

    regressed = evidence_service.record(
        polish_workspace.paths,
        task_type="short-response",
        modality="writing",
        score=0.0,
        target=ITEM,
        dimension="writing",
        claims=["delayed-transfer"],
        retrieval="delayed",
        delay_hours=72.0,
        context="writing:work",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert regressed.outcome == "failure"
    assert regressed.stage_before == "stable"
    assert regressed.stage_after == "encountered"


def test_a_delayed_failure_reactivates_a_monitored_error(
    polish_workspace: PolishWorkspace,
) -> None:
    """A failure on the target is a reason to watch again, and says it is the weaker signal."""

    error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=OTHER,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    for context in ("drill:one", "drill:two"):
        evidence_service.record(
            polish_workspace.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            target=OTHER,
            dimension="writing",
            claims=["controlled-production"],
            context=context,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    monitored = error_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert [entry.status for entry in monitored.entries] == ["monitoring"]

    failure = evidence_service.record(
        polish_workspace.paths,
        task_type="short-response",
        modality="writing",
        score=0.0,
        target=OTHER,
        dimension="writing",
        claims=["controlled-production"],
        context="drill:three",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert failure.errors_reactivated
    reopened = error_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert [entry.status for entry in reopened.entries] == ["reactivated"]
    assert reopened.entries[0].status_reason is not None
    assert "stronger signal" in reopened.entries[0].status_reason


def test_one_correct_answer_cannot_resolve_an_error(
    polish_workspace: PolishWorkspace,
) -> None:
    error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=OTHER,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    evidence_service.record(
        polish_workspace.paths,
        task_type="extended-productive",
        modality="writing",
        score=1.0,
        target=OTHER,
        dimension="writing",
        claims=["spontaneous-production"],
        context="essay:one",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    listed = error_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )

    assert listed.entries[0].status != "resolved"
    assert listed.entries[0].outstanding


def test_a_full_set_of_counter_evidence_resolves_the_error(
    polish_workspace: PolishWorkspace,
) -> None:
    error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=OTHER,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    for index, (context, retrieval, delay) in enumerate(
        (
            ("essay:one", "immediate", None),
            ("conversation:two", "delayed", 48.0),
            ("essay:three", "immediate", None),
        )
    ):
        evidence_service.record(
            polish_workspace.paths,
            task_type="extended-productive",
            modality="writing",
            score=1.0,
            target=OTHER,
            dimension="writing",
            claims=["spontaneous-production"] if index != 1 else ["delayed-transfer"],
            retrieval=retrieval,
            delay_hours=delay,
            context=context,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    listed = error_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )

    assert listed.entries[0].status == "resolved"
    assert listed.entries[0].outstanding == ()
    assert listed.live == 0


def test_an_uncertain_signature_is_refused_and_both_remedies_work(
    polish_workspace: PolishWorkspace,
) -> None:
    first = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam biletu",
        description="Genitive after szukać.",
        target=OTHER,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        error_service.record(
            polish_workspace.paths,
            category="case-government",
            signature="szukam biletow",
            description="A near miss.",
            target=OTHER,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "error_match_uncertain"
    assert "--attach-to" in failure.value.payload.message
    assert first.error_id in {
        str(detail.context.get("error_id")) for detail in failure.value.payload.details
    }

    attached = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam biletow",
        description="A near miss, filed against the existing pattern.",
        target=OTHER,
        attach_to=first.error_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    distinct = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam biletach",
        description="A different error after all.",
        target=OTHER,
        distinct=True,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert attached.error_id == first.error_id
    assert attached.occurrence_count == 2
    assert distinct.error_id != first.error_id
    assert distinct.created is True


def test_recompute_is_idempotent_and_a_dry_run_writes_nothing(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(3):
        recognition(polish_workspace, context=f"objective:{index}")

    first = evidence_service.recompute(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    second = evidence_service.recompute(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    dry = evidence_service.recompute(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        dry_run=True,
        clock=polish_workspace.clock,
    )

    assert first.items_changed == 0, "recording already settled the stage"
    assert second.items_changed == 0
    assert dry.dry_run is True
    assert dry.items_changed == 0
    assert "dry run: nothing was written" in dry.warnings


def test_a_policy_change_is_replayable_over_evidence_that_never_moved(
    polish_workspace: PolishWorkspace,
) -> None:
    """The point of retaining raw evidence: a stage recomputes, it is not migrated."""

    for index in range(2):
        recognition(polish_workspace, context=f"objective:{index}")
    assert stage(polish_workspace) == "recognized"

    # Age the clock past several half-lives. The evidence is untouched; only the
    # decayed weight behind the gate changes, and the stage follows.
    polish_workspace.clock.advance(timedelta(days=400))
    replayed = evidence_service.recompute(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )

    assert replayed.items_changed == 1
    assert replayed.changes[0].stage_before == "recognized"
    assert replayed.changes[0].stage_after == "encountered"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM evidence")) == 2


def test_every_estimate_is_traceable_to_evidence_and_an_algorithm_version(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(2):
        recognition(polish_workspace, context=f"objective:{index}")

    history = estimate_service.history(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        dimension="reading",
        clock=polish_workspace.clock,
    )

    assert history.total >= 1
    newest = history.snapshots[0]
    assert newest.evidence_ids, "a snapshot must name the observations behind it"
    assert newest.calculation_version.startswith("estimate.")
    assert newest.factors
    assert any(factor.name == "evidence" for factor in newest.factors)
    assert newest.previous_snapshot_id is not None or history.total == 1


def test_an_untested_dimension_stays_untested_rather_than_scoring_zero(
    polish_workspace: PolishWorkspace,
) -> None:
    recognition(polish_workspace, context="objective:one")

    report = estimate_service.report(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )

    tested = {entry.dimension for entry in report.estimates if entry.evidence_count}
    assert "reading" in tested
    assert "pronunciation" in report.not_tested
    assert all(
        entry.level_code is None
        for entry in report.estimates
        if entry.estimate_status == "not-tested"
    )
    assert any("untested, not weak" in warning for warning in report.warnings)


def test_a_global_level_is_only_offered_on_request_and_labelled_a_summary(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(2):
        recognition(polish_workspace, context=f"objective:{index}")

    plain = estimate_service.report(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    summarised = estimate_service.report(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        summary=True,
        clock=polish_workspace.clock,
    )

    assert plain.summary_level is None
    assert summarised.summary_level is not None
    assert summarised.summary_label is not None
    assert "not a level the learner holds" in summarised.summary_label


def test_a_response_is_kept_as_an_excerpt_and_never_beyond_consent(
    polish_workspace: PolishWorkspace,
) -> None:
    consented = evidence_service.record(
        polish_workspace.paths,
        task_type="short-response",
        modality="writing",
        score=1.0,
        target=ITEM,
        dimension="writing",
        claims=["controlled-production"],
        response="Jestem na dworcu i czekam na pociąg.",
        response_visibility="full",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert consented.response_visibility == "full"
    assert consented.response_excerpt is not None

    learner_service.update_track(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=polish_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            target=ITEM,
            dimension="writing",
            claims=["controlled-production"],
            response="Jestem na dworcu.",
            response_visibility="full",
            context="written:refused",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "transcript_consent_required"

    withheld = evidence_service.record(
        polish_workspace.paths,
        task_type="short-response",
        modality="writing",
        score=1.0,
        target=ITEM,
        dimension="writing",
        claims=["controlled-production"],
        response="Jestem na dworcu.",
        context="written:withheld",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert withheld.response_visibility == "withheld"
    assert withheld.response_excerpt is None


def test_an_observation_is_context_and_can_never_promote_an_item(
    polish_workspace: PolishWorkspace,
) -> None:
    attempt = recognition(polish_workspace, context="objective:one")
    observation = evidence_service.observations(
        polish_workspace.paths,
        category="fatigue",
        note="Tired after work; short answers.",
        salience="high",
        attempt=attempt.attempt_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert observation.category == "fatigue"
    assert stage(polish_workspace) == "encountered", "an observation is not evidence"


def test_the_learner_model_leaves_the_database_consistent(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(3):
        recognition(polish_workspace, context=f"objective:{index}")
    error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=OTHER,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    error_service.add_followup(
        polish_workspace.paths,
        kind="practice",
        action="Drill the genitive after szukać.",
        target=OTHER,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)

    assert [check.name for check in report.failures] == []
    named = {check.name for check in report.checks}
    assert {
        "evidence_claim_compatibility",
        "evidence_attempt_agreement",
        "mastery_evidence_ceiling",
        "mastery_aggregation_version",
        "error_identity",
        "estimate_status",
    } <= named


def test_uncertainty_narrows_only_when_independent_observations_agree(
    polish_workspace: PolishWorkspace,
) -> None:
    """The property the plan names: diversity, not volume, is what raises confidence."""

    for _ in range(3):
        evidence_service.record(
            polish_workspace.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=ITEM,
            dimension="reading",
            context="objective:one-and-only",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    repeated = estimate_service.report(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    reading = next(entry for entry in repeated.estimates if entry.dimension == "reading")

    # One observation, not three: repeating a context corroborates without sharpening,
    # so `evidence_count` is what the estimate actually rests on.
    assert reading.evidence_count == 1
    assert reading.estimate_status == "provisional", "one context is a reading, not a measurement"
    assert reading.confidence_label == "low"

    evidence_service.record(
        polish_workspace.paths,
        task_type="reading-comprehension",
        modality="text",
        score=1.0,
        target=OTHER,
        dimension="reading",
        context="reading:independent",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    varied = estimate_service.report(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    widened = next(entry for entry in varied.estimates if entry.dimension == "reading")

    assert widened.estimate_status == "estimated"
    assert widened.uncertainty is not None
    assert reading.uncertainty is not None
    assert widened.uncertainty < reading.uncertainty


def test_a_run_s_own_task_is_not_counted_twice_when_recorded_as_evidence(
    polish_workspace: PolishWorkspace,
) -> None:
    """The run's posterior already holds it; folding it in again would double-count."""

    from linguawiki.services import assessment as assessment_service

    run = assessment_service.start(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        run_type="pilot-calibration",
        clock=polish_workspace.clock,
    )
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )
    assessment_service.finalize(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )

    recorded = evidence_service.record(
        polish_workspace.paths,
        task_type=served.task_type,
        modality=served.modality,
        score=1.0,
        dimension=served.dimension,
        origin="assessment",
        assessment_run=run.run_id,
        task=served.content_id,
        # No `target`: the pilot bank's tasks declare none, so an item-targeted
        # observation from one would be a claim the task never made. What the run
        # produced is a dimension observation, which is what must not be counted twice.
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    recomputed = evidence_service.recompute(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )

    assert recorded.attempt_id
    assert recomputed.estimates is not None
    changes = {change.dimension: change for change in recomputed.estimates.changes}
    settled = changes.get(served.dimension)
    if settled is not None:
        assert recorded.evidence[0].evidence_id in settled.excluded_evidence
        assert recorded.evidence[0].evidence_id not in settled.evidence_ids
    history = estimate_service.history(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        dimension=served.dimension,
        clock=polish_workspace.clock,
    )
    for snapshot in history.snapshots:
        assert recorded.evidence[0].evidence_id not in snapshot.evidence_ids


def test_the_evidence_listing_shows_the_observations_behind_an_item(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(3):
        recognition(polish_workspace, context=f"objective:{index}")
    evidence_service.record(
        polish_workspace.paths,
        task_type="short-response",
        modality="writing",
        score=1.0,
        target=OTHER,
        dimension="writing",
        claims=["controlled-production"],
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    everything = evidence_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    by_item = evidence_service.listing(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        item=ITEM,
        clock=polish_workspace.clock,
    )
    by_dimension = evidence_service.listing(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        dimension="writing",
        clock=polish_workspace.clock,
    )

    assert everything.total == 4
    assert by_item.total == 3
    assert {entry.target_content_id for entry in by_item.entries} == {by_item.target_content_id}
    assert [entry.claim for entry in by_dimension.entries] == ["controlled-production"]
    # Newest first, so a caller reading the head of the list sees the latest observation.
    stamps = [entry.occurred_at for entry in everything.entries]
    assert stamps == sorted(stamps, reverse=True)


def test_a_truncated_evidence_listing_says_how_much_it_left_out(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(4):
        recognition(polish_workspace, context=f"objective:{index}")

    listed = evidence_service.listing(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        limit=2,
        clock=polish_workspace.clock,
    )

    assert len(listed.entries) == 2
    assert listed.total == 4
    assert listed.warnings


@pytest.mark.parametrize("limit", [0, 501])
def test_an_evidence_listing_limit_outside_the_range_is_refused(
    limit: int, polish_workspace: PolishWorkspace
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.listing(
            polish_workspace.paths,
            track=polish_workspace.track_id,
            limit=limit,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "invalid_limit"


def test_a_blank_context_key_is_refused_rather_than_stored(
    polish_workspace: PolishWorkspace,
) -> None:
    """A blank context would make every observation its own context, defeating diversity."""

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=ITEM,
            dimension="reading",
            context="   ",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "invalid_context_key"


def test_a_first_encounter_cannot_be_delayed_retrieval(
    polish_workspace: PolishWorkspace,
) -> None:
    """There is nothing for it to be delayed from, and the refusal says both remedies."""

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            target=ITEM,
            dimension="writing",
            claims=["delayed-transfer"],
            retrieval="delayed",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "retrieval_not_delayed"
    assert "first attempt" in failure.value.payload.message
    assert "--delay-hours" in failure.value.payload.message


def test_a_real_gap_in_the_item_s_own_history_is_accepted_as_a_delay(
    polish_workspace: PolishWorkspace,
) -> None:
    """The gap need not be declared if the item's history already shows it."""

    recognition(polish_workspace, context="objective:first")
    polish_workspace.clock.advance(timedelta(days=3))

    delayed = evidence_service.record(
        polish_workspace.paths,
        task_type="short-response",
        modality="writing",
        score=1.0,
        target=ITEM,
        dimension="writing",
        claims=["delayed-transfer"],
        retrieval="delayed",
        context="written:later",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert delayed.retrieval == "delayed"
    assert delayed.evidence[0].claim == "delayed-transfer"


def test_a_second_observation_in_a_known_context_is_a_repeat_not_a_novelty(
    polish_workspace: PolishWorkspace,
) -> None:
    """Novelty is read from the item's history, so a caller cannot declare it."""

    first = recognition(polish_workspace, context="objective:same")
    second = recognition(polish_workspace, context="objective:same")

    assert first.evidence[0].novelty == "novel"
    assert second.evidence[0].novelty == "repeat"


def test_an_unknown_assessment_task_is_refused(polish_workspace: PolishWorkspace) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=ITEM,
            dimension="reading",
            task="cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_task_not_found"


def test_an_attempt_attributed_to_a_run_must_name_the_task_it_answered(
    polish_workspace: PolishWorkspace,
) -> None:
    """Without the task there is no difficulty, so the run could not have produced it."""

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=ITEM,
            dimension="reading",
            origin="assessment",
            assessment_run="asm_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_task_required"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"score": 1.5}, "invalid_score"),
        ({"target": None, "dimension": None}, "evidence_target_required"),
        ({"claims": ["fluency"]}, "unknown_evidence_claim"),
        ({"origin": "session"}, "unknown_attempt_origin"),
    ],
)
def test_an_invalid_observation_is_refused_before_anything_is_written(
    arguments: dict[str, object], code: str, polish_workspace: PolishWorkspace
) -> None:
    """A live-session origin is refused here: this stage has no session to reference."""

    call: dict[str, object] = {
        "task_type": "objective",
        "modality": "text",
        "score": 1.0,
        "target": ITEM,
        "dimension": "reading",
    }
    call.update(arguments)

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
            **call,  # type: ignore[arg-type]
        )

    assert failure.value.payload.code == code
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM attempts")) == 0


def test_an_observation_about_an_unknown_attempt_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.observations(
            polish_workspace.paths,
            category="fatigue",
            note="Tired.",
            attempt="att_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "attempt_not_found"


def test_an_observation_needs_a_note_and_a_known_category(
    polish_workspace: PolishWorkspace,
) -> None:
    for arguments, code in (
        ({"note": "   "}, "invalid_observation"),
        ({"category": "vibes"}, "unknown_observation_category"),
        ({"salience": "enormous"}, "unknown_salience"),
    ):
        call: dict[str, object] = {"category": "note", "note": "A note."}
        call.update(arguments)
        with pytest.raises(LinguaWikiError) as failure:
            evidence_service.observations(
                polish_workspace.paths,
                track=polish_workspace.track_id,
                clock=polish_workspace.clock,
                **call,  # type: ignore[arg-type]
            )
        assert failure.value.payload.code == code


def test_a_response_asked_for_without_a_response_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    """`--response-visibility excerpt` with nothing to retain is a caller mistake."""

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            polish_workspace.paths,
            task_type="short-response",
            modality="writing",
            score=1.0,
            target=ITEM,
            dimension="writing",
            claims=["controlled-production"],
            response_visibility="excerpt",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "response_required"


def test_a_long_response_is_kept_only_as_a_bounded_excerpt(
    polish_workspace: PolishWorkspace,
) -> None:
    """An excerpt justifies the evidence; it is deliberately not a transcript."""

    recorded = evidence_service.record(
        polish_workspace.paths,
        task_type="extended-productive",
        modality="writing",
        score=1.0,
        target=ITEM,
        dimension="writing",
        claims=["spontaneous-production"],
        response="Na dworcu " * 200,
        response_visibility="excerpt",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert recorded.response_excerpt is not None
    assert len(recorded.response_excerpt) == evidence_service.EXCERPT_LIMIT


def test_recomputing_one_item_leaves_the_others_alone(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(2):
        recognition(polish_workspace, context=f"objective:{index}")
    recognition(polish_workspace, context="objective:other", item=OTHER)

    scoped = evidence_service.recompute(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        item=OTHER,
        dimensions=False,
        clock=polish_workspace.clock,
    )

    assert scoped.items_considered == 1
    assert scoped.estimates is None
