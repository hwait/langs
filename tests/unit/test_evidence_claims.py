"""What an observation is allowed to claim, and what it is worth.

The compatibility matrix is the guard that keeps the rest of the learner model honest:
if a selection task could produce a spontaneous-production claim, no downstream ceiling
would help. So each refusal is tested by name, and the whole matrix is checked for the
properties it has to have rather than only for the cases that happen to be used.
"""

from __future__ import annotations

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.evidence import (
    ASSESSOR_FACTORS,
    CLAIM_CEILINGS,
    CLAIM_RULES,
    CLAIMS,
    HELP_LEVELS,
    MODALITIES,
    PRODUCTIVE_MODALITIES,
    SELECTION_TASK_TYPES,
    TASK_TYPES,
    assert_compatible,
    claim_rule,
    claims_for_task,
    help_strength,
    observation_strength,
    outcome_for,
    polarity_for,
)

ITEM = "cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def compatible(claim: str, **overrides: object) -> object:
    arguments: dict[str, object] = {
        "modality": "text",
        "task_type": "objective",
        "help_level": "none",
        "retrieval": "immediate",
        "polarity": "positive",
        "target_content_id": ITEM,
        "dimension": "reading",
    }
    arguments.update(overrides)
    return assert_compatible(claim, **arguments)  # type: ignore[arg-type]


def test_a_selection_task_produces_recognition_and_comprehension_only() -> None:
    """Choosing the right option is recognition, whatever the learner actually knows."""

    for task_type in SELECTION_TASK_TYPES:
        produced = claims_for_task(task_type)
        assert "spontaneous-production" not in produced
        assert "controlled-production" not in produced
        assert "delayed-transfer" not in produced


def test_a_spontaneous_claim_from_a_selection_task_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        compatible("spontaneous-production", modality="writing", task_type="objective")

    assert failure.value.payload.code == "evidence_task_incompatible"


def test_a_production_claim_from_a_receptive_modality_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        compatible("spontaneous-production", modality="text", task_type="meaning-focused-exchange")

    assert failure.value.payload.code == "evidence_modality_incompatible"
    assert "speech" in failure.value.payload.message


def test_a_delayed_transfer_claim_without_a_delay_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        compatible("delayed-transfer", task_type="short-response", retrieval="immediate")

    assert failure.value.payload.code == "evidence_delay_required"


def test_a_delayed_selection_task_is_delayed_recognition_not_transfer() -> None:
    """Recognising an option after a week is still recognition."""

    with pytest.raises(LinguaWikiError) as failure:
        compatible("delayed-transfer", task_type="objective", retrieval="delayed")

    assert failure.value.payload.code == "evidence_task_incompatible"


@pytest.mark.parametrize("claim", ["spontaneous-production", "controlled-production"])
def test_a_positive_production_claim_cannot_survive_the_answer_being_given(claim: str) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        compatible(
            claim,
            modality="writing",
            task_type="extended-productive",
            help_level="full-answer",
        )

    assert failure.value.payload.code == "evidence_help_exceeds_claim"


def test_a_negative_claim_survives_any_help_level() -> None:
    """Failing with a hint is a clearer failure, not an invalid observation."""

    rule = compatible(
        "spontaneous-production",
        modality="writing",
        task_type="extended-productive",
        help_level="full-answer",
        polarity="negative",
    )

    assert rule is CLAIM_RULES["spontaneous-production"]


def test_an_intelligibility_claim_must_name_a_dimension() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        compatible(
            "intelligibility",
            modality="speech",
            task_type="pronunciation-target",
            dimension=None,
        )

    assert failure.value.payload.code == "evidence_dimension_required"


def test_evidence_about_nothing_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        compatible("recognition", target_content_id=None, dimension=None)

    assert failure.value.payload.code == "evidence_target_required"


@pytest.mark.parametrize("claim", CLAIMS)
def test_every_claim_declares_a_reachable_ceiling(claim: str) -> None:
    rule = claim_rule(claim)

    assert rule.item_ceiling == CLAIM_CEILINGS[claim]
    assert rule.modalities
    assert rule.task_types
    assert rule.maximum_help in HELP_LEVELS


