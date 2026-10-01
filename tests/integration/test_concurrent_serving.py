"""What two callers may get, and what a second serve of one task may say.

From C3 a browser and the CLI are live at once, so "serve the next task" stops being a
question only one caller asks. Three rules hold it together, and all three live in the
service layer rather than in the server, because a guard the server owns is a guard the
CLI does not have:

* a dimension holding an unanswered task is not served another;
* when no open dimension is free the outstanding task is handed back *from its snapshot*,
  writing nothing -- the exposure was recorded when it was first served, and counting it
  twice pushes an item out of the reuse window on the strength of a task nobody answered;
* an idempotency key identifies one operation, in both directions, so a retry replays and
  a reused key carrying something else is a conflict rather than somebody else's answer.
"""

from __future__ import annotations

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.services import assessment as assessment_service
from tests.conftest import PolishWorkspace

#: A task the pilot serves with a help allowance that is not the column default, so a
#: snapshot that silently fell back to `none` is visible rather than plausible.
CONSTRAINED_HELP = "dictionary-not-allowed"


def _serve_one(workspace: PolishWorkspace, run_id: str) -> assessment_service.NextTaskReport:
    served = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
    assert isinstance(served, assessment_service.NextTaskReport), "the bank served nothing"
    return served


def _snapshotted_help(workspace: PolishWorkspace, run_id: str, content_id: str) -> object:
    with open_reader(workspace.paths) as database:
        row = database.one(
            "SELECT permitted_help FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
            [run_id, content_id],
        )
    assert row is not None
    return row[0]


