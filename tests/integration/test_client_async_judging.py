"""C6: a judge's verdict arrives later than the answer, and lands exactly once.

The recordings are generated tones (`tests/support/recordings.py`), never anything anybody
said. What is under test is the account the workspace keeps of a verdict: bound to the
submission it judged, revalidated when it lands, held while the run is paused, applied in
the same commit that received it, and never credited twice however often it is delivered.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import assessment as assessment_service
from linguawiki.services import judging, withdrawal
from linguawiki.services import learners as learner_service
from linguawiki.services import recordings as recording_service
from tests.conftest import PolishWorkspace
from tests.integration.test_client_audio import (
    _cli,
    database_check,
    failed_checks,
    rows,
    serve,
    speaking,
    spoken_bytes,
    start_spoken,
    take,
)

__all__ = ["speaking"]


class Crash(BaseException):
    """A process dying mid-step. A `BaseException`, so nothing treats it as a refusal."""


def refusal(call: Any, *args: Any, **kwargs: Any) -> LinguaWikiError:
    with pytest.raises(LinguaWikiError) as failure:
        call(*args, **kwargs)
    return failure.value


def submitted(workspace: PolishWorkspace, seed: int) -> tuple[str, str, str, str]:
    """A pronunciation run with one recording waiting for a judge:
    `(run_id, content_id, submission_id, artifact_id)`."""

    run_id = start_spoken(workspace, ("pronunciation",))
    task = serve(workspace, run_id)
    taken = take(workspace, run_id, task.content_id, data=spoken_bytes(seed))
    assert taken.submission is not None and taken.artifact_id is not None
    return run_id, task.content_id, taken.submission.submission_id, taken.artifact_id


def verdict(
    workspace: PolishWorkspace,
    run_id: str,
    content_id: str,
    submission_id: str | None,
    *,
    score: float = 0.75,
    key: str | None = None,
    rubric: dict[str, object] | None = None,
    claim: str | None = None,
    audio_artifact: str | None = None,
) -> assessment_service.AssessmentRunReport:
    return assessment_service.record(
        workspace.paths,
        run=run_id,
        content_id=content_id,
        score=score,
        submission=submission_id,
        audio_artifact=audio_artifact,
        claim=claim,
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        confidence="medium",
        rubric=rubric if rubric is not None else {"accuracy": score},
        idempotency_key=key,
        clock=workspace.clock,
    )


def pause(workspace: PolishWorkspace, run_id: str) -> None:
    assessment_service.set_status(
        workspace.paths, status="paused", run=run_id, clock=workspace.clock
    )


def results(workspace: PolishWorkspace, run_id: str) -> int:
    return int(
        rows(workspace, "SELECT count(*) FROM assessment_results WHERE run_id = ?", [run_id])[0][0]
    )


def verdict_rows(workspace: PolishWorkspace, submission_id: str) -> list[Any]:
    return rows(
        workspace,
        "SELECT verdict.verdict_id, verdict.raw_score, outcome.outcome "
        "FROM assessment_verdicts verdict "
        "LEFT JOIN assessment_verdict_outcomes outcome USING (verdict_id) "
        "WHERE verdict.submission_id = ? ORDER BY verdict.received_at",
        [submission_id],
    )


def assert_clean(workspace: PolishWorkspace) -> None:
    assert not failed_checks(workspace)


# --- verdict ---------------------------------------------------------------------------------


def test_a_keyed_verdict_delivered_twice_is_applied_once(speaking: PolishWorkspace) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 101)

    first = verdict(speaking, run_id, content_id, submission_id, key="claim-1")
    again = verdict(speaking, run_id, content_id, submission_id, key="claim-1")

    assert first.verdict_status == "applied" and not first.held
    assert again.verdict_id == first.verdict_id and again.verdict_status == "applied"
    assert again.tasks_recorded == first.tasks_recorded == 1
    assert results(speaking, run_id) == 1
    assert verdict_rows(speaking, submission_id) == [(first.verdict_id, 0.75, "applied")]
    payload = rows(
        speaking,
        "SELECT payload_json FROM domain_events WHERE idempotency_key = 'claim-1'",
    )
    assert first.verdict_id is not None and first.verdict_id in str(payload[0][0])
    assert_clean(speaking)


def test_a_verdict_on_a_paused_run_is_held_and_replays_as_held(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 102)
    pause(speaking, run_id)

    held = verdict(speaking, run_id, content_id, submission_id, key="claim-2")
    replay = verdict(speaking, run_id, content_id, submission_id, key="claim-2")

    assert held.held and held.verdict_status == "held" and held.status == "paused"
    assert replay.held and replay.verdict_id == held.verdict_id
    # Nothing about the learner changed: no result, the submission still waits, the task
    # still holds its dimension.
    assert results(speaking, run_id) == 0
    assert rows(speaking, "SELECT status FROM assessment_submissions") == [("pending",)]
    assert verdict_rows(speaking, submission_id) == [(held.verdict_id, 0.75, None)]
    assert_clean(speaking)


def test_a_paused_run_still_refuses_a_result_bound_to_no_submission(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    pause(speaking, run_id)

    failure = refusal(verdict, speaking, run_id, task.content_id, None)

    assert failure.payload.code == "assessment_run_paused"
    assert rows(speaking, "SELECT count(*) FROM assessment_verdicts") == [(0,)]


def test_a_keyless_repeat_is_accepted_and_a_different_verdict_is_a_result_conflict(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 103)
    verdict(speaking, run_id, content_id, submission_id)

    verdict(speaking, run_id, content_id, submission_id)
    failure = refusal(verdict, speaking, run_id, content_id, submission_id, score=0.25)

    assert failure.payload.code == "assessment_result_conflict"
    assert failure.payload.details[0].context == {"recorded": "0.75", "offered": "0.25"}
    assert results(speaking, run_id) == 1 and len(verdict_rows(speaking, submission_id)) == 1


def test_a_keyless_repeat_against_a_held_verdict_compares_with_it(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 104)
    pause(speaking, run_id)
    first = verdict(speaking, run_id, content_id, submission_id)

    repeat = verdict(speaking, run_id, content_id, submission_id)
    failure = refusal(verdict, speaking, run_id, content_id, submission_id, score=0.25)

    assert repeat.held and repeat.verdict_id == first.verdict_id
    assert failure.payload.code == "assessment_result_conflict"
    assert len(verdict_rows(speaking, submission_id)) == 1


def test_a_reused_key_with_a_different_verdict_is_an_idempotency_conflict(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 105)
    verdict(speaking, run_id, content_id, submission_id, key="claim-5")

    failure = refusal(
        verdict, speaking, run_id, content_id, submission_id, score=0.25, key="claim-5"
    )

    assert failure.payload.code == "idempotency_conflict"


@pytest.mark.parametrize("paused", [False, True], ids=["applied", "held"])
def test_a_second_keyed_verdict_for_one_submission_is_a_verdict_conflict(
    speaking: PolishWorkspace, paused: bool
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 106 if paused else 107)
    if paused:
        pause(speaking, run_id)
    first = verdict(speaking, run_id, content_id, submission_id, key="claim-a")

    failure = refusal(
        verdict, speaking, run_id, content_id, submission_id, score=0.25, key="claim-b"
    )
    identical = verdict(speaking, run_id, content_id, submission_id, key="claim-c")

    assert failure.payload.code == "assessment_verdict_conflict"
    assert "0.75" in failure.payload.message
    assert failure.payload.details[0].context == {"recorded": "0.75", "offered": "0.25"}
    # Of two claims' verdicts the first to commit wins; an identical second is a repeat.
    assert identical.verdict_id == first.verdict_id and identical.held is paused
    assert len(verdict_rows(speaking, submission_id)) == 1


def test_a_verdict_on_a_superseded_submission_names_its_successor(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, first, _ = submitted(speaking, 108)
    again = take(speaking, run_id, content_id, data=spoken_bytes(109))
    assert again.submission is not None
    successor = again.submission.submission_id

    failure = refusal(verdict, speaking, run_id, content_id, first)

    assert failure.payload.code == "assessment_submission_superseded"
    assert successor in failure.payload.message
    assert failure.payload.details[0].context == {"successor": successor}
    # The successor is what the judge is told to judge, and that works.
    assert verdict(speaking, run_id, content_id, successor).verdict_status == "applied"


def test_a_verdict_on_a_purged_submission_is_refused_with_its_withdrawal_code(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, artifact_id = submitted(speaking, 110)
    artifact_service.purge(speaking.paths, artifact=artifact_id)

    failure = refusal(verdict, speaking, run_id, content_id, submission_id)

    assert failure.payload.code == "assessment_submission_withdrawn"
    assert failure.payload.details[0].context["code"] == withdrawal.PURGED_CODE
    assert results(speaking, run_id) == 0


def test_an_unjudgeable_recording_named_by_submission_is_settled_then_refused(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, artifact_id = submitted(speaking, 111)
    path = speaking.root / str(
        rows(speaking, "SELECT relative_path FROM artifacts WHERE artifact_id = ?", [artifact_id])[
            0
        ][0]
    )
    path.write_bytes(path.read_bytes() + b"altered")

    failure = refusal(verdict, speaking, run_id, content_id, submission_id)

    assert failure.payload.code == "assessment_audio_altered"
    assert rows(speaking, "SELECT status, withdrawn_code FROM assessment_submissions") == [
        ("withdrawn", "assessment_audio_altered")
    ]
    assert rows(speaking, "SELECT count(*) FROM assessment_verdicts") == [(0,)]


def test_a_purge_on_a_paused_run_voids_the_verdict_held_for_it(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, artifact_id = submitted(speaking, 112)
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, submission_id)

    artifact_service.purge(speaking.paths, artifact=artifact_id)

    stored = rows(
        speaking,
        "SELECT outcome, code FROM assessment_verdict_outcomes WHERE verdict_id = ?",
        [held.verdict_id],
    )
    assert stored == [("void", withdrawal.PURGED_CODE)]
    assert_clean(speaking)


def test_a_crash_inside_the_transaction_writes_nothing_and_the_retry_applies(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 113)

    def die(*_args: Any, **_kwargs: Any) -> None:
        raise Crash("exposure")

    with monkeypatch.context() as patch:
        patch.setattr(assessment_service, "_record_exposure", die)
        with pytest.raises(Crash):
            verdict(speaking, run_id, content_id, submission_id, key="claim-13")

    assert results(speaking, run_id) == 0
    assert rows(speaking, "SELECT count(*) FROM assessment_verdicts") == [(0,)]
    assert rows(speaking, "SELECT status FROM assessment_submissions") == [("pending",)]
    assert rows(
        speaking, "SELECT count(*) FROM domain_events WHERE idempotency_key = 'claim-13'"
    ) == [(0,)]

    retried = verdict(speaking, run_id, content_id, submission_id, key="claim-13")

    assert retried.verdict_status == "applied" and results(speaking, run_id) == 1
    assert_clean(speaking)


def test_a_response_lost_after_the_commit_is_replayed_by_the_retry(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 114)
    real = assessment_service._verdict_report
    calls = {"n": 0}

    def lose_first(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Crash("response lost")
        return real(*args, **kwargs)

    monkeypatch.setattr(assessment_service, "_verdict_report", lose_first)
    with pytest.raises(Crash):
        verdict(speaking, run_id, content_id, submission_id, key="claim-14")

    retried = verdict(speaking, run_id, content_id, submission_id, key="claim-14")

    assert retried.verdict_status == "applied"
    assert results(speaking, run_id) == 1 and len(verdict_rows(speaking, submission_id)) == 1


def test_a_late_verdict_is_dated_by_the_answer_and_recorded_when_it_landed(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 115)
    speaking.clock.advance(timedelta(days=7))

    verdict(speaking, run_id, content_id, submission_id)

    observed, recorded, created, received = rows(
        speaking,
        "SELECT result.observed_at, result.recorded_at, submission.created_at, "
        "verdict.received_at FROM assessment_results result "
        "JOIN assessment_verdict_outcomes outcome ON outcome.result_id = result.result_id "
        "JOIN assessment_verdicts verdict USING (verdict_id) "
        "JOIN assessment_submissions submission USING (submission_id) "
        "WHERE result.run_id = ?",
        [run_id],
    )[0]
    assert observed == created
    assert recorded - observed >= timedelta(days=7)
    assert received == recorded
    assert_clean(speaking)


RUBRIC_WITH_WORDS = {
    "version": 1,
    "dimensions": [
        {"name": "accuracy", "score": 0.5, "note": "said 'dzien dobry' as 'dzien dobly'"},
        {"name": "fluency", "score": 1.0, "note": "no pauses"},
    ],
    "rationale": "the learner said: dzien dobry, jak sie masz",
    "total": 0.75,
}


@pytest.mark.parametrize("paused", [False, True], ids=["applied", "held"])
def test_a_track_forbidding_retention_keeps_no_free_text_from_the_verdict(
    speaking: PolishWorkspace, paused: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording track cannot decline transcripts today -- audio retention requires
    transcript retention -- so the consent the verdict is planned under is substituted,
    exactly where `plan_verdict` reads it. The rule under test is the rubric's: a judge's
    note quoting the learner is the learner's words, and a track that declined keeping
    them keeps none of it, on the verdict or on the result it produces."""

    run_id, content_id, submission_id, _ = submitted(speaking, 116 if paused else 121)
    if paused:
        pause(speaking, run_id)
    original = learner_service.track_context

    def declining(database: Any, track_id: str) -> Any:
        record = original(database, track_id)
        preferences = {**record.preferences, "transcript_retention_consent": False}
        return record.model_copy(update={"preferences": preferences})

    with monkeypatch.context() as patch:
        patch.setattr(learner_service, "track_context", declining)
        landed = verdict(speaking, run_id, content_id, submission_id, rubric=RUBRIC_WITH_WORDS)

    assert landed.held is paused
    expected = (
        '{"dimensions": [{"name": "accuracy", "note": null, "score": 0.5}, '
        '{"name": "fluency", "note": null, "score": 1.0}], "rationale": null, '
        '"total": 0.75, "version": 1}'
    )
    stored = rows(
        speaking,
        "SELECT rubric_json, response_excerpt FROM assessment_verdicts WHERE verdict_id = ?",
        [landed.verdict_id],
    )
    assert stored == [(expected, None)]
    everything = rows(
        speaking,
        "SELECT rubric_json, response_excerpt FROM assessment_verdicts UNION ALL "
        "SELECT rubric_json, response_excerpt FROM assessment_results",
    )
    assert len(everything) == (1 if paused else 2)
    for rubric_json, excerpt in everything:
        assert excerpt is None
        assert "dzien" not in str(rubric_json) and "pauses" not in str(rubric_json)


