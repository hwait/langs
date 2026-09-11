"""Cataloguing material the learner works through, and recording what it taught them.

The commands here are deliberately thin over `sources.py`, which owns every rule. What
this module adds is the database: resolving a source within its own track, refusing an
excerpt the rights do not permit *before* it is stored, and keeping the comprehension
observations in the order that makes unaided comprehension meaningful.

One thing is worth stating plainly because it shapes the whole module: a source belongs
to a track, not to a workspace. Two learners reading the same book have two catalogue
entries, because progress, comprehension, and rights notes are facts about a learner's
relationship with the material rather than about the material.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from linguawiki import sources as source_policy
from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer, quote_identifier
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId, ObservationId, SourceId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import learners as learner_service


class SourceUnitReport(ContractModel):
    unit_id: str
    sequence: int
    label: str
    starts_at_ms: int | None = None
    ends_at_ms: int | None = None
    start_locator: str | None = None
    end_locator: str | None = None
    excerpt: str | None = None
    difficulty: float | None = None
    #: Whether the learner worked this unit through. Coverage counts completions, so a
    #: reread does not report as reading the source twice.
    completed: bool = False
    #: What the learner understood of it, and with how much help.
    unaided_band: str | None = None
    aided_band: str | None = None


class SourceProgressReport(ContractModel):
    status: str
    mode: str
    position_unit_id: str | None = None
    position_label: str | None = None
    completed_units: int = 0
    total_units: int | None = None
    #: `None` when the source has no known length -- a podcast feed has no last episode,
    #: and a fraction of a guess is a number that looks like knowledge.
    coverage: float | None = None
    minutes_spent: int = 0
    #: The best band reached *anywhere* in the source without help, and with it. A
    #: summary of the units that were measured, not a claim about the whole work: the
    #: counts below are what make it readable, because one easy chapter read unaided
    #: would otherwise report the entire book as understood unaided.
    unaided_band: str | None = None
    aided_band: str | None = None
    units_measured: int = 0
    units_measured_unaided: int = 0
    started_at: str | None = None
    last_worked_at: str | None = None
    completed_at: str | None = None


class SourceReport(ContractModel):
    source_id: str
    track_id: str
    kind: str
    status: str
    title: str
    creator: str | None = None
    canonical_uri: str | None = None
    language: str
    level_code: str | None = None
    rights: str
    rights_note: str | None = None
    has_audio: bool = False
    has_transcript: bool = False
    total_units: int | None = None
    notes: str | None = None
    units: tuple[SourceUnitReport, ...] = ()
    progress: SourceProgressReport | None = None
    #: What this source has given the learner: items noted, errors surfaced.
    extracted_items: int = 0
    policy_version: str
    created_at: str
    warnings: tuple[str, ...] = ()


class SourceListing(ContractModel):
    track_id: str
    total: int
    sources: tuple[SourceReport, ...] = ()
    warnings: tuple[str, ...] = ()


class ComprehensionReport(ContractModel):
    observation_id: str
    source_id: str
    unit_id: str | None = None
    sequence: int
    aid: str
    band: str
    mode: str
    replays: int = 0
    lookups: int = 0
    #: What the source's progress row says after folding this in.
    progress: SourceProgressReport
    observed_at: str
    warnings: tuple[str, ...] = ()


def _resolve_source(database: Database, source: str, *, track_id: str) -> str:
    """Resolve a source reference inside the track that owns it.

    Scoped the same way a knowledge item is, and for the same reason: a source
    identifier from another learner's track is not missing, it is *theirs*, and the
    refusal says so rather than reporting it as unknown.
    """

    row = database.one("SELECT source_id, track_id FROM sources WHERE source_id = ?", [source])
    if row is None:
        row = database.one(
            "SELECT source_id, track_id FROM sources WHERE track_id = ? AND title = ?",
            [track_id, source],
        )
    if row is None:
        raise LinguaWikiError(
            "source_not_found",
            f"no source {source} on this track; `source list` shows what is catalogued",
            details=(ErrorDetail(field="source", reason="unknown source"),),
        )
    if str(row[1]) != track_id:
        raise LinguaWikiError(
            "source_out_of_scope",
            f"source {source} belongs to track {row[1]}, not this one; a learner's "
            "reading list is their own",
            details=(ErrorDetail(field="source", reason=str(row[1])),),
        )
    return str(row[0])


def _progress(database: Database, *, track_id: str, source_id: str) -> SourceProgressReport:
    row = database.one(
        "SELECT progress.status, progress.mode, progress.position_unit_id, unit.label, "
        "progress.completed_units, source.total_units, progress.coverage, "
        "progress.minutes_spent, progress.unaided_band, progress.aided_band, "
        "progress.started_at, progress.last_worked_at, progress.completed_at "
        "FROM track_source_progress progress "
        "JOIN sources source ON source.source_id = progress.source_id "
        "LEFT JOIN source_units unit ON unit.unit_id = progress.position_unit_id "
        "WHERE progress.track_id = ? AND progress.source_id = ?",
        [track_id, source_id],
    )
    measured = database.one(
        "SELECT count(DISTINCT unit_id), "
        "count(DISTINCT CASE WHEN aid = 'unaided' THEN unit_id END) "
        "FROM comprehension_observations WHERE track_id = ? AND source_id = ?",
        [track_id, source_id],
    )
    units_measured = int(measured[0]) if measured else 0
    units_measured_unaided = int(measured[1]) if measured else 0
    if row is None:
        total = database.scalar("SELECT total_units FROM sources WHERE source_id = ?", [source_id])
        return SourceProgressReport(status="not-started", mode="intensive", total_units=total)
    return SourceProgressReport(
        status=str(row[0]),
        mode=str(row[1]),
        position_unit_id=None if row[2] is None else str(row[2]),
        position_label=None if row[3] is None else str(row[3]),
        completed_units=int(row[4]),
        total_units=None if row[5] is None else int(row[5]),
        coverage=None if row[6] is None else float(row[6]),
        minutes_spent=int(row[7]),
        unaided_band=None if row[8] is None else str(row[8]),
        aided_band=None if row[9] is None else str(row[9]),
        units_measured=units_measured,
        units_measured_unaided=units_measured_unaided,
        started_at=None if row[10] is None else aware_utc(row[10]).isoformat(),
        last_worked_at=None if row[11] is None else aware_utc(row[11]).isoformat(),
        completed_at=None if row[12] is None else aware_utc(row[12]).isoformat(),
    )


def _observations(
    database: Database, *, track_id: str, source_id: str
) -> dict[str | None, list[source_policy.Comprehension]]:
    """Every comprehension observation of this source, grouped by the unit it was about."""

    grouped: dict[str | None, list[source_policy.Comprehension]] = {}
    for unit_id, aid, band, sequence in database.query(
        "SELECT unit_id, aid, band, sequence FROM comprehension_observations "
        "WHERE track_id = ? AND source_id = ? ORDER BY sequence",
        [track_id, source_id],
    ):
        grouped.setdefault(None if unit_id is None else str(unit_id), []).append(
            source_policy.Comprehension(aid=str(aid), band=str(band), sequence=int(sequence))
        )
    return grouped


def _units(database: Database, *, track_id: str, source_id: str) -> tuple[SourceUnitReport, ...]:
    """A source's units, each carrying what the learner understood of it.

    The bands are computed through the policy rather than by SQL aggregation, because
    band order is a policy ordering and not an alphabetical one: `max()` over the strings
    would rank `none` above `gist` and report a learner who understood nothing as having
    understood the most.
    """

    observed = _observations(database, track_id=track_id, source_id=source_id)
    return tuple(
        SourceUnitReport(
            unit_id=str(row[0]),
            sequence=int(row[1]),
            label=str(row[2]),
            starts_at_ms=None if row[3] is None else int(row[3]),
            ends_at_ms=None if row[4] is None else int(row[4]),
            start_locator=None if row[5] is None else str(row[5]),
            end_locator=None if row[6] is None else str(row[6]),
            excerpt=None if row[7] is None else str(row[7]),
            difficulty=None if row[8] is None else float(row[8]),
            completed=row[9] is not None,
            unaided_band=source_policy.unaided_comprehension(observed.get(str(row[0]), [])),
            aided_band=source_policy.aided_comprehension(observed.get(str(row[0]), [])),
        )
        for row in database.query(
            "SELECT unit.unit_id, unit.sequence, unit.label, unit.starts_at_ms, "
            "unit.ends_at_ms, unit.start_locator, unit.end_locator, unit.excerpt, "
            "unit.difficulty, unit.completed_at "
            "FROM source_units unit WHERE unit.source_id = ? ORDER BY unit.sequence",
            [source_id],
        )
    )


def _read_source(database: Database, *, track_id: str, source_id: str) -> SourceReport:
    row = database.one(
        "SELECT source_id, track_id, kind, status, title, creator, canonical_uri, "
        "language, level_code, rights, rights_note, has_audio, has_transcript, "
        "total_units, notes, policy_version, created_at FROM sources WHERE source_id = ?",
        [source_id],
    )
    if row is None:
        raise LinguaWikiError(
            "source_not_found",
            f"no source {source_id} in this workspace",
            details=(ErrorDetail(field="source", reason="unknown source"),),
        )
    extracted = int(
        database.scalar(
            "SELECT count(*) FROM source_item_links link "
            "JOIN source_units unit ON unit.unit_id = link.unit_id "
            "WHERE unit.source_id = ?",
            [source_id],
        )
    )
    return SourceReport(
        source_id=str(row[0]),
        track_id=str(row[1]),
        kind=str(row[2]),
        status=str(row[3]),
        title=str(row[4]),
        creator=None if row[5] is None else str(row[5]),
        canonical_uri=None if row[6] is None else str(row[6]),
        language=str(row[7]),
        level_code=None if row[8] is None else str(row[8]),
        rights=str(row[9]),
        rights_note=None if row[10] is None else str(row[10]),
        has_audio=bool(row[11]),
        has_transcript=bool(row[12]),
        total_units=None if row[13] is None else int(row[13]),
        notes=None if row[14] is None else str(row[14]),
        units=_units(database, track_id=track_id, source_id=source_id),
        progress=_progress(database, track_id=track_id, source_id=source_id),
        extracted_items=extracted,
        policy_version=str(row[15]),
        created_at=aware_utc(row[16]).isoformat(),
    )


def add(
    paths: WorkspacePaths,
    *,
    kind: str,
    title: str,
    language: str | None = None,
    creator: str | None = None,
    canonical_uri: str | None = None,
    level_code: str | None = None,
    rights: str = "metadata-only",
    rights_note: str | None = None,
    has_audio: bool = False,
    has_transcript: bool = False,
    total_units: int | None = None,
    notes: str | None = None,
    units: Sequence[Mapping[str, Any]] = (),
    status: str = "cataloged",
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "source.add",
) -> SourceReport:
    """Catalogue material the learner will work through.

    The rights class is the load-bearing argument and it defaults to the most
    restrictive: `metadata-only` stores the title, where to find it, and the learner's
    own notes, and nothing of the source's text. A learner who holds broader rights says
    so, rather than the system assuming it on their behalf.
    """

    source_policy.assert_policy_is_sound()
    source_policy.assert_known(
        kind, vocabulary=source_policy.SOURCE_KINDS, field="kind", code="unknown_source_kind"
    )
    source_policy.assert_known(
        rights,
        vocabulary=source_policy.RIGHTS_CLASSES,
        field="rights",
        code="unknown_rights_class",
    )
    source_policy.assert_known(
        status,
        vocabulary=source_policy.SOURCE_STATUSES,
        field="status",
        code="unknown_source_status",
    )
    if not title.strip():
        raise LinguaWikiError(
            "invalid_source_title",
            "a source needs a title to be found by",
            details=(ErrorDetail(field="title", reason="blank title"),),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        resolved_language = language or record.target_language
        existing = database.one(
            "SELECT source_id FROM sources WHERE track_id = ? AND kind = ? AND title = ? "
            "AND creator IS NOT DISTINCT FROM ?",
            [track_id, kind, title, creator],
        )
        if existing is not None:
            # Catalogued twice is one source. Returning it rather than refusing, because
            # the learner's intent -- "this book is on my list" -- is already satisfied.
            return _read_source(database, track_id=track_id, source_id=str(existing[0])).model_copy(
                update={
                    "warnings": (
                        "this source was already catalogued; its existing entry is "
                        "returned, and nothing was changed",
                    )
                }
            )
        prepared = _prepared_units(units, rights=rights, title=title)
        source_id = str(SourceId.new())
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO sources (source_id, track_id, kind, status, title, creator, "
                "canonical_uri, language, level_code, rights, rights_note, has_audio, "
                "has_transcript, total_units, notes, policy_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    source_id,
                    track_id,
                    kind,
                    status,
                    title,
                    creator,
                    canonical_uri,
                    resolved_language,
                    level_code,
                    rights,
                    rights_note,
                    has_audio,
                    has_transcript,
                    total_units if total_units is not None else (len(prepared) or None),
                    notes,
                    source_policy.SOURCE_POLICY_VERSION,
                    naive_utc(now),
                    naive_utc(now),
                ],
            )
            for unit in prepared:
                transaction.execute(
                    "INSERT INTO source_units (unit_id, source_id, parent_unit_id, sequence, "
                    "label, starts_at_ms, ends_at_ms, start_locator, end_locator, excerpt, "
                    "notes, difficulty, created_at, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        str(SourceId.new()),
                        source_id,
                        unit["sequence"],
                        unit["label"],
                        unit.get("starts_at_ms"),
                        unit.get("ends_at_ms"),
                        unit.get("start_locator"),
                        unit.get("end_locator"),
                        unit.get("excerpt"),
                        unit.get("notes"),
                        unit.get("difficulty"),
                        naive_utc(now),
                        naive_utc(now),
                    ],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([source_id]),
                after_summary=(
                    f"catalogued {kind} '{title}' as {rights} with {len(prepared)} unit(s)"
                ),
            )
            migration_module.record_domain_event(
                transaction,
                event_type="source.catalogued",
                aggregate_type="source",
                aggregate_id=source_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {"kind": kind, "rights": rights, "units": len(prepared)}, sort_keys=True
                ),
            )
        report = _read_source(database, track_id=track_id, source_id=source_id)
    warnings: list[str] = []
    if rights == "metadata-only":
        warnings.append(
            "catalogued as metadata-only, so none of the source's own text can be stored; "
            "record the learner's notes about it, and re-catalogue with the rights that "
            "apply if a short quotation is permitted"
        )
    if has_audio and not record.preferences.get("audio_retention_consent"):
        warnings.append(
            "this source has audio, and the track has not consented to retaining audio; "
            "recordings can still be worked with, but nothing acoustic will be kept"
        )
    return report.model_copy(update={"warnings": tuple(warnings)})


def _prepared_units(
    units: Sequence[Mapping[str, Any]], *, rights: str, title: str
) -> list[dict[str, Any]]:
    """Validate the units before any of them is written.

    Checked as a set rather than one at a time: a caller who supplies duplicate sequence
    numbers has described an ordering that cannot exist, and finding that out halfway
    through the insert would leave a source with an arbitrary prefix of its own contents.
    """

    prepared: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position, unit in enumerate(units, start=1):
        label = str(unit.get("label") or "").strip()
        if not label:
            raise LinguaWikiError(
                "invalid_source_unit",
                f"unit {position} of '{title}' has no label, so nothing could refer to it",
                details=(ErrorDetail(field="label", reason="blank label"),),
            )
        sequence = int(unit.get("sequence", position))
        if sequence in seen:
            raise LinguaWikiError(
                "duplicate_unit_sequence",
                f"'{title}' has two units at position {sequence}; a source's order is "
                "what a position in it means",
                details=(ErrorDetail(field="sequence", reason=str(sequence)),),
            )
        seen.add(sequence)
        excerpt = unit.get("excerpt")
        source_policy.assert_excerpt_permitted(
            None if excerpt is None else str(excerpt),
            rights=rights,
            reference=f"unit {sequence} of '{title}'",
        )
        prepared.append(
            {
                "sequence": sequence,
                "label": label,
                "starts_at_ms": unit.get("starts_at_ms"),
                "ends_at_ms": unit.get("ends_at_ms"),
                "start_locator": unit.get("start_locator"),
                "end_locator": unit.get("end_locator"),
                "excerpt": None if excerpt is None else str(excerpt),
                "notes": unit.get("notes"),
                "difficulty": unit.get("difficulty"),
            }
        )
    return prepared


def listing(
    paths: WorkspacePaths,
    *,
    status: str | None = None,
    kind: str | None = None,
    track: str | None = None,
    limit: int = 50,
    clock: Clock | None = None,
) -> SourceListing:
    """The track's catalogue, active work first."""

    if status is not None:
        source_policy.assert_known(
            status,
            vocabulary=source_policy.SOURCE_STATUSES,
            field="status",
            code="unknown_source_status",
        )
    if kind is not None:
        source_policy.assert_known(
            kind, vocabulary=source_policy.SOURCE_KINDS, field="kind", code="unknown_source_kind"
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        conditions = ["track_id = ?"]
        parameters: list[Any] = [track_id]
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        if kind is not None:
            conditions.append("kind = ?")
            parameters.append(kind)
        where = " AND ".join(conditions)
        total = int(database.scalar(f"SELECT count(*) FROM sources WHERE {where}", parameters))
        identifiers = [
            str(row[0])
            for row in database.query(
                f"SELECT source_id FROM sources WHERE {where} "
                "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'reviewed' THEN 1 "
                "WHEN 'cataloged' THEN 2 WHEN 'proposed' THEN 3 WHEN 'completed' THEN 4 "
                "ELSE 5 END, title LIMIT ?",
                [*parameters, limit],
            )
        ]
        return SourceListing(
            track_id=track_id,
            total=total,
            sources=tuple(
                _read_source(database, track_id=track_id, source_id=identity)
                for identity in identifiers
            ),
        )


def show(
    paths: WorkspacePaths,
    *,
    source: str,
    track: str | None = None,
    clock: Clock | None = None,
) -> SourceReport:
    """One source with its units, the learner's progress, and what it has given them."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = _resolve_source(database, source, track_id=track_id)
        return _read_source(database, track_id=track_id, source_id=source_id)


def _recompute_progress(
    transaction: Database,
    *,
    track_id: str,
    source_id: str,
    now: datetime,
    mode: str | None = None,
    position_unit_id: str | None = None,
    completed_units: int | None = None,
    minutes_delta: int = 0,
    status: str | None = None,
) -> None:
    """Fold the source's observations into one progress row.

    Recomputed from the observations rather than accumulated, so the bands are always a
    function of what was recorded: a corrected observation changes the answer, and a
    policy change can be replayed over the same rows.
    """

    observations = [
        source_policy.Comprehension(aid=str(aid), band=str(band), sequence=int(sequence))
        for aid, band, sequence in transaction.query(
            "SELECT aid, band, sequence FROM comprehension_observations "
            "WHERE track_id = ? AND source_id = ? ORDER BY sequence",
            [track_id, source_id],
        )
    ]
    unaided = source_policy.unaided_comprehension(observations)
    aided = source_policy.aided_comprehension(observations)
    existing = transaction.one(
        "SELECT status, mode, position_unit_id, completed_units, minutes_spent, started_at "
        "FROM track_source_progress WHERE track_id = ? AND source_id = ?",
        [track_id, source_id],
    )
    total_units = transaction.scalar(
        "SELECT total_units FROM sources WHERE source_id = ?", [source_id]
    )
    if existing is None:
        completed = max(0, completed_units or 0)
        resolved_status = status or "in-progress"
        coverage = source_policy.coverage(
            completed_units=completed,
            total_units=None if total_units is None else int(total_units),
        )
        transaction.execute(
            "INSERT INTO track_source_progress (track_id, source_id, status, mode, "
            "position_unit_id, completed_units, minutes_spent, unaided_band, aided_band, "
            "coverage, policy_version, started_at, last_worked_at, completed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                track_id,
                source_id,
                resolved_status,
                mode or "intensive",
                position_unit_id,
                completed,
                max(0, minutes_delta),
                unaided,
                aided,
                coverage,
                source_policy.SOURCE_POLICY_VERSION,
                naive_utc(now),
                naive_utc(now),
                naive_utc(now) if resolved_status == "completed" else None,
                naive_utc(now),
            ],
        )
        return
    completed = int(existing[3]) if completed_units is None else max(0, completed_units)
    resolved_status = status or (
        "in-progress" if str(existing[0]) in ("not-started", "in-progress") else str(existing[0])
    )
    coverage = source_policy.coverage(
        completed_units=completed,
        total_units=None if total_units is None else int(total_units),
    )
    transaction.execute(
        "UPDATE track_source_progress SET status = ?, mode = ?, "
        "position_unit_id = coalesce(?, position_unit_id), completed_units = ?, "
        "minutes_spent = ?, unaided_band = ?, aided_band = ?, coverage = ?, "
        "started_at = coalesce(started_at, ?), last_worked_at = ?, "
        "completed_at = CASE WHEN ? = 'completed' THEN coalesce(completed_at, ?) ELSE NULL END, "
        "updated_at = ? WHERE track_id = ? AND source_id = ?",
        [
            resolved_status,
            mode or str(existing[1]),
            position_unit_id,
            completed,
            max(0, int(existing[4]) + minutes_delta),
            unaided,
            aided,
            coverage,
            naive_utc(now),
            naive_utc(now),
            resolved_status,
            naive_utc(now),
            naive_utc(now),
            track_id,
            source_id,
        ],
    )


def record_comprehension(
    paths: WorkspacePaths,
    *,
    source: str,
    band: str,
    aid: str = "unaided",
    unit: str | None = None,
    mode: str = "intensive",
    replays: int = 0,
    lookups: int = 0,
    minutes: int | None = None,
    note: str | None = None,
    observed_at: datetime | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "source.comprehension",
) -> ComprehensionReport:
    """Record how much of a unit the learner understood, and with what help.

    The order is the evidence. An unaided reading recorded *after* an aided one is not a
    second observation -- it is the first one with the help left out -- so it is refused
    rather than stored, and the message says what to do instead. Without that rule the
    strongest kind of comprehension evidence would be the easiest kind to manufacture.
    """

    source_policy.assert_known(
        aid,
        vocabulary=source_policy.COMPREHENSION_AIDS,
        field="aid",
        code="unknown_comprehension_aid",
    )
    source_policy.assert_known(
        band,
        vocabulary=source_policy.COMPREHENSION_BANDS,
        field="band",
        code="unknown_comprehension_band",
    )
    source_policy.assert_known(
        mode, vocabulary=source_policy.STUDY_MODES, field="mode", code="unknown_study_mode"
    )
    if replays < 0 or lookups < 0:
        raise LinguaWikiError(
            "invalid_comprehension_counts",
            "a reread or a lookup cannot happen a negative number of times",
            details=(ErrorDetail(field="replays", reason=str(replays)),),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = _resolve_source(database, source, track_id=track_id)
        unit_id = None if unit is None else _resolve_unit(database, unit, source_id=source_id)
        existing = [
            source_policy.Comprehension(aid=str(row[0]), band=str(row[1]), sequence=int(row[2]))
            for row in database.query(
                "SELECT aid, band, sequence FROM comprehension_observations "
                "WHERE track_id = ? AND source_id = ? AND unit_id IS NOT DISTINCT FROM ? "
                "ORDER BY sequence",
                [track_id, source_id, unit_id],
            )
        ]
        sequence = len(existing) + 1
        proposed = source_policy.Comprehension(aid=aid, band=band, sequence=sequence)
        source_policy.assert_observation_order(
            [*existing, proposed],
            reference=f"{unit or 'this source'}",
        )
        observation_id = str(ObservationId.new())
        moment = aware_utc(observed_at) if observed_at is not None else aware_utc(database.now())
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO comprehension_observations (observation_id, track_id, source_id, "
                "unit_id, sequence, aid, band, mode, replays, lookups, minutes, note, "
                "session_id, observed_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                [
                    observation_id,
                    track_id,
                    source_id,
                    unit_id,
                    sequence,
                    aid,
                    band,
                    mode,
                    replays,
                    lookups,
                    minutes,
                    None if note is None else note[:2000],
                    naive_utc(moment),
                    naive_utc(now),
                ],
            )
            _recompute_progress(
                transaction,
                track_id=track_id,
                source_id=source_id,
                now=now,
                mode=mode,
                position_unit_id=unit_id,
                minutes_delta=minutes or 0,
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([observation_id]),
                after_summary=f"{aid} comprehension of {band} recorded",
            )
        progress = _progress(database, track_id=track_id, source_id=source_id)
    warnings: list[str] = []
    if aid == "unaided" and source_policy.band_strength(band) < source_policy.band_strength(
        source_policy.UNAIDED_THRESHOLD
    ):
        warnings.append(
            f"understood {band} unaided, which is below the {source_policy.UNAIDED_THRESHOLD} "
            "threshold; the material is above the learner's current level for unaided work"
        )
    if aid != "unaided" and not existing:
        warnings.append(
            f"the first record of this unit is a {aid} reading, so there is no unaided "
            "measurement of it and there cannot be one later; record the unaided attempt "
            "first next time, because help cannot be un-given"
        )
    return ComprehensionReport(
        observation_id=observation_id,
        source_id=source_id,
        unit_id=unit_id,
        sequence=sequence,
        aid=aid,
        band=band,
        mode=mode,
        replays=replays,
        lookups=lookups,
        progress=progress,
        observed_at=moment.isoformat(),
        warnings=tuple(warnings),
    )


def materialize_progress(
    transaction: Database,
    *,
    track_id: str,
    session_id: str,
    source: str,
    band: str,
    unit: str | None = None,
    aid: str = "unaided",
    mode: str = "intensive",
    replays: int = 0,
    lookups: int = 0,
    minutes: int | None = None,
    completed: bool = False,
    note: str | None = None,
    observed_at: datetime,
    now: datetime,
) -> str:
    """Credit one session's work on a source, inside the close's own transaction.

    This is the same write `source comprehension` performs, reached the other way: through
    the session close rather than through a command. It is here rather than in the session
    service because the rule it has to obey lives here -- an unaided reading recorded after
    an aided one is refused at close exactly as it is refused at the command, and the close
    must not be a way around it.

    Refusing at close does mean a session that staged such an event cannot finish until it
    is discarded. That is the right way round: the alternative is a close that silently
    downgrades the learner's strongest kind of comprehension evidence.
    """

    source_policy.assert_known(
        aid,
        vocabulary=source_policy.COMPREHENSION_AIDS,
        field="aid",
        code="unknown_comprehension_aid",
    )
    source_policy.assert_known(
        band,
        vocabulary=source_policy.COMPREHENSION_BANDS,
        field="band",
        code="unknown_comprehension_band",
    )
    source_policy.assert_known(
        mode, vocabulary=source_policy.STUDY_MODES, field="mode", code="unknown_study_mode"
    )
    source_id = _resolve_source(transaction, source, track_id=track_id)
    unit_id = None if unit is None else _resolve_unit(transaction, unit, source_id=source_id)
    existing = [
        source_policy.Comprehension(aid=str(row[0]), band=str(row[1]), sequence=int(row[2]))
        for row in transaction.query(
            "SELECT aid, band, sequence FROM comprehension_observations "
            "WHERE track_id = ? AND source_id = ? AND unit_id IS NOT DISTINCT FROM ? "
            "ORDER BY sequence",
            [track_id, source_id, unit_id],
        )
    ]
    sequence = len(existing) + 1
    source_policy.assert_observation_order(
        [*existing, source_policy.Comprehension(aid=aid, band=band, sequence=sequence)],
        reference=f"{unit or source}",
    )
    observation_id = str(ObservationId.new())
    transaction.execute(
        "INSERT INTO comprehension_observations (observation_id, track_id, source_id, "
        "unit_id, sequence, aid, band, mode, replays, lookups, minutes, note, "
        "session_id, observed_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            observation_id,
            track_id,
            source_id,
            unit_id,
            sequence,
            aid,
            band,
            mode,
            max(0, replays),
            max(0, lookups),
            minutes,
            None if note is None else note[:2000],
            session_id,
            naive_utc(aware_utc(observed_at)),
            naive_utc(now),
        ],
    )
    if completed and unit_id is not None:
        transaction.execute(
            "UPDATE source_units SET completed_at = coalesce(completed_at, ?), updated_at = ? "
            "WHERE unit_id = ?",
            [naive_utc(now), naive_utc(now), unit_id],
        )
    total_units = transaction.scalar(
        "SELECT total_units FROM sources WHERE source_id = ?", [source_id]
    )
    completed_units = _completed_units(transaction, source_id=source_id)
    _recompute_progress(
        transaction,
        track_id=track_id,
        source_id=source_id,
        now=now,
        mode=mode,
        position_unit_id=unit_id,
        completed_units=completed_units,
        minutes_delta=minutes or 0,
        status=(
            "completed" if total_units is not None and completed_units >= int(total_units) else None
        ),
    )
    return observation_id


def _resolve_unit(database: Database, unit: str, *, source_id: str) -> str:
    """Resolve a unit by identifier, label, or position, inside its own source."""

    row = database.one(
        "SELECT unit_id FROM source_units WHERE unit_id = ? AND source_id = ?",
        [unit, source_id],
    )
    if row is None:
        row = database.one(
            "SELECT unit_id FROM source_units WHERE source_id = ? AND label = ?",
            [source_id, unit],
        )
    if row is None and unit.isdigit():
        row = database.one(
            "SELECT unit_id FROM source_units WHERE source_id = ? AND sequence = ?",
            [source_id, int(unit)],
        )
    if row is None:
        raise LinguaWikiError(
            "source_unit_not_found",
            f"this source has no unit {unit}; `source show` lists its units",
            details=(ErrorDetail(field="unit", reason="unknown unit"),),
        )
    return str(row[0])


def position(
    paths: WorkspacePaths,
    *,
    source: str,
    unit: str | None = None,
    mode: str | None = None,
    minutes: int | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "source.position",
) -> SourceReport:
    """Record where the learner is in a source, and how they are working through it."""

    if mode is not None:
        source_policy.assert_known(
            mode, vocabulary=source_policy.STUDY_MODES, field="mode", code="unknown_study_mode"
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = _resolve_source(database, source, track_id=track_id)
        unit_id = None if unit is None else _resolve_unit(database, unit, source_id=source_id)
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            _recompute_progress(
                transaction,
                track_id=track_id,
                source_id=source_id,
                now=now,
                mode=mode,
                position_unit_id=unit_id,
                minutes_delta=minutes or 0,
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([source_id]),
                after_summary=f"position set to {unit or 'unchanged'}",
            )
        return _read_source(database, track_id=track_id, source_id=source_id)


def complete_unit(
    paths: WorkspacePaths,
    *,
    source: str,
    unit: str,
    minutes: int | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "source.complete-unit",
) -> SourceReport:
    """Mark one unit worked through, advancing coverage.

    Completing the same unit twice does not advance coverage twice: a reread is not more
    of the source. That is why completion is a timestamp on the unit rather than a
    counter -- a counter would have to remember what it had already counted, and every
    way of doing that is a way of getting it wrong.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = _resolve_source(database, source, track_id=track_id)
        unit_id = _resolve_unit(database, unit, source_id=source_id)
        was_complete = (
            database.scalar("SELECT completed_at FROM source_units WHERE unit_id = ?", [unit_id])
            is not None
        )
        measured = int(
            database.scalar(
                "SELECT count(*) FROM comprehension_observations "
                "WHERE track_id = ? AND unit_id = ?",
                [track_id, unit_id],
            )
        )
        now = aware_utc(database.now())
        total_units = database.scalar(
            "SELECT total_units FROM sources WHERE source_id = ?", [source_id]
        )
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE source_units SET completed_at = coalesce(completed_at, ?), "
                "updated_at = ? WHERE unit_id = ?",
                [naive_utc(now), naive_utc(now), unit_id],
            )
            completed = _completed_units(transaction, source_id=source_id)
            _recompute_progress(
                transaction,
                track_id=track_id,
                source_id=source_id,
                now=now,
                position_unit_id=unit_id,
                completed_units=completed,
                minutes_delta=minutes or 0,
                status=(
                    "completed"
                    if total_units is not None and completed >= int(total_units)
                    else None
                ),
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([unit_id]),
                after_summary=f"{completed} unit(s) of this source completed",
            )
        report = _read_source(database, track_id=track_id, source_id=source_id)
    warnings: list[str] = []
    if was_complete:
        warnings.append(
            "this unit was already complete, so coverage did not advance; a reread is "
            "not more of the source"
        )
    if not measured:
        warnings.append(
            "no comprehension was recorded for this unit, so it counts toward coverage "
            "and toward nothing else; record what the learner understood if it was worked"
        )
    return report.model_copy(update={"warnings": tuple(warnings)})


