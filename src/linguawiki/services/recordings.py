"""A recording the learner makes in the browser, from its first byte to the judge who hears it.

These bytes are created by the client rather than offered by the learner, and registration
can fail -- the writer is locked, the run has closed, the hash is wrong. A recording written
and then not registered is accounted for by nothing, which is the failure Stage 5 closed for
package ingestion. So a capture is **two-phase, with an owner at every moment**:

1. a `capture_stagings` row is committed first, naming the capture, the served task, the
   staged path, the final path, and the hash the bytes must have;
2. the bytes are written under `staging/captures/`, which is outside the roots
   `artifacts.register` accepts, so a staged file cannot be registered by mistake;
3. the row moves to `promoting` in its own short transaction, so a crash from here on
   leaves a state the next invocation can name;
4. the file is moved into `artifacts/captures/` with `os.replace` -- one filesystem, atomic;
5. the registration, the staging row's move to `registered`, and the submission binding the
   recording to its served task commit **in one transaction**. From that commit the artifact
   row owns the bytes; before it, the staging row does, wherever they are.

`artifacts.register` records that a file exists, without moving it, and it stays that way:
every other caller relies on it registering the path it is given. The move is owned here.

A refusal at any point removes the bytes it can identify -- hashed first, because a file
whose hash no longer matches is not this capture, and deleting what you cannot identify is
the one mistake worse than leaving it. `recover` resolves every row a crash left behind
before `client serve` accepts a request, and `db check` runs the same classification as a
diagnostic that reports and never repairs.

A capture is a learner's own recording, not something a package brought in, so it is
registered with `origin="learner-recording"` and **no** `external_id`. The retention sweep
reads a producer identity as "arrived with a package" and deletes it under
`delete-after-ingestion`; a capture ID there would have the sweep remove browser recordings
under a policy that is not about them. The capture ID lives on the staging row and on the
submission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.db.integrity import CheckResult
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import AssessmentId, EventId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import RECORDED_JUDGED_TASK_TYPES, SPOKEN_MODALITY
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import assessment as assessment_service
from linguawiki.services import learners as learner_service
from linguawiki.services import withdrawal
from linguawiki.services.learners import RecordingPolicy, recording_policy

#: The private top-level directory staged bytes live under, and the part of it captures use.
STAGING_DIRECTORY = "staging"
CAPTURE_STAGING = "staging/captures"
#: Where a promoted capture lives: under an artifact root, one directory per track, so two
#: learners' recordings can never share a path.
CAPTURE_DIRECTORY = "artifacts/captures"
#: The roots a staged path may be read under. `register` accepts only the artifact roots,
#: which is the point: nothing can register a staged file by naming it.
STAGING_ROOTS: tuple[str, ...] = (STAGING_DIRECTORY,)
#: The most one capture may be. A spoken answer is a minute or two of compressed audio;
#: anything near this is not an answer, and the single-threaded server reads it in one go.
MAXIMUM_CAPTURE_BYTES = 16 * 1024 * 1024
#: What a browser records, and the extension each is stored under. A type outside the list
#: is refused rather than stored under a guessed name.
CAPTURE_MEDIA_TYPES: Mapping[str, str] = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
}
#: The client's own identifier for a capture: a v4 UUID, lower case. Matched narrowly
#: because it becomes part of two file names.
CAPTURE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: How long a capture waits out a writer held by another command before abandoning. The
#: learner has already spoken, so a transient refusal is worth waiting for.
TRANSIENT_WAIT_SECONDS = 5.0
#: How long `client serve` waits for the writer before refusing to start over unresolved
#: captures.
RECOVERY_WAIT_SECONDS = 10.0
CAPTURE_COMMAND = "assessment.capture"
RECOVERY_COMMAND = "client.recover"

STAGED = "staged"
PROMOTING = "promoting"
REGISTERED = "registered"
REFUSED = "refused"


# --- consent -----------------------------------------------------------------------------


def track_recording_policy(database: Database, track_id: str) -> RecordingPolicy:
    return learner_service.track_recording_policy(database, track_id)


# --- reports -----------------------------------------------------------------------------


class SubmissionReport(ContractModel):
    """Which recording answers which served task, and where its judgement stands."""

    submission_id: str
    run_id: str
    content_id: str
    capture_id: str
    artifact_id: str
    status: str
    superseded_by: str | None = None
    withdrawn_code: str | None = None
    withdrawn_reason: str | None = None
    created_at: str


class CaptureReport(ContractModel):
    """What became of one capture."""

    capture_id: str
    run_id: str
    content_id: str
    state: str
    sha256: str
    artifact_id: str | None = None
    submission: SubmissionReport | None = None
    #: The earlier submissions for the same task this capture replaced. Their recordings
    #: were purged in the same transaction: evidence of nothing, and keeping them would be
    #: retention nobody asked for.
    superseded: tuple[str, ...] = ()
    #: True when this capture had already been registered and the upload was a retry.
    replayed: bool = False
    recording: RecordingPolicy
    warnings: tuple[str, ...] = ()


_SUBMISSION_COLUMNS = (
    "submission_id, run_id, content_id, capture_id, artifact_id, status, superseded_by, "
    "withdrawn_code, withdrawn_reason, created_at"
)


def _submission_from(row: tuple[Any, ...]) -> SubmissionReport:
    return SubmissionReport(
        submission_id=str(row[0]),
        run_id=str(row[1]),
        content_id=str(row[2]),
        capture_id=str(row[3]),
        artifact_id=str(row[4]),
        status=str(row[5]),
        superseded_by=None if row[6] is None else str(row[6]),
        withdrawn_code=None if row[7] is None else str(row[7]),
        withdrawn_reason=None if row[8] is None else str(row[8]),
        created_at=aware_utc(row[9]).isoformat(),
    )


def live_submission(database: Database, run_id: str, content_id: str) -> SubmissionReport | None:
    """The submission that answers this served task now: pending or judged, at most one."""

    row = database.one(
        f"SELECT {_SUBMISSION_COLUMNS} FROM assessment_submissions "
        "WHERE run_id = ? AND content_id = ? AND status IN ('pending', 'judged') "
        "ORDER BY created_at DESC, submission_id DESC LIMIT 1",
        [run_id, content_id],
    )
    return None if row is None else _submission_from(row)


def latest_submission(database: Database, run_id: str, content_id: str) -> SubmissionReport | None:
    """The most recent submission for a served task, whatever became of it."""

    row = database.one(
        f"SELECT {_SUBMISSION_COLUMNS} FROM assessment_submissions "
        "WHERE run_id = ? AND content_id = ? AND status <> 'superseded' "
        "ORDER BY created_at DESC, submission_id DESC LIMIT 1",
        [run_id, content_id],
    )
    return None if row is None else _submission_from(row)


def _submission_by_capture(database: Database, capture_id: str) -> SubmissionReport | None:
    row = database.one(
        f"SELECT {_SUBMISSION_COLUMNS} FROM assessment_submissions WHERE capture_id = ?",
        [capture_id],
    )
    return None if row is None else _submission_from(row)


# --- the staging row ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Staging:
    capture_id: str
    track_id: str
    run_id: str
    content_id: str
    staged_path: str
    final_path: str
    sha256: str
    byte_size: int
    media_type: str
    state: str
    artifact_id: str | None
    refusal_code: str | None
    refusal_reason: str | None


_STAGING_COLUMNS = (
    "capture_id, track_id, run_id, content_id, staged_path, final_path, sha256, byte_size, "
    "media_type, state, artifact_id, refusal_code, refusal_reason"
)


def _staging_from(row: tuple[Any, ...]) -> _Staging:
    return _Staging(
        capture_id=str(row[0]),
        track_id=str(row[1]),
        run_id=str(row[2]),
        content_id=str(row[3]),
        staged_path=str(row[4]),
        final_path=str(row[5]),
        sha256=str(row[6]),
        byte_size=int(row[7]),
        media_type=str(row[8]),
        state=str(row[9]),
        artifact_id=None if row[10] is None else str(row[10]),
        refusal_code=None if row[11] is None else str(row[11]),
        refusal_reason=None if row[12] is None else str(row[12]),
    )


def _staging(database: Database, capture_id: str) -> _Staging | None:
    row = database.one(
        f"SELECT {_STAGING_COLUMNS} FROM capture_stagings WHERE capture_id = ?", [capture_id]
    )
    return None if row is None else _staging_from(row)


def _unresolved(database: Database) -> list[_Staging]:
    return [
        _staging_from(row)
        for row in database.query(
            f"SELECT {_STAGING_COLUMNS} FROM capture_stagings "
            "WHERE state IN ('staged', 'promoting') ORDER BY created_at, capture_id"
        )
    ]


def _classify_staged(root: Path, relative_path: str) -> tuple[str, Path | None]:
    return artifact_service.classify_path(root, relative_path, roots=STAGING_ROOTS)


def _classify_final(root: Path, relative_path: str) -> tuple[str, Path | None]:
    return artifact_service.classify_path(root, relative_path)


# --- validation --------------------------------------------------------------------------


def _media_type(value: str) -> tuple[str, str]:
    base = value.split(";", 1)[0].strip().lower()
    extension = CAPTURE_MEDIA_TYPES.get(base)
    if extension is None:
        raise LinguaWikiError(
            "capture_media_type_unsupported",
            f"{value!r} is not a recording type this workspace stores; send one of "
            f"{sorted(CAPTURE_MEDIA_TYPES)}",
            details=(ErrorDetail(field="media_type", reason=base or "absent"),),
        )
    return base, extension


def _assert_capture_id(capture_id: str) -> None:
    if CAPTURE_ID.fullmatch(capture_id) is None:
        raise LinguaWikiError(
            "invalid_arguments",
            f"{capture_id!r} is not a capture identifier; the client sends a lower-case UUID",
            details=(ErrorDetail(field="capture_id", reason="not a UUID"),),
        )


def _assert_payload(data: bytes) -> None:
    if not data:
        raise LinguaWikiError(
            "capture_empty",
            "the capture has no bytes; a recording of nothing is not an answer",
            details=(ErrorDetail(field="body", reason="empty"),),
        )
    if len(data) > MAXIMUM_CAPTURE_BYTES:
        raise LinguaWikiError(
            "capture_too_large",
            f"a capture may be at most {MAXIMUM_CAPTURE_BYTES} bytes, and this is {len(data)}",
            details=(ErrorDetail(field="body", reason="over the cap"),),
        )


def _preflight(
    database: Database,
    root: Path,
    *,
    run_id: str,
    content_id: str,
    sha256: str,
    capture_id: str,
    paths_to_claim: tuple[str, ...] = (),
    resuming: bool = False,
) -> str:
    """Every refusal a capture can meet before its first write, as a read. Returns the track.

    `resuming` is for a capture already staged: the learner spoke while the run was being
    worked, so a run paused since then still takes it -- only a closed run cannot. Refusing
    it for a pause would delete an answer the learner gave, and a retry after resuming
    would replay that refusal.

    The same function for a new capture and for a resumed one: a capture recovered after a
    restart is held to the rules in force when it is promoted, not the ones in force when
    the learner spoke -- a run that closed in between cannot be answered.
    """

    run = database.one(
        "SELECT run_id, track_id, status FROM assessment_runs WHERE run_id = ?", [run_id]
    )
    if run is None:
        raise LinguaWikiError(
            "assessment_run_not_found",
            f"no assessment run with ID {run_id}",
            details=(ErrorDetail(field="run", reason="unknown run"),),
        )
    track_id = str(run[1])
    if not (resuming and str(run[2]) in assessment_service.RESUMABLE_STATUSES):
        assessment_service.assert_running(run_id, status=str(run[2]), action="take a recording")
    # Before the task's own status: once judged the task is also settled, and "the learner's
    # answer was taken" is the reason a caller can act on.
    live = live_submission(database, run_id, content_id)
    if live is not None and live.status == "judged":
        raise LinguaWikiError(
            "assessment_task_already_judged",
            f"{content_id} was already judged from recording {live.artifact_id}; the "
            "learner's answer was taken",
            details=(ErrorDetail(field="content_id", reason=live.submission_id),),
        )
    shown = assessment_service.served_task_report(database, run_id, content_id=content_id)
    if shown.status != "served":
        raise LinguaWikiError(
            "assessment_task_settled",
            f"{content_id} is {shown.status}, so it no longer takes an answer",
            details=(ErrorDetail(field="content_id", reason=f"task is {shown.status}"),),
        )
    if shown.modality != SPOKEN_MODALITY or shown.task_type not in RECORDED_JUDGED_TASK_TYPES:
        raise LinguaWikiError(
            "capture_not_spoken",
            f"{content_id} is a {shown.modality} {shown.task_type} task, and a recording "
            "answers only a spoken task a judge scores",
            details=(ErrorDetail(field="content_id", reason=shown.task_type),),
        )
    policy = track_recording_policy(database, track_id)
    if not policy.offered:
        raise LinguaWikiError(
            "capture_not_permitted",
            f"a recording cannot be taken for this track: {policy.reason}",
            details=(ErrorDetail(field="track", reason=str(policy.reason)),),
        )
    twin = database.one(
        "SELECT artifact_id FROM artifacts WHERE track_id = ? AND sha256 = ?", [track_id, sha256]
    )
    if twin is not None:
        # Byte-identical recordings -- a silent clip from a deterministic encoder -- would
        # collide on `(track_id, sha256)`, and binding this capture to the first one's
        # artifact would credit one answer twice.
        raise LinguaWikiError(
            "capture_duplicate_bytes",
            f"these are exactly the bytes of recording {twin[0]}, so they cannot be a new "
            "answer; record again",
            details=(ErrorDetail(field="artifact", reason=str(twin[0])),),
        )
    for relative in paths_to_claim:
        classify = _classify_staged if relative.startswith(CAPTURE_STAGING) else _classify_final
        state, _ = classify(root, relative)
        if state != artifact_service.PATH_ABSENT:
            raise LinguaWikiError(
                "capture_path_occupied",
                f"something is already at {relative}, and nothing here accounts for it; it "
                "was left where it is",
                details=(ErrorDetail(field="capture_id", reason=capture_id),),
            )
    return track_id


# --- refusing, and removing only what is identified ------------------------------------


def _remove_identified(root: Path, row: _Staging) -> list[str]:
    """Remove this capture's bytes wherever the row says they are -- if they are its bytes.

    Returns findings for anything left behind. A file whose hash differs is not this
    capture, and is reported rather than deleted.
    """

    findings: list[str] = []
    for relative, classify in (
        (row.staged_path, _classify_staged),
        (row.final_path, _classify_final),
    ):
        state, absolute = classify(root, relative)
        if state == artifact_service.PATH_ABSENT:
            continue
        if state != artifact_service.PATH_PRESENT or absolute is None:
            findings.append(f"capture {row.capture_id}: {relative} is {state}, and was left")
            continue
        if artifact_service.digest_or_none(absolute) != row.sha256:
            findings.append(
                f"capture {row.capture_id}: {relative} no longer holds the captured bytes, so it "
                "cannot be identified and was not deleted"
            )
            continue
        try:
            absolute.unlink()
        except OSError as failure:
            raise LinguaWikiError(
                "capture_file_not_removed",
                f"capture {row.capture_id} is being refused, and its bytes at {relative} could "
                f"not be removed: {failure}. Nothing was recorded, because a refusal that left "
                "the recording behind would be accounted for by nothing.",
                details=(ErrorDetail(field="capture_id", reason=str(failure)),),
            ) from failure
    return findings


def _refuse(
    database: Database, root: Path, row: _Staging, failure: LinguaWikiError
) -> LinguaWikiError:
    """Record the refusal and remove the bytes it can identify, in one transaction.

    The deletion is inside the transaction that records it: a refusal committed and then
    failing to delete would leave a row saying the bytes went, beside the bytes.
    """

    findings: list[str] = []
    with database.transaction() as transaction:
        transaction.execute(
            "UPDATE capture_stagings SET state = 'refused', refusal_code = ?, "
            "refusal_reason = ?, updated_at = ? WHERE capture_id = ?",
            [failure.payload.code, failure.payload.message, transaction.now(), row.capture_id],
        )
        findings = _remove_identified(root, row)
    if findings:
        return LinguaWikiError(
            failure.payload.code,
            failure.payload.message + " " + " ".join(findings),
            details=failure.payload.details,
        )
    return failure


# --- the capture -------------------------------------------------------------------------


def _with_transient_retry[T](
    operation: Callable[[], T],
    *,
    wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Retry `writer_locked` and `database_busy` until `wait_seconds` have passed.

    Safe because every attempt resumes from the staging row: a capture is keyed by its
    identifier, so a second attempt continues the first rather than repeating it.
    """

    deadline = monotonic() + wait_seconds
    delay = 0.05
    while True:
        try:
            return operation()
        except LinguaWikiError as failure:
            if not failure.payload.retryable or monotonic() + delay > deadline:
                raise
            sleep(delay)
            delay = min(delay * 2, 0.5)


