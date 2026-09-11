"""The authoritative table set of the schema, and the invariant that it is complete.

`TABLE_ORDER` is the foreign-key-safe order used for export and restore. The set of
tables a given schema version *must* have is derived from the migrations themselves
rather than hard-coded, so a new migration cannot leave the two out of step.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from functools import cache

import duckdb

from linguawiki.errors import ErrorDetail, LinguaWikiError

# Parents precede children so a portable restore can insert in this exact order.
TABLE_ORDER: tuple[str, ...] = (
    "schema_migrations",
    "workspaces",
    "workspace_versions",
    "users",
    "user_languages",
    "learning_tracks",
    "track_preferences",
    "settings",
    "proficiency_frameworks",
    "proficiency_framework_levels",
    "language_packs",
    "pack_installations",
    "pack_files",
    "pack_frameworks",
    "pack_framework_levels",
    "prompt_templates",
    "generation_runs",
    "generation_batches",
    "content_records",
    "content_origins",
    "content_reviews",
    "content_dependencies",
    "batch_inspections",
    "proficiency_descriptors",
    "resource_bundles",
    "resource_bundle_items",
    "activity_templates",
    "source_recommendations",
    "knowledge_items",
    "knowledge_aliases",
    "knowledge_relations",
    "examples",
    "item_tags",
    "track_item_state",
    "assessment_definitions",
    "assessment_tasks",
    "assessment_runs",
    "assessment_run_tasks",
    "assessment_results",
    "assessment_item_exposures",
    "placement_dimension_state",
    "skill_estimates",
    "onboarding_runs",
    "onboarding_answers",
    "calibration_queue_items",
    "resource_plans",
    "resource_plan_items",
    "curricula",
    "curriculum_units",
    "curriculum_unit_objectives",
    "track_curriculum_progress",
    "curriculum_audits",
    "curriculum_audit_items",
    "sessions",
    "session_blocks",
    "session_block_targets",
    "session_plan_candidates",
    "activities",
    "session_packages",
    "session_event_batches",
    "session_staged_events",
    "session_finalizations",
    "sources",
    "source_units",
    "track_source_progress",
    "comprehension_observations",
    "source_item_links",
    "artifacts",
    "utterances",
    "transcript_revisions",
    "utterance_interpretations",
    "pronunciation_observations",
    "attempts",
    "evidence",
    "session_observations",
    "error_patterns",
    "error_occurrences",
    "error_evidence",
    "followups",
    "estimate_history",
    "estimate_evidence",
    "domain_events",
    "audit_log",
    "projection_state",
    "jobs",
)


ColumnSpecification = tuple[tuple[str, str], ...]
SchemaSpecification = dict[str, ColumnSpecification]


@cache
def expected_schema(version: int) -> SchemaSpecification:
    """The full specification of a schema version: every table and its ordered columns.

    Derived by applying migrations 1..version to an in-memory database, so it is the
    migrations that define the answer. Table names alone are not the schema: an export
    that drops a column restores cleanly and replaces the data with a column default.
    """

    from linguawiki.db import migrations as migration_module

    assert_known_schema_version(version)
    connection = duckdb.connect()
    try:
        for migration in migration_module.migrations():
            if migration.version > version:
                break
            try:
                connection.execute(migration.sql)
            except duckdb.Error as exc:
                raise LinguaWikiError(
                    "invalid_migration",
                    f"migration {migration.migration_id} cannot be applied, so the schema "
                    f"of version {version} cannot be determined",
                    details=(ErrorDetail(field=migration.migration_id, reason=type(exc).__name__),),
                ) from exc
        rows = connection.execute(
            "SELECT c.table_name, c.column_name, c.data_type "
            "FROM information_schema.columns c JOIN information_schema.tables t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = 'main' AND t.table_type = 'BASE TABLE' "
            "ORDER BY c.table_name, c.ordinal_position"
        ).fetchall()
    finally:
        connection.close()
    specification: SchemaSpecification = {}
    for table, column, data_type in rows:
        specification[str(table)] = (
            *specification.get(str(table), ()),
            (str(column), str(data_type)),
        )
    return specification


def tables_at_schema_version(version: int) -> frozenset[str]:
    """Every table a database at this schema version must contain."""

    return frozenset(expected_schema(version))


def assert_known_schema_version(version: int) -> int:
    """Require a schema version this release actually knows how to build."""

    from linguawiki.db import migrations as migration_module

    head = migration_module.head_version()
    if version < 0 or version > head:
        raise LinguaWikiError(
            "invalid_schema_version",
            f"schema version {version} is not one this release knows (0 to {head})",
            details=(ErrorDetail(field="database_schema_version", reason=str(version)),),
        )
    return version


def assert_matches_schema(
    observed: Mapping[str, Sequence[tuple[str, str]]],
    *,
    schema_version: int,
    source: str,
    code: str,
) -> None:
    """Require an observed layout to be exactly the schema version's specification.

    Checks the table set *and* each table's ordered column names and types. Fitting
    inside the expectation is never enough: a missing table loses its rows silently and
    a missing column replaces its data with a column default.
    """

    assert_known_schema_version(schema_version)
    expected = expected_schema(schema_version)
    problems: list[ErrorDetail] = []
    missing = sorted(set(expected) - set(observed))
    unknown = sorted(set(observed) - set(expected))
    if missing:
        problems.append(ErrorDetail(field="missing_tables", reason=", ".join(missing)))
    if unknown:
        problems.append(ErrorDetail(field="unknown_tables", reason=", ".join(unknown)))
    for table in sorted(set(expected) & set(observed)):
        actual = tuple((str(name), str(kind)) for name, kind in observed[table])
        if actual != expected[table]:
            problems.append(
                ErrorDetail(
                    field=table,
                    reason="column specification differs",
                    context={
                        "expected": ", ".join(f"{name} {kind}" for name, kind in expected[table]),
                        "actual": ", ".join(f"{name} {kind}" for name, kind in actual),
                    },
                )
            )
    if problems:
        raise LinguaWikiError(
            code,
            f"{source} does not match schema version {schema_version} ({len(expected)} tables)",
            details=tuple(problems),
        )


def assert_complete_table_set(
    tables: Iterable[str], *, schema_version: int, source: str, code: str
) -> frozenset[str]:
    """Require a table listing to be exactly the schema version's table set."""

    listed = list(tables)
    duplicates = sorted({name for name in listed if listed.count(name) > 1})
    if duplicates:
        raise LinguaWikiError(
            code,
            f"{source} lists a table more than once",
            details=(ErrorDetail(field="tables", reason=", ".join(duplicates)),),
        )
    expected = expected_schema(schema_version)
    assert_matches_schema(
        {table: expected.get(table, ()) for table in listed},
        schema_version=schema_version,
        source=source,
        code=code,
    )
    return frozenset(listed)
