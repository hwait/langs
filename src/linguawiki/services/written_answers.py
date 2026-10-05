"""A written answer handed in for a judge: the text counterpart of a capture.

Until C6 the only way to hand in a judged task was `record`, which needs the score a judge
reached -- so a page with no judge could not take a learner's writing at all. `submit`
takes the answer and nothing else: the task stays `served`, the screen reports it
`awaiting-judge`, and a judge reads it later through `pending` and `claim` exactly as it
hears a recording. Registration does not score.

**Eligibility comes first and is a predicate.** A written task is judged from the whole
answer, so it is taken only where `placement.written_judging_permitted` holds -- the track
keeps a written answer whole. An excerpt-only track (the default, `EXCERPT_LIMIT`
characters) would have long writing judged on a fragment, so it is refused here, and a
`machine+judged` run never serves it the task in the first place.

**Retention is applied before anything is stored.** The answer goes through
`evidence.retain_response`, and the row holds what that keeps -- here, given eligibility,
the whole text and its digest. Nothing else gets a copy: the domain event carries the
digest, never the words, because an event is never edited.

**Identity is the producer's.** The page mints a `submission_key` per answer, as it mints a
capture ID per recording, and it lands in the same `capture_id` column under the same
unique index, so a resent answer is found with no new mechanism. The same key for the same
`(run, task, answer)` replays; the same key for anything else is `idempotency_conflict`,
naming the digest that key already recorded. A second, different answer to a task already
answered in writing is `assessment_task_already_submitted`: a typed answer is handed in
deliberately, unlike a retaken recording, and one live text per task means no text is ever
cleared in place to make room for another.
"""

from __future__ import annotations

import hashlib
import json

from linguawiki import idempotency
from linguawiki.clock import Clock, SystemClock
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import AssessmentId, EventId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import judged_written_task, written_judging_permitted
from linguawiki.services import assessment as assessment_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import judging
from linguawiki.services import learners as learner_service
from linguawiki.services import recordings as recording_service
from linguawiki.services.recordings import SubmissionReport

SUBMIT_COMMAND = "assessment.submit"
#: The event a written answer's arrival records. Keyed by the submission key, so that a key
#: another operation already used is refused by `idempotency.resolve` rather than by the
#: unique index on the event table.
SUBMITTED_EVENT = "assessment.submitted"
#: The most a written answer may be. A placement answer is a paragraph or two; anything
#: near this is not an answer, and the row holding it is read by every judge that claims it.
MAXIMUM_WRITTEN_ANSWER_CHARACTERS = 20_000
#: The most a submission key may be. It is an identifier, not content.
MAXIMUM_SUBMISSION_KEY_CHARACTERS = 200


class WrittenSubmissionReport(ContractModel):
    """What became of one written answer. Never its text: the caller sent it."""

    run_id: str
    content_id: str
    submission: SubmissionReport
    #: True when this key had already handed in this answer and the call was a retry.
    replayed: bool = False
    warnings: tuple[str, ...] = ()


def _assert_key(submission_key: str) -> None:
    if not submission_key.strip():
        raise LinguaWikiError(
            "invalid_arguments",
            "a written answer needs a submission key: the identifier its producer minted "
            "for it, which a retry sends again",
            details=(ErrorDetail(field="submission_key", reason="blank"),),
        )
    if len(submission_key) > MAXIMUM_SUBMISSION_KEY_CHARACTERS:
        raise LinguaWikiError(
            "invalid_arguments",
            f"a submission key may be at most {MAXIMUM_SUBMISSION_KEY_CHARACTERS} characters",
            details=(ErrorDetail(field="submission_key", reason="too long"),),
        )


def _assert_response(response: str) -> None:
    # Before anything is read or written. `min_length=1` would accept "   ", which the
    # table's CHECK refuses -- after the writer is held, as a raw constraint error.
    if not response.strip():
        raise LinguaWikiError(
            "invalid_arguments",
            "an empty answer is a skip, not an answer to judge; skip the task instead",
            details=(ErrorDetail(field="response", reason="blank"),),
        )
    if len(response) > MAXIMUM_WRITTEN_ANSWER_CHARACTERS:
        raise LinguaWikiError(
            "assessment_response_too_long",
            f"a written answer may be at most {MAXIMUM_WRITTEN_ANSWER_CHARACTERS} characters, "
            f"and this is {len(response)}",
            details=(ErrorDetail(field="response", reason="over the cap"),),
        )