def capture(
    paths: WorkspacePaths,
    *,
    run: str,
    content_id: str,
    capture_id: str,
    data: bytes,
    media_type: str,
    clock: Clock | None = None,
    actor: str = assessment_service.DEFAULT_ACTOR,
    command: str = CAPTURE_COMMAND,
    wait_seconds: float = TRANSIENT_WAIT_SECONDS,
) -> CaptureReport:
    """Take one recording the learner made for a served spoken task, and bind it.

    Idempotent by `capture_id`: a re-sent upload of a capture already registered returns
    its artifact and submission and registers nothing again -- the "registration
    committed, response lost" case. The same identifier with different bytes, or for a
    different task, is a conflict naming what was recorded.
    """

    _assert_capture_id(capture_id)
    base_type, extension = _media_type(media_type)
    _assert_payload(data)
    digest = hashlib.sha256(data).hexdigest()
    active_clock = clock or SystemClock()

    def attempt() -> CaptureReport:
        with open_writer(paths, command=command, clock=active_clock) as database:
            return _capture_in_writer(
                paths,
                database,
                run=run,
                content_id=content_id,
                capture_id=capture_id,
                data=data,
                digest=digest,
                media_type=base_type,
                extension=extension,
                actor=actor,
                command=command,
            )

    return _with_transient_retry(attempt, wait_seconds=wait_seconds)


