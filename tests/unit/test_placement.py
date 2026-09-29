"""Placement algorithm v1 as arithmetic, tested without a database.

The stop rule is the part that matters: it has four conditions, and a run that stops
because only three of them hold reports a precision it never reached.
"""

from __future__ import annotations

import math

import pytest

from linguawiki.placement import (
    ALGORITHM_VERSION,
    BUDGETS,
    CONNECTED_SPEECH_BUDGET,
    MINIMUM_FAMILIES,
    PRECISION_MASS,
    PRECISION_WIDTH,
    REUSE_WINDOW_MONTHS,
    Candidate,
    ability_grid,
    broad_prior,
    budget_for,
    close_dimension,
    confidence_label,
    credible_interval,
    declared_prior,
    estimated_level,
    expected_posterior_entropy,
    initial_state,
    posterior_mean,
    posterior_median,
    posterior_sd,
    record_score,
    select_task,
    stop_decision,
    success_probability,
    unavailable_reason,
    update_posterior,
)

CEFR = ("A1", "A2", "B1", "B2", "C1", "C2")


def task(
    identifier: str,
    *,
    difficulty: float,
    family: str = "travel",
    dimension: str = "reading",
    task_type: str = "objective",
    modality: str = "text",
    anchor: bool = False,
) -> Candidate:
    return Candidate(
        content_id=identifier,
        dimension=dimension,
        task_type=task_type,
        difficulty=difficulty,
        content_family=family,
        modality=modality,
        level_code=CEFR[min(len(CEFR) - 1, round(difficulty))],
        is_anchor=anchor,
    )


def reading_state(declared: float | None = 1.0):
    return initial_state(
        dimension="reading",
        dimension_kind="receptive",
        level_count=len(CEFR),
        declared_index=declared,
    )


def test_the_grid_is_one_unit_per_band_at_half_band_resolution() -> None:
    grid = ability_grid(len(CEFR))

    assert grid[0] == 0.0
    assert grid[-1] == float(len(CEFR) - 1)
    assert grid[1] - grid[0] == pytest.approx(0.5)
    assert len(grid) == 2 * (len(CEFR) - 1) + 1


def test_a_framework_with_one_level_cannot_place_anyone() -> None:
    with pytest.raises(ValueError):
        ability_grid(1)


def test_the_response_curve_is_the_documented_logistic() -> None:
    assert success_probability(1.0, 1.0) == pytest.approx(0.5)
    assert success_probability(2.0, 1.0) == pytest.approx(1 / (1 + math.exp(-1.7)))
    assert success_probability(0.0, 5.0) < 0.01
    assert success_probability(5.0, 0.0) > 0.99
    # Clamped, so a single answer can never make a grid point impossible for ever.
    assert 0.0 < success_probability(-50.0, 50.0) < 1.0


def test_a_declared_level_centres_the_prior_without_narrowing_the_conclusion() -> None:
    grid = ability_grid(len(CEFR))
    declared = declared_prior(grid, centre=1.0)
    broad = broad_prior(grid)

    assert sum(declared) == pytest.approx(1.0)
    assert posterior_median(grid, declared) == pytest.approx(1.0)
    assert posterior_sd(grid, declared) < posterior_sd(grid, broad)
    # One band of spread: the prior still gives real weight two bands away.
    assert declared[grid.index(3.0)] > 0.01


def test_a_fractional_score_moves_the_posterior_between_the_two_extremes() -> None:
    grid = ability_grid(len(CEFR))
    prior = broad_prior(grid)

    correct = update_posterior(grid, prior, difficulty=2.0, score=1.0)
    partial = update_posterior(grid, prior, difficulty=2.0, score=0.5)
    wrong = update_posterior(grid, prior, difficulty=2.0, score=0.0)

    assert posterior_mean(grid, wrong) < posterior_mean(grid, partial)
    assert posterior_mean(grid, partial) < posterior_mean(grid, correct)
    assert sum(partial) == pytest.approx(1.0)


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_a_score_outside_the_unit_interval_is_refused(score: float) -> None:
    grid = ability_grid(len(CEFR))

    with pytest.raises(ValueError):
        update_posterior(grid, broad_prior(grid), difficulty=1.0, score=score)


def test_the_credible_interval_is_the_narrowest_one_holding_the_required_mass() -> None:
    grid = ability_grid(len(CEFR))
    posterior = declared_prior(grid, centre=2.0, spread=0.3)

    low, high = credible_interval(grid, posterior)

    assert low <= 2.0 <= high
    inside = sum(
        weight for point, weight in zip(grid, posterior, strict=True) if low <= point <= high
    )
    assert inside >= PRECISION_MASS
    assert high - low <= PRECISION_WIDTH


