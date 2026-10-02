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
from collections.abc import Mapping
from dataclasses import dataclass, replace
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

#: The mastery stage at which a pronunciation target stops being one the learner is
#: working on. A claim about a stable item is history; a claim about anything below it is
#: evidence for work in progress, and the recording behind it outranks a retention window.
ACHIEVED_STAGE = "stable"

#: Every problem `declared_audio_problems` reports is the same kind: a package naming audio
#: this workspace cannot let a claim rest on. Exported so a caller can attach the code
#: without reading the prose to work out what went wrong.
AUDIO_PROBLEM_CODE = "package_audio_unavailable"

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
    #: Set when this file is a selected excerpt of another recording. The retention rules
    #: turn on it: a clip supporting an unfinished target is kept past its window, and a
    #: whole conversation is not.
    clip_of_artifact_id: str | None = None
    clip_starts_at_ms: int | None = None
    clip_ends_at_ms: int | None = None
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
    #: Registered paths that now lead outside the workspace. Worse than altered: a file
    #: somewhere else would be standing in for the learner's recording.
    escaped: tuple[str, ...] = ()
    #: There, and unreadable. Neither intact nor altered, and not a reason to fail the
    #: command whose job is to report on these files.
    unreadable: tuple[str, ...] = ()
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
    #: Assessment results a judge reached by listening to this recording. Marked
    #: invalidated, never deleted, and the estimates they shaped are rebuilt.
    invalidated_results: tuple[str, ...] = ()
    #: Recordings a judge had not yet heard, withdrawn with their tasks skipped.
    withdrawn_submissions: tuple[str, ...] = ()
    #: Verdicts a judge had delivered for those recordings while their run was paused,
    #: voided with them: they can never be applied, and the judge's work is named rather
    #: than silently dropped.
    voided_verdicts: tuple[str, ...] = ()
    #: What survived, and why -- the counterpart of the above, and the reason a purge is
    #: safe to offer at all.
    surviving_language_evidence: int = 0
    purged_at: str
    warnings: tuple[str, ...] = ()


def _assert_safe_relative(
    relative_path: str, *, roots: tuple[str, ...] = ARTIFACT_ROOTS
) -> PurePosixPath:
    """Refuse a path that is not inside one of the workspace's private roots."""

    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise LinguaWikiError(
            "unsafe_artifact_path",
            f"{relative_path} must be a relative path inside the workspace; an absolute "
            "path ties the record to one machine and '..' reaches outside it entirely",
            details=(ErrorDetail(field="relative_path", reason=relative_path),),
        )
    if not path.parts or path.parts[0] not in roots:
        raise LinguaWikiError(
            "artifact_outside_private_roots",
            f"{relative_path} is outside {' and '.join(roots)}/, which are the "
            "directories the workspace already keeps out of Git; a recording registered "
            "anywhere else would be a recording Git is willing to commit",
            details=(
                ErrorDetail(field="relative_path", reason=path.parts[0] if path.parts else ""),
            ),
        )
    return path


@dataclass(frozen=True, slots=True)
class PathOwner:
    """The live row that owns a file, and what it expects to be there."""

    artifact_id: str
    track_id: str
    sha256: str


def path_owners(database: Database) -> dict[str, PathOwner]:
    """Who owns each registered file, workspace-wide.

    One source of truth, because the two callers disagreed twice. A *file* is
    workspace-global while the rows describing it are per track, so scoping this to a track
    let two learners register one file and either one's purge delete the other's recording;
    and then the writer was fixed and the package preflight was not, so a package could
    validate, register its first recording, and be refused on its second.

    Only live, retained rows own anything. A tombstone records where a recording *used to
    be*, and a declined row records that bytes existed and were deleted -- neither is a claim
    on the filename, so reusing it for a new recording is the learner's to do.
    """

    return {
        str(relative_path): PathOwner(
            artifact_id=str(artifact_id), track_id=str(track_id), sha256=str(digest)
        )
        for artifact_id, track_id, relative_path, digest in database.query(
            "SELECT artifact_id, track_id, relative_path, sha256 FROM artifacts "
            "WHERE purged_at IS NULL AND retained"
        )
    }


def active_paths(database: Database) -> dict[str, str]:
    """The bytes each owned path is expected to hold, for the tombstone predicate."""

    return {path: owner.sha256 for path, owner in path_owners(database).items()}


def tombstone_path_is_excused(root: Path, relative_path: str, *, owned: Mapping[str, str]) -> bool:
    """Whether a tombstone's old path is explained by a live row rather than resurrection.

    Both conditions are needed and each was missing once: a live row has to own the path, and
    what is *there* has to be that row's recording. Ownership alone let the purged recording
    be restored over the new one with the privacy audit staying green.
    """

    expected = owned.get(relative_path)
    if expected is None:
        return False
    present = contained_file(root, relative_path)
    return present is not None and digest_or_none(present) == expected


def _path_exists(path: Path) -> bool:
    """Whether something is at this path, without letting the question raise.

    `Path.exists` propagates a permission failure on a parent directory, which is the
    difference between "there is no recording here" and "this workspace cannot look".
    """

    try:
        return path.exists()
    except OSError:
        # Cannot tell. Treated as "something is there", because the alternative is
        # reporting a file as absent on the strength of not having been allowed to look.
        return True


#: What a stored path turns out to be. `contained_file` answers the yes/no question every
#: reader needs; this says *which* no, because the three are different facts about the
#: learner's recording and calling an unreadable file "escaping" sends somebody to fix the
#: wrong thing.
PATH_PRESENT = "present"
PATH_ABSENT = "absent"
PATH_ESCAPED = "escaped"
PATH_UNREADABLE = "unreadable"


def classify_path(
    root: Path, relative_path: str, *, roots: tuple[str, ...] = ARTIFACT_ROOTS
) -> tuple[str, Path | None]:
    """Say what is at a stored path: present, absent, escaping, or unreadable.

    `roots` is the private roots the path must stay under. Only the capture service widens
    it, to read the staging root that `register` deliberately refuses.
    """

    try:
        safe = _assert_safe_relative(relative_path, roots=roots)
        absolute = assert_within(root / str(safe), root, purpose="artifact")
    except LinguaWikiError as failure:
        # Two different facts, and calling one by the other's name sends somebody to look
        # for a symlink out of the workspace that does not exist. `assert_within` refuses a
        # path it cannot *resolve* -- a symlink loop raises `RuntimeError` from
        # `Path.resolve` under Python 3.12 -- separately from one that resolves outside.
        if failure.payload.code == "unresolvable_target":
            return PATH_UNREADABLE, None
        return PATH_ESCAPED, None
    try:
        if absolute.is_file():
            return PATH_PRESENT, absolute
    except OSError:
        # `Path.is_file` swallows only the errno values that mean "not there". A permission
        # failure on the path or a parent propagates, and this is where that becomes an
        # answer rather than an exception.
        return PATH_UNREADABLE, None
    return (PATH_UNREADABLE, None) if _path_exists(absolute) else (PATH_ABSENT, None)


def contained_file(root: Path, relative_path: str) -> Path | None:
    """The file a registered row names, if it really is a file inside this workspace.

    One helper for every read of a registered path, because "join the root and hash it" was
    written out six times and each copy trusted the path differently. Registration resolved
    it and the readers did not -- so replacing a registered file with a symlink to matching
    bytes *outside* the workspace made `verify` report it present, made a package's audio
    available, made the listing disagree with `verify`, and made recovery treat the outside
    target as canonical and delete a genuine in-workspace copy.

    `None` means "not a file this workspace holds". Callers that have to tell the three kinds
    of no apart use `classify_path`.
    """

    state, absolute = classify_path(root, relative_path)
    return absolute if state == PATH_PRESENT else None


def digest_or_none(path: Path) -> str | None:
    """The file's hash, or nothing if it cannot be read.

    A recording that is unreadable, or that vanished between the check and the open, is one
    no claim can rest on -- which is an answer, not an error to propagate. Raising turned a
    deliberately accepted no-op retry into a failure.
    """

    try:
        return file_digest(path)
    except OSError:
        return None