def _capture_in_writer(
    paths: WorkspacePaths,
    database: Database,
    *,
    run: str,
    content_id: str,
    capture_id: str,
    data: bytes,
    digest: str,
    media_type: str,
    extension: str,
    actor: str,
    command: str,
) -> CaptureReport:
    existing = _staging(database, capture_id)
    if existing is not None:
        if existing.sha256 != digest or (existing.run_id, existing.content_id) != (
            run,
            content_id,
        ):
            raise LinguaWikiError(
                "capture_conflict",
                f"capture {capture_id} was already received for {existing.content_id} in run "
                f"{existing.run_id} with bytes hashing to {existing.sha256[:12]}...; a retry "
                "resends the same recording, and a new recording needs a new identifier",
                details=(
                    ErrorDetail(
                        field="capture_id",
                        reason="recorded with different content",
                        context={"sha256": existing.sha256, "run_id": existing.run_id},
                    ),
                ),
            )
        if existing.state == REGISTERED:
            return _report(database, existing, replayed=True)
        if existing.state == REFUSED:
            # The same answer a retry is entitled to: the refusal it was given first.
            raise LinguaWikiError(
                str(existing.refusal_code),
                str(existing.refusal_reason),
                details=(ErrorDetail(field="capture_id", reason="refused earlier"),),
            )
        return _promote(paths, database, existing, data=data, actor=actor, command=command)
    run_id = assessment_service.resolve_run(database, run)
    track_id = str(
        database.scalar("SELECT track_id FROM assessment_runs WHERE run_id = ?", [run_id])
    )
    staged_path = f"{CAPTURE_STAGING}/{capture_id}.{extension}"
    final_path = f"{CAPTURE_DIRECTORY}/{track_id}/{capture_id}.{extension}"
    _preflight(
        database,
        paths.root,
        run_id=run_id,
        content_id=content_id,
        sha256=digest,
        capture_id=capture_id,
        paths_to_claim=(staged_path, final_path),
    )
    with database.transaction() as transaction:
        now = transaction.now()
        transaction.execute(
            f"INSERT INTO capture_stagings ({_STAGING_COLUMNS}, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', NULL, NULL, NULL, ?, ?)",
            [
                capture_id,
                track_id,
                run_id,
                content_id,
                staged_path,
                final_path,
                digest,
                len(data),
                media_type,
                now,
                now,
            ],
        )
    row = _staging(database, capture_id)
    assert row is not None
    # Written only after the row committed, so the bytes are accounted for from the moment
    # they exist.
    _write_staged(paths.root, staged_path, data)
    return _promote(paths, database, row, data=data, actor=actor, command=command)


