"""Transcript layers, what disagreement between them means, and what audio is required for.

A spoken session arrives as text somebody or something produced from sound, and the
whole difficulty is that the text is *not* the evidence. Three layers, each a different
claim about what was said:

- **raw** -- what the machine or the notetaker first produced. Immutable, because it is
  the record of what the transcription actually did, and correcting it in place would
  destroy the only evidence that it was ever wrong.
- **normalized** -- the same hearing, tidied: punctuation, casing, filler. It may not
  change *which words* were heard.
- **reviewed-hearing** -- a person listened again and says the machine misheard. This is
  a different claim from a correction, and conflating the two is the mistake this whole
  module exists to prevent.

The distinction that matters pedagogically: **a learner error is something the learner
said wrong; a transcription artifact is something the machine heard wrong.** Teaching the
second back to the learner as a mistake is worse than losing it, so an occurrence whose
classification is not `learner-error` is recorded and counted against nobody.

And the acoustic rule: **a correct transcript proves nothing about pronunciation.** Text
can carry a claim about the learner's *language* -- what they said, whether the case was
right -- but a claim about how they *sounded* needs the sound. If the audio is later
purged, the acoustic claims that rested on it are invalidated while the language evidence
from the same utterance survives, because only one of the two ever depended on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from linguawiki.errors import ErrorDetail, LinguaWikiError

#: Bumped when a rule below changes meaning. Stored on the rows it produced.
TRANSCRIPT_POLICY_VERSION = "transcript.v1"

#: The layers, weakest claim first. A layer may only derive from one before it: a
#: reviewed hearing revises what was heard, and nothing revises a reviewed hearing except
#: another review, which is a new revision rather than an edit.
LAYERS: tuple[str, ...] = ("raw", "normalized", "reviewed-hearing")

#: Which layer a revision may be derived from. `raw` derives from nothing: it is what
#: arrived.
LAYER_SOURCES: dict[str, tuple[str, ...]] = {
    "raw": (),
    "normalized": ("raw",),
    "reviewed-hearing": ("raw", "normalized"),
}

#: What a revision claims to have changed. The pair is the point: `normalization` may not
#: change which words were heard, and `hearing` says the machine got the words wrong.
REVISION_KINDS: tuple[str, ...] = (
    #: Punctuation, casing, filler removal. The words are the same words.
    "normalization",
    #: A person listened again and heard different words.
    "hearing",
)

#: How an occurrence in a transcript is classified. Only the first is the learner's
#: mistake; the rest are facts about the transcription or about nobody's certainty.
CLASSIFICATIONS: tuple[str, ...] = (
    "learner-error",
    "transcription-artifact",
    "uncertain",
)

#: What a pronunciation observation claims. Ordered by how much it asserts.
PRONUNCIATION_STATUSES: tuple[str, ...] = ("observed", "uncertain", "confirmed")

#: The status that requires audio. A `confirmed` acoustic claim without sound is a claim
#: about how something sounded made by reading.
CONFIRMED_STATUS = "confirmed"

#: The dimensions a pronunciation observation can be about, from the plan's lesson
#: behaviour. They are separate because a learner can be perfectly intelligible and
#: nothing like a native speaker, and reporting one number would hide that.
PRONUNCIATION_DIMENSIONS: tuple[str, ...] = (
    "intelligibility",
    "phonetic-accuracy",
    "prosody",
    "native-likeness",
)

#: Which dimensions can *only* ever be judged from audio, whatever the status. A
#: transcript cannot carry prosody at all: the text of a question and the text of a
#: flat statement can be identical.
AUDIO_ONLY_DIMENSIONS: tuple[str, ...] = ("prosody", "native-likeness")

#: What an evidence claim rests on. `transcript` means text only; `audio` means the sound
#: is available; `direct` means the observation was made live, in the session itself.
EVIDENCE_BASES: tuple[str, ...] = ("direct", "transcript", "audio")

#: Why an artifact is no longer available. A tombstone says which, because "the learner
#: asked" and "the retention window expired" are different facts about the same absence.
PURGE_REASONS: tuple[str, ...] = (
    "learner-request",
    "retention-expiry",
    "source-withdrawn",
    "superseded",
)


def assert_known(value: str, *, vocabulary: Sequence[str], field: str, code: str) -> str:
    if value not in vocabulary:
        raise LinguaWikiError(
            code,
            f"{value} is not a valid {field}; expected one of {list(vocabulary)}",
            details=(ErrorDetail(field=field, reason=value),),
        )
    return value


def assert_layer_derivation(*, layer: str, derived_from: str | None) -> None:
    """Refuse a layer that claims to come from somewhere it cannot."""

    assert_known(layer, vocabulary=LAYERS, field="layer", code="unknown_transcript_layer")
    permitted = LAYER_SOURCES[layer]
    if derived_from is None:
        if permitted:
            raise LinguaWikiError(
                "transcript_layer_unsourced",
                f"a {layer} layer revises an earlier one and must say which: "
                f"{' or '.join(permitted)}",
                details=(ErrorDetail(field="derived_from", reason="missing"),),
            )
        return
    if not permitted:
        raise LinguaWikiError(
            "raw_transcript_is_not_derived",
            "the raw layer is what arrived; it cannot be derived from anything, and "
            "changing it would destroy the record of what the transcription did",
            details=(ErrorDetail(field="derived_from", reason=derived_from),),
        )
    if derived_from not in permitted:
        raise LinguaWikiError(
            "invalid_transcript_derivation",
            f"a {layer} layer derives from {' or '.join(permitted)}, not {derived_from}",
            details=(ErrorDetail(field="derived_from", reason=derived_from),),
        )


def assert_revision_is_honest(*, kind: str, before: str, after: str, reference: str) -> None:
    """Refuse a revision whose kind does not match what it actually changed.

    A `normalization` that changes the words is a *hearing* claim wearing the label of a
    tidy-up, and the difference decides whether the learner is told they mispronounced
    something or the machine is told it misheard. Compared on words rather than
    characters, because punctuation and casing are exactly what normalization may change.
    """

    assert_known(kind, vocabulary=REVISION_KINDS, field="kind", code="unknown_revision_kind")
    if kind != "normalization":
        return
    if _words(before) != _words(after):
        raise LinguaWikiError(
            "normalization_changed_the_words",
            f"{reference} is recorded as a normalization, but it changes which words were "
            "heard; a different hearing is a reviewed-hearing revision, because it is a "
            "claim about the transcription rather than about its punctuation",
            details=(ErrorDetail(field="kind", reason=kind),),
        )


def _words(text: str) -> tuple[str, ...]:
    """The words of a line, ignoring what normalization is allowed to change.

    Casing and surrounding punctuation only. Nothing here transliterates, strips
    diacritics, or knows a script: two spellings that differ by a diacritic are two
    different hearings in any language where that matters, which is most of them.
    """

    stripped = "".join(
        character if character.isalnum() or character.isspace() else " " for character in text
    )
    return tuple(word.casefold() for word in stripped.split())


def same_words(before: str, after: str) -> bool:
    """Whether two readings heard the same words, ignoring punctuation and casing.

    The question a revision's *kind* answers, exposed because importing a package has to
    ask it about a layer the producer already labelled, rather than about a claim being
    made now.
    """

    return _words(before) == _words(after)


@dataclass(frozen=True, slots=True)
class Hearing:
    """What one layer says was said, for comparing layers against each other."""

    layer: str
    text: str


def disagreement(hearings: Sequence[Hearing]) -> tuple[str, ...]:
    """Which layers disagree about the words, reported rather than resolved.

    Disagreement is data: it is the measure of how much the transcription can be trusted,
    and a workspace that silently preferred the latest layer would hide exactly the
    uncertainty a learner needs to see before believing a correction.
    """

    by_layer = {hearing.layer: _words(hearing.text) for hearing in hearings}
    ordered = [layer for layer in LAYERS if layer in by_layer]
    differing: list[str] = []
    for previous, current in pairwise(ordered):
        if by_layer[previous] != by_layer[current]:
            differing.append(f"{previous} vs {current}")
    return tuple(differing)


def best_hearing(hearings: Sequence[Hearing]) -> Hearing | None:
    """The most reviewed hearing available: what a person confirmed, else the machine's."""

    by_layer = {hearing.layer: hearing for hearing in hearings}
    for layer in reversed(LAYERS):
        if layer in by_layer:
            return by_layer[layer]
    return None


