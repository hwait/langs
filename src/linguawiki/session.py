"""What a session is, what shape it may take, and which moves are legal.

Three separate policies live here, and they are here rather than in the service because
each one is a rule a reviewer or a learner may want to read:

- the **lifecycle**: which status may follow which, so a crash leaves a state the next
  invocation can name instead of a state nobody planned for;
- the **shape** of a session: a warm-up, whole core blocks, and reserved closure time.
  A request for 47 minutes is honoured as 47 minutes rather than rounded into a lie
  about fitting neatly into thirds;
- the **constraints** on a plan: novelty caps, productive use when something new is
  introduced, incompatible activities, and fatigue.

Nothing here touches a database or names a language. Everything is a pure function of
its arguments, which is what lets the planner be property-tested at every duration the
plan asks about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from linguawiki.errors import ErrorDetail, LinguaWikiError

#: Bumped when a rule below changes meaning. Stored on every session it shaped, so a
#: plan can be told apart from one an earlier policy produced.
LIFECYCLE_VERSION = "session.v1"

# --- Lifecycle ---------------------------------------------------------------------

#: Every status a session can hold. `closing` is durable on purpose: a crash between
#: "I am finalizing" and "I finalized" has to be distinguishable from both.
STATUSES: tuple[str, ...] = (
    "planned",
    "active",
    "closing",
    "completed",
    "partial",
    "abandoned",
)

#: The statuses from which no further move is possible.
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "partial", "abandoned")

#: Which status may follow which. `closing -> closing` is legal because a close that
#: crashed mid-transaction is retried, and the retry must not be a new kind of move.
TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "planned": ("active", "abandoned"),
    "active": ("closing", "abandoned"),
    "closing": ("closing", "completed", "partial", "abandoned"),
    "completed": (),
    "partial": (),
    "abandoned": (),
}

#: The outcomes a close may materialize work under. `abandoned` is not among them: it
#: keeps staged work for audit and credits none of it.
CLOSE_OUTCOMES: tuple[str, ...] = ("completed", "partial")


def assert_known_status(status: str) -> str:
    """Refuse a status this policy has never heard of."""

    if status not in STATUSES:
        raise LinguaWikiError(
            "unknown_session_status",
            f"a session status is one of {list(STATUSES)}",
            details=(ErrorDetail(field="status", reason=status),),
        )
    return status


def assert_transition(*, current: str, target: str, session_id: str) -> str:
    """Refuse an illegal lifecycle move, naming both ends of it.

    The message says what the session *is*, because the caller is usually an agent that
    lost the thread: "this session is already completed" is actionable where "invalid
    transition" is not.
    """

    assert_known_status(current)
    assert_known_status(target)
    permitted = TRANSITIONS[current]
    if target not in permitted:
        raise LinguaWikiError(
            "invalid_session_transition",
            f"session {session_id} is {current}, and a {current} session cannot become "
            f"{target}" + (f"; it may only become {' or '.join(permitted)}" if permitted else ""),
            details=(ErrorDetail(field="status", reason=current, context={"attempted": target}),),
        )
    return target


# --- Block vocabulary --------------------------------------------------------------

#: The weekly balance areas from the plan. Vocabulary is deliberately absent: it is
#: integrated across blocks rather than given a quota of its own.
#: The modalities an attempt can be recorded under, mirroring `evidence.MODALITIES`. A
#: test keeps the two from drifting; they are separate so this module stays importable
#: without the evidence policy.
MODALITIES: tuple[str, ...] = ("text", "audio", "speech", "writing")

AREAS: tuple[str, ...] = (
    "retrieval",
    "listening",
    "speaking",
    "reading",
    "writing",
    "grammar",
    "pronunciation",
    "closure",
)

#: Minimum blocks per area in one week, from the plan's balance table. `closure` has no
#: target because it is session overhead rather than practice.
WEEKLY_BLOCK_TARGETS: Mapping[str, int] = {
    "retrieval": 2,
    "listening": 2,
    "speaking": 2,
    "reading": 1,
    "writing": 1,
    "grammar": 1,
    "pronunciation": 1,
}

#: Sessions the weekly policy expects unless the learner overrides it.
WEEKLY_SESSION_TARGET = 5


#: The kinds of skill dimension a pack may declare, from the placement policy. A block
#: names a *kind*, never a dimension: the dimension names themselves are the pack's
#: (`spoken-production` in one framework, something else in another), and a core module
#: that hard-coded them would be a core module that knows a framework.
DIMENSION_KINDS: tuple[str, ...] = ("receptive", "form", "productive", "pronunciation")


@dataclass(frozen=True, slots=True)
class BlockType:
    """One kind of block, and what it demands of the learner and their equipment."""

    name: str
    area: str
    #: The kind of dimension a block of this kind produces evidence about. The service
    #: resolves it to one of the track's own dimensions using this and the modality.
    dimension_kind: str
    #: The modality its attempts are recorded under.
    modality: str
    #: Whether the learner produces language rather than choosing or receiving it. New
    #: material may not be introduced in a session without one of these.
    productive: bool
    #: Whether it needs a working microphone or a voice channel.
    requires_voice: bool = False
    #: Whether it may carry targets the learner has never met.
    introduces_novelty: bool = True


BLOCK_TYPES: tuple[BlockType, ...] = (
    BlockType(
        name="warm-up-retrieval",
        area="retrieval",
        dimension_kind="receptive",
        modality="text",
        productive=False,
        introduces_novelty=False,
    ),
    BlockType(
        name="rich-review",
        area="retrieval",
        dimension_kind="receptive",
        modality="text",
        productive=False,
        introduces_novelty=False,
    ),
    BlockType(
        name="grammar-focus",
        area="grammar",
        dimension_kind="form",
        modality="text",
        productive=False,
    ),
    BlockType(
        name="reading",
        area="reading",
        dimension_kind="receptive",
        modality="text",
        productive=False,
    ),
    BlockType(
        name="listening",
        area="listening",
        dimension_kind="receptive",
        modality="audio",
        productive=False,
    ),
    BlockType(
        name="writing",
        area="writing",
        dimension_kind="productive",
        modality="writing",
        productive=True,
    ),
    BlockType(
        name="speaking",
        area="speaking",
        dimension_kind="productive",
        modality="speech",
        productive=True,
        requires_voice=True,
    ),
    BlockType(
        name="pronunciation",
        area="pronunciation",
        dimension_kind="pronunciation",
        modality="speech",
        productive=True,
        requires_voice=True,
    ),
    BlockType(
        name="closure-retrieval",
        area="closure",
        dimension_kind="receptive",
        modality="text",
        productive=False,
        introduces_novelty=False,
    ),
)

BLOCK_TYPES_BY_NAME: Mapping[str, BlockType] = {block.name: block for block in BLOCK_TYPES}

#: The block that opens a session and the block that closes it. Both are retrieval, and
#: neither may introduce anything new: a session starts and ends with what is already
#: half-known.
WARM_UP_BLOCK = "warm-up-retrieval"
CLOSURE_BLOCK = "closure-retrieval"

#: An explicit learner mode maps to the block types it permits in the core of the
#: session. The warm-up and closure are always retrieval, whatever the mode.
MODES: Mapping[str, tuple[str, ...]] = {
    "mixed": (
        "rich-review",
        "grammar-focus",
        "reading",
        "listening",
        "writing",
        "speaking",
        "pronunciation",
    ),
    "review": ("rich-review",),
    "grammar": ("grammar-focus", "writing"),
    "reading": ("reading", "rich-review"),
    "listening": ("listening", "rich-review"),
    "writing": ("writing", "grammar-focus"),
    "speaking": ("speaking", "pronunciation"),
    "pronunciation": ("pronunciation", "speaking"),
}

DEFAULT_MODE = "mixed"


def assert_known_mode(mode: str) -> str:
    if mode not in MODES:
        raise LinguaWikiError(
            "unknown_session_mode",
            f"a session mode is one of {sorted(MODES)}",
            details=(ErrorDetail(field="mode", reason=mode),),
        )
    return mode


def block_type(name: str) -> BlockType:
    if name not in BLOCK_TYPES_BY_NAME:
        raise LinguaWikiError(
            "unknown_block_type",
            f"a block type is one of {sorted(BLOCK_TYPES_BY_NAME)}",
            details=(ErrorDetail(field="block_type", reason=name),),
        )
    return BLOCK_TYPES_BY_NAME[name]


# --- Activities --------------------------------------------------------------------

#: Concrete things a block can consist of. The procedures themselves are the learning
#: skill's business; core only needs to know which may share a block.
ACTIVITY_KINDS: tuple[str, ...] = (
    "elicitation",
    "graduated-hints",
    "controlled-practice",
    "free-production",
    "comprehension-questions",
    "shadowing",
    "translation",
    "teach-back",
    "role-play",
    "exam-task",
    "retrieval-quiz",
)

#: Pairs that cannot share one block. Exam conditions are the whole of it: a task that
#: records silently and gives feedback afterwards cannot coexist with hint-based
#: coaching in the same block, because the learner cannot be in both regimes at once.
INCOMPATIBLE_ACTIVITIES: tuple[frozenset[str], ...] = (
    frozenset({"exam-task", "graduated-hints"}),
    frozenset({"exam-task", "elicitation"}),
    frozenset({"exam-task", "teach-back"}),
)

#: Correction modes from the plan's lesson behaviour, and the activities each forbids.
CORRECTION_MODES: tuple[str, ...] = ("fluency", "accuracy", "exam")
CORRECTION_MODE_FORBIDS: Mapping[str, tuple[str, ...]] = {
    "fluency": (),
    "accuracy": (),
    "exam": ("graduated-hints", "elicitation", "teach-back"),
}


def assert_activities_compatible(
    *, block: str, activities: Sequence[str], correction_mode: str
) -> None:
    """Refuse a block whose activities cannot be run together, or under its mode."""

    if correction_mode not in CORRECTION_MODES:
        raise LinguaWikiError(
            "unknown_correction_mode",
            f"a correction mode is one of {list(CORRECTION_MODES)}",
            details=(ErrorDetail(field="correction_mode", reason=correction_mode),),
        )
    for kind in activities:
        if kind not in ACTIVITY_KINDS:
            raise LinguaWikiError(
                "unknown_activity_kind",
                f"an activity kind is one of {list(ACTIVITY_KINDS)}",
                details=(ErrorDetail(field="activity", reason=kind),),
            )
    present = set(activities)
    for pair in INCOMPATIBLE_ACTIVITIES:
        if pair <= present:
            first, second = sorted(pair)
            raise LinguaWikiError(
                "incompatible_activities",
                f"block {block} cannot run {first} and {second} together: the learner "
                "cannot be under exam conditions and being coached at the same time",
                details=(ErrorDetail(field="activities", reason=f"{first}+{second}"),),
            )
    forbidden = sorted(present & set(CORRECTION_MODE_FORBIDS[correction_mode]))
    if forbidden:
        raise LinguaWikiError(
            "activity_forbidden_by_mode",
            f"block {block} is in {correction_mode} mode, which forbids {', '.join(forbidden)}",
            details=(ErrorDetail(field="correction_mode", reason=correction_mode),),
        )


# --- Duration shaping -------------------------------------------------------------

#: A session shorter than this cannot hold a warm-up, one useful block, and a closure.
MINIMUM_MINUTES = 20
#: Beyond this a single sitting stops being one session; the planner refuses rather
#: than quietly planning three hours of practice.
MAXIMUM_MINUTES = 180
#: Reserved for the opening retrieval, which is never the place for new material.
WARM_UP_MINUTES = 5
#: Reserved final retrieval and the closing priorities. A hard constraint from the
#: plan: this time is taken out of the budget before any core block is scheduled.
CLOSURE_MINUTES = 7
#: What a core block aims for. Longer requests add blocks; they do not stretch one.
CORE_BLOCK_MINUTES = 20
#: The plan's two-to-six band, counting the warm-up and the core blocks.
MAXIMUM_CORE_BLOCKS = 5


@dataclass(frozen=True, slots=True)
class Slot:
    """One scheduled block position: what it is for and how long it has."""

    sequence: int
    role: str
    minutes: int


def shape_for(minutes: int) -> tuple[Slot, ...]:
    """Split a duration into a warm-up, whole core blocks, and a reserved closure.

    The remainder is spread over the earliest core blocks rather than dropped, so the
    slots always sum to exactly the minutes requested. A 47-minute session is a
    47-minute session; rounding it to 40 would be a plan for a lesson nobody asked for.
    """

    if not MINIMUM_MINUTES <= minutes <= MAXIMUM_MINUTES:
        raise LinguaWikiError(
            "invalid_session_duration",
            f"a session runs between {MINIMUM_MINUTES} and {MAXIMUM_MINUTES} minutes",
            details=(ErrorDetail(field="minutes", reason=str(minutes)),),
        )
    core_budget = minutes - WARM_UP_MINUTES - CLOSURE_MINUTES
    if core_budget < 1:
        # Only reachable for the shortest sessions: give the warm-up and the closure
        # proportionally less rather than scheduling a block with no time in it.
        warm_up = max(2, minutes // 4)
        closure = max(2, minutes // 4)
        return (
            Slot(sequence=1, role="warm-up", minutes=warm_up),
            Slot(sequence=2, role="core", minutes=minutes - warm_up - closure),
            Slot(sequence=3, role="closure", minutes=closure),
        )
    core_blocks = min(MAXIMUM_CORE_BLOCKS, max(1, core_budget // CORE_BLOCK_MINUTES))
    base, remainder = divmod(core_budget, core_blocks)
    slots = [Slot(sequence=1, role="warm-up", minutes=WARM_UP_MINUTES)]
    for index in range(core_blocks):
        extra = 1 if index < remainder else 0
        slots.append(Slot(sequence=index + 2, role="core", minutes=base + extra))
    slots.append(Slot(sequence=core_blocks + 2, role="closure", minutes=CLOSURE_MINUTES))
    return tuple(slots)


# --- Novelty and fatigue ----------------------------------------------------------

#: Energy the learner reports. It shortens the plan rather than the lesson: fewer new
#: things and fewer core blocks, never a session that pretends to be longer than the
#: learner has in them.
ENERGY_LEVELS: tuple[str, ...] = ("low", "normal", "high")
#: How many core blocks each energy level allows at most.
ENERGY_CORE_LIMITS: Mapping[str, int] = {"low": 2, "normal": 5, "high": 5}
#: How much of the novelty budget each energy level keeps.
ENERGY_NOVELTY_FACTORS: Mapping[str, float] = {"low": 0.5, "normal": 1.0, "high": 1.0}

#: New targets allowed in a 20-minute session, and per additional core block.
BASE_NOVEL_TARGETS = 2
NOVEL_TARGETS_PER_CORE_BLOCK = 2
#: However long the session, this many new things is the most anyone absorbs at once.
MAXIMUM_NOVEL_TARGETS = 8


#: Minutes of a block one target realistically gets. A 20-minute block that claims ten
#: items is a list, not a plan -- and the targets it could not reach are better left for
#: another block than written down as though they were covered.
MINUTES_PER_TARGET = 5
#: However long a block, this many targets is the most it can hold usefully.
MAXIMUM_BLOCK_TARGETS = 6


def block_target_limit(minutes: int) -> int:
    """How many targets one block of this length can actually work through."""

    return max(1, min(MAXIMUM_BLOCK_TARGETS, minutes // MINUTES_PER_TARGET))


def assert_known_energy(energy: str) -> str:
    if energy not in ENERGY_LEVELS:
        raise LinguaWikiError(
            "unknown_energy_level",
            f"an energy level is one of {list(ENERGY_LEVELS)}",
            details=(ErrorDetail(field="energy", reason=energy),),
        )
    return energy


def novel_target_cap(*, core_blocks: int, energy: str = "normal") -> int:
    """The most previously unseen targets one session may introduce."""

    assert_known_energy(energy)
    budget = BASE_NOVEL_TARGETS + NOVEL_TARGETS_PER_CORE_BLOCK * max(0, core_blocks - 1)
    scaled = int(budget * ENERGY_NOVELTY_FACTORS[energy])
    return max(0, min(MAXIMUM_NOVEL_TARGETS, scaled))


def core_block_limit(*, slots: Sequence[Slot], energy: str = "normal") -> int:
    """How many core slots the learner's reported energy leaves usable."""

    assert_known_energy(energy)
    available = sum(1 for slot in slots if slot.role == "core")
    return max(1, min(available, ENERGY_CORE_LIMITS[energy]))