def _completed_units(database: Database, *, source_id: str) -> int:
    return int(
        database.scalar(
            "SELECT count(*) FROM source_units WHERE source_id = ? AND completed_at IS NOT NULL",
            [source_id],
        )
    )


def set_status(
    paths: WorkspacePaths,
    *,
    source: str,
    status: str,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "source.status",
) -> SourceReport:
    """Move a source through its lifecycle, refusing a move it cannot make."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = _resolve_source(database, source, track_id=track_id)
        current = str(
            database.scalar("SELECT status FROM sources WHERE source_id = ?", [source_id])
        )
        source_policy.assert_source_transition(current=current, target=status, source_id=source_id)
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE sources SET status = ?, updated_at = ? WHERE source_id = ?",
                [status, naive_utc(now), source_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([source_id]),
                before_summary=f"status {current}",
                after_summary=f"status {status}",
            )
        return _read_source(database, track_id=track_id, source_id=source_id)


def link_item(
    paths: WorkspacePaths,
    *,
    source: str,
    unit: str,
    target: str,
    target_kind: str = "knowledge-item",
    relation: str = "encountered-in",
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "source.link",
) -> SourceReport:
    """Record that a unit gave the learner something: an item, an example, an error.

    The link is provenance -- it is how "where did I meet this word?" has an answer --
    and it is a reference to the learner's own note, never a copy of the source's text.
    """

    source_policy.assert_known(
        target_kind,
        vocabulary=("knowledge-item", "example", "error-pattern"),
        field="target_kind",
        code="unknown_link_target",
    )
    source_policy.assert_known(
        relation,
        vocabulary=("encountered-in", "extracted-from", "illustrated-by", "practised-in"),
        field="relation",
        code="unknown_link_relation",
    )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = _resolve_source(database, source, track_id=track_id)
        unit_id = _resolve_unit(database, unit, source_id=source_id)
        target_id = _resolve_link_target(
            database, target, target_kind=target_kind, track_id=track_id
        )
        mode = str(
            database.scalar(
                "SELECT mode FROM track_source_progress WHERE track_id = ? AND source_id = ?",
                [track_id, source_id],
            )
            or "intensive"
        )
        extracted = int(
            database.scalar(
                "SELECT count(*) FROM source_item_links link "
                "JOIN source_units unit ON unit.unit_id = link.unit_id "
                "WHERE unit.source_id = ? AND link.relation = 'extracted-from'",
                [source_id],
            )
        )
        if relation == "extracted-from":
            source_policy.assert_mode_permits_extraction(
                mode=mode, extracted=extracted + 1, reference=f"'{source}'"
            )
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO source_item_links (unit_id, target_kind, target_id, relation, "
                "created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [unit_id, target_kind, target_id, relation, naive_utc(now)],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([unit_id, target_id]),
                after_summary=f"{target_kind} {relation} this unit",
            )
        return _read_source(database, track_id=track_id, source_id=source_id)


def _resolve_link_target(
    database: Database, target: str, *, target_kind: str, track_id: str
) -> str:
    """Resolve what a unit is being linked to, inside this track's own material."""

    if target_kind == "knowledge-item":
        from linguawiki.services import knowledge as knowledge_service

        return knowledge_service.resolve_item(database, target, track_id=track_id)
    table, column = (
        ("examples", "example_id") if target_kind == "example" else ("error_patterns", "error_id")
    )
    # Quoted rather than interpolated even though both names are literals chosen here:
    # the repository's rule is that every identifier reaching SQL goes through this, and
    # an exception "because this one is safe" is how the rule stops being checkable.
    scoped = quote_identifier(table)
    key = quote_identifier(column)
    row = database.one(f"SELECT {key} FROM {scoped} WHERE {key} = ?", [target])
    if row is None:
        raise LinguaWikiError(
            "link_target_not_found",
            f"no {target_kind} {target} in this workspace",
            details=(ErrorDetail(field="target", reason="unknown target"),),
        )
    return str(row[0])