def _write_staged(root: Path, relative: str, data: bytes) -> None:
    """Write the bytes once, durably, and never over something already there."""

    target = root / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as failure:
        raise LinguaWikiError(
            "capture_write_failed",
            f"the recording could not be written to {relative}: {failure}. The capture is "
            "staged without its bytes, and resending it writes them.",
            details=(ErrorDetail(field="capture_id", reason=str(failure)),),
        ) from failure


def _promote(
    paths: WorkspacePaths,
    database: Database,
    row: _Staging,
    *,
    data: bytes | None,
    actor: str,
    command: str,
) -> CaptureReport:
    """Carry one non-terminal capture to `registered`, or to `refused`, from wherever it is.

    `data` is the bytes when the caller still holds them -- the upload -- and `None` in
    recovery, where only the disk can answer.
    """

    root = paths.root
    if row.state == STAGED:
        state, absolute = _classify_staged(root, row.staged_path)
        if state == artifact_service.PATH_ABSENT and data is not None:
            # Committed and then not written: a crash between the two, and this is the
            # retry carrying the same bytes. Writing them now is the step that was missed.
            _write_staged(root, row.staged_path, data)
            state, absolute = _classify_staged(root, row.staged_path)
        if state == artifact_service.PATH_ABSENT:
            raise _refuse(
                database,
                root,
                row,
                LinguaWikiError(
                    "capture_file_missing",
                    f"capture {row.capture_id} was staged and its bytes never arrived; record "
                    "again",
                    details=(ErrorDetail(field="capture_id", reason="no staged file"),),
                ),
            )
        # Nothing else is checked here: `_move` verifies the bytes and the destination at the
        # rename, and `_register_and_bind` runs the preflight before anything binds, so a
        # check here would only be a third copy of both.
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE capture_stagings SET state = 'promoting', updated_at = ? "
                "WHERE capture_id = ?",
                [transaction.now(), row.capture_id],
            )
        row = _staging(database, row.capture_id) or row
        _move(database, root, row)
        return _register_and_bind(paths, database, row, actor=actor, command=command)
    # `promoting`: the crash was after the state change. Where the bytes are says which
    # step it was.
    staged_state, _ = _classify_staged(root, row.staged_path)
    final_state, _ = _classify_final(root, row.final_path)
    if staged_state == artifact_service.PATH_PRESENT:
        _move(database, root, row)
        return _register_and_bind(paths, database, row, actor=actor, command=command)
    if final_state == artifact_service.PATH_PRESENT:
        return _register_and_bind(paths, database, row, actor=actor, command=command)
    # Neither. With `os.replace` there is no moment at which the bytes are at neither
    # path, so this is a deletion from outside -- named, never fallen through.
    raise _refuse(
        database,
        root,
        row,
        LinguaWikiError(
            "capture_file_missing",
            f"capture {row.capture_id} was being promoted and its bytes are at neither "
            f"{row.staged_path} nor {row.final_path}; something outside LinguaWiki removed "
            "them",
            details=(ErrorDetail(field="capture_id", reason="at neither path"),),
        ),
    )