def test_a_consenting_default_keeps_a_bounded_excerpt_of_each_note() -> None:
    declined = assessment_service.retain_rubric(
        {"name": "accuracy", "note": "quoted words"},
        preferences={"transcript_retention_consent": False},
    )
    assert declined == {"name": "accuracy", "note": None}

    kept = assessment_service.retain_rubric(
        {"dimensions": [{"name": "accuracy", "note": "x" * 1000}], "name": "y" * 300},
        preferences={},
    )

    assert kept["dimensions"] == [{"name": "accuracy", "note": "x" * 240}]
    # A "name" too long to be a criterion name is prose, whatever its key says.
    assert kept["name"] == "y" * 240
    # Applying the rule twice changes nothing, so a held verdict can be planned again.
    assert assessment_service.retain_rubric(kept, preferences={}) == kept


def test_a_claim_must_be_for_the_submission_the_verdict_judges(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 117)
    handed = judging.claim(
        speaking.paths, run=run_id, judge="synthetic-judge", clock=speaking.clock
    )
    claim_id = handed.claimed[0].claim_id
    # A claim for a submission this one is not. Written by hand: no command can hand out a
    # claim for a submission that does not exist, which is what makes it a mismatch here.
    with open_writer(speaking.paths, command="test.claim", clock=speaking.clock) as database:
        now = database.now()
        database.execute(
            "INSERT INTO judging_claims (claim_id, submission_id, judge, claimed_at, "
            "lease_expires_at) VALUES "
            "('asm_01J0000000000000000000CLM2', 'asm_01J000000000000000000OTHER', 'judge', ?, ?)",
            [now, now + timedelta(minutes=10)],
        )

    unknown = refusal(
        verdict, speaking, run_id, content_id, submission_id, claim="asm_01J00000000000000NOCLAIM"
    )
    mismatch = refusal(
        verdict, speaking, run_id, content_id, submission_id, claim="asm_01J0000000000000000000CLM2"
    )
    landed = verdict(speaking, run_id, content_id, submission_id, claim=claim_id)

    assert unknown.payload.code == "assessment_claim_not_found"
    assert mismatch.payload.code == "assessment_claim_mismatch"
    assert rows(
        speaking,
        "SELECT claim_id FROM assessment_verdicts WHERE verdict_id = ?",
        [landed.verdict_id],
    ) == [(claim_id,)]


