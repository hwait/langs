from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from linguawiki.codex_discovery import output_discovers_skill

ROOT = Path(__file__).resolve().parents[2]


def test_prompt_debugger_output_parser_finds_repository_skill() -> None:
    output = json.dumps(
        [
            {
                "text": (
                    f"- `r2` = `{ROOT / '.agents' / 'skills'}`\n"
                    "- linguawiki: description (file: r2/linguawiki/SKILL.md)"
                )
            }
        ]
    )

    assert output_discovers_skill(output, root=ROOT, skill_name="linguawiki")


@pytest.mark.external
@pytest.mark.skipif(
    os.environ.get("LINGUAWIKI_RUN_CODEX_DISCOVERY") != "1",
    reason="set LINGUAWIKI_RUN_CODEX_DISCOVERY=1 for a real Codex discovery check",
)
def test_installed_codex_discovers_repository_skill() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_codex_skill_discovery.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
