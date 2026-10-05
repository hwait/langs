"""The session engine: plan, run, stage, close, and recover.

One rule shapes this module, and everything awkward about it follows from that rule
being real rather than aspirational: **while a session is running, nothing about the
learner changes.** `session log` writes provisional rows and no more. Only `session
close` turns them into attempts, evidence, error occurrences, follow-ups, and notes --
in one transaction, once, and with the result stored so that a repeat answers with the
first close's report instead of doing the work twice.

The four crash states the plan requires are each a state this module can name:

- *unflushed*: observations the skill never sent. They are simply absent, and `status`
  reports the last staged sequence so the reader can see what the session holds.
- *staged*: batches landed, no close. `resume` continues, `partial-close` credits the
  valid work, `abandon` keeps it for audit and credits none of it.
- *mid-close*: the transaction rolled back, and `sessions.status` is `closing` with no
  finalization. That is a distinguishable state on purpose, and `close` retries it.
- *response lost*: the close committed and the caller never saw it. The stored result is
  returned, marked as a replay.

Two things are deliberately not decided here. What an observation *proves* belongs to
`evidence.py` and `mastery.py`; which blocks a session should hold belongs to
`planner.py`. This module reads the database, calls those, and writes down the answer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from linguawiki import idempotency
from linguawiki import planner as planner_policy
from linguawiki import session as session_policy
from linguawiki import sources as source_policy
from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.contracts import (
    STAGED_PAYLOAD_KINDS,
    SessionEventBatch,
    StagedAttemptPayload,
    StagedCorrectionPayload,
)
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError, validated_contract
from linguawiki.evidence import STRENGTH_VERSION, assert_known
from linguawiki.idempotency import canonical_hash
from linguawiki.ids import (
    ActivityId,
    BatchId,
    BlockId,
    EventId,
    FinalizationId,
    IngestionId,
    SessionId,
    StagedEventId,
)
from linguawiki.mastery import AGGREGATION_VERSION
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import errors as error_service
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import learners as learner_service
from linguawiki.services import sources as source_service

#: How far back "recently" reaches for weekly balance and repetition. The plan's balance
#: table is a weekly policy, so the window that measures it is a week.
BALANCE_WINDOW_DAYS = 7
#: How many candidate items one block type is offered. More than this is noise: the
#: planner can only schedule what a block has time for.
CANDIDATE_TARGET_LIMIT = 8
#: Events one flush may carry. A batch is meant to be one block's worth of observations;
#: a caller with more than this has stopped flushing at block boundaries.
MAXIMUM_BATCH_EVENTS = 200
#: What a session's own summary may hold, mirroring the column's CHECK. Kept here so the
#: refusal happens before the close writes anything, rather than as a raw constraint
#: error halfway through it.
MAXIMUM_SUMMARY_LENGTH = 4000

#: The origin every attempt a session materializes carries.
SESSION_ORIGIN = "session"

#: Which staged event kinds a close turns into what.
MATERIALIZED_KINDS: Mapping[str, str] = {
    "attempt.observed": "attempt",
    "correction.given": "error-occurrence",
    "pronunciation.assessment": "pronunciation",
    "source.progress": "comprehension",
    "observation.noted": "observation",
    "follow_up": "followup",
}


class SessionTargetReport(ContractModel):
    content_id: str
    title: str
    stage: str
    novel: bool
    due: bool
    priority: int = 0


class SessionActivityReport(ContractModel):
    activity_id: str
    sequence: int
    kind: str
    prompt: str
    status: str
    settings: dict[str, object] = Field(default_factory=dict)


class SessionBlockReport(ContractModel):
    block_id: str
    sequence: int
    role: str
    block_type: str
    area: str
    dimension: str
    modality: str
    planned_minutes: int
    objective: str
    rationale: tuple[str, ...] = ()
    status: str
    score: float = 0.0
    difficulty: float | None = None
    novel_targets: int = 0
    repeated: bool = False
    targets: tuple[SessionTargetReport, ...] = ()
    activities: tuple[SessionActivityReport, ...] = ()


class SessionOmissionReport(ContractModel):
    """A candidate the planner ranked highly and did not schedule."""

    block_type: str
    score: float
    reason: str


class StagedEventReport(ContractModel):
    staged_event_id: str
    batch_sequence: int
    sequence: int
    kind: str
    status: str
    evidence_basis: str
    block_id: str | None = None
    summary: str
    materialized_kind: str | None = None
    materialized_id: str | None = None
    discard_reason: str | None = None


class StagedListing(ContractModel):
    """The provisional events a session is holding, for a caller that asked for them."""

    events: tuple[StagedEventReport, ...] = ()
    total: int = 0
    warnings: tuple[str, ...] = ()


class ResumePoint(ContractModel):
    """Where a session should be picked up: the first block and activity not finished.

    Reported rather than inferred by the caller, because inferring it needs the block
    and activity statuses to be right -- which is the whole reason `session log` advances
    them and the close settles them.
    """

    block_id: str
    sequence: int
    block_type: str
    objective: str
    activity_id: str | None = None
    activity_kind: str | None = None


class SessionReport(ContractModel):
    session_id: str
    track_id: str
    status: str
    mode: str
    energy: str
    correction_mode: str
    requested_minutes: int
    planned_minutes: int
    actual_minutes: int | None = None
    timezone: str
    intent: str | None = None
    novel_target_cap: int
    novel_targets: int
    planner_version: str
    lifecycle_version: str
    blocks: tuple[SessionBlockReport, ...] = ()
    omissions: tuple[SessionOmissionReport, ...] = ()
    #: What the session is holding but has not credited: batches flushed and events
    #: staged. A reader deciding between resume, partial-close, and abandon needs both.
    batches: int = 0
    staged_events: int = 0
    last_batch_sequence: int | None = None
    #: The fingerprint of what a close would consume now. A caller confirming a close
    #: passes it back as `expected_staging`, and the close refuses if it has moved.
    staging_digest: str = ""
    #: Present while a session can still be worked. `None` once it is finished, or when
    #: every block has been worked through.
    resume_from: ResumePoint | None = None
    finalization: CloseReport | None = None
    planned_at: str
    started_at: str | None = None
    closed_at: str | None = None
    next_actions: tuple[str, ...] = ()
    #: True when a keyed command found its own earlier call and answered with the session
    #: as it stands, rather than doing the work again.
    replayed: bool = False
    warnings: tuple[str, ...] = ()


class SessionBatchReport(ContractModel):
    """What one flush did, including doing nothing because it had already landed."""

    batch_id: str
    session_id: str
    sequence: int
    idempotency_key: str
    content_hash: str
    event_count: int
    staged_events: int
    #: True when this exact batch was already stored. The caller retried; the learner's
    #: record did not change twice.
    duplicate: bool = False
    warnings: tuple[str, ...] = ()


#: The name callers have always imported. The model is named for what it reports so the
#: published contract does not confuse it with an assessment batch.
BatchReport = SessionBatchReport


class StageChangeReport(ContractModel):
    content_id: str
    title: str
    stage_before: str | None = None
    stage_after: str
    explanation: tuple[str, ...] = ()


class CloseReport(ContractModel):
    """The one report a close returns, and the one a repeat returns again."""

    session_id: str
    finalization_id: str
    outcome: str
    status: str
    staged_consumed: int = 0
    staged_discarded: int = 0
    attempts_written: int = 0
    evidence_written: int = 0
    errors_written: int = 0
    followups_written: int = 0
    observations_written: int = 0
    #: Comprehension observations written against catalogued sources. Reading and
    #: listening reach the learner's model through the close like everything else.
    comprehension_written: int = 0
    #: Which sources this session worked on, for the next plan to continue rather than
    #: start something new.
    sources_worked: tuple[str, ...] = ()
    first_batch_sequence: int | None = None
    last_batch_sequence: int | None = None
    stage_changes: tuple[StageChangeReport, ...] = ()
    errors_touched: tuple[str, ...] = ()
    dimensions_recomputed: tuple[str, ...] = ()
    calculation_versions: dict[str, str] = Field(default_factory=dict)
    projection_stale: bool = True
    summary: str | None = None
    #: True when the close had already happened and this is the stored result. The one
    #: case where the honest answer to "what did you just do" is "nothing".
    replayed: bool = False
    closed_at: str
    warnings: tuple[str, ...] = ()


class IngestReport(ContractModel):
    ingestion_id: str
    package_id: str
    package_hash: str
    session_id: str
    track_id: str
    external_session_id: str
    mode: str
    staged_events: int
    skipped_events: int = 0
    batch_id: str | None = None
    audio_available: bool = False
    retention_policy: str
    #: True when this package hash was already ingested. Nothing was staged again.
    duplicate: bool = False
    warnings: tuple[str, ...] = ()


class RecoveredEvent(ContractModel):
    """One staged event moved by a recovery: where it was, and the row it became."""

    source_staged_event_id: str
    staged_event_id: str


class RecoverReport(ContractModel):
    source_session_id: str
    target_session_id: str
    batch_id: str | None = None
    recovered: int = 0
    skipped: int = 0
    recovered_events: tuple[RecoveredEvent, ...] = ()
    #: True when this key already recovered and the report is rebuilt from what it
    #: recorded. A retry after the events moved is answered with where they went.
    replayed: bool = False
    warnings: tuple[str, ...] = ()


class SessionListEntry(ContractModel):
    """One session as a page deciding what to draw needs it."""

    session_id: str
    status: str
    mode: str
    planned_minutes: int
    planned_at: str
    staged_events: int = 0
    batches: int = 0
    finalized: bool = False
    open: bool = False
    recoverable: bool = False


class SessionListReport(ContractModel):
    track_id: str
    sessions: tuple[SessionListEntry, ...] = ()


class BatchSummary(ContractModel):
    """A flush as identifiers: never its events, which can hold the learner's words."""

    sequence: int
    idempotency_key: str
    content_hash: str
    event_count: int
    created_at: str


class StagingState(ContractModel):
    """What a close of this session would consume now, and its fingerprint."""

    digest: str
    count: int


class SessionScreen(ContractModel):
    """Everything a page needs to draw one session, read on one connection."""

    session: SessionReport
    staged: StagedListing
    #: Staged (not yet credited) events per block. Events with no block -- every
    #: recovered one among them -- are counted in `unattributed`, because a partial close
    #: excludes by block and cannot reach them.
    staged_by_block: dict[str, int] = Field(default_factory=dict)
    unattributed: int = 0
    batches: tuple[BatchSummary, ...] = ()
    missing_batch_sequences: tuple[int, ...] = ()
    staging: StagingState
    #: The operations legal now, by route operation ID. Derived from the same table as
    #: the CLI's `next_actions`, so a page and a terminal never disagree about it.
    actions: tuple[str, ...] = ()
    closing_interrupted: bool = False


def _json_list(raw: object) -> tuple[str, ...]:
    """Read a stored JSON string list, treating damage as absence rather than raising."""

    if not isinstance(raw, str):
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(entry) for entry in parsed)


def _json_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Reading the facts a plan is made of -------------------------------------------


def _preferences(track: learner_service.TrackRecord) -> Mapping[str, Any]:
    preferences = track.preferences
    return preferences if isinstance(preferences, dict) else {}


def _preference_list(track: learner_service.TrackRecord, key: str) -> tuple[str, ...]:
    value = _preferences(track).get(key)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(entry) for entry in value)
    return ()


def _recent_area_counts(database: Database, *, track_id: str, now: datetime) -> Mapping[str, int]:
    """Blocks per area completed in the balance window.

    Only completed blocks count. A planned block the learner never reached is not
    practice, and counting it would let an abandoned session suppress the area it
    promised to cover.
    """

    since = naive_utc(now - timedelta(days=BALANCE_WINDOW_DAYS))
    rows = database.query(
        "SELECT block.area, count(*) FROM session_blocks block "
        "JOIN sessions session ON session.session_id = block.session_id "
        "WHERE session.track_id = ? AND block.status = 'completed' "
        "AND session.closed_at IS NOT NULL AND session.closed_at >= ? "
        "GROUP BY block.area",
        [track_id, since],
    )
    return {str(area): int(count) for area, count in rows}


def weekly_deficits(recent: Mapping[str, int]) -> dict[str, int]:
    """How far each area is behind its weekly minimum.

    A missed week creates a deficit for future ranking and no more: the plan is explicit
    that the next session must not be overloaded trying to catch up, which is why the
    deficit feeds a *score* rather than a quota.
    """

    return {
        area: max(0, minimum - recent.get(area, 0))
        for area, minimum in session_policy.WEEKLY_BLOCK_TARGETS.items()
    }


def _dimension_facts(
    database: Database, *, track: learner_service.TrackRecord
) -> Mapping[str, tuple[float | None, float]]:
    """Ability and uncertainty per dimension, both on 0..1, from the current estimates.

    The stored score is an index on the track's own framework ladder, so it is divided
    by that ladder's span rather than by a constant: the planner must not carry a CEFR
    assumption into a track using another framework.

    A dimension with no estimate has no ability -- `None` rather than a guessed midpoint,
    because an unmeasured dimension is what the planner should be *uncertain* about, not
    confident in. `not-tested` is therefore maximum uncertainty, which is exactly what
    makes it worth a block.
    """

    span = max(1, len(track.framework_levels) - 1)
    facts: dict[str, tuple[float | None, float]] = {}
    for record in estimate_service.read_estimates(database, track_id=track.track_id):
        ability = None if record.score is None else min(1.0, max(0.0, record.score / span))
        uncertainty = (
            1.0
            if record.estimate_status == "not-tested" or record.uncertainty is None
            else min(1.0, max(0.0, record.uncertainty / span))
        )
        facts[record.dimension] = (ability, uncertainty)
    return facts


def _dimension_names(
    database: Database, *, track: learner_service.TrackRecord
) -> Mapping[tuple[str, str], str]:
    """Which of the track's own dimensions each (kind, modality) block maps onto.

    A block type says it produces *productive* evidence in *speech*; which dimension
    that is belongs to the pack. `spoken-production` in this CEFR pilot, something else
    in another framework -- and a core module that hard-coded either would be a core
    module that knows a framework.

    The pack declares each dimension's kind in its manifest; the modality comes from the
    dimension's own bank tasks, which is the only place the association is recorded. A
    dimension with no tasks still maps by kind where its kind is unambiguous, so a pack
    whose bank is thin still gets a plan.
    """

    row = database.one(
        "SELECT pack.manifest_json FROM learning_tracks track "
        "JOIN pack_installations pack ON pack.pack_id = track.pack_id "
        "WHERE track.track_id = ?",
        [track.track_id],
    )
    if row is None or row[0] is None:
        return {}
    from linguawiki.contracts import PackManifest

    kinds = dict(PackManifest.model_validate(json.loads(str(row[0]))).dimension_kinds)
    modalities: dict[str, set[str]] = {}
    for dimension, modality in database.query(
        "SELECT task.dimension, task.modality FROM assessment_tasks task "
        "JOIN content_records content ON content.content_id = task.content_id "
        "JOIN learning_tracks track ON track.track_id = ? "
        "WHERE content.pack_id = track.pack_id GROUP BY task.dimension, task.modality",
        [track.track_id],
    ):
        modalities.setdefault(str(dimension), set()).add(str(modality))
    resolved: dict[tuple[str, str], str] = {}
    for dimension, kind in sorted(kinds.items()):
        served = modalities.get(dimension, set())
        for modality in served:
            resolved.setdefault((str(kind), modality), dimension)
    # A kind with exactly one dimension needs no modality evidence to be unambiguous:
    # a pack that declares one productive dimension means that one, whatever its bank
    # happens to hold.
    by_kind: dict[str, list[str]] = {}
    for dimension, kind in sorted(kinds.items()):
        by_kind.setdefault(str(kind), []).append(dimension)
    for declared_kind, alternatives in by_kind.items():
        if len(alternatives) == 1:
            for modality in session_policy.MODALITIES:
                resolved.setdefault((declared_kind, modality), alternatives[0])
    return resolved


def _candidate_targets(
    database: Database,
    *,
    track_id: str,
    dimension: str,
    now: datetime,
    interests: Sequence[str],
    goals: Sequence[str],
) -> tuple[planner_policy.CandidateTarget, ...]:
    """The items a block on this dimension could work on, most urgent first.

    Due items come first, then unseen ones. Both are read from the track's own state, so
    an item belonging to another track's pack can never appear in this plan -- the
    scoping rule that Stage 3's review round seven was about.
    """

    rows = database.query(
        "SELECT item.content_id, item.title, "
        "coalesce(state.stage, 'unseen') AS stage, "
        "coalesce(state.priority, 0) AS priority, "
        "state.next_review_at, "
        "state.content_id IS NULL AS unseen, "
        "( "
        "  SELECT count(*) FROM knowledge_relations relation "
        "  LEFT JOIN track_item_state prerequisite "
        "    ON prerequisite.content_id = relation.target_content_id "
        "   AND prerequisite.track_id = ? "
        "  WHERE relation.source_content_id = item.content_id "
        "    AND relation.relation_type = 'prerequisite' "
        "    AND coalesce(prerequisite.stage, 'unseen') IN ('unseen', 'encountered') "
        ") AS prerequisite_gaps, "
        "( "
        "  SELECT attempt.outcome FROM attempts attempt "
        "  WHERE attempt.track_id = ? AND attempt.target_content_id = item.content_id "
        "  ORDER BY attempt.occurred_at DESC LIMIT 1 "
        ") AS last_outcome, "
        "( "
        "  SELECT attempt.task_type FROM attempts attempt "
        "  WHERE attempt.track_id = ? AND attempt.target_content_id = item.content_id "
        "  ORDER BY attempt.occurred_at DESC LIMIT 1 "
        ") AS last_task_type, "
        "( "
        "  SELECT string_agg(tagged.tag_value, ' ') FROM item_tags tagged "
        "  WHERE tagged.content_id = item.content_id "
        ") AS tags "
        "FROM knowledge_items item "
        "JOIN learning_tracks track ON track.track_id = ? "
        "LEFT JOIN content_records content ON content.content_id = item.content_id "
        "LEFT JOIN track_item_state state "
        "  ON state.content_id = item.content_id AND state.track_id = track.track_id "
        "WHERE (content.pack_id = track.pack_id OR content.pack_id IS NULL) "
        "ORDER BY "
        "  CASE WHEN state.next_review_at IS NOT NULL AND state.next_review_at <= ? "
        "       THEN 0 ELSE 1 END, "
        "  coalesce(state.priority, 0) DESC, "
        "  state.next_review_at NULLS LAST, "
        "  item.content_id "
        "LIMIT ?",
        [track_id, track_id, track_id, track_id, naive_utc(now), CANDIDATE_TARGET_LIMIT * 3],
    )
    interest_terms = tuple(term.lower() for term in interests if term)
    goal_terms = tuple(term.lower() for term in goals if term)
    targets: list[planner_policy.CandidateTarget] = []
    for row in rows:
        (
            content_id,
            title,
            stage,
            priority,
            next_review_at,
            unseen,
            prerequisite_gaps,
            last_outcome,
            last_task_type,
            tags,
        ) = row
        haystack = f"{title} {tags or ''}".lower()
        due = next_review_at is not None and aware_utc(next_review_at) <= now
        targets.append(
            planner_policy.CandidateTarget(
                content_id=str(content_id),
                title=str(title),
                stage=str(stage),
                novel=bool(unseen) or str(stage) == "unseen",
                due=bool(due),
                priority=int(priority),
                prerequisite_gaps=int(prerequisite_gaps),
                interest_match=any(term in haystack for term in interest_terms),
                goal_match=any(term in haystack for term in goal_terms),
                last_outcome=None if last_outcome is None else str(last_outcome),
                # A failed item returns only in a different shape of task. The block type
                # decides that, so the variation is resolved per candidate below.
                variation=False,
                difficulty=None,
            )
        )
    return tuple(targets[:CANDIDATE_TARGET_LIMIT])


