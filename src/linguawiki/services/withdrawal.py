"""What an assessment claim becomes when the recording it rests on goes.

A judge listened to a recording and scored it. When the recording is purged -- by the
learner, by a retention sweep, or because a newer capture replaced it -- the score is a
claim about something nobody can hear any more, and it must not outlive the recording.

Four things happen, in the caller's transaction, and none of them deletes anything:

- the result is **marked** invalidated, never removed: a learner told their vowel was
  wrong deserves to see that the evidence is gone;
- the run's dimension posterior is **replayed** from the results that survive, starting at
  the prior the run recorded;
- the track's current estimate for that dimension is **rebuilt** from what survives --
  another run, evidence recorded outside any run, the declared hypothesis, or
  `not-tested` -- rather than left standing on what was withdrawn;
- the history snapshots that rested on the result are **annotated**, never rewritten.

A recording a judge has not yet heard is the other case: its submission is withdrawn, the
served task is skipped -- `assessment_run_tasks.status` holds only `served`, `answered`, and
`skipped`, and DuckDB cannot widen that CHECK -- and the reason lives on the submission.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from linguawiki.clock import aware_utc
from linguawiki.db.connection import Database
from linguawiki.placement import estimated_level
from linguawiki.services import assessment as assessment_service
from linguawiki.services import estimates as estimate_service
from linguawiki.services import learners as learner_service

#: The code a submission carries when its recording was purged before a judge heard it.
PURGED_CODE = "assessment_audio_purged"


@dataclass(frozen=True, slots=True)
class WithdrawalOutcome:
    invalidated_results: tuple[str, ...] = ()
    withdrawn_submissions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Withdrawn:
    result_id: str
    run_id: str
    track_id: str
    dimension: str
    recorded_at: datetime


def dependent_results(database: Database, *, artifact_id: str) -> list[str]:
    """The standing assessment results a judge reached by listening to this recording."""

    return [
        str(result_id)
        for (result_id,) in database.query(
            "SELECT result_id FROM assessment_results "
            "WHERE audio_artifact_id = ? AND invalidated_at IS NULL ORDER BY result_id",
            [artifact_id],
        )
    ]


def withdraw_submission(database: Database, *, submission_id: str, code: str, reason: str) -> None:
    """Withdraw a submission no judge can now hear, and settle its task as skipped.

    The task is settled rather than left `served`, because a served task holds its
    dimension: the one-outstanding-task guard would otherwise keep that dimension waiting
    for a verdict that can never arrive. The reason is the submission's, by name, so a
    learner can be told why the answer they gave was not marked.
    """

    row = database.one(
        "SELECT run_id, content_id FROM assessment_submissions "
        "WHERE submission_id = ? AND status = 'pending'",
        [submission_id],
    )
    if row is None:
        return
    now = database.now()
    database.execute(
        "UPDATE assessment_submissions SET status = 'withdrawn', withdrawn_code = ?, "
        "withdrawn_reason = ?, updated_at = ? WHERE submission_id = ?",
        [code, reason, now, submission_id],
    )
    database.execute(
        "UPDATE assessment_run_tasks SET status = 'skipped' "
        "WHERE run_id = ? AND content_id = ? AND status = 'served'",
        [str(row[0]), str(row[1])],
    )


def write_withdrawal(database: Database, *, artifact_id: str, reason: str) -> WithdrawalOutcome:
    """Settle every assessment claim resting on this recording, in the caller's transaction."""

    withdrawn_submissions = []
    for (submission_id,) in database.query(
        "SELECT submission_id FROM assessment_submissions "
        "WHERE artifact_id = ? AND status = 'pending' ORDER BY submission_id",
        [artifact_id],
    ):
        withdraw_submission(
            database, submission_id=str(submission_id), code=PURGED_CODE, reason=reason
        )
        withdrawn_submissions.append(str(submission_id))
    rows = [
        _Withdrawn(
            result_id=str(row[0]),
            run_id=str(row[1]),
            track_id=str(row[2]),
            dimension=str(row[3]),
            recorded_at=row[4],
        )
        for row in database.query(
            "SELECT result.result_id, result.run_id, run.track_id, result.dimension, "
            "result.recorded_at FROM assessment_results result "
            "JOIN assessment_runs run ON run.run_id = result.run_id "
            "WHERE result.audio_artifact_id = ? AND result.invalidated_at IS NULL "
            "ORDER BY result.recorded_at, result.result_id",
            [artifact_id],
        )
    ]
    if not rows:
        return WithdrawalOutcome(withdrawn_submissions=tuple(withdrawn_submissions))
    now = database.now()
    for entry in rows:
        database.execute(
            "UPDATE assessment_results SET invalidated_at = ?, invalidated_reason = ? "
            "WHERE result_id = ?",
            [now, reason, entry.result_id],
        )
    for run_id, dimension in sorted({(entry.run_id, entry.dimension) for entry in rows}):
        assessment_service.replay_dimension(database, run_id, dimension)
    for track_id, dimension in sorted({(entry.track_id, entry.dimension) for entry in rows}):
        affected = [
            entry for entry in rows if entry.track_id == track_id and entry.dimension == dimension
        ]
        _rebuild_estimate(
            database,
            track_id=track_id,
            dimension=dimension,
            withdrawn=affected,
            artifact_id=artifact_id,
            reason=reason,
        )
    return WithdrawalOutcome(
        invalidated_results=tuple(entry.result_id for entry in rows),
        withdrawn_submissions=tuple(withdrawn_submissions),
    )


