"""Bounded calibration runs against a pack's bank, and the claims they may not make.

Two refusals are the point: comprehensive placement needs a placement-ready bank, and a
dimension nothing can serve is `not-tested` rather than failed.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.placement import ALGORITHM_VERSION, REUSE_WINDOW_MONTHS
from linguawiki.services import assessment as assessment_service
from linguawiki.services import onboarding as onboarding_service
from tests.conftest import PolishWorkspace


def _run_to_completion(workspace: PolishWorkspace, run_id: str, *, score: float = 1.0) -> int:
    served = 0
    while True:
        outcome = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
        if not isinstance(outcome, assessment_service.NextTaskReport):
            return served
        assessment_service.record(
            workspace.paths,
            run=run_id,
            content_id=outcome.content_id,
            score=score,
            clock=workspace.clock,
        )
        served += 1


def test_a_pilot_calibration_starts_with_every_dimension_open(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    assert report.calibration_label == "pilot-calibration"
    assert report.algorithm_version == ALGORITHM_VERSION
    assert report.pack_maturity == "pilot"
    assert {entry.dimension for entry in report.dimensions} == {
        "reading",
        "listening",
        "vocabulary-control",
        "grammar-control",
        "writing",
        "spoken-production",
        "pronunciation",
    }
    assert all(entry.status == "open" for entry in report.dimensions)
    assert all(entry.tasks_used == 0 for entry in report.dimensions)
    assert report.untested_dimensions == ()


def test_a_declared_level_centres_the_prior_without_asserting_it(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    assert report.declared_level == "A2"
    assert all(entry.estimated_level is None for entry in report.dimensions)
    assert all(entry.confidence == "low" for entry in report.dimensions)


def test_comprehensive_placement_is_refused_on_a_pilot_pack_with_the_reasons(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.start(
            polish_workspace.paths, run_type="placement", clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "placement_bank_insufficient"
    assert failure.value.payload.details[0].context["maturity"] == "pilot"
    fields = {detail.field for detail in failure.value.payload.details}
    assert "alternate_placement_forms" in fields
    assert any(field.startswith("objective_bank/") for field in fields)


def test_a_dimension_the_learners_equipment_cannot_serve_is_not_tested(
    polish_workspace: PolishWorkspace,
) -> None:
    """A learner with no microphone has not failed a speaking test."""

    report = assessment_service.start(
        polish_workspace.paths,
        modalities=["text", "writing"],
        clock=polish_workspace.clock,
    )

    untested = {
        entry.dimension: entry for entry in report.dimensions if entry.status == "not-tested"
    }
    # Listening joins them: every listening task in this pack needs audio.
    assert set(untested) == {"listening", "pronunciation", "spoken-production"}
    for entry in untested.values():
        assert entry.confidence == "not-tested"
        assert entry.unavailable_reason
        assert entry.tasks_used == 0
    assert set(report.untested_dimensions) == {
        "listening",
        "pronunciation",
        "spoken-production",
    }


def test_a_dimension_the_pack_cannot_test_is_not_tested(
    polish_workspace: PolishWorkspace,
) -> None:
    with (
        open_writer(
            polish_workspace.paths, command="test.empty-bank", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE content_records SET lifecycle = 'draft' WHERE content_id IN "
            "(SELECT content_id FROM assessment_tasks WHERE dimension = 'listening')"
        )

    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    listening = next(entry for entry in report.dimensions if entry.dimension == "listening")
    assert listening.status == "not-tested"
    assert listening.unavailable_reason == "the pack has no reviewed task for this dimension"


def test_only_the_dimensions_asked_for_are_opened(polish_workspace: PolishWorkspace) -> None:
    report = assessment_service.start(
        polish_workspace.paths, dimensions=["reading"], clock=polish_workspace.clock
    )

    assert [entry.dimension for entry in report.dimensions] == ["reading"]

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.start(
            polish_workspace.paths, dimensions=["telepathy"], clock=polish_workspace.clock
        )
    assert failure.value.payload.code == "dimension_unknown"


def test_a_served_task_carries_everything_needed_to_present_it(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    served = assessment_service.next_task(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    assert isinstance(served, assessment_service.NextTaskReport)
    assert served.prompt
    assert served.task_type in {
        "objective",
        "short-response",
        "extended-productive",
        "pronunciation-target",
        "connected-speech",
    }
    assert served.permitted_help
    assert served.selection_reason == "informativeness"
    assert served.sequence == 1


def test_recording_a_task_that_was_never_served_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        content_id = str(database.scalar("SELECT content_id FROM assessment_tasks LIMIT 1"))

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=report.run_id,
            content_id=content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_task_not_served"


def test_recording_the_same_task_twice_is_an_idempotent_no_op(
    polish_workspace: PolishWorkspace,
) -> None:
    """A retry must not fold the same evidence into the posterior twice."""

    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)

    first = assessment_service.record(
        polish_workspace.paths,
        run=report.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )
    second = assessment_service.record(
        polish_workspace.paths,
        run=report.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )

    assert first.tasks_recorded == 1
    assert second.tasks_recorded == 1
    first_state = next(e for e in first.dimensions if e.dimension == served.dimension)
    second_state = next(e for e in second.dimensions if e.dimension == served.dimension)
    assert first_state.posterior_mean == second_state.posterior_mean

    # A retry repeats an observation; it does not replace one. Offering a *different*
    # verdict for the same task is refused rather than silently ignored, because
    # returning the run report unchanged told the caller a correction had landed.
    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=report.run_id,
            content_id=served.content_id,
            score=0.0,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_result_conflict"
    after = assessment_service.report(polish_workspace.paths, run=report.run_id)
    refused_state = next(e for e in after.dimensions if e.dimension == served.dimension)
    assert refused_state.posterior_mean == first_state.posterior_mean


@pytest.mark.parametrize("score", [-0.5, 1.5])
def test_a_score_outside_the_unit_interval_is_refused(
    polish_workspace: PolishWorkspace, score: float
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=report.run_id,
            content_id=served.content_id,
            score=score,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "invalid_arguments"


def test_a_run_serves_the_least_progressed_dimension_first(
    polish_workspace: PolishWorkspace,
) -> None:
    """A run that stops early must have spread its evidence, not finished one dimension."""

    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    dimensions = []
    for _ in range(7):
        served = assessment_service.next_task(
            polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
        )
        assert isinstance(served, assessment_service.NextTaskReport)
        dimensions.append(served.dimension)
        assessment_service.record(
            polish_workspace.paths,
            run=report.run_id,
            content_id=served.content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )

    assert len(set(dimensions)) == 7


def test_a_run_pauses_and_resumes_with_every_posterior_intact(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    for _ in range(4):
        served = assessment_service.next_task(
            polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
        )
        assert isinstance(served, assessment_service.NextTaskReport)
        assessment_service.record(
            polish_workspace.paths,
            run=report.run_id,
            content_id=served.content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )
    before = assessment_service.report(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    paused = assessment_service.set_status(
        polish_workspace.paths, status="paused", run=report.run_id, clock=polish_workspace.clock
    )
    resumed = assessment_service.set_status(
        polish_workspace.paths,
        status="in-progress",
        run=report.run_id,
        clock=polish_workspace.clock,
    )

    assert paused.status == "paused"
    assert resumed.status == "in-progress"
    assert [entry.posterior_mean for entry in resumed.dimensions] == [
        entry.posterior_mean for entry in before.dimensions
    ]
    # A paused run can still be continued.
    served = assessment_service.next_task(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)


def test_finalizing_writes_one_traceable_estimate_per_dimension(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    _run_to_completion(polish_workspace, report.run_id)

    final = assessment_service.finalize(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    assert final.status == "finalized"
    assert final.stop_reason == "completed"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        estimates = database.query(
            "SELECT dimension, basis, confidence_label, evidence_count, source_run_id, "
            "calculation_version FROM skill_estimates ORDER BY dimension"
        )

    assert len(estimates) == 7
    for _dimension, basis, confidence, evidence, source, calculation in estimates:
        assert str(basis) == "calibration"
        assert str(confidence) in {"low", "medium", "high"}
        assert int(evidence) > 0
        assert str(source) == report.run_id
        assert ALGORITHM_VERSION in str(calculation)


def test_finalizing_never_collapses_the_profile_into_one_level(
    polish_workspace: PolishWorkspace,
) -> None:
    """Each dimension keeps its own estimate, range, and confidence."""

    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    _run_to_completion(polish_workspace, report.run_id)
    final = assessment_service.finalize(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    for entry in final.dimensions:
        assert entry.estimated_level is not None
        assert entry.credible_low is not None and entry.credible_high is not None
        assert entry.stop_reason
        assert entry.uncertainty is not None


def test_finalizing_an_unprobed_dimension_leaves_it_a_hypothesis(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(
        polish_workspace.paths, dimensions=["reading"], clock=polish_workspace.clock
    )

    final = assessment_service.finalize(
        polish_workspace.paths,
        run=report.run_id,
        reason="learner requested a stop",
        clock=polish_workspace.clock,
    )

    reading = next(entry for entry in final.dimensions if entry.dimension == "reading")
    assert reading.tasks_used == 0
    assert reading.status == "not-tested"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        basis = database.scalar("SELECT basis FROM skill_estimates WHERE dimension = 'reading'")
    assert str(basis) == "declared-hypothesis"


def test_finalizing_twice_returns_the_same_result(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    first = assessment_service.finalize(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    second = assessment_service.finalize(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    assert second.finalized_at == first.finalized_at
    assert second.status == "finalized"


def test_a_finalized_run_takes_no_further_results(
    polish_workspace: PolishWorkspace,
) -> None:
    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.finalize(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as record_failure:
        assessment_service.record(
            polish_workspace.paths,
            run=report.run_id,
            content_id=served.content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )
    with pytest.raises(LinguaWikiError) as next_failure:
        assessment_service.next_task(
            polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
        )
    with pytest.raises(LinguaWikiError) as status_failure:
        assessment_service.set_status(
            polish_workspace.paths,
            status="paused",
            run=report.run_id,
            clock=polish_workspace.clock,
        )

    assert record_failure.value.payload.code == "assessment_run_closed"
    assert next_failure.value.payload.code == "assessment_run_closed"
    assert status_failure.value.payload.code == "assessment_run_closed"


def test_an_item_stays_unavailable_inside_the_reuse_window(
    polish_workspace: PolishWorkspace,
) -> None:
    """Improvement must not be memorisation, so a seen item is not served again."""

    first = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=first.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.record(
        polish_workspace.paths,
        run=first.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )
    assessment_service.finalize(
        polish_workspace.paths, run=first.run_id, clock=polish_workspace.clock
    )

    second = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    seen: set[str] = set()
    while True:
        outcome = assessment_service.next_task(
            polish_workspace.paths, run=second.run_id, clock=polish_workspace.clock
        )
        if not isinstance(outcome, assessment_service.NextTaskReport):
            break
        seen.add(outcome.content_id)
        assessment_service.record(
            polish_workspace.paths,
            run=second.run_id,
            content_id=outcome.content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )

    assert served.content_id not in seen


def test_an_exhausted_bank_stops_a_dimension_and_says_so(
    polish_workspace: PolishWorkspace,
) -> None:
    """A bank-exhausted stop is a low-confidence result, not a precision claim."""

    report = assessment_service.start(
        polish_workspace.paths, dimensions=["pronunciation"], clock=polish_workspace.clock
    )
    _run_to_completion(polish_workspace, report.run_id, score=0.5)
    final = assessment_service.report(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )

    pronunciation = next(entry for entry in final.dimensions)
    assert pronunciation.stop_reason is not None
    if pronunciation.stop_reason.startswith("bank exhausted"):
        assert pronunciation.confidence in {"low", "medium"}


def test_an_exposure_older_than_the_reuse_window_becomes_available_again(
    polish_workspace: PolishWorkspace,
) -> None:
    first = assessment_service.start(
        polish_workspace.paths, dimensions=["reading"], clock=polish_workspace.clock
    )
    served = assessment_service.next_task(
        polish_workspace.paths, run=first.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.record(
        polish_workspace.paths,
        run=first.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )
    assessment_service.finalize(
        polish_workspace.paths, run=first.run_id, clock=polish_workspace.clock
    )
    stale = polish_workspace.clock.now() - timedelta(days=30 * (REUSE_WINDOW_MONTHS + 1))
    with (
        open_writer(
            polish_workspace.paths, command="test.age-exposure", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_item_exposures SET last_exposed_at = ?",
            [stale.replace(tzinfo=None)],
        )

    second = assessment_service.start(
        polish_workspace.paths, dimensions=["reading"], clock=polish_workspace.clock
    )
    seen: set[str] = set()
    for _ in range(6):
        outcome = assessment_service.next_task(
            polish_workspace.paths, run=second.run_id, clock=polish_workspace.clock
        )
        if not isinstance(outcome, assessment_service.NextTaskReport):
            break
        seen.add(outcome.content_id)
        assessment_service.record(
            polish_workspace.paths,
            run=second.run_id,
            content_id=outcome.content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )

    assert served.content_id in seen


def test_an_idempotency_key_replays_the_run_it_opened(
    polish_workspace: PolishWorkspace,
) -> None:
    first = assessment_service.start(
        polish_workspace.paths, idempotency_key="calibration-1", clock=polish_workspace.clock
    )
    second = assessment_service.start(
        polish_workspace.paths, idempotency_key="calibration-1", clock=polish_workspace.clock
    )

    assert second.run_id == first.run_id


def test_an_unknown_run_or_an_ambiguous_selection_is_named(
    polish_workspace: PolishWorkspace,
) -> None:
    assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assessment_service.start(
        polish_workspace.paths, dimensions=["reading"], clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as unknown:
        assessment_service.report(
            polish_workspace.paths,
            run="asm_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clock=polish_workspace.clock,
        )
    with pytest.raises(LinguaWikiError) as ambiguous:
        assessment_service.report(polish_workspace.paths, clock=polish_workspace.clock)

    assert unknown.value.payload.code == "assessment_run_not_found"
    assert ambiguous.value.payload.code == "assessment_run_selection_required"


def test_an_unknown_run_type_or_assessor_kind_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as run_type:
        assessment_service.start(
            polish_workspace.paths, run_type="milestone", clock=polish_workspace.clock
        )

    report = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    with pytest.raises(LinguaWikiError) as assessor:
        assessment_service.record(
            polish_workspace.paths,
            run=report.run_id,
            content_id=served.content_id,
            score=1.0,
            assessor_kind="oracle",
            clock=polish_workspace.clock,
        )

    assert run_type.value.payload.code == "invalid_arguments"
    assert assessor.value.payload.code == "invalid_arguments"


def test_a_rubric_and_an_excerpt_are_stored_with_the_scorer(
    polish_workspace: PolishWorkspace,
) -> None:
    """A total alone cannot be reviewed later, so the detail must survive."""

    report = assessment_service.start(
        polish_workspace.paths, dimensions=["writing"], clock=polish_workspace.clock
    )
    served = assessment_service.next_task(
        polish_workspace.paths, run=report.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)

    assessment_service.record(
        polish_workspace.paths,
        run=report.run_id,
        content_id=served.content_id,
        score=0.75,
        rubric={"dimensions": [{"name": "accuracy", "score": 0.5, "note": "case errors"}]},
        response_excerpt="Cześć, mam pytanie o pralkę.",
        assessor_kind="ai",
        assessor="example-model",
        confidence="low",
        clock=polish_workspace.clock,
    )

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        row = database.one(
            "SELECT rubric_json, response_excerpt, assessor_kind, assessor, confidence, "
            "prior_json, posterior_json, difficulty FROM assessment_results"
        )
    assert row is not None
    assert "accuracy" in str(row[0])
    assert str(row[1]) == "Cześć, mam pytanie o pralkę."
    assert (str(row[2]), str(row[3]), str(row[4])) == ("ai", "example-model", "low")
    # The prior and posterior are kept so the run can be replayed.
    assert str(row[5]) != str(row[6])
    assert float(row[7]) == served.difficulty


def test_the_calibration_opened_by_onboarding_is_the_one_that_runs(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    finalized = onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    assert finalized.assessment_run_id is not None

    served = assessment_service.next_task(
        polish_workspace.paths, run=finalized.assessment_run_id, clock=polish_workspace.clock
    )

    assert isinstance(served, assessment_service.NextTaskReport)
    assert served.run_id == finalized.assessment_run_id


def test_the_placement_refusal_survives_a_pack_directory_that_has_moved(
    polish_workspace: PolishWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal must not crash inside its own explanation."""

    from linguawiki import resources as core_resources

    monkeypatch.setattr(core_resources, "language_packs_directory", lambda: tmp_path / "gone")
    with (
        open_writer(
            polish_workspace.paths, command="test.move-pack", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE pack_installations SET source_path = ?", [str(tmp_path / "moved")]
        )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.start(
            polish_workspace.paths, run_type="placement", clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "placement_bank_insufficient"
    # Only the maturity detail survives; the bank could not be measured.
    assert [detail.field for detail in failure.value.payload.details] == ["maturity"]
