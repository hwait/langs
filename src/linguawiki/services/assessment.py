"""Bounded calibration and placement runs against an installed pack's bank.

The persisted state is what makes a run resumable: every dimension's grid, prior,
posterior, budget, families covered, boundary probes, and stop reason live in
`placement_dimension_state`, and every scored task keeps the prior and posterior it was
folded into. A conversation can end mid-run and the next invocation continues, pauses, or
finalizes with what it finds.

Two refusals are the point of this module rather than incidental to it:

- comprehensive placement runs only where the pack's bank is `placement-ready` for the
  bands being tested; a `pilot` pack gets a *labelled* calibration and cannot pretend
  otherwise;
- a dimension the pack or the learner's equipment cannot serve is `not-tested`, never
  failed, and says which of the two was missing.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import Field

from linguawiki import evidence as evidence_policy
from linguawiki import idempotency
from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.contracts import (
    SNAPSHOT_ABSENT,
    SNAPSHOT_WHOLE,
    PackMaturity,
    ServedAsset,
    TaskPresentation,
    parse_answer_key,
    parse_asset_identity,
    parse_task_presentation,
    reads_as_json_object,
    served_snapshot_state,
    snapshot_lost_its_presentation,
)
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import AssessmentId, EventId
from linguawiki.models import ContractModel
from linguawiki.packs import coverage as coverage_module
from linguawiki.packs.format import load_pack, resolve_pack_path
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import (
    ALGORITHM_VERSION,
    DEFAULT_SCORING,
    JUDGED_SCORING,
    RECORDING_SCORINGS,
    REUSE_WINDOW_MONTHS,
    SCORING_POLICY_VERSION,
    Candidate,
    DimensionState,
    assert_machine_scorable,
    assert_scoring_condition,
    budget_for,
    close_dimension,
    confidence_label,
    credible_interval,
    estimated_level,
    initial_state,
    judged_spoken_task,
    judged_written_task,
    posterior_mean,
    posterior_sd,
    record_score,
    recording_permitted,
    score_response,
    select_task,
    servable_candidate,
    servable_under,
    unavailable_reason,
    written_judging_permitted,
)
from linguawiki.provenance import PROMOTED_LIFECYCLES
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service

if TYPE_CHECKING:
    from pathlib import Path

    from linguawiki.services.recordings import JudgeableAudio, JudgeableText
    from linguawiki.services.withdrawal import Outstanding, Settlement, SettlementOutcome

#: Modalities a workspace can always offer, whatever the learner's equipment.
BASELINE_MODALITIES = ("text", "writing", "audio")
ESTIMATE_CALCULATION_VERSION = "estimate.v1"


#: Where a dimension stands for the learner, beside its stored `status`. `open` has work in
#: it: a task can be served, or the learner is holding one unanswered. `waiting` is open and
#: every task it holds has an answer handed in that nobody has marked -- `select_task` must
#: not probe a posterior no answer has moved, so there is nothing sound to serve in it until
#: a judge delivers. `closed` is `stopped` or `not-tested`: nothing more is served in it.
DimensionProgress = Literal["open", "waiting", "closed"]

#: Where a run's remaining work stands, beside its stored `status`, so a client can tell
#: "wait for a judge" from "finished, finalize now" without inferring it from a batch that
#: served nothing. `working`: some dimension is open, or a task is in the learner's hands.
#: `waiting`: none is, and a judgement is outstanding -- an answer nobody has marked, or a
#: verdict held for the resume. `complete`: nothing is left, so finalizing is what remains.
#: `closed`: the run was finalized or abandoned. A paused run reports what resuming finds.
RunProgress = Literal["working", "waiting", "complete", "closed"]

#: Where judging one outstanding submission stands: nobody holds it, a judge holds a live
#: lease on it, its verdict has arrived and is held until the run resumes, or every judging
#: attempt has ended without a verdict (`judging.lapsed_submissions`) -- nobody may claim it,
#: and the next writer touching the run withdraws it as `assessment_judging_exhausted`.
ClaimStage = Literal["unclaimed", "claimed", "held", "lapsed"]

#: What answers a submission: a recording, or a written answer (0035's `kind` CHECK).
SubmissionKind = Literal["recording", "text"]


class OutstandingJudgement(ContractModel):
    """One answer waiting on a judgement, and what it is waiting on.

    Never the learner's words: this reaches the screen, which has no consent parameter to
    check them against. A judge reads an answer through `pending` and `claim`. Derived from
    the claim and verdict rows and the clock -- `judging.claim_states` and
    `judging.held_submissions` -- never stored, so it cannot disagree with them.
    """

    submission_id: str
    content_id: str
    #: The dimension the task was served in, from the run's record of serving it. `None`
    #: only when that record is missing, which is damage `db check` names; absence is not
    #: guessed into a dimension.
    dimension: str | None = None
    kind: SubmissionKind
    #: The submission's own status. Only a pending submission is outstanding, so it is the
    #: one value this report can carry; a second would be a report about settled work.
    status: Literal["pending"] = "pending"
    claim_state: ClaimStage
    #: For `unclaimed`: since when it could be claimed -- the latest of the answer's
    #: arrival, the end of its last claim, and the voiding of a verdict held for it (it was
    #: held, not claimable, until then).
    unclaimed_since: str | None = None
    #: For `claimed`: the judge holding the live lease, and when the lease runs out.
    claimed_by: str | None = None
    claimed_until: str | None = None
    #: Whether a verdict for it is held until the run resumes.
    verdict_held: bool = False
    #: Claims made for it so far; judging stops after `judging.JUDGING_POLICY.max_attempts`.
    attempts: int = 0


class DimensionReport(ContractModel):
    dimension: str
    dimension_kind: str
    status: str
    #: `status` as the learner meets it: `open`, `waiting`, or `closed` (`DimensionProgress`).
    progress: DimensionProgress = "open"
    tasks_used: int
    minimum_tasks: int
    maximum_tasks: int
    families: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    boundary_probed: bool = False
    stop_reason: str | None = None
    confidence: str = "low"
    estimated_level: str | None = None
    credible_low: str | None = None
    credible_high: str | None = None
    posterior_mean: float | None = None
    uncertainty: float | None = None
    unavailable_reason: str | None = None


class NextTaskReport(ContractModel):
    run_id: str
    dimension: str
    sequence: int
    content_id: str
    stable_key: str
    task_type: str
    modality: str
    level_code: str
    difficulty: float
    content_family: str
    prompt: str
    #: What the learner is shown, exactly as it was snapshotted -- with a shuffled order
    #: already resolved, so a resumed task cannot reshuffle. `None` means free text.
    presentation: TaskPresentation | None = None
    #: The recording the learner heard, as `(content id, sha256)`. The digest is the
    #: half that answers "is this the same recording", so both are kept.
    asset: ServedAsset | None = None
    rubric: dict[str, object] = Field(default_factory=dict)
    rubric_version: int = 1
    #: `None` only when this report hands back a task served before migration 0032, whose
    #: snapshot holds no allowance. A fresh serve always names one. Absence is reported as
    #: absence rather than as `"none"`, which would be a claim that help was refused.
    permitted_help: str | None = "none"
    selection_reason: str = "informativeness"
    remaining_open_dimensions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: True when this is the task the run was already holding, handed back rather than
    #: selected. Nothing was written, no exposure was counted, and the posterior has not
    #: moved -- a caller that treated it as a new task would be crediting the learner with
    #: having faced two.
    served_again: bool = False
    #: Whether this task is still awaiting an answer. Always `served` on a fresh serve, and
    #: read from the snapshot on a hand-back -- including the replay of a key whose task has
    #: since been scored, which otherwise looked exactly like fresh work and would have put
    #: an answered question back in front of the learner.
    status: str = "served"


class WithdrawnSubmission(ContractModel):
    """A submission a command withdrew, and why -- with the held verdicts it voided.

    Defined here rather than in `judging`, which imports this module: a run report names
    the submissions a transition withdrew, and a model a field is typed with has to exist
    when the field does.
    """

    submission_id: str
    content_id: str
    code: str
    reason: str
    voided_verdicts: tuple[str, ...] = ()


class AppliedVerdict(ContractModel):
    """A held verdict a resume revalidated and applied: the result it became."""

    verdict_id: str
    submission_id: str
    content_id: str
    result_id: str
    score: float


class VoidedVerdict(ContractModel):
    """A verdict that will never be applied, and why -- by the code a skill acts on and
    in the words a learner is shown."""

    verdict_id: str
    submission_id: str
    content_id: str
    code: str
    reason: str


class AssessmentRunReport(ContractModel):
    run_id: str
    track_id: str
    run_type: str
    calibration_label: str
    status: str
    algorithm_version: str
    pack_key: str
    pack_version: str
    pack_maturity: str
    framework_id: str
    framework_levels: tuple[str, ...]
    declared_level: str | None
    available_modalities: tuple[str, ...]
    dimensions: tuple[DimensionReport, ...]
    #: `any` or `machine`: which tasks this run may serve, as it was opened. A client reads
    #: it to know whether a task it is handed can need a judge.
    scoring: str = DEFAULT_SCORING
    tasks_served: int = 0
    #: Results that still stand. Invalidated ones -- their recording was purged -- are
    #: counted separately, never as recorded.
    tasks_recorded: int = 0
    results_invalidated: int = 0
    stop_reason: str | None = None
    started_at: str | None = None
    finalized_at: str | None = None
    untested_dimensions: tuple[str, ...] = ()
    #: What is left to do in the run (`RunProgress`): `working`, `waiting`, `complete`, or
    #: `closed`. Waiting is never hidden: it is a state of its own, and
    #: `outstanding_judgements` says what it is waiting on.
    progress: RunProgress = "working"
    #: Every answer in the run waiting on a judgement -- pending, claimed, or with a verdict
    #: held -- in the order the answers arrived. Bounded by the one-outstanding-task-per-
    #: dimension guard, as the screen's outstanding tasks are.
    outstanding_judgements: tuple[OutstandingJudgement, ...] = ()
    warnings: tuple[str, ...] = ()
    #: True when the verdict this call delivered (or replayed) is *held*: stored against a
    #: paused run, not yet applied, and revalidated when the run resumes. Nothing about the
    #: learner has changed, and a caller reading the counts above would otherwise think its
    #: verdict was lost.
    held: bool = False
    #: The verdict this call delivered or replayed, when it was bound to a submission, and
    #: what became of it: `held`, `applied`, or `void`. A replay reports the state now, so a
    #: verdict held when it arrived and applied at resume replays as applied.
    verdict_id: str | None = None
    verdict_status: str | None = None
    #: What a transition did to the judgements still outstanding when it ran, so nothing
    #: about the learner's answers changes without the report saying so. A resume applies
    #: held verdicts that still revalidate (`applied_verdicts`) and voids the rest; abandoning
    #: withdraws every pending submission (`withdrawn`); finalizing with
    #: `exclude_outstanding` withdraws them as `excluded`. `voided_verdicts` is every verdict
    #: the transition voided, whichever of those it came through.
    applied_verdicts: tuple[AppliedVerdict, ...] = ()
    voided_verdicts: tuple[VoidedVerdict, ...] = ()
    withdrawn: tuple[WithdrawnSubmission, ...] = ()
    excluded: tuple[WithdrawnSubmission, ...] = ()


def _grid_json(state: DimensionState) -> str:
    return json.dumps({"step": 0.5, "points": list(state.grid)})


def _state_rows(state: DimensionState) -> Mapping[str, object]:
    return {
        "grid_json": _grid_json(state),
        "prior_json": json.dumps(list(state.prior)),
        "posterior_json": json.dumps(list(state.posterior)),
        "families_json": json.dumps(
            {"families": list(state.families), "task_types": list(state.task_types)}
        ),
    }


def _load_state(row: Sequence[Any], *, dimension_kind: str) -> DimensionState:
    grid = tuple(float(value) for value in json.loads(str(row[4]))["points"])
    coverage = json.loads(str(row[10]))
    return DimensionState(
        dimension=str(row[1]),
        dimension_kind=dimension_kind,
        grid=grid,
        prior=tuple(float(value) for value in json.loads(str(row[5]))),
        posterior=tuple(float(value) for value in json.loads(str(row[6]))),
        minimum_tasks=int(row[7]),
        maximum_tasks=int(row[8]),
        tasks_used=int(row[9]),
        families=tuple(str(value) for value in coverage.get("families", ())),
        task_types=tuple(str(value) for value in coverage.get("task_types", ())),
        probed_above=bool(coverage.get("probed_above", False)),
        probed_below=bool(coverage.get("probed_below", False)),
        connected_speech_used=int(coverage.get("connected_speech_used", 0)),
        status=str(row[3]),
        stop_reason=None if row[12] is None else str(row[12]),
    )


def _coverage_json(state: DimensionState) -> str:
    return json.dumps(
        {
            "families": list(state.families),
            "task_types": list(state.task_types),
            "probed_above": state.probed_above,
            "probed_below": state.probed_below,
            "connected_speech_used": state.connected_speech_used,
        }
    )


def _write_state(
    database: Database, *, run_id: str, state: DimensionState, levels: Sequence[str], insert: bool
) -> None:
    level, low, high = estimated_level(state, levels)
    rows = _state_rows(state)
    now = database.now()
    if insert:
        database.execute(
            "INSERT INTO placement_dimension_state (run_id, dimension, algorithm_version, "
            "status, grid_json, prior_json, posterior_json, minimum_tasks, maximum_tasks, "
            "tasks_used, families_json, boundary_probed, stop_reason, confidence_label, "
            "estimated_level, credible_low, credible_high, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                state.dimension,
                ALGORITHM_VERSION,
                state.status,
                rows["grid_json"],
                rows["prior_json"],
                rows["posterior_json"],
                state.minimum_tasks,
                state.maximum_tasks,
                state.tasks_used,
                _coverage_json(state),
                state.boundary_probed,
                state.stop_reason,
                confidence_label(state),
                level,
                low,
                high,
                now,
            ],
        )
        return
    database.execute(
        "UPDATE placement_dimension_state SET status = ?, posterior_json = ?, tasks_used = ?, "
        "families_json = ?, boundary_probed = ?, stop_reason = ?, confidence_label = ?, "
        "estimated_level = ?, credible_low = ?, credible_high = ?, updated_at = ? "
        "WHERE run_id = ? AND dimension = ?",
        [
            state.status,
            rows["posterior_json"],
            state.tasks_used,
            _coverage_json(state),
            state.boundary_probed,
            state.stop_reason,
            confidence_label(state),
            level,
            low,
            high,
            now,
            run_id,
            state.dimension,
        ],
    )


def _run_row(database: Database, run_id: str) -> Sequence[Any]:
    row = database.one(
        "SELECT run_id, track_id, definition_id, run_type, status, algorithm_version, "
        "conditions_json, stop_reason, started_at, finalized_at FROM assessment_runs "
        "WHERE run_id = ?",
        [run_id],
    )
    if row is None:
        raise LinguaWikiError(
            "assessment_run_not_found",
            f"no assessment run with ID {run_id}",
            details=(ErrorDetail(field="run", reason="unknown run"),),
        )
    return row


def resolve_run(database: Database, run: str | None, *, track_id: str | None = None) -> str:
    if run is not None:
        return str(_run_row(database, run)[0])
    parameters: list[object] = []
    clause = "WHERE status IN ('in-progress', 'paused')"
    if track_id is not None:
        clause += " AND track_id = ?"
        parameters.append(track_id)
    rows = database.query(
        f"SELECT run_id FROM assessment_runs {clause} ORDER BY started_at DESC", parameters
    )
    if len(rows) == 1:
        return str(rows[0][0])
    raise LinguaWikiError(
        "assessment_run_selection_required",
        f"name a run explicitly: {len(rows)} runs are open",
        details=(ErrorDetail(field="run", reason="ambiguous run selection"),),
    )


def _candidates(database: Database, pack_id: str) -> tuple[Candidate, ...]:
    placeholders = ", ".join("?" for _ in PROMOTED_LIFECYCLES)
    return tuple(
        Candidate(
            content_id=str(row[0]),
            dimension=str(row[1]),
            task_type=str(row[2]),
            difficulty=float(row[3]),
            content_family=str(row[4]),
            modality=str(row[5]),
            level_code=str(row[6]),
            is_anchor=bool(row[7]),
        )
        for row in database.query(
            "SELECT task.content_id, task.dimension, task.task_type, task.difficulty, "
            "task.content_family, task.modality, task.level_code, task.is_anchor "
            "FROM assessment_tasks task "
            "JOIN content_records record ON record.content_id = task.content_id "
            f"WHERE record.pack_id = ? AND record.lifecycle IN ({placeholders}) "
            "AND NOT record.quarantined ORDER BY record.stable_key",
            [pack_id, *PROMOTED_LIFECYCLES],
        )
    )


def _recorded_task_ids(database: Database, pack_id: str) -> frozenset[str]:
    """The bank tasks whose presentation declares a recording to play.

    Read through the one presentation parser, and a record it refuses counts as *not*
    recorded rather than raising: this answers "may a machine run serve it", and a task
    whose presentation cannot be read cannot be served either -- the serve path refuses it
    by name, which is the better place for the message.
    """

    recorded: set[str] = set()
    for content_id, raw in database.query(
        "SELECT task.content_id, task.presentation_json FROM assessment_tasks task "
        "JOIN content_records record ON record.content_id = task.content_id "
        "WHERE record.pack_id = ? AND task.presentation_json IS NOT NULL",
        [pack_id],
    ):
        try:
            shown = parse_task_presentation(str(raw))
        except LinguaWikiError:
            continue
        if shown is not None and shown.audio is not None:
            recorded.add(str(content_id))
    return frozenset(recorded)


def _run_scoring(conditions: Mapping[str, Any]) -> str:
    """The scoring condition a run was opened under. A run older than it is `any`."""

    return str(conditions.get("scoring") or DEFAULT_SCORING)


def _excluded_task_ids(
    database: Database, *, track_id: str, run_id: str, clock: Clock
) -> tuple[str, ...]:
    """Tasks this learner may not receive: already in this run, or recently exposed.

    A placement item stays unavailable for six months unless it is an explicitly
    designated longitudinal anchor, so improvement cannot be mere memorisation.
    """

    cutoff = clock.now() - timedelta(days=30 * REUSE_WINDOW_MONTHS)
    served = {
        str(content_id)
        for (content_id,) in database.query(
            "SELECT content_id FROM assessment_run_tasks WHERE run_id = ?", [run_id]
        )
    }
    recent = {
        str(content_id)
        for (content_id,) in database.query(
            "SELECT exposure.content_id FROM assessment_item_exposures exposure "
            "WHERE exposure.track_id = ? AND NOT exposure.is_anchor "
            "AND exposure.last_exposed_at >= ?",
            [track_id, cutoff.replace(tzinfo=None)],
        )
    }
    return tuple(sorted(served | recent))


def outstanding_task_ids(database: Database, run_id: str) -> tuple[str, ...]:
    """Every task this run served and has not settled, in the order it served them.

    Separate from `_outstanding_dimensions`, which answers "may this dimension be served
    again" and therefore keys by dimension -- collapsing two outstanding tasks in one
    dimension into one, which is the right answer to *that* question and would silently
    hide answerable work from a screen. One structure, one question.
    """

    return tuple(
        str(content_id)
        for (content_id,) in database.query(
            "SELECT content_id FROM assessment_run_tasks "
            "WHERE run_id = ? AND status = 'served' ORDER BY sequence",
            [run_id],
        )
    )


def _reported(result: AssessmentRunReport, warnings: Sequence[str]) -> AssessmentRunReport:
    """A run report carrying the warnings the command collected on its way to writing."""

    return result.model_copy(update={"warnings": (*result.warnings, *warnings)})


def _outstanding_dimensions(database: Database, run_id: str) -> Mapping[str, str]:
    """Dimensions holding a task that was served and never settled, to their task.

    `_excluded_task_ids` excludes served *content*; nothing excluded the *dimension*, so
    two serves with different idempotency keys selected two tasks in one dimension before
    either answer moved the posterior -- and `select_task` probes the boundary of the
    current posterior, so both probed the same place and one of them was wasted exposure.

    `skipped` and `answered` are settled. Only `served` is outstanding.
    """

    return {
        str(dimension): str(content_id)
        for dimension, content_id in database.query(
            "SELECT dimension, content_id FROM assessment_run_tasks "
            "WHERE run_id = ? AND status = 'served' ORDER BY sequence",
            [run_id],
        )
    }


#: The event types a keyed assessment operation records. They are the names an
#: `idempotency_conflict` reports, so a caller can tell "this key already served you a
#: task" from "this key already scored one" -- which is the whole point of refusing a key
#: that belongs to another operation.
STARTED_EVENT = "assessment.started"
SERVED_EVENT = "assessment.served"
RECORDED_EVENT = "assessment.recorded"
PLAYED_EVENT = "assessment.played"
#: A batch: one task per free open dimension, served in one transaction under one key.
BATCHED_EVENT = "assessment.batched"
FINALIZED_EVENT = "assessment.finalized"


#: Who drove a mutation, for `audit_log.actor`. The *command* name stays the same across
#: entry points -- ADR 0008 requires the trail to read as a CLI-driven one, and a different
#: name per surface would make every audit query ask twice -- so the surface is recorded
#: here instead. `cli` is the default because the CLI is the entry point that existed first
#: and a caller that forgets to say is, in practice, the CLI.
DEFAULT_ACTOR = "cli"

#: The surfaces that record every play of a recording before it is heard. Only they can
#: claim *zero* -- that the learner answered without listening -- so a result with no play
#: rows gets a count from them and `NULL` from any other surface, which is the truth about
#: a CLI or skill that never saw the plays. Recorded plays count whoever records the result.
PLAY_TRACKING_ACTORS: frozenset[str] = frozenset({"client"})


#: Why a report names the task it names. `informativeness` is selection having run;
#: `outstanding` is the run handing back what it was already holding, which is not a
#: selection at all -- letting the default stand would credit the report to a computation
#: nobody performed.
HANDED_BACK = "outstanding"


#: Which run statuses each status may become. A closed run is closed: `finalized` is the
#: only end an estimate may be written from, and `abandoned` is the end of the run. Both
#: are terminal, so a resumed abandonment cannot quietly turn into a finalized estimate
#: built from evidence the learner walked away from.
RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "in-progress": frozenset({"in-progress", "paused", "finalized", "abandoned"}),
    "paused": frozenset({"in-progress", "paused", "finalized", "abandoned"}),
    "finalized": frozenset(),
    "abandoned": frozenset(),
}


#: The only status a run may be worked in. `RUN_TRANSITIONS` says what a status may
#: *become*, which is a different question: `paused` may become `in-progress`, and reading
#: "has an outgoing transition" as "is open" let a paused run be served and scored without
#: ever being resumed.
RUNNING_STATUS = "in-progress"


def _pinned_levels(
    conditions: Mapping[str, Any], *, fallback: Sequence[str] = ()
) -> tuple[str, ...]:
    """The ordered level list this run was opened against.

    `estimated_level` turns a posterior median into a band by *index*, so the list is
    part of the run's scale, not a lookup that may be refreshed. Reading the track's
    current list instead meant that narrowing a pack from A1-C2 to A1-B2 silently
    restated an already-scored run's C1 as B2 -- while the report still named the pack
    version the run actually used.
    """

    recorded = conditions.get("framework_levels")
    if recorded:
        return tuple(str(code) for code in recorded)
    return tuple(fallback)


def _served_candidate(run_id: str, *, content_id: str, served: Sequence[Any]) -> Candidate:
    """Rebuild the task as it was served, from the facts recorded when it was served."""

    if served[5] is None:
        # Only reachable for a row written before migration 0016, which no release
        # shipped. Refusing beats guessing the difficulty of a task nobody recorded.
        raise LinguaWikiError(
            "assessment_task_unrecorded",
            f"run {run_id} recorded no difficulty for the task it served, so the answer "
            "cannot be scored; abandon the run and start a new one",
            details=(ErrorDetail(field="content_id", reason="served facts are missing"),),
        )
    return Candidate(
        content_id=content_id,
        dimension=str(served[1]),
        task_type=str(served[3]),
        difficulty=float(served[5]),
        content_family=str(served[6]),
        modality=str(served[7]),
        level_code=str(served[4]),
        is_anchor=bool(served[8]),
    )


def _assert_same_bank(
    run_id: str, *, conditions: Mapping[str, Any], pack_row: Mapping[str, str]
) -> None:
    """Refuse to serve from a bank that is not the one this run started against.

    Every posterior in the run was built from difficulties, families, and rubrics of one
    pack revision. Serving from a newer revision mixes two scales into one estimate, and
    the estimate then describes no bank in particular. Finalize or abandon the run and
    start a new one against the new pack.
    """

    pinned = conditions.get("pack_content_address")
    if pinned is None or str(pinned) == pack_row["checksum"]:
        return
    raise LinguaWikiError(
        "assessment_pack_drifted",
        f"run {run_id} started against {conditions.get('pack_key')} "
        f"{conditions.get('pack_version')} and that pack has since changed; finalize or "
        "abandon this run and start a new one against the installed pack",
        details=(
            ErrorDetail(
                field="pack",
                reason="installed pack differs from the one the run started against",
                context={"started": str(pinned), "installed": pack_row["checksum"]},
            ),
        ),
    )


def _assert_running(run_id: str, *, status: str, action: str = "serve another task") -> None:
    if status == RUNNING_STATUS:
        return
    resumable = RUNNING_STATUS in RUN_TRANSITIONS.get(status, frozenset())
    raise LinguaWikiError(
        "assessment_run_paused" if resumable else "assessment_run_closed",
        f"run {run_id} is {status}, so it cannot {action}"
        + ("; resume it first" if resumable else "; start a new run to test again"),
        details=(ErrorDetail(field="run", reason=f"run is {status}"),),
    )


def _settle_lapsed(
    database: Database,
    run_id: str,
    *,
    command: str,
    actor: str,
    sparing_claim: str | None = None,
) -> tuple[WithdrawnSubmission, ...]:
    """`judging.sweep_lapsed`, what every writer touching a run does before its own work."""

    from linguawiki.services import judging

    return judging.sweep_lapsed(
        database, run_id, command=command, actor=actor, sparing_claim=sparing_claim
    )


def _noting_settled[R: AssessmentRunReport | NextTaskReport | BatchReport](
    settled: tuple[WithdrawnSubmission, ...], work: Callable[[], R]
) -> R:
    """Run a command's own work after its sweep, and make what it says name the sweep.

    A submission the sweep withdrew is the learner's answer leaving the record; a command
    that did that and reported only its own work would have acted silently. On success the
    report's warnings name each one; on a refusal its details do, because the sweep
    committed whatever the command then decided.
    """

    from linguawiki.services import judging

    if not settled:
        return work()
    try:
        result = work()
    except LinguaWikiError as failure:
        raise judging.with_settled(failure, settled) from failure
    noted = result.model_copy(
        update={"warnings": (*result.warnings, *judging.settled_warnings(settled))}
    )
    return cast(R, noted)


def assert_running(run_id: str, *, status: str, action: str) -> None:
    """Refuse work on a run that is not being worked, by the codes its callers know."""

    _assert_running(run_id, status=status, action=action)


def _assert_transition(run_id: str, *, current: str, target: str) -> None:
    allowed = RUN_TRANSITIONS.get(current, frozenset())
    if target in allowed:
        return
    raise LinguaWikiError(
        "assessment_run_closed",
        f"run {run_id} is {current}, so it cannot become {target}"
        + (f"; it may only become {sorted(allowed)}" if allowed else "; that status is final"),
        details=(
            ErrorDetail(
                field="status",
                reason="transition is not allowed",
                context={"from": current, "to": target},
            ),
        ),
    )


def _record_exposure(
    database: Database,
    *,
    track_id: str,
    content_id: str,
    purpose: str,
    is_anchor: bool,
    now: datetime,
    answered: bool = False,
) -> None:
    """Record that a task was put in front of this learner, or that it was answered.

    Serving and answering are separate counters. `next_task` calls this when it serves;
    `record` calls it with `answered=True` when a score arrives, which advances the answer
    count without pretending the item was served a second time.
    """

    existing = database.one(
        "SELECT exposure_count, answered_count FROM assessment_item_exposures "
        "WHERE track_id = ? AND content_id = ?",
        [track_id, content_id],
    )
    if existing is None:
        database.execute(
            "INSERT INTO assessment_item_exposures (track_id, content_id, purpose, "
            "exposure_count, answered_count, is_anchor, first_exposed_at, last_exposed_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            [track_id, content_id, purpose, 1 if answered else 0, is_anchor, now, now],
        )
        return
    database.execute(
        "UPDATE assessment_item_exposures SET exposure_count = ?, answered_count = ?, "
        "last_exposed_at = ? WHERE track_id = ? AND content_id = ?",
        [
            int(existing[0]) if answered else int(existing[0]) + 1,
            int(existing[1]) + 1 if answered else int(existing[1]),
            now,
            track_id,
            content_id,
        ],
    )


def _available_modalities(preferences: Mapping[str, object]) -> tuple[str, ...]:
    """Which modalities this learner's equipment and consent actually allow."""

    modalities = list(BASELINE_MODALITIES)
    if preferences.get("voice_available") is not False or preferences.get(
        "audio_recording_available"
    ):
        modalities.append("speech")
    return tuple(dict.fromkeys(modalities))