def assert_acoustic_claim_has_audio(
    *, status: str, dimension: str, basis: str, reference: str
) -> None:
    """Refuse a claim about how something sounded that rests only on how it reads.

    Two separate rules, and both are needed:

    - a **confirmed** claim in any dimension needs the audio, because confirming is
      saying "I heard this", and
    - prosody and native-likeness need it at *every* status, because the text of a
      question and the text of a flat statement are the same text.
    """

    assert_known(
        status,
        vocabulary=PRONUNCIATION_STATUSES,
        field="status",
        code="unknown_pronunciation_status",
    )
    assert_known(
        dimension,
        vocabulary=PRONUNCIATION_DIMENSIONS,
        field="dimension",
        code="unknown_pronunciation_dimension",
    )
    assert_known(basis, vocabulary=EVIDENCE_BASES, field="basis", code="unknown_evidence_basis")
    if basis == "audio":
        return
    if status == CONFIRMED_STATUS:
        raise LinguaWikiError(
            "pronunciation_requires_audio",
            f"{reference} confirms {dimension} from a {basis} basis; a correct transcript "
            "proves nothing about how something sounded. Link the audio, or record it as "
            "observed or uncertain -- which is what it is.",
            details=(
                ErrorDetail(field="status", reason=status),
                ErrorDetail(field="basis", reason=basis),
            ),
        )
    if dimension in AUDIO_ONLY_DIMENSIONS:
        raise LinguaWikiError(
            "dimension_requires_audio",
            f"{reference} judges {dimension} from a {basis} basis, and text cannot carry "
            "it at all: the words of a question and the words of a flat statement are the "
            "same words. Link the audio, or judge a dimension the transcript can support.",
            details=(
                ErrorDetail(field="dimension", reason=dimension),
                ErrorDetail(field="basis", reason=basis),
            ),
        )


