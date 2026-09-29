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

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.contracts import PackMaturity, parse_answer_key
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
    REUSE_WINDOW_MONTHS,
    SCORING_POLICY_VERSION,
    Candidate,
    DimensionState,
    assert_machine_scorable,
    budget_for,
    close_dimension,
    confidence_label,
    credible_interval,
    estimated_level,
    initial_state,
    posterior_mean,
    posterior_sd,
    record_score,
    score_response,
    select_task,
    unavailable_reason,
)
from linguawiki.provenance import PROMOTED_LIFECYCLES
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service

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
    rubric: dict[str, object] = Field(default_factory=dict)
    rubric_version: int = 1
    permitted_help: str = "none"
    selection_reason: str = "informativeness"
    remaining_open_dimensions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


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
    tasks_served: int = 0
    tasks_recorded: int = 0
    stop_reason: str | None = None
    started_at: str | None = None
    finalized_at: str | None = None
    untested_dimensions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


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


def _resolve_run(database: Database, run: str | None, *, track_id: str | None = None) -> str:
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
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.start",
) -> AssessmentRunReport:
    """Open a bounded calibration or placement run and persist its starting state."""

    active_clock = clock or SystemClock()
    if run_type not in ("pilot-calibration", "placement"):
        raise LinguaWikiError(
            "invalid_arguments", "run_type must be pilot-calibration or placement"
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        pack_row = pack_service.installed_pack(database, record.pack_key)
        label = _assert_placement_bank(pack_row, run_type=run_type)
        if idempotency_key is not None:
            existing = database.one(
                "SELECT run_id FROM assessment_runs WHERE idempotency_key = ?",
                [idempotency_key],
            )
            if existing is not None:
                return _run_report(database, str(existing[0]))
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
                affected_records_json=json.dumps([str(run_id)]),
                after_summary=f"started {label} for {track_id}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="assessment.started",
                aggregate_type="assessment_run",
                aggregate_id=str(run_id),
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {"run_type": run_type, "calibration_label": label}, sort_keys=True
                ),
                idempotency_key=f"assessment.started:{run_id}",
            )
    result = report(paths, run=str(run_id), clock=active_clock)
    return result.model_copy(update={"warnings": (*result.warnings, *warnings)})


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


