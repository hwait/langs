"""Importing what was said, revising it honestly, and judging how it sounded.

Stage 4 ingested a `lingua.session.v1` package and staged its *events*. This module
keeps the package's other half: the transcript itself, as layers, so that a correction
can be told apart from a mishearing and an acoustic claim can be told apart from a
reading.

The immutability of the raw layer is the thing to understand. `utterances.raw_text` is
what the transcription produced, and it is never edited: every later reading is a row in
`transcript_revisions` naming what it changed. That is not bookkeeping fussiness -- it is
the only way to answer "did the learner say that, or did the machine hear it?", and that
question decides whether a learner is taught a correction for a mistake they never made.

What this module does *not* do is decide what an observation proves. The claim, the
strength, the stage it can promote -- all of that belongs to `evidence.py`, reached
through the session close like everything else. A transcript is an account of what
happened, and accounts become evidence at one boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from linguawiki import transcripts as transcript_policy
from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError, validated_contract
from linguawiki.ids import EventId, InterpretationId, PronunciationId, RevisionId, UtteranceId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import evidence as evidence_service
from linguawiki.services import learners as learner_service


class RevisionReport(ContractModel):
    revision_id: str
    layer: str
    derived_from: str
    kind: str
    #: Present only within the track's retention consent, like every other learner text.
    text: str | None = None
    reviewer_kind: str
    confidence: str
    created_at: str


class InterpretationReport(ContractModel):
    interpretation_id: str
    classification: str
    meaning: str | None = None
    corrected_form: str | None = None
    explanation: str | None = None
    confidence: str
    reviewer_kind: str
    #: True only for `learner-error`. The others are recorded and counted against nobody:
    #: a mishearing taught back as a mistake is worse than a mishearing lost.
    counts_against_the_learner: bool = False


class PronunciationReport(ContractModel):
    observation_id: str
    dimension: str
    status: str
    basis: str
    audio_artifact_id: str | None = None
    note: str | None = None
    #: Set when the audio this rested on was purged. The claim stays on the record,
    #: marked as no longer supported.
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    observed_at: str


class UtteranceReport(ContractModel):
    utterance_id: str
    external_id: str
    speaker: str
    sequence: int
    started_at: str
    ended_at: str
    #: What each layer says was said, best-reviewed last. Subject to retention consent.
    raw_text: str | None = None
    visibility: str
    raw_confidence: float | None = None
    audio_artifact_id: str | None = None
    audio_available: bool = False
    revisions: tuple[RevisionReport, ...] = ()
    interpretations: tuple[InterpretationReport, ...] = ()
    pronunciation: tuple[PronunciationReport, ...] = ()
    #: Which layers disagree about the words. Reported rather than resolved: it is the
    #: measure of how far the transcription can be trusted.
    disagreement: tuple[str, ...] = ()
    #: The most reviewed hearing available, which is what a correction should quote.
    best_layer: str = "raw"


class TranscriptReport(ContractModel):
    track_id: str
    ingestion_id: str | None = None
    session_id: str | None = None
    external_session_id: str | None = None
    utterances: tuple[UtteranceReport, ...] = ()
    total: int = 0
    #: How much of the learner's own words this workspace kept.
    retention_policy: str = "withheld"
    warnings: tuple[str, ...] = ()


class ImportReport(ContractModel):
    track_id: str
    ingestion_id: str
    imported: int = 0
    skipped: int = 0
    duplicate: bool = False
    retention_policy: str = "withheld"
    warnings: tuple[str, ...] = ()


def _retained_text(text: str, *, preferences: Mapping[str, Any]) -> tuple[str, str | None, str]:
    """Apply the track's retention rule to one line of the learner's own words.

    The same rule `evidence.py` applies to a response, applied where the transcript first
    arrives: a workspace that refused transcript retention keeps the hash and not the
    words, and a session's staged events already work this way.

    The one difference is what consent *means* here. An attempt keeps a bounded excerpt
    because the excerpt is all the evidence needs; a transcript line is the thing itself,
    and a learner who consented to transcript retention consented to having their
    transcript. Truncating it anyway would leave a workspace that cannot tell a corrected
    line from a cut-off one.
    """

    requested = "full" if preferences.get("transcript_retention_consent") is True else None
    visibility, excerpt, digest = evidence_service.retain_response(
        text, requested=requested, preferences=preferences
    )
    return (
        visibility,
        excerpt,
        digest or hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _preferences(track: learner_service.TrackRecord) -> Mapping[str, Any]:
    preferences = track.preferences
    return preferences if isinstance(preferences, dict) else {}


def retention_policy(preferences: Mapping[str, Any]) -> str:
    """What this track keeps of the learner's own words, named once for the reports."""

    if preferences.get("transcript_retention_consent"):
        return "full"
    if preferences.get("transcript_retention_consent") is False:
        return "withheld"
    return "excerpt"