def _assert_clip_is_a_clip(
    *, clip_of: str | None, kind: str, clip_starts_at_ms: int | None, clip_ends_at_ms: int | None
) -> None:
    """Refuse a "clip" that is not an excerpt of anything in particular.

    Clip provenance buys longer retention than a whole recording gets, so what counts as a
    clip has to be more than a pointer: an entire second conversation, registered as a
    "clip" of the first with no offsets at all, was kept past its window on the strength of
    naming another artifact. A clip is audio, of audio, over a window that exists.
    """

    offsets = (clip_starts_at_ms, clip_ends_at_ms)
    if clip_of is None:
        if any(offset is not None for offset in offsets):
            raise LinguaWikiError(
                "clip_source_required",
                "a clip is an excerpt *of* something; without the recording it came from, "
                "its offsets describe a position in nothing",
                details=(ErrorDetail(field="clip_of", reason="missing"),),
            )
        return
    if kind != "audio":
        raise LinguaWikiError(
            "clip_is_not_audio",
            f"a selected clip is an excerpt of a recording, and this is {kind}",
            details=(ErrorDetail(field="kind", reason=kind),),
        )
    if any(offset is None for offset in offsets):
        raise LinguaWikiError(
            "clip_window_required",
            "a clip says which part of the recording it is: both --from-ms and --to-ms are "
            "needed, because a clip with no window is the whole thing wearing a label that "
            "earns it longer retention",
            details=(
                ErrorDetail(field="clip_starts_at_ms", reason=str(clip_starts_at_ms)),
                ErrorDetail(field="clip_ends_at_ms", reason=str(clip_ends_at_ms)),
            ),
        )
    assert clip_starts_at_ms is not None and clip_ends_at_ms is not None
    if clip_starts_at_ms < 0:
        raise LinguaWikiError(
            "invalid_clip_window",
            f"a clip cannot start {abs(clip_starts_at_ms)}ms before the recording does",
            details=(ErrorDetail(field="clip_starts_at_ms", reason=str(clip_starts_at_ms)),),
        )
    if clip_ends_at_ms <= clip_starts_at_ms:
        raise LinguaWikiError(
            "invalid_clip_window",
            f"a clip from {clip_starts_at_ms}ms to {clip_ends_at_ms}ms has no duration",
            details=(ErrorDetail(field="clip_ends_at_ms", reason=str(clip_ends_at_ms)),),
        )


def _remove_file(absolute: Path, *, artifact_id: str, reason: str) -> None:
    """Delete the bytes, and refuse to pretend if they will not go.

    A row that says a recording is gone while the recording is on disk is worse than an
    error: the learner reads it as a privacy request honoured. So a deletion that fails
    fails loudly, and the caller's transaction takes the row with it.
    """

    try:
        if absolute.is_file():
            absolute.unlink()
    except OSError as failure:
        raise LinguaWikiError(
            "artifact_file_not_removed",
            f"{artifact_id} is being recorded as {reason}, and its file could not be "
            f"deleted: {failure}. Nothing was recorded, because a record saying the "
            "recording is gone while it is still here is a privacy claim that is not true.",
            details=(ErrorDetail(field="relative_path", reason=str(failure)),),
        ) from failure


def assert_safe_relative(relative_path: str) -> PurePosixPath:
    """Public form of the path rule, for callers outside this module."""

    return _assert_safe_relative(relative_path)


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
        "origin, rights, source_id, retained, purged_at, purge_reason, created_at, "
        "clip_of_artifact_id, clip_starts_at_ms, clip_ends_at_ms "
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
        clip_of_artifact_id=None if row[14] is None else str(row[14]),
        clip_starts_at_ms=None if row[15] is None else int(row[15]),
        clip_ends_at_ms=None if row[16] is None else int(row[16]),
    )


@dataclass(frozen=True, slots=True)
class RegistrationPlan:
    """Everything `register` decided before writing, so a caller can write it in its own
    transaction.

    `existing` is set when these bytes are already registered on this track: the plan then
    describes a duplicate rather than a new row, and `write_registration` refuses it --
    only `register` itself knows how to reconcile a second copy of a recording.
    """

    relative_path: str
    absolute: Path
    kind: str
    media_type: str | None
    origin: str
    rights: str
    external_id: str | None
    digest: str
    size: int
    track_id: str
    source_id: str | None
    clip_of_artifact_id: str | None
    clip_starts_at_ms: int | None
    clip_ends_at_ms: int | None
    retained: bool
    #: `(artifact_id, retained, external_id)` of the row already holding these bytes.
    existing: tuple[str, bool, str | None] | None = None


