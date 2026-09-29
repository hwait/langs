"""Scoring an objective or short-response task from the record of what was served.

The whole point of this file is that no model, client, or server is involved, and that
nothing a pack says *after* the sitting can change what a learner is credited with. Two
facts carry it: the answer key is snapshotted when the task is served, and the learner's
response is an input to scoring rather than something the database keeps.
"""

from __future__ import annotations

import json

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.placement import SCORING_POLICY_VERSION
from linguawiki.services import assessment as assessment_service
from tests.conftest import PolishWorkspace

#: The dimensions whose banks are machine-scorable, so a served task is objective or
#: short-response rather than rubric-scored.
SCORABLE_DIMENSIONS = ("reading", "listening", "vocabulary-control", "grammar-control")


def _serve_scorable(workspace: PolishWorkspace, run_id: str) -> assessment_service.NextTaskReport:
    """Serve tasks until one of the machine-scorable types comes up."""

    while True:
        outcome = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
        assert isinstance(outcome, assessment_service.NextTaskReport), "bank ran out"
        if outcome.task_type in ("objective", "short-response"):
            return outcome
        # Rubric-scored: park it so the staircase keeps moving.
        assessment_service.record(
            workspace.paths,
            run=run_id,
            content_id=outcome.content_id,
            score=0.5,
            assessor_kind="ai",
            clock=workspace.clock,
        )


def _rewrite(workspace: PolishWorkspace, statement: str, parameters: list[object]) -> None:
    """Hand-edit the database the way a damaged restore or a pack edit would."""

    with (
        open_writer(workspace.paths, command="test.rewrite") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(statement, parameters)


def _served_row(workspace: PolishWorkspace, run_id: str, content_id: str) -> tuple[object, ...]:
    with open_reader(workspace.paths) as database:
        row = database.one(
            "SELECT expected_json, prompt_snapshot, rubric_json FROM assessment_run_tasks "
            "WHERE run_id = ? AND content_id = ?",
            [run_id, content_id],
        )
    assert row is not None
    return tuple(row)


def _result_row(workspace: PolishWorkspace, run_id: str, content_id: str) -> tuple[object, ...]:
    with open_reader(workspace.paths) as database:
        row = database.one(
            "SELECT raw_score, score_source, scoring_policy_version, response_visibility, "
            "response_excerpt, response_hash FROM assessment_results "
            "WHERE run_id = ? AND content_id = ?",
            [run_id, content_id],
        )
    assert row is not None
    return tuple(row)


def _answers(workspace: PolishWorkspace, run_id: str, content_id: str) -> tuple[str, ...]:
    expected, _prompt, _rubric = _served_row(workspace, run_id, content_id)
    return tuple(json.loads(str(expected))["answers"])


def test_serving_a_task_snapshots_the_key_the_prompt_and_the_rubric(
    polish_workspace: PolishWorkspace,
) -> None:
    """A pack is mutable and a run is not, so the material a scorer needs is copied."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)

    expected, prompt, rubric = _served_row(polish_workspace, run.run_id, served.content_id)

    assert prompt == served.prompt
    assert json.loads(str(rubric)) == served.rubric
    assert json.loads(str(expected))["answers"], "the key must be the one the pack held"


def test_a_correct_and_an_incorrect_answer_are_scored_without_a_model(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    first = _serve_scorable(polish_workspace, run.run_id)
    correct = _answers(polish_workspace, run.run_id, first.content_id)[0]

    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=first.content_id,
        response=correct,
        clock=polish_workspace.clock,
    )
    second = _serve_scorable(polish_workspace, run.run_id)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=second.content_id,
        response="zdecydowanie nie ta odpowiedź",
        clock=polish_workspace.clock,
    )

    assert _result_row(polish_workspace, run.run_id, first.content_id)[0] == 1.0
    assert _result_row(polish_workspace, run.run_id, second.content_id)[0] == 0.0


def test_a_computed_score_records_the_policy_and_a_supplied_one_does_not(
    polish_workspace: PolishWorkspace,
) -> None:
    """`assessor_kind` has only ever labelled a score; `score_source` says who reached it."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    computed = _serve_scorable(polish_workspace, run.run_id)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=computed.content_id,
        response=_answers(polish_workspace, run.run_id, computed.content_id)[0],
        clock=polish_workspace.clock,
    )
    supplied = _serve_scorable(polish_workspace, run.run_id)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=supplied.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )

    score, source, policy, *_ = _result_row(polish_workspace, run.run_id, computed.content_id)
    assert (score, source, policy) == (1.0, "computed", SCORING_POLICY_VERSION)
    score, source, policy, *_ = _result_row(polish_workspace, run.run_id, supplied.content_id)
    assert (score, source, policy) == (1.0, "supplied", None)


