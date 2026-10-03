#!/usr/bin/env python3
"""Seeding and digest snippets run inside a target DuckDB environment.

`check_duckdb_upgrade.py` executes this module with the interpreter of a virtual
environment that has a specific DuckDB version installed, so it must only import the
installed core.

The seed drives the *real* services rather than hand-writing INSERTs. A hand-written
seed drifts from the schema the moment a service changes how it writes, and an
upgrade test on stale rows proves nothing about the data this release actually
produces. Only the few tables no workflow reaches are filled directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from linguawiki.db.backup import registered_tables
from linguawiki.db.connection import open_reader, open_temporary, open_writer
from linguawiki.db.schema import TABLE_ORDER
from linguawiki.ids import AssessmentId
from linguawiki.paths import workspace_paths
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import assessment as assessment_service
from linguawiki.services import authoring as authoring_service
from linguawiki.services import curriculum as curriculum_service
from linguawiki.services import errors as error_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import packs as pack_service
from linguawiki.services import sessions as session_service
from linguawiki.services import sources as source_service
from linguawiki.services import speaking as speaking_service
from linguawiki.services import transcripts as transcript_service
from linguawiki.services import wiki as wiki_service

SEED_AT = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
PACK_KEY = "pl-pilot"
JOB_ID = "evt_01ARZ3NDEKTSV4RRFFQ69G5FAV"
EVENT_ID = "evt_01ARZ3NDEKTSV4RRFFQ69G5FAW"
TEMPLATE_KEY = "upgrade-fixture-template"
#: Enough scored tasks to fill results, exposures, and posterior state without
#: finalizing every dimension, so both open and stopped state is exercised.
SCORED_TASKS = 8


class Clock:
    def now(self) -> datetime:
        return SEED_AT


def _install_pack(paths: object) -> None:
    pack_service.install(paths, PACK_KEY, clock=Clock())  # type: ignore[arg-type]


def _learner(paths: object) -> str:
    learner_service.create_user(
        paths,  # type: ignore[arg-type]
        display_name="Синтетический Учащийся",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        support_languages=["en"],
        clock=Clock(),
    )
    track = learner_service.create_track(
        paths,  # type: ignore[arg-type]
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        target_level="B1",
        goal="Rozmawiać po polsku",
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
        clock=Clock(),
    )
    return track.track_id


def _onboard(paths: object) -> str:
    onboarding_service.start(
        paths,  # type: ignore[arg-type]
        mode="declared-level",
        declared_level="A2",
        clock=Clock(),
    )
    onboarding_service.record_answer(
        paths,  # type: ignore[arg-type]
        key="self_reported_level",
        value="A2",
        clock=Clock(),
    )
    report = onboarding_service.finalize(paths, clock=Clock())  # type: ignore[arg-type]
    assert report.assessment_run_id is not None
    return report.assessment_run_id


def _calibrate(paths: object, run_id: str) -> None:
    for _ in range(SCORED_TASKS):
        served = assessment_service.next_task(
            paths,  # type: ignore[arg-type]
            run=run_id,
            clock=Clock(),
        )
        if not isinstance(served, assessment_service.NextTaskReport):
            break
        assessment_service.record(
            paths,  # type: ignore[arg-type]
            run=run_id,
            content_id=served.content_id,
            score=1.0,
            clock=Clock(),
        )
    assessment_service.set_status(
        paths,  # type: ignore[arg-type]
        status="paused",
        run=run_id,
        clock=Clock(),
    )


LISTENING_KEY = "pl.task.listening.02"


def _tone(frequency: float = 440.0) -> bytes:
    """A third of a second of a generated tone: a real WAV, and nothing anybody said."""

    import io
    import math
    import struct
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / 8000)))
                for i in range(2400)
            )
        )
    return buffer.getvalue()


def _listening(paths: object, *, root: Path) -> None:
    """A pilot version with one recording, and a play of it recorded the way a page does.

    The pilot ships no recording, so `assessment_task_plays` could not otherwise be
    reached through the commands that own it. The recording is generated.
    """

    import shutil

    from linguawiki.contracts import PackManifest
    from linguawiki.packs.format import directory_digests, pack_content_address
    from linguawiki.packs.stamp import stamp_pack

    with open_reader(paths, clock=Clock()) as database:  # type: ignore[arg-type]
        source = Path(str(pack_service.installed_pack(database, PACK_KEY)["source_path"]))
    pack = root.parent / "pl-pilot-recorded"
    shutil.copytree(source, pack)
    (pack / "media").mkdir(exist_ok=True)
    (pack / "media" / "listening-02.wav").write_bytes(_tone())
    (pack / "assets").mkdir(exist_ok=True)
    (pack / "assets" / "pl-a2-audio.json").write_text(
        json.dumps(
            {
                "schema_name": "lingua.pack.assets.v1",
                "schema_version": 1,
                "catalog_key": "pl-a2-audio",
                "assets": [
                    {
                        "asset_key": "pl.audio.listening-02",
                        "path": "media/listening-02.wav",
                        "media_type": "audio/wav",
                        "duration_ms": 300,
                        "transcript": "synthetic tone",
                        "provenance": {
                            "origin_profile": "authored-original",
                            "review_profile": "authored-verified",
                            "lifecycle": "verified",
                            "risk_tier": 2,
                            "content_hash": "a" * 64,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    form_path = pack / "assessments" / "pl-a2-calibration.json"
    form = json.loads(form_path.read_text(encoding="utf-8"))
    for task in form["tasks"]:
        if task["stable_key"] == LISTENING_KEY:
            task["presentation"] = {
                **task["presentation"],
                "audio": {"asset_key": "pl.audio.listening-02", "replay_allowance": 2},
            }
    form_path.write_text(json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    major, minor, _patch = (int(part) for part in manifest["version"].split("."))
    manifest["version"] = f"{major}.{minor + 1}.0"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    stamp_pack(pack)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = directory_digests(pack)
    manifest["content_address"] = None
    parsed = PackManifest.model_validate(manifest)
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    pack_service.install(paths, pack, clock=Clock(), allow_update=True)  # type: ignore[arg-type]

    run = assessment_service.start(
        paths,  # type: ignore[arg-type]
        dimensions=["listening"],
        modalities=["audio"],
        scoring="machine",
        clock=Clock(),
    )
    served = assessment_service.next_task(paths, run=run.run_id, clock=Clock())  # type: ignore[arg-type]
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.record_play(
        paths,  # type: ignore[arg-type]
        run=run.run_id,
        content_id=served.content_id,
        idempotency_key="upgrade-fixture-play",
        clock=Clock(),
        actor="client",
    )
    assessment_service.record(
        paths,  # type: ignore[arg-type]
        run=run.run_id,
        content_id=served.content_id,
        response="dziesięć minut",
        clock=Clock(),
        actor="client",
    )


def _captured(paths: object) -> None:
    """A spoken answer recorded twice, judged, finalized, and purged -- as a page and a
    judge do it.

    The second recording supersedes the first before the verdict, so a superseded
    submission and its purged recording exist; purging the judged one afterwards marks its
    result invalidated and annotates the estimate snapshot that rested on it. The staging
    rows are what the captures left behind. The recordings are generated tones.
    """

    from linguawiki.services import recordings as recording_service

    learner_service.update_track(
        paths,  # type: ignore[arg-type]
        preferences=learner_service.TrackPreferences(
            voice_available=True,
            transcript_retention_consent=True,
            audio_recording_available=True,
            audio_retention_consent=True,
        ),
        clock=Clock(),
    )
    run = assessment_service.start(
        paths,  # type: ignore[arg-type]
        dimensions=["pronunciation"],
        modalities=["speech"],
        scoring="machine+recorded",
        clock=Clock(),
    )
    served = assessment_service.next_task(paths, run=run.run_id, clock=Clock())  # type: ignore[arg-type]
    assert isinstance(served, assessment_service.NextTaskReport)
    taken = None
    for index, frequency in enumerate((523.25, 587.33)):
        taken = recording_service.capture(
            paths,  # type: ignore[arg-type]
            run=run.run_id,
            content_id=served.content_id,
            capture_id=f"00000000-0000-4000-8000-00000000000{index}",
            data=_tone(frequency),
            media_type="audio/wav",
            clock=Clock(),
            actor="client",
        )
    assert taken is not None and taken.artifact_id is not None
    assessment_service.record(
        paths,  # type: ignore[arg-type]
        run=run.run_id,
        content_id=served.content_id,
        score=0.5,
        audio_artifact=taken.artifact_id,
        assessor_kind="ai",
        assessor="upgrade-fixture-judge",
        confidence="medium",
        rubric={"segmentals": 0.5},
        clock=Clock(),
    )
    assessment_service.finalize(paths, run=run.run_id, clock=Clock())  # type: ignore[arg-type]
    artifact_service.purge(paths, artifact=taken.artifact_id, clock=Clock())  # type: ignore[arg-type]


def _curriculum(paths: object) -> None:
    curriculum_service.import_curriculum(
        paths,  # type: ignore[arg-type]
        {
            "schema_name": "lingua.curriculum.v1",
            "schema_version": 1,
            "title": "Synthetic prior course",
            "kind": "user-authored",
            "version": "1",
            "rights_status": "user-authored",
            "provenance": "written for the upgrade fixture",
            "units": [
                {
                    "code": "u1",
                    "title": "Greetings",
                    "level": "A1",
                    "objectives": [
                        {
                            "objective": "Greet and take leave",
                            "mapped_kind": "knowledge",
                            "mapped_key": "pl.lex.dzien-dobry",
                            "map_confidence": "high",
                        },
                        {"objective": "Spell a surname aloud"},
                    ],
                },
                {
                    "code": "u2",
                    "title": "Travel",
                    "level": "A2",
                    "objectives": [
                        {
                            "objective": "Buy a ticket",
                            "mapped_kind": "knowledge",
                            "mapped_key": "pl.lex.bilet",
                            "map_confidence": "high",
                        },
                        {
                            "objective": "Ask about a platform",
                            "mapped_kind": "knowledge",
                            "mapped_key": "pl.lex.peron",
                            "map_confidence": "high",
                        },
                    ],
                },
            ],
        },
        clock=Clock(),
    )
    curriculum_service.position(
        paths,  # type: ignore[arg-type]
        completed=["u1", "u2"],
        clock=Clock(),
    )
    audit = curriculum_service.audit_start(
        paths,  # type: ignore[arg-type]
        sample_size=3,
        clock=Clock(),
    )
    curriculum_service.audit_record(
        paths,  # type: ignore[arg-type]
        results=[
            {"target_ref": item.target_ref, "outcome": outcome}
            for item, outcome in zip(audit.items, ("correct", "incorrect", "partial"), strict=False)
        ],
        audit=audit.audit_id,
        clock=Clock(),
    )
    curriculum_service.audit_finalize(
        paths,  # type: ignore[arg-type]
        audit=audit.audit_id,
        clock=Clock(),
    )


def _authoring(paths: object) -> None:
    """Fill the template, run, batch, and inspection tables through the real flow."""

    authoring_service.validate_template(
        paths,  # type: ignore[arg-type]
        payload={
            "schema_name": "lingua.pack.template.v1",
            "schema_version": 1,
            "template_key": TEMPLATE_KEY,
            "version": 1,
            "purpose": "generation",
            "intended_kinds": ["construction"],
            "body": "Draft synthetic constructions for the upgrade fixture.",
            "known_failure_modes": ["invents register"],
        },
        clock=Clock(),
    )
    batch = authoring_service.generate_draft(
        paths,  # type: ignore[arg-type]
        template_key=TEMPLATE_KEY,
        version=1,
        item_count=2,
        provider="fixture",
        model="fixture-model",
        clock=Clock(),
    )
    authoring_service.import_items(
        paths,  # type: ignore[arg-type]
        items=[
            {
                "stable_key": "pl.fixture.draft-one",
                "kind": "construction",
                "title": "fixture draft one",
                "body": "Synthetic draft used by the storage-upgrade fixture.",
                "level": "A2",
                "themes": ["praca i biuro"],
                "risk_tier": 1,
                "dependencies": ["pl.lex.biuro"],
            }
        ],
        batch_id=batch.batch_id,
        clock=Clock(),
    )
    queue = authoring_service.review_queue(paths, limit=50, clock=Clock())  # type: ignore[arg-type]
    drafted = next(entry for entry in queue.items if entry.stable_key == "pl.fixture.draft-one")
    authoring_service.review(
        paths,  # type: ignore[arg-type]
        content_id=drafted.content_id,
        axis="linguistic",
        state="machine-checked",
        reviewer_kind="machine",
        reviewer="fixture-checker",
        method="synthetic check",
        inspection="accepted",
        clock=Clock(),
    )


def _learner_model(paths: object) -> None:
    """Fill the Stage 3 tables through the commands that own them.

    One attempt of each shape the aggregation treats differently -- a recognition, a
    controlled production, and a delayed transfer -- so the upgrade is judged on rows
    that exercise the claim ceilings and the delay rule rather than on one uniform row
    repeated. The error is recorded, then given counter-evidence, so both the pattern
    and its `error_evidence` links exist.
    """

    knowledge_service.upsert(
        paths,  # type: ignore[arg-type]
        stable_key="fixture.note.dworzec",
        kind="concept",
        title="notatka o dworcu",
        body="Learner note kept by the storage-upgrade fixture.",
        level="A2",
        aliases=[{"alias": "notatka dworzec", "locale": "pl"}],
        themes=["podróże"],
        clock=Clock(),
    )
    knowledge_service.link(
        paths,  # type: ignore[arg-type]
        source="fixture.note.dworzec",
        relation_type="related",
        target="pl.lex.dworzec",
        clock=Clock(),
    )
    first = evidence_service.record(
        paths,  # type: ignore[arg-type]
        task_type="objective",
        modality="text",
        score=1.0,
        target="pl.lex.dworzec",
        dimension="reading",
        context="fixture:objective",
        clock=Clock(),
    )
    evidence_service.record(
        paths,  # type: ignore[arg-type]
        task_type="short-response",
        modality="writing",
        score=0.9,
        target="pl.lex.dworzec",
        dimension="writing",
        claims=["controlled-production"],
        context="fixture:written",
        response="Jestem na dworcu.",
        response_visibility="excerpt",
        clock=Clock(),
    )
    evidence_service.record(
        paths,  # type: ignore[arg-type]
        task_type="meaning-focused-exchange",
        modality="speech",
        score=1.0,
        target="pl.lex.bilet",
        dimension="speaking",
        claims=["delayed-transfer"],
        retrieval="delayed",
        delay_hours=48.0,
        context="fixture:conversation",
        clock=Clock(),
    )
    evidence_service.observations(
        paths,  # type: ignore[arg-type]
        category="strategy",
        note="Asks for repetition rather than guessing.",
        attempt=first.attempt_id,
        clock=Clock(),
    )
    error_service.record(
        paths,  # type: ignore[arg-type]
        category="case-government",
        signature="szukam bilet",
        description="Accusative used where the verb governs the genitive.",
        target="pl.lex.bilet",
        learner_form="szukam bilet",
        corrected_form="szukam biletu",
        attempt=first.attempt_id,
        clock=Clock(),
    )
    # Counter-evidence after the error exists, so the link rows are written by the same
    # path a learner's own work would take.
    evidence_service.record(
        paths,  # type: ignore[arg-type]
        task_type="extended-productive",
        modality="writing",
        score=1.0,
        target="pl.lex.bilet",
        dimension="writing",
        claims=["spontaneous-production"],
        context="fixture:essay",
        clock=Clock(),
    )
    error_service.add_followup(
        paths,  # type: ignore[arg-type]
        kind="practice",
        action="Drill genitive after szukać in three novel contexts.",
        target="pl.lex.bilet",
        priority=2,
        clock=Clock(),
    )
    evidence_service.recompute(paths, clock=Clock())  # type: ignore[arg-type]


def _session(paths: object) -> None:
    """One whole session: planned, started, flushed, closed, plus an ingested package.

    The session engine's tables are the reason this exists twice over: they hold the
    provisional state a crash would leave behind, and a version change has to preserve
    a staged event as exactly as it preserves a closed one. So the seed leaves *both* --
    a closed session with its finalization, and a second session still holding staged
    work that nothing has credited.
    """

    plan = session_service.create(
        paths,  # type: ignore[arg-type]
        minutes=60,
        mode="mixed",
        idempotency_key="upgrade-fixture-plan",
        clock=Clock(),
    )
    session_service.start(paths, clock=Clock())  # type: ignore[arg-type]
    core = next(block for block in plan.blocks if block.role == "core")
    target = core.targets[0].content_id if core.targets else None
    session_service.log(
        paths,  # type: ignore[arg-type]
        batch={
            "sequence": 1,
            "idempotency_key": "upgrade-fixture-batch-1",
            "block": core.block_id,
            "events": [
                {
                    "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB1",
                    "kind": "attempt.observed",
                    # Deliberately before the flush: the export has to preserve the
                    # difference between when an observation happened and when it was
                    # sent, because every derived fact is built from the first.
                    "occurred_at": "2026-02-02T19:15:00Z",
                    "payload": {
                        "task_type": "short-response",
                        "modality": core.modality,
                        "dimension": core.dimension,
                        "target": target,
                        "score": 1.0,
                        "claims": ["controlled-production"],
                        "assessor_kind": "ai",
                    },
                },
                {
                    "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB2",
                    "kind": "correction.given",
                    "occurred_at": "2026-02-03T04:05:07Z",
                    "payload": {
                        "category": "word-order",
                        "signature": "fixture correction",
                        "description": "Recorded by the storage-upgrade fixture.",
                    },
                },
                {
                    "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB3",
                    "kind": "observation.noted",
                    "occurred_at": "2026-02-03T04:05:08Z",
                    "payload": {"category": "strategy", "note": "Asks before guessing."},
                },
                {
                    "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB4",
                    "kind": "follow_up",
                    "occurred_at": "2026-02-03T04:05:09Z",
                    "payload": {"kind": "practice", "action": "Revisit in the next session."},
                },
            ],
        },
        clock=Clock(),
    )
    session_service.close(
        paths,  # type: ignore[arg-type]
        outcome="completed",
        session=plan.session_id,
        actual_minutes=58,
        fatigue="medium",
        summary="Storage-upgrade fixture session.",
        idempotency_key="upgrade-fixture-close",
        clock=Clock(),
    )
    # A second session left mid-flight, so the export carries staged rows that no
    # finalization has consumed -- the state a crash produces.
    open_plan = session_service.create(
        paths,  # type: ignore[arg-type]
        minutes=40,
        mode="review",
        idempotency_key="upgrade-fixture-plan-2",
        clock=Clock(),
    )
    session_service.start(paths, session=open_plan.session_id, clock=Clock())  # type: ignore[arg-type]
    open_core = next(block for block in open_plan.blocks if block.role == "core")
    session_service.log(
        paths,  # type: ignore[arg-type]
        batch={
            "sequence": 1,
            "idempotency_key": "upgrade-fixture-batch-2",
            "block": open_core.block_id,
            "events": [
                {
                    "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB5",
                    "kind": "observation.noted",
                    "occurred_at": "2026-02-03T04:05:10Z",
                    "payload": {"category": "fatigue", "note": "Stopped mid-block."},
                }
            ],
        },
        session=open_plan.session_id,
        clock=Clock(),
    )
    session_service.ingest_package(
        paths,  # type: ignore[arg-type]
        package=_session_package(),
        session=open_plan.session_id,
        producer="storage-upgrade-fixture",
        clock=Clock(),
    )
    wiki_service.build(paths, view="dashboard", clock=Clock())  # type: ignore[arg-type]


def _session_package() -> dict[str, object]:
    """A minimal `lingua.session.v1` package: one utterance, one attempt event."""

    return {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_upgrade_fixture",
        "external_session_id": "fixture-external-1",
        "target_language": "pl",
        "mode": "completed",
        "started_at": "2026-02-03T04:00:00Z",
        "ended_at": "2026-02-03T04:10:00Z",
        "transcript_layers": [
            {
                "kind": "raw",
                "utterances": [
                    {
                        "utterance_id": "utt_fixture_1",
                        "speaker": "learner",
                        "started_at": "2026-02-03T04:01:00Z",
                        "ended_at": "2026-02-03T04:01:05Z",
                        "text": "Szukam biletu.",
                    }
                ],
            }
        ],
        "events": [
            {
                "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FB6",
                "kind": "follow_up",
                "occurred_at": "2026-02-03T04:02:00Z",
                "payload": {"summary": "Practise ticket vocabulary before the next call."},
            }
        ],
    }


def _material(paths: object) -> None:
    """Every Stage 5 table, through the commands that own them.

    Both halves of the stage are left behind on purpose. A catalogued source with
    comprehension recorded, a unit completed, and an item extracted covers the reading
    side; an ingested conversation with its transcript layers, an interpretation, a
    standing acoustic claim, and a *purged* recording covers the speaking side.

    The purge matters most. It is the one row that is a tombstone rather than a fact, and
    an upgrade that lost the tombstone would turn "the learner asked us to delete this"
    into "there was never a recording", which is the opposite of what the row says.
    """

    podcast = source_service.add(
        paths,  # type: ignore[arg-type]
        kind="podcast",
        title="Polski Daily",
        creator="Paulina",
        canonical_uri="https://example.invalid/feed.xml",
        rights="metadata-only",
        has_audio=True,
        has_transcript=True,
        units=[
            {"label": "Odcinek 1", "starts_at_ms": 0, "ends_at_ms": 480_000},
            {"label": "Odcinek 2", "starts_at_ms": 0, "ends_at_ms": 520_000},
        ],
        clock=Clock(),
    )
    # Unaided first, then aided: the order the policy requires, so the seed is a state the
    # commands could actually have produced.
    source_service.record_comprehension(
        paths,  # type: ignore[arg-type]
        source=podcast.source_id,
        unit="Odcinek 1",
        aid="unaided",
        band="gist",
        mode="extensive",
        minutes=8,
        clock=Clock(),
    )
    source_service.record_comprehension(
        paths,  # type: ignore[arg-type]
        source=podcast.source_id,
        unit="Odcinek 1",
        aid="subtitled",
        band="most",
        replays=2,
        lookups=3,
        minutes=5,
        clock=Clock(),
    )
    source_service.complete_unit(
        paths,  # type: ignore[arg-type]
        source=podcast.source_id,
        unit="Odcinek 1",
        minutes=2,
        clock=Clock(),
    )
    knowledge_service.upsert(
        paths,  # type: ignore[arg-type]
        stable_key="note.podcast.dworzec",
        kind="concept",
        title="Na dworcu",
        body="Station vocabulary met while listening.",
        clock=Clock(),
    )
    source_service.link_item(
        paths,  # type: ignore[arg-type]
        source=podcast.source_id,
        unit="Odcinek 1",
        target="note.podcast.dworzec",
        relation="extracted-from",
        clock=Clock(),
    )
    source_service.set_status(
        paths,  # type: ignore[arg-type]
        source=podcast.source_id,
        status="active",
        clock=Clock(),
    )


def _spoken(paths: object, *, root: Path) -> None:
    """A conversation with its layers, its readings, and a purged recording."""

    plan = session_service.create(
        paths,  # type: ignore[arg-type]
        minutes=40,
        mode="mixed",
        idempotency_key="upgrade-fixture-spoken-plan",
        clock=Clock(),
    )
    session_service.start(paths, session=plan.session_id, clock=Clock())  # type: ignore[arg-type]
    speaking_service.ingest(
        paths,  # type: ignore[arg-type]
        package=_spoken_package(),
        session=plan.session_id,
        producer="storage-upgrade-fixture",
        clock=Clock(),
    )
    transcript_service.review(
        paths,  # type: ignore[arg-type]
        utterance="utt_spoken_2",
        text="Dokąd pani jedzie?",
        reviewer="fixture",
        reason="listened again",
        clock=Clock(),
    )
    transcript_service.interpret(
        paths,  # type: ignore[arg-type]
        utterance="utt_spoken_2",
        classification="transcription-artifact",
        explanation="The tutor's audio clipped; both hearings are plausible.",
        clock=Clock(),
    )
    transcript_service.interpret(
        paths,  # type: ignore[arg-type]
        utterance="utt_spoken_1",
        classification="learner-error",
        category="orthography",
        corrected_form="Chciałbym kupić bilet.",
        meaning="I would like to buy a ticket.",
        # A recorded override, because it is the row whose *absence* an upgrade would not
        # notice: without it the fixture proves nothing about the column that says a
        # person overruled the transcriber's own doubt.
        despite_low_confidence=True,
        reviewer_kind="human",
        reviewer="fixture-listener",
        override_reason="Listened to the recording; the ending is wrong.",
        clock=Clock(),
    )
    transcript_service.record_pronunciation(
        paths,  # type: ignore[arg-type]
        dimension="intelligibility",
        status="observed",
        basis="transcript",
        utterance="utt_spoken_1",
        note="Understandable in context.",
        clock=Clock(),
    )
    learner_service.update_track(
        paths,  # type: ignore[arg-type]
        preferences=learner_service.TrackPreferences(
            audio_retention_consent=True,
            audio_retention_policy="rolling-days",
            audio_retention_days=30,
        ),
        clock=Clock(),
    )
    # A second reading of the same layer, so a superseded revision is in the fixture: it
    # is the row an upgrade is most likely to lose, being the one nothing points at.
    transcript_service.review(
        paths,  # type: ignore[arg-type]
        utterance="utt_spoken_2",
        text="Dokąd pani jedzie dzisiaj?",
        reviewer="fixture-second-listener",
        clock=Clock(),
    )
    recording = root / "artifacts" / "audio" / "fixture.wav"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"RIFF" + b"\x00" * 64)
    artifact = artifact_service.register(
        paths,  # type: ignore[arg-type]
        relative_path="artifacts/audio/fixture.wav",
        kind="audio",
        origin="learner-recording",
        clock=Clock(),
    )
    transcript_service.record_pronunciation(
        paths,  # type: ignore[arg-type]
        dimension="prosody",
        status="confirmed",
        basis="audio",
        utterance="utt_spoken_1",
        audio=artifact.artifact_id,
        note="Question intonation flattened.",
        clock=Clock(),
    )
    # A selected clip of the recording above. It is the row whose *retention* differs from
    # its source's, so an upgrade that lost the clip columns would silently turn a kept
    # excerpt into another whole recording due for deletion.
    clip = root / "artifacts" / "audio" / "moment.wav"
    clip.write_bytes(b"RIFF" + b"\x02" * 24)
    artifact_service.register(
        paths,  # type: ignore[arg-type]
        relative_path="artifacts/audio/moment.wav",
        kind="audio",
        origin="learner-recording",
        clip_of=artifact.artifact_id,
        clip_starts_at_ms=4_000,
        clip_ends_at_ms=7_500,
        clock=Clock(),
    )
    second = root / "artifacts" / "audio" / "withdrawn.wav"
    second.write_bytes(b"RIFF" + b"\x01" * 32)
    withdrawn = artifact_service.register(
        paths,  # type: ignore[arg-type]
        relative_path="artifacts/audio/withdrawn.wav",
        kind="audio",
        origin="learner-recording",
        clock=Clock(),
    )
    # A tombstone, and the invalidated claim that rested on it.
    transcript_service.record_pronunciation(
        paths,  # type: ignore[arg-type]
        dimension="native-likeness",
        status="confirmed",
        basis="audio",
        utterance="utt_spoken_2",
        audio=withdrawn.artifact_id,
        clock=Clock(),
    )
    artifact_service.purge(
        paths,  # type: ignore[arg-type]
        artifact=withdrawn.artifact_id,
        reason="learner-request",
        clock=Clock(),
    )
    session_service.abandon(
        paths,  # type: ignore[arg-type]
        session=plan.session_id,
        reason="fixture leaves this session holding staged work",
        clock=Clock(),
    )


def _spoken_package() -> dict[str, object]:
    """A conversation with a raw layer and a normalized one that changes no words."""

    return {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": "pkg_upgrade_spoken",
        "external_session_id": "fixture-external-2",
        "transcriber": {
            "name": "fixture-transcriber",
            "version": "1",
            "confidence_basis": "exp(avg_logprob) per segment, 0..1",
        },
        "target_language": "pl",
        "mode": "completed",
        "started_at": "2026-02-03T05:00:00Z",
        "ended_at": "2026-02-03T05:20:00Z",
        "transcript_layers": [
            {
                "kind": "raw",
                "utterances": [
                    {
                        "utterance_id": "utt_spoken_1",
                        "speaker": "learner",
                        "started_at": "2026-02-03T05:01:00Z",
                        "ended_at": "2026-02-03T05:01:06Z",
                        "text": "chcialbym kupic bilet",
                        "confidence": 0.2,
                    },
                    {
                        "utterance_id": "utt_spoken_2",
                        "speaker": "tutor",
                        "started_at": "2026-02-03T05:01:10Z",
                        "ended_at": "2026-02-03T05:01:14Z",
                        "text": "dokad pan jedzie",
                    },
                ],
            },
            {
                "kind": "normalized",
                "derived_from": "raw",
                "utterances": [
                    {
                        "utterance_id": "utt_spoken_1",
                        "speaker": "learner",
                        "started_at": "2026-02-03T05:01:00Z",
                        "ended_at": "2026-02-03T05:01:06Z",
                        "text": "Chcialbym kupic bilet.",
                    }
                ],
            },
        ],
        "events": [],
    }


def _judging_rows(paths: object) -> None:
    """A judge's claim and release, and a served round, on the run `_captured` judged.

    Written directly, as `_job_row` is: the commands that own these tables (`assessment
    claim`, `release`, and batched serving) arrive later in C6. The rows are the ones those
    commands write -- a lease the judge gave back before the verdict that judged the
    submission, and a one-task round naming the task the run served -- so `db check` holds
    them to the same rules on both versions.
    """

    with (
        open_writer(paths, command="seed", clock=Clock()) as database,  # type: ignore[arg-type]
        database.transaction() as tx,
    ):
        now = tx.now()
        submission_id, run_id = tx.one(
            "SELECT submission_id, run_id FROM assessment_submissions "
            "WHERE status = 'judged' ORDER BY submission_id LIMIT 1"
        )
        content_id, dimension = tx.one(
            "SELECT content_id, dimension FROM assessment_run_tasks WHERE run_id = ? "
            "ORDER BY sequence LIMIT 1",
            [run_id],
        )
        claim_id = str(AssessmentId.new())
        tx.execute(
            "INSERT INTO judging_claims (claim_id, submission_id, judge, claimed_at, "
            "lease_expires_at) VALUES (?, ?, ?, ?, ?)",
            [claim_id, submission_id, "upgrade-fixture-judge", now, now + timedelta(minutes=10)],
        )
        reason = "the judge restarted before it listened"
        tx.execute(
            "INSERT INTO judging_releases (claim_id, released_at, terminal, code, reason, "
            "reason_hash) VALUES (?, ?, FALSE, NULL, ?, ?)",
            [claim_id, now, reason, hashlib.sha256(reason.encode("utf-8")).hexdigest()],
        )
        batch_id = str(AssessmentId.new())
        tx.execute(
            "INSERT INTO assessment_batches (batch_id, run_id, idempotency_key, request_hash, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            [batch_id, run_id, "upgrade-fixture-round-1", "d" * 64, now],
        )
        tx.execute(
            "INSERT INTO assessment_batch_tasks (batch_id, position, content_id, dimension) "
            "VALUES (?, 1, ?, ?)",
            [batch_id, content_id, dimension],
        )


def _job_row(paths: object) -> None:
    """The one table no Stage 2 workflow writes: local job state."""

    with (
        open_writer(paths, command="seed", clock=Clock()) as database,  # type: ignore[arg-type]
        database.transaction() as tx,
    ):
        now = tx.now()
        tx.execute(
            "INSERT INTO jobs (job_id, kind, status, correlation_id, error_code, "
            "error_message, error_details_json, started_at, finished_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                JOB_ID,
                "wiki.render",
                "failed",
                EVENT_ID,
                "render_failed",
                "template missing",
                '{"template":"index"}',
                now,
                now,
                now,
                now,
            ],
        )
        tx.execute(
            "UPDATE projection_state SET last_event_id = ?, content_hash = ?, "
            "stale = FALSE, generated_at = ?, updated_at = ? WHERE projection = 'wiki'",
            [EVENT_ID, "c" * 64, now, now],
        )


def seed(root: Path) -> None:
    """Fill every table this schema version has, through the commands that own them."""

    paths = workspace_paths(root)
    _install_pack(paths)
    _learner(paths)
    run_id = _onboard(paths)
    _calibrate(paths, run_id)
    _curriculum(paths)
    _authoring(paths)
    _learner_model(paths)
    _session(paths)
    _material(paths)
    _spoken(paths, root=root)
    _listening(paths, root=root)
    _captured(paths)
    _judging_rows(paths)
    _job_row(paths)


def _digest_rows(database: object, table: str) -> dict[str, object]:
    rows = database.query(f'SELECT * FROM "{table}" ORDER BY ALL')  # type: ignore[attr-defined]
    canonical = "\n".join(
        "\x1f".join("\x00NULL" if value is None else repr(value) for value in row) for row in rows
    )
    return {
        "rows": len(rows),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "columns": [name for name, _ in database.columns(table)],  # type: ignore[attr-defined]
    }


def digest(database_path: Path) -> dict[str, object]:
    """Per-table row count, column list, and content hash, plus key stable identities."""

    with open_temporary(database_path, clock=Clock()) as database:
        present = registered_tables(database)
        tables = {table: _digest_rows(database, table) for table in TABLE_ORDER if table in present}
        identities = {
            "workspace_id": database.scalar("SELECT workspace_id FROM workspaces"),
            "user_ids": [row[0] for row in database.query("SELECT user_id FROM users ORDER BY 1")],
            "track_ids": [
                row[0] for row in database.query("SELECT track_id FROM learning_tracks ORDER BY 1")
            ],
            "pack_checksums": [
                row[0]
                for row in database.query("SELECT checksum FROM pack_installations ORDER BY 1")
            ],
            "content_hashes": [
                row[0]
                for row in database.query(
                    "SELECT content_hash FROM content_records ORDER BY content_id"
                )
            ],
            "skill_estimates": database.query(
                "SELECT dimension, level_code, confidence_label, basis FROM skill_estimates "
                "ORDER BY dimension"
            ),
            "placement_state": database.query(
                "SELECT dimension, status, tasks_used, confidence_label "
                "FROM placement_dimension_state ORDER BY dimension"
            ),
            # The learner model has to survive a version change as exactly as the pack
            # does: a stage that shifted, or an error that lost its counter-evidence,
            # would be a silent rewriting of what the learner was observed to do.
            "item_stages": database.query(
                "SELECT content_id, stage, gated_stage, evidence_ceiling, aggregation_version "
                "FROM track_item_state ORDER BY content_id"
            ),
            "evidence_claims": database.query(
                "SELECT claim, polarity, strength, context_key, novelty, retrieval "
                "FROM evidence ORDER BY evidence_id"
            ),
            "error_statuses": database.query(
                "SELECT category, signature, status, occurrence_count, success_count "
                "FROM error_patterns ORDER BY error_id"
            ),
            "estimate_snapshots": database.query(
                "SELECT dimension, estimate_status, level_code, basis, evidence_count "
                "FROM estimate_history ORDER BY snapshot_id"
            ),
            "event_ids": [
                row[0] for row in database.query("SELECT event_id FROM domain_events ORDER BY 1")
            ],
            "projection": database.query(
                "SELECT projection, projection_version, last_event_id, content_hash, stale "
                "FROM projection_state ORDER BY 1"
            ),
        }
    return {"tables": tables, "identities": identities}


def workspace_digest(root: Path) -> dict[str, object]:
    paths = workspace_paths(root)
    with open_reader(paths, clock=Clock()):
        pass
    return digest(paths.database)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "digest", "workspace-digest"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.action == "seed":
        seed(args.path)
        return 0
    payload = (
        workspace_digest(args.path) if args.action == "workspace-digest" else digest(args.path)
    )
    json.dump(payload, sys.stdout, sort_keys=True, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
