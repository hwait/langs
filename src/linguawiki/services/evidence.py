"""Recording attempts and evidence, and recomputing what they imply.

The command surface here is deliberately narrow. A live session records its own attempts
through the session engine; what is left, and what this module owns, is the two cases
that happen outside one: importing an observation that was made elsewhere, and repairing
one that was recorded wrongly. Both are marked as such on the row, so a learner model
built from imports can be told from one built from observed work.

Four things are derived rather than accepted from the caller, because each is a place a
caller could otherwise talk their way into a promotion:

- *the claim*, which defaults to the weakest one the task type can support. An unstated
  claim is never the strongest one available;
- *novelty*, from whether this context has been seen before on this target. A caller
  cannot declare a fifth repetition novel;
- *the delay*, which must be a real one. `--retrieval delayed` is refused unless the gap
  is there, either declared in hours or visible in the target's own history;
- *the strength*, from the score, the help, the assessor, and their confidence, so a
  hinted AI-graded guess is worth less than a clean deterministic match and nobody has
  to remember to say so.

What is retained of the learner's own words follows consent, not convenience: without
transcript consent an attempt keeps a bounded excerpt, and a caller asking to keep the
full text is refused by name rather than quietly truncated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.evidence import (
    ASSESSOR_KINDS,
    ATTEMPT_ORIGINS,
    CLAIMS,
    COMMAND_ORIGINS,
    CONFIDENCE_LEVELS,
    CORRECTION_MODES,
    HELP_LEVELS,
    MODALITIES,
    OBSERVATION_CATEGORIES,
    RESPONSE_VISIBILITIES,
    RETRIEVAL_CLASSES,
    SALIENCES,
    STRENGTH_VERSION,
    TASK_TYPES,
    assert_compatible,
    assert_known,
    claims_for_task,
    observation_strength,
    outcome_for,
    polarity_for,
)
from linguawiki.ids import AttemptId, EventId, EvidenceId, ObservationId
from linguawiki.mastery import (
    AGGREGATION_VERSION,
    DEFAULT_POLICY,
    MasteryOutcome,
    MasteryPolicy,
    Observation,
    aggregate,
)
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import estimates as estimate_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service

#: The shortest gap that makes retrieval genuinely delayed rather than same-session.
DELAYED_MINIMUM_HOURS = 12.0
#: How much of a learner's response is kept when consent allows an excerpt but not the
#: whole text. Long enough to justify the evidence, short enough not to be a transcript.
EXCERPT_LIMIT = 240
#: Confidence below this much movement is decay, not news. Without it a recomputation a
#: second after the last one would report every item as changed.
CONFIDENCE_TOLERANCE = 0.005
#: What the standalone command accepts, which is narrower than what the schema allows:
#: `session` is written by the session engine through `write_attempt`, never by hand.
SUPPORTED_ORIGINS: tuple[str, ...] = COMMAND_ORIGINS


class EvidenceRecord(ContractModel):
    evidence_id: str
    claim: str
    polarity: str
    strength: float
    target_content_id: str | None = None
    dimension: str | None = None
    context_key: str
    novelty: str
    retrieval: str
    help_level: str
    modality: str
    task_type: str
    occurred_at: str


class AttemptReport(ContractModel):
    attempt_id: str
    track_id: str
    origin: str
    task_type: str
    modality: str
    outcome: str
    normalized_score: float
    help_level: str
    retrieval: str
    context_key: str
    target_content_id: str | None = None
    target_title: str | None = None
    dimension: str | None = None
    response_visibility: str
    response_excerpt: str | None = None
    assessor_kind: str
    confidence: str
    evidence: tuple[EvidenceRecord, ...] = ()
    #: What the attempt changed. An attempt that promoted nothing says so.
    stage_before: str | None = None
    stage_after: str | None = None
    stage_explanation: tuple[str, ...] = ()
    errors_reactivated: tuple[str, ...] = ()
    errors_supported: tuple[str, ...] = ()
    occurred_at: str
    recorded_at: str
    warnings: tuple[str, ...] = ()


class ItemStageChange(ContractModel):
    content_id: str
    stable_key: str
    title: str
    stage_before: str | None = None
    stage_after: str
    gated_stage: str
    evidence_ceiling: str
    confidence: float
    positive: int
    negative: int
    contexts: int
    regressed_claims: tuple[str, ...] = ()
    explanation: tuple[str, ...] = ()
    changed: bool = True


class RecomputeReport(ContractModel):
    track_id: str
    dry_run: bool
    aggregation_version: str
    items_considered: int
    items_changed: int
    changes: tuple[ItemStageChange, ...] = ()
    unchanged: tuple[str, ...] = ()
    estimates: estimate_service.RecomputeReport | None = None
    errors_reactivated: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class EvidenceListing(ContractModel):
    track_id: str
    limit: int
    total: int
    target_content_id: str | None = None
    dimension: str | None = None
    entries: tuple[EvidenceRecord, ...] = ()
    warnings: tuple[str, ...] = ()


class ObservationReport(ContractModel):
    observation_id: str
    track_id: str
    category: str
    salience: str
    note: str
    attempt_id: str | None = None
    observed_at: str
    warnings: tuple[str, ...] = ()


def _default_claims(task_type: str) -> tuple[str, ...]:
    """The weakest claim the task type can support.

    Unstated is not the same as strongest. A caller who means production says so; a
    caller who says nothing gets the claim that cannot overstate what happened.
    """

    supported = claims_for_task(task_type)
    if not supported:
        raise LinguaWikiError(
            "task_supports_no_claim",
            f"a {task_type} task supports no evidence claim, so it cannot produce evidence",
            details=(ErrorDetail(field="task_type", reason="no compatible claim"),),
        )
    return (supported[0],)


def _context_key(*, context: str | None, task_type: str, modality: str, family: str | None) -> str:
    """The unit diversity is counted in.

    Defaults to the shape of the demand plus where the material came from, because two
    successes on the same prompt are one observation and the aggregation has to be able
    to tell them apart from two successes in different settings.
    """

    if context is not None:
        if not context.strip():
            raise LinguaWikiError(
                "invalid_context_key",
                "a context key cannot be blank",
                details=(ErrorDetail(field="context", reason="blank context key"),),
            )
        return context
    return f"{task_type}:{family or modality}"


def _assert_delay(
    database: Database,
    *,
    track_id: str,
    retrieval: str,
    delay_hours: float | None,
    target_content_id: str | None,
    dimension: str | None,
    occurred_at: datetime,
) -> float | None:
    """Require a claimed delay to be a real one.

    Without this, a caller could label every attempt `delayed` and walk an item to
    `stable` in one sitting, because the delay gates the strongest evidence there is. The
    gap is accepted either as a declared number of hours or as one visible in the
    target's own history; neither being present is a refusal that names both remedies.
    """

    if retrieval != "delayed":
        return delay_hours
    if delay_hours is not None:
        if delay_hours + 1e-9 < DELAYED_MINIMUM_HOURS:
            raise LinguaWikiError(
                "retrieval_not_delayed",
                f"a delay of {delay_hours:g}h is not delayed retrieval; that needs at least "
                f"{DELAYED_MINIMUM_HOURS:g}h. Record it as --retrieval same-session.",
                details=(ErrorDetail(field="delay_hours", reason="gap too short"),),
            )
        return delay_hours
    scope = "target_content_id" if target_content_id is not None else "dimension"
    value = target_content_id if target_content_id is not None else dimension
    previous = database.scalar(
        f"SELECT max(occurred_at) FROM attempts WHERE track_id = ? AND {scope} = ?",
        [track_id, value],
    )
    if previous is None:
        raise LinguaWikiError(
            "retrieval_not_delayed",
            "delayed retrieval needs an earlier encounter to be delayed from, and this is "
            f"the first attempt on {value}. Pass --delay-hours if the gap is known, or "
            "record it as --retrieval immediate.",
            details=(ErrorDetail(field="retrieval", reason="no earlier encounter"),),
        )
    gap = float((naive_utc(occurred_at) - previous) / timedelta(hours=1))
    if gap + 1e-9 < DELAYED_MINIMUM_HOURS:
        raise LinguaWikiError(
            "retrieval_not_delayed",
            f"the last encounter with {value} was {gap:.1f}h ago, which is not delayed "
            f"retrieval ({DELAYED_MINIMUM_HOURS:g}h). Pass --delay-hours if the real gap is "
            "longer, or record it as --retrieval same-session.",
            details=(ErrorDetail(field="retrieval", reason="gap too short"),),
        )
    return round(gap, 3)


def _novelty(
    database: Database,
    *,
    track_id: str,
    context_key: str,
    target_content_id: str | None,
    dimension: str | None,
) -> str:
    """Whether this context is new for this target, read rather than asserted."""

    scope = "target_content_id" if target_content_id is not None else "dimension"
    value = target_content_id if target_content_id is not None else dimension
    seen = database.scalar(
        f"SELECT count(*) FROM evidence WHERE track_id = ? AND {scope} = ? AND context_key = ?",
        [track_id, value, context_key],
    )
    return "repeat" if int(seen) else "novel"


def retain_response(
    response: str | None,
    *,
    requested: str | None,
    preferences: Mapping[str, object],
) -> tuple[str, str | None, str | None]:
    """Decide what of the learner's own words is kept, and refuse to exceed consent.

    Consent is a track preference, and the absence of a decision is not a yes: without
    an explicit yes the attempt keeps a bounded excerpt, which is what justifies the
    evidence, and an explicit no keeps only a hash. A caller asking for the full text
    without consent is refused rather than silently truncated, because a silent
    downgrade would let them believe the transcript is there.
    """

    if response is None:
        if requested in ("excerpt", "full"):
            raise LinguaWikiError(
                "response_required",
                f"--response-visibility {requested} needs a response to retain",
                details=(ErrorDetail(field="response", reason="no response supplied"),),
            )
        return ("withheld", None, None)
    digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
    consent = preferences.get("transcript_retention_consent")
    if requested is not None:
        assert_known(
            requested,
            vocabulary=RESPONSE_VISIBILITIES,
            field="response_visibility",
            code="unknown_response_visibility",
        )
    if requested == "withheld":
        return ("withheld", None, digest)
    if requested == "full":
        if consent is not True:
            raise LinguaWikiError(
                "transcript_consent_required",
                "keeping a learner's full response needs transcript retention consent on "
                "the track; set it with 'linguawiki track update --transcript-consent', or "
                "record the attempt with an excerpt",
                details=(ErrorDetail(field="response_visibility", reason="consent absent"),),
            )
        return ("full", response, digest)
    if consent is False:
        return ("withheld", None, digest)
    return ("excerpt", response[:EXCERPT_LIMIT], digest)


@dataclass(frozen=True, slots=True)
class ServedTask:
    """What the learner actually faced, and where that account came from."""

    content_id: str
    run_id: str | None
    task_type: str
    modality: str
    dimension: str
    difficulty: float
    content_family: str
    #: The items the task targeted when it was served. `None` means unknown -- served
    #: before migration 0022, or read from the bank where the answer is not knowable.
    #: Neither unknown nor empty is permission to attribute an item observation to it.
    targets: tuple[str, ...] | None
    #: `run-snapshot` when the run recorded what it served, `bank` when there is no run
    #: and the pack's current record is the only account there is.
    source: str
    #: Whether the bank item has changed since it was served. The snapshot still governs;
    #: this is what stops an un-snapshotted fact being asserted against drifted data.
    drifted: bool = False


#: The served record's columns, in the order `_served_snapshot` selects them.
SNAPSHOT_FACTS = (
    "content_id",
    "dimension",
    "difficulty",
    "content_family",
    "task_type",
    "modality",
    "content_hash",
    "target_refs_json",
)
#: The columns migration 0016 added, which is what makes a record legacy or damaged. All
#: null means the task was served before the snapshot existed; none null means it was
#: served after. `dimension` is excluded: it predates 0016 and is NOT NULL.
SNAPSHOT_COLUMNS = (2, 3, 4, 5, 6)


def _resolve_task(
    database: Database, *, task: str | None, run: str | None, track_id: str
) -> ServedTask | None:
    """Read what the learner faced, from the run that served it wherever there is one.

    A pack is mutable and a run is not, which is why `assessment next` snapshots the
    served facts into `assessment_run_tasks`. Reading the bank instead let a later pack
    edit rewrite history: a `short-response`/`text` task the learner answered became an
    `extended-productive`/`writing` one, and the default claim rose from recognition to
    controlled production for an observation nobody made.

    A run also belongs to one track. Checking only that the run served the task let one
    learner's run attach to another learner's evidence, on the same pack -- so the run's
    own track has to match the track the observation is being recorded on.
    """

    if task is None:
        return None
    if run is not None:
        return _served_snapshot(database, task=task, run=run, track_id=track_id)
    return _bank_task(database, task=task, track_id=track_id)


def _bank_task(database: Database, *, task: str, track_id: str) -> ServedTask:
    """The pack's current record of a task, for an observation outside any run."""

    row = database.one(
        "SELECT bank.content_id, bank.dimension, bank.difficulty, bank.content_family, "
        "bank.task_type, bank.modality, bank.target_refs_json "
        "FROM assessment_tasks bank "
        "JOIN assessment_definitions form ON form.definition_id = bank.definition_id "
        "JOIN learning_tracks track ON track.pack_id = form.pack_id "
        "WHERE bank.content_id = ? AND track.track_id = ?",
        [task, track_id],
    )
    if row is None:
        unscoped = database.scalar(
            "SELECT content_id FROM assessment_tasks WHERE content_id = ?", [task]
        )
        if unscoped is not None:
            raise LinguaWikiError(
                "assessment_task_out_of_scope",
                f"{task} belongs to a pack this track is not taught from",
                details=(ErrorDetail(field="task", reason="another pack's bank"),),
            )
        raise LinguaWikiError(
            "assessment_task_not_found",
            f"no assessment task with ID {task}",
            details=(ErrorDetail(field="task", reason="unknown task"),),
        )
    return ServedTask(
        content_id=str(row[0]),
        run_id=None,
        dimension=str(row[1]),
        difficulty=float(row[2]),
        content_family=str(row[3]),
        task_type=str(row[4]),
        modality=str(row[5]),
        targets=tuple(str(target) for target in json.loads(str(row[6]))),
        source="bank",
    )