def _revision_hash(*, external_id: str, layer: str, kind: str, text: str) -> str:
    """The identity of a revision: what it revised, what it claims, and what it says.

    Over the *full* text rather than what retention kept, so that two workspaces with
    different consent settings agree on whether they hold the same revision.
    """

    canonical = json.dumps(
        {"external_id": external_id, "layer": layer, "kind": kind, "text": text},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_utterance(database: Database, utterance: str, *, track_id: str) -> str:
    """Resolve an utterance by our identifier or by the producer's, inside its own track."""

    row = database.one(
        "SELECT utterance_id FROM utterances WHERE utterance_id = ? AND track_id = ?",
        [utterance, track_id],
    )
    if row is None:
        row = database.one(
            "SELECT utterance_id FROM utterances WHERE track_id = ? AND external_id = ?",
            [track_id, utterance],
        )
    if row is None:
        raise LinguaWikiError(
            "utterance_not_found",
            f"this track has no utterance {utterance}; `transcript show` lists what was "
            "imported, by both our identifier and the producer's",
            details=(ErrorDetail(field="utterance", reason=utterance),),
        )
    return str(row[0])


def _verified_original(
    *, stored_text: str | None, visibility: str, text_hash: str | None, supplied: str | None
) -> str:
    """The actual earlier text, either because we kept it or because the caller proved it.

    A normalization is defined by what it does *not* change, so checking it needs the
    words it started from. A workspace that refused transcript retention does not have
    them -- but the caller proposing the normalization does, and the stored hash can tell
    whether what they supplied is the text that was actually heard. That keeps the honesty
    check working at every retention setting instead of only the most permissive one.
    """

    if visibility == "full" and stored_text is not None:
        # We kept the words, so they are the answer whatever the caller supplied. A
        # supplied original is a claim about the same line, checked against what we hold.
        if supplied is not None and supplied != stored_text:
            raise LinguaWikiError(
                "original_text_mismatch",
                "the supplied original is not the text this workspace holds for that "
                "reading. Normalizing a different line would file a claim about words "
                "nobody said.",
                details=(ErrorDetail(field="original", reason="does not match"),),
            )
        return stored_text
    if supplied is not None:
        # Without the words we can only accept an original the hash vouches for. A layer
        # that carries no hash cannot vouch for anything, so it is refused rather than
        # trusted: an unverified "original" would make the honesty check agree with
        # whatever the caller claimed it started from.
        if text_hash is None:
            raise LinguaWikiError(
                "original_text_unverifiable",
                "there is no recorded hash for that reading, so a supplied original cannot "
                "be checked against it, and an unchecked original would make this "
                "revision agree with itself",
                details=(ErrorDetail(field="original", reason="nothing to verify against"),),
            )
        digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        if digest != text_hash:
            raise LinguaWikiError(
                "original_text_mismatch",
                "the supplied original is not the text that was heard: its hash does not "
                "match the one recorded for this utterance. Normalizing a different line "
                "would file a claim about words nobody said.",
                details=(ErrorDetail(field="original", reason=digest),),
            )
        return supplied
    raise LinguaWikiError(
        "original_text_unavailable",
        "this workspace keeps only a hash of what was said, by the track's own retention "
        "consent, so a normalization cannot be checked against the words it started from. "
        "Supply the original text and it will be verified against that hash.",
        details=(ErrorDetail(field="visibility", reason=visibility),),
    )


def import_package(
    paths: WorkspacePaths,
    *,
    package: Mapping[str, Any],
    ingestion_id: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "transcript.import",
) -> ImportReport:
    """Store the transcript half of a session package, as layers.

    Stage 4's `session ingest-package` took the package's *events* and staged them. This
    takes what those events are about. The raw layer becomes immutable `utterances`; every
    derived layer becomes a revision row naming what it changed.

    One refusal is worth stating: a layer declared `normalized` whose words differ from the
    raw layer is rejected, package and all. Accepting it would file a mishearing under the
    label of a tidy-up, and later the learner would be corrected for a word they never
    said. A producer that genuinely heard different words has a layer for that.

    Re-importing is a no-op rather than a duplicate: an utterance is identified by the
    producer's own ID within the track, so a checkpoint export and the completed export of
    the same call agree about which utterances they share.
    """

    from linguawiki.contracts import SessionPackage

    validated = validated_contract(
        SessionPackage, package, code="invalid_session_package", subject="session package"
    )
    layers = {layer.kind: layer for layer in validated.transcript_layers}
    raw_layer = layers["raw"]
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        ingestion_row = None
        if ingestion_id is not None:
            ingestion_row = database.one(
                "SELECT track_id, session_id, package_hash FROM session_packages "
                "WHERE ingestion_id = ?",
                [ingestion_id],
            )
            if ingestion_row is None:
                raise LinguaWikiError(
                    "ingestion_not_found",
                    f"no package ingestion {ingestion_id} in this workspace; ingest the "
                    "package first, then import its transcript against what that returned",
                    details=(ErrorDetail(field="ingestion_id", reason=ingestion_id),),
                )
            track_id = str(ingestion_row[0])
            session_id = None if ingestion_row[1] is None else str(ingestion_row[1])
        else:
            track_id = learner_service.resolve_track(database, track)
            session_id = None if validated.session_id is None else str(validated.session_id)
        if validated.track_hint is not None and str(validated.track_hint) != track_id:
            raise LinguaWikiError(
                "package_track_mismatch",
                f"this package names track {validated.track_hint} and is being imported "
                f"into {track_id}; an external recording is one learner's, and attaching it "
                "elsewhere would put their words in another learner's record",
                details=(ErrorDetail(field="track_hint", reason=str(validated.track_hint)),),
            )
        if session_id is not None:
            # The same rule one level down. A package that names a session on another
            # track would otherwise attach one learner's words to another learner's
            # sitting -- and the track hint, which is what the check above reads, is
            # optional.
            owner = database.one("SELECT track_id FROM sessions WHERE session_id = ?", [session_id])
            if owner is None:
                raise LinguaWikiError(
                    "session_not_found",
                    f"this package names session {session_id}, which does not exist in "
                    "this workspace",
                    details=(ErrorDetail(field="session_id", reason=session_id),),
                )
            if str(owner[0]) != track_id:
                raise LinguaWikiError(
                    "package_track_mismatch",
                    f"this package names session {session_id}, which belongs to another "
                    "track; an external recording is one learner's, and attaching it "
                    "elsewhere would put their words in another learner's record",
                    details=(ErrorDetail(field="session_id", reason=session_id),),
                )
        record = learner_service.track_context(database, track_id)
        preferences = _preferences(record)
        retention = retention_policy(preferences)
        warnings: list[str] = []

        # Check every derived layer against the one it came from *before* writing anything,
        # so a package with a mislabelled layer leaves no half-imported transcript behind.
        raw_by_id = {u.utterance_id: u.text for u in raw_layer.utterances}
        planned_revisions: list[tuple[str, str, str, str]] = []
        for kind in transcript_policy.LAYERS[1:]:
            layer = layers.get(kind)
            if layer is None:
                continue
            source_layer = layers[str(layer.derived_from)]
            source_text = {u.utterance_id: u.text for u in source_layer.utterances}
            for utterance in layer.utterances:
                before = source_text.get(utterance.utterance_id, raw_by_id[utterance.utterance_id])
                if kind == "normalized":
                    # A normalized layer says the words are the same words. Held to it.
                    revision_kind = "normalization"
                    transcript_policy.assert_revision_is_honest(
                        kind=revision_kind,
                        before=before,
                        after=utterance.text,
                        reference=f"the {kind} layer of {utterance.utterance_id}",
                    )
                else:
                    # A review that changed the words is a hearing; one that did not is a
                    # person confirming what the machine wrote. The row says which.
                    revision_kind = (
                        "normalization"
                        if transcript_policy.same_words(before, utterance.text)
                        else "hearing"
                    )
                planned_revisions.append(
                    (utterance.utterance_id, kind, str(layer.derived_from), revision_kind)
                )
        text_by_layer = {
            kind: {u.utterance_id: u.text for u in layer.utterances}
            for kind, layer in layers.items()
        }

        existing = {
            str(external_id): str(utterance_id)
            for utterance_id, external_id in database.query(
                "SELECT utterance_id, external_id FROM utterances WHERE track_id = ?", [track_id]
            )
        }
        now = aware_utc(database.now())
        imported = 0
        skipped = 0
        written: list[str] = []
        with database.transaction() as transaction:
            minted: dict[str, str] = {}
            for sequence, utterance in enumerate(raw_layer.utterances, start=1):
                if utterance.utterance_id in existing:
                    minted[utterance.utterance_id] = existing[utterance.utterance_id]
                    skipped += 1
                    continue
                visibility, kept, digest = _retained_text(utterance.text, preferences=preferences)
                utterance_id = str(UtteranceId.new())
                minted[utterance.utterance_id] = utterance_id
                transaction.execute(
                    "INSERT INTO utterances (utterance_id, track_id, external_id, ingestion_id, "
                    "session_id, source_id, speaker, sequence, started_at, ended_at, raw_text, "
                    "raw_confidence, audio_artifact_id, visibility, text_hash, policy_version, "
                    "recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                    [
                        utterance_id,
                        track_id,
                        utterance.utterance_id,
                        ingestion_id,
                        session_id,
                        str(utterance.speaker),
                        sequence,
                        naive_utc(aware_utc(utterance.started_at)),
                        naive_utc(aware_utc(utterance.ended_at)),
                        kept or "",
                        visibility,
                        digest,
                        transcript_policy.TRANSCRIPT_POLICY_VERSION,
                        naive_utc(now),
                    ],
                )
                imported += 1
                written.append(utterance_id)
            for external_id, layer_kind, derived_from, revision_kind in planned_revisions:
                utterance_id = minted[external_id]
                present = transaction.one(
                    "SELECT 1 FROM transcript_revisions WHERE utterance_id = ? AND layer = ?",
                    [utterance_id, layer_kind],
                )
                if present is not None:
                    continue
                text = text_by_layer[layer_kind][external_id]
                visibility, kept, _ = _retained_text(text, preferences=preferences)
                revision_id = str(RevisionId.new())
                transaction.execute(
                    "INSERT INTO transcript_revisions (revision_id, utterance_id, layer, "
                    "derived_from, kind, text, reviewer_kind, reviewer, reason, confidence, "
                    "visibility, revision_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'ai', ?, ?, 'medium', ?, ?, ?)",
                    [
                        revision_id,
                        utterance_id,
                        layer_kind,
                        derived_from,
                        revision_kind,
                        kept or "",
                        None if validated.package_id is None else str(validated.package_id),
                        f"imported with the {layer_kind} layer of the package",
                        visibility,
                        _revision_hash(
                            external_id=external_id,
                            layer=layer_kind,
                            kind=revision_kind,
                            text=text,
                        ),
                        naive_utc(now),
                    ],
                )
                written.append(revision_id)
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps(written),
                after_summary=f"{imported} utterances imported, {skipped} already present",
            )
    if retention != "full":
        warnings.append(
            f"this track keeps the learner's words as {retention}; the transcript is stored "
            "under that rule and the hash carries what was said"
        )
    if skipped and not imported:
        warnings.append(
            "every utterance in this package was already imported, so nothing was written; "
            "a checkpoint and the completed export of the same call share their utterances"
        )
    return ImportReport(
        track_id=track_id,
        ingestion_id=ingestion_id or "",
        imported=imported,
        skipped=skipped,
        duplicate=bool(skipped and not imported),
        retention_policy=retention,
        warnings=tuple(warnings),
    )


