"""Record a retained portable-export fixture for the current database schema version.

A retained fixture pins the *shape* of an export -- table set, ordered column names, and
column types -- so a later migration cannot rename or retype a column without a matching
change to the export path. `tests/support/portable_fixture.py` rebuilds the Parquet from
the recorded types alone, which is why the rows are stored as JSON rather than Parquet.

Run this once per release that changes the schema, then add the new directory:

    uv run python scripts/record_portable_fixture.py

Never edit an existing directory for a *released* schema version: it records the shape
that release produced, and rewriting it would erase the very evidence the fixture exists
to keep. Replacing a directory is only correct while its schema version is unreleased.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from linguawiki.db import migrations as migration_module  # noqa: E402
from linguawiki.db.backup import METADATA_NAME  # noqa: E402
from linguawiki.db.connection import quote_identifiers  # noqa: E402
from linguawiki.paths import workspace_paths  # noqa: E402
from linguawiki.services import database as database_service  # noqa: E402
from linguawiki.services import workspace as workspace_service  # noqa: E402

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "portable-export"


def _json_ready(value: object) -> object:
    if isinstance(value, datetime):
        # The fixture records UTC instants as text; the materializer converts them back
        # using the column type it read, not a guess about the value.
        stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value


def record(target: Path) -> Path:
    """Export a freshly initialized synthetic workspace and record it as a fixture."""

    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        root = scratch_path / "PolishLinguaWiki"
        workspace_service.initialize(
            workspace_service.InitOptions(
                path=root,
                backup_root=scratch_path / "backups",
                name="Polish LinguaWiki",
                timezone="Europe/Warsaw",
            )
        )
        export = database_service.export_portable(
            workspace_paths(root), target=scratch_path / "export"
        )
        directory = Path(export.directory)
        metadata = json.loads((directory / METADATA_NAME).read_text(encoding="utf-8"))
        if target.exists():
            shutil.rmtree(target)
        (target / "rows").mkdir(parents=True)
        connection = duckdb.connect()
        try:
            for table in metadata["tables"]:
                names = [str(name) for name, _ in table["columns"]]
                selection = quote_identifiers(names)
                rows = connection.execute(
                    f"SELECT {selection} FROM read_parquet('{directory / table['file']}') "
                    "ORDER BY ALL"
                ).fetchall()
                (target / "rows" / f"{table['table']}.json").write_text(
                    json.dumps(
                        [dict(zip(names, map(_json_ready, row), strict=True)) for row in rows],
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        finally:
            connection.close()
        (target / METADATA_NAME).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-version",
        type=int,
        default=migration_module.head_version(),
        help="the schema version to record (default: the packaged head)",
    )
    arguments = parser.parse_args()
    target = FIXTURE_ROOT / f"schema-{arguments.schema_version}"
    print(f"recording {target.relative_to(ROOT)}")
    record(target)
    print(
        "recorded; list it in RETAINED_SCHEMA_VERSIONS in tests/integration/test_backup_restore.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
