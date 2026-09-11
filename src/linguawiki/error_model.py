"""When two mistakes are the same mistake, and what it takes to call one fixed.

Two decisions live here, and both are the kind that quietly corrupts a learner model if
it is made ad hoc at the call site.

The first is identity. A recurring error is `category + normalized signature + target
item`, and normalization is deliberately blunt: case folding, canonical composition, and
the removal of marks, punctuation, and separators. It never splits on whitespace,
because a language that does not use it must dedupe as well as one that does. Two
signatures that neither match nor clearly differ are *not* merged -- they are reported as
an uncertain match for a person to settle, because silently merging two different errors
loses the distinction forever while asking costs one question.

The second is resolution. One correct answer is never a fix; the policy requires
controlled, novel, spontaneous, and delayed success, and `assert_policy_is_sound` refuses
a configuration that could resolve an error on a single observation. An error that
recurs after being resolved comes back as `reactivated` rather than as a new error, so
its history stays in one place.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.text import normalize_identity

#: Bumped when a rule below changes meaning; stored on every pattern.
POLICY_VERSION = "error.v1"

STATUSES: tuple[str, ...] = (
    "observed",
    "active",
    "monitoring",
    "resolved",
    "reactivated",
    "unconfirmed",
    "superseded",
)
#: Nothing observed yet is definitely the learner's error: every occurrence so far was a
#: transcription artifact or was classified uncertain.
UNCONFIRMED_STATUS = "unconfirmed"
#: The one occurrence classification that is evidence of a learner error at all.
CONFIRMED_CLASSIFICATION = "learner-error"
#: A pattern whose identity was re-derived when a duplicate item was merged away. Its
#: history moved to the successor it names; it takes no further part in the lifecycle.
SUPERSEDED_STATUS = "superseded"
#: Statuses in which the error is still counted against the learner's current work.
LIVE_STATUSES: tuple[str, ...] = ("observed", "active", "reactivated")
#: When two records of the same error are folded together -- which happens when a
#: duplicate knowledge item is merged away -- this is the status that survives, weakest
#: first. A still-occurring error outranks a monitored or resolved one: the recurrence is
#: the newer fact and the safer conclusion to keep.
STATUS_PRECEDENCE: tuple[str, ...] = (
    "unconfirmed",
    "resolved",
    "monitoring",
    "observed",
    "active",
    "reactivated",
)

#: How a success can qualify as counter-evidence. One evidence row may qualify several
#: ways at once, and the policy counts each way separately.
QUALIFICATIONS: tuple[str, ...] = ("controlled", "novel", "spontaneous", "delayed")

#: Whether an occurrence was the learner's error at all. A transcription artifact is not
#: evidence of an error, and conflating the two is how a learner gets taught a mistake
#: the microphone made.
CLASSIFICATIONS: tuple[str, ...] = ("learner-error", "transcription-artifact", "uncertain")
MEANING_IMPACTS: tuple[str, ...] = ("none", "minor", "major", "breakdown")
SEVERITIES: tuple[str, ...] = ("low", "medium", "high")

#: Below the floor two signatures are different errors; at or above the ceiling they are
#: the same one. Between them nobody should be guessing, so the match is reported.
UNCERTAIN_FLOOR = 0.72
CERTAIN_CEILING = 0.995


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    """What it takes to move an error along its lifecycle."""

    version: str = POLICY_VERSION
    #: Occurrences at which a merely observed error becomes an active one.
    activate_at_occurrences: int = 2
    #: Counter-evidence needed before an active error is worth monitoring.
    monitoring_successes: int = 2
    monitoring_qualifications: tuple[str, ...] = ("controlled",)
    #: Counter-evidence needed to call it resolved. Never satisfiable by one success.
    resolution_successes: int = 3
    resolution_qualifications: tuple[str, ...] = ("novel", "spontaneous", "delayed")
    resolution_contexts: int = 2
    #: Whether a recurrence after monitoring or resolution reopens the same error.
    reactivate_on_recurrence: bool = True


DEFAULT_POLICY = ResolutionPolicy()


@dataclass(frozen=True, slots=True)
class CounterEvidence:
    """The counter-evidence tallied for one error, as the policy needs to see it."""

    successes: int = 0
    contexts: int = 0
    qualifications: Mapping[str, int] = field(default_factory=dict)
    #: The newest occurrence and the newest success, used to tell recurrence from repair.
    last_occurrence_at: datetime | None = None
    last_success_at: datetime | None = None

    def count(self, qualification: str) -> int:
        return int(self.qualifications.get(qualification, 0))


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """One existing pattern a new occurrence might belong to."""

    error_id: str
    signature: str
    similarity: float
    status: str


@dataclass(frozen=True, slots=True)
class Transition:
    """A lifecycle decision, with the reason that produced it."""

    status: str
    reason: str
    changed: bool


def normalize_signature(value: str) -> str:
    """Reduce an error signature to a form two occurrences can be compared in.

    Nothing here assumes an alphabet, a tokenization, or a direction. Canonical
    composition first, so the same grapheme written two ways is one; then case folding;
    then the removal of marks, punctuation, and separators, which is what makes
    `nie ma` and `Nie-ma!` the same signature without splitting either into words.
    """

    normalized = normalize_identity(value)
    if not normalized:
        raise LinguaWikiError(
            "empty_error_signature",
            "an error signature must contain at least one letter, digit, or symbol",
            details=(ErrorDetail(field="signature", reason="nothing remains after normalization"),),
        )
    return normalized


def signature_similarity(left: str, right: str) -> float:
    """How alike two normalized signatures are, on a character sequence basis.

    Character-level rather than token-level on purpose: a tonal, non-whitespace fixture
    pack has to dedupe as reliably as an inflected, whitespace-delimited one.
    """

    if left == right:
        return 1.0
    return round(SequenceMatcher(None, left, right).ratio(), 6)


def classify_match(similarity: float) -> str:
    """Whether a similarity is a match, a different error, or nobody's call to make."""

    if similarity >= CERTAIN_CEILING:
        return "same"
    return "uncertain" if similarity >= UNCERTAIN_FLOOR else "different"