def _voice_available(track: learner_service.TrackRecord) -> bool:
    return bool(_preferences(track).get("voice_available"))


def _correction_mode(track: learner_service.TrackRecord, requested: str | None) -> str:
    """The correction regime for the session: what was asked, or the track's default."""

    if requested is not None:
        return requested
    declared = _preferences(track).get("correction_mode")
    return str(declared) if isinstance(declared, str) else "accuracy"


def _curriculum_continuity(database: Database, *, track_id: str) -> float:
    """How much of an unfinished course unit the learner is in the middle of.

    Zero when nothing is imported, which is the honest answer rather than a neutral
    guess: a track with no curriculum has no position to continue from.
    """

    row = database.one(
        "SELECT count(*) FILTER (WHERE state = 'current'), count(*) "
        "FROM track_curriculum_progress WHERE track_id = ?",
        [track_id],
    )
    if row is None or not row[1]:
        return 0.0
    return min(1.0, float(row[0]) / float(row[1]))


def _source_continuity(database: Database, *, track_id: str) -> dict[str, float]:
    """How much unfinished material is waiting, by the block area that could use it.

    A source in progress is a commitment the learner already made, and the strongest
    reason to plan a reading block is that there is a book they are halfway through. It is
    keyed by area rather than returned as one number because a half-read novel is no
    argument for a pronunciation block: a component that applied to every area would be a
    weight on a constant, which is the thing Stage 4 refused to ship.

    Material the learner has put down is excluded, and the filter has to name the
    vocabularies that exist to do it. Stage 5 compared `source.status <> 'abandoned'` --
    and `abandoned` is a *progress* status, never a source one, so the comparison was true
    of every row and excluded nothing. An archived source went on arguing for a reading
    block once a session, which is the planner arguing with a decision the learner made.
    """

    by_area: dict[str, float] = {}
    for kind, unfinished, total in database.query(
        "SELECT source.kind, "
        "count(*) FILTER (WHERE progress.status = 'in-progress'), count(*) "
        "FROM track_source_progress progress "
        "JOIN sources source ON source.source_id = progress.source_id "
        "WHERE progress.track_id = ? AND source.status NOT IN ('archived', 'rejected') "
        "AND progress.status <> 'abandoned' GROUP BY source.kind",
        [track_id],
    ):
        if not int(total):
            continue
        share = min(1.0, float(unfinished) / float(total))
        for area in source_policy.AREAS_FOR_KIND.get(str(kind), ()):
            by_area[area] = max(by_area.get(area, 0.0), share)
    return by_area


def _transfer_value(block: session_policy.BlockType) -> float:
    """How much a block's work carries into other dimensions.

    Productive use of language transfers further than recognizing it, and a spoken block
    exercises listening alongside speaking. A policy, stated once, rather than a number
    hidden in the scoring.
    """

    if block.area in ("speaking", "pronunciation"):
        return 0.8
    if block.productive:
        return 0.6
    if block.area == "retrieval":
        return 0.5
    return 0.3


def _block_candidates(
    database: Database,
    *,
    track: learner_service.TrackRecord,
    now: datetime,
) -> tuple[planner_policy.Candidate, ...]:
    """Build one candidate per block type from what the database actually holds."""

    track_id = track.track_id
    recent = _recent_area_counts(database, track_id=track_id, now=now)
    dimensions = _dimension_facts(database, track=track)
    continuity = _curriculum_continuity(database, track_id=track_id)
    source_continuity = _source_continuity(database, track_id=track_id)
    interests = _preference_list(track, "interests")
    goals = _preference_list(track, "goals") + ((track.goal,) if track.goal else ())
    followups = int(
        database.scalar(
            "SELECT count(*) FROM followups WHERE track_id = ? AND status = 'open'",
            [track_id],
        )
    )
    active_errors_by_dimension = {
        str(dimension): int(count)
        for dimension, count in database.query(
            "SELECT coalesce(attempt.dimension, 'unassigned'), count(DISTINCT pattern.error_id) "
            "FROM error_patterns pattern "
            "LEFT JOIN error_occurrences occurrence ON occurrence.error_id = pattern.error_id "
            "LEFT JOIN attempts attempt ON attempt.attempt_id = occurrence.attempt_id "
            "WHERE pattern.track_id = ? "
            "AND pattern.status IN ('observed', 'active', 'reactivated', 'monitoring') "
            "GROUP BY 1",
            [track_id],
        )
    }
    voice = _voice_available(track)
    names = _dimension_names(database, track=track)
    candidates: list[planner_policy.Candidate] = []
    for block in session_policy.BLOCK_TYPES:
        if block.name in (session_policy.WARM_UP_BLOCK, session_policy.CLOSURE_BLOCK):
            # The framing blocks are not candidates: every session has exactly one of
            # each, and they are filled from what the core blocks chose.
            continue
        dimension = names.get((block.dimension_kind, block.modality), "")
        ability, uncertainty = dimensions.get(dimension, (None, 1.0))
        targets = _candidate_targets(
            database,
            track_id=track_id,
            dimension=dimension,
            now=now,
            interests=interests,
            goals=goals,
        )
        # A failed item may return, but not in the same shape of task. A block whose
        # modality differs from the one it failed in *is* the variation.
        targets = tuple(
            target
            if target.last_outcome != "failure"
            else _with_variation(target, block=block, database=database, track_id=track_id)
            for target in targets
        )
        if not block.introduces_novelty:
            targets = tuple(target for target in targets if not target.novel)
        unavailable_reason = None
        if block.requires_voice and not voice:
            unavailable_reason = "the track records no voice channel"
        elif not dimension:
            unavailable_reason = (
                f"this track's pack declares no {block.dimension_kind} dimension in "
                f"{block.modality}"
            )
        elif not targets:
            unavailable_reason = "no candidate item is available for this dimension"
        candidates.append(
            planner_policy.Candidate(
                block_type=block.name,
                dimension=dimension,
                targets=targets,
                due_followups=followups if block.area == "retrieval" else 0,
                active_errors=active_errors_by_dimension.get(dimension, 0),
                uncertainty=uncertainty,
                curriculum_continuity=continuity,
                source_continuity=source_continuity.get(block.area, 0.0),
                transfer_value=_transfer_value(block),
                recent_blocks=recent.get(block.area, 0),
                ability=ability,
                available=unavailable_reason is None,
                unavailable_reason=unavailable_reason,
            )
        )
    return tuple(candidates)


def _with_variation(
    target: planner_policy.CandidateTarget,
    *,
    block: session_policy.BlockType,
    database: Database,
    track_id: str,
) -> planner_policy.CandidateTarget:
    """Mark a previously failed target as varied when this block really differs.

    "Do not repeat a failed task unchanged" is only meaningful if *changed* is measured
    rather than assumed. The change we can establish from the record is the demand: a
    different modality or task type is a different task. Same modality, same failure --
    the planner leaves it out and says why.
    """

    row = database.one(
        "SELECT modality, task_type FROM attempts "
        "WHERE track_id = ? AND target_content_id = ? AND outcome = 'failure' "
        "ORDER BY occurred_at DESC LIMIT 1",
        [track_id, target.content_id],
    )
    if row is None:
        return target
    previous_modality = str(row[0])
    return planner_policy.CandidateTarget(
        content_id=target.content_id,
        title=target.title,
        stage=target.stage,
        novel=target.novel,
        due=target.due,
        priority=target.priority,
        prerequisite_gaps=target.prerequisite_gaps,
        interest_match=target.interest_match,
        goal_match=target.goal_match,
        last_outcome=target.last_outcome,
        variation=previous_modality != block.modality,
        difficulty=target.difficulty,
    )


# --- Planning ----------------------------------------------------------------------


#: Which activities each block type runs, and under which correction regime they are
#: legal. Kept here rather than in the skill so that "exam conditions and graduated
#: hints cannot share a block" is enforced by the tool instead of remembered by a prompt.
BLOCK_ACTIVITIES: Mapping[str, tuple[str, ...]] = {
    "warm-up-retrieval": ("retrieval-quiz",),
    "rich-review": ("retrieval-quiz", "controlled-practice"),
    "grammar-focus": ("elicitation", "controlled-practice"),
    "reading": ("comprehension-questions", "translation"),
    "listening": ("comprehension-questions", "shadowing"),
    "writing": ("controlled-practice", "free-production"),
    "speaking": ("role-play", "free-production"),
    "pronunciation": ("shadowing", "controlled-practice"),
    "closure-retrieval": ("retrieval-quiz",),
}

#: What an exam-mode session runs instead. Under exam conditions nothing is elicited or
#: hinted, so the activities that coach are replaced rather than filtered into nothing.
EXAM_ACTIVITIES: tuple[str, ...] = ("exam-task",)


def _activities_for(block: str, *, correction_mode: str) -> tuple[str, ...]:
    """The activities a block runs, checked against the session's correction mode."""

    planned = BLOCK_ACTIVITIES[block]
    forbidden = set(session_policy.CORRECTION_MODE_FORBIDS[correction_mode])
    remaining = tuple(kind for kind in planned if kind not in forbidden)
    if not remaining:
        remaining = EXAM_ACTIVITIES if correction_mode == "exam" else planned[:1]
    session_policy.assert_activities_compatible(
        block=block, activities=remaining, correction_mode=correction_mode
    )
    return remaining


def _activity_prompt(*, block: str, kind: str, objective: str) -> str:
    """A neutral instruction for the block, with no language-specific content in it.

    The skill turns this into teaching. Core states the demand -- what kind of work, on
    what -- and nothing about how to say anything in any language.
    """

    return f"{kind} for {block}: {objective}"


def plan_request_hash(
    *,
    track_id: str,
    minutes: int,
    mode: str,
    energy: str,
    intent: str | None,
    correction_mode: str,
) -> str:
    """The fingerprint of a plan request, so a reused key can be told from a retry.

    A key alone cannot: asking for 100 minutes of grammar under the key that planned 60
    minutes of mixed work returned the old plan, and the caller was told nothing. What
    they get now is `idempotency_conflict`, which is the same treatment a reused batch
    key gets and for the same reason -- the second call meant something different.
    """

    return canonical_hash(
        {
            "track_id": track_id,
            "minutes": minutes,
            "mode": mode,
            "energy": energy,
            "intent": intent,
            "correction_mode": correction_mode,
        }
    )


def _existing_by_key(
    database: Database, *, idempotency_key: str | None, request_hash: str
) -> str | None:
    """The session this key already planned, or a refusal if it planned a different one."""

    if idempotency_key is None:
        return None
    row = database.one(
        "SELECT session_id, request_hash FROM sessions WHERE idempotency_key = ?",
        [idempotency_key],
    )
    if row is None:
        return None
    if row[1] is not None and str(row[1]) != request_hash:
        raise LinguaWikiError(
            "idempotency_conflict",
            f"idempotency key {idempotency_key} already planned session {row[0]} from a "
            "different request; a retry must ask for the same session, and a different "
            "plan needs a new key",
            details=(
                ErrorDetail(field="idempotency_key", reason="reused for another request"),
                ErrorDetail(field="session", reason=str(row[0])),
            ),
        )
    return str(row[0])


def _write_plan(
    transaction: Database,
    *,
    track: learner_service.TrackRecord,
    plan: planner_policy.Plan,
    scores: Sequence[tuple[planner_policy.Score, bool, str | None]],
    intent: str | None,
    correction_mode: str,
    idempotency_key: str | None,
    request_hash: str,
    now: datetime,
    actor: str = "cli",
) -> str:
    """Persist a plan: the session, its blocks, their targets, and the ranking."""

    session_id = str(SessionId.new())
    transaction.execute(
        "INSERT INTO sessions (session_id, track_id, status, mode, requested_minutes, "
        "planned_minutes, energy, correction_mode, intent, novel_target_cap, timezone, "
        "planner_version, lifecycle_version, plan_warnings_json, idempotency_key, "
        "request_hash, planned_at, created_at, updated_at) "
        "VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            session_id,
            track.track_id,
            plan.mode,
            plan.requested_minutes,
            plan.planned_minutes,
            plan.energy,
            correction_mode,
            intent,
            plan.novel_target_cap,
            track.timezone,
            plan.planner_version,
            plan.lifecycle_version,
            json.dumps(list(plan.warnings), ensure_ascii=False),
            idempotency_key,
            request_hash,
            now,
            now,
            now,
        ],
    )
    for block in plan.blocks:
        block_id = str(BlockId.new())
        transaction.execute(
            "INSERT INTO session_blocks (block_id, session_id, sequence, role, block_type, "
            "area, dimension, modality, planned_minutes, objective, rationale_json, score, "
            "difficulty, novel_targets, repeated, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
            [
                block_id,
                session_id,
                block.sequence,
                block.role,
                block.block_type,
                block.area,
                block.dimension,
                block.modality,
                block.minutes,
                block.objective,
                json.dumps(list(block.rationale), ensure_ascii=False),
                block.score,
                block.difficulty,
                block.novel_targets,
                block.repeated,
                now,
                now,
            ],
        )
        for position, target in enumerate(block.targets, start=1):
            transaction.execute(
                "INSERT INTO session_block_targets (block_id, content_id, sequence, novel, "
                "due, stage, priority, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    block_id,
                    target.content_id,
                    position,
                    target.novel,
                    target.due,
                    target.stage,
                    target.priority,
                    now,
                ],
            )
        for position, kind in enumerate(
            _activities_for(block.block_type, correction_mode=correction_mode), start=1
        ):
            transaction.execute(
                "INSERT INTO activities (activity_id, block_id, sequence, kind, prompt, "
                "settings_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                [
                    str(ActivityId.new()),
                    block_id,
                    position,
                    kind,
                    _activity_prompt(block=block.block_type, kind=kind, objective=block.objective),
                    json.dumps(
                        {
                            "correction_mode": correction_mode,
                            "dimension": block.dimension,
                            "modality": block.modality,
                        },
                        sort_keys=True,
                    ),
                    now,
                    now,
                ],
            )
    for position, (score, selected, omission_reason) in enumerate(scores, start=1):
        transaction.execute(
            "INSERT INTO session_plan_candidates (session_id, block_type, sequence, score, "
            "selected, omission_reason, components_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                session_id,
                score.block_type,
                position,
                score.total,
                selected,
                omission_reason,
                json.dumps(dict(score.components), sort_keys=True),
                now,
            ],
        )
    migration_module.record_audit_entry(
        transaction,
        command="plan.create",
        actor=actor,
        correlation_id=EventId.new(),
        outcome="succeeded",
        affected_records_json=json.dumps([session_id]),
        after_summary=(
            f"{plan.mode} plan of {len(plan.blocks)} block(s) over {plan.planned_minutes} minute(s)"
        ),
    )
    migration_module.record_domain_event(
        transaction,
        event_type="session.planned",
        aggregate_type="session",
        aggregate_id=session_id,
        correlation_id=EventId.new(),
        payload_json=json.dumps(
            {
                "mode": plan.mode,
                "requested_minutes": plan.requested_minutes,
                "planned_minutes": plan.planned_minutes,
                "blocks": [block.block_type for block in plan.blocks],
                "planner_version": plan.planner_version,
            },
            sort_keys=True,
        ),
        idempotency_key=idempotency_key,
    )
    return session_id


def create(
    paths: WorkspacePaths,
    *,
    minutes: int,
    mode: str = session_policy.DEFAULT_MODE,
    energy: str = "normal",
    intent: str | None = None,
    correction_mode: str | None = None,
    track: str | None = None,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "plan.create",
    actor: str = "cli",
) -> SessionReport:
    """Plan one session: choose its blocks, and record why each of them is there."""

    session_policy.assert_known_mode(mode)
    session_policy.assert_known_energy(energy)
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        resolved_correction = _correction_mode(record, correction_mode)
        request_hash = plan_request_hash(
            track_id=track_id,
            minutes=minutes,
            mode=mode,
            energy=energy,
            intent=intent,
            correction_mode=resolved_correction,
        )
        existing = _existing_by_key(
            database, idempotency_key=idempotency_key, request_hash=request_hash
        )
        if existing is not None:
            # The same key, the same request: this session was already planned, and
            # returning it rather than planning a second one is the point of the key.
            return _read_session(database, session_id=existing)
        now = aware_utc(database.now())
        candidates = _block_candidates(database, track=record, now=now)
        request = planner_policy.PlanRequest(
            minutes=minutes,
            mode=mode,
            energy=energy,
            intent=intent,
            correction_mode=resolved_correction,
            voice_available=_voice_available(record),
            interests=_preference_list(record, "interests"),
            goals=_preference_list(record, "goals"),
        )
        recent = _recent_area_counts(database, track_id=track_id, now=now)
        framing = session_policy.block_type(session_policy.WARM_UP_BLOCK)
        framing_dimension = _dimension_names(database, track=record).get(
            (framing.dimension_kind, framing.modality), ""
        )
        if not framing_dimension:
            raise LinguaWikiError(
                "framing_dimension_unavailable",
                f"this track's pack declares no {framing.dimension_kind} dimension in "
                f"{framing.modality}, so a session cannot be framed by retrieval",
                details=(ErrorDetail(field="pack", reason="no retrieval dimension"),),
            )
        plan = planner_policy.build_plan(
            request=request,
            candidates=candidates,
            deficits=weekly_deficits(recent),
            framing_dimension=framing_dimension,
        )
        scores = _ranking(plan=plan, candidates=candidates, request=request, recent=recent)
        with database.transaction() as transaction:
            session_id = _write_plan(
                transaction,
                track=record,
                plan=plan,
                scores=scores,
                intent=intent,
                correction_mode=resolved_correction,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
                actor=actor,
            )
        return _read_session(database, session_id=session_id)


