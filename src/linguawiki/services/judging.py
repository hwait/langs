"""Who is judging a submission, for how long, and when to stop asking.

Core supplies no judge. A judge -- an agent, a person, a script -- pulls work through
`claim`, holds **no database connection** while it judges, and delivers through
`assessment record --submission … --claim …`, or gives the work back through `release`.
Core keeps the ledger:

- a **claim** is one attempt: `judging_claims` gains a row with a lease, and the lease
  only schedules -- a verdict arriving after it expired is still accepted if nothing else
  has happened to the submission, because when a judge finished says nothing about whether
  it was right;
- a claim **ends** when a verdict naming it commits, when it is released, or -- with no
  write at all -- when its lease runs out. Claimed, expired, and exhausted are derived from
  the rows and the clock, never stored, so a restart of anything is the expired-lease case;
- **attempts** are the claims made for a submission. When `JUDGING_POLICY.max_attempts`
  have been made and the last has ended with no verdict, the submission is withdrawn with
  `assessment_judging_exhausted`.

That last rule **costs the learner an observation for a judge's failure**, deliberately:
the alternative is a submission nobody may claim holding its dimension forever. Nothing
runs in the background to apply it, so `sweep_lapsed` runs inside every writer that
touches the run, before that writer's own work, and `db check` reports an exhausted
submission nobody has touched since.

Every withdrawal here goes through `withdrawal.settle`, the one writer of settlements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import AssessmentId, EventId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import assessment as assessment_service
from linguawiki.services import learners as learner_service
from linguawiki.services import recordings as recording_service
from linguawiki.services.withdrawal import Settlement, SettlementOutcome, settle


@dataclass(frozen=True, slots=True)
class JudgingPolicy:
    """How long a claim is held by default, and how many are made before giving up.

    Versioned, and the version is written into the reason of every withdrawal it causes,
    so a withdrawn submission says which rule withdrew it after the rule has changed.
    """

    version: str
    max_attempts: int
    default_lease_seconds: int


JUDGING_POLICY = JudgingPolicy(version="judging.v1", max_attempts=3, default_lease_seconds=600)

#: The code a submission is withdrawn with when every attempt to judge it ended without a
#: verdict.
EXHAUSTED_CODE = "assessment_judging_exhausted"

#: The longest lease a claim may ask for. A lease only schedules, but a lease of a year
#: would hold a dimension for a year with nothing anybody could do about it.
MAXIMUM_LEASE_SECONDS = 24 * 60 * 60

CLAIM_COMMAND = "assessment.claim"
RELEASE_COMMAND = "assessment.release"


# --- where a submission's judging stands ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimState:
    """Where judging one submission stands: attempts made, and the live lease if any."""

    attempts: int
    live_claim: str | None = None
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None


UNCLAIMED = ClaimState(attempts=0)

# A claim is live while its lease runs and nothing has ended it: no release, no verdict.
# One fragment, used by every reader that asks, so "live" cannot mean two things.
_LIVE = (
    "{claim}.lease_expires_at > ? "
    "AND NOT EXISTS (SELECT 1 FROM judging_releases release "
    "WHERE release.claim_id = {claim}.claim_id) "
    "AND NOT EXISTS (SELECT 1 FROM assessment_verdicts verdict "
    "WHERE verdict.claim_id = {claim}.claim_id)"
)


def claim_states(
    database: Database, run_id: str, *, now: datetime | None = None
) -> dict[str, ClaimState]:
    """The judging state of every submission in a run that has ever been claimed.

    The clock is read only when there is a claim to compare it with, so a run nobody has
    claimed from costs no clock read.
    """

    rows = database.query(
        "SELECT claim.submission_id, claim.claim_id, claim.judge, claim.lease_expires_at, "
        "EXISTS (SELECT 1 FROM judging_releases release "
        "WHERE release.claim_id = claim.claim_id), "
        "EXISTS (SELECT 1 FROM assessment_verdicts verdict "
        "WHERE verdict.claim_id = claim.claim_id) "
        "FROM judging_claims claim JOIN assessment_submissions submission "
        "ON submission.submission_id = claim.submission_id "
        "WHERE submission.run_id = ? ORDER BY claim.claimed_at, claim.claim_id",
        [run_id],
    )
    if not rows:
        return {}
    moment = now if now is not None else database.now()
    states: dict[str, ClaimState] = {}
    for submission_id, claim_id, judge, expires, released, judged in rows:
        before = states.get(str(submission_id), UNCLAIMED)
        live = expires > moment and not released and not judged
        states[str(submission_id)] = ClaimState(
            attempts=before.attempts + 1,
            live_claim=str(claim_id) if live else before.live_claim,
            claimed_by=str(judge) if live else before.claimed_by,
            lease_expires_at=expires if live else before.lease_expires_at,
        )
    return states


def held_submissions(database: Database, run_id: str) -> frozenset[str]:
    """Submissions in this run with a verdict held until the run resumes."""

    return frozenset(
        str(submission_id)
        for (submission_id,) in database.query(
            "SELECT DISTINCT verdict.submission_id FROM assessment_verdicts verdict "
            "JOIN assessment_submissions submission "
            "ON submission.submission_id = verdict.submission_id "
            "WHERE submission.run_id = ? AND NOT EXISTS (SELECT 1 "
            "FROM assessment_verdict_outcomes outcome "
            "WHERE outcome.verdict_id = verdict.verdict_id)",
            [run_id],
        )
    )


@dataclass(frozen=True, slots=True)
class Lapsed:
    """A pending submission whose every attempt has ended without a verdict."""

    submission_id: str
    run_id: str
    attempts: int


def lapsed_submissions(
    database: Database,
    *,
    run_id: str | None,
    sparing_claim: str | None = None,
    now: datetime | None = None,
) -> list[Lapsed]:
    """Pending submissions that have used every attempt, with none of them still live and
    no verdict standing for them -- the ones `sweep_lapsed` withdraws.

    `run_id=None` asks across the workspace, which is what `db check` does. A submission
    with a held verdict is not lapsed: it was judged, and waits only for the resume.

    `sparing_claim` leaves out the submission a still-unreleased claim was made for. A
    verdict delivered under it is about to be planned, and an expired lease is accepted
    when nothing else has happened to the submission: settling it on the way in would be
    the "something else", made by the very call delivering the verdict. A released claim
    spares nothing; its verdict is refused anyway.

    The clock is read only when some submission has used every attempt.
    """

    limit = JUDGING_POLICY.max_attempts
    scope = (
        "submission.status = 'pending' AND (CAST(? AS VARCHAR) IS NULL OR submission.run_id = ?)"
    )
    exhausted = database.scalar(
        "SELECT count(*) FROM (SELECT submission.submission_id FROM assessment_submissions "
        "submission JOIN judging_claims claim ON claim.submission_id = submission.submission_id "
        f"WHERE {scope} GROUP BY submission.submission_id HAVING count(*) >= ?)",
        [run_id, run_id, limit],
    )
    if not exhausted:
        return []
    moment = now if now is not None else database.now()
    return [
        Lapsed(submission_id=str(submission_id), run_id=str(owner), attempts=int(attempts))
        for submission_id, owner, attempts in database.query(
            "SELECT submission.submission_id, submission.run_id, count(*) "
            "FROM assessment_submissions submission "
            "JOIN judging_claims claim ON claim.submission_id = submission.submission_id "
            f"WHERE {scope} "
            "AND NOT EXISTS (SELECT 1 FROM judging_claims live "
            "WHERE live.submission_id = submission.submission_id AND "
            + _LIVE.format(claim="live")
            + ") AND NOT EXISTS (SELECT 1 FROM assessment_verdicts standing "
            "LEFT JOIN assessment_verdict_outcomes outcome "
            "ON outcome.verdict_id = standing.verdict_id "
            "WHERE standing.submission_id = submission.submission_id "
            "AND (outcome.outcome IS NULL OR outcome.outcome = 'applied')) "
            "AND NOT EXISTS (SELECT 1 FROM judging_claims spared "
            "WHERE spared.claim_id = CAST(? AS VARCHAR) "
            "AND spared.submission_id = submission.submission_id "
            "AND NOT EXISTS (SELECT 1 FROM judging_releases release "
            "WHERE release.claim_id = spared.claim_id)) "
            "GROUP BY submission.submission_id, submission.run_id HAVING count(*) >= ? "
            "ORDER BY submission.submission_id",
            [run_id, run_id, moment, sparing_claim, limit],
        )
    ]


def exhausted_reason(attempts: int) -> str:
    policy = JUDGING_POLICY
    return (
        f"{attempts} judging attempt(s) ended without a verdict, and judging policy "
        f"{policy.version} stops asking after {policy.max_attempts}; the answer is withdrawn "
        "rather than left holding its dimension for a judge that is not coming"
    )


def _settle_lapsed(
    database: Database, run_id: str, *, sparing_claim: str | None = None
) -> tuple[SettlementOutcome, ...]:
    """Withdraw every lapsed submission in the run, inside the caller's transaction.

    `assessment_judging_exhausted`, with the policy version in the reason. Through
    `withdrawal.settle`, so the task is skipped, the dimension unblocked, and the reason on
    the submission, exactly as any other withdrawal. Private: a writer outside this module
    calls `sweep_lapsed`, which also reports and audits what this wrote; `release` calls
    this inside its own transaction and audits it there.
    """

    return tuple(
        settle(
            database,
            Settlement(
                submission_id=entry.submission_id,
                code=EXHAUSTED_CODE,
                reason=exhausted_reason(entry.attempts),
            ),
        )
        for entry in lapsed_submissions(database, run_id=run_id, sparing_claim=sparing_claim)
    )


def sweep_lapsed(
    database: Database,
    run_id: str,
    *,
    command: str,
    actor: str,
    sparing_claim: str | None = None,
) -> tuple[WithdrawnSubmission, ...]:
    """Settle what has lapsed in a run, before a writer's own work -- **the** entry point.

    Every writer touching the run calls this, and nothing else, first: claim, release,
    record, set_status, finalize, serving, and (Tasks 5 and 6) submit and batch.

    Its own short transaction, opened only when something has lapsed, rather than the
    caller's: what it settles is true whether or not the caller's work then succeeds -- a
    command refused for its own reasons must not leave an exhausted submission holding its
    dimension -- and a verdict planned after it must see the submission as it now is.
    DuckDB forbids nested transactions, so it runs before the caller opens one.

    The audit entry is written in the same transaction, under the caller's command, and
    the withdrawals are returned so the caller's report -- or its refusal -- names them: a
    learner's answer leaving the record is never something a command did silently.
    """

    if not lapsed_submissions(database, run_id=run_id, sparing_claim=sparing_claim):
        return ()
    with database.transaction() as transaction:
        withdrawn = tuple(
            _withdrawn(
                transaction, _settle_lapsed(transaction, run_id, sparing_claim=sparing_claim)
            )
        )
        if withdrawn:
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                actor=actor,
                affected_records_json=json.dumps(
                    sorted([run_id, *(entry.submission_id for entry in withdrawn)])
                ),
                after_summary=(
                    f"withdrew {len(withdrawn)} submission(s) whose judging attempts lapsed "
                    f"({EXHAUSTED_CODE}): " + ", ".join(entry.submission_id for entry in withdrawn)
                ),
            )
    return withdrawn


def settled_warnings(withdrawn: tuple[WithdrawnSubmission, ...]) -> tuple[str, ...]:
    """What a report says of submissions a sweep withdrew before the command's own work."""

    return tuple(
        f"withdrew submission {entry.submission_id} ({entry.content_id}) as {entry.code}: "
        f"{entry.reason}"
        for entry in withdrawn
    )


