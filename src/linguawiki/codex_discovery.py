"""Parsing helpers for Codex repository-skill discovery diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _all_strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _all_strings(item)]
    return []


def output_discovers_skill(output: str, *, root: Path, skill_name: str) -> bool:
    payload = json.loads(output)
    combined = "\n".join(_all_strings(payload))
    skill_root = root / ".agents" / "skills"
    return (
        f"- {skill_name}:" in combined
        and str(skill_root) in combined
        and f"{skill_name}/SKILL.md" in combined
    )
