"""Bounded resource planning: what an installed pack should prepare for one track.

Preparation is a dry-run plan first and an application second, because the failure mode
here is silent excess: importing a whole pack would create thousands of active knowledge
states, and importing part of one without saying so would make the learner believe a band
is covered. So a plan

- resolves the declared band's bundles *plus their prerequisites*, recursively, and
  nothing else;
- ranks what it found, keeps what fits the item budget, and records every dropped item as
  an explicit `skip` with a reason -- a truncated plan is never a quiet one;
- imports reference knowledge as `unseen`; no plan ever creates evidence or mastery;
- proposes external sources rather than fetching them;
- carries the pack's maturity into its own label, so a pilot pack yields a plan that says
  it is a pilot curriculum.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import Field

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.contracts import PackManifest, PackMaturity
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId, ReviewId
from linguawiki.models import ContractModel
from linguawiki.packs import coverage as coverage_module
from linguawiki.packs.format import KNOWLEDGE_KIND, RECOMMENDATION_KIND
from linguawiki.paths import WorkspacePaths
from linguawiki.provenance import PROMOTED_LIFECYCLES
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service

#: Reference items a single preparation may activate. A band is not a whole language.
DEFAULT_ITEM_BUDGET = 150
DEFAULT_WEEKS = 2
#: Item kinds a plan imports into learner state, in the order it prefers them.
IMPORTED_KINDS = ("knowledge", "example", "descriptor", "assessment_task", "activity_template")
#: Item kinds a plan only ever proposes.
PROPOSED_KINDS = (RECOMMENDATION_KIND,)


class PlanItem(ContractModel):
    sequence: int
    item_kind: str
    item_ref: str
    stable_key: str
    bundle_key: str
    action: str
    reason: str
    week: int | None = None


class ResourcePlanReport(ContractModel):
    plan_id: str | None
    track_id: str
    pack_key: str
    pack_version: str
    pack_maturity: str
    onboarding_mode: str
    plan_label: str
    status: str
    framework_id: str
    level_codes: tuple[str, ...]
    bundles: tuple[str, ...]
    item_budget: int
    weeks: int
    counts: dict[str, int] = Field(default_factory=dict)
    items: tuple[PlanItem, ...] = ()
    skipped: tuple[PlanItem, ...] = ()
    proposed_sources: tuple[PlanItem, ...] = ()
    unsupported_dimensions: tuple[str, ...] = ()
    missing_modalities: tuple[str, ...] = ()
    dry_run: bool = True
    applied_at: str | None = None
    #: Reference items now active for this track, whoever created them.
    imported_items: int = 0
    #: Items *this* preparation created. A repeat prepares nothing new, because a
    #: reference import never disturbs state the learner has already moved.
    newly_imported: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Candidate:
    item_kind: str
    content_id: str
    stable_key: str
    bundle_key: str
    depth: int
    themes: tuple[str, ...]
    priority: int


def _string_preference(preferences: Mapping[str, object], key: str) -> tuple[str, ...]:
    """Read a list-of-strings preference defensively; the column holds arbitrary JSON."""

    value = preferences.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if isinstance(entry, str))


def _manifest(pack_row: Mapping[str, str]) -> PackManifest:
    return PackManifest.model_validate(json.loads(pack_row["manifest_json"]))


def _bundles(database: Database, pack_id: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Every installed bundle: key -> (level code, dependency keys)."""

    return {
        str(key): (str(level), tuple(json.loads(str(dependencies))))
        for key, level, dependencies in database.query(
            "SELECT bundle_key, level_code, dependencies_json FROM resource_bundles "
            "WHERE pack_id = ? ORDER BY bundle_key",
            [pack_id],
        )
    }


