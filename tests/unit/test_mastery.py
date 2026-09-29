"""The promotion, regression, and ceiling rules, and their invariants.

These are property tests as much as example tests, because the rules they encode are
what the stage's exit gate is about: recognition must never buy production, a delayed
failure must be able to pull an item back, and recomputation must be idempotent whatever
order the evidence arrives in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.evidence import CLAIM_CEILINGS, CLAIMS
from linguawiki.mastery import (
    AGGREGATION_VERSION,
    CLAIM_ORDER,
    DEFAULT_POLICY,
    STAGES,
    MasteryPolicy,
    Observation,
    StageGate,
    aggregate,
    assert_policy_is_sound,
    decay,
    observation_weight,
    stage_ceiling_for_claims,
    stage_strength,
    with_gate,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def observation(
    index: int,
    claim: str,
    *,
    polarity: str = "positive",
    context: str | None = None,
    retrieval: str = "immediate",
    help_level: str = "none",
    novelty: str = "novel",
    strength: float = 1.0,
    days_ago: float = 1.0,
) -> Observation:
    return Observation(
        evidence_id=f"evd_{index:04d}",
        claim=claim,
        polarity=polarity,
        strength=strength,
        context_key=context or f"context-{index}",
        retrieval=retrieval,
        help_level=help_level,
        novelty=novelty,
        occurred_at=NOW - timedelta(days=days_ago),
    )


def test_no_evidence_is_unseen_and_says_so() -> None:
    outcome = aggregate((), now=NOW)

    assert outcome.stage == "unseen"
    assert outcome.confidence == 0.0
    assert outcome.aggregation_version == AGGREGATION_VERSION
    assert [factor.name for factor in outcome.factors] == ["no-evidence"]


@pytest.mark.parametrize("count", [2, 5, 20, 50])
def test_recognition_never_promotes_production_however_often_it_succeeds(count: int) -> None:
    """The exit gate: repetition changes confidence, never the kind of claim."""

    outcome = aggregate([observation(index, "recognition") for index in range(count)], now=NOW)

    assert outcome.stage == "recognized"
    assert outcome.ceiling == "recognized"
    assert stage_strength(outcome.stage) < stage_strength("controlled-production")


def test_a_comprehension_pair_in_novel_contexts_reaches_understood() -> None:
    outcome = aggregate(
        [
            observation(1, "comprehension", context="reading"),
            observation(2, "comprehension", context="listening"),
        ],
        now=NOW,
    )

    assert outcome.stage == "understood"


def test_one_spontaneous_success_reaches_spontaneous_production() -> None:
    outcome = aggregate([observation(1, "spontaneous-production")], now=NOW)

    assert outcome.stage == "spontaneous-production"
    assert outcome.confidence < 0.2, "one observation is not confidence"


def test_two_delayed_transfers_in_two_contexts_reach_stable() -> None:
    outcome = aggregate(
        [
            observation(1, "delayed-transfer", context="a", retrieval="delayed", days_ago=10),
            observation(2, "delayed-transfer", context="b", retrieval="delayed", days_ago=9),
        ],
        now=NOW,
    )

    assert outcome.stage == "stable"


def test_a_delayed_failure_regresses_a_stable_item_and_names_the_claim() -> None:
    """The exit gate's other half: failure has to be able to take a stage away."""

    successes = [
        observation(1, "delayed-transfer", context="a", retrieval="delayed", days_ago=10),
        observation(2, "delayed-transfer", context="b", retrieval="delayed", days_ago=9),
    ]
    assert aggregate(successes, now=NOW).stage == "stable"

    regressed = aggregate(
        [
            *successes,
            observation(
                3,
                "delayed-transfer",
                polarity="negative",
                context="c",
                retrieval="delayed",
                days_ago=0.5,
            ),
        ],
        now=NOW,
    )

    assert regressed.stage == "encountered"
    assert regressed.regressed_claims == ("delayed-transfer",)
    assert any(factor.name == "regression" for factor in regressed.factors)


def test_a_later_success_at_the_same_claim_re_earns_the_stage() -> None:
    records = [
        observation(1, "delayed-transfer", context="a", retrieval="delayed", days_ago=10),
        observation(2, "delayed-transfer", context="b", retrieval="delayed", days_ago=9),
        observation(
            3, "delayed-transfer", polarity="negative", context="c", retrieval="delayed", days_ago=5
        ),
        observation(4, "delayed-transfer", context="d", retrieval="delayed", days_ago=1),
    ]

    outcome = aggregate(records, now=NOW)

    assert outcome.regressed_claims == ()
    assert outcome.stage == "stable"