@pytest.mark.parametrize("claim", ["controlled-production", "spontaneous-production"])
def test_production_claims_require_a_productive_modality(claim: str) -> None:
    assert set(CLAIM_RULES[claim].modalities) <= set(PRODUCTIVE_MODALITIES)


def test_every_task_type_can_produce_at_least_one_claim() -> None:
    """A task type nothing can be claimed from could never justify any evidence."""

    for task_type in TASK_TYPES:
        assert claims_for_task(task_type), task_type


def test_an_unknown_claim_task_or_modality_is_refused_by_name() -> None:
    for arguments, code in (
        ({"claim": "fluency"}, "unknown_evidence_claim"),
        ({"task_type": "vibes"}, "unknown_task_type"),
        ({"modality": "telepathy"}, "unknown_modality"),
        ({"polarity": "maybe"}, "unknown_polarity"),
        ({"retrieval": "eventually"}, "unknown_retrieval_class"),
    ):
        claim = str(arguments.pop("claim", "recognition"))
        with pytest.raises(LinguaWikiError) as failure:
            compatible(claim, **arguments)
        assert failure.value.payload.code == code


def test_an_unknown_help_level_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        help_strength("a little")

    assert failure.value.payload.code == "unknown_help_level"


def test_strength_discounts_hints_ai_grading_and_low_confidence() -> None:
    clean = observation_strength(
        normalized_score=1.0,
        polarity="positive",
        help_level="none",
        assessor_kind="deterministic",
        confidence="high",
    )
    hinted = observation_strength(
        normalized_score=1.0,
        polarity="positive",
        help_level="hinted",
        assessor_kind="deterministic",
        confidence="high",
    )
    ai_graded = observation_strength(
        normalized_score=1.0,
        polarity="positive",
        help_level="none",
        assessor_kind="ai",
        confidence="high",
    )
    unsure = observation_strength(
        normalized_score=1.0,
        polarity="positive",
        help_level="none",
        assessor_kind="deterministic",
        confidence="low",
    )

    assert clean == 1.0
    assert hinted < clean
    assert ai_graded < clean
    assert unsure < clean
    assert ASSESSOR_FACTORS["ai"] < ASSESSOR_FACTORS["deterministic"]


def test_a_given_answer_is_worth_nothing_positive() -> None:
    assert (
        observation_strength(
            normalized_score=1.0,
            polarity="positive",
            help_level="full-answer",
            assessor_kind="deterministic",
            confidence="high",
        )
        == 0.0
    )


def test_a_failure_is_not_discounted_for_the_help_it_had() -> None:
    """Failing *with* a hint is a stronger failure, so the help does not soften it."""

    assert (
        observation_strength(
            normalized_score=0.0,
            polarity="negative",
            help_level="scaffolded",
            assessor_kind="ai",
            confidence="low",
        )
        == 1.0
    )


def test_a_score_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        observation_strength(
            normalized_score=1.5,
            polarity="positive",
            help_level="none",
            assessor_kind="human",
            confidence="high",
        )

    assert failure.value.payload.code == "invalid_score"


@pytest.mark.parametrize(
    ("score", "outcome", "polarity"),
    [
        (1.0, "success", "positive"),
        (0.8, "success", "positive"),
        (0.6, "partial", "partial"),
        (0.2, "failure", "negative"),
        (0.0, "failure", "negative"),
    ],
)
def test_scores_classify_into_outcomes_and_polarities(
    score: float, outcome: str, polarity: str
) -> None:
    assert outcome_for(score) == outcome
    assert polarity_for(outcome) == polarity


def test_no_claim_rule_mentions_a_language_or_a_script() -> None:
    """The whole vocabulary describes demand, never a linguistic structure."""

    forbidden = ("polish", "chinese", "pl", "zh", "latin", "hanzi", "cyrillic")
    for claim, rule in CLAIM_RULES.items():
        text = " ".join((claim, *rule.modalities, *rule.task_types)).lower()
        for token in forbidden:
            assert f" {token} " not in f" {text} ", (claim, token)


def test_every_modality_appears_in_some_claim_rule() -> None:
    covered = {modality for rule in CLAIM_RULES.values() for modality in rule.modalities}

    assert covered == set(MODALITIES)