def _record_revision(
    paths: WorkspacePaths,
    *,
    utterance: str,
    text: str,
    layer: str,
    kind: str,
    original: str | None,
    reviewer_kind: str,
    reviewer: str | None,
    reason: str | None,
    confidence: str,
    track: str | None,
    clock: Clock | None,
    command: str,
) -> RevisionReport:
    """Add one later reading of an utterance, without touching what arrived."""

    transcript_policy.assert_known(
        confidence,
        vocabulary=("low", "medium", "high"),
        field="confidence",
        code="unknown_confidence",
    )
    transcript_policy.assert_known(
        reviewer_kind,
        vocabulary=("deterministic", "ai", "learner", "human"),
        field="reviewer_kind",
        code="unknown_reviewer_kind",
    )
    if not text.strip():
        raise LinguaWikiError(
            "empty_revision",
            "a revision has to say what was heard; an empty reading is a deletion, and the "
            "raw layer is deliberately not deletable",
            details=(ErrorDetail(field="text", reason="blank"),),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        utterance_id = _resolve_utterance(database, utterance, track_id=track_id)
        record = learner_service.track_context(database, track_id)
        preferences = _preferences(record)
        row = database.one(
            "SELECT external_id, raw_text, visibility, text_hash FROM utterances "
            "WHERE utterance_id = ?",
            [utterance_id],
        )
        if row is None:
            raise LinguaWikiError(
                "utterance_not_found",
                f"no utterance {utterance_id} in this workspace",
                details=(ErrorDetail(field="utterance", reason="unknown utterance"),),
            )
        external_id = str(row[0])
        # A revision derives from the most reviewed layer below it that actually exists:
        # normalizing what a person already re-heard would silently discard their reading.
        available = {
            str(existing_layer): (str(existing_text), str(existing_visibility))
            for existing_layer, existing_text, existing_visibility in database.query(
                "SELECT layer, text, visibility FROM transcript_revisions WHERE utterance_id = ?",
                [utterance_id],
            )
        }
        permitted = [
            candidate
            for candidate in transcript_policy.LAYER_SOURCES[layer]
            if candidate == "raw" or candidate in available
        ]
        derived_from = permitted[-1]
        transcript_policy.assert_layer_derivation(layer=layer, derived_from=derived_from)
        if derived_from == "raw":
            stored_text, visibility, text_hash = str(row[1]), str(row[2]), row[3]
        else:
            stored_text, visibility = available[derived_from]
            text_hash = None
        if kind == "normalization":
            before = _verified_original(
                stored_text=stored_text,
                visibility=visibility,
                text_hash=None if text_hash is None else str(text_hash),
                supplied=original,
            )
            transcript_policy.assert_revision_is_honest(
                kind=kind,
                before=before,
                after=text,
                reference=f"this revision of {external_id}",
            )
        kept_visibility, kept, _ = _retained_text(text, preferences=preferences)
        revision_id = str(RevisionId.new())
        now = aware_utc(database.now())
        superseded = layer in available
        with database.transaction() as transaction:
            if superseded:
                # A second review of the same layer is a new hearing, not an edit of the
                # old one -- but only one row per layer can be the current reading, so the
                # superseded row goes and the audit entry keeps that it was there.
                transaction.execute(
                    "DELETE FROM transcript_revisions WHERE utterance_id = ? AND layer = ?",
                    [utterance_id, layer],
                )
            transaction.execute(
                "INSERT INTO transcript_revisions (revision_id, utterance_id, layer, "
                "derived_from, kind, text, reviewer_kind, reviewer, reason, confidence, "
                "visibility, revision_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    revision_id,
                    utterance_id,
                    layer,
                    derived_from,
                    kind,
                    kept or "",
                    reviewer_kind,
                    reviewer,
                    reason,
                    confidence,
                    kept_visibility,
                    _revision_hash(external_id=external_id, layer=layer, kind=kind, text=text),
                    naive_utc(now),
                ],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([revision_id]),
                after_summary=(
                    f"{kind} of {external_id} recorded at the {layer} layer"
                    + (", replacing an earlier reading" if superseded else "")
                ),
            )
    return RevisionReport(
        revision_id=revision_id,
        layer=layer,
        derived_from=derived_from,
        kind=kind,
        text=kept,
        reviewer_kind=reviewer_kind,
        confidence=confidence,
        created_at=now.isoformat(),
    )