def _ranking(
    *,
    plan: planner_policy.Plan,
    candidates: Sequence[planner_policy.Candidate],
    request: planner_policy.PlanRequest,
    recent: Mapping[str, int],
) -> tuple[tuple[planner_policy.Score, bool, str | None], ...]:
    """Every candidate's score, whether it was selected, and why it was not.

    Recomputed from the same inputs the plan used rather than collected during
    selection, so the stored ranking is a function of the plan and cannot drift from it.
    An unavailable candidate is ranked too: "there is no microphone" is the most useful
    omission reason there is.
    """

    selected = {block.block_type for block in plan.blocks if block.role == "core"}
    omissions = {omission.block_type: omission.reason for omission in plan.omissions}
    deficits = weekly_deficits(recent)
    ranked: list[tuple[planner_policy.Score, bool, str | None]] = []
    for candidate in candidates:
        score = planner_policy.score_candidate(
            candidate,
            request=request,
            deficits=deficits,
            remaining_novelty=plan.novel_target_cap,
        )
        if candidate.block_type in selected:
            ranked.append((score, True, None))
            continue
        reason = omissions.get(candidate.block_type)
        if reason is None:
            if not candidate.available:
                reason = candidate.unavailable_reason or "no material was available"
            elif candidate.block_type not in session_policy.MODES[plan.mode]:
                reason = f"{plan.mode} mode does not include this block type"
            else:
                reason = "ranked below the blocks this session had room for"
        ranked.append((score, False, reason))
    ranked.sort(key=lambda entry: (-entry[0].total, entry[0].block_type))
    return tuple(ranked)


# --- Reading a session back --------------------------------------------------------


def _session_row(database: Database, session_id: str) -> Sequence[Any]:
    row = database.one(
        "SELECT session_id, track_id, status, mode, requested_minutes, planned_minutes, "
        "actual_minutes, energy, correction_mode, intent, novel_target_cap, timezone, "
        "planner_version, lifecycle_version, plan_warnings_json, summary, planned_at, "
        "started_at, closing_at, closed_at FROM sessions WHERE session_id = ?",
        [session_id],
    )
    if row is None:
        raise LinguaWikiError(
            "session_not_found",
            f"no session {session_id} in this workspace",
            details=(ErrorDetail(field="session", reason="unknown session"),),
        )
    return row


def resolve_session(database: Database, session: str | None, *, track_id: str | None = None) -> str:
    """Resolve an explicit session, or the one this track is in the middle of.

    "The one in the middle" is deliberately narrow: a session that is `planned`,
    `active`, or `closing`. A finished session is never resumed by omission, because
    continuing into a closed session would credit its work twice.
    """

    if session is not None:
        row = _session_row(database, session)
        if track_id is not None and str(row[1]) != track_id:
            raise LinguaWikiError(
                "session_track_mismatch",
                f"session {session} belongs to another track on this workspace",
                details=(ErrorDetail(field="session", reason="different track"),),
            )
        return str(row[0])
    if track_id is None:
        raise LinguaWikiError(
            "session_required",
            "name a session, or a track whose open session should be used",
            details=(ErrorDetail(field="session", reason="no session and no track"),),
        )
    rows = database.query(
        "SELECT session_id, status FROM sessions WHERE track_id = ? "
        "AND status IN ('planned', 'active', 'closing') ORDER BY planned_at DESC",
        [track_id],
    )
    if not rows:
        # A close whose response was lost is retried by name, so the message hands the
        # caller the name: "plan one first" is the wrong advice for somebody whose
        # session already closed and who only lost the answer.
        recent = database.one(
            "SELECT session_id, status FROM sessions WHERE track_id = ? "
            "ORDER BY planned_at DESC LIMIT 1",
            [track_id],
        )
        raise LinguaWikiError(
            "no_open_session",
            "this track has no planned, active, or closing session; plan one first"
            + (
                f". The most recent session is {recent[0]} ({recent[1]}); "
                f"`session show --session {recent[0]}` reports what its close recorded"
                if recent is not None
                else ""
            ),
            details=(ErrorDetail(field="session", reason="no open session"),),
        )
    if len(rows) > 1:
        raise LinguaWikiError(
            "ambiguous_session",
            "this track has more than one open session; name the one you mean: "
            + ", ".join(f"{row[0]} ({row[1]})" for row in rows),
            details=tuple(
                ErrorDetail(
                    field="session", reason=str(row[1]), context={"session_id": str(row[0])}
                )
                for row in rows
            ),
        )
    return str(rows[0][0])


def resolve_session_and_track(
    database: Database, session: str | None, track: str | None
) -> tuple[str, str]:
    """`(track_id, session_id)`, resolving the track *from the session* when one is named.

    Resolving the track first and the session second refused every named session on a
    workspace with two active tracks: `resolve_track(None)` cannot choose, and the
    session that would have told it was never read. A named session belongs to one track,
    so that track is the answer, and a track given beside it must agree.
    """

    if session is not None:
        row = _session_row(database, session)
        track_id = str(row[1])
        if track is not None and learner_service.resolve_track(database, track) != track_id:
            raise LinguaWikiError(
                "session_track_mismatch",
                f"session {session} belongs to another track on this workspace",
                details=(ErrorDetail(field="session", reason="different track"),),
            )
        return track_id, str(row[0])
    track_id = learner_service.resolve_track(database, track)
    return track_id, resolve_session(database, None, track_id=track_id)


def _read_blocks(database: Database, *, session_id: str) -> tuple[SessionBlockReport, ...]:
    blocks: list[SessionBlockReport] = []
    for row in database.query(
        "SELECT block_id, sequence, role, block_type, area, dimension, modality, "
        "planned_minutes, objective, rationale_json, score, difficulty, novel_targets, "
        "repeated, status FROM session_blocks WHERE session_id = ? ORDER BY sequence",
        [session_id],
    ):
        block_id = str(row[0])
        targets = tuple(
            SessionTargetReport(
                content_id=str(target[0]),
                title=str(target[1]),
                stage=str(target[2]),
                novel=bool(target[3]),
                due=bool(target[4]),
                priority=int(target[5]),
            )
            for target in database.query(
                "SELECT target.content_id, item.title, target.stage, target.novel, "
                "target.due, target.priority FROM session_block_targets target "
                "JOIN knowledge_items item ON item.content_id = target.content_id "
                "WHERE target.block_id = ? ORDER BY target.sequence",
                [block_id],
            )
        )
        activities = tuple(
            SessionActivityReport(
                activity_id=str(activity[0]),
                sequence=int(activity[1]),
                kind=str(activity[2]),
                prompt=str(activity[3]),
                status=str(activity[4]),
                settings=_json_object(activity[5]),
            )
            for activity in database.query(
                "SELECT activity_id, sequence, kind, prompt, status, settings_json "
                "FROM activities WHERE block_id = ? ORDER BY sequence",
                [block_id],
            )
        )
        blocks.append(
            SessionBlockReport(
                block_id=block_id,
                sequence=int(row[1]),
                role=str(row[2]),
                block_type=str(row[3]),
                area=str(row[4]),
                dimension=str(row[5]),
                modality=str(row[6]),
                planned_minutes=int(row[7]),
                objective=str(row[8]),
                rationale=_json_list(row[9]),
                score=float(row[10]),
                difficulty=None if row[11] is None else float(row[11]),
                novel_targets=int(row[12]),
                repeated=bool(row[13]),
                status=str(row[14]),
                targets=targets,
                activities=activities,
            )
        )
    return tuple(blocks)


def _resume_point(blocks: Sequence[SessionBlockReport], *, status: str) -> ResumePoint | None:
    """The first block that is not finished, and the first activity in it that is not."""

    if status in session_policy.TERMINAL_STATUSES:
        return None
    for block in blocks:
        if block.status in ("completed", "skipped"):
            continue
        activity = next(
            (entry for entry in block.activities if entry.status not in ("completed", "skipped")),
            None,
        )
        return ResumePoint(
            block_id=block.block_id,
            sequence=block.sequence,
            block_type=block.block_type,
            objective=block.objective,
            activity_id=None if activity is None else activity.activity_id,
            activity_kind=None if activity is None else activity.kind,
        )
    return None


#: What may happen to a session next: `(operation ID, the CLI's words for it)`. One table
#: for both readers, so a page drawing buttons and a terminal printing advice cannot
#: disagree. Words are `None` where the CLI has never advertised the move.
_ACTIONS: Mapping[str, tuple[tuple[str, str | None], ...]] = {
    "planned": (("session.start", "session start"), ("session.abandon", None)),
    "active": (
        ("session.import", "session log"),
        ("session.close", "session close --outcome completed"),
        ("session.partial-close", "session partial-close"),
        ("session.abandon", None),
    ),
    "closing": (
        ("session.close", "session close (retry: the previous close did not finish)"),
        ("session.abandon", "session abandon"),
    ),
    "recoverable": (("session.recover", "session recover (staged work is still on record)"),),
}


def is_recoverable(*, status: str, staged: int) -> bool:
    """Whether `recover` would accept this session as a source. One predicate for both."""

    return status in session_policy.TERMINAL_STATUSES and staged > 0


def _action_rows(
    *, status: str, staged: int, finalized: bool
) -> tuple[tuple[str, str | None], ...]:
    if status == "closing":
        return () if finalized else _ACTIONS["closing"]
    if is_recoverable(status=status, staged=staged):
        return _ACTIONS["recoverable"]
    return _ACTIONS.get(status, ())


def _actions(*, status: str, staged: int, finalized: bool) -> tuple[str, ...]:
    return tuple(
        operation
        for operation, _ in _action_rows(status=status, staged=staged, finalized=finalized)
    )


def _next_actions(*, status: str, staged: int, finalized: bool) -> tuple[str, ...]:
    """What the reader can do next, in the words of the commands that do it."""

    return tuple(
        words
        for _, words in _action_rows(status=status, staged=staged, finalized=finalized)
        if words is not None
    )


def _read_finalization(database: Database, *, session_id: str) -> CloseReport | None:
    row = database.one(
        "SELECT result_json FROM session_finalizations WHERE session_id = ?", [session_id]
    )
    if row is None:
        return None
    payload = _json_object(row[0])
    if not payload:
        return None
    return CloseReport.model_validate(payload)


def _read_session(database: Database, *, session_id: str) -> SessionReport:
    row = _session_row(database, session_id)
    counts = database.one(
        "SELECT (SELECT count(*) FROM session_event_batches WHERE session_id = ?), "
        "(SELECT count(*) FROM session_staged_events WHERE session_id = ? AND status = 'staged'), "
        "(SELECT max(sequence) FROM session_event_batches WHERE session_id = ?)",
        [session_id, session_id, session_id],
    )
    batches = int(counts[0]) if counts else 0
    staged = int(counts[1]) if counts else 0
    last_sequence = None if counts is None or counts[2] is None else int(counts[2])
    blocks = _read_blocks(database, session_id=session_id)
    omissions = tuple(
        SessionOmissionReport(
            block_type=str(candidate[0]), score=float(candidate[1]), reason=str(candidate[2])
        )
        for candidate in database.query(
            "SELECT block_type, score, omission_reason FROM session_plan_candidates "
            "WHERE session_id = ? AND selected = FALSE AND omission_reason IS NOT NULL "
            "AND score >= ? ORDER BY score DESC, block_type",
            [session_id, planner_policy.OMISSION_THRESHOLD],
        )
    )
    finalization = _read_finalization(database, session_id=session_id)
    return SessionReport(
        session_id=str(row[0]),
        track_id=str(row[1]),
        status=str(row[2]),
        mode=str(row[3]),
        requested_minutes=int(row[4]),
        planned_minutes=int(row[5]),
        actual_minutes=None if row[6] is None else int(row[6]),
        energy=str(row[7]),
        correction_mode=str(row[8]),
        intent=None if row[9] is None else str(row[9]),
        novel_target_cap=int(row[10]),
        timezone=str(row[11]),
        planner_version=str(row[12]),
        lifecycle_version=str(row[13]),
        novel_targets=sum(block.novel_targets for block in blocks),
        blocks=blocks,
        omissions=omissions,
        batches=batches,
        staged_events=staged,
        last_batch_sequence=last_sequence,
        staging_digest=_staging_state(database, session_id=session_id).digest,
        resume_from=_resume_point(blocks, status=str(row[2])),
        finalization=finalization,
        planned_at=aware_utc(row[16]).isoformat(),
        started_at=None if row[17] is None else aware_utc(row[17]).isoformat(),
        closed_at=None if row[19] is None else aware_utc(row[19]).isoformat(),
        next_actions=_next_actions(
            status=str(row[2]), staged=staged, finalized=finalization is not None
        ),
        warnings=_json_list(row[14]),
    )


def show(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
) -> SessionReport:
    """Read one session: its plan, what it is holding, and what it became."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        _, session_id = resolve_session_and_track(database, session, track)
        return _read_session(database, session_id=session_id)


def staged(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    track: str | None = None,
    limit: int = 100,
    clock: Clock | None = None,
) -> tuple[StagedEventReport, ...]:
    """The provisional events a session is holding, in the order they were flushed."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        _, session_id = resolve_session_and_track(database, session, track)
        return _read_staged(database, session_id=session_id, limit=limit)


def _staged_listing(
    database: Database,
    *,
    session_id: str,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
) -> StagedListing:
    """A page of staged rows and how many the filter matches in all.

    `total` counts the *filtered* set, so a caller paging through "what can still be
    recovered" can tell when it has seen all of it -- half a list looks exactly like a
    short one otherwise.
    """

    if limit < 1 or offset < 0:
        raise LinguaWikiError(
            "invalid_page",
            "a listing page needs a positive limit and a non-negative offset",
            details=(
                ErrorDetail(field="limit", reason=str(limit)),
                ErrorDetail(field="offset", reason=str(offset)),
            ),
        )
    clause, parameters = _staged_filter(status)
    total = int(
        database.scalar(
            "SELECT count(*) FROM session_staged_events staged WHERE staged.session_id = ?"
            + clause,
            [session_id, *parameters],
        )
    )
    events = _read_staged(
        database, session_id=session_id, limit=limit, offset=offset, status=status
    )
    warnings = (
        (f"{total - offset - len(events)} more event(s) follow this page",)
        if offset + len(events) < total
        else ()
    )
    return StagedListing(events=events, total=total, warnings=warnings)


def staged_listing(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    track: str | None = None,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
    clock: Clock | None = None,
) -> StagedListing:
    """The staged rows of a session, filtered by status, with the filtered total."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        _, session_id = resolve_session_and_track(database, session, track)
        return _staged_listing(
            database, session_id=session_id, limit=limit, offset=offset, status=status
        )


def discover(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    states: Sequence[str] = ("open", "recoverable"),
    clock: Clock | None = None,
) -> SessionListReport:
    """The sessions of one track a page can carry on with or recover from, newest first.

    Per track and never across tracks: a session belongs to one learner, and a list that
    mixed two would invite recovering one learner's work into another's plan.
    """

    for state in states:
        assert_known(
            state, vocabulary=("open", "recoverable"), field="state", code="unknown_session_state"
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        rows = database.query(
            "SELECT session.session_id, session.status, session.mode, session.planned_minutes, "
            "session.planned_at, "
            "(SELECT count(*) FROM session_staged_events staged "
            " WHERE staged.session_id = session.session_id AND staged.status = 'staged'), "
            "(SELECT count(*) FROM session_event_batches batch "
            " WHERE batch.session_id = session.session_id), "
            "(SELECT count(*) FROM session_finalizations final "
            " WHERE final.session_id = session.session_id) "
            "FROM sessions session WHERE session.track_id = ? "
            "ORDER BY session.planned_at DESC, session.session_id DESC",
            [track_id],
        )
        entries = []
        for row in rows:
            status, staged = str(row[1]), int(row[5])
            entry = SessionListEntry(
                session_id=str(row[0]),
                status=status,
                mode=str(row[2]),
                planned_minutes=int(row[3]),
                planned_at=aware_utc(row[4]).isoformat(),
                staged_events=staged,
                batches=int(row[6]),
                finalized=int(row[7]) > 0,
                open=status in ("planned", "active", "closing"),
                recoverable=is_recoverable(status=status, staged=staged),
            )
            if ("open" in states and entry.open) or ("recoverable" in states and entry.recoverable):
                entries.append(entry)
        return SessionListReport(track_id=track_id, sessions=tuple(entries))


def _staging_state(database: Database, *, session_id: str) -> StagingState:
    """The fingerprint of what a close would consume now: the set `_staged_rows` reads."""

    identifiers = sorted(
        row.staged_event_id
        for row in _staged_rows(database, session_id=session_id, discard_blocks=())
    )
    return StagingState(digest=canonical_hash(identifiers), count=len(identifiers))


def screen(
    paths: WorkspacePaths,
    *,
    session: str,
    track: str | None = None,
    clock: Clock | None = None,
) -> SessionScreen:
    """One read for a page: the plan, the staging, the batches, and the legal next moves."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        _, session_id = resolve_session_and_track(database, session, track)
        report = _read_session(database, session_id=session_id)
        by_block: dict[str, int] = {}
        unattributed = 0
        for block_id, number in database.query(
            "SELECT block_id, count(*) FROM session_staged_events "
            "WHERE session_id = ? AND status = 'staged' GROUP BY block_id ORDER BY block_id",
            [session_id],
        ):
            if block_id is None:
                unattributed = int(number)
            else:
                by_block[str(block_id)] = int(number)
        batches = tuple(
            BatchSummary(
                sequence=int(row[0]),
                idempotency_key=str(row[1]),
                content_hash=str(row[2]),
                event_count=int(row[3]),
                created_at=aware_utc(row[4]).isoformat(),
            )
            for row in database.query(
                "SELECT sequence, idempotency_key, content_hash, event_count, created_at "
                "FROM session_event_batches WHERE session_id = ? ORDER BY sequence",
                [session_id],
            )
        )
        finalized = report.finalization is not None
        return SessionScreen(
            session=report,
            staged=_staged_listing(database, session_id=session_id),
            staged_by_block=by_block,
            unattributed=unattributed,
            batches=batches,
            missing_batch_sequences=_missing_batch_sequences(database, session_id=session_id),
            staging=_staging_state(database, session_id=session_id),
            actions=_actions(
                status=report.status, staged=report.staged_events, finalized=finalized
            ),
            closing_interrupted=report.status == "closing" and not finalized,
        )


def _staged_summary(kind: str, payload: Mapping[str, Any]) -> str:
    """One line about a staged event that never quotes the learner.

    A staged payload can hold the learner's own words, and a listing is read in places a
    transcript should not appear. The summary names the shape of the observation; the
    words stay in the payload, under the retention rule that applies to it.
    """

    if kind == "attempt.observed":
        target = payload.get("target") or payload.get("dimension") or "unattributed"
        return f"{payload.get('task_type', 'attempt')} on {target}, score {payload.get('score')}"
    if kind == "correction.given":
        return (
            f"{payload.get('category', 'correction')} correction ({payload.get('classification')})"
        )
    if kind == "pronunciation.assessment":
        return f"pronunciation {payload.get('status', 'observed')}"
    if kind == "source.progress":
        unit = payload.get("unit") or "the source"
        return f"{payload.get('aid', 'unaided')} comprehension of {unit}"
    if kind == "observation.noted":
        return f"{payload.get('category', 'note')} note"
    if kind == "follow_up":
        return f"{payload.get('kind', 'follow-up')} follow-up"
    return kind