def _move(database: Database, root: Path, row: _Staging) -> None:
    """Step (3): one rename on one filesystem, so the bytes are at exactly one path.

    Checked here, at the rename, rather than trusted from whoever called it: recovery reaches
    this after a crash, when the disk may have changed since the row was written. The staged
    file has to be contained and hold the captured bytes, and the destination has to be
    empty -- `os.replace` silently overwrites, and overwriting a file nothing here can
    identify is the one thing worse than leaving it.
    """

    staged_state, staged = _classify_staged(root, row.staged_path)
    if (
        staged_state != artifact_service.PATH_PRESENT
        or staged is None
        or artifact_service.digest_or_none(staged) != row.sha256
    ):
        raise _refuse(
            database,
            root,
            row,
            LinguaWikiError(
                "capture_hash_mismatch",
                f"the staged bytes of capture {row.capture_id} are not the bytes that were "
                f"sent ({staged_state})",
                details=(ErrorDetail(field="sha256", reason=row.sha256),),
            ),
        )
    final_state, _ = _classify_final(root, row.final_path)
    if final_state != artifact_service.PATH_ABSENT:
        raise _refuse(
            database,
            root,
            row,
            LinguaWikiError(
                "capture_path_occupied",
                f"something is already at {row.final_path} ({final_state}), and nothing here "
                "accounts for it; it was left where it is and the capture was not moved over it",
                details=(ErrorDetail(field="capture_id", reason=row.capture_id),),
            ),
        )
    target = root / row.final_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
    except OSError as failure:
        # The disk refused the move -- a read-only or cross-volume artifacts directory. The
        # bytes are still staged and the row still says so, which is a state the next
        # attempt can name; nothing is deleted, because nothing is wrong with the capture.
        raise LinguaWikiError(
            "capture_move_failed",
            f"capture {row.capture_id} could not be moved from {row.staged_path} to "
            f"{row.final_path}: {failure}. Its bytes were left staged.",
            details=(ErrorDetail(field="capture_id", reason=row.capture_id),),
        ) from failure


def _register_and_bind(
    paths: WorkspacePaths, database: Database, row: _Staging, *, actor: str, command: str
) -> CaptureReport:
    """Step (4): register, mark the staging row, bind the submission -- one transaction.

    Every refusal is reached first, through the same preflight and the same registration
    plan the writer uses, so a refusal leaves the bytes identified and removed rather than
    a half-written binding.
    """

    root = paths.root
    try:
        _preflight(
            database,
            root,
            run_id=row.run_id,
            content_id=row.content_id,
            sha256=row.sha256,
            capture_id=row.capture_id,
            # Already staged: the learner spoke while the run was live. A fresh upload got
            # here through a preflight that required a running run, in this same writer.
            resuming=True,
        )
        plan = artifact_service.plan_registration(
            database,
            root,
            relative_path=row.final_path,
            kind="audio",
            media_type=row.media_type,
            origin="learner-recording",
            rights="full-local",
            retained=True,
            expected_sha256=row.sha256,
            track=row.track_id,
        )
        if plan.existing is not None:
            raise LinguaWikiError(
                "capture_duplicate_bytes",
                f"these are exactly the bytes of recording {plan.existing[0]}, so they cannot "
                "be a new answer; record again",
                details=(ErrorDetail(field="artifact", reason=plan.existing[0]),),
            )
    except LinguaWikiError as failure:
        raise _refuse(database, root, row, failure) from None
    previous = live_submission(database, row.run_id, row.content_id)
    submission_id = str(AssessmentId.new())
    superseded: list[str] = []
    with database.transaction() as transaction:
        now = transaction.now()
        artifact_id = artifact_service.write_registration(transaction, plan, command=command)
        transaction.execute(
            f"INSERT INTO assessment_submissions ({_SUBMISSION_COLUMNS}, kind, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, 'recording', ?)",
            [submission_id, row.run_id, row.content_id, row.capture_id, artifact_id, now, now],
        )
        transaction.execute(
            "UPDATE capture_stagings SET state = 'registered', artifact_id = ?, updated_at = ? "
            "WHERE capture_id = ?",
            [artifact_id, now, row.capture_id],
        )
        if previous is not None:
            # Before a verdict, a second recording of the same task replaces the first. The
            # first is evidence of nothing now, so it goes in the same operation: there is
            # no moment at which two recordings answer one task, or none does.
            transaction.execute(
                "UPDATE assessment_submissions SET status = 'superseded', superseded_by = ?, "
                "updated_at = ? WHERE submission_id = ?",
                [submission_id, now, previous.submission_id],
            )
            artifact_service.write_purge(
                transaction,
                root,
                artifact_id=previous.artifact_id,
                reason="superseded",
                command=command,
            )
            superseded.append(previous.submission_id)
        migration_module.record_audit_entry(
            transaction,
            command=command,
            correlation_id=EventId.new(),
            outcome="succeeded",
            actor=actor,
            affected_records_json=json.dumps(
                [row.run_id, row.content_id, artifact_id, submission_id], sort_keys=True
            ),
            after_summary=(
                f"bound a recording of {row.byte_size} byte(s) to {row.content_id}"
                + (f", superseding {superseded[0]}" if superseded else "")
            ),
        )
        migration_module.record_domain_event(
            transaction,
            event_type="assessment.captured",
            aggregate_type="assessment_run",
            aggregate_id=row.run_id,
            correlation_id=EventId.new(),
            payload_json=json.dumps(
                {"content_id": row.content_id, "submission_id": submission_id}, sort_keys=True
            ),
        )
    registered = _staging(database, row.capture_id)
    assert registered is not None
    return _report(database, registered, superseded=tuple(superseded))


