"""Structured database integrity and drift checks behind `linguawiki db check`."""

from __future__ import annotations

import json

from pydantic import Field

from linguawiki import evidence as evidence_policy
from linguawiki.contracts import (
    SNAPSHOT_PARTIAL,
    LockManifest,
    parse_answer_key,
    parse_asset_identity,
    parse_task_presentation,
    reads_as_json_object,
    served_snapshot_state,
    snapshot_lost_its_presentation,
)
from linguawiki.db import migrations as migration_module
from linguawiki.db.backup import table_row_counts
from linguawiki.db.connection import Database, quote_identifier
from linguawiki.db.schema import (
    SchemaSpecification,
    expected_schema,
    tables_at_schema_version,
)
from linguawiki.errors import LinguaWikiError
from linguawiki.ids import IdPrefix, validate_id
from linguawiki.models import ContractModel
from linguawiki.placement import MACHINE_SCORABLE_TASK_TYPES
from linguawiki.presentation import choices_answering_key
from linguawiki.versions import core_pin, skill_bundle_pin

ORPHAN_RELATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("workspace_versions", "workspace_id", "workspaces", "workspace_id"),
    ("users", "workspace_id", "workspaces", "workspace_id"),
    ("user_languages", "user_id", "users", "user_id"),
    ("learning_tracks", "user_id", "users", "user_id"),
    ("track_preferences", "track_id", "learning_tracks", "track_id"),
    ("proficiency_framework_levels", "framework_id", "proficiency_frameworks", "framework_id"),
    ("pack_installations", "pack_id", "language_packs", "pack_id"),
    # Relations that are deliberately not foreign keys, because DuckDB cannot update a
    # row another table references, or cannot bulk-restore a self-referencing table.
    ("curriculum_units", "parent_unit_id", "curriculum_units", "unit_id"),
    ("resource_bundle_items", "item_ref", "content_records", "content_id"),
    ("calibration_queue_items", "audit_id", "curriculum_audits", "audit_id"),
    ("onboarding_runs", "resource_plan_id", "resource_plans", "plan_id"),
    ("onboarding_runs", "assessment_run_id", "assessment_runs", "run_id"),
    # DuckDB's ALTER TABLE cannot add a foreign key, and learning_tracks is referenced by
    # too many tables to recreate.
    ("learning_tracks", "pack_id", "language_packs", "pack_id"),
    # An estimate snapshot names the one it superseded. Self-referencing, so a portable
    # restore could not insert the table in one statement if it were a foreign key.
    ("estimate_history", "previous_snapshot_id", "estimate_history", "snapshot_id"),
    # `knowledge merge` repoints these onto the item a duplicate was folded into, and
    # DuckDB rewrites an update of a foreign-key column as a delete and an insert, which
    # a row referenced by another table refuses. All three are referenced, so the
    # relation is checked here instead of declared.
    ("attempts", "target_content_id", "knowledge_items", "content_id"),
    ("evidence", "target_content_id", "knowledge_items", "content_id"),
    ("error_patterns", "target_content_id", "knowledge_items", "content_id"),
    # A superseded pattern names the one its history moved to. Self-referencing and
    # written after insertion, so not a foreign key on either count.
    ("error_patterns", "superseded_by", "error_patterns", "error_id"),
    # A staged event names the close that consumed it and the row it became. Both are
    # written by that close, after the staged row already exists, and DuckDB rewrites an
    # update of a foreign-key column as a delete and an insert -- which the referenced
    # row refuses. So the relations are checked here rather than declared, and
    # `materialized_id` is checked per kind by `_session_checks` because one column
    # points at four different tables depending on what the event became.
    ("session_staged_events", "finalization_id", "session_finalizations", "finalization_id"),
    # C5. Every column naming an artifact is unenforced, because a purge rewrites the
    # artifact row; a staging row learns its artifact in the transaction that registers it;
    # a submission names its successor after both exist; and a result's artifact column
    # was added after the table, which DuckDB cannot give a foreign key.
    ("capture_stagings", "artifact_id", "artifacts", "artifact_id"),
    ("assessment_submissions", "artifact_id", "artifacts", "artifact_id"),
    ("assessment_submissions", "superseded_by", "assessment_submissions", "submission_id"),
    ("assessment_results", "audio_artifact_id", "artifacts", "artifact_id"),
    ("estimate_annotations", "result_id", "assessment_results", "result_id"),
    ("estimate_annotations", "artifact_id", "artifacts", "artifact_id"),
    # C6. A submission is written in place, so nothing that names one may be a foreign
    # key; claims, verdicts, and outcomes are insert-only today and checked all the same,
    # so the first stage to write one in place does not have to rebuild a table.
    ("judging_claims", "submission_id", "assessment_submissions", "submission_id"),
    ("judging_releases", "claim_id", "judging_claims", "claim_id"),
    ("assessment_verdicts", "submission_id", "assessment_submissions", "submission_id"),
    ("assessment_verdicts", "claim_id", "judging_claims", "claim_id"),
    ("assessment_verdict_outcomes", "verdict_id", "assessment_verdicts", "verdict_id"),
    ("assessment_verdict_outcomes", "result_id", "assessment_results", "result_id"),
)
#: Relations that hold for only some rows of the child: `(table, column, parent,
#: parent_column, scope_column, scope_value)`. A recording's `capture_id` names its staging
#: row; a written answer's names the producer's submission key, which no staging row
#: describes. Where the scope column does not exist yet -- a database behind migration
#: 0035 -- every row is in scope, because every submission then was a recording.
SCOPED_ORPHAN_RELATIONS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "assessment_submissions",
        "capture_id",
        "capture_stagings",
        "capture_id",
        "kind",
        "recording",
    ),
)
REQUIRED_PROJECTIONS = ("wiki",)


class CheckResult(ContractModel):
    name: str
    status: str
    message: str
    context: dict[str, str] = Field(default_factory=dict)


class IntegrityReport(ContractModel):
    ok: bool
    database: str
    database_schema_version: int
    packaged_schema_version: int
    checks: tuple[CheckResult, ...]
    row_counts: dict[str, int] = Field(default_factory=dict)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.status == "failed")

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(check.message for check in self.checks if check.status == "warning")


def _ok(name: str, message: str, **context: str) -> CheckResult:
    return CheckResult(name=name, status="ok", message=message, context=context)


def _failed(name: str, message: str, **context: str) -> CheckResult:
    return CheckResult(name=name, status="failed", message=message, context=context)


def _warning(name: str, message: str, **context: str) -> CheckResult:
    return CheckResult(name=name, status="warning", message=message, context=context)


def _migration_checks(database: Database, *, allow_behind_head: bool) -> list[CheckResult]:
    """Whether the history is ours, and whether it has reached this release's head.

    `allow_behind_head` is for a database that is *expected* to be behind: a restore of an
    older export produces exactly that, and calling it a failure would make a recovery
    look like a corruption.
    """

    packaged = migration_module.head_version()
    try:
        migration_module.assert_history_matches_package(database)
    except LinguaWikiError as exc:
        return [_failed("migration_history", exc.payload.message, code=exc.payload.code)]
    applied = migration_module.applied_version(database)
    checks = [_ok("migration_history", "applied migrations match the packaged checksums")]
    if applied >= packaged:
        checks.append(_ok("migration_head", f"database schema is at version {applied}"))
    elif allow_behind_head:
        checks.append(
            _warning(
                "migration_head",
                f"schema version {applied} is behind this release's {packaged}; "
                "run 'linguawiki db migrate' before using it",
                applied=str(applied),
                packaged=str(packaged),
            )
        )
    else:
        checks.append(
            _failed(
                "migration_head",
                "the database is behind this core release; run 'linguawiki db migrate'",
                applied=str(applied),
                packaged=str(packaged),
            )
        )
    return checks


def _table_checks(database: Database, *, expected: frozenset[str]) -> list[CheckResult]:
    """Compare the tables present against the applied schema version's own table set.

    The expectation is derived from the applied version rather than from this release's
    head, so a database restored at an older schema is judged against the schema it
    actually claims -- otherwise every older export looks like it is missing tables.
    """

    present = set(database.table_names())
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    checks: list[CheckResult] = []
    if missing:
        checks.append(
            _failed("tables_present", "expected tables are missing", missing=", ".join(missing))
        )
    else:
        checks.append(_ok("tables_present", f"all {len(expected)} registered tables exist"))
    if unexpected:
        checks.append(
            _failed(
                "tables_registered",
                "the database has tables outside the schema registry",
                unexpected=", ".join(unexpected),
            )
        )
    return checks


def _identity_checks(database: Database) -> list[CheckResult]:
    workspaces = int(database.scalar("SELECT count(*) FROM workspaces"))
    if workspaces == 1:
        checks = [_ok("workspace_identity", "exactly one workspace row is present")]
    else:
        checks = [
            _failed(
                "workspace_identity",
                "a learner database must hold exactly one workspace row",
                rows=str(workspaces),
            )
        ]
    duplicate_primaries = database.query(
        "SELECT user_id, count(*) FROM learning_tracks WHERE is_primary "
        "GROUP BY user_id HAVING count(*) > 1 ORDER BY user_id"
    )
    if duplicate_primaries:
        checks.append(
            _failed(
                "primary_track",
                "a user has more than one primary learning track",
                users=", ".join(str(row[0]) for row in duplicate_primaries),
            )
        )
    else:
        checks.append(_ok("primary_track", "every user has at most one primary track"))
    return checks


def _track_pack_binding_checks(database: Database) -> list[CheckResult]:
    """Every track must name the pack it is taught from.

    The column is nullable because `ALTER TABLE ADD COLUMN` cannot be otherwise, and
    migration 0016 fills in every track whose language has exactly one installed pack.
    What is left is genuinely ambiguous -- two packs could have taught it -- and guessing
    is not a repair, so it is reported as a named condition for a person to settle.
    """

    unbound = [
        str(track_id)
        for (track_id,) in database.query(
            "SELECT track_id FROM learning_tracks WHERE pack_id IS NULL ORDER BY track_id"
        )
    ]
    if unbound:
        return [
            _failed(
                "track_pack_binding",
                "a learning track does not name the pack it is taught from; re-create the "
                "track against the intended pack",
                tracks=", ".join(unbound[:20]),
            )
        ]
    return [_ok("track_pack_binding", "every learning track names its pack")]


#: Framework-scoped pack records: table and its key column.
#: The table, its key column, and what makes one of its records *live*. A record the pack
#: no longer ships is deprecated rather than deleted, because learner state may still
#: point at it, and it keeps the framework it was authored under -- the global framework
#: record is what keeps that label interpretable. Only records the pack still serves have
#: to agree with what the pack declares today.
FRAMEWORK_SCOPED_RECORDS: tuple[tuple[str, str, str], ...] = (
    (
        "proficiency_descriptors",
        "descriptor_id",
        "EXISTS (SELECT 1 FROM content_records item "
        "WHERE item.content_id = record.descriptor_id AND item.lifecycle <> 'deprecated')",
    ),
    (
        "resource_bundles",
        "bundle_id",
        "EXISTS (SELECT 1 FROM content_records item "
        "WHERE item.content_id = record.bundle_id AND item.lifecycle <> 'deprecated')",
    ),
    (
        # A definition is not a content record, so it carries its own status. Null means
        # 'active': every row written before migration 0018 was one the pack shipped.
        "assessment_definitions",
        "definition_id",
        "coalesce(record.status, 'active') <> 'superseded'",
    ),
)


def _pack_framework_scoping_checks(database: Database) -> list[CheckResult]:
    """Every live framework-scoped record names a framework its own pack declares.

    The installers deliberately never update `framework_id` -- DuckDB rewrites such an
    update as a delete and an insert, which referencing rows refuse -- so a pack that
    changed its framework left these rows behind pointing at the old one. `pack install`
    now refuses that update; this is what makes the resulting state visible either way,
    and it covers all three tables rather than trusting one of them.
    """

    stale: list[str] = []
    for table, key_column, live in FRAMEWORK_SCOPED_RECORDS:
        scoped = quote_identifier(table)
        key = quote_identifier(key_column)
        # Schema 0018 introduced the definition status. Before then every definition
        # was live, so an integrity check of a behind-head database must not reference
        # a column that its schema does not have.
        if table == "assessment_definitions" and "status" not in {
            name for name, _ in database.columns(table)
        }:
            live = "TRUE"
        stale.extend(
            f"{table}.{identity} -> {framework_id}"
            for identity, framework_id in database.query(
                f"SELECT record.{key}, record.framework_id FROM {scoped} record "
                f"WHERE record.framework_id IS NOT NULL AND {live} AND NOT EXISTS ("
                "  SELECT 1 FROM pack_frameworks declared "
                "  WHERE declared.pack_id = record.pack_id "
                "  AND declared.framework_id = record.framework_id) "
                f"ORDER BY record.{key}"
            )
        )
    if stale:
        return [
            _failed(
                "pack_framework_scoping",
                "a framework-scoped pack record names a framework its pack does not declare",
                records="; ".join(stale[:20]),
            )
        ]
    return [
        _ok("pack_framework_scoping", "every framework-scoped record matches its pack's frameworks")
    ]


def _active_track_uniqueness_checks(database: Database) -> list[CheckResult]:
    """One track a learner is still taught in, per language, region, and script.

    Migration 0017 dropped the unique index that carried this, because it counted
    archived tracks and so made replacing a track impossible. DuckDB has no partial
    unique index, so the narrowed rule lives here -- the same treatment `is_primary` has.
    """

    duplicates = [
        f"{user_id}/{language}"
        for user_id, language, _count in database.query(
            "SELECT user_id, target_language, count(*) FROM learning_tracks "
            "WHERE status <> 'archived' "
            "GROUP BY user_id, target_language, coalesce(region, ''), coalesce(script, '') "
            "HAVING count(*) > 1 ORDER BY 1, 2"
        )
    ]
    if duplicates:
        return [
            _failed(
                "active_track_uniqueness",
                "a learner has more than one live track for the same language, region, and "
                "script; archive the one being replaced",
                tracks=", ".join(duplicates[:20]),
            )
        ]
    return [_ok("active_track_uniqueness", "no learner has duplicate live tracks")]


def _track_framework_binding_checks(database: Database) -> list[CheckResult]:
    """A track's framework must be one its own pack declares.

    Every level label a track records is a label of this framework, so a track whose pack
    has stopped declaring it is recording labels no installed pack vouches for. `pack
    install` refuses the update that would cause it; this catches the state however it
    arrived.

    Archived tracks are exempt. Archiving one is how a learner is moved onto a
    replacement framework, and the labels it keeps stay interpretable through the global
    framework record, which only ever grows.
    """

    unbound = [
        f"{track_id} -> {framework_id}"
        for track_id, framework_id in database.query(
            "SELECT track.track_id, track.proficiency_framework FROM learning_tracks track "
            "WHERE track.pack_id IS NOT NULL AND track.status <> 'archived' AND NOT EXISTS ("
            "  SELECT 1 FROM pack_frameworks declared "
            "  WHERE declared.pack_id = track.pack_id "
            "  AND declared.framework_id = track.proficiency_framework) "
            "ORDER BY track.track_id"
        )
    ]
    if unbound:
        return [
            _failed(
                "track_framework_binding",
                "a learning track is taught in a framework its own pack does not declare",
                tracks="; ".join(unbound[:20]),
            )
        ]
    return [_ok("track_framework_binding", "every track's framework is declared by its pack")]


def _content_checks(database: Database) -> list[CheckResult]:
    """Invariants of the content tables that no CHECK constraint can carry.

    `content_records` asserts its ownership rule here rather than as a CHECK, because the
    columns it spans -- `pack_id` and `track_id` -- are both foreign keys, and DuckDB
    rewrites an update of a foreign-key column on a referenced row as a delete and an
    insert. Every content row is referenced, so the rule has to live outside the schema.
    (Stage 2 attributed this to the multi-column CHECK itself; measurement says otherwise,
    and `tests/unit/test_duckdb_update_limits.py` records which of the two it is. The
    decision is unchanged either way.)
    """

    checks: list[CheckResult] = []
    both = int(
        database.scalar(
            "SELECT count(*) FROM content_records WHERE pack_id IS NOT NULL "
            "AND track_id IS NOT NULL"
        )
    )
    if both:
        checks.append(
            _failed(
                "content_ownership",
                "content belongs to a pack or to a track, never to both",
                rows=str(both),
            )
        )
    else:
        checks.append(_ok("content_ownership", "every content row has one owner at most"))
    uninstalled = int(
        database.scalar(
            "SELECT count(*) FROM language_packs pack WHERE NOT EXISTS "
            "(SELECT 1 FROM pack_installations installation "
            " WHERE installation.pack_id = pack.pack_id)"
        )
    )
    if uninstalled:
        checks.append(
            _failed(
                "pack_installation",
                "a registered pack has no installation row",
                packs=str(uninstalled),
            )
        )
    else:
        checks.append(_ok("pack_installation", "every registered pack records its installation"))
    return checks


