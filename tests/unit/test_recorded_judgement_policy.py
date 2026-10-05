"""The two pure rules C5 adds: which runs may serve a spoken task, and what a judge may claim."""

from __future__ import annotations

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.evidence import (
    AI_CONFIDENCE_CEILING,
    JUDGEMENT_POLICY_VERSION,
    assert_judged_claim,
    judged_claim_applies,
)
from linguawiki.placement import (
    RECORDED_SCORING,
    RECORDING_NOT_PERMITTED,
    Candidate,
    recording_permitted,
    servable_candidate,
    servable_under,
)


def _candidate(*, task_type: str, modality: str, content_id: str = "cnt_x") -> Candidate:
    return Candidate(
        content_id=content_id,
        dimension="pronunciation",
        task_type=task_type,
        difficulty=1.0,
        content_family="family",
        modality=modality,
        level_code="A2",
        is_anchor=False,
    )


SPOKEN = _candidate(task_type="pronunciation-target", modality="speech")
WRITTEN = _candidate(task_type="extended-productive", modality="writing")


@pytest.mark.parametrize(
    ("preferences", "permitted"),
    [
        ({"audio_recording_available": True, "audio_retention_consent": True}, True),
        ({"audio_recording_available": True}, False),
        ({"audio_retention_consent": True}, False),
        # Equipment is not consent: `voice_available` adds the speech *modality* and gates
        # nothing here.
        ({"voice_available": True, "audio_retention_consent": True}, False),
        ({"audio_recording_available": "yes", "audio_retention_consent": True}, False),
    ],
)
def test_recording_needs_equipment_and_consent_and_nothing_else_stands_in(
    preferences: dict[str, object], permitted: bool
) -> None:
    assert recording_permitted(preferences) is permitted


def test_a_spoken_task_is_servable_only_under_recorded_scoring_with_recording_permitted() -> None:
    assert servable_candidate(
        SPOKEN, scoring=RECORDED_SCORING, recorded=frozenset(), recording=True
    )
    assert not servable_candidate(
        SPOKEN, scoring=RECORDED_SCORING, recorded=frozenset(), recording=False
    )
    assert not servable_candidate(SPOKEN, scoring="machine", recorded=frozenset(), recording=True)
    # A written productive task needs a judge with no recording to judge from.
    assert not servable_candidate(
        WRITTEN, scoring=RECORDED_SCORING, recorded=frozenset(), recording=True
    )


def test_a_dimension_emptied_by_the_recording_gate_says_so() -> None:
    allowed, reason = servable_under(
        (SPOKEN,), scoring=RECORDED_SCORING, recorded=frozenset(), recording=False
    )
    assert allowed == () and reason == RECORDING_NOT_PERMITTED


@pytest.mark.parametrize(
    ("kind", "modality", "applies"),
    [
        ("pronunciation", "speech", True),
        ("productive", "speech", True),
        ("productive", "writing", False),
        ("receptive", "audio", False),
    ],
)
def test_the_judgement_rule_covers_what_a_judge_hears(
    kind: str, modality: str, applies: bool
) -> None:
    assert judged_claim_applies(dimension_kind=kind, modality=modality) is applies


def test_an_ai_judge_names_itself_and_stays_at_or_below_its_ceiling() -> None:
    claim = {"dimension_kind": "pronunciation", "modality": "speech"}
    assert (
        assert_judged_claim(
            **claim, assessor_kind="ai", assessor="judge", confidence=AI_CONFIDENCE_CEILING
        )
        == JUDGEMENT_POLICY_VERSION
    )
    with pytest.raises(LinguaWikiError) as anonymous:
        assert_judged_claim(**claim, assessor_kind="ai", assessor=None, confidence="low")
    assert anonymous.value.payload.code == "assessor_required"
    with pytest.raises(LinguaWikiError) as overclaimed:
        assert_judged_claim(**claim, assessor_kind="ai", assessor="judge", confidence="high")
    assert overclaimed.value.payload.code == "assessor_confidence_ceiling"
    # A person may be confident; they still have to be named.
    assert assert_judged_claim(**claim, assessor_kind="human", assessor="t", confidence="high")
    with pytest.raises(LinguaWikiError):
        assert_judged_claim(**claim, assessor_kind="human", assessor="", confidence="high")


def test_a_score_the_rule_does_not_cover_carries_no_version() -> None:
    assert (
        assert_judged_claim(
            dimension_kind="productive",
            modality="writing",
            assessor_kind="ai",
            assessor=None,
            confidence="high",
        )
        is None
    )