#: The statuses a staged row can hold, as the schema's CHECK lists them.
STAGED_STATUSES: tuple[str, ...] = ("staged", "materialized", "discarded", "rejected")


def _staged_filter(status: str | None) -> tuple[str, list[Any]]:
    if status is None:
        return "", []
    assert_known(status, vocabulary=STAGED_STATUSES, field="status", code="unknown_staged_status")
    return " AND staged.status = ?", [status]


def _read_staged(
    database: Database,
    *,
    session_id: str,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
) -> tuple[StagedEventReport, ...]:
    clause, parameters = _staged_filter(status)
    rows = database.query(
        "SELECT staged.staged_event_id, batch.sequence, staged.sequence, staged.kind, "
        "staged.status, staged.evidence_basis, staged.block_id, staged.payload_json, "
        "staged.materialized_kind, staged.materialized_id, staged.discard_reason "
        "FROM session_staged_events staged "
        "JOIN session_event_batches batch ON batch.batch_id = staged.batch_id "
        "WHERE staged.session_id = ?" + clause + " "
        "ORDER BY batch.sequence, staged.sequence LIMIT ? OFFSET ?",
        [session_id, *parameters, limit, offset],
    )
    return tuple(
        StagedEventReport(
            staged_event_id=str(row[0]),
            batch_sequence=int(row[1]),
            sequence=int(row[2]),
            kind=str(row[3]),
            status=str(row[4]),
            evidence_basis=str(row[5]),
            block_id=None if row[6] is None else str(row[6]),
            summary=_staged_summary(str(row[3]), _json_object(row[7])),
            materialized_kind=None if row[8] is None else str(row[8]),
            materialized_id=None if row[9] is None else str(row[9]),
            discard_reason=None if row[10] is None else str(row[10]),
        )
        for row in rows
    )


# --- Lifecycle ---------------------------------------------------------------------


def _set_status(
    transaction: Database,
    *,
    session_id: str,
    current: str,
    target: str,
    now: datetime,
    started: bool = False,
    closing: bool = False,
    closed: bool = False,
    actual_minutes: int | None = None,
    fatigue: str | None = None,
    summary: str | None = None,
) -> None:
    """Move a session, having already checked the move is legal."""

    session_policy.assert_transition(current=current, target=target, session_id=session_id)
    stamp = naive_utc(now)
    transaction.execute(
        "UPDATE sessions SET status = ?, "
        "started_at = CASE WHEN ? THEN ? ELSE started_at END, "
        "closing_at = CASE WHEN ? THEN ? ELSE closing_at END, "
        "closed_at = CASE WHEN ? THEN ? ELSE closed_at END, "
        "actual_minutes = coalesce(?, actual_minutes), "
        "fatigue = coalesce(?, fatigue), "
        "summary = coalesce(?, summary), "
        "updated_at = ? WHERE session_id = ?",
        [
            target,
            started,
            stamp,
            closing,
            stamp,
            closed,
            stamp,
            actual_minutes,
            fatigue,
            summary,
            stamp,
            session_id,
        ],
    )


def start(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "session.start",
) -> SessionReport:
    """Begin a planned session. Starting an already-active one is a no-op."""

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id, session_id = resolve_session_and_track(database, session, track)
        row = _session_row(database, session_id)
        status = str(row[2])
        if status == "active":
            return _read_session(database, session_id=session_id)
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            _set_status(
                transaction,
                session_id=session_id,
                current=status,
                target="active",
                now=now,
                started=True,
            )
            transaction.execute(
                "UPDATE session_blocks SET status = 'active', updated_at = ? "
                "WHERE session_id = ? AND sequence = 1",
                [naive_utc(now), session_id],
            )
            migration_module.record_domain_event(
                transaction,
                event_type="session.started",
                aggregate_type="session",
                aggregate_id=session_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps({"mode": str(row[3])}, sort_keys=True),
            )
        return _read_session(database, session_id=session_id)


