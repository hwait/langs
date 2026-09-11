"""Placement algorithm v1: a transparent ordinal Bayesian staircase.

The whole model is here, in arithmetic a reader can follow, because an opaque estimate is
useless to a learner who wants to know why the system thinks they are at A2. Every step is
recorded so a run can be replayed:

1. Framework levels sit on an ordered numeric grid, one unit per band, with half-band
   resolution so reviewed half-band item difficulties mean something.
2. The prior is centred on the declared level with a spread of one band, or broad when no
   level was declared.
3. A task is chosen near the posterior median, maximising expected uncertainty reduction,
   subject to content-family diversity, modality availability, and exposure limits.
4. A score in `[0, 1]` folds in as `p ** s * (1 - p) ** (1 - s)` with
   `p = 1 / (1 + exp(-1.7 * (ability - difficulty)))`, then the posterior is normalised.
5. A dimension stops only when its budget, coverage, precision, and boundary-probe
   conditions all hold; otherwise it stops at the maximum budget or on request, and says
   so with a confidence label rather than being forced into a precise band.

The response curve's parameters are fixed and expert-authored in v1. Calibrating them
from aggregate usage would be a new, explicitly versioned model — not an edit here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

ALGORITHM_VERSION = "placement.v1"
#: Logistic slope of the v1 response curve. Fixed by design.
DISCRIMINATION = 1.7
#: Grid resolution in bands. Half a band is the finest reviewed item difficulty.
GRID_STEP = 0.5
#: Standard deviation, in bands, of a prior centred on a declared level.
DECLARED_PRIOR_SPREAD = 1.0
#: Posterior mass that must fall inside one adjacent band before a dimension may stop.
PRECISION_MASS = 0.8
#: Width, in bands, of the credible interval the precision rule allows.
PRECISION_WIDTH = 1.0
#: Content families or task types a dimension's evidence must span.
MINIMUM_FAMILIES = 2
#: Months an ordinary placement item stays unavailable to the same learner.
REUSE_WINDOW_MONTHS = 6

#: Minimum and maximum task budgets per dimension kind.
BUDGETS: Mapping[str, tuple[int, int]] = {
    "receptive": (6, 12),
    "form": (6, 12),
    "productive": (3, 5),
    "pronunciation": (4, 8),
}
#: Connected-speech samples a pronunciation dimension needs beyond its targets.
CONNECTED_SPEECH_BUDGET = (1, 2)
#: Task types each dimension kind is scored from.
TASK_TYPES: Mapping[str, tuple[str, ...]] = {
    "receptive": ("objective", "short-response"),
    "form": ("objective", "short-response"),
    "productive": ("extended-productive",),
    "pronunciation": ("pronunciation-target", "connected-speech"),
}
#: Modalities each dimension kind needs available before it can be tested.
REQUIRED_MODALITIES: Mapping[str, tuple[str, ...]] = {
    "receptive": ("text", "audio"),
    "form": ("text",),
    "productive": ("speech", "writing"),
    "pronunciation": ("speech",),
}


def budget_for(dimension_kind: str) -> tuple[int, int]:
    try:
        return BUDGETS[dimension_kind]
    except KeyError as exc:
        raise ValueError(f"unknown dimension kind: {dimension_kind}") from exc


def ability_grid(level_count: int, *, step: float = GRID_STEP) -> tuple[float, ...]:
    """The ordered ability grid for a framework with `level_count` levels."""

    if level_count < 2:
        raise ValueError("a framework needs at least two levels to place a learner on")
    points = round((level_count - 1) / step) + 1
    return tuple(round(index * step, 6) for index in range(points))


def success_probability(ability: float, difficulty: float) -> float:
    """The v1 response curve, clamped away from 0 and 1 so a score is never fatal."""

    probability = 1.0 / (1.0 + math.exp(-DISCRIMINATION * (ability - difficulty)))
    return min(max(probability, 1e-9), 1.0 - 1e-9)


def broad_prior(grid: Sequence[float]) -> tuple[float, ...]:
    """A uniform prior, used when the learner declares no level."""

    weight = 1.0 / len(grid)
    return tuple(weight for _ in grid)


def declared_prior(
    grid: Sequence[float], *, centre: float, spread: float = DECLARED_PRIOR_SPREAD
) -> tuple[float, ...]:
    """A prior centred on a declared level with a spread of about one band.

    A declared level is a starting hypothesis, so the prior is deliberately wide: it
    changes which task is asked first, not what the result may conclude.
    """

    weights = [math.exp(-0.5 * ((point - centre) / spread) ** 2) for point in grid]
    total = sum(weights)
    return tuple(weight / total for weight in weights)


def normalize(weights: Sequence[float]) -> tuple[float, ...]:
    total = sum(weights)
    if total <= 0:
        return broad_prior(weights)
    return tuple(weight / total for weight in weights)


def update_posterior(
    grid: Sequence[float],
    prior: Sequence[float],
    *,
    difficulty: float,
    score: float,
    weight: float = 1.0,
) -> tuple[float, ...]:
    """Fold one fractional score into the ability distribution.

    `weight` tempers the observation: the likelihood is raised to that power, so a
    heavily hinted, AI-scored, or long-decayed observation moves the posterior less than
    a clean one without being discarded. A bank task served by the placement staircase
    always weighs 1.0; evidence recorded outside a run does not.
    """

    if not 0.0 <= score <= 1.0:
        raise ValueError("a task score must lie in [0, 1]")
    if not 0.0 < weight <= 1.0:
        raise ValueError("an observation weight must lie in (0, 1]")
    updated = []
    for point, mass in zip(grid, prior, strict=True):
        probability = success_probability(point, difficulty)
        likelihood = probability**score * (1.0 - probability) ** (1.0 - score)
        updated.append(mass * likelihood**weight)
    return normalize(updated)


def posterior_mean(grid: Sequence[float], posterior: Sequence[float]) -> float:
    return sum(point * weight for point, weight in zip(grid, posterior, strict=True))


def posterior_median(grid: Sequence[float], posterior: Sequence[float]) -> float:
    cumulative = 0.0
    for point, weight in zip(grid, posterior, strict=True):
        cumulative += weight
        if cumulative >= 0.5:
            return point
    return grid[-1]


def posterior_sd(grid: Sequence[float], posterior: Sequence[float]) -> float:
    mean = posterior_mean(grid, posterior)
    variance = sum(
        weight * (point - mean) ** 2 for point, weight in zip(grid, posterior, strict=True)
    )
    return math.sqrt(variance)


def credible_interval(
    grid: Sequence[float], posterior: Sequence[float], *, mass: float = PRECISION_MASS
) -> tuple[float, float]:
    """The narrowest contiguous interval holding at least `mass` of the posterior."""

    best: tuple[float, float] | None = None
    best_width = math.inf
    for start in range(len(grid)):
        total = 0.0
        for end in range(start, len(grid)):
            total += posterior[end]
            if total >= mass:
                width = grid[end] - grid[start]
                if width < best_width:
                    best_width = width
                    best = (grid[start], grid[end])
                break
    return best if best is not None else (grid[0], grid[-1])


def entropy(posterior: Sequence[float]) -> float:
    return -sum(weight * math.log(weight) for weight in posterior if weight > 0.0)


def expected_posterior_entropy(
    grid: Sequence[float], posterior: Sequence[float], *, difficulty: float
) -> float:
    """Expected entropy after asking a task of this difficulty.

    Averaged over the two outcomes the learner could produce, weighted by how likely
    the current posterior thinks each is. Lower is a more informative task.
    """

    probability_correct = sum(
        weight * success_probability(point, difficulty)
        for point, weight in zip(grid, posterior, strict=True)
    )
    correct = update_posterior(grid, posterior, difficulty=difficulty, score=1.0)
    incorrect = update_posterior(grid, posterior, difficulty=difficulty, score=0.0)
    return probability_correct * entropy(correct) + (1.0 - probability_correct) * entropy(incorrect)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One selectable bank item, with everything selection is allowed to consider."""

    content_id: str
    dimension: str
    task_type: str
    difficulty: float
    content_family: str
    modality: str
    level_code: str
    is_anchor: bool


