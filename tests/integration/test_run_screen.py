"""One call per screen, assembled from a reader, carrying nothing the learner said.

The client needs the whole state of a run in one request: a reader is not free -- a
separate-process `open_reader` beside a held writer is refused -- so a screen built from
four calls is four chances to be told the database is busy.

What it may carry is bounded by the same rules as everything else. Outstanding tasks come
from the serve-time snapshot rather than the bank, and the learner's own words never appear:
this report has no consent parameter to check them against.
"""

from __future__ import annotations

import json

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import assessment as assessment_service
from linguawiki.services import assessment_view as view_service
from tests.conftest import PolishWorkspace

RESPONSE = "zupelnie wyjatkowa odpowiedz uczacego sie"


def _serve(workspace: PolishWorkspace, run_id: str) -> assessment_service.NextTaskReport:
    served = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
    assert isinstance(served, assessment_service.NextTaskReport)
    return served


def test_the_screen_carries_every_dimension_and_the_task_each_is_holding(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve(polish_workspace, run.run_id)

    screen = view_service.run_screen(polish_workspace.paths, run=run.run_id)

    assert screen.run_id == run.run_id
    assert screen.status == "in-progress"
    assert {dimension.dimension for dimension in screen.dimensions} == {
        dimension.dimension for dimension in run.dimensions
    }
    outstanding = {task.content_id: task for task in screen.outstanding}
    assert set(outstanding) == {served.content_id}
    assert outstanding[served.content_id].dimension == served.dimension
    assert outstanding[served.content_id].prompt == served.prompt


def test_the_screen_says_how_each_outstanding_task_is_answered(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    seen: set[str] = set()
    while len(seen) < 2:
        served = _serve(polish_workspace, run.run_id)
        if served.served_again:
            break
        seen.add(served.content_id)

    screen = view_service.run_screen(polish_workspace.paths, run=run.run_id)

    for task in screen.outstanding:
        chooses = task.presentation is not None and bool(task.presentation.choices)
        assert task.answer_with == ("choice" if chooses else "text")
        assert task.plays_audio == (
            task.presentation is not None and task.presentation.audio is not None
        )


def test_the_screen_shows_what_was_served_and_not_what_the_pack_now_says(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve(polish_workspace, run.run_id)
    with (
        open_writer(polish_workspace.paths, command="test.edit-bank") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_tasks SET prompt = ?, permitted_help = ? WHERE content_id = ?",
            ["a question nobody was asked", "dictionary-allowed", served.content_id],
        )

    screen = view_service.run_screen(polish_workspace.paths, run=run.run_id)

    task = next(entry for entry in screen.outstanding if entry.content_id == served.content_id)
    assert task.prompt == served.prompt != "a question nobody was asked"
    assert task.permitted_help == served.permitted_help


def test_a_stopped_dimension_still_shows_the_task_it_is_holding(
    polish_workspace: PolishWorkspace,
) -> None:
    """The guard runs only while a dimension is open, and `record` still accepts the task.

    `stopped` is the vocabulary `placement_dimension_state` actually permits -- `open`,
    `stopped`, `not-tested` -- so this is a state a real bank exhaustion produces.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve(polish_workspace, run.run_id)
    with (
        open_writer(polish_workspace.paths, command="test.close-dimension") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE placement_dimension_state SET status = 'stopped', stop_reason = 'tested' "
            "WHERE run_id = ? AND dimension = ?",
            [run.run_id, served.dimension],
        )

    screen = view_service.run_screen(polish_workspace.paths, run=run.run_id)

    assert served.content_id in {task.content_id for task in screen.outstanding}


def test_the_screen_never_carries_the_learner_s_own_words(
    polish_workspace: PolishWorkspace,
) -> None:
    """There is no consent parameter here to check a response against."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve(polish_workspace, run.run_id)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=0.5,
        response=RESPONSE,
        response_visibility="full",
        assessor_kind="ai",
        clock=polish_workspace.clock,
    )

    screen = view_service.run_screen(polish_workspace.paths, run=run.run_id)

    assert RESPONSE not in json.dumps(screen.model_dump(mode="json"), ensure_ascii=False)


def test_a_screen_for_a_run_that_does_not_exist_is_refused_by_name(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        view_service.run_screen(polish_workspace.paths, run="asm_01ARZ3NDEKTSV4RRFFQ69G5FAV")

    assert failure.value.payload.code == "assessment_run_not_found"


def test_the_cli_answers_the_same_screen_question(
    polish_workspace: PolishWorkspace,
) -> None:
    """Both entry points have to be answerable to the same cases to be compared at all."""

    import subprocess
    import sys

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve(polish_workspace, run.run_id)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "linguawiki",
            "assessment",
            "screen",
            "--run",
            run.run_id,
            "--workspace",
            str(polish_workspace.root),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["run_id"] == run.run_id
    assert [task["content_id"] for task in payload["data"]["outstanding"]] == [served.content_id]


def test_the_human_rendering_names_the_task_that_is_waiting(
    polish_workspace: PolishWorkspace,
) -> None:
    """A count is not actionable: an operator has to know which task to go and look at."""

    import subprocess
    import sys

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve(polish_workspace, run.run_id)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "linguawiki",
            "assessment",
            "screen",
            "--run",
            run.run_id,
            "--workspace",
            str(polish_workspace.root),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert served.content_id in result.stdout
    assert served.dimension in result.stdout