def resume(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "session.resume",
) -> SessionReport:
    """Pick a session back up after an interruption, and say what state it is in.

    A `planned` session starts. An `active` one is already resumable and is returned as
    it stands. A `closing` one is the interesting case: the previous close did not
    finish, so the report says to retry it rather than pretending the session is fresh.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id, session_id = resolve_session_and_track(database, session, track)
        row = _session_row(database, session_id)
        status = str(row[2])
        if status == "planned":
            # Resuming a session that never started is starting it. The alternative --
            # refusing -- would make the recovery path depend on remembering which
            # command the crash interrupted.
            now = aware_utc(database.now())
            with database.transaction() as transaction:
                _set_status(
                    transaction,
                    session_id=session_id,
                    current=status,
                    target="active",
                    now=now,
                    started=True,
                )
        report = _read_session(database, session_id=session_id)
    if report.status == "closing" and report.finalization is None:
        return report.model_copy(
            update={
                "warnings": (
                    *report.warnings,
                    "this session was being closed when it stopped, and nothing was "
                    "credited; retry the close to finish it, or abandon it",
                ),
            }
        )
    if report.status in session_policy.TERMINAL_STATUSES:
        raise LinguaWikiError(
            "session_already_finished",
            f"session {report.session_id} is {report.status} and cannot be resumed"
            + (
                f"; {report.staged_events} staged event(s) remain on record and can be "
                "recovered into a new session"
                if report.staged_events
                else ""
            ),
            details=(ErrorDetail(field="status", reason=report.status),),
        )
    return report


def abandon(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    track: str | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "session.abandon",
    actor: str = "cli",
) -> SessionReport:
    """Abandon a session: keep every staged event for audit, credit none of it.

    The staged rows stay `staged` rather than being discarded, so `session recover` can
    still offer them for explicit review. Abandoning is a decision about *credit*, not
    an instruction to forget what happened.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id, session_id = resolve_session_and_track(database, session, track)
        request = idempotency.request_hash(
            operation="session.abandon",
            session_id=session_id,
            reason=None if reason is None else canonical_hash(reason),
        )
        # Before the transition, which would refuse an abandoned session: a retry whose
        # first call landed is answered as the retry it is, not as an illegal move.
        if (
            idempotency.resolve(
                database, key=idempotency_key, event_type="session.abandoned", request_hash=request
            )
            is not None
        ):
            replayed = _read_session(database, session_id=session_id)
            return replayed.model_copy(
                update={
                    "replayed": True,
                    "warnings": (
                        *replayed.warnings,
                        "this session was already abandoned under this key; nothing was "
                        "written again",
                    ),
                }
            )
        row = _session_row(database, session_id)
        status = str(row[2])
        staged_count = int(
            database.scalar(
                "SELECT count(*) FROM session_staged_events WHERE session_id = ? "
                "AND status = 'staged'",
                [session_id],
            )
        )
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            # A plan the learner never showed up for is abandoned without ever having
            # started, and the schema now says so rather than requiring a start time
            # this command would have had to invent.
            _set_status(
                transaction,
                session_id=session_id,
                current=status,
                target="abandoned",
                now=now,
                closed=True,
                summary=reason,
            )
            transaction.execute(
                "UPDATE session_blocks SET status = 'skipped', updated_at = ? "
                "WHERE session_id = ? AND status IN ('planned', 'active')",
                [naive_utc(now), session_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                actor=actor,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([session_id]),
                before_summary=f"status {status}",
                after_summary=(
                    f"abandoned with {staged_count} staged event(s) kept for audit"
                    + (f": {reason}" if reason else "")
                ),
            )
            migration_module.record_domain_event(
                transaction,
                event_type="session.abandoned",
                aggregate_type="session",
                aggregate_id=session_id,
                correlation_id=EventId.new(),
                payload_json=idempotency.payload(
                    request, staged_events=staged_count, reason=reason
                ),
                idempotency_key=idempotency_key,
            )
        report = _read_session(database, session_id=session_id)
    if staged_count:
        return report.model_copy(
            update={
                "warnings": (
                    *report.warnings,
                    f"{staged_count} staged event(s) are kept on record and credited to "
                    "nothing; `session recover` can move them into a new session after review",
                ),
            }
        )
    return report


# --- Durable staging ---------------------------------------------------------------


def _batch_payload(batch: SessionEventBatch) -> list[dict[str, Any]]:
    """The events in the canonical form both the hash and the storage use."""

    return [event.model_dump(mode="json") for event in batch.events]


def _evidence_basis(kind: str, payload: Mapping[str, Any], *, source: str) -> str:
    """What the observation rests on: the learner's audible performance, or a text of it.

    A pronunciation claim is confirmable from audio and never from a transcript, so the
    basis is recorded when the event is staged rather than decided at close, when the
    package it came from is no longer in front of us.
    """

    if kind == "pronunciation.assessment":
        return "audio" if payload.get("audio_artifact_id") else "transcript"
    if source == "package":
        return "transcript"
    return "direct"


def _own_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip provenance a skill flush is not entitled to claim.

    The fields that say "this came from an ingested package, out of this utterance, from
    this transcript layer" are set by the ingestion path. A flush that could set them
    would be a flush that can describe itself as a reviewed hearing of a recording
    nobody has -- so they are removed rather than trusted, on the same principle as the
    package's own `details`.
    """

    return {key: value for key, value in payload.items() if key not in _CLAIMED_PROVENANCE}


#: What `_own_payload` removes from a skill flush or an import.
_CLAIMED_PROVENANCE: frozenset[str] = frozenset(
    {"source", "package_id", "external_session_id", "utterance_id", "transcript_layer"}
)


def _retained_payload(
    payload: Mapping[str, Any], *, preferences: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply the track's retention rule *before* the payload is stored anywhere.

    This was the stage's worst defect. Applying retention only when the attempt was
    written meant a staged event held the learner's full text for the whole life of the
    session -- and, because staged rows are kept as the audit trail of what a close was
    given, for the whole life of the workspace afterwards. A track that had explicitly
    refused transcript retention had it retained anyway.

    Retention now happens once, at the boundary where the text first arrives, using the
    same rule `evidence.py` applies to an attempt. A caller asking to keep more than
    consent allows is refused here rather than at close: the refusal is the same one
    either way, and getting it at flush time leaves a session that can still be closed.
    """

    if "response" not in payload and "response_visibility" not in payload:
        return dict(payload)
    visibility, excerpt, digest = evidence_service.retain_response(
        None if payload.get("response") is None else str(payload["response"]),
        requested=(
            None
            if payload.get("response_visibility") is None
            else str(payload["response_visibility"])
        ),
        preferences=preferences,
    )
    retained = {
        key: value
        for key, value in payload.items()
        if key not in ("response", "response_visibility", "response_hash")
    }
    retained["response_visibility"] = visibility
    if excerpt is not None:
        retained["response"] = excerpt
    if digest is not None:
        retained["response_hash"] = digest
    return retained


def _assert_events_are_new(
    database: Database,
    *,
    session_id: str,
    event_ids: Sequence[str],
    external_session_id: str | None = None,
    track_id: str | None = None,
) -> None:
    """Refuse observations this session has already been given.

    Two scopes, because a duplicate arrives two ways:

    - **within the session**: a flush re-sent under a new key, or a package ingested
      alongside a flush that already carried the same event. The unique index enforces
      it; this is the named refusal that reaches the caller instead of a raw constraint
      error;
    - **within the external session**: a voice provider exports a `checkpoint` and then a
      `completed` package for one call, and the second repeats the first's events. Those
      may land on *different* LinguaWiki sessions, which no index over one session can
      catch, so the overlap is looked up through the packages of that external session.
    """

    already = (
        [
            str(row[0])
            for row in database.query(
                "SELECT source_event_id FROM session_staged_events WHERE session_id = ? "
                "AND source_event_id IN ("
                + ", ".join("?" for _ in event_ids)
                + ") ORDER BY source_event_id",
                [session_id, *event_ids],
            )
        ]
        if event_ids
        else []
    )
    if already:
        raise LinguaWikiError(
            "duplicate_session_event",
            f"session {session_id} already holds event(s) "
            f"{', '.join(already)}; one observation is staged once, and re-sending it "
            "would credit the learner twice for work they did once",
            details=tuple(ErrorDetail(field="event_id", reason=event) for event in already),
        )
    if external_session_id is None or track_id is None or not event_ids:
        return
    elsewhere = [
        f"{row[0]} (on session {row[1]})"
        for row in database.query(
            "SELECT staged.source_event_id, staged.session_id "
            "FROM session_staged_events staged "
            "JOIN session_event_batches batch ON batch.batch_id = staged.batch_id "
            "JOIN session_packages package ON package.ingestion_id = batch.ingestion_id "
            "WHERE package.external_session_id = ? AND package.track_id = ? "
            "AND staged.source_event_id IN ("
            + ", ".join("?" for _ in event_ids)
            + ") ORDER BY staged.source_event_id",
            [external_session_id, track_id, *event_ids],
        )
    ]
    if elsewhere:
        raise LinguaWikiError(
            "duplicate_external_event",
            f"external session {external_session_id} has already contributed event(s) "
            f"{'; '.join(elsewhere)}. A checkpoint export and the completed export of one "
            "call overlap by design: ingest the completed export into the session that "
            "holds the checkpoint, or export only the events after it.",
            details=tuple(ErrorDetail(field="event_id", reason=entry) for entry in elsewhere),
        )


def _assert_loggable(*, session_id: str, status: str) -> None:
    if status == "active":
        return
    if status == "planned":
        raise LinguaWikiError(
            "session_not_started",
            f"session {session_id} has not started; run `session start` before flushing",
            details=(ErrorDetail(field="status", reason=status),),
        )
    raise LinguaWikiError(
        "session_not_active",
        f"session {session_id} is {status} and cannot take new observations"
        + (
            "; a close that did not finish is retried, not flushed into"
            if status == "closing"
            else "; plan a new session"
        ),
        details=(ErrorDetail(field="status", reason=status),),
    )


def _resolve_block(database: Database, *, session_id: str, block: str | None) -> str | None:
    """Check a block belongs to this session before anything references it."""

    if block is None:
        return None
    row = database.one(
        "SELECT block_id FROM session_blocks WHERE block_id = ? AND session_id = ?",
        [block, session_id],
    )
    if row is None:
        raise LinguaWikiError(
            "block_not_in_session",
            f"block {block} is not part of session {session_id}",
            details=(ErrorDetail(field="block", reason="block belongs to another session"),),
        )
    return str(row[0])


def _resolve_activity(
    database: Database, *, session_id: str, activity: str | None, block: str | None = None
) -> str | None:
    """Check an activity belongs to this session, and to the block the event names.

    Checking only the session was not enough: an event could name block A while pointing
    at an activity of block B, and both checks passed independently. The attribution is
    what `resume` reads to say which activity comes next and what the close marks
    completed, so a wrong one is a wrong answer to both questions.
    """

    if activity is None:
        return None
    row = database.one(
        "SELECT activity.activity_id, activity.block_id FROM activities activity "
        "JOIN session_blocks block ON block.block_id = activity.block_id "
        "WHERE activity.activity_id = ? AND block.session_id = ?",
        [activity, session_id],
    )
    if row is None:
        raise LinguaWikiError(
            "activity_not_in_session",
            f"activity {activity} is not part of session {session_id}",
            details=(ErrorDetail(field="activity", reason="activity belongs elsewhere"),),
        )
    if block is not None and str(row[1]) != block:
        raise LinguaWikiError(
            "activity_not_in_block",
            f"activity {activity} belongs to block {row[1]}, and this event names block "
            f"{block}; an observation records the activity it actually happened in",
            details=(
                ErrorDetail(field="activity", reason=str(row[1])),
                ErrorDetail(field="block", reason=block),
            ),
        )
    return str(row[0])


def log(
    paths: WorkspacePaths,
    *,
    batch: SessionEventBatch | Mapping[str, Any],
    session: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "session.log",
    actor: str = "cli",
    require_declared_assessor: bool = False,
) -> SessionBatchReport:
    """Store one flush durably, and change nothing about the learner.

    Three things make a retry safe, and each of them refuses a *different* mistake:

    - the idempotency key: the same key with the same content is accepted once, and the
      same key with different content is a conflict rather than an overwrite -- the
      second call would otherwise discard the first call's observations silently;
    - the sequence number: two different batches cannot claim the same position, so a
      gap is visible at close instead of being closed over;
    - the content hash: when the caller sends one and it disagrees with the events, the
      batch is refused, because something was lost between the skill and here.

    `require_declared_assessor` is for a file of unknown origin. An attempt's
    `assessor_kind` defaults to `ai`, which is right for the skill's own flush and a
    guess about anything else; an import has to say who judged each attempt, and the
    question is answered from the fields the payload *set*, before any default fills in.
    """

    validated = (
        batch
        if isinstance(batch, SessionEventBatch)
        else validated_contract(
            SessionEventBatch, batch, code="invalid_session_batch", subject="batch"
        )
    )
    if len(validated.events) > MAXIMUM_BATCH_EVENTS:
        raise LinguaWikiError(
            "batch_too_large",
            f"a batch carries at most {MAXIMUM_BATCH_EVENTS} events; flush at block boundaries",
            details=(ErrorDetail(field="events", reason=str(len(validated.events))),),
        )
    declared_session = None if validated.session_id is None else str(validated.session_id)
    if session is not None and declared_session is not None and declared_session != session:
        raise LinguaWikiError(
            "session_batch_wrong_session",
            f"this batch names session {declared_session} and was sent to session {session}; "
            "a batch is staged on the session it was written for",
            details=(
                ErrorDetail(field="session_id", reason=declared_session),
                ErrorDetail(field="session", reason=session),
            ),
        )
    if require_declared_assessor:
        undeclared = [
            str(event.event_id)
            for event in validated.events
            if event.kind == "attempt.observed"
            and "assessor_kind" not in event.payload.model_fields_set
        ]
        if undeclared:
            raise LinguaWikiError(
                "session_import_assessor_required",
                "an imported attempt must say who assessed it; these do not: "
                + ", ".join(undeclared),
                details=tuple(
                    ErrorDetail(
                        field="assessor_kind", reason="not declared", context={"event_id": event}
                    )
                    for event in undeclared
                ),
            )
    payload = _batch_payload(validated)
    content_hash = canonical_hash(payload)
    if validated.content_hash is not None and validated.content_hash != content_hash:
        raise LinguaWikiError(
            "batch_hash_mismatch",
            "the batch's declared content hash does not match its events, so something "
            "changed in transit; resend the batch rather than storing it",
            details=(
                ErrorDetail(field="content_hash", reason=validated.content_hash),
                ErrorDetail(field="computed", reason=content_hash),
            ),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id, session_id = resolve_session_and_track(
            database, session or declared_session, track
        )
        existing = database.one(
            "SELECT batch_id, session_id, sequence, content_hash, event_count FROM "
            "session_event_batches WHERE idempotency_key = ?",
            [validated.idempotency_key],
        )
        if existing is not None:
            if str(existing[1]) != session_id or str(existing[3]) != content_hash:
                raise LinguaWikiError(
                    "idempotency_conflict",
                    f"idempotency key {validated.idempotency_key} already stored a different "
                    "batch; a retry must carry the same events, and new observations need a "
                    "new key",
                    details=(
                        ErrorDetail(field="idempotency_key", reason="reused for other content"),
                        ErrorDetail(field="session", reason=str(existing[1])),
                    ),
                )
            staged_count = int(
                database.scalar(
                    "SELECT count(*) FROM session_staged_events WHERE batch_id = ?",
                    [existing[0]],
                )
            )
            return SessionBatchReport(
                batch_id=str(existing[0]),
                session_id=session_id,
                sequence=int(existing[2]),
                idempotency_key=validated.idempotency_key,
                content_hash=content_hash,
                event_count=int(existing[4]),
                staged_events=staged_count,
                duplicate=True,
                warnings=("this batch was already stored, so nothing was written again",),
            )
        row = _session_row(database, session_id)
        _assert_loggable(session_id=session_id, status=str(row[2]))
        occupied = database.one(
            "SELECT batch_id, idempotency_key FROM session_event_batches "
            "WHERE session_id = ? AND sequence = ?",
            [session_id, validated.sequence],
        )
        if occupied is not None:
            raise LinguaWikiError(
                "batch_sequence_taken",
                f"batch {validated.sequence} of session {session_id} was already stored "
                f"under key {occupied[1]}; flush the next sequence instead",
                details=(
                    ErrorDetail(field="sequence", reason=str(validated.sequence)),
                    ErrorDetail(field="batch_id", reason=str(occupied[0])),
                ),
            )
        batch_block = _resolve_block(
            database,
            session_id=session_id,
            block=None if validated.block is None else str(validated.block),
        )
        resolved_blocks = [
            _resolve_block(
                database,
                session_id=session_id,
                block=None if event.block is None else str(event.block),
            )
            or batch_block
            for event in validated.events
        ]
        resolved_activities = [
            _resolve_activity(
                database,
                session_id=session_id,
                activity=None if event.activity is None else str(event.activity),
                block=resolved_blocks[position],
            )
            for position, event in enumerate(validated.events)
        ]
        _assert_events_are_new(
            database,
            session_id=session_id,
            event_ids=[str(event.event_id) for event in validated.events],
        )
        record = learner_service.track_context(database, track_id)
        # Retention rewrites what the batch contract validated, so the *stored* form is
        # the one that has to be materializable -- and it is checked here rather than at
        # close, where a refusal would leave the session holding work nobody can credit.
        retained_payloads = []
        stripped_warnings = []
        for event in validated.events:
            stripped = sorted(
                field
                for field in event.payload.model_fields_set
                if field in _CLAIMED_PROVENANCE and getattr(event.payload, field, None) is not None
            )
            if stripped:
                stripped_warnings.append(
                    f"event {event.event_id}: removed provenance a flush may not claim "
                    f"({', '.join(stripped)})"
                )
        for event, entry in zip(validated.events, payload, strict=True):
            retained = _retained_payload(
                _own_payload(entry["payload"]), preferences=_preferences(record)
            )
            assert_materializable(retained, kind=event.kind, reference=f"event {event.event_id}")
            retained_payloads.append(retained)
        batch_id = str(BatchId.new())
        now = aware_utc(database.now())
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO session_event_batches (batch_id, session_id, sequence, "
                "idempotency_key, content_hash, event_count, source, block_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'skill', ?, ?)",
                [
                    batch_id,
                    session_id,
                    validated.sequence,
                    validated.idempotency_key,
                    content_hash,
                    len(validated.events),
                    batch_block,
                    naive_utc(now),
                ],
            )
            for position, (event, retained) in enumerate(
                zip(validated.events, retained_payloads, strict=True), start=1
            ):
                transaction.execute(
                    "INSERT INTO session_staged_events (staged_event_id, session_id, batch_id, "
                    "sequence, kind, schema_version, payload_json, block_id, activity_id, "
                    "evidence_basis, status, occurred_at, source_event_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?)",
                    [
                        str(StagedEventId.new()),
                        session_id,
                        batch_id,
                        position,
                        event.kind,
                        validated.schema_version,
                        json.dumps(retained, sort_keys=True, ensure_ascii=False),
                        resolved_blocks[position - 1],
                        resolved_activities[position - 1],
                        _evidence_basis(event.kind, retained, source="skill"),
                        naive_utc(event.occurred_at),
                        str(event.event_id),
                        naive_utc(now),
                    ],
                )
            # A flushed block is the one being worked, so it and its activities become
            # `active`. Without this the whole plan stayed `planned`, and `resume` had no
            # way to say which block a session had reached.
            for block_id in {block for block in resolved_blocks if block is not None}:
                transaction.execute(
                    "UPDATE session_blocks SET status = 'active', updated_at = ? "
                    "WHERE block_id = ? AND status = 'planned'",
                    [naive_utc(now), block_id],
                )
            for activity_id in {
                activity for activity in resolved_activities if activity is not None
            }:
                transaction.execute(
                    "UPDATE activities SET status = 'active', updated_at = ? "
                    "WHERE activity_id = ? AND status = 'planned'",
                    [naive_utc(now), activity_id],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                actor=actor,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([batch_id]),
                after_summary=(
                    f"batch {validated.sequence} staged {len(validated.events)} event(s); "
                    "nothing materialized"
                ),
            )
            migration_module.record_domain_event(
                transaction,
                event_type="session.batch_staged",
                aggregate_type="session",
                aggregate_id=session_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {
                        "sequence": validated.sequence,
                        "event_count": len(validated.events),
                        "content_hash": content_hash,
                    },
                    sort_keys=True,
                ),
                idempotency_key=validated.idempotency_key,
            )
        return SessionBatchReport(
            batch_id=batch_id,
            session_id=session_id,
            sequence=validated.sequence,
            idempotency_key=validated.idempotency_key,
            content_hash=content_hash,
            event_count=len(validated.events),
            staged_events=len(validated.events),
            warnings=tuple(stripped_warnings),
        )


# --- Atomic close ------------------------------------------------------------------


#: Every policy version a close's derived state was produced by, recorded on the
#: finalization so a later recomputation can tell what these numbers were made of.
def _calculation_versions() -> dict[str, str]:
    return {
        "aggregation": AGGREGATION_VERSION,
        "planner": planner_policy.PLANNER_VERSION,
        "lifecycle": session_policy.LIFECYCLE_VERSION,
        "strength": STRENGTH_VERSION,
        CLOSE_REQUEST_VERSION_KEY: CLOSE_REQUEST_VERSION,
    }


#: Marks a finalization whose close event carries a request hash. Its absence, together
#: with an event payload in exactly the shape below, is what identifies a close recorded
#: before requests were hashed -- two markers, so damage to one is not read as age.
CLOSE_REQUEST_VERSION_KEY = "close_request"
CLOSE_REQUEST_VERSION = "1"
_LEGACY_CLOSE_PAYLOAD_KEYS = frozenset(
    {"attempts", "errors", "evidence", "outcome", "staged_consumed"}
)


def _close_request_hash(
    *,
    session_id: str,
    outcome: str,
    actual_minutes: int | None,
    fatigue: str | None,
    summary: str | None,
    discard_blocks: Sequence[str],
    expected_staging: str | None,
) -> str:
    """Everything a close turns on. The summary goes in as a hash: it is learner text,
    and the hash is stored in an event that is never edited."""

    return idempotency.request_hash(
        operation="session.close",
        session_id=session_id,
        outcome=outcome,
        actual_minutes=actual_minutes,
        fatigue=fatigue,
        summary=None if summary is None else canonical_hash(summary),
        discard_blocks=sorted(set(discard_blocks)),
        expected_staging=expected_staging,
    )


def _unvouched(key: str, session_id: str) -> LinguaWikiError:
    return LinguaWikiError(
        "idempotency_conflict",
        f"close key {key} closed session {session_id}, but what that close was asked for "
        "cannot be read back, so this call cannot be shown to be a retry of it; "
        "`session show` reports what it recorded",
        details=(
            ErrorDetail(
                field="idempotency_key",
                reason="recorded request is unknown",
                context={"idempotency_key": key},
            ),
        ),
    )


def _vouch_for_keyed_close(
    database: Database,
    *,
    session_id: str,
    key: str,
    request: str,
) -> tuple[str, ...]:
    """Decide whether a keyed close of a finalized session is a retry, before replaying it.

    Returns the warnings the replay should carry. Raises when the stored close cannot be
    shown to be the same request. Three cases, told apart by what the finalization row
    says about itself *and* what its event says -- never by one of them alone:

    - a C7 close: the request hash decides, through `idempotency.resolve`;
    - a close from before request hashing: its outcome alone decides, as it always did;
    - anything else -- a missing event, a payload that will not parse, markers that
      disagree -- is a record nobody can vouch for, and is refused.
    """

    raw_versions = database.scalar(
        "SELECT calculation_versions_json FROM session_finalizations WHERE session_id = ?",
        [session_id],
    )
    versions = _json_object(raw_versions)
    event = database.one(
        "SELECT event_type, payload_json FROM domain_events WHERE idempotency_key = ?",
        [key],
    )
    if CLOSE_REQUEST_VERSION_KEY in versions:
        if event is None:
            raise _unvouched(key, session_id)
        idempotency.resolve(database, key=key, event_type="session.closed", request_hash=request)
        return ()
    if event is None or str(event[0]) != "session.closed":
        raise _unvouched(key, session_id)
    try:
        stored = json.loads(str(event[1]))
    except (ValueError, RecursionError):
        raise _unvouched(key, session_id) from None
    if not isinstance(stored, dict) or set(stored) != _LEGACY_CLOSE_PAYLOAD_KEYS:
        raise _unvouched(key, session_id)
    return (
        "this close predates request hashing, so only its outcome was compared with this retry",
    )


def _missing_batch_sequences(database: Database, *, session_id: str) -> tuple[int, ...]:
    """Which of a session's flushes never arrived.

    Batch sequences are the skill's own count of its flushes, so a gap means one was
    lost. A *completed* close refuses on that, because crediting the rest would report a
    session as whole while a block's observations are missing. A **partial** close is
    exactly the documented remedy for it and must proceed: refusing there contradicted
    the message that recommended it, and left the learner with no way to keep the work
    that did arrive.
    """

    sequences = [
        int(row[0])
        for row in database.query(
            "SELECT sequence FROM session_event_batches WHERE session_id = ? ORDER BY sequence",
            [session_id],
        )
    ]
    if not sequences:
        return ()
    return tuple(sorted(set(range(1, max(sequences) + 1)) - set(sequences)))


@dataclass(frozen=True, slots=True)
class _StagedRow:
    """One staged event as it was stored, before anything is decided about it.

    `occurred_at` is when the observation *happened*; `flushed_at` is when it was sent.
    Keeping both is the whole reason the column exists: a block worked at 18:30 and
    flushed at 19:10 is one observation with two timestamps, and every derived fact --
    the delay class, the chronology of two attempts on one item, when an error was last
    seen, which estimate snapshot it falls into -- is computed from the first.
    """

    staged_event_id: str
    kind: str
    block_id: str | None
    activity_id: str | None
    evidence_basis: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    flushed_at: datetime
    discard_reason: str | None = None


#: The staged payload contract for each event kind, from the contracts module so the
#: service and the published schema cannot describe different sets. A payload is
#: revalidated against it both on the way into storage and on the way out: storage is a
#: round trip through JSON, so datetimes come back as strings, and a payload can be
#: damaged, hand-edited, or restored from another release in between.
STAGED_PAYLOAD_MODELS: Mapping[str, type[Any]] = STAGED_PAYLOAD_KINDS


def assert_materializable(payload: Mapping[str, Any], *, kind: str, reference: str) -> Any:
    """Validate a staged payload against the contract for its kind, and say what fails.

    Called twice on purpose, at both ends of the staging boundary:

    - when the payload is **built**, so a package or a flush that could never be
      credited is refused where it arrives. A package whose events carried only an
      utterance and empty details used to be accepted and then refuse to close, which
      left the session stuck holding work nobody could credit;
    - when it is **read back at close**, because storage is a round trip through JSON and
      a payload can be damaged, hand-edited, or restored from another release in between.

    The message names the field, because "invalid payload" tells a producer nothing about
    which part of their export is wrong.
    """

    model = STAGED_PAYLOAD_MODELS.get(kind)
    if model is None:
        raise LinguaWikiError(
            "unmaterializable_staged_event",
            f"{reference} is a {kind}, which this release cannot materialize",
            details=(ErrorDetail(field="kind", reason=kind),),
        )
    try:
        return model.model_validate(dict(payload))
    except ValidationError as failure:
        first = failure.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "payload"
        raise LinguaWikiError(
            "invalid_staged_payload",
            f"{reference} ({kind}) cannot be materialized: {field} {first['msg'].lower()}. "
            f"A payload of kind {kind} needs the fields its own contract declares -- see "
            "schemas/lingua.session.events.v1.json.",
            details=(
                ErrorDetail(field="kind", reason=kind),
                ErrorDetail(field=field, reason=str(first["msg"])),
            ),
        ) from failure


def _validated_payload(row: _StagedRow) -> Any:
    """Revalidate one staged payload on the way out of storage."""

    return assert_materializable(
        row.payload, kind=row.kind, reference=f"staged event {row.staged_event_id}"
    )


def _plan_attempt_event(
    database: Database,
    *,
    work: StagedAttemptPayload,
    track_id: str,
    staged_event_id: str,
    occurred_at: datetime,
    correction_mode: str,
) -> evidence_service.AttemptPlan:
    """Resolve one staged attempt into everything `evidence.py` decided about it.

    The staged event says what happened. What it *proves* -- the claim, the novelty, the
    delay, the strength -- is derived there and not here, and the identifier of the
    staged row becomes the attempt's idempotency key, so the same staged event can never
    become two attempts even if a close were somehow run twice.
    """

    return evidence_service.plan_attempt(
        database,
        score=work.score,
        task_type=work.task_type,
        modality=work.modality,
        target=work.target,
        dimension=work.dimension,
        claims=work.claims or None,
        help_level=work.help_level,
        correction_mode=_attempt_correction_mode(work, session_mode=correction_mode),
        retrieval=work.retrieval,
        delay_hours=work.delay_hours,
        latency_ms=work.latency_ms,
        context=work.context,
        response=work.response,
        response_visibility=work.response_visibility,
        response_hash=work.response_hash,
        assessor_kind=work.assessor_kind,
        assessor=work.assessor,
        confidence=work.confidence,
        origin=SESSION_ORIGIN,
        observed_at=occurred_at,
        idempotency_key=staged_event_id,
        track=track_id,
        command="session.close",
    )


#: How a session's correction regime maps onto the correction an attempt records. The
#: session's regime is about teaching; the attempt's field is about what the learner was
#: given during that attempt, and `exam` means they were given nothing.
_SESSION_CORRECTION_MODES: Mapping[str, str] = {
    "fluency": "delayed",
    "accuracy": "immediate",
    "exam": "none",
}


def _attempt_correction_mode(work: StagedAttemptPayload, *, session_mode: str) -> str:
    if work.correction_mode != "none":
        return work.correction_mode
    return _SESSION_CORRECTION_MODES.get(session_mode, "none")


def _plan_correction_event(
    database: Database,
    *,
    work: StagedCorrectionPayload,
    track_id: str,
    occurred_at: datetime,
) -> error_service.OccurrencePlan:
    return error_service.plan_occurrence(
        database,
        category=work.category,
        signature=work.signature,
        description=work.description,
        target=work.target,
        learner_form=work.learner_form,
        corrected_form=work.corrected_form,
        explanation=work.explanation,
        meaning_impact=work.meaning_impact,
        classification=work.classification,
        confidence=work.confidence,
        severity=work.severity,
        observed_at=occurred_at,
        attach_to=work.attach_to,
        distinct=work.distinct,
        track=track_id,
        command="session.close",
    )


def _staged_rows(
    database: Database, *, session_id: str, discard_blocks: Sequence[str]
) -> tuple[_StagedRow, ...]:
    """Read the staged events a close will consume, in the order they were flushed.

    Order matters and is not cosmetic: an item's second attempt in a session is only
    *delayed* relative to the first if the first is folded in before it, and the second
    occurrence of an error is only the second if the first has been counted.
    """

    discarded = set(discard_blocks)
    rows: list[_StagedRow] = []
    for row in database.query(
        "SELECT staged.staged_event_id, staged.kind, staged.block_id, staged.activity_id, "
        "staged.evidence_basis, staged.payload_json, staged.occurred_at, staged.created_at "
        "FROM session_staged_events staged "
        "JOIN session_event_batches batch ON batch.batch_id = staged.batch_id "
        "WHERE staged.session_id = ? AND staged.status = 'staged' "
        "ORDER BY staged.occurred_at, batch.sequence, staged.sequence",
        [session_id],
    ):
        block_id = None if row[2] is None else str(row[2])
        rows.append(
            _StagedRow(
                staged_event_id=str(row[0]),
                kind=str(row[1]),
                block_id=block_id,
                activity_id=None if row[3] is None else str(row[3]),
                evidence_basis=str(row[4]),
                payload=_json_object(row[5]),
                occurred_at=aware_utc(row[6]),
                flushed_at=aware_utc(row[7]),
                discard_reason=(
                    f"block {block_id} was reviewed and excluded from this partial close"
                    if block_id is not None and block_id in discarded
                    else None
                ),
            )
        )
    return tuple(rows)


def _materialize_pronunciation(
    transaction: Database,
    *,
    row: _StagedRow,
    payload: Any,
    track_id: str,
    now: datetime,
) -> str:
    """Write a staged pronunciation event as the acoustic claim it is.

    Stage 4 wrote it as a generic session observation, because the table for acoustic
    claims did not exist yet. It does now, and the difference is not filing: a claim in
    `pronunciation_observations` names the audio it rests on, so purging that audio
    invalidates it. A generic observation named nothing, so a `confirmed` claim from a
    recording the learner later deleted went on standing with nothing able to find it.

    The basis comes from the staged row rather than the payload. It was decided when the
    observation was staged, from what the workspace actually had, which is the only moment
    anyone knew.
    """

    from linguawiki import transcripts as transcript_policy
    from linguawiki.ids import PronunciationId

    audio_artifact_id: str | None = None
    named = getattr(payload, "audio_artifact_id", None)
    if named is not None:
        # Kept and unpurged, or it is not audio this claim may rest on. A row that exists
        # is not the same fact as a recording anyone can still listen to, and resolving on
        # existence alone let a `confirmed` claim cite a file that had been deleted at the
        # door for want of consent.
        audio_artifact_id = (
            str(
                transaction.scalar(
                    "SELECT artifact_id FROM artifacts WHERE track_id = ? "
                    "AND (artifact_id = ? OR external_id = ?) "
                    # Audio, kept, and unpurged. A transcript file satisfied a `confirmed`
                    # claim for want of the first condition, which is the acoustic rule
                    # defeated by a file extension.
                    "AND kind = 'audio' AND purged_at IS NULL AND retained",
                    [track_id, str(named), str(named)],
                )
                or ""
            )
            or None
        )
    basis = row.evidence_basis if audio_artifact_id is not None else "transcript"
    status = str(payload.status)
    dimension = str(payload.acoustic_dimension)
    # The same rule the command obeys, reached the other way. A close that quietly
    # downgraded a refused claim would be a way around it; a close that refuses says the
    # package promised evidence it did not bring.
    transcript_policy.assert_acoustic_claim_has_audio(
        status=status,
        dimension=dimension,
        basis=basis,
        reference=f"staged event {row.staged_event_id}",
    )
    utterance_id = None
    external_utterance = getattr(payload, "utterance_id", None)
    if external_utterance is not None:
        # Scoped to the external session the event came from. A producer's utterance IDs
        # are unique inside one call, so resolving on the ID alone attached one
        # conversation's acoustic claims to another conversation's words.
        external_session = getattr(payload, "external_session_id", None)
        utterance_id = transaction.scalar(
            "SELECT utterance_id FROM utterances WHERE track_id = ? AND external_id = ? "
            "AND external_session_id IS NOT DISTINCT FROM ?",
            [
                track_id,
                str(external_utterance),
                None if external_session is None else str(external_session),
            ],
        )
    observation_id = str(PronunciationId.new())
    transaction.execute(
        "INSERT INTO pronunciation_observations (observation_id, track_id, utterance_id, "
        "dimension, status, basis, audio_artifact_id, target_content_id, note, "
        "reviewer_kind, reviewer, invalidated_at, invalidation_reason, policy_version, "
        "observed_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai', NULL, NULL, NULL, ?, ?, ?)",
        [
            observation_id,
            track_id,
            None if utterance_id is None else str(utterance_id),
            dimension,
            status,
            basis,
            audio_artifact_id,
            payload.target,
            payload.note[:2000],
            transcript_policy.TRANSCRIPT_POLICY_VERSION,
            naive_utc(row.occurred_at),
            naive_utc(now),
        ],
    )
    return observation_id


def _materialize(
    transaction: Database,
    *,
    session_id: str,
    track_id: str,
    correction_mode: str,
    finalization_id: str,
    rows: Sequence[_StagedRow],
    now: datetime,
) -> dict[str, Any]:
    """Write every staged event, in order, deciding each one against what came before.

    Each event is resolved *inside* this transaction rather than in a planning pass
    before it, and the difference is not stylistic: two corrections of the same pattern
    planned against the same prior state both believe they are the first, which produced
    a duplicate-key failure where the pattern was new and a silently wrong occurrence
    count where it was not. Resolving as we go means the second sees the first.
    """

    counts = {
        "attempts": 0,
        "evidence": 0,
        "errors": 0,
        "followups": 0,
        "observations": 0,
        "comprehension": 0,
        "discarded": 0,
    }
    stage_changes: list[StageChangeReport] = []
    errors_touched: list[str] = []
    dimensions: set[str] = set()
    sources_worked: set[str] = set()
    worked_blocks: set[str | None] = set()
    for row in rows:
        if row.discard_reason is not None:
            transaction.execute(
                "UPDATE session_staged_events SET status = 'discarded', discard_reason = ? "
                "WHERE staged_event_id = ?",
                [row.discard_reason, row.staged_event_id],
            )
            counts["discarded"] += 1
            continue
        payload = _validated_payload(row)
        materialized_kind = MATERIALIZED_KINDS[row.kind]
        materialized_id: str
        if row.kind == "attempt.observed":
            plan = _plan_attempt_event(
                transaction,
                work=payload,
                track_id=track_id,
                staged_event_id=row.staged_event_id,
                occurred_at=row.occurred_at,
                correction_mode=correction_mode,
            )
            report = evidence_service.write_attempt(transaction, plan)
            materialized_id = report.attempt_id
            counts["attempts"] += 1
            counts["evidence"] += len(report.evidence)
            if report.dimension:
                dimensions.add(report.dimension)
            if report.stage_after is not None and report.stage_after != report.stage_before:
                stage_changes.append(
                    StageChangeReport(
                        content_id=str(report.target_content_id),
                        title=report.target_title or str(report.target_content_id),
                        stage_before=report.stage_before,
                        stage_after=report.stage_after,
                        explanation=report.stage_explanation,
                    )
                )
            errors_touched.extend(report.errors_supported)
            errors_touched.extend(report.errors_reactivated)
        elif row.kind == "correction.given":
            occurrence = error_service.plan_occurrence(
                transaction,
                category=payload.category,
                signature=payload.signature,
                description=payload.description,
                target=payload.target,
                learner_form=payload.learner_form,
                corrected_form=payload.corrected_form,
                explanation=payload.explanation,
                meaning_impact=payload.meaning_impact,
                classification=payload.classification,
                confidence=payload.confidence,
                severity=payload.severity,
                observed_at=row.occurred_at,
                attach_to=payload.attach_to,
                distinct=payload.distinct,
                track=track_id,
                command="session.close",
            )
            error_report = error_service.write_occurrence(transaction, occurrence)
            # The *occurrence*, not the pattern: two corrections of one error are two
            # occurrences of it, and naming the pattern here would make them look like
            # one durable row credited twice.
            assert error_report.recorded_occurrence_id is not None
            materialized_id = error_report.recorded_occurrence_id
            counts["errors"] += 1
            errors_touched.append(error_report.error_id)
        elif row.kind == "pronunciation.assessment":
            materialized_id = _materialize_pronunciation(
                transaction,
                row=row,
                payload=payload,
                track_id=track_id,
                now=now,
            )
            counts["observations"] += 1
        elif row.kind == "source.progress":
            materialized_id = source_service.materialize_progress(
                transaction,
                track_id=track_id,
                session_id=session_id,
                source=payload.source_ref,
                band=payload.band,
                unit=payload.unit,
                aid=payload.aid,
                mode=payload.mode,
                replays=payload.replays,
                lookups=payload.lookups,
                minutes=payload.minutes,
                completed=payload.completed,
                note=payload.note,
                observed_at=row.occurred_at,
                now=now,
            )
            counts["comprehension"] += 1
            sources_worked.add(payload.source_ref)
        elif row.kind == "follow_up":
            followup = error_service.write_followup(
                transaction,
                kind=payload.kind,
                action=payload.action,
                target=payload.target,
                error=payload.error,
                priority=payload.priority,
                due_from=payload.due_from,
                due_by=payload.due_by,
                track=track_id,
                command="session.close",
            )
            materialized_id = followup.followup_id
            counts["followups"] += 1
        else:
            observation = evidence_service.write_observation(
                transaction,
                category=_observation_category(row, payload),
                note=_observation_note(row, payload),
                salience=_observation_salience(row, payload),
                observed_at=row.occurred_at,
                track=track_id,
                command="session.close",
            )
            materialized_id = observation.observation_id
            counts["observations"] += 1
        transaction.execute(
            "UPDATE session_staged_events SET status = 'materialized', finalization_id = ?, "
            "materialized_kind = ?, materialized_id = ? WHERE staged_event_id = ?",
            [finalization_id, materialized_kind, materialized_id, row.staged_event_id],
        )
        worked_blocks.add(row.block_id)
        if row.activity_id is not None:
            transaction.execute(
                "UPDATE activities SET status = 'completed', updated_at = ? WHERE activity_id = ?",
                [naive_utc(now), row.activity_id],
            )
    planned_core = int(
        transaction.scalar(
            "SELECT count(*) FROM session_blocks WHERE session_id = ? AND role = 'core'",
            [session_id],
        )
    )
    transaction.execute(
        "UPDATE session_blocks SET status = 'completed', updated_at = ? "
        "WHERE session_id = ? AND block_id IN "
        "(SELECT DISTINCT block_id FROM session_staged_events "
        " WHERE session_id = ? AND status = 'materialized' AND block_id IS NOT NULL)",
        [naive_utc(now), session_id, session_id],
    )
    transaction.execute(
        "UPDATE session_blocks SET status = 'skipped', updated_at = ? "
        "WHERE session_id = ? AND status IN ('planned', 'active')",
        [naive_utc(now), session_id],
    )
    # An activity nobody worked is skipped rather than left `planned`: a session that has
    # ended holds no plans, and `resume` decides what is next from these statuses.
    transaction.execute(
        "UPDATE activities SET status = 'completed', updated_at = ? WHERE status = 'planned' "
        "AND block_id IN (SELECT block_id FROM session_blocks WHERE session_id = ? "
        "AND status = 'completed') AND block_id IN "
        "(SELECT DISTINCT block_id FROM session_staged_events WHERE session_id = ? "
        " AND status = 'materialized' AND block_id IS NOT NULL AND activity_id IS NULL)",
        [naive_utc(now), session_id, session_id],
    )
    transaction.execute(
        "UPDATE activities SET status = 'skipped', updated_at = ? WHERE status IN "
        "('planned', 'active') AND block_id IN "
        "(SELECT block_id FROM session_blocks WHERE session_id = ?)",
        [naive_utc(now), session_id],
    )
    return {
        "counts": counts,
        "stage_changes": tuple(stage_changes),
        "errors_touched": tuple(dict.fromkeys(errors_touched)),
        "dimensions": tuple(sorted(dimensions)),
        "sources_worked": tuple(sorted(sources_worked)),
        "worked_blocks": worked_blocks,
        "planned_core_blocks": planned_core,
    }


def _observation_category(row: _StagedRow, payload: Any) -> str:
    if row.kind == "observation.noted":
        return str(payload.category)
    # A pronunciation event is a note in this stage: pronunciation *evidence* comes from
    # attempts in a pronunciation block, and confirming how something sounded needs the
    # utterance-and-audio model Stage 5 owns. Recording it as a note keeps the
    # observation without letting a transcript become a claim about speech.
    return "note"


def _observation_note(row: _StagedRow, payload: Any) -> str:
    if row.kind == "observation.noted":
        return str(payload.note)
    basis = "audio" if row.evidence_basis == "audio" else "transcript only"
    return f"pronunciation {payload.status} ({basis}): {payload.note}".strip()


def _observation_salience(row: _StagedRow, payload: Any) -> str:
    if row.kind == "observation.noted":
        return str(payload.salience)
    return "high" if payload.status == "confirmed" else "medium"


def _assert_close_key_belongs(database: Database, *, session_id: str, idempotency_key: str) -> None:
    """One close key names one close, in both directions.

    Two ways to break that, and both are checked here because both mean the caller has
    the wrong idea about what they are retrying:

    - the key already closed a **different** session, so using it here would make one key
      identify two closes;
    - this session was closed under a **different** key, so the caller is retrying a call
      they never made -- and returning this session's result to them would confirm a
      belief that is wrong.
    """

    owner = database.one(
        "SELECT session_id FROM session_finalizations WHERE idempotency_key = ?",
        [idempotency_key],
    )
    if owner is not None and str(owner[0]) != session_id:
        raise LinguaWikiError(
            "idempotency_conflict",
            f"close key {idempotency_key} already closed session {owner[0]}; a retry must "
            "name the session it closed, and closing another one needs its own key",
            details=(
                ErrorDetail(field="idempotency_key", reason="reused for another session"),
                ErrorDetail(field="session", reason=str(owner[0])),
            ),
        )
    stored_key = database.scalar(
        "SELECT idempotency_key FROM session_finalizations WHERE session_id = ?",
        [session_id],
    )
    if stored_key is not None and str(stored_key) != idempotency_key:
        raise LinguaWikiError(
            "idempotency_conflict",
            f"session {session_id} was closed under a different key; this key has closed "
            "nothing, so there is no result to return for it. `session show` reports what "
            "that close recorded.",
            details=(
                ErrorDetail(field="idempotency_key", reason="does not match the stored close"),
                ErrorDetail(field="session", reason=session_id),
            ),
        )


def close(
    paths: WorkspacePaths,
    *,
    outcome: str = "completed",
    session: str | None = None,
    track: str | None = None,
    actual_minutes: int | None = None,
    fatigue: str | None = None,
    summary: str | None = None,
    discard_blocks: Sequence[str] = (),
    idempotency_key: str | None = None,
    expected_staging: str | None = None,
    clock: Clock | None = None,
    command: str = "session.close",
    actor: str = "cli",
) -> CloseReport:
    """Finalize a session: materialize its staged work exactly once, in one transaction.

    The order of operations is the recovery story:

    1. a session that already has a finalization returns that stored result. The close
       happened; the caller only lost the answer;
    2. the session moves to `closing` in its own short transaction, so a crash inside
       step 4 leaves a state that says "a close was interrupted" rather than one
       indistinguishable from an untouched session;
    3. everything is *resolved* -- claims, novelty, delays, error identities -- with no
       writes, because those decisions read the database and can still fail;
    4. one transaction writes the attempts, evidence, occurrences, follow-ups, notes,
       block statuses, the session's own status, the projection's staleness, and the
       finalization row holding this report. Either all of that is true afterwards or
       none of it is.

    A keyed close is bound to a hash of its whole request, so a retry with other
    arguments is a conflict rather than a replay that pretends to honour them.
    `expected_staging` is the digest of the staged set the caller confirmed: when the set
    has changed since, the close is refused before `closing` is written, so a close
    never credits work its caller did not see.
    """

    if outcome not in session_policy.CLOSE_OUTCOMES:
        raise LinguaWikiError(
            "unknown_close_outcome",
            f"a close is {' or '.join(session_policy.CLOSE_OUTCOMES)}; abandoning is a "
            "different command",
            details=(ErrorDetail(field="outcome", reason=outcome),),
        )
    if fatigue is not None:
        assert_known(
            fatigue, vocabulary=("low", "medium", "high"), field="fatigue", code="unknown_fatigue"
        )
    if actual_minutes is not None and actual_minutes < 0:
        raise LinguaWikiError(
            "invalid_actual_minutes",
            "a session cannot have run for a negative number of minutes",
            details=(ErrorDetail(field="actual_minutes", reason=str(actual_minutes)),),
        )
    if summary is not None and len(summary) > MAXIMUM_SUMMARY_LENGTH:
        raise LinguaWikiError(
            "invalid_session_summary",
            f"a session summary is at most {MAXIMUM_SUMMARY_LENGTH} characters; this one "
            f"is {len(summary)}",
            details=(ErrorDetail(field="summary", reason=str(len(summary))),),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id, session_id = resolve_session_and_track(database, session, track)
        row = _session_row(database, session_id)
        status = str(row[2])
        # Before the replay, not after it. A finalized session returned its stored result
        # without ever comparing the key, so one key could successfully identify two
        # different closes -- which is the opposite of what an idempotency key is for.
        request = _close_request_hash(
            session_id=session_id,
            outcome=outcome,
            actual_minutes=actual_minutes,
            fatigue=fatigue,
            summary=summary,
            discard_blocks=discard_blocks,
            expected_staging=expected_staging,
        )
        if idempotency_key is not None:
            _assert_close_key_belongs(
                database, session_id=session_id, idempotency_key=idempotency_key
            )
        stored = _read_finalization(database, session_id=session_id)
        replay_warnings: tuple[str, ...] = ()
        if stored is not None and idempotency_key is not None:
            replay_warnings = _vouch_for_keyed_close(
                database, session_id=session_id, key=idempotency_key, request=request
            )
        elif idempotency_key is not None and (
            idempotency.resolve(
                database, key=idempotency_key, event_type="session.closed", request_hash=request
            )
            is not None
        ):
            # An event under this key with no finalization behind it: the record is
            # damaged, and replaying nothing as if it were a close would be a lie.
            raise _unvouched(idempotency_key, session_id)
        if stored is not None:
            if stored.outcome != outcome:
                raise LinguaWikiError(
                    "session_already_finalized",
                    f"session {session_id} was already closed as {stored.outcome}; a close "
                    f"cannot be redone as {outcome}",
                    details=(
                        ErrorDetail(field="outcome", reason=stored.outcome),
                        ErrorDetail(field="finalization", reason=stored.finalization_id),
                    ),
                )
            return stored.model_copy(
                update={
                    "replayed": True,
                    "warnings": (
                        *stored.warnings,
                        *replay_warnings,
                        "this session was already closed; the original result is returned "
                        "and nothing was written again",
                    ),
                }
            )
        if status in session_policy.TERMINAL_STATUSES:
            raise LinguaWikiError(
                "session_not_closable",
                f"session {session_id} is {status} with no finalization, so there is "
                "nothing to close; staged work can be recovered into a new session",
                details=(ErrorDetail(field="status", reason=status),),
            )
        missing = _missing_batch_sequences(database, session_id=session_id)
        if missing and outcome == "completed":
            raise LinguaWikiError(
                "session_batch_gap",
                f"session {session_id} is missing batch sequence(s) "
                f"{', '.join(str(number) for number in missing)}; a flush was lost, so this "
                "session cannot be credited as complete. Re-send the missing batch, or "
                "close as partial once its work has been reviewed.",
                details=tuple(
                    ErrorDetail(field="sequence", reason=str(number)) for number in missing
                ),
            )
        for block in discard_blocks:
            _resolve_block(database, session_id=session_id, block=block)
        if expected_staging is not None:
            current = _staging_state(database, session_id=session_id)
            if current.digest != expected_staging:
                raise LinguaWikiError(
                    "session_staging_changed",
                    f"session {session_id} holds different staged work from what this close "
                    f"was confirmed against: {current.count} event(s) are staged now. Review "
                    "them and confirm the close again.",
                    details=(
                        ErrorDetail(field="expected_staging", reason=expected_staging),
                        ErrorDetail(
                            field="staging",
                            reason=current.digest,
                            context={"count": current.count},
                        ),
                    ),
                )
        now = aware_utc(database.now())
        if status != "closing":
            # Its own transaction: the durable `closing` marker has to survive a failure
            # of the work that follows it.
            with database.transaction() as transaction:
                _set_status(
                    transaction,
                    session_id=session_id,
                    current=status,
                    target="closing",
                    now=now,
                    closing=True,
                )
        rows = _staged_rows(database, session_id=session_id, discard_blocks=discard_blocks)
        finalization_id = str(FinalizationId.new())
        batch_range = database.one(
            "SELECT min(sequence), max(sequence) FROM session_event_batches WHERE session_id = ?",
            [session_id],
        )
        first_sequence = (
            None if batch_range is None or batch_range[0] is None else int(batch_range[0])
        )
        last_sequence = (
            None if batch_range is None or batch_range[1] is None else int(batch_range[1])
        )
        with database.transaction() as transaction:
            written = _materialize(
                transaction,
                session_id=session_id,
                track_id=track_id,
                correction_mode=str(row[8]),
                finalization_id=finalization_id,
                rows=rows,
                now=now,
            )
            counts = written["counts"]
            report = CloseReport(
                session_id=session_id,
                finalization_id=finalization_id,
                outcome=outcome,
                status=outcome,
                staged_consumed=len(rows) - counts["discarded"],
                staged_discarded=counts["discarded"],
                attempts_written=counts["attempts"],
                evidence_written=counts["evidence"],
                errors_written=counts["errors"],
                followups_written=counts["followups"],
                observations_written=counts["observations"],
                comprehension_written=counts["comprehension"],
                sources_worked=written["sources_worked"],
                first_batch_sequence=first_sequence,
                last_batch_sequence=last_sequence,
                stage_changes=written["stage_changes"],
                errors_touched=written["errors_touched"],
                dimensions_recomputed=written["dimensions"],
                calculation_versions=_calculation_versions(),
                projection_stale=True,
                summary=summary,
                closed_at=now.isoformat(),
                warnings=_close_warnings(
                    outcome=outcome,
                    rows=rows,
                    worked_blocks=written["worked_blocks"],
                    planned_core_blocks=written["planned_core_blocks"],
                    missing_batches=missing,
                ),
            )
            _set_status(
                transaction,
                session_id=session_id,
                current="closing",
                target=outcome,
                now=now,
                closed=True,
                actual_minutes=actual_minutes,
                fatigue=fatigue,
                summary=summary,
            )
            transaction.execute(
                "INSERT INTO session_finalizations (finalization_id, session_id, outcome, "
                "idempotency_key, first_batch_sequence, last_batch_sequence, staged_consumed, "
                "staged_discarded, attempts_written, evidence_written, errors_written, "
                "followups_written, observations_written, calculation_versions_json, "
                "result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    finalization_id,
                    session_id,
                    outcome,
                    idempotency_key or finalization_id,
                    first_sequence,
                    last_sequence,
                    report.staged_consumed,
                    report.staged_discarded,
                    report.attempts_written,
                    report.evidence_written,
                    report.errors_written,
                    report.followups_written,
                    report.observations_written,
                    json.dumps(report.calculation_versions, sort_keys=True),
                    report.model_dump_json(),
                    naive_utc(now),
                ],
            )
            # The projection is a function of this data, so it is stale the moment the
            # data moves. Marked here rather than by `wiki build`, which only ever
            # *clears* staleness.
            transaction.execute(
                "UPDATE projection_state SET stale = TRUE, updated_at = ? WHERE projection = ?",
                [naive_utc(now), "wiki"],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                actor=actor,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([session_id, finalization_id]),
                before_summary=f"status {status}",
                after_summary=(
                    f"{outcome}: {report.attempts_written} attempt(s), "
                    f"{report.evidence_written} evidence row(s), {report.errors_written} "
                    f"error occurrence(s), {report.followups_written} follow-up(s)"
                ),
            )
            migration_module.record_domain_event(
                transaction,
                event_type="session.closed",
                aggregate_type="session",
                aggregate_id=session_id,
                correlation_id=EventId.new(),
                payload_json=idempotency.payload(
                    request,
                    outcome=outcome,
                    attempts=report.attempts_written,
                    evidence=report.evidence_written,
                    errors=report.errors_written,
                    staged_consumed=report.staged_consumed,
                ),
                idempotency_key=idempotency_key or finalization_id,
            )
        return report


def _close_warnings(
    *,
    outcome: str,
    rows: Sequence[_StagedRow],
    worked_blocks: set[str | None],
    planned_core_blocks: int,
    missing_batches: Sequence[int],
) -> tuple[str, ...]:
    """What the reader should know about what this close did and did not credit."""

    warnings: list[str] = []
    covered = len({block for block in worked_blocks if block is not None})
    if outcome == "completed" and covered < planned_core_blocks:
        # A session can be closed as completed and still have covered less than it
        # planned. Saying so is the difference between a learner's record and a
        # learner's impression of their record.
        warnings.append(
            f"{covered} of {planned_core_blocks} planned core block(s) produced "
            "observations; the rest are recorded as skipped"
        )
    if missing_batches:
        warnings.append(
            "flush sequence(s) "
            + ", ".join(str(number) for number in missing_batches)
            + " never arrived, so this close credits only the batches that did; the "
            "missing observations are lost rather than pending"
        )
    if not rows:
        warnings.append(
            "this session had no staged observations, so it closed without changing "
            "anything about the learner"
        )
    transcript_only = sum(
        1
        for row in rows
        if row.kind == "pronunciation.assessment" and row.evidence_basis != "audio"
    )
    if transcript_only:
        warnings.append(
            f"{transcript_only} pronunciation observation(s) rest on a transcript rather "
            "than audio, so they are recorded as notes and confirm nothing about "
            "pronunciation"
        )
    audio_backed = sum(
        1
        for row in rows
        if row.kind == "pronunciation.assessment" and row.evidence_basis == "audio"
    )
    if audio_backed:
        warnings.append(
            f"{audio_backed} pronunciation observation(s) are linked to audio and recorded "
            "as notes; pronunciation evidence comes from attempts in a pronunciation block"
        )
    if outcome == "partial":
        warnings.append(
            "closed as partial: the staged work is credited and the objectives of blocks "
            "that were not worked are not"
        )
    return tuple(warnings)


def partial_close(
    paths: WorkspacePaths,
    *,
    session: str | None = None,
    track: str | None = None,
    actual_minutes: int | None = None,
    fatigue: str | None = None,
    summary: str | None = None,
    discard_blocks: Sequence[str] = (),
    idempotency_key: str | None = None,
    expected_staging: str | None = None,
    clock: Clock | None = None,
    command: str = "session.partial-close",
    actor: str = "cli",
) -> CloseReport:
    """Close a deliberately shortened session, crediting only the work that happened.

    `--discard-block` is what makes this a *reviewed* partial close: a block the learner
    started and abandoned mid-way can be excluded by name, and its staged events are
    kept with the reason rather than deleted.
    """

    return close(
        paths,
        outcome="partial",
        session=session,
        track=track,
        actual_minutes=actual_minutes,
        fatigue=fatigue,
        summary=summary,
        discard_blocks=discard_blocks,
        idempotency_key=idempotency_key,
        expected_staging=expected_staging,
        clock=clock,
        command=command,
        actor=actor,
    )


# --- Session-package ingestion -----------------------------------------------------


#: What a package's own event kinds become when they are staged. A package carries the
#: same vocabulary as a flush, so the mapping is the identity -- stated rather than
#: assumed, because a future package schema may add a kind this stage cannot materialize.
#: Fields a package's free-form `details` may never set. Each is either decided by this
#: workspace (what of the learner's words it keeps) or is provenance about the package
#: itself, and a file that could set them would be a file that rewrites its own record.
PROTECTED_PACKAGE_FIELDS: frozenset[str] = frozenset(
    {
        "response",
        "response_visibility",
        "response_hash",
        "source",
        "package_id",
        "external_session_id",
        "utterance_id",
        "transcript_layer",
        "status",
        "audio_artifact_id",
    }
)

PACKAGE_EVENT_KINDS: tuple[str, ...] = (
    "attempt.observed",
    "correction.given",
    "pronunciation.assessment",
    "follow_up",
)


def _package_retention(track: learner_service.TrackRecord) -> str:
    """How much of an external transcript this track has consented to keep.

    Staged payloads follow the same retention policy as anything else the learner said,
    which is why this is read from the track rather than from the package: a producer
    does not get to decide what a workspace keeps.
    """

    preferences = _preferences(track)
    if preferences.get("audio_retention_consent") and preferences.get(
        "transcript_retention_consent"
    ):
        return "full"
    if preferences.get("transcript_retention_consent"):
        return "excerpt"
    return "withheld"


def _package_manifest(package: Any, *, retention: str) -> dict[str, Any]:
    """What arrived, without the transcript itself.

    The manifest is metadata a report can show: how many utterances per layer, which
    artifacts, whether audio is retained. The utterance *text* is not in it, so a
    workspace that refused transcript retention does not keep one here by accident.
    """

    return {
        "layers": [
            {
                "kind": layer.kind,
                "derived_from": layer.derived_from,
                "utterances": len(layer.utterances),
                "speakers": sorted({str(utterance.speaker) for utterance in layer.utterances}),
            }
            for layer in package.transcript_layers
        ],
        "artifacts": [
            {
                "artifact_id": str(artifact.artifact_id),
                "kind": str(artifact.kind),
                "sha256": artifact.sha256,
                "retained": artifact.retained,
            }
            for artifact in package.artifacts
        ],
        "learning_targets": [str(target) for target in package.learning_targets],
        "retention_policy": retention,
        "event_kinds": sorted({event.kind for event in package.events}),
    }


def _package_audio_available(
    database: Database, validated: Any, *, audio: Mapping[str, str], root: Path
) -> bool:
    """Whether a recording this package names is audio this workspace can still play.

    Four conditions, and each was wrong once: the artifact has to be *audio*, it has to be
    *kept*, it has to be one this *package* declared, and the file has to be there with the
    bytes it was registered with. Counting resolved artifacts made a transcript-only package
    claim audio; omitting the field from the duplicate return made a re-ingest claim none
    while its recording sat there; and reading only the row said "available" of a recording
    somebody had deleted by hand.

    The last condition costs a hash of one file, which is a file this command has usually
    just hashed. That is the price of the word meaning what it says.
    """

    from linguawiki.services import artifacts as artifact_service

    declared = {str(artifact.artifact_id) for artifact in validated.artifacts}
    resolved = [artifact_id for external, artifact_id in audio.items() if external in declared]
    if not resolved:
        return False
    placeholders = ", ".join("?" for _ in resolved)
    playable = database.query(
        f"SELECT relative_path, sha256 FROM artifacts WHERE artifact_id IN ({placeholders}) "
        "AND kind = 'audio' AND retained AND purged_at IS NULL",
        resolved,
    )
    # `contained_file` rather than a join: a registered path replaced by a symlink out of
    # the workspace made a file somewhere else count as the learner's recording. And
    # `digest_or_none` rather than a bare hash, because a recording that is unreadable, or
    # that disappears between the check and the open, means "not available" -- raising turned
    # a duplicate retry, which this command deliberately accepts as a no-op, into a failure.
    return any(
        (contained := artifact_service.contained_file(root, str(relative_path))) is not None
        and artifact_service.digest_or_none(contained) == str(digest)
        for relative_path, digest in playable
    )


def package_problems(
    database: Database,
    validated: Any,
    *,
    package_hash: str,
    session: str | None,
    track: str | None,
    root: Path,
) -> tuple[str, list[tuple[str, str]], tuple[str, str] | None, Mapping[str, Any]]:
    """Every reason this package could not be ingested, collected rather than raised.

    One implementation, used by the review that reports problems and by the ingestion that
    refuses on them. They had drifted twice: review checked the track hint and not the
    language, the session, the event identity, or whether an event could be materialized,
    so it called packages valid that ingestion refused -- and it ran the audio checks on an
    exact duplicate, which ingestion deliberately accepts as a no-op retry.

    Returns the resolved track, the problems as `(code, message)` pairs, the duplicate this
    content already has here, and what the workspace holds of its audio. The codes travel
    with the messages because a skill reading the error envelope needs to tell "this names
    audio nobody has" from "this session is closed", and a refusal that collapsed several
    distinct failures into one generic code took that away.
    """

    from linguawiki.services import artifacts as artifact_service
    from linguawiki.services import transcripts as transcript_service

    problems: list[tuple[str, str]] = []
    track_id = learner_service.resolve_track(database, track)
    record = learner_service.track_context(database, track_id)
    for check in (_assert_package_belongs,):
        try:
            check(validated, track_id=track_id, record=record)
        except LinguaWikiError as failure:
            # A package on the wrong track or in the wrong language says nothing reliable
            # about anything else, so this is the one group that stops the pass.
            return track_id, [(failure.payload.code, failure.payload.message)], None, {}
    duplicate = database.one(
        "SELECT ingestion_id, session_id, track_id FROM session_packages WHERE package_hash = ?",
        [package_hash],
    )
    if duplicate is not None:
        if str(duplicate[2]) != track_id:
            problems.append(
                (
                    "package_track_conflict",
                    f"this package's content is already ingested on track {duplicate[2]}, "
                    f"and this ingestion is for {track_id}. One recording belongs to one "
                    "learner: export it from the producer under that learner's own "
                    "session, or ingest it on the track that owns it.",
                )
            )
            return track_id, problems, None, {}
        # A retry of content already here changes nothing, so nothing else is checked: the
        # audio it once named may since have been purged, and that is not a reason to
        # refuse a no-op.
        return track_id, problems, (str(duplicate[0]), str(duplicate[1] or "")), {}
    try:
        session_id = resolve_session(
            database,
            session or (str(validated.session_id) if validated.session_id else None),
            track_id=track_id,
        )
        _assert_loggable(session_id=session_id, status=str(_session_row(database, session_id)[2]))
    except LinguaWikiError as failure:
        # Without a session to attach to, the event checks below have nothing to run
        # against; the audio and transcript checks still do.
        problems.append((failure.payload.code, failure.payload.message))
        session_id = None
    staged_events = [event for event in validated.events if event.kind in PACKAGE_EVENT_KINDS]
    if session_id is not None:
        try:
            _assert_events_are_new(
                database,
                session_id=session_id,
                event_ids=[str(event.event_id) for event in staged_events],
                external_session_id=validated.external_session_id,
                track_id=track_id,
            )
        except LinguaWikiError as failure:
            problems.append((failure.payload.code, failure.payload.message))
    retention = _package_retention(record)
    for event in staged_events:
        try:
            assert_materializable(
                _retained_payload(
                    _package_event_payload(event, package=validated, retention=retention),
                    preferences=_preferences(record),
                ),
                kind=event.kind,
                reference=f"event {event.event_id}",
            )
        except LinguaWikiError as failure:
            problems.append((failure.payload.code, failure.payload.message))
    try:
        transcript_service.assert_import_is_possible(database, validated, track_id=track_id)
    except LinguaWikiError as failure:
        problems.append((failure.payload.code, failure.payload.message))
    registered = artifact_service.registered_by_producer(database, track_id=track_id)
    problems.extend(
        (artifact_service.AUDIO_PROBLEM_CODE, entry)
        for entry in artifact_service.declared_audio_problems(
            validated,
            root=root,
            registered=registered,
            # Workspace-wide, like the writer's own rule: producer identifiers are per track
            # but the files they name are not.
            owners=artifact_service.path_owners(database),
            track_id=track_id,
            audio_consent=bool(_preferences(record).get("audio_retention_consent")),
        )
    )
    return track_id, problems, None, registered


@dataclass(frozen=True, slots=True)
class _Preflight:
    """What a read-only pass over a package established."""

    track_id: str
    #: Set when this content is already ingested here. Nothing is registered and nothing
    #: is staged: a duplicate must not have the side effects of a first ingestion.
    duplicate_of: tuple[str, str] | None
    registered: Mapping[str, Any]


def _preflight_package(
    paths: WorkspacePaths,
    validated: Any,
    *,
    package_hash: str,
    session: str | None,
    track: str | None,
    clock: Clock,
) -> _Preflight:
    """Every refusal a package can earn, in one read-only pass.

    Nothing here writes, and that is the whole design. Registering the audio used to happen
    at the top of this function, *before* the track, language, session-state, duplicate,
    event-identity, and materializability checks that ran inside the writer -- so a package
    refused for being in the wrong language had already left its recording registered in
    the learner's workspace. Every check that can refuse now runs before anything is
    written, and the write happens once this returns.

    The checks are repeated inside the writer afterwards. They are cheap, and they are the
    real guard against a workspace that changed between the read and the write; what this
    pass buys is that the *common* case refuses without side effects.
    """

    with open_reader(paths, clock=clock) as database:
        track_id, problems, duplicate_of, registered = package_problems(
            database,
            validated,
            package_hash=package_hash,
            session=session,
            track=track,
            root=paths.root,
        )
    if problems:
        codes = {code for code, _ in problems}
        raise LinguaWikiError(
            # One code when every problem agrees on what went wrong, whether there is one
            # of them or five; the shared code only when they genuinely differ, because a
            # single code cannot honestly describe a mixed list.
            codes.pop() if len(codes) == 1 else "package_not_ingestable",
            "; ".join(message for _, message in problems),
            details=tuple(ErrorDetail(field=code, reason=message) for code, message in problems),
        )
    return _Preflight(track_id=track_id, duplicate_of=duplicate_of, registered=registered)


def _assert_package_belongs(validated: Any, *, track_id: str, record: Any) -> None:
    """Refuse a package that is somebody else's, or in another language."""

    if validated.track_hint is not None and str(validated.track_hint) != track_id:
        raise LinguaWikiError(
            "package_track_mismatch",
            f"the package names track {validated.track_hint}, and this ingestion is "
            f"for {track_id}; an external recording belongs to one learner",
            details=(ErrorDetail(field="track_hint", reason=str(validated.track_hint)),),
        )
    if validated.target_language.lower() != record.target_language.lower():
        raise LinguaWikiError(
            "package_language_mismatch",
            f"the package is a {validated.target_language} session and this track "
            f"studies {record.target_language}; a recording in another language is "
            "not this track's evidence",
            details=(
                ErrorDetail(field="target_language", reason=validated.target_language),
                ErrorDetail(field="track", reason=record.target_language),
            ),
        )


def ingest_package(
    paths: WorkspacePaths,
    *,
    package: Mapping[str, Any],
    session: str | None = None,
    track: str | None = None,
    file_sha256: str | None = None,
    producer: str | None = None,
    clock: Clock | None = None,
    command: str = "session.ingest-package",
) -> IngestReport:
    """Validate an externally produced session and stage its events.

    Every refusal a package can earn happens here, before the first row is written, and
    that is deliberate. Staging the events and storing the transcript are separate
    transactions -- each idempotent on its own content -- so nothing can undo the other,
    and the order is the only thing that keeps a rejected package from leaving half of
    itself behind. The checks live in this function rather than in `speaking.ingest`
    because this is the *lower* entry point: anything that stages a package's events comes
    through here, and a safeguard only one caller runs is a safeguard with a way around it.

    Refusals about not trusting a file:

    - an unsupported schema is refused with the name and version it declared, so the
      operator can migrate the producer rather than guess why nothing happened;
    - a package whose *content* was already ingested stages nothing. Deduplication is by
      canonical hash, so the same conversation exported twice is one session however the
      bytes were arranged;
    - a package naming a session on another track is refused: an external recording is
      one learner's, and attaching it elsewhere would put their work in another
      learner's model.

    - a layer declared `normalized` whose words differ from the raw layer is refused, and
      so is audio the package names that is missing, altered, outside the private roots,
      or already held here under different bytes;
    - a package confirming pronunciation on a track that has not consented to keeping
      audio is refused: the recording would be deleted at the door and the claim would
      rest on nothing.

    Events are staged, never materialized. A package is somebody else's account of a
    session, and it becomes part of the learner's model through the same close as
    everything else.

    This stages the events; it does not store the transcript. `speaking.ingest` does both
    and is what every command surface calls.
    """

    from linguawiki.contracts import SessionPackage
    from linguawiki.services import artifacts as artifact_service

    schema_name = package.get("schema_name", "lingua.session.v1")
    schema_version = package.get("schema_version", 1)
    if schema_name != "lingua.session.v1" or schema_version != 1:
        raise LinguaWikiError(
            "unsupported_package_schema",
            f"this release ingests lingua.session.v1 version 1; the package declares "
            f"{schema_name} version {schema_version}. Export it from the producer at the "
            "supported version, or convert it before ingesting.",
            details=(
                ErrorDetail(field="schema_name", reason=str(schema_name)),
                ErrorDetail(field="schema_version", reason=str(schema_version)),
            ),
        )
    validated = validated_contract(
        SessionPackage, package, code="invalid_session_package", subject="session package"
    )
    package_hash = canonical_hash(validated.model_dump(mode="json"))
    active_clock = clock or SystemClock()
    preflight = _preflight_package(
        paths,
        validated,
        package_hash=package_hash,
        session=session,
        track=track,
        clock=active_clock,
    )
    audio: dict[str, str] = {}
    if preflight.duplicate_of is None:
        # After every refusal and before the first staged row: a package that cannot be
        # ingested registers nothing, and a package that can has its recording in place
        # before an utterance or a claim needs to name it.
        from linguawiki.services import artifacts as artifact_service

        audio = artifact_service.register_declared_audio(
            paths,
            validated,
            track=preflight.track_id,
            registered=preflight.registered,
            clock=active_clock,
            command=command,
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        _assert_package_belongs(validated, track_id=track_id, record=record)
        existing = database.one(
            "SELECT ingestion_id, session_id, event_count, track_id FROM session_packages "
            "WHERE package_hash = ?",
            [package_hash],
        )
        if existing is not None:
            # A duplicate is only a duplicate *for the learner who has it*. Reporting
            # another learner's session as this ingestion's home told learner B where
            # learner A's recording went, and answered "already ingested" to a learner
            # who has never had it.
            if str(existing[3]) != track_id:
                raise LinguaWikiError(
                    "package_track_conflict",
                    f"this package's content is already ingested on track {existing[3]}, "
                    f"and this ingestion is for {track_id}. One recording belongs to one "
                    "learner: export it from the producer under that learner's own "
                    "session, or ingest it on the track that owns it.",
                    details=(
                        ErrorDetail(field="package_hash", reason=package_hash),
                        ErrorDetail(field="track", reason=str(existing[3])),
                    ),
                )
            return IngestReport(
                ingestion_id=str(existing[0]),
                package_id=validated.package_id,
                package_hash=package_hash,
                session_id=str(existing[1]) if existing[1] else "",
                track_id=track_id,
                external_session_id=validated.external_session_id,
                mode=validated.mode,
                staged_events=0,
                skipped_events=len(validated.events),
                audio_available=_package_audio_available(
                    database,
                    validated,
                    root=paths.root,
                    audio={
                        external: held.artifact_id
                        for external, held in artifact_service.registered_by_producer(
                            database, track_id=track_id
                        ).items()
                    },
                ),
                retention_policy=_package_retention(record),
                duplicate=True,
                warnings=(
                    "this package's content was already ingested, so nothing was staged "
                    "again; its events are on the session it was first attached to",
                ),
            )
        session_id = resolve_session(
            database,
            session or (str(validated.session_id) if validated.session_id else None),
            track_id=track_id,
        )
        row = _session_row(database, session_id)
        _assert_loggable(session_id=session_id, status=str(row[2]))
        retention = _package_retention(record)
        # This package's *audio*, as this workspace holds it. `bool(audio)` counted every
        # resolved artifact, so a transcript-only package reported audio available -- and a
        # transcript is exactly what cannot support an acoustic claim.
        audio_available = _package_audio_available(
            database, validated, audio=audio, root=paths.root
        )
        staged_events = [event for event in validated.events if event.kind in PACKAGE_EVENT_KINDS]
        skipped = len(validated.events) - len(staged_events)
        _assert_events_are_new(
            database,
            session_id=session_id,
            event_ids=[str(event.event_id) for event in staged_events],
            external_session_id=validated.external_session_id,
            track_id=track_id,
        )
        # Built, retention-filtered, and *then* validated -- before anything is stored.
        # A package's events carry an utterance and free-form details, so the payload a
        # close would have to materialize is one this function constructs: validating it
        # at close meant a package could be accepted and then refuse to close, leaving
        # the session stuck with work nobody could credit.
        prepared: list[dict[str, Any]] = []
        for event in staged_events:
            payload = _retained_payload(
                _package_event_payload(event, package=validated, retention=retention),
                preferences=_preferences(record),
            )
            assert_materializable(payload, kind=event.kind, reference=f"event {event.event_id}")
            prepared.append(payload)
        sequence = int(
            database.scalar(
                "SELECT coalesce(max(sequence), 0) + 1 FROM session_event_batches "
                "WHERE session_id = ?",
                [session_id],
            )
        )
        ingestion_id = str(IngestionId.new())
        batch_id = str(BatchId.new()) if staged_events else None
        now = aware_utc(database.now())
        warnings: list[str] = []
        if skipped:
            warnings.append(
                f"{skipped} event(s) of a kind this release cannot materialize were not "
                "staged; they remain in the package manifest"
            )
        if retention == "withheld":
            warnings.append(
                "this track has not consented to transcript retention, so the package's "
                "utterance text is not stored; its events are staged without it"
            )
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO session_packages (ingestion_id, package_id, schema_name, "
                "schema_version, package_hash, file_sha256, track_id, session_id, "
                "external_session_id, producer, mode, ingest_status, manifest_json, "
                "retention_policy, event_count, artifact_count, audio_available, started_at, "
                "ended_at, ingested_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ingestion_id,
                    validated.package_id,
                    validated.schema_name,
                    validated.schema_version,
                    package_hash,
                    file_sha256,
                    track_id,
                    session_id,
                    validated.external_session_id,
                    producer,
                    validated.mode,
                    json.dumps(
                        _package_manifest(validated, retention=retention),
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    retention,
                    len(validated.events),
                    len(validated.artifacts),
                    audio_available,
                    naive_utc(validated.started_at),
                    naive_utc(validated.ended_at),
                    naive_utc(now),
                    naive_utc(now),
                ],
            )
            if batch_id is not None:
                payloads = prepared
                transaction.execute(
                    "INSERT INTO session_event_batches (batch_id, session_id, sequence, "
                    "idempotency_key, content_hash, event_count, source, ingestion_id, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, 'package', ?, ?)",
                    [
                        batch_id,
                        session_id,
                        sequence,
                        f"package:{package_hash}",
                        canonical_hash(payloads),
                        len(staged_events),
                        ingestion_id,
                        naive_utc(now),
                    ],
                )
                for position, (event, payload) in enumerate(
                    zip(staged_events, payloads, strict=True), start=1
                ):
                    transaction.execute(
                        "INSERT INTO session_staged_events (staged_event_id, session_id, "
                        "batch_id, sequence, kind, schema_version, payload_json, "
                        "evidence_basis, status, occurred_at, source_event_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?)",
                        [
                            str(StagedEventId.new()),
                            session_id,
                            batch_id,
                            position,
                            event.kind,
                            1,
                            json.dumps(payload, sort_keys=True, ensure_ascii=False),
                            _evidence_basis(event.kind, payload, source="package"),
                            naive_utc(event.occurred_at),
                            str(event.event_id),
                            naive_utc(now),
                        ],
                    )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([ingestion_id]),
                after_summary=(
                    f"staged {len(staged_events)} event(s) from package "
                    f"{validated.package_id} into {session_id}"
                ),
            )
            migration_module.record_domain_event(
                transaction,
                event_type="session.package_ingested",
                aggregate_type="session",
                aggregate_id=session_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {
                        "package_hash": package_hash,
                        "external_session_id": validated.external_session_id,
                        "staged_events": len(staged_events),
                        "audio_available": audio_available,
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"package:{package_hash}",
            )
        return IngestReport(
            ingestion_id=ingestion_id,
            package_id=validated.package_id,
            package_hash=package_hash,
            session_id=session_id,
            track_id=track_id,
            external_session_id=validated.external_session_id,
            mode=validated.mode,
            staged_events=len(staged_events),
            skipped_events=skipped,
            batch_id=batch_id,
            audio_available=audio_available,
            retention_policy=retention,
            warnings=tuple(warnings),
        )


def _package_event_payload(event: Any, *, package: Any, retention: str) -> dict[str, Any]:
    """Turn one package event into the staged payload a close can materialize.

    The transcript layers are the reason this is not a copy. A package's event points at
    an utterance, and what the learner *said* lives in the layers: `raw` is what the
    machine heard, `reviewed-hearing` is what a person confirmed. The reviewed layer wins
    where it exists, and the payload records which layer it came from -- so an evidence
    claim can never rest on a hearing nobody checked without that being visible.
    """

    layers = {layer.kind: layer for layer in package.transcript_layers}
    utterance_id = getattr(event.payload, "utterance_id", None)
    text: str | None = None
    source_layer: str | None = None
    for kind in ("reviewed-hearing", "normalized", "raw"):
        layer = layers.get(kind)
        if layer is None:
            continue
        for utterance in layer.utterances:
            if utterance.utterance_id == utterance_id:
                text = utterance.text
                source_layer = kind
                break
        if text is not None:
            break
    # A producer's free-form `details` are merged *first* and cannot reach the fields
    # this function decides. Merging them afterwards let a package set `response` and
    # `response_visibility` itself, which is a retention rule a file gets to overrule --
    # exactly the escape hatch the staged-payload defect had on the other side.
    payload: dict[str, Any] = {
        key: value
        for key, value in (getattr(event.payload, "details", {}) or {}).items()
        if key not in PROTECTED_PACKAGE_FIELDS
    }
    payload.update(
        {
            "source": "package",
            "package_id": package.package_id,
            # The session an utterance ID is unique inside. Without it a close resolved
            # one conversation's events against another conversation's utterances.
            "external_session_id": package.external_session_id,
            "utterance_id": utterance_id,
            "transcript_layer": source_layer,
        }
    )
    if text is not None:
        # Handed to the retention rule rather than filtered by hand: the rule refuses to
        # exceed consent and reduces what it may keep, in one place.
        payload["response"] = text
        payload["response_visibility"] = None if retention == "withheld" else retention
    if event.kind == "pronunciation.assessment":
        payload["status"] = event.payload.status
        payload["audio_artifact_id"] = (
            None
            if event.payload.audio_artifact_id is None
            else str(event.payload.audio_artifact_id)
        )
        payload.setdefault("note", f"pronunciation {event.payload.status} in an external session")
    if event.kind == "follow_up":
        payload.setdefault("kind", "practice")
        payload["action"] = event.payload.summary
    return payload


# --- Recovery ----------------------------------------------------------------------


def recover(
    paths: WorkspacePaths,
    *,
    source: str,
    target: str | None = None,
    events: Sequence[str] = (),
    track: str | None = None,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    command: str = "session.recover",
    actor: str = "cli",
) -> RecoverReport:
    """Move staged events from a finished session into an open one, after review.

    This exists because abandoning a session keeps its observations, and sometimes the
    right answer is neither "credit all of it" nor "lose all of it". The events are
    copied into a new batch on the target session and the originals are marked
    `discarded` naming where they went, so the same observation cannot be credited
    twice and the audit trail says what happened.

    A keyed recovery is bound to the request *as asked* -- the source, the target or "the
    open session", and the selection -- never to what that selection resolved to, because
    a retry after the events moved resolves to nothing. What it resolved to is kept as a
    snapshot on the event, and a retry is answered from it before the source, the target,
    or the events are looked at again.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id, source_id = resolve_session_and_track(database, source, track)
        selected = tuple(events)
        request = idempotency.request_hash(
            operation="session.recover",
            source_session_id=source_id,
            target=target,
            selection="explicit" if selected else "all",
            events=sorted(selected),
        )
        recorded = idempotency.resolve(
            database,
            key=idempotency_key,
            event_type="session.staged_recovered",
            request_hash=request,
        )
        if recorded is not None:
            return _replayed_recovery(recorded, source_id=source_id)
        source_row = _session_row(database, source_id)
        if str(source_row[2]) not in session_policy.TERMINAL_STATUSES:
            raise LinguaWikiError(
                "session_still_open",
                f"session {source_id} is {source_row[2]}; close or abandon it before "
                "recovering its staged work",
                details=(ErrorDetail(field="status", reason=str(source_row[2])),),
            )
        target_id = resolve_session(database, target, track_id=track_id)
        if target_id == source_id:
            raise LinguaWikiError(
                "recovery_target_invalid",
                "staged work cannot be recovered into the session it came from",
                details=(ErrorDetail(field="target", reason="same session"),),
            )
        target_row = _session_row(database, target_id)
        _assert_loggable(session_id=target_id, status=str(target_row[2]))
        rows = database.query(
            "SELECT staged_event_id, kind, schema_version, payload_json, evidence_basis, "
            "occurred_at, source_event_id FROM session_staged_events "
            "WHERE session_id = ? AND status = 'staged' ORDER BY occurred_at, sequence",
            [source_id],
        )
        available = {str(row[0]): row for row in rows}
        if selected:
            unknown = [event for event in selected if event not in available]
            if unknown:
                raise LinguaWikiError(
                    "staged_event_not_recoverable",
                    "these staged events are not available on the source session: "
                    + ", ".join(unknown),
                    details=tuple(ErrorDetail(field="event", reason=event) for event in unknown),
                )
            chosen = [available[event] for event in selected]
        else:
            chosen = list(rows)
        if not chosen:
            return RecoverReport(
                source_session_id=source_id,
                target_session_id=target_id,
                recovered=0,
                warnings=("this session holds no staged events to recover",),
            )
        sequence = int(
            database.scalar(
                "SELECT coalesce(max(sequence), 0) + 1 FROM session_event_batches "
                "WHERE session_id = ?",
                [target_id],
            )
        )
        payloads = [_json_object(row[3]) for row in chosen]
        _assert_events_are_new(
            database,
            session_id=target_id,
            event_ids=[str(row[6]) for row in chosen],
        )
        batch_id = str(BatchId.new())
        now = aware_utc(database.now())
        moved: list[RecoveredEvent] = []
        warnings = (
            "the recovered events are staged on the target session and credited to "
            "nothing until it is closed",
        )
        with database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO session_event_batches (batch_id, session_id, sequence, "
                "idempotency_key, content_hash, event_count, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'skill', ?)",
                [
                    batch_id,
                    target_id,
                    sequence,
                    # Prefixed rather than the caller's key itself: batch keys share one
                    # namespace with every flush, and a recovery key must not collide
                    # with a skill's batch key.
                    f"recovery:{idempotency_key}"
                    if idempotency_key is not None
                    else f"recovery:{source_id}:{sequence}",
                    canonical_hash(payloads),
                    len(chosen),
                    naive_utc(now),
                ],
            )
            for position, (row, payload) in enumerate(zip(chosen, payloads, strict=True), start=1):
                new_id = str(StagedEventId.new())
                moved.append(
                    RecoveredEvent(source_staged_event_id=str(row[0]), staged_event_id=new_id)
                )
                transaction.execute(
                    "INSERT INTO session_staged_events (staged_event_id, session_id, batch_id, "
                    "sequence, kind, schema_version, payload_json, evidence_basis, status, "
                    "occurred_at, source_event_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?)",
                    [
                        new_id,
                        target_id,
                        batch_id,
                        position,
                        str(row[1]),
                        int(row[2]),
                        json.dumps(payload, sort_keys=True, ensure_ascii=False),
                        str(row[4]),
                        # The original event time: a recovered observation still happened
                        # when it happened, and the recovery is not a new observation.
                        naive_utc(aware_utc(row[5])),
                        # And it keeps its identity, so recovering it twice is refused.
                        str(row[6]),
                        naive_utc(now),
                    ],
                )
                transaction.execute(
                    "UPDATE session_staged_events SET status = 'discarded', discard_reason = ? "
                    "WHERE staged_event_id = ?",
                    [f"recovered into session {target_id}", str(row[0])],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                actor=actor,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([source_id, target_id, batch_id]),
                before_summary=f"{len(chosen)} staged event(s) on {source_id}",
                after_summary=f"recovered into {target_id} as batch {sequence}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="session.staged_recovered",
                aggregate_type="session",
                aggregate_id=target_id,
                correlation_id=EventId.new(),
                payload_json=idempotency.payload(
                    request,
                    source_session_id=source_id,
                    target_session_id=target_id,
                    batch_id=batch_id,
                    recovered=len(chosen),
                    skipped=len(rows) - len(chosen),
                    recovered_events=[event.model_dump(mode="json") for event in moved],
                    warnings=list(warnings),
                ),
                idempotency_key=idempotency_key,
            )
        return RecoverReport(
            source_session_id=source_id,
            target_session_id=target_id,
            batch_id=batch_id,
            recovered=len(chosen),
            skipped=len(rows) - len(chosen),
            recovered_events=tuple(moved),
            warnings=warnings,
        )


def _replayed_recovery(recorded: Mapping[str, Any], *, source_id: str) -> RecoverReport:
    """The report a keyed recovery returned, rebuilt from the snapshot its event kept."""

    try:
        return RecoverReport(
            source_session_id=str(recorded["source_session_id"]),
            target_session_id=str(recorded["target_session_id"]),
            batch_id=None if recorded.get("batch_id") is None else str(recorded["batch_id"]),
            recovered=int(recorded["recovered"]),
            skipped=int(recorded.get("skipped", 0)),
            recovered_events=tuple(
                RecoveredEvent.model_validate(entry) for entry in recorded["recovered_events"]
            ),
            replayed=True,
            warnings=(
                *(str(warning) for warning in recorded.get("warnings", ())),
                "this recovery already happened under this key; nothing was moved again",
            ),
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise LinguaWikiError(
            "idempotency_conflict",
            f"this key recovered staged work from session {source_id}, but what it moved "
            "can no longer be read back; use a new key",
            details=(ErrorDetail(field="idempotency_key", reason="recorded result is unreadable"),),
        ) from None


__all__ = [
    "BALANCE_WINDOW_DAYS",
    "MAXIMUM_BATCH_EVENTS",
    "BatchReport",
    "CloseReport",
    "IngestReport",
    "RecoverReport",
    "RecoveredEvent",
    "ResumePoint",
    "SessionBatchReport",
    "SessionBlockReport",
    "SessionListReport",
    "SessionReport",
    "SessionScreen",
    "StagedEventReport",
    "StagedListing",
    "StagingState",
    "abandon",
    "canonical_hash",
    "close",
    "create",
    "discover",
    "ingest_package",
    "is_recoverable",
    "log",
    "partial_close",
    "recover",
    "resolve_session",
    "resolve_session_and_track",
    "resume",
    "screen",
    "show",
    "staged",
    "staged_listing",
    "start",
    "weekly_deficits",
]
