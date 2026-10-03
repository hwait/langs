"""Shared fixtures: controllable clocks and initialized synthetic workspaces."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from linguawiki.db import migrations as migration_module
from linguawiki.db import schema as schema_module
from linguawiki.paths import WORKSPACE_CONFIG_NAME, WorkspacePaths, workspace_paths
from linguawiki.services import workspace as workspace_service
from tests.support.clocks import AdvancingClock


@dataclass(frozen=True, slots=True)
class SyntheticWorkspace:
    """One initialized learner workspace plus the roots the test may write to."""

    paths: WorkspacePaths
    backup_root: Path
    clock: AdvancingClock
    report: workspace_service.WorkspaceInitReport

    @property
    def root(self) -> Path:
        return self.paths.root


@pytest.fixture(autouse=True)
def _isolated_migration_registry() -> Iterator[None]:
    """Keep every migration-derived cache from leaking between tests.

    `expected_schema` is derived from the migrations, so a test that patches the
    migration directory must not leave a schema for a version the real release
    does not have.
    """

    def _clear() -> None:
        migration_module.migrations.cache_clear()
        schema_module.expected_schema.cache_clear()

    _clear()
    yield
    _clear()


@pytest.fixture
def clock() -> AdvancingClock:
    return AdvancingClock()


@pytest.fixture
def backup_root(tmp_path: Path) -> Path:
    """A backup root outside every learner repository created by a test."""

    return tmp_path / "backups"


@pytest.fixture
def workspace_target(tmp_path: Path) -> Path:
    return tmp_path / "repositories" / "PolishLinguaWiki"


#: The dogfood pack that ships with the core while the pack contract stabilizes.
PILOT_PACK = Path(__file__).resolve().parents[1] / "language-packs" / "pl-pilot"
FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "language-packs" / "fixtures"

#: The version `pl-pilot` currently ships, and one strictly after it.
#:
#: Both are derived rather than written down. A published pack version is immutable, so
#: every test that republishes the pilot to exercise `pack update` needs a version the
#: pack does not already have -- and each of the ~20 that hard-coded `"0.2.0"` turned
#: into a `pack_version_conflict` the moment the pilot was actually published as 0.2.0.
PILOT_VERSION: str = json.loads((PILOT_PACK / "manifest.json").read_text(encoding="utf-8"))[
    "version"
]


def _next_minor(version: str) -> str:
    major, minor, _patch = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


NEXT_PILOT_VERSION: str = _next_minor(PILOT_VERSION)


@dataclass(frozen=True, slots=True)
class PolishWorkspace:
    """A workspace with the Polish pilot pack installed, one learner, and one A2 track."""

    paths: WorkspacePaths
    backup_root: Path
    clock: AdvancingClock
    user_id: str
    track_id: str

    @property
    def root(self) -> Path:
        return self.paths.root


class _CountingClock:
    """An `AdvancingClock` that remembers how many times it was read.

    The count is what makes the template honest: a per-test clock fast-forwarded by the
    same number of reads is left exactly where a real build would have left it, so rows
    written after the copy are dated after the pack, not before it.
    """

    def __init__(self) -> None:
        self._inner = AdvancingClock()
        self.reads = 0

    def now(self) -> datetime:
        self.reads += 1
        return self._inner.now()


@dataclass(frozen=True, slots=True)
class PilotTemplate:
    """One pilot workspace, built once, for every test that needs one to be copied."""

    root: Path
    backup_root: Path
    report: workspace_service.WorkspaceInitReport
    clock_reads: int


def materialize_pilot(
    template: PilotTemplate, *, target: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """Put a copy of the template where a real install would have put a workspace.

    Installing the pilot pack is 2.25 seconds of DuckDB writes and nearly three hundred
    tests need it, so it is done once and copied. `tests/workspaces/test_template_fixture.py`
    holds this to what a real install produces -- table by table, through `db check`, and
    on both the things a copy gets wrong: the manifest's absolute backup root, and the
    clock, which must be advanced past the timestamps the copied rows already carry.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template.root, target)
    if template.backup_root.exists():
        shutil.copytree(template.backup_root, backup_root, dirs_exist_ok=True)
    configuration = target / WORKSPACE_CONFIG_NAME
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            f'backup_root = "{template.backup_root}"', f'backup_root = "{backup_root}"'
        ),
        encoding="utf-8",
    )
    for _ in range(template.clock_reads):
        clock.now()


