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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from linguawiki.clock import aware_utc
from linguawiki.db.connection import Database
from linguawiki.errors import LinguaWikiError
from linguawiki.placement import estimated_level
from linguawiki.services import assessment as assessment_service
from linguawiki.services import estimates as estimate_service
from linguawiki.services import learners as learner_service

#: The code a submission carries when its recording was purged before a judge heard it.
PURGED_CODE = "assessment_audio_purged"
#: The submission a held verdict judged was replaced by a later answer.
SUPERSEDED_CODE = "assessment_submission_superseded"
#: The run was abandoned with the submission still waiting for a judge.
ABANDONED_CODE = "assessment_run_abandoned"
#: The run was finalized `exclude_outstanding`, with the submission still waiting.
FINALIZED_CODE = "assessment_run_finalized"
#: The track stopped consenting to keeping written answers before this one was judged.
NOT_RETAINED_CODE = "assessment_response_not_retained"
#: Why a consent change purged a recording, in the learner's terms. The tombstone's own
#: `purge_reason` is a closed vocabulary (`learner-request` here: the learner withdrew the
#: consent), so this is what keeps "why" on the submission and in the audit log.
CONSENT_WITHDRAWN = "consent withdrawn"


@dataclass(frozen=True, slots=True)
class WithdrawalOutcome:
    invalidated_results: tuple[str, ...] = ()
    withdrawn_submissions: tuple[str, ...] = ()
    #: Verdicts held for those submissions on a paused run, voided with them. Carried out
    #: so the command that purged says so: a verdict is something a judge delivered.
    voided_verdicts: tuple[str, ...] = ()


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


def withdraw_submission(
    database: Database,
    *,
    submission_id: str,
    code: str,
    reason: str,
    clear_text: bool = False,
) -> bool:
    """Withdraw a submission no judge can now hear, and settle its task as skipped.

    The task is settled rather than left `served`, because a served task holds its
    dimension: the one-outstanding-task guard would otherwise keep that dimension waiting
    for a verdict that can never arrive. The reason is the submission's, by name, so a
    learner can be told why the answer they gave was not marked.

    Returns whether it withdrew anything: a submission no longer pending was settled by
    whatever moved it, and is left as that left it.

    `clear_text` also discards a written answer's retained text, in the same statement
    that withdraws it: the consent that let it be kept has gone, and a moment at which the
    row is withdrawn for that reason and still holds the words is the breach itself. The
    digest stays -- it carries nothing, and still answers "was this the answer?".
    """

    row = database.one(
        "SELECT run_id, content_id FROM assessment_submissions "
        "WHERE submission_id = ? AND status = 'pending'",
        [submission_id],
    )
    if row is None:
        return False
    now = database.now()
    if clear_text:
        database.execute(
            "UPDATE assessment_submissions SET status = 'withdrawn', withdrawn_code = ?, "
            "withdrawn_reason = ?, response_text = NULL, updated_at = ? WHERE submission_id = ?",
            [code, reason, now, submission_id],
        )
    else:
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
    return True


@dataclass(frozen=True, slots=True)
class Settlement:
    """A submission no verdict can now be applied to, and why -- decided, not yet written.

    `assessment.plan_verdict` returns one instead of writing, because planning only reads:
    the C5 refusal that withdrew an unjudgeable recording and *then* raised is this value,
    written by `settle` and raised by whoever planned it. The code and reason are what the
    submission and any held verdict are settled with, so a learner is told the same thing
    the judge was. `refusal`, when present, is what the planning caller raises once the
    settlement has committed; a caller that settles in passing (a resume, a sweep) reports
    it instead.
    """

    submission_id: str
    code: str
    reason: str
    refusal: LinguaWikiError | None = None
    #: Discard a written answer's text as it is withdrawn (`withdraw_submission`).
    clear_text: bool = False


@dataclass(frozen=True, slots=True)
class SettlementOutcome:
    """What one `settle` wrote: whether the submission was withdrawn, and which held
    verdicts were voided with it."""

    submission_id: str
    withdrawn: bool
    voided_verdicts: tuple[str, ...] = ()


