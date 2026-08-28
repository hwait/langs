#!/usr/bin/env python3
"""Use Codex's prompt debugger to verify real repository-skill discovery."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from linguawiki.codex_discovery import output_discovers_skill

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILL = ROOT / ".agents" / "skills" / "linguawiki" / "SKILL.md"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex:
        print("Codex executable was not found")
        return 1
    result = subprocess.run(
        [
            args.codex,
            "-C",
            str(ROOT),
            "debug",
            "prompt-input",
            "Use $linguawiki to check status.",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, end="")
        return result.returncode
    if not output_discovers_skill(result.stdout, root=ROOT, skill_name="linguawiki"):
        print(f"Codex did not discover {EXPECTED_SKILL}")
        return 1
    print(f"Codex discovered {EXPECTED_SKILL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
