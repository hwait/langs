"""How a lesson plan is chosen, and why each block is in it.

The planner is a scoring function plus a set of hard constraints, both pure. Two things
follow from that and are the reason it is written this way:

- **every block can be explained.** The rationale a learner or an agent reads is derived
  from the same component scores the selection used, so the explanation cannot drift
  away from the decision. The same is true of an *omission*: a high-priority candidate
  that did not make it says which constraint stopped it.
- **the constraints outrank the score.** Reserved closure time, the novelty cap, the
  requirement that new material is used productively, and "never repeat a failed task
  unchanged" are applied after ranking. A block that scores highest and breaks a
  constraint loses, and the plan says so.

Nothing here reads a database or knows a language. The service assembles candidates from
DuckDB, the planner decides, and the service writes down what it decided.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from linguawiki import session as session_policy
from linguawiki.errors import ErrorDetail, LinguaWikiError

#: Bumped when a weight or a rule below changes meaning. Stored on the sessions it
#: planned, so a plan is never mistaken for one an earlier policy would have produced.
PLANNER_VERSION = "planner.v1"

#: What each component of the score is worth. They are a policy, not a measurement, and
#: they are named so that a plan's explanation can quote them.
WEIGHTS: Mapping[str, float] = {
    "due_urgency": 1.0,
    "balance_deficit": 0.8,
    "goal_relevance": 0.6,
    "curriculum_continuity": 0.5,
    "source_continuity": 0.45,
    "uncertainty_reduction": 0.4,
    "learner_interest": 0.3,
    "transfer_value": 0.3,
    "recent_repetition": -0.7,
    "prerequisite_gap": -0.9,
    "overload_risk": -0.6,
    "novelty_excess": -0.5,
}

#: Components that count against a candidate. Kept explicit so a sign error in `WEIGHTS`
#: is a policy failure rather than a silent inversion of the ranking.
PENALTIES: tuple[str, ...] = (
    "recent_repetition",
    "prerequisite_gap",
    "overload_risk",
    "novelty_excess",
)

#: A candidate at or above this score is "high priority": if it is not selected, the
#: plan owes the reader a reason.
OMISSION_THRESHOLD = 0.35

#: The most weaknesses a closure block names. The plan's lesson behaviour is explicit
#: about this: a session ends with brief retrieval and at most three priorities.
MAXIMUM_CLOSURE_TARGETS = 3

#: How many due items saturate the urgency component, and how many blocks of one area
#: in a week saturate the repetition penalty.
DUE_SATURATION = 5.0
REPETITION_SATURATION = 3.0
#: Distance between an item's difficulty and the learner's ability at which a block is
#: as overloading as it can be.
OVERLOAD_SATURATION = 0.5


@dataclass(frozen=True, slots=True)
class CandidateTarget:
    """One knowledge item a block could work on."""

    content_id: str
    title: str
    stage: str
    #: Never encountered by this learner. Novel targets are what the cap counts.
    novel: bool
    #: Due for retrieval, by the item's own next-review time.
    due: bool
    priority: int = 0
    prerequisite_gaps: int = 0
    interest_match: bool = False
    goal_match: bool = False
    #: The outcome of the last attempt on this item, when there was one.
    last_outcome: str | None = None
    #: Whether this appearance differs from the one that failed -- a different block
    #: type, more help, or a different activity. Without it a failed task is not
    #: rescheduled, because repeating it unchanged is the one thing the plan forbids.
    variation: bool = False
    #: The item's difficulty on the same 0..1 scale as the learner's ability.
    difficulty: float | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One possible block, with the facts the score is computed from."""

    block_type: str
    #: The track's own dimension this block produces evidence about, resolved by the
    #: service from the block's dimension *kind* and the pack's declarations. Empty only
    #: for a candidate the pack cannot serve, which is then unavailable anyway.
    dimension: str = ""
    targets: tuple[CandidateTarget, ...] = ()
    #: Follow-ups and active errors this block would work through.
    due_followups: int = 0
    active_errors: int = 0
    #: Uncertainty of the estimate for this block's dimension, 0..1. A block that would
    #: reduce a wide interval is worth more than one that confirms a narrow one.
    uncertainty: float = 0.0
    #: How much of an unfinished *curriculum* unit this block continues, 0..1, read from
    #: the track's imported course position.
    curriculum_continuity: float = 0.0
    #: How much unfinished *material* this block would pick up: a book half read, a
    #: podcast series with episodes left. Scored separately from curriculum continuity
    #: because they answer different questions -- a course says what the learner is
    #: supposed to do next, and a half-finished book says what they will actually return
    #: to. Only the block areas a source can serve carry it; the rest stay at zero rather
    #: than inheriting a number about material they cannot use.
    source_continuity: float = 0.0
    #: How much this block's work carries over to other dimensions, 0..1.
    transfer_value: float = 0.0
    #: Blocks of this area completed in the last seven days.
    recent_blocks: int = 0
    #: The learner's ability on this block's dimension, 0..1, when it is known.
    ability: float | None = None
    #: Whether the workspace and the learner's equipment can run it at all.
    available: bool = True
    unavailable_reason: str | None = None

    @property
    def policy(self) -> session_policy.BlockType:
        return session_policy.block_type(self.block_type)

    @property
    def area(self) -> str:
        return self.policy.area

    @property
    def novel_targets(self) -> tuple[CandidateTarget, ...]:
        return tuple(target for target in self.targets if target.novel)

    @property
    def due_targets(self) -> tuple[CandidateTarget, ...]:
        return tuple(target for target in self.targets if target.due)