def resolve_bundles(
    bundles: Mapping[str, tuple[str, tuple[str, ...]]], *, level_codes: Sequence[str]
) -> tuple[tuple[str, int], ...]:
    """The bundles for these levels plus their prerequisites, deepest first.

    Depth is how far a bundle is from the requested band: prerequisites come first so a
    truncated plan keeps the foundations rather than the newest material.
    """

    selected: dict[str, int] = {}
    frontier = [(key, 0) for key, (level, _) in sorted(bundles.items()) if level in level_codes]
    if not frontier:
        return ()
    # A cycle is refused by manifest validation, but the resolver deepens a bundle every
    # time it is reached, so a cycle reaching it from anywhere else would loop for ever.
    # The depth bound is the defence that does not depend on validation having run.
    depth_limit = len(bundles)
    while frontier:
        key, depth = frontier.pop()
        if key not in bundles:
            raise LinguaWikiError(
                "bundle_not_installed",
                f"bundle {key} is a declared prerequisite but is not installed",
                details=(ErrorDetail(field="bundle", reason="missing prerequisite bundle"),),
            )
        if depth > depth_limit:
            raise LinguaWikiError(
                "bundle_dependency_cycle",
                f"bundle {key} is reachable from itself through its prerequisites; a pack's "
                "bundle dependencies must be acyclic",
                details=(
                    ErrorDetail(
                        field="bundle",
                        reason="dependency cycle",
                        context={"bundle": key, "depth": str(depth)},
                    ),
                ),
            )
        if key in selected and selected[key] >= depth:
            continue
        selected[key] = max(selected.get(key, 0), depth)
        frontier.extend((dependency, depth + 1) for dependency in bundles[key][1])
    return tuple(sorted(selected.items(), key=lambda entry: (-entry[1], entry[0])))


