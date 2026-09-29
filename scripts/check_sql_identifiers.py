#!/usr/bin/env python3
"""Fail when an identifier is interpolated into SQL without `quote_identifier`.

A table or column name read back from the catalog is attacker-controlled data: anyone
who can add a table chooses its name. Wrapping such a name in a bare `"{name}"` lets it
terminate the identifier and append statements, which is how `db backup` — a recovery
command — briefly became able to drop a table.

This is a lint rather than a test because the property is syntactic: no interpolated SQL
fragment may contain a bare double-quoted placeholder.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "linguawiki"
# A placeholder wrapped directly in double quotes inside an f-string, e.g. "{table}".
BARE_QUOTED_PLACEHOLDER = re.compile(r'"\{[^{}]+\}"')
SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|COPY|CREATE|ALTER|DROP|ATTACH|DETACH|FROM|JOIN)\b",
    re.IGNORECASE,
)


def _fstring_text(node: ast.JoinedStr) -> str:
    """Reconstruct an f-string with its placeholders marked, for pattern matching."""

    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append("{placeholder}")
    return "".join(parts)


def violations(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"file does not parse: {exc.msg}")]
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        text = _fstring_text(node)
        if not SQL_KEYWORDS.search(text):
            continue
        if BARE_QUOTED_PLACEHOLDER.search(text):
            found.append((node.lineno, text.strip()[:80]))
    return found


def package_files() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, help="check one file instead of the package")
    args = parser.parse_args(argv)
    targets = [args.path] if args.path else package_files()
    problems: list[str] = []
    for path in targets:
        for line, text in violations(path):
            location = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            problems.append(
                f"{location}:{line}: interpolated identifier is quoted by hand; "
                f"use quote_identifier() -- {text}"
            )
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print("SQL identifier quoting check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