def _dimension_kinds(manifest_json: str) -> Mapping[str, str]:
    from linguawiki.contracts import PackManifest

    manifest = PackManifest.model_validate(json.loads(manifest_json))
    return dict(manifest.dimension_kinds)


def _unmet_placement_requirements(
    pack_row: Mapping[str, str],
) -> tuple[coverage_module.Requirement, ...]:
    """Measure the installed pack's bank, if its directory is still readable.

    The refusal must not depend on the pack directory: a learner may have installed from a
    path that has since moved. A terse refusal naming only the maturity is better than a
    crash inside the explanation of a refusal.
    """

    for reference in (pack_row.get("source_path") or "", pack_row["pack_key"]):
        if not reference:
            continue
        try:
            pack = load_pack(resolve_pack_path(reference))
        except LinguaWikiError:
            continue
        return coverage_module.unmet_requirements(pack, PackMaturity.PLACEMENT_READY)
    return ()


def _assert_placement_bank(pack_row: Mapping[str, str], *, run_type: str) -> str:
    """Decide the calibration label, refusing a placement claim the bank cannot support."""

    maturity = pack_row["maturity"]
    if run_type != "placement":
        return "pilot-calibration"
    if maturity != PackMaturity.PLACEMENT_READY:
        unmet = _unmet_placement_requirements(pack_row)
        raise LinguaWikiError(
            "placement_bank_insufficient",
            f"{pack_row['pack_key']} is {maturity}, so comprehensive placement cannot run; "
            "start a labelled pilot calibration instead",
            details=(
                ErrorDetail(
                    field="maturity",
                    reason="pack is not placement-ready",
                    context={"maturity": maturity},
                ),
                *(
                    ErrorDetail(
                        field=requirement.name,
                        reason=f"required {requirement.required}, observed {requirement.observed}",
                        context={"band": requirement.band or ""},
                    )
                    for requirement in unmet[:20]
                ),
            ),
        )
    return "comprehensive-placement"


def start(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    run_type: str = "pilot-calibration",
    dimensions: Sequence[str] | None = None,
    modalities: Sequence[str] | None = None,
    scoring: str = DEFAULT_SCORING,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.start",
    actor: str = DEFAULT_ACTOR,
) -> AssessmentRunReport:
    """Open a bounded calibration or placement run and persist its starting state."""

    active_clock = clock or SystemClock()
    if run_type not in ("pilot-calibration", "placement"):
        raise LinguaWikiError(
            "invalid_arguments", "run_type must be pilot-calibration or placement"
        )
    assert_scoring_condition(scoring)
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        pack_row = pack_service.installed_pack(database, record.pack_key)
        label = _assert_placement_bank(pack_row, run_type=run_type)
        # Bound to a hash of the request, like every other key in this file. `start` had only
        # ever looked the key up in `assessment_runs` and handed back whatever run it found,
        # so a second call asking for *different* dimensions or modalities under one key was
        # answered with the first run and told nothing -- a key alone cannot tell a retry from
        # a reuse, which is the whole reason the hash exists.
        opening_fingerprint = idempotency.request_hash(
            operation=STARTED_EVENT,
            track_id=track_id,
            run_type=run_type,
            dimensions=sorted(dimensions) if dimensions else None,
            modalities=sorted(modalities) if modalities else None,
            # Only when it is not the default, so a keyed start made before the condition
            # existed still hashes to what it hashed to then and its retry stays a retry.
            **({} if scoring == DEFAULT_SCORING else {"scoring": scoring}),
        )
        replayed = idempotency.resolve(
            database,
            key=idempotency_key,
            event_type=STARTED_EVENT,
            request_hash=opening_fingerprint,
        )
        if replayed is not None:
            return run_report(database, str(replayed["run_id"]))
        if idempotency_key is not None:
            existing = database.one(
                "SELECT run_id FROM assessment_runs WHERE idempotency_key = ?",
                [idempotency_key],
            )
            if existing is not None:
                # A run opened before the key carried a request hash. Nothing records what
                # that call asked for, so this one cannot be shown to be a retry of it --
                # and `idempotency.resolve` refuses exactly this case, so returning the run
                # here instead contradicted the rule two lines above it. A caller asking for
                # different dimensions under a historical key was answered with the old run
                # and told nothing.
                raise LinguaWikiError(
                    "idempotency_conflict",
                    f"idempotency key {idempotency_key} already opened run {existing[0]}, "
                    "but what it was asked for was not recorded, so this call cannot be "
                    "shown to be a retry of it; use a new key, or ask for that run by id",
                    details=(
                        ErrorDetail(
                            field="idempotency_key",
                            reason="recorded request is unknown",
                            context={"run_id": str(existing[0])},
                        ),
                    ),
                )
        kinds = _dimension_kinds(pack_row["manifest_json"])
        requested = tuple(dimensions) if dimensions else tuple(kinds)
        unknown = sorted(set(requested) - set(kinds))
        if unknown:
            raise LinguaWikiError(
                "dimension_unknown",
                f"{pack_row['pack_key']} does not declare dimension(s) {unknown}",
                details=(ErrorDetail(field="dimensions", reason=", ".join(unknown)),),
            )
        available = tuple(modalities) if modalities else _available_modalities(record.preferences)
        candidates = _candidates(database, pack_row["pack_id"])
        recorded = _recorded_task_ids(database, pack_row["pack_id"])
        levels = record.framework_levels
        declared_index = (
            float(levels.index(record.declared_level)) if record.declared_level in levels else None
        )
        run_id = AssessmentId.new()
        correlation_id = EventId.new()
        states: list[DimensionState] = []
        warnings: list[str] = []
        if record.declared_level is not None and declared_index is None:
            # Falling back to a broad prior is the right arithmetic, but doing it in
            # silence let a track keep a level its pack no longer teaches and never say
            # that the declared level had stopped counting for anything.
            warnings.append(
                f"the track declares {record.declared_level}, which {pack_row['pack_key']} "
                f"{pack_row['version']} does not teach ({list(levels)}); this run starts from "
                "a broad prior instead of a declared one"
            )
        for dimension in requested:
            kind = kinds[dimension]
            bank = [candidate for candidate in candidates if candidate.dimension == dimension]
            state = initial_state(
                dimension=dimension,
                dimension_kind=kind,
                level_count=len(levels),
                declared_index=declared_index,
            )
            reason = unavailable_reason(
                dimension_kind=kind, available_modalities=available, bank_size=len(bank)
            )
            servable = [candidate for candidate in bank if candidate.modality in available]
            if reason is None and not servable:
                reason = "the pack's tasks for this dimension need an unavailable modality"
            if reason is None:
                reason = servable_under(
                    servable,
                    scoring=scoring,
                    recorded=recorded,
                    recording=recording_permitted(record.preferences),
                    writing=written_judging_permitted(record.preferences),
                )[1]
            if reason is not None:
                state = close_dimension(state, reason=reason)
                warnings.append(f"{dimension} is not tested: {reason}")
            states.append(state)
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO assessment_runs (run_id, track_id, definition_id, run_type, status, "
                "algorithm_version, conditions_json, stop_reason, idempotency_key, started_at, "
                "finalized_at, updated_at) VALUES (?, ?, NULL, ?, 'in-progress', ?, ?, NULL, ?, "
                "?, NULL, ?)",
                [
                    str(run_id),
                    track_id,
                    run_type,
                    ALGORITHM_VERSION,
                    json.dumps(
                        {
                            "calibration_label": label,
                            "pack_key": pack_row["pack_key"],
                            "pack_version": pack_row["version"],
                            "pack_maturity": pack_row["maturity"],
                            # The bank this run's evidence is comparable within. A run
                            # spanning two banks is two runs wearing one estimate.
                            "pack_content_address": pack_row["checksum"],
                            "framework_id": record.proficiency_framework,
                            # The ordered band list every estimate in this run is
                            # expressed on. It is pinned because a level's *index* is
                            # what the posterior grid means: re-reading a narrowed list
                            # later renames the same distribution to a different band.
                            "framework_levels": list(levels),
                            "available_modalities": list(available),
                            "scoring": scoring,
                            "declared_level": record.declared_level,
                            "dimension_kinds": {
                                dimension: kinds[dimension] for dimension in requested
                            },
                        },
                        sort_keys=True,
                    ),
                    idempotency_key,
                    now,
                    now,
                ],
            )
            for state in states:
                _write_state(
                    transaction, run_id=str(run_id), state=state, levels=levels, insert=True
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                actor=actor,
                affected_records_json=json.dumps([str(run_id)]),
                after_summary=f"started {label} for {track_id}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type=STARTED_EVENT,
                aggregate_type="assessment_run",
                aggregate_id=str(run_id),
                correlation_id=correlation_id,
                payload_json=idempotency.payload(
                    opening_fingerprint,
                    run_id=str(run_id),
                    run_type=run_type,
                    calibration_label=label,
                ),
                # The caller's key when there is one, so `resolve` can find it; the synthetic
                # one otherwise, which is what has always kept this event unique per run.
                idempotency_key=idempotency_key or f"assessment.started:{run_id}",
            )
        return _reported(run_report(database, str(run_id)), warnings)


def _dimension_states(
    database: Database, run_id: str, kinds: Mapping[str, str]
) -> list[DimensionState]:
    rows = database.query(
        "SELECT run_id, dimension, algorithm_version, status, grid_json, prior_json, "
        "posterior_json, minimum_tasks, maximum_tasks, tasks_used, families_json, "
        "boundary_probed, stop_reason, confidence_label, estimated_level, credible_low, "
        "credible_high FROM placement_dimension_state WHERE run_id = ? ORDER BY dimension",
        [run_id],
    )
    return [_load_state(row, dimension_kind=kinds[str(row[1])]) for row in rows]


def _serve_presentation(
    shown: TaskPresentation | None, *, run_id: str, content_id: str
) -> TaskPresentation | None:
    """Resolve everything about a presentation that a serve decides, once.

    Only the order is decided here today. A shuffle is a property of *this* serving, not
    of the bank: resolving it at read time would reshuffle a resumed task, and the
    learner would be shown a different arrangement of the question they are part-way
    through. The realized order is what is stored, so `order` comes back `fixed` --
    "already settled" rather than "the pack said fixed".

    The seed is the run and the task rather than the clock, so the same serve is
    reproducible from the record if anybody ever has to ask what happened.
    """

    if shown is None or shown.order != "shuffled":
        return shown
    order = list(shown.choices)
    random.Random(f"{run_id}:{content_id}").shuffle(order)
    return shown.model_copy(update={"choices": tuple(order), "order": "fixed"})


def _hand_back(
    database: Database, run_id: str, *, content_id: str, open_dimensions: Sequence[str]
) -> NextTaskReport:
    """The task this run is already holding, read from the record of its serving.

    Not from the bank, and not through `select_task`: selection would probe the boundary of
    a posterior no answer has moved and propose a *different* task, under a report that
    claimed the learner was being asked the one they already have open.

    Nothing is written. The exposure row was created when the task was first served, and
    incrementing it would push the item out of the six-month reuse window on the strength
    of a task the learner never answered once.
    """

    shown = served_task_report(database, run_id, content_id=content_id)
    if shown.prompt is None:
        # A row served before 0030 kept no prompt, so there is nothing to put the learner
        # in front of. Refusing names the run that cannot answer for itself; falling back
        # to the bank would ask a question this sitting has no record of having asked.
        raise LinguaWikiError(
            "assessment_task_unrecorded",
            f"run {run_id} recorded no prompt for the task it is holding, so it cannot be "
            "shown again; abandon the run and start a new one",
            details=(ErrorDetail(field="content_id", reason="served facts are missing"),),
        )
    stable_key = database.scalar(
        "SELECT stable_key FROM content_records WHERE content_id = ?", [content_id]
    )
    return NextTaskReport(
        run_id=run_id,
        dimension=shown.dimension,
        sequence=shown.sequence,
        content_id=content_id,
        # Read live, and correctly so: a content ID is derived from
        # `(pack_key, kind, stable_key)`, so the key cannot change without changing the ID
        # this row names. It is identity, not content, and does not belong in a snapshot.
        stable_key=str(stable_key),
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
        selection_reason=HANDED_BACK,
        remaining_open_dimensions=tuple(open_dimensions),
        served_again=True,
        status=shown.status,
    )


@dataclass(frozen=True, slots=True)
class ServeContext:
    """Everything a serve reads once per call, before any dimension is planned.

    What is *not* here is the point: the served set, the exposures, and the next sequence
    number are read by `plan_serve` itself, every time it is called. A batch plans each
    dimension after writing the one before it, and a context that cached them would let
    dimension *n* select as though dimensions 1..n-1 had served nothing.
    """

    run_id: str
    track_id: str
    #: The run's type, which is what an exposure row records as its purpose.
    purpose: str
    pack_key: str | None
    #: The run's dimensions and their kinds, in the order the run recorded them.
    kinds: Mapping[str, str]
    levels: tuple[str, ...]
    #: The bank tasks this run may serve under its own scoring condition and the learner's
    #: *current* consent -- read now, not from the run, so consent withdrawn after the run
    #: opened stops the next spoken or written judged task from being served.
    candidates: tuple[Candidate, ...]
    available: tuple[str, ...]


def _serve_context(database: Database, run_id: str, row: Sequence[Any]) -> ServeContext:
    """Read what every dimension's plan shares, refusing a run that cannot serve at all."""

    _assert_running(run_id, status=str(row[4]))
    conditions = json.loads(str(row[6]))
    kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
    record = learner_service.track_context(database, str(row[1]))
    pack_row = pack_service.installed_pack(database, record.pack_key)
    _assert_same_bank(run_id, conditions=conditions, pack_row=pack_row)
    # The run's own condition, never the caller's: a run opened for machine scoring never
    # serves a task that needs a judge, whatever the bank has come to hold.
    scoring = _run_scoring(conditions)
    recorded = (
        frozenset()
        if scoring == DEFAULT_SCORING
        else _recorded_task_ids(database, pack_row["pack_id"])
    )
    recording = recording_permitted(record.preferences)
    writing = written_judging_permitted(record.preferences)
    return ServeContext(
        run_id=run_id,
        track_id=str(row[1]),
        purpose=str(row[3]),
        pack_key=record.pack_key,
        kinds=kinds,
        levels=_pinned_levels(conditions, fallback=record.framework_levels),
        candidates=tuple(
            candidate
            for candidate in _candidates(database, pack_row["pack_id"])
            if servable_candidate(
                candidate,
                scoring=scoring,
                recorded=recorded,
                recording=recording,
                writing=writing,
            )
        ),
        available=tuple(str(value) for value in conditions["available_modalities"]),
    )


@dataclass(frozen=True, slots=True)
class ServePlan:
    """One dimension's serve, decided and not yet written.

    Everything `write_serve` stores is resolved here -- the selection, the bank row's
    snapshot, the shuffled order, the recording's identity, the sequence -- so the write
    has nothing left to refuse except what the database itself would.
    """

    run_id: str
    track_id: str
    purpose: str
    dimension: str
    sequence: int
    candidate: Candidate
    selection_reason: str
    stable_key: str
    prompt: str
    rubric_json: str
    rubric_version: int
    permitted_help: str
    content_hash: str
    target_refs_json: str
    expected_json: str
    presentation: TaskPresentation | None
    asset: ServedAsset | None

    def report(
        self, *, remaining_open_dimensions: Sequence[str], warnings: Sequence[str] = ()
    ) -> NextTaskReport:
        return NextTaskReport(
            run_id=self.run_id,
            dimension=self.dimension,
            sequence=self.sequence,
            content_id=self.candidate.content_id,
            stable_key=self.stable_key,
            task_type=self.candidate.task_type,
            modality=self.candidate.modality,
            level_code=self.candidate.level_code,
            difficulty=self.candidate.difficulty,
            content_family=self.candidate.content_family,
            prompt=self.prompt,
            presentation=self.presentation,
            asset=self.asset,
            rubric=json.loads(self.rubric_json),
            rubric_version=self.rubric_version,
            permitted_help=self.permitted_help,
            selection_reason=self.selection_reason,
            remaining_open_dimensions=tuple(remaining_open_dimensions),
            warnings=tuple(warnings),
        )


