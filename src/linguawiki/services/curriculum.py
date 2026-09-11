"""Generic prior-course import and audit: "I finished N units -- what transferred?".

The workflow is language- and course-agnostic on purpose: it applies to a textbook, a
class syllabus, or a unit list the learner wrote themselves. Three rules shape it.

- **Only outlines.** The database stores unit structure, objectives, mappings, and short
  references. Copied course text is refused by the rights field, not by convention.
- **A claim is not evidence.** Declaring a unit complete moves its mapped items to at most
  `encountered`, with `self-report` provenance. Nothing here creates recognition or
  production evidence, and nothing marks mastery.
- **Mappings are declared, never guessed.** An objective maps to a pack item because the
  input said so; anything unmapped stays an explicit gap that the audit reports.

The audit then samples what is most likely to have decayed -- central prerequisites,
recent units, production targets, and a thinner slice of older material -- and turns
misses into an evidence-gap queue rather than into a grade.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, model_validator

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import AssessmentId, ContentId, EventId, ReviewId
from linguawiki.models import ContractModel
from linguawiki.packs.format import DESCRIPTOR_KIND, KNOWLEDGE_KIND, content_id_for
from linguawiki.paths import WorkspacePaths
from linguawiki.provenance import PROMOTED_LIFECYCLES
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service

#: Default audit sample. Small on purpose: an audit is a probe, not a re-examination.
DEFAULT_AUDIT_SAMPLE = 12
#: Why the sample chose a target, and how heavily it weighs.
RISK_WEIGHTS: Mapping[str, float] = {
    "central-prerequisite": 3.0,
    "recent-unit": 2.5,
    "production-target": 2.0,
    "older-material": 1.0,
}
#: Knowledge kinds a learner is expected to *produce*, not merely recognise.
PRODUCTION_KINDS = frozenset({"lexeme", "form", "construction", "grammar"})
#: The strongest state a self-report may create.
SELF_REPORT_STAGE = "encountered"


class CurriculumObjectiveInput(ContractModel):
    objective: str = Field(min_length=1)
    dimension: str | None = None
    mapped_kind: Literal["knowledge", "descriptor"] | None = None
    mapped_key: str | None = None
    map_confidence: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def mapping_is_complete_or_absent(self) -> CurriculumObjectiveInput:
        if (self.mapped_kind is None) != (self.mapped_key is None):
            raise ValueError("a mapping needs both mapped_kind and mapped_key")
        if self.mapped_kind is not None and self.map_confidence is None:
            raise ValueError("a declared mapping must state its confidence")
        return self


class CurriculumUnitInput(ContractModel):
    code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    level: str | None = None
    parent_code: str | None = None
    objectives: tuple[CurriculumObjectiveInput, ...] = ()


class CurriculumInput(ContractModel):
    """`curriculum import --input`: an outline the learner is legally free to store."""

    schema_name: Literal["lingua.curriculum.v1"] = "lingua.curriculum.v1"
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1)
    kind: Literal["course", "book", "exam", "syllabus", "user-authored"]
    version: str = Field(min_length=1)
    source_reference: str | None = None
    rights_status: Literal["user-authored", "personal-use-only", "cleared"]
    provenance: str = Field(min_length=1)
    units: tuple[CurriculumUnitInput, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def units_are_unique_and_parents_resolve(self) -> CurriculumInput:
        codes = [unit.code for unit in self.units]
        if len(codes) != len(set(codes)):
            raise ValueError("unit codes must be unique inside a curriculum")
        known = set(codes)
        for unit in self.units:
            if unit.parent_code is not None and unit.parent_code not in known:
                raise ValueError(f"{unit.code} names an unknown parent {unit.parent_code}")
            if unit.parent_code == unit.code:
                raise ValueError(f"{unit.code} cannot be its own parent")
        return self


class ObjectiveReport(ContractModel):
    unit_code: str
    sequence: int
    objective: str
    dimension: str | None = None
    mapped_kind: str | None = None
    mapped_ref: str | None = None
    mapped_key: str | None = None
    map_confidence: str | None = None


class UnitReport(ContractModel):
    unit_id: str
    code: str
    title: str
    level: str | None
    parent_code: str | None
    sequence: int
    state: str
    provenance: str
    objectives: int
    mapped_objectives: int


class CurriculumReport(ContractModel):
    curriculum_id: str
    track_id: str
    title: str
    kind: str
    version: str
    rights_status: str
    provenance: str
    source_reference: str | None
    units: tuple[UnitReport, ...] = ()
    unmapped_objectives: tuple[ObjectiveReport, ...] = ()
    mapped_objectives: int = 0
    total_objectives: int = 0
    encountered_items: int = 0
    warnings: tuple[str, ...] = ()


class AuditItemReport(ContractModel):
    sequence: int
    unit_code: str
    target_kind: str
    target_ref: str
    stable_key: str
    risk_reason: str
    risk_weight: float
    status: str
    outcome: str | None = None
    score: float | None = None


class AuditReport(ContractModel):
    audit_id: str
    track_id: str
    curriculum_id: str
    status: str
    sample_size: int
    recorded: int
    stop_reason: str | None = None
    items: tuple[AuditItemReport, ...] = ()
    confirmed_gaps: tuple[str, ...] = ()
    queued_calibration: tuple[str, ...] = ()
    untested_targets: tuple[str, ...] = ()
    started_at: str | None = None
    finalized_at: str | None = None
    warnings: tuple[str, ...] = ()


def _curriculum_row(database: Database, curriculum_id: str) -> Sequence[Any]:
    row = database.one(
        "SELECT curriculum_id, track_id, title, kind, version, source_reference, "
        "rights_status, provenance FROM curricula WHERE curriculum_id = ?",
        [curriculum_id],
    )
    if row is None:
        raise LinguaWikiError(
            "curriculum_not_found",
            f"no curriculum with ID {curriculum_id}",
            details=(ErrorDetail(field="curriculum", reason="unknown curriculum"),),
        )
    return row


def _resolve_curriculum(database: Database, curriculum: str | None, track_id: str | None) -> str:
    if curriculum is not None:
        return str(_curriculum_row(database, curriculum)[0])
    parameters: list[object] = []
    clause = ""
    if track_id is not None:
        clause = "WHERE track_id = ?"
        parameters.append(track_id)
    rows = database.query(
        f"SELECT curriculum_id FROM curricula {clause} ORDER BY created_at DESC", parameters
    )
    if len(rows) == 1:
        return str(rows[0][0])
    raise LinguaWikiError(
        "curriculum_selection_required",
        f"name a curriculum explicitly: {len(rows)} are imported",
        details=(ErrorDetail(field="curriculum", reason="ambiguous curriculum selection"),),
    )


def _resolve_mapping(
    database: Database,
    *,
    pack_key: str,
    pack_id: str,
    mapped_kind: str,
    mapped_key: str,
) -> str:
    """Resolve a declared mapping to an installed pack item, or refuse it."""

    content_kind = KNOWLEDGE_KIND if mapped_kind == "knowledge" else DESCRIPTOR_KIND
    content_id = str(content_id_for(pack_key, content_kind, mapped_key))
    found = database.one(
        "SELECT lifecycle FROM content_records WHERE content_id = ? AND pack_id = ?",
        [content_id, pack_id],
    )
    if found is None:
        raise LinguaWikiError(
            "curriculum_mapping_unresolved",
            f"{mapped_kind} {mapped_key} is not an item of the installed pack {pack_key}",
            details=(
                ErrorDetail(
                    field="mapped_key",
                    reason="mapping target is not installed",
                    context={"kind": mapped_kind, "key": mapped_key},
                ),
            ),
        )
    return content_id


def import_curriculum(
    paths: WorkspacePaths,
    payload: Mapping[str, Any],
    *,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "curriculum.import",
) -> CurriculumReport:
    """Store an outline and its declared mappings; leave every gap explicit."""

    active_clock = clock or SystemClock()
    document = CurriculumInput.model_validate(payload)
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        pack_row = pack_service.installed_pack(database, record.pack_key)
        existing = database.one(
            "SELECT curriculum_id FROM curricula WHERE track_id = ? AND title = ? AND version = ?",
            [track_id, document.title, document.version],
        )
        if existing is not None:
            raise LinguaWikiError(
                "curriculum_exists",
                f"{document.title} {document.version} is already imported as {existing[0]}",
                details=(ErrorDetail(field="curriculum", reason="duplicate curriculum"),),
            )
        levels = set(record.framework_levels)
        unknown_levels = sorted({unit.level for unit in document.units if unit.level} - levels)
        if unknown_levels:
            raise LinguaWikiError(
                "level_not_in_framework",
                f"the outline uses level(s) {unknown_levels} that are not in "
                f"{record.proficiency_framework}",
                details=(ErrorDetail(field="level", reason=", ".join(unknown_levels)),),
            )
        curriculum_id = ContentId.derive(track_id, "curriculum", document.title, document.version)
        unit_ids = {
            unit.code: str(ContentId.derive(str(curriculum_id), "unit", unit.code))
            for unit in document.units
        }
        resolved: dict[tuple[str, int], str] = {}
        for unit in document.units:
            for index, objective in enumerate(unit.objectives, start=1):
                if objective.mapped_kind is None or objective.mapped_key is None:
                    continue
                resolved[(unit.code, index)] = _resolve_mapping(
                    database,
                    pack_key=pack_row["pack_key"],
                    pack_id=pack_row["pack_id"],
                    mapped_kind=objective.mapped_kind,
                    mapped_key=objective.mapped_key,
                )
        correlation_id = EventId.new()
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO curricula (curriculum_id, track_id, pack_id, title, kind, version, "
                "source_reference, rights_status, provenance, framework_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(curriculum_id),
                    track_id,
                    pack_row["pack_id"],
                    document.title,
                    document.kind,
                    document.version,
                    document.source_reference,
                    document.rights_status,
                    document.provenance,
                    record.proficiency_framework,
                    now,
                    now,
                ],
            )
            for sequence, unit in enumerate(document.units, start=1):
                transaction.execute(
                    "INSERT INTO curriculum_units (unit_id, curriculum_id, parent_unit_id, "
                    "sequence, code, title, level_code, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        unit_ids[unit.code],
                        str(curriculum_id),
                        None if unit.parent_code is None else unit_ids[unit.parent_code],
                        sequence,
                        unit.code,
                        unit.title,
                        unit.level,
                        now,
                    ],
                )
                for index, objective in enumerate(unit.objectives, start=1):
                    transaction.execute(
                        "INSERT INTO curriculum_unit_objectives (unit_id, sequence, objective, "
                        "dimension, mapped_kind, mapped_ref, mapped_by, map_confidence) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            unit_ids[unit.code],
                            index,
                            objective.objective,
                            objective.dimension,
                            objective.mapped_kind,
                            resolved.get((unit.code, index)),
                            "import" if objective.mapped_kind else None,
                            objective.map_confidence,
                        ],
                    )
                transaction.execute(
                    "INSERT INTO track_curriculum_progress (track_id, unit_id, state, provenance, "
                    "evidence_summary_json, started_at, completed_at, updated_at) "
                    "VALUES (?, ?, 'not-started', ?, '{}', NULL, NULL, ?)",
                    [track_id, unit_ids[unit.code], document.provenance, now],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([str(curriculum_id)]),
                after_summary=f"imported {document.title} {document.version} "
                f"({len(document.units)} unit(s))",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="curriculum.imported",
                aggregate_type="curriculum",
                aggregate_id=str(curriculum_id),
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {
                        "title": document.title,
                        "version": document.version,
                        "units": len(document.units),
                        "mapped_objectives": len(resolved),
                        "rights_status": document.rights_status,
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"curriculum.imported:{curriculum_id}",
            )
    return show(paths, curriculum=str(curriculum_id), clock=active_clock)


def position(
    paths: WorkspacePaths,
    *,
    completed: Sequence[str] = (),
    current: Sequence[str] = (),
    curriculum: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "curriculum.position",
) -> CurriculumReport:
    """Record which units the learner claims to have finished.

    A claim reaches `encountered` and no further, with `self-report` provenance. It never
    creates evidence, and it never lowers a stage the learner has actually earned.
    """

    active_clock = clock or SystemClock()
    overlap = sorted(set(completed) & set(current))
    if overlap:
        raise LinguaWikiError(
            "invalid_arguments",
            f"unit(s) {overlap} cannot be both completed and current",
            details=(ErrorDetail(field="units", reason=", ".join(overlap)),),
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        curriculum_id = _resolve_curriculum(database, curriculum, track_id)
        units = {
            str(code): str(unit_id)
            for unit_id, code in database.query(
                "SELECT unit_id, code FROM curriculum_units WHERE curriculum_id = ?",
                [curriculum_id],
            )
        }
        unknown = sorted((set(completed) | set(current)) - set(units))
        if unknown:
            raise LinguaWikiError(
                "curriculum_unit_unknown",
                f"unit code(s) {unknown} are not in this curriculum",
                details=(ErrorDetail(field="units", reason=", ".join(unknown)),),
            )
        mapped = _mapped_targets(database, curriculum_id, list(completed))
        correlation_id = EventId.new()
        encountered = 0
        with database.transaction() as transaction:
            now = transaction.now()
            for code, state in (
                *((code, "claimed-complete") for code in completed),
                *((code, "current") for code in current),
            ):
                transaction.execute(
                    "UPDATE track_curriculum_progress SET state = ?, provenance = 'self-report', "
                    "started_at = coalesce(started_at, ?), completed_at = ?, updated_at = ? "
                    "WHERE track_id = ? AND unit_id = ?",
                    [
                        state,
                        now,
                        now if state == "claimed-complete" else None,
                        now,
                        track_id,
                        units[code],
                    ],
                )
            for content_id in sorted({ref for _, ref, kind in mapped if kind == "knowledge"}):
                existing = transaction.one(
                    "SELECT stage, stage_source FROM track_item_state WHERE track_id = ? "
                    "AND content_id = ?",
                    [track_id, content_id],
                )
                if existing is None:
                    transaction.execute(
                        "INSERT INTO track_item_state (track_id, content_id, stage, "
                        "stage_source, confidence, priority, first_encounter_at, "
                        "last_encounter_at, next_review_at, positive_evidence, "
                        "negative_evidence, updated_at) "
                        "VALUES (?, ?, ?, 'self-report', 0.0, 0, NULL, NULL, NULL, 0, 0, ?)",
                        [track_id, content_id, SELF_REPORT_STAGE, now],
                    )
                    encountered += 1
                elif str(existing[0]) == "unseen":
                    transaction.execute(
                        "UPDATE track_item_state SET stage = ?, stage_source = 'self-report', "
                        "updated_at = ? WHERE track_id = ? AND content_id = ?",
                        [SELF_REPORT_STAGE, now, track_id, content_id],
                    )
                    encountered += 1
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([curriculum_id]),
                after_summary=f"claimed {len(completed)} complete and {len(current)} current "
                f"unit(s); {encountered} item(s) reached {SELF_REPORT_STAGE}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="curriculum.positioned",
                aggregate_type="curriculum",
                aggregate_id=curriculum_id,
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {
                        "completed": list(completed),
                        "current": list(current),
                        "encountered_items": encountered,
                    },
                    sort_keys=True,
                ),
            )
    return show(paths, curriculum=curriculum_id, clock=active_clock)


def _mapped_targets(
    database: Database, curriculum_id: str, unit_codes: Sequence[str] | None = None
) -> list[tuple[str, str, str]]:
    """(unit code, content ID, mapped kind) for every mapped objective."""

    parameters: list[object] = [curriculum_id]
    clause = ""
    if unit_codes:
        placeholders = ", ".join("?" for _ in unit_codes)
        clause = f"AND unit.code IN ({placeholders})"
        parameters.extend(unit_codes)
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in database.query(
            "SELECT unit.code, objective.mapped_ref, objective.mapped_kind "
            "FROM curriculum_units unit "
            "JOIN curriculum_unit_objectives objective ON objective.unit_id = unit.unit_id "
            f"WHERE unit.curriculum_id = ? AND objective.mapped_ref IS NOT NULL {clause} "
            "ORDER BY unit.sequence, objective.sequence",
            parameters,
        )
    ]


def _audit_sample(
    database: Database,
    *,
    curriculum_id: str,
    track_id: str,
    sample_size: int,
) -> list[tuple[str, str, str, str, str, float]]:
    """A risk-weighted sample: (unit code, kind, content id, stable key, reason, weight)."""

    units = database.query(
        "SELECT unit.code, unit.sequence, progress.state FROM curriculum_units unit "
        "JOIN track_curriculum_progress progress ON progress.unit_id = unit.unit_id "
        "WHERE unit.curriculum_id = ? AND progress.track_id = ? ORDER BY unit.sequence",
        [curriculum_id, track_id],
    )
    claimed = [str(row[0]) for row in units if str(row[2]) == "claimed-complete"]
    if not claimed:
        raise LinguaWikiError(
            "curriculum_position_required",
            "record which units are complete before auditing them",
            details=(ErrorDetail(field="position", reason="no claimed-complete unit"),),
        )
    recent = set(claimed[-2:])
    placeholders = ", ".join("?" for _ in PROMOTED_LIFECYCLES)
    rows = database.query(
        "SELECT unit.code, objective.mapped_kind, objective.mapped_ref, record.stable_key, "
        "item.kind, "
        "(SELECT count(*) FROM knowledge_relations relation "
        "  WHERE relation.target_content_id = objective.mapped_ref "
        "  AND relation.relation_type = 'prerequisite') AS dependents "
        "FROM curriculum_units unit "
        "JOIN curriculum_unit_objectives objective ON objective.unit_id = unit.unit_id "
        "JOIN content_records record ON record.content_id = objective.mapped_ref "
        "LEFT JOIN knowledge_items item ON item.content_id = objective.mapped_ref "
        f"WHERE unit.curriculum_id = ? AND objective.mapped_ref IS NOT NULL "
        f"AND record.lifecycle IN ({placeholders}) ORDER BY unit.sequence, objective.sequence",
        [curriculum_id, *PROMOTED_LIFECYCLES],
    )
    scored: list[tuple[str, str, str, str, str, float]] = []
    seen: set[str] = set()
    for code, mapped_kind, content_id, stable_key, item_kind, dependents in rows:
        if str(code) not in claimed or str(content_id) in seen:
            continue
        seen.add(str(content_id))
        if int(dependents) >= 2:
            reason = "central-prerequisite"
        elif str(code) in recent:
            reason = "recent-unit"
        elif item_kind is not None and str(item_kind) in PRODUCTION_KINDS:
            reason = "production-target"
        else:
            reason = "older-material"
        scored.append(
            (
                str(code),
                str(mapped_kind),
                str(content_id),
                str(stable_key),
                reason,
                RISK_WEIGHTS[reason],
            )
        )
    scored.sort(key=lambda entry: (-entry[5], entry[3]))
    return scored[:sample_size]


def audit_start(
    paths: WorkspacePaths,
    *,
    curriculum: str | None = None,
    track: str | None = None,
    sample_size: int = DEFAULT_AUDIT_SAMPLE,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "curriculum.audit-start",
) -> AuditReport:
    """Select the risk-weighted sample this audit will actually probe."""

    active_clock = clock or SystemClock()
    if sample_size < 1:
        raise LinguaWikiError("invalid_arguments", "sample_size must be positive")
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        curriculum_id = _resolve_curriculum(database, curriculum, track_id)
        if idempotency_key is not None:
            existing = database.one(
                "SELECT audit_id FROM curriculum_audits WHERE idempotency_key = ?",
                [idempotency_key],
            )
            if existing is not None:
                return _audit_state(database, str(existing[0]))
        open_audit = database.one(
            "SELECT audit_id FROM curriculum_audits WHERE track_id = ? AND curriculum_id = ? "
            "AND status = 'in-progress'",
            [track_id, curriculum_id],
        )
        if open_audit is not None:
            raise LinguaWikiError(
                "curriculum_audit_in_progress",
                f"audit {open_audit[0]} is still open; record or finalize it first",
                details=(ErrorDetail(field="audit", reason="audit already open"),),
            )
        sample = _audit_sample(
            database,
            curriculum_id=curriculum_id,
            track_id=track_id,
            sample_size=sample_size,
        )
        audit_id = AssessmentId.new()
        correlation_id = EventId.new()
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO curriculum_audits (audit_id, track_id, curriculum_id, status, "
                "sample_size, idempotency_key, stop_reason, started_at, finalized_at, "
                "updated_at) VALUES (?, ?, ?, 'in-progress', ?, ?, NULL, ?, NULL, ?)",
                [
                    str(audit_id),
                    track_id,
                    curriculum_id,
                    len(sample),
                    idempotency_key,
                    now,
                    now,
                ],
            )
            for sequence, (code, kind, content_id, _key, reason, weight) in enumerate(
                sample, start=1
            ):
                unit_id = transaction.scalar(
                    "SELECT unit_id FROM curriculum_units WHERE curriculum_id = ? AND code = ?",
                    [curriculum_id, code],
                )
                transaction.execute(
                    "INSERT INTO curriculum_audit_items (audit_id, sequence, unit_id, "
                    "target_kind, target_ref, risk_reason, risk_weight, status, outcome, "
                    "score, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL)",
                    [
                        str(audit_id),
                        sequence,
                        str(unit_id),
                        kind,
                        content_id,
                        reason,
                        weight,
                    ],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([str(audit_id)]),
                after_summary=f"selected {len(sample)} risk-weighted audit target(s)",
            )
    return audit_report(paths, audit=str(audit_id), clock=active_clock)


def audit_record(
    paths: WorkspacePaths,
    *,
    results: Sequence[Mapping[str, Any]],
    audit: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "curriculum.audit-record",
) -> AuditReport:
    """Record a batch of audit results; a retry of the same target is a no-op."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        audit_id = _resolve_audit(database, audit, track_id)
        row = _audit_row(database, audit_id)
        if str(row[3]) != "in-progress":
            raise LinguaWikiError(
                "curriculum_audit_closed",
                f"audit {audit_id} is {row[3]} and takes no further results",
                details=(ErrorDetail(field="audit", reason="audit is closed"),),
            )
        pending = {
            str(target): int(sequence)
            for sequence, target in database.query(
                "SELECT sequence, target_ref FROM curriculum_audit_items WHERE audit_id = ? "
                "AND status = 'pending'",
                [audit_id],
            )
        }
        with database.transaction() as transaction:
            now = transaction.now()
            for entry in results:
                target = str(entry.get("target_ref", ""))
                outcome = str(entry.get("outcome", ""))
                if outcome not in ("correct", "partial", "incorrect"):
                    raise LinguaWikiError(
                        "invalid_arguments",
                        "each result needs outcome correct, partial, or incorrect",
                        details=(ErrorDetail(field="outcome", reason=outcome or "missing"),),
                    )
                if target not in pending:
                    continue
                score = entry.get("score")
                numeric = (
                    float(score)
                    if score is not None
                    else {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}[outcome]
                )
                if not 0.0 <= numeric <= 1.0:
                    raise LinguaWikiError(
                        "invalid_arguments", "an audit score must lie between 0.0 and 1.0"
                    )
                transaction.execute(
                    "UPDATE curriculum_audit_items SET status = 'recorded', outcome = ?, "
                    "score = ?, recorded_at = ? WHERE audit_id = ? AND sequence = ?",
                    [outcome, numeric, now, audit_id, pending[target]],
                )
            transaction.execute(
                "UPDATE curriculum_audits SET updated_at = ? WHERE audit_id = ?", [now, audit_id]
            )
    return audit_report(paths, audit=audit_id, clock=active_clock)