@dataclass(frozen=True, slots=True)
class Selection:
    candidate: Candidate
    expected_entropy: float
    reason: str


@dataclass(frozen=True, slots=True)
class DimensionState:
    """The persisted state of one dimension inside a placement or calibration run."""

    dimension: str
    dimension_kind: str
    grid: tuple[float, ...]
    posterior: tuple[float, ...]
    prior: tuple[float, ...]
    minimum_tasks: int
    maximum_tasks: int
    tasks_used: int = 0
    families: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    probed_above: bool = False
    probed_below: bool = False
    connected_speech_used: int = 0
    status: str = "open"
    stop_reason: str | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def boundary_probed(self) -> bool:
        return self.probed_above or self.probed_below

    @property
    def coverage(self) -> int:
        """How broadly this dimension's evidence spreads.

        The *wider* of the two counts, not their union: a union treats one family plus
        one task type as two kinds of coverage, which every single-family run satisfies
        for free. The rule is that the evidence spans two content families, or two task
        types -- not that it has one of each.
        """

        return max(len(set(self.families)), len(set(self.task_types)))


def initial_state(
    *,
    dimension: str,
    dimension_kind: str,
    level_count: int,
    declared_index: float | None,
) -> DimensionState:
    grid = ability_grid(level_count)
    prior = (
        broad_prior(grid) if declared_index is None else declared_prior(grid, centre=declared_index)
    )
    minimum, maximum = budget_for(dimension_kind)
    return DimensionState(
        dimension=dimension,
        dimension_kind=dimension_kind,
        grid=grid,
        prior=prior,
        posterior=prior,
        minimum_tasks=minimum,
        maximum_tasks=maximum,
    )


