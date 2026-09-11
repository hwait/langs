"""What a source is, what may be kept of it, and what working through it proves.

A source is the learner's own catalogue entry for material somebody else made: a book
they own, a podcast they subscribe to, a video, a conversation they had. Three rules
shape everything here, and each exists because the obvious alternative is wrong:

- **A source is not pack content.** Pack content is authored, reviewed, and licensed for
  redistribution; a source is somebody else's work the learner has access to. They never
  convert into one another, and a knowledge item extracted while reading is the learner's
  own note *about* the source, not a copy of it.
- **Only short quotations, and only where the rights allow.** The excerpt cap is a hard
  limit in the schema, not a guideline, because "I'll keep just this chapter" is how a
  learning workspace becomes an unlicensed copy of a book.
- **Unaided comprehension is a fact about a first encounter.** You cannot un-see a
  translation, so the order of observations is the evidence: what the learner understood
  before help is the measurement, and what they understood after it measures the help.

Nothing here touches a database or names a language.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

from linguawiki.errors import ErrorDetail, LinguaWikiError

#: Bumped when a rule below changes meaning. Stored on the progress rows it produced, so
#: a coverage figure can be told apart from one an earlier policy computed.
SOURCE_POLICY_VERSION = "source.v1"

#: The kinds of material a learner can work through. Deliberately broad: the plan asks
#: for course, book, article, podcast, video, conversation, and the learner's own
#: material, and a kind this list cannot express would be a kind that has to be lied
#: about to be catalogued.
SOURCE_KINDS: tuple[str, ...] = (
    "course",
    "curriculum",
    "book",
    "article",
    "podcast",
    "video",
    "conversation",
    "learner-created",
)

#: Which session areas a source of each kind can actually serve. A half-read novel is a
#: reason to plan reading and nothing else; proposing a pronunciation block because a book
#: is unfinished would be an argument the learner cannot follow. `conversation` covers both
#: directions because a recorded conversation is material to listen back to as well as
#: evidence of speaking.
AREAS_FOR_KIND: dict[str, tuple[str, ...]] = {
    "course": ("reading", "grammar"),
    "curriculum": ("reading", "grammar"),
    "book": ("reading",),
    "article": ("reading",),
    "podcast": ("listening",),
    "video": ("listening",),
    "conversation": ("listening", "speaking"),
    "learner-created": ("writing",),
}

#: Kinds whose units are positions in time rather than in text. It decides what a
#: position *means*, so it is policy rather than presentation.
TIMED_KINDS: tuple[str, ...] = ("podcast", "video", "conversation")

#: The lifecycle from the plan. `rejected` is reachable only from `cataloged`: a source
#: the learner has already worked through is archived, never rejected, because rejecting
#: it would orphan the evidence it produced.
SOURCE_STATUSES: tuple[str, ...] = (
    "proposed",
    "cataloged",
    "reviewed",
    "active",
    "completed",
    "archived",
    "rejected",
)

SOURCE_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "proposed": ("cataloged", "rejected"),
    "cataloged": ("reviewed", "active", "rejected", "archived"),
    "reviewed": ("active", "archived"),
    "active": ("completed", "archived"),
    "completed": ("active", "archived"),
    "archived": ("active",),
    "rejected": (),
}

#: What the learner may keep of the source's own words, strongest permission last. This
#: is about *rights*, not about consent: consent governs the learner's words, and this
#: governs somebody else's.
RIGHTS_CLASSES: tuple[str, ...] = (
    #: Nothing may be stored but metadata: the title, where to find it, and the
    #: learner's own notes. The default, because assuming less is the safe direction.
    "metadata-only",
    #: A short quotation may be kept to justify an observation -- fair-dealing sized,
    #: not a chapter.
    "short-excerpt",
    #: The learner holds the rights, or the licence permits local storage in full:
    #: their own recording, their own writing, a public-domain or openly licensed text.
    "full-local",
)

#: The longest quotation a `short-excerpt` source may keep. Long enough to justify an
#: observation, short enough that a workspace full of them is not a copy of the work.
EXCERPT_LIMIT = 300
#: What `full-local` may hold in the database itself. Anything larger belongs in an
#: artifact outside DuckDB, which is what the artifact table is for.
FULL_LOCAL_LIMIT = 20_000

#: How the learner is working through the material. The distinction is pedagogical and
#: it changes what the progress rows mean, so it belongs here rather than in a flag.
STUDY_MODES: tuple[str, ...] = (
    #: A short passage worked closely: several observations and corrections from a small
    #: amount of text, and extracted items are expected.
    "intensive",
    #: A longer passage for gist and volume: broad progress and comprehension, few or no
    #: extracted items, and no line-by-line correction.
    "extensive",
)

#: Whether the learner had help, and what kind. The order matters: each level is more
#: help than the one before it, and a later observation may not claim a lower one.
COMPREHENSION_AIDS: tuple[str, ...] = (
    "unaided",
    "glossed",
    "subtitled",
    "translated",
    "explained",
)

#: How much of the unit the learner reports understanding. A band rather than a
#: percentage: a learner reporting "about half" to the nearest per cent is inventing
#: precision, and the aggregation would then be arithmetic on a guess.
COMPREHENSION_BANDS: tuple[str, ...] = ("none", "little", "gist", "most", "full")

#: The band a unit must reach unaided before it counts as comprehended without help.
UNAIDED_THRESHOLD = "gist"

#: Progress through a source. `abandoned` is not a failure: a book the learner stopped
#: reading is information about the book as much as about them.
PROGRESS_STATUSES: tuple[str, ...] = (
    "not-started",
    "in-progress",
    "completed",
    "abandoned",
)


def assert_known(value: str, *, vocabulary: Sequence[str], field: str, code: str) -> str:
    """Refuse a value outside a vocabulary, naming the field and what is allowed."""

    if value not in vocabulary:
        raise LinguaWikiError(
            code,
            f"{value} is not a valid {field}; expected one of {list(vocabulary)}",
            details=(ErrorDetail(field=field, reason=value),),
        )
    return value


def assert_source_transition(*, current: str, target: str, source_id: str) -> str:
    """Refuse a lifecycle move a source cannot make, saying what it is."""

    assert_known(current, vocabulary=SOURCE_STATUSES, field="status", code="unknown_source_status")
    assert_known(target, vocabulary=SOURCE_STATUSES, field="status", code="unknown_source_status")
    permitted = SOURCE_TRANSITIONS[current]
    if target not in permitted:
        raise LinguaWikiError(
            "invalid_source_transition",
            f"source {source_id} is {current} and cannot become {target}"
            + (f"; it may only become {' or '.join(permitted)}" if permitted else ""),
            details=(ErrorDetail(field="status", reason=current, context={"attempted": target}),),
        )
    return target


def excerpt_limit(rights: str) -> int:
    """How much of the source's own words this rights class permits keeping."""

    assert_known(rights, vocabulary=RIGHTS_CLASSES, field="rights", code="unknown_rights_class")
    if rights == "metadata-only":
        return 0
    if rights == "short-excerpt":
        return EXCERPT_LIMIT
    return FULL_LOCAL_LIMIT