def _report(
    database: Database,
    row: _Staging,
    *,
    replayed: bool = False,
    superseded: tuple[str, ...] = (),
) -> CaptureReport:
    return CaptureReport(
        capture_id=row.capture_id,
        run_id=row.run_id,
        content_id=row.content_id,
        state=row.state,
        sha256=row.sha256,
        artifact_id=row.artifact_id,
        submission=_submission_by_capture(database, row.capture_id),
        superseded=superseded,
        replayed=replayed,
        recording=track_recording_policy(database, row.track_id),
        warnings=(
            ("this capture was already received; the recorded submission is returned",)
            if replayed
            else ()
        ),
    )


# --- recovery ----------------------------------------------------------------------------


class RecoveryReport(ContractModel):
    examined: int = 0
    registered: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    #: Files nothing accounts for, and anything a refusal could not identify. Reported,
    #: never deleted.
    findings: tuple[str, ...] = ()


def recover(
    paths: WorkspacePaths,
    *,
    clock: Clock | None = None,
    wait_seconds: float = RECOVERY_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RecoveryReport:
    """Resolve every capture a crash left `staged` or `promoting`, before serving resumes.

    Needs the writer. It waits for a bounded interval and then refuses with
    `writer_locked`, naming recovery as what it was waiting for: serving requests over
    unresolved captures is the guess this exists to prevent.
    """

    active_clock = clock or SystemClock()

    def attempt() -> RecoveryReport:
        with open_writer(paths, command=RECOVERY_COMMAND, clock=active_clock) as database:
            rows = _unresolved(database)
            registered: list[str] = []
            refused: list[str] = []
            findings: list[str] = []
            for row in rows:
                try:
                    _promote(
                        paths,
                        database,
                        row,
                        data=None,
                        actor="client",
                        command=RECOVERY_COMMAND,
                    )
                    registered.append(row.capture_id)
                except (LinguaWikiError, OSError) as raised:
                    failure = (
                        raised
                        if isinstance(raised, LinguaWikiError)
                        else LinguaWikiError("capture_unresolved", str(raised))
                    )
                    after = _staging(database, row.capture_id)
                    if after is None or after.state in (STAGED, PROMOTING):
                        # The refusal did not land -- its cleanup failed and the transaction
                        # recording it rolled back. Reporting that as a resolved capture
                        # would let the server start over exactly what recovery exists for.
                        raise LinguaWikiError(
                            "capture_unresolved",
                            f"capture {row.capture_id} could not be resolved: "
                            f"{failure.payload.message}. The server will not start over it; "
                            "fix the cause and start it again.",
                            details=failure.payload.details,
                        ) from raised
                    refused.append(row.capture_id)
                    findings.append(f"capture {row.capture_id}: {failure.payload.message}")
            findings.extend(unaccounted_staged_files(paths, database))
            return RecoveryReport(
                examined=len(rows),
                registered=tuple(registered),
                refused=tuple(refused),
                findings=tuple(findings),
            )

    try:
        return _with_transient_retry(
            attempt, wait_seconds=wait_seconds, sleep=sleep, monotonic=monotonic
        )
    except LinguaWikiError as failure:
        if failure.payload.code not in ("writer_locked", "database_busy"):
            raise
        raise LinguaWikiError(
            "writer_locked",
            "another LinguaWiki command held the workspace for longer than capture recovery "
            f"waits ({wait_seconds:g}s). The server will not accept requests over recordings "
            "a crash may have left unresolved; start it again when that command finishes.",
            details=failure.payload.details,
        ) from failure


def unaccounted_staged_files(paths: WorkspacePaths, database: Database) -> list[str]:
    """Files under the capture staging root that no unresolved staging row names."""

    named = {
        str(path)
        for (path,) in database.query(
            "SELECT staged_path FROM capture_stagings WHERE state IN ('staged', 'promoting')"
        )
    }
    base = paths.root / CAPTURE_STAGING
    try:
        entries = sorted(base.rglob("*")) if base.is_dir() else []
    except OSError as failure:
        return [f"{CAPTURE_STAGING}/ cannot be listed: {failure}"]
    findings = []
    for entry in entries:
        try:
            if entry.is_dir():
                continue
        except OSError:
            pass
        relative = entry.relative_to(paths.root).as_posix()
        if relative not in named:
            findings.append(
                f"{relative} is under the capture staging root and no capture accounts for it; "
                "it was left where it is"
            )
    return findings


# --- the evidence check ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JudgeableAudio:
    """A recording proven to be the one a judge may score this task from."""

    artifact_id: str
    path: Path
    sha256: str
    media_type: str | None
    submission: SubmissionReport


def _judging_refusal(code: str, message: str, *, artifact_id: str) -> LinguaWikiError:
    return LinguaWikiError(
        code, message, details=(ErrorDetail(field="audio_artifact", reason=artifact_id),)
    )


#: The refusals that are about the bound recording itself rather than about what a caller
#: named. When one of them refuses a verdict, no judge can hear the recording any more, so
#: its submission is withdrawn and the task skipped.
RECORDING_FAILURES: frozenset[str] = frozenset(
    {
        "assessment_audio_purged",
        "assessment_audio_not_retained",
        "assessment_audio_missing",
        "assessment_audio_escaped",
        "assessment_audio_unreadable",
        "assessment_audio_altered",
    }
)


def assert_judgeable(
    database: Database,
    root: Path,
    *,
    artifact_id: str,
    run_id: str,
    content_id: str,
) -> JudgeableAudio:
    """Whether a judge may score this served task from this recording -- the one check.

    Used by `assessment pending` before handing a recording out, by `record` inside the
    writer that stores the verdict, and by `db check`. "Present and retained" is not
    enough, and each failure keeps its own code:

    - it belongs to the **run's own track**, resolved through `assessment_runs`;
    - its `kind` is `audio` -- a transcript cannot carry an acoustic claim;
    - it is retained, and neither purged nor recorded as not kept;
    - it is the artifact of the live submission for this served task;
    - the file is present inside the workspace and hashes to what was registered.
    """

    row = database.one(
        "SELECT artifact_id, track_id, kind, relative_path, sha256, retained, purged_at, "
        "media_type FROM artifacts WHERE artifact_id = ?",
        [artifact_id],
    )
    if row is None:
        raise _judging_refusal(
            "artifact_not_found",
            f"no artifact {artifact_id} in this workspace",
            artifact_id=artifact_id,
        )
    run_track = database.scalar("SELECT track_id FROM assessment_runs WHERE run_id = ?", [run_id])
    if run_track is None:
        raise LinguaWikiError(
            "assessment_run_not_found",
            f"no assessment run with ID {run_id}",
            details=(ErrorDetail(field="run", reason="unknown run"),),
        )
    if str(row[1]) != str(run_track):
        raise _judging_refusal(
            "assessment_audio_out_of_scope",
            f"{artifact_id} belongs to another track's learner, and run {run_id} is not theirs",
            artifact_id=artifact_id,
        )
    if str(row[2]) != "audio":
        raise _judging_refusal(
            "assessment_audio_not_audio",
            f"{artifact_id} is {row[2]}, and how something sounded can only be judged from audio",
            artifact_id=artifact_id,
        )
    if row[6] is not None:
        raise _judging_refusal(
            "assessment_audio_purged",
            f"{artifact_id} was purged, so there is nothing to judge",
            artifact_id=artifact_id,
        )
    if not bool(row[5]):
        raise _judging_refusal(
            "assessment_audio_not_retained",
            f"{artifact_id} is recorded as not kept, so no claim can rest on it",
            artifact_id=artifact_id,
        )
    live = live_submission(database, run_id, content_id)
    if live is None:
        raise _judging_refusal(
            "assessment_submission_missing",
            f"{content_id} has no recording submitted in run {run_id}, so there is nothing a "
            "judge was asked to hear",
            artifact_id=artifact_id,
        )
    if live.artifact_id != artifact_id:
        raise LinguaWikiError(
            "assessment_audio_not_submitted",
            f"{content_id} is answered by recording {live.artifact_id}, not {artifact_id}; a "
            "verdict is about the recording the learner submitted",
            details=(
                ErrorDetail(
                    field="audio_artifact",
                    reason="not the submitted recording",
                    context={"bound": live.artifact_id},
                ),
            ),
        )
    relative_path = str(row[3])
    state, absolute = artifact_service.classify_path(root, relative_path)
    if state == artifact_service.PATH_ESCAPED:
        raise _judging_refusal(
            "assessment_audio_escaped",
            f"{artifact_id} is registered at a path that now leads outside this workspace; "
            "nothing is read through it",
            artifact_id=artifact_id,
        )
    if state == artifact_service.PATH_UNREADABLE:
        raise _judging_refusal(
            "assessment_audio_unreadable",
            f"{artifact_id} is there and cannot be read",
            artifact_id=artifact_id,
        )
    if state != artifact_service.PATH_PRESENT or absolute is None:
        raise _judging_refusal(
            "assessment_audio_missing",
            f"{artifact_id} is not where it was registered, so there is nothing to judge",
            artifact_id=artifact_id,
        )
    found = artifact_service.digest_or_none(absolute)
    if found is None:
        raise _judging_refusal(
            "assessment_audio_unreadable",
            f"{artifact_id} is there and cannot be read",
            artifact_id=artifact_id,
        )
    if found != str(row[4]):
        raise _judging_refusal(
            "assessment_audio_altered",
            f"{artifact_id} no longer holds the bytes it was registered with; a judgement of "
            "these would be a judgement of something the learner never submitted",
            artifact_id=artifact_id,
        )
    return JudgeableAudio(
        artifact_id=artifact_id,
        path=absolute,
        sha256=str(row[4]),
        media_type=None if row[7] is None else str(row[7]),
        submission=live,
    )


def judgeable_problem(
    database: Database, root: Path, *, artifact_id: str, run_id: str, content_id: str
) -> LinguaWikiError | None:
    """`assert_judgeable` as an answer rather than a refusal, for the readers that report."""

    try:
        assert_judgeable(
            database, root, artifact_id=artifact_id, run_id=run_id, content_id=content_id
        )
    except LinguaWikiError as failure:
        return failure
    return None


# --- delivery to a judge -----------------------------------------------------------------


class PendingTask(ContractModel):
    """What a judge needs: the task as it was served, and how to reach the recording."""

    content_id: str
    dimension: str
    dimension_kind: str
    task_type: str
    modality: str
    level_code: str
    prompt: str | None = None
    rubric: dict[str, object] = Field(default_factory=dict)
    rubric_version: int = 1


class PendingJudgement(ContractModel):
    submission: SubmissionReport
    task: PendingTask
    #: The recording's absolute path inside this workspace, resolved with containment.
    #: `None` when it cannot be judged, with the reason beside it.
    audio_path: str | None = None
    media_type: str | None = None
    sha256: str | None = None
    judgeable: bool
    problem_code: str | None = None
    problem: str | None = None


class PendingReport(ContractModel):
    run_id: str
    track_id: str
    recording: RecordingPolicy
    pending: tuple[PendingJudgement, ...] = ()
    warnings: tuple[str, ...] = ()


def pending(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> PendingReport:
    """Every recording in a run waiting for a judge, with what the judge needs to hear it.

    The judge reads the audio through this contract, never by guessing a path: the path is
    the one `assert_judgeable` proved holds the submitted bytes, and a recording that fails
    the check is listed with its reason rather than handed out.
    """

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = assessment_service.resolve_run(database, run, track_id=track_id)
        run_track = str(
            database.scalar("SELECT track_id FROM assessment_runs WHERE run_id = ?", [run_id])
        )
        kinds = assessment_service.run_dimension_kinds(database, run_id)
        entries: list[PendingJudgement] = []
        for row in database.query(
            f"SELECT {_SUBMISSION_COLUMNS} FROM assessment_submissions "
            "WHERE run_id = ? AND status = 'pending' ORDER BY created_at, submission_id",
            [run_id],
        ):
            submission = _submission_from(row)
            shown = assessment_service.served_task_report(
                database, run_id, content_id=submission.content_id
            )
            task = PendingTask(
                content_id=shown.content_id,
                dimension=shown.dimension,
                dimension_kind=kinds.get(shown.dimension, ""),
                task_type=shown.task_type,
                modality=shown.modality,
                level_code=shown.level_code,
                prompt=shown.prompt,
                rubric=shown.rubric,
                rubric_version=shown.rubric_version,
            )
            try:
                audio = assert_judgeable(
                    database,
                    paths.root,
                    artifact_id=submission.artifact_id,
                    run_id=run_id,
                    content_id=submission.content_id,
                )
            except LinguaWikiError as failure:
                entries.append(
                    PendingJudgement(
                        submission=submission,
                        task=task,
                        judgeable=False,
                        problem_code=failure.payload.code,
                        problem=failure.payload.message,
                    )
                )
                continue
            entries.append(
                PendingJudgement(
                    submission=submission,
                    task=task,
                    audio_path=str(audio.path),
                    media_type=audio.media_type,
                    sha256=audio.sha256,
                    judgeable=True,
                )
            )
        warnings = tuple(
            f"{entry.submission.content_id} cannot be judged: {entry.problem}"
            for entry in entries
            if not entry.judgeable
        )
        return PendingReport(
            run_id=run_id,
            track_id=run_track,
            recording=track_recording_policy(database, run_track),
            pending=tuple(entries),
            warnings=warnings,
        )


def filesystem_checks(paths: WorkspacePaths, database: Database) -> list[CheckResult]:
    """The `db check` findings only the disk can answer. A diagnostic: it never repairs.

    Never raises, either. A diagnostic that aborts tells an operator less than one that
    lies, so a check that cannot be run is reported as a failure that names why.
    """

    try:
        return _filesystem_checks(paths, database)
    except Exception as failure:  # a diagnostic reports; it does not raise
        return [
            CheckResult(
                name="capture_files_accounted",
                status="failed",
                message=f"the capture checks could not be run: {type(failure).__name__}",
                context={"error": str(failure)[:500]},
            )
        ]


def _filesystem_checks(paths: WorkspacePaths, database: Database) -> list[CheckResult]:
    root = paths.root
    stray = unaccounted_staged_files(paths, database)
    lost: list[str] = []
    for row in _unresolved(database):
        staged_state, _ = _classify_staged(root, row.staged_path)
        final_state, _ = _classify_final(root, row.final_path)
        present = artifact_service.PATH_PRESENT
        if row.state == PROMOTING and present not in (staged_state, final_state):
            lost.append(f"{row.capture_id} is promoting and its bytes are at neither path")
    unjudgeable: list[str] = []
    for kind, (run_id, content_id, artifact_id) in [
        ("pending", tuple(entry))
        for entry in database.query(
            "SELECT run_id, content_id, artifact_id FROM assessment_submissions "
            "WHERE status = 'pending' ORDER BY 1, 2"
        )
    ] + [
        ("judged", tuple(entry))
        for entry in database.query(
            "SELECT run_id, content_id, audio_artifact_id FROM assessment_results "
            "WHERE audio_artifact_id IS NOT NULL AND invalidated_at IS NULL ORDER BY 1, 2"
        )
    ]:
        problem = judgeable_problem(
            database,
            root,
            artifact_id=str(artifact_id),
            run_id=str(run_id),
            content_id=str(content_id),
        )
        if problem is not None:
            unjudgeable.append(
                f"{kind} {run_id}/{content_id} on {artifact_id}: {problem.payload.code}"
            )
    return [
        CheckResult(
            name="capture_files_accounted",
            status="failed" if stray or lost else "ok",
            message=(
                "bytes under the capture staging root are accounted for by no capture, or a "
                "capture's bytes are at neither of its paths"
                if stray or lost
                else "every byte under the capture staging root belongs to an unresolved capture"
            ),
            context={"files": "; ".join([*stray, *lost][:50])} if stray or lost else {},
        ),
        CheckResult(
            name="judged_recordings_judgeable",
            status="failed" if unjudgeable else "ok",
            message=(
                "a recording a judge has been asked to hear, or a standing verdict rests on, "
                "is no longer the recording it was"
                if unjudgeable
                else "every recording awaiting or behind a verdict is present and unaltered"
            ),
            context={"recordings": "; ".join(unjudgeable[:50])} if unjudgeable else {},
        ),
    ]


def withdraw_unjudgeable(
    database: Database, *, submission: SubmissionReport, failure: LinguaWikiError
) -> None:
    """A verdict arrived for a recording no judge can hear any more: withdraw it, by name."""

    with database.transaction() as transaction:
        withdrawal.withdraw_submission(
            transaction,
            submission_id=submission.submission_id,
            code=failure.payload.code,
            reason=failure.payload.message,
        )


__all__ = [
    "CAPTURE_DIRECTORY",
    "CAPTURE_MEDIA_TYPES",
    "CAPTURE_STAGING",
    "MAXIMUM_CAPTURE_BYTES",
    "RECORDING_FAILURES",
    "STAGING_DIRECTORY",
    "CaptureReport",
    "JudgeableAudio",
    "PendingJudgement",
    "PendingReport",
    "PendingTask",
    "RecordingPolicy",
    "RecoveryReport",
    "SubmissionReport",
    "assert_judgeable",
    "capture",
    "filesystem_checks",
    "judgeable_problem",
    "latest_submission",
    "live_submission",
    "pending",
    "recording_policy",
    "recover",
    "track_recording_policy",
    "unaccounted_staged_files",
    "withdraw_unjudgeable",
]
