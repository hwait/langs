"""Direct tests for the four invariants the writing paths share.

Each of these was previously enforced in one place and not another, which is how the
same class of defect kept reappearing at a different call site. These tests exercise
the invariant itself, independently of any command that relies on it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from linguawiki.db import migrations as migration_module
from linguawiki.db.backup import (
    TableExport,
    manifest_coverage,
    payload_file_name,
    resolve_payload,
)
from linguawiki.db.connection import open_temporary, quote_identifier, quote_identifiers
from linguawiki.db.schema import (
    TABLE_ORDER,
    assert_complete_table_set,
    assert_matches_schema,
    expected_schema,
    tables_at_schema_version,
)
from linguawiki.db.state import (
    INSPECTABLE_STATES,
    WRITABLE_STATES,
    DatabaseState,
    assert_writable,
    classify_database,
    schema_divergence,
)
from linguawiki.errors import LinguaWikiError
from linguawiki.services import workspace as workspace_service
from linguawiki.services.privacy import (
    CandidateSource,
    candidate_listing,
    check_privacy,
    workspace_policy,
)
from tests.support.clocks import AdvancingClock

# --- Invariant: a database at schema version N has exactly N's tables ----------------


def test_the_expected_table_set_is_derived_from_the_migrations() -> None:
    head = migration_module.head_version()

    assert tables_at_schema_version(0) == frozenset()
    assert tables_at_schema_version(head) == frozenset(TABLE_ORDER)
    for version in range(1, head + 1):
        # A migration never removes a table. It need not add one either: 0015 adds a
        # column, 0008 recreates language_packs to correct a CHECK DuckDB cannot drop,
        # and 0017 only drops an index.
        before, after = tables_at_schema_version(version - 1), tables_at_schema_version(version)
        assert before <= after, version


def test_no_migration_is_a_no_op() -> None:
    """Every numbered file has to do something.

    Asserted on the SQL rather than on the table/column projection, because a legitimate
    migration may change neither: 0016 backfills data and 0017 drops an index, and both
    leave `expected_schema` identical.
    """

    for migration in migration_module.migrations():
        statements = "\n".join(
            line
            for line in migration.sql.splitlines()
            if line.strip() and not line.strip().startswith("--")
        )
        assert statements.strip(), migration.migration_id


def test_the_table_registry_and_the_migrations_cannot_drift() -> None:
    """A new migration that forgets TABLE_ORDER fails here rather than at restore."""

    assert frozenset(TABLE_ORDER) == tables_at_schema_version(migration_module.head_version())


def test_a_complete_table_set_is_accepted() -> None:
    head = migration_module.head_version()

    assert assert_complete_table_set(
        TABLE_ORDER, schema_version=head, source="the export", code="export_incomplete"
    ) == frozenset(TABLE_ORDER)


@pytest.mark.parametrize("dropped", ["domain_events", "schema_migrations", "jobs", "workspaces"])
def test_a_table_set_missing_any_table_is_refused(dropped: str) -> None:
    """Fitting inside the expected set is not enough; a missing table loses its rows."""

    tables = [name for name in TABLE_ORDER if name != dropped]

    with pytest.raises(LinguaWikiError) as failure:
        assert_complete_table_set(
            tables,
            schema_version=migration_module.head_version(),
            source="the export",
            code="export_incomplete",
        )

    assert failure.value.payload.code == "export_incomplete"
    assert failure.value.payload.details[0].field == "missing_tables"
    assert dropped in failure.value.payload.details[0].reason


def test_an_unknown_table_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        assert_complete_table_set(
            [*TABLE_ORDER, "smuggled"],
            schema_version=migration_module.head_version(),
            source="the export",
            code="export_incomplete",
        )

    assert failure.value.payload.details[0].field == "unknown_tables"
    assert "smuggled" in failure.value.payload.details[0].reason


def test_a_duplicated_table_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        assert_complete_table_set(
            [*TABLE_ORDER, "jobs"],
            schema_version=migration_module.head_version(),
            source="the export",
            code="export_incomplete",
        )

    assert "jobs" in failure.value.payload.details[0].reason


def test_a_table_set_for_the_wrong_schema_version_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        assert_complete_table_set(
            TABLE_ORDER, schema_version=5, source="the export", code="export_incomplete"
        )

    assert failure.value.payload.details[0].field == "unknown_tables"


@pytest.mark.parametrize("version", [-1, migration_module.head_version() + 1, 999])
def test_an_unknown_schema_version_is_refused(version: int) -> None:
    """An export may not name a schema version this release cannot build."""

    with pytest.raises(LinguaWikiError) as failure:
        tables_at_schema_version(version)

    assert failure.value.payload.code == "invalid_schema_version"
    assert failure.value.payload.details[0].reason == str(version)


def test_the_specification_includes_ordered_columns_and_types() -> None:
    head = migration_module.head_version()
    specification = expected_schema(head)

    assert set(specification) == frozenset(TABLE_ORDER)
    assert specification["domain_events"][0] == ("event_id", "VARCHAR")
    assert ("payload_json", "VARCHAR") in specification["domain_events"]
    assert [name for name, _ in specification["schema_migrations"]][:2] == [
        "version",
        "migration_id",
    ]


@pytest.mark.parametrize(
    ("table", "column"),
    [("domain_events", "payload_json"), ("workspaces", "history_policy"), ("jobs", "status")],
)
def test_a_layout_missing_any_column_is_refused(table: str, column: str) -> None:
    """A dropped column restores as a default, so the column list is part of the schema."""

    head = migration_module.head_version()
    observed = {
        name: tuple(entry for entry in columns if entry[0] != column) if name == table else columns
        for name, columns in expected_schema(head).items()
    }

    with pytest.raises(LinguaWikiError) as failure:
        assert_matches_schema(
            observed, schema_version=head, source="the export", code="export_incomplete"
        )

    detail = next(item for item in failure.value.payload.details if item.field == table)
    assert detail.reason == "column specification differs"
    assert column in detail.context["expected"]
    assert column not in detail.context["actual"]


def test_a_layout_with_reordered_columns_is_refused() -> None:
    head = migration_module.head_version()
    observed = dict(expected_schema(head))
    observed["jobs"] = tuple(reversed(observed["jobs"]))

    with pytest.raises(LinguaWikiError) as failure:
        assert_matches_schema(
            observed, schema_version=head, source="the export", code="export_incomplete"
        )

    assert failure.value.payload.details[0].field == "jobs"


def test_a_layout_with_a_retyped_column_is_refused() -> None:
    head = migration_module.head_version()
    observed = dict(expected_schema(head))
    observed["jobs"] = (("job_id", "INTEGER"), *observed["jobs"][1:])

    with pytest.raises(LinguaWikiError) as failure:
        assert_matches_schema(
            observed, schema_version=head, source="the export", code="export_incomplete"
        )

    assert "job_id INTEGER" in failure.value.payload.details[0].context["actual"]


def test_the_full_specification_is_accepted() -> None:
    head = migration_module.head_version()

    assert (
        assert_matches_schema(
            expected_schema(head),
            schema_version=head,
            source="the export",
            code="export_incomplete",
        )
        is None
    )


# --- Invariant: a payload reference is canonical, contained, and checksummed ----------


@pytest.mark.parametrize(
    "reference",
    [
        "/etc/passwd.parquet",
        "../escape.parquet",
        "nested/workspaces.parquet",
        "workspaces.PARQUET",
        "other.parquet",
        "",
    ],
)
def test_a_table_may_only_reference_its_canonical_payload(reference: str) -> None:
    """A free-form reference could name a file the manifest never checksummed."""

    with pytest.raises(ValidationError):
        TableExport(table="workspaces", file=reference, row_count=0, columns=())


def test_the_canonical_payload_name_is_derived_from_the_table() -> None:
    export = TableExport(
        table="workspaces", file=payload_file_name("workspaces"), row_count=1, columns=()
    )

    assert payload_file_name("domain_events") == "domain_events.parquet"
    assert export.file == "workspaces.parquet"


def test_a_negative_row_count_is_refused() -> None:
    with pytest.raises(ValidationError):
        TableExport(table="jobs", file="jobs.parquet", row_count=-1, columns=())


def test_a_payload_must_be_covered_by_the_manifest(tmp_path: Path) -> None:
    export = TableExport(table="jobs", file="jobs.parquet", row_count=0, columns=())

    resolved = resolve_payload(tmp_path, export, covered=frozenset({"jobs.parquet"}))
    assert resolved == (tmp_path / "jobs.parquet").resolve()

    with pytest.raises(LinguaWikiError) as failure:
        resolve_payload(tmp_path, export, covered=frozenset())

    assert failure.value.payload.code == "backup_verification_failed"
    assert failure.value.payload.details[0].reason == "payload is unchecksummed"


def test_manifest_coverage_is_required(tmp_path: Path) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        manifest_coverage(tmp_path)

    assert failure.value.payload.code == "backup_manifest_missing"


# --- Invariant: every identifier reaching SQL is quoted -------------------------------

HOSTILE_IDENTIFIERS = (
    'x"; DROP TABLE jobs; --',
    'y"; DELETE FROM workspaces; --',
    'z" UNION SELECT 1; --',
    'a""b',
    "o'brien",
    "with space",
    "trailing\\",
)


@pytest.mark.parametrize("name", HOSTILE_IDENTIFIERS)
def test_a_hostile_identifier_is_quoted_not_wrapped(name: str) -> None:
    """Anyone who can add a table chooses its name, so the name is data."""

    quoted = quote_identifier(name)

    assert quoted.startswith('"') and quoted.endswith('"')
    # Every embedded quote is doubled, so the identifier cannot be terminated early.
    assert quoted[1:-1].count('"') % 2 == 0
    assert quoted[1:-1].replace('""', '"') == name


def test_a_nul_byte_in_an_identifier_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        quote_identifier("bad\x00name")

    assert failure.value.payload.code == "invalid_identifier"


def test_quoted_identifier_lists_are_safe() -> None:
    assert quote_identifiers(["a", 'b"c']) == '"a", "b""c"'


@pytest.mark.parametrize("name", HOSTILE_IDENTIFIERS)
def test_counting_a_hostile_table_is_literal_and_harmless(
    name: str, tmp_path: Path, clock: AdvancingClock
) -> None:
    """The count must be of that table, and nothing else may be touched."""

    path = _database(tmp_path, "hostile.duckdb")
    with open_temporary(path, clock=clock) as database:
        migration_module.migrate(database)
        with database.transaction() as transaction:
            transaction.execute(f"CREATE TABLE {quote_identifier(name)}(id VARCHAR)")
            transaction.execute(f"INSERT INTO {quote_identifier(name)} VALUES ('a'), ('b')")
        before = sorted(database.table_names())

        assert database.count(name) == 2
        assert sorted(database.table_names()) == before
        assert database.count("jobs") == 0
        assert database.count("workspaces") == 0


def test_no_source_file_quotes_an_identifier_by_hand(tmp_path: Path) -> None:
    """A lint keeps the quoting invariant from regressing anywhere in the package."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        import check_sql_identifiers as lint
    finally:
        sys.path.pop(0)

    assert [
        (path, problem) for path in lint.package_files() for problem in lint.violations(path)
    ] == []
    assert lint.main([]) == 0

    offender = tmp_path / "offender.py"
    offender.write_text(
        "def bad(database, table):\n"
        "    return database.scalar(f'SELECT count(*) FROM \"{table}\"')\n",
        encoding="utf-8",
    )
    assert lint.violations(offender)

    clean = tmp_path / "clean.py"
    clean.write_text(
        "def fine(database, table):\n"
        '    return database.scalar(f"SELECT count(*) FROM {quote_identifier(table)}")\n',
        encoding="utf-8",
    )
    assert lint.violations(clean) == []


