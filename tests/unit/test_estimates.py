"""Estimate status, snapshot equality, and the refusals that keep a status honest.

The database-free parts of the estimate service: what counts as a change worth a
snapshot, and the three contradictions `write_estimate` refuses outright.
"""

from __future__ import annotations

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.placement import ability_grid, broad_prior, posterior_mean, update_posterior
from linguawiki.services.estimates import (
    BASES,
    CALCULATION_VERSION,
    ESTIMATE_STATUSES,
    NUMERIC_TOLERANCE,
    SNAPSHOT_FIELDS,
    EstimateRecord,
    _differs,
)

TRACK = "trk_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def record(**overrides: object) -> EstimateRecord:
    arguments: dict[str, object] = {
        "track_id": TRACK,
        "dimension": "reading",
        "framework_id": "cefr",
        "estimate_status": "estimated",
        "level_code": "A2",
        "level_low": "A1",
        "level_high": "B1",
        "score": 1.5,
        "uncertainty": 0.8,
        "confidence_label": "medium",
        "basis": "evidence",
        "evidence_count": 4,
        "source_run_id": None,
        "calculation_version": CALCULATION_VERSION,
        "as_of": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
    }
    arguments.update(overrides)
    return EstimateRecord(**arguments)  # type: ignore[arg-type]


def test_a_first_estimate_is_always_a_change() -> None:
    assert _differs(None, record()) is True


def test_recomputing_the_same_estimate_is_not_a_change() -> None:
    """Idempotent recomputation: the history records changes, not recomputations."""

    assert _differs(record(), record()) is False


def test_a_later_timestamp_alone_is_not_a_change() -> None:
    """The estimate is still current; nothing about it moved."""

    later = "2026-10-01T00:00:00+00:00"

    assert _differs(record(), record(as_of=later, updated_at=later)) is False


@pytest.mark.parametrize("field_name", SNAPSHOT_FIELDS)
def test_every_snapshot_field_is_one_a_change_is_recorded_for(field_name: str) -> None:
    moved = {
        "estimate_status": "provisional",
        "level_code": "B2",
        "level_low": "A2",
        "level_high": "C1",
        "score": 2.7,
        "uncertainty": 0.2,
        "confidence_label": "high",
        "basis": "placement",
        "evidence_count": 9,
    }[field_name]

    assert _differs(record(), record(**{field_name: moved})) is True


def test_floating_point_noise_is_not_a_change() -> None:
    """Recomputation must not be bit-identical to count as unchanged."""

    assert _differs(record(), record(score=1.5 + NUMERIC_TOLERANCE / 10)) is False
    assert _differs(record(), record(score=1.5 + 0.01)) is True


def test_a_null_becoming_a_value_is_a_change() -> None:
    assert _differs(record(level_code=None), record(level_code="A2")) is True


def test_the_status_vocabulary_separates_untested_from_unmeasured() -> None:
    assert ESTIMATE_STATUSES == ("not-tested", "provisional", "estimated")
    assert "declared-hypothesis" in BASES


def test_the_ability_grid_and_a_broad_prior_agree_on_the_middle() -> None:
    """A dimension with no evidence sits in the middle of the framework, not at its top."""

    grid = ability_grid(6)
    prior = broad_prior(grid)

    assert 2.0 <= posterior_mean(grid, prior) <= 3.0


def test_a_tempered_observation_moves_the_posterior_less_than_a_clean_one() -> None:
    """The weight is how a hinted or decayed observation counts without being dropped."""

    grid = ability_grid(6)
    prior = broad_prior(grid)
    clean = update_posterior(grid, prior, difficulty=4.0, score=1.0, weight=1.0)
    tempered = update_posterior(grid, prior, difficulty=4.0, score=1.0, weight=0.2)

    assert posterior_mean(grid, clean) > posterior_mean(grid, tempered)
    assert posterior_mean(grid, tempered) > posterior_mean(grid, prior)


@pytest.mark.parametrize("weight", [0.0, -0.5, 1.5])
def test_an_observation_weight_outside_the_unit_interval_is_refused(weight: float) -> None:
    grid = ability_grid(6)

    with pytest.raises(ValueError, match="weight"):
        update_posterior(grid, broad_prior(grid), difficulty=1.0, score=1.0, weight=weight)


def test_repeating_one_observation_narrows_less_than_two_independent_ones() -> None:
    """Uncertainty falls with independent observations, which is why weight exists."""

    from linguawiki.placement import posterior_sd

    grid = ability_grid(6)
    prior = broad_prior(grid)
    once = update_posterior(grid, prior, difficulty=2.0, score=1.0, weight=0.3)
    twice = update_posterior(grid, once, difficulty=2.0, score=1.0, weight=0.3)
    full = update_posterior(
        grid,
        update_posterior(grid, prior, difficulty=2.0, score=1.0, weight=1.0),
        difficulty=2.0,
        score=1.0,
        weight=1.0,
    )

    assert posterior_sd(grid, twice) > posterior_sd(grid, full)


def test_an_unknown_status_or_basis_is_refused_before_anything_is_written() -> None:
    from linguawiki.services.estimates import write_estimate

    class Refusing:
        """A database that fails loudly if the writer reaches it."""

        def now(self) -> object:
            raise AssertionError("validation must happen before any database access")

    for arguments, code in (
        ({"estimate_status": "guessed"}, "unknown_estimate_status"),
        ({"basis": "vibes"}, "unknown_estimate_basis"),
    ):
        with pytest.raises(LinguaWikiError) as failure:
            write_estimate(
                Refusing(),  # type: ignore[arg-type]
                track_id=TRACK,
                framework_id="cefr",
                dimension="reading",
                estimate_status=str(arguments.get("estimate_status", "estimated")),
                level_code="A2",
                level_low="A1",
                level_high="B1",
                score=1.5,
                uncertainty=0.5,
                confidence="medium",
                basis=str(arguments.get("basis", "evidence")),
                evidence_count=3,
                source_run_id=None,
                reason="test",
            )
        assert failure.value.payload.code == code
