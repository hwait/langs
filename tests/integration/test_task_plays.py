"""How often the learner played a recording, recorded where it cannot be talked up.

Replays are evidence. A play is the learner pressing play: it is recorded server-side
*before* the recording is heard, refused past a finite allowance, and counted by `record`
from the rows rather than accepted from the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import assessment as assessment_service
from linguawiki.services import assessment_view as view_service
from tests.conftest import PolishWorkspace
from tests.support.recordings import publish_pilot_with_recordings


def _listening_run(workspace: PolishWorkspace) -> assessment_service.AssessmentRunReport:
    return assessment_service.start(
        workspace.paths,
        dimensions=["listening"],
        modalities=["audio"],
        scoring="machine",
        clock=workspace.clock,
    )


def _serve(workspace: PolishWorkspace, run_id: str) -> assessment_service.NextTaskReport:
    task = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
    assert isinstance(task, assessment_service.NextTaskReport)
    return task


def _play(workspace: PolishWorkspace, run_id: str, content_id: str, key: str) -> object:
    return assessment_service.record_play(
        workspace.paths,
        run=run_id,
        content_id=content_id,
        idempotency_key=key,
        clock=workspace.clock,
        actor="client",
    )


@pytest.fixture
def recorded(polish_workspace: PolishWorkspace, tmp_path: Path) -> PolishWorkspace:
    publish_pilot_with_recordings(polish_workspace, tmp_path, replay_allowance=2)
    return polish_workspace


def _code(call: object) -> str:
    assert isinstance(call, LinguaWikiError)
    return call.payload.code


def test_a_pack_with_recordings_makes_listening_testable_under_machine_scoring(
    recorded: PolishWorkspace,
) -> None:
    run = _listening_run(recorded)

    assert run.untested_dimensions == ()
    task = _serve(recorded, run.run_id)
    assert task.modality == "audio"
    assert task.asset is not None


def test_plays_are_counted_and_refused_past_the_allowance(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)

    first = _play(recorded, run.run_id, task.content_id, "play-1")
    second = _play(recorded, run.run_id, task.content_id, "play-2")

    assert (first.plays_used, first.plays_remaining) == (1, 1)  # type: ignore[attr-defined]
    assert (second.plays_used, second.plays_remaining) == (2, 0)  # type: ignore[attr-defined]
    with pytest.raises(LinguaWikiError) as refused:
        _play(recorded, run.run_id, task.content_id, "play-3")
    assert refused.value.payload.code == "assessment_replays_exhausted"
    # The refusal wrote nothing, so the count is still two.
    with open_reader(recorded.paths) as database:
        assert database.scalar("SELECT count(*) FROM assessment_task_plays") == 2


def test_a_retried_play_is_one_play(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)

    _play(recorded, run.run_id, task.content_id, "play-1")
    again = _play(recorded, run.run_id, task.content_id, "play-1")

    assert again.plays_used == 1  # type: ignore[attr-defined]
    with open_reader(recorded.paths) as database:
        assert database.scalar("SELECT count(*) FROM assessment_task_plays") == 1


def test_a_play_key_reused_for_another_operation_conflicts(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = assessment_service.next_task(
        recorded.paths, run=run.run_id, clock=recorded.clock, idempotency_key="shared"
    )
    assert isinstance(task, assessment_service.NextTaskReport)

    with pytest.raises(LinguaWikiError) as refused:
        _play(recorded, run.run_id, task.content_id, "shared")
    assert refused.value.payload.code == "idempotency_conflict"


def test_a_task_with_nothing_to_play_cannot_be_played(polish_workspace: PolishWorkspace) -> None:
    run = assessment_service.start(
        polish_workspace.paths,
        dimensions=["reading"],
        scoring="machine",
        clock=polish_workspace.clock,
    )
    task = _serve(polish_workspace, run.run_id)

    with pytest.raises(LinguaWikiError) as refused:
        _play(polish_workspace, run.run_id, task.content_id, "play-1")
    assert refused.value.payload.code == "assessment_task_plays_nothing"


def test_an_answered_task_cannot_be_played_again(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    assessment_service.record(
        recorded.paths,
        run=run.run_id,
        content_id=task.content_id,
        score=1.0,
        assessor_kind="human",
        clock=recorded.clock,
    )

    with pytest.raises(LinguaWikiError) as refused:
        _play(recorded, run.run_id, task.content_id, "play-1")
    assert refused.value.payload.code == "assessment_task_settled"


def test_a_paused_run_takes_no_plays(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    assessment_service.set_status(
        recorded.paths, run=run.run_id, status="paused", clock=recorded.clock
    )

    with pytest.raises(LinguaWikiError) as refused:
        _play(recorded, run.run_id, task.content_id, "play-1")
    assert refused.value.payload.code == "assessment_run_paused"


def test_record_derives_the_count_from_the_rows(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    _play(recorded, run.run_id, task.content_id, "play-1")
    _play(recorded, run.run_id, task.content_id, "play-2")

    assessment_service.record(
        recorded.paths,
        run=run.run_id,
        content_id=task.content_id,
        score=1.0,
        assessor_kind="human",
        clock=recorded.clock,
        actor="client",
    )

    with open_reader(recorded.paths) as database:
        count = database.scalar(
            "SELECT play_count FROM assessment_results WHERE run_id = ? AND content_id = ?",
            [run.run_id, task.content_id],
        )
    assert count == 2


def test_a_surface_that_tracks_no_plays_records_no_count(recorded: PolishWorkspace) -> None:
    """Zero is a claim -- the learner answered without listening -- and the CLI cannot make it."""

    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)

    assessment_service.record(
        recorded.paths,
        run=run.run_id,
        content_id=task.content_id,
        score=1.0,
        assessor_kind="human",
        clock=recorded.clock,
    )

    with open_reader(recorded.paths) as database:
        count = database.scalar(
            "SELECT play_count FROM assessment_results WHERE run_id = ?", [run.run_id]
        )
    assert count is None


def test_the_screen_reports_the_same_count_after_a_reload(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    _play(recorded, run.run_id, task.content_id, "play-1")

    screen = view_service.run_screen(recorded.paths, run=run.run_id)

    (outstanding,) = screen.outstanding
    assert outstanding.plays_audio is True
    assert (outstanding.plays_used, outstanding.plays_remaining) == (1, 1)


def test_an_unlimited_allowance_reports_no_remaining_count(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    publish_pilot_with_recordings(polish_workspace, tmp_path, replay_allowance=None)
    run = _listening_run(polish_workspace)
    task = _serve(polish_workspace, run.run_id)

    report = _play(polish_workspace, run.run_id, task.content_id, "play-1")

    assert (report.plays_used, report.plays_remaining) == (1, None)  # type: ignore[attr-defined]


def _checks(workspace: PolishWorkspace) -> dict[str, object]:
    from linguawiki.db.integrity import check_database

    with open_reader(workspace.paths) as database:
        report = check_database(database)
    return {check.name: check for check in report.checks}


def test_a_healthy_workspace_passes_the_play_checks(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    _play(recorded, run.run_id, task.content_id, "play-1")
    assessment_service.record(
        recorded.paths,
        run=run.run_id,
        content_id=task.content_id,
        score=1.0,
        assessor_kind="human",
        clock=recorded.clock,
        actor="client",
    )

    checks = _checks(recorded)
    for name in ("task_plays_name_served_audio", "result_play_counts_agree"):
        assert name in checks, f"{name} did not run"
        assert checks[name].status == "ok", checks[name].message  # type: ignore[attr-defined]


def test_db_check_finds_a_count_that_disagrees_with_its_rows(recorded: PolishWorkspace) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    _play(recorded, run.run_id, task.content_id, "play-1")
    assessment_service.record(
        recorded.paths,
        run=run.run_id,
        content_id=task.content_id,
        score=1.0,
        assessor_kind="human",
        clock=recorded.clock,
        actor="client",
    )
    with open_writer(recorded.paths, command="test.damage") as database:
        database.execute("UPDATE assessment_results SET play_count = 5")

    check = _checks(recorded)["result_play_counts_agree"]
    assert check.status == "failed"  # type: ignore[attr-defined]
    assert task.content_id in "".join(check.context.values())  # type: ignore[attr-defined]


def test_db_check_finds_a_null_count_beside_play_rows(recorded: PolishWorkspace) -> None:
    """A NULL on one side of a comparison is a mismatch, not a row to skip."""

    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    _play(recorded, run.run_id, task.content_id, "play-1")
    assessment_service.record(
        recorded.paths,
        run=run.run_id,
        content_id=task.content_id,
        score=1.0,
        assessor_kind="human",
        clock=recorded.clock,
    )

    assert _checks(recorded)["result_play_counts_agree"].status == "failed"  # type: ignore[attr-defined]


def test_db_check_finds_a_play_on_a_task_the_run_never_served(
    recorded: PolishWorkspace,
) -> None:
    run = _listening_run(recorded)
    task = _serve(recorded, run.run_id)
    with open_writer(recorded.paths, command="test.damage") as database:
        other = database.scalar(
            "SELECT content_id FROM assessment_tasks WHERE content_id <> ? LIMIT 1",
            [task.content_id],
        )
        database.execute(
            "INSERT INTO assessment_task_plays VALUES "
            "('asm_01ARZ3NDEKTSV4RRFFQ69G5FAV', ?, ?, 'stray', now()::TIMESTAMP)",
            [run.run_id, other],
        )

    check = _checks(recorded)["task_plays_name_served_audio"]
    assert check.status == "failed"  # type: ignore[attr-defined]
