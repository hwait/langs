from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest

from linguawiki.db import backup as backup_module
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import open_temporary
from linguawiki.db.schema import TABLE_ORDER, tables_at_schema_version
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from tests.conftest import SyntheticWorkspace
from tests.support import portable_fixture
from tests.support.clocks import AdvancingClock, FixedClock

MANIFEST = "linguawiki.backup-manifest.v1"
#: Every retained export shape. Each must still restore; only the one for this release's
#: head is expected to match the *current* export shape.
RETAINED_SCHEMA_VERSIONS = (7, 22, 23, 26, 29, 30, 31, 32, 33, 34)
HEAD_SCHEMA_VERSION = migration_module.head_version()


def _snapshot(path: Path, clock: AdvancingClock) -> dict[str, object]:
    """Read the comparable contents of a database: counts, IDs, and version pins."""

    with open_temporary(path, clock=clock) as database:
        return {
            "schema_version": migration_module.applied_version(database),
            "row_counts": dict(backup_module.table_row_counts(database)),
            "workspace": database.one(
                "SELECT workspace_id, name, normalized_name, history_policy, timezone, "
                "created_at FROM workspaces"
            ),
            "versions": database.query(
                "SELECT component, component_key, version, sha256 FROM workspace_versions "
                "ORDER BY component, component_key"
            ),
            "events": database.query(
                "SELECT event_id, event_type, aggregate_id, idempotency_key FROM domain_events "
                "ORDER BY event_id"
            ),
            "projection": database.query(
                "SELECT projection, projection_version, content_hash, stale FROM projection_state "
                "ORDER BY projection"
            ),
            "migrations": [
                (record.version, record.migration_id, record.checksum)
                for record in migration_module.applied_migrations(database)
            ],
        }


def test_backup_creates_verified_native_and_portable_layers(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
        reason="test",
    )
    directory = Path(report.directory)

    assert report.verified is True
    assert report.total_rows > 0
    assert Path(report.native_database or "").is_file()
    assert (directory / "portable" / "metadata.json").is_file()
    assert (directory / "manifest.json").is_file()
    assert {export.table for export in report.tables} == set(TABLE_ORDER)
    for export in report.tables:
        assert (directory / "portable" / export.file).is_file()


