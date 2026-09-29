#!/usr/bin/env python3
"""Install the built wheel into a fresh environment and run the release path.

The distribution check only inspects archive contents. This script proves the
released artifact actually works: a clean interpreter environment, no source tree
on the path, a workspace created outside the core repository, the bundled language
pack installed *by key* from the wheel, a learner onboarded at a declared level,
one calibration task served, one observation recorded and aggregated into an item stage,
one bounded context bundle built, one session planned, flushed, and closed, the
learner dashboard rendered, and `workspace doctor` plus `db check` passing.

It needs `uv` and access to a package index, so it is not part of the offline
gate. Run it before publishing a release:

    uv run python scripts/check_clean_environment.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from linguawiki.contracts import DELIVERY_STAGE

ROOT = Path(__file__).resolve().parents[1]
#: A knowledge item the bundled pilot pack ships, used to prove the learner model
#: works from the wheel rather than only from the source tree.
EVIDENCE_TARGET = "pl.lex.dworzec"


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
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


def resolve_uv(explicit: str | None) -> Path | None:
    """Find uv: the explicit path, then PATH, then the repository's vendored copy.

    An explicit path that does not exist is an error rather than a silent fallback.
    """

    if explicit is not None:
        candidate = Path(explicit).resolve()
        return candidate if candidate.is_file() else None
    for found in (shutil.which("uv"), ROOT / ".tools" / "uv"):
        if found and Path(found).is_file():
            return Path(found).resolve()
    return None


def child_environment(uv: Path) -> dict[str, str]:
    """Put uv on PATH for every child, including the installed CLI's own uv calls."""

    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join([str(uv.parent), environment.get("PATH", os.defpath)])
    return environment