def _run_status(database: Database, run_id: str) -> str:
    return str(database.scalar("SELECT status FROM assessment_runs WHERE run_id = ?", [run_id]))


def _rebuild_estimate(
    database: Database,
    *,
    track_id: str,
    dimension: str,
    withdrawn: Sequence[_Withdrawn],
    artifact_id: str,
    reason: str,
) -> None:
    """Rebuild the current estimate from what survives, and annotate what rested on it.

    Only when the withdrawn results reached the estimate: through a finalized run, or
    through the run the current estimate was written from. A result in a run still being
    worked shaped nothing but that run's posterior, which the replay already rebuilt.
    """

    runs = {entry.run_id for entry in withdrawn}
    current = estimate_service.read_estimate(database, track_id=track_id, dimension=dimension)
    finalized = {run_id for run_id in runs if _run_status(database, run_id) == "finalized"}
    if not finalized and (current is None or current.source_run_id not in runs):
        return
    record = learner_service.track_context(database, track_id)
    factor = estimate_service.EstimateFactor(
        name="evidence-withdrawn",
        weight=0.0,
        detail=(
            f"{len(withdrawn)} assessment result(s) withdrawn because {reason}: "
            + ", ".join(entry.result_id for entry in withdrawn)
        ),
    )

    def fallback() -> estimate_service.EstimateChange:
        return _write_what_survives(
            database,
            track_id=track_id,
            framework_id=record.proficiency_framework,
            levels=record.framework_levels,
            dimension=dimension,
            declared_level=record.declared_level,
            factor=factor,
            runs=runs,
        )

    change = estimate_service.recompute_dimension(
        database,
        track_id=track_id,
        framework_id=record.proficiency_framework,
        dimension=dimension,
        levels=record.framework_levels,
        now=aware_utc(database.now()),
        mode=estimate_service.WITHDRAWN_RECOMPUTE,
        fallback=fallback,
        extra_factors=(factor,),
    )
    exclude = () if change.snapshot_id is None else (change.snapshot_id,)
    for entry in withdrawn:
        estimate_service.annotate_withdrawn(
            database,
            track_id=track_id,
            dimension=dimension,
            run_id=entry.run_id,
            result_id=entry.result_id,
            since=entry.recorded_at,
            artifact_id=artifact_id,
            reason=f"rested on result {entry.result_id}, withdrawn because {reason}",
            exclude=exclude,
        )


def _write_what_survives(
    database: Database,
    *,
    track_id: str,
    framework_id: str,
    levels: Sequence[str],
    dimension: str,
    declared_level: str | None,
    factor: estimate_service.EstimateFactor,
    runs: set[str],
) -> estimate_service.EstimateChange:
    """No usable evidence remains: the newest finalized run that still measured the
    dimension, else the declared hypothesis, else `not-tested`.

    Never anything higher than what survives. "The newest" is by the run's own
    chronology, so an older run whose state the replay just rewrote cannot displace a
    newer calibration.
    """

    baseline = database.one(
        "SELECT state.run_id FROM placement_dimension_state state "
        "JOIN assessment_runs run ON run.run_id = state.run_id "
        "WHERE run.track_id = ? AND state.dimension = ? AND state.tasks_used > 0 "
        "AND run.status = 'finalized' "
        "ORDER BY run.started_at DESC, run.run_id DESC LIMIT 1",
        [track_id, dimension],
    )
    if baseline is not None:
        run_id = str(baseline[0])
        state = assessment_service.run_state(database, run_id, dimension)
        run_levels = assessment_service.run_levels(database, run_id)
        level, low, high = estimated_level(state, run_levels)
        return estimate_service.upsert_from_state(
            database,
            track_id=track_id,
            framework_id=framework_id,
            state=state,
            level=level,
            low=low,
            high=high,
            run_id=run_id,
            basis=assessment_service.run_basis(database, run_id),
            reason=f"rebuilt from run {run_id} after evidence was withdrawn",
            extra_factors=(factor,),
        )
    if declared_level is not None and declared_level in levels:
        kind = _dimension_kind(database, dimension, runs=runs)
        return assessment_service.declared_estimate(
            database,
            track_id=track_id,
            framework_id=framework_id,
            levels=levels,
            dimension=dimension,
            dimension_kind=kind,
            declared_level=declared_level,
            reason=(
                f"the evidence for {dimension} was withdrawn, so the learner's declared level "
                f"{declared_level} is what remains"
            ),
            factors=(factor,),
        )
    return estimate_service.write_estimate(
        database,
        track_id=track_id,
        framework_id=framework_id,
        dimension=dimension,
        estimate_status="not-tested",
        level_code=None,
        level_low=None,
        level_high=None,
        score=None,
        uncertainty=None,
        confidence="not-tested",
        basis="declared-hypothesis",
        evidence_count=0,
        source_run_id=None,
        reason=f"the evidence for {dimension} was withdrawn and nothing else measured it",
        factors=(factor,),
    )


def _dimension_kind(database: Database, dimension: str, *, runs: set[str]) -> str:
    for run_id in sorted(runs):
        kinds = assessment_service.run_dimension_kinds(database, run_id)
        if dimension in kinds:
            return kinds[dimension]
    raise AssertionError(f"no run recorded the kind of {dimension}")


__all__ = [
    "PURGED_CODE",
    "WithdrawalOutcome",
    "dependent_results",
    "withdraw_submission",
    "write_withdrawal",
]