# --- Invariant: only a recognized database may be written to -------------------------


def _database(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_an_absent_or_empty_database_is_empty(tmp_path: Path, clock: AdvancingClock) -> None:
    path = _database(tmp_path, "empty.duckdb")
    duckdb.connect(str(path)).close()

    with open_temporary(path, clock=clock) as database:
        assert classify_database(database) is DatabaseState.EMPTY
        assert assert_writable(database, command="db init") is DatabaseState.EMPTY


def test_a_fully_migrated_database_is_managed(tmp_path: Path, clock: AdvancingClock) -> None:
    path = _database(tmp_path, "managed.duckdb")
    with open_temporary(path, clock=clock) as database:
        migration_module.migrate(database)

        assert classify_database(database) is DatabaseState.MANAGED


def test_a_partially_migrated_database_is_managed(tmp_path: Path, clock: AdvancingClock) -> None:
    """Stopping part-way is legitimate; its table set matches its claimed version."""

    path = _database(tmp_path, "partial.duckdb")
    with open_temporary(path, clock=clock) as database:
        for migration in migration_module.migrations()[:4]:
            with database.transaction() as transaction:
                transaction.execute(migration.sql)
                transaction.execute(
                    "INSERT INTO schema_migrations "
                    "(version, migration_id, checksum, application_version, applied_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        migration.version,
                        migration.migration_id,
                        migration.checksum,
                        "0.1.0",
                        transaction.now(),
                    ],
                )

        assert classify_database(database) is DatabaseState.MANAGED


