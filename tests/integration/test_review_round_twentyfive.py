"""Regressions for the final three Stage 5 review findings.

The two preflight cases pin one contract: package review and registration make the same
decision before the first write. The retention case pins the destructive boundary: a file
offered with an instruction to keep it is never deleted to preserve an older decision.
"""

from __future__ import annotations

import hashlib
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

ART = "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "art_01ARZ3NDEKTSV4RRFFQ69G5FB9"


def write(root: Path, name: str, body: bytes) -> tuple[str, str]:
    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return f"imports/{name}", hashlib.sha256(body).hexdigest()


def entry(relative: str, digest: str, *, artifact_id: str = ART) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": "audio",
        "relative_path": relative,
        "sha256": digest,
        "retained": True,
    }


def package(*, artifacts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return {
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
        "artifacts": artifacts,
    }


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


def artifact_count(workspace: PolishWorkspace) -> int:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return int(database.scalar("SELECT count(*) FROM artifacts"))


def test_package_preflight_catches_a_same_track_path_alteration_before_any_write(
    running: PolishWorkspace,
) -> None:
    owned, owned_digest = write(
        running.root, "owned.opus", b"OggS original recording" + b"\x00" * 20
    )
    artifact_service.register(
        running.paths,
        relative_path=owned,
        kind="audio",
        expected_sha256=owned_digest,
        track=running.track_id,
        clock=running.clock,
    )
    altered_body = b"OggS different recording" + b"\x00" * 20
    (running.root / owned).write_bytes(altered_body)
    altered_digest = hashlib.sha256(altered_body).hexdigest()
    fresh, fresh_digest = write(running.root, "fresh.opus", b"OggS fresh" + b"\x00" * 20)
    payload = package(
        artifacts=(
            entry(fresh, fresh_digest, artifact_id=OTHER),
            entry(owned, altered_digest),
        )
    )

    reviewed = speaking_service.validate(
        running.paths, package=payload, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("no longer has the bytes" in problem for problem in reviewed.problems)
    before = artifact_count(running)
    with pytest.raises(LinguaWikiError):
        speaking_service.ingest(
            running.paths, package=payload, track=running.track_id, clock=running.clock
        )
    assert artifact_count(running) == before, "the first package artifact was written anyway"


def test_a_request_to_retain_declined_bytes_refuses_without_deleting_them(
    running: PolishWorkspace,
) -> None:
    relative, digest = write(running.root, "declined.opus", b"OggS declined" + b"\x00" * 20)
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
    (running.root / relative).write_bytes(b"OggS declined" + b"\x00" * 20)

    with pytest.raises(LinguaWikiError) as failure:
        artifact_service.register(
            running.paths,
            relative_path=relative,
            kind="audio",
            retained=True,
            expected_sha256=digest,
            track=running.track_id,
            clock=running.clock,
        )
    assert failure.value.payload.code == "artifact_retention_conflict"
    assert (running.root / relative).is_file(), "a retain request deleted the offered file"
    with open_reader(running.paths, clock=running.clock) as database:
        assert not bool(
            database.scalar(
                "SELECT retained FROM artifacts WHERE artifact_id = ?", [declined.artifact_id]
            )
        )


def test_a_package_binds_an_unbound_local_artifact(running: PolishWorkspace) -> None:
    relative, digest = write(running.root, "call.opus", b"OggS existing" + b"\x00" * 20)
    local = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    payload = package(artifacts=(entry(relative, digest),))

    reviewed = speaking_service.validate(
        running.paths, package=payload, track=running.track_id, clock=running.clock
    )
    assert reviewed.valid
    ingested = session_service.ingest_package(
        running.paths, package=payload, track=running.track_id, clock=running.clock
    )
    assert ingested.audio_available
    assert artifact_count(running) == 1
    with open_reader(running.paths, clock=running.clock) as database:
        assert (
            database.scalar(
                "SELECT external_id FROM artifacts WHERE artifact_id = ?", [local.artifact_id]
            )
            == ART
        )


def test_an_unbound_non_audio_artifact_cannot_be_bound_as_audio(
    running: PolishWorkspace,
) -> None:
    relative, digest = write(running.root, "words.txt", b"not a recording")
    artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="transcript",
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    payload = package(artifacts=(entry(relative, digest),))

    reviewed = speaking_service.validate(
        running.paths, package=payload, track=running.track_id, clock=running.clock
    )
    assert not reviewed.valid
    assert any("held here as transcript" in problem for problem in reviewed.problems)