def test_the_manifest_checksums_every_backup_file(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    directory = Path(report.directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert set(manifest["files"]) == files
    backup_module.verify_manifest(directory)


def test_a_tampered_backup_fails_verification(synthetic_workspace: SyntheticWorkspace) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    directory = Path(report.directory)
    (directory / "portable" / "workspaces.parquet").write_bytes(b"corrupted")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_manifest(directory)

    assert failure.value.payload.code == "backup_verification_failed"
    assert {detail.reason for detail in failure.value.payload.details} == {"byte count differs"}


def test_a_missing_backup_file_fails_verification(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    directory = Path(report.directory)
    (directory / "portable" / "jobs.parquet").unlink()

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_manifest(directory)

    assert failure.value.payload.details[0].reason == "file is missing"


@pytest.mark.parametrize("layer", ["portable/workspaces.parquet", "native/linguawiki.duckdb"])
def test_a_payload_file_removed_from_the_manifest_fails_verification(
    layer: str, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Dropping a file from the manifest must not exempt it from verification."""

    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    directory = Path(report.directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"][layer]
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / layer).write_bytes(b"replaced payload")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(
            directory, tmp_path / "restored.duckdb", clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "backup_verification_failed"
    assert failure.value.payload.details[0].field == layer
    assert failure.value.payload.details[0].reason == "file is not covered by the manifest"
    assert not (tmp_path / "restored.duckdb").exists()


def test_an_extra_file_added_to_a_backup_fails_verification(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    (Path(report.directory) / "portable" / "smuggled.parquet").write_bytes(b"extra")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_manifest(Path(report.directory))

    assert failure.value.payload.details[0].field == "portable/smuggled.parquet"


def test_a_payload_resized_without_changing_its_length_fails_verification(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """Byte counts and checksums are both recorded, so either change is caught."""

    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    target = Path(report.directory) / "portable" / "workspaces.parquet"
    original = target.read_bytes()
    target.write_bytes(bytes(len(original)))

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_manifest(Path(report.directory))

    assert failure.value.payload.details[0].reason == "checksum differs"


@pytest.mark.parametrize(
    ("manifest", "reason"),
    [
        ({"schema_name": "something.else", "schema_version": 1, "files": {}}, "manifest"),
        ({"schema_name": MANIFEST, "schema_version": 999, "files": {}}, "manifest"),
        ({"schema_name": MANIFEST, "files": {}}, "manifest"),
        ({"schema_name": MANIFEST, "schema_version": 1}, "lists no files"),
        (
            {
                "schema_name": MANIFEST,
                "schema_version": 1,
                "files": {"../escape": {"sha256": "a" * 64, "bytes": 1}},
            },
            "names an unsafe path",
        ),
        (
            {
                "schema_name": MANIFEST,
                "schema_version": 1,
                "files": {"/etc/passwd": {"sha256": "a" * 64, "bytes": 1}},
            },
            "names an unsafe path",
        ),
        (
            {
                "schema_name": MANIFEST,
                "schema_version": 1,
                "files": {"portable/x.parquet": {"sha256": "not-a-hash", "bytes": 1}},
            },
            "malformed entry",
        ),
        (
            {
                "schema_name": MANIFEST,
                "schema_version": 1,
                "files": {"portable/x.parquet": {"sha256": "a" * 64}},
            },
            "malformed entry",
        ),
    ],
)
def test_a_malformed_manifest_is_refused(
    manifest: dict[str, object], reason: str, tmp_path: Path
) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_manifest(tmp_path)

    assert failure.value.payload.code == "backup_verification_failed"
    assert reason in failure.value.payload.message


def test_an_unreadable_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_manifest(tmp_path)

    assert "unreadable" in failure.value.payload.message


def test_a_missing_manifest_fails_verification(tmp_path: Path) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_manifest(tmp_path)

    assert failure.value.payload.code == "backup_verification_failed"


@pytest.mark.parametrize("kind", ["native", "portable"])
def test_restore_refuses_a_backup_whose_manifest_does_not_match(
    kind: str, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Restore must verify the checksum manifest before reading any payload."""

    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    directory = Path(report.directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["portable/workspaces.parquet"]["sha256"] = "0" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / f"{kind}-restore.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=synthetic_workspace.clock, kind=kind)

    assert failure.value.payload.code == "backup_verification_failed"
    assert not target.exists()


@pytest.mark.parametrize("dropped", ["domain_events", "jobs", "workspace_versions"])
def test_restore_refuses_an_export_missing_a_whole_table(
    dropped: str, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """A table absent from the metadata used to restore as silently empty."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    metadata["tables"] = [item for item in metadata["tables"] if item["table"] != dropped]
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (directory / f"{dropped}.parquet").unlink()
    backup_module.write_manifest(directory)
    target = tmp_path / "restored.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code == "backup_verification_failed"
    assert dropped in failure.value.payload.details[0].reason
    assert not target.exists()


@pytest.mark.parametrize(
    ("table", "column"),
    [("domain_events", "payload_json"), ("workspaces", "history_policy"), ("jobs", "status")],
)
def test_restore_refuses_an_export_missing_a_column(
    table: str, column: str, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """A dropped column used to restore as a column default, replacing real data."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    parquet = directory / f"{table}.parquet"
    connection = duckdb.connect()
    kept = [
        str(name)
        for (name,) in connection.execute(
            "SELECT name FROM parquet_schema(?) WHERE num_children IS NULL", [str(parquet)]
        ).fetchall()
        if name != column
    ]
    connection.execute(
        f"COPY (SELECT {', '.join(kept)} FROM read_parquet(?)) TO ? (FORMAT PARQUET)",
        [str(parquet), str(parquet)],
    )
    connection.close()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    for item in metadata["tables"]:
        if item["table"] == table:
            item["columns"] = [entry for entry in item["columns"] if entry[0] != column]
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    backup_module.write_manifest(directory)
    target = tmp_path / "restored.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code == "backup_verification_failed"
    detail = next(item for item in failure.value.payload.details if item.field == table)
    assert column in detail.context["expected"]
    assert not target.exists()


def test_restore_refuses_an_export_whose_parquet_lost_a_column(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Metadata that describes columns its own Parquet file does not have."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    parquet = directory / "domain_events.parquet"
    connection = duckdb.connect()
    connection.execute(
        "COPY (SELECT event_id FROM read_parquet(?)) TO ? (FORMAT PARQUET)",
        [str(parquet), str(parquet)],
    )
    connection.close()
    backup_module.write_manifest(directory)

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, tmp_path / "restored.duckdb", clock=clock)

    assert failure.value.payload.code == "backup_verification_failed"
    detail = next(item for item in failure.value.payload.details if item.field == "domain_events")
    assert detail.reason == "the parquet file does not have the described columns"


@pytest.mark.parametrize(
    ("column", "expression"),
    [("name", "encode(name)"), ("created_at", "epoch_ms(created_at)")],
)
def test_restore_refuses_an_export_whose_column_type_changed(
    column: str, expression: str, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """VARCHAR and BLOB share a physical Parquet type; the logical type is what matters."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    parquet = directory / "workspaces.parquet"
    connection = duckdb.connect()
    names = [
        str(name)
        for (name,) in connection.execute(
            "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))", [str(parquet)]
        ).fetchall()
    ]
    projection = ", ".join(
        f"{expression} AS {column}" if name == column else name for name in names
    )
    connection.execute(
        f"COPY (SELECT {projection} FROM read_parquet(?)) TO ? (FORMAT PARQUET)",
        [str(parquet), str(parquet)],
    )
    connection.close()
    backup_module.write_manifest(directory)
    target = tmp_path / "restored.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code == "backup_verification_failed"
    detail = next(item for item in failure.value.payload.details if item.field == "workspaces")
    assert detail.reason == "the parquet file does not have the described columns"
    assert not target.exists()


def test_parquet_columns_reports_logical_types(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """The comparison basis is DuckDB's inference, not the physical Parquet type."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)

    with open_temporary(Path(":memory:"), clock=clock) as scratch:
        columns = dict(backup_module.parquet_columns(scratch, directory / "workspaces.parquet"))

    assert columns["name"] == "VARCHAR"
    assert columns["created_at"] == "TIMESTAMP"
    assert columns["is_singleton"] == "BOOLEAN"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"schema_name": "totally.made.up"}, "portable_metadata_invalid"),
        ({"schema_version": 42}, "portable_metadata_invalid"),
        ({"migrations": []}, "restore_schema_unsupported"),
    ],
)
def test_restore_refuses_an_export_with_an_unenforced_contract(
    mutation: dict[str, object],
    code: str,
    synthetic_workspace: SyntheticWorkspace,
    tmp_path: Path,
) -> None:
    """Format identifiers and the migration prefix are contracts, not decoration."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    metadata.update(mutation)
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    backup_module.write_manifest(directory)

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, tmp_path / "restored.duckdb", clock=clock)

    assert failure.value.payload.code == code


def test_restore_refuses_a_payload_reference_outside_the_export(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """An absolute reference read a file no manifest had ever checksummed."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    outside = tmp_path / "outside.parquet"
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE staged AS SELECT * REPLACE ('Unchecked payload' AS name) "
        "FROM read_parquet(?)",
        [str(directory / "workspaces.parquet")],
    )
    connection.execute("COPY staged TO ? (FORMAT PARQUET)", [str(outside)])
    connection.close()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    for item in metadata["tables"]:
        if item["table"] == "workspaces":
            item["file"] = str(outside)
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    backup_module.write_manifest(directory)
    target = tmp_path / "restored.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code == "portable_metadata_invalid"
    assert not target.exists()


def test_restore_refuses_a_payload_dropped_from_the_manifest(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Metadata references and manifest entries must describe the same file set."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"]["workspaces.parquet"]
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, tmp_path / "restored.duckdb", clock=clock)

    assert failure.value.payload.code == "backup_verification_failed"


def test_restore_refuses_an_export_with_a_duplicated_table_entry(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Mapping the sequence by name hid duplicates and inflated the row count."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    metadata["tables"] += [item for item in metadata["tables"] if item["table"] == "workspaces"]
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    backup_module.write_manifest(directory)

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, tmp_path / "restored.duckdb", clock=clock)

    assert failure.value.payload.code == "portable_metadata_invalid"
    assert "workspaces" in str(failure.value.payload.details)


def test_a_damaged_database_can_still_be_backed_up(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The database most worth preserving is the one that needs repair."""

    from linguawiki.db.connection import open_writer

    with (
        open_writer(
            synthetic_workspace.paths, command="test", clock=synthetic_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 2", ["a" * 64]
        )

    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
        portable=False,
    )

    assert Path(report.native_database or "").is_file()

    with pytest.raises(LinguaWikiError) as failure:
        database_service.migrate(
            synthetic_workspace.paths,
            backup_root=synthetic_workspace.backup_root,
            clock=synthetic_workspace.clock,
        )

    assert failure.value.payload.code == "database_damaged"


@pytest.mark.parametrize("claimed", [999, migration_module.head_version() + 1, -1])
def test_restore_refuses_an_export_claiming_an_unknown_schema_version(
    claimed: int, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """An export used to be restorable while reporting a schema version that cannot exist."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    metadata["database_schema_version"] = claimed
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    backup_module.write_manifest(directory)
    target = tmp_path / "restored.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code in {"invalid_schema_version", "restore_schema_unsupported"}
    assert not target.exists()


def test_restore_refuses_an_export_with_an_unknown_table(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    metadata["tables"].append(
        {"table": "smuggled", "file": "smuggled.parquet", "row_count": 0, "columns": []}
    )
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (directory / "smuggled.parquet").write_bytes(b"")
    backup_module.write_manifest(directory)

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, tmp_path / "restored.duckdb", clock=clock)

    assert "smuggled" in failure.value.payload.details[0].reason


def _empty_a_table(directory: Path, table: str) -> None:
    """Rewrite one Parquet file as zero rows and re-stamp the metadata and manifest."""

    connection = duckdb.connect()
    path = directory / f"{table}.parquet"
    connection.execute(
        "COPY (SELECT * FROM read_parquet(?) WHERE FALSE) TO ? (FORMAT PARQUET)",
        [str(path), str(path)],
    )
    connection.close()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    for item in metadata["tables"]:
        if item["table"] == table:
            item["row_count"] = 0
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    backup_module.write_manifest(directory)


def test_a_restore_must_pass_the_same_integrity_checks_as_a_live_database(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Restoring is not finished until the result would pass `db check`."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    # projection_state has no foreign keys, so this reaches the integrity gate itself.
    _empty_a_table(directory, "projection_state")
    target = tmp_path / "restored.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code == "restore_verification_failed"
    assert any("projection_state" in detail.reason for detail in failure.value.payload.details)
    assert not target.exists()


def test_an_export_that_violates_the_schema_is_a_restore_failure_not_a_crash(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Dropping a parent row leaves orphans the schema rejects; that must be structured."""

    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    _empty_a_table(directory, "workspaces")
    target = tmp_path / "restored.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code == "restore_verification_failed"
    assert failure.value.payload.details[0].reason == "ConstraintException"
    assert not target.exists()


def test_an_export_from_an_incomplete_database_is_refused(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    """Export validates its own completeness, so a damaged database cannot produce one."""

    from linguawiki.db.connection import open_writer
    from linguawiki.paths import workspace_paths

    paths = workspace_paths(tmp_path / "Damaged")
    with open_writer(paths, command="test.seed", clock=clock, create=True) as database:
        migration_module.migrate(database)
        with database.transaction() as transaction:
            transaction.execute("DROP TABLE jobs")
        with pytest.raises(LinguaWikiError) as failure:
            backup_module.portable_export(database, tmp_path / "export")

    assert failure.value.payload.code == "portable_export_unsupported"
    assert "jobs" in failure.value.payload.details[0].context["divergence"]


def test_restore_refuses_a_truncated_backup(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    directory = Path(report.directory)
    Path(report.native_database or "").unlink()

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(
            directory, tmp_path / "restored.duckdb", clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "backup_verification_failed"


def test_restore_refuses_a_directory_assembled_outside_linguawiki(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    report = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=synthetic_workspace.clock
    )
    directory = Path(report.directory)
    (directory / "manifest.json").unlink()

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(
            directory, tmp_path / "restored.duckdb", clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "backup_manifest_missing"


def test_restoring_a_bare_database_file_needs_no_manifest(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """A hand-copied database file is still restorable; only managed backups are verified."""

    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
        portable=False,
    )
    loose = tmp_path / "loose.duckdb"
    shutil.copy2(Path(report.native_database or ""), loose)

    restored = backup_module.restore(
        loose, tmp_path / "restored.duckdb", clock=synthetic_workspace.clock
    )

    assert restored.source_kind == "native"
    assert restored.verified is True


@pytest.mark.parametrize("reason", ["../../../escaped", "with space", "UPPER", "", "a" * 33])
def test_a_backup_reason_cannot_steer_the_directory_out_of_its_root(
    reason: str, synthetic_workspace: SyntheticWorkspace
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        database_service.backup(
            synthetic_workspace.paths,
            backup_root=synthetic_workspace.backup_root,
            clock=synthetic_workspace.clock,
            reason=reason,
        )

    assert failure.value.payload.code == "invalid_backup_reason"
    assert not (synthetic_workspace.backup_root.parent / "escaped").exists()


def test_every_backup_directory_stays_under_the_backup_root(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
        reason="pre-migrate",
    )

    assert Path(report.directory).parent == synthetic_workspace.backup_root.resolve()


def test_native_and_portable_restores_are_equivalent(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    clock = synthetic_workspace.clock
    report = database_service.backup(
        synthetic_workspace.paths, backup_root=synthetic_workspace.backup_root, clock=clock
    )
    original = _snapshot(synthetic_workspace.paths.database, clock)

    native = backup_module.restore(
        report.directory, tmp_path / "native-restore.duckdb", clock=clock, kind="native"
    )
    portable = backup_module.restore(
        report.directory, tmp_path / "portable-restore.duckdb", clock=clock, kind="portable"
    )

    assert native.source_kind == "native"
    assert portable.source_kind == "portable"
    assert native.verified is True
    assert portable.verified is True
    assert _snapshot(Path(native.target), clock) == original
    assert _snapshot(Path(portable.target), clock) == original


def test_a_portable_restore_recreates_the_schema_and_constraints(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    restored = backup_module.restore(export.directory, tmp_path / "fresh.duckdb", clock=clock)

    with open_temporary(Path(restored.target), clock=clock) as database:
        assert set(database.table_names()) == set(TABLE_ORDER)
        with pytest.raises(duckdb.Error):
            database.execute(
                "INSERT INTO workspace_versions (workspace_id, component, component_key, "
                "version, sha256, applied_at) VALUES (?, ?, ?, ?, ?, ?)",
                ["wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV", "core", "core", "1", "a" * 64, database.now()],
            )


def test_export_portable_is_standalone_and_verified(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    report = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=synthetic_workspace.clock
    )
    directory = Path(report.directory)

    assert report.verified is True
    assert (directory / "metadata.json").is_file()
    assert (directory / "manifest.json").is_file()
    metadata = backup_module.read_portable_metadata(directory)
    assert metadata.database_schema_version == migration_module.head_version()
    assert metadata.workspace_id == synthetic_workspace.report.workspace_id
    assert metadata.duckdb_version == duckdb.__version__
    backup_module.verify_manifest(directory)


def test_export_portable_refuses_a_non_empty_target(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.parquet").write_bytes(b"")

    with pytest.raises(LinguaWikiError) as failure:
        database_service.export_portable(
            synthetic_workspace.paths, target=target, clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "export_target_not_empty"


def test_restore_never_overwrites_an_existing_path(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    occupied = tmp_path / "occupied.duckdb"
    occupied.write_bytes(b"existing learner data")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(report.directory, occupied, clock=synthetic_workspace.clock)

    assert failure.value.payload.code == "restore_target_exists"
    assert occupied.read_bytes() == b"existing learner data"


def test_restore_refuses_the_active_database_path(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
    )
    active = synthetic_workspace.paths.database
    active.unlink()

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(
            report.directory,
            active,
            clock=synthetic_workspace.clock,
            active_database=active,
        )

    assert failure.value.payload.code == "restore_target_active"


def test_restore_rejects_an_unknown_source(tmp_path: Path, clock: AdvancingClock) -> None:
    empty = tmp_path / "not-a-backup"
    empty.mkdir()

    with pytest.raises(LinguaWikiError) as missing:
        backup_module.restore(tmp_path / "absent", tmp_path / "target.duckdb", clock=clock)
    assert missing.value.payload.code == "restore_source_invalid"

    with pytest.raises(LinguaWikiError) as unknown:
        backup_module.restore(empty, tmp_path / "target.duckdb", clock=clock)
    assert unknown.value.payload.code == "restore_source_invalid"


def test_restore_rejects_a_requested_kind_that_is_absent(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    report = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=synthetic_workspace.clock,
        native=False,
    )

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(
            report.directory,
            tmp_path / "target.duckdb",
            clock=synthetic_workspace.clock,
            kind="native",
        )

    assert failure.value.payload.code == "restore_source_invalid"


def test_restore_rejects_an_empty_native_database(tmp_path: Path, clock: AdvancingClock) -> None:
    empty = tmp_path / "empty.duckdb"
    duckdb.connect(str(empty)).close()

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(empty, tmp_path / "target.duckdb", clock=clock)

    assert failure.value.payload.code == "restore_source_invalid"


def test_a_failed_restore_leaves_no_partial_target(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    metadata["migrations"][2][2] = "f" * 64
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    # Re-stamp the manifest so this exercises the schema check, not the checksum check.
    backup_module.write_manifest(directory)
    target = tmp_path / "partial.duckdb"

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, target, clock=clock)

    assert failure.value.payload.code == "restore_schema_unsupported"
    assert not target.exists()


def test_an_export_from_an_unknown_release_is_refused(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    metadata["migrations"].append([99, "0099_from_the_future", "f" * 64])
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    backup_module.write_manifest(directory)

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.restore(directory, tmp_path / "target.duckdb", clock=clock)

    assert failure.value.payload.code == "restore_schema_unsupported"


def test_a_portable_export_with_a_wrong_row_count_fails_verification(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    clock = synthetic_workspace.clock
    export = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=clock
    )
    directory = Path(export.directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    for table in metadata["tables"]:
        if table["table"] == "workspaces":
            table["row_count"] = 99
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        backup_module.verify_portable_export(directory, clock=clock)

    assert failure.value.payload.code == "backup_verification_failed"
    assert failure.value.payload.details[0].reason == "row count differs"


def test_portable_metadata_is_required(tmp_path: Path) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        backup_module.read_portable_metadata(tmp_path)

    assert failure.value.payload.code == "portable_metadata_missing"


def test_a_native_backup_target_is_never_overwritten(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    clock = synthetic_workspace.clock
    first = database_service.backup(
        synthetic_workspace.paths,
        backup_root=synthetic_workspace.backup_root,
        clock=clock,
        reason="collide",
    )

    with (
        pytest.raises(LinguaWikiError) as failure,
        open_temporary(synthetic_workspace.paths.database, clock=clock) as database,
    ):
        backup_module.native_backup(database, Path(first.native_database or ""))

    assert failure.value.payload.code == "backup_target_exists"


def test_an_existing_backup_directory_is_never_reused(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    frozen = FixedClock()
    occupied = tmp_path / "collisions" / f"{frozen.now():%Y%m%dT%H%M%SZ}-manual"
    occupied.mkdir(parents=True)

    with (
        pytest.raises(LinguaWikiError) as failure,
        open_temporary(synthetic_workspace.paths.database, clock=frozen) as database,
    ):
        backup_module.create_backup(
            database, backup_root=occupied.parent, reason="manual", clock=frozen
        )

    assert failure.value.payload.code == "backup_target_exists"


AWKWARD_DIRECTORIES = ("O'Brien", "back\\slash", 'quote"mark', "with space", "semi;colon")


@pytest.mark.parametrize("directory", AWKWARD_DIRECTORIES)
def test_backup_and_restore_survive_awkward_but_valid_paths(
    directory: str, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Filesystem paths reach DuckDB as parameters, never as SQL string literals."""

    clock = synthetic_workspace.clock
    awkward_root = tmp_path / directory
    export = database_service.export_portable(
        synthetic_workspace.paths, target=awkward_root / "export", clock=clock
    )
    backup = database_service.backup(
        synthetic_workspace.paths, backup_root=awkward_root / "backups", clock=clock
    )
    restored = backup_module.restore(
        export.directory, awkward_root / "restored.duckdb", clock=clock
    )

    assert Path(export.directory).is_dir()
    assert backup.verified is True
    assert restored.verified is True
    assert restored.total_rows == export.total_rows
    with open_temporary(Path(restored.target), clock=clock) as database:
        assert int(database.scalar("SELECT count(*) FROM workspaces")) == 1


@pytest.mark.parametrize("directory", AWKWARD_DIRECTORIES)
def test_a_workspace_can_live_under_an_awkward_but_valid_path(
    directory: str, tmp_path: Path, clock: AdvancingClock
) -> None:
    from linguawiki.services import workspace as workspace_service

    root = tmp_path / directory / "PolishLinguaWiki"

    report = workspace_service.initialize(
        workspace_service.InitOptions(
            path=root, backup_root=tmp_path / directory / "backups", name="Polish LinguaWiki"
        ),
        clock=clock,
    )

    assert report.created is True
    assert workspace_service.doctor(root, clock=clock).ok is True


@pytest.mark.parametrize("schema_version", RETAINED_SCHEMA_VERSIONS)
def test_an_export_of_a_retained_schema_shape_still_restores(
    schema_version: int, tmp_path: Path, clock: AdvancingClock
) -> None:
    """Pins the on-disk export *shape* against the current restore path.

    The Parquet here is rebuilt by the installed DuckDB from retained rows and column
    types, so this proves schema-shape compatibility, not cross-version compatibility.
    Data actually written by a different DuckDB is exercised by
    `scripts/check_duckdb_upgrade.py`, which installs both versions for real.
    """

    export = portable_fixture.materialize(schema_version, tmp_path / "retained-export")

    backup_module.verify_manifest(export)
    metadata = backup_module.verify_portable_export(export, clock=clock)
    report = backup_module.restore(export, tmp_path / "retained.duckdb", clock=clock)

    assert metadata.database_schema_version == schema_version
    assert report.verified is True
    assert report.total_rows == sum(export.row_count for export in metadata.tables)
    with open_temporary(Path(report.target), clock=clock) as database:
        assert migration_module.applied_version(database) == schema_version
        assert int(database.scalar("SELECT count(*) FROM workspaces")) == 1
        assert set(database.table_names()) == set(tables_at_schema_version(schema_version))


@pytest.mark.parametrize("schema_version", [HEAD_SCHEMA_VERSION])
def test_the_retained_export_matches_the_current_export_shape(
    schema_version: int, synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Column names and types may not drift silently away from a retained export.

    Only the fixture for this release's head can match the current export. The older
    fixtures pin shapes this release no longer produces, and are exercised by the
    restore test above instead.
    """

    current = database_service.export_portable(
        synthetic_workspace.paths, target=tmp_path / "export", clock=synthetic_workspace.clock
    )
    retained = json.loads(
        (portable_fixture.fixture_directory(schema_version) / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    now = backup_module.read_portable_metadata(Path(current.directory))

    assert {table["table"]: table["columns"] for table in retained["tables"]} == {
        export.table: [list(column) for column in export.columns] for export in now.tables
    }