def _evidence_checks(database: Database) -> list[CheckResult]:
    """Every stored claim is one the attempt behind it could actually support.

    The rule is enforced when the row is written, so a violation here means a row that
    did not come through `evidence record`: a restored export from a build with a looser
    rule, or a hand-edited database. Either way it is the difference between a learner
    model and a wish, so it is named rather than trusted.

    Distinct combinations are checked rather than rows: the vocabulary is small, so this
    is a handful of validations however much evidence there is.
    """

    from linguawiki.evidence import assert_compatible

    problems: list[str] = []
    for claim, modality, task_type, help_level, retrieval, polarity, total in database.query(
        "SELECT claim, modality, task_type, help_level, retrieval, polarity, count(*) "
        "FROM evidence GROUP BY ALL ORDER BY 1, 2, 3, 4, 5, 6"
    ):
        try:
            assert_compatible(
                str(claim),
                modality=str(modality),
                task_type=str(task_type),
                help_level=str(help_level),
                retrieval=str(retrieval),
                polarity=str(polarity),
                dimension="present",
                target_content_id="present",
            )
        except LinguaWikiError as exc:
            problems.append(f"{claim} from {task_type}/{modality} ({total}): {exc.payload.code}")
    checks: list[CheckResult] = []
    if problems:
        checks.append(
            _failed(
                "evidence_claim_compatibility",
                "an evidence claim is not one its attempt could support",
                claims="; ".join(problems[:20]),
            )
        )
    else:
        checks.append(
            _ok("evidence_claim_compatibility", "every claim matches the attempt behind it")
        )
    # An evidence row restates the conditions of its attempt so aggregation never has to
    # join. Restating means they can disagree, so the agreement is checked.
    disagreements = int(
        database.scalar(
            "SELECT count(*) FROM evidence item "
            "JOIN attempts attempt ON attempt.attempt_id = item.attempt_id "
            "WHERE item.track_id <> attempt.track_id "
            "OR coalesce(item.target_content_id, '-') <> coalesce(attempt.target_content_id, '-') "
            "OR coalesce(item.dimension, '-') <> coalesce(attempt.dimension, '-') "
            "OR item.context_key <> attempt.context_key "
            "OR item.help_level <> attempt.help_level "
            "OR item.modality <> attempt.modality "
            "OR item.task_type <> attempt.task_type "
            "OR item.retrieval <> attempt.retrieval"
        )
    )
    if disagreements:
        checks.append(
            _failed(
                "evidence_attempt_agreement",
                "an evidence row disagrees with the attempt it came from",
                rows=str(disagreements),
            )
        )
    else:
        checks.append(
            _ok("evidence_attempt_agreement", "every evidence row matches its own attempt")
        )
    # One attempt cannot justify the same claim about the same target twice: that would
    # double-count one observation into a promotion. A unique index cannot carry the rule,
    # because an index over `target_content_id` would stop `knowledge merge` repointing
    # the row, so it is enforced on write and checked here.
    duplicated = [
        f"{attempt_id}/{claim}"
        for attempt_id, claim, _target, _dimension, _total in database.query(
            "SELECT attempt_id, claim, coalesce(target_content_id, '-'), "
            "coalesce(dimension, '-'), count(*) FROM evidence GROUP BY ALL "
            "HAVING count(*) > 1 ORDER BY 1, 2"
        )
    ]
    if duplicated:
        checks.append(
            _failed(
                "evidence_atom_uniqueness",
                "one attempt justifies the same claim about the same target more than "
                "once, so a single observation is counted twice",
                atoms="; ".join(duplicated[:20]),
            )
        )
    else:
        checks.append(_ok("evidence_atom_uniqueness", "no observation is counted more than once"))
    # An attempt or an observation about neither an item nor a dimension measures
    # nothing. This was a multi-column CHECK until the column stopped being a foreign
    # key; it is the same rule, asserted in the same place as the rest of them.
    scopeless = int(
        database.scalar(
            "SELECT (SELECT count(*) FROM attempts "
            " WHERE target_content_id IS NULL AND dimension IS NULL) "
            "+ (SELECT count(*) FROM evidence "
            " WHERE target_content_id IS NULL AND dimension IS NULL)"
        )
    )
    if scopeless:
        checks.append(
            _failed(
                "evidence_target_scope",
                "an attempt or an observation names neither a knowledge item nor a "
                "skill dimension, so it measures nothing",
                rows=str(scopeless),
            )
        )
    else:
        checks.append(
            _ok("evidence_target_scope", "every observation names an item or a dimension")
        )
    return checks


#: Learner state that names a knowledge item, and the column it names it with. Each is
#: scoped to one track, so the item has to be one that track's pack ships or that track
#: authored.
TRACK_SCOPED_TARGETS: tuple[tuple[str, str], ...] = (
    ("attempts", "target_content_id"),
    ("evidence", "target_content_id"),
    ("error_patterns", "target_content_id"),
    ("followups", "target_content_id"),
    ("track_item_state", "content_id"),
)


def _track_item_scope_checks(database: Database) -> list[CheckResult]:
    """Learner state may only name items its own track could be studying.

    A track names the pack it is taught from, and every item identity is relative to
    that pack. Recording evidence against another pack's item produces a learner model
    of a language the learner is not studying, and the commands now refuse it -- this is
    what makes the resulting state visible if it arrived another way.

    Content owned by neither a pack nor a track is in scope for everyone: that is where
    shared, unowned material would live.
    """

    stray: list[str] = []
    for table, column in TRACK_SCOPED_TARGETS:
        scoped = quote_identifier(table)
        key = quote_identifier(column)
        stray.extend(
            f"{table}: {track_id} -> {content_id}"
            for track_id, content_id in database.query(
                f"SELECT row.track_id, row.{key} FROM {scoped} row "
                "JOIN learning_tracks track ON track.track_id = row.track_id "
                "JOIN content_records record ON record.content_id = row."
                f"{key} "
                f"WHERE row.{key} IS NOT NULL "
                "AND coalesce(record.pack_id, '') <> coalesce(track.pack_id, '') "
                "AND coalesce(record.track_id, '') <> row.track_id "
                "AND NOT (record.pack_id IS NULL AND record.track_id IS NULL) "
                f"ORDER BY row.track_id, row.{key}"
            )
        )
    if stray:
        return [
            _failed(
                "track_item_scope",
                "learner state names a knowledge item from a pack its track is not taught "
                "from, or from another learner's track",
                rows="; ".join(stray[:20]),
            )
        ]
    return [_ok("track_item_scope", "every recorded item belongs to its own track's material")]


def _knowledge_graph_checks(database: Database) -> list[CheckResult]:
    """One edge per (source, type, target), whatever identity it was derived under.

    An edge's identity comes from the parts it describes, so a repointed endpoint that
    kept its old identity leaves a row describing an item it no longer touches -- and the
    next attempt to add that edge derives the new identity, finds nothing, and creates a
    duplicate. Migration 0011 deliberately has no second unique index over the parts, so
    the rule is asserted here.
    """

    duplicated = [
        f"{source} --{relation_type}--> {target}"
        for source, relation_type, target, _count in database.query(
            "SELECT source_content_id, relation_type, "
            "coalesce(target_content_id, target_ref), count(*) "
            "FROM knowledge_relations GROUP BY ALL HAVING count(*) > 1 ORDER BY 1, 2, 3"
        )
    ]
    if duplicated:
        return [
            _failed(
                "knowledge_relation_uniqueness",
                "the same edge exists more than once, so an identity describes an item it "
                "no longer touches",
                edges="; ".join(duplicated[:20]),
            )
        ]
    return [_ok("knowledge_relation_uniqueness", "every edge exists exactly once")]


def served_target_list(raw: object) -> tuple[str, ...] | None:
    """Parse a served target snapshot, returning `None` when it is not one.

    `target_refs_json` is an added column, and DuckDB cannot add a column with a
    constraint, so nothing in the schema guarantees the text is even JSON. Parsing it in
    SQL made `db check` abort on the first malformed value -- a diagnostic that crashes
    reports less than one that lies. So it is parsed here, defensively, and anything that
    is not a list of content identifiers is reported rather than raised.
    """

    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    identifiers: list[str] = []
    for entry in parsed:
        if not isinstance(entry, str):
            return None
        try:
            identifiers.append(validate_id(entry, IdPrefix.CONTENT))
        except ValueError:
            return None
    return tuple(identifiers)


def _served_target_checks(database: Database) -> list[CheckResult]:
    """Every target snapshot is well formed, and every targeted attempt is inside its own.

    Two failures were possible while this rode on a SQL predicate guarded by
    `IS NOT NULL`: a restored attempt with an item target and no snapshot passed, because
    the comparison never ran; and a malformed snapshot passed while unused and aborted
    the whole check when used. Absent data is exactly where a check has to speak.
    """

    checks: list[CheckResult] = []
    malformed = [
        f"{run_id}/{content_id}"
        for run_id, content_id, raw in database.query(
            "SELECT run_id, content_id, target_refs_json FROM assessment_run_tasks "
            "WHERE target_refs_json IS NOT NULL ORDER BY run_id, content_id"
        )
        if served_target_list(raw) is None
    ]
    if malformed:
        checks.append(
            _failed(
                "served_targets_wellformed",
                "a served record's target list is not a JSON array of content identifiers, "
                "so what the task tested cannot be read from it",
                served="; ".join(malformed[:20]),
            )
        )
    else:
        checks.append(
            _ok("served_targets_wellformed", "every served target list is a list of content IDs")
        )
    problems: list[str] = []
    for attempt_id, target, raw in database.query(
        "SELECT attempt.attempt_id, attempt.target_content_id, served.target_refs_json "
        "FROM attempts attempt "
        "JOIN assessment_run_tasks served "
        "  ON served.run_id = attempt.assessment_run_id "
        "  AND served.content_id = attempt.task_content_id "
        "WHERE attempt.target_content_id IS NOT NULL ORDER BY attempt.attempt_id"
    ):
        if raw is None:
            problems.append(f"{attempt_id}: the served record does not say what was targeted")
            continue
        targets = served_target_list(raw)
        if targets is None:
            problems.append(f"{attempt_id}: the served target list cannot be read")
        elif str(target) not in targets:
            problems.append(f"{attempt_id}: {target} is not among the served targets")
    if problems:
        checks.append(
            _failed(
                "attempt_served_targets",
                "an attempt is about an item the task it names was not recorded as "
                "testing, or was recorded without saying what it tested at all",
                attempts="; ".join(problems[:20]),
            )
        )
    else:
        checks.append(
            _ok("attempt_served_targets", "every targeted attempt is inside its task's targets")
        )
    return checks


def _reads_as_answer_key(raw: str) -> bool:
    """Whether a stored answer key is one the scorer could actually use.

    Asks `parse_answer_key`, so the check and the scorer cannot drift into disagreeing
    about what a usable key is -- and catches its refusal, because a diagnostic never
    raises.
    """

    try:
        parse_answer_key(raw)
    except LinguaWikiError:
        return False
    return True


def _reads_as_presentation(raw: str) -> bool:
    """Asks `parse_task_presentation`, so the check and the reader cannot disagree."""

    try:
        parse_task_presentation(raw)
    except LinguaWikiError:
        return False
    return True


def _presentation_checks(database: Database) -> list[CheckResult]:
    """Migration 0031's two groups: the bank's record, and the record of what was served.

    None of it can be a constraint. DuckDB refuses `ALTER TABLE ... ADD COLUMN` with one,
    so neither `presentation_json` carries `json_valid` and neither carries a vocabulary
    check on the kind. Each rule parses defensively: a diagnostic that aborts on damaged
    input tells an operator less than one that names the rows that will not parse.

    This is a third independent snapshot group, beside 0016's and 0030's. A row may
    legitimately be whole in one and absent in another, so widening either of those
    checks would make it lie about the others.
    """

    checks: list[CheckResult] = []
    # The bank. `task_type` and `modality` sit on the same row, so the check asks what
    # the row itself says it needs rather than the stronger question of every row.
    bank: list[str] = []
    for content_id, task_type, modality, raw, expected_json in database.query(
        "SELECT content_id, task_type, modality, presentation_json, expected_json "
        "FROM assessment_tasks WHERE presentation_json IS NOT NULL ORDER BY content_id"
    ):
        try:
            shown = parse_task_presentation(str(raw))
        except LinguaWikiError as failure:
            bank.append(f"{content_id}: {failure.payload.details[0].reason}")
            continue
        if shown is None:
            continue
        if shown.choices and str(task_type) != "objective":
            bank.append(f"{content_id}: a {task_type} task carries choices")
        if shown.audio is not None and str(modality) != "audio":
            bank.append(f"{content_id}: a {modality} task carries a recording")
        if not shown.choices:
            continue
        # The consequence, not the derivation. A bank that was hand-repaired, partially
        # restored, or written by a future installer bug can hold buttons none of which
        # the key accepts -- and then every learner who presses one scores 0.0 with
        # nothing anywhere saying why. Checking only that the record *parses* passes
        # exactly the state the model's other half forbids.
        try:
            key = parse_answer_key(str(expected_json))
        except LinguaWikiError:
            bank.append(f"{content_id}: a task with choices has no usable answer key")
            continue
        answering = choices_answering_key([choice.value for choice in shown.choices], key.answers)
        if len(answering) != 1:
            bank.append(
                f"{content_id}: {len(answering)} of its choices are answers the key accepts"
            )
    if bank:
        checks.append(
            _failed(
                "bank_presentation_wellformed",
                "an installed task says it is shown in a way its own type or modality "
                "cannot be, so a client would render a question nobody can answer",
                tasks="; ".join(bank),
            )
        )
    else:
        checks.append(
            _ok(
                "bank_presentation_wellformed",
                "every installed presentation agrees with the task it belongs to",
            )
        )
    # The served snapshot. Completeness and readability are separate questions asked over
    # the same rows: a record that will not parse cannot be judged complete, and a
    # complete pair of columns can still hold a record nobody can render.
    partial: list[str] = []
    malformed: list[str] = []
    # Every served row, not only the ones with something in them. A snapshot cleared to
    # NULL beside an unchanged bank row that *has* a presentation is damage the reader
    # refuses, and filtering it out is how an operator was told the workspace was clean
    # and then refused. The bank row comes along on the same query, because that
    # comparison is the whole rule.
    for (
        run_id,
        content_id,
        raw_shown,
        raw_identity,
        served_hash,
        bank_shown,
        bank_hash,
    ) in database.query(
        "SELECT served.run_id, served.content_id, served.presentation_json, "
        "served.asset_identity_json, served.content_hash, task.presentation_json, "
        "record.content_hash FROM assessment_run_tasks served "
        "LEFT JOIN assessment_tasks task ON task.content_id = served.content_id "
        "LEFT JOIN content_records record ON record.content_id = served.content_id "
        "ORDER BY served.run_id, served.content_id"
    ):
        where = f"{run_id}/{content_id}"
        # Through the same two parsers the reader uses. `db check` and the read path
        # asking this question two different ways is how an operator is told a workspace
        # is clean and then refused -- told the opposite of the truth, in this file's own
        # words -- and "present" is *parses to something*, not "the column is not NULL":
        # an empty string is absent to one and present to the other.
        shown = None
        readable_shown = True
        if raw_shown is not None:
            try:
                shown = parse_task_presentation(str(raw_shown))
            except LinguaWikiError:
                readable_shown = False
                malformed.append(f"{where}: the presentation cannot be read")
        identity = None
        readable_identity = True
        if raw_identity is not None:
            try:
                # A key with no digest is refused here by the same rule the reader
                # applies: it answers "which recording was meant" and not "is this the
                # recording they heard", which is the only question the snapshot settles.
                identity = parse_asset_identity(str(raw_identity))
            except LinguaWikiError:
                readable_identity = False
                malformed.append(f"{where}: the asset identity cannot be read")
        if readable_shown and readable_identity:
            if identity is not None and shown is None:
                partial.append(f"{where}: a recording with nothing saying how it was shown")
            if shown is not None and shown.audio is not None and identity is None:
                partial.append(f"{where}: an audio task with nothing saying which bytes")
            if shown is None and snapshot_lost_its_presentation(
                served_hash=served_hash, bank_hash=bank_hash, bank_presentation=bank_shown
            ):
                partial.append(
                    f"{where}: no presentation, beside an unchanged bank row that has one"
                )
    if partial:
        checks.append(
            _failed(
                "served_presentation_complete",
                "a run holds half of what it showed -- a recording with no presentation, "
                "or an audio presentation with no recording -- which can establish "
                "neither what the learner faced nor that the record predates the column",
                served="; ".join(partial),
            )
        )
    else:
        checks.append(
            _ok(
                "served_presentation_complete",
                "every served presentation and asset identity is whole or wholly absent",
            )
        )
    if malformed:
        checks.append(
            _failed(
                "served_presentation_wellformed",
                "a served record of what the learner was shown cannot be read, so what "
                "they were actually asked can no longer be established from the run",
                served="; ".join(malformed),
            )
        )
    else:
        checks.append(
            _ok(
                "served_presentation_wellformed",
                "every served presentation parses and every asset identity names bytes",
            )
        )
    return checks


