"""The templated pilot workspace must be the workspace a real install produces.

`installed_pilot` and `polish_workspace` copy a session-built template instead of running
`pack_service.install` for every test, because that install is 2.25 seconds of DuckDB
writes repeated identically nearly three hundred times. A fixture that is *almost* the
real thing is worse than a slow one -- every test in the suite would be asserting against
a workspace production never builds -- so the shortcut is only admissible with this file
holding it to the original.
"""

from __future__ import annotations

from pathlib import Path

from linguawiki.db.backup import table_row_counts
from linguawiki.db.connection import open_reader
from linguawiki.db.integrity import check_database
from linguawiki.paths import workspace_paths
from linguawiki.services import packs as pack_service
from linguawiki.services import workspace as workspace_service
from tests.conftest import PILOT_PACK, PilotTemplate, materialize_pilot
from tests.support.clocks import AdvancingClock


def _built_the_long_way(base: Path) -> Path:
    clock = AdvancingClock()
    root = base / "repositories" / "PolishLinguaWiki"
    workspace_service.initialize(
        workspace_service.InitOptions(
            path=root,
            backup_root=base / "backups",
            name="Polish LinguaWiki",
            timezone="Europe/Warsaw",
        ),
        clock=clock,
    )
    pack_service.install(workspace_paths(root), PILOT_PACK, clock=clock)
    return root


def _tables(root: Path) -> dict[str, int]:
    with open_reader(workspace_paths(root)) as database:
        return dict(table_row_counts(database))


def test_a_materialized_workspace_holds_what_a_real_install_holds(
    pilot_template: PilotTemplate, tmp_path: Path
) -> None:
    real = _built_the_long_way(tmp_path / "real")
    copied_root = tmp_path / "copied" / "repositories" / "PolishLinguaWiki"
    materialize_pilot(
        pilot_template,
        target=copied_root,
        backup_root=tmp_path / "copied" / "backups",
        clock=AdvancingClock(),
    )

    assert _tables(copied_root) == _tables(real)


def test_a_materialized_workspace_passes_every_integrity_check(
    pilot_template: PilotTemplate, tmp_path: Path
) -> None:
    root = tmp_path / "repositories" / "PolishLinguaWiki"
    materialize_pilot(
        pilot_template, target=root, backup_root=tmp_path / "backups", clock=AdvancingClock()
    )

    with open_reader(workspace_paths(root)) as database:
        report = check_database(database)

    assert report.ok, [check for check in report.checks if check.status == "failed"]


def test_a_materialized_workspace_records_the_backup_root_it_was_given(
    pilot_template: PilotTemplate, tmp_path: Path
) -> None:
    """The manifest names an absolute path, and a copy that kept the template's would
    refuse every later `workspace init` against the real one."""

    root = tmp_path / "repositories" / "PolishLinguaWiki"
    backups = tmp_path / "backups"
    materialize_pilot(pilot_template, target=root, backup_root=backups, clock=AdvancingClock())

    configuration = workspace_service.load_configuration(workspace_paths(root))

    assert configuration.backup_root == str(backups)
    assert str(pilot_template.backup_root) not in configuration.backup_root


def test_the_clock_is_left_where_a_real_build_would_have_left_it(
    pilot_template: PilotTemplate, tmp_path: Path
) -> None:
    """Rows carry the template's timestamps, so a test writing next must come after them.

    Without the fast-forward the per-test clock restarts at the epoch and every later
    write is dated *before* the pack it depends on -- which silently rewrites exactly the
    chronologies the learner model derives its state from.
    """

    clock = AdvancingClock()
    materialize_pilot(
        pilot_template,
        target=tmp_path / "repositories" / "PolishLinguaWiki",
        backup_root=tmp_path / "backups",
        clock=clock,
    )
    root = tmp_path / "repositories" / "PolishLinguaWiki"

    with open_reader(workspace_paths(root)) as database:
        latest = database.scalar("SELECT max(created_at) FROM content_records")

    assert clock.now().replace(tzinfo=None) > latest
