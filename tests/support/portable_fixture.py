"""Materialize a retained portable-export fixture into a real Parquet export."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

from linguawiki.db.backup import METADATA_NAME, write_manifest

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "portable-export"


def fixture_directory(schema_version: int) -> Path:
    return FIXTURE_ROOT / f"schema-{schema_version}"


def _value(raw: object, data_type: str) -> object:
    """Convert a recorded JSON value using only the fixture's own column type."""

    if raw is None:
        return None
    if data_type == "TIMESTAMP" and isinstance(raw, str):
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    return raw


def materialize(schema_version: int, target: Path) -> Path:
    """Rebuild the retained export as Parquet plus metadata and a fresh manifest."""

    source = fixture_directory(schema_version)
    metadata = json.loads((source / METADATA_NAME).read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    for table in metadata["tables"]:
        columns = [(str(name), str(data_type)) for name, data_type in table["columns"]]
        definition = ", ".join(f'"{name}" {data_type}' for name, data_type in columns)
        staging = f"staging_{table['table']}"
        connection.execute(f'CREATE TABLE "{staging}" ({definition})')
        rows = json.loads((source / "rows" / f"{table['table']}.json").read_text(encoding="utf-8"))
        placeholders = ", ".join("?" for _ in columns)
        for row in rows:
            connection.execute(
                f'INSERT INTO "{staging}" VALUES ({placeholders})',
                [_value(row[name], data_type) for name, data_type in columns],
            )
        connection.execute(
            f'COPY (SELECT * FROM "{staging}" ORDER BY ALL) '
            f"TO '{target / table['file']}' (FORMAT PARQUET)"
        )
    connection.close()
    (target / METADATA_NAME).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_manifest(target)
    return target
