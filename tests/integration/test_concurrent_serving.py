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


def _audit(workspace: PolishWorkspace) -> list[tuple[str, str, str]]:
    with open_reader(workspace.paths) as database:
        return [
            (str(actor), str(command), str(affected))
            for actor, command, affected in database.query(
                "SELECT actor, command, affected_records_json FROM audit_log "
                "ORDER BY recorded_at, audit_id"
            )
        ]


def test_serving_and_scoring_each_leave_an_audit_row(
    polish_workspace: PolishWorkspace,
) -> None:
    """The two mutations the client loop runs constantly wrote no audit row at all."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )

    commands = [command for _, command, _ in _audit(polish_workspace)]

    assert commands.count("assessment.next") == 1
    assert commands.count("assessment.record") == 1
    served_row = next(row for row in _audit(polish_workspace) if row[1] == "assessment.next")
    assert served.content_id in served_row[2]
    assert run.run_id in served_row[2]


def test_the_audit_row_names_the_surface_without_renaming_the_command(
    polish_workspace: PolishWorkspace,
) -> None:
    """`actor` is the one honest difference; a per-surface command name is not.

    A different command per entry point would make every audit query ask twice, and
    ADR 0008 requires the trail to read as a CLI-driven one.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=1.0,
        actor="client",
        clock=polish_workspace.clock,
    )

    rows = _audit(polish_workspace)

    assert [actor for actor, command, _ in rows if command == "assessment.next"] == ["cli"]
    assert [actor for actor, command, _ in rows if command == "assessment.record"] == ["client"]


def test_handing_a_task_back_leaves_no_audit_row(
    polish_workspace: PolishWorkspace,
) -> None:
    """Nothing was mutated, and an audit row for a read records something that did not happen."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    while True:
        if _serve_one(polish_workspace, run.run_id).served_again:
            break
    before = len(_audit(polish_workspace))

    _serve_one(polish_workspace, run.run_id)

    assert len(_audit(polish_workspace)) == before


def test_a_retried_serve_returns_the_task_it_first_served(
    polish_workspace: PolishWorkspace,
) -> None:
    """`next_task` took no key, so a retry consumed another task and burned its exposure."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    first = assessment_service.next_task(
        polish_workspace.paths,
        run=run.run_id,
        idempotency_key="serve-1",
        clock=polish_workspace.clock,
    )
    assert isinstance(first, assessment_service.NextTaskReport)
    rows_before = _row_count(polish_workspace, run.run_id)

    again = assessment_service.next_task(
        polish_workspace.paths,
        run=run.run_id,
        idempotency_key="serve-1",
        clock=polish_workspace.clock,
    )

    assert isinstance(again, assessment_service.NextTaskReport)
    assert again.content_id == first.content_id
    assert again.served_again
    assert _row_count(polish_workspace, run.run_id) == rows_before
    assert _exposure(polish_workspace, first.content_id)[0] == 1


