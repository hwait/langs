"""Initialization is atomic: an interrupted run must leave a retryable target."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import duckdb
import pytest

from linguawiki.db import migrations as migration_module
from linguawiki.errors import LinguaWikiError
from linguawiki.paths import workspace_paths
from linguawiki.services import workspace as workspace_service
from linguawiki.services.privacy import GIT_SAFE_TOP_LEVEL
from tests.conftest import SyntheticWorkspace
from tests.support.clocks import AdvancingClock

INTERRUPTION_POINTS = (
    "install_bundle",
    "write_lock",
    "_git_init",
)


def _options(path: Path, backup_root: Path, **extra: object) -> workspace_service.InitOptions:
    return workspace_service.InitOptions(
        path=path,
        backup_root=backup_root,
        name="Polish LinguaWiki",
        timezone="Europe/Warsaw",
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("interruption", INTERRUPTION_POINTS)
def test_an_interrupted_initialization_leaves_the_target_untouched(
    interruption: str,
    tmp_path: Path,
    backup_root: Path,
    clock: AdvancingClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "PolishLinguaWiki"

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError(f"interrupted during {interruption}")

    monkeypatch.setattr(workspace_service, interruption, explode)

    with pytest.raises(RuntimeError):
        workspace_service.initialize(_options(target, backup_root, git_init=True), clock=clock)

    assert not target.exists()
    assert workspace_service.abandoned_staging_directories(workspace_paths(target)) == ()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["backups"]


@pytest.mark.parametrize("interruption", INTERRUPTION_POINTS)
def test_initialization_can_be_retried_after_an_interruption(
    interruption: str,
    tmp_path: Path,
    backup_root: Path,
    clock: AdvancingClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "PolishLinguaWiki"

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(workspace_service, interruption, explode)
    with pytest.raises(RuntimeError):
        workspace_service.initialize(_options(target, backup_root, git_init=True), clock=clock)
    monkeypatch.undo()

    report = workspace_service.initialize(_options(target, backup_root, git_init=True), clock=clock)
    doctor = workspace_service.doctor(target, clock=clock)

    assert report.created is True
    assert report.database_schema_version == migration_module.head_version()
    assert report.git_initialized is True
    assert (target / ".git").is_dir()
    assert doctor.ok is True


def test_a_failed_migration_during_initialization_is_also_retryable(
    tmp_path: Path,
    backup_root: Path,
    clock: AdvancingClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "PolishLinguaWiki"
    monkeypatch.setattr(
        migration_module,
        "migrate",
        lambda _database: (_ for _ in ()).throw(RuntimeError("migration exploded")),
    )

    with pytest.raises(RuntimeError):
        workspace_service.initialize(_options(target, backup_root), clock=clock)
    monkeypatch.undo()

    assert not target.exists()
    assert workspace_service.initialize(_options(target, backup_root), clock=clock).created


def test_a_leftover_staging_directory_is_reported_but_never_deleted(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """Whatever sits at a staging-shaped path is not ours to destroy."""

    target = tmp_path / "PolishLinguaWiki"
    tmp_path.mkdir(parents=True, exist_ok=True)
    abandoned = tmp_path / f"{workspace_service.staging_prefix(workspace_paths(target))}earlier"
    abandoned.mkdir(parents=True)
    (abandoned / "half-written.toml").write_text("possibly precious", encoding="utf-8")

    report = workspace_service.initialize(_options(target, backup_root), clock=clock)

    assert abandoned.is_dir()
    assert (abandoned / "half-written.toml").read_text(encoding="utf-8") == "possibly precious"
    assert any("interrupted run left a staging directory" in item for item in report.warnings)
    assert not (target / "half-written.toml").exists()
    assert workspace_service.doctor(target, clock=clock).ok is True


def test_initialization_never_removes_a_pre_existing_sibling_directory(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """The old fixed staging path silently destroyed whatever already lived there."""

    target = tmp_path / "PolishLinguaWiki"
    prefix = workspace_service.staging_prefix(workspace_paths(target))
    collisions = [tmp_path / prefix.rstrip("-"), tmp_path / f"{prefix}0"]
    for collision in collisions:
        collision.mkdir(parents=True)
        (collision / "someone-elses-data.txt").write_text("precious", encoding="utf-8")

    workspace_service.initialize(_options(target, backup_root), clock=clock)

    for collision in collisions:
        assert (collision / "someone-elses-data.txt").read_text(encoding="utf-8") == "precious"


def test_each_initialization_owns_a_unique_staging_directory(
    tmp_path: Path, backup_root: Path
) -> None:
    """Two concurrent initializers must never be handed the same staging path."""

    paths = workspace_paths(tmp_path / "PolishLinguaWiki")
    first = workspace_service.create_staging_directory(paths)
    second = workspace_service.create_staging_directory(paths)

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert first.name.startswith(workspace_service.staging_prefix(paths))
    assert set(workspace_service.abandoned_staging_directories(paths)) == {first, second}


def test_no_staging_directory_survives_a_successful_initialization(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    assert workspace_service.abandoned_staging_directories(synthetic_workspace.paths) == ()


def test_a_published_workspace_is_private_to_its_owner(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The staging directory is created 0700 and that mode survives publication."""

    mode = synthetic_workspace.root.stat().st_mode

    assert mode & 0o077 == 0