def submit(
    paths: WorkspacePaths,
    *,
    content_id: str,
    submission_key: str,
    response: str,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = SUBMIT_COMMAND,
    actor: str = assessment_service.DEFAULT_ACTOR,
) -> WrittenSubmissionReport:
    """Hand in the learner's written answer to a served task, for a judge.

    One writer: the lapsed-judging sweep every writer runs first, then a preflight that is
    only reads -- the task served in this run, a judged written task, not already answered,
    the track still eligible -- then retention, then one transaction inserting the
    submission with its audit entry and event. Every refusal comes before that transaction.
    """

    _assert_key(submission_key)
    _assert_response(response)
    digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
    with open_writer(paths, command=command, clock=clock or SystemClock()) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = assessment_service.resolve_run(database, run, track_id=track_id)
        settled = judging.sweep_lapsed(database, run_id, command=command, actor=actor)
        try:
            report = _submit_in_writer(
                database,
                run_id=run_id,
                content_id=content_id,
                submission_key=submission_key,
                response=response,
                digest=digest,
                command=command,
                actor=actor,
            )
        except LinguaWikiError as failure:
            if not settled:
                raise
            raise judging.with_settled(failure, settled) from failure
        if not settled:
            return report
        return report.model_copy(
            update={"warnings": (*report.warnings, *judging.settled_warnings(settled))}
        )


def _replay_or_conflict(
    existing: SubmissionReport,
    *,
    submission_key: str,
    run_id: str,
    content_id: str,
    digest: str,
) -> WrittenSubmissionReport:
    """The answer this key already handed in, if this call is a retry of it; refused if not.

    Compared by `(kind, run, task, digest)` -- the request this key was bound to -- and
    *before* anything about the run's current state, so a retry after the run closed or the
    answer was judged still gets what the first call got.
    """

    recorded = (existing.kind, existing.run_id, existing.content_id, existing.response_digest)
    if recorded == ("text", run_id, content_id, digest):
        return WrittenSubmissionReport(
            run_id=run_id,
            content_id=content_id,
            submission=existing,
            replayed=True,
            warnings=("this answer was already received; the recorded submission is returned",),
        )
    what = (
        f"a written answer to {existing.content_id} in run {existing.run_id} whose text "
        f"hashes to {existing.response_digest}"
        if existing.kind == "text"
        else f"a recording of {existing.content_id} in run {existing.run_id}"
    )
    raise LinguaWikiError(
        "idempotency_conflict",
        f"submission key {submission_key} already handed in {what}; a retry resends the same "
        "answer, and a different answer needs a new key",
        details=(
            ErrorDetail(
                field="submission_key",
                reason="recorded with different content",
                context={
                    "submission_id": existing.submission_id,
                    "kind": existing.kind,
                    "run_id": existing.run_id,
                    "content_id": existing.content_id,
                    "response_digest": str(existing.response_digest),
                },
            ),
        ),
    )