@pytest.mark.parametrize(
    ("label", "statements"),
    [
        ("unrelated tables", ["CREATE TABLE somebody_elses_data(id INTEGER)"]),
        (
            "history table but no rows",
            ["CREATE TABLE schema_migrations(version INTEGER)"],
        ),
        (
            "history table with foreign columns",
            [
                "CREATE TABLE schema_migrations(other VARCHAR)",
                "INSERT INTO schema_migrations VALUES ('x')",
            ],
        ),
    ],
)
def test_a_database_linguawiki_did_not_create_is_unmanaged(
    label: str, statements: list[str], tmp_path: Path, clock: AdvancingClock
) -> None:
    path = _database(tmp_path, "alien.duckdb")
    connection = duckdb.connect(str(path))
    for statement in statements:
        connection.execute(statement)
    connection.close()

    with open_temporary(path, clock=clock) as database:
        assert classify_database(database) is DatabaseState.UNMANAGED
        with pytest.raises(LinguaWikiError) as failure:
            assert_writable(database, command="db migrate")

    assert failure.value.payload.code == "database_not_empty"


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("history dropped column", "ALTER TABLE schema_migrations DROP COLUMN application_version"),
        ("history retyped column", "ALTER TABLE schema_migrations ALTER applied_at TYPE VARCHAR"),
        ("history extra column", "ALTER TABLE schema_migrations ADD COLUMN note VARCHAR"),
        ("extra table", "CREATE TABLE hand_made(id VARCHAR)"),
        ("dropped table", "DROP TABLE jobs"),
        ("extra column", "ALTER TABLE workspaces ADD COLUMN smuggled VARCHAR"),
        ("dropped column", "ALTER TABLE jobs DROP COLUMN error_message"),
        ("retyped column", "ALTER TABLE jobs ALTER started_at TYPE VARCHAR"),
    ],
)
def test_a_layout_that_contradicts_its_history_is_damaged(
    label: str, statement: str, tmp_path: Path, clock: AdvancingClock
) -> None:
    """Ownership comes from the history; soundness comes from the complete schema.

    Each of these has a perfectly valid migration history, so the database is
    unmistakably ours — but its layout is not what that history should have produced.
    """

    path = _database(tmp_path, f"{label.replace(' ', '-')}.duckdb")
    with open_temporary(path, clock=clock) as database:
        migration_module.migrate(database)
        with database.transaction() as transaction:
            transaction.execute(statement)

        assert classify_database(database) is DatabaseState.DAMAGED
        # Refused by ordinary writers...
        with pytest.raises(LinguaWikiError) as failure:
            assert_writable(database, command="db migrate")
        assert failure.value.payload.code == "database_damaged"
        # ...but still preservable, which a foreign database would not be.
        assert (
            assert_writable(database, command="db backup", allow_damaged=True)
            is DatabaseState.DAMAGED
        )
        assert schema_divergence(database)


