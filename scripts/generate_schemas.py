#!/usr/bin/env python3
"""Generate checked-in JSON Schema snapshots from the Pydantic contracts."""

from __future__ import annotations

import argparse
from pathlib import Path

from linguawiki.schema_snapshots import rendered_schemas

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIRECTORY = ROOT / "schemas"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    mismatches: list[str] = []
    for path, expected in rendered_schemas(SCHEMA_DIRECTORY).items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                mismatches.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    if mismatches:
        print("Schema snapshots are stale or missing:")
        for mismatch in mismatches:
            print(f"- {mismatch}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
