"""Resumable onboarding: from a declared level to a bounded, labelled starting point.

Onboarding is deliberately split into four commands, because the expensive parts must
survive a conversation ending: `start` records the mode and the declared level, `record`
collects self-report answers, `status` says what is still missing, and `finalize` does the
work — resolve the band and its prerequisites, prepare bounded resources, build a
calibration queue, and open the calibration run.

What onboarding must never do is the reason it exists in this shape:

- a declared level becomes `declared-hypothesis` estimates with no evidence, never
  mastery, and every imported reference item stays `unseen`;
- a `pilot` pack yields a labelled pilot calibration and a pilot curriculum; a
  comprehensive placement claim is refused by the assessment service, not smoothed over
  here;
- dimensions the pack or the learner's equipment cannot serve are reported as unsupported
  rather than quietly dropped.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.contracts import PackManifest
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import AssessmentId, EventId, ReviewId
from linguawiki.models import ContractModel
from linguawiki.packs import coverage as coverage_module
from linguawiki.packs.format import KNOWLEDGE_KIND
from linguawiki.paths import WorkspacePaths
from linguawiki.provenance import PROMOTED_LIFECYCLES
from linguawiki.services import assessment as assessment_service
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service
from linguawiki.services import resources as resource_service

#: Self-report answers onboarding understands. Anything else is refused rather than
#: stored as an untyped note that no later stage knows how to read.
ANSWER_KEYS = (
    "self_reported_level",
    "can_do_summary",
    "study_history",
    "recent_materials",
    "weekly_availability",
    "priority_dimensions",
    "known_gaps",
    "audio_setup",
    "notes",
)
#: How many knowledge targets a calibration queue may hold, per purpose.
CALIBRATION_SAMPLE = 10


class CalibrationQueueEntry(ContractModel):
    queue_item_id: str
    purpose: str
    target_kind: str
    target_ref: str
    stable_key: str
    dimension: str | None = None
    priority: int
    rationale: str
    status: str


class OnboardingReport(ContractModel):
    onboarding_id: str
    track_id: str
    mode: str
    status: str
    declared_level: str | None
    pack_key: str
    pack_version: str
    pack_maturity: str
    calibration_label: str
    framework_id: str
    framework_levels: tuple[str, ...]
    supported_modes: tuple[str, ...]
    next_step: str | None
    answers: dict[str, Any] = Field(default_factory=dict)
    seeded_estimates: tuple[str, ...] = ()
    calibration_queue: tuple[CalibrationQueueEntry, ...] = ()
    resource_plan: resource_service.ResourcePlanReport | None = None
    assessment_run_id: str | None = None
    unsupported_dimensions: tuple[str, ...] = ()
    covered_themes: tuple[str, ...] = ()
    started_at: str | None = None
    finalized_at: str | None = None
    warnings: tuple[str, ...] = ()


def _manifest(pack_row: Mapping[str, str]) -> PackManifest:
    return PackManifest.model_validate(json.loads(pack_row["manifest_json"]))


def _run_row(database: Database, onboarding_id: str) -> Sequence[Any]:
    row = database.one(
        "SELECT onboarding_id, track_id, mode, status, declared_level, pack_id, pack_maturity, "
        "calibration_label, assessment_run_id, resource_plan_id, started_at, finalized_at "
        "FROM onboarding_runs WHERE onboarding_id = ?",
        [onboarding_id],
    )
    if row is None:
        raise LinguaWikiError(
            "onboarding_not_found",
            f"no onboarding run with ID {onboarding_id}",
            details=(ErrorDetail(field="onboarding", reason="unknown onboarding run"),),
        )
    return row


def _resolve_run(database: Database, onboarding: str | None, track: str | None) -> str:
    if onboarding is not None:
        return str(_run_row(database, onboarding)[0])
    parameters: list[object] = []
    clause = "WHERE status <> 'abandoned'"
    if track is not None:
        clause += " AND track_id = ?"
        parameters.append(learner_service.resolve_track(database, track))
    rows = database.query(
        f"SELECT onboarding_id FROM onboarding_runs {clause} ORDER BY started_at DESC",
        parameters,
    )
    if rows:
        return str(rows[0][0])
    raise LinguaWikiError(
        "onboarding_not_started",
        "no onboarding run exists yet; run 'linguawiki onboard start' first",
        details=(ErrorDetail(field="onboarding", reason="no onboarding run"),),
    )


#: Which onboarding statuses each status may become. `finalized` and `abandoned` are both
#: terminal: a learner who walked away from onboarding has not consented to the plan a
#: finalization would write, and a finalized run's plan is already in force.
RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "started": frozenset({"awaiting-input", "calibrating", "finalized", "abandoned"}),
    "awaiting-input": frozenset({"awaiting-input", "calibrating", "finalized", "abandoned"}),
    "calibrating": frozenset({"calibrating", "finalized", "abandoned"}),
    "finalized": frozenset(),
    "abandoned": frozenset(),
}


def _assert_transition(onboarding_id: str, *, current: str, target: str) -> None:
    allowed = RUN_TRANSITIONS.get(current, frozenset())
    if target in allowed:
        return
    raise LinguaWikiError(
        "onboarding_closed" if not allowed else "onboarding_transition_invalid",
        f"onboarding run {onboarding_id} is {current}, so it cannot become {target}"
        + (f"; it may only become {sorted(allowed)}" if allowed else "; start a new run instead"),
        details=(
            ErrorDetail(
                field="status",
                reason="transition is not allowed",
                context={"from": current, "to": target},
            ),
        ),
    )


def start(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    mode: str = "declared-level",
    declared_level: str | None = None,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "onboard.start",
) -> OnboardingReport:
    """Open onboarding and seed provisional, low-confidence declared-level estimates."""

    active_clock = clock or SystemClock()
    if mode not in ("declared-level", "placement"):
        raise LinguaWikiError("invalid_arguments", "mode must be declared-level or placement")
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        pack_row = pack_service.installed_pack(database, record.pack_key)
        manifest = _manifest(pack_row)
        supported = coverage_module.supported_onboarding_modes(pack_row["maturity"])
        if mode not in supported:
            raise LinguaWikiError(
                "onboarding_mode_unsupported",
                f"{pack_row['pack_key']} is {pack_row['maturity']} and supports "
                f"{list(supported) or 'no'} onboarding mode(s), not {mode}",
                details=(
                    ErrorDetail(
                        field="mode",
                        reason="pack maturity does not support this mode",
                        context={"maturity": pack_row["maturity"]},
                    ),
                ),
            )
        level = declared_level or record.declared_level
        if mode == "declared-level" and level is None:
            raise LinguaWikiError(
                "level_required",
                "declared-level onboarding needs a level from the track's framework",
                details=(ErrorDetail(field="declared_level", reason="no declared level"),),
            )
        if level is not None and level not in record.framework_levels:
            raise LinguaWikiError(
                "level_not_in_framework",
                f"{level} is not a level of {record.proficiency_framework}",
                details=(ErrorDetail(field="declared_level", reason="level not in framework"),),
            )
        replayed = (
            database.one(
                "SELECT onboarding_id FROM onboarding_runs WHERE idempotency_key = ?",
                [idempotency_key],
            )
            if idempotency_key is not None
            else None
        )
        if replayed is not None:
            return _status(database, str(replayed[0]), clock=active_clock)
        open_run = database.one(
            "SELECT onboarding_id FROM onboarding_runs WHERE track_id = ? "
            "AND status NOT IN ('finalized', 'abandoned')",
            [track_id],
        )
        if open_run is not None:
            raise LinguaWikiError(
                "onboarding_in_progress",
                f"onboarding run {open_run[0]} is still open for this track; record answers or "
                "finalize it",
                details=(ErrorDetail(field="onboarding", reason="run already open"),),
            )
        label = (
            "comprehensive-placement"
            if mode == "placement" and pack_row["maturity"] == "placement-ready"
            else "pilot-calibration"
        )
        onboarding_id = AssessmentId.new()
        correlation_id = EventId.new()
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO onboarding_runs (onboarding_id, track_id, mode, status, "
                "declared_level, pack_id, pack_maturity, calibration_label, assessment_run_id, "
                "resource_plan_id, idempotency_key, started_at, finalized_at, updated_at) "
                "VALUES (?, ?, ?, 'awaiting-input', ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?)",
                [
                    str(onboarding_id),
                    track_id,
                    mode,
                    level,
                    pack_row["pack_id"],
                    pack_row["maturity"],
                    label,
                    idempotency_key,
                    now,
                    now,
                ],
            )
            if level is not None and level != record.declared_level:
                transaction.execute(
                    "UPDATE learning_tracks SET declared_level = ?, updated_at = ? "
                    "WHERE track_id = ?",
                    [level, now, track_id],
                )
            seeded = assessment_service.seed_declared_estimates(
                transaction,
                track_id=track_id,
                framework_id=record.proficiency_framework,
                levels=record.framework_levels,
                dimension_kinds=manifest.dimension_kinds,
                declared_level=level,
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([str(onboarding_id)]),
                after_summary=f"opened {mode} onboarding at declared level {level}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="onboarding.started",
                aggregate_type="track",
                aggregate_id=track_id,
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {
                        "onboarding_id": str(onboarding_id),
                        "mode": mode,
                        "declared_level": level,
                        "calibration_label": label,
                        "seeded_dimensions": list(seeded),
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"onboarding.started:{onboarding_id}",
            )
        result = _status(database, str(onboarding_id), clock=active_clock)
    return result.model_copy(update={"seeded_estimates": seeded})


def record_answer(
    paths: WorkspacePaths,
    *,
    key: str,
    value: object,
    onboarding: str | None = None,
    track: str | None = None,
    provenance: str = "self-report",
    clock: Clock | None = None,
    command: str = "onboard.record",
) -> OnboardingReport:
    """Store one self-report answer. A self-report is a prior, never evidence."""

    active_clock = clock or SystemClock()
    if key not in ANSWER_KEYS:
        raise LinguaWikiError(
            "unknown_answer_key",
            f"{key} is not an onboarding answer this release understands",
            details=(ErrorDetail(field="key", reason=f"expected one of {list(ANSWER_KEYS)}"),),
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        onboarding_id = _resolve_run(database, onboarding, track)
        row = _run_row(database, onboarding_id)
        _assert_transition(onboarding_id, current=str(row[3]), target="awaiting-input")
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO onboarding_answers (onboarding_id, key, value_json, provenance, "
                "recorded_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (onboarding_id, key) "
                "DO UPDATE SET value_json = excluded.value_json, "
                "provenance = excluded.provenance, recorded_at = excluded.recorded_at",
                [
                    onboarding_id,
                    key,
                    json.dumps(value, ensure_ascii=False, sort_keys=True),
                    provenance,
                    now,
                ],
            )
            transaction.execute(
                "UPDATE onboarding_runs SET status = 'awaiting-input', updated_at = ? "
                "WHERE onboarding_id = ? AND status = 'started'",
                [now, onboarding_id],
            )
        return _status(database, onboarding_id, clock=active_clock)


def _calibration_targets(
    database: Database,
    *,
    pack_id: str,
    ordered_bundles: Sequence[tuple[str, int]],
    declared_level: str | None,
    sample: int,
) -> list[tuple[str, str, str, str, int]]:
    """A bounded sample of prerequisite and declared-band knowledge targets.

    Prerequisites come first and are sampled independently of the declared band, because
    a declared level is exactly the claim that needs checking underneath.
    """

    placeholders = ", ".join("?" for _ in PROMOTED_LIFECYCLES)
    selected: list[tuple[str, str, str, str, int]] = []
    seen: set[str] = set()
    # Prerequisites live at bundle depth above zero; the declared band is depth zero.
    for purpose, prerequisite in (("prerequisite-sample", True), ("declared-band", False)):
        taken = 0
        for bundle_key, depth in ordered_bundles:
            if (depth > 0) is not prerequisite or taken >= sample:
                continue
            rows = database.query(
                "SELECT entry.item_ref, record.stable_key, item.level_min "
                "FROM resource_bundles bundle "
                "JOIN resource_bundle_items entry ON entry.bundle_id = bundle.bundle_id "
                "JOIN content_records record ON record.content_id = entry.item_ref "
                "JOIN knowledge_items item ON item.content_id = entry.item_ref "
                f"WHERE bundle.pack_id = ? AND bundle.bundle_key = ? "
                f"AND entry.item_kind = ? AND record.lifecycle IN ({placeholders}) "
                "AND NOT record.quarantined ORDER BY entry.sequence",
                [pack_id, bundle_key, KNOWLEDGE_KIND, *PROMOTED_LIFECYCLES],
            )
            for content_id, stable_key, level in rows:
                if taken >= sample or str(content_id) in seen:
                    continue
                if purpose == "declared-band" and declared_level and str(level) != declared_level:
                    continue
                seen.add(str(content_id))
                taken += 1
                selected.append(
                    (
                        purpose,
                        str(content_id),
                        str(stable_key),
                        f"sampled from bundle {bundle_key} at prerequisite depth {depth}",
                        depth,
                    )
                )
    return selected


def finalize(
    paths: WorkspacePaths,
    *,
    onboarding: str | None = None,
    track: str | None = None,
    weeks: int = resource_service.DEFAULT_WEEKS,
    item_budget: int = resource_service.DEFAULT_ITEM_BUDGET,
    calibration_sample: int = CALIBRATION_SAMPLE,
    open_calibration: bool = True,
    clock: Clock | None = None,
    command: str = "onboard.finalize",
) -> OnboardingReport:
    """Resolve the band, prepare bounded resources, and build the calibration queue."""

    active_clock = clock or SystemClock()
    with open_reader(paths, clock=active_clock) as reader:
        onboarding_id = _resolve_run(reader, onboarding, track)
        row = _run_row(reader, onboarding_id)
        if str(row[3]) == "finalized":
            return _status(reader, onboarding_id, clock=active_clock)
        _assert_transition(onboarding_id, current=str(row[3]), target="finalized")
        track_id = str(row[1])
        mode = str(row[2])
        declared_level = None if row[4] is None else str(row[4])
    plan = resource_service.prepare(
        paths,
        track=track_id,
        onboarding_mode=mode,
        level_codes=None if declared_level is None else (declared_level,),
        weeks=weeks,
        item_budget=item_budget,
        clock=active_clock,
    )
    run_id: str | None = None
    with open_writer(paths, command=command, clock=active_clock) as database:
        record = learner_service.track_context(database, track_id)
        pack_row = pack_service.installed_pack(database, record.pack_key)
        installed_bundles = {
            str(key): (str(level), tuple(json.loads(str(dependencies))))
            for key, level, dependencies in database.query(
                "SELECT bundle_key, level_code, dependencies_json FROM resource_bundles "
                "WHERE pack_id = ? ORDER BY bundle_key",
                [pack_row["pack_id"]],
            )
        }
        ordered = resource_service.resolve_bundles(
            installed_bundles,
            level_codes=(declared_level,) if declared_level else record.framework_levels,
        )
        targets = _calibration_targets(
            database,
            pack_id=pack_row["pack_id"],
            ordered_bundles=ordered,
            declared_level=declared_level,
            sample=calibration_sample,
        )
        with database.transaction() as transaction:
            now = transaction.now()
            for purpose, content_id, _stable_key, rationale, depth in targets:
                existing = transaction.one(
                    "SELECT queue_item_id FROM calibration_queue_items WHERE track_id = ? "
                    "AND purpose = ? AND target_kind = ? AND target_ref = ?",
                    [track_id, purpose, KNOWLEDGE_KIND, content_id],
                )
                if existing is not None:
                    continue
                transaction.execute(
                    "INSERT INTO calibration_queue_items (queue_item_id, track_id, "
                    "onboarding_id, audit_id, purpose, target_kind, target_ref, dimension, "
                    "priority, rationale, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, 'pending', ?, ?)",
                    [
                        str(ReviewId.new()),
                        track_id,
                        onboarding_id,
                        purpose,
                        KNOWLEDGE_KIND,
                        content_id,
                        depth,
                        rationale,
                        now,
                        now,
                    ],
                )
            transaction.execute(
                "UPDATE onboarding_runs SET status = 'calibrating', updated_at = ? "
                "WHERE onboarding_id = ?",
                [now, onboarding_id],
            )
    if open_calibration:
        run = assessment_service.start(
            paths,
            track=track_id,
            run_type="placement" if mode == "placement" else "pilot-calibration",
            idempotency_key=f"onboarding.calibration:{onboarding_id}",
            clock=active_clock,
        )
        run_id = run.run_id
    with (
        open_writer(paths, command=command, clock=active_clock) as database,
        database.transaction() as transaction,
    ):
        now = transaction.now()
        transaction.execute(
            "UPDATE onboarding_runs SET status = 'finalized', assessment_run_id = ?, "
            "finalized_at = ?, updated_at = ? WHERE onboarding_id = ?",
            [run_id, now, now, onboarding_id],
        )
        migration_module.record_audit_entry(
            transaction,
            command=command,
            correlation_id=EventId.new(),
            outcome="succeeded",
            affected_records_json=json.dumps([onboarding_id]),
            after_summary=(
                f"finalized onboarding: {plan.imported_items} unseen reference item(s), "
                f"{len(targets)} calibration target(s)"
            ),
        )
        migration_module.record_domain_event(
            transaction,
            event_type="onboarding.finalized",
            aggregate_type="track",
            aggregate_id=track_id,
            correlation_id=EventId.new(),
            payload_json=json.dumps(
                {
                    "onboarding_id": onboarding_id,
                    "plan_label": plan.plan_label,
                    "imported_items": plan.imported_items,
                    "calibration_targets": len(targets),
                    "assessment_run_id": run_id,
                },
                sort_keys=True,
            ),
            idempotency_key=f"onboarding.finalized:{onboarding_id}",
        )
    return status(paths, onboarding=onboarding_id, clock=active_clock)


def abandon(
    paths: WorkspacePaths,
    *,
    onboarding: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "onboard.abandon",
) -> OnboardingReport:
    """Abandon an onboarding run, keeping its answers for audit."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        onboarding_id = _resolve_run(database, onboarding, track)
        row = _run_row(database, onboarding_id)
        if str(row[3]) == "abandoned":
            # Reported through the connection this writer already holds: DuckDB serves one
            # connection per file, so calling `status(paths, ...)` here would deadlock.
            return _status(database, onboarding_id, clock=active_clock)
        _assert_transition(onboarding_id, current=str(row[3]), target="abandoned")
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE onboarding_runs SET status = 'abandoned', updated_at = ? "
                "WHERE onboarding_id = ?",
                [transaction.now(), onboarding_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([onboarding_id]),
                after_summary="abandoned onboarding",
            )
    return status(paths, onboarding=onboarding_id, clock=active_clock)