def assert_policy_is_sound() -> None:
    """Refuse a policy that contradicts itself, before it can shape a session.

    Two of these caught real mistakes while this module was being written: a closure
    reserve larger than the minimum session, which made every short plan illegal, and a
    weekly target naming an area no block type produces, which no session could ever
    satisfy.
    """

    problems: list[str] = []
    if WARM_UP_MINUTES + CLOSURE_MINUTES >= MINIMUM_MINUTES:
        problems.append("the reserved warm-up and closure leave no time in the shortest session")
    areas_with_blocks = {block.area for block in BLOCK_TYPES}
    for area in WEEKLY_BLOCK_TARGETS:
        if area not in areas_with_blocks:
            problems.append(f"weekly target names {area}, which no block type produces")
        if area not in AREAS:
            problems.append(f"weekly target names {area}, which is not a known area")
    for block in BLOCK_TYPES:
        if block.area not in AREAS:
            problems.append(f"block {block.name} names unknown area {block.area}")
        if block.dimension_kind not in DIMENSION_KINDS:
            problems.append(
                f"block {block.name} names unknown dimension kind {block.dimension_kind}"
            )
        if block.modality not in MODALITIES:
            problems.append(f"block {block.name} names unknown modality {block.modality}")
    if not any(BLOCK_TYPES_BY_NAME[name].productive for name in MODES[DEFAULT_MODE]):
        problems.append(
            "the default mode permits no productive block, so nothing new can be taught"
        )
    for mode, permitted in MODES.items():
        for name in permitted:
            if name not in BLOCK_TYPES_BY_NAME:
                problems.append(f"mode {mode} permits unknown block type {name}")
    for name in (WARM_UP_BLOCK, CLOSURE_BLOCK):
        if BLOCK_TYPES_BY_NAME[name].introduces_novelty:
            problems.append(f"{name} may introduce novelty, but it frames the session")
    if sum(WEEKLY_BLOCK_TARGETS.values()) < WEEKLY_SESSION_TARGET:
        problems.append("the weekly area targets cannot fill the expected sessions")
    for status, targets in TRANSITIONS.items():
        assert_known_status(status)
        for target in targets:
            assert_known_status(target)
    for outcome in CLOSE_OUTCOMES:
        if outcome not in TRANSITIONS["closing"]:
            problems.append(f"a closing session cannot become {outcome}")
    if problems:
        raise LinguaWikiError(
            "unsound_session_policy",
            "the session policy contradicts itself: " + "; ".join(problems),
            details=tuple(ErrorDetail(field="policy", reason=problem) for problem in problems),
        )


__all__ = [
    "ACTIVITY_KINDS",
    "AREAS",
    "BLOCK_TYPES",
    "BLOCK_TYPES_BY_NAME",
    "CLOSE_OUTCOMES",
    "CLOSURE_BLOCK",
    "CLOSURE_MINUTES",
    "CORE_BLOCK_MINUTES",
    "DEFAULT_MODE",
    "DIMENSION_KINDS",
    "ENERGY_LEVELS",
    "LIFECYCLE_VERSION",
    "MAXIMUM_MINUTES",
    "MAXIMUM_NOVEL_TARGETS",
    "MINIMUM_MINUTES",
    "MODES",
    "STATUSES",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "WARM_UP_BLOCK",
    "WARM_UP_MINUTES",
    "WEEKLY_BLOCK_TARGETS",
    "BlockType",
    "Slot",
    "assert_activities_compatible",
    "assert_known_energy",
    "assert_known_mode",
    "assert_known_status",
    "assert_policy_is_sound",
    "assert_transition",
    "block_target_limit",
    "block_type",
    "core_block_limit",
    "novel_target_cap",
    "shape_for",
]
