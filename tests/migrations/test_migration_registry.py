from __future__ import annotations

import json
from pathlib import Path

import pytest

from linguawiki import resources
from linguawiki.db import migrations as migration_module
from linguawiki.db.schema import TABLE_ORDER
from linguawiki.errors import LinguaWikiError

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "released-migrations.json"


def test_released_migration_files_are_immutable() -> None:
    """A released migration may never be edited; add a new numbered file instead."""

    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    packaged = {
        migration.migration_id: migration.checksum for migration in migration_module.migrations()
    }

    for migration_id, checksum in recorded.items():
        assert migration_id in packaged, f"released migration disappeared: {migration_id}"
        assert packaged[migration_id] == checksum, f"released migration changed: {migration_id}"


def test_migrations_are_contiguous_and_uniquely_named() -> None:
    loaded = migration_module.migrations()
    versions = [migration.version for migration in loaded]

    assert versions == list(range(1, len(loaded) + 1))
    assert len({migration.migration_id for migration in loaded}) == len(loaded)
    assert migration_module.head_version() == versions[-1]


def test_the_first_migration_creates_the_history_table() -> None:
    assert "schema_migrations" in migration_module.migrations()[0].sql


def test_badly_named_migration_files_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "not-a-migration.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(resources, "migrations_directory", lambda: tmp_path)
    migration_module.migrations.cache_clear()

    with pytest.raises(LinguaWikiError) as failure:
        migration_module.migrations()

    assert failure.value.payload.code == "invalid_migration_name"


def test_gaps_in_the_migration_sequence_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_third.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(resources, "migrations_directory", lambda: tmp_path)
    migration_module.migrations.cache_clear()

    with pytest.raises(LinguaWikiError) as failure:
        migration_module.migrations()

    assert failure.value.payload.code == "invalid_migration_sequence"


def test_the_table_registry_lists_every_created_table_in_dependency_order() -> None:
    assert len(set(TABLE_ORDER)) == len(TABLE_ORDER)
    assert TABLE_ORDER[0] == "schema_migrations"
    assert TABLE_ORDER.index("workspaces") < TABLE_ORDER.index("workspace_versions")
    assert TABLE_ORDER.index("users") < TABLE_ORDER.index("user_languages")
    assert TABLE_ORDER.index("learning_tracks") < TABLE_ORDER.index("track_preferences")
    assert TABLE_ORDER.index("proficiency_frameworks") < TABLE_ORDER.index("language_packs")