def test_a_database_with_a_corrupt_history_is_damaged_not_writable(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    """Recognizing a database as ours is not the same as declaring it safe to write."""

    path = _database(tmp_path, "divergent.duckdb")
    with open_temporary(path, clock=clock) as database:
        migration_module.migrate(database)
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 2", ["a" * 64]
            )

        assert classify_database(database) is DatabaseState.DAMAGED
        with pytest.raises(LinguaWikiError) as failure:
            assert_writable(database, command="db migrate")
        assert failure.value.payload.code == "database_damaged"
        # Backing it up before repair must still be possible.
        assert (
            assert_writable(database, command="db backup", allow_damaged=True)
            is DatabaseState.DAMAGED
        )


def test_a_database_from_a_newer_release_is_unsupported_not_writable(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    """An older core must never mutate a database a newer one wrote."""

    path = _database(tmp_path, "future.duckdb")
    with open_temporary(path, clock=clock) as database:
        migration_module.migrate(database)
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO schema_migrations "
                "(version, migration_id, checksum, application_version, applied_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [999, "0999_from_the_future", "f" * 64, "9.9.9", transaction.now()],
            )

        assert classify_database(database) is DatabaseState.UNSUPPORTED
        with pytest.raises(LinguaWikiError) as failure:
            assert_writable(database, command="db migrate")
        assert failure.value.payload.code == "database_unsupported"
        assert (
            assert_writable(database, command="db backup", allow_damaged=True)
            is DatabaseState.UNSUPPORTED
        )


