"""C6: a judge's verdict arrives later than the answer, and lands exactly once.

The recordings are generated tones (`tests/support/recordings.py`), never anything anybody
said. What is under test is the account the workspace keeps of a verdict: bound to the
submission it judged, revalidated when it lands, held while the run is paused, applied in
the same commit that received it, and never credited twice however often it is delivered.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.paths import workspace_paths
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import assessment as assessment_service
from linguawiki.services import judging, withdrawal
from linguawiki.services import learners as learner_service
from linguawiki.services import recordings as recording_service
from tests.conftest import (
    PilotTemplate,
    PolishWorkspace,
    SyntheticWorkspace,
    materialize_pilot,
    polish_learner,
)
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
from tests.support.clocks import AdvancingClock

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
    them keeps none of it, on the verdict or on the result it produces.

    A release reason is the same prose by the same judge, so the rule covers it too: the
    release row and a terminal release's `withdrawn_reason` keep the placeholder, and the
    report echoes what was kept."""

    run_id, answered = two_submitted(speaking, 116 if paused else 121)
    (content_id, submission_id, _), (_, given_up, _) = answered
    if paused:
        pause(speaking, run_id)
    original = learner_service.track_context
    quoting = "the learner only said 'dzien dobry, jak sie masz' and stopped"

    def declining(database: Any, track_id: str) -> Any:
        record = original(database, track_id)
        preferences = {**record.preferences, "transcript_retention_consent": False}
        return record.model_copy(update={"preferences": preferences})

    with monkeypatch.context() as patch:
        patch.setattr(learner_service, "track_context", declining)
        claims = {
            entry.submission.submission_id: entry.claim_id
            for entry in claim(speaking, run_id).claimed
        }
        returned = release(speaking, claims[submission_id], reason=quoting)
        withdrawn = release(
            speaking,
            claims[given_up],
            terminal=True,
            code="assessment_audio_unintelligible",
            reason=quoting,
        )
        # A retry of the same release is compared by its digest, and replays.
        assert release(speaking, claims[submission_id], reason=quoting).replayed
        again = claimed_one(speaking, run_id)
        landed = verdict(
            speaking, run_id, content_id, submission_id, rubric=RUBRIC_WITH_WORDS, claim=again
        )

    assert returned.reason == withdrawn.reason == judging.RELEASE_REASON_WITHHELD
    assert returned.returned_to_queue and withdrawn.submission_status == "withdrawn"
    assert rows(speaking, "SELECT DISTINCT reason FROM judging_releases") == [
        (judging.RELEASE_REASON_WITHHELD,)
    ]
    assert rows(
        speaking,
        "SELECT withdrawn_code, withdrawn_reason FROM assessment_submissions "
        "WHERE submission_id = ?",
        [given_up],
    ) == [("assessment_audio_unintelligible", judging.RELEASE_REASON_WITHHELD)]
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


def test_a_verdict_naming_a_submission_from_another_learners_run_is_refused(
    speaking: PolishWorkspace,
) -> None:
    """A run belongs to one learner. The submission is resolved through the run it was
    made in, and one from another track's run is refused by that name -- not as an answer
    to some other task, which would send the judge looking for the right task."""

    from tests.integration.test_artifact_refusals import second_track

    _, _, theirs, _ = submitted(speaking, 241)
    other = second_track(speaking)
    run_id = assessment_service.start(
        speaking.paths, track=other.track_id, dimensions=["reading"], clock=speaking.clock
    ).run_id
    task = assessment_service.next_task(speaking.paths, run=run_id, clock=speaking.clock)
    assert isinstance(task, assessment_service.NextTaskReport)

    failure = refusal(verdict, speaking, run_id, task.content_id, theirs)

    assert failure.payload.code == "assessment_submission_out_of_scope"
    assert failure.payload.details[0].reason == "another track's run"
    assert results(speaking, run_id) == 0
    assert submission_state(speaking, theirs) == ("pending", None)


def _written(workspace: PolishWorkspace) -> tuple[Any, ...]:
    return (
        rows(workspace, "SELECT count(*) FROM assessment_verdicts")[0][0],
        rows(workspace, "SELECT count(*) FROM assessment_results")[0][0],
        rows(workspace, "SELECT count(*) FROM domain_events")[0][0],
        rows(workspace, "SELECT count(*) FROM judging_claims")[0][0],
    )


def test_a_verdict_selecting_one_track_cannot_reach_another_learners_submission(
    speaking: PolishWorkspace,
) -> None:
    """`--track A --submission <B's>` with no `--run`: the run is derived from the
    submission, and is B's. The caller's track is compared with it before anything is
    settled or written, so a verdict can never land in a learner the caller did not name."""

    from tests.integration.test_artifact_refusals import second_track

    _, content_id, theirs, _ = submitted(speaking, 242)
    other = second_track(speaking)
    before = _written(speaking)

    failure = refusal(
        assessment_service.record,
        speaking.paths,
        track=other.track_id,
        content_id=content_id,
        submission=theirs,
        score=0.75,
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        idempotency_key="cross-learner",
        clock=speaking.clock,
    )

    assert failure.payload.code == "assessment_submission_out_of_scope"
    assert failure.payload.details[0].reason == "another track's run"
    assert _written(speaking) == before
    assert submission_state(speaking, theirs) == ("pending", None)


def test_a_verdict_naming_another_learners_run_under_ones_own_track_is_refused(
    speaking: PolishWorkspace,
) -> None:
    """The same, with B's run named as well: `resolve_run` refuses it by whose run it is."""

    from tests.integration.test_artifact_refusals import second_track

    run_id, content_id, theirs, _ = submitted(speaking, 243)
    owner = rows(speaking, "SELECT track_id FROM assessment_runs WHERE run_id = ?", [run_id])
    other = second_track(speaking)
    before = _written(speaking)

    failure = refusal(
        assessment_service.record,
        speaking.paths,
        track=other.track_id,
        run=run_id,
        content_id=content_id,
        submission=theirs,
        score=0.75,
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        clock=speaking.clock,
    )

    assert failure.payload.code == "assessment_run_out_of_scope"
    assert failure.payload.details[0].context == {"track": owner[0][0]}
    assert _written(speaking) == before
    assert submission_state(speaking, theirs) == ("pending", None)


def test_a_claim_naming_another_learners_run_is_refused(speaking: PolishWorkspace) -> None:
    """Every command taking a track and a run goes through `resolve_run`, so the guard
    holds for a command that never names a submission."""

    from tests.integration.test_artifact_refusals import second_track

    run_id, _, theirs, _ = submitted(speaking, 244)
    other = second_track(speaking)
    before = _written(speaking)

    failure = refusal(
        judging.claim,
        speaking.paths,
        judge="synthetic-ai-judge",
        run=run_id,
        track=other.track_id,
        clock=speaking.clock,
    )

    assert failure.payload.code == "assessment_run_out_of_scope"
    assert _written(speaking) == before
    assert submission_state(speaking, theirs) == ("pending", None)