def with_settled(
    failure: LinguaWikiError, withdrawn: tuple[WithdrawnSubmission, ...]
) -> LinguaWikiError:
    """The refusal a command raised, naming what its sweep had already withdrawn.

    The sweep committed before the command refused, so the refusal is the only account the
    caller gets of it. Same code and message: the refusal is still about what it was about.
    """

    payload = failure.payload
    return LinguaWikiError(
        payload.code,
        payload.message,
        retryable=payload.retryable,
        details=(
            *payload.details,
            *(
                ErrorDetail(
                    field="settled",
                    reason="withdrawn before this command's own work",
                    context={
                        "submission_id": entry.submission_id,
                        "content_id": entry.content_id,
                        "code": entry.code,
                        "reason": entry.reason,
                    },
                )
                for entry in withdrawn
            ),
        ),
    )


class WithdrawnSubmission(ContractModel):
    """A submission a judging command withdrew, and why."""

    submission_id: str
    content_id: str
    code: str
    reason: str
    voided_verdicts: tuple[str, ...] = ()


#: Codes the system withdraws with by itself -- a purge, a lapse, a consent change, a run
#: closing. A judge giving up names its own reason; borrowing one of these would make a
#: judge's decision read as a privacy or lifecycle event that never happened.
RESERVED_RELEASE_CODES: frozenset[str] = frozenset(
    {
        "assessment_audio_purged",
        "assessment_judging_exhausted",
        "assessment_response_not_retained",
    }
)
RESERVED_RELEASE_PREFIXES: tuple[str, ...] = ("assessment_run_", "assessment_consent")