def test_only_empty_and_managed_databases_are_writable() -> None:
    """The writable set is explicit, so a new state defaults to refused."""

    assert frozenset({DatabaseState.EMPTY, DatabaseState.MANAGED}) == WRITABLE_STATES
    assert frozenset({DatabaseState.DAMAGED, DatabaseState.UNSUPPORTED}) == INSPECTABLE_STATES
    assert set(DatabaseState) == WRITABLE_STATES | INSPECTABLE_STATES | {DatabaseState.UNMANAGED}


def test_ownership_is_detectable_without_typed_decoding(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    """Drift inside the history table must not disown the database."""

    path = _database(tmp_path, "drifted-history.duckdb")
    with open_temporary(path, clock=clock) as database:
        migration_module.migrate(database)
        head = migration_module.head_version()
        with database.transaction() as transaction:
            transaction.execute("ALTER TABLE schema_migrations ALTER applied_at TYPE VARCHAR")

        # Raw reads still work; typed decoding declares its precondition instead of
        # raising an unexpected error.
        assert migration_module.recorded_migration_ids(database)
        assert migration_module.claimed_schema_version(database) == head
        assert len(migration_module.raw_history(database)) == head
        with pytest.raises(LinguaWikiError) as failure:
            migration_module.applied_migrations(database)
        assert failure.value.payload.code == "history_table_malformed"


def test_a_foreign_history_table_is_still_disowned(tmp_path: Path, clock: AdvancingClock) -> None:
    """Resilient ownership detection must not adopt somebody else's table."""

    path = _database(tmp_path, "foreign-history.duckdb")
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE schema_migrations(migration_id VARCHAR)")
    connection.execute("INSERT INTO schema_migrations VALUES ('their_own_migration')")
    connection.close()

    with open_temporary(path, clock=clock) as database:
        assert migration_module.recorded_migration_ids(database) == ("their_own_migration",)
        assert classify_database(database) is DatabaseState.UNMANAGED


def test_a_history_table_without_migration_ids_is_disowned(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    path = _database(tmp_path, "no-ids.duckdb")
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE schema_migrations(version INTEGER)")
    connection.execute("INSERT INTO schema_migrations VALUES (1)")
    connection.close()

    with open_temporary(path, clock=clock) as database:
        assert migration_module.recorded_migration_ids(database) is None
        assert classify_database(database) is DatabaseState.UNMANAGED


def test_the_history_table_shape_survives_a_broken_later_migration() -> None:
    """Reading the history matters most when a later migration is broken."""

    columns = migration_module.history_table_columns()

    assert [name for name, _ in columns][:2] == ["version", "migration_id"]
    assert columns == expected_schema(migration_module.head_version())["schema_migrations"]


# --- Invariant: dependency resolution is evaluated, not name-matched -----------------


def test_every_transitive_constraint_is_retained() -> None:
    constraints = workspace_service.core_dependency_constraints()

    assert str(constraints["duckdb"]) == f"=={duckdb.__version__}"
    # A range constraint must survive; discarding it let a lock claim 0.0.0.
    assert str(constraints["markupsafe"]).startswith(">=")
    assert set(workspace_service.core_requirements()) < set(constraints)


def test_a_version_below_every_range_constraint_does_not_satisfy_it() -> None:
    """The exact hole that was open: range-constrained deps claimed as 0.0.0."""

    constraints = workspace_service.core_dependency_constraints()
    ranged = {
        name: specifiers
        for name, specifiers in constraints.items()
        if str(specifiers) and not str(specifiers).startswith("==")
    }

    assert ranged, "the core's graph should contain range-constrained dependencies"
    for name, specifiers in ranged.items():
        assert not specifiers.contains("0.0.0", prereleases=True), f"{name} {specifiers}"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ({}, "empty source"),
        ({"unknown": "x"}, "not a recognised uv variant"),
        ({"registry": "x", "git": "y"}, "not a recognised uv variant"),
        ({"registry": ""}, "no location"),
        ({"registry": 1}, "no location"),
        ("not-a-mapping", "without a source"),
        (None, "without a source"),
    ],
)
def test_a_source_that_is_not_one_uv_variant_is_refused(source: object, reason: str) -> None:
    """`source = {}` is a mapping but matches no uv variant, and uv refuses it."""

    problem = workspace_service._source_variant_problem(source)

    assert problem is not None
    assert reason in problem