def _served_help_allowance_checks(database: Database) -> list[CheckResult]:
    """Migration 0032's column, which no constraint can guard.

    `assessment_tasks.permitted_help` carries `length(permitted_help) > 0`; the served copy
    cannot, because DuckDB refuses a constraint on an added column. So the rule lives here,
    and it is deliberately *one* rule: a single column has no partial state, which is why
    this is not folded into 0030's or 0031's "whole or wholly absent" groups.

    NULL is not damage. It is what every row served before 0032 holds, and reading it as
    blank would report history as a fault -- while reading it as `"none"` would be worse
    still, because "help was refused" and "nobody recorded what help was allowed" are
    different facts about a sitting.
    """

    blank = [
        f"{run_id}/{content_id}"
        for run_id, content_id, raw in database.query(
            "SELECT run_id, content_id, permitted_help FROM assessment_run_tasks "
            "ORDER BY run_id, content_id"
        )
        # Asked in Python, not as `trim(permitted_help) = ''`: DuckDB's `trim` removes
        # spaces where `str.strip` removes every kind of whitespace, so a tab-only value
        # would be damage to the writer and whole to a SQL version of this check.
        if raw is not None and not str(raw).strip()
    ]
    if blank:
        return [
            _failed(
                "served_help_allowance_wellformed",
                "a served task records a help allowance that says nothing, so what the "
                "learner was allowed while answering can no longer be established from "
                "the run",
                served="; ".join(blank),
            )
        ]
    return [
        _ok(
            "served_help_allowance_wellformed",
            "every recorded help allowance names something, or is absent because the row "
            "predates the column",
        )
    ]


def _task_play_checks(database: Database) -> list[CheckResult]:
    """Migration 0033: plays name served recordings, and a count agrees with its rows.

    `(run_id, content_id)` on a play cannot be a foreign key -- run tasks are keyed by
    `(run_id, sequence)` and updated as they are answered -- so the relation is asserted
    here. Membership is established with `NOT EXISTS`, never an inner join, which would
    pass every play with no counterpart.

    A NULL count beside play rows is a mismatch rather than a row to skip: it is a result
    that was scored without the plays it should have been derived from.
    """

    stray = [
        f"{run_id}/{content_id}"
        for run_id, content_id in database.query(
            "SELECT DISTINCT play.run_id, play.content_id FROM assessment_task_plays play "
            "WHERE NOT EXISTS (SELECT 1 FROM assessment_run_tasks task "
            "WHERE task.run_id = play.run_id AND task.content_id = play.content_id "
            "AND task.asset_identity_json IS NOT NULL) ORDER BY 1, 2"
        )
    ]
    disagreeing = [
        f"{run_id}/{content_id} records {count}, rows say {plays}"
        for run_id, content_id, count, plays, played in database.query(
            "SELECT result.run_id, result.content_id, result.play_count, "
            "(SELECT count(*) FROM assessment_task_plays play "
            " WHERE play.run_id = result.run_id AND play.content_id = result.content_id), "
            "EXISTS (SELECT 1 FROM assessment_run_tasks task "
            " WHERE task.run_id = result.run_id AND task.content_id = result.content_id "
            " AND task.asset_identity_json IS NOT NULL) "
            "FROM assessment_results result ORDER BY 1, 2"
        )
        if (count is None and int(plays) > 0)
        or (count is not None and (int(count) != int(plays) or not played))
    ]
    checks: list[CheckResult] = []
    if stray:
        checks.append(
            _failed(
                "task_plays_name_served_audio",
                "a play is recorded against a task its run did not serve with a recording, "
                "so it counts a hearing of nothing",
                plays="; ".join(stray),
            )
        )
    else:
        checks.append(
            _ok(
                "task_plays_name_served_audio",
                "every recorded play names a task its run served with a recording",
            )
        )
    if disagreeing:
        checks.append(
            _failed(
                "result_play_counts_agree",
                "a result's play count disagrees with the plays recorded for its task, so "
                "how often the learner listened can no longer be established",
                results="; ".join(disagreeing),
            )
        )
    else:
        checks.append(
            _ok(
                "result_play_counts_agree",
                "every recorded play count equals the plays it was derived from",
            )
        )
    return checks


def _named(name: str, problems: list[str], *, failed: str, ok: str, field: str) -> CheckResult:
    if problems:
        return _failed(name, failed, **{field: "; ".join(problems[:50])})
    return _ok(name, ok)


def _pack_asset_checks(database: Database) -> list[CheckResult]:
    """Migration 0034: an installed recording is a file the installed pack verified.

    `pack_assets` is what serving reads a digest from instead of loading the pack, so a
    row that disagrees with `pack_files` -- the digests the install verified -- would let a
    serve snapshot a hash nobody checked.
    """

    disagreeing = [
        f"{content_id} at {path}"
        for content_id, path in database.query(
            "SELECT asset.content_id, asset.path FROM pack_assets asset "
            "WHERE NOT EXISTS (SELECT 1 FROM pack_files file WHERE file.pack_id = asset.pack_id "
            "AND file.relative_path = asset.path AND file.sha256 = asset.sha256) ORDER BY 1"
        )
    ]
    return [
        _named(
            "pack_assets_match_installed_files",
            disagreeing,
            failed="an installed recording's digest is not the digest its pack verified, so a "
            "serve would snapshot bytes nobody checked",
            ok="every installed recording matches a file its pack verified",
            field="assets",
        )
    ]


def _recorded_judgement_checks(database: Database, *, submission_kinds: bool) -> list[CheckResult]:
    """Migration 0034: captures, the submissions binding them, and the verdicts on them.

    None of the cross-table rules can be a constraint -- every artifact column is
    unenforced, `status` is mutable, and the result columns were added after the table --
    so each is asserted here, over the data, with membership established by `NOT EXISTS`
    rather than an inner join that would pass every row with no counterpart.
    """

    checks: list[CheckResult] = []
    unresolved = [
        f"{capture_id} ({state})"
        for capture_id, state in database.query(
            "SELECT capture_id, state FROM capture_stagings "
            "WHERE state IN ('staged', 'promoting') ORDER BY 1"
        )
    ]
    if unresolved:
        checks.append(
            _warning(
                "capture_stagings_resolved",
                "a capture is neither registered nor refused; `client serve` recovers it "
                "before it accepts a request",
                captures="; ".join(unresolved[:50]),
            )
        )
    registered = [
        str(capture_id)
        for (capture_id,) in database.query(
            "SELECT staging.capture_id FROM capture_stagings staging "
            "WHERE staging.state = 'registered' AND ("
            "  NOT EXISTS (SELECT 1 FROM artifacts artifact "
            "    WHERE artifact.artifact_id = staging.artifact_id "
            "    AND artifact.track_id = staging.track_id AND artifact.kind = 'audio' "
            "    AND artifact.sha256 = staging.sha256) "
            "  OR NOT EXISTS (SELECT 1 FROM assessment_submissions submission "
            "    WHERE submission.capture_id = staging.capture_id "
            "    AND submission.artifact_id = staging.artifact_id "
            "    AND submission.run_id = staging.run_id "
            "    AND submission.content_id = staging.content_id)) ORDER BY 1"
        )
    ]
    if not unresolved:
        checks.append(
            _named(
                "capture_stagings_resolved",
                registered,
                failed="a capture is recorded as registered, and its artifact or its "
                "submission does not say so -- the three are written in one transaction, so "
                "this is damage rather than a crash",
                ok="every capture is registered with its artifact and submission, or refused",
                field="captures",
            )
        )
    elif registered:
        checks.append(
            _failed(
                "capture_registrations_agree",
                "a capture is recorded as registered, and its artifact or its submission does "
                "not say so",
                captures="; ".join(registered[:50]),
            )
        )
    doubled = [
        f"{run_id}/{content_id} ({count})"
        for run_id, content_id, count in database.query(
            "SELECT run_id, content_id, count(*) FROM assessment_submissions "
            "WHERE status <> 'superseded' GROUP BY run_id, content_id HAVING count(*) > 1 "
            "ORDER BY 1, 2"
        )
    ]
    checks.append(
        _named(
            "one_live_submission_per_task",
            doubled,
            failed="a served task has more than one submission that is not superseded, so "
            "which recording answers it cannot be said",
            ok="every served task has at most one submission that is not superseded",
            field="tasks",
        )
    )
    unnamed = [
        str(submission_id)
        for (submission_id,) in database.query(
            "SELECT submission.submission_id FROM assessment_submissions submission "
            "WHERE submission.status = 'superseded' AND NOT EXISTS ("
            "  SELECT 1 FROM assessment_submissions successor "
            "  WHERE successor.submission_id = submission.superseded_by "
            "  AND successor.run_id = submission.run_id "
            "  AND successor.content_id = submission.content_id "
            "  AND successor.submission_id <> submission.submission_id) ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "submission_supersession_named",
            unnamed,
            failed="a superseded submission does not name a successor answering the same task",
            ok="every superseded submission names its successor for the same task",
            field="submissions",
        )
    )
    # From 0035 a submission may be a written answer, which names no recording: the rules
    # about the artifact apply to recordings, and the rules about the task to both.
    recording = "submission.kind = 'recording'" if submission_kinds else "TRUE"
    disagreeing = [
        f"{submission_id} ({reason})"
        for submission_id, reason in database.query(
            "SELECT submission.submission_id, CASE "
            f"  WHEN {recording} AND artifact.artifact_id IS NULL THEN 'names no artifact' "
            f"  WHEN {recording} AND artifact.track_id <> run.track_id "
            "    THEN 'another track''s recording' "
            f"  WHEN {recording} AND artifact.kind <> 'audio' THEN 'not audio' "
            f"  WHEN {recording} AND submission.status = 'pending' "
            "    AND (artifact.purged_at IS NOT NULL OR NOT artifact.retained) "
            "    THEN 'pending on a recording that is gone' "
            "  WHEN submission.status = 'pending' AND coalesce(task.status, '') <> 'served' "
            "    THEN 'pending on a task that is not outstanding' "
            "  WHEN submission.status = 'withdrawn' AND coalesce(task.status, '') = 'served' "
            "    THEN 'withdrawn while its task is still outstanding' "
            f"  WHEN {recording} AND submission.status = 'judged' AND NOT EXISTS (SELECT 1 FROM "
            "    assessment_results result WHERE result.run_id = submission.run_id "
            "    AND result.content_id = submission.content_id "
            "    AND result.audio_artifact_id = submission.artifact_id) "
            "    THEN 'judged with no result resting on it' "
            "  END AS reason "
            "FROM assessment_submissions submission "
            "JOIN assessment_runs run ON run.run_id = submission.run_id "
            "LEFT JOIN artifacts artifact ON artifact.artifact_id = submission.artifact_id "
            "LEFT JOIN assessment_run_tasks task ON task.run_id = submission.run_id "
            "  AND task.content_id = submission.content_id "
            "WHERE reason IS NOT NULL ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "submission_artifacts_agree",
            disagreeing,
            failed="a submission and the recording or task it names disagree",
            ok="every submission names its own track's recording, in a state its task agrees with",
            field="submissions",
        )
    )
    outlived = [
        f"{result_id} ({reason})"
        for result_id, reason in database.query(
            "SELECT result.result_id, CASE "
            "  WHEN artifact.artifact_id IS NULL THEN 'names no artifact' "
            "  WHEN artifact.track_id <> run.track_id THEN 'another track''s recording' "
            "  WHEN artifact.kind <> 'audio' THEN 'not audio' "
            "  WHEN result.invalidated_at IS NULL AND (artifact.purged_at IS NOT NULL "
            "    OR NOT artifact.retained) THEN 'standing on a recording that is gone' "
            "  WHEN NOT EXISTS (SELECT 1 FROM assessment_submissions submission "
            "    WHERE submission.run_id = result.run_id "
            "    AND submission.content_id = result.content_id "
            "    AND submission.artifact_id = result.audio_artifact_id "
            "    AND submission.status = 'judged') THEN 'no judged submission binds it' "
            "  END AS reason "
            "FROM assessment_results result "
            "JOIN assessment_runs run ON run.run_id = result.run_id "
            "LEFT JOIN artifacts artifact ON artifact.artifact_id = result.audio_artifact_id "
            "WHERE result.audio_artifact_id IS NOT NULL AND reason IS NOT NULL ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "results_rest_on_their_recording",
            outlived,
            failed="an assessment result outlived, or never had, the recording it rests on",
            ok="every judged result rests on its own submitted recording, or is invalidated",
            field="results",
        )
    )
    half = [
        str(result_id)
        for (result_id,) in database.query(
            "SELECT result_id FROM assessment_results "
            "WHERE (invalidated_at IS NULL) <> (invalidated_reason IS NULL) "
            "OR (invalidated_at IS NOT NULL AND audio_artifact_id IS NULL) "
            "OR (invalidated_reason IS NOT NULL AND length(trim(invalidated_reason)) = 0) "
            "ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "result_invalidation_complete",
            half,
            failed="a result is half invalidated -- a moment without a reason, a reason "
            "without a moment, or invalidated with no recording to have lost",
            ok="every invalidated result says when and why, and names the recording it lost",
            field="results",
        )
    )
    # Asked of the policy itself, never restated here: a second encoding of the rule in SQL
    # is a second rule, and the first time the two disagree the check reports damage the
    # service accepted, or passes what it refused.
    unjudged: list[str] = []
    for (
        result_id,
        conditions,
        dimension,
        modality,
        kind,
        assessor,
        confidence,
        version,
    ) in database.query(
        "SELECT result.result_id, run.conditions_json, result.dimension, task.modality, "
        "result.assessor_kind, result.assessor, result.confidence, "
        "result.judgement_policy_version FROM assessment_results result "
        "JOIN assessment_runs run ON run.run_id = result.run_id "
        "LEFT JOIN assessment_run_tasks task ON task.run_id = result.run_id "
        "AND task.content_id = result.content_id "
        "WHERE result.audio_artifact_id IS NOT NULL ORDER BY 1"
    ):
        try:
            kinds = json.loads(str(conditions)).get("dimension_kinds") or {}
        except (ValueError, AttributeError, RecursionError):
            kinds = {}
        dimension_kind = kinds.get(str(dimension)) if isinstance(kinds, dict) else None
        if dimension_kind is None or modality is None:
            unjudged.append(f"{result_id} (the run cannot say what kind of task it judged)")
            continue
        if str(kind) not in evidence_policy.JUDGING_ASSESSORS:
            unjudged.append(f"{result_id} (judged from a recording by a {kind} assessor)")
            continue
        try:
            expected = evidence_policy.assert_judged_claim(
                dimension_kind=str(dimension_kind),
                modality=str(modality),
                assessor_kind=str(kind),
                assessor=None if assessor is None else str(assessor),
                confidence=str(confidence),
            )
        except LinguaWikiError as failure:
            unjudged.append(f"{result_id} ({failure.payload.code})")
            continue
        if (None if version is None else str(version)) != expected:
            unjudged.append(f"{result_id} (records policy {version}, the rule says {expected})")
    checks.append(
        _named(
            "judged_claims_within_policy",
            unjudged,
            failed="a verdict on a recording claims more than its judge may, or does not say "
            "which rule decided it",
            ok="every verdict on a recording names its judge and stays within its ceiling",
            field="results",
        )
    )
    return checks


#: The migration that introduced `assessment_results.observed_at`. A result recorded
#: before it was applied never had the column; one recorded after it always does.
OBSERVATION_TIME_MIGRATION = 35


