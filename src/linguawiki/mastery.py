"""How raw evidence becomes one item stage, and why.

The aggregation is a transparent rule set rather than a model, for three reasons: a
learner can be told why an item moved, a policy change can be replayed over evidence
that never changed, and a defect is a wrong rule rather than a wrong weight nobody can
find.

Two invariants outrank the gates and are applied after them:

- a stage is capped by the strongest *kind* of evidence behind it, so recognition can
  never promote spontaneous production however often it succeeds;
- failure regresses. Positive and negative evidence are weighed per claim, and a claim
  whose negatives outweigh its positives stops supporting its ceiling -- which is what
  lets a delayed failure pull an item back off `stable`.

Everything here is a pure function of the evidence passed in plus `now`, so
recomputation is idempotent by construction and `evidence recompute --dry-run` can show
the difference before writing it. No rule mentions a language, a script, or a word.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.evidence import CLAIM_CEILINGS, CLAIM_RULES, help_strength

#: Bumped whenever a rule below changes meaning. Stored on every computed row so a
#: stage can be told apart from one an earlier policy produced.
AGGREGATION_VERSION = "mastery.v1"

#: The ladder, weakest first. The index is the stage's ordinal strength.
STAGES: tuple[str, ...] = (
    "unseen",
    "encountered",
    "recognized",
    "understood",
    "controlled-production",
    "spontaneous-production",
    "stable",
)

#: How long a positive observation keeps half its weight. Evidence is never deleted; it
#: only stops carrying a promotion on its own.
HALF_LIFE_DAYS = 45.0
#: A negative observation decays more slowly than a positive one: the reason to doubt
#: outlives the reason to believe, which is what makes regression stick.
NEGATIVE_HALF_LIFE_DAYS = 90.0

#: What each claim is worth per unit of observed strength. Spontaneous and delayed
#: evidence outweigh recognition and immediate repetition, which is the whole ordering
#: the plan asks for.
CLAIM_WEIGHTS: Mapping[str, float] = {
    "recognition": 0.35,
    "comprehension": 0.55,
    "controlled-production": 0.75,
    "spontaneous-production": 1.0,
    "delayed-transfer": 1.0,
    "intelligibility": 0.4,
}
#: Delayed retrieval is worth more than immediate repetition at the same claim.
RETRIEVAL_FACTORS: Mapping[str, float] = {
    "immediate": 0.6,
    "same-session": 0.8,
    "delayed": 1.0,
}
#: A context met for the first time is worth more than the same one again.
NOVELTY_FACTORS: Mapping[str, float] = {"novel": 1.0, "repeat": 0.8}
#: A partial success is evidence of something, but not of the claim as stated.
POLARITY_FACTORS: Mapping[str, float] = {"positive": 1.0, "partial": 0.5, "negative": 1.0}

#: Positives must outweigh negatives by this much before a claim still supports its
#: ceiling. Above 1.0, so a single fresh failure against a single success regresses.
REGRESSION_RATIO = 1.0
#: Weight at which confidence saturates, and the number of distinct contexts required
#: before confidence may reach it at all.
CONFIDENCE_SATURATION = 4.0
CONFIDENCE_CONTEXTS = 3
#: A claim that failed while a stronger one still stands is contradictory data. The
#: honest response is less confidence in the surviving stage, not a collapse of it.
CONTRADICTION_PENALTY = 0.25


@dataclass(frozen=True, slots=True)
class StageGate:
    """What has to hold before an item may sit at this stage."""

    stage: str
    #: The weakest claim that counts toward this gate; anything stronger counts too.
    minimum_claim: str
    #: Distinct positive observations at or above `minimum_claim`.
    observations: int
    #: Distinct context keys among them. Diversity, not repetition.
    contexts: int
    #: Decayed weighted mass required from qualifying evidence.
    weight: float
    #: Whether a qualifying observation must come after a delay.
    requires_delay: bool = False
    #: Whether a qualifying observation must be in a context not seen before.
    requires_novel: bool = False
    #: The most help a qualifying observation may have had.
    maximum_help: str = "scaffolded"


#: Claims ordered by how much they demand of the learner. A gate naming a minimum claim
#: is satisfied by anything at or above it in this order.
CLAIM_ORDER: tuple[str, ...] = (
    "recognition",
    "comprehension",
    "controlled-production",
    "spontaneous-production",
    "delayed-transfer",
)

#: The defaults from the plan's mastery gates. They are a starting policy, not a
#: linguistic truth, and a pack or item kind may override them.
DEFAULT_GATES: tuple[StageGate, ...] = (
    StageGate(
        stage="encountered",
        minimum_claim="recognition",
        observations=0,
        contexts=0,
        weight=0.0,
    ),
    StageGate(
        stage="recognized",
        minimum_claim="recognition",
        observations=2,
        contexts=2,
        weight=0.25,
    ),
    StageGate(
        stage="understood",
        minimum_claim="comprehension",
        observations=2,
        contexts=2,
        weight=0.40,
        requires_novel=True,
        maximum_help="hinted",
    ),
    StageGate(
        stage="controlled-production",
        minimum_claim="controlled-production",
        observations=2,
        contexts=2,
        weight=0.55,
        maximum_help="prompted",
    ),
    StageGate(
        stage="spontaneous-production",
        minimum_claim="spontaneous-production",
        observations=1,
        contexts=1,
        weight=0.35,
        maximum_help="none",
    ),
    StageGate(
        stage="stable",
        # `delayed-transfer` is the claim for "used or retrieved correctly after a
        # delay", whatever the modality, and it is the only claim whose ceiling is
        # `stable`. Naming a weaker one here would let this gate grant a stage the
        # evidence ceiling immediately takes away -- a rule contradicting itself.
        minimum_claim="delayed-transfer",
        observations=2,
        contexts=2,
        weight=0.70,
        requires_delay=True,
        maximum_help="prompted",
    ),
)


@dataclass(frozen=True, slots=True)
class MasteryPolicy:
    """One versioned aggregation policy, overridable per item kind."""

    version: str = AGGREGATION_VERSION
    gates: tuple[StageGate, ...] = DEFAULT_GATES
    half_life_days: float = HALF_LIFE_DAYS
    negative_half_life_days: float = NEGATIVE_HALF_LIFE_DAYS
    regression_ratio: float = REGRESSION_RATIO
    confidence_saturation: float = CONFIDENCE_SATURATION
    confidence_contexts: int = CONFIDENCE_CONTEXTS
    #: Whether the most recent observation at a claim decides whether the claim stands.
    latest_failure_withdraws: bool = True
    #: How much each contradicted claim costs the confidence in the surviving stage.
    contradiction_penalty: float = CONTRADICTION_PENALTY
    #: Gate overrides by item kind, so a `character` can need different proof from a
    #: `pragmatics` note without either of them being special-cased in code.
    kind_gates: Mapping[str, tuple[StageGate, ...]] = field(default_factory=dict)

    def gates_for(self, kind: str | None) -> tuple[StageGate, ...]:
        if kind is not None and kind in self.kind_gates:
            return self.kind_gates[kind]
        return self.gates


DEFAULT_POLICY = MasteryPolicy()


@dataclass(frozen=True, slots=True)
class Observation:
    """One evidence row, reduced to what aggregation is allowed to look at."""

    evidence_id: str
    claim: str
    polarity: str
    strength: float
    context_key: str
    retrieval: str
    help_level: str
    novelty: str
    occurred_at: datetime

    @property
    def ceiling(self) -> str:
        return CLAIM_CEILINGS[self.claim]


@dataclass(frozen=True, slots=True)
class Factor:
    """One named, numeric reason the outcome came out the way it did."""

    name: str
    value: float
    detail: str


@dataclass(frozen=True, slots=True)
class MasteryOutcome:
    """The computed stage plus everything needed to explain and reproduce it."""

    stage: str
    confidence: float
    aggregation_version: str
    #: The stage the gates alone would have granted, before the evidence-kind ceiling.
    gated_stage: str
    #: The strongest stage any positive evidence can justify.
    ceiling: str
    positive_count: int
    negative_count: int
    contexts: tuple[str, ...]
    claims: tuple[str, ...]
    #: Claims whose negatives outweigh their positives, so their ceiling is withdrawn.
    regressed_claims: tuple[str, ...]
    factors: tuple[Factor, ...]
    last_evidence_at: datetime | None
    first_evidence_at: datetime | None


def stage_strength(stage: str) -> int:
    """The ordinal position of a stage, refusing an unknown one by name."""

    if stage not in STAGES:
        raise LinguaWikiError(
            "unknown_mastery_stage",
            f"{stage} is not a mastery stage; expected one of {list(STAGES)}",
            details=(ErrorDetail(field="stage", reason="unknown stage"),),
        )
    return STAGES.index(stage)


def claim_rank(claim: str) -> int:
    """How much a claim demands of the learner; dimension-scoped claims rank lowest."""

    return CLAIM_ORDER.index(claim) if claim in CLAIM_ORDER else -1


def decay(observed_at: datetime, *, now: datetime, half_life_days: float) -> float:
    """The share of an observation's weight that survives to `now`.

    Never zero: evidence is retained and keeps contributing, which is what separates
    decay from deletion. Evidence stamped in the future is not discounted; a clock skew
    is not a reason to inflate it either.
    """

    if half_life_days <= 0:
        raise LinguaWikiError(
            "invalid_mastery_policy",
            "a half-life must be a positive number of days",
            details=(ErrorDetail(field="half_life_days", reason=str(half_life_days)),),
        )
    elapsed = (now - observed_at) / timedelta(days=1)
    if elapsed <= 0:
        return 1.0
    return float(2.0 ** (-elapsed / half_life_days))


def observation_weight(
    observation: Observation, *, now: datetime, policy: MasteryPolicy = DEFAULT_POLICY
) -> float:
    """One observation's decayed contribution under this policy."""

    half_life = (
        policy.negative_half_life_days
        if observation.polarity == "negative"
        else policy.half_life_days
    )
    return (
        observation.strength
        * CLAIM_WEIGHTS[observation.claim]
        * RETRIEVAL_FACTORS[observation.retrieval]
        * NOVELTY_FACTORS[observation.novelty]
        * POLARITY_FACTORS[observation.polarity]
        * decay(observation.occurred_at, now=now, half_life_days=half_life)
    )


