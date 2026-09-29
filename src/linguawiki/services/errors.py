"""Recurring errors: recognizing one, tracking it, and refusing to call it fixed.

The learner-facing promise is that a mistake is followed until it stops happening, and
that "it stopped happening" is a conclusion drawn from evidence rather than from one
lucky answer. Two mechanisms carry that.

*Identity is derived, and ambiguity is a question.* An error is
`(track, category, normalized signature, target)`, and the identity is the primary key,
so recording the same mistake twice is one pattern with two occurrences. A new signature
that is neither clearly the same nor clearly different is refused with the candidates
named: merging two different errors loses the distinction permanently, and asking costs
one flag. Nothing here tokenizes or transliterates, so a non-whitespace language dedupes
as reliably as an inflected one.

*Resolution is earned.* Counter-evidence is not asserted by the caller; it is derived
from the evidence rows themselves, so a hinted repetition cannot be filed as a
spontaneous success. The policy requires controlled, novel, spontaneous, and delayed
success across more than one context, and a configuration that could resolve an error on
a single observation is refused before it can be used.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.error_model import (
    CLASSIFICATIONS,
    CONFIRMED_CLASSIFICATION,
    DEFAULT_POLICY,
    LIVE_STATUSES,
    MEANING_IMPACTS,
    QUALIFICATIONS,
    SEVERITIES,
    STATUSES,
    SUPERSEDED_STATUS,
    UNCONFIRMED_STATUS,
    CounterEvidence,
    MatchCandidate,
    ResolutionPolicy,
    Transition,
    assert_known_status,
    assert_policy_is_sound,
    next_status_after_occurrence,
    next_status_after_success,
    normalize_signature,
    qualifications_for,
    uncertain_matches,
)
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.evidence import CONFIDENCE_LEVELS, assert_known
from linguawiki.ids import ErrorId, EventId, FollowUpId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service

if TYPE_CHECKING:
    from linguawiki.services.evidence import EvidenceRecord

#: The sentinel standing in for "no target item" in a derived identity. Content IDs are
#: prefixed, so it cannot collide with one.
NO_TARGET = "-"
FOLLOWUP_KINDS: tuple[str, ...] = ("review", "clarify", "practice", "resource", "assessment")
FOLLOWUP_STATUSES: tuple[str, ...] = ("open", "scheduled", "done", "dropped")


class OccurrenceRecord(ContractModel):
    occurrence_id: str
    attempt_id: str | None = None
    learner_form: str | None = None
    corrected_form: str | None = None
    explanation: str | None = None
    meaning_impact: str
    classification: str
    confidence: str
    observed_at: str


class CounterEvidenceRecord(ContractModel):
    evidence_id: str
    qualification: str
    context_key: str
    recorded_at: str


class ErrorReport(ContractModel):
    error_id: str
    track_id: str
    category: str
    signature: str
    description: str
    target_content_id: str | None = None
    target_title: str | None = None
    severity: str
    status: str
    status_reason: str | None = None
    #: Set when this pattern was folded into another; that is where its history went.
    superseded_by: str | None = None
    policy_version: str
    occurrence_count: int
    success_count: int
    #: What the policy is still waiting for. Empty when the error is resolved.
    outstanding: tuple[str, ...] = ()
    created: bool = False
    transitioned: bool = False
    occurrences: tuple[OccurrenceRecord, ...] = ()
    counter_evidence: tuple[CounterEvidenceRecord, ...] = ()
    first_seen_at: str
    last_seen_at: str
    last_success_at: str | None = None
    resolved_at: str | None = None
    #: The occurrence this call recorded, when it recorded one. Two corrections of
    #: one pattern are two occurrences of it, so a caller that has to name what it
    #: just wrote needs the occurrence rather than the pattern.
    recorded_occurrence_id: str | None = None
    warnings: tuple[str, ...] = ()


class ErrorListing(ContractModel):
    track_id: str
    status: str | None = None
    limit: int
    total: int
    live: int
    entries: tuple[ErrorReport, ...] = ()
    warnings: tuple[str, ...] = ()


class FollowUpReport(ContractModel):
    followup_id: str
    track_id: str
    kind: str
    action: str
    status: str
    priority: int
    target_content_id: str | None = None
    error_id: str | None = None
    due_from: str | None = None
    due_by: str | None = None
    created_at: str
    warnings: tuple[str, ...] = ()


class FollowUpListing(ContractModel):
    track_id: str
    status: str | None = None
    limit: int
    total: int
    entries: tuple[FollowUpReport, ...] = ()
    warnings: tuple[str, ...] = ()


def error_identity(
    *, track_id: str, category: str, signature: str, target_content_id: str | None
) -> str:
    """The derived identity of one recurring error.

    Deriving it rather than generating one is what makes deduplication a primary-key
    fact: recording the same category, signature, and target twice cannot produce two
    patterns, and a nullable target has one identity rather than one per occurrence.
    """

    return str(ErrorId.derive(track_id, category, signature, target_content_id or NO_TARGET))


def _counter_evidence(database: Database, *, error_id: str) -> CounterEvidence:
    counts = {
        str(qualification): int(total)
        for qualification, total in database.query(
            "SELECT qualification, count(*) FROM error_evidence WHERE error_id = ? "
            "GROUP BY qualification",
            [error_id],
        )
    }
    successes = int(
        database.scalar(
            "SELECT count(DISTINCT evidence_id) FROM error_evidence WHERE error_id = ?",
            [error_id],
        )
    )
    contexts = int(
        database.scalar(
            "SELECT count(DISTINCT context_key) FROM error_evidence WHERE error_id = ?",
            [error_id],
        )
    )
    row = database.one(
        "SELECT last_seen_at, last_success_at FROM error_patterns WHERE error_id = ?",
        [error_id],
    )
    return CounterEvidence(
        successes=successes,
        contexts=contexts,
        qualifications=counts,
        last_occurrence_at=None if row is None or row[0] is None else aware_utc(row[0]),
        last_success_at=None if row is None or row[1] is None else aware_utc(row[1]),
    )


def _outstanding(
    counter: CounterEvidence, *, status: str, policy: ResolutionPolicy
) -> tuple[str, ...]:
    """What the policy is still waiting for before this error can move on.

    Nothing, for a pattern that is not waiting: resolved, superseded because its history
    moved elsewhere, or unconfirmed because nothing observed was definitely the learner's
    error. Listing unmet requirements against any of those would read as though the
    learner had an error that is not theirs, or no longer lives there.
    """

    if status in ("resolved", SUPERSEDED_STATUS, UNCONFIRMED_STATUS):
        return ()
    missing: list[str] = []
    if counter.successes < policy.resolution_successes:
        missing.append(f"{counter.successes}/{policy.resolution_successes} qualifying success(es)")
    if counter.contexts < policy.resolution_contexts:
        missing.append(f"{counter.contexts}/{policy.resolution_contexts} distinct context(s)")
    missing.extend(
        f"a {qualification} success"
        for qualification in policy.resolution_qualifications
        if not counter.count(qualification)
    )
    return tuple(missing)


def _read_error(
    database: Database,
    *,
    error_id: str,
    policy: ResolutionPolicy = DEFAULT_POLICY,
    include_history: bool = True,
) -> ErrorReport:
    row = database.one(
        "SELECT pattern.error_id, pattern.track_id, pattern.category, pattern.signature, "
        "pattern.description, pattern.target_content_id, pattern.severity, pattern.status, "
        "pattern.status_reason, pattern.policy_version, pattern.occurrence_count, "
        "pattern.success_count, pattern.first_seen_at, pattern.last_seen_at, "
        "pattern.last_success_at, pattern.resolved_at, item.title, pattern.superseded_by "
        "FROM error_patterns pattern "
        "LEFT JOIN knowledge_items item ON item.content_id = pattern.target_content_id "
        "WHERE pattern.error_id = ?",
        [error_id],
    )
    if row is None:
        raise LinguaWikiError(
            "error_pattern_not_found",
            f"no error pattern with ID {error_id}",
            details=(ErrorDetail(field="error", reason="unknown error pattern"),),
        )
    counter = _counter_evidence(database, error_id=error_id)
    occurrences: tuple[OccurrenceRecord, ...] = ()
    counter_rows: tuple[CounterEvidenceRecord, ...] = ()
    if include_history:
        occurrences = tuple(
            OccurrenceRecord(
                occurrence_id=str(entry[0]),
                attempt_id=None if entry[1] is None else str(entry[1]),
                learner_form=None if entry[2] is None else str(entry[2]),
                corrected_form=None if entry[3] is None else str(entry[3]),
                explanation=None if entry[4] is None else str(entry[4]),
                meaning_impact=str(entry[5]),
                classification=str(entry[6]),
                confidence=str(entry[7]),
                observed_at=aware_utc(entry[8]).isoformat(),
            )
            for entry in database.query(
                "SELECT occurrence_id, attempt_id, learner_form, corrected_form, explanation, "
                "meaning_impact, classification, confidence, observed_at FROM error_occurrences "
                "WHERE error_id = ? ORDER BY observed_at, occurrence_id",
                [error_id],
            )
        )
        counter_rows = tuple(
            CounterEvidenceRecord(
                evidence_id=str(entry[0]),
                qualification=str(entry[1]),
                context_key=str(entry[2]),
                recorded_at=aware_utc(entry[3]).isoformat(),
            )
            for entry in database.query(
                "SELECT evidence_id, qualification, context_key, recorded_at FROM error_evidence "
                "WHERE error_id = ? ORDER BY recorded_at, evidence_id, qualification",
                [error_id],
            )
        )
    return ErrorReport(
        error_id=str(row[0]),
        track_id=str(row[1]),
        category=str(row[2]),
        signature=str(row[3]),
        description=str(row[4]),
        target_content_id=None if row[5] is None else str(row[5]),
        severity=str(row[6]),
        status=str(row[7]),
        status_reason=None if row[8] is None else str(row[8]),
        policy_version=str(row[9]),
        occurrence_count=int(row[10]),
        success_count=int(row[11]),
        outstanding=_outstanding(counter, status=str(row[7]), policy=policy),
        occurrences=occurrences,
        counter_evidence=counter_rows,
        first_seen_at=aware_utc(row[12]).isoformat(),
        last_seen_at=aware_utc(row[13]).isoformat(),
        last_success_at=None if row[14] is None else aware_utc(row[14]).isoformat(),
        resolved_at=None if row[15] is None else aware_utc(row[15]).isoformat(),
        target_title=None if row[16] is None else str(row[16]),
        superseded_by=None if row[17] is None else str(row[17]),
    )


def read_error(
    database: Database,
    *,
    error_id: str,
    policy: ResolutionPolicy = DEFAULT_POLICY,
    include_history: bool = True,
) -> ErrorReport:
    """One error pattern read inside a caller's connection.

    `include_history` is off for a context bundle: the occurrences hold the learner's
    own words, and the pattern's signature is the pedagogical fact an agent needs.
    """

    return _read_error(database, error_id=error_id, policy=policy, include_history=include_history)


def resolve_error(
    database: Database, *, track_id: str, error: str, allow_superseded: bool = False
) -> str:
    """Resolve an error reference to an identity, refusing another track's error.

    A superseded pattern is refused with its successor named: its history moved when a
    duplicate knowledge item was merged away, and an occurrence recorded against it
    would land where nothing reads it.
    """

    row = database.one(
        "SELECT error_id, track_id, status, superseded_by FROM error_patterns WHERE error_id = ?",
        [error],
    )
    if row is None:
        raise LinguaWikiError(
            "error_pattern_not_found",
            f"no error pattern with ID {error}",
            details=(ErrorDetail(field="error", reason="unknown error pattern"),),
        )
    if str(row[1]) != track_id:
        raise LinguaWikiError(
            "error_pattern_not_on_track",
            f"{error} belongs to another track",
            details=(ErrorDetail(field="error", reason="wrong track"),),
        )
    if str(row[2]) == SUPERSEDED_STATUS and not allow_superseded:
        successor = "an unnamed pattern" if row[3] is None else str(row[3])
        raise LinguaWikiError(
            "error_pattern_superseded",
            f"{error} was folded into {successor} when a duplicate item was merged away; "
            f"use {successor} instead",
            details=(
                ErrorDetail(
                    field="error",
                    reason="superseded pattern",
                    context={"superseded_by": successor},
                ),
            ),
        )
    return str(row[0])


def _follow_supersession(database: Database, *, error_id: str) -> tuple[str, str | None]:
    """Follow a superseded pattern to the one its history moved to.

    Recording against a shell would put the occurrence where nothing reads it, and a
    refusal here would be a dead end: the two really are the same error, so the
    occurrence belongs on the successor and the caller is told it was redirected.
    """

    seen: set[str] = set()
    current = error_id
    while current not in seen:
        seen.add(current)
        row = database.one(
            "SELECT status, superseded_by FROM error_patterns WHERE error_id = ?", [current]
        )
        if row is None or str(row[0]) != SUPERSEDED_STATUS or row[1] is None:
            break
        current = str(row[1])
    return (current, None if current == error_id else current)


def _candidates(
    database: Database, *, track_id: str, category: str, target_content_id: str | None
) -> tuple[MatchCandidate, ...]:
    """Existing patterns in the same category and on the same target.

    Scoped to the same category and target on purpose: two signatures that look alike in
    different categories are not the same error, and comparing them would manufacture
    ambiguity where none exists.
    """

    scope = "target_content_id IS NULL" if target_content_id is None else "target_content_id = ?"
    parameters: list[Any] = [track_id, category]
    if target_content_id is not None:
        parameters.append(target_content_id)
    return tuple(
        MatchCandidate(
            error_id=str(row[0]), signature=str(row[1]), similarity=0.0, status=str(row[2])
        )
        for row in database.query(
            "SELECT error_id, signature, status FROM error_patterns "
            # A superseded shell cannot be attached to, so offering it as a candidate
            # would name a remedy that does not work.
            f"WHERE track_id = ? AND category = ? AND {scope} "
            f"AND status <> '{SUPERSEDED_STATUS}' ORDER BY error_id",
            parameters,
        )
    )


@dataclass(frozen=True, slots=True)
class OccurrencePlan:
    """One error occurrence, resolved against the database and not yet written.

    The same split as `evidence.plan_attempt`, and for the same reason: a session close
    materializes corrections alongside attempts inside one transaction, and it cannot
    call a command that opens its own.
    """

    track_id: str
    command: str
    error_id: str
    redirected: str | None
    existing: tuple[Any, ...] | None
    category: str
    signature: str
    description: str
    target_content_id: str | None
    severity: str
    status: str
    status_reason: str
    transitioned: bool
    occurrences: int
    confirmed: bool
    classification: str
    confidence: str
    meaning_impact: str
    learner_form: str | None
    corrected_form: str | None
    explanation: str | None
    attempt_id: str | None
    observed_at: datetime
    policy: ResolutionPolicy


def plan_occurrence(
    database: Database,
    *,
    category: str,
    signature: str,
    description: str,
    target: str | None = None,
    learner_form: str | None = None,
    corrected_form: str | None = None,
    explanation: str | None = None,
    meaning_impact: str = "minor",
    classification: str = "learner-error",
    confidence: str = "medium",
    severity: str = "medium",
    attempt: str | None = None,
    observed_at: Any = None,
    attach_to: str | None = None,
    distinct: bool = False,
    track: str | None = None,
    policy: ResolutionPolicy = DEFAULT_POLICY,
    command: str = "errors.record",
) -> OccurrencePlan:
    """Resolve one occurrence: which pattern it belongs to, and what it does to it.

    An exact identity match appends an occurrence. A close-but-not-equal signature is
    refused with the candidates listed: `--attach-to` files it against an existing
    pattern, `--distinct` insists it is a new one. Both remedies work, which is the point
    of refusing rather than guessing. Nothing is written here.
    """

    assert_policy_is_sound(policy)
    if not category.strip():
        raise LinguaWikiError(
            "invalid_error_category",
            "an error needs a category",
            details=(ErrorDetail(field="category", reason="blank category"),),
        )
    assert_known(
        meaning_impact,
        vocabulary=MEANING_IMPACTS,
        field="meaning_impact",
        code="unknown_meaning_impact",
    )
    assert_known(
        classification,
        vocabulary=CLASSIFICATIONS,
        field="classification",
        code="unknown_error_classification",
    )
    assert_known(
        confidence, vocabulary=CONFIDENCE_LEVELS, field="confidence", code="unknown_confidence"
    )
    assert_known(severity, vocabulary=SEVERITIES, field="severity", code="unknown_severity")
    if attach_to is not None and distinct:
        raise LinguaWikiError(
            "conflicting_error_match",
            "--attach-to names an existing pattern and --distinct insists on a new one; choose one",
            details=(ErrorDetail(field="attach_to", reason="conflicting instructions"),),
        )
    normalized = normalize_signature(signature)
    track_id = learner_service.resolve_track(database, track)
    target_id = (
        None
        if target is None
        else knowledge_service.resolve_item(database, target, track_id=track_id)
    )
    if attempt is not None and not int(
        database.scalar(
            "SELECT count(*) FROM attempts WHERE attempt_id = ? AND track_id = ?",
            [attempt, track_id],
        )
    ):
        raise LinguaWikiError(
            "attempt_not_found",
            f"no attempt {attempt} on this track",
            details=(ErrorDetail(field="attempt", reason="unknown attempt"),),
        )
    error_id = (
        resolve_error(database, track_id=track_id, error=attach_to)
        if attach_to is not None
        else error_identity(
            track_id=track_id,
            category=category,
            signature=normalized,
            target_content_id=target_id,
        )
    )
    error_id, redirected = _follow_supersession(database, error_id=error_id)
    existing = database.one(
        "SELECT status, occurrence_count, signature FROM error_patterns WHERE error_id = ?",
        [error_id],
    )
    if existing is None and not distinct and attach_to is None:
        close = uncertain_matches(
            normalized,
            _candidates(
                database, track_id=track_id, category=category, target_content_id=target_id
            ),
        )
        if close:
            raise LinguaWikiError(
                "error_match_uncertain",
                f"'{signature}' is close to {len(close)} recorded error(s) in {category} "
                "without matching one. Pass --attach-to <error-id> to file it against an "
                "existing pattern, or --distinct to record it as a new one.",
                details=tuple(
                    ErrorDetail(
                        field="signature",
                        reason=f"{candidate.similarity:.3f} similar to {candidate.signature}",
                        context={
                            "error_id": candidate.error_id,
                            "status": candidate.status,
                        },
                    )
                    for candidate in close
                ),
            )
    moment = aware_utc(observed_at) if observed_at is not None else aware_utc(database.now())
    # Only a confirmed learner error counts toward activation. An artifact -- or a
    # classification nobody settled -- is kept on the occurrence row for the audit
    # trail and changes nothing about the pattern.
    confirmed = classification == CONFIRMED_CLASSIFICATION
    occurrences = (int(existing[1]) if existing is not None else 0) + (1 if confirmed else 0)
    transition = (
        Transition(
            status="observed" if confirmed else UNCONFIRMED_STATUS,
            reason=(
                "first occurrence recorded"
                if confirmed
                else f"the first occurrence was classified {classification}, so nothing "
                "is counted against the learner yet"
            ),
            changed=True,
        )
        if existing is None
        else next_status_after_occurrence(
            str(existing[0]), occurrences=occurrences, confirmed=confirmed, policy=policy
        )
    )
    return OccurrencePlan(
        track_id=track_id,
        command=command,
        error_id=error_id,
        redirected=redirected,
        existing=None if existing is None else tuple(existing),
        category=category,
        signature=normalized,
        description=description,
        target_content_id=target_id,
        severity=severity,
        status=transition.status,
        status_reason=transition.reason,
        transitioned=transition.changed,
        occurrences=occurrences,
        confirmed=confirmed,
        classification=classification,
        confidence=confidence,
        meaning_impact=meaning_impact,
        learner_form=learner_form,
        corrected_form=corrected_form,
        explanation=explanation,
        attempt_id=attempt,
        observed_at=moment,
        policy=policy,
    )


def write_occurrence(transaction: Database, plan: OccurrencePlan) -> ErrorReport:
    """Write one planned occurrence and the pattern transition it causes."""

    track_id = plan.track_id
    error_id = plan.error_id
    existing = plan.existing
    category = plan.category
    normalized = plan.signature
    description = plan.description
    target_id = plan.target_content_id
    severity = plan.severity
    occurrences = plan.occurrences
    confirmed = plan.confirmed
    classification = plan.classification
    confidence = plan.confidence
    meaning_impact = plan.meaning_impact
    learner_form = plan.learner_form
    corrected_form = plan.corrected_form
    explanation = plan.explanation
    attempt = plan.attempt_id
    moment = plan.observed_at
    policy = plan.policy
    command = plan.command
    redirected = plan.redirected
    transition = Transition(
        status=plan.status, reason=plan.status_reason, changed=plan.transitioned
    )
    occurrence_id = str(ErrorId.new())
    now = transaction.now()
    if existing is None:
        transaction.execute(
            "INSERT INTO error_patterns (error_id, track_id, category, signature, "
            "description, target_content_id, severity, status, status_reason, "
            "policy_version, occurrence_count, success_count, first_seen_at, "
            "last_seen_at, last_success_at, monitoring_since, resolved_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, ?)",
            [
                error_id,
                track_id,
                category,
                normalized,
                description,
                target_id,
                severity,
                transition.status,
                transition.reason,
                policy.version,
                occurrences,
                naive_utc(moment),
                naive_utc(moment),
                now,
            ],
        )
    else:
        transaction.execute(
            "UPDATE error_patterns SET status = ?, status_reason = ?, "
            "occurrence_count = ?, severity = ?, "
            # `last_seen_at` is what the resolution policy weighs against the
            # last success, so an unconfirmed occurrence must not advance it.
            "last_seen_at = CASE WHEN ? THEN ? ELSE last_seen_at END, "
            "resolved_at = CASE WHEN ? = 'resolved' THEN resolved_at ELSE NULL END, "
            "updated_at = ? WHERE error_id = ?",
            [
                transition.status,
                transition.reason,
                occurrences,
                severity,
                confirmed,
                naive_utc(moment),
                transition.status,
                now,
                error_id,
            ],
        )
    transaction.execute(
        "INSERT INTO error_occurrences (occurrence_id, error_id, attempt_id, "
        "learner_form, corrected_form, explanation, meaning_impact, classification, "
        "confidence, observed_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            occurrence_id,
            error_id,
            attempt,
            None if learner_form is None else learner_form[:2000],
            None if corrected_form is None else corrected_form[:2000],
            explanation,
            meaning_impact,
            classification,
            confidence,
            naive_utc(moment),
            now,
        ],
    )
    migration_module.record_audit_entry(
        transaction,
        command=command,
        correlation_id=EventId.new(),
        outcome="succeeded",
        affected_records_json=json.dumps([error_id]),
        before_summary=None if existing is None else f"status {existing[0]}",
        after_summary=f"occurrence {occurrences}; status {transition.status}",
    )
    migration_module.record_domain_event(
        transaction,
        event_type="error.occurred",
        aggregate_type="error_pattern",
        aggregate_id=error_id,
        correlation_id=EventId.new(),
        payload_json=json.dumps(
            {
                "category": category,
                "status": transition.status,
                "occurrence_count": occurrences,
            },
            sort_keys=True,
        ),
    )
    report = _read_error(transaction, error_id=error_id, policy=policy)
    warnings = []
    if not confirmed:
        warnings.append(
            f"classified {classification}, so it is on record but is not counted against "
            "the learner; record it as a learner error to activate the pattern"
        )
    if redirected is not None:
        warnings.append(
            f"the pattern this occurrence derives was folded into {redirected} when a "
            "duplicate item was merged away; it was recorded there"
        )
    return report.model_copy(
        update={
            "created": existing is None,
            "transitioned": transition.changed,
            "recorded_occurrence_id": occurrence_id,
            "warnings": tuple(warnings),
        }
    )


def record(
    paths: WorkspacePaths,
    *,
    category: str,
    signature: str,
    description: str,
    target: str | None = None,
    learner_form: str | None = None,
    corrected_form: str | None = None,
    explanation: str | None = None,
    meaning_impact: str = "minor",
    classification: str = "learner-error",
    confidence: str = "medium",
    severity: str = "medium",
    attempt: str | None = None,
    observed_at: Any = None,
    attach_to: str | None = None,
    distinct: bool = False,
    track: str | None = None,
    policy: ResolutionPolicy = DEFAULT_POLICY,
    clock: Clock | None = None,
    command: str = "errors.record",
) -> ErrorReport:
    """Record one occurrence, deduplicating it into the pattern it belongs to.

    An exact identity match appends an occurrence. A close-but-not-equal signature is
    refused with the candidates listed: `--attach-to` files it against an existing
    pattern, `--distinct` insists it is a new one. Both remedies work, which is the point
    of refusing rather than guessing.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        plan = plan_occurrence(
            database,
            category=category,
            signature=signature,
            description=description,
            target=target,
            learner_form=learner_form,
            corrected_form=corrected_form,
            explanation=explanation,
            meaning_impact=meaning_impact,
            classification=classification,
            confidence=confidence,
            severity=severity,
            attempt=attempt,
            observed_at=observed_at,
            attach_to=attach_to,
            distinct=distinct,
            track=track,
            policy=policy,
            command=command,
        )
        with database.transaction() as transaction:
            return write_occurrence(transaction, plan)