def _themes_of(database: Database, content_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
    if not content_ids:
        return {}
    placeholders = ", ".join("?" for _ in content_ids)
    themes: dict[str, list[str]] = {}
    for content_id, value in database.query(
        f"SELECT content_id, tag_value FROM item_tags WHERE tag_kind = 'theme' "
        f"AND content_id IN ({placeholders}) ORDER BY content_id, tag_value",
        list(content_ids),
    ):
        themes.setdefault(str(content_id), []).append(str(value))
    return {key: tuple(values) for key, values in themes.items()}


def _bundle_candidates(
    database: Database,
    *,
    pack_id: str,
    ordered_bundles: Sequence[tuple[str, int]],
) -> list[_Candidate]:
    """Every promoted item the selected bundles list, tagged with its bundle depth."""

    placeholders = ", ".join("?" for _ in PROMOTED_LIFECYCLES)
    candidates: list[_Candidate] = []
    for bundle_key, depth in ordered_bundles:
        rows = database.query(
            "SELECT entry.item_kind, entry.item_ref, record.stable_key, entry.sequence "
            "FROM resource_bundles bundle "
            "JOIN resource_bundle_items entry ON entry.bundle_id = bundle.bundle_id "
            "JOIN content_records record ON record.content_id = entry.item_ref "
            f"WHERE bundle.pack_id = ? AND bundle.bundle_key = ? "
            f"AND record.lifecycle IN ({placeholders}) AND NOT record.quarantined "
            "ORDER BY entry.sequence",
            [pack_id, bundle_key, *PROMOTED_LIFECYCLES],
        )
        knowledge_ids = [str(row[1]) for row in rows if str(row[0]) == KNOWLEDGE_KIND]
        themes = _themes_of(database, knowledge_ids)
        for row in rows:
            candidates.append(
                _Candidate(
                    item_kind=str(row[0]),
                    content_id=str(row[1]),
                    stable_key=str(row[2]),
                    bundle_key=bundle_key,
                    depth=depth,
                    themes=themes.get(str(row[1]), ()),
                    priority=int(row[3]),
                )
            )
    return candidates


def _rank(
    candidate: _Candidate, *, interests: Sequence[str], kind_order: Sequence[str]
) -> tuple[int, int, int, int, str]:
    """Prerequisites first, then interest matches, then a stable order."""

    interest_match = 0 if set(candidate.themes) & set(interests) else 1
    try:
        kind_rank = list(kind_order).index(candidate.item_kind)
    except ValueError:
        kind_rank = len(kind_order)
    return (-candidate.depth, kind_rank, interest_match, candidate.priority, candidate.stable_key)


def _supported_modes(maturity: str) -> tuple[str, ...]:
    return coverage_module.supported_onboarding_modes(maturity)


def plan(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    onboarding_mode: str = "declared-level",
    level_codes: Sequence[str] | None = None,
    weeks: int = DEFAULT_WEEKS,
    item_budget: int = DEFAULT_ITEM_BUDGET,
    clock: Clock | None = None,
) -> ResourcePlanReport:
    """Produce the plan without writing it: the dry run `resources prepare` applies."""

    active_clock = clock or SystemClock()
    with open_reader(paths, clock=active_clock) as database:
        return _build_plan(
            database,
            track=track,
            onboarding_mode=onboarding_mode,
            level_codes=level_codes,
            weeks=weeks,
            item_budget=item_budget,
        )


def _build_plan(
    database: Database,
    *,
    track: str | None,
    onboarding_mode: str,
    level_codes: Sequence[str] | None,
    weeks: int,
    item_budget: int,
) -> ResourcePlanReport:
    if weeks < 1 or item_budget < 1:
        raise LinguaWikiError("invalid_arguments", "weeks and item_budget must be positive")
    track_id = learner_service.resolve_track(database, track)
    record = learner_service.track_context(database, track_id)
    pack_row = pack_service.installed_pack(database, record.pack_key)
    manifest = _manifest(pack_row)
    maturity = pack_row["maturity"]
    modes = _supported_modes(maturity)
    if onboarding_mode not in modes:
        raise LinguaWikiError(
            "onboarding_mode_unsupported",
            f"{pack_row['pack_key']} is {maturity}, which supports {list(modes) or 'no'} "
            f"onboarding mode(s), not {onboarding_mode}",
            details=(
                ErrorDetail(
                    field="onboarding_mode",
                    reason="pack maturity does not support this mode",
                    context={"maturity": maturity, "supported": ", ".join(modes)},
                ),
            ),
        )
    requested_levels = tuple(level_codes) if level_codes else ()
    if not requested_levels:
        if record.declared_level is None:
            raise LinguaWikiError(
                "level_required",
                "declare a level on the track or name the levels to prepare",
                details=(ErrorDetail(field="level", reason="no declared level"),),
            )
        requested_levels = (record.declared_level,)
    unknown_levels = sorted(set(requested_levels) - set(record.framework_levels))
    if unknown_levels:
        raise LinguaWikiError(
            "level_not_in_framework",
            f"{unknown_levels} are not levels of {record.proficiency_framework}",
            details=(ErrorDetail(field="level", reason=", ".join(unknown_levels)),),
        )
    uncovered = sorted(set(requested_levels) - set(manifest.bands))
    warnings: list[str] = []
    if uncovered:
        warnings.append(
            f"{pack_row['pack_key']} does not claim to cover {uncovered}; the plan can only "
            "prepare the bands the pack declares"
        )
    installed_bundles = _bundles(database, pack_row["pack_id"])
    ordered = resolve_bundles(installed_bundles, level_codes=requested_levels)
    if not ordered:
        raise LinguaWikiError(
            "no_bundle_for_level",
            f"{pack_row['pack_key']} has no resource bundle for {list(requested_levels)}",
            details=(ErrorDetail(field="level", reason="no bundle at this level"),),
        )
    interests = _string_preference(record.preferences, "interests")
    avoided = set(_string_preference(record.preferences, "avoided_topics"))
    candidates = _bundle_candidates(database, pack_id=pack_row["pack_id"], ordered_bundles=ordered)
    seen: set[tuple[str, str]] = set()
    kept: list[PlanItem] = []
    skipped: list[PlanItem] = []
    proposed: list[PlanItem] = []
    sequence = 0
    for candidate in sorted(
        candidates, key=lambda c: _rank(c, interests=interests, kind_order=IMPORTED_KINDS)
    ):
        key = (candidate.item_kind, candidate.content_id)
        if key in seen:
            continue
        seen.add(key)
        sequence += 1
        entry = PlanItem(
            sequence=sequence,
            item_kind=candidate.item_kind,
            item_ref=candidate.content_id,
            stable_key=candidate.stable_key,
            bundle_key=candidate.bundle_key,
            action="import",
            reason=f"bundle {candidate.bundle_key} at prerequisite depth {candidate.depth}",
        )
        if candidate.item_kind in PROPOSED_KINDS:
            proposed.append(
                entry.model_copy(
                    update={
                        "action": "propose",
                        "reason": "external material is proposed, never downloaded",
                    }
                )
            )
            continue
        if set(candidate.themes) & avoided:
            skipped.append(
                entry.model_copy(
                    update={
                        "action": "skip",
                        "reason": "theme is on the learner's avoided-topics list",
                    }
                )
            )
            continue
        if len([item for item in kept if item.item_kind == KNOWLEDGE_KIND]) >= item_budget and (
            candidate.item_kind == KNOWLEDGE_KIND
        ):
            skipped.append(
                entry.model_copy(
                    update={
                        "action": "skip",
                        "reason": f"item budget of {item_budget} reference items is full",
                    }
                )
            )
            continue
        kept.append(entry)
    per_week = max(1, len(kept) // weeks)
    kept = [
        item.model_copy(update={"week": min(weeks, 1 + index // per_week)})
        for index, item in enumerate(kept)
    ]
    tested_dimensions = {
        str(dimension)
        for (dimension,) in database.query(
            "SELECT DISTINCT task.dimension FROM assessment_tasks task "
            "JOIN content_records record ON record.content_id = task.content_id "
            "WHERE record.pack_id = ?",
            [pack_row["pack_id"]],
        )
    }
    unsupported = tuple(sorted(set(manifest.dimensions) - tested_dimensions))
    modality_coverage = {
        str(modality)
        for (modality,) in database.query(
            "SELECT DISTINCT modality FROM source_recommendations WHERE pack_id = ?",
            [pack_row["pack_id"]],
        )
    }
    missing_modalities = tuple(sorted(set(manifest.modalities) - modality_coverage))
    if unsupported:
        warnings.append(f"the pack tests no task for dimension(s) {list(unsupported)}")
    if missing_modalities:
        warnings.append(
            f"no reviewed source recommendation for modality/-ies {list(missing_modalities)}"
        )
    label = "pilot curriculum" if maturity == PackMaturity.PILOT else f"{maturity} curriculum"
    counts: dict[str, int] = {}
    for item in kept:
        counts[item.item_kind] = counts.get(item.item_kind, 0) + 1
    return ResourcePlanReport(
        plan_id=None,
        track_id=track_id,
        pack_key=pack_row["pack_key"],
        pack_version=pack_row["version"],
        pack_maturity=maturity,
        onboarding_mode=onboarding_mode,
        plan_label=label,
        status="planned",
        framework_id=record.proficiency_framework,
        level_codes=tuple(requested_levels),
        bundles=tuple(key for key, _ in ordered),
        item_budget=item_budget,
        weeks=weeks,
        counts=counts,
        items=tuple(kept),
        skipped=tuple(skipped),
        proposed_sources=tuple(proposed),
        unsupported_dimensions=unsupported,
        missing_modalities=missing_modalities,
        dry_run=True,
        warnings=tuple(warnings),
    )


def prepare(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    onboarding_mode: str = "declared-level",
    level_codes: Sequence[str] | None = None,
    weeks: int = DEFAULT_WEEKS,
    item_budget: int = DEFAULT_ITEM_BUDGET,
    dry_run: bool = False,
    clock: Clock | None = None,
    command: str = "resources.prepare",
) -> ResourcePlanReport:
    """Persist a plan and import its reference items as `unseen` learner state."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        report = _build_plan(
            database,
            track=track,
            onboarding_mode=onboarding_mode,
            level_codes=level_codes,
            weeks=weeks,
            item_budget=item_budget,
        )
        if dry_run:
            return report
        plan_id = ReviewId.new()
        correlation_id = EventId.new()
        with database.transaction() as transaction:
            imported = _apply_plan(
                transaction, report=report, plan_id=str(plan_id), correlation_id=correlation_id
            )
        result = plan_state(database, report.track_id)
    return result.model_copy(
        update={
            "newly_imported": imported,
            "warnings": (*result.warnings, *report.warnings),
        }
    )


def _apply_plan(
    database: Database, *, report: ResourcePlanReport, plan_id: str, correlation_id: EventId
) -> int:
    now = database.now()
    database.execute(
        "UPDATE resource_plans SET status = 'superseded', updated_at = ? "
        "WHERE track_id = ? AND status <> 'superseded'",
        [now, report.track_id],
    )
    database.execute(
        "INSERT INTO resource_plans (plan_id, track_id, pack_id, onboarding_mode, status, "
        "plan_label, level_codes_json, bundles_json, item_budget, weeks, unsupported_json, "
        "created_at, applied_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'applied', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            plan_id,
            report.track_id,
            str(pack_service.pack_row_id(report.pack_key)),
            report.onboarding_mode,
            report.plan_label,
            json.dumps(list(report.level_codes)),
            json.dumps(list(report.bundles)),
            report.item_budget,
            report.weeks,
            json.dumps(
                {
                    "dimensions": list(report.unsupported_dimensions),
                    "modalities": list(report.missing_modalities),
                }
            ),
            now,
            now,
            now,
        ],
    )
    for item in (*report.items, *report.skipped, *report.proposed_sources):
        database.execute(
            "INSERT INTO resource_plan_items (plan_id, sequence, item_kind, item_ref, "
            "bundle_key, action, reason, week) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                plan_id,
                item.sequence,
                item.item_kind,
                item.item_ref,
                item.bundle_key,
                item.action,
                item.reason,
                item.week,
            ],
        )
    imported = 0
    for item in report.items:
        if item.item_kind != KNOWLEDGE_KIND:
            continue
        existing = database.one(
            "SELECT stage FROM track_item_state WHERE track_id = ? AND content_id = ?",
            [report.track_id, item.item_ref],
        )
        if existing is not None:
            # A reference import never touches state the learner has already moved.
            continue
        database.execute(
            "INSERT INTO track_item_state (track_id, content_id, stage, stage_source, "
            "confidence, priority, first_encounter_at, last_encounter_at, next_review_at, "
            "positive_evidence, negative_evidence, updated_at) "
            "VALUES (?, ?, 'unseen', 'reference-import', 0.0, ?, NULL, NULL, NULL, 0, 0, ?)",
            [report.track_id, item.item_ref, item.week or 1, now],
        )
        imported += 1
    database.execute(
        "UPDATE onboarding_runs SET resource_plan_id = ?, updated_at = ? "
        "WHERE track_id = ? AND status <> 'finalized'",
        [plan_id, now, report.track_id],
    )
    migration_module.record_audit_entry(
        database,
        command="resources.prepare",
        correlation_id=correlation_id,
        outcome="succeeded",
        affected_records_json=json.dumps([plan_id]),
        after_summary=f"prepared {imported} reference item(s) as unseen",
    )
    migration_module.record_domain_event(
        database,
        event_type="resources.prepared",
        aggregate_type="track",
        aggregate_id=report.track_id,
        correlation_id=correlation_id,
        payload_json=json.dumps(
            {
                "plan_id": plan_id,
                "bundles": list(report.bundles),
                "imported": imported,
                "label": report.plan_label,
            },
            sort_keys=True,
        ),
        idempotency_key=f"resources.prepared:{plan_id}",
    )
    return imported


def show(
    paths: WorkspacePaths, *, track: str | None = None, clock: Clock | None = None
) -> ResourcePlanReport:
    """The applied plan for a track, with what it actually imported."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        return plan_state(database, learner_service.resolve_track(database, track))


def plan_state(database: Database, track_id: str) -> ResourcePlanReport:
    """Read the applied plan through a caller's connection.

    Reports take a `Database` rather than a workspace because DuckDB serves one
    connection per database file: a command that has just written cannot open a second
    connection to report what it did without deadlocking on its own writer lock.
    """

    row = database.one(
        "SELECT plan_id, pack_id, onboarding_mode, status, plan_label, level_codes_json, "
        "bundles_json, item_budget, weeks, unsupported_json, applied_at "
        "FROM resource_plans WHERE track_id = ? AND status = 'applied' "
        "ORDER BY created_at DESC",
        [track_id],
    )
    record = learner_service.track_context(database, track_id)
    pack_row = pack_service.installed_pack(database, record.pack_key)
    if row is None:
        return ResourcePlanReport(
            plan_id=None,
            track_id=track_id,
            pack_key=pack_row["pack_key"],
            pack_version=pack_row["version"],
            pack_maturity=pack_row["maturity"],
            onboarding_mode="declared-level",
            plan_label="none",
            status="none",
            framework_id=record.proficiency_framework,
            level_codes=(),
            bundles=(),
            item_budget=DEFAULT_ITEM_BUDGET,
            weeks=DEFAULT_WEEKS,
            dry_run=False,
            warnings=("no resource plan has been applied to this track yet",),
        )
    plan_id = str(row[0])
    entries = [
        PlanItem(
            sequence=int(item[0]),
            item_kind=str(item[1]),
            item_ref=str(item[2]),
            stable_key=str(item[5]),
            bundle_key=str(item[3]),
            action=str(item[4]),
            reason=str(item[6]),
            week=None if item[7] is None else int(item[7]),
        )
        for item in database.query(
            "SELECT entry.sequence, entry.item_kind, entry.item_ref, entry.bundle_key, "
            "entry.action, record.stable_key, entry.reason, entry.week "
            "FROM resource_plan_items entry "
            "JOIN content_records record ON record.content_id = entry.item_ref "
            "WHERE entry.plan_id = ? ORDER BY entry.sequence",
            [plan_id],
        )
    ]
    unsupported = json.loads(str(row[9]))
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.action == "import":
            counts[entry.item_kind] = counts.get(entry.item_kind, 0) + 1
    imported = int(
        database.scalar(
            "SELECT count(*) FROM track_item_state WHERE track_id = ? "
            "AND stage_source = 'reference-import'",
            [track_id],
        )
    )
    return ResourcePlanReport(
        plan_id=plan_id,
        track_id=track_id,
        pack_key=pack_row["pack_key"],
        pack_version=pack_row["version"],
        pack_maturity=pack_row["maturity"],
        onboarding_mode=str(row[2]),
        plan_label=str(row[4]),
        status=str(row[3]),
        framework_id=record.proficiency_framework,
        level_codes=tuple(json.loads(str(row[5]))),
        bundles=tuple(json.loads(str(row[6]))),
        item_budget=int(row[7]),
        weeks=int(row[8]),
        counts=counts,
        items=tuple(entry for entry in entries if entry.action == "import"),
        skipped=tuple(entry for entry in entries if entry.action == "skip"),
        proposed_sources=tuple(entry for entry in entries if entry.action == "propose"),
        unsupported_dimensions=tuple(unsupported.get("dimensions", ())),
        missing_modalities=tuple(unsupported.get("modalities", ())),
        dry_run=False,
        applied_at=None if row[10] is None else str(aware_utc(row[10]).isoformat()),
        imported_items=imported,
    )


__all__ = [
    "DEFAULT_ITEM_BUDGET",
    "DEFAULT_WEEKS",
    "PlanItem",
    "ResourcePlanReport",
    "plan",
    "plan_state",
    "prepare",
    "resolve_bundles",
    "show",
]