def _qualifies(observation: Observation, gate: StageGate) -> bool:
    """Whether one positive observation counts toward a gate."""

    if observation.polarity == "negative":
        return False
    if claim_rank(observation.claim) < claim_rank(gate.minimum_claim):
        return False
    return help_strength(observation.help_level) <= help_strength(gate.maximum_help)


def _regressed_claims(
    observations: Sequence[Observation], *, now: datetime, policy: MasteryPolicy
) -> tuple[str, ...]:
    """Claims whose negative mass outweighs their positive mass.

    Computed per claim rather than globally: failing to produce a form spontaneously is
    a reason to withdraw the spontaneous ceiling, and no reason at all to doubt that the
    learner recognises it.
    """

    positive: dict[str, float] = {}
    negative: dict[str, float] = {}
    latest: dict[str, tuple[datetime, str, str]] = {}
    for observation in observations:
        bucket = negative if observation.polarity == "negative" else positive
        bucket[observation.claim] = bucket.get(observation.claim, 0.0) + observation_weight(
            observation, now=now, policy=policy
        )
        stamp = (observation.occurred_at, observation.evidence_id, observation.polarity)
        if observation.claim not in latest or stamp[:2] > latest[observation.claim][:2]:
            latest[observation.claim] = stamp
    outweighed = {
        claim
        for claim, against in negative.items()
        if against > 0.0 and positive.get(claim, 0.0) < against * policy.regression_ratio
    }
    # The last thing we saw dominates, whatever the older mass says. An item whose most
    # recent test at a claim was a failure is not one the learner has at that claim --
    # and a delayed failure is exactly the case the exit gate names. Re-earning the
    # claim needs new evidence at it, not the passage of time.
    stalest = {claim for claim, (_, _, polarity) in latest.items() if polarity == "negative"}
    return tuple(sorted(outweighed | (stalest if policy.latest_failure_withdraws else set())))