#: What a judge may use instead, named in the refusal.
JUDGE_CODE_HINT = (
    "a code of your own naming why the answer cannot be judged, such as "
    "assessment_audio_unintelligible or assessment_response_off_task"
)


def reserved_code(code: str) -> bool:
    return code in RESERVED_RELEASE_CODES or code.startswith(RESERVED_RELEASE_PREFIXES)


# --- reports -------------------------------------------------------------------------------


class ClaimedJudgement(recording_service.PendingJudgement):
    """What `assessment pending` says of an entry, plus the claim it was handed out under.

    `attempts`, `claimed_by`, and `lease_expires_at` describe the state *after* this
    claim: it is the live lease, and it is one of the attempts.
    """

    claim_id: str


class ClaimReport(ContractModel):
    run_id: str
    track_id: str
    judge: str
    lease_seconds: int
    policy_version: str
    max_attempts: int
    claimed: tuple[ClaimedJudgement, ...] = ()
    #: Submissions this call withdrew rather than hand out: an unjudgeable recording, or one
    #: whose every attempt had lapsed.
    withdrawn: tuple[WithdrawnSubmission, ...] = ()
    #: Pending submissions left unclaimed: another judge's lease is live, a verdict is held
    #: for them, or `--limit` was reached.
    waiting: int = 0
    warnings: tuple[str, ...] = ()


