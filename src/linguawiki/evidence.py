"""What an observation is allowed to claim.

An attempt is a thing that happened; evidence is a *claim* about a skill or an item that
the attempt justifies. The two are separate because the same attempt justifies different
claims depending on what the learner actually had to do: choosing the right answer from
four options is recognition however fluent the learner is, and no amount of it becomes
spontaneous production.

That is the rule this module exists to make unbreakable. Every claim declares the
modalities and task types that can produce it, the most help it survives, and whether it
needs a delay -- and `assert_compatible` refuses the rest by name. Nothing downstream has
to re-derive it: `mastery` reads the same ceilings, so an item's stage cannot outrun the
kind of evidence behind it.

Nothing here knows about a language. A task type is a shape of demand, not a
construction, and no rule mentions an alphabet, a tokenization, or a script.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from linguawiki.errors import ErrorDetail, LinguaWikiError

#: The version of the strength calculation, stored with the row so a policy change can
#: be told apart from a data change. Strength is derived only from facts the attempt
#: itself records, so it stays recomputable from raw attempts forever.
STRENGTH_VERSION = "evidence.v1"

#: Where an attempt came from, matching the schema's own CHECK. `session` is here
#: because Stage 4 produces it: a session close writes attempts through this module's
#: `write_attempt`.
ATTEMPT_ORIGINS: tuple[str, ...] = (
    "assessment",
    "curriculum-audit",
    "import",
    "repair",
    "session",
)
#: The origins the standalone `evidence record` command may claim. A live session's
#: attempts belong to the session engine, which owns the staged event they came from and
#: the finalization that credited them -- recording one by hand would leave an attempt
#: no session accounts for.
COMMAND_ORIGINS: tuple[str, ...] = tuple(
    origin for origin in ATTEMPT_ORIGINS if origin != "session"
)

MODALITIES: tuple[str, ...] = ("text", "audio", "speech", "writing")
#: Modalities in which the learner produces language, rather than receiving it.
PRODUCTIVE_MODALITIES: tuple[str, ...] = ("speech", "writing")
RECEPTIVE_MODALITIES: tuple[str, ...] = ("text", "audio")

#: A task type is a shape of demand. The first five are the assessment bank's own types,
#: so a scored assessment task maps onto one without translation.
TASK_TYPES: tuple[str, ...] = (
    "objective",
    "short-response",
    "extended-productive",
    "pronunciation-target",
    "connected-speech",
    "meaning-focused-exchange",
    "reading-comprehension",
    "listening-comprehension",
    "recall-prompt",
)
#: Task types where the learner picks from offered material rather than retrieving it.
#: A correct answer here is recognition, and that is the whole point of the distinction.
SELECTION_TASK_TYPES: tuple[str, ...] = ("objective",)
#: Task types where the learner is attending to meaning and chose the form themselves,
#: rather than filling a slot the task shaped for them. Spontaneity is a property of the
#: demand, so it is declared here once and read wherever it is needed.
MEANING_FOCUSED_TASK_TYPES: tuple[str, ...] = (
    "extended-productive",
    "meaning-focused-exchange",
    "connected-speech",
)

CLAIMS: tuple[str, ...] = (
    "recognition",
    "comprehension",
    "controlled-production",
    "spontaneous-production",
    "delayed-transfer",
    "intelligibility",
)

#: Ordered weakest to strongest: the index is how much was given away.
HELP_LEVELS: tuple[str, ...] = ("none", "prompted", "hinted", "scaffolded", "full-answer")
#: Help that supplies the answer itself. No positive claim survives it.
FULL_HELP = "full-answer"

RETRIEVAL_CLASSES: tuple[str, ...] = ("immediate", "same-session", "delayed")
POLARITIES: tuple[str, ...] = ("positive", "partial", "negative")
NOVELTY: tuple[str, ...] = ("novel", "repeat")
CORRECTION_MODES: tuple[str, ...] = ("none", "delayed", "immediate", "recast", "explicit")
ASSESSOR_KINDS: tuple[str, ...] = ("deterministic", "ai", "learner", "human")
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")
OUTCOMES: tuple[str, ...] = ("success", "partial", "failure")

#: What of the learner's own words a record may keep. Consent decides, not convenience.
RESPONSE_VISIBILITIES: tuple[str, ...] = ("withheld", "excerpt", "full")
#: Qualitative notes about the learner rather than about an item: how a session felt,
#: what strategy they reached for. They travel with a session and never become evidence.
OBSERVATION_CATEGORIES: tuple[str, ...] = (
    "fatigue",
    "confidence",
    "strategy",
    "notable-success",
    "note",
)
SALIENCES: tuple[str, ...] = ("low", "medium", "high")

#: How much a claim is discounted for who scored it and how sure they were. An AI
#: judgement of a learner's own writing is evidence; it is not the same evidence as a
#: deterministic key match, and a low-confidence transcription is weaker still.
CONFIDENCE_FACTORS: Mapping[str, float] = {"low": 0.5, "medium": 0.8, "high": 1.0}
ASSESSOR_FACTORS: Mapping[str, float] = {
    "deterministic": 1.0,
    "human": 1.0,
    "ai": 0.85,
    "learner": 0.7,
}
#: Each help level's surviving share of an observation's strength.
HELP_FACTORS: Mapping[str, float] = {
    "none": 1.0,
    "prompted": 0.85,
    "hinted": 0.6,
    "scaffolded": 0.35,
    "full-answer": 0.0,
}


@dataclass(frozen=True, slots=True)
class ClaimRule:
    """What has to be true of an attempt before it can carry this claim."""

    claim: str
    #: Modalities that can produce the claim at all.
    modalities: tuple[str, ...]
    #: Task types that can produce it. A task type absent here cannot, whatever the score.
    task_types: tuple[str, ...]
    #: The most help a *positive* claim survives, as an index into `HELP_LEVELS`.
    maximum_help: str
    #: Whether the claim is only meaningful after a delay.
    requires_delay: bool
    #: The highest item stage this claim can ever justify. `mastery` reads this.
    item_ceiling: str
    #: A claim about how the learner sounds is about a dimension, not about an item.
    dimension_scoped: bool = False


CLAIM_RULES: Mapping[str, ClaimRule] = {
    rule.claim: rule
    for rule in (
        ClaimRule(
            claim="recognition",
            modalities=MODALITIES,
            task_types=(
                "objective",
                "short-response",
                "reading-comprehension",
                "listening-comprehension",
                "recall-prompt",
            ),
            maximum_help="scaffolded",
            requires_delay=False,
            item_ceiling="recognized",
        ),
        ClaimRule(
            claim="comprehension",
            modalities=MODALITIES,
            task_types=(
                "objective",
                "short-response",
                "reading-comprehension",
                "listening-comprehension",
                "meaning-focused-exchange",
            ),
            maximum_help="hinted",
            requires_delay=False,
            item_ceiling="understood",
        ),
        ClaimRule(
            claim="controlled-production",
            modalities=PRODUCTIVE_MODALITIES,
            task_types=(
                "short-response",
                "extended-productive",
                "recall-prompt",
                "connected-speech",
            ),
            maximum_help="prompted",
            requires_delay=False,
            item_ceiling="controlled-production",
        ),
        ClaimRule(
            claim="spontaneous-production",
            modalities=PRODUCTIVE_MODALITIES,
            # Spontaneous means the learner chose the form while attending to meaning.
            # A slot to fill is controlled production even when the answer is right.
            task_types=("extended-productive", "meaning-focused-exchange", "connected-speech"),
            maximum_help="none",
            requires_delay=False,
            item_ceiling="spontaneous-production",
        ),
        ClaimRule(
            claim="delayed-transfer",
            modalities=MODALITIES,
            # Selection tasks are excluded: recognising an option after a delay is
            # delayed recognition, and calling it transfer is the overclaim this
            # vocabulary exists to prevent.
            task_types=(
                "short-response",
                "extended-productive",
                "meaning-focused-exchange",
                "connected-speech",
                "reading-comprehension",
                "listening-comprehension",
            ),
            maximum_help="prompted",
            requires_delay=True,
            item_ceiling="stable",
        ),
        ClaimRule(
            claim="intelligibility",
            modalities=("speech",),
            task_types=("pronunciation-target", "connected-speech", "meaning-focused-exchange"),
            maximum_help="prompted",
            requires_delay=False,
            # How a learner sounds is a dimension estimate. It says nothing about whether
            # they know the item, so it cannot move an item past having been met.
            item_ceiling="encountered",
            dimension_scoped=True,
        ),
    )
}

#: Every claim's item-stage ceiling, which is the whole language-agnostic guard.
CLAIM_CEILINGS: Mapping[str, str] = {
    claim: rule.item_ceiling for claim, rule in CLAIM_RULES.items()
}


def claim_rule(claim: str) -> ClaimRule:
    """The rule for a claim, refusing an unknown one by name."""

    rule = CLAIM_RULES.get(claim)
    if rule is None:
        raise LinguaWikiError(
            "unknown_evidence_claim",
            f"{claim} is not an evidence claim; expected one of {list(CLAIMS)}",
            details=(ErrorDetail(field="claim", reason="unknown claim"),),
        )
    return rule


def help_strength(help_level: str) -> int:
    """The ordinal position of a help level, refusing an unknown one."""

    if help_level not in HELP_LEVELS:
        raise LinguaWikiError(
            "unknown_help_level",
            f"{help_level} is not a help level; expected one of {list(HELP_LEVELS)}",
            details=(ErrorDetail(field="help_level", reason="unknown help level"),),
        )
    return HELP_LEVELS.index(help_level)


def assert_known(value: str, *, vocabulary: Sequence[str], field: str, code: str) -> str:
    """Require a value from a closed vocabulary, naming the field and the options."""

    if value not in vocabulary:
        raise LinguaWikiError(
            code,
            f"{value} is not a valid {field}; expected one of {list(vocabulary)}",
            details=(ErrorDetail(field=field, reason=f"unknown {field}"),),
        )
    return value


def assert_compatible(
    claim: str,
    *,
    modality: str,
    task_type: str,
    help_level: str,
    retrieval: str,
    polarity: str,
    dimension: str | None = None,
    target_content_id: str | None = None,
) -> ClaimRule:
    """Refuse a claim the attempt behind it cannot support.

    Every refusal names the claim, what the attempt was, and what the claim needs, so a
    caller can either fix the claim or record the weaker one that is actually true.
    """

    rule = claim_rule(claim)
    assert_known(modality, vocabulary=MODALITIES, field="modality", code="unknown_modality")
    assert_known(task_type, vocabulary=TASK_TYPES, field="task_type", code="unknown_task_type")
    assert_known(polarity, vocabulary=POLARITIES, field="polarity", code="unknown_polarity")
    assert_known(
        retrieval, vocabulary=RETRIEVAL_CLASSES, field="retrieval", code="unknown_retrieval_class"
    )
    if target_content_id is None and dimension is None:
        raise LinguaWikiError(
            "evidence_target_required",
            "evidence names a knowledge item, a skill dimension, or both",
            details=(ErrorDetail(field="target", reason="neither an item nor a dimension"),),
        )
    if rule.dimension_scoped and dimension is None:
        raise LinguaWikiError(
            "evidence_dimension_required",
            f"a {claim} claim is about a skill dimension and must name one",
            details=(ErrorDetail(field="dimension", reason="dimension-scoped claim"),),
        )
    if modality not in rule.modalities:
        raise LinguaWikiError(
            "evidence_modality_incompatible",
            f"a {claim} claim cannot come from a {modality} attempt; it needs one of "
            f"{list(rule.modalities)}",
            details=(
                ErrorDetail(
                    field="modality",
                    reason="modality cannot produce this claim",
                    context={"claim": claim, "modality": modality},
                ),
            ),
        )
    if task_type not in rule.task_types:
        raise LinguaWikiError(
            "evidence_task_incompatible",
            f"a {task_type} task cannot produce a {claim} claim; it produces one of "
            f"{list(claims_for_task(task_type))}",
            details=(
                ErrorDetail(
                    field="task_type",
                    reason="task type cannot produce this claim",
                    context={"claim": claim, "task_type": task_type},
                ),
            ),
        )
    if rule.requires_delay and retrieval != "delayed":
        raise LinguaWikiError(
            "evidence_delay_required",
            f"a {claim} claim needs delayed retrieval, not {retrieval}",
            details=(ErrorDetail(field="retrieval", reason="claim requires a delay"),),
        )
    if polarity != "negative" and help_strength(help_level) > help_strength(rule.maximum_help):
        raise LinguaWikiError(
            "evidence_help_exceeds_claim",
            f"a {polarity} {claim} claim cannot survive {help_level} help; that claim allows "
            f"at most {rule.maximum_help}",
            details=(
                ErrorDetail(
                    field="help_level",
                    reason="help level too high for this claim",
                    context={"claim": claim, "help_level": help_level},
                ),
            ),
        )
    return rule


def observation_strength(
    *,
    normalized_score: float,
    polarity: str,
    help_level: str,
    assessor_kind: str,
    confidence: str,
) -> float:
    """How much this observation is worth, before any aggregation policy touches it.

    Only facts the attempt itself records go in, which is what keeps every later
    recomputation possible from raw attempts. Hints, an AI grader, and a low-confidence
    transcription each discount the observation rather than disqualifying it -- except
    full help, which leaves nothing to have observed.
    """

    if not 0.0 <= normalized_score <= 1.0:
        raise LinguaWikiError(
            "invalid_score",
            "a normalized score is between 0.0 and 1.0",
            details=(ErrorDetail(field="normalized_score", reason=str(normalized_score)),),
        )
    assert_known(polarity, vocabulary=POLARITIES, field="polarity", code="unknown_polarity")
    assert_known(
        assessor_kind,
        vocabulary=ASSESSOR_KINDS,
        field="assessor_kind",
        code="unknown_assessor_kind",
    )
    assert_known(
        confidence, vocabulary=CONFIDENCE_LEVELS, field="confidence", code="unknown_confidence"
    )
    help_strength(help_level)
    # A negative observation is as informative as the failure was complete, so it is not
    # discounted for help: a learner who failed *with* a hint failed more clearly.
    magnitude = 1.0 - normalized_score if polarity == "negative" else normalized_score
    factor = (
        1.0
        if polarity == "negative"
        else HELP_FACTORS[help_level]
        * ASSESSOR_FACTORS[assessor_kind]
        * CONFIDENCE_FACTORS[confidence]
    )
    return round(min(1.0, max(0.0, magnitude * factor)), 6)


def outcome_for(
    normalized_score: float, *, success_at: float = 0.8, partial_at: float = 0.5
) -> str:
    """Classify a score into the three outcomes, so callers agree on the boundaries."""

    if normalized_score >= success_at:
        return "success"
    return "partial" if normalized_score >= partial_at else "failure"


def polarity_for(outcome: str) -> str:
    """The polarity an outcome carries."""

    assert_known(outcome, vocabulary=OUTCOMES, field="outcome", code="unknown_outcome")
    return {"success": "positive", "partial": "partial", "failure": "negative"}[outcome]


def claims_for_task(task_type: str) -> tuple[str, ...]:
    """Every claim a task type can produce, in the declared order."""

    assert_known(task_type, vocabulary=TASK_TYPES, field="task_type", code="unknown_task_type")
    return tuple(claim for claim in CLAIMS if task_type in CLAIM_RULES[claim].task_types)


__all__ = [
    "ASSESSOR_FACTORS",
    "ASSESSOR_KINDS",
    "ATTEMPT_ORIGINS",
    "CLAIMS",
    "CLAIM_CEILINGS",
    "CLAIM_RULES",
    "CONFIDENCE_FACTORS",
    "CONFIDENCE_LEVELS",
    "CORRECTION_MODES",
    "FULL_HELP",
    "HELP_FACTORS",
    "HELP_LEVELS",
    "MEANING_FOCUSED_TASK_TYPES",
    "MODALITIES",
    "NOVELTY",
    "OUTCOMES",
    "POLARITIES",
    "PRODUCTIVE_MODALITIES",
    "RECEPTIVE_MODALITIES",
    "RETRIEVAL_CLASSES",
    "SELECTION_TASK_TYPES",
    "STRENGTH_VERSION",
    "TASK_TYPES",
    "ClaimRule",
    "assert_compatible",
    "assert_known",
    "claim_rule",
    "claims_for_task",
    "help_strength",
    "observation_strength",
    "outcome_for",
    "polarity_for",
]