def test_a_submission_from_another_run_or_task_is_refused_by_name(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 118)

    unknown = refusal(verdict, speaking, run_id, content_id, "asm_01J000000000000000NOSUBM")
    elsewhere = refusal(verdict, speaking, run_id, "cnt_not_this_one", submission_id)

    assert unknown.payload.code == "assessment_submission_not_found"
    assert elsewhere.payload.code == "assessment_submission_mismatch"


def test_a_held_verdict_applies_through_plan_and_write_in_a_callers_transaction(
    speaking: PolishWorkspace,
) -> None:
    """The interface the resume path is built on: one held verdict, revalidated by
    `plan_verdict` and applied by `write_verdict` inside a transaction the caller owns."""

    run_id, content_id, submission_id, _ = submitted(speaking, 119)
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, submission_id)
    assert held.verdict_id is not None

    with open_writer(speaking.paths, command="test.resume", clock=speaking.clock) as database:
        request = assessment_service.held_verdict_request(database, held.verdict_id)
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE assessment_runs SET status = 'in-progress' WHERE run_id = ?", [run_id]
            )
            plan = assessment_service.plan_verdict(transaction, request, root=speaking.root)
            assert isinstance(plan, assessment_service.VerdictPlan) and plan.action == "apply"
            written = assessment_service.write_verdict(transaction, plan)

    assert written.verdict_id == held.verdict_id and written.result_id is not None
    assert verdict_rows(speaking, submission_id) == [(held.verdict_id, 0.75, "applied")]
    assert rows(speaking, "SELECT status FROM assessment_submissions") == [("judged",)]
    assert verdict(speaking, run_id, content_id, submission_id).verdict_id == held.verdict_id
    assert database_check(speaking).ok