@dataclass(frozen=True, slots=True)
class Score:
    """A candidate's total and the components behind it."""

    block_type: str
    total: float
    components: Mapping[str, float]

    def contributions(self) -> tuple[tuple[str, float], ...]:
        """Components that actually moved the total, largest magnitude first."""

        moved = [(name, value * WEIGHTS[name]) for name, value in self.components.items() if value]
        return tuple(sorted(moved, key=lambda pair: (-abs(pair[1]), pair[0])))


@dataclass(frozen=True, slots=True)
class PlannedBlock:
    """One block of the chosen plan."""

    sequence: int
    role: str
    block_type: str
    area: str
    minutes: int
    dimension: str
    modality: str
    objective: str
    rationale: tuple[str, ...]
    targets: tuple[CandidateTarget, ...]
    score: float
    novel_targets: int
    difficulty: float | None = None
    #: A second block of a type already in the plan, scheduled because no other type
    #: could use the time. Variety is a preference here, not a prohibition.
    repeated: bool = False


@dataclass(frozen=True, slots=True)
class Omission:
    """A candidate that was worth scheduling and was not scheduled."""

    block_type: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    """The whole decision: blocks, what was left out, and what shaped it."""

    #: What the learner asked for. `planned_minutes` may be less, and when it is, a
    #: warning says why: a tired learner is given a shorter session, not longer blocks.
    requested_minutes: int
    mode: str
    energy: str
    planner_version: str
    lifecycle_version: str
    blocks: tuple[PlannedBlock, ...]
    omissions: tuple[Omission, ...]
    novel_target_cap: int
    novel_targets: int
    warnings: tuple[str, ...] = ()

    @property
    def planned_minutes(self) -> int:
        return sum(block.minutes for block in self.blocks)


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """What the learner asked for, and what their track makes possible."""

    minutes: int
    mode: str = session_policy.DEFAULT_MODE
    energy: str = "normal"
    intent: str | None = None
    correction_mode: str = "accuracy"
    voice_available: bool = False
    interests: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()

    def validated(self) -> PlanRequest:
        session_policy.assert_known_mode(self.mode)
        session_policy.assert_known_energy(self.energy)
        if self.correction_mode not in session_policy.CORRECTION_MODES:
            raise LinguaWikiError(
                "unknown_correction_mode",
                f"a correction mode is one of {list(session_policy.CORRECTION_MODES)}",
                details=(ErrorDetail(field="correction_mode", reason=self.correction_mode),),
            )
        return self


def _saturate(value: float, *, saturation: float) -> float:
    """Map a count onto 0..1 without letting one huge number dominate the score."""

    if saturation <= 0:
        return 0.0
    return min(1.0, max(0.0, value / saturation))


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _due_urgency(candidate: Candidate) -> float:
    due = len(candidate.due_targets) + candidate.due_followups + candidate.active_errors
    return _saturate(float(due), saturation=DUE_SATURATION)


def _goal_relevance(candidate: Candidate, request: PlanRequest) -> float:
    if not candidate.targets:
        return 0.0
    matched = sum(1 for target in candidate.targets if target.goal_match)
    intent_bonus = 0.0
    if request.intent and request.intent.strip():
        # An explicit intent is about *this* session, so it counts for the area the
        # learner named even before any item matches it by tag.
        intent_bonus = 0.5 if candidate.area in request.intent.lower() else 0.0
    return _clamp(matched / len(candidate.targets) + intent_bonus)


def _interest(candidate: Candidate) -> float:
    if not candidate.targets:
        return 0.0
    return _clamp(
        sum(1 for target in candidate.targets if target.interest_match) / len(candidate.targets)
    )


