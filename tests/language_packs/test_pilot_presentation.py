"""Every `pl-pilot` task says how it is shown, and the right button scores.

The pilot is the pack the client is built against, so "renderable without parsing prose"
is a property of this pack or it is a property of nothing. The last test is the one that
matters: it takes the correct choice straight out of the pack and puts it through the
real scorer, because a contract that says a button submits its value is worth exactly as
much as the run that credits it.
"""

from __future__ import annotations

import pytest

from linguawiki.contracts import PackAssessmentTask
from linguawiki.packs.format import load_pack
from linguawiki.placement import score_response
from tests.conftest import PILOT_PACK


def pilot_tasks() -> list[PackAssessmentTask]:
    return [
        item.payload
        for item in load_pack(PILOT_PACK).tasks
        if isinstance(item.payload, PackAssessmentTask)
    ]


TASKS = pilot_tasks()
OBJECTIVE = [task for task in TASKS if task.task_type == "objective"]


def test_the_pilot_is_worth_testing_this_way() -> None:
    assert len(TASKS) == 39
    assert len(OBJECTIVE) == 12


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.stable_key)
def test_every_pilot_task_declares_how_it_is_shown(task: PackAssessmentTask) -> None:
    """Absent is legal in the contract and is not a decision anybody made here."""

    assert task.presentation is not None


@pytest.mark.parametrize("task", OBJECTIVE, ids=lambda task: task.stable_key)
def test_every_objective_task_is_answered_by_choosing(task: PackAssessmentTask) -> None:
    assert task.presentation is not None
    assert task.presentation.kind == "multiple-choice"
    assert len(task.presentation.choices) >= 2


@pytest.mark.parametrize("task", OBJECTIVE, ids=lambda task: task.stable_key)
def test_the_options_are_no_longer_hidden_in_the_prompt(task: PackAssessmentTask) -> None:
    """`"Which is correct? 'pięć bilety' / 'pięć biletów' / …"` is the shape this ends.

    The rule is about the *set* of options, not about any one string. A reading task's
    prompt quotes the passage, and the passage is where the answer is -- finding
    `pomidorowa` in `"Zupa dnia: pomidorowa -- 12 zł"` is the task working, not the
    options leaking. A prompt that holds every option is the smuggled list.
    """

    assert task.presentation is not None
    leaked = [choice.value for choice in task.presentation.choices if choice.value in task.prompt]
    assert len(leaked) < len(task.presentation.choices), f"the prompt still lists {leaked}"


@pytest.mark.parametrize("task", OBJECTIVE, ids=lambda task: task.stable_key)
def test_pressing_the_right_button_scores_and_the_others_do_not(
    task: PackAssessmentTask,
) -> None:
    assert task.presentation is not None and task.expected is not None
    scored = {
        choice.value: score_response(
            task_type=task.task_type, answers=task.expected.answers, response=choice.value
        )
        for choice in task.presentation.choices
    }
    credited = [value for value, score in scored.items() if score == 1.0]
    assert len(credited) == 1, f"{task.stable_key} credits {credited}"


def test_the_listening_tasks_declare_no_recording_the_pack_does_not_hold() -> None:
    """The pilot ships no audio, so no task may claim any.

    Recorded as a test rather than a note, because the fallback C2a names -- keep the
    prompt, declare no asset -- is only honest while nothing pretends otherwise. When
    the six recordings exist, this test is what has to be rewritten to say so.
    """

    listening = [task for task in TASKS if task.modality == "audio"]
    assert len(listening) == 6
    assert all(
        task.presentation is not None and task.presentation.audio is None for task in listening
    )
    assert load_pack(PILOT_PACK).assets == ()