def test_the_cli_records_a_verdict_by_submission(
    speaking: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 120)

    code, payload = _cli(
        speaking,
        capsys,
        "assessment",
        "record",
        "--content",
        content_id,
        "--submission",
        submission_id,
        "--score",
        "0.5",
        "--assessor-kind",
        "ai",
        "--assessor",
        "synthetic-ai-judge",
        "--idempotency-key",
        "cli-claim",
    )

    assert code == 0, payload
    assert payload["data"]["run_id"] == run_id
    assert payload["data"]["verdict_status"] == "applied"


# --- processing ------------------------------------------------------------------------------


def claim(
    workspace: PolishWorkspace,
    run_id: str,
    *,
    judge: str = "synthetic-judge",
    lease: int | None = None,
    limit: int | None = None,
) -> judging.ClaimReport:
    return judging.claim(
        workspace.paths,
        run=run_id,
        judge=judge,
        lease_seconds=lease,
        limit=limit,
        clock=workspace.clock,
    )


def claimed_one(workspace: PolishWorkspace, run_id: str, **kwargs: Any) -> str:
    report = claim(workspace, run_id, **kwargs)
    assert len(report.claimed) == 1, report
    return report.claimed[0].claim_id


def release(
    workspace: PolishWorkspace,
    claim_id: str,
    *,
    reason: str = "the judge timed out",
    terminal: bool = False,
    code: str | None = None,
) -> judging.ReleaseReport:
    return judging.release(
        workspace.paths,
        claim=claim_id,
        reason=reason,
        terminal=terminal,
        code=code,
        clock=workspace.clock,
    )