def _submission_lifecycle_checks(database: Database) -> list[CheckResult]:
    """Migration 0035: a submission's kind, and the verdicts and outcomes that judge it.

    Each rule a CHECK on the rebuilt table is re-asserted here, for a restore or a hand
    repair that predates it, and the cross-table rules -- which no constraint can express,
    because every relation naming a submission, a claim, a verdict, or a result is
    unenforced -- are asserted over the data. Membership is established with `NOT EXISTS`
    or a `LEFT JOIN` that reports the missing side, never an inner join that would pass a
    row with no counterpart.
    """

    checks: list[CheckResult] = []
    misshapen = [
        f"{submission_id} ({reason})"
        for submission_id, reason in database.query(
            "SELECT submission_id, CASE "
            "  WHEN kind IS NULL OR kind NOT IN ('recording', 'text') "
            "    THEN 'kind ' || coalesce(kind, 'is missing') "
            "  WHEN length(trim(coalesce(capture_id, ''))) = 0 THEN 'no producer identifier' "
            # 0035 holds these in the one CHECK it also holds the kind rules in, so a
            # restore that predates the table loses them together; they are re-asserted
            # together.
            "  WHEN (status = 'superseded') IS DISTINCT FROM (superseded_by IS NOT NULL) "
            "    THEN 'superseded exactly when it names a successor, and it does not' "
            "  WHEN (status = 'withdrawn') IS DISTINCT FROM "
            "    (withdrawn_code IS NOT NULL AND withdrawn_reason IS NOT NULL) "
            "    THEN 'withdrawn exactly when it gives a code and a reason, and it does not' "
            "  WHEN kind = 'recording' AND artifact_id IS NULL THEN 'a recording with no artifact' "
            "  WHEN kind = 'text' AND artifact_id IS NOT NULL THEN 'a written answer naming an "
            "artifact' "
            "  WHEN kind = 'recording' AND (response_visibility IS NOT NULL "
            "    OR response_text IS NOT NULL OR response_digest IS NOT NULL) "
            "    THEN 'a recording carrying text' "
            "  WHEN kind = 'text' AND (response_visibility IS NULL OR response_digest IS NULL) "
            "    THEN 'a written answer with no retained form' "
            "  WHEN kind = 'text' AND response_text IS NULL AND status <> 'withdrawn' "
            "    THEN 'a written answer whose text is gone while it still waits' "
            "  WHEN response_visibility NOT IN ('withheld', 'excerpt', 'full') "
            "    THEN 'response_visibility ' || response_visibility "
            "  WHEN response_visibility = 'withheld' AND response_text IS NOT NULL "
            "    THEN 'withheld, and keeps the text' "
            "  WHEN response_text IS NOT NULL AND length(trim(response_text)) = 0 "
            "    THEN 'blank text' "
            "  WHEN response_digest IS NOT NULL AND length(response_digest) <> 64 "
            "    THEN 'a digest that is not a sha256' "
            "  END AS problem "
            "FROM assessment_submissions WHERE problem IS NOT NULL ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "submission_kind_shape",
            misshapen,
            failed="a submission does not have the shape of its kind or its status: a "
            "recording names its artifact and carries no text, a written answer carries its "
            "retained text and none, and a superseded or withdrawn one says why",
            ok="every submission has the shape of its kind and its status",
            field="submissions",
        )
    )
    doubled = [
        f"{submission_id} ({count})"
        for submission_id, count in database.query(
            "SELECT verdict.submission_id, count(*) FROM assessment_verdict_outcomes outcome "
            "JOIN assessment_verdicts verdict ON verdict.verdict_id = outcome.verdict_id "
            "WHERE outcome.outcome = 'applied' GROUP BY verdict.submission_id "
            "HAVING count(*) > 1 ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "one_applied_verdict_per_submission",
            doubled,
            failed="a submission has more than one applied verdict, so one observation was "
            "credited more than once",
            ok="every submission has at most one applied verdict",
            field="submissions",
        )
    )
    unmatched = [
        f"{verdict_id} ({reason})"
        for verdict_id, reason in database.query(
            "SELECT outcome.verdict_id, CASE "
            "  WHEN outcome.result_id IS NULL THEN 'applied to no result' "
            "  WHEN result.result_id IS NULL THEN 'its result does not exist' "
            "  WHEN submission.submission_id IS NULL THEN 'its verdict names no submission' "
            "  WHEN result.run_id <> submission.run_id "
            "    OR result.content_id <> submission.content_id "
            "    THEN 'its result answers another task' "
            "  WHEN submission.kind = 'recording' "
            "    AND result.audio_artifact_id IS DISTINCT FROM submission.artifact_id "
            "    THEN 'its result rests on another recording' "
            # A written answer is read, not heard: a result judged from one that names a
            # recording claims acoustic evidence nobody submitted, and a purge of that
            # recording would invalidate a judgement of writing.
            "  WHEN submission.kind = 'text' AND result.audio_artifact_id IS NOT NULL "
            "    THEN 'a written answer''s result rests on a recording' "
            "  WHEN (SELECT count(*) FROM assessment_verdict_outcomes other "
            "    WHERE other.outcome = 'applied' AND other.result_id = outcome.result_id) > 1 "
            "    THEN 'its result is claimed by another verdict too' "
            "  END AS problem "
            "FROM assessment_verdict_outcomes outcome "
            "LEFT JOIN assessment_verdicts verdict ON verdict.verdict_id = outcome.verdict_id "
            "LEFT JOIN assessment_submissions submission "
            "  ON submission.submission_id = verdict.submission_id "
            "LEFT JOIN assessment_results result ON result.result_id = outcome.result_id "
            "WHERE outcome.outcome = 'applied' AND problem IS NOT NULL ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "applied_verdicts_name_their_result",
            unmatched,
            failed="an applied verdict names a result that is missing, or that answers "
            "something other than its submission",
            ok="every applied verdict names its own submission's result",
            field="verdicts",
        )
    )
    # 0035's single-column CHECK on `requested_visibility`, re-asserted for a restore that
    # predates it, and R10's rule that a written answer's verdict keeps none of its words:
    # the row is insert-only, so a copy there is one no consent withdrawal can reach.
    #
    # R19 widens that to every kind: a verdict keeps a learner excerpt only while it is the
    # excerpt's sole copy, which is a held recording verdict before resume. One applied in
    # the transaction that received it keeps none -- its result holds the retained form.
    # The insert-only row cannot drop an excerpt later, so a held verdict applied at resume,
    # or voided, still has one, and is allowed it. The rule that tells the two apart: an
    # immediate apply writes `received_at` and the outcome's `decided_at` from the one
    # write-time instant, so they are equal; a resume is a later transaction, so a verdict
    # that was held has `received_at` strictly before `decided_at`. An excerpt on an applied
    # verdict whose times are equal (or reversed) is therefore one nobody needed to keep.
    misshapen_verdicts = [
        f"{verdict_id} ({reason})"
        for verdict_id, reason in database.query(
            "SELECT verdict.verdict_id, CASE "
            "  WHEN verdict.requested_visibility IS NOT NULL "
            "    AND verdict.requested_visibility NOT IN ('withheld', 'excerpt', 'full') "
            "    THEN 'requested_visibility ' || verdict.requested_visibility "
            "  WHEN submission.kind = 'text' AND (verdict.response_excerpt IS NOT NULL "
            "    OR verdict.response_visibility IS DISTINCT FROM 'withheld') "
            "    THEN 'a written answer''s verdict keeps some of its words' "
            "  WHEN verdict.response_excerpt IS NOT NULL AND outcome.outcome = 'applied' "
            "    AND NOT (verdict.received_at < outcome.decided_at) "
            "    THEN 'applied when it arrived, and keeps an excerpt its result already holds' "
            "  END AS problem "
            "FROM assessment_verdicts verdict "
            "LEFT JOIN assessment_submissions submission "
            "  ON submission.submission_id = verdict.submission_id "
            "LEFT JOIN assessment_verdict_outcomes outcome "
            "  ON outcome.verdict_id = verdict.verdict_id "
            "WHERE problem IS NOT NULL ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "verdict_response_shape",
            misshapen_verdicts,
            failed="a verdict asks for a visibility outside the vocabulary, or keeps some of "
            "the learner's words where it is not their only copy: a written answer's verdict "
            "never may (the submission holds them), and a verdict applied when it arrived "
            "never may (its result holds them); only a recording verdict that was held may",
            ok="every verdict's request is known, and only a verdict that was held keeps the "
            "learner's words",
            field="verdicts",
        )
    )
    # A held verdict waits for a paused run to resume. On a run in any other state it can
    # never be applied, and nothing would ever say what became of it.
    stranded = [
        f"{verdict_id} ({reason})"
        for verdict_id, reason in database.query(
            "SELECT verdict.verdict_id, CASE "
            "  WHEN submission.submission_id IS NULL THEN 'names no submission' "
            "  WHEN run.run_id IS NULL THEN 'its submission names no run' "
            "  WHEN run.status <> 'paused' THEN 'held on a run that is ' || run.status "
            "  END AS problem "
            "FROM assessment_verdicts verdict "
            "LEFT JOIN assessment_submissions submission "
            "  ON submission.submission_id = verdict.submission_id "
            "LEFT JOIN assessment_runs run ON run.run_id = submission.run_id "
            "WHERE NOT EXISTS (SELECT 1 FROM assessment_verdict_outcomes outcome "
            "  WHERE outcome.verdict_id = verdict.verdict_id) "
            "AND problem IS NOT NULL ORDER BY 1"
        )
    ]
    checks.append(
        _named(
            "held_verdicts_on_paused_runs",
            stranded,
            failed="a verdict is held on a run that is not paused, so nothing will ever apply "
            "or void it",
            ok="every held verdict waits on a paused run",
            field="verdicts",
        )
    )
    unaccounted = [
        str(submission_id)
        for (submission_id,) in database.query(
            "SELECT submission.submission_id FROM assessment_submissions submission "
            "WHERE submission.status = 'judged' AND NOT EXISTS ("
            "  SELECT 1 FROM assessment_verdicts verdict "
            "  JOIN assessment_verdict_outcomes outcome "
            "    ON outcome.verdict_id = verdict.verdict_id "
            "  WHERE verdict.submission_id = submission.submission_id "
            "  AND outcome.outcome = 'applied') ORDER BY 1"
        )
    ]
    # A submission whose every judging attempt ended without a verdict is withdrawn by the
    # next writer that touches its run. Nothing runs in the background, so a workspace
    # nobody has touched since says so here -- by the same question the writers ask, so
    # the two cannot disagree about what "lapsed" means.
    from linguawiki.services import judging

    # A run that is finalized or abandoned holds no pending submission -- closing it
    # withdraws them all -- so one there is reported once, by the check below, whose remedy
    # works on a closed run. Every command named here refuses one.
    open_runs = {
        str(run_id)
        for (run_id,) in database.query(
            "SELECT run_id FROM assessment_runs WHERE status IN ('in-progress', 'paused')"
        )
    }
    lapsed = [
        f"{entry.submission_id} (run {entry.run_id}, {entry.attempts} attempt(s), none live)"
        for entry in judging.lapsed_submissions(database, run_id=None)
        if entry.run_id in open_runs
    ]
    checks.append(
        _named(
            "lapsed_judging_settled",
            lapsed,
            failed="a pending submission has used every judging attempt the policy allows and "
            "none is still live, so nobody may claim it and it holds its dimension; the next "
            "command that writes to its run -- serving a task, `assessment claim`, `record`, "
            "`release`, pausing or resuming, or `finalize` -- withdraws it",
            ok="no pending submission has run out of judging attempts unsettled",
            field="submissions",
        )
    )
    # Closing a run settles everything it still owes: abandoning withdraws every pending
    # submission, and finalizing either refuses or withdraws them. One left pending on a
    # closed run can never be judged -- every judging command refuses a closed run -- and
    # nothing would ever say what became of it. A run closed before C6 could leave one.
    stranded_rows = [
        (str(submission_id), str(kind), str(run_id), str(status))
        for submission_id, kind, run_id, status in database.query(
            "SELECT submission.submission_id, submission.kind, submission.run_id, "
            "coalesce(run.status, 'missing') FROM assessment_submissions submission "
            "LEFT JOIN assessment_runs run ON run.run_id = submission.run_id "
            "WHERE submission.status = 'pending' "
            "AND (run.run_id IS NULL OR run.status NOT IN ('in-progress', 'paused')) "
            "ORDER BY 1"
        )
    ]
    stranded_pending = [
        f"{submission_id} ({kind} on run {run_id}, which is {status})"
        for submission_id, kind, run_id, status in stranded_rows
    ]
    # A remedy per kind, and only for the kinds present: `artifact purge` reaches a
    # recording and nothing else, and naming it for a written answer would send an operator
    # to a command that cannot help. A written answer is withdrawn -- its text cleared --
    # by withdrawing transcript retention consent on its track, which settles every
    # pending written answer the track has, whatever its run's state.
    stranded_kinds = {kind for _, kind, _, _ in stranded_rows}
    remedies = []
    if "recording" in stranded_kinds:
        remedies.append("`artifact purge` of its recording withdraws a recorded one")
    if "text" in stranded_kinds:
        remedies.append(
            "setting transcript_retention_consent to false on its track (`track update "
            "--input`) withdraws a written one and clears its text"
        )
    checks.append(
        _named(
            "no_pending_submission_on_a_closed_run",
            stranded_pending,
            failed="a submission still waits for a judge on a run that is closed, so no "
            "verdict can ever land on it; "
            + ("; ".join(remedies) or "nothing in this release withdraws it")
            + ", with its reason on the row",
            ok="no submission waits for a judge on a closed run",
            field="submissions",
        )
    )
    checks.append(
        _named(
            "judged_submissions_have_an_applied_verdict",
            unaccounted,
            failed="a submission is judged and no applied verdict says by whom or with what",
            ok="every judged submission has the applied verdict that judged it",
            field="submissions",
        )
    )
    # A batch's membership is what a retry of its key is answered with, and each member is
    # reported in its run task's current state. The foreign key reaches the *bank* row, not
    # the run's record of serving it, so a member its own run never served -- a restore, a
    # hand repair -- would be answered with no state at all. Membership is established with
    # a LEFT JOIN, so a member with no counterpart is a finding rather than a row to skip.
    unserved_members = [
        f"{batch_id} position {position} ({content_id} in {dimension})"
        for batch_id, position, content_id, dimension in database.query(
            "SELECT member.batch_id, member.position, member.content_id, member.dimension "
            "FROM assessment_batch_tasks member "
            "LEFT JOIN assessment_batches batch ON batch.batch_id = member.batch_id "
            "LEFT JOIN assessment_run_tasks served ON served.run_id = batch.run_id "
            "  AND served.content_id = member.content_id "
            "  AND served.dimension = member.dimension "
            "WHERE served.content_id IS NULL ORDER BY 1, 2"
        )
    ]
    checks.append(
        _named(
            "batch_members_were_served_by_their_run",
            unserved_members,
            failed="a batch names a task its run has no record of serving in that dimension, "
            "so a retry of the batch's key cannot say where the task stands; serve with a new "
            "key, and restore the run's tasks from a backup if they were lost",
            ok="every batch member is a task its run served, in the dimension it names",
            field="batches",
        )
    )
    # When the learner answered. The rule, by what a result is:
    #
    # - bound to a submission (an applied verdict names it): the submission's `created_at`,
    #   however long the verdict took to arrive;
    # - any other result: its own `recorded_at`, because answering and scoring were one act;
    # - NULL only on a result older than migration 0035, which is a result recorded at or
    #   before the moment 0035 was applied. `schema_migrations.applied_at` is that moment,
    #   and it survives an export and a restore with the rows it dates.
    #
    # NULL is an answer here and never a row to skip: a result written after 0035 with no
    # observation time is precisely the one every reader of the column would misdate.
    misdated = [
        f"{result_id} ({reason})"
        for result_id, reason in database.query(
            "SELECT DISTINCT result.result_id, CASE "
            "  WHEN result.observed_at IS NULL AND (migrated.applied_at IS NULL "
            "    OR result.recorded_at > migrated.applied_at) "
            "    THEN 'recorded after migration 0035 with no observation time' "
            "  WHEN result.observed_at IS NULL THEN NULL "
            "  WHEN bound.created_at IS NOT NULL AND result.observed_at <> bound.created_at "
            "    THEN 'observed_at is not when its submission was made' "
            "  WHEN bound.created_at IS NULL AND result.observed_at <> result.recorded_at "
            "    THEN 'observed_at is not when it was recorded' "
            "  END AS problem "
            "FROM assessment_results result "
            "LEFT JOIN (SELECT applied_at FROM schema_migrations WHERE version = ?) migrated "
            "  ON TRUE "
            "LEFT JOIN (SELECT outcome.result_id, submission.created_at "
            "  FROM assessment_verdict_outcomes outcome "
            "  JOIN assessment_verdicts verdict ON verdict.verdict_id = outcome.verdict_id "
            "  JOIN assessment_submissions submission "
            "    ON submission.submission_id = verdict.submission_id "
            "  WHERE outcome.outcome = 'applied') bound ON bound.result_id = result.result_id "
            "WHERE problem IS NOT NULL ORDER BY 1",
            [OBSERVATION_TIME_MIGRATION],
        )
    ]
    checks.append(
        _named(
            "result_observation_times",
            misdated,
            failed="a result's observation time is missing or is not when the learner answered",
            ok="every result since migration 0035 says when the learner answered",
            field="results",
        )
    )
    return checks


