"""One call per screen: everything a client needs to draw a run, and nothing else.

A reader is not free. A separate-process `open_reader` beside a held writer is refused with
the retryable `database_busy`, so a screen assembled from four calls is four chances to be
told the database is busy and four retry budgets to spend. One call is the whole report.

This module reads; it decides nothing. Selection, scoring, the stop rule and every refusal
stay in `services/assessment.py`, and the outstanding tasks here come from the same
`served_task_report` the serve path hands back -- which is what keeps "what the learner was
shown" to one answer rather than two that can drift.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from linguawiki.clock import Clock, SystemClock
from linguawiki.contracts import ServedAsset, TaskPresentation
from linguawiki.db.connection import Database, open_reader
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import (
    DEFAULT_SCORING,
    MACHINE_SCORABLE_TASK_TYPES,
    PLAYED_MODALITY,
    RECORDED_JUDGED_TASK_TYPES,
    SPOKEN_MODALITY,
)
from linguawiki.services import assessment as assessment_service
from linguawiki.services import learners as learner_service
from linguawiki.services import recordings as recording_service
from linguawiki.services.learners import RecordingPolicy
from linguawiki.services.recordings import SubmissionReport

#: How an outstanding task is answered. Derived from the served presentation rather than
#: from the task type: `objective` and `short-response` are both machine-scorable, and
#: whether the learner presses a button or types is a question about rendering. A client
#: that read the task type would draw a field for a task that shipped choices.
AnswerMode = Literal["choice", "text", "recording"]
#: Where an outstanding task stands. `awaiting-judge` is an answer that has been submitted
#: -- a recording, or a written answer (`submission.kind`) -- and that nobody has marked yet.
TaskState = Literal["awaiting-answer", "awaiting-judge"]


class OutstandingTask(ContractModel):
    """A task this run served and has not settled, as it was served.

    Never from `assessment_tasks`. A pack is mutable and a run is not, so re-reading the
    bank would let an edit made after the serving decide what the learner is held to have
    been asked.
    """

    content_id: str
    dimension: str
    sequence: int
    task_type: str
    modality: str
    level_code: str
    difficulty: float
    content_family: str
    prompt: str | None = None
    presentation: TaskPresentation | None = None
    asset: ServedAsset | None = None
    rubric: dict[str, object] = Field(default_factory=dict)
    rubric_version: int = 1
    #: `None` for a task served before migration 0032. Absence, not `"none"`: "help was
    #: refused" and "nobody recorded what help was allowed" are different facts.
    permitted_help: str | None = None
    answer_with: AnswerMode = "text"
    #: Whether the learner has a recording to play. A client needs this before it reads the
    #: presentation, because it decides whether the screen has a player on it at all.
    plays_audio: bool = False
    #: Whether only a judge can score this task. A run opened under `any` can be holding
    #: one, and a client with no judge must say so rather than offer an answer the server
    #: will refuse to compute. Derived from the served task type through the scoring
    #: policy, never re-decided by the client.
    needs_judge: bool = False
    #: Whether this task is heard and was served with nothing to play. A run opened under
    #: `any` can hold one: it is machine-scorable, so `needs_judge` is false, and drawing
    #: its prompt instead would turn a listening task into a reading one. A client must not
    #: offer it.
    missing_recording: bool = False
    #: Plays recorded so far, and what a finite allowance has left (`None`: unlimited).
    #: Read from the play rows, so a reloaded page, a resumed run, and a later sitting all
    #: show the same number -- nothing about plays lives only in a page.
    plays_used: int = 0
    plays_remaining: int | None = None
    #: `awaiting-judge` once an answer has been submitted for it, recorded or written.
    state: TaskState = "awaiting-answer"
    #: The live submission answering this task: its `kind`, and the recording's artifact
    #: ID or a written answer's digest. Never a written answer's text -- the page sent it,
    #: and the read model does not carry a learner's words.
    submission: SubmissionReport | None = None


class RunScreen(ContractModel):
    """The whole state of one run, in one read."""

    screen_version: int = 1
    run_id: str
    track_id: str
    run_type: str
    calibration_label: str
    status: str
    pack_key: str
    pack_version: str
    framework_id: str
    framework_levels: tuple[str, ...] = ()
    available_modalities: tuple[str, ...] = ()
    #: The scoring condition the run was opened under (`placement.SCORING_CONDITIONS`).
    #: Under `any` an outstanding task can need a judge, which a client without one must not
    #: offer to answer; under `machine+recorded` and `machine+judged` the judged tasks it
    #: serves are answered by a recording or a written submission, and wait for the judge.
    scoring: str = DEFAULT_SCORING
    dimensions: tuple[assessment_service.DimensionReport, ...] = ()
    #: At most one per *open* dimension, after the serve-time guard, and possibly one more
    #: per dimension that closed while holding a task -- which `record` still accepts, so a
    #: screen that hid it would leave answerable work with no surface that mentions it.
    outstanding: tuple[OutstandingTask, ...] = ()
    tasks_served: int = 0
    tasks_recorded: int = 0
    results_invalidated: int = 0
    #: Whether this track lets the learner record a spoken answer, and the retention policy
    #: the recording will be kept under -- reported so the page can say it, never chosen.
    recording: RecordingPolicy | None = None
    warnings: tuple[str, ...] = ()


def _answer_mode(shown: TaskPresentation | None, *, modality: str, task_type: str) -> AnswerMode:
    if modality == SPOKEN_MODALITY and task_type in RECORDED_JUDGED_TASK_TYPES:
        return "recording"
    return "choice" if shown is not None and shown.choices else "text"


def run_screen_report(database: Database, run_id: str) -> RunScreen:
    """The `(database, id)` form, for use inside a writer that already holds the lock."""

    run = assessment_service.run_report(database, run_id)
    outstanding = []
    for content_id in assessment_service.outstanding_task_ids(database, run_id):
        shown = assessment_service.served_task_report(database, run_id, content_id=content_id)
        audio = None if shown.presentation is None else shown.presentation.audio
        submitted = recording_service.live_submission(database, run_id, content_id)
        used = (
            0
            if audio is None
            else int(
                database.scalar(
                    "SELECT count(*) FROM assessment_task_plays "
                    "WHERE run_id = ? AND content_id = ?",
                    [run_id, content_id],
                )
            )
        )
        outstanding.append(
            OutstandingTask(
                content_id=content_id,
                dimension=shown.dimension,
                sequence=shown.sequence,
                task_type=shown.task_type,
                modality=shown.modality,
                level_code=shown.level_code,
                difficulty=shown.difficulty,
                content_family=shown.content_family,
                prompt=shown.prompt,
                presentation=shown.presentation,
                asset=shown.asset,
                rubric=shown.rubric,
                rubric_version=shown.rubric_version,
                permitted_help=shown.permitted_help,
                answer_with=_answer_mode(
                    shown.presentation, modality=shown.modality, task_type=shown.task_type
                ),
                plays_audio=audio is not None,
                plays_used=used,
                plays_remaining=(
                    None
                    if audio is None
                    else assessment_service.plays_remaining(audio.replay_allowance, used)
                ),
                needs_judge=shown.task_type not in MACHINE_SCORABLE_TASK_TYPES,
                missing_recording=shown.modality == PLAYED_MODALITY and audio is None,
                state="awaiting-judge" if submitted is not None else "awaiting-answer",
                submission=submitted,
            )
        )
    # No bound and no `omissions`, and this is the place to say why: the guard in the serve
    # path leaves at most one outstanding task per dimension, and a run has a handful of
    # dimensions, so there is no list here that can outgrow a caller's budget. A later
    # stage that adds one owes this report an `omissions` field, because half a list looks
    # exactly like a short one.
    return RunScreen(
        run_id=run.run_id,
        track_id=run.track_id,
        run_type=run.run_type,
        calibration_label=run.calibration_label,
        status=run.status,
        pack_key=run.pack_key,
        pack_version=run.pack_version,
        framework_id=run.framework_id,
        framework_levels=run.framework_levels,
        available_modalities=run.available_modalities,
        scoring=run.scoring,
        dimensions=run.dimensions,
        outstanding=tuple(outstanding),
        tasks_served=run.tasks_served,
        tasks_recorded=run.tasks_recorded,
        results_invalidated=run.results_invalidated,
        recording=learner_service.track_recording_policy(database, run.track_id),
        warnings=run.warnings,
    )


def run_screen(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> RunScreen:
    """The whole state of a run, for a caller that holds no connection."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        return run_screen_report(
            database, assessment_service.resolve_run(database, run, track_id=track_id)
        )


__all__ = [
    "AnswerMode",
    "OutstandingTask",
    "RunScreen",
    "TaskState",
    "run_screen",
    "run_screen_report",
]