def test_an_informative_task_sits_near_the_posterior_median() -> None:
    grid = ability_grid(len(CEFR))
    posterior = declared_prior(grid, centre=2.0, spread=0.5)

    near = expected_posterior_entropy(grid, posterior, difficulty=2.0)
    far = expected_posterior_entropy(grid, posterior, difficulty=5.0)

    assert near < far


@pytest.mark.parametrize(
    ("kind", "budget"),
    [("receptive", (6, 12)), ("form", (6, 12)), ("productive", (3, 5)), ("pronunciation", (4, 8))],
)
def test_each_dimension_kind_has_the_documented_budget(kind: str, budget: tuple[int, int]) -> None:
    assert budget_for(kind) == budget
    assert BUDGETS[kind] == budget


def test_an_unknown_dimension_kind_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        budget_for("telepathy")


def test_a_dimension_below_its_minimum_budget_never_stops() -> None:
    state = reading_state()

    for index in range(state.minimum_tasks - 1):
        state = record_score(
            state,
            task(f"cnt_{index}", difficulty=1.0, family=f"family-{index}"),
            score=1.0,
            level_count=len(CEFR),
        )

    assert state.status == "open"
    assert stop_decision(state, level_count=len(CEFR)) == (False, None)


def test_precision_alone_does_not_stop_a_dimension_with_one_content_family() -> None:
    """The coverage condition is independent: six answers from one family is not evidence."""

    state = reading_state()
    for index in range(state.minimum_tasks):
        state = record_score(
            state,
            task(f"cnt_{index}", difficulty=1.0, family="travel"),
            score=1.0,
            level_count=len(CEFR),
        )

    assert state.coverage < MINIMUM_FAMILIES
    assert state.status == "open"


def test_a_dimension_that_meets_every_condition_stops_on_precision() -> None:
    state = reading_state()
    difficulties = [1.0, 1.0, 1.5, 0.5, 2.0, 1.0]
    families = ["travel", "food", "work", "home", "health", "travel"]
    for index, (difficulty, family) in enumerate(zip(difficulties, families, strict=True)):
        state = record_score(
            state,
            task(f"cnt_{index}", difficulty=difficulty, family=family),
            score=1.0 if difficulty <= 1.5 else 0.0,
            level_count=len(CEFR),
        )

    assert state.status == "stopped"
    assert state.stop_reason == "precision-reached"
    assert state.boundary_probed
    assert state.coverage >= MINIMUM_FAMILIES


def test_a_dimension_at_its_maximum_budget_stops_and_says_so() -> None:
    state = reading_state()
    for index in range(state.maximum_tasks):
        # Alternating answers keep the posterior wide, so only the budget can stop it.
        state = record_score(
            state,
            task(f"cnt_{index}", difficulty=1.0 + (index % 5) * 0.5, family=f"family-{index}"),
            score=float(index % 2),
            level_count=len(CEFR),
        )

    assert state.status == "stopped"
    assert state.stop_reason == "maximum-budget"
    assert confidence_label(state) in {"low", "medium"}


def test_an_estimate_at_the_framework_edge_needs_no_boundary_probe() -> None:
    """There is nothing above C2 to probe with, so requiring a probe would never stop."""

    state = reading_state(declared=5.0)
    for index in range(state.minimum_tasks):
        state = record_score(
            state,
            task(f"cnt_{index}", difficulty=5.0, family=f"family-{index % 3}"),
            score=1.0,
            level_count=len(CEFR),
        )

    assert state.status == "stopped"
    assert state.stop_reason == "precision-reached"


def test_a_pronunciation_dimension_needs_a_connected_speech_sample() -> None:
    state = initial_state(
        dimension="pronunciation",
        dimension_kind="pronunciation",
        level_count=len(CEFR),
        declared_index=1.0,
    )
    # Difficulties spread so the precision and boundary-probe conditions are satisfied;
    # the only condition left unmet is the connected-speech sample.
    for index, difficulty in enumerate([1.0, 1.5, 0.5, 1.0][: state.minimum_tasks]):
        state = record_score(
            state,
            task(
                f"cnt_{index}",
                difficulty=difficulty,
                family=f"contrast-{index}",
                dimension="pronunciation",
                task_type="pronunciation-target",
                modality="speech",
            ),
            score=1.0 if difficulty <= 1.0 else 0.0,
            level_count=len(CEFR),
        )

    assert state.connected_speech_used == 0
    assert state.boundary_probed
    assert state.coverage >= MINIMUM_FAMILIES
    assert (
        credible_interval(state.grid, state.posterior)[1]
        - credible_interval(state.grid, state.posterior)[0]
        <= PRECISION_WIDTH
    )
    assert state.status == "open"

    state = record_score(
        state,
        task(
            "cnt_connected",
            difficulty=1.0,
            family="connected",
            dimension="pronunciation",
            task_type="connected-speech",
            modality="speech",
        ),
        score=1.0,
        level_count=len(CEFR),
    )

    assert state.connected_speech_used == CONNECTED_SPEECH_BUDGET[0]
    assert state.status == "stopped"