def void_held_verdicts(
    database: Database, *, submission_id: str, code: str, reason: str
) -> tuple[str, ...]:
    """Give every held verdict for this submission a `void` outcome, in the caller's
    transaction, and return their identifiers.

    A held verdict is a verdict with no outcome. Once its submission cannot be judged it
    can never be applied, and leaving it outcome-less would keep it reading as waiting for
    a resume that cannot help it. Voided, not deleted: the verdict arrived, and the record
    says what became of it and why.
    """

    held = [
        str(verdict_id)
        for (verdict_id,) in database.query(
            "SELECT verdict.verdict_id FROM assessment_verdicts verdict "
            "WHERE verdict.submission_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM assessment_verdict_outcomes outcome "
            "WHERE outcome.verdict_id = verdict.verdict_id) "
            "ORDER BY verdict.received_at, verdict.verdict_id",
            [submission_id],
        )
    ]
    if not held:
        return ()
    now = database.now()
    for verdict_id in held:
        database.execute(
            "INSERT INTO assessment_verdict_outcomes (verdict_id, outcome, result_id, code, "
            "reason, decided_at) VALUES (?, 'void', NULL, ?, ?, ?)",
            [verdict_id, code, reason, now],
        )
    return tuple(held)


def settle(database: Database, settlement: Settlement) -> SettlementOutcome:
    """The one writer of settlements, inside the caller's transaction.

    Withdraws the submission (task skipped, dimension unblocked, reason on the row) and
    voids every verdict held for it. Both halves run whatever the other found: a
    submission an earlier writer already withdrew can still have a held verdict that
    nobody voided, and a pending submission with nothing held still has to stop holding
    its dimension. No commit of its own -- `record` commits it in a transaction of its
    own before raising, and a resume or a sweep writes it inside theirs.
    """

    withdrawn = withdraw_submission(
        database,
        submission_id=settlement.submission_id,
        code=settlement.code,
        reason=settlement.reason,
        clear_text=settlement.clear_text,
    )
    voided = void_held_verdicts(
        database,
        submission_id=settlement.submission_id,
        code=settlement.code,
        reason=settlement.reason,
    )
    return SettlementOutcome(
        submission_id=settlement.submission_id, withdrawn=withdrawn, voided_verdicts=voided
    )


def write_withdrawal(database: Database, *, artifact_id: str, reason: str) -> WithdrawalOutcome:
    """Settle every assessment claim resting on this recording, in the caller's transaction."""

    withdrawn_submissions = []
    voided_verdicts: list[str] = []
    for (submission_id,) in database.query(
        "SELECT submission_id FROM assessment_submissions "
        "WHERE artifact_id = ? AND status = 'pending' ORDER BY submission_id",
        [artifact_id],
    ):
        # Through `settle`, so a verdict held for it on a paused run is voided with the
        # same code rather than left waiting for a resume that could only refuse it.
        settled = settle(
            database,
            Settlement(submission_id=str(submission_id), code=PURGED_CODE, reason=reason),
        )
        withdrawn_submissions.append(str(submission_id))
        voided_verdicts.extend(settled.voided_verdicts)
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
        return WithdrawalOutcome(
            withdrawn_submissions=tuple(withdrawn_submissions),
            voided_verdicts=tuple(voided_verdicts),
        )
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
        voided_verdicts=tuple(voided_verdicts),
    )


def describe_withdrawn(
    database: Database, submission_ids: Sequence[str], voided: Sequence[str] = ()
) -> tuple[assessment_service.WithdrawnSubmission, ...]:
    """What a report says of submissions withdrawn in this transaction, read back from the
    rows: the code and reason the learner is shown, and the voided verdicts that were held
    for each. Read back rather than restated, so the report cannot say one thing while the
    row says another."""

    owners = {
        str(verdict_id): str(submission_id)
        for verdict_id, submission_id in (
            database.query(
                "SELECT verdict_id, submission_id FROM assessment_verdicts "
                f"WHERE verdict_id IN ({', '.join('?' for _ in voided)})",
                list(voided),
            )
            if voided
            else []
        )
    }
    described = []
    for submission_id in dict.fromkeys(submission_ids):
        row = database.one(
            "SELECT content_id, withdrawn_code, withdrawn_reason FROM assessment_submissions "
            "WHERE submission_id = ?",
            [submission_id],
        )
        if row is None:
            continue
        described.append(
            assessment_service.WithdrawnSubmission(
                submission_id=submission_id,
                content_id=str(row[0]),
                code=str(row[1]),
                reason=str(row[2]),
                voided_verdicts=tuple(
                    verdict_id for verdict_id in voided if owners.get(verdict_id) == submission_id
                ),
            )
        )
    return tuple(described)


def describe_voided(
    database: Database, verdict_ids: Sequence[str]
) -> tuple[assessment_service.VoidedVerdict, ...]:
    """The voided verdicts a report names, with the code and reason each was voided for."""

    described = []
    for verdict_id in dict.fromkeys(verdict_ids):
        row = database.one(
            "SELECT verdict.submission_id, submission.content_id, outcome.code, outcome.reason "
            "FROM assessment_verdicts verdict "
            "JOIN assessment_verdict_outcomes outcome ON outcome.verdict_id = verdict.verdict_id "
            "LEFT JOIN assessment_submissions submission "
            "  ON submission.submission_id = verdict.submission_id "
            "WHERE verdict.verdict_id = ? AND outcome.outcome = 'void'",
            [verdict_id],
        )
        if row is None:
            continue
        described.append(
            assessment_service.VoidedVerdict(
                verdict_id=verdict_id,
                submission_id=str(row[0]),
                content_id=str(row[1]),
                code=str(row[2]),
                reason=str(row[3]),
            )
        )
    return tuple(described)