def test_a_supplied_score_wins_but_the_response_still_goes_through_retention(
    polish_workspace: PolishWorkspace,
) -> None:
    """Scoring in the background and discarding it would make the stored score unexplainable."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)

    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=0.0,
        response=_answers(polish_workspace, run.run_id, served.content_id)[0],
        clock=polish_workspace.clock,
    )

    score, source, policy, visibility, excerpt, digest = _result_row(
        polish_workspace, run.run_id, served.content_id
    )
    assert (score, source, policy) == (0.0, "supplied", None)
    assert visibility == "excerpt"
    assert excerpt and digest


def test_scoring_reads_the_snapshot_rather_than_the_pack_the_learner_never_saw(
    polish_workspace: PolishWorkspace,
) -> None:
    """The drift case the snapshot exists for: the pack changes between serve and score."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    as_served = _answers(polish_workspace, run.run_id, served.content_id)[0]

    _rewrite(
        polish_workspace,
        "UPDATE assessment_tasks SET expected_json = ? WHERE content_id = ?",
        [json.dumps({"answers": ["a completely other answer"]}), served.content_id],
    )

    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        response=as_served,
        clock=polish_workspace.clock,
    )

    assert _result_row(polish_workspace, run.run_id, served.content_id)[0] == 1.0


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ("", "invalid_arguments"),
        ("   ", "invalid_arguments"),
        (None, "assessment_score_required"),
    ],
)
def test_a_non_answer_leaves_the_task_still_served(
    polish_workspace: PolishWorkspace, response: str | None, code: str
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            response=response,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == code
    with open_reader(polish_workspace.paths) as database:
        status = database.scalar(
            "SELECT status FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
            [run.run_id, served.content_id],
        )
        results = database.scalar(
            "SELECT count(*) FROM assessment_results WHERE run_id = ? AND content_id = ?",
            [run.run_id, served.content_id],
        )
    assert str(status) == "served"
    assert int(results) == 0


def _blank_snapshot(workspace: PolishWorkspace, run_id: str, content_id: str, *keep: str) -> None:
    """Make a served row look pre-0030, optionally keeping some of the three columns."""

    columns = {"expected_json", "prompt_snapshot", "rubric_json"} - set(keep)
    assignments = ", ".join(f"{column} = NULL" for column in sorted(columns))
    _rewrite(
        workspace,
        f"UPDATE assessment_run_tasks SET {assignments} WHERE run_id = ? AND content_id = ?",
        [run_id, content_id],
    )


def test_a_row_served_before_the_snapshot_scores_from_the_bank_when_nothing_has_changed(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    correct = _answers(polish_workspace, run.run_id, served.content_id)[0]
    _blank_snapshot(polish_workspace, run.run_id, served.content_id)

    report = assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        response=correct,
        clock=polish_workspace.clock,
    )

    assert _result_row(polish_workspace, run.run_id, served.content_id)[0] == 1.0
    assert any("read from the bank" in warning for warning in report.warnings), report.warnings


def test_a_row_served_before_the_snapshot_is_refused_once_the_pack_has_drifted(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    correct = _answers(polish_workspace, run.run_id, served.content_id)[0]
    _blank_snapshot(polish_workspace, run.run_id, served.content_id)
    _rewrite(
        polish_workspace,
        "UPDATE content_records SET content_hash = ? WHERE content_id = ?",
        ["f" * 64, served.content_id],
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            response=correct,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_score_required"


@pytest.mark.parametrize("kept", ["expected_json", "prompt_snapshot", "rubric_json"])
def test_a_partial_snapshot_is_damage_rather_than_a_legacy_row(
    polish_workspace: PolishWorkspace, kept: str
) -> None:
    """Falling back would let the mutable pack answer for a fact the record actually held."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    correct = _answers(polish_workspace, run.run_id, served.content_id)[0]
    _blank_snapshot(polish_workspace, run.run_id, served.content_id, kept)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            response=correct,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_snapshot_partial"


@pytest.mark.parametrize(
    "damaged",
    [
        '{"answers": []}',
        '{"answers": "yes"}',
        '{"unrecognized": true}',
        '{"answers": [1]}',
        '{"answers": ["  "]}',
        "{not json",
    ],
)
def test_a_damaged_snapshot_key_is_refused_by_name(
    polish_workspace: PolishWorkspace, damaged: str
) -> None:
    """Neither `expected_json` column could carry a `json_valid` constraint to catch it."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    _rewrite(
        polish_workspace,
        "UPDATE assessment_run_tasks SET expected_json = ? WHERE run_id = ? AND content_id = ?",
        [damaged, run.run_id, served.content_id],
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            response="cokolwiek",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_answer_key_malformed"


def test_a_rubric_scored_task_cannot_be_computed_and_says_so(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    while True:
        served = assessment_service.next_task(
            polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
        )
        assert isinstance(served, assessment_service.NextTaskReport)
        if served.task_type not in ("objective", "short-response"):
            break
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            response="a written answer nobody can grade by comparison",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_not_machine_scorable"
    assert served.task_type in failure.value.payload.message


def _decline_transcripts(workspace: PolishWorkspace) -> None:
    from linguawiki.services import learners as learner_service

    learner_service.update_track(
        workspace.paths,
        track=workspace.track_id,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=workspace.clock,
    )


def test_a_declined_transcript_does_not_block_scoring_and_keeps_only_the_hash(
    polish_workspace: PolishWorkspace,
) -> None:
    """The score rests on the comparison, not on keeping the learner's words."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    correct = _answers(polish_workspace, run.run_id, served.content_id)[0]
    _decline_transcripts(polish_workspace)

    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        response=correct,
        clock=polish_workspace.clock,
    )

    score, source, _policy, visibility, excerpt, digest = _result_row(
        polish_workspace, run.run_id, served.content_id
    )
    assert (score, source, visibility, excerpt) == (1.0, "computed", "withheld", None)
    assert digest, "the hash is what lets a response offered later be checked against this one"


def test_asking_to_keep_more_than_consent_allows_is_refused_before_the_first_write(
    polish_workspace: PolishWorkspace,
) -> None:
    """A refusal at the door leaves the task answerable; one after the write would not."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    correct = _answers(polish_workspace, run.run_id, served.content_id)[0]
    _decline_transcripts(polish_workspace)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            response=correct,
            response_visibility="full",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "transcript_consent_required"
    with open_reader(polish_workspace.paths) as database:
        status = database.scalar(
            "SELECT status FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
            [run.run_id, served.content_id],
        )
        results = database.scalar(
            "SELECT count(*) FROM assessment_results WHERE run_id = ? AND content_id = ?",
            [run.run_id, served.content_id],
        )
    assert str(status) == "served"
    assert int(results) == 0


def test_a_caller_supplied_excerpt_goes_through_the_same_consent_rule(
    polish_workspace: PolishWorkspace,
) -> None:
    """The route this stage did not add reaches the column through the same door."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    _decline_transcripts(polish_workspace)

    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=1.0,
        response_excerpt="the learner's own words, chosen by the caller",
        clock=polish_workspace.clock,
    )

    _score, _source, _policy, visibility, excerpt, digest = _result_row(
        polish_workspace, run.run_id, served.content_id
    )
    assert (visibility, excerpt) == ("withheld", None)
    assert digest


def test_a_response_and_an_excerpt_are_two_accounts_of_one_answer(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            score=1.0,
            response="the whole answer",
            response_excerpt="part of it",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "invalid_arguments"


def test_withholding_is_honoured_even_where_consent_was_given(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = _serve_scorable(polish_workspace, run.run_id)
    correct = _answers(polish_workspace, run.run_id, served.content_id)[0]

    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        response=correct,
        response_visibility="withheld",
        clock=polish_workspace.clock,
    )

    score, _source, _policy, visibility, excerpt, digest = _result_row(
        polish_workspace, run.run_id, served.content_id
    )
    assert (score, visibility, excerpt) == (1.0, "withheld", None)
    assert digest
