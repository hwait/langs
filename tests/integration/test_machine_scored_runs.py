"""A run opened for machine scoring serves only what the server can score.

Modality and scoring are two axes. Modality is what the learner's equipment allows;
scoring is what the server can decide without a judge. The default run includes
`writing`, which needs one, and restricting to `text` alone makes listening untestable --
so a browser run asks for the scoring condition by name, and the condition is stored on
the run and honoured by every later serve rather than re-supplied by the caller.
"""

from __future__ import annotations

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.placement import MACHINE_SCORABLE_TASK_TYPES
from linguawiki.services import assessment as assessment_service
from tests.conftest import PolishWorkspace


def _by_dimension(report: assessment_service.AssessmentRunReport) -> dict[str, str | None]:
    return {entry.dimension: entry.unavailable_reason for entry in report.dimensions}


def test_a_machine_run_closes_what_it_cannot_score_with_the_reason(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(
        polish_workspace.paths,
        scoring="machine",
        modalities=["text", "audio"],
        clock=polish_workspace.clock,
    )

    reasons = _by_dimension(run)
    assert run.scoring == "machine"
    # Text dimensions with comparable tasks stay open.
    for open_dimension in ("grammar-control", "vocabulary-control", "reading"):
        assert reasons[open_dimension] is None, open_dimension
    # Listening has machine-scorable tasks, and the pilot ships no recording for them: a
    # prompt drawn instead of a recording would be a reading test under another name.
    assert reasons["listening"] == "its listening tasks ship no recording"
    # Writing is excluded by modality before scoring is asked.
    assert "writing" in run.untested_dimensions
    assert "listening" in run.untested_dimensions


def test_a_machine_run_with_writing_available_still_refuses_judged_tasks(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(
        polish_workspace.paths,
        scoring="machine",
        modalities=["text", "writing"],
        clock=polish_workspace.clock,
    )

    assert _by_dimension(run)["writing"] == "no machine-scorable task"


def test_a_machine_run_never_serves_a_task_that_needs_a_judge(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(
        polish_workspace.paths,
        scoring="machine",
        modalities=["text", "audio", "writing", "speech"],
        clock=polish_workspace.clock,
    )
    served_types: set[str] = set()
    for _ in range(80):
        task = assessment_service.next_task(
            polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
        )
        if isinstance(task, assessment_service.AssessmentRunReport):
            break
        served_types.add(task.task_type)
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=task.content_id,
            score=0.5,
            assessor_kind="human",
            clock=polish_workspace.clock,
        )

    assert served_types, "the run served nothing"
    assert served_types <= set(MACHINE_SCORABLE_TASK_TYPES)


def test_the_condition_is_stored_rather_than_resupplied(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(
        polish_workspace.paths, scoring="machine", clock=polish_workspace.clock
    )

    with open_reader(polish_workspace.paths) as database:
        conditions = database.scalar(
            "SELECT conditions_json FROM assessment_runs WHERE run_id = ?", [run.run_id]
        )
    assert '"scoring": "machine"' in str(conditions)
    assert assessment_service.report(polish_workspace.paths, run=run.run_id).scoring == "machine"


def test_the_default_is_any_and_a_run_without_the_condition_reads_as_any(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    assert run.scoring == "any"
    import json

    from linguawiki.db.connection import open_writer

    with open_writer(polish_workspace.paths, command="test.legacy") as database:
        raw = database.scalar(
            "SELECT conditions_json FROM assessment_runs WHERE run_id = ?", [run.run_id]
        )
        conditions = json.loads(str(raw))
        del conditions["scoring"]
        database.execute(
            "UPDATE assessment_runs SET conditions_json = ? WHERE run_id = ?",
            [json.dumps(conditions, sort_keys=True), run.run_id],
        )
    assert assessment_service.report(polish_workspace.paths, run=run.run_id).scoring == "any"


def test_the_condition_is_part_of_what_a_start_key_promises(
    polish_workspace: PolishWorkspace,
) -> None:
    assessment_service.start(
        polish_workspace.paths, idempotency_key="start-1", clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as refused:
        assessment_service.start(
            polish_workspace.paths,
            scoring="machine",
            idempotency_key="start-1",
            clock=polish_workspace.clock,
        )
    assert refused.value.payload.code == "idempotency_conflict"


def test_an_unknown_condition_is_refused_by_name(polish_workspace: PolishWorkspace) -> None:
    with pytest.raises(LinguaWikiError) as refused:
        assessment_service.start(
            polish_workspace.paths, scoring="judged", clock=polish_workspace.clock
        )
    assert refused.value.payload.code == "invalid_arguments"
    assert "machine" in refused.value.payload.message


def test_the_screen_says_which_outstanding_task_needs_a_judge(
    polish_workspace: PolishWorkspace,
) -> None:
    """A run the browser did not shape can be holding judged work it must not answer."""

    from linguawiki.services import assessment_view as view_service

    run = assessment_service.start(
        polish_workspace.paths,
        dimensions=["writing"],
        modalities=["writing"],
        clock=polish_workspace.clock,
    )
    assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )

    screen = view_service.run_screen(polish_workspace.paths, run=run.run_id)
    assert screen.scoring == "any"
    assert [task.needs_judge for task in screen.outstanding] == [True]


def test_the_cli_opens_a_machine_run(
    polish_workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from linguawiki.cli import run

    code = run(
        [
            "assessment",
            "start",
            "--scoring",
            "machine",
            "--workspace",
            str(polish_workspace.root),
            "--format",
            "json",
        ],
        clock=polish_workspace.clock,
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["data"]["scoring"] == "machine"


def test_the_screen_says_which_heard_task_has_no_recording(
    polish_workspace: PolishWorkspace,
) -> None:
    """A run opened under `any` can hold a listening task the pilot cannot play.

    It is machine-scorable, so `needs_judge` is false -- and drawing its prompt would turn a
    listening task into a reading one. The screen says so, so a page does not offer it.
    """

    from linguawiki.services import assessment_view as view_service

    run = assessment_service.start(
        polish_workspace.paths, dimensions=["listening"], clock=polish_workspace.clock
    )
    assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )

    (task,) = view_service.run_screen(polish_workspace.paths, run=run.run_id).outstanding
    assert (task.modality, task.needs_judge, task.plays_audio) == ("audio", False, False)
    assert task.missing_recording is True