def settle_from_evidence(
    database: Database,
    *,
    track_id: str,
    target_content_id: str | None,
    evidence: Sequence[EvidenceRecord],
    policy: ResolutionPolicy = DEFAULT_POLICY,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Attach new evidence to the errors it bears on, and move them if it is enough.

    Returns the errors the evidence supported and the errors it reactivated. Which
    qualifications the evidence carries is derived from the evidence row, never asserted:
    that is what stops a hinted repetition from being filed as a spontaneous success and
    resolving an error it did not fix.

    A failure on the target reactivates a monitored or resolved error rather than
    resolving nothing quietly. It is the weaker signal -- a recorded occurrence is the
    strong one -- so the reason says so.
    """

    if target_content_id is None or not evidence:
        return ((), ())
    # Superseded shells are excluded: their history moved to the successor, which carries
    # the same target, so the evidence reaches it there. Attaching to a shell would also
    # fail the `error_supersession` check, which requires one to hold nothing.
    # Superseded shells are excluded because their history moved to the successor, and
    # unconfirmed ones because there is no established error for a success to argue
    # against -- crediting one would let an artifact be "fixed".
    patterns = database.query(
        "SELECT error_id, status FROM error_patterns WHERE track_id = ? "
        f"AND target_content_id = ? AND status NOT IN ('{SUPERSEDED_STATUS}', "
        f"'{UNCONFIRMED_STATUS}') ORDER BY error_id",
        [track_id, target_content_id],
    )
    supported: list[str] = []
    reactivated: list[str] = []
    now = database.now()
    for error_id, status in patterns:
        identity = str(error_id)
        current = str(status)
        negative = [entry for entry in evidence if entry.polarity == "negative"]
        positive = [entry for entry in evidence if entry.polarity == "positive"]
        if negative:
            if current in ("monitoring", "resolved"):
                database.execute(
                    "UPDATE error_patterns SET status = 'reactivated', status_reason = ?, "
                    "resolved_at = NULL, updated_at = ? WHERE error_id = ?",
                    [
                        "a failure on the target item while the error was "
                        f"{current}; a recorded occurrence would be the stronger signal",
                        now,
                        identity,
                    ],
                )
                reactivated.append(identity)
            continue
        attached = 0
        for entry in positive:
            for qualification in qualifications_for(
                claim=entry.claim,
                retrieval=entry.retrieval,
                novelty=entry.novelty,
                help_level=entry.help_level,
                modality=entry.modality,
                task_type=entry.task_type,
            ):
                database.execute(
                    "INSERT INTO error_evidence (error_id, evidence_id, qualification, "
                    "context_key, recorded_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    [identity, entry.evidence_id, qualification, entry.context_key, now],
                )
                attached += 1
        if not attached:
            continue
        supported.append(identity)
        successes = int(
            database.scalar(
                "SELECT count(DISTINCT evidence_id) FROM error_evidence WHERE error_id = ?",
                [identity],
            )
        )
        database.execute(
            "UPDATE error_patterns SET success_count = ?, last_success_at = ?, updated_at = ? "
            "WHERE error_id = ?",
            [successes, now, now, identity],
        )
        counter = _counter_evidence(database, error_id=identity)
        transition = next_status_after_success(current, counter=counter, policy=policy)
        if not transition.changed:
            continue
        database.execute(
            "UPDATE error_patterns SET status = ?, status_reason = ?, "
            "monitoring_since = CASE WHEN ? = 'monitoring' THEN ? ELSE monitoring_since END, "
            "resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE NULL END, updated_at = ? "
            "WHERE error_id = ?",
            [
                transition.status,
                transition.reason,
                transition.status,
                now,
                transition.status,
                now,
                now,
                identity,
            ],
        )
        migration_module.record_domain_event(
            database,
            event_type="error.transitioned",
            aggregate_type="error_pattern",
            aggregate_id=identity,
            correlation_id=EventId.new(),
            payload_json=json.dumps(
                {"from": current, "to": transition.status, "reason": transition.reason},
                sort_keys=True,
            ),
        )
    return (tuple(supported), tuple(reactivated))


def show(
    paths: WorkspacePaths,
    *,
    error: str,
    track: str | None = None,
    clock: Clock | None = None,
) -> ErrorReport:
    """One error with its occurrences, counter-evidence, and what it still needs."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        return _read_error(
            database, error_id=resolve_error(database, track_id=track_id, error=error)
        )


