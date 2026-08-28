from datetime import UTC, datetime
from pathlib import Path

import pytest
from jinja2 import UndefinedError

from linguawiki.ids import WorkspaceId
from linguawiki.workspace_template import WorkspaceTemplateContext, render_workspace_contracts

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "learner-workspace"


def test_workspace_template_contains_contract_files_but_no_core_source() -> None:
    files = {
        path.relative_to(TEMPLATE).as_posix() for path in TEMPLATE.rglob("*") if path.is_file()
    }

    assert files == {
        ".gitignore.j2",
        "AGENTS.md.j2",
        "linguawiki.lock.j2",
        "linguawiki.toml.j2",
        "pyproject.toml.j2",
    }
    assert not (TEMPLATE / "src" / "linguawiki").exists()


def test_workspace_template_pins_core_and_excludes_private_state() -> None:
    project = (TEMPLATE / "pyproject.toml.j2").read_text(encoding="utf-8")
    ignore = (TEMPLATE / ".gitignore.j2").read_text(encoding="utf-8")

    assert "linguawiki=={{ core_version }}" in project
    for private_path in ("*.duckdb", "backups/", "recordings/", "transcripts/raw/"):
        assert private_path in ignore


def test_workspace_templates_render_and_validate_against_v1_contracts() -> None:
    context = WorkspaceTemplateContext(
        workspace_name="Synthetic Learner Wiki",
        normalized_workspace_name="synthetic-learner-wiki",
        workspace_id=WorkspaceId("wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        timezone="Europe/Warsaw",
        backup_root="/external/synthetic-backup",
        core_version="0.1.0",
        core_sha256="0" * 64,
        database_schema_version="0",
        database_schema_sha256="1" * 64,
        skill_bundle_version="0.1.0",
        skill_bundle_sha256="2" * 64,
    )

    rendered = render_workspace_contracts(TEMPLATE, context)

    assert rendered.configuration.schema_name == "lingua.workspace.v1"
    assert rendered.configuration.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert rendered.lock.schema_name == "linguawiki.lock.v1"
    assert set(rendered.files) == {
        ".gitignore.j2",
        "AGENTS.md.j2",
        "linguawiki.lock.j2",
        "linguawiki.toml.j2",
        "pyproject.toml.j2",
    }


def test_every_workspace_template_is_rendered_with_strict_variables(tmp_path: Path) -> None:
    for source in TEMPLATE.iterdir():
        (tmp_path / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    agents = tmp_path / "AGENTS.md.j2"
    agents.write_text(
        agents.read_text(encoding="utf-8") + "\n{{ misspelled_workspace_name }}\n",
        encoding="utf-8",
    )
    context = WorkspaceTemplateContext(
        workspace_name="Synthetic Learner Wiki",
        normalized_workspace_name="synthetic-learner-wiki",
        workspace_id=WorkspaceId("wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        timezone="Europe/Warsaw",
        backup_root="/external/synthetic-backup",
        core_version="0.1.0",
        core_sha256="0" * 64,
        database_schema_version="0",
        database_schema_sha256="1" * 64,
        skill_bundle_version="0.1.0",
        skill_bundle_sha256="2" * 64,
    )

    with pytest.raises(UndefinedError, match="misspelled_workspace_name"):
        render_workspace_contracts(tmp_path, context)