def submission_state(workspace: PolishWorkspace, submission_id: str) -> tuple[Any, ...]:
    return tuple(
        rows(
            workspace,
            "SELECT status, withdrawn_code FROM assessment_submissions WHERE submission_id = ?",
            [submission_id],
        )[0]
    )


def lapse(workspace: PolishWorkspace) -> None:
    """Let any lease the tests hand out run out."""

    workspace.clock.advance(timedelta(seconds=judging.JUDGING_POLICY.default_lease_seconds + 1))


def test_a_claim_holds_no_connection_while_the_judge_works(speaking: PolishWorkspace) -> None:
    run_id, content_id, submission_id, artifact_id = submitted(speaking, 201)

    handed = claim(speaking, run_id)

    assert [entry.submission.submission_id for entry in handed.claimed] == [submission_id]
    entry = handed.claimed[0]
    # Everything `pending` says of the entry, plus the claim.
    assert entry.judgeable and entry.audio_path is not None and entry.sha256 is not None
    assert entry.attempts == 1 and entry.claimed_by == "synthetic-judge"
    assert entry.lease_expires_at is not None and handed.lease_seconds == 600
    listed = recording_service.pending(speaking.paths, run=run_id, clock=speaking.clock)
    assert listed.pending[0].claimed_by == "synthetic-judge"
    assert listed.pending[0].lease_expires_at == entry.lease_expires_at
    assert listed.pending[0].attempts == 1

    # While the judge is "judging", another writer gets the workspace: nothing is held. It
    # is a second judge, who finds the one submission under a live lease.
    rival = claim(speaking, run_id, judge="second-judge")
    assert rival.claimed == () and rival.waiting == 1

    landed = verdict(
        speaking,
        run_id,
        content_id,
        submission_id,
        claim=entry.claim_id,
        key=entry.claim_id,
        audio_artifact=artifact_id,
    )

    assert landed.verdict_status == "applied"
    after = recording_service.pending(speaking.paths, run=run_id, clock=speaking.clock)
    assert after.pending == ()
    assert_clean(speaking)


