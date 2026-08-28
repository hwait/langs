#!/usr/bin/env python3
"""Render or check the generated privacy section in the root .gitignore."""

from __future__ import annotations

import argparse
from pathlib import Path

from linguawiki.repository_policy import (
    GENERATED_END,
    GENERATED_START,
    load_privacy_policy,
    render_generated_gitignore,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "privacy-policy.toml"
GITIGNORE = ROOT / ".gitignore"
WORKSPACE_GITIGNORE = ROOT / "templates" / "learner-workspace" / ".gitignore.j2"


def expected_gitignore(current: str) -> str:
    start = current.index(GENERATED_START)
    end = current.index(GENERATED_END, start) + len(GENERATED_END)
    generated = render_generated_gitignore(load_privacy_policy(POLICY))
    return f"{current[:start]}{generated}{current[end:]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path in (GITIGNORE, WORKSPACE_GITIGNORE):
        current = path.read_text(encoding="utf-8")
        expected = expected_gitignore(current)
        if args.check:
            if current != expected:
                print(f"{path.relative_to(ROOT)} privacy section is stale")
                return 1
        else:
            path.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