def _served_snapshot(database: Database, *, task: str, run: str, track_id: str) -> ServedTask:
    """What the run recorded serving, refusing another track's run outright."""

    run_row = database.one("SELECT track_id FROM assessment_runs WHERE run_id = ?", [run])
    if run_row is None:
        raise LinguaWikiError(
            "assessment_run_not_found",
            f"no assessment run with ID {run}",
            details=(ErrorDetail(field="assessment_run", reason="unknown run"),),
        )
    if str(run_row[0]) != track_id:
        raise LinguaWikiError(
            "assessment_run_not_on_track",
            f"run {run} belongs to another track, so it cannot have produced an "
            "observation of this learner",
            details=(
                ErrorDetail(
                    field="assessment_run",
                    reason="the run's track is not this track",
                    context={"run_track": str(run_row[0])},
                ),
            ),
        )
    served = database.one(
        "SELECT served.content_id, served.dimension, served.difficulty, "
        "served.content_family, served.task_type, served.modality, served.content_hash, "
        "served.target_refs_json FROM assessment_run_tasks served "
        "WHERE served.run_id = ? AND served.content_id = ?",
        [run, task],
    )
    if served is None:
        raise LinguaWikiError(
            "assessment_task_not_served",
            f"run {run} never served {task}, so it cannot have produced this observation; "
            "record it without --assessment-run, or name the run that served it",
            details=(ErrorDetail(field="task", reason="task not served by this run"),),
        )
    bank = database.one(
        "SELECT record.content_hash, bank.target_refs_json, bank.task_type, bank.modality, "
        "bank.dimension, bank.difficulty, bank.content_family FROM assessment_tasks bank "
        "JOIN content_records record ON record.content_id = bank.content_id "
        "WHERE bank.content_id = ?",
        [task],
    )
    # Migration 0016 added every fact below at once, so a row served before it has all
    # of them null and a row served after it has none of them null. Anything in between
    # is damage, not history -- and treating a partial null as legacy let the bank be
    # consulted for facts the snapshot actually held, which is the whole defect again in
    # a smaller disguise.
    captured = [index for index in SNAPSHOT_COLUMNS if served[index] is not None]
    if not captured:
        recovered = _bank_task(database, task=task, track_id=track_id)
        return ServedTask(
            content_id=recovered.content_id,
            run_id=run,
            dimension=recovered.dimension,
            difficulty=recovered.difficulty,
            content_family=recovered.content_family,
            task_type=recovered.task_type,
            modality=recovered.modality,
            # The bank's current targets say nothing about what this task tested when it
            # was served, so no item observation may be attributed to it.
            targets=None,
            source="bank",
        )
    if len(captured) != len(SNAPSHOT_COLUMNS):
        missing = ", ".join(
            SNAPSHOT_FACTS[index] for index in SNAPSHOT_COLUMNS if served[index] is None
        )
        raise LinguaWikiError(
            "assessment_snapshot_incomplete",
            f"run {run} holds a partial record of serving {task}: {missing} is missing "
            "while the rest is present. A task served before the snapshot existed has "
            "none of it; this record has been damaged, so what the learner faced cannot "
            "be established. Record the observation without --assessment-run.",
            details=(
                ErrorDetail(
                    field="assessment_run",
                    reason="the served record is partially populated",
                    context={"missing": missing},
                ),
            ),
        )
    drifted = bank is not None and _has_drifted(served=served, bank=bank)
    return ServedTask(
        content_id=str(served[0]),
        run_id=run,
        dimension=str(served[1]),
        difficulty=float(served[2]),
        content_family=str(served[3]),
        task_type=str(served[4]),
        modality=str(served[5]),
        # Snapshotted since migration 0022. Null means a task served before that, whose
        # targets were never captured -- unknown, which is not the same as none and is
        # not permission either.
        targets=(
            None
            if served[7] is None
            else tuple(str(target) for target in json.loads(str(served[7])))
        ),
        source="run-snapshot",
        drifted=drifted,
    )