def at_framework_edge(state: DimensionState, *, level_count: int) -> bool:
    """Whether the *estimate* sits in the framework's lowest or highest band.

    The exemption from the boundary probe belongs to an estimate that has nowhere further
    to be probed. Reading it off the last task's own level label instead let a single
    edge-labelled task excuse the probe for an estimate sitting in the middle of the
    framework, which is where a probe matters most.
    """

    if state.tasks_used == 0:
        return False
    index = _level_index(posterior_median(state.grid, state.posterior), level_count)
    return index in (0, level_count - 1)


def stop_decision(state: DimensionState, *, level_count: int) -> tuple[bool, str | None]:
    """Whether this dimension may stop, and why.

    All four conditions must hold: the minimum budget, evidence across at least two
    content families or task types, at least 80% of the posterior inside one adjacent
    band, and a boundary probe above or below the estimate unless the estimate sits at
    the edge of the framework.
    """

    if state.tasks_used >= state.maximum_tasks:
        return True, "maximum-budget"
    if state.tasks_used < state.minimum_tasks:
        return False, None
    if state.coverage < MINIMUM_FAMILIES:
        return False, None
    low, high = credible_interval(state.grid, state.posterior)
    if high - low > PRECISION_WIDTH:
        return False, None
    if not at_framework_edge(state, level_count=level_count) and not state.boundary_probed:
        return False, None
    if (
        state.dimension_kind == "pronunciation"
        and state.connected_speech_used < CONNECTED_SPEECH_BUDGET[0]
    ):
        return False, None
    return True, "precision-reached"


def confidence_label(state: DimensionState) -> str:
    """A low, medium, or high label; never a precise band with no evidence behind it."""

    if state.status == "not-tested":
        return "not-tested"
    low, high = credible_interval(state.grid, state.posterior)
    width = high - low
    if state.stop_reason == "precision-reached" and state.coverage >= MINIMUM_FAMILIES:
        return "high" if width <= PRECISION_WIDTH / 2 else "medium"
    if width <= PRECISION_WIDTH and state.tasks_used >= state.minimum_tasks:
        return "medium"
    return "low"


def estimated_level(
    state: DimensionState, levels: Sequence[str]
) -> tuple[str | None, str | None, str | None]:
    """The estimated band and its credible range, as framework level labels."""

    if state.tasks_used == 0:
        return None, None, None
    median = posterior_median(state.grid, state.posterior)
    low, high = credible_interval(state.grid, state.posterior)
    # The bounds round outwards. Rounding them to the nearest band reported a range
    # narrower than the evidence supports -- a half-band interval became a single band.
    return (
        levels[_level_index(median, len(levels))],
        levels[_level_index(math.floor(low), len(levels))],
        levels[_level_index(math.ceil(high), len(levels))],
    )


def _level_index(point: float, level_count: int) -> int:
    return min(max(round(point), 0), level_count - 1)


def select_task(
    state: DimensionState,
    candidates: Sequence[Candidate],
    *,
    available_modalities: Sequence[str],
    excluded: Sequence[str] = (),
) -> Selection | None:
    """Choose the next task for one dimension, or None when the bank cannot serve it.

    Selection is deliberately layered: eligibility first (modality, exposure, task type),
    then the diversity and boundary-probe duties the stop rule will later demand, and only
    then informativeness. Choosing the most informative eligible task first would keep
    asking from one content family and never satisfy the coverage condition.
    """

    excluded_ids = set(excluded)
    allowed_types = TASK_TYPES[state.dimension_kind]
    modalities = set(available_modalities)
    eligible = [
        candidate
        for candidate in candidates
        if candidate.dimension == state.dimension
        and candidate.task_type in allowed_types
        and candidate.modality in modalities
        and candidate.content_id not in excluded_ids
    ]
    if not eligible:
        return None
    if (
        state.dimension_kind == "pronunciation"
        and state.connected_speech_used < CONNECTED_SPEECH_BUDGET[0]
        and state.tasks_used >= state.minimum_tasks - 1
    ):
        connected = [
            candidate for candidate in eligible if candidate.task_type == "connected-speech"
        ]
        if connected:
            eligible = connected
    median = posterior_median(state.grid, state.posterior)
    needs_family = state.coverage < MINIMUM_FAMILIES and state.tasks_used > 0
    fresh_family = [
        candidate for candidate in eligible if candidate.content_family not in state.families
    ]
    if needs_family and fresh_family:
        eligible = fresh_family
        reason = "content-family diversity"
    elif state.tasks_used >= state.minimum_tasks and not state.boundary_probed:
        probes = [
            candidate
            for candidate in eligible
            if abs(candidate.difficulty - median) >= PRECISION_WIDTH / 2
        ]
        eligible = probes or eligible
        reason = "boundary probe" if probes else "informativeness"
    else:
        reason = "informativeness"
    scored = [
        (
            expected_posterior_entropy(state.grid, state.posterior, difficulty=c.difficulty),
            abs(c.difficulty - median),
            c.content_id,
            c,
        )
        for c in eligible
    ]
    scored.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    best = scored[0]
    return Selection(candidate=best[3], expected_entropy=best[0], reason=reason)


