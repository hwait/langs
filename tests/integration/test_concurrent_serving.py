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
