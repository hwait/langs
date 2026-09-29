#!/usr/bin/env python3
"""Prove a DuckDB version change keeps learner data readable.

The plan accepts a DuckDB dependency upgrade only after native-open, portable-export,
and portable-restore all pass against data written by the previously pinned version.
That cannot be shown by a fixture the current DuckDB just wrote, so this script really
installs two DuckDB versions:

1. `--from` (default: the version this release pins) creates a workspace, seeds every
   Stage 1 table with synthetic learner data, and takes a verified native plus portable
   backup;
2. `--to` (the candidate) opens that native database, re-exports it, and restores both
   the native and the portable backup the older version produced.

Every step is compared per table: row count, column list, a content hash of the ordered
rows, and the stable identities (workspace, user, track, pack checksum, event IDs, and
the wiki projection row). Aggregate totals would hide a table that silently emptied.

Direction matters, and only one direction is a guarantee. DuckDB promises that a newer
release reads an older file, not the reverse, so a candidate *older* than `--from` is
judged on the portable export alone: it must restore the Parquet the newer release wrote,
which is the actual recovery path off a bad upgrade. Requiring native open there produced
a check that passed by luck -- run for run, with identical data, DuckDB 1.4.1 opening a
1.5.5 file either worked or died with `INTERNAL Error: Failed to load metadata pointer`.

It needs `uv` and a package index, so it is not part of the offline gate:

    uv run python scripts/check_duckdb_upgrade.py --to 1.6.0
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_clean_environment import child_environment, resolve_uv  # noqa: E402


def _run(
    command: list[str], *, cwd: Path, env: dict[str, str], quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    if not quiet:
        print(f"+ {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=cwd, check=False, text=True, capture_output=True, env=env)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"command failed with exit status {result.returncode}: {command[0]}")
    return result


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    payload: dict[str, object] = json.loads(result.stdout)
    if payload.get("ok") is not True:
        raise SystemExit(f"command reported failure: {result.stdout}")
    return payload


def _data(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    data = _payload(result)["data"]
    assert isinstance(data, dict)
    return data


DATABASE_RELATIVE = Path("data") / "linguawiki.duckdb"


def _compare(label: str, expected: dict[str, object], actual: dict[str, object]) -> list[str]:
    """Compare per-table content and stable identities, not aggregate row totals."""

    problems: list[str] = []
    expected_tables = expected["tables"]
    actual_tables = actual["tables"]
    assert isinstance(expected_tables, dict) and isinstance(actual_tables, dict)
    if set(expected_tables) != set(actual_tables):
        problems.append(
            f"{label}: table set differs "
            f"(missing {sorted(set(expected_tables) - set(actual_tables))}, "
            f"extra {sorted(set(actual_tables) - set(expected_tables))})"
        )
    for table in sorted(set(expected_tables) & set(actual_tables)):
        if expected_tables[table] != actual_tables[table]:
            problems.append(
                f"{label}: {table} differs: expected {expected_tables[table]}, "
                f"got {actual_tables[table]}"
            )
    if expected["identities"] != actual["identities"]:
        problems.append(
            f"{label}: stable identities differ: expected {expected['identities']}, "
            f"got {actual['identities']}"
        )
    return problems


def release(version: str) -> tuple[int, ...]:
    """Order two DuckDB releases, so the script knows which direction it is testing.

    A missing or non-numeric component sorts as 0 rather than raising: the version comes
    from an operator's `--to`, and a gate that dies on `1.6` instead of testing it would
    be the more annoying failure.
    """

    return tuple(int(piece) if piece.isdigit() else 0 for piece in version.split("."))


def pinned_duckdb() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip().strip('",')
        if stripped.startswith("duckdb=="):
            return stripped.split("==", 1)[1]
    raise SystemExit("could not find the pinned duckdb version in pyproject.toml")


def _environment(
    sandbox: Path, name: str, wheel: Path, duckdb_version: str, *, uv: Path, env: dict[str, str]
) -> Path:
    """A virtual environment with the core wheel and one specific DuckDB version."""

    root = sandbox / name
    _run([str(uv), "venv", str(root)], cwd=sandbox, env=env)
    python = root / "bin" / "python"
    _run([str(uv), "pip", "install", "--python", str(python), str(wheel)], cwd=sandbox, env=env)
    _run(
        [str(uv), "pip", "install", "--python", str(python), f"duckdb=={duckdb_version}"],
        cwd=sandbox,
        env=env,
    )
    reported = _run(
        [str(python), "-c", "import duckdb; print(duckdb.__version__)"], cwd=sandbox, env=env
    ).stdout.strip()
    if reported != duckdb_version:
        raise SystemExit(f"{name} reports duckdb {reported}, expected {duckdb_version}")
    return root / "bin" / "linguawiki"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv")
    parser.add_argument("--from", dest="source", help="DuckDB version that writes the data")
    parser.add_argument("--to", dest="target", required=True, help="candidate DuckDB version")
    args = parser.parse_args()
    uv = resolve_uv(args.uv)
    if uv is None:
        print("uv was not found on PATH or in .tools; pass --uv <path>", file=sys.stderr)
        return 1
    env = child_environment(uv)
    source_version = args.source or pinned_duckdb()
    if source_version == args.target:
        print(f"--from and --to are both {args.target}; nothing to compare", file=sys.stderr)
        return 1
    downgrade = release(args.target) < release(source_version)
    if downgrade:
        print(
            f"{args.target} is older than {source_version}: DuckDB guarantees only that a "
            f"newer release reads an older file, so this direction is judged on the "
            f"portable export alone -- native open is measured luck, not compatibility",
            flush=True,
        )

    with tempfile.TemporaryDirectory(prefix="linguawiki-duckdb-") as raw:
        sandbox = Path(raw)
        distribution = sandbox / "dist"
        _run([str(uv), "build", "--wheel", "--out-dir", str(distribution)], cwd=ROOT, env=env)
        wheel = next(iter(sorted(distribution.glob("linguawiki-*.whl"))))

        support = str(ROOT / "scripts" / "duckdb_upgrade_support.py")
        writer_root = sandbox / "writer"
        writer = _environment(sandbox, "writer", wheel, source_version, uv=uv, env=env)
        writer_python = writer_root / "bin" / "python"
        workspace = sandbox / "PolishLinguaWiki"
        backups = sandbox / "backups"
        _data(
            _run(
                [
                    str(writer),
                    "workspace",
                    "init",
                    str(workspace),
                    "--backup-root",
                    str(backups),
                    "--name",
                    "Polish LinguaWiki",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        _run([str(writer_python), support, "seed", str(workspace)], cwd=sandbox, env=env)
        written = _data(
            _run(
                [str(writer), "db", "backup", "--workspace", str(workspace), "--format", "json"],
                cwd=sandbox,
                env=env,
            )
        )
        original = json.loads(
            _run(
                [str(writer_python), support, "workspace-digest", str(workspace)],
                cwd=sandbox,
                env=env,
                quiet=True,
            ).stdout
        )
        empty = sorted(table for table, entry in original["tables"].items() if entry["rows"] == 0)
        if empty:
            raise SystemExit(f"seeding left tables empty, so they prove nothing: {empty}")
        print(
            f"seeded {len(original['tables'])} tables "
            f"({sum(entry['rows'] for entry in original['tables'].values())} rows) "
            f"with duckdb {source_version}",
            flush=True,
        )

        reader_root = sandbox / "reader"
        reader = _environment(sandbox, "reader", wheel, args.target, uv=uv, env=env)
        reader_python = reader_root / "bin" / "python"

        def digest(path: Path) -> dict[str, object]:
            payload: dict[str, object] = json.loads(
                _run(
                    [str(reader_python), support, "digest", str(path)],
                    cwd=sandbox,
                    env=env,
                    quiet=True,
                ).stdout
            )
            return payload

        problems: list[str] = []
        if downgrade:
            # A downgrade crosses the storage guarantee backwards, so only the portable
            # export is required to survive it -- and it is the whole recovery story: a
            # learner who has to go back to an older release restores from Parquet.
            older = _data(
                _run(
                    [
                        str(reader),
                        "db",
                        "restore",
                        "--workspace",
                        str(workspace),
                        "--from",
                        str(written["directory"]),
                        "--kind",
                        "portable",
                        "--to",
                        str(sandbox / "restored-portable.duckdb"),
                        "--format",
                        "json",
                    ],
                    cwd=sandbox,
                    env=env,
                )
            )
            if older["verified"] is not True:
                problems.append(f"portable restore was not verified: {older}")
            problems.extend(_compare("portable restore", original, digest(Path(older["target"]))))
            proven = "the portable export restore"
        else:
            # 1. Native open: the candidate must read the database the older version wrote.
            checked = _data(
                _run(
                    [
                        str(reader),
                        "db",
                        "check",
                        "--workspace",
                        str(workspace),
                        "--format",
                        "json",
                    ],
                    cwd=sandbox,
                    env=env,
                )
            )
            if checked["ok"] is not True:
                problems.append(f"db check failed under duckdb {args.target}: {checked}")
            problems.extend(
                _compare("native open", original, digest(workspace / DATABASE_RELATIVE))
            )
            # 2. A re-export under the candidate must restore to the same content.
            exported = _data(
                _run(
                    [
                        str(reader),
                        "db",
                        "export-portable",
                        "--workspace",
                        str(workspace),
                        "--to",
                        str(sandbox / "export-new"),
                        "--format",
                        "json",
                    ],
                    cwd=sandbox,
                    env=env,
                )
            )
            reexported = sandbox / "reexport-restored.duckdb"
            _run(
                [
                    str(reader),
                    "db",
                    "restore",
                    "--workspace",
                    str(workspace),
                    "--from",
                    str(exported["directory"]),
                    "--to",
                    str(reexported),
                    "--kind",
                    "portable",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
            problems.extend(_compare("re-export restore", original, digest(reexported)))
            # 3. Both backups the older version wrote must restore identically.
            for kind in ("native", "portable"):
                report = _data(
                    _run(
                        [
                            str(reader),
                            "db",
                            "restore",
                            "--workspace",
                            str(workspace),
                            "--from",
                            str(written["directory"]),
                            "--kind",
                            kind,
                            "--to",
                            str(sandbox / f"restored-{kind}.duckdb"),
                            "--format",
                            "json",
                        ],
                        cwd=sandbox,
                        env=env,
                    )
                )
                if report["verified"] is not True:
                    problems.append(f"{kind} restore was not verified: {report}")
                problems.extend(
                    _compare(f"{kind} restore", original, digest(Path(report["target"])))
                )
            proven = "native open, portable export, and native/portable restore"
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1

    total = sum(entry["rows"] for entry in original["tables"].values())
    print(
        f"DuckDB {source_version} -> {args.target}: {proven} preserved every row, column, "
        f"content hash, and stable identity across {len(original['tables'])} tables "
        f"({total} rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
