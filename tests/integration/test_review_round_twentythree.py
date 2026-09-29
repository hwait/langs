"""Regressions for the four defects round twenty-three found in Stage 5.

Two are about a mismatch of scope: an artifact *row* belongs to a track, but the *file* it
names belongs to the workspace, and treating the file as track-scoped let one learner's
privacy decision delete another learner's recording. One is about a predicate that was half
a check. The last is reporting: a tombstone stopped being counted as purged if its old path
had become strange, and the CLI printed counts where an operator needed identifiers.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import database as database_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import privacy as privacy_service
from tests.conftest import PolishWorkspace

ROOT = Path(__file__).resolve().parents[2]


def write(root: Path, name: str, body: bytes) -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return f"imports/{name}", hashlib.sha256(body).hexdigest()


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    learner_service.update_track(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        preferences=learner_service.TrackPreferences(audio_retention_consent=True),
        clock=polish_workspace.clock,
    )
    return polish_workspace


def _second_track(running: PolishWorkspace) -> str:
    other = learner_service.create_user(
        running.paths,
        display_name="Drugi Uczeń",
        timezone="Europe/Warsaw",
        native_languages=["en"],
        clock=running.clock,
    )
    track = learner_service.create_track(
        running.paths,
        user=other.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A1",
        preferences=learner_service.TrackPreferences(audio_retention_consent=True),
        clock=running.clock,
    )
    return track.track_id


# --- 1. One file, one owner ----------------------------------------------------------


def test_two_tracks_cannot_register_the_same_file(running: PolishWorkspace) -> None:
    """Either learner's purge would have deleted the other's recording."""

    other = _second_track(running)
    relative, digest = write(running.root, "shared.opus", b"OggS one file" + b"\x00" * 20)
    mine = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path=relative,
            kind="audio",
            expected_sha256=digest,
            track=other,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_path_owned_elsewhere"
    assert mine.artifact_id in str(failure.value)
    assert (running.root / relative).is_file()


def test_db_check_finds_a_file_two_live_artifacts_claim(running: PolishWorkspace) -> None:
    """The rule refuses new collisions; a restore can still present an old one."""

    other = _second_track(running)
    mine_relative, mine_digest = write(running.root, "mine.opus", b"OggS mine" + b"\x00" * 20)
    artifact_service.register(
        running.paths,
        relative_path=mine_relative,
        kind="audio",
        expected_sha256=mine_digest,
        track=running.track_id,
        clock=running.clock,
    )
    theirs_relative, theirs_digest = write(
        running.root, "theirs.opus", b"OggS theirs" + b"\x00" * 20
    )
    theirs = artifact_service.register(
        running.paths,
        relative_path=theirs_relative,
        kind="audio",
        expected_sha256=theirs_digest,
        track=other,
        clock=running.clock,
    )
    with (
        open_writer(running.paths, command="test", clock=running.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE artifacts SET relative_path = ? WHERE artifact_id = ?",
            [mine_relative, theirs.artifact_id],
        )
    report = database_service.check(running.paths, clock=running.clock)
    assert not report.ok
    assert "artifact_path_ownership" in {check.name for check in report.failures}


def test_one_learner_may_still_re_register_their_own_path(running: PolishWorkspace) -> None:
    """Workspace-wide ownership must not break the same track's own recovery."""

    relative, digest = write(running.root, "call.opus", b"OggS mine" + b"\x00" * 20)
    first = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    again = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    assert again.artifact_id == first.artifact_id


# --- 2. Ownership excuses a path only for the bytes it owns -------------------------


