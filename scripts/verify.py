#!/usr/bin/env python3
"""Canonical verification gate used locally and in CI.

Two tiers, because they answer different questions and cost an order of magnitude apart.

`--fast` is the **stage** gate: every static check plus the whole test suite, with no
coverage instrumentation, no wheel, and no distribution check. It answers "is this change
correct and does it break anything anywhere?", which is what a stage handoff needs.

The default is the **release** gate: the same, plus branch coverage against its floor, the
wheel build, and the distribution check. Those are properties of a release rather than of
a change -- checking the coverage floor once before a merge proves exactly what checking it
after every stage of a seven-stage plan proves, for a fraction of the time.

Coverage is what separates them: branch tracking runs the suite roughly nine times slower
here, and `COVERAGE_CORE=sysmon` cannot help, because `sys.monitoring` gained branch events
in Python 3.14 and this project is pinned to 3.12.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="linguawiki-verify-") as scratch:
        return _verify(scratch)


def _verify(scratch: str) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-skill-validator", type=Path)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="stage gate: every static check and the whole suite, without coverage, "
        "the wheel, or the distribution check",
    )
    parser.add_argument(
        "--jobs",
        default="auto",
        help="pytest-xdist workers ('auto', a number, or '0' to run in this process)",
    )
    parser.add_argument(
        "--clean-environment",
        action="store_true",
        help="also install the built wheel in a fresh environment (needs uv and an index)",
    )
    parser.add_argument("--uv", type=Path, help="uv executable for the clean-environment check")
    parser.add_argument(
        "--no-repository-writes",
        action="store_true",
        help="keep coverage data and build output out of the repository (for read-only checkouts)",
    )
    parser.add_argument(
        "--duckdb-upgrade",
        metavar="VERSION",
        help="also prove a candidate DuckDB version can read data the pinned one wrote",
    )
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
    run([python, "scripts/check_sql_identifiers.py"])
    environment = dict(os.environ)
    if args.no_repository_writes:
        environment["COVERAGE_FILE"] = str(Path(scratch) / "coverage")
    pytest_command = [python, "-m", "pytest"]
    # Workers are per-test-process, and every test builds its own workspace under its own
    # `tmp_path`, so there is no shared state for them to race over. `0` keeps the suite in
    # this process for the cases where a worker pool hides a traceback.
    if args.jobs != "0":
        pytest_command.extend(["-n", args.jobs, "--dist", "loadfile"])
    if not args.fast:
        pytest_command.extend(
            [
                "--cov=linguawiki",
                "--cov-branch",
                "--cov-report=term-missing",
                "--cov-fail-under=90",
            ]
        )
    run(pytest_command, env=environment)
    if args.fast:
        run(["git", "diff", "--check"])
        return 0
    with tempfile.TemporaryDirectory(prefix="linguawiki-dist-") as distribution_directory:
        run([python, "-m", "hatchling", "build", "-d", distribution_directory])
        run([python, "scripts/check_distribution.py", "--dist", distribution_directory])
    if args.clean_environment:
        clean_environment = [python, "scripts/check_clean_environment.py"]
        if args.uv is not None:
            clean_environment.extend(["--uv", str(args.uv.resolve())])
        run(clean_environment)
    if args.duckdb_upgrade:
        upgrade = [python, "scripts/check_duckdb_upgrade.py", "--to", args.duckdb_upgrade]
        if args.uv is not None:
            upgrade.extend(["--uv", str(args.uv.resolve())])
        run(upgrade)
    run(["git", "diff", "--check"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
