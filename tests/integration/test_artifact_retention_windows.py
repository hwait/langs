"""The retention window's arithmetic, and what happens when a deletion will not go.

`sweep` is the only command that deletes a learner's recordings without being asked about
a specific one, so the branch that decides *whether today is the day* deserves a test of
its own rather than being reached incidentally. Three of the last four review rounds found
defects in this module, and the two that would have destroyed a recording were both in a
path that nothing exercised directly.

The deletion-failure case pins the ordering argument written into `purge`: a record saying
a recording is gone while the recording is on disk is a privacy claim that is not true, so
a failed deletion records nothing at all.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from tests.conftest import PolishWorkspace


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    return polish_workspace


def keep_a_recording(
    workspace: PolishWorkspace, name: str, *, external_id: str | None = None
) -> str:
    """A retained recording on disk, and the artifact id that owns it."""

    path = workspace.root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    registered = artifact_service.register(
        workspace.paths,
        relative_path=f"imports/{name}",
        kind="audio",
        retained=True,
        external_id=external_id,
        expected_sha256=hashlib.sha256(name.encode()).hexdigest(),
        track=workspace.track_id,
        clock=workspace.clock,
    )
    artifact_id: str = registered.artifact_id
    return artifact_id


def set_policy(workspace: PolishWorkspace, policy: str, *, days: int | None = None) -> None:
    learner_service.update_track(
        workspace.paths,
        track=workspace.track_id,
        preferences=learner_service.TrackPreferences(
            audio_retention_policy=policy,
            audio_retention_days=days,
        ),
        clock=workspace.clock,
    )


# --- The window --------------------------------------------------------------------------


def test_the_default_policy_sweeps_nothing(running: PolishWorkspace) -> None:
    """`keep` is the default, and the default must not delete anything.

    Worth a test precisely because it asserts an absence: a policy string that stopped
    matching would turn the no-op default into a sweep, and no other test would notice.
    """

    artifact_id = keep_a_recording(running, "kept.opus")
    swept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert swept.policy == "keep"
    assert swept.purged == ()
    assert swept.considered == 1, "the recording was considered and deliberately kept"
    assert (running.root / "imports" / "kept.opus").is_file()
    assert artifact_id


def test_a_recording_inside_the_rolling_window_is_kept_and_one_past_it_is_not(
    running: PolishWorkspace,
) -> None:
    """The comparison itself: `age < retention_days` keeps, and the boundary is inclusive.

    Both sides in one test because the arithmetic is the thing under test, and a window
    that kept everything or swept everything would pass a test of either half alone.
    """

    set_policy(running, "rolling-days", days=30)
    old = keep_a_recording(running, "old.opus")
    running.clock.advance(timedelta(days=40))
    recent = keep_a_recording(running, "recent.opus")

    swept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert swept.policy == "rolling-days"
    assert swept.retention_days == 30
    assert swept.purged == (old,), "the recording past the window should have gone"
    assert recent not in swept.purged, "a recording inside the window was deleted"
    assert not (running.root / "imports" / "old.opus").exists()
    assert (running.root / "imports" / "recent.opus").is_file()


def test_a_dry_run_reports_the_same_verdict_and_deletes_nothing(
    running: PolishWorkspace,
) -> None:
    """The point of `--dry-run` on a destructive command: identical answer, no deletion.

    If the dry run and the real sweep could disagree, the preview would be worthless
    exactly when it matters -- this is the same "the preflight is not the writer" shape
    that produced several of this stage's defects.
    """

    set_policy(running, "rolling-days", days=7)
    old = keep_a_recording(running, "preview.opus")
    running.clock.advance(timedelta(days=10))

    preview = artifact_service.sweep(
        running.paths, dry_run=True, track=running.track_id, clock=running.clock
    )
    assert preview.dry_run
    assert preview.purged == (old,)
    assert (running.root / "imports" / "preview.opus").is_file(), "a dry run deleted a file"

    real = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert real.purged == preview.purged, "the preview and the sweep disagreed"
    assert not (running.root / "imports" / "preview.opus").exists()


def test_delete_after_ingestion_leaves_the_learners_own_recordings_alone(
    running: PolishWorkspace,
) -> None:
    """This policy is about what arrived with an ingest, and nothing else.

    A recording with no producer identifier was never brought in by a package, so it is
    not what the learner decided about. Sweeping it too would be a different decision made
    on their behalf.
    """

    set_policy(running, "delete-after-ingestion")
    ingested = keep_a_recording(running, "from-package.opus", external_id="call-a/utt-1")
    own = keep_a_recording(running, "my-own.opus")

    swept = artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert swept.purged == (ingested,)
    assert own not in swept.purged
    assert not (running.root / "imports" / "from-package.opus").exists()
    assert (running.root / "imports" / "my-own.opus").is_file(), "a learner's own recording went"


# --- When the bytes will not go -----------------------------------------------------------


def test_a_purge_whose_deletion_fails_records_nothing_at_all(
    running: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering argument in `purge`, asserted rather than only commented.

    The file is deleted before the commit so that a failure can only ever leave the
    weaker wreckage: the file still present and *no* tombstone. The opposite order would
    leave a tombstone telling the learner their recording was deleted while it sat on
    disk, which is the one outcome that is a lie.
    """

    artifact_id = keep_a_recording(running, "stubborn.opus")
    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "stubborn.opus":
            raise OSError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.purge(
            running.paths,
            artifact=artifact_id,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_file_not_removed"
    monkeypatch.undo()

    assert (running.root / "imports" / "stubborn.opus").is_file()
    with open_reader(running.paths, clock=running.clock) as database:
        row = database.one(
            "SELECT purged_at, purge_reason FROM artifacts WHERE artifact_id = ?",
            [artifact_id],
        )
    assert row is not None
    assert row[0] is None, "a tombstone claims a deletion that did not happen"
    assert row[1] is None


def test_a_sweep_whose_deletion_fails_keeps_the_row_honest(
    running: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule reached through the sweep, which deletes several files in one go.

    A sweep is the case where a partial failure is most likely, and the guarantee has to
    be the same: whatever it managed to delete, no row says "purged" about a file that is
    still there.
    """

    set_policy(running, "delete-after-ingestion")
    first = keep_a_recording(running, "goes.opus", external_id="call-a/utt-1")
    stuck = keep_a_recording(running, "stays.opus", external_id="call-a/utt-2")
    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "stays.opus":
            raise OSError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.sweep(running.paths, track=running.track_id, clock=running.clock)
    assert failure.value.payload.code == "artifact_file_not_removed"
    monkeypatch.undo()

    assert (running.root / "imports" / "stays.opus").is_file()
    # The invariant, asserted over every row rather than over the two this test made: a
    # tombstone may only exist for a file that is actually gone. Checking it this way does
    # not depend on which recording the sweep reached first.
    with open_reader(running.paths, clock=running.clock) as database:
        tombstoned = {
            str(relative_path)
            for (relative_path,) in database.query(
                "SELECT relative_path FROM artifacts WHERE purged_at IS NOT NULL"
            )
        }
    still_there = sorted(
        relative_path for relative_path in tombstoned if (running.root / relative_path).exists()
    )
    assert still_there == [], f"tombstoned but still on disk: {still_there}"
    assert stuck and first, "both recordings were registered"
