from __future__ import annotations

from pathlib import Path

from linguawiki import __version__
from linguawiki.db import migrations as migration_module
from linguawiki.versions import (
    core_pin,
    core_source_hash,
    file_sha256,
    skill_bundle_hash,
    skill_bundle_pin,
    tree_entries,
    tree_sha256,
)


def test_tree_hash_is_order_independent_but_content_sensitive() -> None:
    first = tree_sha256([("a", "1" * 64), ("b", "2" * 64)])
    reordered = tree_sha256([("b", "2" * 64), ("a", "1" * 64)])
    renamed = tree_sha256([("a", "1" * 64), ("c", "2" * 64)])

    assert first == reordered
    assert first != renamed


def test_tree_entries_skip_caches_and_dotfiles(tmp_path: Path) -> None:
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.pyc").write_bytes(b"x")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "kept.md").write_text("x", encoding="utf-8")

    assert set(tree_entries(tmp_path)) == {"nested/kept.md"}


def test_tree_entries_can_skip_named_files(tmp_path: Path) -> None:
    (tmp_path / "kept.md").write_text("x", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("x", encoding="utf-8")

    assert set(tree_entries(tmp_path, skip=("ignored.md",))) == {"kept.md"}


def test_file_hash_matches_content(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"lingua")

    assert file_sha256(path) == tree_entries(tmp_path)["file"]


def test_core_and_bundle_pins_are_stable_and_versioned() -> None:
    assert core_pin() == core_pin()
    assert core_pin().version == __version__
    assert core_pin().sha256 == core_source_hash()
    assert skill_bundle_pin().sha256 == skill_bundle_hash()
    assert core_pin().sha256 != skill_bundle_pin().sha256


def test_database_schema_pin_tracks_the_migration_head() -> None:
    pin = migration_module.database_schema_pin()

    assert pin.version == str(migration_module.head_version())
    assert pin == migration_module.database_schema_pin()