def _ceiling(
    observations: Sequence[Observation], *, regressed: Sequence[str]
) -> tuple[str, tuple[str, ...]]:
    """The strongest stage the surviving positive evidence can justify.

    This is the guard the whole module exists for. A claim that has been withdrawn by
    failure contributes no ceiling, and a claim nobody made contributes none either --
    so an item reaches `spontaneous-production` only if somebody actually observed it.
    """

    claims = tuple(
        sorted(
            {
                observation.claim
                for observation in observations
                if observation.polarity != "negative" and observation.claim not in regressed
            }
        )
    )
    if not claims:
        return ("unseen" if not observations else "encountered", claims)
    strongest = max(claims, key=lambda claim: stage_strength(CLAIM_CEILINGS[claim]))
    return (CLAIM_CEILINGS[strongest], claims)


def _confidence(
    qualifying: Sequence[Observation],
    *,
    weight: float,
    contexts: int,
    contradictions: int,
    policy: MasteryPolicy,
) -> float:
    """Confidence rises with mass, but only as far as diversity permits.

    Repeating one context cannot raise confidence past its share of the requirement:
    twenty successes on the same prompt are one observation repeated, and treating them
    as twenty is how a learner gets told they are ready for something they are not.
    """

    if not qualifying:
        return 0.0
    mass = min(1.0, weight / policy.confidence_saturation)
    diversity = min(1.0, contexts / policy.confidence_contexts)
    contradicted = max(0.0, 1.0 - contradictions * policy.contradiction_penalty)
    return round(mass * diversity * contradicted, 6)