def _prerequisite_gap(candidate: Candidate) -> float:
    if not candidate.targets:
        return 0.0
    gaps = sum(target.prerequisite_gaps for target in candidate.targets)
    return _saturate(float(gaps), saturation=float(len(candidate.targets)))


def _overload_risk(candidate: Candidate) -> float:
    """How far this block sits above what the learner can currently handle."""

    if candidate.ability is None:
        return 0.0
    above = [
        target.difficulty - candidate.ability
        for target in candidate.targets
        if target.difficulty is not None and target.difficulty > candidate.ability
    ]
    if not above:
        return 0.0
    return _saturate(sum(above) / len(above), saturation=OVERLOAD_SATURATION)


def _novelty_excess(candidate: Candidate, *, remaining_novelty: int) -> float:
    """How much of this block's new material the session has no budget for."""

    novel = len(candidate.novel_targets)
    if novel == 0:
        return 0.0
    over = max(0, novel - max(0, remaining_novelty))
    return _clamp(over / novel)


def score_candidate(
    candidate: Candidate,
    *,
    request: PlanRequest,
    deficits: Mapping[str, int],
    remaining_novelty: int,
) -> Score:
    """Score one candidate block. Pure, and the only place a weight is applied."""

    components = {
        "due_urgency": _due_urgency(candidate),
        "balance_deficit": _saturate(
            float(deficits.get(candidate.area, 0)),
            saturation=float(max(1, session_policy.WEEKLY_BLOCK_TARGETS.get(candidate.area, 1))),
        ),
        "goal_relevance": _goal_relevance(candidate, request),
        "curriculum_continuity": _clamp(candidate.curriculum_continuity),
        "source_continuity": _clamp(candidate.source_continuity),
        "uncertainty_reduction": _clamp(candidate.uncertainty),
        "learner_interest": _interest(candidate),
        "transfer_value": _clamp(candidate.transfer_value),
        "recent_repetition": _saturate(
            float(candidate.recent_blocks), saturation=REPETITION_SATURATION
        ),
        "prerequisite_gap": _prerequisite_gap(candidate),
        "overload_risk": _overload_risk(candidate),
        "novelty_excess": _novelty_excess(candidate, remaining_novelty=remaining_novelty),
    }
    total = sum(value * WEIGHTS[name] for name, value in components.items())
    return Score(block_type=candidate.block_type, total=total, components=components)


#: How a component is phrased when it appears in a block's rationale.
_PHRASES: Mapping[str, str] = {
    "due_urgency": "due work is waiting",
    "balance_deficit": "this area is behind for the week",
    "goal_relevance": "it serves the learner's stated goal",
    "curriculum_continuity": "it continues where the last session stopped",
    "source_continuity": "there is material the learner started and has not finished",
    "uncertainty_reduction": "the estimate for this dimension is wide",
    "learner_interest": "the material matches a declared interest",
    "transfer_value": "the work carries over to other dimensions",
    "recent_repetition": "this area has already run several times this week",
    "prerequisite_gap": "some targets have unmet prerequisites",
    "overload_risk": "the material sits above the current level",
    "novelty_excess": "more new material than the session's novelty budget allows",
}


def explain(score: Score, *, limit: int = 3) -> tuple[str, ...]:
    """Turn the components that moved a score into readable reasons.

    Derived from the score rather than written beside it, so a block's explanation is
    always the arithmetic that selected it.
    """

    reasons: list[str] = []
    for name, contribution in score.contributions()[:limit]:
        prefix = "against: " if name in PENALTIES else ""
        reasons.append(f"{prefix}{_PHRASES[name]} ({contribution:+.2f})")
    return tuple(reasons)


def _mode_candidates(
    request: PlanRequest, candidates: Sequence[Candidate]
) -> tuple[tuple[Candidate, ...], tuple[str, ...]]:
    """The candidates an explicit mode permits, and why the others are out."""

    permitted = session_policy.MODES[request.mode]
    kept: list[Candidate] = []
    notes: list[str] = []
    for candidate in candidates:
        if candidate.block_type not in permitted:
            continue
        if candidate.policy.requires_voice and not request.voice_available:
            notes.append(
                f"{candidate.block_type} needs a voice channel, and this track has none recorded"
            )
            continue
        if not candidate.available:
            notes.append(
                f"{candidate.block_type} is unavailable: "
                f"{candidate.unavailable_reason or 'no material'}"
            )
            continue
        kept.append(candidate)
    return tuple(kept), tuple(notes)