@pytest.mark.parametrize(
    "source",
    [
        {"registry": "https://pypi.org/simple"},
        {"git": "https://example.invalid/x.git"},
        {"directory": "."},
        {"editable": "."},
        {"virtual": "."},
        {"path": "dist/x.whl"},
        {"url": "https://example.invalid/x.whl"},
    ],
)
def test_every_recognised_uv_source_variant_is_accepted(source: dict[str, str]) -> None:
    assert workspace_service._source_variant_problem(source) is None


def test_the_retained_lock_uses_only_recognised_source_variants() -> None:
    import tomllib

    payload = tomllib.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "workspace-uv-lock" / "uv.lock"
        ).read_text(encoding="utf-8")
    )

    for package in payload["package"]:
        assert workspace_service._source_variant_problem(package.get("source")) is None, package[
            "name"
        ]


@pytest.mark.parametrize("version", [0, 2, 999, -1])
def test_an_unsupported_uv_lock_format_version_is_refused(version: int) -> None:
    assert version not in workspace_service.SUPPORTED_UV_LOCK_VERSIONS


REGISTRY = "registry=https://pypi.org/simple"
MIRROR = "registry=https://mirror.invalid/simple"


def _identity(name: str, version: str, source: str = REGISTRY) -> object:
    return workspace_service.PackageIdentity(name=name, version=version, source=source)


def _edge(name: str, version: str | None = None, source: str | None = None) -> object:
    return workspace_service.DependencyEdge(name=name, version=version, source=source)


def test_an_edge_selects_an_identity_not_a_name() -> None:
    """Two versions of one package are legitimate; an edge names which one it takes."""

    stub = _identity("markupsafe", "0.0.0")
    real = _identity("markupsafe", "3.0.3")
    by_name = {"markupsafe": [stub, real]}

    assert workspace_service._resolve_edge(_edge("markupsafe", "0.0.0"), by_name) == stub
    # An unqualified edge cannot pick between two candidates.
    assert workspace_service._resolve_edge(_edge("markupsafe"), by_name) is None
    assert workspace_service._resolve_edge(_edge("markupsafe"), {"markupsafe": [real]}) == real
    assert workspace_service._resolve_edge(_edge("markupsafe", "9.9.9"), by_name) is None


def test_the_same_version_from_two_sources_is_two_identities() -> None:
    """uv identifies a node by name, version, *and* source."""

    from_index = _identity("markupsafe", "3.0.3", REGISTRY)
    from_mirror = _identity("markupsafe", "3.0.3", MIRROR)
    by_name = {"markupsafe": [from_index, from_mirror]}

    assert from_index != from_mirror
    # Version alone no longer disambiguates, so the edge must state the source.
    assert workspace_service._resolve_edge(_edge("markupsafe", "3.0.3"), by_name) is None
    assert (
        workspace_service._resolve_edge(_edge("markupsafe", "3.0.3", MIRROR), by_name)
        == from_mirror
    )
    assert workspace_service._resolve_edge(_edge("markupsafe", None, REGISTRY), by_name) == (
        from_index
    )


def test_the_canonical_source_form_is_stable_and_distinguishing() -> None:
    canonical = workspace_service.canonical_source

    assert canonical({"registry": "https://pypi.org/simple"}) == REGISTRY
    assert canonical({"b": "2", "a": "1"}) == canonical({"a": "1", "b": "2"})
    assert canonical({"registry": "x"}) != canonical({"git": "x"})
    assert canonical(None) == ""