def listing(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    status: str | None = None,
    live_only: bool = False,
    limit: int = 50,
    clock: Clock | None = None,
) -> ErrorListing:
    """The track's errors, worst first, with the live ones counted separately."""

    if status is not None:
        assert_known_status(status)
    if limit < 1 or limit > 500:
        raise LinguaWikiError(
            "invalid_limit",
            "a listing limit is between 1 and 500",
            details=(ErrorDetail(field="limit", reason=str(limit)),),
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        # A superseded shell is history, not an error the learner still has, so a
        # listing hides it unless it is asked for by status explicitly.
        conditions = ["track_id = ?", f"status <> '{SUPERSEDED_STATUS}'"]
        parameters: list[Any] = [track_id]
        if status is not None:
            conditions = ["track_id = ?", "status = ?"]
            parameters.append(status)
        elif live_only:
            placeholders = ", ".join("?" for _ in LIVE_STATUSES)
            conditions.append(f"status IN ({placeholders})")
            parameters.extend(LIVE_STATUSES)
        where = " AND ".join(conditions)
        total = int(
            database.scalar(f"SELECT count(*) FROM error_patterns WHERE {where}", parameters)
        )
        live = int(
            database.scalar(
                "SELECT count(*) FROM error_patterns WHERE track_id = ? AND status IN "
                f"({', '.join('?' for _ in LIVE_STATUSES)})",
                [track_id, *LIVE_STATUSES],
            )
        )
        identities = [
            str(error_id)
            for (error_id,) in database.query(
                f"SELECT error_id FROM error_patterns WHERE {where} "
                # Worst first: severity, then how often, then how recently.
                "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
                "occurrence_count DESC, last_seen_at DESC, error_id LIMIT ?",
                [*parameters, limit],
            )
        ]
        entries = tuple(
            _read_error(database, error_id=identity, include_history=False)
            for identity in identities
        )
    return ErrorListing(
        track_id=track_id,
        status=status,
        limit=limit,
        total=total,
        live=live,
        entries=entries,
        warnings=(
            (f"{total} error(s) recorded; {len(entries)} returned",) if total > len(entries) else ()
        ),
    )


def add_followup(
    paths: WorkspacePaths,
    *,
    kind: str,
    action: str,
    target: str | None = None,
    error: str | None = None,
    attempt: str | None = None,
    priority: int = 0,
    due_from: Any = None,
    due_by: Any = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "errors.followup",
) -> FollowUpReport:
    """Queue one follow-up: something to come back to, with a due window.

    The rules live in `write_followup`, which a session close calls inside its own
    transaction; this is the standalone command around it.
    """

    active_clock = clock or SystemClock()
    with (
        open_writer(paths, command=command, clock=active_clock) as database,
        database.transaction() as transaction,
    ):
        return write_followup(
            transaction,
            kind=kind,
            action=action,
            target=target,
            error=error,
            attempt=attempt,
            priority=priority,
            due_from=due_from,
            due_by=due_by,
            track=track,
            command=command,
        )


def write_followup(
    transaction: Database,
    *,
    kind: str,
    action: str,
    target: str | None = None,
    error: str | None = None,
    attempt: str | None = None,
    priority: int = 0,
    due_from: Any = None,
    due_by: Any = None,
    track: str | None = None,
    command: str = "errors.followup",
) -> FollowUpReport:
    """Queue one follow-up inside a caller's transaction.

    A session close writes the follow-ups its blocks produced alongside the attempts and
    corrections, in the same transaction: a follow-up that survived while the observation
    behind it rolled back would be a reminder about something that never happened.
    """

    assert_known(kind, vocabulary=FOLLOWUP_KINDS, field="kind", code="unknown_followup_kind")
    if not action.strip():
        raise LinguaWikiError(
            "invalid_followup",
            "a follow-up needs an action",
            details=(ErrorDetail(field="action", reason="blank action"),),
        )
    if due_from is not None and due_by is not None and aware_utc(due_from) > aware_utc(due_by):
        raise LinguaWikiError(
            "invalid_due_window",
            "a follow-up's due window cannot end before it starts",
            details=(ErrorDetail(field="due_by", reason="window ends before it starts"),),
        )
    track_id = learner_service.resolve_track(transaction, track)
    target_id = (
        None
        if target is None
        else knowledge_service.resolve_item(transaction, target, track_id=track_id)
    )
    error_id = None if error is None else resolve_error(transaction, track_id=track_id, error=error)
    followup_id = str(FollowUpId.new())
    now = transaction.now()
    transaction.execute(
        "INSERT INTO followups (followup_id, track_id, kind, action, target_content_id, "
        "error_id, origin_attempt_id, priority, status, due_from, due_by, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
        [
            followup_id,
            track_id,
            kind,
            action,
            target_id,
            error_id,
            attempt,
            priority,
            None if due_from is None else naive_utc(aware_utc(due_from)),
            None if due_by is None else naive_utc(aware_utc(due_by)),
            now,
            now,
        ],
    )
    migration_module.record_audit_entry(
        transaction,
        command=command,
        correlation_id=EventId.new(),
        outcome="succeeded",
        affected_records_json=json.dumps([followup_id]),
        after_summary=f"{kind} follow-up queued",
    )

    return FollowUpReport(
        followup_id=followup_id,
        track_id=track_id,
        kind=kind,
        action=action,
        status="open",
        priority=priority,
        target_content_id=target_id,
        error_id=error_id,
        due_from=None if due_from is None else aware_utc(due_from).isoformat(),
        due_by=None if due_by is None else aware_utc(due_by).isoformat(),
        created_at=aware_utc(now).isoformat(),
    )


def followups(
    database: Database, *, track_id: str, status: str | None = "open", limit: int = 50
) -> tuple[FollowUpReport, ...]:
    """Open follow-ups for a track, inside a caller's connection."""

    conditions = ["track_id = ?"]
    parameters: list[Any] = [track_id]
    if status is not None:
        conditions.append("status = ?")
        parameters.append(status)
    return tuple(
        FollowUpReport(
            followup_id=str(row[0]),
            track_id=track_id,
            kind=str(row[1]),
            action=str(row[2]),
            status=str(row[3]),
            priority=int(row[4]),
            target_content_id=None if row[5] is None else str(row[5]),
            error_id=None if row[6] is None else str(row[6]),
            due_from=None if row[7] is None else aware_utc(row[7]).isoformat(),
            due_by=None if row[8] is None else aware_utc(row[8]).isoformat(),
            created_at=aware_utc(row[9]).isoformat(),
        )
        for row in database.query(
            "SELECT followup_id, kind, action, status, priority, target_content_id, error_id, "
            "due_from, due_by, created_at FROM followups "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY priority DESC, coalesce(due_by, created_at), followup_id LIMIT ?",
            [*parameters, limit],
        )
    )


def list_followups(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    status: str | None = "open",
    limit: int = 50,
    clock: Clock | None = None,
) -> FollowUpListing:
    """The track's follow-up queue."""

    if status is not None:
        assert_known(
            status, vocabulary=FOLLOWUP_STATUSES, field="status", code="unknown_followup_status"
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        total = int(
            database.scalar(
                "SELECT count(*) FROM followups WHERE track_id = ?"
                + ("" if status is None else " AND status = ?"),
                [track_id] if status is None else [track_id, status],
            )
        )
        entries = followups(database, track_id=track_id, status=status, limit=limit)
    return FollowUpListing(
        track_id=track_id, status=status, limit=limit, total=total, entries=entries
    )


__all__ = [
    "FOLLOWUP_KINDS",
    "FOLLOWUP_STATUSES",
    "NO_TARGET",
    "QUALIFICATIONS",
    "STATUSES",
    "CounterEvidenceRecord",
    "ErrorListing",
    "ErrorReport",
    "FollowUpListing",
    "FollowUpReport",
    "OccurrenceRecord",
    "add_followup",
    "error_identity",
    "followups",
    "list_followups",
    "listing",
    "read_error",
    "record",
    "resolve_error",
    "settle_from_evidence",
    "show",
]