def _objective(candidate: Candidate, *, role: str) -> str:
    """A one-line statement of what the block is for, in the plan's own terms."""

    if role == "warm-up":
        if not candidate.targets:
            return "no earlier material to retrieve yet: orient, then start the first block"
        return "retrieve what the last sessions covered before anything new"
    if role == "closure":
        if not candidate.targets:
            return "close the session: what was met today, and what to expect next time"
        return "final retrieval and the session's priority weaknesses"
    due = len(candidate.due_targets)
    novel = len(candidate.novel_targets)
    parts = []
    if due:
        parts.append(f"{due} due target(s)")
    if novel:
        parts.append(f"{novel} new target(s)")
    if candidate.active_errors:
        parts.append(f"{candidate.active_errors} active error(s)")
    if candidate.due_followups:
        parts.append(f"{candidate.due_followups} follow-up(s)")
    detail = ", ".join(parts) if parts else "consolidation"
    return f"{candidate.block_type}: {detail}"


def _trim_novelty(candidate: Candidate, *, budget: int, charged: set[str]) -> tuple[Candidate, int]:
    """Drop new targets a session has no budget left for, keeping the rest.

    The budget counts *distinct new items*, so an item already introduced in an earlier
    block is free here: meeting a new word in a grammar block and then using it in a
    speaking block is one new thing worked twice, which is the transfer the plan asks
    for -- charging it twice would starve the session of the productive block that makes
    the new material usable.

    Trimming rather than dropping the block is deliberate: the review targets in it are
    still the best use of that time, and a block silently removed teaches nobody anything.
    """

    novel = candidate.novel_targets
    fresh = [target for target in novel if target.content_id not in charged]
    if len(fresh) <= budget:
        charged.update(target.content_id for target in fresh)
        return candidate, budget - len(fresh)
    keep = {target.content_id for target in novel if target.content_id in charged}
    for target in sorted(fresh, key=lambda item: (-item.priority, item.content_id))[:budget]:
        keep.add(target.content_id)
        charged.add(target.content_id)
    trimmed = tuple(
        target for target in candidate.targets if not target.novel or target.content_id in keep
    )
    return replace(candidate, targets=trimmed), 0


def _has_work(candidate: Candidate) -> bool:
    """Whether a block still has something to do.

    Item targets are the usual answer, but a review block working through follow-ups or
    active errors is doing real work with no item of its own, so both count.
    """

    return bool(candidate.targets or candidate.due_followups or candidate.active_errors)


def _trim_to_capacity(candidate: Candidate, *, minutes: int) -> Candidate:
    """Keep only the targets a block of this length can actually work through.

    Due work first, then priority. What does not fit is not discarded: it stays out of
    the block's target list, which leaves it available for a later block instead of
    being recorded as covered when it was not.
    """

    limit = session_policy.block_target_limit(minutes)
    if len(candidate.targets) <= limit:
        return candidate
    ordered = sorted(
        candidate.targets,
        key=lambda target: (not target.due, -target.priority, target.content_id),
    )
    keep = {target.content_id for target in ordered[:limit]}
    return replace(
        candidate,
        targets=tuple(target for target in candidate.targets if target.content_id in keep),
    )


def _eligible_targets(candidate: Candidate) -> tuple[Candidate, tuple[str, ...]]:
    """Remove targets that may not be scheduled again as they are."""

    kept: list[CandidateTarget] = []
    notes: list[str] = []
    for target in candidate.targets:
        if target.last_outcome == "failure" and not target.variation:
            notes.append(
                f"{target.content_id} failed last time and is not rescheduled unchanged; "
                "it needs a different activity or more help"
            )
            continue
        kept.append(target)
    return replace(candidate, targets=tuple(kept)), tuple(notes)


