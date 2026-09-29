"""The gate must run every check it was asked for, whichever tier was selected.

`--fast` chooses a *tier*; `--duckdb-upgrade` and `--clean-environment` are named by the
caller. Returning early from the tier skipped both and still exited 0, which is the one
failure a verification gate must never have: reporting success for work it did not do.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import verify as verify_module  # noqa: E402


def _commands(monkeypatch: pytest.MonkeyPatch, argv: list[str], tmp_path: Path) -> list[list[str]]:
    issued: list[list[str]] = []

    def record(command: list[str], *, env: dict[str, str] | None = None) -> None:
        issued.append(command)

    monkeypatch.setattr(verify_module, "run", record)
    monkeypatch.setattr(sys, "argv", ["verify.py", *argv])
    assert verify_module._verify(str(tmp_path)) == 0
    return issued


def _ran(issued: list[list[str]], needle: str) -> bool:
    return any(needle in " ".join(command) for command in issued)


@pytest.mark.parametrize("tier", [[], ["--fast"]])
def test_an_explicitly_requested_check_runs_in_either_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tier: list[str]
) -> None:
    issued = _commands(
        monkeypatch, [*tier, "--duckdb-upgrade", "9.9.9", "--clean-environment"], tmp_path
    )

    assert _ran(issued, "check_duckdb_upgrade.py --to 9.9.9"), tier
    assert _ran(issued, "check_clean_environment.py"), tier
    assert _ran(issued, "git diff --check"), tier


def test_the_fast_tier_drops_coverage_the_wheel_and_the_distribution_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fast = _commands(monkeypatch, ["--fast"], tmp_path)
    full = _commands(monkeypatch, [], tmp_path)

    assert not _ran(fast, "--cov=linguawiki")
    assert not _ran(fast, "hatchling build")
    assert not _ran(fast, "check_distribution.py")
    assert _ran(full, "--cov=linguawiki")
    assert _ran(full, "hatchling build")
    assert _ran(full, "check_distribution.py")


def test_both_tiers_run_every_static_check_and_the_whole_suite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The stage gate is cheaper than the release gate, never narrower in what it tests."""

    static = (
        "ruff format --check",
        "ruff check",
        "mypy src",
        "generate_schemas.py --check",
        "validate_skills.py",
        "render_privacy_gitignore.py --check",
        "check_repository_privacy.py",
        "check_sql_identifiers.py",
    )
    for tier in ([], ["--fast"]):
        issued = _commands(monkeypatch, tier, tmp_path)
        for check in static:
            assert _ran(issued, check), (tier, check)
        pytest_calls = [c for c in issued if "-m pytest" in " ".join(c)]
        assert len(pytest_calls) == 1, tier
        assert not any(argument.startswith("tests/") for argument in pytest_calls[0]), (
            "the gate runs the whole suite, never a subset"
        )