def test_the_purged_recording_returning_over_a_new_one_is_reported(
    running: PolishWorkspace,
) -> None:
    """A live row owning the path suppressed the warning whatever was actually there."""

    relative, digest = write(running.root, "session.opus", b"OggS purged" + b"\x00" * 20)
    purged = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    artifact_service.purge(
        running.paths,
        artifact=purged.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    fresh, fresh_digest = write(
        running.root, "session.opus", b"OggS a new conversation" + b"\x00" * 20
    )
    artifact_service.register(
        running.paths,
        relative_path=fresh,
        kind="audio",
        expected_sha256=fresh_digest,
        track=running.track_id,
        clock=running.clock,
    )
    # Clean while the live row's own recording is there.
    assert (
        privacy_service.audit(
            running.paths, track=running.track_id, clock=running.clock
        ).retention.unkept_files_still_present
        == ()
    )

    # The purged recording restored over it: the path is owned, and what is on it is not
    # the owner's.
    (running.root / relative).write_bytes(b"OggS purged" + b"\x00" * 20)
    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.retention.unkept_files_still_present == (purged.artifact_id,)
    assert not audited.ok
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not verified.ok, "and the two agree"


# --- 3. Resolution itself can raise --------------------------------------------------


def _looping(running: PolishWorkspace, relative: str) -> None:
    """Point a registered path at a symlink loop, which `resolve` raises on in 3.12."""

    (running.root / relative).unlink()
    (running.root / "imports" / "loop-a").symlink_to("loop-b")
    (running.root / "imports" / "loop-b").symlink_to("loop-a")
    (running.root / relative).symlink_to("loop-a")


def test_a_symlink_loop_at_a_registered_path_is_reported_not_raised(
    running: PolishWorkspace,
) -> None:
    relative, digest = write(running.root, "call.opus", b"OggS a recording" + b"\x00" * 20)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    _looping(running, relative)

    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert not verified.ok
    assert verified.unreadable == (registered.artifact_id,)

    listing = artifact_service.listing(running.paths, track=running.track_id, clock=running.clock)
    assert [entry.present for entry in listing.artifacts] == [False]

    audited = privacy_service.audit(running.paths, track=running.track_id, clock=running.clock)
    assert audited.retention.artifacts_held == 1


def test_registering_through_a_symlink_loop_is_refused_by_name(
    running: PolishWorkspace,
) -> None:
    (running.root / "imports").mkdir(parents=True, exist_ok=True)
    (running.root / "imports" / "loop-a").symlink_to("loop-b")
    (running.root / "imports" / "loop-b").symlink_to("loop-a")
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path="imports/loop-a",
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_unreadable"


# --- 4. Verification reports orthogonal facts, and names them ----------------------


def test_a_tombstone_is_counted_as_purged_whatever_its_old_path_became(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    relative, digest = write(running.root, "gone.opus", b"OggS purgeable" + b"\x00" * 20)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    artifact_service.purge(
        running.paths,
        artifact=registered.artifact_id,
        reason="learner-request",
        track=running.track_id,
        clock=running.clock,
    )
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "x.opus").write_bytes(b"OggS elsewhere")
    (running.root / relative).symlink_to(outside / "x.opus")

    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.purged == (registered.artifact_id,), "it is still a tombstone"
    assert verified.escaped == (registered.artifact_id,), "and its path has become strange"


def test_the_cli_names_escaped_and_unreadable_artifacts(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    """A count in a warning says something is wrong, not which file to go and look at."""

    relative, digest = write(running.root, "call.opus", b"OggS a recording" + b"\x00" * 20)
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "x.opus").write_bytes(b"OggS elsewhere")
    (running.root / relative).unlink()
    (running.root / relative).symlink_to(outside / "x.opus")

    result = subprocess.run(
        [
            str(ROOT / ".tools" / "uv"),
            "run",
            "linguawiki",
            "artifact",
            "verify",
            "--workspace",
            str(running.root),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert registered.artifact_id in result.stdout, result.stdout + result.stderr
    assert "escaped:" in result.stdout

    structured = subprocess.run(
        [
            str(ROOT / ".tools" / "uv"),
            "run",
            "linguawiki",
            "artifact",
            "verify",
            "--workspace",
            str(running.root),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    payload = json.loads(structured.stdout)
    assert payload["data"]["escaped"] == [registered.artifact_id]