def test_a_failure_at_a_weaker_claim_costs_confidence_not_the_stage() -> None:
    """Contradictory evidence lowers certainty; it does not collapse an unrelated claim.

    Failing to recognize a form while producing it after a delay is contradictory data,
    and the honest response is less confidence in the surviving stage rather than a
    stage nobody's evidence supports.
    """

    stable = [
        observation(1, "delayed-transfer", context="a", retrieval="delayed", days_ago=10),
        observation(2, "delayed-transfer", context="b", retrieval="delayed", days_ago=9),
    ]
    contradicted = aggregate(
        [*stable, observation(3, "recognition", polarity="negative", context="c", days_ago=0.5)],
        now=NOW,
    )

    assert contradicted.stage == "stable"
    assert contradicted.regressed_claims == ("recognition",)
    assert contradicted.confidence < aggregate(stable, now=NOW).confidence


def test_repetition_in_one_context_cannot_promote_or_convince() -> None:
    """Diversity, not volume. Five successes on one prompt are one observation."""

    repeated = aggregate(
        [observation(index, "recognition", context="same") for index in range(5)], now=NOW
    )
    varied = aggregate([observation(index, "recognition") for index in range(5)], now=NOW)

    assert repeated.stage == "encountered"
    assert varied.stage == "recognized"
    assert repeated.confidence < varied.confidence


def test_full_help_leaves_no_positive_weight_to_promote_with() -> None:
    """A given answer is not an observation of the learner."""

    outcome = aggregate(
        [
            observation(index, "recognition", help_level="full-answer", strength=0.0)
            for index in range(4)
        ],
        now=NOW,
    )

    assert outcome.stage == "encountered"


@pytest.mark.parametrize("claim", CLAIMS)
def test_a_single_claim_can_never_exceed_its_declared_ceiling(claim: str) -> None:
    """Whatever the gates say, the kind of evidence caps the stage."""

    records = [
        observation(
            index, claim, context=f"context-{index}", retrieval="delayed", days_ago=10 - index
        )
        for index in range(6)
    ]

    outcome = aggregate(records, now=NOW)

    assert stage_strength(outcome.stage) <= stage_strength(CLAIM_CEILINGS[claim])
    assert outcome.ceiling == CLAIM_CEILINGS[claim]


def test_intelligibility_is_about_a_dimension_and_moves_no_item_past_encountered() -> None:
    outcome = aggregate([observation(index, "intelligibility") for index in range(6)], now=NOW)

    assert outcome.stage == "encountered"


@pytest.mark.parametrize("size", [2, 3, 4])
def test_aggregation_is_order_independent(size: int) -> None:
    """Idempotent recomputation: the answer is a function of the set, not the sequence."""

    records = [
        observation(1, "recognition", context="a"),
        observation(2, "comprehension", context="b"),
        observation(3, "controlled-production", context="c"),
        observation(4, "spontaneous-production", context="d"),
    ][:size]
    expected = aggregate(records, now=NOW)

    for ordering in permutations(records):
        assert aggregate(list(ordering), now=NOW) == expected


def test_recomputation_is_idempotent() -> None:
    records = [
        observation(1, "comprehension", context="a"),
        observation(2, "comprehension", context="b"),
        observation(3, "recognition", polarity="negative", context="c"),
    ]

    first = aggregate(records, now=NOW)
    second = aggregate(records, now=NOW)

    assert first == second


def test_decay_reduces_weight_without_ever_removing_evidence() -> None:
    fresh = observation(1, "recognition", days_ago=0)
    old = observation(2, "recognition", days_ago=365)

    assert observation_weight(fresh, now=NOW) > observation_weight(old, now=NOW)
    assert observation_weight(old, now=NOW) > 0.0, "evidence decays, it is never deleted"


def test_evidence_stamped_in_the_future_is_not_inflated() -> None:
    assert decay(NOW + timedelta(days=30), now=NOW, half_life_days=45.0) == 1.0