def plan_registration(
    database: Database,
    root: Path,
    *,
    relative_path: str,
    kind: str = "audio",
    media_type: str | None = None,
    origin: str = "learner-recording",
    rights: str = "metadata-only",
    source: str | None = None,
    retained: bool | None = None,
    external_id: str | None = None,
    expected_sha256: str | None = None,
    clip_of: str | None = None,
    clip_starts_at_ms: int | None = None,
    clip_ends_at_ms: int | None = None,
    track: str | None = None,
) -> RegistrationPlan:
    """Decide a registration inside the caller's connection, refusing everything it would.

    A read: it opens the file to hash it and reads the database, and writes neither. That
    is what lets a caller holding the writer -- a capture binding its recording to a served
    task -- run every refusal before its first write and then commit the registration
    beside its own rows, which a `register` opening its own writer could not do.
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
    _assert_clip_is_a_clip(
        clip_of=clip_of,
        kind=kind,
        clip_starts_at_ms=clip_starts_at_ms,
        clip_ends_at_ms=clip_ends_at_ms,
    )
    safe = _assert_safe_relative(relative_path)
    # Classified *before* the bare containment call, so a path that cannot be resolved at
    # all -- a symlink loop, say -- is reported as the unreadable file it is rather than as
    # a containment failure, and `assert_within` below never meets one.
    state, absolute = classify_path(root, relative_path)
    if state != PATH_PRESENT:
        # Absent and unreadable are different facts, and the operator's next move differs:
        # find the file, or fix its permissions.
        if state == PATH_UNREADABLE:
            raise LinguaWikiError(
                "artifact_unreadable",
                f"{relative_path} is there and cannot be read, so there is no hash to "
                "record and nothing to register; a recording no command can read is one no "
                "claim can rest on",
                details=(ErrorDetail(field="relative_path", reason=relative_path),),
            )
        raise LinguaWikiError(
            "artifact_file_missing",
            f"no file at {relative_path}; register an artifact that is actually there, "
            "so its hash records what was registered",
            details=(ErrorDetail(field="relative_path", reason="missing file"),),
        )
    assert absolute is not None
    digest = digest_or_none(absolute)
    if digest is None:
        raise LinguaWikiError(
            "artifact_unreadable",
            f"{relative_path} is there and cannot be read, so there is no hash to record "
            "and nothing to register; a recording no command can read is one no claim can "
            "rest on",
            details=(ErrorDetail(field="relative_path", reason=relative_path),),
        )
    if expected_sha256 is not None and digest != expected_sha256:
        raise LinguaWikiError(
            "artifact_hash_mismatch",
            f"{relative_path} is not the file that was expected: it hashes to "
            f"{digest[:12]}... and {expected_sha256[:12]}... was named. Registering it "
            "anyway would attach claims about one recording to another.",
            details=(
                ErrorDetail(field="sha256", reason=digest),
                ErrorDetail(field="expected_sha256", reason=expected_sha256),
            ),
        )
    try:
        size = absolute.stat().st_size
    except OSError as failure:
        # The file was there a moment ago and is not now, or has become inaccessible.
        # That is the same answer as an unreadable file, by the same rule.
        raise LinguaWikiError(
            "artifact_unreadable",
            f"{relative_path} could not be read through: {failure}. A recording no command "
            "can read is one no claim can rest on.",
            details=(ErrorDetail(field="relative_path", reason=relative_path),),
        ) from failure
    track_id = learner_service.resolve_track(database, track)
    record = learner_service.track_context(database, track_id)
    source_id = None
    if source is not None:
        from linguawiki.services import sources as source_service

        source_id = source_service._resolve_source(database, source, track_id=track_id)
    clip_of_artifact_id = None
    if clip_of is not None:
        # Resolved inside the track, and required to be audio: a clip of a transcript
        # is not the thing this concept is for.
        row = database.one(
            "SELECT artifact_id, kind FROM artifacts WHERE track_id = ? "
            "AND (artifact_id = ? OR external_id = ?)",
            [track_id, clip_of, clip_of],
        )
        if row is None:
            raise LinguaWikiError(
                "clip_source_not_found",
                f"this track holds no recording {clip_of} for a clip to come from",
                details=(ErrorDetail(field="clip_of", reason=clip_of),),
            )
        if str(row[1]) != "audio":
            raise LinguaWikiError(
                "clip_source_is_not_audio",
                f"{row[0]} is {row[1]}; a selected clip is an excerpt of a recording",
                details=(ErrorDetail(field="clip_of", reason=str(row[1])),),
            )
        clip_of_artifact_id = str(row[0])
        if (
            str(
                database.scalar(
                    "SELECT sha256 FROM artifacts WHERE artifact_id = ?", [clip_of_artifact_id]
                )
            )
            == digest
        ):
            raise LinguaWikiError(
                "clip_of_itself",
                "these are the bytes of the recording this clip says it came from, so "
                "it is not an excerpt of anything: it is the whole thing with a label "
                "that would earn it longer retention",
                details=(ErrorDetail(field="clip_of", reason=clip_of),),
            )
    preferences = record.preferences if isinstance(record.preferences, dict) else {}
    consented = bool(preferences.get("audio_retention_consent"))
    resolved_retained = (
        retained if retained is not None else (consented if kind == "audio" else True)
    )
    owner = path_owners(database).get(str(safe))
    if owner is not None and owner.track_id != track_id:
        raise LinguaWikiError(
            "artifact_path_owned_elsewhere",
            f"{relative_path} is already registered on another track as "
            f"{owner.artifact_id}. One file cannot belong to two learners: a purge or a "
            "retention sweep by either would delete the other's recording. Give this "
            "learner their own copy under a path of their own.",
            details=(
                ErrorDetail(field="relative_path", reason=relative_path),
                ErrorDetail(field="artifact", reason=owner.artifact_id),
            ),
        )
    if owner is not None and owner.sha256 != digest:
        # The path is registered and its bytes have changed. Making a second row for it
        # left two artifacts claiming one file, with `verify` reporting the first as
        # altered and nothing explaining the second. An alteration is a fact about the
        # recording that is already registered, so it is reported as one.
        raise LinguaWikiError(
            "artifact_altered",
            f"{owner.artifact_id} is already registered at {relative_path}, and the file "
            f"there no longer matches it: it hashes to {digest[:12]}... and the record "
            f"says {owner.sha256[:12]}.... Registering it again would leave two "
            "rows claiming one file. `artifact verify` reports the alteration; purge the "
            "artifact if the recording is genuinely gone.",
            details=(
                ErrorDetail(field="relative_path", reason=relative_path),
                ErrorDetail(field="sha256", reason=digest),
            ),
        )
    tombstone = database.one(
        "SELECT artifact_id, purge_reason FROM artifacts WHERE track_id = ? "
        "AND sha256 = ? AND purged_at IS NOT NULL",
        [track_id, digest],
    )
    if tombstone is not None:
        # Refused *before* the not-retained branch below, which would otherwise delete
        # the file just offered: a purged row is non-retained, so resurrection looked
        # exactly like "the learner declined this" and the recording was destroyed.
        raise LinguaWikiError(
            "artifact_purged",
            f"{tombstone[0]} is this recording, purged at the learner's request "
            f"({tombstone[1]}). The workspace does not un-delete a recording, and it "
            "holds one row per recording, so there is nowhere for a second life to go. "
            "The file just offered was left where it is.",
            details=(
                ErrorDetail(field="artifact", reason=str(tombstone[0])),
                ErrorDetail(field="purge_reason", reason=str(tombstone[1])),
            ),
        )
    plan = RegistrationPlan(
        relative_path=str(safe),
        absolute=absolute,
        kind=kind,
        media_type=media_type,
        origin=origin,
        rights=rights,
        external_id=external_id,
        digest=digest,
        size=size,
        track_id=track_id,
        source_id=source_id,
        clip_of_artifact_id=clip_of_artifact_id,
        clip_starts_at_ms=clip_starts_at_ms,
        clip_ends_at_ms=clip_ends_at_ms,
        retained=resolved_retained,
    )
    existing = database.one(
        "SELECT artifact_id, retained, external_id FROM artifacts "
        "WHERE track_id = ? AND sha256 = ? AND purged_at IS NULL",
        [track_id, digest],
    )
    if existing is None:
        return plan
    # Same bytes, same artifact. Registering a file twice is one file, however many paths
    # it arrived under -- but the *other* inputs are not decoration, and returning early
    # used to discard both of them.
    held_id, held_retained, held_external = (
        str(existing[0]),
        bool(existing[1]),
        None if existing[2] is None else str(existing[2]),
    )
    if held_retained and not resolved_retained:
        # "Keep this" and "do not keep this" about one recording. Silently returning the
        # retained row left a second copy of the bytes on disk under a row that says they
        # are not kept.
        raise LinguaWikiError(
            "artifact_retention_conflict",
            f"{held_id} already holds these exact bytes and is kept; this registration "
            "asks for the same recording not to be kept. Purge the artifact if the learner "
            "has changed their mind -- that deletes the recording and settles the claims "
            "resting on it.",
            details=(ErrorDetail(field="retained", reason="conflicts with " + held_id),),
        )
    if not held_retained and resolved_retained:
        # A renewed request to keep these bytes contradicts the durable record that they
        # were not kept. Silently applying the old decision was worse: it deleted the file
        # the caller had explicitly asked to retain. Reversing a privacy decision needs its
        # own audited operation; registration refuses and leaves the bytes untouched until
        # one exists.
        raise LinguaWikiError(
            "artifact_retention_conflict",
            f"{held_id} records that these exact bytes were not kept, while this "
            "registration asks to retain them. Registration cannot silently reverse that "
            "privacy decision, and it will not delete a file offered with the opposite "
            "instruction; the file was left where it is.",
            details=(ErrorDetail(field="retained", reason="conflicts with " + held_id),),
        )
    if external_id is not None and held_external not in (None, external_id):
        raise LinguaWikiError(
            "artifact_identity_conflict",
            f"{held_id} already holds these bytes under producer identifier "
            f"{held_external}, and this registration names {external_id}. One recording "
            "cannot answer to two producer identifiers without claims about it landing on "
            "whichever row a later lookup happens to find.",
            details=(ErrorDetail(field="external_id", reason=str(held_external)),),
        )
    return replace(plan, existing=(held_id, held_retained, held_external))


def write_registration(database: Database, plan: RegistrationPlan, *, command: str) -> str:
    """Write a planned new registration inside the caller's transaction.

    Only a plan for bytes this track does not yet hold: reconciling a second copy of a
    registered recording -- binding, repointing, deleting the duplicate -- is `register`'s
    to decide, and doing it here would bind a caller's rows to a recording it never made.
    """

    if plan.existing is not None:
        raise LinguaWikiError(
            "artifact_already_registered",
            f"these bytes are already registered as {plan.existing[0]}; a new registration "
            "cannot be written for them",
            details=(ErrorDetail(field="artifact", reason=plan.existing[0]),),
        )
    artifact_id = str(ArtifactId.new())
    now = database.now()
    database.execute(
        "INSERT INTO artifacts (artifact_id, track_id, kind, relative_path, "
        "media_type, byte_size, sha256, origin, rights, source_id, retained, "
        "external_id, clip_of_artifact_id, clip_starts_at_ms, clip_ends_at_ms, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            artifact_id,
            plan.track_id,
            plan.kind,
            plan.relative_path,
            plan.media_type,
            plan.size,
            plan.digest,
            plan.origin,
            plan.rights,
            plan.source_id,
            plan.retained,
            plan.external_id,
            plan.clip_of_artifact_id,
            plan.clip_starts_at_ms,
            plan.clip_ends_at_ms,
            now,
            now,
        ],
    )
    migration_module.record_audit_entry(
        database,
        command=command,
        correlation_id=EventId.new(),
        outcome="succeeded",
        # The path is a private location, so the audit trail names the artifact and its
        # hash rather than where on disk the learner keeps it.
        affected_records_json=json.dumps([artifact_id]),
        after_summary=f"registered a {plan.kind} artifact of {plan.size} byte(s)",
    )
    if not plan.retained:
        # Inside the transaction, deliberately. Recording "not kept" and keeping the bytes
        # is the one combination that must not exist -- the row says they are gone and the
        # privacy audit believes it -- and a deletion that failed after the commit left
        # exactly that state on disk.
        _remove_file(plan.absolute, artifact_id=artifact_id, reason="not retained")
    return artifact_id


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
    external_id: str | None = None,
    expected_sha256: str | None = None,
    clip_of: str | None = None,
    clip_starts_at_ms: int | None = None,
    clip_ends_at_ms: int | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "artifact.register",
) -> ArtifactReport:
    """Record that a file exists, without moving it or reading it into the database.

    Retention defaults to the track's own consent rather than to `True`: a learner who
    has not agreed to audio being kept has a recording registered as *not retained*.

    "Not retained" then *means* it. Stage 5 wrote the row and left the bytes where they
    were, so a workspace could hold a recording the learner never agreed to keep while
    reporting that it held none -- the privacy audit counts only retained artifacts, and
    this one was invisible to it. The file is deleted, and the row is what survives: a
    record that it existed, its hash, and that it was not kept. That is what makes a later
    absence explicable rather than merely unexplained.

    `expected_sha256` is for a caller who already knows what the file should be -- an
    ingested package naming its own audio. A mismatch is refused rather than recorded,
    because registering the wrong file under a package's artifact ID attaches a claim to
    a recording of something else.

    `clip_of` marks this file as a selected excerpt of another recording. The distinction
    is not cosmetic: the plan prefers keeping short clips to keeping whole conversations
    indefinitely, and the retention sweep holds back a clip that supports an unfinished
    pronunciation target where it would not hold back the entire call.

    It is not a promotion and must not become one: every caller relies on it registering
    the path it is given. Moving a file into place is the caller's, and a capture does it
    in `services/recordings.py` before planning the registration.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        plan = plan_registration(
            database,
            paths.root,
            relative_path=relative_path,
            kind=kind,
            media_type=media_type,
            origin=origin,
            rights=rights,
            source=source,
            retained=retained,
            external_id=external_id,
            expected_sha256=expected_sha256,
            clip_of=clip_of,
            clip_starts_at_ms=clip_starts_at_ms,
            clip_ends_at_ms=clip_ends_at_ms,
            track=track,
        )
        if plan.existing is not None:
            return _reconcile_duplicate(paths, database, plan)
        with database.transaction() as transaction:
            artifact_id = write_registration(transaction, plan, command=command)
        report = _read_artifact(database, artifact_id=artifact_id)
    warnings: list[str] = []
    if kind == "audio" and not plan.retained:
        warnings.append(
            "registered as not retained, because this track has not consented to keeping "
            "audio; the recording can be worked with now, and no pronunciation claim made "
            "from it will survive as confirmed"
        )
    return report.model_copy(update={"warnings": tuple(warnings)})


