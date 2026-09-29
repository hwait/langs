"""The deterministic scoring rule, on its own: no database, no pack, no model.

The comparison is the policy. Everything it deliberately does *not* do is a rule too --
a dropped diacritic is a different word, and differently placed punctuation is a
different answer -- because any form a pack wants accepted belongs in `answers`, where a
reviewer can see it.
"""

from __future__ import annotations

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.placement import MACHINE_SCORABLE_TASK_TYPES, score_response, scoring_form


def _score(response: str, *answers: str, task_type: str = "short-response") -> float:
    return score_response(task_type=task_type, answers=answers, response=response)


@pytest.mark.parametrize("task_type", MACHINE_SCORABLE_TASK_TYPES)
def test_an_exact_answer_scores_one_and_another_answer_scores_zero(task_type: str) -> None:
    assert _score("w środę", "w środę", task_type=task_type) == 1.0
    assert _score("w czwartek", "w środę", task_type=task_type) == 0.0


def test_any_declared_answer_matches() -> None:
    assert _score("owszem", "tak", "owszem") == 1.0


@pytest.mark.parametrize(
    ("response", "why"),
    [
        ("W ŚRODĘ", "case"),
        ("  w środę  ", "surrounding whitespace"),
        ("w\tśrodę", "a tab is whitespace"),
        ("w    środę", "a collapsed internal run"),
        ("w środę", "decomposed rather than precomposed"),
    ],
)
def test_normalization_accepts_the_same_answer_written_differently(response: str, why: str) -> None:
    assert _score(response, "w środę") == 1.0, why


@pytest.mark.parametrize(
    ("response", "why"),
    [
        ("w srode", "a dropped diacritic is a different word"),
        ("wśrodę", "a dropped separator is a different string"),
        ("w, środę", "punctuation is part of the answer"),
    ],
)
def test_normalization_does_not_reach_past_case_and_whitespace(response: str, why: str) -> None:
    assert _score(response, "w środę") == 0.0, why


def test_punctuation_is_not_stripped_so_two_readings_stay_distinct() -> None:
    """`normalize_identity` would score both of these the same; scoring must not."""

    assert _score("nie wiem", "nie wiem") == 1.0
    assert _score("nie, wiem", "nie wiem") == 0.0
    assert scoring_form("nie, wiem") != scoring_form("nie wiem")


@pytest.mark.parametrize("response", ["", "   ", "\n\t "])
def test_a_blank_response_is_a_skip_rather_than_a_wrong_answer(response: str) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        _score(response, "w środę")

    assert failure.value.payload.code == "invalid_arguments"
    assert failure.value.payload.details[0].field == "response"


@pytest.mark.parametrize(
    "task_type", ["extended-productive", "pronunciation-target", "connected-speech"]
)
def test_a_rubric_scored_task_is_refused_by_name(task_type: str) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        _score("anything", "w środę", task_type=task_type)

    assert failure.value.payload.code == "assessment_not_machine_scorable"
    assert task_type in failure.value.payload.message


def test_a_key_that_accepts_nothing_is_refused_rather_than_scoring_zero() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        score_response(task_type="objective", answers=(), response="w środę")

    assert failure.value.payload.code == "assessment_answer_key_malformed"