def _submit_in_writer(
    database: Database,
    *,
    run_id: str,
    content_id: str,
    submission_key: str,
    response: str,
    digest: str,
    command: str,
    actor: str,
) -> WrittenSubmissionReport:
    existing = recording_service.submission_by_key(database, submission_key)
    if existing is not None:
        return _replay_or_conflict(
            existing,
            submission_key=submission_key,
            run_id=run_id,
            content_id=content_id,
            digest=digest,
        )
    # The request this key is bound to: the answer by its digest, never by its words, since
    # the hash is stored in an event nothing ever edits.
    fingerprint = idempotency.request_hash(
        operation=SUBMITTED_EVENT,
        run_id=run_id,
        content_id=content_id,
        kind="text",
        response=digest,
    )
    if (
        idempotency.resolve(
            database, key=submission_key, event_type=SUBMITTED_EVENT, request_hash=fingerprint
        )
        is not None
    ):
        # The event says this key handed this answer in, and no submission carries the key:
        # a hand repair or a partial restore. Replaying would return nothing; inserting
        # would collide with the event's key.
        raise LinguaWikiError(
            "idempotency_conflict",
            f"submission key {submission_key} is recorded as having handed in this answer, and "
            "no submission carries it now; `db check` names what is damaged",
            details=(ErrorDetail(field="submission_key", reason="recorded, and missing"),),
        )
    run_row = database.one(
        "SELECT track_id, status FROM assessment_runs WHERE run_id = ?", [run_id]
    )
    assert run_row is not None  # `resolve_run` found it, in this writer
    track_id = str(run_row[0])
    assessment_service.assert_running(
        run_id, status=str(run_row[1]), action="take a written answer"
    )
    # Before the task's own status: once judged or handed in, the learner's answer was
    # taken, and that is the reason a caller can act on.
    live = recording_service.live_submission(database, run_id, content_id)
    if live is not None and live.status == "judged":
        raise LinguaWikiError(
            "assessment_task_already_judged",
            f"{content_id} was already judged; the learner's answer was taken",
            details=(ErrorDetail(field="content_id", reason=live.submission_id),),
        )
    if live is not None:
        raise LinguaWikiError(
            "assessment_task_already_submitted",
            f"{content_id} already has an answer waiting for a judge ({live.submission_id}); "
            "a written answer is handed in once, and resending that one needs the key it "
            "was sent with",
            details=(
                ErrorDetail(
                    field="content_id",
                    reason="already submitted",
                    context={
                        "submission_id": live.submission_id,
                        "response_digest": str(live.response_digest),
                    },
                ),
            ),
        )
    shown = assessment_service.served_task_report(database, run_id, content_id=content_id)
    if shown.status != "served":
        raise LinguaWikiError(
            "assessment_task_settled",
            f"{content_id} is {shown.status}, so it no longer takes an answer",
            details=(ErrorDetail(field="content_id", reason=f"task is {shown.status}"),),
        )
    if not judged_written_task(modality=shown.modality, task_type=shown.task_type):
        raise LinguaWikiError(
            "assessment_task_not_written",
            f"{content_id} is a {shown.modality} {shown.task_type} task, and a written "
            "submission answers only a written task a judge scores; a machine-scorable "
            "answer is recorded, and a spoken one is captured",
            details=(ErrorDetail(field="content_id", reason=shown.task_type),),
        )
    preferences = learner_service.track_context(database, track_id).preferences
    if not written_judging_permitted(preferences):
        raise LinguaWikiError(
            "transcript_consent_required",
            f"{content_id} is judged from the whole answer, and this track keeps at most an "
            "excerpt of what a learner writes; set transcript_retention_consent to true in the "
            "track's preferences (`linguawiki track update --input`) to hand written answers "
            "in for judging",
            details=(ErrorDetail(field="track", reason="written answers are not kept whole"),),
        )
    # Retention before storage, and here the predicate above means it keeps the whole
    # answer. Asked for `full` explicitly, so that the rule refuses rather than quietly
    # keeping an excerpt if the predicate and the rule ever came apart.
    visibility, kept, retained_digest = evidence_service.retain_response(
        response, requested="full", preferences=preferences
    )
    if visibility != "full" or kept is None or retained_digest != digest:
        raise AssertionError("written_judging_permitted held, and retention did not keep it whole")
    submission_id = str(AssessmentId.new())
    with database.transaction() as transaction:
        now = transaction.now()
        transaction.execute(
            "INSERT INTO assessment_submissions (submission_id, run_id, content_id, kind, "
            "capture_id, artifact_id, response_visibility, response_text, response_digest, "
            "status, superseded_by, withdrawn_code, withdrawn_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, 'text', ?, NULL, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?)",
            [
                submission_id,
                run_id,
                content_id,
                submission_key,
                visibility,
                kept,
                retained_digest,
                now,
                now,
            ],
        )
        correlation_id = EventId.new()
        migration_module.record_audit_entry(
            transaction,
            command=command,
            correlation_id=correlation_id,
            outcome="succeeded",
            actor=actor,
            affected_records_json=json.dumps([run_id, content_id, submission_id], sort_keys=True),
            after_summary=(
                f"took a written answer of {len(response)} character(s) to {content_id} for a judge"
            ),
        )
        migration_module.record_domain_event(
            transaction,
            event_type=SUBMITTED_EVENT,
            aggregate_type="assessment_run",
            aggregate_id=run_id,
            correlation_id=correlation_id,
            payload_json=idempotency.payload(
                fingerprint, content_id=content_id, submission_id=submission_id
            ),
            idempotency_key=submission_key,
        )
    stored = recording_service.submission_by_key(database, submission_key)
    assert stored is not None
    return WrittenSubmissionReport(run_id=run_id, content_id=content_id, submission=stored)


__all__ = [
    "MAXIMUM_SUBMISSION_KEY_CHARACTERS",
    "MAXIMUM_WRITTEN_ANSWER_CHARACTERS",
    "SUBMITTED_EVENT",
    "SUBMIT_COMMAND",
    "WrittenSubmissionReport",
    "submit",
]