@dataclass(frozen=True, slots=True)
class Outstanding:
    """What a run still owes a judgement on: submissions waiting for a judge, as
    `(submission_id, content_id)`, and verdicts held for a resume, as
    `(verdict_id, submission_id, content_id)`."""

    pending: tuple[tuple[str, str], ...] = ()
    held: tuple[tuple[str, str, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.pending or self.held)


def outstanding(database: Database, run_id: str) -> Outstanding:
    """The judgements a run has not settled, oldest first."""

    pending = tuple(
        (str(submission_id), str(content_id))
        for submission_id, content_id in database.query(
            "SELECT submission_id, content_id FROM assessment_submissions "
            "WHERE run_id = ? AND status = 'pending' ORDER BY created_at, submission_id",
            [run_id],
        )
    )
    held = tuple(
        (str(verdict_id), str(submission_id), str(content_id))
        for verdict_id, submission_id, content_id in database.query(
            "SELECT verdict.verdict_id, verdict.submission_id, submission.content_id "
            "FROM assessment_verdicts verdict "
            "JOIN assessment_submissions submission "
            "  ON submission.submission_id = verdict.submission_id "
            "WHERE submission.run_id = ? AND NOT EXISTS ("
            "  SELECT 1 FROM assessment_verdict_outcomes outcome "
            "  WHERE outcome.verdict_id = verdict.verdict_id) "
            "ORDER BY verdict.received_at, verdict.verdict_id",
            [run_id],
        )
    )
    return Outstanding(pending=pending, held=held)


def withdraw_outstanding(
    database: Database, run_id: str, *, code: str, reason: str
) -> tuple[tuple[assessment_service.WithdrawnSubmission, ...], tuple[str, ...]]:
    """Settle everything a closing run still owes, inside the caller's transaction.

    Every pending submission is withdrawn (task skipped) and the verdicts held for it are
    voided, through `settle`; then any verdict still held -- one whose submission is no
    longer pending, such as one superseded while the run was paused -- is voided with the
    same code. A closing run can apply nothing, so nothing on it may go on reading as
    waiting. Returns the withdrawn submissions and every verdict voided, by either route.
    """

    owed = outstanding(database, run_id)
    outcomes = [
        settle(database, Settlement(submission_id=submission_id, code=code, reason=reason))
        for submission_id, _ in owed.pending
    ]
    voided = [verdict_id for outcome in outcomes for verdict_id in outcome.voided_verdicts]
    for submission_id in dict.fromkeys(entry[1] for entry in outstanding(database, run_id).held):
        successor = database.scalar(
            "SELECT superseded_by FROM assessment_submissions "
            "WHERE submission_id = ? AND status = 'superseded'",
            [submission_id],
        )
        # A superseded answer's verdict ended with the answer, whatever the run did next:
        # it is voided as superseded, naming what replaced it, exactly as a resume would.
        voided.extend(
            void_held_verdicts(
                database,
                submission_id=submission_id,
                code=code if successor is None else SUPERSEDED_CODE,
                reason=reason
                if successor is None
                else f"{submission_id} was replaced by {successor}: the learner answered "
                "again, and this verdict judges an answer no longer submitted",
            )
        )
    withdrawn = describe_withdrawn(
        database, [outcome.submission_id for outcome in outcomes if outcome.withdrawn], voided
    )
    return withdrawn, tuple(voided)


@dataclass(frozen=True, slots=True)
class ConsentOutcome:
    """What a consent change settled: the submissions it withdrew (each naming the held
    verdicts voided with it) and the recordings it purged."""

    withdrawn: tuple[assessment_service.WithdrawnSubmission, ...] = ()
    purged_artifacts: tuple[str, ...] = ()

    @property
    def voided_verdicts(self) -> tuple[str, ...]:
        return tuple(verdict for entry in self.withdrawn for verdict in entry.voided_verdicts)


def _audio_withdrawn(before: Mapping[str, object], after: Mapping[str, object]) -> bool:
    # Consent to keep a recording is given only by `True`; anything else is its absence.
    key = "audio_retention_consent"
    return before.get(key) is True and after.get(key) is not True


def _transcript_withdrawn(before: Mapping[str, object], after: Mapping[str, object]) -> bool:
    # Unset keeps an excerpt and `True` keeps the whole text; only `False` keeps none
    # (`evidence.retain_response`), so it is turning *to* `False` that withdraws.
    key = "transcript_retention_consent"
    return before.get(key) is not False and after.get(key) is False


def on_consent_change(
    database: Database,
    root: Path,
    *,
    track_id: str,
    before: Mapping[str, object],
    after: Mapping[str, object],
    command: str,
) -> ConsentOutcome:
    """Settle the judgements a consent change took the ground from under -- the one home
    for that, inside the caller's transaction (`learners.update_track`'s).

    - **Audio retention consent turned off**: every pending recording submission on the
      track has its recording purged through `artifacts.write_purge` -- the file deleted
      inside the transaction that records it, as every purge is -- and the purge withdraws
      it with `assessment_audio_purged`. One event, one code; `consent withdrawn` is the
      reason it carries, so the why is not lost.
    - **Transcript retention consent turned off**: every pending written answer on the
      track is withdrawn with `assessment_response_not_retained`, and its text cleared in
      the statement that withdraws it.

    Either way the verdicts held for those submissions are voided with them. Nothing is
    refused here: the learner's decision is the one fact in the room that is not open to
    question, and everything else is settled to fit it.
    """

    from linguawiki.services import artifacts as artifact_service

    withdrawn: list[str] = []
    voided: list[str] = []
    purged: list[str] = []
    if _audio_withdrawn(before, after):
        for submission_id, artifact_id, purged_at in database.query(
            "SELECT submission.submission_id, submission.artifact_id, artifact.purged_at "
            "FROM assessment_submissions submission "
            "JOIN assessment_runs run ON run.run_id = submission.run_id "
            "LEFT JOIN artifacts artifact ON artifact.artifact_id = submission.artifact_id "
            "WHERE run.track_id = ? AND submission.kind = 'recording' "
            "AND submission.status = 'pending' ORDER BY submission.created_at, 1",
            [track_id],
        ):
            if purged_at is None and artifact_id is not None:
                # One purge may settle several submissions resting on one recording, so a
                # later row in this loop can already be withdrawn; `write_purge` settles
                # only what is still pending.
                if str(artifact_id) in purged:
                    continue
                outcome = artifact_service.write_purge(
                    database,
                    root,
                    artifact_id=str(artifact_id),
                    reason="learner-request",
                    detail=CONSENT_WITHDRAWN,
                    command=command,
                )
                purged.append(str(artifact_id))
                withdrawn.extend(outcome.withdrawn_submissions)
                voided.extend(outcome.voided_verdicts)
            else:
                # Already gone -- a tombstone with a submission still pending is damage
                # `db check` reports -- so there is nothing to purge, and the submission
                # is settled by the code a purge would have given it.
                settled = settle(
                    database,
                    Settlement(
                        submission_id=str(submission_id),
                        code=PURGED_CODE,
                        reason=f"the recording was purged ({CONSENT_WITHDRAWN})",
                    ),
                )
                if settled.withdrawn:
                    withdrawn.append(str(submission_id))
                voided.extend(settled.voided_verdicts)
    if _transcript_withdrawn(before, after):
        for (submission_id,) in database.query(
            "SELECT submission.submission_id FROM assessment_submissions submission "
            "JOIN assessment_runs run ON run.run_id = submission.run_id "
            "WHERE run.track_id = ? AND submission.kind = 'text' "
            "AND submission.status = 'pending' ORDER BY submission.created_at, 1",
            [track_id],
        ):
            settled = settle(
                database,
                Settlement(
                    submission_id=str(submission_id),
                    code=NOT_RETAINED_CODE,
                    reason=(
                        f"{CONSENT_WITHDRAWN}: the track stopped agreeing to keep written "
                        "answers before a judge read this one, so its text was discarded"
                    ),
                    clear_text=True,
                ),
            )
            if settled.withdrawn:
                withdrawn.append(str(submission_id))
            voided.extend(settled.voided_verdicts)
    return ConsentOutcome(
        withdrawn=describe_withdrawn(database, withdrawn, voided),
        purged_artifacts=tuple(purged),
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
    "ABANDONED_CODE",
    "CONSENT_WITHDRAWN",
    "FINALIZED_CODE",
    "NOT_RETAINED_CODE",
    "PURGED_CODE",
    "SUPERSEDED_CODE",
    "ConsentOutcome",
    "Outstanding",
    "Settlement",
    "SettlementOutcome",
    "WithdrawalOutcome",
    "dependent_results",
    "describe_voided",
    "describe_withdrawn",
    "on_consent_change",
    "outstanding",
    "settle",
    "void_held_verdicts",
    "withdraw_outstanding",
    "withdraw_submission",
    "write_withdrawal",
]