def _served_answer_key_checks(database: Database) -> list[CheckResult]:
    """Migration 0030's snapshot group, and the provenance of every score that followed.

    None of it can be a constraint. DuckDB refuses `ALTER TABLE ... ADD COLUMN` with one,
    so `expected_json` and `rubric_json` carry no `json_valid` and `score_source` and
    `response_visibility` carry no vocabulary check. The rules therefore live here, and
    each one parses defensively: a diagnostic that aborts on damaged input tells an
    operator less than one that names the rows that will not parse.

    0016's snapshot group and this one are independent. A row may legitimately be whole in
    one and absent in the other -- a task served between the two migrations is exactly
    that -- which is why this is a second named check rather than a widening of
    `served_snapshot_complete`.
    """

    checks: list[CheckResult] = []
    # Asked in Python through the one predicate `_scorable_key` uses, not as a SQL
    # expression. A SQL version is how this diverged: DuckDB's `trim` removes spaces where
    # `str.strip` removes every kind of whitespace, so a prompt of "\t\n" was damage to
    # the writer and whole to this check. One function answers the question.
    partial = [
        f"{run_id}/{content_id}"
        for run_id, content_id, expected_json, prompt_snapshot, rubric_json in database.query(
            "SELECT run_id, content_id, expected_json, prompt_snapshot, rubric_json "
            "FROM assessment_run_tasks ORDER BY run_id, content_id"
        )
        if served_snapshot_state((expected_json, prompt_snapshot, rubric_json)) == SNAPSHOT_PARTIAL
    ]
    if partial:
        checks.append(
            _failed(
                "served_answer_key_complete",
                "a run holds part of the material it served -- the answer key, the prompt, "
                "or the rubric body but not all three -- which can establish neither what "
                "the learner was asked nor that the record predates the snapshot",
                served="; ".join(partial),
            )
        )
    else:
        checks.append(
            _ok(
                "served_answer_key_complete",
                "every served answer key, prompt, and rubric is whole or wholly absent",
            )
        )
    # What "well formed" means depends on what the task is scored by, and asking the
    # stronger question of every row made this fire on healthy data: a rubric-scored task
    # has no answer key by design and snapshots `{}`, so demanding a usable key reported
    # every real calibration as damaged. A check that fires on correct data is worse than
    # no check, because it teaches an operator to ignore it. The task type is on the same
    # row (migration 0016), so the check asks what the row itself says it needs.
    malformed: list[str] = []
    for run_id, content_id, task_type, expected_json, rubric_json in database.query(
        "SELECT run_id, content_id, task_type, expected_json, rubric_json "
        "FROM assessment_run_tasks "
        "WHERE expected_json IS NOT NULL OR rubric_json IS NOT NULL "
        "ORDER BY run_id, content_id"
    ):
        scorable = task_type is not None and str(task_type) in MACHINE_SCORABLE_TASK_TYPES
        if expected_json is not None:
            if scorable and not _reads_as_answer_key(str(expected_json)):
                malformed.append(f"{run_id}/{content_id}: the answer key cannot be read")
            elif not scorable and not reads_as_json_object(str(expected_json)):
                malformed.append(f"{run_id}/{content_id}: the answer key is not a JSON object")
        if rubric_json is not None and not reads_as_json_object(str(rubric_json)):
            malformed.append(f"{run_id}/{content_id}: the rubric body is not a JSON object")
    if malformed:
        checks.append(
            _failed(
                "served_answer_key_wellformed",
                "a served record's answer key or rubric body cannot be read, so the task "
                "it describes cannot be scored from the account the run actually kept",
                served="; ".join(malformed),
            )
        )
    else:
        checks.append(
            _ok(
                "served_answer_key_wellformed",
                "every served answer key parses and every served rubric body is an object",
            )
        )
    # Both directions. A version recorded beside a supplied score is a claim that work
    # nobody did had been done, and is exactly as wrong as a computed score with none.
    provenance = [
        f"{result_id}: {reason}"
        for result_id, reason in database.query(
            "SELECT result_id, 'score_source ' || coalesce(score_source, 'is missing') "
            "FROM assessment_results WHERE score_source IS NULL "
            "  OR score_source NOT IN ('computed', 'supplied') "
            "UNION ALL "
            "SELECT result_id, 'a computed score records no scoring policy version' "
            "FROM assessment_results "
            "WHERE score_source = 'computed' AND scoring_policy_version IS NULL "
            "UNION ALL "
            "SELECT result_id, 'a supplied score records a scoring policy version' "
            "FROM assessment_results "
            "WHERE score_source = 'supplied' AND scoring_policy_version IS NOT NULL "
            "ORDER BY 1, 2"
        )
    ]
    if provenance:
        checks.append(
            _failed(
                "result_score_provenance",
                "a result does not say who reached its score, or claims a scoring policy "
                "ran where a caller supplied the verdict instead",
                results="; ".join(provenance),
            )
        )
    else:
        checks.append(
            _ok(
                "result_score_provenance",
                "every score says whether a policy or a caller reached it",
            )
        )
    retention = [
        f"{result_id}: {reason}"
        for result_id, reason in database.query(
            "SELECT result_id, 'response_visibility ' "
            "  || coalesce(response_visibility, 'is missing') "
            "FROM assessment_results WHERE response_visibility IS NULL "
            "  OR response_visibility NOT IN ('withheld', 'excerpt', 'full') "
            "UNION ALL "
            "SELECT result_id, response_visibility || ' keeps no text' "
            "FROM assessment_results "
            "WHERE response_visibility IN ('excerpt', 'full') AND response_excerpt IS NULL "
            "UNION ALL "
            "SELECT result_id, 'a withheld response kept its text anyway' "
            "FROM assessment_results "
            "WHERE response_visibility = 'withheld' AND response_excerpt IS NOT NULL "
            "ORDER BY 1, 2"
        )
    ]
    if retention:
        checks.append(
            _failed(
                "result_response_retention",
                "a result's stored response disagrees with what it says it kept, so a "
                "learner's retention decision cannot be read off the row it governs",
                results="; ".join(retention),
            )
        )
    else:
        checks.append(
            _ok("result_response_retention", "every result keeps exactly what it says it kept")
        )
    return checks


def _session_checks(database: Database, *, present: frozenset[str]) -> list[CheckResult]:
    """Whether the session engine's promises survived whatever happened to this file.

    Every one of these was checkable only at write time before, which means a restore, a
    partial recovery, or a build under a looser rule could leave a session that no
    command would produce -- and the whole point of the close boundary is that "the
    learner's state changed exactly once" is a property of the data rather than of the
    order somebody called things in.
    """

    checks: list[CheckResult] = []
    # 1. A finished session has exactly one close, and an unfinished one has none.
    mismatched = [
        f"{session_id} is {status} with {count} finalization(s)"
        for session_id, status, count in database.query(
            "SELECT session.session_id, session.status, count(final.finalization_id) "
            "FROM sessions session "
            "LEFT JOIN session_finalizations final ON final.session_id = session.session_id "
            "GROUP BY session.session_id, session.status "
            "HAVING (session.status IN ('completed', 'partial') "
            "        AND count(final.finalization_id) <> 1) "
            "    OR (session.status NOT IN ('completed', 'partial') "
            "        AND count(final.finalization_id) > 0) "
            "ORDER BY session.session_id"
        )
    ]
    if mismatched:
        checks.append(
            _failed(
                "session_finalization_pairing",
                "a session's status and its close do not agree: a completed or partial "
                "session is closed exactly once, and anything else is not closed at all",
                sessions="; ".join(mismatched[:20]),
            )
        )
    else:
        checks.append(
            _ok("session_finalization_pairing", "every finished session has exactly one close")
        )
    # 2. A close's recorded outcome is the status the session ended in.
    disagreeing = [
        f"{session_id}: session says {status}, close says {outcome}"
        for session_id, status, outcome in database.query(
            "SELECT session.session_id, session.status, final.outcome FROM sessions session "
            "JOIN session_finalizations final ON final.session_id = session.session_id "
            "WHERE session.status <> final.outcome ORDER BY session.session_id"
        )
    ]
    if disagreeing:
        checks.append(
            _failed(
                "session_outcome_agreement",
                "a session's status disagrees with the outcome its close recorded",
                sessions="; ".join(disagreeing[:20]),
            )
        )
    else:
        checks.append(
            _ok("session_outcome_agreement", "every close's outcome is the session's status")
        )
    # 3. Flush sequences are gap-free. A missing batch means a lost flush, and a session
    #    credited without it reports work the learner did as absent.
    gaps: list[str] = []
    for session_id, batches, highest in database.query(
        "SELECT session_id, count(*), max(sequence) FROM session_event_batches "
        "GROUP BY session_id ORDER BY session_id"
    ):
        if int(batches) != int(highest):
            gaps.append(f"{session_id}: {batches} batch(es) up to sequence {highest}")
    if gaps:
        checks.append(
            _failed(
                "session_batch_sequence",
                "a session's flush sequence has a gap, so at least one batch of "
                "observations is missing from it",
                sessions="; ".join(gaps[:20]),
            )
        )
    else:
        checks.append(
            _ok("session_batch_sequence", "every session's flushes are numbered without gaps")
        )
    # 4. One staged event became at most one durable row. This is the "exactly once"
    #    promise, stated as data: two staged rows naming the same attempt would mean one
    #    observation credited twice.
    duplicated = [
        f"{kind} {materialized_id} claimed by {count} staged events"
        for kind, materialized_id, count in database.query(
            "SELECT materialized_kind, materialized_id, count(*) FROM session_staged_events "
            "WHERE status = 'materialized' GROUP BY materialized_kind, materialized_id "
            "HAVING count(*) > 1 ORDER BY materialized_id"
        )
    ]
    if duplicated:
        checks.append(
            _failed(
                "staged_event_materialized_once",
                "one durable row is claimed by more than one staged event, so an "
                "observation was credited twice",
                rows="; ".join(duplicated[:20]),
            )
        )
    else:
        checks.append(
            _ok("staged_event_materialized_once", "every materialized row has one staged event")
        )
    # 5. A materialized staged event names a close that exists, and one that belongs to
    #    its own session.
    stranded = [
        f"{staged_event_id} names {finalization_id}"
        for staged_event_id, finalization_id in database.query(
            "SELECT staged.staged_event_id, staged.finalization_id "
            "FROM session_staged_events staged "
            "LEFT JOIN session_finalizations final "
            "  ON final.finalization_id = staged.finalization_id "
            " AND final.session_id = staged.session_id "
            "WHERE staged.status = 'materialized' AND final.finalization_id IS NULL "
            "ORDER BY staged.staged_event_id"
        )
    ]
    if stranded:
        checks.append(
            _failed(
                "staged_event_finalization",
                "a materialized staged event names a close that does not exist, or one "
                "that closed a different session",
                events="; ".join(stranded[:20]),
            )
        )
    else:
        checks.append(
            _ok("staged_event_finalization", "every materialized staged event names its own close")
        )
    # 6. A finished session leaves nothing merely staged: it was materialized, discarded,
    #    or rejected, and each of those is a decision somebody can read. `abandoned` is
    #    excluded on purpose -- it keeps its staged work for audit, which is its point.
    unresolved = [
        f"{session_id}: {count} staged event(s)"
        for session_id, count in database.query(
            "SELECT staged.session_id, count(*) FROM session_staged_events staged "
            "JOIN sessions session ON session.session_id = staged.session_id "
            "WHERE session.status IN ('completed', 'partial') AND staged.status = 'staged' "
            "GROUP BY staged.session_id ORDER BY staged.session_id"
        )
    ]
    if unresolved:
        checks.append(
            _failed(
                "closed_session_staging_resolved",
                "a closed session still holds staged events, so its close did not decide "
                "what to do with all of them",
                sessions="; ".join(unresolved[:20]),
            )
        )
    else:
        checks.append(
            _ok("closed_session_staging_resolved", "no closed session holds unresolved staging")
        )
    # 7. A session's blocks, batches, and packages belong to its own track. An attempt
    #    materialized from one learner's session must not sit in another's model, and a
    #    restore is exactly where that could otherwise happen.
    crossed = [
        f"{attempt_id} on {attempt_track} from a session on {session_track}"
        for attempt_id, attempt_track, session_track in database.query(
            "SELECT attempt.attempt_id, attempt.track_id, session.track_id "
            "FROM session_staged_events staged "
            "JOIN sessions session ON session.session_id = staged.session_id "
            "JOIN attempts attempt ON attempt.attempt_id = staged.materialized_id "
            "WHERE staged.status = 'materialized' AND staged.materialized_kind = 'attempt' "
            "AND attempt.track_id <> session.track_id ORDER BY attempt.attempt_id"
        )
    ]
    if crossed:
        checks.append(
            _failed(
                "session_attempt_track",
                "an attempt materialized by a session belongs to another learner's track",
                attempts="; ".join(crossed[:20]),
            )
        )
    else:
        checks.append(
            _ok("session_attempt_track", "every session-materialized attempt is its own track's")
        )
    # 8. The novelty cap is a promise about the whole session, so it is counted over the
    #    session rather than over each block.
    over_cap = [
        f"{session_id}: {novel} new target(s) against a cap of {cap}"
        for session_id, novel, cap in database.query(
            "SELECT session.session_id, count(DISTINCT target.content_id), "
            "session.novel_target_cap FROM sessions session "
            "JOIN session_blocks block ON block.session_id = session.session_id "
            "JOIN session_block_targets target ON target.block_id = block.block_id "
            "WHERE target.novel GROUP BY session.session_id, session.novel_target_cap "
            "HAVING count(DISTINCT target.content_id) > session.novel_target_cap "
            "ORDER BY session.session_id"
        )
    ]
    if over_cap:
        checks.append(
            _failed(
                "session_novelty_cap",
                "a session plans more new targets than its own novelty cap allows",
                sessions="; ".join(over_cap[:20]),
            )
        )
    else:
        checks.append(_ok("session_novelty_cap", "every session respects its novelty cap"))
    # 9. A block's own shape: the framing blocks introduce nothing, and a plan's minutes
    #    never exceed what the learner asked for.
    framing = [
        f"{block_id} ({role})"
        for block_id, role in database.query(
            "SELECT block.block_id, block.role FROM session_blocks block "
            "JOIN session_block_targets target ON target.block_id = block.block_id "
            "WHERE block.role <> 'core' AND target.novel ORDER BY block.block_id"
        )
    ]
    if framing:
        checks.append(
            _failed(
                "session_framing_blocks",
                "a warm-up or closure block carries a new target: the blocks that frame a "
                "session are for material the learner has already met",
                blocks="; ".join(framing[:20]),
            )
        )
    else:
        checks.append(_ok("session_framing_blocks", "no framing block introduces new material"))
    # 10. A materialized event names a row of the kind it says it became. One column
    #     points at four tables, so no foreign key can express this, and the wrong table
    #     is exactly what "the pattern, not the occurrence" looked like.
    tables = {
        "attempt": ("attempts", "attempt_id"),
        "error-occurrence": ("error_occurrences", "occurrence_id"),
        "followup": ("followups", "followup_id"),
        "observation": ("session_observations", "observation_id"),
        "comprehension": ("comprehension_observations", "observation_id"),
        "pronunciation": ("pronunciation_observations", "observation_id"),
    }
    unresolved_targets: list[str] = []
    for kind, (table, column) in tables.items():
        if table not in present:
            # A kind whose table this schema version does not have yet. The CHECK on
            # `materialized_kind` cannot have admitted it either, so there is nothing to
            # resolve -- and querying a table that does not exist would fail the whole
            # report rather than report anything.
            continue
        unresolved_targets.extend(
            f"{staged_event_id} -> {kind} {materialized_id}"
            for staged_event_id, materialized_id in database.query(
                "SELECT staged.staged_event_id, staged.materialized_id "
                "FROM session_staged_events staged "
                f"LEFT JOIN {quote_identifier(table)} target "
                f"  ON target.{quote_identifier(column)} = staged.materialized_id "
                "WHERE staged.status = 'materialized' AND staged.materialized_kind = ? "
                f"AND target.{quote_identifier(column)} IS NULL "
                "ORDER BY staged.staged_event_id",
                [kind],
            )
        )
    if unresolved_targets:
        checks.append(
            _failed(
                "staged_event_materialized_target",
                "a materialized staged event names a row that does not exist in the table "
                "its kind belongs to",
                events="; ".join(unresolved_targets[:20]),
            )
        )
    else:
        checks.append(
            _ok(
                "staged_event_materialized_target",
                "every materialized staged event names a row of its own kind",
            )
        )
    # 11. An attempt records when the learner did the thing, not when it was flushed.
    misdated = [
        f"{attempt_id}: attempt at {attempt_at}, event at {event_at}"
        for attempt_id, attempt_at, event_at in database.query(
            "SELECT attempt.attempt_id, attempt.occurred_at, staged.occurred_at "
            "FROM session_staged_events staged "
            "JOIN attempts attempt ON attempt.attempt_id = staged.materialized_id "
            "WHERE staged.status = 'materialized' AND staged.materialized_kind = 'attempt' "
            "AND attempt.occurred_at <> staged.occurred_at ORDER BY attempt.attempt_id"
        )
    ]
    if misdated:
        checks.append(
            _failed(
                "session_attempt_occurrence_time",
                "an attempt is dated differently from the event it came from, so the "
                "learner's chronology is not the one they lived",
                attempts="; ".join(misdated[:20]),
            )
        )
    else:
        checks.append(
            _ok(
                "session_attempt_occurrence_time",
                "every session attempt is dated when the observation happened",
            )
        )
    # 12. A staged event's activity belongs to the block it names.
    misattributed = [
        f"{staged_event_id}: activity in {activity_block}, event in {event_block}"
        for staged_event_id, activity_block, event_block in database.query(
            "SELECT staged.staged_event_id, activity.block_id, staged.block_id "
            "FROM session_staged_events staged "
            "JOIN activities activity ON activity.activity_id = staged.activity_id "
            "WHERE staged.block_id IS NOT NULL AND activity.block_id <> staged.block_id "
            "ORDER BY staged.staged_event_id"
        )
    ]
    if misattributed:
        checks.append(
            _failed(
                "session_activity_block",
                "a staged event names an activity from another block, so what the learner "
                "was doing cannot be read from it",
                events="; ".join(misattributed[:20]),
            )
        )
    else:
        checks.append(_ok("session_activity_block", "every staged event's activity is its block's"))
    # 13. A package's session belongs to the package's own track. Ingestion refuses the
    #     cross-learner case; this is the same rule for a database that arrived restored.
    strayed = [
        f"{ingestion_id}: package on {package_track}, session on {session_track}"
        for ingestion_id, package_track, session_track in database.query(
            "SELECT package.ingestion_id, package.track_id, session.track_id "
            "FROM session_packages package "
            "JOIN sessions session ON session.session_id = package.session_id "
            "WHERE package.track_id <> session.track_id ORDER BY package.ingestion_id"
        )
    ]
    if strayed:
        checks.append(
            _failed(
                "session_package_track",
                "an ingested package is attached to a session on another learner's track",
                packages="; ".join(strayed[:20]),
            )
        )
    else:
        checks.append(
            _ok("session_package_track", "every ingested package is on its own track's session")
        )
    # 14. One external session contributes each of its events once. The unique index
    #     covers a single LinguaWiki session; a checkpoint export and a completed export
    #     of the same call can land on two, which only this can see.
    doubled = [
        f"{source_event_id} from {external_session_id} ({count} times)"
        for external_session_id, source_event_id, count in database.query(
            "SELECT package.external_session_id, staged.source_event_id, count(*) "
            "FROM session_staged_events staged "
            "JOIN session_event_batches batch ON batch.batch_id = staged.batch_id "
            "JOIN session_packages package ON package.ingestion_id = batch.ingestion_id "
            "GROUP BY package.external_session_id, staged.source_event_id, package.track_id "
            "HAVING count(*) > 1 ORDER BY staged.source_event_id"
        )
    ]
    if doubled:
        checks.append(
            _failed(
                "external_event_uniqueness",
                "one external session's event is staged more than once, so an observation "
                "made once would be credited twice",
                events="; ".join(doubled[:20]),
            )
        )
    else:
        checks.append(_ok("external_event_uniqueness", "every external event is staged once"))
    # 15. A package is ingested once. The unique index enforces it going forward; this
    #     says so for a database that arrived from somewhere else.
    repeated = [
        f"{package_hash} ({count} ingestions)"
        for package_hash, count in database.query(
            "SELECT package_hash, count(*) FROM session_packages GROUP BY package_hash "
            "HAVING count(*) > 1 ORDER BY package_hash"
        )
    ]
    if repeated:
        checks.append(
            _failed(
                "session_package_uniqueness",
                "the same package content is ingested more than once, so one external "
                "session is staged twice",
                packages="; ".join(repeated[:20]),
            )
        )
    else:
        checks.append(_ok("session_package_uniqueness", "every session package is ingested once"))
    return checks


