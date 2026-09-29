from __future__ import annotations

from pathlib import Path

import pytest

from linguawiki import resources

CORE_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "locator",
    [
        resources.privacy_policy_path,
        resources.schema_directory,
        resources.workspace_template_directory,
        resources.skill_bundle_directory,
        resources.migrations_directory,
    ],
)
def test_every_packaged_resource_resolves(locator: object) -> None:
    path = locator()  # type: ignore[operator]

    assert path.exists(), path


def test_source_checkouts_resolve_to_the_repository_copies() -> None:
    assert resources.privacy_policy_path() == CORE_ROOT / "config" / "privacy-policy.toml"
    assert resources.schema_directory() == CORE_ROOT / "schemas"
    assert resources.skill_bundle_directory() == CORE_ROOT / ".agents" / "skills"


def test_installed_wheels_prefer_the_bundled_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a wheel the resources live under linguawiki/resources/, not beside src/."""

    monkeypatch.setattr(resources, "_INSTALLED_RESOURCES", CORE_ROOT)

    assert resources.privacy_policy_path() == CORE_ROOT / "config/privacy-policy.toml"
    assert resources.schema_directory() == CORE_ROOT / "schemas"


def test_migrations_always_ship_inside_the_package() -> None:
    assert resources.migrations_directory() == resources.package_root() / "db" / "sql"
    assert sorted(path.name for path in resources.migrations_directory().glob("*.sql"))[0] == (
        "0001_schema_migration_history.sql"
    )