class ReleaseReport(ContractModel):
    claim_id: str
    submission_id: str
    run_id: str
    judge: str
    terminal: bool
    code: str | None = None
    reason: str
    released_at: str
    #: The submission's status after the release: `pending` (back in the queue), or
    #: `withdrawn` by a terminal release or by running out of attempts.
    submission_status: str
    #: True when another judge may claim it now.
    returned_to_queue: bool
    attempts: int
    max_attempts: int
    #: True when this call found the identical release already recorded and replayed it.
    replayed: bool = False
    withdrawn: tuple[WithdrawnSubmission, ...] = ()
    warnings: tuple[str, ...] = ()


def _withdrawn(
    database: Database, outcomes: tuple[SettlementOutcome, ...]
) -> list[WithdrawnSubmission]:
    reported = []
    for outcome in outcomes:
        if not outcome.withdrawn and not outcome.voided_verdicts:
            continue
        row = database.one(
            "SELECT content_id, withdrawn_code, withdrawn_reason FROM assessment_submissions "
            "WHERE submission_id = ?",
            [outcome.submission_id],
        )
        if row is None:
            continue
        reported.append(
            WithdrawnSubmission(
                submission_id=outcome.submission_id,
                content_id=str(row[0]),
                code=str(row[1]),
                reason=str(row[2]),
                voided_verdicts=outcome.voided_verdicts,
            )
        )
    return reported