def build_plan(
    *,
    request: PlanRequest,
    candidates: Sequence[Candidate],
    deficits: Mapping[str, int] | None = None,
    #: The dimension the warm-up and closure record against. The service resolves it the
    #: same way it resolves a core block's, from the pack's own declarations.
    framing_dimension: str = "",
) -> Plan:
    """Choose the blocks for one session, and record everything that shaped the choice."""

    session_policy.assert_policy_is_sound()
    validated = request.validated()
    slots = session_policy.shape_for(validated.minutes)
    core_slots = [slot for slot in slots if slot.role == "core"]
    usable_core = session_policy.core_block_limit(slots=slots, energy=validated.energy)
    cap = session_policy.novel_target_cap(core_blocks=len(core_slots), energy=validated.energy)
    warnings: list[str] = []
    omissions: list[Omission] = []
    if usable_core < len(core_slots):
        warnings.append(
            f"{validated.energy} energy limits this session to {usable_core} core block(s) "
            f"of the {len(core_slots)} its duration allows"
        )
    permitted, mode_notes = _mode_candidates(validated, candidates)
    warnings.extend(mode_notes)
    if not permitted:
        raise LinguaWikiError(
            "session_mode_unavailable",
            f"mode {validated.mode} cannot be planned: "
            + ("; ".join(mode_notes) if mode_notes else "no candidate block is available"),
            details=(
                ErrorDetail(field="mode", reason=validated.mode),
                *(ErrorDetail(field="candidate", reason=note) for note in mode_notes),
            ),
        )

    remaining_novelty = cap
    charged_novelty: set[str] = set()
    trimmed_types: list[str] = []
    scored: list[tuple[Score, Candidate]] = []
    for candidate in permitted:
        eligible, notes = _eligible_targets(candidate)
        warnings.extend(notes)
        scored.append(
            (
                score_candidate(
                    eligible,
                    request=validated,
                    deficits=deficits or {},
                    remaining_novelty=remaining_novelty,
                ),
                eligible,
            )
        )
    # Sorted by score, then by name: two candidates that tie must not depend on the
    # order the database happened to return them in.
    scored.sort(key=lambda pair: (-pair[0].total, pair[0].block_type))

    chosen: list[tuple[Score, Candidate, session_policy.Slot]] = []
    used_types: set[str] = set()
    assigned: set[str] = set()
    for score, candidate in scored:
        if len(chosen) >= usable_core:
            omissions.append(
                Omission(
                    block_type=candidate.block_type,
                    score=score.total,
                    reason="no core block left in this session's duration",
                )
            )
            continue
        if candidate.block_type in used_types:
            omissions.append(
                Omission(
                    block_type=candidate.block_type,
                    score=score.total,
                    reason="this block type is already in the plan, and variety outranks a repeat",
                )
            )
            continue
        slot = core_slots[len(chosen)]
        # Capacity before novelty, so the novelty budget is charged for the new targets
        # the block actually holds rather than for ones that never fit in it.
        sized = _trim_to_capacity(candidate, minutes=slot.minutes)
        trimmed, remaining_novelty = _trim_novelty(
            sized, budget=remaining_novelty, charged=charged_novelty
        )
        if len(trimmed.novel_targets) < len(sized.novel_targets):
            trimmed_types.append(candidate.block_type)
        if not _has_work(trimmed):
            # Everything this block could have done was either taken by an earlier block
            # or trimmed by the novelty budget. A block with nothing in it is worse than
            # a shorter session, because the learner is asked to sit through it.
            omissions.append(
                Omission(
                    block_type=candidate.block_type,
                    score=score.total,
                    reason=(
                        "nothing was left for it to work on: its targets were new material "
                        f"beyond the session's novelty budget of {cap}, or already scheduled"
                    ),
                )
            )
            continue
        used_types.add(candidate.block_type)
        assigned.update(target.content_id for target in trimmed.targets)
        chosen.append((score, trimmed, slot))

    # Variety is a preference, not a prohibition. Where the session still has core time
    # and no unused block type can fill it, a type already in the plan runs again on
    # targets it has not covered yet -- which is better than one inflated block and
    # better than handing back time the learner asked for.
    if len(chosen) < usable_core:
        chosen, repeat_notes, remaining_novelty = _fill_with_repeats(
            chosen=chosen,
            scored=scored,
            core_slots=core_slots,
            usable_core=usable_core,
            assigned=assigned,
            remaining_novelty=remaining_novelty,
            charged=charged_novelty,
        )
        warnings.extend(repeat_notes)

    chosen, productive_warnings = _require_productive_use(
        chosen=chosen, scored=scored, usable_core=usable_core
    )
    warnings.extend(productive_warnings)
    chosen, cap_notes = _enforce_novelty_cap(chosen, cap=cap)
    warnings.extend(cap_notes)
    if trimmed_types:
        # One line, not one per block: the interesting fact is the budget, and a reader
        # who sees the same sentence five times learns nothing from repetitions two to five.
        warnings.append(
            f"the session's novelty budget of {cap} new target(s) was reached, so "
            f"{', '.join(sorted(set(trimmed_types)))} carried less new material than they "
            "could have; the rest waits for the next session"
        )
    # Distinct items, not appearances: an item introduced in one block and used again in
    # another is one new thing to learn, and the cap counts things to learn.
    novel_total = len(
        {target.content_id for _, candidate, _ in chosen for target in candidate.novel_targets}
    )

    blocks: list[PlannedBlock] = []
    warm_up_targets = _framing_targets(chosen, limit=MAXIMUM_CLOSURE_TARGETS, include_new=False)
    blocks.append(
        _framing_block(
            slot=slots[0],
            block=session_policy.WARM_UP_BLOCK,
            role="warm-up",
            targets=warm_up_targets,
            dimension=framing_dimension,
        )
    )
    repeated_types: set[str] = set()
    for score, candidate, slot in chosen:
        repeated = candidate.block_type in repeated_types
        repeated_types.add(candidate.block_type)
        blocks.append(
            PlannedBlock(
                sequence=slot.sequence,
                role="core",
                block_type=candidate.block_type,
                area=candidate.area,
                minutes=slot.minutes,
                dimension=candidate.dimension,
                modality=candidate.policy.modality,
                objective=_objective(candidate, role="core"),
                rationale=explain(score)
                + (
                    ("repeated because no other block type could use this time",)
                    if repeated
                    else ()
                ),
                targets=candidate.targets,
                score=score.total,
                novel_targets=len(candidate.novel_targets),
                difficulty=_block_difficulty(candidate),
                repeated=repeated,
            )
        )
    blocks.append(
        _framing_block(
            slot=slots[-1],
            block=session_policy.CLOSURE_BLOCK,
            role="closure",
            targets=_framing_targets(chosen, limit=MAXIMUM_CLOSURE_TARGETS, include_new=True),
            dimension=framing_dimension,
        )
    )
    blocks = _renumber(blocks)
    planned = sum(block.minutes for block in blocks)
    if planned < validated.minutes:
        warnings.append(
            f"planned {planned} of the {validated.minutes} minutes requested: only "
            f"{len(chosen)} core block(s) could be filled, and stretching one of them "
            "would not be the lesson that was asked for"
        )
    plan = Plan(
        requested_minutes=validated.minutes,
        mode=validated.mode,
        energy=validated.energy,
        planner_version=PLANNER_VERSION,
        lifecycle_version=session_policy.LIFECYCLE_VERSION,
        blocks=tuple(blocks),
        omissions=tuple(omission for omission in omissions if omission.score >= OMISSION_THRESHOLD),
        novel_target_cap=cap,
        novel_targets=novel_total,
        warnings=tuple(dict.fromkeys(warnings)),
    )
    _assert_plan_is_valid(plan)
    return plan


