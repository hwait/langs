"""The one place that decides whether a piece of content is reviewed enough.

Origin and review are independent questions, and each review axis has its own state
vocabulary because the words mean different things: `cleared` settles redistribution and
says nothing about whether a pronunciation contrast is real. Rather than compare state
names, every state is given an ordinal *strength* inside its axis, and a promotion gate
requires a minimum strength per axis, taken as the elementwise maximum of the risk tier's
row and the target lifecycle's row.

Two rules are structural rather than tabular and are enforced here as well:

- a machine or AI reviewer can never reach a state that claims human or reference
  verification, however many times it is run;
- content at risk tier 3 or 4 needs a *verified* source alignment, never
  `not-applicable`, which is the floor ADR 0005 froze.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from linguawiki.contracts import (
    ContentOrigin,
    ContentReview,
    OriginClass,
    PackItemReview,
    ReviewAxis,
    ReviewerKind,
    ReviewState,
)

#: Lifecycle values that assert the content is good enough for some use.
PROMOTED_LIFECYCLES = ("approved-personal", "verified", "publication-ready")
#: Lifecycle values that say the content is explicitly not yet trusted.
UNPROMOTED_LIFECYCLES = ("draft", "candidate", "needs-review", "rejected", "deprecated")

REVIEW_AXES: tuple[str, ...] = tuple(str(axis) for axis in ReviewAxis)

#: Each axis's own states, weakest first. The index is the state's strength.
AXIS_STATES: Mapping[str, tuple[str, ...]] = {
    "linguistic": ("unreviewed", "machine-checked", "reference-verified", "human-verified"),
    "pedagogical": ("unreviewed", "machine-checked", "learner-approved", "teacher-verified"),
    "source-alignment": ("unchecked", "machine-checked", "verified"),
    "rights": ("unknown", "personal-use-only", "cleared"),
    "privacy": ("private", "redacted", "shareable"),
}
#: States that mean "this axis does not apply", rather than "not yet done".
NOT_APPLICABLE_STATES: Mapping[str, str] = {"source-alignment": "not-applicable"}
#: States that are a definite negative verdict rather than an unfinished one.
FAILED_STATES: Mapping[str, frozenset[str]] = {"rights": frozenset({"restricted"})}

#: The strongest state a machine or AI reviewer may ever claim on each axis.
MACHINE_CEILING: Mapping[str, int] = {
    "linguistic": 1,
    "pedagogical": 1,
    "source-alignment": 1,
    "rights": 0,
    "privacy": 0,
}
MACHINE_REVIEWER_KINDS = frozenset({ReviewerKind.MACHINE, ReviewerKind.AI})

#: Minimum strength per axis implied by an item's risk tier.
RISK_TIER_REQUIREMENTS: Mapping[int, Mapping[str, int]] = {
    0: {"linguistic": 0, "pedagogical": 0, "source-alignment": 0, "rights": 0, "privacy": 0},
    1: {"linguistic": 1, "pedagogical": 1, "source-alignment": 1, "rights": 1, "privacy": 0},
    2: {"linguistic": 1, "pedagogical": 2, "source-alignment": 1, "rights": 1, "privacy": 0},
    3: {"linguistic": 2, "pedagogical": 3, "source-alignment": 2, "rights": 1, "privacy": 0},
    4: {"linguistic": 3, "pedagogical": 3, "source-alignment": 2, "rights": 2, "privacy": 2},
}
#: Minimum strength per axis implied by the lifecycle the item claims.
LIFECYCLE_REQUIREMENTS: Mapping[str, Mapping[str, int]] = {
    "approved-personal": {
        "linguistic": 1,
        "pedagogical": 1,
        "source-alignment": 0,
        "rights": 1,
        "privacy": 0,
    },
    "verified": {
        "linguistic": 2,
        "pedagogical": 2,
        "source-alignment": 0,
        "rights": 1,
        "privacy": 0,
    },
    "publication-ready": {
        "linguistic": 3,
        "pedagogical": 3,
        "source-alignment": 2,
        "rights": 2,
        "privacy": 2,
    },
}
#: Tiers at which `not-applicable` source alignment is no longer acceptable.
SOURCE_ALIGNMENT_REQUIRED_FROM_TIER = 3

#: Origin classes whose content cannot alone support a canonical or shared claim.
AI_ORIGIN_CLASSES = frozenset(
    {OriginClass.AI_GENERATED, OriginClass.AI_ADAPTED, OriginClass.SYNTHETIC_MEDIA}
)
#: Risk tier from which an AI origin needs an identified external source.
CANONICAL_TIER = 3

#: `lingua.content.v1` names two origin classes more briefly than the authoring
#: vocabulary does. The projection maps them; nothing else translates origins.
CONTENT_ORIGIN_BY_CLASS: Mapping[str, ContentOrigin] = {
    OriginClass.AUTHENTIC_SOURCE: ContentOrigin.AUTHENTIC,
    OriginClass.SYNTHETIC_MEDIA: ContentOrigin.SYNTHETIC,
    OriginClass.HUMAN_AUTHORED: ContentOrigin.HUMAN_AUTHORED,
    OriginClass.AI_ADAPTED: ContentOrigin.AI_ADAPTED,
    OriginClass.AI_GENERATED: ContentOrigin.AI_GENERATED,
    OriginClass.LEARNER_PRODUCED: ContentOrigin.LEARNER_PRODUCED,
    OriginClass.SOURCE_DERIVED: ContentOrigin.SOURCE_DERIVED,
}


class SamplingPolicy(StrEnum):
    """How thoroughly a generated batch must be inspected."""

    FULL_INSPECTION = "full-inspection"
    SAMPLED = "sampled"


#: Runs of a new or materially changed template that are inspected in full.
FULL_INSPECTION_RUNS = 3
#: Minimum sample of a stable template's batch, as a share and as a floor.
SAMPLE_SHARE = 0.2
SAMPLE_FLOOR = 5


def axis_states(axis: str) -> tuple[str, ...]:
    try:
        return AXIS_STATES[axis]
    except KeyError as exc:
        raise ValueError(f"unknown review axis: {axis}") from exc


def is_known_state(axis: str, state: str) -> bool:
    """Whether an axis admits this state at all.

    The ordered vocabulary is only part of it: an axis may also have a `not-applicable`
    state and an outright negative verdict, and both are *known* states. Leaving them out
    made `restricted` rights report as an unknown state instead of as the refusal it is.
    """

    return (
        state in axis_states(axis)
        or state == NOT_APPLICABLE_STATES.get(axis)
        or state in FAILED_STATES.get(axis, frozenset())
    )


def state_strength(axis: str, state: str) -> int:
    """The ordinal strength of one axis state, or -1 for a negative verdict.

    `not-applicable` has no strength: it removes the axis from consideration rather
    than satisfying it, which is why it is reported separately.
    """

    if state in FAILED_STATES.get(axis, frozenset()):
        return -1
    if state == NOT_APPLICABLE_STATES.get(axis):
        return -1
    states = axis_states(axis)
    if state not in states:
        raise ValueError(f"{state} is not a state of the {axis} review axis")
    return states.index(state)


def required_strengths(*, risk_tier: int, lifecycle: str) -> dict[str, int]:
    """The minimum strength each axis needs for this tier and lifecycle."""

    if risk_tier not in RISK_TIER_REQUIREMENTS:
        raise ValueError(f"risk tier {risk_tier} is outside 0-4")
    tier_row = RISK_TIER_REQUIREMENTS[risk_tier]
    lifecycle_row = LIFECYCLE_REQUIREMENTS.get(lifecycle, {})
    if lifecycle in UNPROMOTED_LIFECYCLES:
        # Unpromoted content makes no claim, so nothing is required of it yet.
        return dict.fromkeys(REVIEW_AXES, 0)
    return {axis: max(tier_row[axis], lifecycle_row.get(axis, 0)) for axis in REVIEW_AXES}


@dataclass(frozen=True, slots=True)
class GateProblem:
    """One reason a piece of content may not claim the lifecycle it declares."""

    axis: str | None
    reason: str

    def __str__(self) -> str:
        return f"{self.axis}: {self.reason}" if self.axis else self.reason


def review_gate_state(axis: str, review: PackItemReview | None, *, required: int) -> ReviewState:
    """Project one axis onto the pass/pending/fail vocabulary of `lingua.content.v1`."""

    if review is None:
        return ReviewState.PENDING if required > 0 else ReviewState.NOT_REQUIRED
    if review.state in FAILED_STATES.get(axis, frozenset()):
        return ReviewState.FAILED
    if review.state == NOT_APPLICABLE_STATES.get(axis):
        return ReviewState.NOT_REQUIRED
    strength = state_strength(axis, review.state)
    if required == 0:
        # Nothing is demanded of this axis, so a weak state is "not required" rather
        # than a pass it never earned.
        return ReviewState.PASSED if strength > 0 else ReviewState.NOT_REQUIRED
    return ReviewState.PASSED if strength >= required else ReviewState.PENDING


def gate_problems(
    *,
    risk_tier: int,
    lifecycle: str,
    reviews: Mapping[str, PackItemReview],
    origin_classes: Sequence[str],
    source_references: Sequence[str] = (),
) -> tuple[GateProblem, ...]:
    """Every reason this content cannot hold the lifecycle it claims."""

    problems: list[GateProblem] = []
    unknown = sorted(set(reviews) - set(REVIEW_AXES))
    if unknown:
        problems.append(GateProblem(None, f"unknown review axes: {', '.join(unknown)}"))
    for axis, review in reviews.items():
        if axis in REVIEW_AXES and not is_known_state(axis, review.state):
            problems.append(GateProblem(axis, f"{review.state} is not a state of this axis"))
    if problems:
        return tuple(problems)
    for axis, review in reviews.items():
        if review.reviewer_kind not in MACHINE_REVIEWER_KINDS:
            continue
        strength = state_strength(axis, review.state)
        if strength > MACHINE_CEILING[axis]:
            problems.append(
                GateProblem(
                    axis,
                    f"a reviewer of kind '{review.reviewer_kind}' cannot claim "
                    f"{review.state}; machine checking stops at "
                    f"{axis_states(axis)[MACHINE_CEILING[axis]]}",
                )
            )
    required = required_strengths(risk_tier=risk_tier, lifecycle=lifecycle)
    for axis in REVIEW_AXES:
        declared = reviews.get(axis)
        minimum = required[axis]
        if declared is None:
            if minimum > 0:
                problems.append(GateProblem(axis, "review is missing"))
            continue
        if declared.state in FAILED_STATES.get(axis, frozenset()):
            problems.append(GateProblem(axis, f"{declared.state} is a negative verdict"))
            continue
        if declared.state == NOT_APPLICABLE_STATES.get(axis):
            if minimum > 0:
                problems.append(
                    GateProblem(axis, f"not-applicable cannot satisfy a minimum of {minimum}")
                )
            continue
        strength = state_strength(axis, declared.state)
        if strength < minimum:
            problems.append(
                GateProblem(
                    axis,
                    f"{declared.state} is weaker than {axis_states(axis)[minimum]}, which "
                    f"risk tier {risk_tier} and lifecycle {lifecycle} require",
                )
            )
        elif minimum > 0 and not declared.method:
            problems.append(GateProblem(axis, "a completed review must record its method"))
    if lifecycle in PROMOTED_LIFECYCLES and risk_tier >= SOURCE_ALIGNMENT_REQUIRED_FROM_TIER:
        alignment = reviews.get("source-alignment")
        if alignment is None or alignment.state == NOT_APPLICABLE_STATES["source-alignment"]:
            problems.append(
                GateProblem(
                    "source-alignment",
                    f"risk tier {risk_tier} content needs a verified source alignment, not "
                    "not-applicable",
                )
            )
    if (
        lifecycle in PROMOTED_LIFECYCLES
        and risk_tier >= CANONICAL_TIER
        and set(origin_classes) <= AI_ORIGIN_CLASSES
        and not source_references
    ):
        problems.append(
            GateProblem(
                None,
                f"risk tier {risk_tier} content of purely AI origin needs an identified "
                "external source",
            )
        )
    return tuple(problems)


def content_reviews(
    *,
    risk_tier: int,
    lifecycle: str,
    reviews: Mapping[str, PackItemReview],
    content_hash: str,
) -> tuple[ContentReview, ...]:
    """Project the per-axis states onto the frozen `lingua.content.v1` review list."""

    required = required_strengths(risk_tier=risk_tier, lifecycle=lifecycle)
    projected: list[ContentReview] = []
    for axis in REVIEW_AXES:
        review = reviews.get(axis)
        state = review_gate_state(axis, review, required=required[axis])
        completed = state in {ReviewState.PASSED, ReviewState.FAILED}
        projected.append(
            ContentReview(
                axis=ReviewAxis(axis),
                state=state,
                reviewed_content_hash=content_hash if completed else None,
                method=(review.method if review is not None else None) if completed else None,
            )
        )
    return tuple(projected)


def content_lifecycle(lifecycle: str) -> str:
    """Map an authoring lifecycle onto the four `lingua.content.v1` accepts."""

    return lifecycle if lifecycle in PROMOTED_LIFECYCLES else "needs-review"


def required_sample(item_count: int) -> int:
    """How many items of a stable template's batch must be inspected by hand."""

    if item_count <= 0:
        return 0
    return min(item_count, max(SAMPLE_FLOOR, math.ceil(SAMPLE_SHARE * item_count)))


def sampling_policy(*, template_maturity: str, inspected_runs: int) -> SamplingPolicy:
    """Full inspection until a template is stable and has three inspected runs."""

    if template_maturity == "stable" and inspected_runs >= FULL_INSPECTION_RUNS:
        return SamplingPolicy.SAMPLED
    return SamplingPolicy.FULL_INSPECTION


def sample_size(*, policy: SamplingPolicy, item_count: int) -> int:
    if policy is SamplingPolicy.FULL_INSPECTION:
        return item_count
    return required_sample(item_count)


__all__ = [
    "AXIS_STATES",
    "CONTENT_ORIGIN_BY_CLASS",
    "FULL_INSPECTION_RUNS",
    "PROMOTED_LIFECYCLES",
    "REVIEW_AXES",
    "UNPROMOTED_LIFECYCLES",
    "GateProblem",
    "SamplingPolicy",
    "axis_states",
    "content_lifecycle",
    "content_reviews",
    "gate_problems",
    "is_known_state",
    "required_sample",
    "required_strengths",
    "review_gate_state",
    "sample_size",
    "sampling_policy",
    "state_strength",
]
