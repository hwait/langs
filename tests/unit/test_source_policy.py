"""The source rules, tested where they are decided rather than through a command.

Two of these are the reason the stage exists: comprehension without help cannot be
recorded after help was given, and what may be stored from a work is bounded by its
rights. Both are policy, so both are tested here and again at the boundaries that
enforce them.
"""

from __future__ import annotations

import pytest

from linguawiki import session as session_policy
from linguawiki import sources as source_policy
from linguawiki.errors import LinguaWikiError


def observation(aid: str, band: str, sequence: int) -> source_policy.Comprehension:
    return source_policy.Comprehension(aid=aid, band=band, sequence=sequence)


def test_the_policy_is_internally_consistent() -> None:
    source_policy.assert_policy_is_sound()


def test_every_source_kind_names_areas_a_session_can_plan() -> None:
    """A kind whose areas are unknown would score a block type that does not exist."""

    areas = set(session_policy.AREAS)
    for kind in source_policy.SOURCE_KINDS:
        served = source_policy.AREAS_FOR_KIND[kind]
        assert served, f"{kind} serves no area"
        assert set(served) <= areas, f"{kind} names areas that are not session areas"


def test_bands_are_ordered_by_strength_and_not_alphabetically() -> None:
    """`max()` over the strings would rank `none` above `gist`, which inverts the answer."""

    strengths = [source_policy.band_strength(band) for band in source_policy.COMPREHENSION_BANDS]
    assert strengths == sorted(strengths)
    assert source_policy.band_strength("none") < source_policy.band_strength("gist")
    assert source_policy.band_strength("gist") < source_policy.band_strength("full")


def test_help_cannot_be_withdrawn() -> None:
    ordered = [observation("unaided", "gist", 1), observation("subtitled", "full", 2)]
    source_policy.assert_observation_order(ordered, reference="unit")

    with pytest.raises(LinguaWikiError) as failure:
        source_policy.assert_observation_order(
            [*ordered, observation("unaided", "full", 3)], reference="unit"
        )
    assert failure.value.payload.code == "comprehension_aid_regressed"


def test_unaided_comprehension_ignores_the_aided_readings_entirely() -> None:
    observations = [
        observation("unaided", "little", 1),
        observation("glossed", "full", 2),
        observation("translated", "full", 3),
    ]
    assert source_policy.unaided_comprehension(observations) == "little"
    assert source_policy.aided_comprehension(observations) == "full"


def test_an_unmeasured_source_is_unmeasured_rather_than_zero() -> None:
    """ "Nobody measured this" and "they understood none of it" are different facts."""

    assert source_policy.unaided_comprehension([]) is None
    assert source_policy.unaided_comprehension([observation("glossed", "most", 1)]) is None


def test_coverage_is_unknown_when_the_total_is() -> None:
    assert source_policy.coverage(completed_units=3, total_units=None) is None
    assert source_policy.coverage(completed_units=0, total_units=4) == 0.0
    assert source_policy.coverage(completed_units=4, total_units=4) == 1.0


def test_coverage_never_exceeds_the_whole_work() -> None:
    assert source_policy.coverage(completed_units=9, total_units=4) == 1.0


@pytest.mark.parametrize(
    ("rights", "length", "permitted"),
    [
        ("metadata-only", 1, False),
        ("short-excerpt", source_policy.EXCERPT_LIMIT, True),
        ("short-excerpt", source_policy.EXCERPT_LIMIT + 1, False),
        ("full-local", source_policy.EXCERPT_LIMIT + 1, True),
        ("full-local", source_policy.FULL_LOCAL_LIMIT + 1, False),
    ],
)
def test_an_excerpt_is_bounded_by_the_rights_it_was_catalogued_under(
    rights: str, length: int, permitted: bool
) -> None:
    if permitted:
        source_policy.assert_excerpt_permitted("x" * length, rights=rights, reference="unit")
        return
    with pytest.raises(LinguaWikiError):
        source_policy.assert_excerpt_permitted("x" * length, rights=rights, reference="unit")


def test_extensive_work_is_not_a_harvest() -> None:
    """Mining a pleasure read turns it into intensive work the learner did not agree to."""

    source_policy.assert_mode_permits_extraction(
        mode="extensive", extracted=source_policy.EXTENSIVE_EXTRACTION_LIMIT, reference="unit"
    )
    with pytest.raises(LinguaWikiError) as failure:
        source_policy.assert_mode_permits_extraction(
            mode="extensive",
            extracted=source_policy.EXTENSIVE_EXTRACTION_LIMIT + 1,
            reference="unit",
        )
    assert "extensive" in str(failure.value)
    # Intensive work has no such limit: extracting from it is the point.
    source_policy.assert_mode_permits_extraction(mode="intensive", extracted=50, reference="unit")


def test_a_source_cannot_take_a_lifecycle_step_it_has_no_edge_for() -> None:
    source_policy.assert_source_transition(current="cataloged", target="active", source_id="src_1")
    with pytest.raises(LinguaWikiError):
        source_policy.assert_source_transition(
            current="active", target="rejected", source_id="src_1"
        )