def plan_serve(
    database: Database, context: ServeContext, state: DimensionState, *, clock: Clock
) -> ServePlan | None:
    """Decide one dimension's next task, or `None` when the bank has nothing left for it.

    A read, so it runs inside the caller's transaction and sees what that transaction has
    already written: the exclusions and the next sequence number are read here on every
    call, never carried in from an earlier one. That is what lets a batch plan its second
    dimension after writing its first and select exactly what a second, separate serve would.
    """

    excluded = _excluded_task_ids(
        database, track_id=context.track_id, run_id=context.run_id, clock=clock
    )
    selection = select_task(
        state, context.candidates, available_modalities=context.available, excluded=excluded
    )
    if selection is None:
        return None
    # The answer key is read here, from the bank row, and never from the selected
    # `Candidate`. `Candidate` carries everything item selection is allowed to consider, and
    # the key is not among it: putting it there would let the staircase see the answers it
    # is choosing between.
    task = database.one(
        "SELECT record.stable_key, task.prompt, task.rubric_json, task.rubric_version, "
        "task.permitted_help, record.content_hash, task.target_refs_json, "
        "task.expected_json, task.presentation_json "
        "FROM assessment_tasks task "
        "JOIN content_records record ON record.content_id = task.content_id "
        "WHERE task.content_id = ?",
        [selection.candidate.content_id],
    )
    assert task is not None
    # Resolved here, before anything is written: a shuffle happens once, and the order the
    # learner saw is the order that is stored. Re-reading the bank on resume would
    # reshuffle, which is a different question asked under the identity of the one they
    # were credited for.
    shown = _serve_presentation(
        parse_task_presentation(None if task[8] is None else str(task[8])),
        run_id=context.run_id,
        content_id=selection.candidate.content_id,
    )
    # Resolved here too: a task that says it plays a recording the installed pack cannot
    # produce is refused while it is still unserved, rather than written as a row nothing
    # can read afterwards.
    played = None
    if _plays_audio(shown):
        played = _serve_asset_identity(database, context.pack_key, shown)
    sequence = (
        int(
            database.scalar(
                "SELECT coalesce(max(sequence), 0) FROM assessment_run_tasks WHERE run_id = ?",
                [context.run_id],
            )
        )
        + 1
    )
    return ServePlan(
        run_id=context.run_id,
        track_id=context.track_id,
        purpose=context.purpose,
        dimension=state.dimension,
        sequence=sequence,
        candidate=selection.candidate,
        selection_reason=selection.reason,
        stable_key=str(task[0]),
        prompt=str(task[1]),
        rubric_json=str(task[2]),
        rubric_version=int(task[3]),
        permitted_help=str(task[4]),
        content_hash=str(task[5]),
        target_refs_json=str(task[6]),
        expected_json=str(task[7]),
        presentation=shown,
        asset=played,
    )