def normalize(
    paths: WorkspacePaths,
    *,
    utterance: str,
    text: str,
    original: str | None = None,
    reviewer_kind: str = "deterministic",
    reviewer: str | None = None,
    reason: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "transcript.normalize",
) -> RevisionReport:
    """Tidy up an utterance without changing which words were heard.

    Held to exactly that: a normalization whose words differ from the layer it came from is
    refused, because it is a claim that the transcription misheard, and that claim decides
    whether the learner is corrected for something they never said.
    """

    return _record_revision(
        paths,
        utterance=utterance,
        text=text,
        layer="normalized",
        kind="normalization",
        original=original,
        reviewer_kind=reviewer_kind,
        reviewer=reviewer,
        reason=reason,
        confidence="high",
        track=track,
        clock=clock,
        command=command,
    )


def review(
    paths: WorkspacePaths,
    *,
    utterance: str,
    text: str,
    original: str | None = None,
    reviewer_kind: str = "human",
    reviewer: str | None = None,
    reason: str | None = None,
    confidence: str = "medium",
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "transcript.review",
) -> RevisionReport:
    """Record what a person heard when they listened again.

    The revision's *kind* is derived from what it did rather than declared: a review that
    changed the words is a hearing claim, and one that did not is somebody confirming the
    transcription. Both are worth having, and calling them the same thing would lose the
    difference between "the machine was wrong" and "the machine was right".
    """

    return _record_revision(
        paths,
        utterance=utterance,
        text=text,
        layer="reviewed-hearing",
        kind="hearing",
        original=original,
        reviewer_kind=reviewer_kind,
        reviewer=reviewer,
        reason=reason,
        confidence=confidence,
        track=track,
        clock=clock,
        command=command,
    )


