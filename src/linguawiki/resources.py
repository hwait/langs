"""Locate packaged resources in both a source checkout and an installed wheel."""

from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_INSTALLED_RESOURCES = _PACKAGE_ROOT / "resources"
_SOURCE_ROOT = _PACKAGE_ROOT.parents[1]


def _resource(relative: str) -> Path:
    """Prefer the wheel's bundled copy and fall back to the source tree."""

    installed = _INSTALLED_RESOURCES / relative
    return installed if installed.exists() else _SOURCE_ROOT / relative


def package_root() -> Path:
    return _PACKAGE_ROOT


def privacy_policy_path() -> Path:
    return _resource("config/privacy-policy.toml")


def schema_directory() -> Path:
    return _resource("schemas")


def workspace_template_directory() -> Path:
    return _resource("templates/learner-workspace")


def skill_bundle_directory() -> Path:
    installed = _INSTALLED_RESOURCES / "skills"
    return installed if installed.exists() else _SOURCE_ROOT / ".agents" / "skills"


def language_packs_directory() -> Path:
    """Packs that ship with the core release, resolvable by pack key."""

    return _resource("language-packs")


def migrations_directory() -> Path:
    return _PACKAGE_ROOT / "db" / "sql"