def test_an_expired_lease_is_claimable_again_and_its_late_verdict_still_lands(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 202)
    first = claimed_one(speaking, run_id, lease=60)
    speaking.clock.advance(timedelta(minutes=2))

    listed = recording_service.pending(speaking.paths, run=run_id, clock=speaking.clock)
    assert listed.pending[0].claimed_by is None and listed.pending[0].attempts == 1

    second = claim(speaking, run_id, judge="second-judge")
    assert second.claimed[0].attempts == 2

    # The lease scheduled; it did not decide. Nothing has happened to the submission, so
    # the first judge's late verdict is the first to commit, and it wins.
    late = verdict(speaking, run_id, content_id, submission_id, claim=first, key=first)
    assert late.verdict_status == "applied"
    # Of two claims' verdicts the first to commit wins; a different second is a conflict,
    # an identical one a repeat.
    conflict = refusal(
        verdict,
        speaking,
        run_id,
        content_id,
        submission_id,
        score=0.25,
        claim=second.claimed[0].claim_id,
        key=second.claimed[0].claim_id,
    )
    assert conflict.payload.code == "assessment_verdict_conflict"
    identical = verdict(
        speaking,
        run_id,
        content_id,
        submission_id,
        claim=second.claimed[0].claim_id,
        key="second-judge-identical",
    )
    assert identical.verdict_id == late.verdict_id
    assert len(verdict_rows(speaking, submission_id)) == 1
    assert_clean(speaking)


def test_a_release_returns_the_submission_to_the_queue_once(speaking: PolishWorkspace) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 203)
    claim_id = claimed_one(speaking, run_id)

    released = release(speaking, claim_id)
    again = release(speaking, claim_id)
    differently = refusal(release, speaking, claim_id, reason="something else")
    late = refusal(verdict, speaking, run_id, content_id, submission_id, claim=claim_id)

    assert released.returned_to_queue and released.submission_status == "pending"
    assert not released.replayed and again.replayed
    assert again.released_at == released.released_at
    assert differently.payload.code == "assessment_claim_released"
    assert differently.payload.details[0].context["reason"] == "the judge timed out"
    # A released claim's verdict is refused by what happened to the claim.
    assert late.payload.code == "assessment_claim_released"
    assert rows(speaking, "SELECT count(*) FROM judging_releases") == [(1,)]
    # Back in the queue: the next judge gets it as a second attempt.
    assert claim(speaking, run_id).claimed[0].attempts == 2
    assert_clean(speaking)


def test_a_claim_that_ended_with_a_verdict_cannot_be_released(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 204)
    claim_id = claimed_one(speaking, run_id)
    landed = verdict(speaking, run_id, content_id, submission_id, claim=claim_id, key=claim_id)

    failure = refusal(release, speaking, claim_id)

    assert failure.payload.code == "assessment_claim_judged"
    assert failure.payload.details[0].context == {
        "verdict": str(landed.verdict_id),
        "outcome": "applied",
    }
    assert rows(speaking, "SELECT count(*) FROM judging_releases") == [(0,)]


def test_a_terminal_release_withdraws_and_the_dimension_serves_again(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 205)
    claim_id = claimed_one(speaking, run_id)

    released = release(
        speaking,
        claim_id,
        terminal=True,
        code="assessment_audio_unintelligible",
        reason="the recording is silence",
    )

    assert released.terminal and released.submission_status == "withdrawn"
    assert not released.returned_to_queue
    assert [entry.submission_id for entry in released.withdrawn] == [submission_id]
    assert rows(
        speaking,
        "SELECT status, withdrawn_code, withdrawn_reason FROM assessment_submissions",
    ) == [("withdrawn", "assessment_audio_unintelligible", "the recording is silence")]
    assert rows(
        speaking,
        "SELECT status FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
        [run_id, content_id],
    ) == [("skipped",)]
    assert serve(speaking, run_id).content_id != content_id
    assert_clean(speaking)