def aggregate(
    observations: Sequence[Observation],
    *,
    now: datetime,
    kind: str | None = None,
    policy: MasteryPolicy = DEFAULT_POLICY,
) -> MasteryOutcome:
    """Compute one item's stage, confidence, and explanation from its raw evidence."""

    ordered = sorted(observations, key=lambda entry: (entry.occurred_at, entry.evidence_id))
    factors: list[Factor] = []
    if not ordered:
        return MasteryOutcome(
            stage="unseen",
            confidence=0.0,
            aggregation_version=policy.version,
            gated_stage="unseen",
            ceiling="unseen",
            positive_count=0,
            negative_count=0,
            contexts=(),
            claims=(),
            regressed_claims=(),
            factors=(Factor(name="no-evidence", value=0.0, detail="no evidence is recorded"),),
            last_evidence_at=None,
            first_evidence_at=None,
        )
    regressed = _regressed_claims(ordered, now=now, policy=policy)
    ceiling, claims = _ceiling(ordered, regressed=regressed)
    gated = "encountered"
    for gate in policy.gates_for(kind):
        qualifying = [
            observation
            for observation in ordered
            if _qualifies(observation, gate) and observation.claim not in regressed
        ]
        contexts = {observation.context_key for observation in qualifying}
        weight = sum(
            observation_weight(observation, now=now, policy=policy) for observation in qualifying
        )
        delayed = any(observation.retrieval == "delayed" for observation in qualifying)
        novel = any(observation.novelty == "novel" for observation in qualifying)
        satisfied = (
            len(qualifying) >= gate.observations
            and len(contexts) >= gate.contexts
            and weight + 1e-9 >= gate.weight
            and (delayed or not gate.requires_delay)
            and (novel or not gate.requires_novel)
        )
        factors.append(
            Factor(
                name=f"gate:{gate.stage}",
                value=round(weight, 6),
                detail=(
                    f"{'met' if satisfied else 'unmet'}: {len(qualifying)}/{gate.observations} "
                    f"observation(s) at {gate.minimum_claim} or above, {len(contexts)}/"
                    f"{gate.contexts} context(s), weight {weight:.3f}/{gate.weight:.3f}"
                    + (", delay required" if gate.requires_delay else "")
                    + (", novel context required" if gate.requires_novel else "")
                ),
            )
        )
        if satisfied and stage_strength(gate.stage) > stage_strength(gated):
            gated = gate.stage
    stage = gated if stage_strength(gated) <= stage_strength(ceiling) else ceiling
    qualifying_all = [observation for observation in ordered if observation.polarity != "negative"]
    contexts_all = tuple(sorted({observation.context_key for observation in qualifying_all}))
    confidence_weight = sum(
        observation_weight(observation, now=now, policy=policy) for observation in qualifying_all
    )
    factors.append(
        Factor(
            name="ceiling",
            value=float(stage_strength(ceiling)),
            detail=(
                f"the strongest evidence recorded is {', '.join(claims) or 'none'}, which caps "
                f"the stage at {ceiling}"
            ),
        )
    )
    if regressed:
        factors.append(
            Factor(
                name="regression",
                value=float(len(regressed)),
                detail=(
                    f"failure outweighs success for {', '.join(regressed)}, so those claims no "
                    "longer support their stage"
                ),
            )
        )
    if stage != gated:
        factors.append(
            Factor(
                name="ceiling-applied",
                value=float(stage_strength(gated) - stage_strength(stage)),
                detail=(
                    f"the gates alone would have granted {gated}; the kind of evidence behind it "
                    f"allows only {stage}"
                ),
            )
        )
    factors.append(
        Factor(
            name="decay",
            value=round(confidence_weight, 6),
            detail=(
                f"{len(qualifying_all)} supporting observation(s) carry {confidence_weight:.3f} "
                f"weight after a {policy.half_life_days:.0f}-day half-life"
            ),
        )
    )
    return MasteryOutcome(
        stage=stage,
        confidence=_confidence(
            qualifying_all,
            weight=confidence_weight,
            contexts=len(contexts_all),
            contradictions=len(regressed),
            policy=policy,
        ),
        aggregation_version=policy.version,
        gated_stage=gated,
        ceiling=ceiling,
        positive_count=len(qualifying_all),
        negative_count=len(ordered) - len(qualifying_all),
        contexts=contexts_all,
        claims=claims,
        regressed_claims=regressed,
        factors=tuple(factors),
        last_evidence_at=ordered[-1].occurred_at,
        first_evidence_at=ordered[0].occurred_at,
    )