def _has_drifted(*, served: Sequence[Any], bank: Sequence[Any]) -> bool:
    """Whether the bank item differs from what the run recorded serving.

    Compared fact by fact as well as by content hash. The hash alone was not enough: it
    covers what the *pack* declared about the item, so a change reaching the bank row by
    another route leaves the hash agreeing while the facts disagree -- and the answer here
    is what decides whether the un-snapshotted facts can be trusted at all.
    """

    if served[6] is not None and str(bank[0]) != str(served[6]):
        return True
    if (str(served[4]), str(served[5])) != (str(bank[2]), str(bank[3])):
        return True
    if (str(served[1]), str(served[3])) != (str(bank[4]), str(bank[6])):
        return True
    return abs(float(served[2]) - float(bank[5])) > 1e-9


def _assert_task_facts(
    served: ServedTask,
    *,
    task_type: str | None,
    modality: str | None,
    dimension: str | None,
    difficulty: float | None = None,
    target_content_id: str | None,
) -> None:
    """Refuse a caller's account of a bank task that the bank contradicts.

    Overriding silently would be worse than refusing: the caller would believe the
    observation it described was recorded. Each mismatch names the stored value, so the
    remedy is to drop the flag rather than to guess again.
    """

    problems = [
        ErrorDetail(
            field=field,
            reason=f"the bank records {stored}, not {claimed}",
            context={"task": served.content_id, "stored": stored, "claimed": str(claimed)},
        )
        for field, claimed, stored in (
            ("task_type", task_type, served.task_type),
            ("modality", modality, served.modality),
            ("dimension", dimension, served.dimension),
        )
        if claimed is not None and claimed != stored
    ]
    # Difficulty is where the observation lands on the ability grid, so a caller's number
    # moves the learner's estimate to a level the task never tested.
    if difficulty is not None and abs(difficulty - served.difficulty) > 1e-9:
        problems.append(
            ErrorDetail(
                field="difficulty",
                reason=f"the record says {served.difficulty:g}, not {difficulty:g}",
                context={"task": served.content_id, "stored": f"{served.difficulty:g}"},
            )
        )
    if problems:
        raise LinguaWikiError(
            "assessment_task_facts_conflict",
            f"{served.content_id} is a {served.task_type} task in {served.modality} for "
            f"{served.dimension}; the bank decides that, so omit the flags that disagree",
            details=tuple(problems),
        )
    if target_content_id is None:
        return
    # Absence of information is not permission. A task whose targets are unknown -- or
    # which names none -- makes no claim about which item it tests, so an item-targeted
    # observation attributed to it is a claim nobody made. It measured a dimension; that
    # is what can be recorded from it.
    if not served.targets:
        raise LinguaWikiError(
            "assessment_task_target_unknown",
            f"{served.content_id} "
            + (
                "was served before its targets were recorded, so which item it tested is "
                "not knowable"
                if served.targets is None
                else "names no target items, so it tests a dimension rather than an item"
            )
            + f". Record the dimension observation without --target, or record {target_content_id}"
            " separately with --origin import and no --task.",
            details=(
                ErrorDetail(
                    field="target",
                    reason="the task's targets are unknown or empty",
                    context={"task": served.content_id, "source": served.source},
                ),
            ),
        )
    if target_content_id not in served.targets:
        raise LinguaWikiError(
            "assessment_task_target_mismatch",
            f"{served.content_id} targets {list(served.targets)}, not {target_content_id}; "
            "an observation from a bank task is about what that task tests",
            details=(
                ErrorDetail(
                    field="target",
                    reason="not a target of this task",
                    context={"targets": list(served.targets)},
                ),
            ),
        )