def _block_difficulty(candidate: Candidate) -> float | None:
    known = [target.difficulty for target in candidate.targets if target.difficulty is not None]
    return sum(known) / len(known) if known else None


def _framing_targets(
    chosen: Sequence[tuple[Score, Candidate, session_policy.Slot]],
    *,
    limit: int,
    include_new: bool,
) -> tuple[CandidateTarget, ...]:
    """The targets a framing block retrieves, priority first.

    The two ends of a session are not symmetrical, and the difference is the whole
    reason this takes a flag:

    - the **warm-up** retrieves only material the learner has already met. Nothing new
      belongs there, because nothing new has been introduced yet;
    - the **closure** retrieves what the session actually worked, new material included.
      By the closing retrieval an item met in block two is no longer new, and leaving it
      out would end the session without ever asking for the thing it just taught.

    A new item that reaches the closure is carried as *not* novel, because novelty is a
    fact about a moment rather than about the item: it was introduced earlier today.
    """

    seen: dict[str, CandidateTarget] = {}
    for _, candidate, _ in chosen:
        for target in candidate.targets:
            if target.content_id in seen:
                continue
            if target.novel and not include_new:
                continue
            seen[target.content_id] = replace(target, novel=False) if target.novel else target
    ordered = sorted(seen.values(), key=lambda target: (-target.priority, target.content_id))
    return tuple(ordered[:limit])


def _framing_block(
    *,
    slot: session_policy.Slot,
    block: str,
    role: str,
    targets: tuple[CandidateTarget, ...],
    dimension: str,
) -> PlannedBlock:
    policy = session_policy.block_type(block)
    candidate = Candidate(block_type=block, dimension=dimension, targets=targets)
    return PlannedBlock(
        sequence=slot.sequence,
        role=role,
        block_type=block,
        area=policy.area,
        minutes=slot.minutes,
        dimension=dimension,
        modality=policy.modality,
        objective=_objective(candidate, role=role),
        rationale=(
            (
                "the session opens with retrieval, never with new material"
                if role == "warm-up"
                else "reserved closing retrieval and at most three priority weaknesses"
            ),
        ),
        targets=targets,
        score=0.0,
        novel_targets=0,
    )


