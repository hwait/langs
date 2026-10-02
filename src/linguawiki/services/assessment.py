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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

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
    RECORDED_JUDGED_TASK_TYPES,
    RECORDED_SCORING,
    REUSE_WINDOW_MONTHS,
    SCORING_POLICY_VERSION,
    SPOKEN_MODALITY,
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
    posterior_mean,
    posterior_sd,
    record_score,
    recording_permitted,
    score_response,
    select_task,
    servable_candidate,
    servable_under,
    unavailable_reason,
)
from linguawiki.provenance import PROMOTED_LIFECYCLES
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service

if TYPE_CHECKING:
    from pathlib import Path

    from linguawiki.services.recordings import JudgeableAudio
    from linguawiki.services.withdrawal import Settlement, SettlementOutcome

#: Modalities a workspace can always offer, whatever the learner's equipment.
BASELINE_MODALITIES = ("text", "writing", "audio")
ESTIMATE_CALCULATION_VERSION = "estimate.v1"


class DimensionReport(ContractModel):
    dimension: str
    dimension_kind: str
    status: str
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
    """Serve the next task, or report the run when no dimension is still open."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
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
                open_dimensions=[
                    state.dimension
                    for state in _dimension_states(
                        database,
                        run_id,
                        {
                            str(k): str(v)
                            for k, v in json.loads(str(row[6]))["dimension_kinds"].items()
                        },
                    )
                    if state.status == "open"
                ],
            )
        _assert_running(run_id, status=str(row[4]))
        conditions = json.loads(str(row[6]))
        kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
        record = learner_service.track_context(database, str(row[1]))
        pack_row = pack_service.installed_pack(database, record.pack_key)
        _assert_same_bank(run_id, conditions=conditions, pack_row=pack_row)
        states = _dimension_states(database, run_id, kinds)
        open_states = [state for state in states if state.status == "open"]
        if not open_states:
            return run_report(database, run_id)
        # The run's own condition, never the caller's: a run opened for machine scoring
        # never serves a task that needs a judge, whatever the bank has come to hold.
        scoring = _run_scoring(conditions)
        recorded = (
            frozenset()
            if scoring == DEFAULT_SCORING
            else _recorded_task_ids(database, pack_row["pack_id"])
        )
        # Read now, not from the run: consent withdrawn after the run opened stops the
        # next spoken task from being served, whatever the run was opened under.
        recording = recording_permitted(record.preferences)
        candidates = tuple(
            candidate
            for candidate in _candidates(database, pack_row["pack_id"])
            if servable_candidate(
                candidate, scoring=scoring, recorded=recorded, recording=recording
            )
        )
        excluded = _excluded_task_ids(
            database, track_id=str(row[1]), run_id=run_id, clock=active_clock
        )
        available = tuple(str(value) for value in conditions["available_modalities"])
        # Serve the least-progressed open dimension first, so a run that stops early has
        # spread its evidence rather than finishing one dimension and testing no other.
        open_states.sort(key=lambda state: (state.tasks_used, state.dimension))
        # A dimension holding an unanswered task is not eligible for another. The order
        # among the rest is unchanged, so the spread rule above still decides which of the
        # *free* dimensions goes first.
        outstanding = _outstanding_dimensions(database, run_id)
        free_states = [state for state in open_states if state.dimension not in outstanding]
        warnings: list[str] = []
        for state in free_states:
            selection = select_task(
                state, candidates, available_modalities=available, excluded=excluded
            )
            if selection is None:
                exhausted = close_dimension(state, reason="bank exhausted inside the reuse window")
                with database.transaction() as transaction:
                    _write_state(
                        transaction,
                        run_id=run_id,
                        state=exhausted,
                        levels=_pinned_levels(conditions, fallback=record.framework_levels),
                        insert=False,
                    )
                warnings.append(f"{state.dimension} stopped early: no unseen task is available")
                continue
            # The answer key is read here, from the bank row, and never from the selected
            # `Candidate`. `Candidate` carries everything item selection is allowed to
            # consider, and the key is not among it: putting it there would let the
            # staircase see the answers it is choosing between.
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
            # Resolved here, before anything is written: a shuffle happens once, and the
            # order the learner saw is the order that is stored. Re-reading the bank on
            # resume would reshuffle, which is a different question asked under the
            # identity of the one they were credited for.
            shown = _serve_presentation(
                parse_task_presentation(None if task[8] is None else str(task[8])),
                run_id=run_id,
                content_id=selection.candidate.content_id,
            )
            # Resolved here too, and before the transaction: a task that says it plays a
            # recording the installed pack cannot produce is refused while it is still
            # unserved, rather than written as a row nothing can read afterwards.
            played = None
            if _plays_audio(shown):
                played = _serve_asset_identity(database, record.pack_key, shown)
            sequence = (
                int(
                    database.scalar(
                        "SELECT coalesce(max(sequence), 0) FROM assessment_run_tasks "
                        "WHERE run_id = ?",
                        [run_id],
                    )
                )
                + 1
            )
            with database.transaction() as transaction:
                now = transaction.now()
                # The served facts are copied here, not re-read later. A pack is mutable
                # and a run is not: the score must fold in the difficulty of the task the
                # learner actually saw, and an observation attributed to this task must be
                # about the items it targeted when it was served -- not the ones a later
                # pack edit says it targets now.
                #
                # The same reasoning is why the answer key, the prompt, and the rubric
                # body are copied: they are what a scorer needs to reach a verdict, and
                # scoring against the live bank would let an edit made after the sitting
                # decide whether the learner was right. The three are written together,
                # always: a row carrying some of them and not the others can establish
                # neither what the learner faced nor that it predates the snapshot.
                transaction.execute(
                    "INSERT INTO assessment_run_tasks (run_id, sequence, content_id, dimension, "
                    "status, served_at, task_type, level_code, difficulty, content_family, "
                    "modality, is_anchor, rubric_version, content_hash, target_refs_json, "
                    "expected_json, prompt_snapshot, rubric_json, presentation_json, "
                    "asset_identity_json, permitted_help) "
                    "VALUES (?, ?, ?, ?, 'served', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        run_id,
                        sequence,
                        selection.candidate.content_id,
                        state.dimension,
                        now,
                        selection.candidate.task_type,
                        selection.candidate.level_code,
                        selection.candidate.difficulty,
                        selection.candidate.content_family,
                        selection.candidate.modality,
                        selection.candidate.is_anchor,
                        int(task[3]),
                        str(task[5]),
                        str(task[6]),
                        str(task[7]),
                        str(task[1]),
                        str(task[2]),
                        None
                        if shown is None
                        else json.dumps(
                            shown.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
                        ),
                        None
                        if played is None
                        else json.dumps(
                            played.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
                        ),
                        # The allowance the learner was held to, from the same row the
                        # report hands back -- so the stored value and the reported one
                        # cannot disagree. Snapshotted for the same reason as the prompt:
                        # a second serve of this task must not read it from a pack that
                        # has been edited since.
                        str(task[4]),
                    ],
                )
                # The learner has now seen it, whether or not they answer. Recording the
                # exposure here, in the same transaction, is what keeps an abandoned run
                # from handing the same task back inside the reuse window.
                _record_exposure(
                    transaction,
                    track_id=str(row[1]),
                    content_id=selection.candidate.content_id,
                    purpose=str(row[3]),
                    is_anchor=selection.candidate.is_anchor,
                    now=now,
                )
                # Serving is a mutation -- it writes a run task and spends an exposure --
                # and it wrote no audit row until C3. Putting it here rather than in the
                # server is what keeps the two entry points' trails identical: a guard or
                # a record the server owns is one the CLI does not have.
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    actor=actor,
                    affected_records_json=json.dumps(
                        [run_id, selection.candidate.content_id], sort_keys=True
                    ),
                    after_summary=(
                        f"served {selection.candidate.content_id} as {state.dimension} "
                        f"task {sequence}"
                    ),
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
                            content_id=selection.candidate.content_id,
                            dimension=state.dimension,
                            sequence=sequence,
                        ),
                        idempotency_key=idempotency_key,
                    )
            return NextTaskReport(
                run_id=run_id,
                dimension=state.dimension,
                sequence=sequence,
                content_id=selection.candidate.content_id,
                stable_key=str(task[0]),
                task_type=selection.candidate.task_type,
                modality=selection.candidate.modality,
                level_code=selection.candidate.level_code,
                difficulty=selection.candidate.difficulty,
                content_family=selection.candidate.content_family,
                prompt=str(task[1]),
                presentation=shown,
                asset=played,
                rubric=json.loads(str(task[2])),
                rubric_version=int(task[3]),
                permitted_help=str(task[4]),
                selection_reason=selection.reason,
                remaining_open_dimensions=tuple(
                    other.dimension for other in open_states if other.status == "open"
                ),
                warnings=tuple(warnings),
            )
        # Nothing fresh could be served. An open dimension still holding an unanswered
        # task has work on it, so the run is not finished and must not report as though it
        # were: hand that task back instead. The states are already sorted, so this is
        # the least-progressed one.
        held = [state for state in open_states if state.dimension in outstanding]
        handed_content_id = outstanding[held[0].dimension] if held else None
        # Read and validated *before* the key is claimed. `_hand_back` refuses a damaged
        # snapshot, and a refusal has to leave nothing behind: claiming the key first meant a
        # refused call burned it, so the retry the caller was entitled to make came back as a
        # conflict about a request that had never succeeded.
        handed = (
            None
            if handed_content_id is None
            else _hand_back(
                database,
                run_id,
                content_id=handed_content_id,
                open_dimensions=[state.dimension for state in held],
            )
        )
        if idempotency_key is not None:
            # Whatever this call did -- handed a task back, or closed the last dimension and
            # served nothing -- it is the one operation this key performed, and recording it
            # is what stops a retry from doing something else. A hand-back writes nothing
            # about the *learner*: no run task, no exposure, no dimension state. An event
            # saying which task this key was answered with is bookkeeping about the request,
            # and without it a retry after the task was scored went on to serve a different
            # one under the same key.
            with database.transaction() as transaction:
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
    if live is None and audio_artifact is None:
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
        "submission.artifact_id, run.track_id FROM assessment_submissions submission "
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
    if str(row[2]) != "recording":
        # Text submissions arrive with their own revalidation (eligibility, §2a); until that
        # exists, a verdict on one is refused rather than applied unchecked.
        raise LinguaWikiError(
            "assessment_submission_kind_unsupported",
            f"{submission_id} is a {row[2]} submission, and this release judges recordings only",
            details=(ErrorDetail(field="submission", reason=str(row[2])),),
        )
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
        if successor is None and live is not None:
            successor = live.submission_id
        context = {"successor": str(successor)}
        if live is not None and live.submission_id != successor:
            context["live"] = live.submission_id
        raise LinguaWikiError(
            "assessment_submission_superseded",
            f"{submission_id} was replaced by {successor}: the learner answered again, and a "
            f"verdict on the earlier answer judges something no longer submitted; judge "
            f"{live.submission_id if live is not None else successor} instead",
            details=(ErrorDetail(field="submission", reason="superseded", context=context),),
        )
    return _NamedSubmission(
        submission_id=submission_id, artifact_id=None if row[7] is None else str(row[7])
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


def _standing_verdict(
    database: Database, submission_id: str, *, excluding: str | None = None
) -> _Standing | None:
    """The submission's verdict that still stands -- applied, or held -- if there is one.

    A void verdict stands for nothing and is skipped. `excluding` is the held verdict being
    applied, which is not a rival to itself.
    """

    row = database.one(
        "SELECT verdict.verdict_id, verdict.raw_score, verdict.response_hash, "
        "verdict.response_visibility, outcome.outcome FROM assessment_verdicts verdict "
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
        "outcome.outcome FROM assessment_verdicts verdict "
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

    from linguawiki.services.withdrawal import Settlement

    run_id, content_id = request.run_id, request.content_id
    row = _run_row(database, run_id)
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
    # Every refusal below runs before any transaction opens, so a refused call leaves the
    # task still `served` and answerable rather than half-recorded.
    warnings: list[str] = []
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
    answered = str(served[2]) == "answered"
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
            _run_scoring(conditions) == RECORDED_SCORING
            and candidate.modality == SPOKEN_MODALITY
            and candidate.task_type in RECORDED_JUDGED_TASK_TYPES
        ),
    )
    if isinstance(bound, Settlement):
        return bound
    judged = bound
    submission_id = None if judged is None else judged.submission.submission_id
    if request.claim_id is not None:
        _assert_claim(
            database,
            request.claim_id,
            submission_id=submission_id
            if submission_id is not None
            else (None if named is None else named.submission_id),
        )
    if held_row is not None:
        # Retained when it was received; retention is not run twice on what it already
        # decided, and the response itself was never stored to run it on.
        visibility = str(held_row[3])
        excerpt = None if held_row[4] is None else str(held_row[4])
        response_hash = None if held_row[5] is None else str(held_row[5])
        rubric_json = str(held_row[2])
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
                    standing.response_visibility,
                    audio_artifact,
                ),
                score=resolved_score,
                response_hash=response_hash,
                visibility=visibility,
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
        database.execute(
            "INSERT INTO assessment_verdicts (verdict_id, submission_id, claim_id, raw_score, "
            "rubric_json, assessor_kind, assessor, confidence, response_visibility, "
            "response_excerpt, response_hash, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                verdict_id,
                plan.submission_id,
                plan.claim_id,
                plan.score,
                plan.rubric_json,
                plan.request.assessor_kind,
                plan.request.assessor,
                plan.request.confidence,
                plan.response_visibility,
                plan.response_excerpt,
                plan.response_hash,
                now,
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
        database.execute(
            "UPDATE assessment_submissions SET status = 'judged', updated_at = ? "
            "WHERE submission_id = ? AND status = 'pending'",
            [now, plan.submission_id],
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
                settle(transaction, plan)
            if plan.refusal is not None:
                raise plan.refusal
            raise LinguaWikiError(
                plan.code,
                plan.reason,
                details=(ErrorDetail(field="submission", reason=plan.submission_id),),
            )
        if plan.action == REPEAT:
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
                            {} if written.verdict_id is None else {"verdict_id": written.verdict_id}
                        ),
                    ),
                    idempotency_key=idempotency_key,
                )
        return _verdict_report(database, run_id, written.verdict_id, plan.warnings)


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
    """Pause or resume a run so a calibration can span several sittings."""

    if status not in ("in-progress", "paused", "abandoned"):
        raise LinguaWikiError(
            "invalid_arguments", "status must be in-progress, paused, or abandoned"
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
        current = _run_row(database, run_id)
        _assert_transition(run_id, current=str(current[4]), target=status)
        with database.transaction() as transaction:
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
                affected_records_json=json.dumps([run_id]),
                before_summary=str(current[4]),
                after_summary=status,
            )
        return run_report(database, run_id)


def finalize(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    reason: str = "completed",
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.finalize",
    actor: str = DEFAULT_ACTOR,
) -> AssessmentRunReport:
    """Close a run, writing one uncertainty-aware estimate per tested dimension."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = resolve_run(database, run, track_id=track_id)
        row = _run_row(database, run_id)
        # Before the already-finalized early return, not after it. `finalize` writes the
        # caller's key straight into the same unique index as `record`, so it had the same
        # unpreflighted `ConstraintException` -- and placing the check below the early return
        # meant a key that closed this run for one reason answered 200 when it was reused for
        # another, which is the exact shape of "a guard placed after the path it guards is not
        # a guard". An exact retry still replays; what differs now conflicts.
        closing_fingerprint = idempotency.request_hash(
            operation=FINALIZED_EVENT, run_id=run_id, reason=reason
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
            return run_report(database, run_id)
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
            return run_report(database, run_id)
        _assert_transition(run_id, current=str(row[4]), target="finalized")
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
            for state in states:
                closed = state if state.status != "open" else close_dimension(state, reason=reason)
                _write_state(transaction, run_id=run_id, state=closed, levels=levels, insert=False)
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
                affected_records_json=json.dumps([run_id]),
                after_summary=f"finalized with reason {reason}",
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
        return run_report(database, run_id)


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

    row = _run_row(database, run_id)
    conditions = json.loads(str(row[6]))
    kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
    record_track = learner_service.track_context(database, str(row[1]))
    levels = _pinned_levels(conditions, fallback=record_track.framework_levels)
    states = _dimension_states(database, run_id, kinds)
    reports: list[DimensionReport] = []
    for state in states:
        level, low, high = estimated_level(state, levels)
        minimum, maximum = budget_for(state.dimension_kind)
        reports.append(
            DimensionReport(
                dimension=state.dimension,
                dimension_kind=state.dimension_kind,
                status=state.status,
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
    )


__all__ = [
    "AssessmentRunReport",
    "DimensionReport",
    "NextTaskReport",
    "PlayReport",
    "RunListReport",
    "VerdictPlan",
    "VerdictRequest",
    "VerdictWrite",
    "finalize",
    "held_verdict_request",
    "next_task",
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
    "write_verdict",
]
