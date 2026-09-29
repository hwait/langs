"""Reading inside the session lifecycle, and what the workspace would leak.

Stage 4 left source progress outside the close and said so. These tests are the other
half of that promise: a reading block records what it did through the same boundary as
everything else, and the rule that makes unaided comprehension meaningful holds there
too. The privacy tests are the stage's other exit condition -- private or copyrighted
material must not reach the files a learner commits.
"""

from __future__ import annotations

from typing import Any

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import privacy as privacy_service
from linguawiki.services import sessions as session_service
from linguawiki.services import sources as source_service
from linguawiki.services import speaking as speaking_service
from tests.conftest import PolishWorkspace

EVENT = "evt_01ARZ3NDEKTSV4RRFFQ69G5F01"
SECOND_EVENT = "evt_01ARZ3NDEKTSV4RRFFQ69G5F02"


@pytest.fixture
def reading(polish_workspace: PolishWorkspace) -> tuple[PolishWorkspace, Any]:
    """An onboarded track, one catalogued podcast, and one active session."""

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    source = source_service.add(
        polish_workspace.paths,
        kind="podcast",
        title="Polski Daily",
        rights="metadata-only",
        has_audio=True,
        units=[{"label": "Odcinek 1"}, {"label": "Odcinek 2"}],
        track=polish_workspace.track_id,
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
    return polish_workspace, source


def batch(*events: dict[str, Any], sequence: int = 1, key: str = "reading-1") -> dict[str, Any]:
    return {
        "schema_name": "lingua.session.events.v1",
        "schema_version": 1,
        "sequence": sequence,
        "idempotency_key": key,
        "events": list(events),
    }


def progress(event_id: str = EVENT, **payload: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_ref": "Polski Daily",
        "unit": "Odcinek 1",
        "aid": "unaided",
        "band": "most",
        "mode": "intensive",
        "minutes": 9,
    }
    body.update(payload)
    return {
        "event_id": event_id,
        "kind": "source.progress",
        "occurred_at": "2026-01-01T10:05:00Z",
        "payload": body,
    }


def test_a_reading_block_reaches_the_learner_through_the_close(
    reading: tuple[PolishWorkspace, Any],
) -> None:
    workspace, source = reading
    session_service.log(
        workspace.paths,
        batch=batch(progress(completed=True)),
        track=workspace.track_id,
        clock=workspace.clock,
    )
    # Nothing is credited while the session runs.
    before = source_service.show(
        workspace.paths, source=source.source_id, track=workspace.track_id, clock=workspace.clock
    )
    assert before.progress is None or before.progress.completed_units == 0

    closed = session_service.close(workspace.paths, track=workspace.track_id, clock=workspace.clock)
    assert closed.comprehension_written == 1
    assert closed.sources_worked == ("Polski Daily",)

    after = source_service.show(
        workspace.paths, source=source.source_id, track=workspace.track_id, clock=workspace.clock
    )
    assert after.progress is not None
    assert after.progress.unaided_band == "most"
    assert after.progress.completed_units == 1
    assert after.progress.minutes_spent == 9
    report = database_service.check(workspace.paths, clock=workspace.clock)
    assert report.ok, [check.name for check in report.failures]


def test_the_close_refuses_to_withdraw_help_the_learner_already_had(
    reading: tuple[PolishWorkspace, Any],
) -> None:
    """The rule holds at the close, so the close is not a way around the command."""

    workspace, source = reading
    source_service.record_comprehension(
        workspace.paths,
        source=source.source_id,
        unit="Odcinek 1",
        aid="translated",
        band="full",
        track=workspace.track_id,
        clock=workspace.clock,
    )
    session_service.log(
        workspace.paths,
        batch=batch(progress()),
        track=workspace.track_id,
        clock=workspace.clock,
    )
    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(workspace.paths, track=workspace.track_id, clock=workspace.clock)
    assert failure.value.payload.code == "comprehension_aid_regressed"


def test_two_progress_events_in_one_close_are_ordered_against_each_other(
    reading: tuple[PolishWorkspace, Any],
) -> None:
    """The second event must see the first: otherwise both believe they are the first."""

    workspace, source = reading
    session_service.log(
        workspace.paths,
        batch=batch(
            progress(aid="unaided", band="gist"),
            progress(SECOND_EVENT, aid="glossed", band="full"),
        ),
        track=workspace.track_id,
        clock=workspace.clock,
    )
    closed = session_service.close(workspace.paths, track=workspace.track_id, clock=workspace.clock)
    assert closed.comprehension_written == 2
    after = source_service.show(
        workspace.paths, source=source.source_id, track=workspace.track_id, clock=workspace.clock
    )
    assert after.progress is not None
    assert after.progress.unaided_band == "gist"
    assert after.progress.aided_band == "full"


def test_a_source_that_does_not_exist_is_refused_by_name(
    reading: tuple[PolishWorkspace, Any],
) -> None:
    workspace, _ = reading
    session_service.log(
        workspace.paths,
        batch=batch(progress(source_ref="A book nobody catalogued")),
        track=workspace.track_id,
        clock=workspace.clock,
    )
    with pytest.raises(LinguaWikiError) as failure:
        session_service.close(workspace.paths, track=workspace.track_id, clock=workspace.clock)
    assert failure.value.payload.code == "source_not_found"


def test_unfinished_material_is_a_reason_to_plan_that_kind_of_block(
    reading: tuple[PolishWorkspace, Any],
) -> None:
    workspace, source = reading
    session_service.abandon(
        workspace.paths, reason="test", track=workspace.track_id, clock=workspace.clock
    )
    source_service.record_comprehension(
        workspace.paths,
        source=source.source_id,
        unit="Odcinek 1",
        aid="unaided",
        band="most",
        track=workspace.track_id,
        clock=workspace.clock,
    )
    plan = session_service.create(
        workspace.paths,
        minutes=60,
        mode="listening",
        track=workspace.track_id,
        clock=workspace.clock,
    )
    rationales = " ".join(" ".join(block.rationale) for block in plan.blocks)
    assert "material the learner started" in rationales


def test_the_privacy_audit_finds_a_transcript_pasted_into_a_committed_page(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    session_service.create(
        polish_workspace.paths,
        minutes=60,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    session_service.start(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    spoken = "chcialbym kupic bilet do Krakowa na jutro rano"
    speaking_service.ingest(
        polish_workspace.paths,
        package={
            "schema_name": "lingua.session.v1",
            "schema_version": 1,
            "package_id": "pkg_rozmowa_1",
            "external_session_id": "rozmowa-1",
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
                            "text": spoken,
                        }
                    ],
                }
            ],
            "events": [],
            "artifacts": [],
        },
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    clean = privacy_service.audit(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert clean.ok
    assert clean.retention.utterances_with_words == 1

    page = polish_workspace.root / "wiki" / "sessions" / "rozmowa-1.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"# Rozmowa\n\nThe learner said: {spoken}\n", encoding="utf-8")

    leaking = privacy_service.audit(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert not leaking.ok
    assert [entry.path for entry in leaking.content_leaks] == ["wiki/sessions/rozmowa-1.md"]
    assert "the learner's own words" in leaking.content_leaks[0].reason


def test_the_privacy_audit_states_what_a_purge_would_cost_before_it_happens(
    polish_workspace: PolishWorkspace,
) -> None:
    from linguawiki.services import artifacts as artifact_service
    from linguawiki.services import learners as learner_service

    learner_service.update_track(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        preferences=learner_service.TrackPreferences(audio_retention_consent=True),
        clock=polish_workspace.clock,
    )
    directory = polish_workspace.root / "artifacts" / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rozmowa.wav").write_bytes(b"RIFF" + b"\x00" * 32)
    artifact = artifact_service.register(
        polish_workspace.paths,
        relative_path="artifacts/audio/rozmowa.wav",
        kind="audio",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    report = privacy_service.audit(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert report.retention.artifacts_held == 1
    assert [entry.artifact_id for entry in report.purge_consequences] == [artifact.artifact_id]


def test_no_command_writes_the_learners_words_into_the_audit_log(
    polish_workspace: PolishWorkspace,
) -> None:
    """The log is read back into reports and shown to skills, so it is a surface too.

    The audit is what proves the claim rather than a promise in a docstring: every summary
    a Stage 5 command writes names a kind, a count, or an identifier, and never a body.
    """

    from linguawiki.services import transcripts as transcript_service

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    source_service.add(
        polish_workspace.paths,
        kind="book",
        title="Lalka",
        rights="short-excerpt",
        units=[{"label": "Rozdział 1", "excerpt": "Ależ to był rok, powiadam państwu!"}],
        track=polish_workspace.track_id,
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
    spoken = "chcialbym kupic bilet do Krakowa na jutro rano"
    speaking_service.ingest(
        polish_workspace.paths,
        package={
            "schema_name": "lingua.session.v1",
            "schema_version": 1,
            "package_id": "pkg_rozmowa_2",
            "external_session_id": "rozmowa-2",
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
                            "text": spoken,
                        }
                    ],
                }
            ],
            "events": [],
            "artifacts": [],
        },
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    transcript_service.normalize(
        polish_workspace.paths,
        utterance="utt_001",
        text="Chcialbym kupic bilet do Krakowa na jutro rano.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    report = privacy_service.audit(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert report.log_leaks == ()
    assert report.ok
