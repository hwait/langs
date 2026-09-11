from __future__ import annotations

from pathlib import Path

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.paths import (
    assert_outside,
    assert_safe_destructive_target,
    assert_within,
    directory_is_empty,
    find_core_repository_root,
    find_git_root,
    require_initialized_workspace,
    resolve_path,
    workspace_paths,
)

CORE_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_paths_are_rejected(value: str) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        resolve_path(value, purpose="workspace")

    assert failure.value.payload.code == "invalid_path"


def test_relative_paths_are_resolved_against_the_process_directory(tmp_path: Path) -> None:
    resolved = resolve_path("child/../child/file", purpose="workspace")

    assert resolved.is_absolute()
    assert ".." not in resolved.parts
    assert tmp_path != resolved


def test_home_relative_paths_are_expanded() -> None:
    assert resolve_path("~", purpose="workspace") == Path.home().resolve()


@pytest.mark.parametrize(
    "target",
    [Path("/"), Path("/usr"), Path.home(), Path.home().parent],
)
def test_root_and_home_targets_are_refused(target: Path) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        assert_safe_destructive_target(target.resolve(), purpose="workspace")

    assert failure.value.payload.code == "unsafe_target"
    assert failure.value.payload.details[0].context["path"] == str(target.resolve())


def test_deep_paths_are_accepted(tmp_path: Path) -> None:
    target = tmp_path / "PolishLinguaWiki"

    assert assert_safe_destructive_target(target, purpose="workspace") == target


def test_destructive_targets_must_stay_inside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    assert assert_within(root / "data" / "linguawiki.duckdb", root, purpose="database")
    with pytest.raises(LinguaWikiError) as failure:
        assert_within(tmp_path / "elsewhere", root, purpose="database")

    assert failure.value.payload.code == "unsafe_target"


def test_backup_roots_must_stay_outside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    assert assert_outside(tmp_path / "backups", root, purpose="backup root")
    with pytest.raises(LinguaWikiError) as failure:
        assert_outside(root / "backups", root, purpose="backup root")

    assert failure.value.payload.code == "backup_root_invalid"


def test_core_repository_is_detected_from_inside_the_product_tree() -> None:
    assert find_core_repository_root(CORE_ROOT / "src" / "linguawiki") == CORE_ROOT


def test_core_repository_is_not_detected_outside_it(tmp_path: Path) -> None:
    assert find_core_repository_root(tmp_path) is None


def test_git_root_is_detected_from_a_nested_path(tmp_path: Path) -> None:
    (tmp_path / "repository" / ".git").mkdir(parents=True)
    nested = tmp_path / "repository" / "nested" / "deeper"
    nested.mkdir(parents=True)

    assert find_git_root(nested) == tmp_path / "repository"
    assert find_git_root(tmp_path) is None


def test_directory_emptiness(tmp_path: Path) -> None:
    assert directory_is_empty(tmp_path)
    (tmp_path / "file").write_text("", encoding="utf-8")
    assert not directory_is_empty(tmp_path)


def test_workspace_paths_expose_the_expected_layout(tmp_path: Path) -> None:
    paths = workspace_paths(tmp_path / "PolishLinguaWiki")

    assert paths.config.name == "linguawiki.toml"
    assert paths.lock.name == "linguawiki.lock"
    assert paths.database.relative_to(paths.root) == Path("data") / "linguawiki.duckdb"
    assert paths.skills.relative_to(paths.root) == Path(".agents") / "skills"


def test_uninitialized_workspaces_are_refused(tmp_path: Path) -> None:
    with pytest.raises(LinguaWikiError) as missing:
        require_initialized_workspace(tmp_path / "absent")
    assert missing.value.payload.code == "workspace_not_found"

    with pytest.raises(LinguaWikiError) as empty:
        require_initialized_workspace(tmp_path)
    assert empty.value.payload.code == "workspace_not_initialized"