def next_task(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.next",
) -> NextTaskReport | AssessmentRunReport:
    """Serve the next task, or report the run when no dimension is still open."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = _resolve_run(database, run, track_id=track_id)
        row = _run_row(database, run_id)
        _assert_running(run_id, status=str(row[4]))
        conditions = json.loads(str(row[6]))
        kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
        record = learner_service.track_context(database, str(row[1]))
        pack_row = pack_service.installed_pack(database, record.pack_key)
        _assert_same_bank(run_id, conditions=conditions, pack_row=pack_row)
        states = _dimension_states(database, run_id, kinds)
        open_states = [state for state in states if state.status == "open"]
        if not open_states:
            return _run_report(database, run_id)
        candidates = _candidates(database, pack_row["pack_id"])
        excluded = _excluded_task_ids(
            database, track_id=str(row[1]), run_id=run_id, clock=active_clock
        )
        available = tuple(str(value) for value in conditions["available_modalities"])
        # Serve the least-progressed open dimension first, so a run that stops early has
        # spread its evidence rather than finishing one dimension and testing no other.
        open_states.sort(key=lambda state: (state.tasks_used, state.dimension))
        warnings: list[str] = []
        for state in open_states:
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
                "task.expected_json "
                "FROM assessment_tasks task "
                "JOIN content_records record ON record.content_id = task.content_id "
                "WHERE task.content_id = ?",
                [selection.candidate.content_id],
            )
            assert task is not None
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
                    "expected_json, prompt_snapshot, rubric_json) "
                    "VALUES (?, ?, ?, ?, 'served', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                rubric=json.loads(str(task[2])),
                rubric_version=int(task[3]),
                permitted_help=str(task[4]),
                selection_reason=selection.reason,
                remaining_open_dimensions=tuple(
                    other.dimension for other in open_states if other.status == "open"
                ),
                warnings=tuple(warnings),
            )
    result = report(paths, run=run_id, clock=active_clock)
    return result.model_copy(update={"warnings": (*result.warnings, *warnings)})


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
    present = [column for column in (expected_json, prompt_snapshot, rubric_json) if column]
    if len(present) == 3:
        return _ScorableKey(parse_answer_key(str(expected_json)).answers, "run-snapshot")
    if present:
        raise LinguaWikiError(
            "assessment_snapshot_partial",
            f"the run holds a partial record of serving {content_id}: it can establish "
            "neither what the learner was asked nor that it predates the snapshot, so it "
            "cannot be scored without a judge",
            details=(
                ErrorDetail(
                    field="content_id",
                    reason="partial served snapshot",
                    context={"present": str(len(present))},
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
    return _ScorableKey(parse_answer_key(None if bank is None else str(bank)).answers, "bank")


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
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.record",
) -> AssessmentRunReport:
    """Score one served task and fold it into its dimension's posterior.

    The score is either *computed* here from the key the run snapshotted, or *supplied* by
    a caller who reached its own verdict; `score_source` records which, because
    `assessor_kind` has only ever *labelled* a score and a compatibility call must not
    read afterwards as the scoring policy having run.

    The learner's `response` is an input, not an artifact. Scoring uses it in memory, and
    it reaches the database only through `retain_response`, which is where consent is
    honoured -- so automatic scoring never requires keeping text a track has declined.
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
        run_id = _resolve_run(database, run, track_id=track_id)
        row = _run_row(database, run_id)
        _assert_running(run_id, status=str(row[4]), action="take further results")
        served = database.one(
            "SELECT sequence, dimension, status, task_type, level_code, difficulty, "
            "content_family, modality, is_anchor, content_hash, expected_json, "
            "prompt_snapshot, rubric_json FROM assessment_run_tasks "
            "WHERE run_id = ? AND content_id = ?",
            [run_id, content_id],
        )
        if served is None:
            raise LinguaWikiError(
                "assessment_task_not_served",
                "that task was not served in this run; ask for the next task first",
                details=(ErrorDetail(field="content_id", reason="task was not served"),),
            )
        if str(served[2]) == "answered":
            # A repeat is an idempotent no-op: a retried record must not fold the same
            # evidence into the posterior twice.
            return _run_report(database, run_id)
        conditions = json.loads(str(row[6]))
        kinds = {str(k): str(v) for k, v in conditions["dimension_kinds"].items()}
        record_track = learner_service.track_context(database, str(row[1]))
        # Scored from what was served, never from what the pack now says. Re-reading the
        # installed pack folded a difficulty the learner never faced into their posterior
        # whenever the pack changed mid-run.
        candidate = _served_candidate(run_id, content_id=content_id, served=served)
        dimension = str(served[1])
        # Every refusal below runs before the transaction opens, so a refused call leaves
        # the task still `served` and answerable rather than half-recorded.
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
        # The response goes through retention either way. Scoring in the background and
        # discarding the result would make the stored score unexplainable, and a
        # caller-supplied excerpt reaches the column by the same route so that the consent
        # rule covers both and not only the one this stage added.
        visibility, excerpt, response_hash = evidence_service.retain_response(
            response if response is not None else response_excerpt,
            requested=response_visibility,
            preferences=record_track.preferences,
        )
        states = {state.dimension: state for state in _dimension_states(database, run_id, kinds)}
        state = states[dimension]
        levels = _pinned_levels(conditions, fallback=record_track.framework_levels)
        prior_snapshot = list(state.posterior)
        updated = record_score(state, candidate, score=resolved_score, level_count=len(levels))
        result_id = AssessmentId.new()
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO assessment_results (result_id, run_id, content_id, dimension, "
                "raw_score, rubric_json, response_excerpt, assessor_kind, assessor, confidence, "
                "prior_json, posterior_json, difficulty, recorded_at, scoring_policy_version, "
                "score_source, response_visibility, response_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(result_id),
                    run_id,
                    content_id,
                    dimension,
                    resolved_score,
                    json.dumps(dict(rubric or {}), ensure_ascii=False, sort_keys=True),
                    excerpt,
                    assessor_kind,
                    assessor,
                    confidence,
                    json.dumps(prior_snapshot),
                    json.dumps(list(updated.posterior)),
                    candidate.difficulty,
                    now,
                    policy_version,
                    score_source,
                    visibility,
                    response_hash,
                ],
            )
            transaction.execute(
                "UPDATE assessment_run_tasks SET status = 'answered' WHERE run_id = ? "
                "AND sequence = ?",
                [run_id, int(served[0])],
            )
            # The exposure row already exists: `next_task` wrote it when it served this
            # item. Scoring adds the answer, and leaves the exposure count alone.
            _record_exposure(
                transaction,
                track_id=str(row[1]),
                content_id=content_id,
                purpose=str(row[3]),
                is_anchor=candidate.is_anchor,
                now=now,
                answered=True,
            )
            _write_state(transaction, run_id=run_id, state=updated, levels=levels, insert=False)
            transaction.execute(
                "UPDATE assessment_runs SET updated_at = ? WHERE run_id = ?", [now, run_id]
            )
            if idempotency_key is not None:
                migration_module.record_domain_event(
                    transaction,
                    event_type="assessment.recorded",
                    aggregate_type="assessment_run",
                    aggregate_id=run_id,
                    correlation_id=EventId.new(),
                    payload_json=json.dumps(
                        {"content_id": content_id, "score": resolved_score}, sort_keys=True
                    ),
                    idempotency_key=idempotency_key,
                )
    result = report(paths, run=run_id, clock=active_clock)
    return result.model_copy(update={"warnings": (*result.warnings, *warnings)})