@pytest.fixture(scope="session")
def pilot_template(tmp_path_factory: pytest.TempPathFactory) -> PilotTemplate:
    base = tmp_path_factory.mktemp("pilot-template")
    root = base / "repositories" / "PolishLinguaWiki"
    backup_root = base / "backups"
    clock = _CountingClock()
    report = workspace_service.initialize(
        workspace_service.InitOptions(
            path=root,
            backup_root=backup_root,
            name="Polish LinguaWiki",
            timezone="Europe/Warsaw",
        ),
        clock=clock,  # type: ignore[arg-type]
    )
    from linguawiki.services import packs as pack_service

    pack_service.install(workspace_paths(root), PILOT_PACK, clock=clock)  # type: ignore[arg-type]
    return PilotTemplate(root=root, backup_root=backup_root, report=report, clock_reads=clock.reads)


@pytest.fixture
def synthetic_workspace(
    workspace_target: Path, backup_root: Path, clock: AdvancingClock
) -> Iterator[SyntheticWorkspace]:
    report = workspace_service.initialize(
        workspace_service.InitOptions(
            path=workspace_target,
            backup_root=backup_root,
            name="Polish LinguaWiki",
            timezone="Europe/Warsaw",
        ),
        clock=clock,
    )
    yield SyntheticWorkspace(
        paths=workspace_paths(workspace_target),
        backup_root=backup_root,
        clock=clock,
        report=report,
    )


@pytest.fixture
def installed_pilot(
    pilot_template: PilotTemplate,
    workspace_target: Path,
    backup_root: Path,
    clock: AdvancingClock,
) -> SyntheticWorkspace:
    """The Polish pilot pack installed into an otherwise empty workspace.

    Copied from the session template rather than installed again. The report is the
    template's, which is the one thing here that is not this test's own -- nothing reads
    it, and `report.workspace_id` is the identity of the database that was copied.
    """

    materialize_pilot(pilot_template, target=workspace_target, backup_root=backup_root, clock=clock)
    return SyntheticWorkspace(
        paths=workspace_paths(workspace_target),
        backup_root=backup_root,
        clock=clock,
        report=pilot_template.report,
    )


@pytest.fixture
def polish_workspace(installed_pilot: SyntheticWorkspace) -> PolishWorkspace:
    """One learner with a declared-A2 Polish track, ready for onboarding."""

    return polish_learner(installed_pilot)


def polish_learner(installed_pilot: SyntheticWorkspace) -> PolishWorkspace:
    """Add the `polish_workspace` learner and track to an installed pilot workspace.

    A function as well as the fixture, for a test that needs two identical workspaces to
    compare -- one built from `installed_pilot`, the other from `materialize_pilot`.
    """

    from linguawiki.services import learners as learner_service

    user = learner_service.create_user(
        installed_pilot.paths,
        display_name="Синтетический Учащийся",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        support_languages=["en"],
        clock=installed_pilot.clock,
    )
    track = learner_service.create_track(
        installed_pilot.paths,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        target_level="B1",
        goal="Rozmawiać po polsku w pracy",
        preferences=learner_service.TrackPreferences(
            goals=("work conversation",),
            interests=("podróże", "praca i biuro"),
            weekly_minutes=210,
            session_minutes=60,
            sessions_per_week=4,
            correction_mode="accuracy",
            voice_available=True,
            transcript_retention_consent=True,
        ),
        clock=installed_pilot.clock,
    )
    return PolishWorkspace(
        paths=installed_pilot.paths,
        backup_root=installed_pilot.backup_root,
        clock=installed_pilot.clock,
        user_id=user.user_id,
        track_id=track.track_id,
    )
