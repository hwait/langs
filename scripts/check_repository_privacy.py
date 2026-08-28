#!/usr/bin/env python3
"""Fail when learner-state or private artifact paths enter the core tree."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

from linguawiki.repository_policy import load_privacy_policy, parse_nul_paths, privacy_violation

ROOT = Path(__file__).resolve().parents[1]


POLICY = ROOT / "config" / "privacy-policy.toml"


def candidate_paths() -> list[PurePosixPath]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return parse_nul_paths(result.stdout)


def main() -> int:
    policy = load_privacy_policy(POLICY)
    violations = [
        (path, reason) for path in candidate_paths() if (reason := privacy_violation(path, policy))
    ]
    if violations:
        for path, reason in violations:
            print(f"{path}: {reason}")
        return 1
    print("Core repository privacy scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
