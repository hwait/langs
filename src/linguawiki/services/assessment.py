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
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field

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
from linguawiki.packs.format import (
    LoadedPack,
    ResolvedAsset,
    load_pack,
    resolve_pack_path,
)
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import (
    ALGORITHM_VERSION,
    DEFAULT_SCORING,
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
    posterior_mean,
    posterior_sd,
    record_score,
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
#: establish a play count -- including zero, which is the claim that the learner answered
#: without listening -- so `record` stores a count for their results and `NULL` for every
#: other surface's, which is the truth about a CLI or skill that never saw the plays.
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
                reason = servable_under(servable, scoring=scoring, recorded=recorded)[1]
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
        candidates = tuple(
            candidate
            for candidate in _candidates(database, pack_row["pack_id"])
            if servable_candidate(candidate, scoring=scoring, recorded=recorded)
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
                on_disk = _pack_on_disk(database, record.pack_key)
                _assert_recording_matches_the_installed_task(
                    on_disk,
                    content_id=selection.candidate.content_id,
                    installed_hash=task[5],
                )
                played = _serve_asset_identity(() if on_disk is None else on_disk.assets, shown)
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
        "SELECT raw_score, response_hash, response_visibility FROM assessment_results "
        "WHERE run_id = ? AND content_id = ?",
        [run_id, content_id],
    )
    if recorded is None:
        return
    conflicts: list[ErrorDetail] = []
    if abs(float(recorded[0]) - score) > 1e-9:
        conflicts.append(
            ErrorDetail(
                field="score",
                reason="a different score is already recorded for this task",
                context={"recorded": str(float(recorded[0])), "offered": str(score)},
            )
        )
    if recorded[1] is not None and response_hash is not None and str(recorded[1]) != response_hash:
        conflicts.append(
            ErrorDetail(
                field="response",
                reason="a different answer is already recorded for this task",
                context={"recorded": str(recorded[1]), "offered": response_hash},
            )
        )
    if recorded[2] is not None and str(recorded[2]) != visibility:
        conflicts.append(
            ErrorDetail(
                field="response_visibility",
                reason="the recorded result keeps a different amount of the answer, and a "
                "retry cannot change what was kept",
                context={"recorded": str(recorded[2]), "offered": visibility},
            )
        )
    if conflicts:
        raise LinguaWikiError(
            "assessment_result_conflict",
            f"{content_id} was already answered in this run with a different result; a "
            "retry repeats an observation, it does not replace one",
            details=tuple(conflicts),
        )


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
    actor: str = DEFAULT_ACTOR,
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
        run_id = resolve_run(database, run, track_id=track_id)
        row = _run_row(database, run_id)
        # The key is checked here -- before the transaction, and before every guard below,
        # including the one that asks whether the run may take further results. A retry is a
        # retry whatever the run has become since: a client whose response was lost and which
        # then paused, or whose pause and retry crossed, was told its completed work was new
        # work it was not allowed to do. Resolving the replay first answers with what already
        # happened, which is true in any run state.
        #
        # It also means the unique index on
        # `domain_events.idempotency_key` is never the thing that refuses: it did, with a
        # raw `ConstraintException` surfaced as `internal_error`, after every refusal below
        # had been placed before the transaction precisely to avoid that.
        #
        # Every argument a refusal below could turn on is in the fingerprint -- the score,
        # the visibility, the assessor, the rubric -- which is what makes returning early
        # on a replay safe: a call that asked for something different conflicts rather than
        # being handed this one's result. The response text itself is never hashed into the
        # payload under its own name; `payload_json` is never edited, so a payload written
        # before the retention rule ran would keep what it kept for the life of the
        # workspace. Its digest answers the same question and carries nothing.
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
        )
        if (
            idempotency.resolve(
                database,
                key=idempotency_key,
                event_type=RECORDED_EVENT,
                request_hash=scoring_fingerprint,
            )
            is not None
        ):
            return run_report(database, run_id)
        _assert_running(run_id, status=str(row[4]), action="take further results")
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
        visibility, excerpt, digest = evidence_service.retain_response(
            response if response is not None else response_excerpt,
            requested=response_visibility,
            preferences=record_track.preferences,
        )
        # `response_hash` promises the hash of the *response*, so that a withheld answer
        # can be checked against one offered later. A caller-supplied excerpt is the
        # caller's own truncation of an answer this command never saw: hashing it under
        # that name would answer "no" for the learner's real answer, which is the opposite
        # of what the column exists to do. No whole answer, no attestation.
        response_hash = digest if response is not None else None
        if str(served[2]) == "answered":
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
            )
            return run_report(database, run_id)
        states = {state.dimension: state for state in _dimension_states(database, run_id, kinds)}
        state = states[dimension]
        levels = _pinned_levels(conditions, fallback=record_track.framework_levels)
        prior_snapshot = list(state.posterior)
        updated = record_score(state, candidate, score=resolved_score, level_count=len(levels))
        result_id = AssessmentId.new()
        # Derived from the play rows, never accepted: a caller-supplied count would be one
        # more place a caller could talk its way into a different claim. Only for a task
        # that played a recording, and only from a surface that records plays.
        play_count = (
            _plays_used(database, run_id, content_id)
            if served[13] is not None and actor in PLAY_TRACKING_ACTORS
            else None
        )
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO assessment_results (result_id, run_id, content_id, dimension, "
                "raw_score, rubric_json, response_excerpt, assessor_kind, assessor, confidence, "
                "prior_json, posterior_json, difficulty, recorded_at, scoring_policy_version, "
                "score_source, response_visibility, response_hash, play_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    play_count,
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
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                actor=actor,
                affected_records_json=json.dumps([run_id, str(result_id)], sort_keys=True),
                after_summary=(
                    f"scored {content_id} in {dimension} at {resolved_score} ({score_source})"
                ),
            )
            if idempotency_key is not None:
                migration_module.record_domain_event(
                    transaction,
                    event_type="assessment.recorded",
                    aggregate_type="assessment_run",
                    aggregate_id=run_id,
                    correlation_id=EventId.new(),
                    payload_json=idempotency.payload(
                        scoring_fingerprint,
                        content_id=content_id,
                        score=resolved_score,
                    ),
                    idempotency_key=idempotency_key,
                )
        return _reported(run_report(database, run_id), warnings)


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


