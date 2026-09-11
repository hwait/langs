"""Shared fixtures: controllable clocks and initialized synthetic workspaces."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from linguawiki.db import migrations as migration_module
from linguawiki.db import schema as schema_module
from linguawiki.paths import WorkspacePaths, workspace_paths
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
def installed_pilot(synthetic_workspace: SyntheticWorkspace) -> SyntheticWorkspace:
    """The Polish pilot pack installed into an otherwise empty workspace."""

    from linguawiki.services import packs as pack_service

    pack_service.install(synthetic_workspace.paths, PILOT_PACK, clock=synthetic_workspace.clock)
    return synthetic_workspace


@pytest.fixture
def polish_workspace(installed_pilot: SyntheticWorkspace) -> PolishWorkspace:
    """One learner with a declared-A2 Polish track, ready for onboarding."""

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