def assert_policy_is_sound(policy: MasteryPolicy) -> MasteryPolicy:
    """Refuse a policy that could promote an item on evidence that cannot support it.

    A gate is a claim about what was proven. A gate whose minimum claim cannot reach the
    stage it grants would let recognition buy production, which no amount of tuning
    should be able to express.
    """

    problems: list[ErrorDetail] = []
    for gates in (policy.gates, *policy.kind_gates.values()):
        for gate in gates:
            stage_strength(gate.stage)
            if gate.minimum_claim not in CLAIM_RULES:
                problems.append(
                    ErrorDetail(field=gate.stage, reason=f"unknown claim {gate.minimum_claim}")
                )
                continue
            # The gate admits every claim at or above its minimum, so the minimum is
            # what it can be satisfied by in the worst case. Judging it by the
            # *strongest* admitted claim is how a gate saying 'any recognition counts'
            # could look sound while granting spontaneous production.
            weakest = stage_strength(CLAIM_CEILINGS[gate.minimum_claim])
            if weakest < stage_strength(gate.stage):
                problems.append(
                    ErrorDetail(
                        field=gate.stage,
                        reason=(
                            f"a {gate.minimum_claim} claim can only justify "
                            f"{CLAIM_CEILINGS[gate.minimum_claim]}, so this gate would grant "
                            f"{gate.stage} on evidence that cannot support it"
                        ),
                    )
                )
            if gate.observations <= 1 and gate.stage == "stable":
                problems.append(
                    ErrorDetail(field=gate.stage, reason="stability cannot rest on one observation")
                )
    if problems:
        raise LinguaWikiError(
            "invalid_mastery_policy",
            "the aggregation policy would grant a stage its evidence cannot support",
            details=tuple(problems),
        )
    return policy


def with_gate(policy: MasteryPolicy, gate: StageGate) -> MasteryPolicy:
    """A copy of the policy with one gate replaced, used by tests and pack overrides."""

    return replace(
        policy,
        gates=tuple(gate if each.stage == gate.stage else each for each in policy.gates),
    )


def stage_ceiling_for_claims(claims: Sequence[str]) -> str:
    """The strongest stage a set of claims can justify, ignoring gates entirely."""

    if not claims:
        return "unseen"
    return max((CLAIM_CEILINGS[claim] for claim in claims), key=lambda stage: stage_strength(stage))


__all__ = [
    "AGGREGATION_VERSION",
    "CLAIM_ORDER",
    "CLAIM_WEIGHTS",
    "CONFIDENCE_CONTEXTS",
    "CONFIDENCE_SATURATION",
    "DEFAULT_GATES",
    "DEFAULT_POLICY",
    "HALF_LIFE_DAYS",
    "NEGATIVE_HALF_LIFE_DAYS",
    "REGRESSION_RATIO",
    "STAGES",
    "Factor",
    "MasteryOutcome",
    "MasteryPolicy",
    "Observation",
    "StageGate",
    "aggregate",
    "assert_policy_is_sound",
    "claim_rank",
    "decay",
    "observation_weight",
    "stage_ceiling_for_claims",
    "stage_strength",
    "with_gate",
]