# --- claim ---------------------------------------------------------------------------------


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def claim(
    paths: WorkspacePaths,
    *,
    judge: str,
    run: str | None = None,
    track: str | None = None,
    lease_seconds: int | None = None,
    limit: int | None = None,
    clock: Clock | None = None,
    command: str = CLAIM_COMMAND,
    actor: str = assessment_service.DEFAULT_ACTOR,
) -> ClaimReport:
    """Hand pending submissions to a judge, each under a lease, in one short transaction.

    Only a judgeable `pending` submission with no live lease, no verdict held for it, and
    attempts left is claimable. The connection closes when this returns: the judge holds
    nothing while it judges, so every other writer goes on working.

    An **unjudgeable** recording is not handed out. `pending` is a reader and only lists
    it with its reason; `claim` is a writer, so it withdraws it -- through
    `withdrawal.settle`, with the recording failure's own code -- and reports it in
    `withdrawn`. Handing it out would send a judge to fail on it, and leaving it would let
    it hold its dimension for a verdict that can never land. A problem that is not about the
    recording itself is left alone and reported as a warning: it is not this command's to
    settle.

    Not idempotent, deliberately: a claim whose response was lost is an attempt nobody
    acts on, and its lease running out returns the submission to the queue -- the same
    outcome a crashed judge gets.
    """

    if _blank(judge):
        raise LinguaWikiError(
            "invalid_arguments",
            "a claim names the judge holding it",
            details=(ErrorDetail(field="judge", reason="blank"),),
        )
    lease = JUDGING_POLICY.default_lease_seconds if lease_seconds is None else lease_seconds
    if not 1 <= lease <= MAXIMUM_LEASE_SECONDS:
        raise LinguaWikiError(
            "invalid_arguments",
            f"a lease runs between 1 and {MAXIMUM_LEASE_SECONDS} seconds",
            details=(ErrorDetail(field="lease", reason=str(lease)),),
        )
    if limit is not None and limit < 1:
        raise LinguaWikiError(
            "invalid_arguments",
            "a limit asks for at least one submission",
            details=(ErrorDetail(field="limit", reason=str(limit)),),
        )
    with open_writer(paths, command=command, clock=clock or SystemClock()) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = assessment_service.resolve_run(database, run, track_id=track_id)
        run_row = database.one(
            "SELECT track_id, status FROM assessment_runs WHERE run_id = ?", [run_id]
        )
        assert run_row is not None
        run_track, status = str(run_row[0]), str(run_row[1])
        # A paused run is judged too: its verdicts are held until it resumes. A closed one
        # takes no verdict, so nothing is handed out for it.
        if status not in assessment_service.RESUMABLE_STATUSES:
            assessment_service.assert_running(run_id, status=status, action="hand work to a judge")
        # Audited and reported by the sweep's own transaction; this command's audit entry
        # below names only what this command's transaction wrote.
        lapsed = sweep_lapsed(database, run_id, command=command, actor=actor)
        entries = recording_service.pending_entries(database, paths.root, run_id)
        unjudgeable = [
            entry
            for entry in entries
            if not entry.judgeable and entry.problem_code in recording_service.RECORDING_FAILURES
        ]
        claimable = [
            entry
            for entry in entries
            if entry.judgeable
            and entry.claimed_by is None
            and not entry.verdict_held
            and entry.attempts < JUDGING_POLICY.max_attempts
        ]
        chosen = claimable if limit is None else claimable[:limit]
        warnings = [
            f"{entry.submission.submission_id} is not handed out: {entry.problem}"
            for entry in entries
            if not entry.judgeable and entry not in unjudgeable
        ]
        withdrawn = list(lapsed)
        claimed: list[ClaimedJudgement] = []
        if unjudgeable or chosen:
            with database.transaction() as transaction:
                settled = tuple(
                    settle(
                        transaction,
                        Settlement(
                            submission_id=entry.submission.submission_id,
                            code=str(entry.problem_code),
                            reason=str(entry.problem),
                        ),
                    )
                    for entry in unjudgeable
                )
                unheard = _withdrawn(transaction, settled)
                withdrawn.extend(unheard)
                now = transaction.now()
                expires = now + timedelta(seconds=lease)
                for entry in chosen:
                    claim_id = str(AssessmentId.new())
                    transaction.execute(
                        "INSERT INTO judging_claims (claim_id, submission_id, judge, "
                        "claimed_at, lease_expires_at) VALUES (?, ?, ?, ?, ?)",
                        [claim_id, entry.submission.submission_id, judge, now, expires],
                    )
                    # The entry as `pending` described it, now with this claim as its
                    # live lease and one of its attempts.
                    described = entry.model_dump(exclude={"submission", "task"})
                    described.update(
                        attempts=entry.attempts + 1,
                        claimed_by=judge,
                        lease_expires_at=aware_utc(expires).isoformat(),
                    )
                    claimed.append(
                        ClaimedJudgement(
                            submission=entry.submission,
                            task=entry.task,
                            claim_id=claim_id,
                            **described,
                        )
                    )
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    actor=actor,
                    affected_records_json=json.dumps(
                        sorted(
                            [run_id]
                            + [entry.claim_id for entry in claimed]
                            + [entry.submission_id for entry in unheard]
                        )
                    ),
                    after_summary=(
                        f"{judge} claimed {len(claimed)} submission(s) for {lease}s; "
                        f"{len(unheard)} unjudgeable withdrawn"
                    ),
                )
        return ClaimReport(
            run_id=run_id,
            track_id=run_track,
            judge=judge,
            lease_seconds=lease,
            policy_version=JUDGING_POLICY.version,
            max_attempts=JUDGING_POLICY.max_attempts,
            claimed=tuple(claimed),
            withdrawn=tuple(withdrawn),
            waiting=len(entries) - len(unjudgeable) - len(chosen) - len(warnings),
            warnings=tuple(warnings),
        )