def _rewrite_bank_help(workspace: PolishWorkspace, content_id: str, allowance: str) -> None:
    """Change the help allowance the *bank* declares, leaving the served row alone."""

    with (
        open_writer(
            workspace.paths, command="test.rewrite-help", clock=workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_tasks SET permitted_help = ? WHERE content_id = ?",
            [allowance, content_id],
        )


def test_the_served_snapshot_keeps_the_help_allowance_it_was_served_with(
    polish_workspace: PolishWorkspace,
) -> None:
    """`permitted_help` lived only on the bank, so a second serve read a mutable pack."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)

    stored = _snapshotted_help(polish_workspace, run.run_id, served.content_id)
    assert stored == served.permitted_help

    _rewrite_bank_help(polish_workspace, served.content_id, CONSTRAINED_HELP)
    after = assessment_service.served_task(
        polish_workspace.paths, run=run.run_id, content_id=served.content_id
    )

    assert after.permitted_help == served.permitted_help
    assert after.permitted_help != CONSTRAINED_HELP


def test_the_served_report_carries_the_rubric_body_and_not_only_its_version(
    polish_workspace: PolishWorkspace,
) -> None:
    """`rubric_version` names a body the report was not handing over."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)

    reported = assessment_service.served_task(
        polish_workspace.paths, run=run.run_id, content_id=served.content_id
    )

    assert reported.rubric == served.rubric
    assert reported.rubric_version == served.rubric_version


def _checks(workspace: PolishWorkspace) -> dict[str, object]:
    from linguawiki.db.integrity import check_database

    with open_reader(workspace.paths) as database:
        report = check_database(database)
    return {check.name: check for check in report.checks}


def _damage_help(workspace: PolishWorkspace, run_id: str, content_id: str, value: object) -> None:
    with (
        open_writer(workspace.paths, command="test.damage-help") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_run_tasks SET permitted_help = ? "
            "WHERE run_id = ? AND content_id = ?",
            [value, run_id, content_id],
        )


def test_a_healthy_workspace_passes_the_help_allowance_check(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    _serve_one(polish_workspace, run.run_id)

    check = _checks(polish_workspace).get("served_help_allowance_wellformed")

    assert check is not None, "served_help_allowance_wellformed did not run"
    assert check.status == "ok", check.message  # type: ignore[attr-defined]


def test_db_check_names_a_served_row_whose_help_allowance_is_blank(
    polish_workspace: PolishWorkspace,
) -> None:
    """`length(permitted_help) > 0` guards the bank's column and cannot guard this one."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)
    _damage_help(polish_workspace, run.run_id, served.content_id, "   ")

    check = _checks(polish_workspace)["served_help_allowance_wellformed"]

    assert check.status == "failed"  # type: ignore[attr-defined]
    # The rows, not a count: an operator has to know which one to go and look at.
    assert served.content_id in check.context["served"]  # type: ignore[attr-defined]


def test_a_row_served_before_the_column_existed_is_not_damage(
    polish_workspace: PolishWorkspace,
) -> None:
    """NULL is truthful -- no snapshot held the allowance -- and must not read as blank."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)
    _damage_help(polish_workspace, run.run_id, served.content_id, None)

    check = _checks(polish_workspace)["served_help_allowance_wellformed"]

    assert check.status == "ok", check.message  # type: ignore[attr-defined]


def _exposure(workspace: PolishWorkspace, content_id: str) -> tuple[int, int, object]:
    with open_reader(workspace.paths) as database:
        row = database.one(
            "SELECT exposure_count, answered_count, last_exposed_at "
            "FROM assessment_item_exposures WHERE content_id = ?",
            [content_id],
        )
    assert row is not None
    return int(row[0]), int(row[1]), row[2]


def _row_count(workspace: PolishWorkspace, run_id: str) -> int:
    with open_reader(workspace.paths) as database:
        return int(
            database.scalar("SELECT count(*) FROM assessment_run_tasks WHERE run_id = ?", [run_id])
        )


def test_a_dimension_holding_an_unanswered_task_is_not_served_another(
    polish_workspace: PolishWorkspace,
) -> None:
    """Two serves with no answer between them probed one dimension twice."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    first = _serve_one(polish_workspace, run.run_id)
    second = _serve_one(polish_workspace, run.run_id)

    assert second.dimension != first.dimension
    assert not second.served_again


def test_when_every_open_dimension_is_outstanding_the_task_is_handed_back(
    polish_workspace: PolishWorkspace,
) -> None:
    """A screen with unanswered work on it still has work on it."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    fresh: list[assessment_service.NextTaskReport] = []
    while True:
        served = _serve_one(polish_workspace, run.run_id)
        if served.served_again:
            break
        fresh.append(served)
        assert len(fresh) < 20, "the guard never engaged"

    assert len({report.dimension for report in fresh}) == len(fresh), "a dimension repeated"
    assert served.content_id in {report.content_id for report in fresh}
    # The least-progressed open dimension, which with nothing answered is the first by
    # name -- the order `next_task` already sorts by, not a second rule.
    assert served.dimension == min(report.dimension for report in fresh)
    assert served.selection_reason == "outstanding"


def test_handing_a_task_back_writes_nothing(
    polish_workspace: PolishWorkspace,
) -> None:
    """The exposure was recorded when it was first served.

    Counting it twice pushes the item out of the six-month reuse window on the strength of
    a task the learner never answered once.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    outstanding: list[str] = []
    while True:
        served = _serve_one(polish_workspace, run.run_id)
        if served.served_again:
            break
        outstanding.append(served.content_id)

    rows_before = _row_count(polish_workspace, run.run_id)
    exposure_before = _exposure(polish_workspace, served.content_id)

    again = _serve_one(polish_workspace, run.run_id)

    assert again.served_again
    assert again.content_id == served.content_id
    assert _row_count(polish_workspace, run.run_id) == rows_before
    assert _exposure(polish_workspace, served.content_id) == exposure_before


def test_a_task_handed_back_is_the_one_that_was_served_not_the_one_the_pack_now_holds(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    while True:
        served = _serve_one(polish_workspace, run.run_id)
        if served.served_again:
            break
    _rewrite_bank_help(polish_workspace, served.content_id, CONSTRAINED_HELP)

    again = _serve_one(polish_workspace, run.run_id)

    assert again.content_id == served.content_id
    assert again.permitted_help == served.permitted_help != CONSTRAINED_HELP
    assert again.prompt == served.prompt
    assert again.rubric == served.rubric
    assert again.presentation == served.presentation


def test_a_damaged_snapshot_refuses_rather_than_serving_a_different_dimension(
    polish_workspace: PolishWorkspace,
) -> None:
    """Falling through would hide the damage behind a task that happens to work."""

    import pytest

    from linguawiki.errors import LinguaWikiError

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    while True:
        served = _serve_one(polish_workspace, run.run_id)
        if served.served_again:
            break
    with (
        open_writer(polish_workspace.paths, command="test.damage") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_run_tasks SET presentation_json = ? "
            "WHERE run_id = ? AND content_id = ?",
            ['{"kind": "unheard-of"}', run.run_id, served.content_id],
        )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.next_task(
            polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "assessment_presentation_malformed"


def _cli_next(workspace: PolishWorkspace, run_id: str) -> dict[str, object]:
    """Serve through a real second process, which is the only honest form of this test.

    One caller invoked twice passes against an in-process cache; two processes is what a
    browser beside a CLI actually is.
    """

    import json
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "linguawiki",
            "assessment",
            "next",
            "--run",
            run_id,
            "--workspace",
            str(workspace.root),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload: dict[str, object] = json.loads(result.stdout)
    assert payload["ok"] is True, payload
    data: dict[str, object] = payload["data"]  # type: ignore[assignment]
    return data


def test_two_independent_callers_cannot_hold_two_tasks_in_one_dimension(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)

    through_the_cli = _cli_next(polish_workspace, run.run_id)

    assert through_the_cli["dimension"] != served.dimension
    assert through_the_cli["content_id"] != served.content_id


def test_the_cli_is_handed_back_the_task_the_service_call_left_open(
    polish_workspace: PolishWorkspace,
) -> None:
    """The guard and the hand-back are one rule, so both processes see the same one."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    while True:
        served = _serve_one(polish_workspace, run.run_id)
        if served.served_again:
            break

    through_the_cli = _cli_next(polish_workspace, run.run_id)

    assert through_the_cli["served_again"] is True
    assert through_the_cli["content_id"] == served.content_id
    assert through_the_cli["selection_reason"] == "outstanding"
