#!/usr/bin/env python3
"""Generate the checked-in OpenAPI document from the Pydantic contracts and the routes."""

from __future__ import annotations

import argparse
from pathlib import Path

from linguawiki.openapi import DOCUMENT_RELATIVE_PATH, rendered

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIRECTORY = ROOT / "schemas"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = SCHEMA_DIRECTORY / DOCUMENT_RELATIVE_PATH
    expected = rendered(SCHEMA_DIRECTORY)
    if args.check:
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            print(f"OpenAPI document is stale or missing: {path.relative_to(ROOT)}")
            return 1
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