def assert_excerpt_permitted(excerpt: str | None, *, rights: str, reference: str) -> str | None:
    """Refuse to store more of somebody else's work than its rights allow.

    Refused rather than truncated, and the difference matters: a silently shortened
    quotation looks like the learner chose its length, and the next reader cannot tell
    that the source's rights are the reason it stops where it does.
    """

    if excerpt is None:
        return None
    limit = excerpt_limit(rights)
    if limit == 0:
        raise LinguaWikiError(
            "excerpt_not_permitted",
            f"{reference} is catalogued as metadata-only, so none of its text may be "
            "stored; record the learner's own note about it instead, or catalogue the "
            "source with the rights that actually apply",
            details=(ErrorDetail(field="rights", reason=rights),),
        )
    if len(excerpt) > limit:
        raise LinguaWikiError(
            "excerpt_too_long",
            f"{reference} permits at most {limit} characters of its own text and this "
            f"excerpt is {len(excerpt)}; quote the part that justifies the observation",
            details=(
                ErrorDetail(field="excerpt", reason=str(len(excerpt))),
                ErrorDetail(field="rights", reason=rights),
            ),
        )
    return excerpt


def aid_strength(aid: str) -> int:
    """How much help a comprehension observation had, as an ordinal."""

    assert_known(aid, vocabulary=COMPREHENSION_AIDS, field="aid", code="unknown_comprehension_aid")
    return COMPREHENSION_AIDS.index(aid)


def band_strength(band: str) -> int:
    assert_known(
        band, vocabulary=COMPREHENSION_BANDS, field="band", code="unknown_comprehension_band"
    )
    return COMPREHENSION_BANDS.index(band)


@dataclass(frozen=True, slots=True)
class Comprehension:
    """One report of how much of a unit the learner understood, and with what help."""

    aid: str
    band: str
    #: Ordinal position among the observations of this unit, from 1. The order is the
    #: evidence: a learner cannot un-see a translation, so what they understood *first*
    #: is the measurement and everything after it measures the help.
    sequence: int


def assert_observation_order(observations: Sequence[Comprehension], *, reference: str) -> None:
    """Refuse a history in which help was withdrawn.

    Aid only ever increases within one unit. A learner who read a passage translated and
    then claims an *unaided* reading of it is not reporting a second observation; they
    are reporting the first one with the help left out. Recording that would make the
    strongest kind of comprehension evidence the easiest kind to manufacture.
    """

    ordered = sorted(observations, key=lambda entry: entry.sequence)
    for previous, current in pairwise(ordered):
        if aid_strength(current.aid) < aid_strength(previous.aid):
            raise LinguaWikiError(
                "comprehension_aid_regressed",
                f"{reference} was already worked with {previous.aid} help, so a later "
                f"{current.aid} reading of it is not an unaided one -- record the first "
                "encounter before offering help, or work a unit the learner has not seen",
                details=(
                    ErrorDetail(field="aid", reason=current.aid),
                    ErrorDetail(field="previous", reason=previous.aid),
                ),
            )