@pytest.mark.parametrize(
    ("declared", "reason"),
    [
        (1, "not a list"),
        ("markupsafe", "not a list"),
        ({"name": "markupsafe"}, "not a list"),
        ([1], "not a mapping"),
        (["markupsafe"], "not a mapping"),
        ([{}], "without a name"),
        ([{"name": ""}], "without a name"),
        ([{"name": 1}], "without a name"),
        ([{"name": "markupsafe", "version": 3}], "version is not a string"),
        ([{"name": "markupsafe", "source": "registry"}], "source is not a mapping"),
    ],
)
def test_a_malformed_dependency_shape_is_named_not_raised(declared: object, reason: str) -> None:
    """A parsable-but-wrong shape must be a diagnostic, never a TypeError."""

    problem = workspace_service._dependency_edges({"dependencies": declared})

    assert isinstance(problem, str)
    assert reason in problem


@pytest.mark.parametrize(
    "declared",
    [
        [],
        [{"name": "markupsafe"}],
        [{"name": "markupsafe", "version": "3.0.3"}],
        [{"name": "markupsafe", "version": "3.0.3", "source": {"registry": "x"}}],
    ],
)
def test_well_formed_dependency_shapes_are_parsed(declared: object) -> None:
    edges = workspace_service._dependency_edges({"dependencies": declared})

    assert not isinstance(edges, str)
    assert len(edges) == len(declared)  # type: ignore[arg-type]


def test_a_package_with_no_dependencies_key_has_no_edges() -> None:
    assert workspace_service._dependency_edges({}) == ()


def test_traversal_follows_the_selected_identity() -> None:
    """Traversing by name let a later entry stand in for the one an edge selected."""

    root = _identity("app", "1.0")
    stub = _identity("dep", "0.0.0")
    real = _identity("dep", "3.0.3")
    graph = {root: (_edge("dep", "0.0.0"),), stub: (), real: ()}
    by_name = {"app": [root], "dep": [stub, real]}

    reachable = workspace_service._reachable(graph, by_name, root)

    assert reachable == {root, stub}
    assert real not in reachable


def test_the_retained_lock_satisfies_every_constraint() -> None:
    """The one lock uv produced must satisfy exactly what the constraints demand."""

    import tomllib

    payload = tomllib.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "workspace-uv-lock" / "uv.lock"
        ).read_text(encoding="utf-8")
    )
    resolved = {package["name"]: package["version"] for package in payload["package"]}

    for name, specifiers in workspace_service.core_dependency_constraints().items():
        assert name in resolved, name
        assert specifiers.contains(resolved[name], prereleases=True), f"{name} {specifiers}"


# --- Invariant: a Git candidate listing either succeeds or fails closed ---------------


def test_a_directory_without_git_lists_from_the_filesystem(tmp_path: Path) -> None:
    listing = candidate_listing(tmp_path, workspace_policy())

    assert listing.source == CandidateSource.FILESYSTEM
    assert listing.failure is None


@pytest.mark.skipif(subprocess.run(["which", "git"], check=False).returncode != 0, reason="no git")
def test_a_git_repository_lists_from_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.md").write_text("x", encoding="utf-8")

    listing = candidate_listing(tmp_path, workspace_policy())

    assert listing.source == CandidateSource.GIT
    assert listing.failure is None
    assert "tracked.md" in listing.paths


def test_a_failing_git_inspection_is_never_a_filesystem_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filesystem listing skips private artifacts, so it must not stand in here."""

    (tmp_path / ".git").mkdir()
    failing = tmp_path / "bin"
    failing.mkdir()
    (failing / "git").write_text("#!/bin/sh\nprintf 'fatal: broken\\n' >&2\nexit 128\n")
    (failing / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(failing))

    listing = candidate_listing(tmp_path, workspace_policy())
    report = check_privacy(tmp_path)

    assert listing.source == CandidateSource.UNAVAILABLE
    assert listing.paths == ()
    assert "exit 128" in (listing.failure or "")
    assert report.ok is False
    assert report.inspection_failure is not None


def test_a_missing_git_binary_in_a_repository_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    report = check_privacy(tmp_path)

    assert report.ok is False
    assert report.source == CandidateSource.UNAVAILABLE
    assert "git is not installed" in (report.inspection_failure or "")