def _assessment_provenance_checks(database: Database) -> list[CheckResult]:
    """An observation from a run belongs to that run's learner, and to what it served.

    A run belongs to one track, and `assessment_run_tasks` records what each task
    demanded when it was served -- because a pack is mutable and a run is not. Both were
    checkable only at write time, so a restore or a build with the looser rule could
    leave an attempt attributed to another learner's run, or carrying facts a later pack
    edit invented. Neither is visible in the row itself, which is what these name.
    """

    checks: list[CheckResult] = []
    foreign = [
        f"{attempt_id}: attempt on {attempt_track}, run on {run_track}"
        for attempt_id, attempt_track, run_track in database.query(
            "SELECT attempt.attempt_id, attempt.track_id, run.track_id FROM attempts attempt "
            "JOIN assessment_runs run ON run.run_id = attempt.assessment_run_id "
            "WHERE attempt.track_id <> run.track_id ORDER BY attempt.attempt_id"
        )
    ]
    if foreign:
        checks.append(
            _failed(
                "attempt_run_track",
                "an attempt is attributed to an assessment run belonging to another "
                "track, so one learner's run is in another learner's model",
                attempts="; ".join(foreign[:20]),
            )
        )
    else:
        checks.append(_ok("attempt_run_track", "every run-backed attempt is on the run's track"))
    # An inner join answers "does the recorded shape match the served one" and nothing
    # else: an attempt whose run/task pair has no served row at all simply vanished from
    # it, which is the state the write path refuses most firmly. So membership is
    # established first, separately, and only then are the facts compared.
    unserved = [
        f"{attempt_id}: {reason}"
        for attempt_id, reason in database.query(
            "SELECT attempt.attempt_id, 'no served record for this run and task' "
            "FROM attempts attempt WHERE attempt.assessment_run_id IS NOT NULL "
            "AND attempt.task_content_id IS NOT NULL AND NOT EXISTS ("
            "  SELECT 1 FROM assessment_run_tasks served "
            "  WHERE served.run_id = attempt.assessment_run_id "
            "  AND served.content_id = attempt.task_content_id) "
            "UNION ALL "
            "SELECT attempt.attempt_id, 'attributed to a run but names no task' "
            "FROM attempts attempt WHERE attempt.assessment_run_id IS NOT NULL "
            "AND attempt.task_content_id IS NULL "
            "ORDER BY 1, 2"
        )
    ]
    if unserved:
        checks.append(
            _failed(
                "attempt_run_membership",
                "an attempt is attributed to an assessment run that has no record of "
                "serving the task it names, so what the learner faced cannot be "
                "established from the run at all",
                attempts="; ".join(unserved[:20]),
            )
        )
    else:
        checks.append(
            _ok("attempt_run_membership", "every run-backed attempt names a task the run served")
        )
    # Compared where the served record holds the fact. A row served before migration 0016
    # has every one of them null; a partial row is damage, which the write path refuses
    # and `attempt_snapshot_complete` reports.
    #
    # A null on the *attempt* side is a mismatch too, not something to skip: an attempt
    # with no difficulty against a task served with one has lost the number its estimate
    # was placed by.
    drifted = [
        f"{attempt_id}: {field}"
        for attempt_id, field in database.query(
            "SELECT attempt.attempt_id, 'task_type' FROM attempts attempt "
            "JOIN assessment_run_tasks served "
            "  ON served.run_id = attempt.assessment_run_id "
            "  AND served.content_id = attempt.task_content_id "
            "WHERE served.task_type IS NOT NULL AND attempt.task_type <> served.task_type "
            "UNION ALL "
            "SELECT attempt.attempt_id, 'modality' FROM attempts attempt "
            "JOIN assessment_run_tasks served "
            "  ON served.run_id = attempt.assessment_run_id "
            "  AND served.content_id = attempt.task_content_id "
            "WHERE served.modality IS NOT NULL AND attempt.modality <> served.modality "
            "UNION ALL "
            "SELECT attempt.attempt_id, 'dimension' FROM attempts attempt "
            "JOIN assessment_run_tasks served "
            "  ON served.run_id = attempt.assessment_run_id "
            "  AND served.content_id = attempt.task_content_id "
            "WHERE served.dimension IS NOT NULL "
            "  AND coalesce(attempt.dimension, '') <> served.dimension "
            "UNION ALL "
            "SELECT attempt.attempt_id, 'difficulty' FROM attempts attempt "
            "JOIN assessment_run_tasks served "
            "  ON served.run_id = attempt.assessment_run_id "
            "  AND served.content_id = attempt.task_content_id "
            "WHERE served.difficulty IS NOT NULL AND ("
            "  attempt.source_difficulty IS NULL "
            "  OR abs(attempt.source_difficulty - served.difficulty) > 1e-9) "
            "ORDER BY 1, 2"
        )
    ]
    if drifted:
        checks.append(
            _failed(
                "attempt_served_facts",
                "an attempt records a task shape the run did not serve, so a later pack "
                "edit has been written into what the learner faced",
                attempts="; ".join(drifted[:20]),
            )
        )
    else:
        checks.append(
            _ok("attempt_served_facts", "every run-backed attempt matches what was served")
        )
    # All of migration 0016's columns, or none of them. A partial record cannot establish
    # what the learner faced, and treating it as a pre-0016 row let the bank answer for
    # facts the record actually held.
    partial = [
        str(run_id) + "/" + str(content_id)
        for run_id, content_id in database.query(
            "SELECT run_id, content_id FROM assessment_run_tasks WHERE ("
            "  task_type IS NULL OR modality IS NULL OR difficulty IS NULL "
            "  OR content_family IS NULL OR content_hash IS NULL) AND ("
            "  task_type IS NOT NULL OR modality IS NOT NULL OR difficulty IS NOT NULL "
            "  OR content_family IS NOT NULL OR content_hash IS NOT NULL) "
            "ORDER BY run_id, content_id"
        )
    ]
    if partial:
        checks.append(
            _failed(
                "served_snapshot_complete",
                "a run holds a partial record of what it served, which can establish "
                "neither what the learner faced nor that the record predates the snapshot",
                served="; ".join(partial[:20]),
            )
        )
    else:
        checks.append(
            _ok("served_snapshot_complete", "every served record is whole or wholly absent")
        )
    return checks


def _mastery_checks(database: Database) -> list[CheckResult]:
    """No item's stage outruns the kind of evidence recorded for it.

    This is the exit-gate rule as a database fact rather than a code path: recognition
    cannot have promoted spontaneous production, whatever wrote the row. `gated_stage`
    and `evidence_ceiling` are stored precisely so the claim can be checked here without
    re-deriving it.
    """

    from linguawiki.mastery import STAGES

    # The ladder is passed as a parameter rather than interpolated: `list_position`
    # gives each stage its ordinal, so the comparison is the same one `mastery` makes
    # and no vocabulary reaches the statement as text.
    ladder = list(STAGES)
    overclaimed = [
        f"{track_id}/{content_id}: {stage} > {ceiling}"
        for track_id, content_id, stage, ceiling in database.query(
            "SELECT track_id, content_id, stage, evidence_ceiling FROM track_item_state "
            "WHERE evidence_ceiling IS NOT NULL "
            "AND coalesce(list_position(?, stage), 0) "
            "> coalesce(list_position(?, evidence_ceiling), 0) "
            "ORDER BY track_id, content_id",
            [ladder, ladder],
        )
    ]
    checks: list[CheckResult] = []
    if overclaimed:
        checks.append(
            _failed(
                "mastery_evidence_ceiling",
                "an item's stage is stronger than the evidence recorded for it allows",
                items="; ".join(overclaimed[:20]),
            )
        )
    else:
        checks.append(
            _ok("mastery_evidence_ceiling", "no item's stage outruns its kind of evidence")
        )
    unversioned = int(
        database.scalar(
            "SELECT count(*) FROM track_item_state WHERE stage_source = 'evidence' "
            "AND aggregation_version IS NULL"
        )
    )
    if unversioned:
        checks.append(
            _failed(
                "mastery_aggregation_version",
                "an evidence-derived stage does not name the policy that produced it",
                rows=str(unversioned),
            )
        )
    else:
        checks.append(
            _ok("mastery_aggregation_version", "every evidence-derived stage names its policy")
        )
    return checks


def _error_model_checks(database: Database) -> list[CheckResult]:
    """Every error pattern's identity is the one its own parts derive.

    Deduplication rides on the primary key, so a row whose key does not match its parts
    is a pattern that will never be found again -- the next occurrence of the same
    mistake would open a second pattern beside it.
    """

    from linguawiki.services.errors import error_identity

    mismatched = [
        str(error_id)
        for error_id, track_id, category, signature, target in database.query(
            "SELECT error_id, track_id, category, signature, target_content_id "
            # A superseded pattern keeps the identity it was authored under; its history
            # moved to the successor it names, and nothing derives its key again.
            "FROM error_patterns WHERE status <> 'superseded' ORDER BY error_id"
        )
        if str(error_id)
        != error_identity(
            track_id=str(track_id),
            category=str(category),
            signature=str(signature),
            target_content_id=None if target is None else str(target),
        )
    ]
    checks: list[CheckResult] = []
    if mismatched:
        checks.append(
            _failed(
                "error_identity",
                "an error pattern's identity does not derive from its own parts, so the "
                "next occurrence of it would open a second pattern",
                errors=", ".join(mismatched[:20]),
            )
        )
    else:
        checks.append(_ok("error_identity", "every error pattern's identity derives from itself"))
    # A pattern counted against the learner must rest on an occurrence that was
    # confirmed as their error. Without this, a transcription artifact persisted as an
    # active error is invisible -- and an artifact taught back as a mistake is the one
    # outcome the classification exists to prevent.
    from linguawiki.error_model import CONFIRMED_CLASSIFICATION, LIVE_STATUSES

    live = ", ".join("?" for _ in LIVE_STATUSES)
    unconfirmed = [
        f"{error_id} ({status})"
        for error_id, status in database.query(
            f"SELECT error_id, status FROM error_patterns WHERE status IN ({live}) "
            "AND NOT EXISTS (SELECT 1 FROM error_occurrences occurrence "
            "  WHERE occurrence.error_id = error_patterns.error_id "
            "  AND occurrence.classification = ?) ORDER BY error_id",
            [*LIVE_STATUSES, CONFIRMED_CLASSIFICATION],
        )
    ]
    miscounted = [
        f"{error_id}: {stored} recorded, {actual} confirmed"
        for error_id, stored, actual in database.query(
            "SELECT pattern.error_id, pattern.occurrence_count, "
            "  (SELECT count(*) FROM error_occurrences occurrence "
            "   WHERE occurrence.error_id = pattern.error_id "
            "   AND occurrence.classification = ?) "
            "FROM error_patterns pattern WHERE pattern.occurrence_count <> "
            "  (SELECT count(*) FROM error_occurrences occurrence "
            "   WHERE occurrence.error_id = pattern.error_id "
            "   AND occurrence.classification = ?) ORDER BY pattern.error_id",
            [CONFIRMED_CLASSIFICATION, CONFIRMED_CLASSIFICATION],
        )
    ]
    if unconfirmed or miscounted:
        checks.append(
            _failed(
                "error_confirmation",
                "an error counted against the learner has no occurrence confirmed as "
                "their error, or its occurrence count disagrees with its confirmed "
                "occurrences",
                unconfirmed="; ".join(unconfirmed[:20]) or "none",
                miscounted="; ".join(miscounted[:20]) or "none",
            )
        )
    else:
        checks.append(_ok("error_confirmation", "every live error rests on a confirmed occurrence"))
    # A superseded pattern's history moved to its successor, so nothing should still
    # point at it and it must say what it became.
    dangling = [
        f"{table} -> {count}"
        for table, count in (
            (
                table,
                int(
                    database.scalar(
                        f"SELECT count(*) FROM {quote_identifier(table)} dependent "
                        "JOIN error_patterns pattern ON pattern.error_id = dependent.error_id "
                        "WHERE pattern.status = 'superseded'"
                    )
                ),
            )
            for table in ("error_occurrences", "error_evidence", "followups")
        )
        if count
    ]
    unnamed = int(
        database.scalar(
            "SELECT count(*) FROM error_patterns WHERE status = 'superseded' "
            "AND superseded_by IS NULL"
        )
    )
    if dangling or unnamed:
        checks.append(
            _failed(
                "error_supersession",
                "a superseded error pattern still holds history, or does not name the "
                "pattern its history moved to",
                dangling="; ".join(dangling) or "none",
                unnamed=str(unnamed),
            )
        )
    else:
        checks.append(
            _ok("error_supersession", "every superseded pattern names its successor and is empty")
        )
    foreign = int(
        database.scalar(
            "SELECT count(*) FROM error_evidence link "
            "JOIN error_patterns pattern ON pattern.error_id = link.error_id "
            "JOIN evidence item ON item.evidence_id = link.evidence_id "
            "WHERE item.track_id <> pattern.track_id"
        )
    )
    if foreign:
        checks.append(
            _failed(
                "error_evidence_scope",
                "counter-evidence for an error comes from another track",
                rows=str(foreign),
            )
        )
    else:
        checks.append(_ok("error_evidence_scope", "all counter-evidence is from its own track"))
    return checks


def _estimate_checks(database: Database) -> list[CheckResult]:
    """A dimension nothing tested is untested, and one nothing measured is provisional.

    The two are different facts and a single confidence label cannot carry both, which
    is why `estimate_status` exists. A row that contradicts its own evidence count would
    let "we did not look" be read as "they cannot do it".
    """

    checks: list[CheckResult] = []
    contradictions = [
        f"{dimension} ({status}, {count} observation(s))"
        for dimension, status, count in database.query(
            "SELECT dimension, estimate_status, evidence_count FROM skill_estimates "
            "WHERE (estimate_status = 'not-tested' AND evidence_count > 0) "
            "OR (estimate_status = 'estimated' AND evidence_count = 0) "
            "ORDER BY dimension"
        )
    ]
    if contradictions:
        checks.append(
            _failed(
                "estimate_status",
                "an estimate's status contradicts the evidence behind it",
                estimates="; ".join(contradictions[:20]),
            )
        )
    else:
        checks.append(_ok("estimate_status", "every estimate's status matches its evidence"))
    unlabelled = int(
        database.scalar("SELECT count(*) FROM skill_estimates WHERE estimate_status IS NULL")
    )
    if unlabelled:
        checks.append(
            _failed(
                "estimate_status_present",
                "an estimate does not say whether it was tested",
                rows=str(unlabelled),
            )
        )
    else:
        checks.append(_ok("estimate_status_present", "every estimate says whether it was tested"))
    foreign = int(
        database.scalar(
            "SELECT count(*) FROM estimate_evidence link "
            "JOIN estimate_history snapshot ON snapshot.snapshot_id = link.snapshot_id "
            "JOIN evidence item ON item.evidence_id = link.evidence_id "
            "WHERE item.track_id <> snapshot.track_id"
        )
    )
    if foreign:
        checks.append(
            _failed(
                "estimate_evidence_scope",
                "an estimate snapshot cites evidence from another track",
                rows=str(foreign),
            )
        )
    else:
        checks.append(
            _ok("estimate_evidence_scope", "every cited observation is from its own track")
        )
    return checks