# --- release -------------------------------------------------------------------------------


def _claim_row(database: Database, claim_id: str) -> tuple[str, str, str]:
    """`(submission_id, run_id, judge)` for a claim, refused by name when unknown."""

    row = database.one(
        "SELECT claim.submission_id, submission.run_id, claim.judge FROM judging_claims claim "
        "LEFT JOIN assessment_submissions submission "
        "ON submission.submission_id = claim.submission_id WHERE claim.claim_id = ?",
        [claim_id],
    )
    if row is None:
        raise LinguaWikiError(
            "assessment_claim_not_found",
            f"no judging claim {claim_id} in this workspace",
            details=(ErrorDetail(field="claim", reason="unknown claim"),),
        )
    if row[1] is None:
        raise LinguaWikiError(
            "assessment_claim_not_found",
            f"judging claim {claim_id} names submission {row[0]}, which this workspace does "
            "not hold; `db check` reports it",
            details=(ErrorDetail(field="claim", reason="its submission is missing"),),
        )
    return str(row[0]), str(row[1]), str(row[2])


def released_refusal(claim_id: str, row: tuple[object, ...]) -> LinguaWikiError:
    """The refusal for work under a claim that was already released -- a release asked
    again differently, or a verdict delivered under it. `row` is
    `(terminal, code, reason, released_at)` from `judging_releases`."""

    terminal, code, reason, released_at = row
    how = f"terminally ({code})" if terminal else "back to the queue"
    when = (
        aware_utc(released_at).isoformat()
        if isinstance(released_at, datetime)
        else str(released_at)
    )
    return LinguaWikiError(
        "assessment_claim_released",
        f"claim {claim_id} was already released {how}: {reason}. A released claim takes no "
        "verdict and no second release; claim the submission again if it is still pending",
        details=(
            ErrorDetail(
                field="claim",
                reason="released",
                context={
                    "terminal": "true" if terminal else "false",
                    "code": "" if code is None else str(code),
                    "reason": str(reason),
                    "released_at": when,
                },
            ),
        ),
    )


def assert_claim_not_released(database: Database, claim_id: str) -> None:
    """Refuse work under a claim that a release has ended; unknown claims are someone
    else's question."""

    row = database.one(
        "SELECT terminal, code, reason, released_at FROM judging_releases WHERE claim_id = ?",
        [claim_id],
    )
    if row is not None:
        raise released_refusal(claim_id, tuple(row))