def test_selection_serves_coverage_before_informativeness() -> None:
    """Choosing the most informative task first never satisfies the coverage rule."""

    state = reading_state()
    state = record_score(
        state,
        task("cnt_first", difficulty=1.0, family="travel"),
        score=1.0,
        level_count=len(CEFR),
    )
    candidates = [
        task("cnt_same_family", difficulty=1.0, family="travel"),
        task("cnt_new_family", difficulty=1.0, family="food"),
    ]

    selection = select_task(
        state, candidates, available_modalities=["text"], excluded=["cnt_first"]
    )

    assert selection is not None
    assert selection.reason == "content-family diversity"
    assert selection.candidate.content_family == "food"


def test_selection_probes_a_boundary_once_the_minimum_budget_is_met() -> None:
    state = reading_state()
    for index in range(state.minimum_tasks):
        state = record_score(
            state,
            task(f"cnt_{index}", difficulty=1.0, family=f"family-{index % 3}"),
            score=float(index % 2),
            level_count=len(CEFR),
        )
    candidates = [
        task("cnt_centre", difficulty=posterior_median(state.grid, state.posterior)),
        task("cnt_probe", difficulty=posterior_median(state.grid, state.posterior) + 1.5),
    ]

    selection = select_task(state, candidates, available_modalities=["text"])

    assert selection is not None
    if not state.boundary_probed:
        assert selection.reason == "boundary probe"
        assert selection.candidate.content_id == "cnt_probe"


def test_selection_refuses_a_task_of_the_wrong_type_or_modality() -> None:
    state = reading_state()
    wrong_type = [task("cnt_a", difficulty=1.0, task_type="extended-productive")]
    wrong_modality = [task("cnt_b", difficulty=1.0, modality="speech")]

    assert select_task(state, wrong_type, available_modalities=["text", "audio"]) is None
    assert select_task(state, wrong_modality, available_modalities=["text"]) is None


def test_selection_never_serves_an_excluded_item() -> None:
    state = reading_state()
    candidates = [task("cnt_seen", difficulty=1.0)]

    assert (
        select_task(state, candidates, available_modalities=["text"], excluded=["cnt_seen"]) is None
    )


def test_an_untestable_dimension_is_not_tested_rather_than_failed() -> None:
    no_microphone = unavailable_reason(
        dimension_kind="pronunciation", available_modalities=["text", "audio"], bank_size=5
    )
    empty_bank = unavailable_reason(
        dimension_kind="receptive", available_modalities=["text"], bank_size=0
    )
    fine = unavailable_reason(
        dimension_kind="receptive", available_modalities=["text"], bank_size=5
    )

    assert no_microphone is not None and "speech" in no_microphone
    assert empty_bank == "the pack has no reviewed task for this dimension"
    assert fine is None


def test_closing_an_unprobed_dimension_marks_it_not_tested() -> None:
    state = close_dimension(reading_state(), reason="no microphone")

    assert state.status == "not-tested"
    assert state.stop_reason == "no microphone"
    assert confidence_label(state) == "not-tested"
    assert estimated_level(state, CEFR) == (None, None, None)


def test_closing_a_partly_probed_dimension_keeps_its_evidence() -> None:
    state = record_score(
        reading_state(), task("cnt_a", difficulty=1.0), score=1.0, level_count=len(CEFR)
    )
    closed = close_dimension(state, reason="learner requested a stop")

    assert closed.status == "stopped"
    assert closed.tasks_used == 1
    assert confidence_label(closed) == "low"


def test_the_reported_range_rounds_outwards() -> None:
    """Rounding the bounds to the nearest band claimed a precision never established."""

    state = reading_state(declared=1.5)
    state = record_score(state, task("cnt_a", difficulty=1.5), score=1.0, level_count=len(CEFR))

    level, low, high = estimated_level(state, CEFR)
    interval = credible_interval(state.grid, state.posterior)

    assert level is not None
    assert CEFR.index(low) <= math.floor(interval[0])
    assert CEFR.index(high) >= math.ceil(interval[1])


def test_the_algorithm_version_and_reuse_window_are_explicit() -> None:
    """Both are recorded with every run, so both must be stated rather than implied."""

    assert ALGORITHM_VERSION == "placement.v1"
    assert REUSE_WINDOW_MONTHS == 6