def _reconcile_duplicate(
    paths: WorkspacePaths, database: Database, plan: RegistrationPlan
) -> ArtifactReport:
    """A second registration of bytes this track already holds: one recording, one row."""

    assert plan.existing is not None
    held_id, held_retained, held_external = plan.existing
    absolute = plan.absolute
    external_id = plan.external_id
    notes = ["this file's content is already registered; the existing artifact is returned"]
    now = aware_utc(database.now())
    if not held_retained:
        # The row says the learner declined to keep this recording. Bytes that reappear
        # under it are governed by that decision, not by the fact that somebody ran
        # `register` again.
        _remove_file(absolute, artifact_id=held_id, reason="not retained")
        return _read_artifact(database, artifact_id=held_id).model_copy(
            update={
                "warnings": (
                    "this recording is on record as one the learner chose not to keep, so "
                    "the copy just offered was removed rather than registered",
                )
            }
        )
    held_relative = str(
        database.scalar("SELECT relative_path FROM artifacts WHERE artifact_id = ?", [held_id])
    )
    held_path = paths.root / held_relative
    # Resolved, not joined. A registered path replaced by a symlink out of the workspace
    # was treated as the canonical copy, and a genuine in-workspace copy offered afterwards
    # was deleted as the duplicate.
    held_contained = contained_file(paths.root, held_relative)
    if held_contained is None and _path_exists(held_path):
        raise LinguaWikiError(
            "artifact_escaped_the_workspace",
            f"{held_id} is registered at {held_relative}, and what is there now leads "
            "outside this workspace. Nothing will be read through it and nothing will be "
            "deleted on its word: `artifact verify` reports it, and the path has to be put "
            "back or the record retired with `artifact purge`.",
            details=(ErrorDetail(field="relative_path", reason=held_relative),),
        )
    binding = external_id is not None and held_external is None
    # A *duplicate* means two copies of the registered recording exist. Presence alone was
    # not enough: when the registered path had been altered, the correct recording restored
    # elsewhere was classified as the duplicate and deleted, so the workspace kept the
    # tampered copy and destroyed the real one.
    held_present = held_contained is not None
    held_matches = held_present and digest_or_none(held_path) == plan.digest
    duplicate_on_disk = absolute != held_path and held_matches
    restores_a_missing_file = absolute != held_path and not held_present
    if absolute != held_path and held_present and not held_matches:
        # The registered path holds bytes that are not this recording's. Repointing the row
        # would leave them under a private root with nothing accounting for them -- the
        # state a not-retained declaration exists to prevent -- and deleting them would
        # destroy a file this workspace cannot identify. Both are decisions for a person.
        raise LinguaWikiError(
            "artifact_altered_at_registered_path",
            f"{held_id} is registered at {held_path.name}, and the file there is neither "
            "this recording nor gone: it has been altered. Repointing the record would "
            "leave those bytes under a private directory with nothing accounting for them. "
            "Settle them first -- `artifact verify` reports the alteration, and `artifact "
            "purge` retires the record if the recording is genuinely lost.",
            details=(
                ErrorDetail(field="artifact", reason=held_id),
                ErrorDetail(field="relative_path", reason=str(held_path.name)),
            ),
        )
    if binding or duplicate_on_disk or restores_a_missing_file:
        # One transaction over both, because the deletion cannot be undone and the binding
        # can: committing the binding first and then failing to delete left the identifier
        # bound to a row while untracked bytes stayed on disk.
        with database.transaction() as transaction:
            if binding:
                # Binding it now is what makes a re-ingested package find this row rather
                # than register the same recording again.
                transaction.execute(
                    "UPDATE artifacts SET external_id = ?, updated_at = ? WHERE artifact_id = ?",
                    [external_id, naive_utc(now), held_id],
                )
                notes.append(f"bound to producer identifier {external_id}")
            if restores_a_missing_file:
                # The registered copy is gone or no longer its own bytes, and these are:
                # the learner moved or restored the recording. Repointing the row is what
                # makes the claims resting on it checkable again.
                transaction.execute(
                    "UPDATE artifacts SET relative_path = ?, updated_at = ? WHERE artifact_id = ?",
                    [plan.relative_path, naive_utc(now), held_id],
                )
                notes.append("the registered copy was missing, so this one takes its place")
            if duplicate_on_disk:
                # A second copy of bytes already held, with the first still there. Whatever
                # the learner decided about the original governs the copy, and leaving it
                # would put a recording on disk no row accounts for.
                _remove_file(absolute, artifact_id=held_id, reason="already registered")
                notes.append("the duplicate copy was removed")
    return _read_artifact(database, artifact_id=held_id).model_copy(
        update={"warnings": tuple(notes)}
    )


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
            "SELECT artifact_id, relative_path, sha256, purged_at, retained FROM artifacts "
            "WHERE track_id = ? ORDER BY created_at",
            [track_id],
        )
        owned = active_paths(database)
    missing: list[str] = []
    altered: list[str] = []
    purged: list[str] = []
    escaped: list[str] = []
    unreadable: list[str] = []
    present = 0
    for artifact_id, relative_path, digest, purged_at, retained in rows:
        # What the row names, and which kind of nothing when it is not a file here. A path
        # that escapes is not "present" -- reading through it would let a file outside the
        # workspace stand in for the learner's recording -- and a path this workspace cannot
        # look at is neither escaping nor absent.
        state, absolute = classify_path(paths.root, str(relative_path))
        tombstoned = purged_at is not None or not retained
        if tombstoned:
            # Recorded first and unconditionally: what the row *is* does not depend on what
            # its old path has since become, and returning early left an escaped or
            # unreadable tombstone out of the count of purged artifacts entirely.
            purged.append(str(artifact_id))
        if state == PATH_ESCAPED:
            escaped.append(str(artifact_id))
            continue
        if state == PATH_UNREADABLE:
            unreadable.append(str(artifact_id))
            continue
        if tombstoned:
            # Both say the same thing about the bytes: they are not here. A purge is a
            # decision made later, a not-retained registration is one made at the door,
            # and either way an absent file is the expected state rather than damage.
            if absolute is not None and not tombstone_path_is_excused(
                paths.root, str(relative_path), owned=owned
            ):
                # The file being back is not a happy accident: the row says it was removed,
                # and the learner has been told so. Unless a *live* row owns that path now,
                # in which case what is there is that recording and this row is history.
                altered.append(
                    f"{artifact_id} (recorded as not kept, but the file is present again)"
                )
            continue
        if absolute is None:
            missing.append(str(artifact_id))
            continue
        present += 1
        found = digest_or_none(absolute)
        if found is None:
            # Readable as a file a moment ago and not now. Neither intact nor altered, and
            # raising made `artifact verify` -- the command whose whole job is to report the
            # state of these files -- fail instead of reporting it.
            unreadable.append(str(artifact_id))
        elif found != str(digest):
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
    if escaped:
        warnings.append(
            f"{len(escaped)} registered path(s) now lead outside this workspace, so a file "
            "somewhere else would be standing in for the learner's recording; nothing is "
            "read through them"
        )
    if unreadable:
        warnings.append(
            f"{len(unreadable)} file(s) are there and cannot be read, so whether they are "
            "still the recordings they were registered as is unknown"
        )
    return VerificationReport(
        track_id=track_id,
        checked=len(rows),
        present=present,
        missing=tuple(missing),
        altered=tuple(altered),
        purged=tuple(purged),
        escaped=tuple(escaped),
        unreadable=tuple(unreadable),
        ok=not missing and not altered and not escaped and not unreadable,
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
            # Resolved, like every other read of a stored path: a listing that called an
            # escaping symlink "present" contradicted `artifact verify` about the same file.
            contained = contained_file(paths.root, report.relative_path)
            artifacts.append(report.model_copy(update={"present": contained is not None}))
        return ArtifactListing(track_id=track_id, total=total, artifacts=tuple(artifacts))


@dataclass(frozen=True, slots=True)
class PurgeOutcome:
    """What one purge settled, written inside the caller's transaction."""

    #: The moment the tombstone records, so a report states the same time the row does.
    purged_at: str
    invalidated_observations: tuple[str, ...] = ()
    invalidated_results: tuple[str, ...] = ()
    withdrawn_submissions: tuple[str, ...] = ()
    voided_verdicts: tuple[str, ...] = ()


def write_purge(
    database: Database,
    root: Path,
    *,
    artifact_id: str,
    reason: str,
    command: str,
    detail: str | None = None,
) -> PurgeOutcome:
    """Tombstone a recording, settle what rested on it, and delete its bytes -- in the
    caller's transaction.

    Separate from `purge` so a caller that already holds a transaction can purge as part
    of it: a capture superseding an earlier recording of the same task purges that
    recording in the transaction that binds the new one, so there is no moment at which
    both are live and no moment at which neither is.

    `reason` is the tombstone's, from a closed vocabulary. `detail`, when given, is the
    why in words -- `consent withdrawn` for a consent change, which the vocabulary has no
    term for -- and it is what the withdrawn submissions and the audit entry say, so the
    reason is not lost to the column's narrower one.
    """

    from linguawiki.services import withdrawal

    why = reason if detail is None else detail

    relative_path = str(
        database.scalar("SELECT relative_path FROM artifacts WHERE artifact_id = ?", [artifact_id])
    )
    dependent = dependent_observations(database, artifact_id=artifact_id)
    now = database.now()
    absolute = assert_within(root / relative_path, root, purpose="artifact")
    database.execute(
        "UPDATE artifacts SET purged_at = ?, purge_reason = ?, retained = FALSE, "
        "updated_at = ? WHERE artifact_id = ?",
        [now, reason, now, artifact_id],
    )
    for observation_id, _ in dependent:
        database.execute(
            "UPDATE pronunciation_observations SET invalidated_at = ?, "
            "invalidation_reason = ? WHERE observation_id = ?",
            [now, f"the audio it rested on was purged ({why})", observation_id],
        )
    # The assessment results that rested on it, and any submission still waiting for a
    # judge. Before C5 a purge found its dependents only among pronunciation observations,
    # so a score outlived the recording it was given for.
    withdrawn = withdrawal.write_withdrawal(
        database, artifact_id=artifact_id, reason=f"the recording was purged ({why})"
    )
    migration_module.record_audit_entry(
        database,
        command=command,
        correlation_id=EventId.new(),
        outcome="succeeded",
        affected_records_json=json.dumps([artifact_id]),
        after_summary=(
            f"purged ({reason if detail is None else f'{reason}: {detail}'}); "
            f"{len(dependent)} acoustic claim(s) and "
            f"{len(withdrawn.invalidated_results)} assessment result(s) invalidated, "
            f"{len(withdrawn.withdrawn_submissions)} unjudged submission(s) withdrawn"
            + (
                f" and verdict(s) {', '.join(withdrawn.voided_verdicts)} voided"
                if withdrawn.voided_verdicts
                else ""
            )
            + ", language evidence untouched"
        ),
    )
    migration_module.record_domain_event(
        database,
        event_type="artifact.purged",
        aggregate_type="artifact",
        aggregate_id=artifact_id,
        correlation_id=EventId.new(),
        payload_json=json.dumps(
            {
                "reason": reason,
                "invalidated": len(dependent),
                "invalidated_results": len(withdrawn.invalidated_results),
                **({} if detail is None else {"detail": detail}),
            },
            sort_keys=True,
        ),
    )
    # Deleted before the commit, deliberately. Either order can fail partway, so the
    # question is which wreckage is safer, and it is not symmetric:
    #
    # - commit first, then delete: a failure leaves a tombstone saying the recording is
    #   gone while the file is still on disk. The learner reads "purged" and believes a
    #   privacy request was honoured that was not.
    # - delete first, then commit: a failure leaves the file gone with no tombstone. The
    #   privacy request *was* honoured, the record is merely incomplete, `artifact verify`
    #   reports it as missing, and running the purge again completes it.
    #
    # Never claim a privacy action that did not happen. An incomplete record of a deletion
    # that did happen is recoverable; the reverse is a lie. Always: Stage 5 offered
    # `--keep-file`, which wrote a tombstone, invalidated the claims that rested on the
    # recording, and left the recording where it was. There is no reading of that state
    # that is true.
    _remove_file(absolute, artifact_id=artifact_id, reason=f"purged ({reason})")
    return PurgeOutcome(
        purged_at=aware_utc(now).isoformat(),
        invalidated_observations=tuple(entry[0] for entry in dependent),
        invalidated_results=withdrawn.invalidated_results,
        withdrawn_submissions=withdrawn.withdrawn_submissions,
        voided_verdicts=withdrawn.voided_verdicts,
    )


def purge(
    paths: WorkspacePaths,
    *,
    artifact: str,
    reason: str = "learner-request",
    dry_run: bool = False,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "artifact.purge",
) -> PurgeReport:
    """Remove a file and settle what depended on it.

    The invalidation is the careful part, and it is deliberately narrow. Losing the audio
    invalidates exactly the claims that needed the audio to be made: anything `confirmed`,
    anything about prosody or native-likeness at any confidence, and every assessment
    result a judge reached by listening to it. It does **not** touch what the learner said
    -- that was established by the transcript, which is still here. Purging a recording is
    a privacy choice, and a privacy choice that also deleted a month of language evidence
    would be one nobody could afford to make.

    `dry_run` reports exactly this without doing it, because the plan requires that the
    consequences are visible before the deletion, not after.
    """

    from linguawiki import sources as source_policy
    from linguawiki.services import withdrawal

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
        results = withdrawal.dependent_results(database, artifact_id=artifact_id)
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
                invalidated_results=tuple(results),
                surviving_language_evidence=surviving,
                purged_at=now.isoformat(),
                warnings=(
                    "dry run: nothing was removed. Purging would invalidate "
                    f"{len(dependent)} acoustic claim(s) and {len(results)} assessment "
                    f"result(s), and leave {surviving} piece(s) of language evidence "
                    "untouched.",
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
        with database.transaction() as transaction:
            outcome = write_purge(
                transaction, paths.root, artifact_id=artifact_id, reason=reason, command=command
            )
    warnings: list[str] = []
    if outcome.invalidated_observations:
        warnings.append(
            f"{len(outcome.invalidated_observations)} acoustic claim(s) no longer stand and "
            "are marked as such; they are kept on the record rather than deleted, because a "
            "learner told their pronunciation was wrong deserves to see that the evidence is "
            "gone"
        )
    if outcome.invalidated_results:
        warnings.append(
            f"{len(outcome.invalidated_results)} assessment result(s) rested on this "
            "recording and are marked invalidated; the estimates they shaped were rebuilt "
            "from what survives, and the history that rested on them is annotated"
        )
    if outcome.withdrawn_submissions:
        warnings.append(
            f"{len(outcome.withdrawn_submissions)} recording(s) a judge had not yet heard "
            "were withdrawn, and their tasks skipped"
        )
    if outcome.voided_verdicts:
        warnings.append(
            "verdict(s) held for them while their run was paused were voided, and will not "
            "be applied when it resumes: " + ", ".join(outcome.voided_verdicts)
        )
    return PurgeReport(
        artifact_id=artifact_id,
        relative_path=relative_path,
        reason=reason,
        file_removed=True,
        invalidated_observations=outcome.invalidated_observations,
        invalidated_results=outcome.invalidated_results,
        withdrawn_submissions=outcome.withdrawn_submissions,
        voided_verdicts=outcome.voided_verdicts,
        surviving_language_evidence=surviving,
        purged_at=outcome.purged_at,
        warnings=tuple(warnings),
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


def declared_audio_problems(
    validated: Any,
    *,
    root: Path,
    registered: Mapping[str, HeldArtifact],
    owners: Mapping[str, PathOwner],
    track_id: str,
    audio_consent: bool,
) -> list[str]:
    """Everything wrong with the audio a package claims, found in one pass.

    A manifest entry is a *claim* about a file, and the whole acoustic rule rests on the
    file being there, so somebody has to look. Each fact gets its own sentence, because
    "your audio is wrong" sends an operator to the wrong place:

    - the path is outside the private roots, so the file could reach Git;
    - the file is not there at all, or is not the file the package describes;
    - a recording this workspace already holds under that identifier has different bytes,
      which means the producer reused an identifier for a different recording;
    - the package asks for audio-backed evidence from a learner who has not agreed to
      audio being kept, so the recording will not survive the ingest that cites it.

    The file is checked on *every* pass, including when the identifier is already known.
    Skipping verification for a known identifier was how a second conversation's audio
    was silently attached to the first recording.
    """

    problems: list[str] = []
    #: Every identifier this package declares, with the content it declares. Used to catch
    #: collisions, which is a question about *content* and so includes entries the package
    #: says were not kept.
    resolved_hashes: dict[str, str] = {}
    #: The identifiers whose audio this package actually brings. A separate set, because
    #: "have we seen these bytes" and "can a claim rest on them" are different questions,
    #: and answering the second with the first let a confirmed event cite a recording the
    #: package itself said was not kept.
    available: set[str] = set()
    held_by_hash = {held.sha256: external for external, held in registered.items()}
    for artifact in validated.artifacts:
        artifact_id = str(artifact.artifact_id)
        declared = str(artifact.sha256)
        try:
            safe = _assert_safe_relative(artifact.relative_path)
        except LinguaWikiError as failure:
            problems.append(f"{artifact_id}: {failure.payload.message}")
            continue
        try:
            # Resolved, exactly as registration resolves it. The lexical check refuses
            # `..`; only resolution catches an `imports/` symlink whose target is outside
            # the workspace, and a package could therefore pass review and be refused at
            # the write -- after earlier artifacts had been registered.
            absolute = assert_within(root / str(safe), root, purpose="artifact")
        except LinguaWikiError as failure:
            problems.append(f"{artifact_id}: {failure.payload.message}")
            continue
        present = absolute.is_file()
        if present:
            try:
                digest = file_digest(absolute)
            except OSError as failure:
                problems.append(
                    f"{artifact_id} names {artifact.relative_path}, which cannot be read: "
                    f"{failure}. A recording nobody can read is one no claim can rest on."
                )
                continue
            if digest != declared:
                # Checked for *every* entry, retained or not. A not-retained declaration is
                # honoured by deleting the file, and a declaration that misidentifies what
                # is at that path would delete a recording it never described: the one this
                # check exists to stop was somebody else's, under `imports/`.
                problems.append(
                    f"{artifact_id} names {artifact.relative_path}, which is not the file "
                    f"the package describes: it hashes to {digest[:12]}... and the package "
                    f"says {declared[:12]}...."
                )
                continue
        elif artifact.retained:
            problems.append(
                f"{artifact_id} names {artifact.relative_path}, which is not in this "
                "workspace. A claim confirmed from audio nobody has is a claim nobody -- "
                "including the learner it is about -- can check."
            )
            continue
        # And the *file* can already belong to somebody. Registration refuses this, and
        # checking it only there meant a package could be approved, register its first
        # recording, and be rejected on its second -- leaving the workspace changed by a
        # package that was refused.
        owner = owners.get(str(safe))
        if owner is not None and owner.track_id != track_id:
            problems.append(
                f"{artifact_id} names {artifact.relative_path}, which is already registered "
                f"on another track as {owner.artifact_id}. One file cannot belong to two "
                "learners: a purge or a retention sweep by either would delete the other's "
                "recording."
            )
            continue
        if owner is not None and owner.sha256 != declared:
            # The writer refuses this as an alteration of the row that already owns the
            # path. Leaving it until registration meant a multi-artifact package could
            # register its first file and fail on this one, despite passing review.
            problems.append(
                f"{artifact_id} names {artifact.relative_path}, which is already registered "
                f"as {owner.artifact_id}, but the file no longer has the bytes that row "
                f"records: the package says {declared[:12]}... and the record says "
                f"{owner.sha256[:12]}.... `artifact verify` reports the alteration; settle "
                "that row before importing a different recording at its path."
            )
            continue
        # Identity collides by *content* as well as by identifier, and a not-retained entry
        # has content too: one entry saying "keep this" beside another saying "do not keep
        # the same bytes" is a contradiction, and leaving it out of these checks let the
        # first be written before the second was refused.
        by_content = next(
            (entry for entry in registered.values() if entry.sha256 == declared), None
        )
        # A local artifact with no producer identifier is not a competing identity. The
        # writer deliberately binds the first identifier a package supplies to that row;
        # preflight used to refuse the same operation before it could happen.
        bindable = by_content is not None and by_content.external_id is None
        twin = held_by_hash.get(declared)
        if twin is not None and twin != artifact_id and not bindable:
            problems.append(
                f"{artifact_id} is the recording this workspace already holds as {twin}. "
                "One recording cannot answer to two producer identifiers without claims "
                "about it landing on whichever row a later lookup happens to find."
            )
            continue
        earlier = next(
            (other for other, other_digest in resolved_hashes.items() if other_digest == declared),
            None,
        )
        if earlier is not None:
            problems.append(
                f"{artifact_id} and {earlier} are the same recording declared twice under "
                "different identifiers; the package has to say which identifier the claims "
                "about it use."
            )
            continue
        resolved_hashes[artifact_id] = declared
        if artifact.retained:
            available.add(artifact_id)
        held = registered.get(artifact_id) or (by_content if bindable else None)
        tombstone = next(
            (entry for entry in (held, by_content) if entry is not None and entry.purged),
            None,
        )
        if tombstone is not None:
            # The learner had this recording deleted. A file offering it again is not an
            # occasion to undo that, and the workspace holds one row per recording, so
            # there is nowhere for a second life to go. The tombstone stands.
            problems.append(
                f"{artifact_id} is a recording this workspace purged at the learner's "
                f"request ({tombstone.artifact_id}). A package does not bring a deleted "
                "recording back: the tombstone is the record that it went, and why."
            )
            continue
        if held is None:
            continue
        if held.sha256 != declared:
            problems.append(
                f"{artifact_id} already names a different recording in this workspace. An "
                "identifier the producer reuses for another file is not the same "
                "recording, and claims made from the first would come to rest on the second."
            )
            continue
        if artifact.retained != held.retained:
            # Both directions, because registration refuses both and review refused one:
            # a package could be approved and then rejected, with the artifacts declared
            # before it already written.
            problems.append(
                (
                    f"{artifact_id} is recorded here as a recording the learner chose not "
                    "to keep, and a package cannot reverse that decision. Registration "
                    "also refuses a contradictory retain request without deleting the "
                    "offered file; reversing it needs an explicit audited retention action."
                )
                if artifact.retained
                else (
                    f"{artifact_id} is a recording this workspace keeps, and this package "
                    "says it was not kept. Deleting it on a file's word is not this "
                    "command's decision: `artifact purge` is how the learner makes it."
                )
            )
            continue
        if held.kind != str(artifact.kind):
            problems.append(
                f"{artifact_id} is held here as {held.kind} and this package calls it "
                f"{artifact.kind}. One file cannot be both, and a claim confirmed from "
                '"audio" that is really a transcript is the thing the acoustic rule '
                "exists to prevent."
            )
            continue
    needed = _audio_backed_events(validated)
    if needed and not audio_consent:
        problems.append(
            "this package confirms pronunciation from audio, and this track has not agreed "
            f"to audio being kept ({', '.join(sorted(needed))}). The recording would be "
            "deleted at the door and the claim would rest on nothing. Record audio "
            "retention consent, or export the observation as observed or uncertain."
        )
    declared_kinds = {
        str(artifact.artifact_id): str(artifact.kind) for artifact in validated.artifacts
    }
    for event_id, named in _audio_backed_events(validated).items():
        held = registered.get(named)
        if not (named in available or (held is not None and held.retained)):
            problems.append(
                f"event {event_id} confirms pronunciation from {named}, which this package "
                "does not carry as retained audio and this workspace does not hold; a "
                "confirmed claim nobody can check is worse than an uncertain one"
            )
            continue
        # And what it rests on has to be a *recording*. A correct transcript proves nothing
        # about how something sounded, which is no less true when the transcript is a file.
        kind = declared_kinds.get(named) or (held.kind if held is not None else None)
        if kind != "audio":
            problems.append(
                f"event {event_id} confirms pronunciation from {named}, which is "
                f"{kind or 'of unknown kind'} rather than audio. Confirming how something "
                "sounded requires the sound."
            )
    return problems


def _audio_backed_events(validated: Any) -> dict[str, str]:
    """Which events claim audio, and which recording each one names."""

    needed: dict[str, str] = {}
    for event in validated.events:
        payload = event.payload
        if getattr(payload, "status", None) != "confirmed":
            continue
        named = getattr(payload, "audio_artifact_id", None)
        if named is not None:
            needed[str(event.event_id)] = str(named)
    return needed


@dataclass(frozen=True, slots=True)
class HeldArtifact:
    """What this workspace knows about a file a package might name.

    Every field answers a question a package's claim depends on, and each was a defect
    before it was a field: the hash says whether this is the same recording, `retained`
    says whether anyone can still listen to it, and `kind` says whether it is a recording
    at all -- a transcript artifact satisfied a `confirmed` audio claim for want of that
    last one.
    """

    artifact_id: str
    sha256: str
    retained: bool
    kind: str
    #: The producer's identity when one has been bound. `None` is materially different
    #: from using the local artifact ID as a lookup key: the first package may bind it.
    external_id: str | None = None
    #: Whether this is a tombstone. Preflight used to exclude purged rows, so a package
    #: could validate a recording whose identity belonged to one -- and registration, whose
    #: lookup was unfiltered, found it and deleted the newly supplied file.
    purged: bool = False


def registered_by_producer(database: Database, *, track_id: str) -> dict[str, HeldArtifact]:
    """Files this workspace holds, keyed by the producer's own identifier."""

    return {
        str(external_id or artifact_id): HeldArtifact(
            artifact_id=str(artifact_id),
            sha256=str(digest),
            retained=bool(retained),
            kind=str(kind),
            external_id=None if external_id is None else str(external_id),
            purged=purged_at is not None,
        )
        for artifact_id, external_id, digest, retained, kind, purged_at in database.query(
            "SELECT artifact_id, external_id, sha256, retained, kind, purged_at "
            "FROM artifacts WHERE track_id = ?",
            [track_id],
        )
    }


def register_declared_audio(
    paths: WorkspacePaths,
    validated: Any,
    *,
    track: str,
    registered: Mapping[str, HeldArtifact],
    clock: Clock,
    command: str,
) -> dict[str, str]:
    """Give every retained recording the package names a durable row of its own.

    The manifest entry is the producer's word for it. The row is this workspace's: it is
    what `artifact verify` checks, what `artifact purge` deletes, and what the acoustic
    claims point at. Without it the claim and the file had no relationship a command could
    act on.
    """

    resolved = {
        external: held.artifact_id for external, held in registered.items() if held.retained
    }
    for artifact in validated.artifacts:
        external = str(artifact.artifact_id)
        if not artifact.retained:
            # A package saying "this existed and was not kept" is a privacy declaration, and
            # skipping it left the bytes sitting unregistered under an ignored directory,
            # accounted for by nothing. Register it as not kept -- which deletes the file --
            # so the record explains the absence and the absence is real.
            #
            # Not skipped when the identifier is already known, either: the row may say "not
            # kept" while the bytes have since been restored, and this declaration is the
            # occasion to honour it again. `register` finds the row by content and removes
            # them.
            if not (paths.root / str(_assert_safe_relative(artifact.relative_path))).is_file():
                continue
            register(
                paths,
                relative_path=artifact.relative_path,
                kind=str(artifact.kind),
                origin="provider-export",
                external_id=None if external in registered else external,
                # The hash it must be, so a misdescribed path cannot delete another file.
                expected_sha256=str(artifact.sha256),
                retained=False,
                track=track,
                clock=clock,
                command=command,
            )
            continue
        if external in registered:
            continue
        record = register(
            paths,
            relative_path=artifact.relative_path,
            kind=str(artifact.kind),
            origin="provider-export",
            external_id=external,
            expected_sha256=str(artifact.sha256),
            track=track,
            clock=clock,
            command=command,
        )
        resolved[external] = record.artifact_id
    return resolved


class SweepReport(ContractModel):
    """What a retention policy would remove, or did."""

    track_id: str
    policy: str
    retention_days: int | None = None
    considered: int = 0
    purged: tuple[str, ...] = ()
    #: Whole recordings that supported evidence and were swept anyway. Named so the
    #: learner can see which conversations they are about to lose the sound of, and clip
    #: what matters before running it for real.
    unclipped_recordings: tuple[str, ...] = ()
    invalidated_observations: tuple[str, ...] = ()
    #: Captures in a run still being worked, heard by a judge or not. Held back under every
    #: policy: deleting one discards the learner's answer or a result the run rests on.
    held_for_judging: tuple[str, ...] = ()
    #: Captures a judged result rests on, due once their run closed. Purged like any whole
    #: recording, so their results are invalidated exactly as an explicit purge does it.
    judged_recordings: tuple[str, ...] = ()
    invalidated_results: tuple[str, ...] = ()
    #: Submissions whose run closed before a judge heard them: the hold lapsed, so they
    #: were withdrawn (or would be, on a dry run) and their recordings treated as any other.
    withdrawn_submissions: tuple[str, ...] = ()
    dry_run: bool = False
    warnings: tuple[str, ...] = ()


def sweep(
    paths: WorkspacePaths,
    *,
    dry_run: bool = False,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "artifact.sweep",
) -> SweepReport:
    """Apply the track's audio retention policy to the recordings it still holds.

    The plan asks for three policies and a workspace that only had a consent boolean could
    express none of them. Consent says a recording *may* be kept; this says for how long,
    and "indefinitely unless somebody remembers" is the answer a privacy setting exists to
    avoid.

    - `keep` removes nothing. The learner decides, one purge at a time.
    - `rolling-days` removes what is older than the window.
    - `delete-after-ingestion` removes anything a package has already been ingested from:
      the claims made from the recording survive it, which is the whole point of recording
      the claims.

    A recording an *active* acoustic claim rests on is left alone and reported. Deleting it
    would invalidate the evidence for a pronunciation target the learner is still working
    on, and a retention window is a weaker reason than that.
    """

    from linguawiki import transcripts as transcript_policy

    active_clock = clock or SystemClock()
    with open_reader(paths, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        preferences = record.preferences if isinstance(record.preferences, dict) else {}
        policy = str(preferences.get("audio_retention_policy") or "keep")
        days = preferences.get("audio_retention_days")
        retention_days = int(str(days)) if days is not None else None
        now = aware_utc(database.now())
        candidates = database.query(
            "SELECT artifact_id, created_at, external_id FROM artifacts "
            "WHERE track_id = ? AND kind = 'audio' AND purged_at IS NULL AND retained "
            "ORDER BY artifact_id",
            [track_id],
        )
        clips = {
            str(artifact_id)
            for (artifact_id,) in database.query(
                "SELECT artifact_id FROM artifacts WHERE track_id = ? "
                "AND clip_of_artifact_id IS NOT NULL",
                [track_id],
            )
        }
        # Audio is held back only for a claim about a target the learner is *still working
        # on*, and only when what would be kept is a selected clip. The plan is explicit
        # that clips are preferred to keeping whole conversations indefinitely, and holding
        # the entire call because one target it touched is unfinished is that indefinite
        # retention wearing a justification.
        supporting = {
            str(artifact_id)
            for (artifact_id,) in database.query(
                "SELECT DISTINCT observation.audio_artifact_id "
                "FROM pronunciation_observations observation "
                "LEFT JOIN track_item_state state "
                "  ON state.content_id = observation.target_content_id "
                "  AND state.track_id = observation.track_id "
                "WHERE observation.track_id = ? "
                "AND observation.audio_artifact_id IS NOT NULL "
                "AND observation.invalidated_at IS NULL AND observation.status = ? "
                "AND observation.target_content_id IS NOT NULL "
                # A target with no state row has certainly not been finished, so the
                # absence holds the recording exactly as an unfinished stage does.
                "AND (state.stage IS NULL OR state.stage <> ?)",
                [track_id, transcript_policy.CONFIRMED_STATUS, ACHIEVED_STAGE],
            )
        }
        # A capture waiting for a judge, and whether its run is still being worked. The
        # sweep never consulted assessment results before C5, so it would delete the
        # recording a judge was about to hear and leave a verdict with nothing under it.
        awaiting = {
            str(artifact_id): (str(submission_id), str(status))
            for artifact_id, submission_id, status in database.query(
                "SELECT submission.artifact_id, submission.submission_id, run.status "
                "FROM assessment_submissions submission "
                "JOIN assessment_runs run ON run.run_id = submission.run_id "
                "WHERE run.track_id = ? AND submission.status = 'pending'",
                [track_id],
            )
        }
        judged = {
            str(artifact_id): str(status)
            for artifact_id, status in database.query(
                "SELECT DISTINCT result.audio_artifact_id, run.status "
                "FROM assessment_results result "
                "JOIN assessment_runs run ON run.run_id = result.run_id "
                "WHERE run.track_id = ? AND result.audio_artifact_id IS NOT NULL "
                "AND result.invalidated_at IS NULL",
                [track_id],
            )
        }
    warnings: list[str] = []
    if policy == "rolling-days" and retention_days is None:
        raise LinguaWikiError(
            "retention_window_missing",
            "this track keeps audio for a rolling window and does not say how many days; "
            "a window nobody set is not a window, and sweeping on a guess would delete "
            "recordings the learner expected to still have",
            details=(ErrorDetail(field="audio_retention_days", reason="not set"),),
        )
    due: list[str] = []
    unclipped: list[str] = []
    held_back = 0
    held_for_judging: list[str] = []
    judged_due: list[str] = []
    lapsed: list[str] = []
    for artifact_id, created_at, external_id in candidates:
        if policy == "keep":
            continue
        if policy == "rolling-days":
            age = (now - aware_utc(created_at)).days
            if retention_days is None or age < retention_days:
                continue
        if policy == "delete-after-ingestion" and external_id is None:
            # A learner's own recording that no package brought in: this policy is about
            # what arrives with an ingest, and deleting the rest would be a different
            # decision than the one they made.
            continue
        waiting = awaiting.get(str(artifact_id))
        live_run = (waiting is not None and waiting[1] in ("in-progress", "paused")) or (
            judged.get(str(artifact_id)) in ("in-progress", "paused")
        )
        if live_run:
            # The run is still being worked. A recording a judge has not heard is an answer
            # the learner has given; one a judge has heard rests a result the run is still
            # folding, and purging it now would invalidate that result and settle the task
            # for the rest of the run. A retention window is a weaker reason than either,
            # and the hold lapses when the run closes.
            held_for_judging.append(str(artifact_id))
            continue
        if waiting is not None:
            # The run closed with the recording unjudged: nothing will hear it now. The
            # purge below withdraws the submission in its own transaction, so the
            # withdrawal and the deletion land together or not at all.
            lapsed.append(waiting[0])
        if str(artifact_id) in judged:
            # A judged result rests on it. It is due like any whole recording, and it goes
            # through `purge`, so the result is invalidated exactly as an explicit purge
            # would invalidate it.
            judged_due.append(str(artifact_id))
        if str(artifact_id) in supporting:
            if str(artifact_id) in clips:
                held_back += 1
                continue
            # A whole recording that evidence rests on: it still goes, and the report says
            # what that costs. Extracting a clip is what preserves the evidence, and a
            # sweep that quietly kept the call instead would be deciding retention for the
            # learner.
            unclipped.append(str(artifact_id))
        due.append(str(artifact_id))
    if held_back:
        warnings.append(
            f"{held_back} selected clip(s) were kept: a confirmed pronunciation claim about "
            "a target the learner has not finished still rests on them, and a retention "
            "window is a weaker reason to delete evidence than an unfinished target is to "
            "keep it"
        )
    if unclipped:
        warnings.append(
            f"{len(unclipped)} whole recording(s) that evidence rests on are due under this "
            "policy and will go with it: "
            + ", ".join(unclipped[:10])
            + ". Extract the moments that matter with `artifact clip` first -- a clip is "
            "kept past the window, an entire conversation is not."
        )
    if held_for_judging:
        warnings.append(
            f"{len(held_for_judging)} recorded answer(s) were kept, because their run is "
            "still being worked: "
            + ", ".join(held_for_judging[:10])
            + ". Deleting them would discard answers the learner has given, or invalidate "
            "results the run still rests on; they become due when the run closes."
        )
    if judged_due:
        warnings.append(
            f"{len(judged_due)} recording(s) a judged assessment result rests on are due and "
            "will go, invalidating those results: " + ", ".join(judged_due[:10])
        )
    if lapsed:
        warnings.append(
            f"{len(lapsed)} submission(s) whose run closed unjudged "
            + ("would be" if dry_run else "were")
            + " withdrawn with their recordings, in the purge that removes them"
        )
    purged: list[str] = []
    invalidated: list[str] = []
    invalidated_results: list[str] = []
    for artifact_id in due:
        report = purge(
            paths,
            artifact=artifact_id,
            reason="retention-expiry",
            dry_run=dry_run,
            track=track_id,
            clock=active_clock,
            command=command,
        )
        purged.append(artifact_id)
        invalidated.extend(report.invalidated_observations)
        invalidated_results.extend(report.invalidated_results)
    if policy == "keep":
        warnings.append(
            "this track keeps audio until the learner says otherwise, so nothing was swept"
        )
    return SweepReport(
        track_id=track_id,
        policy=policy,
        retention_days=retention_days,
        considered=len(candidates),
        purged=tuple(purged),
        unclipped_recordings=tuple(unclipped),
        invalidated_observations=tuple(dict.fromkeys(invalidated)),
        held_for_judging=tuple(held_for_judging),
        judged_recordings=tuple(judged_due),
        invalidated_results=tuple(dict.fromkeys(invalidated_results)),
        withdrawn_submissions=tuple(lapsed),
        dry_run=dry_run,
        warnings=tuple(warnings),
    )