def _item_difficulty(database: Database, *, content_id: str, levels: Sequence[str]) -> float | None:
    """An item's difficulty on the ability grid, from the level its pack assigned it.

    The framework's own ordering is the scale, so no level label is ever translated
    between frameworks and an item with no level simply has no difficulty.
    """

    level = database.scalar(
        "SELECT level_min FROM knowledge_items WHERE content_id = ?", [content_id]
    )
    if level is None or str(level) not in levels:
        return None
    return float(levels.index(str(level)))


@dataclass(frozen=True, slots=True)
class AttemptPlan:
    """One attempt, fully resolved and not yet written anywhere.

    Splitting the decision from the write is what lets a session close materialize a
    whole batch of observations inside one transaction: DuckDB serves one connection per
    database and forbids nested transactions, so a close cannot call a command that
    opens its own. Every rule about what an observation may claim still runs in one
    place -- the part that reads the database -- and the writer only inserts what that
    decided.
    """

    track: learner_service.TrackRecord
    track_id: str
    origin: str
    command: str
    normalized_score: float
    outcome: str
    polarity: str
    task_type: str
    modality: str
    dimension: str | None
    target_content_id: str | None
    target_title: str | None
    task_content_id: str | None
    assessment_run_id: str | None
    claims: tuple[str, ...]
    help_level: str
    correction_mode: str
    retrieval: str
    delay_hours: float | None
    latency_ms: int | None
    difficulty: float | None
    context_key: str
    novelty: str
    strength: float
    response_visibility: str
    response_excerpt: str | None
    response_hash: str | None
    assessor_kind: str
    assessor: str | None
    confidence: str
    idempotency_key: str | None
    occurred_at: datetime
    stage_before: str | None
    warnings: tuple[str, ...]