def interpret(
    paths: WorkspacePaths,
    *,
    utterance: str,
    classification: str,
    meaning: str | None = None,
    corrected_form: str | None = None,
    explanation: str | None = None,
    confidence: str = "medium",
    reviewer_kind: str = "ai",
    reviewer: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "transcript.interpret",
) -> InterpretationReport:
    """Say what an utterance was: a learner's mistake, or the transcription's.

    Only `learner-error` counts against the learner. The other two classifications exist so
    that a suspicious line can be recorded *without* becoming a correction: teaching a
    mishearing back as a mistake is worse than losing it, because the learner then practises
    away from a form they had right.
    """

    transcript_policy.assert_known(
        classification,
        vocabulary=transcript_policy.CLASSIFICATIONS,
        field="classification",
        code="unknown_classification",
    )
    transcript_policy.assert_known(
        confidence,
        vocabulary=("low", "medium", "high"),
        field="confidence",
        code="unknown_confidence",
    )
    if classification == "learner-error" and not corrected_form:
        raise LinguaWikiError(
            "correction_required",
            "calling this a learner error without saying what the correct form is leaves a "
            "mark against the learner and nothing to learn from it",
            details=(ErrorDetail(field="corrected_form", reason="missing"),),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        utterance_id = _resolve_utterance(database, utterance, track_id=track_id)
        interpretation_id = str(InterpretationId.new())
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO utterance_interpretations (interpretation_id, utterance_id, "
                "classification, meaning, corrected_form, explanation, confidence, "
                "reviewer_kind, reviewer, error_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                [
                    interpretation_id,
                    utterance_id,
                    classification,
                    None if meaning is None else meaning[:2000],
                    None if corrected_form is None else corrected_form[:2000],
                    None if explanation is None else explanation[:2000],
                    confidence,
                    reviewer_kind,
                    reviewer,
                    naive_utc(now),
                ],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([interpretation_id]),
                after_summary=f"utterance classified as {classification}",
            )
    return InterpretationReport(
        interpretation_id=interpretation_id,
        classification=classification,
        meaning=meaning,
        corrected_form=corrected_form,
        explanation=explanation,
        confidence=confidence,
        reviewer_kind=reviewer_kind,
        counts_against_the_learner=classification == "learner-error",
    )


