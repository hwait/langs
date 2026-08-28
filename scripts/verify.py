#!/usr/bin/env python3
"""Canonical Stage 0 verification gate used locally and in CI."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str]) -> None:
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-skill-validator", type=Path)
    args = parser.parse_args()
    python = sys.executable
    run([python, "-m", "ruff", "format", "--check", "."])
    run([python, "-m", "ruff", "check", "."])
    run([python, "-m", "mypy", "src"])
    run([python, "scripts/generate_schemas.py", "--check"])
    validate_skills = [python, "scripts/validate_skills.py"]
    if args.external_skill_validator is not None:
        validate_skills.extend(["--external-validator", str(args.external_skill_validator)])
    run(validate_skills)
    run([python, "scripts/render_privacy_gitignore.py", "--check"])
    run([python, "scripts/check_repository_privacy.py"])
    run(
        [
            python,
            "-m",
            "pytest",
            "--cov=linguawiki",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-fail-under=90",
        ]
    )
    with tempfile.TemporaryDirectory(prefix="linguawiki-dist-") as distribution_directory:
        run([python, "-m", "hatchling", "build", "-d", distribution_directory])
        run([python, "scripts/check_distribution.py", "--dist", distribution_directory])
    run(["git", "diff", "--check"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