def test_a_release_is_refused_when_its_arguments_disagree(speaking: PolishWorkspace) -> None:
    run_id, _, _, _ = submitted(speaking, 206)
    claim_id = claimed_one(speaking, run_id)

    no_code = refusal(release, speaking, claim_id, terminal=True)
    stray_code = refusal(release, speaking, claim_id, code="assessment_x")
    blank = refusal(release, speaking, claim_id, reason="   ")
    unknown = refusal(release, speaking, "asm_01J00000000000000NOCLAIM")

    assert [no_code.payload.code, stray_code.payload.code, blank.payload.code] == [
        "invalid_arguments"
    ] * 3
    assert unknown.payload.code == "assessment_claim_not_found"
    assert rows(speaking, "SELECT count(*) FROM judging_releases") == [(0,)]


def exhaust(workspace: PolishWorkspace, run_id: str) -> list[str]:
    """Claim the run's one submission as often as the policy allows, letting each lease
    run out -- built with the clock, never by hand edits."""

    claims = []
    for _ in range(judging.JUDGING_POLICY.max_attempts):
        claims.append(claimed_one(workspace, run_id))
        lapse(workspace)
    return claims


def test_exhausted_attempts_are_reported_then_settled_by_an_unrelated_writer(
    speaking: PolishWorkspace,
) -> None:
    from linguawiki.services import database as database_service

    run_id, content_id, submission_id, _ = submitted(speaking, 207)
    exhaust(speaking, run_id)

    # Nobody has touched the run since the last lease ran out: `db check` says so.
    report = database_service.check(speaking.paths, clock=speaking.clock)
    lapsed = {check.name: check for check in report.checks}["lapsed_judging_settled"]
    assert lapsed.status == "failed" and submission_id in str(lapsed.context["submissions"])
    assert submission_state(speaking, submission_id) == ("pending", None)

    # Serving is a writer touching the run: it settles the lapsed submission first, and
    # the dimension that submission held serves again.
    served = serve(speaking, run_id)

    assert served.content_id != content_id
    status, code = submission_state(speaking, submission_id)
    assert (status, code) == ("withdrawn", judging.EXHAUSTED_CODE)
    reason = rows(
        speaking,
        "SELECT withdrawn_reason FROM assessment_submissions WHERE submission_id = ?",
        [submission_id],
    )[0][0]
    assert judging.JUDGING_POLICY.version in reason
    assert database_service.check(speaking.paths, clock=speaking.clock).ok


@pytest.mark.parametrize(
    "writer",
    ["claim", "pause", "finalize", "record"],
)
def test_every_writer_touching_the_run_settles_what_lapsed(
    speaking: PolishWorkspace, writer: str
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 208)
    exhaust(speaking, run_id)

    if writer == "claim":
        report = claim(speaking, run_id)
        assert report.claimed == ()
        assert [entry.code for entry in report.withdrawn] == [judging.EXHAUSTED_CODE]
    elif writer == "pause":
        pause(speaking, run_id)
    elif writer == "finalize":
        assessment_service.finalize(speaking.paths, run=run_id, clock=speaking.clock)
    else:
        # A keyless verdict naming no claim spares nothing: the submission is settled first,
        # and the verdict refused by what became of it.
        failure = refusal(verdict, speaking, run_id, content_id, submission_id)
        assert failure.payload.code == "assessment_submission_withdrawn"

    assert submission_state(speaking, submission_id) == ("withdrawn", judging.EXHAUSTED_CODE)


def test_the_last_attempts_late_verdict_is_spared_by_its_own_delivery(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 209)
    claims = exhaust(speaking, run_id)

    landed = verdict(speaking, run_id, content_id, submission_id, claim=claims[-1], key=claims[-1])

    assert landed.verdict_status == "applied"
    assert submission_state(speaking, submission_id) == ("judged", None)
    assert database_check(speaking).ok


def test_releasing_the_last_attempt_withdraws_it_as_exhausted(speaking: PolishWorkspace) -> None:
    run_id, _, submission_id, _ = submitted(speaking, 210)
    for _ in range(judging.JUDGING_POLICY.max_attempts - 1):
        claimed_one(speaking, run_id)
        lapse(speaking)
    last = claimed_one(speaking, run_id)

    released = release(speaking, last)

    assert released.attempts == judging.JUDGING_POLICY.max_attempts
    assert not released.returned_to_queue and released.submission_status == "withdrawn"
    assert [entry.code for entry in released.withdrawn] == [judging.EXHAUSTED_CODE]
    assert submission_state(speaking, submission_id) == ("withdrawn", judging.EXHAUSTED_CODE)
    assert_clean(speaking)