def _source_checks(database: Database) -> list[CheckResult]:
    """A source's progress, its rights, and its comprehension record agree with themselves.

    The rights checks are the ones that matter outside this repository. An excerpt longer
    than the rights class permits is a copyright problem stored in a learner's database,
    and it cannot be found by reading the code: it has to be found in the data.
    """

    from linguawiki import sources as source_policy

    checks: list[CheckResult] = []
    over_long = [
        f"{unit_id} ({rights}, {length} characters)"
        for unit_id, rights, length in database.query(
            "SELECT unit.unit_id, source.rights, length(unit.excerpt) "
            "FROM source_units unit JOIN sources source ON source.source_id = unit.source_id "
            "WHERE unit.excerpt IS NOT NULL ORDER BY unit.unit_id"
        )
        if int(length) > source_policy.excerpt_limit(str(rights))
    ]
    if over_long:
        checks.append(
            _failed(
                "source_excerpt_rights",
                "a stored excerpt is longer than its source's rights class permits; this is "
                "a copyright boundary crossed inside the learner's own database",
                units=", ".join(over_long[:20]),
            )
        )
    else:
        checks.append(
            _ok("source_excerpt_rights", "every stored excerpt is within its rights class")
        )
    metadata_only = [
        str(unit_id)
        for (unit_id,) in database.query(
            "SELECT unit.unit_id FROM source_units unit "
            "JOIN sources source ON source.source_id = unit.source_id "
            "WHERE source.rights = 'metadata-only' AND unit.excerpt IS NOT NULL "
            "ORDER BY unit.unit_id"
        )
    ]
    if metadata_only:
        checks.append(
            _failed(
                "source_metadata_only",
                "a source catalogued as metadata-only holds text from the work itself",
                units=", ".join(metadata_only[:20]),
            )
        )
    else:
        checks.append(
            _ok("source_metadata_only", "metadata-only sources hold no text from the work")
        )
    # Comprehension is ordered, and the order is the evidence: an unaided reading recorded
    # after an aided one is the first one with the help left out.
    withdrawn: list[str] = []
    for source_id, unit_id in database.query(
        "SELECT DISTINCT source_id, unit_id FROM comprehension_observations "
        "ORDER BY source_id, unit_id"
    ):
        observations = [
            source_policy.Comprehension(aid=str(aid), band=str(band), sequence=int(sequence))
            for aid, band, sequence in database.query(
                "SELECT aid, band, sequence FROM comprehension_observations "
                "WHERE source_id = ? AND unit_id IS NOT DISTINCT FROM ? ORDER BY sequence",
                [source_id, unit_id],
            )
        ]
        try:
            source_policy.assert_observation_order(observations, reference=str(source_id))
        except LinguaWikiError:
            withdrawn.append(f"{source_id}/{unit_id or 'whole source'}")
    if withdrawn:
        checks.append(
            _failed(
                "comprehension_order",
                "an unaided comprehension record follows an aided one, which would report "
                "help that was already given as comprehension without it",
                units=", ".join(withdrawn[:20]),
            )
        )
    else:
        checks.append(
            _ok("comprehension_order", "unaided comprehension precedes the help it was without")
        )
    # A source belongs to one learner and so does everything it links to. Existence was
    # checked and ownership was not, so learner A's book could point at learner B's error
    # pattern -- and `errors show` then reports it as A's history.
    crossed_links = [
        f"{unit_id} -> {target_id}"
        for unit_id, target_id in database.query(
            "SELECT link.unit_id, link.target_id FROM source_item_links link "
            "JOIN source_units unit ON unit.unit_id = link.unit_id "
            "JOIN sources source ON source.source_id = unit.source_id "
            "JOIN error_patterns pattern ON pattern.error_id = link.target_id "
            "WHERE link.target_kind = 'error-pattern' AND pattern.track_id <> source.track_id "
            "ORDER BY link.unit_id"
        )
    ]
    if crossed_links:
        checks.append(
            _failed(
                "source_link_track_scope",
                "a source links to an error pattern belonging to another learner, which "
                "would put one learner's mistakes in the other's history",
                links=", ".join(crossed_links[:20]),
            )
        )
    else:
        checks.append(
            _ok("source_link_track_scope", "every source link stays inside its own track")
        )
    counted = [
        f"{source_id}: {recorded} recorded, {actual} completed"
        for source_id, recorded, actual in database.query(
            "SELECT progress.source_id, progress.completed_units, "
            "(SELECT count(*) FROM source_units unit WHERE unit.source_id = progress.source_id "
            " AND unit.completed_at IS NOT NULL) "
            "FROM track_source_progress progress ORDER BY progress.source_id"
        )
        if int(recorded) != int(actual)
    ]
    if counted:
        checks.append(
            _failed(
                "source_progress_count",
                "a source's recorded progress disagrees with the units actually completed",
                sources=", ".join(counted[:20]),
            )
        )
    else:
        checks.append(_ok("source_progress_count", "recorded progress matches the units completed"))
    return checks


def _artifact_checks(database: Database, *, external_identity: bool) -> list[CheckResult]:
    """A purge is a fact about a file, and the rows that named it have to agree.

    Two directions matter. A row still claiming audio that was purged would read as
    evidenced and be uncheckable; a claim that *should* have been invalidated and was not
    is the same problem wearing a tombstone.
    """

    from linguawiki import transcripts as transcript_policy

    checks: list[CheckResult] = []
    purged = {
        str(artifact_id): str(reason)
        for artifact_id, reason in database.query(
            "SELECT artifact_id, purge_reason FROM artifacts WHERE purged_at IS NOT NULL"
        )
    }
    known = {
        str(artifact_id) for (artifact_id,) in database.query("SELECT artifact_id FROM artifacts")
    }
    dangling = [
        f"{observation_id} -> {artifact_id}"
        for observation_id, artifact_id in database.query(
            "SELECT observation_id, audio_artifact_id FROM pronunciation_observations "
            "WHERE audio_artifact_id IS NOT NULL ORDER BY observation_id"
        )
        if str(artifact_id) not in known
    ]
    dangling.extend(
        f"{utterance_id} -> {artifact_id}"
        for utterance_id, artifact_id in database.query(
            "SELECT utterance_id, audio_artifact_id FROM utterances "
            "WHERE audio_artifact_id IS NOT NULL ORDER BY utterance_id"
        )
        if str(artifact_id) not in known
    )
    if dangling:
        checks.append(
            _failed(
                "artifact_reference",
                "a row names audio this workspace has no record of at all, so neither the "
                "sound nor the fact that it was deleted can be produced",
                references=", ".join(dangling[:20]),
            )
        )
    else:
        checks.append(_ok("artifact_reference", "every audio reference resolves to a record"))
    standing = [
        f"{observation_id} ({dimension}, {status})"
        for observation_id, status, dimension, artifact_id in database.query(
            "SELECT observation_id, status, dimension, audio_artifact_id "
            "FROM pronunciation_observations WHERE audio_artifact_id IS NOT NULL "
            "AND invalidated_at IS NULL ORDER BY observation_id"
        )
        if str(artifact_id) in purged
        and transcript_policy.invalidated_by_purge(status=str(status), dimension=str(dimension))
    ]
    # Not kept is the same fact as gone, for a claim that has to rest on something
    # anyone can check. `db check` looked only at purged rows, so a claim on audio that
    # was deleted at the door -- for want of consent -- read as perfectly supported.
    standing.extend(
        f"{observation_id} ({dimension}, {status}, never kept)"
        for observation_id, status, dimension, artifact_id in database.query(
            "SELECT observation.observation_id, observation.status, observation.dimension, "
            "observation.audio_artifact_id FROM pronunciation_observations observation "
            "JOIN artifacts artifact ON artifact.artifact_id = observation.audio_artifact_id "
            "WHERE observation.audio_artifact_id IS NOT NULL "
            "AND observation.invalidated_at IS NULL AND NOT artifact.retained "
            "ORDER BY observation.observation_id"
        )
        if transcript_policy.invalidated_by_purge(status=str(status), dimension=str(dimension))
    )
    if standing:
        checks.append(
            _failed(
                "acoustic_claim_support",
                "an acoustic claim still stands on audio that was purged; it reads as "
                "evidenced and nobody, including the learner it is about, can check it",
                observations=", ".join(standing[:20]),
            )
        )
    else:
        checks.append(_ok("acoustic_claim_support", "every standing acoustic claim has its audio"))
    if not external_identity:
        # A database written before 0027 has no producer identifier to be unique.
        return checks
    # A producer's identifier has to mean one recording. Two rows carrying the same one
    # would make a re-ingested package register its audio twice, and the claims from the
    # two ingests would rest on different rows for the same file.
    duplicated = [
        f"{external_id} ({count} artifacts)"
        for external_id, count in database.query(
            "SELECT external_id, count(*) FROM artifacts WHERE external_id IS NOT NULL "
            "GROUP BY track_id, external_id HAVING count(*) > 1 ORDER BY external_id"
        )
    ]
    if duplicated:
        checks.append(
            _failed(
                "artifact_external_identity",
                "one producer identifier names more than one artifact, so a re-ingested "
                "package cannot tell which row is the recording it brought",
                artifacts=", ".join(duplicated[:20]),
            )
        )
    else:
        checks.append(
            _ok("artifact_external_identity", "every producer identifier names one artifact")
        )
    # One file, one owner. A *file* is workspace-global while the rows describing it are per
    # track, so two live rows on one path meant either learner's purge or retention sweep
    # would delete the other's recording. The command refuses it now; this finds the ones
    # that arrived before it did, or through a restore.
    shared = [
        f"{relative_path} ({count} live artifacts)"
        for relative_path, count in database.query(
            "SELECT relative_path, count(*) FROM artifacts WHERE purged_at IS NULL "
            "AND retained GROUP BY relative_path HAVING count(*) > 1 ORDER BY relative_path"
        )
    ]
    if shared:
        checks.append(
            _failed(
                "artifact_path_ownership",
                "one file is registered by more than one live artifact, so a purge or a "
                "retention sweep on either would delete a recording the other still claims",
                paths=", ".join(shared[:20]),
            )
        )
    else:
        checks.append(_ok("artifact_path_ownership", "every registered file has one owner"))
    # A clip is an excerpt of a recording this workspace has a record of. Not a foreign
    # key, because a purge repoints the source row's own key.
    # Everything that makes a row a clip rather than a whole recording wearing a label.
    # Clip provenance buys retention a whole recording does not get, so a check that only
    # asked whether *some* source row existed was a check a full conversation could pass.
    malformed_clips = [
        f"{artifact_id}: {reason}" for artifact_id, reason in _clip_problems(database)
    ]
    if malformed_clips:
        checks.append(
            _failed(
                "clip_provenance",
                "a row claims to be a selected clip without being one, which would earn a "
                "whole recording the longer retention a clip is given",
                clips=", ".join(malformed_clips[:20]),
            )
        )
    else:
        checks.append(
            _ok("clip_provenance", "every clip is audio, of this track's audio, over a window")
        )
    # A claim about how something sounded rests on a *recording*. The basis rule was
    # checked and the file's kind was not, so a transcript artifact satisfied a confirmed
    # audio claim and `db check` reported it clean.
    mistyped = [
        f"{observation_id} -> {artifact_id} ({kind})"
        for observation_id, artifact_id, kind in database.query(
            "SELECT observation.observation_id, artifact.artifact_id, artifact.kind "
            "FROM pronunciation_observations observation "
            "JOIN artifacts artifact ON artifact.artifact_id = observation.audio_artifact_id "
            "WHERE artifact.kind <> 'audio' ORDER BY observation.observation_id"
        )
    ]
    mistyped.extend(
        f"{utterance_id} -> {artifact_id} ({kind})"
        for utterance_id, artifact_id, kind in database.query(
            "SELECT utterance.utterance_id, artifact.artifact_id, artifact.kind "
            "FROM utterances utterance "
            "JOIN artifacts artifact ON artifact.artifact_id = utterance.audio_artifact_id "
            "WHERE artifact.kind <> 'audio' ORDER BY utterance.utterance_id"
        )
    )
    if mistyped:
        checks.append(
            _failed(
                "acoustic_claim_is_audio",
                "a claim about how something sounded rests on a file that is not a "
                "recording; a correct transcript proves nothing about how it sounded, and "
                "that is no less true when the transcript is a file",
                references=", ".join(mistyped[:20]),
            )
        )
    else:
        checks.append(_ok("acoustic_claim_is_audio", "every acoustic claim rests on a recording"))
    return checks


def _clip_problems(database: Database) -> list[tuple[str, str]]:
    """Why each row that claims to be a clip is not one.

    Every row with *any* clip field set is inspected, not only those naming a source: a row
    carrying offsets and no source was invisible to a query keyed on the source, and so was
    a whole recording recorded as a clip of itself, which qualified for the longer retention
    a clip is given.
    """

    problems: list[tuple[str, str]] = []
    sources = {
        str(artifact_id): (str(track_id), str(kind))
        for artifact_id, track_id, kind in database.query(
            "SELECT artifact_id, track_id, kind FROM artifacts"
        )
    }
    for artifact_id, track_id, kind, source, starts, ends in database.query(
        "SELECT artifact_id, track_id, kind, clip_of_artifact_id, clip_starts_at_ms, "
        "clip_ends_at_ms FROM artifacts WHERE clip_of_artifact_id IS NOT NULL "
        "OR clip_starts_at_ms IS NOT NULL OR clip_ends_at_ms IS NOT NULL "
        "ORDER BY artifact_id"
    ):
        identifier = str(artifact_id)
        if source is None:
            problems.append(
                (identifier, "has clip offsets and names no recording they are offsets into")
            )
            continue
        if str(source) == identifier:
            problems.append(
                (identifier, "is recorded as an excerpt of itself, which is the whole thing")
            )
            continue
        held = sources.get(str(source))
        if held is None:
            problems.append((identifier, f"names {source}, which is not here"))
            continue
        if held[0] != str(track_id):
            problems.append((identifier, f"is an excerpt of another learner's {source}"))
            continue
        if held[1] != "audio" or str(kind) != "audio":
            problems.append((identifier, "is not audio, or is not audio's excerpt"))
            continue
        if starts is None or ends is None:
            problems.append((identifier, "has no window, so it is the whole recording"))
            continue
        if int(starts) < 0 or int(ends) <= int(starts):
            problems.append((identifier, f"has an impossible window {starts}-{ends}ms"))
    return problems