def record_pronunciation(
    paths: WorkspacePaths,
    *,
    dimension: str,
    status: str,
    basis: str = "transcript",
    utterance: str | None = None,
    audio: str | None = None,
    target: str | None = None,
    note: str | None = None,
    reviewer_kind: str = "ai",
    reviewer: str | None = None,
    observed_at: datetime | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "transcript.pronunciation",
) -> PronunciationReport:
    """Record a judgement about how something sounded, only where sound supports it.

    The rule this enforces is the reason the stage exists. A transcript says which words
    were produced; it says nothing about how they were said. So a `confirmed` claim needs
    the audio, and prosody and native-likeness need it whatever the confidence, because the
    text of a question and the text of a flat statement are identical.

    An audio basis also has to name audio that is still *there*. Pointing at a purged
    recording would leave a claim that reads as evidenced and cannot be checked by anyone,
    including the learner it is about.
    """

    transcript_policy.assert_acoustic_claim_has_audio(
        status=status,
        dimension=dimension,
        basis=basis,
        reference="this observation",
    )
    transcript_policy.assert_known(
        reviewer_kind,
        vocabulary=("deterministic", "ai", "learner", "human"),
        field="reviewer_kind",
        code="unknown_reviewer_kind",
    )
    if basis == "audio" and audio is None:
        raise LinguaWikiError(
            "audio_artifact_required",
            "an audio-based claim has to name the recording it rests on; without it the "
            "basis is the transcript, whatever the claim says",
            details=(ErrorDetail(field="audio", reason="missing"),),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        utterance_id = (
            None
            if utterance is None
            else _resolve_utterance(database, utterance, track_id=track_id)
        )
        artifact_id: str | None = None
        if audio is not None:
            row = database.one(
                "SELECT artifact_id, kind, retained, purged_at FROM artifacts "
                "WHERE artifact_id = ? AND track_id = ?",
                [audio, track_id],
            )
            if row is None:
                raise LinguaWikiError(
                    "artifact_not_found",
                    f"this track has no artifact {audio}; `artifact list` shows what is "
                    "registered, including what has been purged",
                    details=(ErrorDetail(field="audio", reason=audio),),
                )
            artifact_id, kind, retained, purged_at = (
                str(row[0]),
                str(row[1]),
                bool(row[2]),
                row[3],
            )
            if kind != "audio":
                raise LinguaWikiError(
                    "artifact_is_not_audio",
                    f"{artifact_id} is registered as {kind}; an acoustic claim has to rest "
                    "on the sound, and a transcript file is the thing it cannot rest on",
                    details=(ErrorDetail(field="audio", reason=kind),),
                )
            if purged_at is not None or not retained:
                raise LinguaWikiError(
                    "audio_not_available",
                    f"{artifact_id} is no longer held in this workspace, so a claim cannot "
                    "be based on it: nobody -- including the learner it is about -- could "
                    "check it. Record what the transcript supports instead.",
                    details=(ErrorDetail(field="audio", reason="purged"),),
                )
        if target is not None:
            # The graph's own resolver: it accepts a content ID, a stable key, or an
            # alias, and scopes every one of them to this track. Reimplementing the lookup
            # here is how an observation ends up attached to another pack's item.
            from linguawiki.services import knowledge as knowledge_service

            target = knowledge_service.resolve_item(database, target, track_id=track_id)
        observation_id = str(PronunciationId.new())
        moment = aware_utc(observed_at) if observed_at is not None else aware_utc(database.now())
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO pronunciation_observations (observation_id, track_id, utterance_id, "
                "dimension, status, basis, audio_artifact_id, target_content_id, note, "
                "reviewer_kind, reviewer, invalidated_at, invalidation_reason, policy_version, "
                "observed_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                [
                    observation_id,
                    track_id,
                    utterance_id,
                    dimension,
                    status,
                    basis,
                    artifact_id,
                    target,
                    None if note is None else note[:2000],
                    reviewer_kind,
                    reviewer,
                    transcript_policy.TRANSCRIPT_POLICY_VERSION,
                    naive_utc(moment),
                    naive_utc(now),
                ],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([observation_id]),
                after_summary=f"{dimension} {status} from a {basis} basis",
            )
    return PronunciationReport(
        observation_id=observation_id,
        dimension=dimension,
        status=status,
        basis=basis,
        audio_artifact_id=artifact_id,
        note=note,
        observed_at=moment.isoformat(),
    )


