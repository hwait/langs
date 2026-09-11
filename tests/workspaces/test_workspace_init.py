from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from linguawiki import __version__
from linguawiki.contract_validation import validate_json_contract
from linguawiki.contracts import LockManifest, WorkspaceManifest
from linguawiki.db import migrations as migration_module
from linguawiki.errors import LinguaWikiError
from linguawiki.paths import PRIVATE_DIRECTORIES, workspace_paths
from linguawiki.resources import schema_directory, workspace_template_directory
from linguawiki.services import workspace as workspace_service
from linguawiki.workspace_template import render_workspace_contracts
from tests.conftest import SyntheticWorkspace
from tests.support.clocks import AdvancingClock

CORE_ROOT = Path(__file__).resolve().parents[2]


def _options(path: Path, backup_root: Path, **overrides: object) -> workspace_service.InitOptions:
    arguments: dict[str, object] = {
        "path": path,
        "backup_root": backup_root,
        "name": "Polish LinguaWiki",
        "timezone": "Europe/Warsaw",
    }
    arguments.update(overrides)
    return workspace_service.InitOptions(**arguments)  # type: ignore[arg-type]


def test_init_creates_the_full_workspace_layout(synthetic_workspace: SyntheticWorkspace) -> None:
    root = synthetic_workspace.root
    report = synthetic_workspace.report

    assert report.created is True
    assert report.history_policy == "git-wiki"
    for relative in (
        "AGENTS.md",
        ".gitignore",
        "linguawiki.toml",
        "pyproject.toml",
        "linguawiki.lock",
        "wiki/index.md",
        ".agents/skills/linguawiki/SKILL.md",
    ):
        assert (root / relative).is_file(), relative
    for relative in PRIVATE_DIRECTORIES:
        assert (root / relative).is_dir(), relative
    assert report.database_schema_version == migration_module.head_version()
    assert (root / "data" / "linguawiki.duckdb").is_file()