def plan_attempt(
    database: Database,
    *,
    score: float,
    #: Omitted when `task` names a bank item, which is authoritative about both.
    task_type: str | None = None,
    modality: str | None = None,
    target: str | None = None,
    dimension: str | None = None,
    claims: Sequence[str] | None = None,
    help_level: str = "none",
    correction_mode: str = "none",
    retrieval: str = "immediate",
    delay_hours: float | None = None,
    latency_ms: int | None = None,
    difficulty: float | None = None,
    context: str | None = None,
    response: str | None = None,
    response_visibility: str | None = None,
    #: The hash of a response that was already reduced under the retention rule before
    #: it reached this call -- a session's staged event applies that rule when it is
    #: flushed, so by close time the full text is gone and only its digest can prove
    #: there was one.
    response_hash: str | None = None,
    assessor_kind: str = "deterministic",
    assessor: str | None = None,
    confidence: str = "medium",
    origin: str = "import",
    assessment_run: str | None = None,
    task: str | None = None,
    observed_at: datetime | None = None,
    idempotency_key: str | None = None,
    track: str | None = None,
    command: str = "evidence.record",
) -> AttemptPlan:
    """Resolve one attempt against the database without writing anything.

    Returns what the observation *is*: which task was served, which claims it supports,
    how strong it is, and what stage the item sat at beforehand. Nothing here mutates,
    so a caller may plan several attempts and write them together afterwards.
    """

    if task_type is not None:
        assert_known(task_type, vocabulary=TASK_TYPES, field="task_type", code="unknown_task_type")
    if modality is not None:
        assert_known(modality, vocabulary=MODALITIES, field="modality", code="unknown_modality")
    assert_known(help_level, vocabulary=HELP_LEVELS, field="help_level", code="unknown_help_level")
    assert_known(
        correction_mode,
        vocabulary=CORRECTION_MODES,
        field="correction_mode",
        code="unknown_correction_mode",
    )
    assert_known(
        retrieval,
        vocabulary=RETRIEVAL_CLASSES,
        field="retrieval",
        code="unknown_retrieval_class",
    )
    assert_known(
        assessor_kind,
        vocabulary=ASSESSOR_KINDS,
        field="assessor_kind",
        code="unknown_assessor_kind",
    )
    assert_known(
        confidence, vocabulary=CONFIDENCE_LEVELS, field="confidence", code="unknown_confidence"
    )
    assert_known(origin, vocabulary=ATTEMPT_ORIGINS, field="origin", code="unknown_attempt_origin")
    if not 0.0 <= score <= 1.0:
        raise LinguaWikiError(
            "invalid_score",
            "a normalized score is between 0.0 and 1.0",
            details=(ErrorDetail(field="score", reason=str(score)),),
        )
    requested_claims = tuple(claims) if claims else None
    if requested_claims is not None:
        for claim in requested_claims:
            assert_known(claim, vocabulary=CLAIMS, field="claim", code="unknown_evidence_claim")
    track_id = learner_service.resolve_track(database, track)
    record_track = learner_service.track_context(database, track_id)
    target_id = (
        None
        if target is None
        else knowledge_service.resolve_item(database, target, track_id=track_id)
    )
    warnings_from_task: list[str] = []
    served = _resolve_task(database, task=task, run=assessment_run, track_id=track_id)
    if assessment_run is not None and served is None:
        raise LinguaWikiError(
            "assessment_task_required",
            "an attempt attributed to an assessment run must name the bank task it "
            "answered; pass --task",
            details=(ErrorDetail(field="task", reason="run without a task"),),
        )
    if served is not None:
        _assert_task_facts(
            served,
            task_type=task_type,
            modality=modality,
            dimension=dimension,
            difficulty=difficulty,
            target_content_id=target_id,
        )
        if served.source == "run-snapshot" and served.drifted:
            warnings_from_task.append(
                f"the bank item behind {served.content_id} has changed since the run "
                "served it; this observation records what the learner faced, not what "
                "the pack now says"
            )
        if served.run_id is not None and served.source == "bank":
            warnings_from_task.append(
                f"run {served.run_id} did not record what it served for "
                f"{served.content_id}, so the pack's current record was used; a task "
                "served by this release is snapshotted"
            )
    # The bank's facts win outright. They are not defaults the caller may adjust.
    task_id = None if served is None else served.content_id
    run_id = assessment_run
    family = None if served is None else served.content_family
    task_difficulty = None if served is None else served.difficulty
    resolved_task_type = task_type if served is None else served.task_type
    resolved_modality = modality if served is None else served.modality
    resolved_dimension = (dimension if served is None else served.dimension) or dimension
    if resolved_task_type is None or resolved_modality is None:
        raise LinguaWikiError(
            "task_shape_required",
            "an observation outside the assessment bank has to say what was demanded: "
            "pass --task-type and --modality, or name a bank task with --task",
            details=(ErrorDetail(field="task_type", reason="no task type and no bank task"),),
        )
    # Checked after the bank task is resolved, because a bank task names its own
    # dimension: an observation from one measures something even when the caller
    # said nothing about what.
    if target_id is None and resolved_dimension is None:
        raise LinguaWikiError(
            "evidence_target_required",
            "an attempt measures a knowledge item, a skill dimension, or both; name one",
            details=(ErrorDetail(field="target", reason="neither an item nor a dimension"),),
        )
    moment = aware_utc(observed_at) if observed_at is not None else aware_utc(database.now())
    context_key = _context_key(
        context=context,
        task_type=resolved_task_type,
        modality=resolved_modality,
        family=family,
    )
    resolved_delay = _assert_delay(
        database,
        track_id=track_id,
        retrieval=retrieval,
        delay_hours=delay_hours,
        target_content_id=target_id,
        dimension=resolved_dimension,
        occurred_at=moment,
    )
    novelty = _novelty(
        database,
        track_id=track_id,
        context_key=context_key,
        target_content_id=target_id,
        dimension=resolved_dimension,
    )
    outcome = outcome_for(score)
    polarity = polarity_for(outcome)
    effective_claims = requested_claims or _default_claims(resolved_task_type)
    for claim in effective_claims:
        assert_compatible(
            claim,
            modality=resolved_modality,
            task_type=resolved_task_type,
            help_level=help_level,
            retrieval=retrieval,
            polarity=polarity,
            dimension=resolved_dimension,
            target_content_id=target_id,
        )
    strength = observation_strength(
        normalized_score=score,
        polarity=polarity,
        help_level=help_level,
        assessor_kind=assessor_kind,
        confidence=confidence,
    )
    visibility, excerpt, digest = retain_response(
        response, requested=response_visibility, preferences=record_track.preferences
    )
    # A withheld response keeps the digest of what it was, and that digest was computed
    # where the text still existed. Recomputing it here would hash the excerpt instead.
    digest = digest or response_hash
    # The served record wins outright where there is one: a caller's difficulty is
    # already refused if it disagrees, so preferring it here could only ever restate
    # the same number -- or reintroduce the override if that check were weakened.
    resolved_difficulty = task_difficulty if task_difficulty is not None else difficulty
    if resolved_difficulty is None and target_id is not None:
        resolved_difficulty = _item_difficulty(
            database, content_id=target_id, levels=record_track.framework_levels
        )
    stage_before = (
        None
        if target_id is None
        else knowledge_service.item_state(database, track_id=track_id, content_id=target_id)
    )
    warnings: list[str] = list(warnings_from_task)
    if requested_claims is None:
        warnings.append(
            f"no claim was named, so the weakest one a {resolved_task_type} task supports was "
            f"recorded: {effective_claims[0]}"
        )
    if resolved_difficulty is None and resolved_dimension is not None:
        warnings.append(
            f"{resolved_dimension} has no difficulty for this attempt, so the evidence "
            "moves the item but not the dimension estimate"
        )
    return AttemptPlan(
        track=record_track,
        track_id=track_id,
        origin=origin,
        command=command,
        normalized_score=score,
        outcome=outcome,
        polarity=polarity,
        task_type=resolved_task_type,
        modality=resolved_modality,
        dimension=resolved_dimension,
        target_content_id=target_id,
        target_title=(
            None
            if target_id is None
            else str(
                database.scalar(
                    "SELECT title FROM knowledge_items WHERE content_id = ?", [target_id]
                )
            )
        ),
        task_content_id=task_id,
        assessment_run_id=run_id,
        claims=tuple(effective_claims),
        help_level=help_level,
        correction_mode=correction_mode,
        retrieval=retrieval,
        delay_hours=resolved_delay,
        latency_ms=latency_ms,
        difficulty=resolved_difficulty,
        context_key=context_key,
        novelty=novelty,
        strength=strength,
        response_visibility=visibility,
        response_excerpt=excerpt,
        response_hash=digest,
        assessor_kind=assessor_kind,
        assessor=assessor,
        confidence=confidence,
        idempotency_key=idempotency_key,
        occurred_at=moment,
        stage_before=None if stage_before is None else stage_before.stage,
        warnings=tuple(warnings),
    )


