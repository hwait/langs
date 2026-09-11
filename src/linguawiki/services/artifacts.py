"""Files that live outside the database, and what happens when one goes away.

An artifact is a recording, a downloaded transcript, a scan: something too large, too
private, or too much somebody else's to sit in DuckDB or in Git. The row records that a
file exists, what it is, and whether it is still there. Never its bytes.

Three rules, and the third is the one the stage turns on:

- **the path is relative and inside the workspace.** An absolute path ties the row to one
  machine; a path with `..` reaches outside the workspace entirely. Both are refused by
  the schema and again here, where the message can explain.
- **identity is the content hash.** Registering the same file twice is one artifact, and
  a file whose bytes have changed under a registered hash is *reported*, not silently
  re-hashed: the whole point of recording the hash is to notice that.
- **purge leaves a tombstone, and invalidates exactly what depended on the audio.** A
  confirmed pronunciation claim needed the sound to be made and needs it to go on
  standing. The language evidence from the same utterance does not: what the learner
  *said* was established by the transcript, which is still there. Deleting both would
  punish the learner for exercising a retention choice.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from linguawiki import transcripts as transcript_policy
from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import ArtifactId, EventId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths, assert_within
from linguawiki.services import learners as learner_service

#: Where an artifact may live inside a workspace. Both are already private directories
#: that Git ignores, which is the point: an artifact cannot be registered somewhere the
#: privacy rules do not already cover.
ARTIFACT_ROOTS: tuple[str, ...] = ("artifacts", "imports")

ARTIFACT_KINDS: tuple[str, ...] = ("audio", "transcript", "other")
ARTIFACT_ORIGINS: tuple[str, ...] = (
    "learner-recording",
    "provider-export",
    "source-download",
    "other",
)


class ArtifactReport(ContractModel):
    artifact_id: str
    track_id: str
    kind: str
    relative_path: str
    media_type: str | None = None
    byte_size: int | None = None
    sha256: str
    origin: str
    rights: str
    source_id: str | None = None
    retained: bool = True
    #: Present exactly when the file is gone, with the reason it went.
    purged_at: str | None = None
    purge_reason: str | None = None
    #: Whether the file is where the row says it is, when that was checked.
    present: bool | None = None
    hash_matches: bool | None = None
    created_at: str
    warnings: tuple[str, ...] = ()


class ArtifactListing(ContractModel):
    track_id: str
    total: int
    artifacts: tuple[ArtifactReport, ...] = ()
    warnings: tuple[str, ...] = ()


class VerificationReport(ContractModel):
    track_id: str
    checked: int
    present: int
    missing: tuple[str, ...] = ()
    altered: tuple[str, ...] = ()
    purged: tuple[str, ...] = ()
    ok: bool = True
    warnings: tuple[str, ...] = ()


class PurgeReport(ContractModel):
    artifact_id: str
    relative_path: str
    reason: str
    file_removed: bool
    #: Acoustic claims that rested on this audio and no longer stand. They are marked,
    #: not deleted: a learner who was told their vowel was wrong deserves to see that
    #: the evidence for it is gone.
    invalidated_observations: tuple[str, ...] = ()
    #: What survived, and why -- the counterpart of the above, and the reason a purge is
    #: safe to offer at all.
    surviving_language_evidence: int = 0
    purged_at: str
    warnings: tuple[str, ...] = ()


def _assert_safe_relative(relative_path: str) -> PurePosixPath:
    """Refuse a path that is not inside one of the workspace's private roots."""

    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise LinguaWikiError(
            "unsafe_artifact_path",
            f"{relative_path} must be a relative path inside the workspace; an absolute "
            "path ties the record to one machine and '..' reaches outside it entirely",
            details=(ErrorDetail(field="relative_path", reason=relative_path),),
        )
    if not path.parts or path.parts[0] not in ARTIFACT_ROOTS:
        raise LinguaWikiError(
            "artifact_outside_private_roots",
            f"{relative_path} is outside {' and '.join(ARTIFACT_ROOTS)}/, which are the "
            "directories the workspace already keeps out of Git; a recording registered "
            "anywhere else would be a recording Git is willing to commit",
            details=(
                ErrorDetail(field="relative_path", reason=path.parts[0] if path.parts else ""),
            ),
        )
    return path