def _require_productive_use(
    *,
    chosen: list[tuple[Score, Candidate, session_policy.Slot]],
    scored: Sequence[tuple[Score, Candidate]],
    usable_core: int,
) -> tuple[list[tuple[Score, Candidate, session_policy.Slot]], tuple[str, ...]]:
    """New material has to be used, not only met.

    When a plan introduces something new and holds no productive block, the weakest
    selected block is exchanged for the best productive candidate. Where no productive
    block is possible at all -- no microphone, no writing candidate -- the novelty is
    dropped instead, because meeting new grammar with no chance to use it is the shape
    of a lesson that feels productive and teaches nothing.
    """

    introduces = sum(len(candidate.novel_targets) for _, candidate, _ in chosen)
    if introduces == 0 or any(candidate.policy.productive for _, candidate, _ in chosen):
        return chosen, ()
    selected_types = {candidate.block_type for _, candidate, _ in chosen}
    replacement = next(
        (
            (score, candidate)
            for score, candidate in scored
            if candidate.policy.productive and candidate.block_type not in selected_types
        ),
        None,
    )
    if replacement is None:
        stripped = [
            (
                score,
                replace(candidate, targets=tuple(t for t in candidate.targets if not t.novel)),
                slot,
            )
            for score, candidate, slot in chosen
        ]
        return stripped, (
            "no productive block is possible in this session, so the new targets were "
            "dropped: new material is introduced only where the learner can use it",
        )
    weakest = min(range(len(chosen)), key=lambda index: chosen[index][0].total)
    displaced = chosen[weakest]
    slot = displaced[2]
    chosen[weakest] = (replacement[0], replacement[1], slot)
    chosen.sort(key=lambda entry: entry[2].sequence)
    if len(chosen) > usable_core:  # pragma: no cover - defensive, selection caps first
        chosen = chosen[:usable_core]
    return chosen, (
        f"{displaced[1].block_type} was exchanged for {replacement[1].block_type}: a session "
        "that introduces new material must also use it productively",
    )


def _fill_with_repeats(
    *,
    chosen: list[tuple[Score, Candidate, session_policy.Slot]],
    scored: Sequence[tuple[Score, Candidate]],
    core_slots: Sequence[session_policy.Slot],
    usable_core: int,
    assigned: set[str],
    remaining_novelty: int,
    charged: set[str],
) -> tuple[list[tuple[Score, Candidate, session_policy.Slot]], tuple[str, ...], int]:
    """Fill leftover core slots by repeating a block type on targets it has not used.

    A repeat needs *unused* targets: running the same block again on the same items is
    the monotony the score already penalises. Each block type is repeated at most once,
    which is why this is a single pass over the ranking -- three review blocks in a row
    would fill the clock and teach less than a shorter, varied session, so the rest of
    the backlog waits for the next one.
    """

    notes: list[str] = []
    budget = remaining_novelty
    for score, candidate in scored:
        if len(chosen) >= usable_core:
            break
        remaining_targets = tuple(
            target for target in candidate.targets if target.content_id not in assigned
        )
        if not remaining_targets:
            continue
        slot = core_slots[len(chosen)]
        repeat = _trim_to_capacity(
            replace(candidate, targets=remaining_targets), minutes=slot.minutes
        )
        trimmed, budget = _trim_novelty(repeat, budget=budget, charged=charged)
        if not trimmed.targets:
            continue
        assigned.update(target.content_id for target in trimmed.targets)
        chosen.append((score, trimmed, slot))
        notes.append(
            f"{candidate.block_type} runs twice in this session, on targets its first "
            "block does not cover: no other block type was available for that time"
        )
    return chosen, tuple(notes), budget


def _enforce_novelty_cap(
    chosen: list[tuple[Score, Candidate, session_policy.Slot]], *, cap: int
) -> tuple[list[tuple[Score, Candidate, session_policy.Slot]], tuple[str, ...]]:
    """Hold the novelty cap over the assembled plan, not over each step that built it.

    The budget is charged as blocks are selected, but selection is not the only thing
    that puts a block in a plan: `_require_productive_use` exchanges one, and its
    replacement was scored before any trimming. A limit enforced at every step except
    the last one is not a limit, so this is the single place the promise is kept -- over
    the whole plan, counting distinct items, in block order.
    """

    kept: set[str] = set()
    notes: list[str] = []
    result: list[tuple[Score, Candidate, session_policy.Slot]] = []
    for score, candidate, slot in chosen:
        surviving: list[CandidateTarget] = []
        for target in candidate.targets:
            if not target.novel:
                surviving.append(target)
                continue
            if target.content_id in kept or len(kept) < cap:
                kept.add(target.content_id)
                surviving.append(target)
                continue
            notes.append(
                f"{candidate.block_type} dropped a new target: the session's novelty "
                f"budget of {cap} was already spent"
            )
        result.append((score, replace(candidate, targets=tuple(surviving)), slot))
    return result, tuple(dict.fromkeys(notes))