def write_attempt(transaction: Database, plan: AttemptPlan) -> AttemptReport:
    """Write one planned attempt, its evidence, and everything they settle.

    The transaction belongs to the caller. `evidence record` opens one for a single
    attempt; a session close opens one for the whole session, which is what makes "a
    completed session updates derived learner state exactly once" a property of the
    schema rather than a hope about the order of calls.
    """

    track_id = plan.track_id
    record_track = plan.track
    target_id = plan.target_content_id
    task_id = plan.task_content_id
    run_id = plan.assessment_run_id
    resolved_dimension = plan.dimension
    resolved_task_type = plan.task_type
    resolved_modality = plan.modality
    resolved_delay = plan.delay_hours
    resolved_difficulty = plan.difficulty
    effective_claims = plan.claims
    context_key = plan.context_key
    novelty = plan.novelty
    strength = plan.strength
    visibility = plan.response_visibility
    excerpt = plan.response_excerpt
    digest = plan.response_hash
    outcome = plan.outcome
    polarity = plan.polarity
    score = plan.normalized_score
    help_level = plan.help_level
    correction_mode = plan.correction_mode
    retrieval = plan.retrieval
    latency_ms = plan.latency_ms
    assessor_kind = plan.assessor_kind
    assessor = plan.assessor
    confidence = plan.confidence
    origin = plan.origin
    command = plan.command
    idempotency_key = plan.idempotency_key
    moment = plan.occurred_at
    stage_before = plan.stage_before
    attempt_id = str(AttemptId.new())
    now = transaction.now()
    transaction.execute(
        "INSERT INTO attempts (attempt_id, track_id, origin, assessment_run_id, "
        "task_content_id, target_content_id, dimension, task_type, modality, outcome, "
        "normalized_score, help_level, correction_mode, retrieval, delay_hours, "
        "latency_ms, source_difficulty, context_key, response_visibility, "
        "response_excerpt, response_hash, assessor_kind, assessor, confidence, "
        "strength_version, idempotency_key, occurred_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)",
        [
            attempt_id,
            track_id,
            origin,
            run_id,
            task_id,
            target_id,
            resolved_dimension,
            resolved_task_type,
            resolved_modality,
            outcome,
            score,
            help_level,
            correction_mode,
            retrieval,
            resolved_delay,
            latency_ms,
            resolved_difficulty,
            context_key,
            visibility,
            excerpt,
            digest,
            assessor_kind,
            assessor,
            confidence,
            STRENGTH_VERSION,
            idempotency_key,
            naive_utc(moment),
            now,
        ],
    )
    written: list[EvidenceRecord] = []
    for claim in effective_claims:
        evidence_id = str(EvidenceId.new())
        transaction.execute(
            "INSERT INTO evidence (evidence_id, track_id, attempt_id, claim, polarity, "
            "target_content_id, dimension, strength, context_key, novelty, retrieval, "
            "delay_hours, help_level, modality, task_type, assessor_kind, confidence, "
            "strength_version, occurred_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                evidence_id,
                track_id,
                attempt_id,
                claim,
                polarity,
                target_id,
                resolved_dimension,
                strength,
                context_key,
                novelty,
                retrieval,
                resolved_delay,
                help_level,
                resolved_modality,
                resolved_task_type,
                assessor_kind,
                confidence,
                STRENGTH_VERSION,
                naive_utc(moment),
                now,
            ],
        )
        written.append(
            EvidenceRecord(
                evidence_id=evidence_id,
                claim=claim,
                polarity=polarity,
                strength=strength,
                target_content_id=target_id,
                dimension=resolved_dimension,
                context_key=context_key,
                novelty=novelty,
                retrieval=retrieval,
                help_level=help_level,
                modality=resolved_modality,
                task_type=resolved_task_type,
                occurred_at=moment.isoformat(),
            )
        )
    from linguawiki.services import errors as error_service

    supported, reactivated = error_service.settle_from_evidence(
        transaction,
        track_id=track_id,
        target_content_id=target_id,
        evidence=tuple(written),
    )
    change = (
        None
        if target_id is None
        else recompute_item(transaction, track_id=track_id, content_id=target_id, now=moment)
    )
    if resolved_dimension is not None:
        estimate_service.recompute_dimension(
            transaction,
            track_id=track_id,
            framework_id=record_track.proficiency_framework,
            dimension=resolved_dimension,
            levels=record_track.framework_levels,
            now=moment,
        )
    migration_module.record_audit_entry(
        transaction,
        command=command,
        correlation_id=EventId.new(),
        outcome="succeeded",
        affected_records_json=json.dumps([attempt_id]),
        before_summary=None if stage_before is None else f"stage {stage_before}",
        after_summary=(
            f"{outcome} {resolved_task_type} attempt with {len(written)} evidence row(s)"
            + (f"; stage {change.stage_after}" if change is not None else "")
        ),
    )
    migration_module.record_domain_event(
        transaction,
        event_type="evidence.recorded",
        aggregate_type="attempt",
        aggregate_id=attempt_id,
        correlation_id=EventId.new(),
        payload_json=json.dumps(
            {
                "claims": list(effective_claims),
                "polarity": polarity,
                "target_content_id": target_id,
                "dimension": resolved_dimension,
            },
            sort_keys=True,
        ),
        idempotency_key=idempotency_key,
    )
    return AttemptReport(
        attempt_id=attempt_id,
        track_id=track_id,
        origin=origin,
        task_type=resolved_task_type,
        modality=resolved_modality,
        outcome=outcome,
        normalized_score=score,
        help_level=help_level,
        retrieval=retrieval,
        context_key=context_key,
        target_content_id=target_id,
        target_title=plan.target_title,
        dimension=resolved_dimension,
        response_visibility=visibility,
        response_excerpt=excerpt,
        assessor_kind=assessor_kind,
        confidence=confidence,
        evidence=tuple(written),
        stage_before=stage_before,
        stage_after=None if change is None else change.stage_after,
        stage_explanation=() if change is None else change.explanation,
        errors_reactivated=reactivated,
        errors_supported=supported,
        occurred_at=moment.isoformat(),
        recorded_at=aware_utc(now).isoformat(),
        warnings=plan.warnings,
    )


def record(
    paths: WorkspacePaths,
    *,
    score: float,
    #: Omitted when `task` names a bank item, which is authoritative about both.
    task_type: str | None = None,
    modality: str | None = None,
    target: str | None = None,
    dimension: str | None = None,
    claims: Sequence[str] | None = None,
    help_level: str = "none",
    correction_mode: str = "none",
    retrieval: str = "immediate",
    delay_hours: float | None = None,
    latency_ms: int | None = None,
    difficulty: float | None = None,
    context: str | None = None,
    response: str | None = None,
    response_visibility: str | None = None,
    assessor_kind: str = "deterministic",
    assessor: str | None = None,
    confidence: str = "medium",
    origin: str = "import",
    assessment_run: str | None = None,
    task: str | None = None,
    observed_at: datetime | None = None,
    idempotency_key: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "evidence.record",
) -> AttemptReport:
    """Record one attempt and the evidence it justifies, then settle what it changed."""

    # Narrower than the schema on purpose: see `SUPPORTED_ORIGINS`.
    assert_known(
        origin, vocabulary=SUPPORTED_ORIGINS, field="origin", code="unknown_attempt_origin"
    )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        plan = plan_attempt(
            database,
            score=score,
            task_type=task_type,
            modality=modality,
            target=target,
            dimension=dimension,
            claims=claims,
            help_level=help_level,
            correction_mode=correction_mode,
            retrieval=retrieval,
            delay_hours=delay_hours,
            latency_ms=latency_ms,
            difficulty=difficulty,
            context=context,
            response=response,
            response_visibility=response_visibility,
            assessor_kind=assessor_kind,
            assessor=assessor,
            confidence=confidence,
            origin=origin,
            assessment_run=assessment_run,
            task=task,
            observed_at=observed_at,
            idempotency_key=idempotency_key,
            track=track,
            command=command,
        )
        with database.transaction() as transaction:
            return write_attempt(transaction, plan)