def test_a_negative_observation_outlives_a_positive_one() -> None:
    """The reason to doubt decays more slowly, which is what makes regression stick."""

    positive = observation(1, "recognition", days_ago=90)
    negative = observation(2, "recognition", polarity="negative", days_ago=90)

    assert observation_weight(negative, now=NOW) > observation_weight(positive, now=NOW)


def test_a_stage_older_evidence_earned_decays_out_of_its_gate() -> None:
    records = [
        observation(1, "recognition", context="a", days_ago=1),
        observation(2, "recognition", context="b", days_ago=1),
    ]

    assert aggregate(records, now=NOW).stage == "recognized"
    assert aggregate(records, now=NOW + timedelta(days=400)).stage == "encountered"


def test_an_unknown_stage_is_refused_by_name() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        stage_strength("fluent")

    assert failure.value.payload.code == "unknown_mastery_stage"


def test_a_non_positive_half_life_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        decay(NOW, now=NOW, half_life_days=0.0)

    assert failure.value.payload.code == "invalid_mastery_policy"


def test_the_default_policy_is_sound() -> None:
    assert assert_policy_is_sound(DEFAULT_POLICY) is DEFAULT_POLICY


def test_a_policy_that_would_let_recognition_buy_production_is_refused() -> None:
    """A gate is a claim about what was proven, and this one could not be."""

    unsound = with_gate(
        DEFAULT_POLICY,
        StageGate(
            stage="spontaneous-production",
            minimum_claim="recognition",
            observations=2,
            contexts=2,
            weight=0.1,
        ),
    )

    with pytest.raises(LinguaWikiError) as failure:
        assert_policy_is_sound(unsound)

    assert failure.value.payload.code == "invalid_mastery_policy"
    assert "spontaneous-production" in {detail.field for detail in failure.value.payload.details}


def test_a_policy_that_would_call_one_observation_stable_is_refused() -> None:
    unsound = with_gate(
        DEFAULT_POLICY,
        StageGate(
            stage="stable",
            minimum_claim="delayed-transfer",
            observations=1,
            contexts=1,
            weight=0.1,
            requires_delay=True,
        ),
    )

    with pytest.raises(LinguaWikiError) as failure:
        assert_policy_is_sound(unsound)

    assert "stability cannot rest on one observation" in {
        detail.reason for detail in failure.value.payload.details
    }


def test_a_policy_naming_an_unknown_claim_is_refused() -> None:
    unsound = with_gate(
        DEFAULT_POLICY,
        StageGate(
            stage="recognized", minimum_claim="vibes", observations=1, contexts=1, weight=0.0
        ),
    )

    with pytest.raises(LinguaWikiError) as failure:
        assert_policy_is_sound(unsound)

    assert any("vibes" in detail.reason for detail in failure.value.payload.details)


def test_a_kind_may_carry_its_own_gates_without_a_language_branch() -> None:
    """Item kinds differ; the code that reads them does not know which language they are.

    A `character` needing more proof than a `culture` note is a pack's decision, and the
    aggregation takes it as a policy rather than as a special case in code.
    """

    strict = StageGate(
        stage="recognized", minimum_claim="recognition", observations=4, contexts=4, weight=0.8
    )
    policy = MasteryPolicy(kind_gates={"character": (DEFAULT_POLICY.gates[0], strict)})
    records = [observation(index, "recognition") for index in range(2)]

    assert aggregate(records, now=NOW, kind="lexeme", policy=policy).stage == "recognized"
    assert aggregate(records, now=NOW, kind="character", policy=policy).stage == "encountered"


def test_the_claim_order_and_the_stage_ladder_agree() -> None:
    """Each stronger claim reaches at least as far up the ladder as the weaker one."""

    ceilings = [stage_strength(CLAIM_CEILINGS[claim]) for claim in CLAIM_ORDER]

    assert ceilings == sorted(ceilings)


def test_the_ceiling_of_a_claim_set_is_its_strongest_member() -> None:
    assert stage_ceiling_for_claims(()) == "unseen"
    assert stage_ceiling_for_claims(("recognition", "spontaneous-production")) == (
        "spontaneous-production"
    )


def test_every_stage_is_reachable_by_some_evidence() -> None:
    """A ladder rung nothing can reach is a rung that means nothing."""

    reachable = {"unseen", "encountered"}
    for claim in CLAIMS:
        reachable.add(CLAIM_CEILINGS[claim])
    reachable.update(gate.stage for gate in DEFAULT_POLICY.gates)

    assert set(STAGES) <= reachable