def _release_report(
    database: Database,
    claim_id: str,
    *,
    replayed: bool,
    withdrawn: list[WithdrawnSubmission],
    warnings: list[str],
) -> ReleaseReport:
    submission_id, run_id, judge = _claim_row(database, claim_id)
    release_row = database.one(
        "SELECT terminal, code, reason, released_at FROM judging_releases WHERE claim_id = ?",
        [claim_id],
    )
    assert release_row is not None
    status = str(
        database.scalar(
            "SELECT status FROM assessment_submissions WHERE submission_id = ?", [submission_id]
        )
    )
    state = claim_states(database, run_id).get(submission_id, UNCLAIMED)
    return ReleaseReport(
        claim_id=claim_id,
        submission_id=submission_id,
        run_id=run_id,
        judge=judge,
        terminal=bool(release_row[0]),
        code=None if release_row[1] is None else str(release_row[1]),
        reason=str(release_row[2]),
        released_at=aware_utc(release_row[3]).isoformat(),
        submission_status=status,
        returned_to_queue=status == "pending"
        and state.live_claim is None
        and state.attempts < JUDGING_POLICY.max_attempts,
        attempts=state.attempts,
        max_attempts=JUDGING_POLICY.max_attempts,
        replayed=replayed,
        withdrawn=tuple(withdrawn),
        warnings=tuple(warnings),
    )