def _wheel(distribution: Path, *, uv: Path, env: dict[str, str]) -> Path:
    _run([str(uv), "build", "--wheel", "--out-dir", str(distribution)], cwd=ROOT, env=env)
    wheels = sorted(distribution.glob("linguawiki-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {distribution}, found {len(wheels)}")
    return wheels[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv")
    parser.add_argument("--wheel", type=Path, help="use an existing wheel instead of building one")
    args = parser.parse_args()
    uv = resolve_uv(args.uv)
    if uv is None:
        print("uv was not found on PATH or in .tools; pass --uv <path>", file=sys.stderr)
        return 1
    env = child_environment(uv)
    with tempfile.TemporaryDirectory(prefix="linguawiki-clean-") as raw:
        sandbox = Path(raw)
        wheel = args.wheel or _wheel(sandbox / "dist", uv=uv, env=env)
        environment = sandbox / "venv"
        _run([str(uv), "venv", str(environment)], cwd=sandbox, env=env)
        _run(
            [
                str(uv),
                "pip",
                "install",
                "--python",
                str(environment / "bin" / "python"),
                str(wheel),
            ],
            cwd=sandbox,
            env=env,
        )
        executable = environment / "bin" / "linguawiki"
        if not executable.is_file():
            raise SystemExit(f"the wheel did not install a console script at {executable}")
        workspace = sandbox / "PolishLinguaWiki"
        # The workspace pins an exact core version, so uv resolves it from the built
        # artifact until that release is published to an index.
        status = _payload(
            _run([str(executable), "status", "--format", "json"], cwd=sandbox, env=env)
        )
        initialized = _payload(
            _run(
                [
                    str(executable),
                    "workspace",
                    "init",
                    str(workspace),
                    "--backup-root",
                    str(sandbox / "backups"),
                    "--name",
                    "Polish LinguaWiki",
                    "--history",
                    "git-wiki",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        locked = _payload(
            _run(
                [
                    str(executable),
                    "workspace",
                    "lock-dependencies",
                    "--workspace",
                    str(workspace),
                    "--find-links",
                    str(wheel.parent),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        doctor = _payload(
            _run(
                [
                    str(executable),
                    "workspace",
                    "doctor",
                    "--workspace",
                    str(workspace),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        # The bundled pack has to travel in the wheel: a workspace holds no core source,
        # so `pack install pl-pilot` is the release path a learner actually takes.
        installed = _payload(
            _run(
                [
                    str(executable),
                    "pack",
                    "install",
                    "--workspace",
                    str(workspace),
                    "pl-pilot",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        _run(
            [
                str(executable),
                "user",
                "create",
                "--workspace",
                str(workspace),
                "--name",
                "Clean Environment Learner",
                "--timezone",
                "Europe/Warsaw",
                "--native",
                "ru",
                "--support",
                "en",
                "--format",
                "json",
            ],
            cwd=sandbox,
            env=env,
        )
        _run(
            [
                str(executable),
                "track",
                "create",
                "--workspace",
                str(workspace),
                "--target-language",
                "pl",
                "--framework",
                "cefr",
                "--declared-level",
                "A2",
                "--format",
                "json",
            ],
            cwd=sandbox,
            env=env,
        )
        _run(
            [
                str(executable),
                "onboard",
                "start",
                "--workspace",
                str(workspace),
                "--declared-level",
                "A2",
                "--format",
                "json",
            ],
            cwd=sandbox,
            env=env,
        )
        onboarded = _payload(
            _run(
                [
                    str(executable),
                    "onboard",
                    "finalize",
                    "--workspace",
                    str(workspace),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        served = _payload(
            _run(
                [
                    str(executable),
                    "assessment",
                    "next",
                    "--workspace",
                    str(workspace),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        # The learner model, from the wheel: one observation, the stage it moved, and the
        # bundle an agent would plan from. A release that can install a pack but cannot
        # record evidence has not shipped this stage.
        recorded = _payload(
            _run(
                [
                    str(executable),
                    "evidence",
                    "record",
                    "--workspace",
                    str(workspace),
                    "--task-type",
                    "objective",
                    "--modality",
                    "text",
                    "--score",
                    "1.0",
                    "--target",
                    EVIDENCE_TARGET,
                    "--dimension",
                    "reading",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        known = _payload(
            _run(
                [
                    str(executable),
                    "knowledge",
                    "get",
                    EVIDENCE_TARGET,
                    "--workspace",
                    str(workspace),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        bundle = _payload(
            _run(
                [
                    str(executable),
                    "context",
                    "session",
                    "--workspace",
                    str(workspace),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        # The session vertical, from the wheel: plan a lesson, flush one block's worth of
        # observations, close it, and render the dashboard. A release that can record an
        # imported observation but cannot run a session has not shipped this stage.
        planned = _payload(
            _run(
                [
                    str(executable),
                    "plan",
                    "create",
                    "--workspace",
                    str(workspace),
                    "--minutes",
                    "60",
                    "--idempotency-key",
                    "clean-environment-plan",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        _payload(
            _run(
                [
                    str(executable),
                    "session",
                    "start",
                    "--workspace",
                    str(workspace),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        plan_data = planned["data"]
        assert isinstance(plan_data, dict)
        blocks = [block for block in plan_data["blocks"] if block["role"] == "core"]
        batch_path = sandbox / "session-batch.json"
        batch_path.write_text(
            json.dumps(
                {
                    "sequence": 1,
                    "idempotency_key": "clean-environment-batch",
                    "block": blocks[0]["block_id"],
                    "events": [
                        {
                            "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                            "kind": "attempt.observed",
                            "occurred_at": "2026-01-01T09:00:00Z",
                            "payload": {
                                "task_type": "objective",
                                "modality": "text",
                                "dimension": "reading",
                                "target": EVIDENCE_TARGET,
                                "score": 1.0,
                                "assessor_kind": "ai",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        flushed = _payload(
            _run(
                [
                    str(executable),
                    "session",
                    "log",
                    "--workspace",
                    str(workspace),
                    "--input",
                    str(batch_path),
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        closed = _payload(
            _run(
                [
                    str(executable),
                    "session",
                    "close",
                    "--workspace",
                    str(workspace),
                    "--outcome",
                    "completed",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        dashboard = _payload(
            _run(
                [
                    str(executable),
                    "wiki",
                    "build",
                    "--workspace",
                    str(workspace),
                    "--view",
                    "dashboard",
                    "--format",
                    "json",
                ],
                cwd=sandbox,
                env=env,
            )
        )
        checked = _payload(
            _run(
                [str(executable), "db", "check", "--workspace", str(workspace), "--format", "json"],
                cwd=sandbox,
                env=env,
            )
        )
        problems: list[str] = []
        if plan_data["status"] != "planned" or not blocks:
            problems.append(f"planning a session produced no core block: {plan_data}")
        if flushed["data"]["staged_events"] != 1:  # type: ignore[index]
            problems.append(f"flushing a batch staged nothing: {flushed['data']}")
        if closed["data"]["attempts_written"] != 1:  # type: ignore[index]
            problems.append(f"closing the session credited nothing: {closed['data']}")
        if closed["data"]["outcome"] != "completed":  # type: ignore[index]
            problems.append(f"the session did not complete: {closed['data']}")
        dashboard_files = dashboard["data"]["files"]  # type: ignore[index]
        if not dashboard_files:
            problems.append("the dashboard projection rendered no file")
        else:
            page = workspace / str(dashboard_files[0])
            if not page.is_file():
                problems.append(f"the dashboard page is missing: {page}")
            elif "generated: true" not in page.read_text(encoding="utf-8"):
                problems.append("the dashboard page is not marked as generated")
        if recorded["data"]["stage_after"] != "encountered":  # type: ignore[index]
            problems.append(f"recording evidence did not move the stage: {recorded['data']}")
        if [entry["claim"] for entry in recorded["data"]["evidence"]] != ["recognition"]:  # type: ignore[index]
            problems.append("an unstated claim was not recorded as the weakest one")
        if known["data"]["state"] is None:  # type: ignore[index]
            problems.append("the recorded evidence left no learner state on the item")
        elif known["data"]["state"]["evidence_ceiling"] != "recognized":  # type: ignore[index]
            problems.append(f"the evidence ceiling was not recorded: {known['data']['state']}")
        if not bundle["data"]["sections"]:  # type: ignore[index]
            problems.append("the session context bundle carried no sections")
        if bundle["data"]["estimated_tokens"] > bundle["data"]["token_limit"]:  # type: ignore[index]
            problems.append("the session context bundle exceeded its own token budget")
        if not bundle["data"]["provenance"]["aggregation_version"]:  # type: ignore[index]
            problems.append("the bundle did not name the policy behind its conclusions")
        if installed["data"]["created"] is not True:  # type: ignore[index]
            problems.append("the bundled pack did not install from the released wheel")
        if installed["data"]["maturity"] != "pilot":  # type: ignore[index]
            problems.append("the bundled pack did not report its maturity")
        if onboarded["data"]["status"] != "finalized":  # type: ignore[index]
            problems.append(f"onboarding did not finalize: {onboarded['data']}")
        if not onboarded["data"]["resource_plan"]:  # type: ignore[index]
            problems.append("onboarding prepared no resources")
        elif onboarded["data"]["resource_plan"]["imported_items"] < 1:  # type: ignore[index]
            problems.append("onboarding imported no reference items")
        if not served["data"].get("prompt"):  # type: ignore[union-attr]
            problems.append("the calibration served no task")
        if not isinstance(status["data"], dict) or status["data"].get("stage") != DELIVERY_STAGE:
            problems.append("status did not report the current stage")
        if initialized["data"]["created"] is not True:  # type: ignore[index]
            problems.append("workspace init did not create a workspace")
        if doctor["data"]["ok"] is not True:  # type: ignore[index]
            failures = [
                check
                for check in doctor["data"]["checks"]  # type: ignore[index]
                if check["status"] == "failed"
            ]
            problems.append(f"workspace doctor failed: {failures}")
        if checked["data"]["ok"] is not True:  # type: ignore[index]
            problems.append(f"db check failed: {checked['data']}")
        if list(workspace.rglob("linguawiki/contracts.py")):
            problems.append("the generated workspace contains a copy of the core package")
        if not (workspace / "data" / "linguawiki.duckdb").is_file():
            problems.append("the learner database was not created")
        if not (workspace / "uv.lock").is_file():
            problems.append("workspace lock-dependencies did not write uv.lock")
        elif "linguawiki" not in (workspace / "uv.lock").read_text(encoding="utf-8"):
            problems.append("the workspace uv.lock does not resolve the core package")
        if locked["data"]["workspace_id"] != initialized["data"]["workspace_id"]:  # type: ignore[index]
            problems.append("lock-dependencies reported a different workspace")
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
    print(
        "Clean-environment installation check passed: workspace, bundled pack install, "
        "onboarding, a served calibration task, one recorded observation, a bounded "
        "context bundle, a session planned and closed, and the learner dashboard all work "
        "with no core source on the path"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