def observations(
    paths: WorkspacePaths,
    *,
    category: str,
    note: str,
    salience: str = "medium",
    attempt: str | None = None,
    observed_at: datetime | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "evidence.observe",
) -> ObservationReport:
    """Record one qualitative observation: fatigue, a strategy, or a teacher's note.

    An observation is context for planning, never evidence. It cannot promote an item and
    is deliberately unable to: it names no claim and carries no strength.
    """

    assert_known(
        category,
        vocabulary=OBSERVATION_CATEGORIES,
        field="category",
        code="unknown_observation_category",
    )
    assert_known(salience, vocabulary=SALIENCES, field="salience", code="unknown_salience")
    if not note.strip():
        raise LinguaWikiError(
            "invalid_observation",
            "an observation needs a note",
            details=(ErrorDetail(field="note", reason="blank note"),),
        )
    active_clock = clock or SystemClock()
    with (
        open_writer(paths, command=command, clock=active_clock) as database,
        database.transaction() as transaction,
    ):
        return write_observation(
            transaction,
            category=category,
            note=note,
            salience=salience,
            attempt=attempt,
            observed_at=observed_at,
            track=track,
            command=command,
        )


def write_observation(
    transaction: Database,
    *,
    category: str,
    note: str,
    salience: str = "medium",
    attempt: str | None = None,
    observed_at: datetime | None = None,
    track: str | None = None,
    command: str = "evidence.observe",
) -> ObservationReport:
    """Record one observation inside a caller's transaction.

    A session close writes its notes here, alongside the attempts and corrections from
    the same session, so a rolled-back close leaves no orphaned commentary about work
    that was never credited.
    """

    assert_known(
        category,
        vocabulary=OBSERVATION_CATEGORIES,
        field="category",
        code="unknown_observation_category",
    )
    assert_known(salience, vocabulary=SALIENCES, field="salience", code="unknown_salience")
    if not note.strip():
        raise LinguaWikiError(
            "invalid_observation",
            "an observation needs a note",
            details=(ErrorDetail(field="note", reason="blank note"),),
        )
    track_id = learner_service.resolve_track(transaction, track)
    if attempt is not None and not transaction.scalar(
        "SELECT count(*) FROM attempts WHERE attempt_id = ? AND track_id = ?",
        [attempt, track_id],
    ):
        raise LinguaWikiError(
            "attempt_not_found",
            f"no attempt {attempt} on this track",
            details=(ErrorDetail(field="attempt", reason="unknown attempt"),),
        )
    observation_id = str(ObservationId.new())
    moment = aware_utc(observed_at) if observed_at is not None else aware_utc(transaction.now())
    now = transaction.now()
    transaction.execute(
        "INSERT INTO session_observations (observation_id, track_id, attempt_id, "
        "category, salience, note, visibility, observed_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'excerpt', ?, ?)",
        [
            observation_id,
            track_id,
            attempt,
            category,
            salience,
            note[:2000],
            naive_utc(moment),
            now,
        ],
    )
    migration_module.record_audit_entry(
        transaction,
        command=command,
        correlation_id=EventId.new(),
        outcome="succeeded",
        affected_records_json=json.dumps([observation_id]),
        after_summary=f"{category} observation ({salience})",
    )
    return ObservationReport(
        observation_id=observation_id,
        track_id=track_id,
        category=category,
        salience=salience,
        note=note[:2000],
        attempt_id=attempt,
        observed_at=moment.isoformat(),
    )


def item_observations(
    database: Database, *, track_id: str, content_id: str
) -> tuple[Observation, ...]:
    """Every evidence row bearing on one item, as the aggregation needs to see it."""

    return tuple(
        Observation(
            evidence_id=str(row[0]),
            claim=str(row[1]),
            polarity=str(row[2]),
            strength=float(row[3]),
            context_key=str(row[4]),
            retrieval=str(row[5]),
            help_level=str(row[6]),
            novelty=str(row[7]),
            occurred_at=aware_utc(row[8]),
        )
        for row in database.query(
            "SELECT evidence_id, claim, polarity, strength, context_key, retrieval, "
            "help_level, novelty, occurred_at FROM evidence "
            "WHERE track_id = ? AND target_content_id = ? ORDER BY occurred_at, evidence_id",
            [track_id, content_id],
        )
    )


def _explanation(outcome: MasteryOutcome) -> tuple[str, ...]:
    return tuple(f"{factor.name}: {factor.detail}" for factor in outcome.factors)


def recompute_item(
    database: Database,
    *,
    track_id: str,
    content_id: str,
    now: datetime,
    policy: MasteryPolicy = DEFAULT_POLICY,
    dry_run: bool = False,
) -> ItemStageChange:
    """Recompute one item's stage from its raw evidence, inside a caller's connection.

    A pure function of the evidence plus `now`, so running it twice writes the same row
    the second time. That is what makes `evidence recompute` safe to run at any moment
    and what lets its dry run be trusted.
    """

    identity = database.one(
        "SELECT record.stable_key, item.title, item.kind FROM knowledge_items item "
        "JOIN content_records record ON record.content_id = item.content_id "
        "WHERE item.content_id = ?",
        [content_id],
    )
    if identity is None:
        raise LinguaWikiError(
            "knowledge_item_not_found",
            f"no knowledge item with ID {content_id}",
            details=(ErrorDetail(field="item", reason="unknown content id"),),
        )
    records = item_observations(database, track_id=track_id, content_id=content_id)
    outcome = aggregate(records, now=now, kind=str(identity[2]), policy=policy)
    existing = database.one(
        "SELECT stage, confidence, gated_stage, evidence_ceiling, positive_evidence, "
        "negative_evidence FROM track_item_state WHERE track_id = ? AND content_id = ?",
        [track_id, content_id],
    )
    stage_before = None if existing is None else str(existing[0])
    # Confidence decays continuously, so an exact comparison would call every
    # recomputation a change and `evidence recompute` would report one every time it
    # ran. A real month of decay clears the tolerance; a few seconds does not.
    changed = existing is None or (
        stage_before,
        None if existing[2] is None else str(existing[2]),
        None if existing[3] is None else str(existing[3]),
        int(existing[4]),
        int(existing[5]),
    ) != (
        outcome.stage,
        outcome.gated_stage,
        outcome.ceiling,
        outcome.positive_count,
        outcome.negative_count,
    )
    if not changed and existing is not None:
        changed = abs(float(existing[1]) - outcome.confidence) > CONFIDENCE_TOLERANCE
    change = ItemStageChange(
        content_id=content_id,
        stable_key=str(identity[0]),
        title=str(identity[1]),
        stage_before=stage_before,
        stage_after=outcome.stage,
        gated_stage=outcome.gated_stage,
        evidence_ceiling=outcome.ceiling,
        confidence=outcome.confidence,
        positive=outcome.positive_count,
        negative=outcome.negative_count,
        contexts=len(outcome.contexts),
        regressed_claims=outcome.regressed_claims,
        explanation=_explanation(outcome),
        changed=changed,
    )
    if dry_run or not changed:
        return change
    stored = database.now()
    explanation_json = json.dumps(list(_explanation(outcome)), ensure_ascii=False)
    first = None if outcome.first_evidence_at is None else naive_utc(outcome.first_evidence_at)
    last = None if outcome.last_evidence_at is None else naive_utc(outcome.last_evidence_at)
    if existing is None:
        database.execute(
            "INSERT INTO track_item_state (track_id, content_id, stage, stage_source, "
            "confidence, priority, first_encounter_at, last_encounter_at, next_review_at, "
            "positive_evidence, negative_evidence, updated_at, aggregation_version, "
            "computed_at, explanation_json, gated_stage, evidence_ceiling) "
            "VALUES (?, ?, ?, 'evidence', ?, 0, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                track_id,
                content_id,
                outcome.stage,
                outcome.confidence,
                first,
                last,
                outcome.positive_count,
                outcome.negative_count,
                stored,
                outcome.aggregation_version,
                stored,
                explanation_json,
                outcome.gated_stage,
                outcome.ceiling,
            ],
        )
        return change
    database.execute(
        "UPDATE track_item_state SET stage = ?, stage_source = 'evidence', confidence = ?, "
        "first_encounter_at = coalesce(first_encounter_at, ?), last_encounter_at = ?, "
        "positive_evidence = ?, negative_evidence = ?, updated_at = ?, "
        "aggregation_version = ?, computed_at = ?, explanation_json = ?, gated_stage = ?, "
        "evidence_ceiling = ? WHERE track_id = ? AND content_id = ?",
        [
            outcome.stage,
            outcome.confidence,
            first,
            last,
            outcome.positive_count,
            outcome.negative_count,
            stored,
            outcome.aggregation_version,
            stored,
            explanation_json,
            outcome.gated_stage,
            outcome.ceiling,
            track_id,
            content_id,
        ],
    )
    return change