def release(
    paths: WorkspacePaths,
    *,
    claim: str,
    reason: str,
    terminal: bool = False,
    code: str | None = None,
    clock: Clock | None = None,
    command: str = RELEASE_COMMAND,
    actor: str = assessment_service.DEFAULT_ACTOR,
) -> ReleaseReport:
    """End a claim with no verdict: give the submission back, or give up on it.

    Non-terminal (the judge crashed, timed out, or cannot judge it now) returns the
    submission to the queue -- the same thing an expired lease does with no write -- and
    the attempt counts. If it was the last attempt the policy allows, the submission is
    withdrawn `assessment_judging_exhausted` in the same transaction, because nobody may
    claim it again and it would otherwise hold its dimension.

    Terminal (`--terminal --code`) withdraws the submission with the judge's code and
    reason, through `withdrawal.settle`: task skipped, dimension unblocked.

    A claim ends once. Releasing one that already ended is refused by how it ended -- a
    verdict (`assessment_claim_judged`), an earlier release asked differently
    (`assessment_claim_released`), or the submission no longer waiting
    (`assessment_claim_ended`) -- and the identical release again is a retry and replays,
    keyed by the claim itself.
    """

    if _blank(reason):
        raise LinguaWikiError(
            "invalid_arguments",
            "a release says why the claim ended",
            details=(ErrorDetail(field="reason", reason="blank"),),
        )
    if terminal and _blank(code):
        raise LinguaWikiError(
            "invalid_arguments",
            "a terminal release withdraws the submission, and names the code it is withdrawn "
            "with; pass --code",
            details=(ErrorDetail(field="code", reason="absent"),),
        )
    if terminal and code is not None and reserved_code(code):
        raise LinguaWikiError(
            "assessment_release_code_reserved",
            f"{code} is a code the system withdraws with by itself (a purge, a lapse, a "
            "consent change, or the run closing), and a judge giving up is none of those; use "
            + JUDGE_CODE_HINT,
            details=(ErrorDetail(field="code", reason="reserved", context={"code": code}),),
        )
    if not terminal and code is not None:
        raise LinguaWikiError(
            "invalid_arguments",
            "only a terminal release withdraws the submission with a code; a release that "
            "returns it to the queue takes only a reason. Pass --terminal to give up on it",
            details=(ErrorDetail(field="code", reason="not terminal"),),
        )
    with open_writer(paths, command=command, clock=clock or SystemClock()) as database:
        submission_id, run_id, _judge = _claim_row(database, claim)
        lapsed = sweep_lapsed(database, run_id, command=command, actor=actor, sparing_claim=claim)
        withdrawn = list(lapsed)

        def work() -> ReleaseReport:
            recorded = database.one(
                "SELECT terminal, code, reason, released_at FROM judging_releases "
                "WHERE claim_id = ?",
                [claim],
            )
            if recorded is not None:
                if (bool(recorded[0]), recorded[1], str(recorded[2])) == (terminal, code, reason):
                    return _release_report(
                        database, claim, replayed=True, withdrawn=withdrawn, warnings=[]
                    )
                raise released_refusal(claim, tuple(recorded))
            verdict = database.one(
                "SELECT verdict.verdict_id, outcome.outcome FROM assessment_verdicts verdict "
                "LEFT JOIN assessment_verdict_outcomes outcome "
                "ON outcome.verdict_id = verdict.verdict_id WHERE verdict.claim_id = ? "
                "ORDER BY verdict.received_at LIMIT 1",
                [claim],
            )
            if verdict is not None:
                outcome = "held" if verdict[1] is None else str(verdict[1])
                raise LinguaWikiError(
                    "assessment_claim_judged",
                    f"claim {claim} already delivered verdict {verdict[0]} ({outcome}); a "
                    "claim ends once, and this one ended with its verdict, so there is nothing "
                    "to release",
                    details=(
                        ErrorDetail(
                            field="claim",
                            reason="judged",
                            context={"verdict": str(verdict[0]), "outcome": outcome},
                        ),
                    ),
                )
            submission = database.one(
                "SELECT status, withdrawn_code, superseded_by FROM assessment_submissions "
                "WHERE submission_id = ?",
                [submission_id],
            )
            assert submission is not None
            standing = submission_id in held_submissions(database, run_id)
            if str(submission[0]) != "pending" or standing:
                # Something other than this claim settled the submission: another claim's
                # verdict, a supersession, a purge, a lapse. Recording a release now would put
                # a terminal code on a submission it never withdrew.
                state = "judged, verdict held" if standing else str(submission[0])
                context = {"submission_status": state}
                if submission[1] is not None:
                    context["code"] = str(submission[1])
                if submission[2] is not None:
                    context["successor"] = str(submission[2])
                raise LinguaWikiError(
                    "assessment_claim_ended",
                    f"claim {claim} is for submission {submission_id}, which is no longer waiting "
                    f"for a judge ({state}); there is nothing to release, and `assessment pending` "
                    "lists what is",
                    details=(ErrorDetail(field="claim", reason="ended", context=context),),
                )
            with database.transaction() as transaction:
                transaction.execute(
                    "INSERT INTO judging_releases (claim_id, released_at, terminal, code, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [claim, transaction.now(), terminal, code, reason],
                )
                settled: tuple[SettlementOutcome, ...] = ()
                if terminal:
                    assert code is not None
                    settled = (
                        settle(
                            transaction,
                            Settlement(submission_id=submission_id, code=code, reason=reason),
                        ),
                    )
                # After the release, for the case the release itself creates: this was the
                # last attempt the policy allows, so nobody may claim the submission again.
                settled += _settle_lapsed(transaction, run_id)
                released = _withdrawn(transaction, settled)
                withdrawn.extend(released)
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    actor=actor,
                    affected_records_json=json.dumps(
                        sorted(
                            {
                                claim,
                                submission_id,
                                run_id,
                                *(entry.submission_id for entry in released),
                            }
                        )
                    ),
                    after_summary=(
                        f"released claim {claim} terminally ({code})"
                        if terminal
                        else f"released claim {claim} back to the queue"
                    ),
                )
            return _release_report(
                database, claim, replayed=False, withdrawn=withdrawn, warnings=[]
            )

        try:
            return work()
        except LinguaWikiError as failure:
            # The sweep committed before this refusal; the refusal is the only account
            # the caller gets of it.
            raise with_settled(failure, lapsed) from failure


__all__ = [
    "CLAIM_COMMAND",
    "EXHAUSTED_CODE",
    "JUDGE_CODE_HINT",
    "JUDGING_POLICY",
    "MAXIMUM_LEASE_SECONDS",
    "RELEASE_COMMAND",
    "RESERVED_RELEASE_CODES",
    "RESERVED_RELEASE_PREFIXES",
    "UNCLAIMED",
    "ClaimReport",
    "ClaimState",
    "ClaimedJudgement",
    "JudgingPolicy",
    "Lapsed",
    "ReleaseReport",
    "WithdrawnSubmission",
    "assert_claim_not_released",
    "claim",
    "claim_states",
    "exhausted_reason",
    "held_submissions",
    "lapsed_submissions",
    "release",
    "released_refusal",
    "reserved_code",
    "settled_warnings",
    "sweep_lapsed",
    "with_settled",
]