def test_initialization_publishes_into_an_existing_empty_directory(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    target = tmp_path / "PolishLinguaWiki"
    target.mkdir()

    report = workspace_service.initialize(_options(target, backup_root), clock=clock)

    assert report.created is True
    assert (target / "linguawiki.toml").is_file()


def test_an_interruption_never_touches_an_already_initialized_workspace(
    synthetic_workspace: SyntheticWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (synthetic_workspace.root / "linguawiki.lock").read_bytes()
    monkeypatch.setattr(
        workspace_service,
        "install_bundle",
        lambda *_, **__: (_ for _ in ()).throw(RuntimeError("must not be called")),
    )

    report = workspace_service.initialize(
        _options(synthetic_workspace.root, synthetic_workspace.backup_root),
        clock=synthetic_workspace.clock,
    )

    assert report.created is False
    assert (synthetic_workspace.root / "linguawiki.lock").read_bytes() == before


def test_the_committed_top_level_matches_the_git_safe_allowlist(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """Everything a fresh workspace commits at the top level is explicitly allowed."""

    private = {"data", "artifacts", "imports", "drafts", "exports"}
    top_level = {path.name for path in synthetic_workspace.root.iterdir()} - private

    assert top_level <= set(GIT_SAFE_TOP_LEVEL)
    assert "uv.lock" in GIT_SAFE_TOP_LEVEL


def _dependency_lock_status(workspace: SyntheticWorkspace) -> str:
    report = workspace_service.doctor(workspace.root, clock=workspace.clock)
    return {check.name: check.status for check in report.checks}["dependency_lock"]


SOURCE = 'source = { registry = "https://pypi.org/simple" }\n'
REAL_LOCK = Path(__file__).resolve().parents[1] / "fixtures" / "workspace-uv-lock" / "uv.lock"


def _install_real_lock(workspace: SyntheticWorkspace) -> None:
    """Use a lock uv actually produced; fabricated locks are only negative cases."""

    workspace.paths.dependency_lock.write_text(
        REAL_LOCK.read_text(encoding="utf-8"), encoding="utf-8"
    )


def _edit_lock(workspace: SyntheticWorkspace, old: str, new: str) -> None:
    _install_real_lock(workspace)
    text = workspace.paths.dependency_lock.read_text(encoding="utf-8")
    assert old in text, f"fixture no longer contains {old!r}; regenerate it"
    workspace.paths.dependency_lock.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_an_absent_dependency_lock_is_a_warning(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    assert synthetic_workspace.report.dependency_lock is False
    assert _dependency_lock_status(synthetic_workspace) == "warning"
    assert (
        workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock).ok
        is True
    )


def test_a_lock_uv_actually_produced_passes(synthetic_workspace: SyntheticWorkspace) -> None:
    _install_real_lock(synthetic_workspace)
    lock = workspace_service.load_lock(synthetic_workspace.paths)

    assert workspace_service.inspect_dependency_lock(synthetic_workspace.paths, lock) is None
    assert _dependency_lock_status(synthetic_workspace) == "ok"


def test_the_retained_lock_pins_exactly_what_the_core_requires(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """If the core's pinned dependencies change, the fixture must be regenerated."""

    _install_real_lock(synthetic_workspace)
    payload = tomllib.loads(synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8"))
    resolved = {package["name"]: package["version"] for package in payload["package"]}

    for name, pin in workspace_service.core_requirements().items():
        assert name in resolved, name
        if pin is not None:
            assert resolved[name] == pin, name


def test_the_core_requirements_carry_their_exact_pins() -> None:
    requirements = workspace_service.core_requirements()

    assert requirements["duckdb"] == duckdb.__version__
    assert all(pin is not None for pin in requirements.values()), (
        "the core pins its direct dependencies exactly"
    )
    assert set(requirements) < set(workspace_service.core_dependency_constraints())


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("not toml", "not a valid uv lock"),
        ("empty", ""),
        ("no format version", '[[package]]\nname = "linguawiki"\nversion = "0.1.0"\n'),
        ("no packages", 'version = 1\npackage = "linguawiki"\n'),
        (
            "core only",
            'version = 1\n\n[[package]]\nname = "linguawiki"\nversion = "0.1.0"\n',
        ),
        (
            "workspace project absent",
            'version = 1\n\n[[package]]\nname = "linguawiki"\nversion = "0.1.0"\n'
            '\n[[package]]\nname = "duckdb"\nversion = "1.5.5"\n',
        ),
        (
            "package without a version",
            'version = 1\n\n[[package]]\nname = "polish-linguawiki"\n',
        ),
    ],
)
def test_a_hand_written_lock_does_not_pass(
    label: str, content: str, synthetic_workspace: SyntheticWorkspace
) -> None:
    synthetic_workspace.paths.dependency_lock.write_text(content, encoding="utf-8")
    lock = workspace_service.load_lock(synthetic_workspace.paths)

    assert workspace_service.inspect_dependency_lock(synthetic_workspace.paths, lock) is not None
    assert _dependency_lock_status(synthetic_workspace) == "failed"


def test_a_lock_whose_workspace_does_not_depend_on_the_core_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The exact shape the previous validator accepted: names present, edge missing."""

    _install_real_lock(synthetic_workspace)
    payload = tomllib.loads(synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8"))
    packages = "\n".join(
        f'[[package]]\nname = "{package["name"]}"\nversion = "{package["version"]}"\n{SOURCE}'
        for package in payload["package"]
    )
    synthetic_workspace.paths.dependency_lock.write_text(
        f"version = 1\n\n{packages}", encoding="utf-8"
    )

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None
    assert problem.reason == "does not make this workspace depend on the core"


def test_the_closure_reaches_past_the_cores_direct_dependencies() -> None:
    """The direct requirements alone are not the graph a workspace has to resolve."""

    direct = set(workspace_service.core_requirements())
    closure = workspace_service.core_dependency_closure()

    assert direct < set(closure)
    # Transitive packages nothing depends on directly must still be required.
    assert {"markupsafe", "pydantic-core", "attrs", "six"} <= set(closure)
    assert closure["duckdb"] == duckdb.__version__
    assert closure["markupsafe"] is None


def test_a_lock_that_drops_every_transitive_package_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """A structurally valid lock naming only the core's direct dependencies."""

    direct = workspace_service.core_requirements()
    edges = "".join(f'    {{ name = "{name}" }},\n' for name in sorted(direct))
    packages = "".join(
        f'\n[[package]]\nname = "{name}"\nversion = "{pin}"\n{SOURCE}'
        for name, pin in sorted(direct.items())
    )
    synthetic_workspace.paths.dependency_lock.write_text(
        "version = 1\n\n"
        '[[package]]\nname = "polish-linguawiki"\nversion = "0.1.0"\n'
        f"{SOURCE}"
        'dependencies = [{ name = "linguawiki" }]\n\n'
        '[[package]]\nname = "linguawiki"\nversion = "0.1.0"\n'
        f"{SOURCE}"
        f"dependencies = [\n{edges}]\n{packages}",
        encoding="utf-8",
    )

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None
    assert problem.reason == "does not reach the whole core dependency graph from this workspace"
    assert "markupsafe" in problem.detail
    assert _dependency_lock_status(synthetic_workspace) == "failed"


@pytest.mark.parametrize("dropped", ["markupsafe", "pydantic-core", "attrs", "six"])
def test_a_lock_missing_one_transitive_package_is_rejected(
    dropped: str, synthetic_workspace: SyntheticWorkspace
) -> None:
    _install_real_lock(synthetic_workspace)
    text = synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8")
    blocks = text.split("\n[[package]]\n")
    kept = [blocks[0]] + [
        block for block in blocks[1:] if not block.startswith(f'name = "{dropped}"')
    ]
    synthetic_workspace.paths.dependency_lock.write_text(
        "\n[[package]]\n".join(kept), encoding="utf-8"
    )

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None
    assert dropped in problem.detail


@pytest.mark.parametrize(
    ("label", "mutation", "reason"),
    [
        ("no source", lambda text: text.replace(SOURCE, "", 1), "without a source"),
        (
            "duplicate identity",
            lambda text: text + f'\n[[package]]\nname = "markupsafe"\nversion = "3.0.3"\n{SOURCE}',
            "same package identity twice",
        ),
    ],
)
def test_a_lock_that_uv_would_reject_is_rejected(
    label: str,
    mutation: object,
    reason: str,
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """Structural fields uv requires are part of "a valid lock", not decoration."""

    _install_real_lock(synthetic_workspace)
    text = synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8")
    synthetic_workspace.paths.dependency_lock.write_text(
        mutation(text),  # type: ignore[operator]
        encoding="utf-8",
    )

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None, label
    assert reason in problem.reason


def test_a_lock_whose_sources_are_empty_mappings_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The case doctor accepted while `uv lock --check --offline` refused it."""

    _install_real_lock(synthetic_workspace)
    text = synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8")
    synthetic_workspace.paths.dependency_lock.write_text(
        re.sub(r"^source = \{.*\}$", "source = {}", text, flags=re.MULTILINE), encoding="utf-8"
    )

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )
    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert problem is not None
    assert "empty source" in problem.reason
    assert report.ok is False
    assert {check.name: check.status for check in report.checks}["dependency_lock"] == "failed"


def test_a_lock_stripped_of_every_source_is_rejected_by_both_checks(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The case doctor used to accept while `uv lock --check` refused it."""

    _install_real_lock(synthetic_workspace)
    text = synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8")
    synthetic_workspace.paths.dependency_lock.write_text(
        "\n".join(line for line in text.splitlines() if not line.startswith("source = ")),
        encoding="utf-8",
    )

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)
    statuses = {check.name: check.status for check in report.checks}

    assert report.ok is False
    assert statuses["dependency_lock"] == "failed"
    # doctor's verdict is offline and structural; uv is consulted where uv is required.
    assert "dependency_lock_uv" not in statuses


@pytest.mark.external
def test_uv_confirms_the_retained_lock(synthetic_workspace: SyntheticWorkspace) -> None:
    """The fixture must be a lock uv itself still accepts as current.

    Marked `external`: uv needs an index or a warm cache to answer.
    """

    vendored = Path(__file__).resolve().parents[2] / ".tools"
    if shutil.which("uv") is None and (vendored / "uv").is_file():
        os.environ["PATH"] = f"{vendored}{os.pathsep}{os.environ['PATH']}"
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    _install_real_lock(synthetic_workspace)

    accepted, detail = workspace_service.uv_lock_verdict(synthetic_workspace.paths) or (None, "")

    assert accepted is True, detail
    assert (
        workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock).ok
        is True
    )


def test_a_lock_with_a_duplicate_version_stub_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """An edge qualified to a stub version was satisfied by a later real entry."""

    _install_real_lock(synthetic_workspace)
    text = synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8")
    stub = f'[[package]]\nname = "markupsafe"\nversion = "0.0.0"\n{SOURCE}\n'
    patched = text.replace(
        '[[package]]\nname = "markupsafe"', stub + '[[package]]\nname = "markupsafe"', 1
    )
    patched = patched.replace(
        '{ name = "markupsafe" }', '{ name = "markupsafe", version = "0.0.0" }'
    )
    synthetic_workspace.paths.dependency_lock.write_text(patched, encoding="utf-8")

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None
    assert "markupsafe 0.0.0 does not satisfy" in problem.detail
    assert _dependency_lock_status(synthetic_workspace) == "failed"


def test_a_lock_with_an_unqualified_edge_to_a_duplicated_package_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """With two candidates and no version on the edge, nothing is selected."""

    _install_real_lock(synthetic_workspace)
    text = synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8")
    stub = f'[[package]]\nname = "markupsafe"\nversion = "0.0.0"\n{SOURCE}\n'
    synthetic_workspace.paths.dependency_lock.write_text(
        text.replace(
            '[[package]]\nname = "markupsafe"', stub + '[[package]]\nname = "markupsafe"', 1
        ),
        encoding="utf-8",
    )

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None
    assert problem.reason == "has dependency edges that resolve to nothing"


@pytest.mark.parametrize("version", [0, 2, 999])
def test_an_unsupported_lock_format_version_is_rejected(
    version: int, synthetic_workspace: SyntheticWorkspace
) -> None:
    _install_real_lock(synthetic_workspace)
    text = synthetic_workspace.paths.dependency_lock.read_text(encoding="utf-8")
    synthetic_workspace.paths.dependency_lock.write_text(
        text.replace("version = 1\n", f"version = {version}\n", 1), encoding="utf-8"
    )

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None
    assert problem.reason == "uses a uv lock format version this release does not support"
    assert problem.detail == str(version)


def test_a_lock_with_a_dangling_dependency_edge_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    _edit_lock(synthetic_workspace, 'name = "duckdb"', 'name = "duckdb-removed"')

    problem = workspace_service.inspect_dependency_lock(
        synthetic_workspace.paths, workspace_service.load_lock(synthetic_workspace.paths)
    )

    assert problem is not None
    assert problem.reason == "has dependency edges that resolve to nothing"
    assert "duckdb" in problem.detail


def test_a_lock_resolving_an_unpinned_dependency_version_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """A fabricated graph can name every package; it cannot fake the pinned versions."""

    _edit_lock(
        synthetic_workspace,
        f'name = "duckdb"\nversion = "{duckdb.__version__}"',
        'name = "duckdb"\nversion = "1.0.0"',
    )

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)
    check = next(item for item in report.checks if item.name == "dependency_lock")

    assert check.status == "failed"
    assert "do not satisfy the core's requirements" in check.message
    assert f"duckdb 1.0.0 does not satisfy =={duckdb.__version__}" in check.context["detail"]


def test_a_lock_that_does_not_resolve_this_workspace_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    _edit_lock(synthetic_workspace, 'name = "polish-linguawiki"', 'name = "some-other-workspace"')

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)
    check = next(item for item in report.checks if item.name == "dependency_lock")

    assert check.status == "failed"
    assert "this workspace project" in check.message
    assert check.context["detail"] == "polish-linguawiki"