def test_a_generated_workspace_holds_no_copy_of_core_source(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    root = synthetic_workspace.root

    assert not (root / "src").exists()
    assert not (root / "src" / "linguawiki").exists()
    assert list(root.rglob("contracts.py")) == []
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == [f"linguawiki=={__version__}"]


def test_generated_configuration_and_lock_validate_against_the_v1_contracts(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    root = synthetic_workspace.root
    configuration = tomllib.loads((root / "linguawiki.toml").read_text(encoding="utf-8"))
    lock = json.loads((root / "linguawiki.lock").read_text(encoding="utf-8"))

    validate_json_contract(
        "lingua.workspace.v1",
        WorkspaceManifest.model_validate(configuration).model_dump(mode="json"),
        schema_directory=schema_directory(),
    )
    validate_json_contract("linguawiki.lock.v1", lock, schema_directory=schema_directory())


def test_the_written_lock_matches_the_versioned_template(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The lock template and the model-written lock must never drift apart."""

    paths = synthetic_workspace.paths
    configuration = workspace_service.load_configuration(paths)
    lock = workspace_service.load_lock(paths)
    context = workspace_service.template_context(
        name=synthetic_workspace.report.name, configuration=configuration, lock=lock
    )
    rendered = render_workspace_contracts(workspace_template_directory(), context)

    assert rendered.lock == lock
    assert rendered.configuration == configuration


def test_the_lock_pins_the_installed_core_schema_and_skill_bundle(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    lock = workspace_service.load_lock(synthetic_workspace.paths)

    assert lock == LockManifest(
        core=lock.core,
        database_schema=migration_module.database_schema_pin(),
        skill_bundle=lock.skill_bundle,
    )
    assert lock.core.version == __version__
    assert lock.packs == ()


def test_the_lock_is_written_atomically_without_leaving_temporary_files(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    assert not (synthetic_workspace.root / "linguawiki.lock.tmp").exists()


def test_reinitializing_an_untouched_workspace_is_idempotent(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    before = (synthetic_workspace.root / "linguawiki.lock").read_bytes()

    report = workspace_service.initialize(
        _options(synthetic_workspace.root, synthetic_workspace.backup_root),
        clock=synthetic_workspace.clock,
    )

    assert report.created is False
    assert report.workspace_id == synthetic_workspace.report.workspace_id
    assert "already initialized" in report.warnings[-1]
    assert (synthetic_workspace.root / "linguawiki.lock").read_bytes() == before


@pytest.mark.parametrize("relative", ["AGENTS.md", ".gitignore", "pyproject.toml"])
def test_reinitializing_a_hand_edited_workspace_is_refused(
    relative: str, synthetic_workspace: SyntheticWorkspace
) -> None:
    edited = synthetic_workspace.root / relative
    edited.write_text(edited.read_text(encoding="utf-8") + "\n# hand edit\n", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            _options(synthetic_workspace.root, synthetic_workspace.backup_root),
            clock=synthetic_workspace.clock,
        )

    assert failure.value.payload.code == "workspace_modified"
    assert failure.value.payload.details[0].field == relative


def test_reinitializing_after_editing_a_generated_skill_is_refused(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    skill = synthetic_workspace.root / ".agents" / "skills" / "linguawiki" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            _options(synthetic_workspace.root, synthetic_workspace.backup_root),
            clock=synthetic_workspace.clock,
        )

    assert failure.value.payload.code == "workspace_modified"


def test_reinitializing_with_a_different_backup_root_is_refused(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            _options(synthetic_workspace.root, tmp_path / "other-backups"),
            clock=synthetic_workspace.clock,
        )

    assert failure.value.payload.code == "workspace_already_initialized"
    assert failure.value.payload.details[0].field == "backup_root"


def test_reinitializing_with_a_different_name_is_refused(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            _options(
                synthetic_workspace.root, synthetic_workspace.backup_root, name="Other Workspace"
            ),
            clock=synthetic_workspace.clock,
        )

    assert failure.value.payload.code == "workspace_already_initialized"
    assert failure.value.payload.details[0].field == "name"


def test_a_non_empty_target_is_refused(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    target = tmp_path / "OccupiedDirectory"
    target.mkdir()
    (target / "notes.md").write_text("existing learner notes", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(_options(target, backup_root), clock=clock)

    assert failure.value.payload.code == "target_not_empty"
    assert (target / "notes.md").read_text(encoding="utf-8") == "existing learner notes"


def test_a_workspace_inside_the_core_repository_is_refused(
    backup_root: Path, clock: AdvancingClock
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            _options(CORE_ROOT / "PolishLinguaWiki", backup_root), clock=clock
        )

    assert failure.value.payload.code == "workspace_inside_core_repository"
    assert failure.value.payload.details[0].context["core_repository"] == str(CORE_ROOT)
    assert not (CORE_ROOT / "PolishLinguaWiki").exists()


def test_a_backup_root_inside_the_workspace_is_refused(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    target = tmp_path / "PolishLinguaWiki"

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(_options(target, target / "backups"), clock=clock)

    assert failure.value.payload.code == "backup_root_invalid"


@pytest.mark.parametrize(
    "policy", ["portable-snapshot", "git-portable-snapshot", "local-only", "invented"]
)
def test_history_policies_outside_the_frozen_contract_are_refused(
    policy: str, tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            _options(tmp_path / "PolishLinguaWiki", backup_root, history_policy=policy),
            clock=clock,
        )

    assert failure.value.payload.code == "unsupported_history_policy"


def test_a_workspace_name_without_alphanumerics_is_refused(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            _options(tmp_path / "PolishLinguaWiki", backup_root, name="---"), clock=clock
        )

    assert failure.value.payload.code == "invalid_workspace_name"


def test_the_workspace_name_defaults_to_the_directory_name(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    report = workspace_service.initialize(
        workspace_service.InitOptions(path=tmp_path / "ChineseLinguaWiki", backup_root=backup_root),
        clock=clock,
    )

    assert report.name == "ChineseLinguaWiki"
    project = tomllib.loads(
        (tmp_path / "ChineseLinguaWiki" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert project["project"]["name"] == "chineselinguawiki"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_optional_git_init_creates_no_commit_and_no_remote(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    target = tmp_path / "PolishLinguaWiki"

    report = workspace_service.initialize(_options(target, backup_root, git_init=True), clock=clock)

    assert report.git_initialized is True
    assert (target / ".git").is_dir()
    assert workspace_service.git_remotes(target) == {}
    log = subprocess.run(["git", "log"], cwd=target, capture_output=True, text=True, check=False)
    assert log.returncode != 0


def test_a_second_git_init_on_an_existing_repository_is_a_no_op(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    (synthetic_workspace.root / ".git").mkdir()

    assert workspace_service._git_init(synthetic_workspace.paths) is True


def test_the_initial_wiki_projection_is_generated_and_empty(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    index = (synthetic_workspace.root / "wiki" / "index.md").read_text(encoding="utf-8")

    assert index.startswith("<!-- generated by linguawiki")
    assert "Polish LinguaWiki" in index
    assert synthetic_workspace.report.wiki_files == (
        "wiki/index.md",
        "wiki/languages/.gitkeep",
        "wiki/learner/.gitkeep",
        "wiki/reports/.gitkeep",
        "wiki/sessions/.gitkeep",
    )


def test_workspace_identity_is_mirrored_in_the_database(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    status = workspace_service.status(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert status.workspace_id == synthetic_workspace.report.workspace_id
    assert status.name == "Polish LinguaWiki"
    assert status.normalized_name == "polish-linguawiki"
    assert status.track_policy == "single"
    assert status.users == 0
    assert status.tracks == 0
    assert status.applied_schema_version == status.packaged_schema_version
    assert status.row_counts["workspaces"] == 1
    assert status.row_counts["workspace_versions"] == 3
    assert status.row_counts["domain_events"] == 1
    assert status.row_counts["audit_log"] == 1


def test_status_requires_an_initialized_workspace(tmp_path: Path) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.status(tmp_path)

    assert failure.value.payload.code == "workspace_not_initialized"


def test_status_reports_a_missing_database(synthetic_workspace: SyntheticWorkspace) -> None:
    (synthetic_workspace.root / "data" / "linguawiki.duckdb").unlink()

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.status(synthetic_workspace.root)

    assert failure.value.payload.code == "database_not_found"


def test_two_workspaces_are_independent(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    first = workspace_service.initialize(
        _options(tmp_path / "PolishLinguaWiki", backup_root, name="Polish LinguaWiki"), clock=clock
    )
    second = workspace_service.initialize(
        _options(tmp_path / "ChineseLinguaWiki", backup_root, name="Chinese LinguaWiki"),
        clock=clock,
    )

    assert first.workspace_id != second.workspace_id
    assert workspace_paths(tmp_path / "PolishLinguaWiki").database.is_file()
    assert workspace_paths(tmp_path / "ChineseLinguaWiki").database.is_file()