def uncertain_matches(
    signature: str, candidates: Sequence[MatchCandidate]
) -> tuple[MatchCandidate, ...]:
    """Candidates too close to dismiss and too far to merge, strongest first."""

    scored = tuple(
        MatchCandidate(
            error_id=candidate.error_id,
            signature=candidate.signature,
            similarity=signature_similarity(signature, candidate.signature),
            status=candidate.status,
        )
        for candidate in candidates
    )
    return tuple(
        sorted(
            (
                candidate
                for candidate in scored
                if classify_match(candidate.similarity) == "uncertain"
            ),
            key=lambda candidate: (-candidate.similarity, candidate.error_id),
        )
    )


def _satisfies(
    counter: CounterEvidence,
    *,
    successes: int,
    qualifications: Sequence[str],
    contexts: int,
) -> tuple[bool, tuple[str, ...]]:
    """Whether counter-evidence meets a requirement, and what is missing if not."""

    missing: list[str] = []
    if counter.successes < successes:
        missing.append(f"{counter.successes}/{successes} qualifying success(es)")
    if counter.contexts < contexts:
        missing.append(f"{counter.contexts}/{contexts} distinct context(s)")
    missing.extend(
        f"no {qualification} success"
        for qualification in qualifications
        if not counter.count(qualification)
    )
    return (not missing, tuple(missing))


def next_status_after_occurrence(
    current: str,
    *,
    occurrences: int,
    confirmed: bool = True,
    policy: ResolutionPolicy = DEFAULT_POLICY,
) -> Transition:
    """The status after one more occurrence of the same error.

    `confirmed` is whether the occurrence was the learner's error at all. An unconfirmed
    one -- a transcription artifact, or a classification nobody settled -- is recorded and
    changes nothing: it cannot activate a pattern and cannot reopen a resolved one. A
    mishearing taught back as a mistake is worse than a mishearing lost.
    """

    assert_superseded_is_not_asked_to_transition(current)
    if not confirmed:
        return Transition(
            status=current,
            reason=(
                "the occurrence was not confirmed as the learner's error, so it is on "
                f"record but counts for nothing; the pattern stays {current}"
            ),
            changed=False,
        )
    if current == UNCONFIRMED_STATUS:
        return Transition(
            status="active" if occurrences >= policy.activate_at_occurrences else "observed",
            reason=(
                "the first confirmed occurrence of a pattern previously seen only as an "
                f"artifact; {occurrences} confirmed occurrence(s) recorded"
            ),
            changed=True,
        )
    if current in ("monitoring", "resolved") and policy.reactivate_on_recurrence:
        return Transition(
            status="reactivated",
            reason=(
                f"the error recurred while {current}; it is the same error, so its history "
                "continues rather than starting again"
            ),
            changed=True,
        )
    if current == "observed" and occurrences >= policy.activate_at_occurrences:
        return Transition(
            status="active",
            reason=(
                f"{occurrences} occurrence(s) reached the activation threshold of "
                f"{policy.activate_at_occurrences}"
            ),
            changed=True,
        )
    return Transition(
        status=current,
        reason=f"{occurrences} occurrence(s) recorded; the status is unchanged",
        changed=False,
    )


