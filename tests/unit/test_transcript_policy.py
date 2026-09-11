"""The transcript rules, tested where they are decided.

The acoustic rule is the one worth reading: a correct transcript proves nothing about how
something sounded, and every case below is a way of getting that wrong.
"""

from __future__ import annotations

import pytest

from linguawiki import transcripts as transcript_policy
from linguawiki.errors import LinguaWikiError


def hearing(layer: str, text: str) -> transcript_policy.Hearing:
    return transcript_policy.Hearing(layer=layer, text=text)


def test_the_policy_is_internally_consistent() -> None:
    transcript_policy.assert_policy_is_sound()


def test_a_normalization_may_change_punctuation_and_casing_and_nothing_else() -> None:
    transcript_policy.assert_revision_is_honest(
        kind="normalization",
        before="dokad pan jedzie",
        after="Dokad pan jedzie?",
        reference="utt_001",
    )
    with pytest.raises(LinguaWikiError) as failure:
        transcript_policy.assert_revision_is_honest(
            kind="normalization",
            before="dokad pan jedzie",
            after="dokad pani jedzie",
            reference="utt_001",
        )
    assert failure.value.payload.code == "normalization_changed_the_words"


def test_a_hearing_may_change_the_words_because_that_is_what_it_claims() -> None:
    transcript_policy.assert_revision_is_honest(
        kind="hearing", before="dokad pan jedzie", after="dokad pani jedzie", reference="utt_001"
    )


def test_words_are_compared_without_transliterating_anything() -> None:
    """Two spellings differing by a diacritic are two hearings, not one tidy-up."""

    assert transcript_policy.same_words("Zrobię to.", "zrobię to")
    assert not transcript_policy.same_words("zrobię to", "zrobie to")


def test_disagreement_is_reported_rather_than_resolved() -> None:
    layers = [
        hearing("raw", "dokad pan jedzie"),
        hearing("normalized", "Dokad pan jedzie?"),
        hearing("reviewed-hearing", "Dokad pani jedzie?"),
    ]
    assert transcript_policy.disagreement(layers) == ("normalized vs reviewed-hearing",)
    assert transcript_policy.disagreement(layers[:2]) == ()


def test_the_best_hearing_is_the_most_reviewed_one_available() -> None:
    assert transcript_policy.best_hearing([]) is None
    assert transcript_policy.best_hearing([hearing("raw", "a")]).layer == "raw"
    assert (
        transcript_policy.best_hearing(
            [hearing("raw", "a"), hearing("reviewed-hearing", "b"), hearing("normalized", "c")]
        ).layer
        == "reviewed-hearing"
    )


@pytest.mark.parametrize("dimension", transcript_policy.PRONUNCIATION_DIMENSIONS)
def test_confirming_anything_needs_the_audio(dimension: str) -> None:
    with pytest.raises(LinguaWikiError):
        transcript_policy.assert_acoustic_claim_has_audio(
            status="confirmed", dimension=dimension, basis="transcript", reference="claim"
        )
    transcript_policy.assert_acoustic_claim_has_audio(
        status="confirmed", dimension=dimension, basis="audio", reference="claim"
    )


@pytest.mark.parametrize("dimension", transcript_policy.AUDIO_ONLY_DIMENSIONS)
@pytest.mark.parametrize("status", ("observed", "uncertain"))
def test_prosody_and_native_likeness_need_audio_at_every_status(
    dimension: str, status: str
) -> None:
    """The words of a question and the words of a flat statement are the same words."""

    with pytest.raises(LinguaWikiError) as failure:
        transcript_policy.assert_acoustic_claim_has_audio(
            status=status, dimension=dimension, basis="transcript", reference="claim"
        )
    assert failure.value.payload.code == "dimension_requires_audio"


def test_a_transcript_can_still_support_the_dimensions_it_can_support() -> None:
    for dimension in ("intelligibility", "phonetic-accuracy"):
        transcript_policy.assert_acoustic_claim_has_audio(
            status="observed", dimension=dimension, basis="transcript", reference="claim"
        )


def test_a_purge_invalidates_what_needed_the_sound_and_only_that() -> None:
    assert transcript_policy.invalidated_by_purge(status="confirmed", dimension="intelligibility")
    assert transcript_policy.invalidated_by_purge(status="observed", dimension="prosody")
    assert not transcript_policy.invalidated_by_purge(
        status="observed", dimension="intelligibility"
    )
    assert not transcript_policy.invalidated_by_purge(
        status="uncertain", dimension="phonetic-accuracy"
    )


def test_a_layer_cannot_derive_from_one_that_is_not_below_it() -> None:
    transcript_policy.assert_layer_derivation(layer="normalized", derived_from="raw")
    transcript_policy.assert_layer_derivation(layer="reviewed-hearing", derived_from="normalized")
    with pytest.raises(LinguaWikiError):
        transcript_policy.assert_layer_derivation(
            layer="normalized", derived_from="reviewed-hearing"
        )
    with pytest.raises(LinguaWikiError):
        transcript_policy.assert_layer_derivation(layer="raw", derived_from="normalized")