def file_digest(path: Path) -> str:
    """The SHA-256 of a file, read in chunks because a recording can be large."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_artifact(database: Database, *, artifact_id: str) -> ArtifactReport:
    row = database.one(
        "SELECT artifact_id, track_id, kind, relative_path, media_type, byte_size, sha256, "
        "origin, rights, source_id, retained, purged_at, purge_reason, created_at "
        "FROM artifacts WHERE artifact_id = ?",
        [artifact_id],
    )
    if row is None:
        raise LinguaWikiError(
            "artifact_not_found",
            f"no artifact {artifact_id} in this workspace",
            details=(ErrorDetail(field="artifact", reason="unknown artifact"),),
        )
    return ArtifactReport(
        artifact_id=str(row[0]),
        track_id=str(row[1]),
        kind=str(row[2]),
        relative_path=str(row[3]),
        media_type=None if row[4] is None else str(row[4]),
        byte_size=None if row[5] is None else int(row[5]),
        sha256=str(row[6]),
        origin=str(row[7]),
        rights=str(row[8]),
        source_id=None if row[9] is None else str(row[9]),
        retained=bool(row[10]),
        purged_at=None if row[11] is None else aware_utc(row[11]).isoformat(),
        purge_reason=None if row[12] is None else str(row[12]),
        created_at=aware_utc(row[13]).isoformat(),
    )


def register(
    paths: WorkspacePaths,
    *,
    relative_path: str,
    kind: str = "audio",
    media_type: str | None = None,
    origin: str = "learner-recording",
    rights: str = "metadata-only",
    source: str | None = None,
    retained: bool | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "artifact.register",
) -> ArtifactReport:
    """Record that a file exists, without moving it or reading it into the database.

    Retention defaults to the track's own consent rather than to `True`: a learner who
    has not agreed to audio being kept has a recording registered as *not retained*,
    which records that the file existed and was not kept. That is what makes a later
    absence explicable instead of merely unexplained.
    """

    from linguawiki import sources as source_policy

    source_policy.assert_known(
        kind, vocabulary=ARTIFACT_KINDS, field="kind", code="unknown_artifact_kind"
    )
    source_policy.assert_known(
        origin, vocabulary=ARTIFACT_ORIGINS, field="origin", code="unknown_artifact_origin"
    )
    source_policy.assert_known(
        rights,
        vocabulary=source_policy.RIGHTS_CLASSES,
        field="rights",
        code="unknown_rights_class",
    )
    safe = _assert_safe_relative(relative_path)
    active_clock = clock or SystemClock()
    absolute = assert_within(paths.root / str(safe), paths.root, purpose="artifact")
    if not absolute.is_file():
        raise LinguaWikiError(
            "artifact_file_missing",
            f"no file at {relative_path}; register an artifact that is actually there, "
            "so its hash records what was registered",
            details=(ErrorDetail(field="relative_path", reason="missing file"),),
        )
    digest = file_digest(absolute)
    size = absolute.stat().st_size
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        source_id = None
        if source is not None:
            from linguawiki.services import sources as source_service

            source_id = source_service._resolve_source(database, source, track_id=track_id)
        existing = database.one(
            "SELECT artifact_id FROM artifacts WHERE track_id = ? AND sha256 = ?",
            [track_id, digest],
        )
        if existing is not None:
            # Same bytes, same artifact. Registering a file twice is one file, however
            # many paths it arrived under.
            return _read_artifact(database, artifact_id=str(existing[0])).model_copy(
                update={
                    "warnings": (
                        "this file's content is already registered; the existing artifact "
                        "is returned and nothing was changed",
                    )
                }
            )
        preferences = record.preferences if isinstance(record.preferences, dict) else {}
        consented = bool(preferences.get("audio_retention_consent"))
        resolved_retained = (
            retained if retained is not None else (consented if kind == "audio" else True)
        )
        artifact_id = str(ArtifactId.new())
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO artifacts (artifact_id, track_id, kind, relative_path, "
                "media_type, byte_size, sha256, origin, rights, source_id, retained, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    artifact_id,
                    track_id,
                    kind,
                    str(safe),
                    media_type,
                    size,
                    digest,
                    origin,
                    rights,
                    source_id,
                    resolved_retained,
                    naive_utc(now),
                    naive_utc(now),
                ],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                # The path is a private location, so the audit trail names the artifact
                # and its hash rather than where on disk the learner keeps it.
                affected_records_json=json.dumps([artifact_id]),
                after_summary=f"registered a {kind} artifact of {size} byte(s)",
            )
        report = _read_artifact(database, artifact_id=artifact_id)
    warnings: list[str] = []
    if kind == "audio" and not resolved_retained:
        warnings.append(
            "registered as not retained, because this track has not consented to keeping "
            "audio; the recording can be worked with now, and no pronunciation claim made "
            "from it will survive as confirmed"
        )
    return report.model_copy(update={"warnings": tuple(warnings)})


def verify(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    clock: Clock | None = None,
) -> VerificationReport:
    """Check that every registered file is where it says, with the bytes it says.

    Three answers, and they are different facts. *Missing* is a file that is gone without
    a tombstone -- somebody moved or deleted it outside the workspace, and the evidence
    resting on it is now unsupported without anything recording why. *Altered* is worse:
    the file is there and its bytes have changed, so a claim about what it contained is
    a claim about something that no longer exists. *Purged* is neither: it is the
    expected absence of a file the learner asked to remove.
    """

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        rows = database.query(
            "SELECT artifact_id, relative_path, sha256, purged_at FROM artifacts "
            "WHERE track_id = ? ORDER BY created_at",
            [track_id],
        )
    missing: list[str] = []
    altered: list[str] = []
    purged: list[str] = []
    present = 0
    for artifact_id, relative_path, digest, purged_at in rows:
        absolute = paths.root / str(relative_path)
        if purged_at is not None:
            purged.append(str(artifact_id))
            if absolute.is_file():
                # A purged artifact whose file is back is not a happy accident: the
                # tombstone says it was removed, and something has put it back.
                altered.append(f"{artifact_id} (purged, but the file is present again)")
            continue
        if not absolute.is_file():
            missing.append(str(artifact_id))
            continue
        present += 1
        if file_digest(absolute) != str(digest):
            altered.append(str(artifact_id))
    warnings: list[str] = []
    if missing:
        warnings.append(
            f"{len(missing)} registered file(s) are gone with no tombstone; `artifact purge` "
            "records a removal so the evidence that rested on it can be settled"
        )
    if altered:
        warnings.append(
            f"{len(altered)} file(s) no longer match the hash they were registered with, so "
            "any claim about what they contained is about something that no longer exists"
        )
    return VerificationReport(
        track_id=track_id,
        checked=len(rows),
        present=present,
        missing=tuple(missing),
        altered=tuple(altered),
        purged=tuple(purged),
        ok=not missing and not altered,
        warnings=tuple(warnings),
    )


def listing(
    paths: WorkspacePaths,
    *,
    kind: str | None = None,
    track: str | None = None,
    limit: int = 100,
    clock: Clock | None = None,
) -> ArtifactListing:
    """The track's registered files, newest first, with their tombstones."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        conditions = ["track_id = ?"]
        parameters: list[Any] = [track_id]
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(kind)
        where = " AND ".join(conditions)
        total = int(database.scalar(f"SELECT count(*) FROM artifacts WHERE {where}", parameters))
        identifiers = [
            str(row[0])
            for row in database.query(
                f"SELECT artifact_id FROM artifacts WHERE {where} ORDER BY created_at DESC LIMIT ?",
                [*parameters, limit],
            )
        ]
        artifacts = []
        for identity in identifiers:
            report = _read_artifact(database, artifact_id=identity)
            absolute = paths.root / report.relative_path
            artifacts.append(report.model_copy(update={"present": absolute.is_file()}))
        return ArtifactListing(track_id=track_id, total=total, artifacts=tuple(artifacts))


