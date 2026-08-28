from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents" / "skills" / "linguawiki" / "SKILL.md"


def clean_subprocess_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE")}


def test_smoke_skill_command_works_in_isolated_directory(tmp_path: Path) -> None:
    executable = shutil.which("linguawiki", path=sysconfig.get_path("scripts"))
    assert executable is not None, "the installed linguawiki console script was not on PATH"
    result = subprocess.run(
        [executable, "status", "--format", "json"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
        env=clean_subprocess_environment(),
    )

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["stage"] == 0


def test_repository_skill_validator() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_skills.py")],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=clean_subprocess_environment(),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_skill_uses_codex_repository_discovery_location() -> None:
    assert SKILL.is_file()
    assert not (ROOT / "skills" / "linguawiki" / "SKILL.md").exists()