def invalidated_by_purge(*, status: str, dimension: str) -> bool:
    """Whether losing the audio invalidates this claim.

    Everything that needed the sound to be made needs it to go on standing. Language
    evidence from the same utterance does not: what the learner *said* was established by
    the transcript, and the transcript is still there.
    """

    return status == CONFIRMED_STATUS or dimension in AUDIO_ONLY_DIMENSIONS


def assert_policy_is_sound() -> None:
    """Refuse a policy that contradicts itself."""

    problems: list[str] = []
    if LAYERS[0] != "raw":
        problems.append("the first layer must be the one that arrived")
    if LAYER_SOURCES["raw"]:
        problems.append("the raw layer cannot derive from anything")
    for layer, permitted in LAYER_SOURCES.items():
        if layer not in LAYERS:
            problems.append(f"{layer} is not a known layer")
        for source in permitted:
            if LAYERS.index(source) >= LAYERS.index(layer):
                problems.append(f"{layer} derives from {source}, which is not earlier")
    if CONFIRMED_STATUS not in PRONUNCIATION_STATUSES:
        problems.append("the confirmed status is not a known pronunciation status")
    for dimension in AUDIO_ONLY_DIMENSIONS:
        if dimension not in PRONUNCIATION_DIMENSIONS:
            problems.append(f"audio-only dimension {dimension} is not a known dimension")
    if "learner-error" not in CLASSIFICATIONS:
        problems.append("a transcript must be able to record an actual learner error")
    if not invalidated_by_purge(status=CONFIRMED_STATUS, dimension="intelligibility"):
        problems.append("a confirmed claim must not survive the loss of its audio")
    if invalidated_by_purge(status="observed", dimension="intelligibility"):
        problems.append("an unconfirmed claim that never needed audio must survive its loss")
    if problems:
        raise LinguaWikiError(
            "unsound_transcript_policy",
            "the transcript policy contradicts itself: " + "; ".join(problems),
            details=tuple(ErrorDetail(field="policy", reason=problem) for problem in problems),
        )


__all__ = [
    "AUDIO_ONLY_DIMENSIONS",
    "CLASSIFICATIONS",
    "CONFIRMED_STATUS",
    "EVIDENCE_BASES",
    "LAYERS",
    "LAYER_SOURCES",
    "PRONUNCIATION_DIMENSIONS",
    "PRONUNCIATION_STATUSES",
    "PURGE_REASONS",
    "REVISION_KINDS",
    "TRANSCRIPT_POLICY_VERSION",
    "Hearing",
    "assert_acoustic_claim_has_audio",
    "assert_known",
    "assert_layer_derivation",
    "assert_policy_is_sound",
    "assert_revision_is_honest",
    "best_hearing",
    "disagreement",
    "invalidated_by_purge",
]