def _revisions(database: Database, *, utterance_id: str) -> tuple[RevisionReport, ...]:
    return tuple(
        RevisionReport(
            revision_id=str(row[0]),
            layer=str(row[1]),
            derived_from=str(row[2]),
            kind=str(row[3]),
            text=None if row[5] == "withheld" else str(row[4]),
            reviewer_kind=str(row[6]),
            confidence=str(row[7]),
            created_at=aware_utc(row[8]).isoformat(),
        )
        for row in database.query(
            "SELECT revision_id, layer, derived_from, kind, text, visibility, reviewer_kind, "
            "confidence, created_at FROM transcript_revisions WHERE utterance_id = ? "
            "ORDER BY created_at, revision_id",
            [utterance_id],
        )
    )


def _interpretations(database: Database, *, utterance_id: str) -> tuple[InterpretationReport, ...]:
    return tuple(
        InterpretationReport(
            interpretation_id=str(row[0]),
            classification=str(row[1]),
            meaning=None if row[2] is None else str(row[2]),
            corrected_form=None if row[3] is None else str(row[3]),
            explanation=None if row[4] is None else str(row[4]),
            confidence=str(row[5]),
            reviewer_kind=str(row[6]),
            counts_against_the_learner=str(row[1]) == "learner-error",
        )
        for row in database.query(
            "SELECT interpretation_id, classification, meaning, corrected_form, explanation, "
            "confidence, reviewer_kind FROM utterance_interpretations WHERE utterance_id = ? "
            "ORDER BY created_at, interpretation_id",
            [utterance_id],
        )
    )


def _pronunciation(database: Database, *, utterance_id: str) -> tuple[PronunciationReport, ...]:
    return tuple(
        PronunciationReport(
            observation_id=str(row[0]),
            dimension=str(row[1]),
            status=str(row[2]),
            basis=str(row[3]),
            audio_artifact_id=None if row[4] is None else str(row[4]),
            note=None if row[5] is None else str(row[5]),
            invalidated_at=None if row[6] is None else aware_utc(row[6]).isoformat(),
            invalidation_reason=None if row[7] is None else str(row[7]),
            observed_at=aware_utc(row[8]).isoformat(),
        )
        for row in database.query(
            "SELECT observation_id, dimension, status, basis, audio_artifact_id, note, "
            "invalidated_at, invalidation_reason, observed_at FROM pronunciation_observations "
            "WHERE utterance_id = ? ORDER BY observed_at, observation_id",
            [utterance_id],
        )
    )


