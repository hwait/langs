"""Regressions for the three defects round twenty-four found in Stage 5.

The first two are both consequences of the previous round's fix, in opposite directions: path
ownership became workspace-wide in the writer and stayed track-scoped in the preflight, and
it became "live and retained" in the readers while the writer still counted every non-purged
row. `path_owners` is now the one place that answers "who owns this file", so the next change
to the rule reaches every caller.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from linguawiki.services import speaking as speaking_service
from tests.conftest import PolishWorkspace

ROOT = Path(__file__).resolve().parents[2]
ART = "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TWIN = "art_01ARZ3NDEKTSV4RRFFQ69G5FB9"


def write(root: Path, name: str, body: bytes) -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return f"imports/{name}", hashlib.sha256(body).hexdigest()


def package(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_a",
        "external_session_id": "call-a",
        "target_language": "pl",
        "mode": "completed",
        "started_at": "2026-01-01T10:00:00Z",
        "ended_at": "2026-01-01T10:20:00Z",
        "transcript_layers": [
            {
                "kind": "raw",
                "derived_from": None,
                "utterances": [
                    {
                        "utterance_id": "utt_001",
                        "speaker": "learner",
                        "started_at": "2026-01-01T10:01:00Z",
                        "ended_at": "2026-01-01T10:01:06Z",
                        "text": "chcialbym kupic bilet do Krakowa na jutro",
                    }
                ],
            }
        ],
        "events": [],
        "artifacts": [],
    }
    payload.update(overrides)
    return payload


def entry(relative: str, digest: str, *, artifact_id: str = ART) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": "audio",
        "relative_path": relative,
        "sha256": digest,
        "retained": True,
    }


def artifact_count(workspace: PolishWorkspace) -> int:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return int(database.scalar("SELECT count(*) FROM artifacts"))


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
    session_service.create(
        polish_workspace.paths,
        minutes=60,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    session_service.start(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
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
    return learner_service.create_track(
        running.paths,
        user=other.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A1",
        preferences=learner_service.TrackPreferences(audio_retention_consent=True),
        clock=running.clock,
    ).track_id


# --- 1. The preflight enforces the writer's ownership rule --------------------------


def test_a_package_naming_another_learners_file_is_refused_before_anything_is_written(
    running: PolishWorkspace,
) -> None:
    """Two recordings, the second colliding: the first used to be registered anyway."""

    other = _second_track(running)
    theirs, theirs_digest = write(running.root, "theirs.opus", b"OggS learner B" + b"\x00" * 20)
    artifact_service.register(
        running.paths,
        relative_path=theirs,
        kind="audio",
        expected_sha256=theirs_digest,
        track=other,
        clock=running.clock,
    )
    mine, mine_digest = write(running.root, "mine.opus", b"OggS learner A" + b"\x00" * 20)
    colliding = package(
        artifacts=(
            entry(mine, mine_digest, artifact_id=TWIN),
            entry(theirs, theirs_digest),
        )
    )

    reviewed = speaking_service.validate(
        running.paths, package=colliding, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("another track" in problem for problem in reviewed.problems)

    before = artifact_count(running)
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=colliding, track=running.track_id, clock=running.clock
        )
    assert artifact_count(running) == before, "the first recording was registered anyway"


def test_a_package_naming_this_learners_own_file_is_fine(running: PolishWorkspace) -> None:
    """The rule is about *other* tracks; a learner's own path must still work."""

    relative, digest = write(running.root, "call.opus", b"OggS mine" + b"\x00" * 20)
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id=ART,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    reviewed = speaking_service.validate(
        running.paths,
        package=package(artifacts=(entry(relative, digest),)),
        track=running.track_id,
        clock=running.clock,
    )
    assert reviewed.valid


# --- 2. A row whose bytes are gone does not own the filename -----------------------


def test_a_declined_recordings_filename_can_be_reused(running: PolishWorkspace) -> None:
    """Its bytes were deleted and the row is history, so the name is free."""

    relative, digest = write(running.root, "session.opus", b"OggS declined" + b"\x00" * 20)
    declined = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        retained=False,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    assert not (running.root / relative).exists()

    fresh, fresh_digest = write(
        running.root, "session.opus", b"OggS a new conversation" + b"\x00" * 20
    )
    recorded = artifact_service.register(
        running.paths,
        relative_path=fresh,
        kind="audio",
        expected_sha256=fresh_digest,
        track=running.track_id,
        clock=running.clock,
    )
    assert recorded.artifact_id != declined.artifact_id
    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.ok, "and neither row makes the other look like damage"


def test_a_purged_recordings_filename_can_be_reused(running: PolishWorkspace) -> None:
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
    assert artifact_service.verify(running.paths, track=running.track_id, clock=running.clock).ok


def test_a_live_retained_file_is_still_protected_from_alteration(
    running: PolishWorkspace,
) -> None:
    """Loosening ownership to live-and-retained must not lose the alteration check."""

    relative, digest = write(running.root, "call.opus", b"OggS original" + b"\x00" * 20)
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    (running.root / relative).write_bytes(b"OggS different" + b"\x00" * 20)
    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path=relative,
            kind="audio",
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_altered"


# --- 3. The summary does not contradict itself ------------------------------------


def test_the_verify_summary_does_not_nest_independent_counts(
    running: PolishWorkspace,
) -> None:
    """ "0 files present, 1 of them altered" was a summary arguing with itself.

    A tombstoned recording whose file has come back is `altered` and is not among the
    present files, so the two counts are independent and have to read that way.
    """

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
    (running.root / relative).write_bytes(b"OggS purgeable" + b"\x00" * 20)

    verified = artifact_service.verify(running.paths, track=running.track_id, clock=running.clock)
    assert verified.present == 0
    assert len(verified.altered) == 1

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
    assert "of them altered" not in result.stdout, result.stdout
    assert "not what the record says" in result.stdout
    assert registered.artifact_id in result.stdout