def _pack_on_disk(database: Database, pack_key: str | None) -> LoadedPack | None:
    """The pack directory this run's track is taught from, as it is right now.

    Scoped to that pack rather than to "the installed pack": a workspace may hold two,
    and `installed_pack` with no key refuses with `pack_selection_required` -- a refusal
    about pack selection surfacing out of a read of one learner's history. A track names
    the pack it is taught from, and a reference is resolved inside that pack's content.

    A pack that will not load reports *itself*. Collapsing a checksum mismatch or an
    unreadable file into "the recording is not available" sends an operator to look for
    a missing file that is sitting right there.
    """

    source = str(pack_service.installed_pack(database, pack_key)["source_path"] or "")
    if not source:
        return None
    # Loading the whole pack to read one digest is wasteful and deliberate for now; C5
    # replaces it with an installed-asset table when audio actually arrives.
    return load_pack(resolve_pack_path(source))


def _assert_recording_matches_the_installed_task(
    pack: LoadedPack | None, *, content_id: str, installed_hash: object
) -> None:
    """The recording and the answer key have to come from one revision of the task.

    The bank holds what was *installed*; the bytes are read from the pack directory,
    which an author can re-record and republish without reinstalling. Serving then
    paired the new recording with the installed key and snapshotted the installed hash
    -- a question the learner was asked that no revision of the pack ever contained,
    and one the run's own record could not describe afterwards.

    A task's hash covers the recording it plays, so comparing it is the whole check.
    """

    if pack is None or installed_hash is None:
        return
    current = next(
        (item for item in pack.tasks if str(item.content_id) == content_id),
        None,
    )
    if current is None or current.content_hash == str(installed_hash):
        return
    raise LinguaWikiError(
        "assessment_task_revision_drifted",
        f"the pack directory holds a different revision of {content_id} than the one "
        "installed, so its recording does not belong to the answer key that would be "
        "served with it; install the pack version you mean to serve",
        details=(ErrorDetail(field="content_hash", reason=f"installed {installed_hash}"),),
    )


def _pack_assets(database: Database, pack_key: str | None) -> tuple[ResolvedAsset, ...]:
    """Every recording the pack this run is taught from ships.

    Scoped to that pack rather than to "the installed pack": a workspace may hold two,
    and `installed_pack` with no key refuses with `pack_selection_required` -- a refusal
    about pack selection surfacing out of a read of one learner's history. A track names
    the pack it is taught from, and a reference is resolved inside that pack's content.

    A pack that will not load reports *itself*. Collapsing a checksum mismatch or an
    unreadable file into "the recording is not available" sends an operator to look for
    a missing file that is sitting right there.
    """

    pack = _pack_on_disk(database, pack_key)
    return () if pack is None else pack.assets


def _run_pack_key(database: Database, run_id: str) -> str | None:
    """The pack the run's track is taught from. `None` leaves pack selection as it was."""

    row = _run_row(database, run_id)
    return learner_service.track_context(database, str(row[1])).pack_key


def _plays_audio(shown: TaskPresentation | None) -> bool:
    return shown is not None and shown.audio is not None


def _serve_asset_identity(
    assets: Sequence[ResolvedAsset], shown: TaskPresentation | None
) -> ServedAsset | None:
    """The recording this serving actually played, recorded when it is played.

    There is no second chance at this. The identity can only be established while the
    task is being served, so a serve that writes the presentation and not the identity
    manufactures exactly the half-state `served_presentation_complete` exists to report,
    with no way forward for anybody who finds it afterwards.
    """

    if shown is None or shown.audio is None:
        return None
    for asset in assets:
        if asset.asset_key == shown.audio.asset_key:
            return ServedAsset(content_id=str(asset.content_id), sha256=asset.sha256)
    raise LinguaWikiError(
        "assessment_asset_unavailable",
        f"this task plays {shown.audio.asset_key}, which the installed pack does not hold",
        details=(ErrorDetail(field="asset", reason="no installed pack holds it"),),
    )


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

    resolved = next(
        (
            candidate
            for candidate in _pack_assets(database, pack_key)
            if str(candidate.content_id) == identity.content_id
        ),
        None,
    )
    if resolved is None:
        raise LinguaWikiError(
            "assessment_asset_unavailable",
            f"the recording {identity.content_id} this task played is not available",
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
        scoring=_run_scoring(conditions),
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
    "PlayReport",
    "finalize",
    "next_task",
    "plays_remaining",
    "record",
    "record_play",
    "report",
    "seed_declared_estimates",
    "set_status",
    "start",
]