def record_score(
    state: DimensionState, candidate: Candidate, *, score: float, level_count: int
) -> DimensionState:
    """Fold a scored task into a dimension's state and re-evaluate the stop rule."""

    posterior = update_posterior(
        state.grid, state.posterior, difficulty=candidate.difficulty, score=score
    )
    median = posterior_median(state.grid, state.posterior)
    updated = DimensionState(
        dimension=state.dimension,
        dimension_kind=state.dimension_kind,
        grid=state.grid,
        prior=state.prior,
        posterior=posterior,
        minimum_tasks=state.minimum_tasks,
        maximum_tasks=state.maximum_tasks,
        tasks_used=state.tasks_used + 1,
        families=tuple(dict.fromkeys((*state.families, candidate.content_family))),
        task_types=tuple(dict.fromkeys((*state.task_types, candidate.task_type))),
        probed_above=state.probed_above or candidate.difficulty >= median + PRECISION_WIDTH / 2,
        probed_below=state.probed_below or candidate.difficulty <= median - PRECISION_WIDTH / 2,
        connected_speech_used=state.connected_speech_used
        + (1 if candidate.task_type == "connected-speech" else 0),
        status=state.status,
        stop_reason=state.stop_reason,
        warnings=state.warnings,
    )
    stop, reason = stop_decision(updated, level_count=level_count)
    if not stop:
        return updated
    return DimensionState(
        dimension=updated.dimension,
        dimension_kind=updated.dimension_kind,
        grid=updated.grid,
        prior=updated.prior,
        posterior=updated.posterior,
        minimum_tasks=updated.minimum_tasks,
        maximum_tasks=updated.maximum_tasks,
        tasks_used=updated.tasks_used,
        families=updated.families,
        task_types=updated.task_types,
        probed_above=updated.probed_above,
        probed_below=updated.probed_below,
        connected_speech_used=updated.connected_speech_used,
        status="stopped",
        stop_reason=reason,
        warnings=updated.warnings,
    )


def close_dimension(state: DimensionState, *, reason: str) -> DimensionState:
    """Stop a dimension for a reason outside the algorithm: fatigue, request, or a gap."""

    return DimensionState(
        dimension=state.dimension,
        dimension_kind=state.dimension_kind,
        grid=state.grid,
        prior=state.prior,
        posterior=state.posterior,
        minimum_tasks=state.minimum_tasks,
        maximum_tasks=state.maximum_tasks,
        tasks_used=state.tasks_used,
        families=state.families,
        task_types=state.task_types,
        probed_above=state.probed_above,
        probed_below=state.probed_below,
        connected_speech_used=state.connected_speech_used,
        status="not-tested" if state.tasks_used == 0 else "stopped",
        stop_reason=reason,
        warnings=state.warnings,
    )


def unavailable_reason(
    *, dimension_kind: str, available_modalities: Sequence[str], bank_size: int
) -> str | None:
    """Why a dimension cannot be tested at all, if it cannot.

    An untestable dimension is `not-tested`, never failed: a learner who owns no
    microphone has not failed a speaking test.
    """

    required = REQUIRED_MODALITIES[dimension_kind]
    if not set(required) & set(available_modalities):
        return f"needs one of the modalities {list(required)}"
    if bank_size == 0:
        return "the pack has no reviewed task for this dimension"
    return None


__all__ = [
    "ALGORITHM_VERSION",
    "BUDGETS",
    "CONNECTED_SPEECH_BUDGET",
    "MINIMUM_FAMILIES",
    "PRECISION_MASS",
    "PRECISION_WIDTH",
    "REUSE_WINDOW_MONTHS",
    "TASK_TYPES",
    "Candidate",
    "DimensionState",
    "Selection",
    "ability_grid",
    "broad_prior",
    "budget_for",
    "close_dimension",
    "confidence_label",
    "credible_interval",
    "declared_prior",
    "estimated_level",
    "expected_posterior_entropy",
    "initial_state",
    "posterior_mean",
    "posterior_median",
    "posterior_sd",
    "record_score",
    "select_task",
    "stop_decision",
    "success_probability",
    "unavailable_reason",
    "update_posterior",
]