def _transcript_checks(
    database: Database, *, supersession: bool, override_provenance: bool
) -> list[CheckResult]:
    """The transcript layers say what they actually did.

    A revision filed as a `normalization` that changed the words is the defect this whole
    stage is built to prevent: it turns the transcription's mistake into the learner's.
    """

    from linguawiki import transcripts as transcript_policy

    checks: list[CheckResult] = []
    raw_text = {
        str(utterance_id): (str(text), str(visibility))
        for utterance_id, text, visibility in database.query(
            "SELECT utterance_id, raw_text, visibility FROM utterances"
        )
    }
    # Before 0027 a layer held exactly one reading, enforced by a unique index, so the
    # current-reading question did not exist and neither did the column that answers it.
    current_filter = " WHERE superseded_at IS NULL" if supersession else ""
    # Keyed by *revision*, not by layer. A layer can hold a superseded reading beside the
    # current one, and keying by layer compared one revision's words against another
    # revision's kind -- which reported a perfectly honest normalization as a lie.
    revision_text = {
        str(revision_id): (str(text), str(visibility))
        for revision_id, text, visibility in database.query(
            "SELECT revision_id, text, visibility FROM transcript_revisions"
        )
    }
    current_by_layer = {
        (str(utterance_id), str(layer)): (str(text), str(visibility))
        for utterance_id, layer, text, visibility in database.query(
            "SELECT utterance_id, layer, text, visibility FROM transcript_revisions"
            + current_filter
        )
    }
    dishonest: list[str] = []
    unverifiable = 0
    for revision_id, utterance_id, derived_from in database.query(
        "SELECT revision_id, utterance_id, derived_from FROM transcript_revisions "
        "WHERE kind = 'normalization' ORDER BY revision_id"
    ):
        after = revision_text[str(revision_id)]
        before = (
            raw_text.get(str(utterance_id))
            if str(derived_from) == "raw"
            else current_by_layer.get((str(utterance_id), str(derived_from)))
        )
        if before is None or before[1] != "full" or after[1] != "full":
            # A workspace that kept only a hash cannot be asked this question, and
            # answering it anyway by comparing truncated text would invent a failure.
            unverifiable += 1
            continue
        if not transcript_policy.same_words(before[0], after[0]):
            dishonest.append(str(revision_id))
    if dishonest:
        checks.append(
            _failed(
                "revision_honesty",
                "a revision filed as a normalization changes which words were heard, which "
                "would teach the transcription's mistake back to the learner as their own",
                revisions=", ".join(dishonest[:20]),
            )
        )
    else:
        checks.append(
            _ok(
                "revision_honesty",
                "every normalization kept the words it started from",
                unverifiable=str(unverifiable),
            )
        )
    misderived = [
        f"{revision_id} ({layer} from {derived_from})"
        for revision_id, layer, derived_from in database.query(
            "SELECT revision_id, layer, derived_from FROM transcript_revisions ORDER BY revision_id"
        )
        if str(derived_from) not in transcript_policy.LAYER_SOURCES.get(str(layer), ())
    ]
    if misderived:
        checks.append(
            _failed(
                "revision_derivation",
                "a revision claims to come from a layer it cannot come from",
                revisions=", ".join(misderived[:20]),
            )
        )
    else:
        checks.append(_ok("revision_derivation", "every revision derives from a layer below it"))
    # An interpretation is about an utterance in some track; a pronunciation observation
    # names its own track. The two have to be the same track, or a learner's words would
    # be carrying another learner's judgement.
    crossed = [
        f"{observation_id}"
        for (observation_id,) in database.query(
            "SELECT observation.observation_id FROM pronunciation_observations observation "
            "JOIN utterances utterance ON utterance.utterance_id = observation.utterance_id "
            "WHERE utterance.track_id <> observation.track_id ORDER BY observation.observation_id"
        )
    ]
    if crossed:
        checks.append(
            _failed(
                "pronunciation_track_scope",
                "a pronunciation observation judges an utterance from another track",
                observations=", ".join(crossed[:20]),
            )
        )
    else:
        checks.append(
            _ok("pronunciation_track_scope", "every pronunciation observation stays in its track")
        )
    unsupported = [
        f"{observation_id} ({dimension}, {status}, {basis})"
        for observation_id, status, dimension, basis in database.query(
            "SELECT observation_id, status, dimension, basis FROM pronunciation_observations "
            "WHERE invalidated_at IS NULL ORDER BY observation_id"
        )
        if str(basis) != "audio"
        and (
            str(status) == transcript_policy.CONFIRMED_STATUS
            or str(dimension) in transcript_policy.AUDIO_ONLY_DIMENSIONS
        )
    ]
    if unsupported:
        checks.append(
            _failed(
                "acoustic_claim_basis",
                "a claim about how something sounded rests only on how it reads",
                observations=", ".join(unsupported[:20]),
            )
        )
    else:
        checks.append(_ok("acoustic_claim_basis", "every acoustic claim rests on audio"))
    if not supersession:
        return checks
    # One *current* reading per layer. DuckDB has no partial index, so the rule that
    # replaced 0025's unique index lives here: a second current row would make "what does
    # this layer say" a question with two answers.
    contested = [
        f"{utterance_id}/{layer} ({count} current readings)"
        for utterance_id, layer, count in database.query(
            "SELECT utterance_id, layer, count(*) FROM transcript_revisions "
            "WHERE superseded_at IS NULL GROUP BY utterance_id, layer "
            "HAVING count(*) > 1 ORDER BY utterance_id"
        )
    ]
    if contested:
        checks.append(
            _failed(
                "revision_supersession",
                "a transcript layer has more than one current reading, so what it says the "
                "learner said has two answers",
                layers=", ".join(contested[:20]),
            )
        )
    else:
        checks.append(
            _ok("revision_supersession", "every transcript layer has one current reading")
        )
    known_revisions = {
        str(revision_id)
        for (revision_id,) in database.query("SELECT revision_id FROM transcript_revisions")
    }
    unpaired = [
        str(revision_id)
        for revision_id, superseded_at, superseded_by in database.query(
            "SELECT revision_id, superseded_at, superseded_by FROM transcript_revisions "
            "ORDER BY revision_id"
        )
        if (superseded_at is None) != (superseded_by is None)
        or (superseded_by is not None and str(superseded_by) not in known_revisions)
    ]
    if unpaired:
        checks.append(
            _failed(
                "revision_supersession_pairing",
                "a superseded reading does not name the reading that replaced it, so the "
                "history it was kept for cannot be followed",
                revisions=", ".join(unpaired[:20]),
            )
        )
    else:
        checks.append(
            _ok(
                "revision_supersession_pairing",
                "every superseded reading names the one that replaced it",
            )
        )
    if not override_provenance:
        return checks
    # The CHECK in 0028 says this, and a table rebuilt without it -- a restore, a
    # hand-repair, a looser build -- would not. An override asserts that somebody heard
    # the sound, so it needs a reviewer who could have and a reason they can be held to.
    # Both halves of the CHECK, because a check that mirrors half a constraint passes the
    # states the other half forbids: a reason recorded against an override that never
    # happened reads as a judgement somebody made, and nobody made it.
    unaccountable: list[str] = []
    for interpretation_id, overrode, reviewer_kind, reason in database.query(
        "SELECT interpretation_id, overrode_low_confidence, reviewer_kind, override_reason "
        "FROM utterance_interpretations ORDER BY interpretation_id"
    ):
        stated = str(reason or "").strip()
        if overrode:
            if str(reviewer_kind) not in transcript_policy.OVERRIDE_REVIEWERS or not stated:
                unaccountable.append(f"{interpretation_id} ({reviewer_kind})")
        elif stated:
            unaccountable.append(f"{interpretation_id} (a reason for no override)")
    if unaccountable:
        checks.append(
            _failed(
                "override_provenance",
                "an override of the transcriber's own uncertainty has no reviewer who "
                "could have heard the audio, or no reason; it marks the learner wrong on "
                "an authority nobody can be held to",
                interpretations=", ".join(unaccountable[:20]),
            )
        )
    else:
        checks.append(_ok("override_provenance", "every low-confidence override names who and why"))
    return checks


def _orphan_checks(database: Database, *, schema: SchemaSpecification) -> list[CheckResult]:
    orphans: list[str] = []

    def present(table: str, column: str) -> bool:
        # A relation is checkable only where both of its columns exist. Gating on table
        # names alone was enough until a relation arrived as an added column, which a
        # database still behind that migration does not have.
        return any(name == column for name, _ in schema.get(table, ()))

    relations: list[tuple[str, str, str, str, tuple[str, str] | None]] = [
        (*relation, None)
        for relation in ORPHAN_RELATIONS
        if present(relation[0], relation[1]) and present(relation[2], relation[3])
    ]
    relations.extend(
        (table, column, parent, parent_column, (scope, value) if present(table, scope) else None)
        for table, column, parent, parent_column, scope, value in SCOPED_ORPHAN_RELATIONS
        if present(table, column) and present(parent, parent_column)
    )
    for table, column, parent, parent_column, scoped in relations:
        child_table = quote_identifier(table)
        parent_table = quote_identifier(parent)
        child_column = f"child.{quote_identifier(column)}"
        parent_key = f"parent.{quote_identifier(parent_column)}"
        within = "" if scoped is None else f" AND child.{quote_identifier(scoped[0])} = ?"
        count = int(
            database.scalar(
                f"SELECT count(*) FROM {child_table} child "
                f"LEFT JOIN {parent_table} parent ON {child_column} = {parent_key} "
                f"WHERE {child_column} IS NOT NULL AND {parent_key} IS NULL{within}",
                [] if scoped is None else [scoped[1]],
            )
        )
        if count:
            orphans.append(f"{table}.{column} -> {parent} ({count})")
    if orphans:
        return [
            _failed(
                "orphan_relations", "relations point at missing parents", orphans="; ".join(orphans)
            )
        ]
    return [_ok("orphan_relations", "no orphan relations were found")]


def _projection_checks(database: Database) -> list[CheckResult]:
    present = {str(row[0]) for row in database.query("SELECT projection FROM projection_state")}
    missing = sorted(set(REQUIRED_PROJECTIONS) - present)
    if missing:
        return [
            _failed(
                "projection_state", "projection state rows are missing", missing=", ".join(missing)
            )
        ]
    stale = [
        str(row[0]) for row in database.query("SELECT projection FROM projection_state WHERE stale")
    ]
    if stale:
        return [_warning("projection_state", "projections are stale", stale=", ".join(stale))]
    return [_ok("projection_state", "projection watermarks are present and current")]


def _version_mirror_checks(database: Database, lock: LockManifest | None) -> list[CheckResult]:
    rows = {
        (str(component), str(key)): (str(version), str(sha256))
        for component, key, version, sha256 in database.query(
            "SELECT component, component_key, version, sha256 FROM workspace_versions"
        )
    }
    expected_components = {"core", "database_schema", "skill_bundle"}
    missing = sorted(
        component for component in expected_components if (component, component) not in rows
    )
    if missing:
        return [
            _failed(
                "version_mirror",
                "installed-version mirror rows are missing",
                missing=", ".join(missing),
            )
        ]
    checks = [_ok("version_mirror", "the database mirrors every installed component version")]
    if lock is not None:
        drift = [
            component
            for component, pin in (
                ("core", lock.core),
                ("database_schema", lock.database_schema),
                ("skill_bundle", lock.skill_bundle),
            )
            if rows[(component, component)] != (pin.version, pin.sha256)
        ]
        if drift:
            checks.append(
                _failed(
                    "lock_mirror",
                    "linguawiki.lock and the database mirror disagree",
                    components=", ".join(sorted(drift)),
                )
            )
        else:
            checks.append(_ok("lock_mirror", "linguawiki.lock matches the database mirror"))
        installed = {
            "core": core_pin(),
            "database_schema": migration_module.database_schema_pin(),
            "skill_bundle": skill_bundle_pin(),
        }
        mismatched = [
            component
            for component, pin in installed.items()
            if (pin.version, pin.sha256)
            != (
                getattr(lock, component).version,
                getattr(lock, component).sha256,
            )
        ]
        if mismatched:
            checks.append(
                _failed(
                    "installed_versions",
                    "the pinned versions do not match the installed core",
                    components=", ".join(sorted(mismatched)),
                )
            )
        else:
            checks.append(_ok("installed_versions", "pinned versions match the installed core"))
    return checks


#: The tables each domain check needs. A check whose tables do not exist at the applied
#: schema version is skipped rather than reported as a failure.
CHECK_REQUIREMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("identity", ("workspaces", "learning_tracks")),
    ("track_pack_binding", ("learning_tracks", "language_packs")),
    ("track_framework_binding", ("learning_tracks", "pack_frameworks")),
    (
        "pack_framework_scoping",
        (
            "pack_frameworks",
            "proficiency_descriptors",
            "resource_bundles",
            "assessment_definitions",
            "assessment_tasks",
            "content_records",
        ),
    ),
    ("content", ("content_records", "language_packs", "pack_installations")),
    ("knowledge_graph", ("knowledge_items", "knowledge_relations")),
    (
        "track_item_scope",
        ("learning_tracks", "content_records", "track_item_state", "attempts", "evidence"),
    ),
    ("evidence", ("attempts", "evidence")),
    (
        "assessment_provenance",
        ("attempts", "assessment_runs", "assessment_run_tasks"),
    ),
    ("served_answer_key", ("assessment_run_tasks", "assessment_results")),
    ("mastery", ("track_item_state", "evidence")),
    ("error_model", ("error_patterns", "error_evidence", "evidence")),
    ("estimates", ("skill_estimates", "estimate_history", "estimate_evidence", "evidence")),
    (
        "sources",
        ("sources", "source_units", "track_source_progress", "comprehension_observations"),
    ),
    ("artifacts", ("artifacts", "pronunciation_observations", "utterances")),
    ("pack_assets", ("pack_assets", "pack_files")),
    (
        "recorded_judgement",
        (
            "capture_stagings",
            "assessment_submissions",
            "assessment_results",
            "assessment_run_tasks",
            "artifacts",
        ),
    ),
    (
        "submission_lifecycle",
        (
            "assessment_submissions",
            "judging_claims",
            "judging_releases",
            "assessment_verdicts",
            "assessment_verdict_outcomes",
            "assessment_results",
            "assessment_runs",
            "schema_migrations",
        ),
    ),
    (
        "transcripts",
        (
            "utterances",
            "transcript_revisions",
            "utterance_interpretations",
            "pronunciation_observations",
        ),
    ),
    (
        "sessions",
        (
            "sessions",
            "session_blocks",
            "session_block_targets",
            "session_event_batches",
            "session_staged_events",
            "session_finalizations",
            "session_packages",
        ),
    ),
    ("projection", ("projection_state",)),
    ("version_mirror", ("workspace_versions",)),
)


def check_database(
    database: Database, *, lock: LockManifest | None = None, allow_behind_head: bool = False
) -> IntegrityReport:
    """Run every deterministic integrity check this database's schema version supports."""

    checks = _migration_checks(database, allow_behind_head=allow_behind_head)
    applied = migration_module.applied_version(database)
    if any(check.status == "failed" for check in checks):
        return IntegrityReport(
            ok=False,
            database=str(database.path),
            database_schema_version=applied,
            packaged_schema_version=migration_module.head_version(),
            checks=tuple(checks),
            row_counts={},
        )
    expected = tables_at_schema_version(applied)
    checks.extend(_table_checks(database, expected=expected))
    available = {name: set(requirements) <= expected for name, requirements in CHECK_REQUIREMENTS}
    if all(check.status != "failed" for check in checks):
        if available["identity"]:
            checks.extend(_identity_checks(database))
        if available["track_pack_binding"] and any(
            name == "pack_id" for name, _ in expected_schema(applied).get("learning_tracks", ())
        ):
            checks.extend(_track_pack_binding_checks(database))
            if available["track_framework_binding"]:
                checks.extend(_track_framework_binding_checks(database))
        if available["identity"]:
            checks.extend(_active_track_uniqueness_checks(database))
        if available["pack_framework_scoping"]:
            checks.extend(_pack_framework_scoping_checks(database))
        if available["content"]:
            checks.extend(_content_checks(database))
        if available["knowledge_graph"]:
            checks.extend(_knowledge_graph_checks(database))
        if available["track_item_scope"] and any(
            name == "pack_id" for name, _ in expected_schema(applied).get("learning_tracks", ())
        ):
            checks.extend(_track_item_scope_checks(database))
        if available["evidence"]:
            checks.extend(_evidence_checks(database))
        if available["assessment_provenance"] and any(
            name == "task_type"
            for name, _ in expected_schema(applied).get("assessment_run_tasks", ())
        ):
            checks.extend(_assessment_provenance_checks(database))
            if any(
                name == "target_refs_json"
                for name, _ in expected_schema(applied).get("assessment_run_tasks", ())
            ):
                checks.extend(_served_target_checks(database))
        if available["served_answer_key"] and any(
            name == "expected_json"
            for name, _ in expected_schema(applied).get("assessment_run_tasks", ())
        ):
            checks.extend(_served_answer_key_checks(database))
        if available["served_answer_key"] and any(
            name == "permitted_help"
            for name, _ in expected_schema(applied).get("assessment_run_tasks", ())
        ):
            checks.extend(_served_help_allowance_checks(database))
        if (
            available["served_answer_key"]
            and "assessment_task_plays" in expected
            and any(
                name == "play_count"
                for name, _ in expected_schema(applied).get("assessment_results", ())
            )
        ):
            checks.extend(_task_play_checks(database))
        if (
            available["served_answer_key"]
            and "assessment_tasks" in expected
            and any(
                name == "presentation_json"
                for name, _ in expected_schema(applied).get("assessment_run_tasks", ())
            )
        ):
            # `assessment_tasks` is in the gate because the bank half of this check reads
            # it, and a diagnostic that raises a `CatalogException` on a workspace missing
            # a table is the one thing `db check` must never do.
            checks.extend(_presentation_checks(database))
        if available["mastery"] and any(
            name == "evidence_ceiling"
            for name, _ in expected_schema(applied).get("track_item_state", ())
        ):
            checks.extend(_mastery_checks(database))
        if available["sessions"]:
            checks.extend(_session_checks(database, present=expected))
        if available["sources"]:
            checks.extend(_source_checks(database))
        if available["artifacts"]:
            checks.extend(
                _artifact_checks(
                    database,
                    external_identity=any(
                        name == "external_id"
                        for name, _ in expected_schema(applied).get("artifacts", ())
                    ),
                )
            )
        if available["transcripts"]:
            checks.extend(
                _transcript_checks(
                    database,
                    supersession=any(
                        name == "superseded_at"
                        for name, _ in expected_schema(applied).get("transcript_revisions", ())
                    ),
                    override_provenance=any(
                        name == "overrode_low_confidence"
                        for name, _ in expected_schema(applied).get("utterance_interpretations", ())
                    ),
                )
            )
        if available["pack_assets"]:
            checks.extend(_pack_asset_checks(database))
        if available["recorded_judgement"]:
            checks.extend(
                _recorded_judgement_checks(
                    database,
                    submission_kinds=any(
                        name == "kind"
                        for name, _ in expected_schema(applied).get("assessment_submissions", ())
                    ),
                )
            )
        if available["submission_lifecycle"]:
            checks.extend(_submission_lifecycle_checks(database))
        if available["error_model"]:
            checks.extend(_error_model_checks(database))
        if available["estimates"] and any(
            name == "estimate_status"
            for name, _ in expected_schema(applied).get("skill_estimates", ())
        ):
            checks.extend(_estimate_checks(database))
        checks.extend(_orphan_checks(database, schema=expected_schema(applied)))
        if available["projection"]:
            checks.extend(_projection_checks(database))
        if available["version_mirror"]:
            checks.extend(_version_mirror_checks(database, lock))
    return IntegrityReport(
        ok=all(check.status != "failed" for check in checks),
        database=str(database.path),
        database_schema_version=applied,
        packaged_schema_version=migration_module.head_version(),
        checks=tuple(checks),
        row_counts=dict(table_row_counts(database)),
    )