def purge(
    paths: WorkspacePaths,
    *,
    artifact: str,
    reason: str = "learner-request",
    remove_file: bool = True,
    dry_run: bool = False,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "artifact.purge",
) -> PurgeReport:
    """Remove a file and settle what depended on it.

    The invalidation is the careful part, and it is deliberately narrow. Losing the audio
    invalidates exactly the claims that needed the audio to be made: anything `confirmed`,
    and anything about prosody or native-likeness at any confidence. It does **not** touch
    what the learner said -- that was established by the transcript, which is still here.
    Purging a recording is a privacy choice, and a privacy choice that also deleted a
    month of language evidence would be one nobody could afford to make.

    `dry_run` reports exactly this without doing it, because the plan requires that the
    consequences are visible before the deletion, not after.
    """

    from linguawiki import sources as source_policy

    source_policy.assert_known(
        reason,
        vocabulary=transcript_policy.PURGE_REASONS,
        field="reason",
        code="unknown_purge_reason",
    )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        row = database.one(
            "SELECT artifact_id, track_id, relative_path, purged_at FROM artifacts "
            "WHERE artifact_id = ?",
            [artifact],
        )
        if row is None:
            raise LinguaWikiError(
                "artifact_not_found",
                f"no artifact {artifact} in this workspace",
                details=(ErrorDetail(field="artifact", reason="unknown artifact"),),
            )
        if str(row[1]) != track_id:
            raise LinguaWikiError(
                "artifact_out_of_scope",
                f"artifact {artifact} belongs to track {row[1]}, not this one",
                details=(ErrorDetail(field="artifact", reason=str(row[1])),),
            )
        artifact_id = str(row[0])
        relative_path = str(row[2])
        already_purged = row[3] is not None
        dependent = dependent_observations(database, artifact_id=artifact_id)
        surviving = int(
            database.scalar(
                "SELECT count(*) FROM evidence evidence "
                "JOIN attempts attempt ON attempt.attempt_id = evidence.attempt_id "
                "WHERE attempt.track_id = ?",
                [track_id],
            )
        )
        now = aware_utc(database.now())
        if dry_run:
            return PurgeReport(
                artifact_id=artifact_id,
                relative_path=relative_path,
                reason=reason,
                file_removed=False,
                invalidated_observations=tuple(entry[0] for entry in dependent),
                surviving_language_evidence=surviving,
                purged_at=now.isoformat(),
                warnings=(
                    "dry run: nothing was removed. Purging would invalidate "
                    f"{len(dependent)} acoustic claim(s) and leave {surviving} piece(s) of "
                    "language evidence untouched.",
                ),
            )
        if already_purged:
            return PurgeReport(
                artifact_id=artifact_id,
                relative_path=relative_path,
                reason=reason,
                file_removed=False,
                surviving_language_evidence=surviving,
                purged_at=aware_utc(row[3]).isoformat(),
                warnings=("this artifact was already purged; nothing was changed",),
            )
        absolute = assert_within(paths.root / relative_path, paths.root, purpose="artifact")
        removed = False
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE artifacts SET purged_at = ?, purge_reason = ?, retained = FALSE, "
                "updated_at = ? WHERE artifact_id = ?",
                [naive_utc(now), reason, naive_utc(now), artifact_id],
            )
            for observation_id, _ in dependent:
                transaction.execute(
                    "UPDATE pronunciation_observations SET invalidated_at = ?, "
                    "invalidation_reason = ? WHERE observation_id = ?",
                    [
                        naive_utc(now),
                        f"the audio it rested on was purged ({reason})",
                        observation_id,
                    ],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([artifact_id]),
                after_summary=(
                    f"purged ({reason}); {len(dependent)} acoustic claim(s) invalidated, "
                    "language evidence untouched"
                ),
            )
            migration_module.record_domain_event(
                transaction,
                event_type="artifact.purged",
                aggregate_type="artifact",
                aggregate_id=artifact_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {"reason": reason, "invalidated": len(dependent)}, sort_keys=True
                ),
            )
            # Deleted before the commit, deliberately. Either order can fail partway,
            # so the question is which wreckage is safer, and it is not symmetric:
            #
            # - commit first, then delete: a failure leaves a tombstone saying the
            #   recording is gone while the file is still on disk. The learner reads
            #   "purged" and believes a privacy request was honoured that was not.
            # - delete first, then commit: a failure leaves the file gone with no
            #   tombstone. The privacy request *was* honoured, the record is merely
            #   incomplete, `artifact verify` reports it as missing, and running the
            #   purge again completes it.
            #
            # Never claim a privacy action that did not happen. An incomplete record of
            # a deletion that did happen is recoverable; the reverse is a lie.
            if remove_file and absolute.is_file():
                absolute.unlink()
                removed = True
    return PurgeReport(
        artifact_id=artifact_id,
        relative_path=relative_path,
        reason=reason,
        file_removed=removed,
        invalidated_observations=tuple(entry[0] for entry in dependent),
        surviving_language_evidence=surviving,
        purged_at=now.isoformat(),
        warnings=(
            (
                f"{len(dependent)} acoustic claim(s) no longer stand and are marked as "
                "such; they are kept on the record rather than deleted, because a learner "
                "told their pronunciation was wrong deserves to see that the evidence is gone",
            )
            if dependent
            else ()
        ),
    )


def dependent_observations(database: Database, *, artifact_id: str) -> list[tuple[str, str]]:
    """The acoustic claims that needed this audio, by the transcript policy's own rule."""

    return [
        (str(observation_id), str(dimension))
        for observation_id, status, dimension in database.query(
            "SELECT observation_id, status, dimension FROM pronunciation_observations "
            "WHERE audio_artifact_id = ? AND invalidated_at IS NULL ORDER BY observation_id",
            [artifact_id],
        )
        if transcript_policy.invalidated_by_purge(status=str(status), dimension=str(dimension))
    ]