def _renumber(blocks: Sequence[PlannedBlock]) -> list[PlannedBlock]:
    """Give the blocks contiguous sequence numbers.

    The minutes of a slot nobody could fill are *not* redistributed. A learner who
    reported low energy asked for a shorter session, and handing them a 54-minute block
    because two others were dropped is the opposite of honouring that.
    """

    return [replace(block, sequence=index + 1) for index, block in enumerate(blocks)]


def _assert_plan_is_valid(plan: Plan) -> None:
    """The invariants a plan must satisfy before anybody is asked to run it.

    Checked here rather than trusted, because every one of them has a way of being
    broken by a later change to the scoring: the minutes must be the minutes asked for,
    the closure must be last, novelty must respect its cap, and no block may appear twice.
    """

    problems: list[str] = []
    if plan.planned_minutes > plan.requested_minutes:
        problems.append(
            f"the blocks total {plan.planned_minutes} minutes for a "
            f"{plan.requested_minutes}-minute request"
        )
    if plan.planned_minutes < plan.requested_minutes and not plan.warnings:
        problems.append("a session shorter than the one requested must say why")
    if not plan.blocks:
        problems.append("a plan with no block is not a plan")
    else:
        if plan.blocks[0].role != "warm-up":
            problems.append("a session opens with a warm-up")
        if plan.blocks[-1].role != "closure":
            problems.append("a session ends with reserved closure time")
        if plan.blocks[-1].minutes < 1:
            problems.append("the closure has no time reserved for it")
    sequences = [block.sequence for block in plan.blocks]
    if sequences != list(range(1, len(plan.blocks) + 1)):
        problems.append("block sequence numbers must be contiguous and start at 1")
    counted: set[str] = set()
    for block in plan.blocks:
        if block.role != "core":
            continue
        if block.block_type in counted and not block.repeated:
            problems.append(
                f"{block.block_type} appears twice without being marked a repeat, so the "
                "plan cannot explain why"
            )
        counted.add(block.block_type)
    # One item may legitimately appear in two *different* core blocks: reading it and
    # then using it in speech are two observations, not one counted twice, and that
    # transfer is the point. What is forbidden is the same block type covering it again.
    for block in plan.blocks:
        if block.repeated and any(
            target.content_id in {t.content_id for t in earlier.targets}
            for earlier in plan.blocks
            if earlier.role == "core"
            and earlier.sequence < block.sequence
            and earlier.block_type == block.block_type
            for target in block.targets
        ):
            problems.append(
                f"the repeated {block.block_type} block covers a target its first block "
                "already covered, which is repetition rather than practice"
            )
    if plan.novel_targets > plan.novel_target_cap:
        problems.append(
            f"{plan.novel_targets} new targets exceed the session's cap of {plan.novel_target_cap}"
        )
    for block in plan.blocks:
        if block.role == "core" and not block.targets and not block.objective:
            problems.append(f"{block.block_type} is scheduled with nothing to work on")
        if block.role != "core" and block.novel_targets:
            problems.append(f"{block.block_type} frames the session and cannot introduce novelty")
        permitted = session_policy.MODES[plan.mode]
        if block.role == "core" and block.block_type not in permitted:
            problems.append(f"{block.block_type} is not permitted in {plan.mode} mode")
    if plan.novel_targets and not any(
        session_policy.block_type(block.block_type).productive for block in plan.blocks
    ):
        problems.append("new material is introduced with no productive block to use it in")
    if problems:
        raise LinguaWikiError(
            "invalid_session_plan",
            "the planner produced a plan that breaks its own constraints: " + "; ".join(problems),
            details=tuple(ErrorDetail(field="plan", reason=problem) for problem in problems),
        )


__all__ = [
    "MAXIMUM_CLOSURE_TARGETS",
    "OMISSION_THRESHOLD",
    "PENALTIES",
    "PLANNER_VERSION",
    "WEIGHTS",
    "Candidate",
    "CandidateTarget",
    "Omission",
    "Plan",
    "PlanRequest",
    "PlannedBlock",
    "Score",
    "build_plan",
    "explain",
    "score_candidate",
]