def unaided_comprehension(observations: Sequence[Comprehension]) -> str | None:
    """What the learner understood before any help, if that was ever measured."""

    unaided = [entry for entry in observations if entry.aid == "unaided"]
    if not unaided:
        return None
    return max(unaided, key=lambda entry: band_strength(entry.band)).band


def aided_comprehension(observations: Sequence[Comprehension]) -> str | None:
    """What they understood with help, which is a different fact and stored separately."""

    aided = [entry for entry in observations if entry.aid != "unaided"]
    if not aided:
        return None
    return max(aided, key=lambda entry: band_strength(entry.band)).band


def comprehended_unaided(observations: Sequence[Comprehension]) -> bool:
    """Whether the learner understood this unit without help, by the policy threshold."""

    band = unaided_comprehension(observations)
    return band is not None and band_strength(band) >= band_strength(UNAIDED_THRESHOLD)


def coverage(*, completed_units: int, total_units: int | None) -> float | None:
    """How much of the source is done, or `None` when nobody said how long it is.

    `None` rather than 0.0, and rather than guessing from the highest unit seen: a
    podcast feed has no last episode, and a progress bar against an invented total is a
    number that looks like knowledge.
    """

    if total_units is None or total_units <= 0:
        return None
    return min(1.0, max(0.0, completed_units / total_units))


def assert_mode_permits_extraction(*, mode: str, extracted: int, reference: str) -> None:
    """Extensive work records breadth, not line-by-line extraction.

    Not a refusal of the item itself -- a learner who meets one striking word while
    reading for volume should keep it -- but of a *harvest*: dozens of items from an
    extensive pass means the learner was reading intensively and labelled it otherwise,
    and the progress rows would then describe something that did not happen.
    """

    assert_known(mode, vocabulary=STUDY_MODES, field="mode", code="unknown_study_mode")
    if mode == "extensive" and extracted > EXTENSIVE_EXTRACTION_LIMIT:
        raise LinguaWikiError(
            "extensive_extraction_excessive",
            f"{reference} is extensive work, which records broad progress rather than "
            f"{extracted} extracted items; record it as intensive, or keep the few items "
            "that were genuinely worth stopping for",
            details=(
                ErrorDetail(field="mode", reason=mode),
                ErrorDetail(field="extracted", reason=str(extracted)),
            ),
        )


#: Items an extensive pass may extract before it stops being extensive.
EXTENSIVE_EXTRACTION_LIMIT = 5


def assert_policy_is_sound() -> None:
    """Refuse a policy that contradicts itself, before it can describe a learner."""

    problems: list[str] = []
    if UNAIDED_THRESHOLD not in COMPREHENSION_BANDS:
        problems.append("the unaided threshold is not a comprehension band")
    if COMPREHENSION_AIDS[0] != "unaided":
        problems.append("the weakest aid level must be no help at all")
    if EXCERPT_LIMIT >= FULL_LOCAL_LIMIT:
        problems.append("a short excerpt is not shorter than a full local copy")
    if excerpt_limit("metadata-only") != 0:
        problems.append("metadata-only permits storing text")
    for status, targets in SOURCE_TRANSITIONS.items():
        if status not in SOURCE_STATUSES:
            problems.append(f"{status} is not a known source status")
        for target in targets:
            if target not in SOURCE_STATUSES:
                problems.append(f"{status} may become unknown status {target}")
    if SOURCE_TRANSITIONS["rejected"]:
        problems.append("a rejected source cannot come back; catalogue it again instead")
    for kind in TIMED_KINDS:
        if kind not in SOURCE_KINDS:
            problems.append(f"timed kind {kind} is not a known source kind")
    for kind in SOURCE_KINDS:
        if kind not in AREAS_FOR_KIND:
            problems.append(f"{kind} names no session area it could serve")
    if problems:
        raise LinguaWikiError(
            "unsound_source_policy",
            "the source policy contradicts itself: " + "; ".join(problems),
            details=tuple(ErrorDetail(field="policy", reason=problem) for problem in problems),
        )


__all__ = [
    "AREAS_FOR_KIND",
    "COMPREHENSION_AIDS",
    "COMPREHENSION_BANDS",
    "EXCERPT_LIMIT",
    "EXTENSIVE_EXTRACTION_LIMIT",
    "FULL_LOCAL_LIMIT",
    "PROGRESS_STATUSES",
    "RIGHTS_CLASSES",
    "SOURCE_KINDS",
    "SOURCE_POLICY_VERSION",
    "SOURCE_STATUSES",
    "SOURCE_TRANSITIONS",
    "STUDY_MODES",
    "TIMED_KINDS",
    "UNAIDED_THRESHOLD",
    "Comprehension",
    "aid_strength",
    "aided_comprehension",
    "assert_excerpt_permitted",
    "assert_known",
    "assert_mode_permits_extraction",
    "assert_observation_order",
    "assert_policy_is_sound",
    "assert_source_transition",
    "band_strength",
    "comprehended_unaided",
    "coverage",
    "excerpt_limit",
    "unaided_comprehension",
]