def _declining(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitute a track that declined transcript retention, where `release` reads it."""

    original = learner_service.track_context

    def declining(database: Any, track_id: str) -> Any:
        record = original(database, track_id)
        preferences = {**record.preferences, "transcript_retention_consent": False}
        return record.model_copy(update={"preferences": preferences})

    monkeypatch.setattr(learner_service, "track_context", declining)


def test_a_different_release_reason_on_a_declining_track_is_a_conflict(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both reasons are kept as the placeholder, so compared by what was kept they were
    one release. Compared by the digest of what arrived, the second is a different
    release, and is refused as one."""

    run_id, _, _, _ = submitted(speaking, 251)
    claim_id = claimed_one(speaking, run_id)
    _declining(monkeypatch)
    first = release(speaking, claim_id, reason="the learner said 'dzien dobry' and stopped")

    failure = refusal(release, speaking, claim_id, reason="the recording cut out")

    assert first.reason == judging.RELEASE_REASON_WITHHELD
    assert failure.payload.code == "assessment_claim_released"
    assert rows(speaking, "SELECT count(*) FROM judging_releases") == [(1,)]


def test_the_same_release_reason_after_a_consent_change_replays(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept as an excerpt, then retried after the track declined retention: the kept form
    would now be the placeholder, but the reason that arrived is the same, so it replays."""

    reason = "the learner said 'dzien dobry' and stopped"
    run_id, _, _, _ = submitted(speaking, 252)
    claim_id = claimed_one(speaking, run_id)
    first = release(speaking, claim_id, reason=reason)
    assert first.reason != judging.RELEASE_REASON_WITHHELD
    _declining(monkeypatch)

    again = release(speaking, claim_id, reason=reason)

    assert again.replayed and again.reason == first.reason
    assert rows(speaking, "SELECT reason_hash FROM judging_releases") == [
        (hashlib.sha256(reason.encode("utf-8")).hexdigest(),)
    ]


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


def test_a_claim_with_no_lease_end_reads_as_expired_rather_than_raising(
    speaking: PolishWorkspace,
) -> None:
    """The column is NOT NULL; a restore that predates the constraint is how one arrives.
    Readers -- the run report, `db check` -- must still answer."""

    run_id, _, submission_id, _ = submitted(speaking, 207)
    claim_id = claimed_one(speaking, run_id)
    with (
        open_writer(speaking.paths, command="test.tamper", clock=speaking.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("CREATE TABLE claims_copy AS SELECT * FROM judging_claims")
        transaction.execute("DROP TABLE judging_claims")
        transaction.execute("ALTER TABLE claims_copy RENAME TO judging_claims")
        transaction.execute(
            "UPDATE judging_claims SET lease_expires_at = NULL WHERE claim_id = ?", [claim_id]
        )

    with open_writer(speaking.paths, command="test.read", clock=speaking.clock) as database:
        state = judging.claim_states(database, run_id)[submission_id]
        claimed_at = database.scalar(
            "SELECT claimed_at FROM judging_claims WHERE claim_id = ?", [claim_id]
        )

    assert state.live_claim is None and state.attempts == 1
    assert state.last_ended_at == claimed_at
    report = assessment_service.report(speaking.paths, run=run_id, clock=speaking.clock)
    assert [entry.claim_state for entry in report.outstanding_judgements] == ["unclaimed"]
    database_check(speaking)


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


@pytest.mark.parametrize(
    "code",
    [
        "assessment_audio_purged",
        "assessment_judging_exhausted",
        "assessment_response_not_retained",
        "assessment_run_abandoned",
        "assessment_run_finalized",
        "assessment_consent_withdrawn",
    ],
)
def test_a_terminal_release_may_not_borrow_a_system_code(
    speaking: PolishWorkspace, code: str
) -> None:
    run_id, _, submission_id, _ = submitted(speaking, 217)
    claim_id = claimed_one(speaking, run_id)

    failure = refusal(release, speaking, claim_id, terminal=True, code=code)

    assert failure.payload.code == "assessment_release_code_reserved"
    assert "assessment_audio_unintelligible" in failure.payload.message
    assert failure.payload.details[0].context == {"code": code}
    assert submission_state(speaking, submission_id) == ("pending", None)
    assert rows(speaking, "SELECT count(*) FROM judging_releases") == [(0,)]


def audits_naming(workspace: PolishWorkspace, submission_id: str) -> list[Any]:
    return rows(
        workspace,
        "SELECT command, after_summary FROM audit_log WHERE affected_records_json LIKE ? "
        "ORDER BY recorded_at",
        [f"%{submission_id}%"],
    )


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
    # The serve says what it withdrew, and so does the audit log -- once.
    assert any(submission_id in warning for warning in served.warnings)
    assert [
        command
        for command, summary in audits_naming(speaking, submission_id)
        if judging.EXHAUSTED_CODE in str(summary)
    ] == ["assessment.next"]
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
    ["claim", "pause", "finalize", "record", "release"],
)
def test_every_writer_touching_the_run_settles_what_lapsed_and_says_so(
    speaking: PolishWorkspace, writer: str
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 208)
    claims = exhaust(speaking, run_id)
    audited_before = audits_naming(speaking, submission_id)

    def named(warnings: tuple[str, ...]) -> bool:
        return any(submission_id in warning for warning in warnings)

    if writer == "claim":
        report = claim(speaking, run_id)
        assert report.claimed == ()
        assert [entry.code for entry in report.withdrawn] == [judging.EXHAUSTED_CODE]
    elif writer == "pause":
        assert named(
            assessment_service.set_status(
                speaking.paths, status="paused", run=run_id, clock=speaking.clock
            ).warnings
        )
    elif writer == "finalize":
        assert named(
            assessment_service.finalize(speaking.paths, run=run_id, clock=speaking.clock).warnings
        )
    elif writer == "record":
        # A keyless verdict naming no claim spares nothing: the submission is settled first,
        # and the verdict refused by what became of it. The sweep committed before the
        # refusal, so the refusal is where the caller is told.
        failure = refusal(verdict, speaking, run_id, content_id, submission_id)
        assert failure.payload.code == "assessment_submission_withdrawn"
        settled = [detail for detail in failure.payload.details if detail.field == "settled"]
        assert [detail.context["submission_id"] for detail in settled] == [submission_id]
        assert settled[0].context["code"] == judging.EXHAUSTED_CODE
    else:
        # Releasing the first, long-expired claim: it spares only an unreleased claim's own
        # submission -- and it is that one -- so release itself settles it after recording.
        report = release(speaking, claims[0])
        assert [entry.submission_id for entry in report.withdrawn] == [submission_id]

    assert submission_state(speaking, submission_id) == ("withdrawn", judging.EXHAUSTED_CODE)
    # Audited exactly once, by whichever transaction withdrew it.
    assert len(audits_naming(speaking, submission_id)) == len(audited_before) + 1


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


# --- run transitions -------------------------------------------------------------------------


def two_submitted(workspace: PolishWorkspace, seed: int) -> tuple[str, list[tuple[str, str, str]]]:
    """A run with two recordings waiting for a judge, one per spoken dimension:
    `(run_id, [(content_id, submission_id, artifact_id), ...])`."""

    run_id = start_spoken(workspace)
    answered = []
    for offset in (0, 1):
        task = serve(workspace, run_id)
        taken = take(workspace, run_id, task.content_id, data=spoken_bytes(seed + offset))
        assert taken.submission is not None and taken.artifact_id is not None
        answered.append((task.content_id, taken.submission.submission_id, taken.artifact_id))
    return run_id, answered


def resume(workspace: PolishWorkspace, run_id: str) -> assessment_service.AssessmentRunReport:
    return assessment_service.set_status(
        workspace.paths, status="in-progress", run=run_id, clock=workspace.clock
    )


def recording_path(workspace: PolishWorkspace, artifact_id: str) -> Any:
    relative = rows(
        workspace, "SELECT relative_path FROM artifacts WHERE artifact_id = ?", [artifact_id]
    )[0][0]
    return workspace.root / str(relative)


def outcome_of(workspace: PolishWorkspace, verdict_id: str | None) -> tuple[Any, ...]:
    return tuple(
        rows(
            workspace,
            "SELECT outcome, code FROM assessment_verdict_outcomes WHERE verdict_id = ?",
            [verdict_id],
        )[0]
    )


def test_a_resume_applies_held_verdicts_in_the_order_they_arrived(
    speaking: PolishWorkspace,
) -> None:
    run_id, answered = two_submitted(speaking, 301)
    pause(speaking, run_id)
    # Delivered in the opposite order to the one they were answered in: arrival decides.
    second, first = answered
    held = [
        verdict(speaking, run_id, content_id, submission_id, score=score)
        for (content_id, submission_id, _), score in ((first, 0.25), (second, 0.75))
    ]
    assert all(entry.held for entry in held)
    assert_clean(speaking)

    resumed = resume(speaking, run_id)

    assert resumed.status == "in-progress"
    assert [entry.verdict_id for entry in resumed.applied_verdicts] == [
        entry.verdict_id for entry in held
    ]
    assert [entry.score for entry in resumed.applied_verdicts] == [0.25, 0.75]
    assert resumed.voided_verdicts == ()
    assert resumed.tasks_recorded == 2
    # Folded in arrival order, each on the posterior the one before it left.
    recorded = rows(
        speaking,
        "SELECT result_id FROM assessment_results WHERE run_id = ? ORDER BY recorded_at, result_id",
        [run_id],
    )
    assert [row[0] for row in recorded] == [entry.result_id for entry in resumed.applied_verdicts]
    assert rows(speaking, "SELECT DISTINCT status FROM assessment_submissions") == [("judged",)]
    for entry in held:
        assert outcome_of(speaking, entry.verdict_id) == ("applied", None)
    assert_clean(speaking)


@pytest.mark.parametrize("invalidation", ["altered", "missing", "superseded"])
def test_a_held_verdict_that_no_longer_holds_is_voided_and_the_resume_succeeds(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    from tests.integration.test_client_audio import Crash as CaptureCrash
    from tests.integration.test_client_audio import crash_at

    run_id, answered = two_submitted(speaking, 311)
    (good_content, good, _), (bad_content, bad, bad_artifact) = answered
    if invalidation == "superseded":
        # The learner answered again and the capture was staged; the run was paused before
        # it was promoted, and recovery promotes it on the paused run -- superseding the
        # answer the held verdict judged.
        with monkeypatch.context() as patched:
            crash_at(patched, "_promote")
            with pytest.raises(CaptureCrash):
                take(speaking, run_id, bad_content, data=spoken_bytes(399))
    pause(speaking, run_id)
    kept = verdict(speaking, run_id, good_content, good)
    stale = verdict(speaking, run_id, bad_content, bad, score=0.5)
    successor = None
    if invalidation == "altered":
        path = recording_path(speaking, bad_artifact)
        path.write_bytes(path.read_bytes() + b"altered")
    elif invalidation == "missing":
        recording_path(speaking, bad_artifact).unlink()
    else:
        recovered = recording_service.recover(speaking.paths, clock=speaking.clock)
        assert len(recovered.registered) == 1
        successor = rows(
            speaking,
            "SELECT superseded_by FROM assessment_submissions WHERE submission_id = ?",
            [bad],
        )[0][0]
        assert successor is not None

    resumed = resume(speaking, run_id)

    assert resumed.status == "in-progress"
    assert [entry.verdict_id for entry in resumed.applied_verdicts] == [kept.verdict_id]
    assert [entry.verdict_id for entry in resumed.voided_verdicts] == [stale.verdict_id]
    void = resumed.voided_verdicts[0]
    assert void.submission_id == bad and void.content_id == bad_content
    if invalidation == "superseded":
        assert void.code == "assessment_submission_superseded"
        assert str(successor) in void.reason
        # The successor is waiting for a judge, and judging it works.
        assert verdict(speaking, run_id, bad_content, str(successor)).verdict_status == "applied"
    else:
        assert void.code == f"assessment_audio_{invalidation}"
        assert submission_state(speaking, bad) == ("withdrawn", void.code)
    assert outcome_of(speaking, stale.verdict_id) == ("void", void.code)
    assert_clean(speaking)


def test_a_resume_after_a_purge_while_paused_has_nothing_left_to_void(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, artifact_id = submitted(speaking, 321)
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, submission_id)

    purged = artifact_service.purge(speaking.paths, artifact=artifact_id)
    resumed = resume(speaking, run_id)

    # The purge voided it, and said so; the resume finds nothing held.
    assert purged.voided_verdicts == (held.verdict_id,)
    assert any(str(held.verdict_id) in warning for warning in purged.warnings)
    assert resumed.applied_verdicts == () and resumed.voided_verdicts == ()
    assert outcome_of(speaking, held.verdict_id) == ("void", withdrawal.PURGED_CODE)
    assert_clean(speaking)


def test_a_crash_during_the_resume_leaves_the_verdict_held_on_a_paused_run(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart with an unapplied verdict resolves to a state `db check` accepts: the
    resume is one transaction, so a crash inside it leaves the run paused and the verdict
    held, and resuming again applies it."""

    run_id, content_id, submission_id, _ = submitted(speaking, 331)
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, submission_id)

    def die(*_args: Any, **_kwargs: Any) -> Any:
        raise Crash("write_verdict")

    with monkeypatch.context() as patched:
        patched.setattr(assessment_service, "write_verdict", die)
        with pytest.raises(Crash):
            resume(speaking, run_id)

    assert rows(speaking, "SELECT status FROM assessment_runs WHERE run_id = ?", [run_id]) == [
        ("paused",)
    ]
    assert verdict_rows(speaking, submission_id) == [(held.verdict_id, 0.75, None)]
    assert_clean(speaking)

    resumed = resume(speaking, run_id)

    assert [entry.verdict_id for entry in resumed.applied_verdicts] == [held.verdict_id]
    assert_clean(speaking)


def test_abandoning_withdraws_what_waits_and_voids_what_is_held(
    speaking: PolishWorkspace,
) -> None:
    run_id, answered = two_submitted(speaking, 341)
    (held_content, held_submission, _), (waiting_content, waiting, _) = answered
    pause(speaking, run_id)
    held = verdict(speaking, run_id, held_content, held_submission)

    abandoned = assessment_service.set_status(
        speaking.paths, status="abandoned", run=run_id, clock=speaking.clock
    )

    assert abandoned.status == "abandoned"
    assert {entry.submission_id for entry in abandoned.withdrawn} == {held_submission, waiting}
    assert {entry.code for entry in abandoned.withdrawn} == {withdrawal.ABANDONED_CODE}
    by_submission = {entry.submission_id: entry for entry in abandoned.withdrawn}
    assert by_submission[held_submission].voided_verdicts == (held.verdict_id,)
    assert by_submission[waiting].voided_verdicts == ()
    assert [entry.verdict_id for entry in abandoned.voided_verdicts] == [held.verdict_id]
    assert abandoned.voided_verdicts[0].code == withdrawal.ABANDONED_CODE
    assert outcome_of(speaking, held.verdict_id) == ("void", withdrawal.ABANDONED_CODE)
    assert rows(
        speaking, "SELECT DISTINCT status FROM assessment_run_tasks WHERE run_id = ?", [run_id]
    ) == [("skipped",)]
    # A verdict arriving afterwards meets the withdrawal, by its code.
    late = refusal(verdict, speaking, run_id, waiting_content, waiting)
    assert late.payload.code == "assessment_submission_withdrawn"
    assert late.payload.details[0].context["code"] == withdrawal.ABANDONED_CODE
    audit = rows(
        speaking,
        "SELECT after_summary FROM audit_log WHERE command = 'assessment.pause' "
        "ORDER BY recorded_at DESC LIMIT 1",
    )
    assert str(held.verdict_id) in str(audit[0][0])
    assert_clean(speaking)


def test_finalize_refuses_while_judgement_is_outstanding_and_the_flag_it_names_works(
    speaking: PolishWorkspace,
) -> None:
    run_id, answered = two_submitted(speaking, 351)
    (held_content, held_submission, _), (waiting_content, waiting, _) = answered
    pause(speaking, run_id)
    held = verdict(speaking, run_id, held_content, held_submission)

    refused = refusal(
        assessment_service.finalize,
        speaking.paths,
        run=run_id,
        idempotency_key="close-it",
        clock=speaking.clock,
    )

    assert refused.payload.code == "assessment_judgement_outstanding"
    assert "--exclude-outstanding" in refused.payload.message
    listed = {
        (detail.field, detail.context.get("submission_id"), detail.context.get("verdict_id"))
        for detail in refused.payload.details
    }
    assert listed == {
        ("submission", held_submission, None),
        ("submission", waiting, None),
        ("verdict", held_submission, held.verdict_id),
    }
    # Nothing moved: the run is paused, the verdict held, both answers waiting.
    assert rows(speaking, "SELECT status FROM assessment_runs WHERE run_id = ?", [run_id]) == [
        ("paused",)
    ]
    assert rows(speaking, "SELECT DISTINCT status FROM assessment_submissions") == [("pending",)]
    assert_clean(speaking)

    # The same key with the flag is a new request, not the refusal replayed.
    closed = assessment_service.finalize(
        speaking.paths,
        run=run_id,
        exclude_outstanding=True,
        idempotency_key="close-it",
        clock=speaking.clock,
    )

    assert closed.status == "finalized"
    assert {entry.submission_id for entry in closed.excluded} == {held_submission, waiting}
    assert {entry.code for entry in closed.excluded} == {withdrawal.FINALIZED_CODE}
    assert [entry.verdict_id for entry in closed.voided_verdicts] == [held.verdict_id]
    assert outcome_of(speaking, held.verdict_id) == ("void", withdrawal.FINALIZED_CODE)
    assert_clean(speaking)

    # Its retry replays and says the same; the key without the flag is another request.
    replayed = assessment_service.finalize(
        speaking.paths,
        run=run_id,
        exclude_outstanding=True,
        idempotency_key="close-it",
        clock=speaking.clock,
    )
    assert replayed.excluded == closed.excluded
    assert replayed.voided_verdicts == closed.voided_verdicts
    reused = refusal(
        assessment_service.finalize,
        speaking.paths,
        run=run_id,
        idempotency_key="close-it",
        clock=speaking.clock,
    )
    assert reused.payload.code == "idempotency_conflict"
    # A verdict arriving afterwards is refused against the closed run, never applied.
    late = refusal(verdict, speaking, run_id, waiting_content, waiting)
    assert late.payload.code == "assessment_submission_withdrawn"
    assert late.payload.details[0].context["code"] == withdrawal.FINALIZED_CODE
    assert results(speaking, run_id) == 0


def test_a_finalize_with_nothing_outstanding_hashes_as_it_did_before_the_flag(
    speaking: PolishWorkspace,
) -> None:
    from linguawiki import idempotency

    run_id, content_id, submission_id, _ = submitted(speaking, 361)
    verdict(speaking, run_id, content_id, submission_id)

    closed = assessment_service.finalize(
        speaking.paths, run=run_id, idempotency_key="old-key", clock=speaking.clock
    )

    assert closed.excluded == () and closed.voided_verdicts == ()
    stored = rows(
        speaking, "SELECT payload_json FROM domain_events WHERE idempotency_key = 'old-key'"
    )
    import json

    assert json.loads(stored[0][0])[idempotency.REQUEST_HASH_FIELD] == idempotency.request_hash(
        operation=assessment_service.FINALIZED_EVENT, run_id=run_id, reason="completed"
    )


def test_the_cli_finalizes_excluding_outstanding_judgement(
    speaking: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id, _, submission_id, _ = submitted(speaking, 371)

    code, refused = _cli(speaking, capsys, "assessment", "finalize", "--run", run_id)
    assert code != 0 and refused["error"]["code"] == "assessment_judgement_outstanding"

    code, payload = _cli(
        speaking, capsys, "assessment", "finalize", "--run", run_id, "--exclude-outstanding"
    )

    assert code == 0, payload
    assert [entry["submission_id"] for entry in payload["data"]["excluded"]] == [submission_id]


# --- lifecycle -------------------------------------------------------------------------------


def test_withdrawing_audio_consent_purges_what_waits_and_voids_what_is_held(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, artifact_id = submitted(speaking, 381)
    path = recording_path(speaking, artifact_id)
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, submission_id)

    updated = learner_service.update_track(
        speaking.paths,
        preferences=learner_service.TrackPreferences(audio_retention_consent=False),
        clock=speaking.clock,
    )

    assert updated.preferences["audio_retention_consent"] is False
    code, reason = rows(
        speaking,
        "SELECT withdrawn_code, withdrawn_reason FROM assessment_submissions "
        "WHERE submission_id = ?",
        [submission_id],
    )[0]
    assert code == withdrawal.PURGED_CODE and withdrawal.CONSENT_WITHDRAWN in str(reason)
    assert rows(
        speaking,
        "SELECT purge_reason, purged_at IS NOT NULL FROM artifacts WHERE artifact_id = ?",
        [artifact_id],
    ) == [("learner-request", True)]
    assert not path.exists()
    assert outcome_of(speaking, held.verdict_id) == ("void", withdrawal.PURGED_CODE)
    assert any(
        submission_id in warning and str(held.verdict_id) in warning for warning in updated.warnings
    )
    # The purge and the preference are one audit-logged change.
    audit = rows(
        speaking,
        "SELECT command FROM audit_log WHERE affected_records_json LIKE ? ORDER BY recorded_at",
        [f"%{artifact_id}%"],
    )
    assert [row[0] for row in audit][-1] == "track.update"
    late = refusal(verdict, speaking, run_id, content_id, submission_id)
    assert late.payload.code == "assessment_submission_withdrawn"
    assert late.payload.details[0].context["code"] == withdrawal.PURGED_CODE
    assert_clean(speaking)


def test_a_consent_withdrawal_whose_purge_fails_changes_nothing(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The purge runs in the preference's own transaction: the preference, the tombstone,
    and the withdrawal land together or not at all."""

    run_id, _, submission_id, _ = submitted(speaking, 391)

    def cannot_delete(*_args: Any, **_kwargs: Any) -> None:
        raise LinguaWikiError("artifact_file_not_removed", "the disk refused the deletion")

    monkeypatch.setattr(artifact_service, "_remove_file", cannot_delete)
    failure = refusal(
        learner_service.update_track,
        speaking.paths,
        preferences=learner_service.TrackPreferences(audio_retention_consent=False),
        clock=speaking.clock,
    )

    assert failure.payload.code == "artifact_file_not_removed"
    assert submission_state(speaking, submission_id) == ("pending", None)
    assert rows(
        speaking,
        "SELECT value_json FROM track_preferences WHERE key = 'audio_retention_consent'",
    ) == [("true",)]
    assert run_id


def test_a_preference_change_that_withdraws_no_consent_settles_nothing(
    speaking: PolishWorkspace,
) -> None:
    _, _, submission_id, _ = submitted(speaking, 395)

    updated = learner_service.update_track(
        speaking.paths,
        preferences=learner_service.TrackPreferences(weekly_minutes=90),
        clock=speaking.clock,
    )

    assert updated.warnings == ()
    assert submission_state(speaking, submission_id) == ("pending", None)


def test_withdrawing_transcript_consent_withdraws_written_answers_and_clears_their_text(
    polish_workspace: PolishWorkspace,
) -> None:
    """Written answers arrive with a later task; the row is inserted directly, shaped as
    0035 says a text submission is."""

    import hashlib

    from linguawiki.ids import AssessmentId

    learner_service.update_track(
        polish_workspace.paths,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=True),
        clock=polish_workspace.clock,
    )
    run_id = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock).run_id
    task = serve(polish_workspace, run_id)
    answer = "synthetic written answer"
    submission_id, verdict_id = str(AssessmentId.new()), str(AssessmentId.new())
    with (
        open_writer(polish_workspace.paths, command="test.seed") as database,
        database.transaction() as transaction,
    ):
        now = transaction.now()
        transaction.execute(
            "INSERT INTO assessment_submissions (submission_id, run_id, content_id, kind, "
            "capture_id, artifact_id, response_visibility, response_text, response_digest, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'text', 'text-key', NULL, 'full', ?, ?, 'pending', ?, ?)",
            [
                submission_id,
                run_id,
                task.content_id,
                answer,
                hashlib.sha256(answer.encode()).hexdigest(),
                now,
                now,
            ],
        )
        transaction.execute(
            "UPDATE assessment_runs SET status = 'paused' WHERE run_id = ?", [run_id]
        )
        transaction.execute(
            "INSERT INTO assessment_verdicts (verdict_id, submission_id, claim_id, raw_score, "
            "rubric_json, assessor_kind, assessor, confidence, response_visibility, "
            "response_excerpt, response_hash, received_at, held) "
            "VALUES (?, ?, NULL, 0.5, '{}', 'ai', 'synthetic-ai-judge', 'medium', "
            "'withheld', NULL, NULL, ?, true)",
            [verdict_id, submission_id, now],
        )
    assert_clean(polish_workspace)

    updated = learner_service.update_track(
        polish_workspace.paths,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=polish_workspace.clock,
    )

    stored = rows(
        polish_workspace,
        "SELECT status, withdrawn_code, response_text, response_digest "
        "FROM assessment_submissions WHERE submission_id = ?",
        [submission_id],
    )[0]
    assert stored[:3] == ("withdrawn", withdrawal.NOT_RETAINED_CODE, None)
    assert stored[3] == hashlib.sha256(answer.encode()).hexdigest()
    assert outcome_of(polish_workspace, verdict_id) == ("void", withdrawal.NOT_RETAINED_CODE)
    assert any(submission_id in warning for warning in updated.warnings)
    assert rows(
        polish_workspace,
        "SELECT status FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
        [run_id, task.content_id],
    ) == [("skipped",)]
    assert_clean(polish_workspace)


# --- deferred minors -------------------------------------------------------------------------


def test_a_settled_verdict_refusal_names_the_held_verdict_it_voided(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, artifact_id = submitted(speaking, 401)
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, submission_id)
    path = recording_path(speaking, artifact_id)
    path.write_bytes(path.read_bytes() + b"altered")

    failure = refusal(verdict, speaking, run_id, content_id, submission_id, score=0.25)

    assert failure.payload.code == "assessment_audio_altered"
    voided = [detail for detail in failure.payload.details if detail.field == "voided"]
    assert [detail.context for detail in voided] == [
        {"verdict_id": held.verdict_id, "submission_id": submission_id}
    ]
    assert outcome_of(speaking, held.verdict_id) == ("void", "assessment_audio_altered")


def test_applying_a_verdict_to_a_submission_no_longer_pending_fails_loudly(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 411)
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, submission_id)
    assert held.verdict_id is not None

    with open_writer(speaking.paths, command="test.resume", clock=speaking.clock) as database:
        request = assessment_service.held_verdict_request(database, held.verdict_id)
        plan = assessment_service.plan_verdict(database, request, root=speaking.root)
        assert isinstance(plan, assessment_service.VerdictPlan)
        with (
            pytest.raises(AssertionError, match="judged 0 pending"),
            database.transaction() as transaction,
        ):
            # The state moves under the plan: something withdrew the submission.
            withdrawal.withdraw_submission(
                transaction, submission_id=submission_id, code="test_moved", reason="moved"
            )
            assessment_service.write_verdict(transaction, plan)

    assert submission_state(speaking, submission_id) == ("pending", None)
    assert results(speaking, run_id) == 0


# --- fix round 1 -----------------------------------------------------------------------------


def test_a_held_verdict_applied_after_transcript_consent_was_withdrawn_copies_no_text(
    speaking: PolishWorkspace,
) -> None:
    """Ruling R9: applying a held verdict re-runs retention against the consent in force
    now. The result is what a verdict arriving now would store; the verdict row keeps what
    it stored under the consent valid when it arrived."""

    run_id, content_id, submission_id, _ = submitted(speaking, 421)
    pause(speaking, run_id)
    words = "the learner said something about the weather"
    held = assessment_service.record(
        speaking.paths,
        run=run_id,
        content_id=content_id,
        score=0.5,
        submission=submission_id,
        response_excerpt=words,
        rubric={"accuracy": 0.5, "note": words},
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        clock=speaking.clock,
    )
    assert held.held
    stored = rows(
        speaking,
        "SELECT response_visibility, response_excerpt, rubric_json FROM assessment_verdicts "
        "WHERE verdict_id = ?",
        [held.verdict_id],
    )[0]
    assert stored[0] == "excerpt" and stored[1] == words and words in str(stored[2])
    # Withdrawn while the run is paused. Written directly: a recording track cannot decline
    # transcripts through `track update` (audio consent requires it), and declining audio
    # too would purge the recording and void the verdict instead -- written answers (Task 5)
    # are where this arises through the command.
    with (
        open_writer(speaking.paths, command="test.consent") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE track_preferences SET value_json = 'false' "
            "WHERE key = 'transcript_retention_consent'"
        )

    resumed = resume(speaking, run_id)

    assert [entry.verdict_id for entry in resumed.applied_verdicts] == [held.verdict_id]
    result = rows(
        speaking,
        "SELECT response_visibility, response_excerpt, rubric_json FROM assessment_results "
        "WHERE run_id = ?",
        [run_id],
    )
    assert len(result) == 1
    visibility, excerpt, rubric_json = result[0]
    assert visibility == "withheld" and excerpt is None
    assert words not in str(rubric_json)
    assert json.loads(str(rubric_json)) == {"accuracy": 0.5, "note": None}
    # The insert-only verdict row is left as it was stored (the deferred stored-excerpt gap).
    assert (
        rows(
            speaking,
            "SELECT response_excerpt FROM assessment_verdicts WHERE verdict_id = ?",
            [held.verdict_id],
        )[0][0]
        == words
    )


def test_a_held_verdict_resumed_under_a_clock_that_stepped_backwards_is_clean(
    speaking: PolishWorkspace,
) -> None:
    """R19 by structure, not by clock. A verdict held, then applied at resume after the
    clock stepped back an hour, is received *after* its outcome is decided -- and it was
    held, so its excerpt is allowed. `held` was written at insert; no time is consulted."""

    words = "the learner said something about the weather"
    run_id, content_id, submission_id, _ = submitted(speaking, 433)
    pause(speaking, run_id)
    held = assessment_service.record(
        speaking.paths,
        run=run_id,
        content_id=content_id,
        score=0.5,
        submission=submission_id,
        response_excerpt=words,
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        clock=speaking.clock,
    )
    assert held.held
    speaking.clock.advance(-timedelta(hours=1))

    resumed = resume(speaking, run_id)

    assert [entry.verdict_id for entry in resumed.applied_verdicts] == [held.verdict_id]
    assert rows(
        speaking,
        "SELECT verdict.held, verdict.response_excerpt, outcome.outcome, "
        "outcome.decided_at < verdict.received_at FROM assessment_verdicts verdict "
        "JOIN assessment_verdict_outcomes outcome USING (verdict_id) WHERE verdict_id = ?",
        [held.verdict_id],
    ) == [(True, words, "applied", True)]
    assert_clean(speaking)


def test_a_recording_verdict_keeps_an_excerpt_only_while_it_is_the_only_copy(
    speaking: PolishWorkspace,
) -> None:
    """Ruling R19. Applied when it arrives, the verdict keeps no excerpt -- the result holds
    the retained form -- and keeps the hash that names the answer. Held, it keeps the
    excerpt, because until resume nothing else does, and the resume writes the result from
    it."""

    words = "the learner said something about the weather"
    run_id, content_id, submission_id, _ = submitted(speaking, 431)

    def deliver(run: str, content: str, submission: str) -> assessment_service.AssessmentRunReport:
        return assessment_service.record(
            speaking.paths,
            run=run,
            content_id=content,
            score=0.5,
            submission=submission,
            response_excerpt=words,
            assessor_kind="ai",
            assessor="synthetic-ai-judge",
            clock=speaking.clock,
        )

    immediate = deliver(run_id, content_id, submission_id)
    assert not immediate.held
    assert rows(
        speaking,
        "SELECT verdict.response_visibility, verdict.response_excerpt, "
        "verdict.response_hash IS NOT DISTINCT FROM result.response_hash, "
        "result.response_visibility, result.response_excerpt "
        "FROM assessment_verdicts verdict "
        "JOIN assessment_verdict_outcomes outcome USING (verdict_id) "
        "JOIN assessment_results result ON result.result_id = outcome.result_id "
        "WHERE verdict.verdict_id = ?",
        [immediate.verdict_id],
    ) == [("withheld", None, True, "excerpt", words)]

    held_run, held_content, held_submission, _ = submitted(speaking, 432)
    pause(speaking, held_run)
    held = deliver(held_run, held_content, held_submission)
    assert held.held
    kept = (
        "SELECT response_visibility, response_excerpt FROM assessment_verdicts WHERE verdict_id = ?"
    )
    assert rows(speaking, kept, [held.verdict_id]) == [("excerpt", words)]
    assert_clean(speaking)

    resumed = resume(speaking, held_run)

    assert [entry.verdict_id for entry in resumed.applied_verdicts] == [held.verdict_id]
    assert rows(
        speaking,
        "SELECT response_visibility, response_excerpt, raw_score FROM assessment_results "
        "WHERE run_id = ?",
        [held_run],
    ) == [("excerpt", words, 0.5)]
    # Insert-only, so the held row keeps what it held (the deferred C1 gap) -- and the
    # check allows exactly that: an applied verdict that was received before it was applied.
    assert rows(speaking, kept, [held.verdict_id]) == [("excerpt", words)]
    assert_clean(speaking)


def test_narrowed_retention_only_ever_narrows() -> None:
    narrowed = assessment_service._narrowed_retention
    long = "x" * 500
    assert narrowed("full", long, preferences={"transcript_retention_consent": True}) == (
        "full",
        long,
    )
    assert narrowed("full", long, preferences={}) == ("excerpt", long[:240])
    assert narrowed("excerpt", "kept", preferences={}) == ("excerpt", "kept")
    assert narrowed("excerpt", "kept", preferences={"transcript_retention_consent": False}) == (
        "withheld",
        None,
    )
    # Never wider: what was withheld stays withheld under any consent.
    assert narrowed("withheld", None, preferences={"transcript_retention_consent": True}) == (
        "withheld",
        None,
    )


def test_abandoning_voids_a_superseded_answers_held_verdict_naming_its_successor(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_client_audio import Crash as CaptureCrash
    from tests.integration.test_client_audio import crash_at

    run_id, content_id, first, _ = submitted(speaking, 431)
    with monkeypatch.context() as patched:
        crash_at(patched, "_promote")
        with pytest.raises(CaptureCrash):
            take(speaking, run_id, content_id, data=spoken_bytes(432))
    pause(speaking, run_id)
    held = verdict(speaking, run_id, content_id, first)
    assert len(recording_service.recover(speaking.paths, clock=speaking.clock).registered) == 1
    successor = str(
        rows(
            speaking,
            "SELECT superseded_by FROM assessment_submissions WHERE submission_id = ?",
            [first],
        )[0][0]
    )

    abandoned = assessment_service.set_status(
        speaking.paths, status="abandoned", run=run_id, clock=speaking.clock
    )

    void = {entry.verdict_id: entry for entry in abandoned.voided_verdicts}[held.verdict_id]
    assert void.code == withdrawal.SUPERSEDED_CODE and successor in void.reason
    assert [(entry.submission_id, entry.code) for entry in abandoned.withdrawn] == [
        (successor, withdrawal.ABANDONED_CODE)
    ]
    assert outcome_of(speaking, held.verdict_id) == ("void", withdrawal.SUPERSEDED_CODE)
    assert_clean(speaking)


# --- text submissions --------------------------------------------------------------------------
#
# Written answers are synthetic sentences written for these tests, never anything a learner
# wrote. The pilot pack's `writing` dimension serves `extended-productive` tasks in the
# `writing` modality, which is what `machine+judged` adds.

ANSWER = (
    "Dzień dobry, pralka nie działa od wczoraj. Kiedy może pan przyjść ją naprawić? "
    "Jestem w domu po siedemnastej. Pozdrawiam, Anna."
)


def keep_writing(workspace: PolishWorkspace, consent: bool | None = True) -> None:
    learner_service.update_track(
        workspace.paths,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=consent),
        clock=workspace.clock,
    )


def unset_transcript_consent(workspace: PolishWorkspace) -> None:
    with (
        open_writer(workspace.paths, command="test.tamper") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "DELETE FROM track_preferences WHERE key = 'transcript_retention_consent'"
        )


@pytest.fixture
def writing(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    keep_writing(polish_workspace)
    return polish_workspace


def start_written(
    workspace: PolishWorkspace, *, scoring: str = "machine+judged"
) -> assessment_service.AssessmentRunReport:
    return assessment_service.start(
        workspace.paths, dimensions=["writing"], scoring=scoring, clock=workspace.clock
    )


def hand_in(
    workspace: PolishWorkspace,
    run_id: str,
    content_id: str,
    *,
    key: str = "answer-1",
    response: str = ANSWER,
) -> Any:
    from linguawiki.services import written_answers

    return written_answers.submit(
        workspace.paths,
        run=run_id,
        content_id=content_id,
        submission_key=key,
        response=response,
        clock=workspace.clock,
        actor="client",
    )


def written(workspace: PolishWorkspace, *, key: str = "answer-1") -> tuple[str, str, str]:
    """A writing run with one written answer waiting: `(run_id, content_id, submission_id)`."""

    run_id = start_written(workspace).run_id
    task = serve(workspace, run_id)
    assert task.modality == "writing" and task.task_type == "extended-productive"
    report = hand_in(workspace, run_id, task.content_id, key=key)
    return run_id, task.content_id, report.submission.submission_id


def digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_anywhere(workspace: PolishWorkspace, text: str) -> list[str]:
    """Every column of every table this stage writes that holds any part of `text`."""

    fragment = text[:30]
    found: list[str] = []
    for table in (
        "assessment_submissions",
        "assessment_verdicts",
        "assessment_verdict_outcomes",
        "assessment_results",
        "judging_claims",
        "judging_releases",
        "domain_events",
        "audit_log",
    ):
        for row in rows(workspace, f"SELECT * FROM {table}"):
            if any(isinstance(value, str) and fragment in value for value in row):
                found.append(table)
    return found


def test_a_written_answer_is_handed_in_claimed_with_its_text_and_judged(
    writing: PolishWorkspace,
) -> None:
    from linguawiki.services import assessment_view as view_service

    run_id, content_id, submission_id = written(writing)

    stored = rows(
        writing,
        "SELECT kind, capture_id, artifact_id, response_visibility, response_text, "
        "response_digest, status FROM assessment_submissions WHERE submission_id = ?",
        [submission_id],
    )
    assert stored == [("text", "answer-1", None, "full", ANSWER, digest(ANSWER), "pending")]
    # Registration does not score: the task waits, and the screen says what for -- without
    # the learner's words, which the page already has and the read model does not carry.
    screen = view_service.run_screen(writing.paths, run=run_id, clock=writing.clock)
    (task,) = screen.outstanding
    assert task.content_id == content_id and task.state == "awaiting-judge"
    assert task.answer_with == "text"
    assert task.submission is not None and task.submission.kind == "text"
    assert task.submission.response_digest == digest(ANSWER)
    assert ANSWER[:30] not in screen.model_dump_json()

    waiting = recording_service.pending(writing.paths, run=run_id, clock=writing.clock)
    (entry,) = waiting.pending
    assert (entry.kind, entry.judgeable, entry.response_text) == ("text", True, ANSWER)
    assert entry.audio_path is None and entry.sha256 is None

    report = claim(writing, run_id)
    (claimed,) = report.claimed
    assert (claimed.kind, claimed.response_text) == ("text", ANSWER)
    applied = verdict(writing, run_id, content_id, submission_id, score=0.6, claim=claimed.claim_id)

    assert applied.verdict_status == "applied" and applied.tasks_recorded == 1
    result = rows(
        writing,
        "SELECT response_visibility, response_excerpt, response_hash, audio_artifact_id, "
        "observed_at = (SELECT created_at FROM assessment_submissions WHERE submission_id = ?) "
        "FROM assessment_results WHERE run_id = ?",
        [submission_id, run_id],
    )
    assert result == [("full", ANSWER, digest(ANSWER), None, True)]
    assert rows(
        writing,
        "SELECT response_visibility, response_excerpt, response_hash FROM assessment_verdicts "
        "WHERE submission_id = ?",
        [submission_id],
    ) == [("withheld", None, digest(ANSWER))]
    assert submission_state(writing, submission_id) == ("judged", None)
    assert_clean(writing)


def test_a_resent_answer_replays_and_a_reused_key_or_a_second_answer_is_refused(
    writing: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id = written(writing)

    again = hand_in(writing, run_id, content_id)
    assert again.replayed and again.submission.submission_id == submission_id

    conflict = refusal(hand_in, writing, run_id, content_id, response=ANSWER + " Dziękuję.")
    assert conflict.payload.code == "idempotency_conflict"
    assert digest(ANSWER) in conflict.payload.message
    assert conflict.payload.details[0].context["response_digest"] == digest(ANSWER)

    second = refusal(hand_in, writing, run_id, content_id, key="answer-2", response="Inna.")
    assert second.payload.code == "assessment_task_already_submitted"
    assert second.payload.details[0].context["submission_id"] == submission_id

    assert rows(
        writing, "SELECT count(*) FROM assessment_submissions WHERE run_id = ?", [run_id]
    ) == [(1,)]
    # One event, bound to the key, carrying the digest and never the words.
    events = rows(
        writing,
        "SELECT payload_json FROM domain_events WHERE idempotency_key = 'answer-1'",
    )
    assert len(events) == 1 and ANSWER[:30] not in str(events[0][0])
    assert_clean(writing)


def test_a_key_another_operation_used_or_a_recording_holds_is_refused(
    writing: PolishWorkspace,
) -> None:
    run_id = start_written(writing).run_id
    task = assessment_service.next_task(
        writing.paths, run=run_id, clock=writing.clock, idempotency_key="serve-1"
    )
    assert isinstance(task, assessment_service.NextTaskReport)

    reused = refusal(hand_in, writing, run_id, task.content_id, key="serve-1")

    assert reused.payload.code == "idempotency_conflict"
    assert rows(writing, "SELECT count(*) FROM assessment_submissions") == [(0,)]


def test_a_capture_id_a_written_answer_holds_is_refused_before_anything_is_staged(
    writing: PolishWorkspace,
) -> None:
    """The mirror of the test above: a capture ID and a submission key are one column, so
    a recording under a written answer's key is refused by name, with no bytes staged."""

    from tests.integration.test_client_audio import permit_recording

    shared = "7f9c2b1e-4a3d-4e8f-9b6a-2c5d1e0f3a4b"  # a valid capture ID, a key first
    written_run, _, submission_id = written(writing, key=shared)
    assessment_service.set_status(
        writing.paths, status="abandoned", run=written_run, clock=writing.clock
    )
    permit_recording(writing)
    run_id = start_spoken(writing, ("pronunciation",))
    task = serve(writing, run_id)

    failure = refusal(
        take,
        writing,
        run_id,
        task.content_id,
        data=spoken_bytes(611),
        identifier=shared,
    )

    assert failure.payload.code == "capture_conflict"
    assert failure.payload.details[0].context["submission_id"] == submission_id
    assert rows(writing, "SELECT count(*) FROM capture_stagings") == [(0,)]
    assert rows(writing, "SELECT count(*) FROM artifacts WHERE kind = 'audio'") == [(0,)]
    staged = writing.root / recording_service.CAPTURE_STAGING
    assert not staged.exists() or not any(staged.iterdir())


@pytest.mark.parametrize("consent", [None, False], ids=["excerpt-only", "declined"])
def test_a_track_that_does_not_keep_writing_whole_is_never_served_it_and_cannot_hand_it_in(
    polish_workspace: PolishWorkspace, consent: bool | None
) -> None:
    from linguawiki.placement import WRITING_NOT_RETAINED

    if consent is None:
        # The workspace fixture consents, and a preference cannot be unset through
        # `track update`; an excerpt-only track is one that never said yes.
        unset_transcript_consent(polish_workspace)
    else:
        keep_writing(polish_workspace, consent)

    judged_run = start_written(polish_workspace)
    (dimension,) = judged_run.dimensions
    assert dimension.status == "not-tested"
    assert dimension.unavailable_reason == WRITING_NOT_RETAINED
    assert any(WRITING_NOT_RETAINED in warning for warning in judged_run.warnings)

    # Under `any` the task is served for a judge to record directly, and handing it in for
    # later judging is still refused: the boundary is the same whichever run asks.
    run_id = start_written(polish_workspace, scoring="any").run_id
    task = serve(polish_workspace, run_id)
    refused = refusal(hand_in, polish_workspace, run_id, task.content_id)

    assert refused.payload.code == "transcript_consent_required"
    assert text_anywhere(polish_workspace, ANSWER) == []
    assert_clean(polish_workspace)


def test_machine_recorded_keeps_its_meaning_and_does_not_serve_writing(
    writing: PolishWorkspace,
) -> None:
    from linguawiki.placement import NO_MACHINE_SCORABLE_TASK

    report = start_written(writing, scoring="machine+recorded")

    (dimension,) = report.dimensions
    assert (dimension.status, dimension.unavailable_reason) == (
        "not-tested",
        NO_MACHINE_SCORABLE_TASK,
    )


def test_machine_judged_still_serves_what_machine_recorded_serves(
    speaking: PolishWorkspace,
) -> None:
    report = assessment_service.start(
        speaking.paths,
        dimensions=["pronunciation", "reading", "writing"],
        modalities=["text", "audio", "speech", "writing"],
        scoring="machine+judged",
        clock=speaking.clock,
    )

    assert {entry.dimension: entry.status for entry in report.dimensions} == {
        "pronunciation": "open",
        "reading": "open",
        "writing": "open",
    }


def test_the_eligibility_predicate_is_the_retention_rule() -> None:
    """`written_judging_permitted` and `retain_response` must agree for every consent value:
    the predicate stands for "retention keeps the answer whole", and drifting from it would
    serve writing that is then judged on a fragment."""

    from linguawiki.placement import written_judging_permitted
    from linguawiki.services import evidence as evidence_service

    for consent in (True, False, None, "yes", 1):
        preferences = {} if consent is None else {"transcript_retention_consent": consent}
        try:
            visibility = evidence_service.retain_response(
                ANSWER, requested="full", preferences=preferences
            )[0]
        except LinguaWikiError:
            visibility = "refused"
        assert written_judging_permitted(preferences) == (visibility == "full"), consent


def test_withdrawing_transcript_consent_after_handing_in_leaves_no_text_anywhere(
    writing: PolishWorkspace,
) -> None:
    """End to end through `submit`: the answer, a claim, a release, then consent goes."""

    run_id, content_id, submission_id = written(writing)
    claim_id = claimed_one(writing, run_id)
    release(writing, claim_id, reason="the judge restarted")

    updated = learner_service.update_track(
        writing.paths,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=writing.clock,
    )

    stored = rows(
        writing,
        "SELECT status, withdrawn_code, response_text, response_digest "
        "FROM assessment_submissions WHERE submission_id = ?",
        [submission_id],
    )
    assert stored == [("withdrawn", withdrawal.NOT_RETAINED_CODE, None, digest(ANSWER))]
    assert any(submission_id in warning for warning in updated.warnings)
    assert text_anywhere(writing, ANSWER) == []
    late = refusal(verdict, writing, run_id, content_id, submission_id, claim=None)
    assert late.payload.code == "assessment_submission_withdrawn"
    assert late.payload.details[0].context["code"] == withdrawal.NOT_RETAINED_CODE
    assert rows(writing, "SELECT count(*) FROM assessment_verdicts") == [(0,)]
    assert_clean(writing)


def test_a_verdict_that_finds_eligibility_gone_withdraws_the_answer_and_clears_its_text(
    writing: PolishWorkspace,
) -> None:
    """Consent is revalidated where the verdict lands, not where it was claimed. The
    preference is changed beneath `update_track`, as a restore or hand edit could: no
    consent hook ran, so only the verdict's own check stands between it and a fragment."""

    run_id, content_id, submission_id = written(writing)
    claim_id = claimed_one(writing, run_id)
    unset_transcript_consent(writing)
    assert "pending_written_answers_judgeable" in failed_checks(writing)

    refused = refusal(verdict, writing, run_id, content_id, submission_id, claim=claim_id)

    assert refused.payload.code == withdrawal.NOT_RETAINED_CODE
    assert submission_state(writing, submission_id) == ("withdrawn", withdrawal.NOT_RETAINED_CODE)
    assert text_anywhere(writing, ANSWER) == []
    assert rows(writing, "SELECT count(*) FROM assessment_verdicts") == [(0,)]
    assert_clean(writing)


def test_a_claim_withdraws_a_written_answer_whose_text_was_altered(
    writing: PolishWorkspace,
) -> None:
    run_id, _, submission_id = written(writing)
    with (
        open_writer(writing.paths, command="test.tamper") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_submissions SET response_text = 'Coś innego.', updated_at = now() "
            "WHERE submission_id = ?",
            [submission_id],
        )
    waiting = recording_service.pending(writing.paths, run=run_id, clock=writing.clock)
    assert waiting.pending[0].problem_code == "assessment_response_altered"
    assert waiting.pending[0].response_text is None

    report = claim(writing, run_id)

    assert report.claimed == ()
    assert [entry.code for entry in report.withdrawn] == ["assessment_response_altered"]
    assert submission_state(writing, submission_id) == (
        "withdrawn",
        "assessment_response_altered",
    )
    assert_clean(writing)


def test_a_held_verdict_on_a_written_answer_is_retained_again_when_the_run_resumes(
    writing: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id = written(writing)
    pause(writing, run_id)

    held = verdict(writing, run_id, content_id, submission_id, score=0.4)
    assert held.held and held.verdict_status == "held"
    # R10: the verdict keeps none of the words; the result takes them from the submission.
    assert rows(
        writing,
        "SELECT response_visibility, response_excerpt, response_hash FROM assessment_verdicts "
        "WHERE submission_id = ?",
        [submission_id],
    ) == [("withheld", None, digest(ANSWER))]

    resumed = resume(writing, run_id)

    assert [entry.submission_id for entry in resumed.applied_verdicts] == [submission_id]
    assert rows(
        writing,
        "SELECT response_visibility, response_excerpt, raw_score FROM assessment_results "
        "WHERE run_id = ?",
        [run_id],
    ) == [("full", ANSWER, 0.4)]
    assert_clean(writing)


def test_a_verdict_on_a_written_answer_must_name_it_and_bring_nothing_of_its_own(
    writing: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id = written(writing)

    unnamed = refusal(verdict, writing, run_id, content_id, None)
    assert unnamed.payload.code == "assessment_submission_required"
    assert submission_id in unnamed.payload.message
    other_words = refusal(
        assessment_service.record,
        writing.paths,
        run=run_id,
        content_id=content_id,
        score=0.5,
        submission=submission_id,
        response="Zupełnie inna odpowiedź.",
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        clock=writing.clock,
    )
    assert other_words.payload.code == "assessment_response_not_submitted"
    no_score = refusal(
        assessment_service.record,
        writing.paths,
        run=run_id,
        content_id=content_id,
        submission=submission_id,
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        clock=writing.clock,
    )
    assert no_score.payload.code == "assessment_score_required"
    deterministic = refusal(
        assessment_service.record,
        writing.paths,
        run=run_id,
        content_id=content_id,
        score=0.5,
        submission=submission_id,
        clock=writing.clock,
    )
    assert deterministic.payload.code == "assessment_judge_required"
    assert submission_state(writing, submission_id) == ("pending", None)
    assert rows(writing, "SELECT count(*) FROM assessment_verdicts") == [(0,)]


def test_a_judged_written_task_in_a_judged_run_takes_no_verdict_without_its_answer(
    writing: PolishWorkspace,
) -> None:
    run_id = start_written(writing).run_id
    task = serve(writing, run_id)

    refused = refusal(verdict, writing, run_id, task.content_id, None)

    assert refused.payload.code == "assessment_submission_required"
    assert results(writing, run_id) == 0


def test_a_written_answer_is_refused_for_a_task_that_is_not_written_or_not_served(
    speaking: PolishWorkspace,
) -> None:
    keep_writing(speaking)
    run_id, content_id, _, _ = submitted(speaking, 501)

    spoken = refusal(hand_in, speaking, run_id, content_id)
    assert spoken.payload.code == "assessment_task_already_submitted"
    other_run = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, other_run)
    assert refusal(hand_in, speaking, other_run, task.content_id, key="k2").payload.code == (
        "assessment_task_not_written"
    )
    blank = refusal(hand_in, speaking, other_run, task.content_id, key="k3", response="  ")
    assert blank.payload.code == "invalid_arguments"
    unserved = refusal(hand_in, speaking, other_run, "cnt_" + "0" * 26, key="k4")
    assert unserved.payload.code == "assessment_task_not_served"


def test_handing_in_settles_what_lapsed_first_and_says_so(writing: PolishWorkspace) -> None:
    run_id, content_id, submission_id = written(writing)
    for _ in range(judging.JUDGING_POLICY.max_attempts):
        claimed_one(writing, run_id)
        lapse(writing)

    # The sweep runs before the submit's own work: the exhausted answer is withdrawn, its
    # task skipped, and the refusal that follows -- the task no longer takes an answer --
    # names the withdrawal, because it committed whatever the command then decided.
    refused = refusal(hand_in, writing, run_id, content_id, key="answer-2", response="Inna.")

    assert refused.payload.code == "assessment_task_settled"
    settled = [detail for detail in refused.payload.details if detail.field == "settled"]
    assert [detail.context["submission_id"] for detail in settled] == [submission_id]
    assert submission_state(writing, submission_id) == ("withdrawn", judging.EXHAUSTED_CODE)
    assert any(
        command == "assessment.submit" for command, _ in audits_naming(writing, submission_id)
    )
    # A retry of the original answer replays it, as it now stands.
    again = hand_in(writing, run_id, content_id)
    assert again.replayed and again.submission.status == "withdrawn"
    assert_clean(writing)


def test_the_cli_hands_in_a_written_answer_from_a_file(
    writing: PolishWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Any
) -> None:
    run_id = start_written(writing).run_id
    task = serve(writing, run_id)
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text(ANSWER, encoding="utf-8")

    arguments = (
        "assessment",
        "submit",
        "--run",
        run_id,
        "--content-id",
        task.content_id,
        "--submission-key",
        "cli-answer",
        "--response-file",
        str(answer_file),
    )
    code, handed_in = _cli(writing, capsys, *arguments)
    assert code == 0, handed_in
    assert handed_in["data"]["submission"]["kind"] == "text"
    assert ANSWER[:30] not in json.dumps(handed_in)
    code, again = _cli(writing, capsys, *arguments)
    assert code == 0 and again["data"]["replayed"] is True

    code, waiting = _cli(writing, capsys, "assessment", "pending", "--run", run_id)
    assert code == 0
    assert waiting["data"]["pending"][0]["response_text"] == ANSWER


# --- fix round 1: R10 and minors ---------------------------------------------------------------


def test_a_verdict_on_a_written_answer_keeps_none_of_its_words_after_consent_goes(
    writing: PolishWorkspace,
) -> None:
    """R10: the applied verdict names the submission and its digest; withdrawing transcript
    consent afterwards leaves no copy of the answer in a verdict row. The result's copy is
    the deferred C1 gap, and is not asserted on here."""

    run_id, content_id, submission_id = written(writing)
    verdict(writing, run_id, content_id, submission_id, key="judged-1")
    keep_writing(writing, False)

    assert "assessment_verdicts" not in text_anywhere(writing, ANSWER)
    assert rows(
        writing,
        "SELECT response_hash FROM assessment_verdicts WHERE submission_id = ?",
        [submission_id],
    ) == [(digest(ANSWER),)]
    assert_clean(writing)


def test_a_held_verdict_whose_consent_went_while_paused_leaves_no_text_and_is_voided(
    writing: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id = written(writing)
    pause(writing, run_id)
    held = verdict(writing, run_id, content_id, submission_id, score=0.4)
    assert held.held and "assessment_verdicts" not in text_anywhere(writing, ANSWER)

    keep_writing(writing, False)
    resumed = resume(writing, run_id)

    # Task 4: the consent change withdrew the answer and voided what was held for it, so
    # the resume has nothing to apply.
    assert resumed.applied_verdicts == ()
    assert outcome_of(writing, held.verdict_id) == ("void", withdrawal.NOT_RETAINED_CODE)
    assert submission_state(writing, submission_id) == (
        "withdrawn",
        withdrawal.NOT_RETAINED_CODE,
    )
    assert text_anywhere(writing, ANSWER) == []
    assert results(writing, run_id) == 0
    assert_clean(writing)


@pytest.mark.parametrize("paused", [False, True], ids=["applied", "held"])
@pytest.mark.parametrize(
    ("requested", "kept"),
    [("excerpt", ("excerpt", ANSWER[:240])), ("withheld", ("withheld", None))],
)
def test_a_judges_narrower_request_is_recorded_beside_the_verdict_and_honoured(
    writing: PolishWorkspace, paused: bool, requested: str, kept: tuple[str, str | None]
) -> None:
    """R12: the verdict row keeps nothing and records the request in its own column, so a
    request survives a hold and is honoured at resume exactly as when applied at once."""

    run_id, content_id, submission_id = written(writing)
    if paused:
        pause(writing, run_id)

    assessment_service.record(
        writing.paths,
        run=run_id,
        content_id=content_id,
        score=0.5,
        submission=submission_id,
        response_visibility=requested,
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        clock=writing.clock,
    )
    if paused:
        resume(writing, run_id)

    assert rows(
        writing,
        "SELECT response_visibility, response_excerpt, response_hash, requested_visibility "
        "FROM assessment_verdicts WHERE submission_id = ?",
        [submission_id],
    ) == [("withheld", None, digest(ANSWER), requested)]
    assert rows(
        writing,
        "SELECT response_visibility, response_excerpt, response_hash FROM assessment_results "
        "WHERE run_id = ?",
        [run_id],
    ) == [(*kept, digest(ANSWER))]
    assert_clean(writing)


def test_a_second_keyed_verdict_asking_for_a_different_visibility_is_a_conflict(
    writing: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id = written(writing)
    verdict(writing, run_id, content_id, submission_id, score=0.5, key="v-1")

    conflict = refusal(
        assessment_service.record,
        writing.paths,
        run=run_id,
        content_id=content_id,
        score=0.5,
        submission=submission_id,
        response_visibility="withheld",
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        rubric={"accuracy": 0.5},
        idempotency_key="v-2",
        clock=writing.clock,
    )

    assert conflict.payload.code == "assessment_verdict_conflict"


def test_a_held_full_request_is_narrowed_at_resume_by_the_preferences_in_force_then(
    speaking: PolishWorkspace,
) -> None:
    """The "retained against current preferences" apply path, with preferences *changed*
    rather than withdrawn. For a written answer every change that keeps less than the whole
    makes it ineligible -- `assert_text_judgeable` settles it, as the eligibility test shows
    -- so the narrowing is exercised on a recording's verdict, which the same held branch
    applies. Transcript consent goes from yes to unset, which `track update` cannot express,
    so the row is removed directly."""

    transcript = " ".join(["Synthetic transcript of a spoken answer."] * 10)
    run_id, content_id, submission_id, _ = submitted(speaking, 601)
    pause(speaking, run_id)
    assessment_service.record(
        speaking.paths,
        run=run_id,
        content_id=content_id,
        score=0.5,
        submission=submission_id,
        response=transcript,
        response_visibility="full",
        assessor_kind="ai",
        assessor="synthetic-ai-judge",
        rubric={"accuracy": 0.5},
        clock=speaking.clock,
    )
    assert rows(
        speaking,
        "SELECT response_visibility, requested_visibility FROM assessment_verdicts "
        "WHERE submission_id = ?",
        [submission_id],
    ) == [("full", "full")]
    unset_transcript_consent(speaking)

    resumed = resume(speaking, run_id)

    assert [entry.submission_id for entry in resumed.applied_verdicts] == [submission_id]
    assert rows(
        speaking,
        "SELECT response_visibility, response_excerpt FROM assessment_results WHERE run_id = ?",
        [run_id],
    ) == [("excerpt", transcript[:240])]


def test_an_over_long_answer_or_key_is_refused_by_name_before_anything_is_written(
    writing: PolishWorkspace,
) -> None:
    from linguawiki.services import written_answers

    run_id = start_written(writing).run_id
    task = serve(writing, run_id)
    limit = written_answers.MAXIMUM_WRITTEN_ANSWER_CHARACTERS
    key_limit = written_answers.MAXIMUM_SUBMISSION_KEY_CHARACTERS

    too_long = refusal(hand_in, writing, run_id, task.content_id, response="a" * (limit + 1))
    long_key = refusal(hand_in, writing, run_id, task.content_id, key="k" * (key_limit + 1))

    assert too_long.payload.code == "assessment_response_too_long"
    assert long_key.payload.code == "invalid_arguments"
    assert long_key.payload.details[0].field == "submission_key"
    assert rows(writing, "SELECT count(*) FROM assessment_submissions") == [(0,)]
    at_the_limit = hand_in(
        writing, run_id, task.content_id, key="k" * key_limit, response="a" * limit
    )
    assert at_the_limit.submission.status == "pending"


def test_a_written_answer_stranded_on_a_closed_run_is_given_a_remedy_that_works(
    writing: PolishWorkspace,
) -> None:
    run_id, _, submission_id = written(writing)
    with (
        open_writer(writing.paths, command="test.tamper") as database,
        database.transaction() as transaction,
    ):
        # A run closed without settling what it owed, as a pre-C6 close or a hand repair
        # leaves it.
        transaction.execute(
            "UPDATE assessment_runs SET status = 'abandoned' WHERE run_id = ?", [run_id]
        )

    check = failed_checks(writing)["no_pending_submission_on_a_closed_run"]
    assert submission_id in check.context["submissions"]
    assert "artifact purge" not in check.message
    assert "transcript_retention_consent" in check.message

    keep_writing(writing, False)

    assert submission_state(writing, submission_id) == (
        "withdrawn",
        withdrawal.NOT_RETAINED_CODE,
    )
    assert "no_pending_submission_on_a_closed_run" not in failed_checks(writing)


# --- batching --------------------------------------------------------------------------------
#
# A batch serves one task in each free open dimension in one transaction. The answers below
# are scores chosen by a fixed rule of the task's stable key, so two runs answering the same
# tasks fold in the same evidence -- which is what lets a batched run be compared with one
# served a task at a time.

MACHINE_DIMENSIONS = ("grammar-control", "reading", "vocabulary-control")


def batch(workspace: PolishWorkspace, run_id: str, key: str) -> assessment_service.BatchReport:
    return assessment_service.next_batch(
        workspace.paths, run=run_id, idempotency_key=key, clock=workspace.clock, actor="client"
    )


def fixed_score(stable_key: str) -> float:
    """A score that depends only on which task was asked, so both runs earn the same."""

    return 1.0 if int(digest(stable_key)[:2], 16) % 3 else 0.0


def answer(workspace: PolishWorkspace, run_id: str, task: Any) -> None:
    assessment_service.record(
        workspace.paths,
        run=run_id,
        content_id=task.content_id,
        score=fixed_score(task.stable_key),
        clock=workspace.clock,
    )


def start_machine(workspace: PolishWorkspace, dimensions: tuple[str, ...]) -> str:
    return assessment_service.start(
        workspace.paths, dimensions=list(dimensions), clock=workspace.clock
    ).run_id


def batched_sequence(workspace: PolishWorkspace, run_id: str) -> dict[str, list[str]]:
    """Serve batches until none serves anything, answering each task; per dimension."""

    served: dict[str, list[str]] = {}
    for round_number in range(1, 100):
        report = batch(workspace, run_id, f"round-{round_number}")
        if not report.tasks:
            return served
        assert not report.outstanding
        for entry in report.tasks:
            served.setdefault(entry.dimension, []).append(entry.task.stable_key)
            answer(workspace, run_id, entry.task)
    raise AssertionError("a bounded run never stopped serving")


def sequential_sequence(workspace: PolishWorkspace, run_id: str) -> dict[str, list[str]]:
    """Serve one task at a time until the run reports itself, answering each; per dimension."""

    served: dict[str, list[str]] = {}
    for _ in range(1, 300):
        outcome = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
        if not isinstance(outcome, assessment_service.NextTaskReport):
            return served
        served.setdefault(outcome.dimension, []).append(outcome.stable_key)
        answer(workspace, run_id, outcome)
    raise AssertionError("a bounded run never stopped serving")


@pytest.fixture
def twin(pilot_template: PilotTemplate, tmp_path: Path) -> PolishWorkspace:
    """A second workspace identical to `polish_workspace`, to serve the other way."""

    target = tmp_path / "twin" / "PolishLinguaWiki"
    clock = AdvancingClock()
    materialize_pilot(
        pilot_template, target=target, backup_root=tmp_path / "twin-backups", clock=clock
    )
    return polish_learner(
        SyntheticWorkspace(
            paths=workspace_paths(target),
            backup_root=tmp_path / "twin-backups",
            clock=clock,
            report=pilot_template.report,
        )
    )


def run_estimates(workspace: PolishWorkspace, run_id: str) -> list[tuple[Any, ...]]:
    return rows(
        workspace,
        "SELECT dimension, status, tasks_used, posterior_json, stop_reason "
        "FROM placement_dimension_state WHERE run_id = ? ORDER BY dimension",
        [run_id],
    )


def test_a_batched_run_serves_what_one_at_a_time_serves(
    polish_workspace: PolishWorkspace, twin: PolishWorkspace
) -> None:
    batched_run = start_machine(polish_workspace, MACHINE_DIMENSIONS)
    sequential_run = start_machine(twin, MACHINE_DIMENSIONS)

    batched = batched_sequence(polish_workspace, batched_run)
    sequential = sequential_sequence(twin, sequential_run)

    assert set(batched) == set(MACHINE_DIMENSIONS)
    assert batched == sequential
    # The same evidence, folded in the same order within each dimension, is the same
    # posterior: batching changed when tasks were shown, not what they were.
    assert run_estimates(polish_workspace, batched_run) == run_estimates(twin, sequential_run)
    assert_clean(polish_workspace)


def test_an_asynchronously_judged_run_estimates_what_a_synchronous_one_does(
    polish_workspace: PolishWorkspace, twin: PolishWorkspace
) -> None:
    """The plan's "Done when": judging elsewhere and later changes when a verdict lands,
    not what it is evidence of. One run is judged through the whole C6 lifecycle -- claimed,
    one verdict applied as it arrives, the run paused, the other verdict held and applied
    at resume -- and its twin records the same answers synchronously with `record`. The
    posteriors, and so the next task each run serves, are the same."""

    from tests.integration.test_client_audio import judge, permit_recording

    def answered(workspace: PolishWorkspace) -> tuple[str, list[tuple[Any, str]]]:
        permit_recording(workspace)
        run_id = start_spoken(workspace)
        taken = []
        for seed in (501, 502):
            task = serve(workspace, run_id)
            capture = take(workspace, run_id, task.content_id, data=spoken_bytes(seed))
            assert capture.artifact_id is not None
            taken.append((task, capture.artifact_id))
        return run_id, taken

    asynchronous, async_tasks = answered(polish_workspace)
    synchronous, sync_tasks = answered(twin)
    assert [task.stable_key for task, _ in async_tasks] == [
        task.stable_key for task, _ in sync_tasks
    ]
    assert len({task.dimension for task, _ in async_tasks}) == 2

    claims = {
        entry.submission.submission_id: entry.claim_id
        for entry in claim(polish_workspace, asynchronous).claimed
    }
    submissions = {
        str(content_id): str(submission_id)
        for content_id, submission_id in rows(
            polish_workspace,
            "SELECT content_id, submission_id FROM assessment_submissions WHERE run_id = ?",
            [asynchronous],
        )
    }
    (first, _), (second, _) = async_tasks
    immediate = verdict(
        polish_workspace,
        asynchronous,
        first.content_id,
        submissions[first.content_id],
        score=fixed_score(first.stable_key),
        claim=claims[submissions[first.content_id]],
    )
    assert not immediate.held
    pause(polish_workspace, asynchronous)
    held = verdict(
        polish_workspace,
        asynchronous,
        second.content_id,
        submissions[second.content_id],
        score=fixed_score(second.stable_key),
        claim=claims[submissions[second.content_id]],
    )
    assert held.held
    resumed = resume(polish_workspace, asynchronous)
    assert [entry.verdict_id for entry in resumed.applied_verdicts] == [held.verdict_id]

    for task, artifact_id in sync_tasks:
        judge(
            twin,
            synchronous,
            task.content_id,
            artifact_id,
            score=fixed_score(task.stable_key),
        )

    estimated = run_estimates(polish_workspace, asynchronous)
    # Both answers folded in, one per dimension -- not two runs that agree on nothing.
    assert sum(int(row[2]) for row in estimated) == 2
    assert estimated == run_estimates(twin, synchronous)
    assert serve(polish_workspace, asynchronous).stable_key == serve(twin, synchronous).stable_key
    assert_clean(polish_workspace)
    assert_clean(twin)


def test_two_dimensions_drawing_on_one_content_family_serve_what_the_sequential_path_would(
    polish_workspace: PolishWorkspace, twin: PolishWorkspace
) -> None:
    # Families are shared across dimensions in the pilot bank, and content-family diversity
    # is a duty *within* a dimension (`select_task` reads the dimension's own families). The
    # batch re-reads every exclusion after each dimension's write, so a round whose best
    # candidates share a family must serve exactly what serving them one at a time would --
    # neither dropping the second for the first one's family nor letting one dimension
    # repeat its own.
    batched_run = start_machine(polish_workspace, MACHINE_DIMENSIONS)
    sequential_run = start_machine(twin, MACHINE_DIMENSIONS)

    shared_rounds = []
    batched: dict[str, list[str]] = {}
    for round_number in range(1, 100):
        report = batch(polish_workspace, batched_run, f"round-{round_number}")
        if not report.tasks:
            break
        families = [entry.task.content_family for entry in report.tasks]
        if len(set(families)) < len(families):
            shared_rounds.append(round_number)
        for entry in report.tasks:
            batched.setdefault(entry.dimension, []).append(entry.task.stable_key)
            answer(polish_workspace, batched_run, entry.task)

    assert shared_rounds, "no round put two dimensions on one family; the test proves nothing"
    assert batched == sequential_sequence(twin, sequential_run)
    for dimension in MACHINE_DIMENSIONS:
        first_two = rows(
            polish_workspace,
            "SELECT content_family FROM assessment_run_tasks "
            "WHERE run_id = ? AND dimension = ? ORDER BY sequence LIMIT 2",
            [batched_run, dimension],
        )
        assert first_two[0] != first_two[1], dimension


def test_a_batch_serves_only_free_dimensions_and_reports_the_rest_outstanding(
    writing: PolishWorkspace,
) -> None:
    run_id = assessment_service.start(
        writing.paths,
        dimensions=["reading", "writing"],
        scoring="machine+judged",
        clock=writing.clock,
    ).run_id
    first = batch(writing, run_id, "round-1")
    by_dimension = {entry.dimension: entry for entry in first.tasks}
    assert sorted(by_dimension) == ["reading", "writing"]
    assert [entry.position for entry in first.tasks] == [1, 2]
    hand_in(writing, run_id, by_dimension["writing"].content_id)
    answer(writing, run_id, by_dimension["reading"].task)

    second = batch(writing, run_id, "round-2")

    assert [entry.dimension for entry in second.tasks] == ["reading"]
    assert second.outstanding == ("writing",) and second.exhausted == ()
    assert second.batch_id != first.batch_id


def test_a_refusal_in_any_dimension_writes_nothing_for_the_batch_and_names_it(
    polish_workspace: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_machine(polish_workspace, MACHINE_DIMENSIONS)
    planned: list[str] = []
    real = assessment_service.plan_serve

    def refusing(database: Any, context: Any, state: Any, *, clock: Any) -> Any:
        planned.append(state.dimension)
        if state.dimension == "vocabulary-control":
            raise LinguaWikiError("assessment_asset_unavailable", "the recording is gone")
        return real(database, context, state, clock=clock)

    monkeypatch.setattr(assessment_service, "plan_serve", refusing)
    failure = refusal(batch, polish_workspace, run_id, "round-1")

    # The run's recorded order, and the refused dimension planned after two that wrote.
    assert planned == list(MACHINE_DIMENSIONS)
    assert failure.payload.code == "assessment_asset_unavailable"
    assert "vocabulary-control" in failure.payload.message
    assert failure.payload.details[0].context == {"dimension": "vocabulary-control"}
    for table in (
        "assessment_run_tasks",
        "assessment_item_exposures",
        "assessment_batches",
        "assessment_batch_tasks",
    ):
        assert rows(polish_workspace, f"SELECT count(*) FROM {table}") == [(0,)], table

    # A refusal leaves its key unspent: the retry the caller is entitled to make serves.
    monkeypatch.setattr(assessment_service, "plan_serve", real)
    retried = batch(polish_workspace, run_id, "round-1")
    assert [entry.dimension for entry in retried.tasks] == list(MACHINE_DIMENSIONS)
    assert_clean(polish_workspace)


def test_a_dimension_the_bank_cannot_serve_is_closed_and_reported_exhausted(
    polish_workspace: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_machine(polish_workspace, MACHINE_DIMENSIONS)
    real = assessment_service.plan_serve

    def empty_reading(database: Any, context: Any, state: Any, *, clock: Any) -> Any:
        return None if state.dimension == "reading" else real(database, context, state, clock=clock)

    monkeypatch.setattr(assessment_service, "plan_serve", empty_reading)
    report = batch(polish_workspace, run_id, "round-1")

    assert [entry.dimension for entry in report.tasks] == ["grammar-control", "vocabulary-control"]
    assert report.exhausted == ("reading",)
    assert any("reading stopped early" in warning for warning in report.warnings)
    assert rows(
        polish_workspace,
        "SELECT status, stop_reason FROM placement_dimension_state "
        "WHERE run_id = ? AND dimension = 'reading'",
        [run_id],
    ) == [("not-tested", assessment_service.EXHAUSTED_REASON)]


def test_a_lost_batch_is_replayed_with_each_task_as_it_stands_and_serves_nothing(
    writing: PolishWorkspace,
) -> None:
    run_id = assessment_service.start(
        writing.paths,
        dimensions=["grammar-control", "reading", "writing"],
        scoring="machine+judged",
        clock=writing.clock,
    ).run_id
    first = batch(writing, run_id, "round-1")
    by_dimension = {entry.dimension: entry for entry in first.tasks}
    membership = [(entry.position, entry.dimension, entry.content_id) for entry in first.tasks]
    assert [entry.state for entry in first.tasks] == ["served"] * 3

    answer(writing, run_id, by_dimension["reading"].task)
    submission = hand_in(writing, run_id, by_dimension["writing"].content_id).submission
    waiting = batch(writing, run_id, "round-1")

    assert waiting.replayed and waiting.batch_id == first.batch_id
    assert [(entry.position, entry.dimension, entry.content_id) for entry in waiting.tasks] == (
        membership
    )
    assert {entry.dimension: entry.state for entry in waiting.tasks} == {
        "grammar-control": "served",
        "reading": "answered",
        "writing": "awaiting-judge",
    }

    verdict(
        writing,
        run_id,
        by_dimension["writing"].content_id,
        submission.submission_id,
        score=0.6,
    )
    judged = batch(writing, run_id, "round-1")

    assert [(entry.position, entry.dimension, entry.content_id) for entry in judged.tasks] == (
        membership
    )
    assert {entry.dimension: entry.state for entry in judged.tasks} == {
        "grammar-control": "served",
        "reading": "answered",
        "writing": "answered",
    }
    # Reading opened again when it was answered; the replay does not add it. That is a new
    # batch, under a new key.
    assert rows(
        writing, "SELECT count(*) FROM assessment_run_tasks WHERE run_id = ?", [run_id]
    ) == [(3,)]
    assert rows(writing, "SELECT count(*) FROM assessment_batches") == [(1,)]
    # The replayed task is the one served, from the record of its serving.
    assert judged.tasks[0].task.prompt == first.tasks[0].task.prompt
    assert judged.tasks[0].task.served_again
    assert_clean(writing)


def test_a_batch_key_is_required_and_bound_to_its_request_and_operation(
    polish_workspace: PolishWorkspace,
) -> None:
    run_id = start_machine(polish_workspace, MACHINE_DIMENSIONS)
    other_run = start_machine(polish_workspace, ("reading",))
    batch(polish_workspace, run_id, "round-1")

    missing = refusal(
        assessment_service.next_batch,
        polish_workspace.paths,
        run=run_id,
        idempotency_key=None,
        clock=polish_workspace.clock,
    )
    elsewhere = refusal(batch, polish_workspace, other_run, "round-1")
    single = refusal(
        assessment_service.next_task,
        polish_workspace.paths,
        run=run_id,
        idempotency_key="round-1",
        clock=polish_workspace.clock,
    )
    assessment_service.next_task(
        polish_workspace.paths,
        run=other_run,
        idempotency_key="single",
        clock=polish_workspace.clock,
    )
    taken = refusal(batch, polish_workspace, other_run, "single")

    assert missing.payload.code == "invalid_arguments"
    assert missing.payload.details[0].field == "idempotency_key"
    assert elsewhere.payload.code == "idempotency_conflict"
    assert single.payload.code == "idempotency_conflict"
    assert taken.payload.code == "idempotency_conflict"
    assert rows(
        polish_workspace, "SELECT count(*) FROM assessment_run_tasks WHERE run_id = ?", [other_run]
    ) == [(1,)]


def test_a_batch_settles_what_lapsed_first_and_says_so(speaking: PolishWorkspace) -> None:
    run_id, content_id, submission_id, _ = submitted(speaking, 230)
    for _ in range(judging.JUDGING_POLICY.max_attempts):
        claimed_one(speaking, run_id)
        lapse(speaking)

    report = batch(speaking, run_id, "round-1")

    assert submission_state(speaking, submission_id)[0] == "withdrawn"
    assert any(submission_id in warning or content_id in warning for warning in report.warnings)
    assert [entry.dimension for entry in report.tasks] == ["pronunciation"]


def test_the_cli_and_the_route_serve_a_batch(
    polish_workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    from linguawiki.client import routes

    run_id = start_machine(polish_workspace, ("grammar-control", "reading"))
    code, payload = _cli(
        polish_workspace,
        capsys,
        "assessment",
        "next",
        "--run",
        run_id,
        "--batch",
        "--idempotency-key",
        "cli-round",
    )
    assert code == 0
    assert [task["dimension"] for task in payload["data"]["tasks"]] == [
        "grammar-control",
        "reading",
    ]
    code, refused_payload = _cli(
        polish_workspace, capsys, "assessment", "next", "--run", run_id, "--batch"
    )
    assert code != 0 and refused_payload["error"]["code"] == "invalid_arguments"

    found = routes.match("POST", f"/runs/{run_id}/batch")
    assert found is not None
    route, values = found
    replayed = route.handler(
        routes.Request(
            path_values=values,
            body={"idempotency_key": "cli-round"},
            clock=polish_workspace.clock,
            paths=polish_workspace.paths,
        )
    )
    assert replayed.replayed and replayed.batch_id == payload["data"]["batch_id"]
    assert rows(
        polish_workspace,
        "SELECT DISTINCT command, actor FROM audit_log WHERE after_summary LIKE 'served %'",
    ) == [("assessment.batch", "cli")]


def test_a_batch_member_its_run_never_served_is_found_and_refused_on_replay(
    polish_workspace: PolishWorkspace,
) -> None:
    run_id = start_machine(polish_workspace, ("reading",))
    made = batch(polish_workspace, run_id, "round-1")
    assert "batch_members_were_served_by_their_run" not in failed_checks(polish_workspace)
    stranger = str(
        rows(
            polish_workspace,
            "SELECT task.content_id FROM assessment_tasks task "
            "WHERE task.dimension = 'grammar-control' LIMIT 1",
        )[0][0]
    )
    with (
        open_writer(polish_workspace.paths, command="test.tamper") as database,
        database.transaction() as transaction,
    ):
        # A hand repair that added a member nothing served.
        transaction.execute(
            "INSERT INTO assessment_batch_tasks (batch_id, position, content_id, dimension) "
            "VALUES (?, 2, ?, 'grammar-control')",
            [made.batch_id, stranger],
        )

    check = failed_checks(polish_workspace)["batch_members_were_served_by_their_run"]
    assert stranger in str(check.context)
    failure = refusal(batch, polish_workspace, run_id, "round-1")
    assert failure.payload.code == "assessment_batch_unrecorded"


# --- waiting ---------------------------------------------------------------------------------
#
# Waiting is a state, not a gap. A dimension whose only outstanding task has an answer handed
# in that nobody has marked is `waiting`; a run with nothing open and a judgement outstanding
# is `waiting`; and the screen lists each answer it waits on, by submission, without a word of
# what the learner wrote.


def screen_of(workspace: PolishWorkspace, run_id: str) -> Any:
    from linguawiki.services import assessment_view as view_service

    return view_service.run_screen(workspace.paths, run=run_id, clock=workspace.clock)


def run_tasks(workspace: PolishWorkspace, run_id: str) -> int:
    return int(
        rows(workspace, "SELECT count(*) FROM assessment_run_tasks WHERE run_id = ?", [run_id])[0][
            0
        ]
    )


def when(stamp: str | None) -> Any:
    from datetime import datetime

    assert stamp is not None
    return datetime.fromisoformat(stamp)


def test_with_every_remaining_dimension_blocked_the_run_waits_and_serves_nothing(
    writing: PolishWorkspace,
) -> None:
    run_id = start_written(writing).run_id
    task = serve(writing, run_id)
    in_hand = assessment_service.report(writing.paths, run=run_id, clock=writing.clock)
    # The learner holds the task: that is work, not waiting.
    assert in_hand.progress == "working"
    assert [entry.progress for entry in in_hand.dimensions] == ["open"]

    handed = hand_in(writing, run_id, task.content_id)
    submission_id = handed.submission.submission_id
    report = assessment_service.report(writing.paths, run=run_id, clock=writing.clock)
    screen = screen_of(writing, run_id)

    assert report.progress == screen.progress == "waiting"
    assert [(entry.status, entry.progress) for entry in report.dimensions] == [("open", "waiting")]
    assert [entry.progress for entry in screen.dimensions] == ["waiting"]
    assert [entry.submission_id for entry in screen.outstanding_judgements] == [submission_id]
    assert screen.outstanding_judgements == report.outstanding_judgements

    # Nothing sound to serve: no task is handed back as though the learner still had to
    # answer it, and nothing is written about the learner.
    served_before = run_tasks(writing, run_id)
    again = assessment_service.next_task(
        writing.paths, run=run_id, clock=writing.clock, idempotency_key="serve-while-waiting"
    )
    assert isinstance(again, assessment_service.AssessmentRunReport)
    assert again.progress == "waiting"
    assert [entry.submission_id for entry in again.outstanding_judgements] == [submission_id]
    rerun = assessment_service.next_task(
        writing.paths, run=run_id, clock=writing.clock, idempotency_key="serve-while-waiting"
    )
    assert isinstance(rerun, assessment_service.AssessmentRunReport)
    empty = batch(writing, run_id, "round-while-waiting")
    assert empty.tasks == () and empty.outstanding == ("writing",)
    assert run_tasks(writing, run_id) == served_before

    verdict(writing, run_id, task.content_id, submission_id, score=0.6)
    after = screen_of(writing, run_id)
    assert after.progress == "working" and after.outstanding_judgements == ()
    assert [entry.progress for entry in after.dimensions] == ["open"]
    assert serve(writing, run_id).content_id != task.content_id
    assert_clean(writing)


def test_a_dimension_waiting_on_a_judge_does_not_stop_the_learner_working_in_another(
    writing: PolishWorkspace,
) -> None:
    run_id = assessment_service.start(
        writing.paths,
        dimensions=["reading", "writing"],
        scoring="machine+judged",
        clock=writing.clock,
    ).run_id
    first = {entry.dimension: entry for entry in batch(writing, run_id, "round-1").tasks}
    hand_in(writing, run_id, first["writing"].content_id)
    answer(writing, run_id, first["reading"].task)

    screen = screen_of(writing, run_id)

    by_dimension = {entry.dimension: entry.progress for entry in screen.dimensions}
    assert by_dimension == {"reading": "open", "writing": "waiting"}
    assert screen.progress == "working"
    assert [entry.dimension for entry in screen.outstanding_judgements] == ["writing"]


def test_a_run_with_nothing_left_is_complete_and_a_finalized_one_is_closed(
    polish_workspace: PolishWorkspace,
) -> None:
    run_id = start_machine(polish_workspace, ("reading",))
    sequential_sequence(polish_workspace, run_id)

    finished = screen_of(polish_workspace, run_id)
    assert finished.status == "in-progress"
    assert finished.progress == "complete" and finished.outstanding_judgements == ()
    assert [entry.progress for entry in finished.dimensions] == ["closed"]

    closed = assessment_service.finalize(
        polish_workspace.paths, run=run_id, clock=polish_workspace.clock
    )
    assert closed.progress == "closed"


def test_outstanding_judgements_follow_unclaimed_claimed_and_held_and_carry_no_text(
    writing: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id = written(writing)
    arrived = rows(
        writing,
        "SELECT created_at FROM assessment_submissions WHERE submission_id = ?",
        [submission_id],
    )[0][0]

    def judgement() -> Any:
        screen = screen_of(writing, run_id)
        assert ANSWER[:30] not in screen.model_dump_json()
        (entry,) = screen.outstanding_judgements
        assert (entry.submission_id, entry.content_id, entry.dimension, entry.kind) == (
            submission_id,
            content_id,
            "writing",
            "text",
        )
        assert entry.status == "pending"
        return entry

    queued = judgement()
    assert queued.claim_state == "unclaimed" and queued.attempts == 0
    assert when(queued.unclaimed_since).replace(tzinfo=None) == arrived
    assert queued.claimed_by is None and queued.claimed_until is None

    report = claim(writing, run_id, judge="synthetic-judge-a")
    (first,) = report.claimed
    held_by = judgement()
    assert held_by.claim_state == "claimed" and held_by.attempts == 1
    assert held_by.claimed_by == "synthetic-judge-a"
    assert when(held_by.claimed_until) == when(first.lease_expires_at)
    assert held_by.unclaimed_since is None and not held_by.verdict_held

    # A lease that ran out puts it back in the queue -- unclaimed since the lease ended,
    # not since the answer arrived: a queue a judge just dropped has not waited that long.
    lapse(writing)
    dropped = judgement()
    assert dropped.claim_state == "unclaimed" and dropped.attempts == 1
    assert when(dropped.unclaimed_since) == when(first.lease_expires_at)

    second = claimed_one(writing, run_id, judge="synthetic-judge-b")
    assert judgement().claimed_by == "synthetic-judge-b"
    pause(writing, run_id)
    verdict(writing, run_id, content_id, submission_id, score=0.5, claim=second)

    held = judgement()
    assert held.claim_state == "held" and held.verdict_held and held.attempts == 2
    assert held.claimed_by is None and held.unclaimed_since is None
    assert screen_of(writing, run_id).progress == "waiting"

    resume(writing, run_id)
    assert screen_of(writing, run_id).outstanding_judgements == ()
    assert_clean(writing)


def test_the_cli_names_the_submissions_a_waiting_run_waits_on(
    writing: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    from linguawiki.cli import run as run_cli

    run_id, content_id, submission_id = written(writing)

    for action in ("report", "screen"):
        capsys.readouterr()
        code = run_cli(
            ["assessment", action, "--run", run_id, "--workspace", str(writing.root)],
            clock=writing.clock,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "progress: waiting" in out, out
        assert f"text submission {submission_id} for {content_id} (writing)" in out, out
        assert "unclaimed since" in out
        assert ANSWER[:30] not in out
    code, payload = _cli(writing, capsys, "assessment", "report", "--run", run_id)
    assert code == 0 and payload["data"]["progress"] == "waiting"
    assert payload["data"]["outstanding_judgements"][0]["submission_id"] == submission_id


def test_the_screen_s_waiting_fields_satisfy_the_published_document(
    writing: PolishWorkspace,
) -> None:
    """The model and the OpenAPI document must agree on real output -- with only the
    required fields, so the defaults are what is under test, and with every one set."""

    from jsonschema import Draft202012Validator

    from linguawiki.openapi import DOCUMENT_RELATIVE_PATH
    from linguawiki.services import assessment_view as view_service

    document = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / DOCUMENT_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )

    def validator(name: str) -> Draft202012Validator:
        return Draft202012Validator(
            {"components": document["components"], "$ref": f"#/components/schemas/{name}"}
        )

    minimal_judgement = assessment_service.OutstandingJudgement(
        submission_id="asm_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        content_id="cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        kind="text",
        status="pending",
        claim_state="unclaimed",
    )
    full_judgement = minimal_judgement.model_copy(
        update={
            "dimension": "writing",
            "unclaimed_since": "2026-01-01T09:00:00+00:00",
            "claimed_by": "synthetic-judge",
            "claimed_until": "2026-01-01T09:10:00+00:00",
            "verdict_held": True,
            "attempts": 2,
        }
    )
    minimal_dimension = assessment_service.DimensionReport(
        dimension="writing",
        dimension_kind="written-production",
        status="open",
        tasks_used=0,
        minimum_tasks=1,
        maximum_tasks=2,
    )
    minimal = view_service.RunScreen(
        run_id="asm_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        track_id="trk_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        run_type="placement",
        calibration_label="provisional",
        status="in-progress",
        pack_key="pl-pilot",
        pack_version="0.1.0",
        framework_id="cefr",
        dimensions=(minimal_dimension,),
        outstanding_judgements=(minimal_judgement,),
    )
    validator("RunScreen").validate(minimal.model_dump(mode="json"))
    validator("OutstandingJudgement").validate(full_judgement.model_dump(mode="json"))

    run_id, _content_id, _submission_id = written(writing)
    real = screen_of(writing, run_id)
    assert real.outstanding_judgements and real.progress == "waiting"
    full = real.model_copy(
        update={
            "outstanding_judgements": (*real.outstanding_judgements, full_judgement),
            "dimensions": tuple(
                entry.model_copy(update={"progress": "waiting"}) for entry in real.dimensions
            ),
        }
    )
    validator("RunScreen").validate(full.model_dump(mode="json"))
    validator("AssessmentRunReport").validate(
        assessment_service.report(writing.paths, run=run_id, clock=writing.clock).model_dump(
            mode="json"
        )
    )
    # And the runtime refuses what the document refuses: a stage outside the vocabulary.
    with pytest.raises(ValueError):
        assessment_service.OutstandingJudgement.model_validate(
            {**minimal_judgement.model_dump(), "claim_state": "abandoned"}
        )


# --- waiting: fix round 1 --------------------------------------------------------------------


def test_a_lapsed_answer_blocks_nothing_and_the_next_serve_settles_it(
    writing: PolishWorkspace,
) -> None:
    run_id, content_id, submission_id = written(writing)
    exhaust(writing, run_id)

    # Nobody may claim it, and the next writer withdraws it: the read model must not say
    # the run waits for a judgement no judge can now deliver.
    screen = screen_of(writing, run_id)
    (entry,) = screen.outstanding_judgements
    assert (entry.submission_id, entry.claim_state) == (submission_id, "lapsed")
    assert entry.attempts == judging.JUDGING_POLICY.max_attempts
    assert entry.unclaimed_since is None and entry.claimed_by is None
    assert [dimension.progress for dimension in screen.dimensions] == ["open"]
    assert screen.progress == "working"
    report = assessment_service.report(writing.paths, run=run_id, clock=writing.clock)
    assert report.progress == "working"
    assert [dimension.progress for dimension in report.dimensions] == ["open"]
    assert report.outstanding_judgements == screen.outstanding_judgements
    # A reader reports; it does not settle.
    assert submission_state(writing, submission_id) == ("pending", None)

    following = serve(writing, run_id)

    assert following.content_id != content_id
    assert submission_state(writing, submission_id) == ("withdrawn", judging.EXHAUSTED_CODE)
    after = screen_of(writing, run_id)
    assert after.outstanding_judgements == () and after.progress == "working"
    assert_clean(writing)


@pytest.mark.parametrize("spelling", ["--content", "--content-id"])
def test_record_and_submit_take_either_spelling_of_the_task(spelling: str) -> None:
    from linguawiki.cli import _parser

    parser = _parser()
    recorded = parser.parse_args(["assessment", "record", spelling, "cnt_1"])
    submitted_answer = parser.parse_args(
        ["assessment", "submit", spelling, "cnt_1", "--submission-key", "k"]
    )

    assert recorded.content == "cnt_1"
    assert submitted_answer.content_id == "cnt_1"


def test_the_cli_says_a_lapsed_answer_lapsed_rather_than_unclaimed_since_nothing(
    writing: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    from linguawiki.cli import run as run_cli

    run_id, _, submission_id = written(writing)
    exhaust(writing, run_id)
    capsys.readouterr()

    code = run_cli(
        ["assessment", "report", "--run", run_id, "--workspace", str(writing.root)],
        clock=writing.clock,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"text submission {submission_id}" in out, out
    assert "lapsed, its judging attempts used up" in out, out
    assert "unclaimed since" not in out


def test_an_answer_whose_held_verdict_was_voided_is_unclaimed_since_the_voiding(
    writing: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, content_id, submission_id = written(writing)
    pause(writing, run_id)
    held = verdict(writing, run_id, content_id, submission_id, score=0.5)
    writing.clock.advance(timedelta(hours=2))

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        # Any refusal that leaves the answer pending: the verdict was wrong, not the answer.
        raise LinguaWikiError("assessment_rubric_invalid", "the held rubric no longer holds")

    with monkeypatch.context() as patched:
        patched.setattr(assessment_service, "plan_verdict", refuse)
        resumed = resume(writing, run_id)
    assert [entry.verdict_id for entry in resumed.voided_verdicts] == [held.verdict_id]
    assert submission_state(writing, submission_id) == ("pending", None)

    (entry,) = screen_of(writing, run_id).outstanding_judgements
    received, decided = rows(
        writing,
        "SELECT verdict.received_at, outcome.decided_at FROM assessment_verdicts verdict "
        "JOIN assessment_verdict_outcomes outcome USING (verdict_id) WHERE verdict_id = ?",
        [held.verdict_id],
    )[0]
    assert decided > received
    assert entry.claim_state == "unclaimed" and not entry.verdict_held
    # Held until the resume voided it -- claimable from then, not from when it arrived.
    assert when(entry.unclaimed_since).replace(tzinfo=None) == decided
    assert screen_of(writing, run_id).progress == "waiting"


def test_the_published_judgement_vocabularies_are_the_runtime_ones() -> None:
    """One place for each vocabulary: the type. The document and the model agree because
    both are read off it, and the runtime refuses what the document does not list."""

    from linguawiki.openapi import DOCUMENT_RELATIVE_PATH

    document = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / DOCUMENT_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    published = document["components"]["schemas"]["OutstandingJudgement"]["properties"]
    assert published["kind"]["enum"] == ["recording", "text"]
    assert published["status"]["const"] == "pending"
    assert published["claim_state"]["enum"] == ["unclaimed", "claimed", "held", "lapsed"]
    base = {
        "submission_id": "asm_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "content_id": "cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "kind": "text",
        "claim_state": "unclaimed",
    }
    for field, value in (("kind", "video"), ("status", "judged"), ("claim_state", "gone")):
        with pytest.raises(ValueError):
            assessment_service.OutstandingJudgement.model_validate({**base, field: value})
