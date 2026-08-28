#!/usr/bin/env python3
"""Deterministically validate repository-scoped Codex skill metadata."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".agents" / "skills"
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,63}$")


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{path}: missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: unterminated YAML frontmatter") from exc
    loaded = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    if not all(isinstance(value, str) for value in loaded.values()):
        raise ValueError(f"{path}: frontmatter values must be strings")
    return loaded


def _validate_local(skill: Path) -> None:
    metadata = _frontmatter(skill / "SKILL.md")
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if name != skill.name or not NAME_PATTERN.fullmatch(name):
        raise ValueError(f"{skill}: invalid or mismatched skill name")
    if not description:
        raise ValueError(f"{skill}: description is required")
    openai = skill / "agents" / "openai.yaml"
    if not openai.is_file():
        raise ValueError(f"{skill}: agents/openai.yaml is required for Codex")
    loaded = yaml.safe_load(openai.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{openai}: metadata must be a YAML mapping")
    interface = loaded.get("interface")
    policy = loaded.get("policy")
    if not isinstance(interface, dict) or not isinstance(policy, dict):
        raise ValueError(f"{openai}: interface and policy mappings are required")
    prompt = interface.get("default_prompt")
    if not isinstance(prompt, str) or f"${name}" not in prompt:
        raise ValueError(f"{openai}: default_prompt must mention ${name}")
    if policy.get("allow_implicit_invocation") is not True:
        raise ValueError(f"{openai}: implicit invocation policy must remain enabled")


def _run_external_validator(validator: Path, skill: Path) -> int:
    result = subprocess.run(
        [sys.executable, str(validator), str(skill)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        print(result.stdout, end="", file=sys.stderr)
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--external-validator",
        type=Path,
        help="explicit validator path; never auto-discovered",
    )
    args = parser.parse_args()
    skills = sorted(path.parent for path in SKILLS.glob("*/SKILL.md"))
    if not skills:
        print("No skills found", file=sys.stderr)
        return 1
    for skill in skills:
        try:
            _validate_local(skill)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        if args.external_validator is not None:
            if not args.external_validator.is_file():
                print(f"validator does not exist: {args.external_validator}", file=sys.stderr)
                return 1
            if result := _run_external_validator(args.external_validator, skill):
                return result
    print(f"Validated {len(skills)} skill(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