def test_a_serve_key_reused_for_another_run_is_a_conflict(
    polish_workspace: PolishWorkspace,
) -> None:
    import pytest

    from linguawiki.errors import LinguaWikiError

    first_run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assessment_service.next_task(
        polish_workspace.paths,
        run=first_run.run_id,
        idempotency_key="serve-1",
        clock=polish_workspace.clock,
    )
    assessment_service.finalize(
        polish_workspace.paths, run=first_run.run_id, clock=polish_workspace.clock
    )
    second_run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.next_task(
            polish_workspace.paths,
            run=second_run.run_id,
            idempotency_key="serve-1",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"
    assert first_run.run_id in failure.value.payload.message


def test_a_record_key_reused_with_a_different_score_is_a_conflict_not_a_crash(
    polish_workspace: PolishWorkspace,
) -> None:
    """The unique index on `domain_events.idempotency_key` had no preflight at all.

    A reused key died on a raw `ConstraintException`, surfaced as `internal_error`, after
    every refusal `record` carefully places before its transaction.
    """

    import pytest

    from linguawiki.errors import LinguaWikiError

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    first = _serve_one(polish_workspace, run.run_id)
    second = _serve_one(polish_workspace, run.run_id)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=first.content_id,
        score=1.0,
        idempotency_key="score-1",
        clock=polish_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=second.content_id,
            score=0.0,
            idempotency_key="score-1",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"


def test_a_retried_record_replays_rather_than_scoring_twice(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_one(polish_workspace, run.run_id)
    arguments = {
        "run": run.run_id,
        "content_id": served.content_id,
        "score": 1.0,
        "idempotency_key": "score-1",
    }
    first = assessment_service.record(
        polish_workspace.paths, clock=polish_workspace.clock, **arguments
    )

    again = assessment_service.record(
        polish_workspace.paths, clock=polish_workspace.clock, **arguments
    )

    assert again.tasks_recorded == first.tasks_recorded
    with open_reader(polish_workspace.paths) as database:
        results = database.scalar(
            "SELECT count(*) FROM assessment_results WHERE run_id = ?", [run.run_id]
        )
    assert int(results) == 1


def test_a_key_belonging_to_another_operation_is_refused_by_name(
    polish_workspace: PolishWorkspace,
) -> None:
    """An idempotency key identifies one operation, in both directions."""

    import pytest

    from linguawiki.errors import LinguaWikiError

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths,
        run=run.run_id,
        idempotency_key="shared",
        clock=polish_workspace.clock,
    )
    assert isinstance(served, assessment_service.NextTaskReport)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            score=1.0,
            idempotency_key="shared",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"
    assert "assessment.served" in failure.value.payload.message


def test_a_finalize_key_reused_for_another_run_is_a_conflict(
    polish_workspace: PolishWorkspace,
) -> None:
    """`finalize` wrote the caller's key into the same index with no preflight either."""

    import pytest

    from linguawiki.errors import LinguaWikiError

    first = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assessment_service.finalize(
        polish_workspace.paths,
        run=first.run_id,
        idempotency_key="close-1",
        clock=polish_workspace.clock,
    )
    second = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.finalize(
            polish_workspace.paths,
            run=second.run_id,
            idempotency_key="close-1",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "idempotency_conflict"
    assert first.run_id in failure.value.payload.message


def test_the_cli_can_retry_a_serve_without_consuming_a_second_task(
    polish_workspace: PolishWorkspace,
) -> None:
    import json
    import subprocess
    import sys

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    command = [
        sys.executable,
        "-m",
        "linguawiki",
        "assessment",
        "next",
        "--run",
        run.run_id,
        "--idempotency-key",
        "cli-serve-1",
        "--workspace",
        str(polish_workspace.root),
        "--format",
        "json",
    ]
    first = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)

    again = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)

    assert again["data"]["content_id"] == first["data"]["content_id"]
    assert again["data"]["served_again"] is True
    assert _row_count(polish_workspace, run.run_id) == 1


def test_a_report_says_whether_the_task_it_names_is_still_awaiting_an_answer(
    polish_workspace: PolishWorkspace,
) -> None:
    """A replay of a serve whose task has since been answered looked like fresh work.

    `NextTaskReport` carried no status, so a hand-back of an outstanding task and a replay of
    a key whose task is settled were the same object -- and a client would have put an
    answered question back in front of the learner.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths,
        run=run.run_id,
        idempotency_key="serve-1",
        clock=polish_workspace.clock,
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assert served.status == "served"
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )

    replayed = assessment_service.next_task(
        polish_workspace.paths,
        run=run.run_id,
        idempotency_key="serve-1",
        clock=polish_workspace.clock,
    )

    assert isinstance(replayed, assessment_service.NextTaskReport)
    assert replayed.content_id == served.content_id
    assert replayed.served_again
    assert replayed.status == "answered"


def test_a_key_that_handed_a_task_back_cannot_later_serve_a_different_one(
    polish_workspace: PolishWorkspace,
) -> None:
    """A hand-back is still one operation, and a key names one operation.

    The hand-back wrote nothing at all, so a retry after the task had been answered found no
    record of the key and went on to serve a *different* task under it.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    while True:
        if _serve_one(polish_workspace, run.run_id).served_again:
            break
    handed = assessment_service.next_task(
        polish_workspace.paths,
        run=run.run_id,
        idempotency_key="handback-1",
        clock=polish_workspace.clock,
    )
    assert isinstance(handed, assessment_service.NextTaskReport)
    assert handed.served_again
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=handed.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )

    again = assessment_service.next_task(
        polish_workspace.paths,
        run=run.run_id,
        idempotency_key="handback-1",
        clock=polish_workspace.clock,
    )

    assert isinstance(again, assessment_service.NextTaskReport)
    assert again.content_id == handed.content_id
    assert again.status == "answered"