def status(
    paths: WorkspacePaths,
    *,
    onboarding: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> OnboardingReport:
    """The onboarding run's state, and what is still expected of the learner."""

    active_clock = clock or SystemClock()
    with open_reader(paths, clock=active_clock) as database:
        return _status(database, _resolve_run(database, onboarding, track), clock=active_clock)


def _status(database: Database, onboarding_id: str, *, clock: Clock) -> OnboardingReport:
    """Read a run's state through a caller's connection.

    Reports take a `Database` rather than a workspace because DuckDB serves one
    connection per database file: a command that has just written cannot open a second
    connection to report what it did without deadlocking on its own writer lock.
    """

    del clock
    row = _run_row(database, onboarding_id)
    track_id = str(row[1])
    record = learner_service.track_context(database, track_id)
    pack_row = pack_service.installed_pack(database, record.pack_key)
    manifest = _manifest(pack_row)
    answers = {
        str(key): json.loads(str(value))
        for key, value in database.query(
            "SELECT key, value_json FROM onboarding_answers WHERE onboarding_id = ? ORDER BY key",
            [onboarding_id],
        )
    }
    queue = tuple(
        CalibrationQueueEntry(
            queue_item_id=str(entry[0]),
            purpose=str(entry[1]),
            target_kind=str(entry[2]),
            target_ref=str(entry[3]),
            stable_key=str(entry[6]),
            dimension=None if entry[4] is None else str(entry[4]),
            priority=int(entry[5]),
            rationale=str(entry[7]),
            status=str(entry[8]),
        )
        for entry in database.query(
            "SELECT item.queue_item_id, item.purpose, item.target_kind, item.target_ref, "
            "item.dimension, item.priority, record.stable_key, item.rationale, item.status "
            "FROM calibration_queue_items item "
            "JOIN content_records record ON record.content_id = item.target_ref "
            "WHERE item.track_id = ? ORDER BY item.priority DESC, record.stable_key",
            [track_id],
        )
    )
    seeded = tuple(
        str(dimension)
        for (dimension,) in database.query(
            "SELECT dimension FROM skill_estimates WHERE track_id = ? ORDER BY dimension",
            [track_id],
        )
    )
    tested_dimensions = {
        str(dimension)
        for (dimension,) in database.query(
            "SELECT DISTINCT task.dimension FROM assessment_tasks task "
            "JOIN content_records item ON item.content_id = task.content_id "
            "WHERE item.pack_id = ?",
            [pack_row["pack_id"]],
        )
    }
    themes = tuple(
        str(theme)
        for (theme,) in database.query(
            "SELECT DISTINCT tag.tag_value FROM item_tags tag "
            "JOIN content_records item ON item.content_id = tag.content_id "
            "WHERE item.pack_id = ? AND tag.tag_kind = 'theme' ORDER BY 1",
            [pack_row["pack_id"]],
        )
    )
    run_status = str(row[3])
    plan = resource_service.plan_state(database, track_id) if row[9] is not None else None
    warnings: list[str] = []
    unsupported = tuple(sorted(set(manifest.dimensions) - tested_dimensions))
    if unsupported:
        warnings.append(f"the pack cannot test dimension(s) {list(unsupported)}")
    if pack_row["maturity"] == "pilot":
        warnings.append(
            "this is a pilot pack: its calibration and curriculum cover only its declared "
            "themes and must not be read as a level claim"
        )
    next_step = None
    if run_status in ("started", "awaiting-input"):
        next_step = "finalize"
    elif run_status == "calibrating":
        next_step = "assessment.next"
    return OnboardingReport(
        onboarding_id=onboarding_id,
        track_id=track_id,
        mode=str(row[2]),
        status=run_status,
        declared_level=None if row[4] is None else str(row[4]),
        pack_key=pack_row["pack_key"],
        pack_version=pack_row["version"],
        pack_maturity=str(row[6]),
        calibration_label=str(row[7]),
        framework_id=record.proficiency_framework,
        framework_levels=record.framework_levels,
        supported_modes=coverage_module.supported_onboarding_modes(pack_row["maturity"]),
        next_step=next_step,
        answers=answers,
        seeded_estimates=seeded,
        calibration_queue=queue,
        resource_plan=plan,
        assessment_run_id=None if row[8] is None else str(row[8]),
        unsupported_dimensions=unsupported,
        covered_themes=themes,
        started_at=str(aware_utc(row[10]).isoformat()),
        finalized_at=None if row[11] is None else str(aware_utc(row[11]).isoformat()),
        warnings=tuple(warnings),
    )


__all__ = [
    "ANSWER_KEYS",
    "CALIBRATION_SAMPLE",
    "CalibrationQueueEntry",
    "OnboardingReport",
    "abandon",
    "finalize",
    "record_answer",
    "start",
    "status",
]
