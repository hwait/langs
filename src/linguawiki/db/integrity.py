"""Structured database integrity and drift checks behind `linguawiki db check`."""

from __future__ import annotations

import json

from pydantic import Field

from linguawiki.contracts import LockManifest
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


def _session_checks(database: Database) -> list[CheckResult]:
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
    }
    unresolved_targets: list[str] = []
    for kind, (table, column) in tables.items():
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


def _orphan_checks(database: Database, *, schema: SchemaSpecification) -> list[CheckResult]:
    orphans: list[str] = []

    def present(table: str, column: str) -> bool:
        # A relation is checkable only where both of its columns exist. Gating on table
        # names alone was enough until a relation arrived as an added column, which a
        # database still behind that migration does not have.
        return any(name == column for name, _ in schema.get(table, ()))

    relations = [
        relation
        for relation in ORPHAN_RELATIONS
        if present(relation[0], relation[1]) and present(relation[2], relation[3])
    ]
    for table, column, parent, parent_column in relations:
        child_table = quote_identifier(table)
        parent_table = quote_identifier(parent)
        child_column = f"child.{quote_identifier(column)}"
        parent_key = f"parent.{quote_identifier(parent_column)}"
        count = int(
            database.scalar(
                f"SELECT count(*) FROM {child_table} child "
                f"LEFT JOIN {parent_table} parent ON {child_column} = {parent_key} "
                f"WHERE {child_column} IS NOT NULL AND {parent_key} IS NULL"
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
    ("mastery", ("track_item_state", "evidence")),
    ("error_model", ("error_patterns", "error_evidence", "evidence")),
    ("estimates", ("skill_estimates", "estimate_history", "estimate_evidence", "evidence")),
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
        if available["mastery"] and any(
            name == "evidence_ceiling"
            for name, _ in expected_schema(applied).get("track_item_state", ())
        ):
            checks.extend(_mastery_checks(database))
        if available["sessions"]:
            checks.extend(_session_checks(database))
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