def recompute(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    item: str | None = None,
    dimensions: bool = True,
    dry_run: bool = False,
    clock: Clock | None = None,
    command: str = "evidence.recompute",
) -> RecomputeReport:
    """Recompute every item stage and dimension estimate from raw evidence.

    The point of retaining raw evidence is that a policy change can be replayed over it.
    A dry run reports exactly which stages would move and why, which is what makes such
    a change reviewable before it lands on a learner's model.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        moment = aware_utc(database.now())
        targets = (
            [knowledge_service.resolve_item(database, item, track_id=track_id)]
            if item is not None
            else [
                str(content_id)
                for (content_id,) in database.query(
                    "SELECT DISTINCT target_content_id FROM evidence WHERE track_id = ? "
                    "AND target_content_id IS NOT NULL ORDER BY target_content_id",
                    [track_id],
                )
            ]
        )
        changes: list[ItemStageChange] = []
        unchanged: list[str] = []
        estimates: estimate_service.RecomputeReport | None = None
        if dry_run:
            for content_id in targets:
                change = recompute_item(
                    database, track_id=track_id, content_id=content_id, now=moment, dry_run=True
                )
                if change.changed:
                    changes.append(change)
                else:
                    unchanged.append(change.stable_key)
            if dimensions:
                estimates = estimate_service.recompute(
                    database, track_id=track_id, now=moment, dry_run=True
                )
        else:
            with database.transaction() as transaction:
                for content_id in targets:
                    change = recompute_item(
                        transaction, track_id=track_id, content_id=content_id, now=moment
                    )
                    if change.changed:
                        changes.append(change)
                    else:
                        unchanged.append(change.stable_key)
                if dimensions:
                    estimates = estimate_service.recompute(
                        transaction, track_id=track_id, now=moment
                    )
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    affected_records_json=json.dumps([change.content_id for change in changes]),
                    after_summary=(
                        f"recomputed {len(targets)} item(s); {len(changes)} stage(s) changed"
                    ),
                )
    return RecomputeReport(
        track_id=track_id,
        dry_run=dry_run,
        aggregation_version=AGGREGATION_VERSION,
        items_considered=len(targets),
        items_changed=len(changes),
        changes=tuple(changes),
        unchanged=tuple(unchanged),
        estimates=estimates,
        warnings=("dry run: nothing was written",) if dry_run else (),
    )


def listing(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    item: str | None = None,
    dimension: str | None = None,
    limit: int = 50,
    clock: Clock | None = None,
) -> EvidenceListing:
    """The evidence behind an item or a dimension, newest first."""

    if limit < 1 or limit > 500:
        raise LinguaWikiError(
            "invalid_limit",
            "a listing limit is between 1 and 500",
            details=(ErrorDetail(field="limit", reason=str(limit)),),
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        target_id = (
            None
            if item is None
            else knowledge_service.resolve_item(database, item, track_id=track_id)
        )
        conditions = ["track_id = ?"]
        parameters: list[Any] = [track_id]
        if target_id is not None:
            conditions.append("target_content_id = ?")
            parameters.append(target_id)
        if dimension is not None:
            conditions.append("dimension = ?")
            parameters.append(dimension)
        where = " AND ".join(conditions)
        total = int(database.scalar(f"SELECT count(*) FROM evidence WHERE {where}", parameters))
        rows = database.query(
            "SELECT evidence_id, claim, polarity, strength, target_content_id, dimension, "
            "context_key, novelty, retrieval, help_level, modality, task_type, occurred_at "
            f"FROM evidence WHERE {where} ORDER BY occurred_at DESC, evidence_id DESC LIMIT ?",
            [*parameters, limit],
        )
        entries = tuple(
            EvidenceRecord(
                evidence_id=str(row[0]),
                claim=str(row[1]),
                polarity=str(row[2]),
                strength=float(row[3]),
                target_content_id=None if row[4] is None else str(row[4]),
                dimension=None if row[5] is None else str(row[5]),
                context_key=str(row[6]),
                novelty=str(row[7]),
                retrieval=str(row[8]),
                help_level=str(row[9]),
                modality=str(row[10]),
                task_type=str(row[11]),
                occurred_at=aware_utc(row[12]).isoformat(),
            )
            for row in rows
        )
    return EvidenceListing(
        track_id=track_id,
        limit=limit,
        total=total,
        target_content_id=target_id,
        dimension=dimension,
        entries=entries,
        warnings=(
            (f"{total} evidence row(s) recorded; {len(entries)} returned",)
            if total > len(entries)
            else ()
        ),
    )


__all__ = [
    "CONFIDENCE_TOLERANCE",
    "DELAYED_MINIMUM_HOURS",
    "EXCERPT_LIMIT",
    "OBSERVATION_CATEGORIES",
    "RESPONSE_VISIBILITIES",
    "SUPPORTED_ORIGINS",
    "AttemptReport",
    "EvidenceListing",
    "EvidenceRecord",
    "ItemStageChange",
    "ObservationReport",
    "RecomputeReport",
    "item_observations",
    "listing",
    "observations",
    "recompute",
    "recompute_item",
    "record",
]