def show(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    ingestion: str | None = None,
    utterance: str | None = None,
    limit: int = 200,
    track: str | None = None,
    clock: Clock | None = None,
) -> TranscriptReport:
    """Read a transcript back with its layers kept apart.

    Layers are reported, not collapsed. `disagreement` says where they differ and
    `best_layer` says which reading a correction should quote -- but both are shown,
    because how far the transcription can be trusted is the thing a learner needs before
    believing anything derived from it.
    """

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        retention = retention_policy(_preferences(record))
        conditions = ["track_id = ?"]
        parameters: list[Any] = [track_id]
        if session is not None:
            conditions.append("session_id = ?")
            parameters.append(session)
        if ingestion is not None:
            conditions.append("ingestion_id = ?")
            parameters.append(ingestion)
        if utterance is not None:
            resolved = _resolve_utterance(database, utterance, track_id=track_id)
            conditions.append("utterance_id = ?")
            parameters.append(resolved)
        where = " AND ".join(conditions)
        total = int(database.scalar(f"SELECT count(*) FROM utterances WHERE {where}", parameters))
        rows = database.query(
            f"SELECT utterance_id, external_id, speaker, sequence, started_at, ended_at, "
            f"raw_text, visibility, raw_confidence, audio_artifact_id, session_id, ingestion_id "
            f"FROM utterances WHERE {where} ORDER BY started_at, sequence LIMIT {int(limit)}",
            parameters,
        )
        available_audio = {
            str(artifact_id)
            for (artifact_id,) in database.query(
                "SELECT artifact_id FROM artifacts WHERE track_id = ? AND purged_at IS NULL "
                "AND retained",
                [track_id],
            )
        }
        reports: list[UtteranceReport] = []
        external_session_id: str | None = None
        session_id: str | None = None
        for row in rows:
            utterance_id = str(row[0])
            revisions = _revisions(database, utterance_id=utterance_id)
            visibility = str(row[7])
            hearings: list[transcript_policy.Hearing] = []
            if visibility != "withheld":
                hearings.append(transcript_policy.Hearing(layer="raw", text=str(row[6])))
            hearings.extend(
                transcript_policy.Hearing(layer=revision.layer, text=revision.text)
                for revision in revisions
                if revision.text is not None
            )
            best = transcript_policy.best_hearing(hearings)
            audio_artifact_id = None if row[9] is None else str(row[9])
            if row[10] is not None:
                session_id = str(row[10])
            reports.append(
                UtteranceReport(
                    utterance_id=utterance_id,
                    external_id=str(row[1]),
                    speaker=str(row[2]),
                    sequence=int(row[3]),
                    started_at=aware_utc(row[4]).isoformat(),
                    ended_at=aware_utc(row[5]).isoformat(),
                    raw_text=None if visibility == "withheld" else str(row[6]),
                    visibility=visibility,
                    raw_confidence=None if row[8] is None else float(row[8]),
                    audio_artifact_id=audio_artifact_id,
                    audio_available=audio_artifact_id in available_audio,
                    revisions=revisions,
                    interpretations=_interpretations(database, utterance_id=utterance_id),
                    pronunciation=_pronunciation(database, utterance_id=utterance_id),
                    disagreement=transcript_policy.disagreement(hearings),
                    best_layer="raw" if best is None else best.layer,
                )
            )
        if ingestion is not None:
            package_row = database.one(
                "SELECT external_session_id FROM session_packages WHERE ingestion_id = ?",
                [ingestion],
            )
            external_session_id = None if package_row is None else str(package_row[0])
    warnings: list[str] = []
    if total > len(reports):
        warnings.append(
            f"{total} utterances match and {len(reports)} are shown; raise --limit to see the rest"
        )
    if retention == "withheld":
        warnings.append(
            "this track keeps only a hash of what was said, so the transcript is reported "
            "without its words; the layers, the disagreements, and the claims are all still "
            "here"
        )
    return TranscriptReport(
        track_id=track_id,
        ingestion_id=ingestion,
        session_id=session or session_id,
        external_session_id=external_session_id,
        utterances=tuple(reports),
        total=total,
        retention_policy=retention,
        warnings=tuple(warnings),
    )


__all__ = [
    "ImportReport",
    "InterpretationReport",
    "PronunciationReport",
    "RevisionReport",
    "TranscriptReport",
    "UtteranceReport",
    "import_package",
    "interpret",
    "normalize",
    "record_pronunciation",
    "retention_policy",
    "review",
    "show",
]