def test_a_claim_withdraws_an_unjudgeable_recording_rather_than_hand_it_out(
    speaking: PolishWorkspace,
) -> None:
    run_id, _, submission_id, artifact_id = submitted(speaking, 211)
    path = speaking.root / str(
        rows(speaking, "SELECT relative_path FROM artifacts WHERE artifact_id = ?", [artifact_id])[
            0
        ][0]
    )
    path.write_bytes(path.read_bytes() + b"altered")
    listed = recording_service.pending(speaking.paths, run=run_id, clock=speaking.clock)
    # The reader only lists it, with its reason.
    assert not listed.pending[0].judgeable
    assert submission_state(speaking, submission_id) == ("pending", None)

    report = claim(speaking, run_id)

    assert report.claimed == ()
    assert [(entry.submission_id, entry.code) for entry in report.withdrawn] == [
        (submission_id, "assessment_audio_altered")
    ]
    assert submission_state(speaking, submission_id) == ("withdrawn", "assessment_audio_altered")


def test_a_claim_refuses_a_closed_run_and_a_blank_judge(speaking: PolishWorkspace) -> None:
    run_id, _, _, _ = submitted(speaking, 212)

    blank = refusal(claim, speaking, run_id, judge="  ")
    no_lease = refusal(claim, speaking, run_id, lease=0)
    assessment_service.set_status(
        speaking.paths, status="abandoned", run=run_id, clock=speaking.clock
    )
    closed = refusal(claim, speaking, run_id)

    assert blank.payload.code == no_lease.payload.code == "invalid_arguments"
    assert closed.payload.code == "assessment_run_closed"
    assert rows(speaking, "SELECT count(*) FROM judging_claims") == [(0,)]


def test_an_identical_verdict_under_a_new_key_binds_that_key(speaking: PolishWorkspace) -> None:
    """Review minor: the second key was planned as a repeat and returned before any event
    was written, so it never bound -- and its retry after the run closed met the closed run
    instead of replaying."""

    run_id, content_id, submission_id, _ = submitted(speaking, 213)
    first = verdict(speaking, run_id, content_id, submission_id, key="first-key")
    second = verdict(speaking, run_id, content_id, submission_id, key="second-key")
    assert second.verdict_id == first.verdict_id
    assert rows(
        speaking, "SELECT count(*) FROM domain_events WHERE idempotency_key = 'second-key'"
    ) == [(1,)]
    assessment_service.finalize(speaking.paths, run=run_id, clock=speaking.clock)

    retried = verdict(speaking, run_id, content_id, submission_id, key="second-key")
    reused = refusal(
        verdict, speaking, run_id, content_id, submission_id, score=0.25, key="second-key"
    )

    assert retried.verdict_id == first.verdict_id and retried.verdict_status == "applied"
    assert reused.payload.code == "idempotency_conflict"
    assert results(speaking, run_id) == 1


def test_a_superseded_submission_with_nothing_answering_names_no_phantom_successor(
    speaking: PolishWorkspace,
) -> None:
    """Review minor: with no live submission the refusal named `None`, or a successor that
    was itself gone, as the thing to judge instead."""

    run_id, content_id, first, _ = submitted(speaking, 214)
    again = take(speaking, run_id, content_id, data=spoken_bytes(215))
    assert again.submission is not None and again.artifact_id is not None
    successor = again.submission.submission_id
    artifact_service.purge(speaking.paths, artifact=again.artifact_id)

    failure = refusal(verdict, speaking, run_id, content_id, first)

    assert failure.payload.code == "assessment_submission_superseded"
    assert "None" not in failure.payload.message
    assert "judge " + successor not in failure.payload.message
    assert "assessment pending" in failure.payload.message
    assert failure.payload.details[0].context == {"successor": successor}


def test_the_cli_claims_and_releases(
    speaking: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id, _, submission_id, _ = submitted(speaking, 216)

    code, claimed = _cli(
        speaking, capsys, "assessment", "claim", "--run", run_id, "--judge", "cli-judge"
    )
    assert code == 0, claimed
    entry = claimed["data"]["claimed"][0]
    assert entry["submission"]["submission_id"] == submission_id
    assert entry["claimed_by"] == "cli-judge" and claimed["data"]["lease_seconds"] == 600

    code, released = _cli(
        speaking,
        capsys,
        "assessment",
        "release",
        "--claim",
        entry["claim_id"],
        "--reason",
        "judge restarted",
    )
    assert code == 0, released
    assert released["data"]["returned_to_queue"] is True
