"""Strict rendering and contract validation for learner-workspace templates."""

from __future__ import annotations

import json
import tomllib
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, StrictUndefined
from pydantic import Field, field_validator

from linguawiki.clock import require_utc, validate_iana_timezone
from linguawiki.contracts import LockManifest, WorkspaceManifest
from linguawiki.ids import WorkspaceId
from linguawiki.models import ContractModel


class WorkspaceTemplateContext(ContractModel):
    workspace_name: str = Field(min_length=1)
    normalized_workspace_name: str = Field(pattern=r"^[a-z0-9-]+$")
    workspace_id: WorkspaceId
    created_at: datetime
    timezone: str
    backup_root: str = Field(min_length=1)
    core_version: str = Field(min_length=1)
    core_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    database_schema_version: str = Field(min_length=1)
    database_schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    skill_bundle_version: str = Field(min_length=1)
    skill_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    _created_at_utc = field_validator("created_at")(require_utc)
    _valid_timezone = field_validator("timezone")(validate_iana_timezone)


class RenderedWorkspaceContracts(ContractModel):
    configuration: WorkspaceManifest
    lock: LockManifest
    files: dict[str, str]


def _environment(template_root: Path) -> Environment:
    return Environment(
        loader=None,
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def _render(template_root: Path, name: str, values: dict[str, str]) -> str:
    source = (template_root / name).read_text(encoding="utf-8")
    return _environment(template_root).from_string(source).render(values)


def render_workspace_contracts(
    template_root: Path, context: WorkspaceTemplateContext
) -> RenderedWorkspaceContracts:
    """Render every workspace template and validate contract-bearing representations."""

    values = {key: str(value) for key, value in context.model_dump(mode="json").items()}
    values["created_at"] = context.created_at.isoformat().replace("+00:00", "Z")
    names = (
        ".gitignore.j2",
        "AGENTS.md.j2",
        "linguawiki.lock.j2",
        "linguawiki.toml.j2",
        "pyproject.toml.j2",
    )
    rendered = {name: _render(template_root, name, values) for name in names}
    configuration = WorkspaceManifest.model_validate(tomllib.loads(rendered["linguawiki.toml.j2"]))
    lock = LockManifest.model_validate(json.loads(rendered["linguawiki.lock.j2"]))
    return RenderedWorkspaceContracts(configuration=configuration, lock=lock, files=rendered)