def next_status_after_success(
    current: str, *, counter: CounterEvidence, policy: ResolutionPolicy = DEFAULT_POLICY
) -> Transition:
    """The status after counter-evidence, refusing to resolve on too little.

    Resolution is checked before monitoring so an error that arrives with a full set of
    counter-evidence does not have to wait a round to be recognized as fixed -- but it
    still cannot skip the requirement, only the delay.
    """

    assert_superseded_is_not_asked_to_transition(current)
    if current == "resolved":
        return Transition(status=current, reason="the error is already resolved", changed=False)
    resolved, resolution_missing = _satisfies(
        counter,
        successes=policy.resolution_successes,
        qualifications=policy.resolution_qualifications,
        contexts=policy.resolution_contexts,
    )
    recurred_since_success = (
        counter.last_occurrence_at is not None
        and counter.last_success_at is not None
        and counter.last_occurrence_at > counter.last_success_at
    )
    if resolved and not recurred_since_success:
        return Transition(
            status="resolved",
            reason=(
                f"{counter.successes} qualifying success(es) across {counter.contexts} context(s) "
                f"met the policy: {', '.join(policy.resolution_qualifications)}"
            ),
            changed=current != "resolved",
        )
    monitored, monitoring_missing = _satisfies(
        counter,
        successes=policy.monitoring_successes,
        qualifications=policy.monitoring_qualifications,
        contexts=1,
    )
    if monitored and current != "monitoring":
        return Transition(
            status="monitoring",
            reason=(
                f"{counter.successes} qualifying success(es) are enough to monitor, not to "
                f"resolve; still missing {', '.join(resolution_missing)}"
            ),
            changed=True,
        )
    outstanding = monitoring_missing if not monitored else resolution_missing
    return Transition(
        status=current,
        reason=f"counter-evidence is short of the next step: {', '.join(outstanding)}",
        changed=False,
    )


def assert_superseded_is_not_asked_to_transition(status: str) -> str:
    """Refuse to move a pattern that has already been folded into another.

    Recording against a superseded pattern would put the occurrence somewhere nothing
    reads. The caller has to reach the successor, which the pattern names.
    """

    assert_known_status(status)
    if status == UNCONFIRMED_STATUS:
        return status
    if status == SUPERSEDED_STATUS:
        raise LinguaWikiError(
            "error_pattern_superseded",
            "this error pattern was folded into another when a duplicate item was merged "
            "away; record against the pattern it names as its successor",
            details=(ErrorDetail(field="error", reason="superseded pattern"),),
        )
    return status


def stronger_status(left: str, right: str) -> str:
    """The status that survives when two records of one error are folded together."""

    assert_known_status(left)
    assert_known_status(right)
    return max((left, right), key=STATUS_PRECEDENCE.index)


def assert_known_status(status: str) -> str:
    """Require one of the five lifecycle statuses."""

    if status not in STATUSES:
        raise LinguaWikiError(
            "unknown_error_status",
            f"{status} is not an error status; expected one of {list(STATUSES)}",
            details=(ErrorDetail(field="status", reason="unknown status"),),
        )
    return status