def test_a_dependency_lock_for_another_core_release_is_rejected(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    _edit_lock(
        synthetic_workspace,
        'name = "linguawiki"\nversion = "0.1.0"',
        'name = "linguawiki"\nversion = "9.9.9"',
    )

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)
    check = next(item for item in report.checks if item.name == "dependency_lock")

    assert check.status == "failed"
    assert check.context["detail"] == "9.9.9 != 0.1.0"


def test_resolving_the_dependency_lock_reports_why_it_cannot_succeed_yet(
    synthetic_workspace: SyntheticWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace pins a released core, so uv can only resolve a published version."""

    vendored = Path(__file__).resolve().parents[2] / ".tools"
    if shutil.which("uv") is None and (vendored / "uv").is_file():
        monkeypatch.setenv("PATH", f"{vendored}{os.pathsep}{os.environ['PATH']}")
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.lock_dependencies(synthetic_workspace.root)

    assert failure.value.payload.code == "uv_lock_failed"
    assert failure.value.payload.details[0].context["stderr"]
    assert not synthetic_workspace.paths.dependency_lock.exists()


def test_a_missing_uv_is_reported_rather_than_skipped(
    synthetic_workspace: SyntheticWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_service.shutil, "which", lambda _name: None)

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.lock_dependencies(synthetic_workspace.root)

    assert failure.value.payload.code == "uv_unavailable"


def test_find_links_is_passed_through_to_uv(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A built-but-unpublished release is resolved from its artifact directory."""

    artifacts = tmp_path / "dist"
    artifacts.mkdir()
    recorded: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        recorded.setdefault("command", command)
        recorded["last"] = command
        _install_real_lock(synthetic_workspace)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(workspace_service.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(workspace_service.subprocess, "run", fake_run)

    report = workspace_service.lock_dependencies(synthetic_workspace.root, find_links=artifacts)

    assert recorded["command"][:2] == ["uv", "lock"]
    assert recorded["command"][-2:] == ["--find-links", str(artifacts.resolve())]
    # The lock is verified against the workspace manifest before it is accepted.
    assert recorded["last"][-1] == "--check"
    assert report.workspace_id == synthetic_workspace.report.workspace_id
    assert _dependency_lock_status(synthetic_workspace) == "ok"


def test_a_uv_run_that_writes_no_usable_lock_is_a_failure(
    synthetic_workspace: SyntheticWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero exit code is not enough; the lock has to actually resolve the core."""

    monkeypatch.setattr(workspace_service.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        workspace_service.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
    )

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.lock_dependencies(synthetic_workspace.root)

    assert failure.value.payload.code == "uv_lock_failed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_initialization_happens_before_publication(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """A published workspace already has its repository, so init is never half-done."""

    target = tmp_path / "PolishLinguaWiki"

    report = workspace_service.initialize(_options(target, backup_root, git_init=True), clock=clock)

    assert report.git_initialized is True
    assert (target / ".git").is_dir()
    assert workspace_service.git_remotes(target) == {}
    assert workspace_service.doctor(target, clock=clock).ok is True
    log = subprocess.run(["git", "log"], cwd=target, capture_output=True, text=True, check=False)
    assert log.returncode != 0