def set_status(
    paths: WorkspacePaths,
    *,
    status: str,
    run: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.pause",
) -> AssessmentRunReport:
    """Pause or resume a run so a calibration can span several sittings."""

    if status not in ("in-progress", "paused", "abandoned"):
        raise LinguaWikiError(
            "invalid_arguments", "status must be in-progress, paused, or abandoned"
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = _resolve_run(database, run, track_id=track_id)
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
                affected_records_json=json.dumps([run_id]),
                before_summary=str(current[4]),
                after_summary=status,
            )
    return report(paths, run=run_id, clock=active_clock)


def finalize(
    paths: WorkspacePaths,
    *,
    run: str | None = None,
    track: str | None = None,
    reason: str = "completed",
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "assessment.finalize",
) -> AssessmentRunReport:
    """Close a run, writing one uncertainty-aware estimate per tested dimension."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        run_id = _resolve_run(database, run, track_id=track_id)
        row = _run_row(database, run_id)
        if str(row[4]) == "finalized":
            return _run_report(database, run_id)
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
                affected_records_json=json.dumps([run_id]),
                after_summary=f"finalized with reason {reason}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="assessment.finalized",
                aggregate_type="assessment_run",
                aggregate_id=run_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps({"reason": reason}, sort_keys=True),
                idempotency_key=idempotency_key or f"assessment.finalized:{run_id}",
            )
    return report(paths, run=run_id, clock=active_clock)


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

    declared_index = float(levels.index(declared_level)) if declared_level in levels else None
    seeded: list[str] = []
    for dimension, kind in sorted(dimension_kinds.items()):
        state = initial_state(
            dimension=dimension,
            dimension_kind=kind,
            level_count=len(levels),
            declared_index=declared_index,
        )
        low, high = credible_interval(state.grid, state.prior)
        estimate_service.upsert_from_state(
            database,
            track_id=track_id,
            framework_id=framework_id,
            state=state,
            level=declared_level,
            low=levels[max(0, min(len(levels) - 1, int(low)))],
            high=levels[max(0, min(len(levels) - 1, int(high) + (1 if high % 1 else 0)))],
            run_id=None,
            basis="declared-hypothesis",
            reason=(
                f"seeded from the learner's declared level {declared_level}"
                if declared_level
                else "seeded with no declared level, so the prior is broad"
            ),
        )
        seeded.append(dimension)
    return tuple(seeded)


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
        return _run_report(database, _resolve_run(database, run, track_id=track_id))


def _run_report(database: Database, run_id: str) -> AssessmentRunReport:
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
    recorded = int(
        database.scalar("SELECT count(*) FROM assessment_results WHERE run_id = ?", [run_id])
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
        tasks_served=served,
        tasks_recorded=recorded,
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
    "finalize",
    "next_task",
    "record",
    "report",
    "seed_declared_estimates",
    "set_status",
    "start",
]