def assert_policy_is_sound(policy: ResolutionPolicy = DEFAULT_POLICY) -> ResolutionPolicy:
    """Refuse a policy under which one correct response could resolve an error.

    This is the rule the plan states outright, so it is checked rather than trusted: a
    policy is a configuration, and a configuration that can be set wrong will be.
    """

    problems: list[ErrorDetail] = []
    if policy.resolution_successes < 2:
        problems.append(
            ErrorDetail(
                field="resolution_successes",
                reason="one correct response can never resolve an error",
                context={"configured": str(policy.resolution_successes)},
            )
        )
    if policy.resolution_contexts < 2:
        problems.append(
            ErrorDetail(
                field="resolution_contexts",
                reason="resolution requires success across more than one context",
            )
        )
    if not policy.resolution_qualifications:
        problems.append(
            ErrorDetail(
                field="resolution_qualifications",
                reason="resolution must require at least one kind of qualifying success",
            )
        )
    unknown = sorted(
        {
            qualification
            for qualification in (
                *policy.resolution_qualifications,
                *policy.monitoring_qualifications,
            )
            if qualification not in QUALIFICATIONS
        }
    )
    if unknown:
        problems.append(
            ErrorDetail(field="qualifications", reason=f"unknown: {', '.join(unknown)}")
        )
    if policy.activate_at_occurrences < 1:
        problems.append(
            ErrorDetail(
                field="activate_at_occurrences", reason="activation needs at least one occurrence"
            )
        )
    if policy.monitoring_successes < 1:
        problems.append(
            ErrorDetail(
                field="monitoring_successes", reason="monitoring needs at least one success"
            )
        )
    if problems:
        raise LinguaWikiError(
            "invalid_error_policy",
            "the error-resolution policy would accept a fix it has not seen",
            details=tuple(problems),
        )
    return policy


def qualifications_for(
    *, claim: str, retrieval: str, novelty: str, help_level: str, modality: str, task_type: str
) -> tuple[str, ...]:
    """How one success qualifies as counter-evidence against an error.

    `controlled` and `spontaneous` are terms about *production*, and the default policy
    requires both -- so they are what stop receptive evidence from retiring an error the
    learner still makes. Getting that wrong let three delayed reading-comprehension
    checks resolve an explicitly named production error, which is the one thing the
    resolution policy exists to prevent.

    So neither is read off the claim alone. `controlled` needs the learner to have
    produced language with no more than a prompt; `spontaneous` needs them to have
    produced it while attending to meaning, unaided. `delayed-transfer` is not by itself
    either of those: it says *when* the observation happened, not what was demanded, and
    its permitted task types span reading and speaking alike.

    Every input is a fact already recorded on the evidence row, so a caller cannot assert
    a qualification -- which is what keeps a hinted repetition from resolving anything.

    The default policy is production-oriented by design. An error that is genuinely
    receptive -- a contrast the learner mishears -- needs a policy whose requirements
    receptive evidence can meet; that is what makes `ResolutionPolicy` a parameter.
    """

    from linguawiki.evidence import (
        MEANING_FOCUSED_TASK_TYPES,
        PRODUCTIVE_MODALITIES,
        SELECTION_TASK_TYPES,
        claim_rule,
        help_strength,
    )

    claim_rule(claim)
    # Producing language, rather than choosing between offered options.
    produced = modality in PRODUCTIVE_MODALITIES and task_type not in SELECTION_TASK_TYPES
    qualifications: list[str] = []
    if produced and help_strength(help_level) <= help_strength("prompted"):
        qualifications.append("controlled")
    if novelty == "novel":
        qualifications.append("novel")
    if produced and help_level == "none" and task_type in MEANING_FOCUSED_TASK_TYPES:
        qualifications.append("spontaneous")
    if retrieval == "delayed":
        qualifications.append("delayed")
    return tuple(sorted(set(qualifications)))


__all__ = [
    "CERTAIN_CEILING",
    "CLASSIFICATIONS",
    "CONFIRMED_CLASSIFICATION",
    "DEFAULT_POLICY",
    "LIVE_STATUSES",
    "MEANING_IMPACTS",
    "POLICY_VERSION",
    "QUALIFICATIONS",
    "SEVERITIES",
    "STATUSES",
    "STATUS_PRECEDENCE",
    "SUPERSEDED_STATUS",
    "UNCERTAIN_FLOOR",
    "UNCONFIRMED_STATUS",
    "CounterEvidence",
    "MatchCandidate",
    "ResolutionPolicy",
    "Transition",
    "assert_known_status",
    "assert_policy_is_sound",
    "assert_superseded_is_not_asked_to_transition",
    "classify_match",
    "next_status_after_occurrence",
    "next_status_after_success",
    "normalize_signature",
    "qualifications_for",
    "signature_similarity",
    "stronger_status",
    "uncertain_matches",
]