def _audit_row(database: Database, audit_id: str) -> Sequence[Any]:
    row = database.one(
        "SELECT audit_id, track_id, curriculum_id, status, sample_size, stop_reason, "
        "started_at, finalized_at FROM curriculum_audits WHERE audit_id = ?",
        [audit_id],
    )
    if row is None:
        raise LinguaWikiError(
            "curriculum_audit_not_found",
            f"no curriculum audit with ID {audit_id}",
            details=(ErrorDetail(field="audit", reason="unknown audit"),),
        )
    return row


def _resolve_audit(database: Database, audit: str | None, track_id: str | None) -> str:
    if audit is not None:
        return str(_audit_row(database, audit)[0])
    parameters: list[object] = []
    clause = "WHERE status = 'in-progress'"
    if track_id is not None:
        clause += " AND track_id = ?"
        parameters.append(track_id)
    rows = database.query(
        f"SELECT audit_id FROM curriculum_audits {clause} ORDER BY started_at DESC", parameters
    )
    if len(rows) == 1:
        return str(rows[0][0])
    raise LinguaWikiError(
        "curriculum_audit_selection_required",
        f"name an audit explicitly: {len(rows)} are open",
        details=(ErrorDetail(field="audit", reason="ambiguous audit selection"),),
    )


def audit_finalize(
    paths: WorkspacePaths,
    *,
    audit: str | None = None,
    track: str | None = None,
    stop_reason: str = "completed",
    clock: Clock | None = None,
    command: str = "curriculum.audit-finalize",
) -> AuditReport:
    """Turn audit misses into an evidence-gap queue, and say what was not tested."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        audit_id = _resolve_audit(database, audit, track_id)
        row = _audit_row(database, audit_id)
        if str(row[3]) == "finalized":
            return _audit_state(database, audit_id)
        items = database.query(
            "SELECT item.sequence, item.target_kind, item.target_ref, item.status, item.outcome, "
            "item.risk_reason, unit.unit_id FROM curriculum_audit_items item "
            "JOIN curriculum_units unit ON unit.unit_id = item.unit_id "
            "WHERE item.audit_id = ? ORDER BY item.sequence",
            [audit_id],
        )
        gaps = [
            (str(entry[1]), str(entry[2]), str(entry[5]))
            for entry in items
            if str(entry[3]) == "recorded" and str(entry[4]) in ("partial", "incorrect")
        ]
        untested = [str(entry[2]) for entry in items if str(entry[3]) == "pending"]
        confirmed = [
            str(entry[2])
            for entry in items
            if str(entry[3]) == "recorded" and str(entry[4]) == "correct"
        ]
        audited_units = sorted({str(entry[6]) for entry in items if str(entry[3]) == "recorded"})
        correlation_id = EventId.new()
        queued: list[str] = []
        with database.transaction() as transaction:
            now = transaction.now()
            for kind, content_id, reason in gaps:
                existing = transaction.one(
                    "SELECT queue_item_id FROM calibration_queue_items WHERE track_id = ? "
                    "AND purpose = 'audit-gap' AND target_kind = ? AND target_ref = ?",
                    [track_id, kind, content_id],
                )
                if existing is not None:
                    continue
                queue_item_id = str(ReviewId.new())
                transaction.execute(
                    "INSERT INTO calibration_queue_items (queue_item_id, track_id, "
                    "onboarding_id, audit_id, purpose, target_kind, target_ref, dimension, "
                    "priority, rationale, status, created_at, updated_at) "
                    "VALUES (?, ?, NULL, ?, 'audit-gap', ?, ?, NULL, ?, ?, 'pending', ?, ?)",
                    [
                        queue_item_id,
                        track_id,
                        audit_id,
                        kind,
                        content_id,
                        int(RISK_WEIGHTS.get(reason, 1.0) * 10),
                        f"audit miss on a {reason} target",
                        now,
                        now,
                    ],
                )
                queued.append(queue_item_id)
            # An audited unit becomes `audited`: the claim has been probed, whatever the
            # outcome. Nothing here grants mastery of its objectives.
            for unit_id in audited_units:
                transaction.execute(
                    "UPDATE track_curriculum_progress SET state = 'audited', "
                    "evidence_summary_json = ?, updated_at = ? WHERE track_id = ? AND unit_id = ?",
                    [
                        json.dumps(
                            {
                                "audit_id": audit_id,
                                "confirmed": len(confirmed),
                                "gaps": len(gaps),
                                "untested": len(untested),
                            },
                            sort_keys=True,
                        ),
                        now,
                        track_id,
                        unit_id,
                    ],
                )
            transaction.execute(
                "UPDATE curriculum_audits SET status = 'finalized', stop_reason = ?, "
                "finalized_at = ?, updated_at = ? WHERE audit_id = ?",
                [stop_reason, now, now, audit_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([audit_id]),
                after_summary=f"{len(confirmed)} confirmed, {len(gaps)} gap(s), "
                f"{len(untested)} untested",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="curriculum.audited",
                aggregate_type="curriculum",
                aggregate_id=str(row[2]),
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {
                        "audit_id": audit_id,
                        "confirmed": len(confirmed),
                        "gaps": len(gaps),
                        "untested": len(untested),
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"curriculum.audited:{audit_id}",
            )
    return audit_report(paths, audit=audit_id, clock=active_clock)


def audit_report(
    paths: WorkspacePaths,
    *,
    audit: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> AuditReport:
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = None if track is None else learner_service.resolve_track(database, track)
        return _audit_state(database, _resolve_audit(database, audit, track_id))


def _audit_state(database: Database, audit_id: str) -> AuditReport:
    """Read an audit's state through a caller's connection.

    Reports take a `Database` rather than a workspace because DuckDB serves one
    connection per database file: a command that has just written cannot open a second
    connection to report what it did without deadlocking on its own writer lock.
    """

    row = _audit_row(database, audit_id)
    entries = tuple(
        AuditItemReport(
            sequence=int(entry[0]),
            unit_code=str(entry[1]),
            target_kind=str(entry[2]),
            target_ref=str(entry[3]),
            stable_key=str(entry[4]),
            risk_reason=str(entry[5]),
            risk_weight=float(entry[6]),
            status=str(entry[7]),
            outcome=None if entry[8] is None else str(entry[8]),
            score=None if entry[9] is None else float(entry[9]),
        )
        for entry in database.query(
            "SELECT item.sequence, unit.code, item.target_kind, item.target_ref, "
            "record.stable_key, item.risk_reason, item.risk_weight, item.status, "
            "item.outcome, item.score FROM curriculum_audit_items item "
            "JOIN curriculum_units unit ON unit.unit_id = item.unit_id "
            "JOIN content_records record ON record.content_id = item.target_ref "
            "WHERE item.audit_id = ? ORDER BY item.sequence",
            [audit_id],
        )
    )
    queued = tuple(
        str(queue_item_id)
        for (queue_item_id,) in database.query(
            "SELECT queue_item_id FROM calibration_queue_items WHERE audit_id = ? "
            "ORDER BY priority DESC, queue_item_id",
            [audit_id],
        )
    )
    warnings: list[str] = []
    untested = tuple(entry.target_ref for entry in entries if entry.status == "pending")
    if untested:
        warnings.append(
            f"{len(untested)} sampled target(s) were never probed; they remain unverified "
            "rather than confirmed"
        )
    return AuditReport(
        audit_id=audit_id,
        track_id=str(row[1]),
        curriculum_id=str(row[2]),
        status=str(row[3]),
        sample_size=int(row[4]),
        recorded=len([entry for entry in entries if entry.status == "recorded"]),
        stop_reason=None if row[5] is None else str(row[5]),
        items=entries,
        confirmed_gaps=tuple(
            entry.target_ref for entry in entries if entry.outcome in ("partial", "incorrect")
        ),
        queued_calibration=queued,
        untested_targets=untested,
        started_at=str(aware_utc(row[6]).isoformat()),
        finalized_at=None if row[7] is None else str(aware_utc(row[7]).isoformat()),
        warnings=tuple(warnings),
    )


def show(
    paths: WorkspacePaths,
    *,
    curriculum: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> CurriculumReport:
    """The imported outline, its mapped and unmapped objectives, and the claims made."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        curriculum_id = _resolve_curriculum(database, curriculum, track_id)
        row = _curriculum_row(database, curriculum_id)
        units = tuple(
            UnitReport(
                unit_id=str(entry[0]),
                code=str(entry[1]),
                title=str(entry[2]),
                level=None if entry[3] is None else str(entry[3]),
                parent_code=None if entry[4] is None else str(entry[4]),
                sequence=int(entry[5]),
                state=str(entry[6]),
                provenance=str(entry[7]),
                objectives=int(entry[8]),
                mapped_objectives=int(entry[9]),
            )
            for entry in database.query(
                "SELECT unit.unit_id, unit.code, unit.title, unit.level_code, parent.code, "
                "unit.sequence, progress.state, progress.provenance, "
                "(SELECT count(*) FROM curriculum_unit_objectives objective "
                "  WHERE objective.unit_id = unit.unit_id), "
                "(SELECT count(*) FROM curriculum_unit_objectives objective "
                "  WHERE objective.unit_id = unit.unit_id AND objective.mapped_ref IS NOT NULL) "
                "FROM curriculum_units unit "
                "LEFT JOIN curriculum_units parent ON parent.unit_id = unit.parent_unit_id "
                "JOIN track_curriculum_progress progress ON progress.unit_id = unit.unit_id "
                "WHERE unit.curriculum_id = ? AND progress.track_id = ? ORDER BY unit.sequence",
                [curriculum_id, track_id],
            )
        )
        unmapped = tuple(
            ObjectiveReport(
                unit_code=str(entry[0]),
                sequence=int(entry[1]),
                objective=str(entry[2]),
                dimension=None if entry[3] is None else str(entry[3]),
            )
            for entry in database.query(
                "SELECT unit.code, objective.sequence, objective.objective, objective.dimension "
                "FROM curriculum_units unit "
                "JOIN curriculum_unit_objectives objective ON objective.unit_id = unit.unit_id "
                "WHERE unit.curriculum_id = ? AND objective.mapped_ref IS NULL "
                "ORDER BY unit.sequence, objective.sequence",
                [curriculum_id],
            )
        )
        total = sum(unit.objectives for unit in units)
        mapped = sum(unit.mapped_objectives for unit in units)
        encountered = int(
            database.scalar(
                "SELECT count(*) FROM track_item_state WHERE track_id = ? "
                "AND stage_source = 'self-report'",
                [track_id],
            )
        )
        warnings: list[str] = []
        if unmapped:
            warnings.append(
                f"{len(unmapped)} objective(s) map to nothing in the installed pack and remain "
                "explicit gaps"
            )
        return CurriculumReport(
            curriculum_id=curriculum_id,
            track_id=track_id,
            title=str(row[2]),
            kind=str(row[3]),
            version=str(row[4]),
            rights_status=str(row[6]),
            provenance=str(row[7]),
            source_reference=None if row[5] is None else str(row[5]),
            units=units,
            unmapped_objectives=unmapped,
            mapped_objectives=mapped,
            total_objectives=total,
            encountered_items=encountered,
            warnings=tuple(warnings),
        )


__all__ = [
    "DEFAULT_AUDIT_SAMPLE",
    "RISK_WEIGHTS",
    "AuditReport",
    "CurriculumInput",
    "CurriculumReport",
    "audit_finalize",
    "audit_record",
    "audit_report",
    "audit_start",
    "import_curriculum",
    "position",
    "show",
]
