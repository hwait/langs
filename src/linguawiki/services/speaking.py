"""Making, checking, and taking in a spoken session.

Speaking arrives from outside. Some other program listened, produced text, and wrote a
file; this module is the border that file crosses. Three things happen here and nothing
else does:

- **making** a package, so that speaking works with no voice tooling at all. A learner
  with a phone recording and a text editor can produce a valid `lingua.session.v1` file,
  and the scaffold is what stops that from being an exercise in reading a schema;
- **checking** one before it is taken in, because the useful moment to learn that a
  package confirms pronunciation without audio is *before* it becomes part of a learner's
  record, not after;
- **taking it in**, which is two operations that belong together: the events are staged
  through the session engine and the transcript is stored as layers.

The adapters exist so that no provider's field names ever reach a domain table. An
adapter's whole job is to end: it maps a foreign shape into the contract and then has no
further say in anything. That is what "core is independent of any voice provider" has to
mean in code, rather than in a document.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from linguawiki import transcripts as transcript_policy
from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.db.connection import open_reader
from linguawiki.errors import ErrorDetail, LinguaWikiError, validated_contract
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import learners as learner_service
from linguawiki.services import sessions as session_service
from linguawiki.services import transcripts as transcript_service

#: The export shapes this release can read.
#:
#: `lingua` is the contract itself, which is what a hand-written package is. The other is
#: a transcription format rather than a product: a segment list with times and text. An
#: adapter is added when a provider's export is stable enough to be worth the coupling,
#: and removed the moment it is not, without anything downstream noticing.
ADAPTERS: tuple[str, ...] = ("lingua", "whisper-verbose-json")


class ValidationReport(ContractModel):
    valid: bool
    package_id: str | None = None
    external_session_id: str | None = None
    mode: str | None = None
    track_id: str | None = None
    session_id: str | None = None
    #: What the package holds, so a reviewer can see the size of what they are approving.
    layers: tuple[str, ...] = ()
    utterances: int = 0
    events: int = 0
    artifacts: int = 0
    #: True when this exact content was already ingested. Ingesting it again stages
    #: nothing, which is the point rather than a failure.
    duplicate: bool = False
    #: What would be kept of the learner's own words if this were ingested now.
    retention_policy: str = "withheld"
    #: Everything that would make ingestion fail, found in one pass rather than one at a
    #: time: a reviewer fixing an export wants the whole list.
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class SpokenIngestReport(ContractModel):
    ingestion_id: str
    package_id: str
    session_id: str
    track_id: str
    external_session_id: str
    mode: str
    staged_events: int = 0
    skipped_events: int = 0
    imported_utterances: int = 0
    skipped_utterances: int = 0
    duplicate: bool = False
    audio_available: bool = False
    retention_policy: str = "withheld"
    warnings: tuple[str, ...] = ()


def _timestamp(value: datetime) -> str:
    return aware_utc(value).isoformat().replace("+00:00", "Z")


def scaffold(
    *,
    external_session_id: str,
    target_language: str,
    started_at: datetime,
    minutes: int = 20,
    session: str | None = None,
    track: str | None = None,
    mode: str = "completed",
    speakers: Sequence[str] = ("learner", "tutor"),
    utterances: int = 4,
) -> dict[str, Any]:
    """Produce an empty but valid package for somebody to fill in.

    This is the manual fallback, and it is a first-class path rather than a degraded one.
    A learner with a recording and a text editor can speak their target language and have
    it counted; requiring a particular voice application for that would make the whole
    modality contingent on a product this repository does not control.

    The utterance stubs are timed evenly across the session only so that the file is valid
    as written. Real times replace them; the point is that nothing is rejected for a
    reason the author cannot see.
    """

    if utterances < 1:
        raise LinguaWikiError(
            "invalid_scaffold",
            "a package with no utterances is a session with nothing said in it",
            details=(ErrorDetail(field="utterances", reason=str(utterances)),),
        )
    start = aware_utc(started_at)
    step = timedelta(minutes=minutes) / (utterances + 1)
    stubs = [
        {
            "utterance_id": f"utt_{index:03d}",
            "speaker": speakers[(index - 1) % len(speakers)],
            "started_at": _timestamp(start + step * index),
            "ended_at": _timestamp(start + step * index + step / 2),
            "text": "replace this with what was actually said",
        }
        for index in range(1, utterances + 1)
    ]
    return {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": f"pkg_{hashlib.sha256(external_session_id.encode()).hexdigest()[:16]}",
        "external_session_id": external_session_id,
        "session_id": session,
        "track_hint": track,
        "target_language": target_language,
        "mode": mode,
        "started_at": _timestamp(start),
        "ended_at": _timestamp(start + timedelta(minutes=minutes)),
        "learning_targets": [],
        "transcript_layers": [{"kind": "raw", "derived_from": None, "utterances": stubs}],
        "events": [],
        "artifacts": [],
    }


def _whisper_utterances(payload: Mapping[str, Any], *, start: datetime, speaker: str) -> list[Any]:
    """Map a segment list with times and text onto utterances, and nothing else.

    Everything the format carries that the contract has no place for -- token logprobs,
    temperatures, compression ratios, the model's name -- stops here. A provider field
    that reached a domain table would have to be migrated out later by someone who no
    longer knows why it is there.
    """

    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise LinguaWikiError(
            "adapter_found_no_segments",
            "this export has no segment list, so there is nothing to map onto utterances; "
            "check that it was written in the verbose JSON form rather than plain text",
            details=(ErrorDetail(field="segments", reason="missing or empty"),),
        )
    mapped: list[Any] = []
    for index, segment in enumerate(segments, start=1):
        if not isinstance(segment, Mapping):
            continue
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        begins = float(segment.get("start", 0.0))
        ends = float(segment.get("end", begins))
        mapped.append(
            {
                "utterance_id": f"utt_{index:03d}",
                "speaker": speaker,
                "started_at": _timestamp(start + timedelta(seconds=begins)),
                "ended_at": _timestamp(start + timedelta(seconds=max(ends, begins))),
                "text": text,
            }
        )
    if not mapped:
        raise LinguaWikiError(
            "adapter_found_no_speech",
            "every segment in this export is empty; there is no transcript to ingest",
            details=(ErrorDetail(field="segments", reason="no text"),),
        )
    return mapped


def adapt(
    payload: Mapping[str, Any],
    *,
    adapter: str,
    external_session_id: str | None = None,
    target_language: str | None = None,
    started_at: datetime | None = None,
    speaker: str = "learner",
    session: str | None = None,
    track: str | None = None,
    mode: str = "completed",
) -> dict[str, Any]:
    """Turn a provider's export into a package, or refuse to guess.

    A `lingua` payload passes through: it is already the contract. Anything else is
    translated by an adapter named here, and an unknown name is refused rather than
    guessed at, because a mis-guessed mapping produces a *valid* package full of wrong
    times, and nothing downstream could tell.
    """

    if adapter not in ADAPTERS:
        raise LinguaWikiError(
            "unknown_adapter",
            f"{adapter} is not an export shape this release reads; it knows "
            f"{list(ADAPTERS)}. A package written by hand is `lingua`.",
            details=(ErrorDetail(field="adapter", reason=adapter),),
        )
    if adapter == "lingua":
        return dict(payload)
    if external_session_id is None or target_language is None or started_at is None:
        raise LinguaWikiError(
            "adapter_context_required",
            "a transcription export carries no session identity, target language, or wall "
            "clock, so ingesting one needs all three supplied; without them the session "
            "could not be told apart from any other and its times would be meaningless",
            details=(ErrorDetail(field="external_session_id", reason="context missing"),),
        )
    start = aware_utc(started_at)
    utterances = _whisper_utterances(payload, start=start, speaker=speaker)
    duration = float(payload.get("duration") or 0.0)
    ended = start + timedelta(seconds=duration) if duration else None
    last = max(
        datetime.fromisoformat(entry["ended_at"].replace("Z", "+00:00")) for entry in utterances
    )
    return {
        "schema_name": "lingua.session.v1",
        "schema_version": 1,
        "package_id": f"pkg_{hashlib.sha256(external_session_id.encode()).hexdigest()[:16]}",
        "external_session_id": external_session_id,
        "session_id": session,
        "track_hint": track,
        "target_language": target_language,
        "mode": mode,
        "started_at": _timestamp(start),
        "ended_at": _timestamp(max(ended, last) if ended else last),
        "learning_targets": [],
        "transcript_layers": [{"kind": "raw", "derived_from": None, "utterances": utterances}],
        "events": [],
        "artifacts": [],
    }


def validate(
    paths: WorkspacePaths,
    *,
    package: Mapping[str, Any],
    track: str | None = None,
    clock: Clock | None = None,
) -> ValidationReport:
    """Say what would happen if this package were ingested, without ingesting it.

    Reviewing comes before ingesting for a plain reason: a package is somebody else's
    account of a learner's session, and the learner is entitled to read it first. So this
    reports the size of what it holds, whether the workspace already has it, what would be
    kept of their words -- and every problem at once, rather than the first one.
    """

    from linguawiki.contracts import SessionPackage

    problems: list[str] = []
    warnings: list[str] = []
    schema_name = package.get("schema_name")
    schema_version = package.get("schema_version")
    if schema_name != "lingua.session.v1" or schema_version != 1:
        return ValidationReport(
            valid=False,
            problems=(
                f"this release reads lingua.session.v1 version 1; the file declares "
                f"{schema_name} version {schema_version}",
            ),
        )
    try:
        validated = validated_contract(
            SessionPackage, package, code="invalid_session_package", subject="session package"
        )
    except LinguaWikiError as failure:
        return ValidationReport(
            valid=False,
            package_id=str(package.get("package_id") or "") or None,
            problems=tuple(f"{detail.field}: {detail.reason}" for detail in failure.payload.details)
            or (failure.payload.message,),
        )
    package_hash = session_service.canonical_hash(validated.model_dump(mode="json"))
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        if validated.track_hint is not None and str(validated.track_hint) != track_id:
            problems.append(
                f"the package names track {validated.track_hint} and this workspace would "
                f"ingest it into {track_id}; an external recording belongs to one learner"
            )
        record = learner_service.track_context(database, track_id)
        preferences = record.preferences if isinstance(record.preferences, dict) else {}
        retention = transcript_service.retention_policy(preferences)
        existing = database.one(
            "SELECT ingestion_id FROM session_packages WHERE package_hash = ?", [package_hash]
        )
        duplicate = existing is not None
        registered = {
            str(artifact_id)
            for (artifact_id,) in database.query(
                "SELECT artifact_id FROM artifacts WHERE track_id = ? AND purged_at IS NULL "
                "AND retained",
                [track_id],
            )
        }
    declared_audio = {
        str(artifact.artifact_id) for artifact in validated.artifacts if artifact.retained
    }
    for event in validated.events:
        payload = event.payload
        status = getattr(payload, "status", None)
        if status is None:
            continue
        artifact_id = getattr(payload, "audio_artifact_id", None)
        named = None if artifact_id is None else str(artifact_id)
        try:
            # A package's pronunciation event names no dimension -- the contract has no
            # field for one -- so only the status rule can be checked here. The dimension
            # rule is enforced where a dimension actually exists: `transcript
            # pronunciation`, and the CHECK on the table behind it.
            transcript_policy.assert_acoustic_claim_has_audio(
                status=str(status),
                dimension="intelligibility",
                basis="audio" if named else "transcript",
                reference=f"event {event.event_id}",
            )
        except LinguaWikiError as failure:
            problems.append(failure.payload.message)
            continue
        if named and named not in declared_audio and named not in registered:
            problems.append(
                f"event {event.event_id} confirms pronunciation from {named}, which this "
                "package does not carry as retained audio and this workspace does not hold; "
                "a confirmed claim nobody can check is worse than an uncertain one"
            )
    if duplicate:
        warnings.append(
            "this exact package was already ingested; ingesting it again stages nothing, "
            "which is how a checkpoint export and the completed export of the same call "
            "are meant to behave"
        )
    if retention != "full":
        warnings.append(
            f"this track keeps the learner's words as {retention}; the transcript will be "
            "stored under that rule"
        )
    if not any(layer.kind == "reviewed-hearing" for layer in validated.transcript_layers):
        warnings.append(
            "no reviewed-hearing layer: nobody has listened to this again, so every word in "
            "it is the transcription's claim rather than a person's"
        )
    return ValidationReport(
        valid=not problems,
        package_id=str(validated.package_id),
        external_session_id=validated.external_session_id,
        mode=validated.mode,
        track_id=track_id,
        session_id=None if validated.session_id is None else str(validated.session_id),
        layers=tuple(layer.kind for layer in validated.transcript_layers),
        utterances=sum(len(layer.utterances) for layer in validated.transcript_layers),
        events=len(validated.events),
        artifacts=len(validated.artifacts),
        duplicate=duplicate,
        retention_policy=retention,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def ingest(
    paths: WorkspacePaths,
    *,
    package: Mapping[str, Any],
    session: str | None = None,
    track: str | None = None,
    file_sha256: str | None = None,
    producer: str | None = None,
    clock: Clock | None = None,
    command: str = "speaking.ingest",
) -> SpokenIngestReport:
    """Take a spoken session in: stage what it claims, store what was said.

    Two writes rather than one, deliberately. Staging is the session engine's business and
    the transcript is this stage's, and each is idempotent on its own content -- so a
    re-ingest stages nothing and imports nothing, and an interruption between them leaves
    a package that can simply be ingested again.

    Nothing here credits the learner with anything. A package becomes part of their model
    at the same boundary as everything else: the session close.
    """

    active_clock = clock or SystemClock()
    staged = session_service.ingest_package(
        paths,
        package=package,
        session=session,
        track=track,
        file_sha256=file_sha256,
        producer=producer,
        clock=active_clock,
        command=command,
    )
    imported = transcript_service.import_package(
        paths,
        package=package,
        ingestion_id=staged.ingestion_id,
        clock=active_clock,
        command=command,
    )
    return SpokenIngestReport(
        ingestion_id=staged.ingestion_id,
        package_id=staged.package_id,
        session_id=staged.session_id,
        track_id=staged.track_id,
        external_session_id=staged.external_session_id,
        mode=staged.mode,
        staged_events=staged.staged_events,
        skipped_events=staged.skipped_events,
        imported_utterances=imported.imported,
        skipped_utterances=imported.skipped,
        duplicate=staged.duplicate,
        audio_available=staged.audio_available,
        retention_policy=staged.retention_policy,
        warnings=tuple(dict.fromkeys((*staged.warnings, *imported.warnings))),
    )


__all__ = [
    "ADAPTERS",
    "SpokenIngestReport",
    "ValidationReport",
    "adapt",
    "ingest",
    "scaffold",
    "validate",
]
