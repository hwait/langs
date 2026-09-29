"""Regressions for the four defects round twenty found in Stage 5.

Two of them are the same shape as every round since fifteen: a check the writer performs
that the read-only preflight did not, so a package could be approved, write its first
artifact, and be refused on its second. The other two are about not leaving the workspace in
a state nothing accounts for, and not turning an unreadable file into a crash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from linguawiki.services import speaking as speaking_service
from tests.conftest import PolishWorkspace

ART = "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "art_01ARZ3NDEKTSV4RRFFQ69G5FB9"


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


def entry(
    relative: str,
    digest: str,
    *,
    artifact_id: str = ART,
    kind: str = "audio",
    retained: bool = True,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "relative_path": relative,
        "sha256": digest,
        "retained": retained,
    }


def recording(root: Path, name: str = "call.opus") -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"OggS" + name.encode() + b"\x00" * 32)
    return f"imports/{name}", hashlib.sha256(path.read_bytes()).hexdigest()


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


# --- 1. The retention conflict runs in both directions -------------------------------


def test_a_package_cannot_declare_a_kept_recording_unkept(running: PolishWorkspace) -> None:
    """The reverse of the case round seventeen fixed, and registration refused both."""

    relative, digest = recording(running.root, "kept.opus")
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        external_id=ART,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    # A second artifact declared first, so a late refusal would have something to leave.
    other_relative, other_digest = recording(running.root, "other.opus")
    reversing = package(
        artifacts=(
            entry(other_relative, other_digest, artifact_id=OTHER),
            entry(relative, digest, retained=False),
        )
    )
    reviewed = speaking_service.validate(
        running.paths, package=reversing, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("artifact purge" in problem for problem in reviewed.problems)

    before = artifact_count(running)
    with pytest.raises(LinguaWikiError) as failure:
        speaking_service.ingest(
            running.paths, package=reversing, track=running.track_id, clock=running.clock
        )
    assert failure.value.payload.code == "package_audio_unavailable"
    assert artifact_count(running) == before, "the second artifact was written anyway"
    assert (running.root / relative).is_file(), "and the kept recording survives"


# --- 2. Containment is resolved, not lexical ----------------------------------------


def test_a_package_naming_a_symlink_out_of_the_workspace_is_refused(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    """The lexical check refuses `..`; only resolution catches a link."""

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    elsewhere = outside / "not-the-learners.opus"
    elsewhere.write_bytes(b"OggS somebody elses data" + b"\x00" * 20)
    imports = running.root / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    (imports / "escape.opus").symlink_to(elsewhere)
    digest = hashlib.sha256(elsewhere.read_bytes()).hexdigest()

    other_relative, other_digest = recording(running.root, "legit.opus")
    escaping = package(
        artifacts=(
            entry(other_relative, other_digest, artifact_id=OTHER),
            entry("imports/escape.opus", digest),
        )
    )
    reviewed = speaking_service.validate(
        running.paths, package=escaping, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("stay inside" in problem for problem in reviewed.problems)

    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=escaping, track=running.track_id, clock=running.clock
        )
    assert artifact_count(running) == 0
    assert elsewhere.is_file(), "and nothing outside the workspace was touched"


def test_a_symlinked_declination_cannot_delete_outside_the_workspace(
    running: PolishWorkspace, tmp_path: Path
) -> None:
    """The declination path deletes, so it needs the containment check most of all."""

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    elsewhere = outside / "precious.opus"
    elsewhere.write_bytes(b"OggS precious" + b"\x00" * 20)
    imports = running.root / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    (imports / "escape.opus").symlink_to(elsewhere)
    digest = hashlib.sha256(elsewhere.read_bytes()).hexdigest()

    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths,
            package=package(artifacts=(entry("imports/escape.opus", digest, retained=False),)),
            track=running.track_id,
            clock=running.clock,
        )
    assert elsewhere.is_file()


# --- 3. Nothing is abandoned under a private root ----------------------------------


def test_an_unreadable_declared_file_is_a_problem_not_a_crash(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    real = artifact_service.file_digest

    def unreadable(path: Path) -> str:
        if path.name == "call.opus":
            raise OSError("Input/output error")
        return real(path)

    with mock.patch.object(artifact_service, "file_digest", unreadable):
        reviewed = speaking_service.validate(
            running.paths,
            package=package(artifacts=(entry(relative, digest),)),
            track=running.track_id,
            clock=running.clock,
        )
    assert not reviewed.valid
    assert any("cannot be read" in problem for problem in reviewed.problems)


# --- 4. An unreadable recording is "not available", not an error -------------------


def test_an_unreadable_recording_does_not_break_a_duplicate_retry(
    running: PolishWorkspace,
) -> None:
    """The retry is deliberately a no-op, so reading its audio must not be able to fail it."""

    relative, digest = recording(running.root)
    sound = package(artifacts=(entry(relative, digest),))
    session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    real = artifact_service.file_digest

    def unreadable(path: Path) -> str:
        if path.name == "call.opus":
            raise OSError("Input/output error")
        return real(path)

    with mock.patch.object(artifact_service, "file_digest", unreadable):
        again = session_service.ingest_package(
            running.paths, package=sound, track=running.track_id, clock=running.clock
        )
    assert again.duplicate
    assert not again.audio_available


def test_a_recording_that_disappears_mid_check_is_not_available(
    running: PolishWorkspace,
) -> None:
    relative, digest = recording(running.root)
    sound = package(artifacts=(entry(relative, digest),))
    session_service.ingest_package(
        running.paths, package=sound, track=running.track_id, clock=running.clock
    )
    real = artifact_service.file_digest

    def vanishing(path: Path) -> str:
        if path.name == "call.opus":
            raise FileNotFoundError(str(path))
        return real(path)

    with mock.patch.object(artifact_service, "file_digest", vanishing):
        again = session_service.ingest_package(
            running.paths, package=sound, track=running.track_id, clock=running.clock
        )
    assert again.duplicate
    assert not again.audio_available


def test_review_says_a_package_cannot_be_ingested_with_no_session_open(
    polish_workspace: PolishWorkspace,
) -> None:
    """Found by the gate: the documented loop reviewed before planning.

    Review runs exactly the checks ingestion runs, so a workspace with nothing to attach a
    package to makes it un-ingestable -- which is true, and worth saying rather than
    reporting a package as valid that the next command will refuse. The skill's loop now
    plans the session first.
    """

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)

    reviewed = speaking_service.validate(
        polish_workspace.paths,
        package=package(),
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert not reviewed.valid
    assert any("session" in problem for problem in reviewed.problems)

    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            polish_workspace.paths,
            package=package(),
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    # With a session open, the same package reviews clean.
    session_service.create(
        polish_workspace.paths,
        minutes=60,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    session_service.start(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert speaking_service.validate(
        polish_workspace.paths,
        package=package(),
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    ).valid
