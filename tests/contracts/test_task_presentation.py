"""A task says how it is shown, and a choice says what it submits.

The client must never parse a prompt to recover structure, and the scorer must never
learn what a choice is. Both follow from one decision: a button submits its choice's
`value`, verbatim, and that string is what `placement.score_response` compares against
the served answer key. Everything here enforces that decision at the only place a pack
can still be corrected -- before it is installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from linguawiki.contracts import PackAssessmentTask

BASE: dict[str, Any] = {
    "stable_key": "pl.task.one",
    "dimension": "reading",
    "level": "A2",
    "difficulty": 1.0,
    "content_family": "travel",
    "modality": "text",
    "prompt": "Which is correct?",
    "task_type": "objective",
    "expected": {"answers": ["pięć biletów"]},
    "provenance": {
        "origin_profile": "authored",
        "review_profile": "verified",
        "lifecycle": "verified",
        "risk_tier": 2,
        "content_hash": "a" * 64,
    },
}

CHOICES = [
    {"value": "pięć bilety"},
    {"value": "pięć biletów"},
    {"value": "pięć biletu"},
]


def task(**overrides: Any) -> PackAssessmentTask:
    return PackAssessmentTask.model_validate({**BASE, **overrides})


def test_a_task_without_a_presentation_record_is_free_text() -> None:
    """Absent is legal and means *render as free text*, which is what every shipped
    pack relies on: neither fixture pack is re-authored by this stage."""

    assert task().presentation is None


def test_multiple_choice_carries_typed_choices_and_a_display_form() -> None:
    shown = task(
        presentation={
            "kind": "multiple-choice",
            "choices": [
                {"value": "pięć bilety"},
                {"value": "pięć biletów", "display": "pięć biletów (5 tickets)"},
                {"value": "pięć biletu"},
            ],
        }
    ).presentation
    assert shown is not None
    assert shown.presentation_version == 1
    assert shown.order == "fixed"
    assert tuple(choice.value for choice in shown.choices) == (
        "pięć bilety",
        "pięć biletów",
        "pięć biletu",
    )
    # `display` is drawn, never submitted and never scored; it defaults to the value so
    # a renderer has one field to read.
    assert shown.choices[1].display == "pięć biletów (5 tickets)"
    assert shown.choices[0].display == "pięć bilety"


@pytest.mark.parametrize(
    ("presentation", "because"),
    [
        ({"kind": "multiple-choice", "choices": [{"value": "only"}]}, "one button is not a choice"),
        ({"kind": "multiple-choice", "choices": []}, "no buttons at all"),
        ({"kind": "free-text", "choices": CHOICES}, "free text has no buttons"),
        ({"kind": "free-text", "order": "shuffled"}, "nothing to shuffle"),
        (
            {"kind": "multiple-choice", "choices": CHOICES, "response_shape": "a number"},
            "a button is not a written response",
        ),
        (
            {"kind": "multiple-choice", "choices": [{"value": "a"}, {"value": "   "}]},
            "min_length=1 accepts whitespace, and no comparison can ever match it",
        ),
        (
            {"kind": "multiple-choice", "choices": [{"value": "Tak"}, {"value": " tak "}]},
            "two buttons that fold to one string are one answer shown twice",
        ),
        (
            {"kind": "multiple-choice", "choices": CHOICES, "audio": {"asset_key": "pl.a"}},
            "a text task plays nothing",
        ),
        ({"kind": "unheard-of", "choices": CHOICES}, "unknown kind"),
        (
            {"kind": "multiple-choice", "choices": CHOICES, "replay_allowance": 2},
            "a replay allowance lives inside the audio record it is about",
        ),
    ],
)
def test_a_presentation_record_that_contradicts_itself_is_refused(
    presentation: dict[str, Any], because: str
) -> None:
    with pytest.raises(PydanticValidationError):
        task(presentation=presentation)


def test_choices_belong_to_an_objective_task_only() -> None:
    with pytest.raises(PydanticValidationError):
        task(
            task_type="extended-productive",
            expected=None,
            rubric={"version": 1},
            presentation={"kind": "multiple-choice", "choices": CHOICES},
        )
    # A short-response task is machine-scored but typed into, not clicked.
    task(
        task_type="short-response",
        presentation={"kind": "free-text", "response_shape": "a noun phrase"},
    )


def test_an_audio_record_belongs_to_a_task_the_learner_listens_to() -> None:
    with pytest.raises(PydanticValidationError):
        task(presentation={"kind": "free-text", "audio": {"asset_key": "pl.audio.one"}})
    listening = task(
        modality="audio",
        task_type="short-response",
        presentation={
            "kind": "free-text",
            "audio": {"asset_key": "pl.audio.one", "replay_allowance": 3},
        },
    ).presentation
    assert listening is not None and listening.audio is not None
    assert listening.audio.asset_key == "pl.audio.one"
    assert listening.audio.replay_allowance == 3


def test_unlimited_replay_is_an_absent_allowance_rather_than_a_large_number() -> None:
    listening = task(
        modality="audio",
        task_type="short-response",
        presentation={"kind": "free-text", "audio": {"asset_key": "pl.audio.one"}},
    ).presentation
    assert listening is not None and listening.audio is not None
    assert listening.audio.replay_allowance is None
    with pytest.raises(PydanticValidationError):
        task(
            modality="audio",
            task_type="short-response",
            presentation={
                "kind": "free-text",
                "audio": {"asset_key": "pl.audio.one", "replay_allowance": 0},
            },
        )


def test_exactly_one_choice_must_answer_the_key() -> None:
    """Zero matches is a task nobody can answer; two is a task with two right buttons.

    The comparison is `placement.scoring_form` -- the one that will actually score it --
    so a pack cannot pass validation on an equality the scorer does not share.
    """

    with pytest.raises(PydanticValidationError):
        task(
            expected={"answers": ["sześć biletów"]},
            presentation={"kind": "multiple-choice", "choices": CHOICES},
        )
    with pytest.raises(PydanticValidationError):
        task(
            expected={"answers": ["pięć biletów", "pięć biletu"]},
            presentation={"kind": "multiple-choice", "choices": CHOICES},
        )
    # Several accepted spellings of one button stay legal: the key may hold synonyms as
    # long as they all point at the same choice.
    task(
        expected={"answers": ["pięć biletów", "PIĘĆ  BILETÓW"]},
        presentation={"kind": "multiple-choice", "choices": CHOICES},
    )


def test_the_key_is_matched_against_the_value_and_never_the_display_form() -> None:
    with pytest.raises(PydanticValidationError):
        task(
            expected={"answers": ["pięć biletów (5 tickets)"]},
            presentation={
                "kind": "multiple-choice",
                "choices": [
                    {"value": "pięć bilety"},
                    {"value": "pięć biletów", "display": "pięć biletów (5 tickets)"},
                ],
            },
        )


def test_the_correct_choice_value_is_what_the_scorer_credits() -> None:
    """The contract's whole point, asserted against the scorer rather than restated."""

    from linguawiki.placement import score_response

    shown = task(presentation={"kind": "multiple-choice", "choices": CHOICES})
    assert shown.expected is not None and shown.presentation is not None
    answers = shown.expected.answers
    scores = {
        choice.value: score_response(
            task_type=shown.task_type, answers=answers, response=choice.value
        )
        for choice in shown.presentation.choices
    }
    assert scores == {"pięć bilety": 0.0, "pięć biletów": 1.0, "pięć biletu": 0.0}