def write_serve(
    transaction: Database, plan: ServePlan, *, now: datetime, command: str, actor: str
) -> None:
    """Write one planned serve inside the caller's transaction: run task, exposure, audit.

    Opens no transaction of its own, because DuckDB forbids nesting them and a batch has to
    write every dimension's serve in one. The keyed event is the caller's, since what a key
    records -- one task, or a batch's membership -- depends on which operation it keys.
    """

    candidate = plan.candidate
    # The served facts are copied here, not re-read later. A pack is mutable and a run is
    # not: the score must fold in the difficulty of the task the learner actually saw, and
    # an observation attributed to this task must be about the items it targeted when it
    # was served -- not the ones a later pack edit says it targets now.
    #
    # The same reasoning is why the answer key, the prompt, and the rubric body are copied:
    # they are what a scorer needs to reach a verdict, and scoring against the live bank
    # would let an edit made after the sitting decide whether the learner was right. The
    # three are written together, always: a row carrying some of them and not the others
    # can establish neither what the learner faced nor that it predates the snapshot.
    transaction.execute(
        "INSERT INTO assessment_run_tasks (run_id, sequence, content_id, "
        "dimension, status, served_at, task_type, level_code, difficulty, "
        "content_family, modality, is_anchor, rubric_version, content_hash, "
        "target_refs_json, "
        "expected_json, prompt_snapshot, rubric_json, presentation_json, "
        "asset_identity_json, permitted_help) "
        "VALUES (?, ?, ?, ?, 'served', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?)",
        [
            plan.run_id,
            plan.sequence,
            candidate.content_id,
            plan.dimension,
            now,
            candidate.task_type,
            candidate.level_code,
            candidate.difficulty,
            candidate.content_family,
            candidate.modality,
            candidate.is_anchor,
            plan.rubric_version,
            plan.content_hash,
            plan.target_refs_json,
            plan.expected_json,
            plan.prompt,
            plan.rubric_json,
            None
            if plan.presentation is None
            else json.dumps(
                plan.presentation.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            ),
            None
            if plan.asset is None
            else json.dumps(plan.asset.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            # The allowance the learner was held to, from the same row the report hands
            # back -- so the stored value and the reported one cannot disagree. Snapshotted
            # for the same reason as the prompt: a second serve of this task must not read
            # it from a pack that has been edited since.
            plan.permitted_help,
        ],
    )
    # The learner has now seen it, whether or not they answer. Recording the exposure here,
    # in the same transaction, is what keeps an abandoned run from handing the same task
    # back inside the reuse window.
    _record_exposure(
        transaction,
        track_id=plan.track_id,
        content_id=candidate.content_id,
        purpose=plan.purpose,
        is_anchor=candidate.is_anchor,
        now=now,
    )
    # Serving is a mutation -- it writes a run task and spends an exposure -- and it wrote
    # no audit row until C3. Putting it here rather than in the server is what keeps the two
    # entry points' trails identical: a guard or a record the server owns is one the CLI
    # does not have.
    migration_module.record_audit_entry(
        transaction,
        command=command,
        correlation_id=EventId.new(),
        outcome="succeeded",
        actor=actor,
        affected_records_json=json.dumps([plan.run_id, candidate.content_id], sort_keys=True),
        after_summary=(f"served {candidate.content_id} as {plan.dimension} task {plan.sequence}"),
    )


#: Why a dimension a serve found open has no task to show for it.
EXHAUSTED_REASON = "bank exhausted inside the reuse window"


def _close_exhausted(transaction: Database, context: ServeContext, state: DimensionState) -> str:
    """Close a dimension the bank can no longer serve, and say so in a warning."""

    _write_state(
        transaction,
        run_id=context.run_id,
        state=close_dimension(state, reason=EXHAUSTED_REASON),
        levels=context.levels,
        insert=False,
    )
    return f"{state.dimension} stopped early: no unseen task is available"


def _open_dimensions(database: Database, run_id: str, row: Sequence[Any]) -> list[str]:
    kinds = {str(k): str(v) for k, v in json.loads(str(row[6]))["dimension_kinds"].items()}
    return [
        state.dimension
        for state in _dimension_states(database, run_id, kinds)
        if state.status == "open"
    ]


def next_task(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.next",
    actor: str = DEFAULT_ACTOR,
    idempotency_key: str | None = None,
) -> NextTaskReport | AssessmentRunReport:
    """Serve the next task, or report the run when no dimension is still open -- or when
    every open one is waiting on a judge, which the report's `progress` says.

    Plan and write in one transaction. Until C6 a dimension the bank had run out of was
    closed in a transaction of its own and the serve committed in another, so a refusal
    planning the second dimension left the first one closed by a call that then failed.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
        # Before serving: a submission whose judging attempts all lapsed is holding its
        # dimension, and settling it is what lets the dimension serve again.
        settled = _settle_lapsed(database, run_id, command=command, actor=actor)

        # The command's own work, after the sweep: whatever it reports -- or refuses
        # with -- names the submissions the sweep withdrew.
        def work() -> NextTaskReport | AssessmentRunReport:
            row = _run_row(database, run_id)
            # Before `_assert_running`, deliberately: a retry of the serve that *closed* the
            # last dimension must replay rather than being told the run is finished, and a
            # conflicting key must be refused whatever state the run has reached since.
            fingerprint = idempotency.request_hash(operation=SERVED_EVENT, run_id=run_id)
            replay = idempotency.resolve(
                database,
                key=idempotency_key,
                event_type=SERVED_EVENT,
                request_hash=fingerprint,
            )
            if replay is not None:
                recorded = replay.get("content_id")
                if recorded is None:
                    # The key recorded a serve that served nothing: the call closed the last
                    # open dimension and returned the run. A retry gets the same answer.
                    return run_report(database, run_id)
                return _hand_back(
                    database,
                    run_id,
                    content_id=str(recorded),
                    open_dimensions=_open_dimensions(database, run_id, row),
                )
            context = _serve_context(database, run_id, row)
            states = _dimension_states(database, run_id, context.kinds)
            open_states = [state for state in states if state.status == "open"]
            if not open_states:
                return run_report(database, run_id)
            # Serve the least-progressed open dimension first, so a run that stops early has
            # spread its evidence rather than finishing one dimension and testing no other.
            open_states.sort(key=lambda state: (state.tasks_used, state.dimension))
            # A dimension holding an unanswered task is not eligible for another. The order
            # among the rest is unchanged, so the spread rule above still decides which of the
            # *free* dimensions goes first.
            outstanding = _outstanding_dimensions(database, run_id)
            free_states = [state for state in open_states if state.dimension not in outstanding]
            warnings: list[str] = []
            with database.transaction() as transaction:
                for state in free_states:
                    plan = plan_serve(transaction, context, state, clock=active_clock)
                    if plan is None:
                        warnings.append(_close_exhausted(transaction, context, state))
                        continue
                    write_serve(
                        transaction, plan, now=transaction.now(), command=command, actor=actor
                    )
                    if idempotency_key is not None:
                        migration_module.record_domain_event(
                            transaction,
                            event_type=SERVED_EVENT,
                            aggregate_type="assessment_run",
                            aggregate_id=run_id,
                            correlation_id=EventId.new(),
                            payload_json=idempotency.payload(
                                fingerprint,
                                content_id=plan.candidate.content_id,
                                dimension=plan.dimension,
                                sequence=plan.sequence,
                            ),
                            idempotency_key=idempotency_key,
                        )
                    return plan.report(
                        remaining_open_dimensions=[other.dimension for other in open_states],
                        warnings=warnings,
                    )
                # Nothing fresh could be served. An open dimension still holding an unanswered
                # task has work on it, so the run is not finished and must not report as though
                # it were: hand that task back instead. The states are already sorted, so this
                # is the least-progressed one.
                #
                # Only a task still awaiting the learner. One whose answer is handed in and
                # waiting for a judge is not work for the learner, and handing it back as a
                # task to answer invited a second answer to a question already answered. When
                # every open dimension is waiting on a judge, nothing is served and the run
                # report says `waiting`, naming what it waits on.
                from linguawiki.services import recordings as recording_service

                held = [
                    state
                    for state in open_states
                    if state.dimension in outstanding
                    and recording_service.live_submission(
                        transaction, run_id, outstanding[state.dimension]
                    )
                    is None
                ]
                handed_content_id = outstanding[held[0].dimension] if held else None
                # Read and validated *before* the key is claimed. `_hand_back` refuses a
                # damaged snapshot, and a refusal has to leave nothing behind -- which, now
                # that it runs inside the transaction, includes the exhausted dimensions
                # closed above.
                handed = (
                    None
                    if handed_content_id is None
                    else _hand_back(
                        transaction,
                        run_id,
                        content_id=handed_content_id,
                        open_dimensions=[state.dimension for state in held],
                    )
                )
                if idempotency_key is not None:
                    # Whatever this call did -- handed a task back, or closed the last
                    # dimension and served nothing -- it is the one operation this key
                    # performed, and recording it is what stops a retry from doing something
                    # else. A hand-back writes nothing about the *learner*: no run task, no
                    # exposure, no dimension state. An event saying which task this key was
                    # answered with is bookkeeping about the request, and without it a retry
                    # after the task was scored went on to serve a different one under the
                    # same key.
                    migration_module.record_domain_event(
                        transaction,
                        event_type=SERVED_EVENT,
                        aggregate_type="assessment_run",
                        aggregate_id=run_id,
                        correlation_id=EventId.new(),
                        payload_json=idempotency.payload(fingerprint, content_id=handed_content_id),
                        idempotency_key=idempotency_key,
                    )
            if handed is not None:
                return handed.model_copy(update={"warnings": tuple(warnings)})
            return _reported(run_report(database, run_id), warnings)

        return _noting_settled(settled, work)


#: Where a batch's task stands now. `served` is still waiting for the learner;
#: `awaiting-judge` has an answer handed in that nobody has marked -- derived exactly as the
#: run screen derives it, from the live submission -- and `answered` and `skipped` are
#: settled.
BatchTaskState = Literal["served", "answered", "awaiting-judge", "skipped"]


class BatchTask(ContractModel):
    """One task a batch served, in the position the batch served it."""

    position: int
    dimension: str
    content_id: str
    state: BatchTaskState
    #: The task as it was served, read from the record of the serving on a replay.
    task: NextTaskReport


class BatchReport(ContractModel):
    """One task for each open dimension, served together -- or replayed by their key."""

    batch_id: str
    run_id: str
    tasks: tuple[BatchTask, ...] = ()
    #: Open dimensions the batch served nothing in because each was already holding an
    #: outstanding task -- unanswered, or answered and awaiting a judge. Not a refusal:
    #: `select_task` must not probe a posterior no answer has moved yet. Not "waiting"
    #: either: an unanswered task is the learner's move, and whether a dimension is blocked
    #: on a judge is `DimensionReport.progress`, the one meaning that word has.
    outstanding: tuple[str, ...] = ()
    #: Dimensions the batch found open with nothing left in the bank to serve, and closed.
    exhausted: tuple[str, ...] = ()
    #: True when this report is a retry of the call that made the batch: nothing was
    #: served, and each task's state is what it is now, not what it was then.
    replayed: bool = False
    warnings: tuple[str, ...] = ()


def _batch_task_state(database: Database, run_id: str, content_id: str) -> BatchTaskState:
    """Where a served task stands now, by the run task's status and its live submission.

    `awaiting-judge` is the run screen's own derivation (`assessment_view.run_screen_report`):
    a task still `served` whose answer has been handed in. A second account of "is this
    waiting for a judge" is a second thing to drift.
    """

    from linguawiki.services import recordings as recording_service

    stored = database.scalar(
        "SELECT status FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
        [run_id, content_id],
    )
    if stored is None:
        # Only damage reaches this: the member and its serving were written together. A
        # state guessed for a task the run never served would be a report about nothing.
        raise LinguaWikiError(
            "assessment_batch_unrecorded",
            f"a batch on run {run_id} names {content_id}, which the run has no record of "
            "serving; run `db check` (batch_members_were_served_by_their_run), and serve "
            "with a new key",
            details=(ErrorDetail(field="content_id", reason="batch member was never served"),),
        )
    status = str(stored)
    if status == "served" and (
        recording_service.live_submission(database, run_id, content_id) is not None
    ):
        return "awaiting-judge"
    return cast(BatchTaskState, status)


def _naming_dimension(failure: LinguaWikiError, dimension: str) -> LinguaWikiError:
    """A dimension's refusal, as the batch's refusal: same code, naming the dimension.

    The code is kept because it is the contract a caller acts on; the dimension is added
    because "which of the five" is the first thing a caller holding a batch needs to know,
    and nothing else in the refusal says it.
    """

    payload = failure.payload
    return LinguaWikiError(
        payload.code,
        f"{dimension}: {payload.message}; nothing in this batch was served",
        retryable=payload.retryable,
        details=(
            ErrorDetail(
                field="dimension",
                reason="this dimension's serve was refused",
                context={"dimension": dimension},
            ),
            *payload.details,
        ),
    )


def _assert_batch_key(idempotency_key: str | None) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise LinguaWikiError(
            "invalid_arguments",
            "a batch needs an idempotency key: it serves several tasks at once, and a retry "
            "after a lost response must return those tasks rather than serve more",
            details=(ErrorDetail(field="idempotency_key", reason="absent"),),
        )
    return idempotency_key


def _replayed_batch(
    database: Database,
    run_id: str,
    row: Sequence[Any],
    *,
    key: str,
    replay: Mapping[str, Any],
    fingerprint: str,
) -> BatchReport:
    """The batch this key made, each task in its state now. Nothing is served.

    Membership is read from `assessment_batch_tasks`, never re-planned: a dimension that has
    opened since is not added, because adding it would be a second batch under the first
    one's key.
    """

    batch_id = str(replay.get("batch_id"))
    stored = database.one(
        "SELECT run_id, idempotency_key, request_hash FROM assessment_batches WHERE batch_id = ?",
        [batch_id],
    )
    if stored is None or (str(stored[0]), str(stored[1]), str(stored[2])) != (
        run_id,
        key,
        fingerprint,
    ):
        # The event and the membership are written in one transaction, so this is damage or
        # a hand repair. Serving afresh would answer the key with tasks it never served.
        raise LinguaWikiError(
            "assessment_batch_unrecorded",
            f"idempotency key {key} recorded batch {batch_id}, and the batch's own record "
            "is missing or disagrees with it; run `db check`, and serve with a new key",
            details=(ErrorDetail(field="idempotency_key", reason="batch record is missing"),),
        )
    open_dimensions = _open_dimensions(database, run_id, row)
    tasks = tuple(
        BatchTask(
            position=int(position),
            dimension=str(dimension),
            content_id=str(content_id),
            state=_batch_task_state(database, run_id, str(content_id)),
            task=_hand_back(
                database, run_id, content_id=str(content_id), open_dimensions=open_dimensions
            ),
        )
        for position, dimension, content_id in database.query(
            "SELECT position, dimension, content_id FROM assessment_batch_tasks "
            "WHERE batch_id = ? ORDER BY position",
            [batch_id],
        )
    )
    return BatchReport(
        batch_id=batch_id,
        run_id=run_id,
        tasks=tasks,
        # Batches recorded before the field was renamed stored the same list as `waiting`.
        outstanding=tuple(
            str(value) for value in replay.get("outstanding", replay.get("waiting")) or ()
        ),
        exhausted=tuple(str(value) for value in replay.get("exhausted") or ()),
        replayed=True,
    )


def next_batch(
    paths: WorkspacePaths,
    *,
    idempotency_key: str | None,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.batch",
    actor: str = DEFAULT_ACTOR,
) -> BatchReport:
    """Serve one task in every open dimension that is free, in one transaction.

    Never two in one dimension: `select_task` probes the boundary of the current posterior,
    so a dimension's second task depends on its first answer. A dimension already holding a
    task is reported `outstanding`; one the bank can no longer serve is closed and reported
    `exhausted`. Neither is a refusal. A refusal in any dimension writes nothing at all --
    no served task, no exposure, no batch -- and names the dimension.

    Dimensions are planned in the order the run recorded them, and each is planned after
    the one before it was written, inside the same transaction, so the exclusions dimension
    *n* sees include whatever dimensions 1..n-1 just served. That is what makes a batch
    select exactly what the same dimensions would have selected served one at a time.
    """

    key = _assert_batch_key(idempotency_key)
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
        settled = _settle_lapsed(database, run_id, command=command, actor=actor)

        def work() -> BatchReport:
            row = _run_row(database, run_id)
            # Before `_assert_running`, like a single serve: a retry is answered with what
            # the key did, whatever state the run has reached since.
            fingerprint = idempotency.request_hash(operation=BATCHED_EVENT, run_id=run_id)
            replay = idempotency.resolve(
                database, key=key, event_type=BATCHED_EVENT, request_hash=fingerprint
            )
            if replay is not None:
                return _replayed_batch(
                    database, run_id, row, key=key, replay=replay, fingerprint=fingerprint
                )
            context = _serve_context(database, run_id, row)
            states = {
                state.dimension: state
                for state in _dimension_states(database, run_id, context.kinds)
            }
            outstanding = _outstanding_dimensions(database, run_id)
            batch_id = str(AssessmentId.new())
            plans: list[ServePlan] = []
            holding: list[str] = []
            exhausted: list[str] = []
            warnings: list[str] = []
            with database.transaction() as transaction:
                now = transaction.now()
                transaction.execute(
                    "INSERT INTO assessment_batches (batch_id, run_id, idempotency_key, "
                    "request_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                    [batch_id, run_id, key, fingerprint, now],
                )
                # The run's recorded order -- `conditions_json.dimension_kinds` as it was
                # stored -- rather than the single serve's least-progressed-first. The spread
                # rule is moot when every free dimension is served at once, and a fixed order
                # is what lets a retry and the one-at-a-time comparison agree.
                for dimension in context.kinds:
                    state = states.get(dimension)
                    if state is None or state.status != "open":
                        continue
                    if dimension in outstanding:
                        holding.append(dimension)
                        continue
                    try:
                        plan = plan_serve(transaction, context, state, clock=active_clock)
                        if plan is None:
                            exhausted.append(dimension)
                            warnings.append(_close_exhausted(transaction, context, state))
                            continue
                        write_serve(transaction, plan, now=now, command=command, actor=actor)
                    except LinguaWikiError as failure:
                        raise _naming_dimension(failure, dimension) from failure
                    plans.append(plan)
                    transaction.execute(
                        "INSERT INTO assessment_batch_tasks (batch_id, position, content_id, "
                        "dimension) VALUES (?, ?, ?, ?)",
                        [batch_id, len(plans), plan.candidate.content_id, dimension],
                    )
                migration_module.record_domain_event(
                    transaction,
                    event_type=BATCHED_EVENT,
                    aggregate_type="assessment_run",
                    aggregate_id=run_id,
                    correlation_id=EventId.new(),
                    payload_json=idempotency.payload(
                        fingerprint,
                        batch_id=batch_id,
                        content_ids=[plan.candidate.content_id for plan in plans],
                        outstanding=holding,
                        exhausted=exhausted,
                    ),
                    idempotency_key=key,
                )
            remaining = [
                dimension
                for dimension in context.kinds
                if dimension in states
                and states[dimension].status == "open"
                and dimension not in exhausted
            ]
            return BatchReport(
                batch_id=batch_id,
                run_id=run_id,
                tasks=tuple(
                    BatchTask(
                        position=position,
                        dimension=plan.dimension,
                        content_id=plan.candidate.content_id,
                        state="served",
                        task=plan.report(remaining_open_dimensions=remaining),
                    )
                    for position, plan in enumerate(plans, start=1)
                ),
                outstanding=tuple(holding),
                exhausted=tuple(exhausted),
                warnings=tuple(warnings),
            )

        return _noting_settled(settled, work)


@dataclass(frozen=True, slots=True)
class _ScorableKey:
    """The answer key a deterministic score may rest on, and where it was read from."""

    answers: tuple[str, ...]
    #: `run-snapshot` when the run recorded the key it served, `bank` when the row
    #: predates the snapshot and the pack's content still hashes to what was served.
    source: str


def _scorable_key(
    database: Database, *, content_id: str, snapshot: Sequence[Any], content_hash: str | None
) -> _ScorableKey:
    """Read the key the learner was actually scored against, refusing every other case.

    The three snapshot columns are one group. All three present is a whole record; all
    three absent means the row predates migration 0030, and only then may the bank answer
    -- and only when it can prove it has not changed, which is exactly what the
    snapshotted `content_hash` is for. Anything in between is damage: it cannot establish
    what the learner faced and it is not a legacy row either, so falling back would let
    the mutable pack answer for a fact the record actually held.

    There is no backfill from the pack here, ever. A key written into an old row from a
    pack edited since would be an account of a sitting that nobody can check.
    """

    expected_json, prompt_snapshot, rubric_json = snapshot
    columns = (expected_json, prompt_snapshot, rubric_json)
    # Whole or wholly absent, and nothing between -- decided by the one predicate
    # `served_answer_key_complete` also asks, so the writer and the diagnostic cannot
    # describe a row two different ways.
    state = served_snapshot_state(columns)
    if state == SNAPSHOT_ABSENT:
        pass
    elif state == SNAPSHOT_WHOLE:
        key = parse_answer_key(str(expected_json))
        # The rubric body travels with the key and is part of the same record. A record
        # `db check` calls unreadable must not quietly produce a credited score.
        if not reads_as_json_object(str(rubric_json)):
            raise LinguaWikiError(
                "assessment_answer_key_malformed",
                f"the record of serving {content_id} cannot be read: its rubric body is "
                "not a JSON object",
                details=(ErrorDetail(field="rubric_json", reason="not a JSON object"),),
            )
        return _ScorableKey(key.answers, "run-snapshot")
    else:
        raise LinguaWikiError(
            "assessment_snapshot_partial",
            f"the run holds a partial record of serving {content_id}: it can establish "
            "neither what the learner was asked nor that it predates the snapshot, so it "
            "cannot be scored without a judge",
            details=(
                ErrorDetail(
                    field="content_id",
                    reason="partial served snapshot",
                    context={
                        "present": ", ".join(
                            name
                            for name, column in zip(
                                ("expected_json", "prompt_snapshot", "rubric_json"),
                                columns,
                                strict=True,
                            )
                            if column is not None
                        )
                    },
                ),
            ),
        )
    current = database.scalar(
        "SELECT record.content_hash FROM content_records record WHERE record.content_id = ?",
        [content_id],
    )
    if content_hash is None or current is None or str(current) != content_hash:
        raise LinguaWikiError(
            "assessment_score_required",
            f"{content_id} was served before its answer key was recorded, and the pack's "
            "content has changed since, so nothing here can say what the learner was "
            "asked; supply a score",
            details=(ErrorDetail(field="score", reason="no usable answer key"),),
        )
    bank = database.scalar(
        "SELECT expected_json FROM assessment_tasks WHERE content_id = ?", [content_id]
    )
    if bank is None:
        # The foreign key from `assessment_run_tasks.content_id` should make this
        # unreachable, which is exactly why it is spelled out rather than left to
        # `parse_answer_key(None)`: were the relation ever dropped or restored without
        # it, "your answer key is broken" would be the wrong account of "the pack no
        # longer holds this task", and a caller acts on the code.
        raise LinguaWikiError(
            "assessment_score_required",
            f"{content_id} was served before its answer key was recorded, and the pack no "
            "longer holds the task, so nothing here can say what the learner was asked; "
            "supply a score",
            details=(ErrorDetail(field="score", reason="the bank no longer holds the task"),),
        )
    return _ScorableKey(parse_answer_key(str(bank)).answers, "bank")


def _assert_repeat(
    database: Database,
    *,
    run_id: str,
    content_id: str,
    score: float,
    response_hash: str | None,
    visibility: str,
    audio_artifact: str | None = None,
    code: str = "assessment_result_conflict",
) -> None:
    """Accept a retry of a recorded result, and refuse a second, different one.

    Retrying is safe; overwriting is not, and neither is answering a different request
    with the first one's outcome. The observation is the score and the answer it rests on,
    so both are compared and the recorded value is named in the refusal -- silently
    keeping the first would leave the caller believing its correction was applied.

    A recorded row that kept no hash is not evidence of a different answer, so a retry
    that supplies one while agreeing on the score is still a repeat: the first call simply
    had nothing to hash.

    Retention is compared too, and that is the case with teeth: a retry asking for
    `withheld` where `full` is recorded was accepted as a repeat, so the caller was told
    its request to stop keeping the learner's words had been honoured while the text sat
    there. Nothing here can grant that request -- the row is an observation, and no command
    withdraws a stored excerpt -- so the honest answer is to refuse and name what is kept.
    """

    recorded = database.one(
        "SELECT raw_score, response_hash, response_visibility, audio_artifact_id "
        "FROM assessment_results WHERE run_id = ? AND content_id = ?",
        [run_id, content_id],
    )
    if recorded is None:
        return
    _assert_same_observation(
        content_id,
        recorded=(
            float(recorded[0]),
            None if recorded[1] is None else str(recorded[1]),
            None if recorded[2] is None else str(recorded[2]),
            None if recorded[3] is None else str(recorded[3]),
        ),
        score=score,
        response_hash=response_hash,
        visibility=visibility,
        audio_artifact=audio_artifact,
        code=code,
    )


def _assert_same_observation(
    content_id: str,
    *,
    recorded: tuple[float, str | None, str | None, str | None],
    score: float,
    response_hash: str | None,
    visibility: str,
    audio_artifact: str | None,
    code: str,
) -> None:
    """Refuse an offered observation that differs from the recorded one, naming both.

    `recorded` is `(score, response hash, visibility, recording)` from whichever account
    holds it -- a result, or a verdict held for a paused run. `code` is
    `assessment_result_conflict` for the C1 contract and `assessment_verdict_conflict` for
    a second keyed verdict on one submission.
    """

    recorded_score, recorded_hash, recorded_visibility, recorded_audio = recorded
    conflicts: list[ErrorDetail] = []
    if abs(recorded_score - score) > 1e-9:
        conflicts.append(
            ErrorDetail(
                field="score",
                reason="a different score is already recorded for this task",
                context={"recorded": str(recorded_score), "offered": str(score)},
            )
        )
    if recorded_hash is not None and response_hash is not None and recorded_hash != response_hash:
        conflicts.append(
            ErrorDetail(
                field="response",
                reason="a different answer is already recorded for this task",
                context={"recorded": recorded_hash, "offered": response_hash},
            )
        )
    if recorded_visibility is not None and recorded_visibility != visibility:
        conflicts.append(
            ErrorDetail(
                field="response_visibility",
                reason="the recorded result keeps a different amount of the answer, and a "
                "retry cannot change what was kept",
                context={"recorded": recorded_visibility, "offered": visibility},
            )
        )
    # The recording a verdict rests on is part of the observation. A repeat naming another
    # one -- or none, where one was recorded, or one where none was -- is a different
    # request, and accepting it would tell the caller its recording was the one judged.
    if recorded_audio != audio_artifact:
        conflicts.append(
            ErrorDetail(
                field="audio_artifact",
                reason="the recorded result rests on a different recording",
                context={"recorded": str(recorded_audio), "offered": str(audio_artifact)},
            )
        )
    if conflicts:
        raise LinguaWikiError(
            code,
            f"{content_id} was already answered in this run with a different result "
            f"(recorded score {recorded_score}); a retry repeats an observation, it does "
            "not replace one",
            details=tuple(conflicts),
        )


def _assert_not_invalidated(database: Database, *, run_id: str, content_id: str) -> None:
    """A task whose judged result was invalidated stays settled: a fresh measurement belongs
    to a fresh serve. Refused by its own name, never as a conflicting repeat."""

    invalidated = database.one(
        "SELECT invalidated_reason FROM assessment_results "
        "WHERE run_id = ? AND content_id = ? AND invalidated_at IS NOT NULL",
        [run_id, content_id],
    )
    if invalidated is not None:
        raise LinguaWikiError(
            "assessment_result_invalidated",
            f"{content_id} was judged in this run and the result was invalidated "
            f"({invalidated[0]}); a task is measured once per serve, so a fresh measurement "
            "belongs to a new run",
            details=(ErrorDetail(field="content_id", reason=str(invalidated[0])),),
        )


def _assert_not_withdrawn(database: Database, *, run_id: str, content_id: str, status: str) -> None:
    """Refuse a verdict for a task skipped because its submission was withdrawn.

    The submission says why -- the recording was purged, altered, or its run closed -- and
    that reason is the refusal, by the code it was withdrawn with.
    """

    if status != "skipped":
        return
    withdrawn = database.one(
        "SELECT withdrawn_code, withdrawn_reason FROM assessment_submissions "
        "WHERE run_id = ? AND content_id = ? AND status = 'withdrawn' "
        "ORDER BY updated_at DESC, submission_id DESC LIMIT 1",
        [run_id, content_id],
    )
    if withdrawn is not None:
        raise LinguaWikiError(
            str(withdrawn[0]),
            f"{content_id} can no longer be judged in this run: {withdrawn[1]}. The "
            "submission was withdrawn and the task skipped.",
            details=(ErrorDetail(field="content_id", reason="submission withdrawn"),),
        )
    raise LinguaWikiError(
        "assessment_task_settled",
        f"{content_id} was skipped in this run, so it takes no result",
        details=(ErrorDetail(field="content_id", reason="task is skipped"),),
    )


def _judged_recording(
    database: Database,
    root: Path,
    *,
    run_id: str,
    content_id: str,
    audio_artifact: str | None,
    assessor_kind: str,
    answered: bool,
    requires_recording: bool = False,
    requires_text: bool = False,
) -> JudgeableAudio | Settlement | None:
    """The submitted recording a verdict rests on, checked now -- or `None` if there is none.

    A task with a recording waiting for a judge takes a verdict only from a judge who names
    that recording. The check is `recordings.assert_judgeable`, the same one `assessment
    pending` ran when it handed the recording out, run again inside this writer. When it
    fails because of the recording itself -- purged, not kept, missing, altered, escaping,
    unreadable -- no judge can hear it any more, so the answer is a `Settlement`: the
    submission is to be withdrawn and the task skipped, and the failure raised after that
    has committed. A refusal has to leave a way forward, and a task holding its dimension
    for a verdict that can never arrive leaves none. Deciding that is a read; writing it is
    `withdrawal.settle`, which is why this returns the settlement rather than writing it.
    """

    from linguawiki.services import recordings as recording_service
    from linguawiki.services.withdrawal import Settlement

    if answered:
        return None
    live = recording_service.live_submission(database, run_id, content_id)
    if live is not None and live.kind == "text":
        # A written answer is judged through `_judged_text`, which is reached by naming it.
        # A verdict that named a recording, or nothing, for a task a written answer
        # answers would otherwise be told about a recording that does not exist.
        raise LinguaWikiError(
            "assessment_submission_required",
            f"{content_id} is answered by written answer {live.submission_id}; a verdict "
            "names the answer it judged, with --submission",
            details=(
                ErrorDetail(
                    field="submission",
                    reason="absent",
                    context={"submission": live.submission_id},
                ),
            ),
        )
    if live is None and audio_artifact is None:
        if requires_text:
            raise LinguaWikiError(
                "assessment_submission_required",
                f"{content_id} is a written task in a run opened for judged writing, and no "
                "answer to it has been submitted; a verdict is reached from the learner's "
                "submitted answer, so submit it first",
                details=(ErrorDetail(field="submission", reason="nothing submitted"),),
            )
        if requires_recording:
            raise LinguaWikiError(
                "assessment_recording_required",
                f"{content_id} is a spoken task in a run opened for recorded judging, and no "
                "recording of it has been submitted; a verdict is reached from the learner's "
                "recording, so record it first",
                details=(ErrorDetail(field="audio_artifact", reason="nothing submitted"),),
            )
        return None
    if live is not None and audio_artifact is None:
        raise LinguaWikiError(
            "assessment_audio_artifact_required",
            f"{content_id} is answered by recording {live.artifact_id}; a verdict names the "
            "recording it was reached from, with --audio-artifact or --submission",
            details=(
                ErrorDetail(
                    field="audio_artifact", reason="absent", context={"bound": live.artifact_id}
                ),
            ),
        )
    if assessor_kind not in evidence_policy.JUDGING_ASSESSORS:
        raise LinguaWikiError(
            "assessment_judge_required",
            f"a recording is judged by an ai or human assessor, not a {assessor_kind} one",
            details=(ErrorDetail(field="assessor_kind", reason=assessor_kind),),
        )
    assert audio_artifact is not None
    try:
        return recording_service.assert_judgeable(
            database,
            root,
            artifact_id=audio_artifact,
            run_id=run_id,
            content_id=content_id,
        )
    except LinguaWikiError as failure:
        if (
            live is not None
            and live.status == "pending"
            and live.artifact_id == audio_artifact
            and failure.payload.code in recording_service.RECORDING_FAILURES
        ):
            return Settlement(
                submission_id=live.submission_id,
                code=failure.payload.code,
                reason=failure.payload.message,
                refusal=failure,
            )
        raise


def _judged_text(
    database: Database,
    named: _NamedSubmission,
    *,
    run_id: str,
    content_id: str,
    request: VerdictRequest,
    answered: bool,
) -> JudgeableText | Settlement | None:
    """The written answer a verdict rests on, checked now -- or `None` once the task is
    answered and the verdict can only be a repeat.

    The text counterpart of `_judged_recording`. `recordings.assert_text_judgeable` is the
    check -- the answer is the live one, its track still keeps a written answer whole, and
    its text is what was handed in -- run again inside this writer, as `pending` ran it
    before handing the text out. Eligibility is revalidated *here*, at application, because
    consent can go between the claim and the verdict. When it fails because of the answer
    itself, the outcome is a `Settlement`: withdrawn, its text cleared where it may no
    longer be kept, and the failure raised once that has committed.

    The judge supplies the score and nothing else about the answer: its words are the
    submission's, so a response the verdict brings must be that answer, and an excerpt of
    the caller's own choosing is refused rather than stored beside the real one.
    """

    from linguawiki.services import recordings as recording_service
    from linguawiki.services.withdrawal import NOT_RETAINED_CODE, Settlement

    if request.audio_artifact is not None:
        raise LinguaWikiError(
            "assessment_audio_not_submitted",
            f"{named.submission_id} is a written answer, and a verdict on it is reached by "
            f"reading it, not from recording {request.audio_artifact}",
            details=(ErrorDetail(field="audio_artifact", reason="a written answer"),),
        )
    if request.response_excerpt is not None:
        raise LinguaWikiError(
            "invalid_arguments",
            f"{named.submission_id} is a written answer, so what is kept of it is decided "
            "from the answer itself; a verdict does not bring an excerpt of its own",
            details=(ErrorDetail(field="response_excerpt", reason="a written answer"),),
        )
    if request.applying is None:
        if request.score is None:
            raise LinguaWikiError(
                "assessment_score_required",
                f"{named.submission_id} is a written answer a judge scores against the "
                "rubric; the verdict supplies the score it reached",
                details=(ErrorDetail(field="score", reason="a judged written answer"),),
            )
        if request.assessor_kind not in evidence_policy.JUDGING_ASSESSORS:
            raise LinguaWikiError(
                "assessment_judge_required",
                f"a written answer is judged by an ai or human assessor, not a "
                f"{request.assessor_kind} one",
                details=(ErrorDetail(field="assessor_kind", reason=request.assessor_kind),),
            )
        if request.response is not None and (
            hashlib.sha256(request.response.encode("utf-8")).hexdigest() != named.response_digest
        ):
            raise LinguaWikiError(
                "assessment_response_not_submitted",
                f"the response this verdict brings is not {named.submission_id}'s answer; a "
                "verdict is about the answer the learner handed in, so leave the response "
                "out and the submission supplies it",
                details=(
                    ErrorDetail(
                        field="response",
                        reason="not the submitted answer",
                        context={"recorded": str(named.response_digest)},
                    ),
                ),
            )
    if answered:
        return None
    try:
        return recording_service.assert_text_judgeable(
            database, submission_id=named.submission_id, run_id=run_id, content_id=content_id
        )
    except LinguaWikiError as failure:
        if failure.payload.code in recording_service.TEXT_FAILURES:
            return Settlement(
                submission_id=named.submission_id,
                code=failure.payload.code,
                reason=failure.payload.message,
                refusal=failure,
                clear_text=failure.payload.code == NOT_RETAINED_CODE,
            )
        raise


# --- verdicts: plan, settle, write ---------------------------------------------------------


#: What became of a verdict, as a report names it. `held` is a verdict with no outcome row:
#: received on a paused run and waiting for the resume to apply it, or to void it.
VERDICT_HELD = "held"
VERDICT_APPLIED = "applied"
VERDICT_VOID = "void"

#: What `plan_verdict` decided to do with a verdict.
APPLY = "apply"
HOLD = "hold"
REPEAT = "repeat"

#: The rubric keys whose string values are the payload's structure rather than prose: the
#: criterion a score is for, and the payload's own version. Everything else a judge writes
#: in a rubric is free text -- a `note`, a `rationale`, an `anchors` description, a quoted
#: phrase -- and the rubric schema (`lingua.pack.assessment.v1`, `additionalProperties:
#: true`) promises nothing narrower, so a key not named here is free text by default. That
#: is the direction a privacy rule has to fail in: a new prose field a judge invents is
#: retained like the learner's words, never kept whole because nobody listed it.
RUBRIC_STRUCTURAL_KEYS: frozenset[str] = frozenset({"name", "criterion", "dimension", "version"})

#: A structural value longer than this is not a criterion name, whatever its key says.
RUBRIC_STRUCTURAL_LIMIT = 64


def retain_rubric(
    rubric: Mapping[str, object] | None,
    *,
    preferences: Mapping[str, object],
    full: bool = False,
) -> dict[str, object]:
    """A verdict's rubric as the track's consent allows it to be kept.

    The rule, applied to every string anywhere in the payload: a value under one of
    `RUBRIC_STRUCTURAL_KEYS`, no longer than `RUBRIC_STRUCTURAL_LIMIT`, is structure and
    kept as it is; every other string is free text and goes through
    `evidence.retain_response`, the rule the learner's answer goes through -- a bounded
    excerpt by default, nothing (`null`) where the track declined transcript retention, and
    the whole text only where the answer itself was kept `full`, which consent already
    allowed. Numbers, booleans, nulls, and the shape (keys, lists, nesting) are kept: they
    are the scores the verdict is about, and they carry no words.

    Keys are kept as they are. A rubric is a mapping of criteria, and its keys are named by
    the skill that writes it; a judge putting the learner's words into a *key* would be a
    misuse this cannot see, and is named here so it is not mistaken for a guarantee.

    Applying it twice changes nothing, so a held verdict's stored rubric can be planned
    again on resume.
    """

    requested = "full" if full else None

    def retained(value: object, key: str | None) -> object:
        if isinstance(value, str):
            if key in RUBRIC_STRUCTURAL_KEYS and len(value) <= RUBRIC_STRUCTURAL_LIMIT:
                return value
            _visibility, kept, _digest = evidence_service.retain_response(
                value, requested=requested, preferences=preferences
            )
            return kept
        if isinstance(value, Mapping):
            return {str(name): retained(item, str(name)) for name, item in value.items()}
        if isinstance(value, list | tuple):
            return [retained(item, None) for item in value]
        return value

    return {str(name): retained(item, str(name)) for name, item in (rubric or {}).items()}


#: Visibilities from least kept to most.
VISIBILITY_ORDER: tuple[str, ...] = ("withheld", "excerpt", "full")


def consented_visibility(preferences: Mapping[str, object]) -> str:
    """The most of a learner's words a track's consent lets anything keep:
    `evidence.retain_response`'s rule, as a ceiling."""

    consent = preferences.get("transcript_retention_consent")
    if consent is True:
        return "full"
    return "withheld" if consent is False else "excerpt"


def narrower_visibility(first: str, second: str) -> str:
    return min(first, second, key=VISIBILITY_ORDER.index)


def _narrowed_retention(
    visibility: str, excerpt: str | None, *, preferences: Mapping[str, object]
) -> tuple[str, str | None]:
    """A retained response as the track's *current* consent allows it, never wider.

    `evidence.retain_response` applied to what was already retained: declined consent keeps
    nothing (`withheld`); the default keeps at most a bounded excerpt, so a `full` answer
    kept under consent since withdrawn is cut back to one; consent keeps what was kept.
    """

    consent = preferences.get("transcript_retention_consent")
    if visibility == "withheld" or excerpt is None or consent is False:
        return "withheld", None
    if visibility == "full" and consent is not True:
        return "excerpt", excerpt[: evidence_service.EXCERPT_LIMIT]
    return visibility, excerpt


@dataclass(frozen=True, slots=True)
class VerdictRequest:
    """One verdict as its caller stated it, with the run already resolved.

    `submission_id` binds it to a submission; a verdict naming only `audio_artifact` is
    bound to the run's live submission for the task, so a recorded-task verdict is always
    submission-bound. `keyed` says whether an idempotency key came with it, which decides
    the code a different verdict for the same submission is refused with.

    `applying` names a held verdict being applied (the resume path): the stored verdict
    supplies the score and the retained response and rubric, and no second verdict row is
    written. Build one with `held_verdict_request`.
    """

    run_id: str
    content_id: str
    score: float | None = None
    response: str | None = None
    response_visibility: str | None = None
    response_excerpt: str | None = None
    rubric: Mapping[str, object] | None = None
    assessor_kind: str = "deterministic"
    assessor: str | None = None
    confidence: str = "medium"
    audio_artifact: str | None = None
    submission_id: str | None = None
    claim_id: str | None = None
    track_id: str | None = None
    actor: str = DEFAULT_ACTOR
    keyed: bool = False
    applying: str | None = None


@dataclass(frozen=True, slots=True)
class VerdictPlan:
    """What `write_verdict` is to do, decided by `plan_verdict` and resting on nothing it
    has not checked.

    `action` is `apply` (write the result and fold the posterior), `hold` (store the
    verdict with no outcome: the run is paused), or `repeat` (the verdict is already
    recorded; nothing is written). For a repeat, `verdict_id` and `held` describe the
    verdict already standing.
    """

    request: VerdictRequest
    action: str
    run_id: str
    track_id: str
    purpose: str
    dimension: str
    sequence: int
    candidate: Candidate
    kinds: Mapping[str, str]
    levels: tuple[str, ...]
    score: float
    score_source: str
    policy_version: str | None
    judgement_version: str | None
    rubric_json: str
    response_visibility: str
    response_excerpt: str | None
    response_hash: str | None
    play_count: int | None
    submission_id: str | None = None
    artifact_id: str | None = None
    claim_id: str | None = None
    verdict_id: str | None = None
    held: bool = False
    warnings: tuple[str, ...] = ()
    #: What the *verdict row* keeps of the answer, when that differs from what the result
    #: keeps: `(visibility, excerpt)`. Set for a written answer, whose verdict keeps none of
    #: its words (R10): always `("withheld", None)` -- the row is insert-only and no consent
    #: withdrawal could reach a copy there. `None`: the verdict keeps what the result keeps.
    verdict_response: tuple[str, str | None] | None = None


@dataclass(frozen=True, slots=True)
class VerdictWrite:
    """What one `write_verdict` wrote."""

    verdict_id: str | None
    result_id: str | None
    held: bool
    dimension: str
    score: float


@dataclass(frozen=True, slots=True)
class _NamedSubmission:
    submission_id: str
    artifact_id: str | None
    kind: str = "recording"
    response_digest: str | None = None


def _named_submission(
    database: Database,
    submission_id: str,
    *,
    run_id: str,
    content_id: str,
    track_id: str | None,
) -> _NamedSubmission:
    """The submission a verdict names, refused unless it is the live answer to this task.

    Superseded and withdrawn are refused by their own codes, each naming what a judge can
    do about it: the successor to judge instead, or the reason nothing will be judged.
    """

    from linguawiki.services import recordings as recording_service

    row = database.one(
        "SELECT submission.run_id, submission.content_id, submission.kind, submission.status, "
        "submission.superseded_by, submission.withdrawn_code, submission.withdrawn_reason, "
        "submission.artifact_id, run.track_id, submission.response_digest "
        "FROM assessment_submissions submission "
        "JOIN assessment_runs run ON run.run_id = submission.run_id "
        "WHERE submission.submission_id = ?",
        [submission_id],
    )
    if row is None:
        raise LinguaWikiError(
            "assessment_submission_not_found",
            f"no submission {submission_id} in this workspace",
            details=(ErrorDetail(field="submission", reason="unknown submission"),),
        )
    if track_id is not None and str(row[8]) != track_id:
        # Resolved through the run, never the submission's own say: a submission belongs to
        # whoever's run it was made in.
        raise LinguaWikiError(
            "assessment_submission_out_of_scope",
            f"{submission_id} was made in another learner's run, not on track {track_id}",
            details=(ErrorDetail(field="submission", reason="another track's run"),),
        )
    if str(row[0]) != run_id or str(row[1]) != content_id:
        raise LinguaWikiError(
            "assessment_submission_mismatch",
            f"{submission_id} answers {row[1]} in run {row[0]}, not {content_id} in run "
            f"{run_id}; a verdict names the submission it was reached from",
            details=(
                ErrorDetail(
                    field="submission",
                    reason="answers another task",
                    context={"run": str(row[0]), "content_id": str(row[1])},
                ),
            ),
        )
    # Either kind is judged here. A written answer's own revalidation -- that the track
    # still keeps it whole, and the text is what was handed in -- is `_judged_text`'s,
    # once this has established it is the task's live answer.
    status = str(row[3])
    live = recording_service.live_submission(database, run_id, content_id)
    if status == "withdrawn":
        raise LinguaWikiError(
            "assessment_submission_withdrawn",
            f"{submission_id} was withdrawn ({row[5]}): {row[6]}. Nothing judges it now; "
            "the task was skipped",
            details=(
                ErrorDetail(
                    field="submission",
                    reason="withdrawn",
                    context={"code": str(row[5]), "reason": str(row[6])},
                ),
            ),
        )
    if status == "superseded" or live is None or live.submission_id != submission_id:
        successor = None if row[4] is None else str(row[4])
        if live is None:
            # Nothing answers the task now: the successor, if one was named, was itself
            # withdrawn or superseded, and naming it as the thing to judge would send the
            # judge to a second refusal.
            replaced = "" if successor is None else f" (replaced by {successor}, which is gone too)"
            raise LinguaWikiError(
                "assessment_submission_superseded",
                f"{submission_id} no longer answers {content_id}{replaced}, and no submission "
                "answers it now, so there is nothing for this verdict to judge; drop it, and "
                "`assessment pending` lists what is waiting for a judge",
                details=(
                    ErrorDetail(
                        field="submission",
                        reason="superseded, and nothing answers the task",
                        context={} if successor is None else {"successor": successor},
                    ),
                ),
            )
        if successor is None:
            successor = live.submission_id
        context = {"successor": str(successor)}
        if live.submission_id != successor:
            context["live"] = live.submission_id
        raise LinguaWikiError(
            "assessment_submission_superseded",
            f"{submission_id} was replaced by {successor}: the learner answered again, and a "
            f"verdict on the earlier answer judges something no longer submitted; judge "
            f"{live.submission_id} instead",
            details=(ErrorDetail(field="submission", reason="superseded", context=context),),
        )
    return _NamedSubmission(
        submission_id=submission_id,
        artifact_id=None if row[7] is None else str(row[7]),
        kind=str(row[2]),
        response_digest=None if row[9] is None else str(row[9]),
    )


def _assert_claim(database: Database, claim_id: str, *, submission_id: str | None) -> None:
    """A verdict naming a claim names one handed out for the submission it judges."""

    row = database.one("SELECT submission_id FROM judging_claims WHERE claim_id = ?", [claim_id])
    if row is None:
        raise LinguaWikiError(
            "assessment_claim_not_found",
            f"no judging claim {claim_id} in this workspace",
            details=(ErrorDetail(field="claim", reason="unknown claim"),),
        )
    if submission_id is None or str(row[0]) != submission_id:
        raise LinguaWikiError(
            "assessment_claim_mismatch",
            f"claim {claim_id} was handed out for submission {row[0]}, not for the one this "
            "verdict judges",
            details=(
                ErrorDetail(
                    field="claim",
                    reason="claim is for another submission",
                    context={"claimed": str(row[0]), "judged": str(submission_id)},
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _Standing:
    """A verdict already recorded for a submission, applied or held."""

    verdict_id: str
    score: float
    response_hash: str | None
    response_visibility: str | None
    held: bool
    requested_visibility: str | None = None


def _standing_verdict(
    database: Database, submission_id: str, *, excluding: str | None = None
) -> _Standing | None:
    """The submission's verdict that still stands -- applied, or held -- if there is one.

    A void verdict stands for nothing and is skipped. `excluding` is the held verdict being
    applied, which is not a rival to itself.
    """

    row = database.one(
        "SELECT verdict.verdict_id, verdict.raw_score, verdict.response_hash, "
        "verdict.response_visibility, outcome.outcome, verdict.requested_visibility "
        "FROM assessment_verdicts verdict "
        "LEFT JOIN assessment_verdict_outcomes outcome ON outcome.verdict_id = verdict.verdict_id "
        "WHERE verdict.submission_id = ? AND verdict.verdict_id IS DISTINCT FROM ? "
        "AND (outcome.outcome IS NULL OR outcome.outcome = 'applied') "
        "ORDER BY outcome.outcome NULLS LAST, verdict.received_at DESC, verdict.verdict_id DESC "
        "LIMIT 1",
        [submission_id, excluding],
    )
    if row is None:
        return None
    return _Standing(
        verdict_id=str(row[0]),
        score=float(row[1]),
        response_hash=None if row[2] is None else str(row[2]),
        response_visibility=None if row[3] is None else str(row[3]),
        held=row[4] is None,
        requested_visibility=None if row[5] is None else str(row[5]),
    )


def _applied_verdict_for_task(database: Database, run_id: str, content_id: str) -> str | None:
    """The applied verdict behind a task's standing result, when it was judged from a
    submission."""

    value = database.scalar(
        "SELECT outcome.verdict_id FROM assessment_verdict_outcomes outcome "
        "JOIN assessment_results result ON result.result_id = outcome.result_id "
        "WHERE outcome.outcome = 'applied' AND result.run_id = ? AND result.content_id = ? "
        "LIMIT 1",
        [run_id, content_id],
    )
    return None if value is None else str(value)


def _held_verdict_row(database: Database, verdict_id: str, *, submission_id: str | None) -> Any:
    row = database.one(
        "SELECT verdict.submission_id, verdict.raw_score, verdict.rubric_json, "
        "verdict.response_visibility, verdict.response_excerpt, verdict.response_hash, "
        "outcome.outcome, verdict.requested_visibility FROM assessment_verdicts verdict "
        "LEFT JOIN assessment_verdict_outcomes outcome ON outcome.verdict_id = verdict.verdict_id "
        "WHERE verdict.verdict_id = ?",
        [verdict_id],
    )
    if row is None or row[6] is not None or str(row[0]) != submission_id:
        raise LinguaWikiError(
            "assessment_verdict_not_held",
            f"verdict {verdict_id} is not a held verdict for submission {submission_id}",
            details=(
                ErrorDetail(
                    field="verdict",
                    reason="not held" if row is not None else "unknown verdict",
                    context={} if row is None else {"outcome": str(row[6])},
                ),
            ),
        )
    return row


def held_verdict_request(database: Database, verdict_id: str) -> VerdictRequest:
    """The request that applies a held verdict, rebuilt from what was stored.

    For the resume path: the stored verdict is the account of what the judge said, so the
    request is read from it -- score, assessor, the retained rubric -- and `applying`
    makes `plan_verdict` revalidate it and `write_verdict` give it an outcome rather than
    a twin.
    """

    row = database.one(
        "SELECT verdict.submission_id, verdict.claim_id, verdict.raw_score, "
        "verdict.rubric_json, verdict.assessor_kind, verdict.assessor, verdict.confidence, "
        "submission.run_id, submission.content_id, submission.artifact_id "
        "FROM assessment_verdicts verdict "
        "JOIN assessment_submissions submission "
        "ON submission.submission_id = verdict.submission_id "
        "WHERE verdict.verdict_id = ?",
        [verdict_id],
    )
    if row is None:
        raise LinguaWikiError(
            "assessment_verdict_not_held",
            f"verdict {verdict_id} is not a held verdict this workspace can name",
            details=(ErrorDetail(field="verdict", reason="unknown verdict"),),
        )
    return VerdictRequest(
        run_id=str(row[7]),
        content_id=str(row[8]),
        score=float(row[2]),
        rubric=_stored_rubric(row[3]),
        assessor_kind=str(row[4]),
        assessor=None if row[5] is None else str(row[5]),
        confidence=str(row[6]),
        audio_artifact=None if row[9] is None else str(row[9]),
        submission_id=str(row[0]),
        claim_id=None if row[1] is None else str(row[1]),
        applying=verdict_id,
    )


def plan_verdict(
    database: Database, request: VerdictRequest, *, root: Path
) -> VerdictPlan | Settlement:
    """Decide what a verdict does, reading only, and refuse it if it cannot land.

    Every refusal `record` makes is here, before any write, so a refused verdict leaves the
    task still answerable. The one outcome that is not a refusal and not a plan is a
    `Settlement`: the recording the verdict names can no longer be heard, so the submission
    must be withdrawn -- returned, not written, because planning reads. The caller settles
    it (`withdrawal.settle`) and raises `settlement.refusal`, or, resuming, voids and
    carries on.

    `root` is the workspace root the recording is checked under.

    A run that is paused takes a submission-bound verdict as `hold` -- revalidated now,
    applied (or voided) at resume -- and refuses any other result, as it always has. A
    request `applying` a held verdict is planned to apply on a paused run too: that is the
    resume itself, which may plan before it flips the run's status.
    """

    from linguawiki.services import judging
    from linguawiki.services.withdrawal import Settlement

    run_id, content_id = request.run_id, request.content_id
    row = _run_row(database, run_id)
    if request.claim_id is not None:
        # First, before what became of the submission: a terminal release withdrew it, and
        # "your claim was released" is the account of *this* judge's attempt, where
        # "the submission was withdrawn" would send it looking for who withdrew it. An
        # expired lease is no refusal -- it schedules, it does not decide.
        judging.assert_claim_not_released(database, request.claim_id)
    named = (
        None
        if request.submission_id is None
        else _named_submission(
            database,
            request.submission_id,
            run_id=run_id,
            content_id=content_id,
            track_id=request.track_id,
        )
    )
    held_row = (
        None
        if request.applying is None
        else _held_verdict_row(database, request.applying, submission_id=request.submission_id)
    )
    # Before the run's own state: a judge retrying a verdict for a result whose recording
    # was purged is told that, whether or not the run has closed since. It is the more
    # specific truth, and the one that says no retry will ever land.
    _assert_not_invalidated(database, run_id=run_id, content_id=content_id)
    # A verdict is submission-bound when its caller named a submission or the recording
    # one answers with. Only such a verdict can be held: a held verdict is revalidated
    # against its submission when it is applied, and nothing else has one to be checked
    # against.
    bound_request = request.submission_id is not None or request.audio_artifact is not None
    status = str(row[4])
    hold = status == "paused" and bound_request and request.applying is None
    if not (status == "paused" and bound_request):
        _assert_running(run_id, status=status, action="take further results")
    served = database.one(
        "SELECT sequence, dimension, status, task_type, level_code, difficulty, "
        "content_family, modality, is_anchor, content_hash, expected_json, "
        "prompt_snapshot, rubric_json, asset_identity_json FROM assessment_run_tasks "
        "WHERE run_id = ? AND content_id = ?",
        [run_id, content_id],
    )
    if served is None:
        raise LinguaWikiError(
            "assessment_task_not_served",
            "that task was not served in this run; ask for the next task first",
            details=(ErrorDetail(field="content_id", reason="task was not served"),),
        )
    conditions = json.loads(str(row[6]))
    kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
    record_track = learner_service.track_context(database, str(row[1]))
    # Scored from what was served, never from what the pack now says. Re-reading the
    # installed pack folded a difficulty the learner never faced into their posterior
    # whenever the pack changed mid-run.
    candidate = _served_candidate(run_id, content_id=content_id, served=served)
    dimension = str(served[1])
    _assert_not_withdrawn(database, run_id=run_id, content_id=content_id, status=str(served[2]))
    score = request.score if held_row is None else float(held_row[1])
    response = request.response if held_row is None else None
    assessor_kind = request.assessor_kind
    answered = str(served[2]) == "answered"
    # Every refusal below runs before any transaction opens, so a refused call leaves the
    # task still `served` and answerable rather than half-recorded.
    warnings: list[str] = []
    # A written answer is judged from its own text, checked here before anything below
    # could read the verdict as a recording's or score it from a key it does not have.
    written: JudgeableText | None = None
    if named is not None and named.kind == "text":
        checked = _judged_text(
            database,
            named,
            run_id=run_id,
            content_id=content_id,
            request=request,
            answered=answered,
        )
        if isinstance(checked, Settlement):
            return checked
        written = checked
    if score is None:
        if response is None:
            raise LinguaWikiError(
                "assessment_score_required",
                f"scoring {content_id} needs either a score or the learner's response",
                details=(ErrorDetail(field="score", reason="neither score nor response"),),
            )
        if assessor_kind != "deterministic":
            raise LinguaWikiError(
                "assessment_score_required",
                f"a {assessor_kind} assessor reaches its own verdict, so it must "
                "supply the score it reached",
                details=(ErrorDetail(field="score", reason=f"{assessor_kind} assessor"),),
            )
        # Before the key: a rubric-scored task has no key by design, so reading one
        # first refuses with "your answer key is broken" where the truth is "this
        # needs a judge". The code is what a skill acts on.
        assert_machine_scorable(candidate.task_type)
        key = _scorable_key(
            database,
            content_id=content_id,
            snapshot=served[10:13],
            content_hash=None if served[9] is None else str(served[9]),
        )
        resolved_score = score_response(
            task_type=candidate.task_type, answers=key.answers, response=response
        )
        score_source, policy_version = "computed", SCORING_POLICY_VERSION
        if key.source == "bank":
            warnings.append(
                f"{content_id} was served before its answer key was recorded; the "
                "pack's content still hashes to what was served, so the key was read "
                "from the bank"
            )
    else:
        # A supplied score wins, and is labelled as supplied with no policy version:
        # `assessor_kind` has only ever *labelled* a score, and recording a version
        # against one would be a claim that work nobody did had been done.
        resolved_score = score
        score_source, policy_version = "supplied", None
    # What a judged score may claim, decided in `evidence.py` and stored with the
    # result. Nothing held an `ai` verdict to anything before C5.
    judgement_version = (
        None
        if score is None
        else evidence_policy.assert_judged_claim(
            dimension_kind=kinds[dimension],
            modality=candidate.modality,
            assessor_kind=assessor_kind,
            assessor=request.assessor,
            confidence=request.confidence,
        )
    )
    # A named submission names its recording, so the verdict needs no second spelling of
    # it; a recording named as well must be that one, which `assert_judgeable` checks.
    audio_artifact = request.audio_artifact
    if audio_artifact is None and named is not None:
        audio_artifact = named.artifact_id
    judged: JudgeableAudio | None = None
    if named is None or named.kind != "text":
        bound = _judged_recording(
            database,
            root,
            run_id=run_id,
            content_id=content_id,
            audio_artifact=audio_artifact,
            assessor_kind=assessor_kind,
            answered=answered,
            # A run opened for recorded judging serves a spoken task *to be recorded*: a
            # verdict on one with nothing submitted would be a result no purge could ever
            # reach, standing on audio nobody holds.
            requires_recording=(
                _run_scoring(conditions) in RECORDING_SCORINGS
                and judged_spoken_task(modality=candidate.modality, task_type=candidate.task_type)
            ),
            # And one opened for judged writing serves a written task *to be submitted*: a
            # verdict reached without the answer would rest on words nobody kept.
            requires_text=(
                _run_scoring(conditions) == JUDGED_SCORING
                and judged_written_task(modality=candidate.modality, task_type=candidate.task_type)
            ),
        )
        if isinstance(bound, Settlement):
            return bound
        judged = bound
    if judged is not None:
        submission_id: str | None = judged.submission.submission_id
    elif written is not None:
        submission_id = written.submission.submission_id
    else:
        submission_id = None
    if request.claim_id is not None:
        _assert_claim(
            database,
            request.claim_id,
            submission_id=submission_id
            if submission_id is not None
            else (None if named is None else named.submission_id),
        )
    verdict_response: tuple[str, str | None] | None = None
    if written is not None:
        # R10/R12: a written answer's verdict keeps none of its words -- it names the
        # submission, its hash is the submission's digest, and the judge's request for how
        # much the result may keep is its own column, `requested_visibility`.
        verdict_response = ("withheld", None)
        requested = (
            request.response_visibility
            if held_row is None
            else (None if held_row[7] is None else str(held_row[7]))
        )
        # The result's text is the submission's *current* text, kept to the narrower of what
        # the judge asked for and what the consent in force now keeps -- so a result written
        # at resume is what a verdict arriving now would write, and can only narrow.
        ceiling = narrower_visibility(
            requested or "full", consented_visibility(record_track.preferences)
        )
        visibility, excerpt, response_hash = evidence_service.retain_response(
            written.text, requested=ceiling, preferences=record_track.preferences
        )
        rubric_json = json.dumps(
            retain_rubric(
                request.rubric if held_row is None else _stored_rubric(held_row[2]),
                preferences=record_track.preferences,
                full=visibility == "full",
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    elif held_row is not None:
        # Retained when it was received, and retained *again* against the consent in force
        # now: the result this writes is a new copy, and a learner who withdrew transcript
        # consent while the run was paused must not find their words copied into it. Only
        # ever narrower -- the response itself was never stored, so nothing can widen --
        # and the verdict row keeps what it stored under the consent valid then.
        visibility, excerpt = _narrowed_retention(
            str(held_row[3]),
            None if held_row[4] is None else str(held_row[4]),
            preferences=record_track.preferences,
        )
        response_hash = None if held_row[5] is None else str(held_row[5])
        rubric_json = json.dumps(
            retain_rubric(
                _stored_rubric(held_row[2]),
                preferences=record_track.preferences,
                full=visibility == "full",
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    elif named is not None and named.kind == "text" and written is None:
        # Answered already, so this can only be a repeat, compared with the result and
        # never written: the answer it judged is the submission's, by its digest, kept as
        # the verdict asked -- whole unless it asked for less.
        visibility = request.response_visibility or "full"
        excerpt, response_hash = None, named.response_digest
        rubric_json = json.dumps(
            retain_rubric(request.rubric, preferences=record_track.preferences),
            ensure_ascii=False,
            sort_keys=True,
        )
    else:
        # The response goes through retention either way. Scoring in the background and
        # discarding the result would make the stored score unexplainable, and a
        # caller-supplied excerpt reaches the column by the same route so that the consent
        # rule covers both and not only the one this stage added.
        visibility, excerpt, digest = evidence_service.retain_response(
            response if response is not None else request.response_excerpt,
            requested=request.response_visibility,
            preferences=record_track.preferences,
        )
        # `response_hash` promises the hash of the *response*, so that a withheld answer
        # can be checked against one offered later. A caller-supplied excerpt is the
        # caller's own truncation of an answer this command never saw: hashing it under
        # that name would answer "no" for the learner's real answer, which is the opposite
        # of what the column exists to do. No whole answer, no attestation.
        response_hash = digest if response is not None else None
        # A submission-bound verdict's rubric goes through retention too, held or applied,
        # so neither the verdict row nor the result it produces is an unfiltered copy. A
        # verdict bound to nothing keeps the rubric as given: that is the deferred C1 gap,
        # which this stage does not widen and does not claim to close.
        rubric_payload = (
            retain_rubric(
                request.rubric,
                preferences=record_track.preferences,
                full=visibility == "full",
            )
            if bound_request or submission_id is not None
            else dict(request.rubric or {})
        )
        rubric_json = json.dumps(rubric_payload, ensure_ascii=False, sort_keys=True)
    # Whether a different verdict is a different *result* (keyless: the C1 contract) or a
    # second verdict for one submission (keyed: a judge holding a new key for work that
    # already landed). The caller acts on the code.
    conflict_code = (
        "assessment_verdict_conflict"
        if request.keyed and bound_request
        else "assessment_result_conflict"
    )
    if answered:
        # Deliberately *here*, below every refusal above rather than above them. A
        # repeat must not fold the same evidence into the posterior twice, but a
        # second call carrying a different answer is not a repeat, and returning the
        # run report for one told the caller a correction had landed when nothing had
        # been written. A guard placed before the path it guards also disables it:
        # with this above, asking to keep more than consent allows on an answered task
        # returned success and the caller believed the transcript was retained.
        _assert_repeat(
            database,
            run_id=run_id,
            content_id=content_id,
            score=resolved_score,
            response_hash=response_hash,
            visibility=visibility,
            audio_artifact=audio_artifact,
            code=conflict_code,
        )
        action = REPEAT
        standing_id = _applied_verdict_for_task(database, run_id, content_id)
        standing_held = False
    else:
        standing = (
            None
            if submission_id is None
            else _standing_verdict(database, submission_id, excluding=request.applying)
        )
        if standing is not None:
            # A held verdict is a recorded verdict: the same one again is a repeat, and a
            # different one is refused exactly as against a result.
            _assert_same_observation(
                content_id,
                recorded=(
                    standing.score,
                    standing.response_hash,
                    standing.response_visibility
                    if verdict_response is None
                    else (standing.requested_visibility or "full"),
                    audio_artifact,
                ),
                score=resolved_score,
                response_hash=response_hash,
                # Verdict against verdict. A written answer's rows all keep nothing, so what
                # tells two of its verdicts apart is what each asked the result to keep.
                visibility=visibility
                if verdict_response is None
                else (request.response_visibility or "full"),
                audio_artifact=audio_artifact,
                code=conflict_code,
            )
            action, standing_id, standing_held = REPEAT, standing.verdict_id, standing.held
        else:
            action = HOLD if hold else APPLY
            standing_id, standing_held = request.applying, hold
    # Derived from the play rows, never accepted: a caller-supplied count would be one
    # more place a caller could talk its way into a different claim. Only for a task
    # that played a recording, and only from a surface that records plays.
    #
    # Recorded plays are a fact whoever records the result: a page plays, the learner
    # pauses, and the CLI scores the answer. Only "no plays" depends on the surface --
    # zero from one that records plays, unknown (`NULL`) from one that cannot know.
    plays = _plays_used(database, run_id, content_id) if served[13] is not None else 0
    play_count = (
        plays
        if served[13] is not None and (plays or request.actor in PLAY_TRACKING_ACTORS)
        else None
    )
    return VerdictPlan(
        request=request,
        action=action,
        run_id=run_id,
        track_id=str(row[1]),
        purpose=str(row[3]),
        dimension=dimension,
        sequence=int(served[0]),
        candidate=candidate,
        kinds=kinds,
        levels=_pinned_levels(conditions, fallback=record_track.framework_levels),
        score=resolved_score,
        score_source=score_source,
        policy_version=policy_version,
        judgement_version=judgement_version,
        rubric_json=rubric_json,
        response_visibility=visibility,
        response_excerpt=excerpt,
        response_hash=response_hash,
        play_count=play_count,
        submission_id=submission_id,
        artifact_id=None if judged is None else judged.artifact_id,
        claim_id=request.claim_id,
        verdict_id=standing_id,
        held=standing_held,
        warnings=tuple(warnings),
        verdict_response=verdict_response,
    )


def settle(database: Database, settlement: Settlement) -> SettlementOutcome:
    """`withdrawal.settle`, the one writer of settlements, under the name callers here
    reach for. Inside the caller's transaction; no commit of its own."""

    from linguawiki.services import withdrawal

    return withdrawal.settle(database, settlement)


def write_verdict(database: Database, plan: VerdictPlan) -> VerdictWrite:
    """Write what `plan_verdict` decided, inside the caller's transaction.

    A submission-bound verdict is stored first, as the judge delivered it (retained), with
    `received_at` the write time; a held one stops there -- a verdict with no outcome. An
    applied one then writes the result, dated twice: `recorded_at` is now, `observed_at` is
    when the learner answered (the submission's `created_at`), so a verdict arriving a week
    later is evidence about the learner of a week ago. The submission becomes `judged`, the
    posterior folds the score, and the verdict's `applied` outcome names the result -- all
    in the one transaction, so none of them can be seen without the others.

    The posterior is folded from the state read *here*, not when the plan was made: inside
    a resume several held verdicts are written in one transaction, and each must see what
    the one before it wrote.

    No commit, no audit entry, and no domain event: those belong to the command.
    """

    if plan.action == REPEAT:
        raise AssertionError("a repeat writes nothing; the caller reports it instead")
    now = database.now()
    verdict_id = plan.request.applying
    if plan.submission_id is not None and verdict_id is None:
        # `rubric_json` here is the retained rubric, always. Verdicts migration 0035
        # backfilled from C5 results hold '{}' instead: there it means "not copied" -- the
        # result's unretained rubric was not duplicated into an insert-only row -- and never
        # "the judge gave no rubric". The applied outcome names the result that holds it.
        verdict_id = str(AssessmentId.new())
        # R19: the row keeps a learner excerpt only while it is the excerpt's sole copy.
        # A written answer's verdict keeps none (R10: the submission holds the text). A
        # recording's verdict applied in this transaction keeps none either -- the result
        # written below holds the retained form, and a second copy on an insert-only row is
        # one no later scrub of the result could reach. Its hash stays: it names the answer
        # without quoting it.
        #
        # A *held* recording verdict keeps its retained excerpt, because until resume it is
        # the only account of the answer the result will be written from. It stays after
        # the resume applies it (the row is insert-only), which is the deferred C1 gap of a
        # stored excerpt nobody can withdraw; `verdict_response_shape` allows exactly that
        # case and no other.
        if plan.verdict_response is not None:
            kept_visibility, kept_excerpt = plan.verdict_response
        elif plan.action == APPLY:
            kept_visibility, kept_excerpt = "withheld", None
        else:
            kept_visibility, kept_excerpt = plan.response_visibility, plan.response_excerpt
        database.execute(
            "INSERT INTO assessment_verdicts (verdict_id, submission_id, claim_id, raw_score, "
            "rubric_json, assessor_kind, assessor, confidence, response_visibility, "
            "response_excerpt, response_hash, received_at, requested_visibility) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                verdict_id,
                plan.submission_id,
                plan.claim_id,
                plan.score,
                plan.rubric_json,
                plan.request.assessor_kind,
                plan.request.assessor,
                plan.request.confidence,
                kept_visibility,
                kept_excerpt,
                plan.response_hash,
                now,
                # What the judge asked to keep, apart from what this row kept: the only
                # account of the request a held written answer's verdict has at resume.
                plan.request.response_visibility,
            ],
        )
    if plan.action == HOLD:
        return VerdictWrite(
            verdict_id=verdict_id,
            result_id=None,
            held=True,
            dimension=plan.dimension,
            score=plan.score,
        )
    states = {
        state.dimension: state for state in _dimension_states(database, plan.run_id, plan.kinds)
    }
    state = states[plan.dimension]
    prior_snapshot = list(state.posterior)
    updated = record_score(state, plan.candidate, score=plan.score, level_count=len(plan.levels))
    result_id = str(AssessmentId.new())
    # When the learner answered: the submission's moment for a judged recording, which a
    # verdict arriving later must not redate; otherwise the write time.
    observed_at = now
    if plan.submission_id is not None:
        observed_at = database.scalar(
            "SELECT created_at FROM assessment_submissions WHERE submission_id = ?",
            [plan.submission_id],
        )
    database.execute(
        "INSERT INTO assessment_results (result_id, run_id, content_id, dimension, "
        "raw_score, rubric_json, response_excerpt, assessor_kind, assessor, confidence, "
        "prior_json, posterior_json, difficulty, recorded_at, scoring_policy_version, "
        "score_source, response_visibility, response_hash, play_count, "
        "audio_artifact_id, judgement_policy_version, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            result_id,
            plan.run_id,
            plan.request.content_id,
            plan.dimension,
            plan.score,
            plan.rubric_json,
            plan.response_excerpt,
            plan.request.assessor_kind,
            plan.request.assessor,
            plan.request.confidence,
            json.dumps(prior_snapshot),
            json.dumps(list(updated.posterior)),
            plan.candidate.difficulty,
            now,
            plan.policy_version,
            plan.score_source,
            plan.response_visibility,
            plan.response_hash,
            plan.play_count,
            plan.artifact_id,
            plan.judgement_version,
            observed_at,
        ],
    )
    database.execute(
        "UPDATE assessment_run_tasks SET status = 'answered' WHERE run_id = ? AND sequence = ?",
        [plan.run_id, plan.sequence],
    )
    if plan.submission_id is not None:
        # In the result's transaction: a judged submission without its result, or a result
        # whose submission still waits for a judge, is a state nobody can explain
        # afterwards.
        judged = database.query(
            "UPDATE assessment_submissions SET status = 'judged', updated_at = ? "
            "WHERE submission_id = ? AND status = 'pending' RETURNING submission_id",
            [now, plan.submission_id],
        )
        if len(judged) != 1:
            # `plan_verdict` established that the submission is pending and live; a plan
            # that reaches here otherwise was made against a state that moved under it, and
            # writing the result anyway would judge a submission that is not waiting.
            raise AssertionError(
                f"applying a verdict to {plan.submission_id} judged {len(judged)} pending "
                "submission(s), not one; the plan was made against a state that has moved"
            )
        database.execute(
            "INSERT INTO assessment_verdict_outcomes (verdict_id, outcome, result_id, code, "
            "reason, decided_at) VALUES (?, 'applied', ?, NULL, NULL, ?)",
            [verdict_id, result_id, now],
        )
    # The exposure row already exists: `next_task` wrote it when it served this item.
    # Scoring adds the answer, and leaves the exposure count alone.
    _record_exposure(
        database,
        track_id=plan.track_id,
        content_id=plan.request.content_id,
        purpose=plan.purpose,
        is_anchor=plan.candidate.is_anchor,
        now=now,
        answered=True,
    )
    _write_state(database, run_id=plan.run_id, state=updated, levels=plan.levels, insert=False)
    database.execute(
        "UPDATE assessment_runs SET updated_at = ? WHERE run_id = ?", [now, plan.run_id]
    )
    return VerdictWrite(
        verdict_id=verdict_id,
        result_id=result_id,
        held=False,
        dimension=plan.dimension,
        score=plan.score,
    )


def _verdict_report(
    database: Database, run_id: str, verdict_id: str | None, warnings: Sequence[str] = ()
) -> AssessmentRunReport:
    """The run report, saying what became of the verdict this call delivered or replayed."""

    report = _reported(run_report(database, run_id), warnings)
    if verdict_id is None:
        return report
    outcome = database.one(
        "SELECT outcome FROM assessment_verdict_outcomes WHERE verdict_id = ?", [verdict_id]
    )
    status = VERDICT_HELD if outcome is None else str(outcome[0])
    return report.model_copy(
        update={"held": status == VERDICT_HELD, "verdict_id": verdict_id, "verdict_status": status}
    )


def _naming_voided(failure: LinguaWikiError, settled: SettlementOutcome) -> LinguaWikiError:
    """The refusal a settlement raises, naming the held verdicts it voided on the way.

    A held verdict is a judge's delivered work; voiding it is part of what the refused call
    did, and the refusal is the only account the caller gets. Same code and message.
    """

    if not settled.voided_verdicts:
        return failure
    payload = failure.payload
    return LinguaWikiError(
        payload.code,
        payload.message,
        retryable=payload.retryable,
        details=(
            *payload.details,
            *(
                ErrorDetail(
                    field="voided",
                    reason="a held verdict voided with the submission",
                    context={"verdict_id": verdict_id, "submission_id": settled.submission_id},
                )
                for verdict_id in settled.voided_verdicts
            ),
        ),
    )


def _submission_run(database: Database, submission_id: str) -> str:
    run_id = database.scalar(
        "SELECT run_id FROM assessment_submissions WHERE submission_id = ?", [submission_id]
    )
    if run_id is None:
        raise LinguaWikiError(
            "assessment_submission_not_found",
            f"no submission {submission_id} in this workspace",
            details=(ErrorDetail(field="submission", reason="unknown submission"),),
        )
    return str(run_id)


def record(
    paths: WorkspacePaths,
    *,
    content_id: str,
    score: float | None = None,
    response: str | None = None,
    response_visibility: str | None = None,
    run: str | None = None,
    track: str | None = None,
    rubric: Mapping[str, object] | None = None,
    response_excerpt: str | None = None,
    assessor_kind: str = "deterministic",
    assessor: str | None = None,
    confidence: str = "medium",
    audio_artifact: str | None = None,
    submission: str | None = None,
    claim: str | None = None,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.record",
    actor: str = DEFAULT_ACTOR,
) -> AssessmentRunReport:
    """Score one served task and fold it into its dimension's posterior.

    A spoken task answered by a recording is scored by a judge who names the submission
    (`submission`) or the recording (`audio_artifact`) they heard. It must be the
    recording the learner submitted, and it is checked again here, inside the writer that
    stores the verdict, rather than only when the task was handed to the judge: a purge,
    an altered file, or a retention change while the judge was listening refuses the
    verdict with the reason, and because a purge takes the same writer, one of the two is
    first and the other sees its result. A verdict for a submission on a paused run is
    *held* -- stored, revalidated at resume, reported `held: true` -- rather than refused.

    The score is either *computed* here from the key the run snapshotted, or *supplied* by
    a caller who reached its own verdict; `score_source` records which, because
    `assessor_kind` has only ever *labelled* a score and a compatibility call must not
    read afterwards as the scoring policy having run.

    The learner's `response` is an input, not an artifact. Scoring uses it in memory, and
    it reaches the database only through `retain_response`, which is where consent is
    honoured -- so automatic scoring never requires keeping text a track has declined.

    The shape is `plan_verdict` (reads, refuses, or settles) then one transaction holding
    `write_verdict` and the domain event, so receipt and application are one commit: a
    crash before it leaves nothing, and a retry under the same key after it replays.
    """

    active_clock = clock or SystemClock()
    if score is not None and not 0.0 <= score <= 1.0:
        raise LinguaWikiError("invalid_arguments", "score must lie between 0.0 and 1.0")
    if assessor_kind not in ("deterministic", "ai", "learner", "human"):
        raise LinguaWikiError(
            "invalid_arguments", "assessor_kind must be deterministic, ai, learner, or human"
        )
    if response is not None and response_excerpt is not None:
        # Two different texts for one answer: whichever were stored, the other would be
        # silently discarded, and the excerpt is the learner's words either way.
        raise LinguaWikiError(
            "invalid_arguments",
            "pass either the learner's response or an excerpt of it, not both; the "
            "excerpt is derived from the response by the retention rule",
            details=(ErrorDetail(field="response", reason="an excerpt was also supplied"),),
        )
    if response is not None and not response.strip():
        raise LinguaWikiError(
            "invalid_arguments",
            "an empty response is a skip, not a wrong answer; skip the task instead",
            details=(ErrorDetail(field="response", reason="blank"),),
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        # A named submission names its run: the judge holding a submission identifier
        # need not also know which run is open, and a run named as well must be that one
        # (`plan_verdict` refuses a mismatch by name).
        if submission is not None and run is None:
            run = _submission_run(database, submission)
        run_id = resolve_run(database, run, track_id=track_id)
        # Every writer touching the run settles what has lapsed before its own work. Not the
        # submission this verdict's own (unreleased) claim was for: an expired lease is
        # accepted when nothing else has happened, and this call settling it on the way in
        # would be that something.
        settled = _settle_lapsed(
            database, run_id, command=command, actor=actor, sparing_claim=claim
        )

        # The command's own work, after the sweep: whatever it reports -- or refuses
        # with -- names the submissions the sweep withdrew.
        def work() -> AssessmentRunReport:
            # The key is checked here -- before the transaction, and before every guard below,
            # including the one that asks whether the run may take further results. A retry is a
            # retry whatever the run has become since: a client whose response was lost and which
            # then paused, or whose pause and retry crossed, was told its completed work was new
            # work it was not allowed to do. Resolving the replay first answers with what already
            # happened, which is true in any run state -- held, applied, or voided, and the
            # report says which.
            #
            # It also means the unique index on
            # `domain_events.idempotency_key` is never the thing that refuses: it did, with a
            # raw `ConstraintException` surfaced as `internal_error`, after every refusal below
            # had been placed before the transaction precisely to avoid that.
            #
            # Every argument a refusal below could turn on is in the fingerprint -- the score,
            # the visibility, the assessor, the rubric, the submission and claim -- which is
            # what makes returning early on a replay safe: a call that asked for something
            # different conflicts rather than being handed this one's result. The response text
            # itself is never hashed into the payload under its own name; `payload_json` is
            # never edited, so a payload written before the retention rule ran would keep what
            # it kept for the life of the workspace. Its digest answers the same question and
            # carries nothing.
            scoring_fingerprint = idempotency.request_hash(
                operation=RECORDED_EVENT,
                run_id=run_id,
                content_id=content_id,
                score=score,
                response=None if response is None else idempotency.canonical_hash(response),
                response_excerpt=None
                if response_excerpt is None
                else idempotency.canonical_hash(response_excerpt),
                response_visibility=response_visibility,
                assessor_kind=assessor_kind,
                assessor=assessor,
                confidence=confidence,
                rubric=dict(rubric or {}),
                # Each only when named, so a keyed verdict recorded before the stage that added
                # it still hashes to what it hashed to then and its retry stays a retry.
                **({} if audio_artifact is None else {"audio_artifact": audio_artifact}),
                **({} if submission is None else {"submission_id": submission}),
                **({} if claim is None else {"claim_id": claim}),
            )
            replayed = idempotency.resolve(
                database,
                key=idempotency_key,
                event_type=RECORDED_EVENT,
                request_hash=scoring_fingerprint,
            )
            if replayed is not None:
                recorded_verdict = replayed.get("verdict_id")
                return _verdict_report(
                    database, run_id, None if recorded_verdict is None else str(recorded_verdict)
                )
            plan = plan_verdict(
                database,
                VerdictRequest(
                    run_id=run_id,
                    content_id=content_id,
                    score=score,
                    response=response,
                    response_visibility=response_visibility,
                    response_excerpt=response_excerpt,
                    rubric=rubric,
                    assessor_kind=assessor_kind,
                    assessor=assessor,
                    confidence=confidence,
                    audio_artifact=audio_artifact,
                    submission_id=submission,
                    claim_id=claim,
                    track_id=track_id,
                    actor=actor,
                    keyed=idempotency_key is not None,
                ),
                root=paths.root,
            )
            if not isinstance(plan, VerdictPlan):
                # The recording can no longer be heard. The withdrawal commits on its own, and
                # the refusal is raised after it, as C5 did: the judge is told why, and the task
                # no longer holds its dimension for a verdict that can never land.
                with database.transaction() as transaction:
                    settled = settle(transaction, plan)
                raise _naming_voided(
                    plan.refusal
                    or LinguaWikiError(
                        plan.code,
                        plan.reason,
                        details=(ErrorDetail(field="submission", reason=plan.submission_id),),
                    ),
                    settled,
                )
            if plan.action == REPEAT:
                if idempotency_key is not None:
                    # The same verdict again under a new key changes nothing about the learner,
                    # but the key has still been spent on it. Left unbound, a retry under it
                    # after the run closed met `_assert_running` instead of replaying, so the
                    # judge was told its delivered verdict was refused. Binding it to the
                    # standing verdict makes its retry replay whatever that verdict has become.
                    with database.transaction() as transaction:
                        migration_module.record_domain_event(
                            transaction,
                            event_type=RECORDED_EVENT,
                            aggregate_type="assessment_run",
                            aggregate_id=run_id,
                            correlation_id=EventId.new(),
                            payload_json=idempotency.payload(
                                scoring_fingerprint,
                                content_id=content_id,
                                score=plan.score,
                                repeat=True,
                                **(
                                    {}
                                    if plan.verdict_id is None
                                    else {"verdict_id": plan.verdict_id}
                                ),
                            ),
                            idempotency_key=idempotency_key,
                        )
                return _verdict_report(database, run_id, plan.verdict_id, plan.warnings)
            with database.transaction() as transaction:
                written = write_verdict(transaction, plan)
                affected = [run_id] + [
                    value for value in (written.verdict_id, written.result_id) if value is not None
                ]
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    actor=actor,
                    affected_records_json=json.dumps(affected, sort_keys=True),
                    after_summary=(
                        f"held a verdict on {content_id} in {written.dimension} at {written.score} "
                        "until the run resumes"
                        if written.held
                        else f"scored {content_id} in {written.dimension} at {written.score} "
                        f"({plan.score_source})"
                    ),
                )
                if idempotency_key is not None:
                    migration_module.record_domain_event(
                        transaction,
                        event_type=RECORDED_EVENT,
                        aggregate_type="assessment_run",
                        aggregate_id=run_id,
                        correlation_id=EventId.new(),
                        # The event names the verdict, which is how a replay finds what it is
                        # replaying: `assessment_verdicts` carries no key of its own.
                        payload_json=idempotency.payload(
                            scoring_fingerprint,
                            content_id=content_id,
                            score=written.score,
                            **(
                                {}
                                if written.verdict_id is None
                                else {"verdict_id": written.verdict_id}
                            ),
                        ),
                        idempotency_key=idempotency_key,
                    )
            return _verdict_report(database, run_id, written.verdict_id, plan.warnings)

        return _noting_settled(settled, work)


class PlayReport(ContractModel):
    """One play of a task's recording, recorded before it was heard."""

    run_id: str
    content_id: str
    #: Plays recorded for this task, this one included.
    plays_used: int
    #: The snapshotted allowance, counted in plays: the first hearing is a play. `None` is
    #: unlimited.
    replay_allowance: int | None = None
    #: `None` when the allowance is unlimited, so "none left" and "no limit" cannot be
    #: confused by a client drawing the count.
    plays_remaining: int | None = None


def _plays_used(database: Database, run_id: str, content_id: str) -> int:
    return int(
        database.scalar(
            "SELECT count(*) FROM assessment_task_plays WHERE run_id = ? AND content_id = ?",
            [run_id, content_id],
        )
    )


def plays_remaining(allowance: int | None, used: int) -> int | None:
    """What a finite allowance has left. One answer, for the play path and the screen."""

    return None if allowance is None else max(allowance - used, 0)


def record_play(
    paths: WorkspacePaths,
    *,
    content_id: str,
    idempotency_key: str,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.play",
    actor: str = DEFAULT_ACTOR,
) -> PlayReport:
    """Record that the learner is about to play a served task's recording.

    Called *before* playback, and playback starts only when it succeeds: a play recorded
    after the fact can be lost with the response, and a play past the allowance has to be
    refused while it is still unheard.

    The key is required. Plays accumulate, so a keyless retry would be a second play --
    there is no state a retry could converge on, which is why `/status` may go keyless
    and this may not.
    """

    active_clock = clock or SystemClock()
    if not idempotency_key or not idempotency_key.strip():
        raise LinguaWikiError(
            "invalid_arguments",
            "a play needs an idempotency key, because plays accumulate and a retry without "
            "one would count a second hearing",
            details=(ErrorDetail(field="idempotency_key", reason="absent"),),
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
        row = _run_row(database, run_id)
        fingerprint = idempotency.request_hash(
            operation=PLAYED_EVENT, run_id=run_id, content_id=content_id
        )
        # Before every guard: a retry of a play that landed is answered with that play,
        # whatever the run or the task has become since.
        replay = idempotency.resolve(
            database, key=idempotency_key, event_type=PLAYED_EVENT, request_hash=fingerprint
        )
        if replay is not None:
            allowance = replay.get("replay_allowance")
            used = int(replay["plays_used"])
            return PlayReport(
                run_id=run_id,
                content_id=content_id,
                plays_used=used,
                replay_allowance=None if allowance is None else int(allowance),
                plays_remaining=plays_remaining(
                    None if allowance is None else int(allowance), used
                ),
            )
        _assert_running(run_id, status=str(row[4]), action="play a recording")
        # Read from the record of the serving, which also proves the recording is still the
        # one the task was served with: a replaced recording is a different question, and
        # playing it would credit the learner's answer to the wrong one.
        shown = served_task_report(database, run_id, content_id=content_id)
        if shown.status != "served":
            raise LinguaWikiError(
                "assessment_task_settled",
                f"{content_id} is {shown.status}, so its recording is no longer part of an "
                "open question",
                details=(ErrorDetail(field="content_id", reason=f"task is {shown.status}"),),
            )
        if shown.presentation is None or shown.presentation.audio is None or shown.asset is None:
            raise LinguaWikiError(
                "assessment_task_plays_nothing",
                f"{content_id} was served with no recording, so there is nothing to play",
                details=(ErrorDetail(field="content_id", reason="no recording"),),
            )
        allowance = shown.presentation.audio.replay_allowance
        used = _plays_used(database, run_id, content_id)
        if allowance is not None and used >= allowance:
            raise LinguaWikiError(
                "assessment_replays_exhausted",
                f"{content_id} may be played {allowance} time(s), and has been",
                details=(ErrorDetail(field="content_id", reason=f"allowance {allowance}"),),
            )
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO assessment_task_plays (play_id, run_id, content_id, "
                "idempotency_key, played_at) VALUES (?, ?, ?, ?, ?)",
                [str(AssessmentId.new()), run_id, content_id, idempotency_key, now],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                actor=actor,
                affected_records_json=json.dumps([run_id, content_id], sort_keys=True),
                after_summary=f"played {content_id} ({used + 1} of {allowance or 'unlimited'})",
            )
            migration_module.record_domain_event(
                transaction,
                event_type=PLAYED_EVENT,
                aggregate_type="assessment_run",
                aggregate_id=run_id,
                correlation_id=EventId.new(),
                payload_json=idempotency.payload(
                    fingerprint,
                    content_id=content_id,
                    plays_used=used + 1,
                    replay_allowance=allowance,
                ),
                idempotency_key=idempotency_key,
            )
        return PlayReport(
            run_id=run_id,
            content_id=content_id,
            plays_used=used + 1,
            replay_allowance=allowance,
            plays_remaining=plays_remaining(allowance, used + 1),
        )


def set_status(
    paths: WorkspacePaths,
    *,
    status: str,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.pause",
    actor: str = DEFAULT_ACTOR,
) -> AssessmentRunReport:
    """Pause, resume, or abandon a run, settling the judgements the move reaches.

    - **Resume** applies the verdicts held while the run was paused, oldest first, each
      revalidated by `plan_verdict` and written by `write_verdict` inside the resume's own
      transaction -- so each sees what the one before it wrote. A held verdict that no
      longer revalidates is voided with the refusal's code and message (and its submission
      settled, when the refusal is a `Settlement`), and the resume still succeeds: the
      learner asked to carry on, and a judge's stale verdict is no reason to refuse that.
    - **Abandon** withdraws every pending submission (`assessment_run_abandoned`, task
      skipped) and voids every held verdict, because nothing on a closed run can be
      applied, and nothing should go on reading as waiting.

    The report names what was applied, voided, and withdrawn.
    """

    from linguawiki.services import withdrawal

    if status not in ("in-progress", "paused", "abandoned"):
        raise LinguaWikiError(
            "invalid_arguments", "status must be in-progress, paused, or abandoned"
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
        settled = _settle_lapsed(database, run_id, command=command, actor=actor)

        # The command's own work, after the sweep: whatever it reports -- or refuses
        # with -- names the submissions the sweep withdrew.
        def work() -> AssessmentRunReport:
            current = _run_row(database, run_id)
            _assert_transition(run_id, current=str(current[4]), target=status)
            applied: tuple[AppliedVerdict, ...] = ()
            voided: tuple[str, ...] = ()
            withdrawn: tuple[WithdrawnSubmission, ...] = ()
            with database.transaction() as transaction:
                if status == RUNNING_STATUS and str(current[4]) == "paused":
                    applied, voided = _apply_held_verdicts(transaction, run_id, root=paths.root)
                elif status == "abandoned":
                    withdrawn, voided = withdrawal.withdraw_outstanding(
                        transaction,
                        run_id,
                        code=withdrawal.ABANDONED_CODE,
                        reason="the run was abandoned before this answer's judgement was applied",
                    )
                transaction.execute(
                    "UPDATE assessment_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    [status, transaction.now(), run_id],
                )
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    actor=actor,
                    affected_records_json=json.dumps(
                        [
                            run_id,
                            *sorted(
                                {
                                    *(entry.verdict_id for entry in applied),
                                    *(entry.result_id for entry in applied),
                                    *voided,
                                    *(entry.submission_id for entry in withdrawn),
                                }
                            ),
                        ]
                    ),
                    before_summary=str(current[4]),
                    after_summary=status
                    + _settled_summary(
                        applied=len(applied), voided=voided, withdrawn=len(withdrawn)
                    ),
                )
            return run_report(database, run_id).model_copy(
                update={
                    "applied_verdicts": applied,
                    "voided_verdicts": withdrawal.describe_voided(database, voided),
                    "withdrawn": withdrawn,
                }
            )

        return _noting_settled(settled, work)


def _settled_summary(*, applied: int, voided: Sequence[str], withdrawn: int) -> str:
    parts = []
    if applied:
        parts.append(f"applied {applied} held verdict(s)")
    if withdrawn:
        parts.append(f"withdrew {withdrawn} pending submission(s)")
    if voided:
        parts.append("voided verdict(s) " + ", ".join(voided))
    return "" if not parts else "; " + "; ".join(parts)


def _apply_held_verdicts(
    database: Database, run_id: str, *, root: Path
) -> tuple[tuple[AppliedVerdict, ...], tuple[str, ...]]:
    """Apply a resuming run's held verdicts, oldest first, in the caller's transaction.

    Each is revalidated *now* -- the plan is the one `record` makes, so a held verdict is
    held to every rule a fresh one is -- and planned inside the transaction, so it sees
    what the verdicts before it wrote. One that no longer revalidates is voided with the
    refusal's own code and message: a superseded submission's names its successor, a
    purged recording's the purge. A `Settlement` (the recording can no longer be heard)
    is written as one, withdrawing the submission too. A refusal that leaves the
    submission pending -- its verdict was wrong, not its answer -- voids only the verdict,
    and the answer goes back to waiting for a judge.

    Returns what was applied and every verdict voided.
    """

    from linguawiki.services import withdrawal

    applied: list[AppliedVerdict] = []
    voided: list[str] = []
    for verdict_id, submission_id, content_id in withdrawal.outstanding(database, run_id).held:
        if verdict_id in voided:
            continue
        try:
            plan = plan_verdict(database, held_verdict_request(database, verdict_id), root=root)
        except LinguaWikiError as failure:
            voided.extend(
                withdrawal.void_held_verdicts(
                    database,
                    submission_id=submission_id,
                    code=failure.payload.code,
                    reason=failure.payload.message,
                )
            )
            continue
        if not isinstance(plan, VerdictPlan):
            voided.extend(settle(database, plan).voided_verdicts)
            continue
        if plan.action == REPEAT:
            # Unreachable: a held verdict is planned against its own submission, excluding
            # itself, and a verdict identical to one already standing is never stored -- the
            # second delivery is a repeat that writes nothing. Two held verdicts for one
            # submission would need a different score, which is a conflict and was refused
            # when it arrived. Applying it would credit one answer twice, so fail loudly.
            raise AssertionError(
                f"held verdict {verdict_id} plans as a repeat of {plan.verdict_id}; a held "
                "verdict is never stored beside an identical standing one"
            )
        written = write_verdict(database, plan)
        if written.result_id is None or written.verdict_id is None:
            raise AssertionError(f"applying held verdict {verdict_id} wrote no result")
        applied.append(
            AppliedVerdict(
                verdict_id=written.verdict_id,
                submission_id=submission_id,
                content_id=content_id,
                result_id=written.result_id,
                score=written.score,
            )
        )
    return tuple(applied), tuple(voided)


def _outstanding_refusal(run_id: str, owed: Outstanding) -> LinguaWikiError:
    return LinguaWikiError(
        "assessment_judgement_outstanding",
        f"run {run_id} still has {len(owed.pending)} answer(s) waiting for a judge and "
        f"{len(owed.held)} verdict(s) held until it resumes, and finalizing now would close "
        "it without them; wait for the judge (resume the run, and held verdicts apply), or "
        "finalize with --exclude-outstanding to withdraw them and close without them",
        details=(
            *(
                ErrorDetail(
                    field="submission",
                    reason="waiting for a judge",
                    context={"submission_id": submission_id, "content_id": content_id},
                )
                for submission_id, content_id in owed.pending
            ),
            *(
                ErrorDetail(
                    field="verdict",
                    reason="held until the run resumes",
                    context={
                        "verdict_id": verdict_id,
                        "submission_id": submission_id,
                        "content_id": content_id,
                    },
                )
                for verdict_id, submission_id, content_id in owed.held
            ),
        ),
    )


def _closing_report(database: Database, run_id: str) -> AssessmentRunReport:
    """A finalized run's report, naming what finalizing excluded -- read from the rows, so
    a replay of the finalize says what the original did."""

    from linguawiki.services import withdrawal

    excluded = [
        str(submission_id)
        for (submission_id,) in database.query(
            "SELECT submission_id FROM assessment_submissions "
            "WHERE run_id = ? AND status = 'withdrawn' AND withdrawn_code = ? "
            "ORDER BY created_at, submission_id",
            [run_id, withdrawal.FINALIZED_CODE],
        )
    ]
    voided = [
        str(verdict_id)
        for (verdict_id,) in database.query(
            "SELECT verdict.verdict_id FROM assessment_verdicts verdict "
            "JOIN assessment_verdict_outcomes outcome ON outcome.verdict_id = verdict.verdict_id "
            "JOIN assessment_submissions submission "
            "  ON submission.submission_id = verdict.submission_id "
            "WHERE submission.run_id = ? AND outcome.outcome = 'void' AND outcome.code = ? "
            "ORDER BY verdict.received_at, verdict.verdict_id",
            [run_id, withdrawal.FINALIZED_CODE],
        )
    ]
    report = run_report(database, run_id)
    if not excluded and not voided:
        return report
    return report.model_copy(
        update={
            "excluded": withdrawal.describe_withdrawn(database, excluded, voided),
            "voided_verdicts": withdrawal.describe_voided(database, voided),
        }
    )


def finalize(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    reason: str = "completed",
    exclude_outstanding: bool = False,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.finalize",
    actor: str = DEFAULT_ACTOR,
) -> AssessmentRunReport:
    """Close a run, writing one uncertainty-aware estimate per tested dimension.

    A run with judgements outstanding -- answers waiting for a judge, verdicts held for a
    resume -- is refused with `assessment_judgement_outstanding`, listing them, unless
    `exclude_outstanding` asks to close without them: then they are withdrawn
    (`assessment_run_finalized`) in the finalize transaction and the report's `excluded`
    names them. A verdict arriving afterwards meets a closed run, never a retroactive
    application.
    """

    from linguawiki.services import withdrawal

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
        settled = _settle_lapsed(database, run_id, command=command, actor=actor)

        # The command's own work, after the sweep: whatever it reports -- or refuses
        # with -- names the submissions the sweep withdrew.
        def work() -> AssessmentRunReport:
            row = _run_row(database, run_id)
            # Before the already-finalized early return, not after it. `finalize` writes the
            # caller's key straight into the same unique index as `record`, so it had the same
            # unpreflighted `ConstraintException` -- and placing the check below the early return
            # meant a key that closed this run for one reason answered 200 when it was reused for
            # another, which is the exact shape of "a guard placed after the path it guards is not
            # a guard". An exact retry still replays; what differs now conflicts.
            #
            # `exclude_outstanding` is part of the request, so a retry with it after a refusal
            # without it is a new request rather than a replay -- the remedy the refusal names
            # has to work. Only when set, so a keyed finalize from before it existed still
            # hashes to what it hashed to then.
            closing_fingerprint = idempotency.request_hash(
                operation=FINALIZED_EVENT,
                run_id=run_id,
                reason=reason,
                **({"exclude_outstanding": True} if exclude_outstanding else {}),
            )
            if (
                idempotency.resolve(
                    database,
                    key=idempotency_key,
                    event_type=FINALIZED_EVENT,
                    request_hash=closing_fingerprint,
                )
                is not None
            ):
                return _closing_report(database, run_id)
            if str(row[4]) == "finalized":
                # The run is already closed, so there is nothing to do -- but the key this call
                # was made under still has to be reserved. Left unbound, a key that successfully
                # "finalized" a run was indistinguishable from one nobody had used, and could go
                # on to open a run instead: an idempotency key identifies one operation, and a
                # key whose operation succeeded as a no-op has still been spent on it.
                if idempotency_key is not None:
                    with database.transaction() as transaction:
                        migration_module.record_domain_event(
                            transaction,
                            event_type=FINALIZED_EVENT,
                            aggregate_type="assessment_run",
                            aggregate_id=run_id,
                            correlation_id=EventId.new(),
                            payload_json=idempotency.payload(
                                closing_fingerprint, reason=reason, already_finalized=True
                            ),
                            idempotency_key=idempotency_key,
                        )
                return _closing_report(database, run_id)
            _assert_transition(run_id, current=str(row[4]), target="finalized")
            owed = withdrawal.outstanding(database, run_id)
            if owed and not exclude_outstanding:
                raise _outstanding_refusal(run_id, owed)
            conditions = json.loads(str(row[6]))
            kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
            record_track = learner_service.track_context(database, str(row[1]))
            levels = _pinned_levels(conditions, fallback=record_track.framework_levels)
            states = _dimension_states(database, run_id, kinds)
            basis = (
                "placement"
                if conditions["calibration_label"] == "comprehensive-placement"
                else "calibration"
            )
            with database.transaction() as transaction:
                now = transaction.now()
                excluded, voided = withdrawal.withdraw_outstanding(
                    transaction,
                    run_id,
                    code=withdrawal.FINALIZED_CODE,
                    reason="the run was finalized without waiting for this answer's judgement",
                )
                for state in states:
                    closed = (
                        state if state.status != "open" else close_dimension(state, reason=reason)
                    )
                    _write_state(
                        transaction, run_id=run_id, state=closed, levels=levels, insert=False
                    )
                    level, low, high = estimated_level(closed, levels)
                    estimate_service.upsert_from_state(
                        transaction,
                        track_id=str(row[1]),
                        framework_id=record_track.proficiency_framework,
                        state=closed,
                        level=level,
                        low=low,
                        high=high,
                        run_id=run_id,
                        basis=basis if closed.tasks_used else "declared-hypothesis",
                        reason=(
                            f"{conditions['calibration_label']} run {run_id} finalized "
                            f"({closed.stop_reason or reason})"
                        ),
                    )
                transaction.execute(
                    "UPDATE assessment_runs SET status = 'finalized', stop_reason = ?, "
                    "finalized_at = ?, updated_at = ? WHERE run_id = ?",
                    [reason, now, now, run_id],
                )
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    actor=actor,
                    affected_records_json=json.dumps(
                        [
                            run_id,
                            *sorted({*voided, *(entry.submission_id for entry in excluded)}),
                        ]
                    ),
                    after_summary=f"finalized with reason {reason}"
                    + _settled_summary(applied=0, voided=voided, withdrawn=len(excluded)),
                )
                migration_module.record_domain_event(
                    transaction,
                    event_type="assessment.finalized",
                    aggregate_type="assessment_run",
                    aggregate_id=run_id,
                    correlation_id=EventId.new(),
                    payload_json=idempotency.payload(closing_fingerprint, reason=reason),
                    idempotency_key=idempotency_key or f"assessment.finalized:{run_id}",
                )
            return _closing_report(database, run_id)

        return _noting_settled(settled, work)


def declared_estimate(
    database: Database,
    *,
    track_id: str,
    framework_id: str,
    levels: Sequence[str],
    dimension: str,
    dimension_kind: str,
    declared_level: str | None,
    reason: str,
    factors: Sequence[estimate_service.EstimateFactor] = (),
) -> estimate_service.EstimateChange:
    """Write one provisional estimate centred on a declared level -- and no higher.

    A declared level is a hypothesis, so the estimate is `declared-hypothesis` with no
    evidence behind it. Used to seed a track, and to fall back to when the evidence a
    dimension had was withdrawn: the learner's own claim is what is left, and it is
    labelled as one.
    """

    declared_index = float(levels.index(declared_level)) if declared_level in levels else None
    state = initial_state(
        dimension=dimension,
        dimension_kind=dimension_kind,
        level_count=len(levels),
        declared_index=declared_index,
    )
    low, high = credible_interval(state.grid, state.prior)
    return estimate_service.upsert_from_state(
        database,
        track_id=track_id,
        framework_id=framework_id,
        state=state,
        level=declared_level,
        low=levels[max(0, min(len(levels) - 1, int(low)))],
        high=levels[max(0, min(len(levels) - 1, int(high) + (1 if high % 1 else 0)))],
        run_id=None,
        basis="declared-hypothesis",
        reason=reason,
        extra_factors=factors,
    )


def seed_declared_estimates(
    database: Database,
    *,
    track_id: str,
    framework_id: str,
    levels: Sequence[str],
    dimension_kinds: Mapping[str, str],
    declared_level: str | None,
) -> tuple[str, ...]:
    """Seed provisional low-confidence estimates centred on a declared level.

    A declared level is a hypothesis, so every seeded estimate is `declared-hypothesis`
    with no evidence behind it. Nothing here marks knowledge mastered.
    """

    seeded: list[str] = []
    for dimension, kind in sorted(dimension_kinds.items()):
        declared_estimate(
            database,
            track_id=track_id,
            framework_id=framework_id,
            levels=levels,
            dimension=dimension,
            dimension_kind=kind,
            declared_level=declared_level,
            reason=(
                f"seeded from the learner's declared level {declared_level}"
                if declared_level
                else "seeded with no declared level, so the prior is broad"
            ),
        )
        seeded.append(dimension)
    return tuple(seeded)


def run_dimension_kinds(database: Database, run_id: str) -> dict[str, str]:
    """The dimension kinds this run was opened with, as it recorded them."""

    conditions = json.loads(str(_run_row(database, run_id)[6]))
    return {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}


def replay_dimension(database: Database, run_id: str, dimension: str) -> DimensionState:
    """Rebuild one dimension's posterior from the results that still stand, and store it.

    Starts at the prior the run recorded for the dimension -- never a fresh one, which
    would quietly drop the declared level the run started from -- and folds each surviving
    result in the order it was recorded, through the same `record_score` the run used and
    the facts it snapshotted when it served each task. A withdrawn result simply is not
    there: the posterior becomes the one the run would have had without it.

    A closed run stays closed. A dimension the replay leaves open in a finalized or
    abandoned run is closed with the reason it originally stopped for; in a run still being
    worked it stays open, and the next serve can measure it again.

    Writes the state inside the caller's connection, so it belongs in the caller's
    transaction.
    """

    row = _run_row(database, run_id)
    conditions = json.loads(str(row[6]))
    kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
    record_track = learner_service.track_context(database, str(row[1]))
    levels = _pinned_levels(conditions, fallback=record_track.framework_levels)
    stored = next(
        state
        for state in _dimension_states(database, run_id, kinds)
        if state.dimension == dimension
    )
    state = DimensionState(
        dimension=stored.dimension,
        dimension_kind=stored.dimension_kind,
        grid=stored.grid,
        prior=stored.prior,
        posterior=stored.prior,
        minimum_tasks=stored.minimum_tasks,
        maximum_tasks=stored.maximum_tasks,
    )
    for content_id, score in database.query(
        "SELECT content_id, raw_score FROM assessment_results "
        "WHERE run_id = ? AND dimension = ? AND invalidated_at IS NULL "
        "ORDER BY recorded_at, result_id",
        [run_id, dimension],
    ):
        served = database.one(
            "SELECT sequence, dimension, status, task_type, level_code, difficulty, "
            "content_family, modality, is_anchor FROM assessment_run_tasks "
            "WHERE run_id = ? AND content_id = ?",
            [run_id, str(content_id)],
        )
        assert served is not None
        candidate = _served_candidate(run_id, content_id=str(content_id), served=served)
        state = record_score(state, candidate, score=float(score), level_count=len(levels))
    if state.status == "open" and str(row[4]) not in RESUMABLE_STATUSES:
        state = close_dimension(
            state, reason=stored.stop_reason or "the evidence it rested on was withdrawn"
        )
    _write_state(database, run_id=run_id, state=state, levels=levels, insert=False)
    return state


def run_state(database: Database, run_id: str, dimension: str) -> DimensionState:
    """One dimension's stored state, for a caller that needs the run's account of it."""

    kinds = run_dimension_kinds(database, run_id)
    return next(
        state
        for state in _dimension_states(database, run_id, kinds)
        if state.dimension == dimension
    )


def run_levels(database: Database, run_id: str) -> tuple[str, ...]:
    """The level list this run's estimates are expressed on, as it was pinned."""

    row = _run_row(database, run_id)
    record_track = learner_service.track_context(database, str(row[1]))
    return _pinned_levels(json.loads(str(row[6])), fallback=record_track.framework_levels)


def run_basis(database: Database, run_id: str) -> str:
    """`placement` or `calibration`: what an estimate written from this run rests on."""

    conditions = json.loads(str(_run_row(database, run_id)[6]))
    return (
        "placement"
        if conditions["calibration_label"] == "comprehensive-placement"
        else "calibration"
    )


class ServedTaskReport(ContractModel):
    """What the learner was shown, read from the record of the serving.

    Never from `assessment_tasks`. A pack is mutable and a run is not, so re-reading the
    bank would let a pack edit made after the sitting decide what the learner is held to
    have been asked -- new choices against the answer key 0030 snapshotted, or a
    replaced recording under the same key.
    """

    run_id: str
    content_id: str
    sequence: int
    status: str
    dimension: str
    task_type: str
    modality: str
    level_code: str
    difficulty: float
    content_family: str
    prompt: str | None = None
    presentation: TaskPresentation | None = None
    asset: ServedAsset | None = None
    rubric_version: int = 1
    #: The rubric *body*, not only the version naming it. 0030 has snapshotted it since
    #: the stage that added it, and reporting the version alone named a document this
    #: report did not hand over -- so a caller that needed it had to go back to the
    #: mutable bank, which is the one thing a served snapshot exists to prevent.
    rubric: dict[str, object] = Field(default_factory=dict)
    #: `None` for a row served before 0032, which is truthful: no snapshot held it. Not
    #: `"none"`, which is a claim that help was refused rather than an absence of record.
    permitted_help: str | None = None


def _stored_rubric(raw: object) -> dict[str, object]:
    """A snapshotted rubric body, or an empty one when it cannot be read.

    `rubric_json` carries no `json_valid`, so this is the second place it is parsed and
    the first that must not raise: `served_task_report` answers "what was the learner
    shown", and a damaged rubric does not change that answer. `served_answer_key_wellformed`
    is what reports the damage.
    """

    if raw is None:
        return {}
    try:
        document = json.loads(str(raw))
    except (ValueError, RecursionError):
        return {}
    return document if isinstance(document, dict) else {}


def _refuse_presentation(code: str, message: str, reason: str) -> LinguaWikiError:
    return LinguaWikiError(
        code, message, details=(ErrorDetail(field="presentation", reason=reason),)
    )


@dataclass(frozen=True, slots=True)
class InstalledAsset:
    """One recording the installed pack ships, as `pack install` recorded it."""

    content_id: str
    asset_key: str
    path: str
    sha256: str
    media_type: str


def _installed_asset(
    database: Database,
    pack_key: str | None,
    *,
    content_id: str | None = None,
    asset_key: str | None = None,
) -> InstalledAsset | None:
    """A recording of the pack this track is taught from, by content ID or asset key.

    Scoped to that pack rather than to "the installed pack": a workspace may hold two, and
    a reference is resolved inside the pack the track names. Read from `pack_assets`, which
    `pack install` writes, rather than by loading the whole pack directory to find one
    digest -- what C2 did while no pack shipped audio.
    """

    pack_id = pack_service.installed_pack(database, pack_key)["pack_id"]
    column, value = (
        ("content_id", content_id) if content_id is not None else ("asset_key", asset_key)
    )
    row = database.one(
        "SELECT content_id, asset_key, path, sha256, media_type FROM pack_assets "
        f"WHERE pack_id = ? AND {column} = ?",
        [pack_id, value],
    )
    if row is None:
        return None
    return InstalledAsset(
        content_id=str(row[0]),
        asset_key=str(row[1]),
        path=str(row[2]),
        sha256=str(row[3]),
        media_type=str(row[4]),
    )


#: What a refusal says when a pack's recordings are not in `pack_assets`. A pack installed
#: before C5 recorded none, and the way forward is to install it again.
REINSTALL_HINT = "if the pack was installed before C5, `pack install` it again to record them"


def _asset_bytes(
    database: Database, pack_key: str | None, asset: InstalledAsset
) -> tuple[bytes | None, str]:
    """The bytes of an installed recording, or nothing and the reason it cannot be read.

    Resolved with containment under the installed source path: a symlink out of the pack
    reads through to bytes nobody installed.
    """

    source = str(pack_service.installed_pack(database, pack_key)["source_path"] or "")
    if not source:
        return None, "the pack's installed location is not recorded"
    # Outside the handler: a pack that cannot be found reports itself, by its own code.
    # Folding that into "the recording's path cannot be resolved" sends an operator to look
    # for one missing file when the whole pack is the problem.
    pack_root = resolve_pack_path(source)
    try:
        root = pack_root.resolve(strict=True)
        path = (root / asset.path).resolve(strict=True)
    except (OSError, RuntimeError):
        # `RuntimeError` is a symlink loop under Python 3.12, which no handler above this
        # one would catch.
        return None, "its path cannot be resolved"
    if not path.is_relative_to(root) or not path.is_file():
        return None, "its path leaves the pack directory"
    try:
        size = path.stat().st_size
        if size > MAXIMUM_RECORDING_BYTES:
            return None, f"it is {size} bytes, over the cap"
        return path.read_bytes(), ""
    except OSError:
        return None, "it cannot be read"


def _run_pack_key(database: Database, run_id: str) -> str | None:
    """The pack the run's track is taught from. `None` leaves pack selection as it was."""

    row = _run_row(database, run_id)
    return learner_service.track_context(database, str(row[1])).pack_key


def _plays_audio(shown: TaskPresentation | None) -> bool:
    return shown is not None and shown.audio is not None


def _serve_asset_identity(
    database: Database, pack_key: str | None, shown: TaskPresentation | None
) -> ServedAsset | None:
    """The recording this serving actually played, recorded when it is played.

    There is no second chance at this. The identity can only be established while the
    task is being served, so a serve that writes the presentation and not the identity
    manufactures exactly the half-state `served_presentation_complete` exists to report,
    with no way forward for anybody who finds it afterwards.

    The recording and the answer key have to come from one revision of the task. The bank
    and `pack_assets` hold what was *installed*; the bytes are read from the pack
    directory, which an author can re-record and republish without reinstalling. Serving
    then paired a recording no installed revision contained with the installed key. So the
    one file is hashed here, while the task is still unserved, and a difference refuses.
    """

    if shown is None or shown.audio is None:
        return None
    asset = _installed_asset(database, pack_key, asset_key=shown.audio.asset_key)
    if asset is None:
        raise LinguaWikiError(
            "assessment_asset_unavailable",
            f"this task plays {shown.audio.asset_key}, which the installed pack does not hold; "
            + REINSTALL_HINT,
            details=(ErrorDetail(field="asset", reason="no installed pack holds it"),),
        )
    data, reason = _asset_bytes(database, pack_key, asset)
    if data is None:
        raise _asset_unavailable(asset.content_id, reason)
    if hashlib.sha256(data).hexdigest() != asset.sha256:
        raise LinguaWikiError(
            "assessment_task_revision_drifted",
            f"the pack directory holds a different recording for {asset.asset_key} than the "
            "one installed, so it does not belong to the answer key that would be served "
            "with it; install the pack version you mean to serve",
            details=(ErrorDetail(field="asset", reason=f"installed {asset.sha256}"),),
        )
    return ServedAsset(content_id=asset.content_id, sha256=asset.sha256)


def _resolve_served_asset(
    database: Database, identity: ServedAsset, *, pack_key: str | None
) -> ServedAsset:
    """Prove the recording the snapshot names is still the recording it named.

    The identity is `(content id, sha256)` and only the digest can answer the question:
    a key resolves to whatever currently answers to it, which is precisely the
    substitution this refuses.

    The two refusals are separate codes on purpose. "Nobody has this recording" and
    "somebody has a different one" send an operator to different places, and matching
    error prose to tell them apart is how that distinction gets lost.
    """

    resolved = _installed_asset(database, pack_key, content_id=identity.content_id)
    if resolved is None:
        raise LinguaWikiError(
            "assessment_asset_unavailable",
            f"the recording {identity.content_id} this task played is not available; "
            + REINSTALL_HINT,
            details=(ErrorDetail(field="asset", reason="the pack no longer holds it"),),
        )
    if resolved.sha256 != identity.sha256:
        raise LinguaWikiError(
            "assessment_asset_changed",
            f"the recording {identity.content_id} has been replaced since it was served; "
            "a different recording is a different question",
            details=(ErrorDetail(field="asset", reason=f"served {identity.sha256}"),),
        )
    return identity


#: The most a recording served to the page may be. A listening task plays seconds of
#: audio; anything near this is not a placement recording, and the single-threaded server
#: would serve nothing else while it streamed.
MAXIMUM_RECORDING_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ServedRecording:
    """The bytes of the recording a served task plays, already proven to be those bytes."""

    content_id: str
    media_type: str
    data: bytes


def _asset_unavailable(content_id: str, reason: str) -> LinguaWikiError:
    return LinguaWikiError(
        "assessment_asset_unavailable",
        f"the recording {content_id} this task plays cannot be read: {reason}",
        details=(ErrorDetail(field="asset", reason=reason),),
    )


def served_recording(
    paths: WorkspacePaths,
    *,
    content_id: str,
    run: str | None = None,
    clock: Clock | None = None,
) -> ServedRecording:
    """The recording an outstanding task was served with, or a refusal naming why not.

    Resolved through the run's own record of the serving, never by re-reading the pack, and
    hashed as it is read: the bytes handed back are compared with the digest the snapshot
    holds, so a learner hears the recording the task was served with or none. A file
    replaced on disk since the pack was installed is caught by that hash: `pack_assets`
    says what was installed, and only the bytes can say what is there now.

    Only while the task is outstanding. A settled task's recording is no longer part of an
    open question, and serving it would let a finished answer be revisited.
    """

    with open_reader(paths, clock=clock or SystemClock()) as database:
        run_id = resolve_run(database, run)
        shown = served_task_report(database, run_id, content_id=content_id)
        if shown.status != "served":
            raise LinguaWikiError(
                "assessment_task_settled",
                f"{content_id} is {shown.status}, so its recording is no longer part of an "
                "open question",
                details=(ErrorDetail(field="content_id", reason=f"task is {shown.status}"),),
            )
        identity = shown.asset
        if identity is None:
            raise LinguaWikiError(
                "assessment_task_plays_nothing",
                f"{content_id} was served with no recording, so there is nothing to play",
                details=(ErrorDetail(field="content_id", reason="no recording"),),
            )
        pack_key = _run_pack_key(database, run_id)
        resolved = _installed_asset(database, pack_key, content_id=identity.content_id)
        if resolved is None:
            raise _asset_unavailable(identity.content_id, "the pack no longer holds it")
        data, reason = _asset_bytes(database, pack_key, resolved)
    if data is None:
        raise _asset_unavailable(identity.content_id, reason)
    if hashlib.sha256(data).hexdigest() != identity.sha256:
        raise LinguaWikiError(
            "assessment_asset_changed",
            f"the recording {identity.content_id} has been replaced since it was served; "
            "a different recording is a different question",
            details=(ErrorDetail(field="asset", reason=f"served {identity.sha256}"),),
        )
    return ServedRecording(
        content_id=identity.content_id, media_type=resolved.media_type, data=data
    )


def served_task(
    paths: WorkspacePaths,
    *,
    content_id: str,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> ServedTaskReport:
    """One served task, as it was served."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        resolved_run = resolve_run(database, run, track_id=track_id)
        return served_task_report(database, resolved_run, content_id=content_id)


def served_task_report(database: Database, run_id: str, *, content_id: str) -> ServedTaskReport:
    """The `(database, id)` form, for use inside a writer that already holds the lock."""

    row = database.one(
        "SELECT sequence, status, dimension, task_type, level_code, difficulty, "
        "content_family, modality, rubric_version, prompt_snapshot, presentation_json, "
        "asset_identity_json, content_hash, rubric_json, permitted_help "
        "FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
        [run_id, content_id],
    )
    if row is None:
        raise LinguaWikiError(
            "assessment_task_not_served",
            "that task was not served in this run; ask for the next task first",
            details=(ErrorDetail(field="content_id", reason="task was not served"),),
        )
    shown = parse_task_presentation(None if row[10] is None else str(row[10]))
    identity = parse_asset_identity(None if row[11] is None else str(row[11]))
    # The two columns are one group. An identity with no presentation, and an audio
    # presentation with no identity, are each damage rather than a legacy row: neither
    # can say what the learner heard, and reading past either sends the reader back to
    # the mutable bank the snapshot exists to replace.
    if identity is not None and shown is None:
        raise _refuse_presentation(
            "assessment_presentation_partial",
            "this task recorded a recording it was played with but not how it was shown",
            "asset identity without a presentation",
        )
    if shown is not None and shown.audio is not None and identity is None:
        raise _refuse_presentation(
            "assessment_presentation_partial",
            "this task recorded that it played a recording but not which bytes",
            "audio presentation without an asset identity",
        )
    if shown is None:
        _assert_no_presentation_was_lost(database, content_id=content_id, served_hash=row[12])
    return ServedTaskReport(
        run_id=run_id,
        content_id=content_id,
        sequence=int(row[0]),
        status=str(row[1]),
        dimension=str(row[2]),
        task_type=str(row[3]),
        level_code=str(row[4]),
        difficulty=float(row[5]),
        content_family=str(row[6]),
        modality=str(row[7]),
        rubric_version=int(row[8]),
        prompt=None if row[9] is None else str(row[9]),
        # Parsed defensively: the column is a round trip through JSON and 0030 could not
        # put a `json_valid` on it, so a damaged body is reported as absent rather than
        # taking down a reader that only wanted to know what was shown.
        rubric=_stored_rubric(row[13]),
        permitted_help=None if row[14] is None else str(row[14]),
        presentation=shown,
        asset=None
        if identity is None
        else _resolve_served_asset(database, identity, pack_key=_run_pack_key(database, run_id)),
    )


def _assert_no_presentation_was_lost(
    database: Database, *, content_id: str, served_hash: object
) -> None:
    """A null snapshot is truthful for a row served before 0031 -- and only then.

    Before that migration no bank held a presentation, so nothing could have been shown
    and nothing was lost. But if the live bank row holds one *and* its content hash still
    equals the snapshotted one, the content is provably identical and cannot have been
    served without it: the snapshot is damage, not a legacy row. Falling back to the bank
    here would be exactly the substitution the snapshot exists to prevent, so it refuses.
    """

    if served_hash is None:
        return
    row = database.one(
        "SELECT task.presentation_json, record.content_hash FROM assessment_tasks task "
        "JOIN content_records record ON record.content_id = task.content_id "
        "WHERE task.content_id = ?",
        [content_id],
    )
    if row is None or not snapshot_lost_its_presentation(
        served_hash=served_hash, bank_hash=row[1], bank_presentation=row[0]
    ):
        return
    raise _refuse_presentation(
        "assessment_presentation_partial",
        "this task recorded no presentation, but the unchanged bank row has one; "
        "the snapshot is damaged rather than older than the column",
        "null snapshot against an unchanged bank row that has a presentation",
    )


#: The statuses a run can be resumed from. Discovery lists nothing else, because a page
#: offering a finalized or abandoned run would be offering work that cannot be done.
RESUMABLE_STATUSES: tuple[str, ...] = ("in-progress", "paused")
#: How many runs one discovery answer carries. A learner has a handful of open runs; the
#: bound exists so the answer has one, and what it leaves out is counted in `omitted`.
DISCOVERY_LIMIT = 20


class RunListReport(ContractModel):
    """The runs a fresh page could resume, newest first, for one track."""

    track_id: str
    runs: tuple[AssessmentRunReport, ...] = ()
    #: Resumable runs beyond the bound. Half a list looks exactly like a short one, so the
    #: count of what was left out travels with what was kept.
    omitted: int = 0
    #: Whether this track lets a learner record a spoken answer, and how long a recording
    #: is kept. A page reads it before it has a run, to know which run to offer to start.
    recording: learner_service.RecordingPolicy | None = None
    #: Whether this track keeps a written answer whole, so a judge can mark it from what
    #: the learner typed (`placement.written_judging_permitted`). The page reads it beside
    #: `recording.offered` to choose `machine+judged`; it has no preferences of its own to
    #: decide that from, and a second account of the rule would be one to drift.
    written_offered: bool = False


def resumable_runs(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    statuses: Sequence[str] | None = None,
    clock: Clock | None = None,
) -> RunListReport:
    """The runs a page that holds only a launch token can find and offer to resume.

    Every read route needs a run identifier and the launch URL carries only the token, so
    without this a fresh page cannot find the run it should resume. The track resolves
    exactly as it does for every other command -- the workspace's only active track when
    none is named -- so a run is never offered to the wrong learner's page.
    """

    wanted = tuple(statuses) if statuses else RESUMABLE_STATUSES
    unknown = sorted(set(wanted) - set(RESUMABLE_STATUSES))
    if unknown:
        raise LinguaWikiError(
            "invalid_arguments",
            f"only resumable runs can be discovered; {unknown} is not one of "
            f"{list(RESUMABLE_STATUSES)}",
            details=(ErrorDetail(field="status", reason="not resumable"),),
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        placeholders = ", ".join("?" for _ in wanted)
        rows = database.query(
            "SELECT run_id FROM assessment_runs "
            f"WHERE track_id = ? AND status IN ({placeholders}) "
            "ORDER BY started_at DESC, run_id DESC",
            [track_id, *wanted],
        )
        kept = rows[:DISCOVERY_LIMIT]
        return RunListReport(
            track_id=track_id,
            runs=tuple(run_report(database, str(row[0])) for row in kept),
            omitted=len(rows) - len(kept),
            recording=learner_service.track_recording_policy(database, track_id),
            written_offered=written_judging_permitted(
                learner_service.track_context(database, track_id).preferences
            ),
        )


def report(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> AssessmentRunReport:
    """The full state of a run: per-dimension estimates, budgets, and untested gaps."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        return run_report(database, resolve_run(database, run, track_id=track_id))


def run_report(database: Database, run_id: str) -> AssessmentRunReport:
    """Read a run's state through a caller's connection.

    Reports take a `Database` rather than a workspace because DuckDB serves one
    connection per database file: a command that has just written cannot open a second
    connection to report what it did without deadlocking on its own writer lock.
    """

    from linguawiki.services import judging

    row = _run_row(database, run_id)
    conditions = json.loads(str(row[6]))
    kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
    record_track = learner_service.track_context(database, str(row[1]))
    levels = _pinned_levels(conditions, fallback=record_track.framework_levels)
    states = _dimension_states(database, run_id, kinds)
    judgements = judging.outstanding_judgements(database, run_id)
    in_hand, judged_only = _held_work(
        database,
        run_id,
        lapsed=frozenset(
            entry.submission_id for entry in judgements if entry.claim_state == "lapsed"
        ),
    )
    reports: list[DimensionReport] = []
    for state in states:
        level, low, high = estimated_level(state, levels)
        minimum, maximum = budget_for(state.dimension_kind)
        reports.append(
            DimensionReport(
                dimension=state.dimension,
                dimension_kind=state.dimension_kind,
                status=state.status,
                progress=_dimension_progress(
                    state.status, dimension=state.dimension, judged=judged_only
                ),
                tasks_used=state.tasks_used,
                minimum_tasks=minimum,
                maximum_tasks=maximum,
                families=state.families,
                task_types=state.task_types,
                boundary_probed=state.boundary_probed,
                stop_reason=state.stop_reason,
                confidence=(
                    "not-tested" if state.status == "not-tested" else confidence_label(state)
                ),
                estimated_level=level,
                credible_low=low,
                credible_high=high,
                posterior_mean=(
                    posterior_mean(state.grid, state.posterior) if state.tasks_used else None
                ),
                uncertainty=(
                    posterior_sd(state.grid, state.posterior) if state.tasks_used else None
                ),
                unavailable_reason=(state.stop_reason if state.status == "not-tested" else None),
            )
        )
    served = int(
        database.scalar("SELECT count(*) FROM assessment_run_tasks WHERE run_id = ?", [run_id])
    )
    # Invalidated results are their own count. They were recorded, and their recording is
    # gone: reporting them as recorded would credit the run with evidence it no longer has.
    recorded, invalidated = (
        int(value)
        for value in database.one(
            "SELECT count(*) FILTER (WHERE invalidated_at IS NULL), "
            "count(*) FILTER (WHERE invalidated_at IS NOT NULL) "
            "FROM assessment_results WHERE run_id = ?",
            [run_id],
        )
        or (0, 0)
    )
    return AssessmentRunReport(
        run_id=run_id,
        track_id=str(row[1]),
        run_type=str(row[3]),
        calibration_label=str(conditions["calibration_label"]),
        status=str(row[4]),
        algorithm_version=str(row[5]),
        pack_key=str(conditions["pack_key"]),
        pack_version=str(conditions["pack_version"]),
        pack_maturity=str(conditions["pack_maturity"]),
        framework_id=str(conditions["framework_id"]),
        framework_levels=levels,
        declared_level=conditions.get("declared_level"),
        available_modalities=tuple(str(value) for value in conditions["available_modalities"]),
        dimensions=tuple(reports),
        scoring=_run_scoring(conditions),
        tasks_served=served,
        tasks_recorded=recorded,
        results_invalidated=invalidated,
        stop_reason=None if row[7] is None else str(row[7]),
        started_at=str(aware_utc(row[8]).isoformat()),
        finalized_at=None if row[9] is None else str(aware_utc(row[9]).isoformat()),
        untested_dimensions=tuple(
            entry.dimension for entry in reports if entry.status == "not-tested"
        ),
        progress=_run_progress(str(row[4]), reports, in_hand=in_hand, judgements=judgements),
        outstanding_judgements=judgements,
    )


def _held_work(
    database: Database, run_id: str, *, lapsed: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """The dimensions holding a served task by whose move it is: `(in hand, judged only)`.

    A dimension is *in hand* while any task it holds still awaits the learner's answer, and
    *judged only* when every task it holds has an answer handed in that nobody has marked.
    "Handed in" is the screen's own derivation -- a `served` task with a live submission
    (`recordings.live_submission`, as `_batch_task_state` reads it) -- because a second
    account of "is this waiting for a judge" is a second thing to drift.

    A `lapsed` submission -- every judging attempt used, none live, no verdict standing,
    as `judging.lapsed_submissions` defines it -- blocks nothing. No judge can claim it, and
    the next writer to touch the run withdraws it and frees the dimension, so its dimension
    counts as in hand: serving is what moves it on. Reading it as waiting would leave a
    page that only polls waiting for a judgement nobody may now deliver.
    """

    from linguawiki.services import recordings as recording_service

    in_hand: set[str] = set()
    judged: set[str] = set()
    for dimension, content_id in database.query(
        "SELECT dimension, content_id FROM assessment_run_tasks "
        "WHERE run_id = ? AND status = 'served' ORDER BY sequence",
        [run_id],
    ):
        live = recording_service.live_submission(database, run_id, str(content_id))
        if live is None or live.submission_id in lapsed:
            in_hand.add(str(dimension))
        else:
            judged.add(str(dimension))
    return frozenset(in_hand), frozenset(judged - in_hand)


def _dimension_progress(
    status: str, *, dimension: str, judged: frozenset[str]
) -> DimensionProgress:
    if status != "open":
        return "closed"
    if dimension in judged:
        return "waiting"
    return "open"


def _run_progress(
    status: str,
    dimensions: Sequence[DimensionReport],
    *,
    in_hand: frozenset[str],
    judgements: Sequence[OutstandingJudgement],
) -> RunProgress:
    """What is left in the run, read from what the dimensions and the judgements say.

    A task in the learner's hands counts as work even in a dimension that has closed:
    `record` still accepts it, so a run holding one is not waiting and not complete. A
    pending submission counts as waiting whatever its dimension's state, because finalizing
    refuses while one is outstanding -- "complete" is never said of a run that finalizing
    would refuse for a judgement it is still owed.
    """

    if status in {"finalized", "abandoned"}:
        return "closed"
    if in_hand or any(entry.progress == "open" for entry in dimensions):
        return "working"
    # A lapsed submission is owed nothing by a judge: the next writer withdraws it.
    if any(entry.claim_state != "lapsed" for entry in judgements):
        return "waiting"
    return "complete"


__all__ = [
    "AppliedVerdict",
    "AssessmentRunReport",
    "BatchReport",
    "BatchTask",
    "BatchTaskState",
    "ClaimStage",
    "DimensionProgress",
    "DimensionReport",
    "NextTaskReport",
    "OutstandingJudgement",
    "PlayReport",
    "RunListReport",
    "RunProgress",
    "ServeContext",
    "ServePlan",
    "SubmissionKind",
    "VerdictPlan",
    "VerdictRequest",
    "VerdictWrite",
    "VoidedVerdict",
    "WithdrawnSubmission",
    "finalize",
    "held_verdict_request",
    "next_batch",
    "next_task",
    "plan_serve",
    "plan_verdict",
    "plays_remaining",
    "record",
    "record_play",
    "report",
    "resumable_runs",
    "retain_rubric",
    "seed_declared_estimates",
    "served_recording",
    "set_status",
    "settle",
    "start",
    "write_serve",
    "write_verdict",
]
