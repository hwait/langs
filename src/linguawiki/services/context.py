"""Bounded, privacy-aware context bundles for the agent that has to act.

A bundle is not a dump of the learner model. Each scope declares the sections it may
include, and a section the scope does not name cannot appear in it however useful it
looks -- which is what makes "only relevant context" a property of the code rather than a
habit. Two of those exclusions are deliberate and worth naming:

- an `assessment` bundle carries no error patterns and no per-item evidence. An assessor
  that knows what the learner usually gets wrong is no longer measuring them;
- no bundle carries a learner's own words unless the command asks for them *and* the
  track consented to retaining them. Without both, the excerpt is reported as withheld
  rather than quietly included.

Everything is bounded twice: each section has a record cap, and the bundle has a token
budget. Whatever does not fit is reported in `omissions` with the count and the reason,
so an agent can tell "there are no active errors" from "there are eleven and you were
shown three". A bundle that silently truncates is worse than no bundle, because the agent
will reason as though it saw everything.

The token figure is a deterministic estimate from the serialized size, not a tokenizer's
count. It is documented as an estimate and used only to bound the bundle, never reported
as a provider's number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.db.connection import Database, open_reader
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import errors as error_service
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service

SCOPES: tuple[str, ...] = ("session", "assessment", "source", "concept")

#: Roughly four characters to a token. A deterministic estimate over the serialized
#: bundle, which is all a budget needs; it is never presented as a provider's count.
CHARACTERS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Budget:
    """One scope's limits, and the sections it is allowed to carry."""

    records: int
    tokens: int
    #: Ordered most important first. Trimming starts from the end.
    sections: tuple[str, ...]


#: Each scope's own budget and section list. A section absent here cannot be included in
#: that scope: relevance is declared, not argued for at the call site.
BUDGETS: dict[str, Budget] = {
    "session": Budget(
        records=60,
        tokens=3000,
        sections=(
            "profile",
            "estimates",
            "active_errors",
            "due_work",
            "recent_evidence",
            "curriculum_position",
            "provenance",
        ),
    ),
    "assessment": Budget(
        records=40,
        tokens=2000,
        # No errors and no per-item evidence: an assessor primed with the learner's
        # usual mistakes is scoring its own expectations.
        sections=("profile", "estimates", "exposure", "provenance"),
    ),
    "source": Budget(
        records=40,
        tokens=2000,
        sections=("profile", "estimates", "sources", "curriculum_position", "provenance"),
    ),
    "concept": Budget(
        records=50,
        tokens=2500,
        sections=(
            "focus",
            "item_state",
            "recent_evidence",
            "active_errors",
            "due_work",
            "provenance",
        ),
    ),
}
MAXIMUM_TOKENS = 20000
MAXIMUM_RECORDS = 500


class Omission(ContractModel):
    """One thing left out, with how much of it there was and why."""

    section: str
    available: int
    included: int
    reason: str


class ProfileSection(ContractModel):
    track_id: str
    user_display_name: str
    target_language: str
    script: str | None = None
    framework_id: str
    framework_levels: tuple[str, ...] = ()
    declared_level: str | None = None
    current_level: str | None = None
    target_level: str | None = None
    goal: str | None = None
    status: str
    timezone: str
    weekly_minutes: int | None = None
    session_minutes: int | None = None
    correction_mode: str | None = None
    explanation_language: str | None = None
    interests: tuple[str, ...] = ()
    avoided_topics: tuple[str, ...] = ()
    accessibility_needs: tuple[str, ...] = ()
    voice_available: bool | None = None
    #: Consent is part of the context because it decides what the agent may ask for.
    transcript_retention_consent: bool | None = None
    audio_retention_consent: bool | None = None


class ProvenanceSection(ContractModel):
    pack_key: str | None = None
    pack_version: str | None = None
    pack_maturity: str | None = None
    pack_checksum: str | None = None
    core_schema_version: int
    aggregation_version: str
    estimate_calculation_version: str
    error_policy_version: str
    #: How much of the material behind this bundle is reviewed, by lifecycle.
    content_lifecycles: dict[str, int] = Field(default_factory=dict)


class DueItem(ContractModel):
    content_id: str
    stable_key: str
    title: str
    stage: str
    confidence: float
    due_at: str | None = None


class CurriculumPosition(ContractModel):
    curriculum_id: str
    title: str
    version: str
    completed_units: int
    total_units: int
    current_unit: str | None = None
    confirmed_gaps: int = 0


class ExposureSummary(ContractModel):
    """What the learner has already met, so an assessment does not retest it."""

    tasks_exposed: int
    tasks_answered: int
    anchors: int
    reuse_window_months: int
    last_exposed_at: str | None = None


class SourceCandidate(ContractModel):
    content_id: str
    stable_key: str
    title: str
    kind: str | None = None
    level: str | None = None
    rights_status: str | None = None
    #: The licence the pack recorded. What may be quoted follows from it, and a bundle
    #: never carries more of a source than the pack's own source policy permits.
    license: str | None = None


class ContextBundle(ContractModel):
    scope: str
    track_id: str
    generated_at: str
    #: The limits this bundle was built under, so an agent can ask for a larger one.
    #: `record_limit` bounds `records`, which counts every section but `provenance` --
    #: one row of version and pack metadata that is what makes the rest traceable.
    record_limit: int
    token_limit: int
    records: int
    estimated_tokens: int
    bounded: bool
    sections: tuple[str, ...] = ()
    omissions: tuple[Omission, ...] = ()
    profile: ProfileSection | None = None
    estimates: tuple[estimate_service.EstimateRecord, ...] = ()
    not_tested: tuple[str, ...] = ()
    active_errors: tuple[error_service.ErrorReport, ...] = ()
    due_work: tuple[error_service.FollowUpReport, ...] = ()
    due_items: tuple[DueItem, ...] = ()
    recent_evidence: tuple[evidence_service.EvidenceRecord, ...] = ()
    curriculum_position: tuple[CurriculumPosition, ...] = ()
    exposure: ExposureSummary | None = None
    sources: tuple[SourceCandidate, ...] = ()
    focus: knowledge_service.KnowledgeItemReport | None = None
    item_state: knowledge_service.ItemStateRecord | None = None
    provenance: ProvenanceSection | None = None
    #: Present only when the command asked for learner text and consent allowed it.
    responses_included: bool = False
    warnings: tuple[str, ...] = ()


def estimated_tokens(payload: Any) -> int:
    """A deterministic size estimate for a serialized payload."""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return -(-len(serialized) // CHARACTERS_PER_TOKEN)


def _profile(database: Database, *, track_id: str) -> ProfileSection:
    record = learner_service.track_context(database, track_id)
    preferences = record.preferences
    display = database.scalar("SELECT display_name FROM users WHERE user_id = ?", [record.user_id])

    def string_tuple(key: str) -> tuple[str, ...]:
        value = preferences.get(key)
        return tuple(str(entry) for entry in value) if isinstance(value, list) else ()

    def optional_bool(key: str) -> bool | None:
        value = preferences.get(key)
        return value if isinstance(value, bool) else None

    def optional_int(key: str) -> int | None:
        value = preferences.get(key)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    return ProfileSection(
        track_id=track_id,
        user_display_name="" if display is None else str(display),
        target_language=record.target_language,
        script=record.script,
        framework_id=record.proficiency_framework,
        framework_levels=record.framework_levels,
        declared_level=record.declared_level,
        current_level=record.current_level,
        target_level=record.target_level,
        goal=record.goal,
        status=record.status,
        timezone=record.timezone,
        weekly_minutes=record.weekly_minutes,
        session_minutes=optional_int("session_minutes"),
        correction_mode=(
            str(preferences["correction_mode"])
            if isinstance(preferences.get("correction_mode"), str)
            else None
        ),
        explanation_language=(
            str(preferences["explanation_language"])
            if isinstance(preferences.get("explanation_language"), str)
            else None
        ),
        interests=string_tuple("interests"),
        avoided_topics=string_tuple("avoided_topics"),
        accessibility_needs=string_tuple("accessibility_needs"),
        voice_available=optional_bool("voice_available"),
        transcript_retention_consent=optional_bool("transcript_retention_consent"),
        audio_retention_consent=optional_bool("audio_retention_consent"),
    )


def _provenance(database: Database, *, track_id: str) -> ProvenanceSection:
    from linguawiki.db import migrations as migration_module
    from linguawiki.error_model import POLICY_VERSION
    from linguawiki.mastery import AGGREGATION_VERSION

    record = learner_service.track_context(database, track_id)
    checksum = (
        None
        if record.pack_id is None
        else database.scalar(
            "SELECT checksum FROM pack_installations WHERE pack_id = ?", [record.pack_id]
        )
    )
    lifecycles = {
        str(lifecycle): int(total)
        for lifecycle, total in database.query(
            "SELECT lifecycle, count(*) FROM content_records WHERE pack_id = ? OR track_id = ? "
            "GROUP BY lifecycle ORDER BY lifecycle",
            [record.pack_id, track_id],
        )
    }
    return ProvenanceSection(
        pack_key=record.pack_key,
        pack_version=record.pack_version,
        pack_maturity=record.pack_maturity,
        pack_checksum=None if checksum is None else str(checksum),
        core_schema_version=migration_module.applied_version(database),
        aggregation_version=AGGREGATION_VERSION,
        estimate_calculation_version=estimate_service.CALCULATION_VERSION,
        error_policy_version=POLICY_VERSION,
        content_lifecycles=lifecycles,
    )


def _due_items(database: Database, *, track_id: str, limit: int) -> tuple[DueItem, ...]:
    """Items the learner has met that are not yet stable, weakest and oldest first."""

    return tuple(
        DueItem(
            content_id=str(row[0]),
            stable_key=str(row[1]),
            title=str(row[2]),
            stage=str(row[3]),
            confidence=float(row[4]),
            due_at=None if row[5] is None else aware_utc(row[5]).isoformat(),
        )
        for row in database.query(
            "SELECT state.content_id, record.stable_key, item.title, state.stage, "
            "state.confidence, state.next_review_at FROM track_item_state state "
            "JOIN knowledge_items item ON item.content_id = state.content_id "
            "JOIN content_records record ON record.content_id = state.content_id "
            "WHERE state.track_id = ? AND state.stage NOT IN ('unseen', 'stable') "
            "ORDER BY state.confidence, coalesce(state.last_encounter_at, state.updated_at), "
            "state.content_id LIMIT ?",
            [track_id, limit],
        )
    )


def _curriculum_position(
    database: Database, *, track_id: str, limit: int
) -> tuple[CurriculumPosition, ...]:
    return tuple(
        CurriculumPosition(
            curriculum_id=str(row[0]),
            title=str(row[1]),
            version=str(row[2]),
            completed_units=int(row[3]),
            total_units=int(row[4]),
            current_unit=None if row[5] is None else str(row[5]),
            confirmed_gaps=int(row[6]),
        )
        for row in database.query(
            "SELECT course.curriculum_id, course.title, course.version, "
            "  (SELECT count(*) FROM track_curriculum_progress progress "
            "   JOIN curriculum_units unit ON unit.unit_id = progress.unit_id "
            "   WHERE progress.track_id = ? AND unit.curriculum_id = course.curriculum_id "
            "   AND progress.state = 'claimed-complete'), "
            "  (SELECT count(*) FROM curriculum_units unit "
            "   WHERE unit.curriculum_id = course.curriculum_id), "
            "  (SELECT min(unit.code) FROM track_curriculum_progress progress "
            "   JOIN curriculum_units unit ON unit.unit_id = progress.unit_id "
            "   WHERE progress.track_id = ? AND unit.curriculum_id = course.curriculum_id "
            "   AND progress.state = 'in-progress'), "
            "  (SELECT count(*) FROM curriculum_audit_items audit_item "
            "   JOIN curriculum_audits audit ON audit.audit_id = audit_item.audit_id "
            "   WHERE audit.track_id = ? AND audit_item.outcome = 'incorrect') "
            "FROM curricula course ORDER BY course.curriculum_id LIMIT ?",
            [track_id, track_id, track_id, limit],
        )
    )


def _exposure(database: Database, *, track_id: str) -> ExposureSummary:
    from linguawiki.placement import REUSE_WINDOW_MONTHS

    row = database.one(
        "SELECT count(*), coalesce(sum(answered_count), 0), "
        "coalesce(sum(CASE WHEN is_anchor THEN 1 ELSE 0 END), 0), max(last_exposed_at) "
        "FROM assessment_item_exposures WHERE track_id = ?",
        [track_id],
    )
    assert row is not None
    return ExposureSummary(
        tasks_exposed=int(row[0]),
        tasks_answered=int(row[1]),
        anchors=int(row[2]),
        reuse_window_months=REUSE_WINDOW_MONTHS,
        last_exposed_at=None if row[3] is None else aware_utc(row[3]).isoformat(),
    )


def _sources(database: Database, *, track_id: str, limit: int) -> tuple[SourceCandidate, ...]:
    """The recommendations a pack ships, with what may be quoted from each.

    Reading and listening *progress* arrives with the source pipeline in a later stage.
    What exists now is what a pack recommends and the rights it records, which is what an
    agent needs before it proposes material.
    """

    record = learner_service.track_context(database, track_id)
    return tuple(
        SourceCandidate(
            content_id=str(row[0]),
            stable_key=str(row[1]),
            title=str(row[2]),
            kind=None if row[3] is None else str(row[3]),
            level=None if row[4] is None else str(row[4]),
            rights_status=None if row[5] is None else str(row[5]),
            license=None if row[6] is None else str(row[6]),
        )
        for row in database.query(
            "SELECT recommendation.recommendation_id, record.stable_key, "
            "recommendation.title, recommendation.modality, recommendation.level_code, "
            "recommendation.rights_status, recommendation.license "
            "FROM source_recommendations recommendation "
            "JOIN content_records record "
            "  ON record.content_id = recommendation.recommendation_id "
            "WHERE recommendation.pack_id = ? ORDER BY record.stable_key LIMIT ?",
            [record.pack_id, limit],
        )
    )


def _recent_evidence(
    database: Database,
    *,
    track_id: str,
    content_id: str | None,
    limit: int,
    include_responses: bool,
) -> tuple[tuple[evidence_service.EvidenceRecord, ...], int]:
    """The newest evidence rows, and how many exist in total.

    Evidence carries no learner text at all: the claim, its strength, and the conditions
    are what justify it, and the words live on the attempt behind it under their own
    visibility rule. `include_responses` therefore changes nothing here, and is reported
    on the bundle so an agent knows the words are not simply missing.
    """

    del include_responses
    conditions = ["track_id = ?"]
    parameters: list[Any] = [track_id]
    if content_id is not None:
        conditions.append("target_content_id = ?")
        parameters.append(content_id)
    where = " AND ".join(conditions)
    total = int(database.scalar(f"SELECT count(*) FROM evidence WHERE {where}", parameters))
    rows = database.query(
        "SELECT evidence_id, claim, polarity, strength, target_content_id, dimension, "
        "context_key, novelty, retrieval, help_level, modality, task_type, occurred_at "
        f"FROM evidence WHERE {where} ORDER BY occurred_at DESC, evidence_id DESC LIMIT ?",
        [*parameters, limit],
    )
    return (
        tuple(
            evidence_service.EvidenceRecord(
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
        ),
        total,
    )


def _active_errors(
    database: Database, *, track_id: str, content_id: str | None, limit: int, redact: bool
) -> tuple[tuple[error_service.ErrorReport, ...], int]:
    """Live error patterns, with their occurrence history redacted unless allowed.

    The pattern's signature and description are the pedagogical facts an agent needs.
    The learner's actual words are on the occurrences, so those are dropped when the
    command has not asked for learner text or consent has not been given.
    """

    from linguawiki.error_model import LIVE_STATUSES

    placeholders = ", ".join("?" for _ in LIVE_STATUSES)
    conditions = [f"status IN ({placeholders})", "track_id = ?"]
    parameters: list[Any] = [*LIVE_STATUSES, track_id]
    if content_id is not None:
        conditions.append("target_content_id = ?")
        parameters.append(content_id)
    where = " AND ".join(conditions)
    total = int(database.scalar(f"SELECT count(*) FROM error_patterns WHERE {where}", parameters))
    identities = [
        str(error_id)
        for (error_id,) in database.query(
            f"SELECT error_id FROM error_patterns WHERE {where} "
            "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "occurrence_count DESC, last_seen_at DESC, error_id LIMIT ?",
            [*parameters, limit],
        )
    ]
    reports = tuple(
        error_service.read_error(database, error_id=identity, include_history=not redact)
        for identity in identities
    )
    return (reports, total)


def _assert_response_consent(profile: ProfileSection, *, include_responses: bool) -> bool:
    """Refuse to include learner text without both the request and the consent."""

    if not include_responses:
        return False
    if profile.transcript_retention_consent is not True:
        raise LinguaWikiError(
            "transcript_consent_required",
            "this bundle was asked to include the learner's own words, and the track has "
            "not consented to retaining them; build the bundle without --include-responses",
            details=(ErrorDetail(field="include_responses", reason="transcript consent absent"),),
        )
    return True


def _ordered_omissions(recorded: dict[str, Omission], *, budget: Budget) -> tuple[Omission, ...]:
    """Omissions in the scope's own section order, so the listing is stable."""

    order = {name: index for index, name in enumerate(budget.sections)}
    return tuple(
        sorted(recorded.values(), key=lambda omission: order.get(omission.section, len(order)))
    )


#: How each section's rows are counted, and which bundle fields hold them.
SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "profile": ("profile",),
    "estimates": ("estimates",),
    "active_errors": ("active_errors",),
    "due_work": ("due_work", "due_items"),
    "recent_evidence": ("recent_evidence",),
    "curriculum_position": ("curriculum_position",),
    "exposure": ("exposure",),
    "sources": ("sources",),
    "focus": ("focus",),
    "item_state": ("item_state",),
    "provenance": ("provenance",),
}


def _section_records(bundle: ContextBundle, section: str) -> int:
    """How many records one section contributes to the bundle's total."""

    total = 0
    for attribute in SECTION_FIELDS.get(section, ()):
        value = getattr(bundle, attribute, None)
        total += len(value) if isinstance(value, tuple) else (0 if value is None else 1)
    return total


def _trim_records(bundle: ContextBundle, *, budget: Budget, record_limit: int) -> ContextBundle:
    """Bring the bundle within its record limit, dropping from the least important end.

    Per-section allowances alone did not bound the whole bundle: several sections hold
    two lists, and each allowance was independent, so a bundle asked for one record
    returned five. The limit is a promise about the bundle, so it is enforced against the
    assembled bundle rather than hoped for section by section.

    `provenance` is exempt and says so: it is what makes the rest traceable, it is a
    single record, and a bundle nobody can trace is not one to act on.
    """

    recorded = {omission.section: omission for omission in bundle.omissions}
    kept = list(bundle.sections)
    updates: dict[str, Any] = {}
    droppable = [name for name in reversed(kept) if name != "provenance"]
    total = sum(_section_records(bundle, name) for name in kept if name != "provenance")
    for name in droppable:
        if total <= record_limit:
            break
        included = _section_records(bundle, name)
        prior = recorded.get(name)
        available = max(included, prior.available if prior is not None else 0)
        for attribute in SECTION_FIELDS.get(name, ()):
            current = getattr(bundle, attribute, None)
            updates[attribute] = () if isinstance(current, tuple) else None
        kept.remove(name)
        total -= included
        recorded[name] = Omission(
            section=name,
            available=available,
            included=0,
            reason=(
                f"the bundle's {record_limit}-record limit was reached, so this section "
                "was dropped whole rather than truncated"
            ),
        )
    return bundle.model_copy(
        update={
            **updates,
            "sections": tuple(kept),
            "omissions": _ordered_omissions(recorded, budget=budget),
            "bounded": bundle.bounded or bool(updates),
        }
    )


def _trim(bundle: ContextBundle, *, budget: Budget, token_limit: int) -> ContextBundle:
    """Drop whole sections from the least important end until the bundle fits.

    Sections are dropped rather than truncated inside: half an error list looks exactly
    like a short one, and an agent cannot tell the difference. A dropped section is
    reported in `omissions` with everything that was in it.
    """

    payload = bundle.model_dump(mode="json")
    tokens = estimated_tokens(payload)
    if tokens <= token_limit:
        return bundle.model_copy(update={"estimated_tokens": tokens})
    # One authoritative omission per section. A section already capped by the record limit
    # has an omission recording how many *existed*; dropping it whole must replace that
    # entry rather than adding a second one that reports the capped count as the total.
    recorded = {omission.section: omission for omission in bundle.omissions}
    # `not_tested` travels with the estimates: a list of dimensions nobody measured is
    # meaningless once the estimates it qualifies are gone.
    fields: dict[str, tuple[str, ...]] = {
        **SECTION_FIELDS,
        "estimates": ("estimates", "not_tested"),
    }
    kept = list(budget.sections)
    updates: dict[str, Any] = {}
    # Never drop provenance: a bundle whose material cannot be traced is not one an
    # agent should act on at all, and it is small.
    droppable = [name for name in reversed(kept) if name != "provenance"]
    for name in droppable:
        section = getattr(bundle, name, None)
        included = len(section) if isinstance(section, tuple) else (0 if section is None else 1)
        prior = recorded.get(name)
        available = max(included, prior.available if prior is not None else 0)
        for attribute in fields.get(name, ()):
            current = getattr(bundle, attribute, None)
            updates[attribute] = () if isinstance(current, tuple) else None
        kept.remove(name)
        recorded[name] = Omission(
            section=name,
            available=available,
            included=0,
            reason=(
                f"the bundle exceeded its {token_limit}-token budget, so this section "
                "was dropped whole rather than truncated"
            ),
        )
        candidate = bundle.model_copy(
            update={
                **updates,
                "sections": tuple(kept),
                "omissions": _ordered_omissions(recorded, budget=budget),
            }
        )
        tokens = estimated_tokens(candidate.model_dump(mode="json"))
        if tokens <= token_limit:
            return candidate.model_copy(update={"estimated_tokens": tokens, "bounded": True})
    # Everything droppable is gone and it still does not fit. Say so: an agent that
    # believes it has a session bundle when it holds only provenance will plan from
    # nothing and not know it.
    return bundle.model_copy(
        update={
            **updates,
            "sections": tuple(kept),
            "omissions": _ordered_omissions(recorded, budget=budget),
            "estimated_tokens": tokens,
            "bounded": True,
            "warnings": (
                *bundle.warnings,
                f"the {token_limit}-token budget could not be met; every section but "
                f"provenance was dropped and the bundle is still {tokens} token(s). "
                "Raise --max-tokens.",
            ),
        }
    )


def _record_count(bundle: ContextBundle) -> int:
    """Every learner record the bundle carries, counted as the record limit counts them.

    Derived from the same section map the trimming uses, so the number reported and the
    number bounded cannot drift apart -- reporting a total the limit did not govern is
    what made a bundle look unbounded.

    `provenance` is excluded from both, and `record_limit` says so: it is one row of
    version and pack metadata, it is what makes every other record traceable, and a
    limit that could remove it would produce a bundle nobody should act on.
    """

    return sum(
        _section_records(bundle, section) for section in bundle.sections if section != "provenance"
    )


def build(
    paths: WorkspacePaths,
    *,
    scope: str,
    track: str | None = None,
    item: str | None = None,
    max_records: int | None = None,
    max_tokens: int | None = None,
    include_responses: bool = False,
    clock: Clock | None = None,
) -> ContextBundle:
    """Assemble one bounded bundle for a scope, reporting whatever it left out."""

    if scope not in SCOPES:
        raise LinguaWikiError(
            "unknown_context_scope",
            f"{scope} is not a context scope; expected one of {list(SCOPES)}",
            details=(ErrorDetail(field="scope", reason="unknown scope"),),
        )
    budget = BUDGETS[scope]
    record_limit = budget.records if max_records is None else max_records
    token_limit = budget.tokens if max_tokens is None else max_tokens
    if not 1 <= record_limit <= MAXIMUM_RECORDS:
        raise LinguaWikiError(
            "invalid_record_limit",
            f"a record limit is between 1 and {MAXIMUM_RECORDS}",
            details=(ErrorDetail(field="max_records", reason=str(record_limit)),),
        )
    if not 200 <= token_limit <= MAXIMUM_TOKENS:
        raise LinguaWikiError(
            "invalid_token_limit",
            f"a token budget is between 200 and {MAXIMUM_TOKENS}",
            details=(ErrorDetail(field="max_tokens", reason=str(token_limit)),),
        )
    if scope == "concept" and item is None:
        raise LinguaWikiError(
            "context_item_required",
            "a concept bundle is about one knowledge item; name it with --item",
            details=(ErrorDetail(field="item", reason="no item named"),),
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        profile = _profile(database, track_id=track_id)
        responses = _assert_response_consent(profile, include_responses=include_responses)
        content_id = (
            None
            if item is None
            else knowledge_service.resolve_item(database, item, track_id=track_id)
        )
        # Each section's share of the record cap, so one long list cannot crowd out the
        # rest before the token budget is even consulted.
        share = max(1, record_limit // max(1, len(budget.sections)))
        omissions: list[Omission] = []
        warnings: list[str] = []
        payload: dict[str, Any] = {}
        if "profile" in budget.sections:
            payload["profile"] = profile
        if "estimates" in budget.sections:
            stored = estimate_service.read_estimates(database, track_id=track_id)
            declared = estimate_service.dimensions_for(database, track_id=track_id)
            known = {entry.dimension for entry in stored}
            payload["estimates"] = stored[:record_limit]
            payload["not_tested"] = tuple(
                sorted(
                    {entry.dimension for entry in stored if entry.estimate_status == "not-tested"}
                    | (set(declared) - known)
                )
            )
            if len(stored) > record_limit:
                omissions.append(
                    Omission(
                        section="estimates",
                        available=len(stored),
                        included=record_limit,
                        reason="the record limit was reached",
                    )
                )
        if "active_errors" in budget.sections:
            reports, total = _active_errors(
                database,
                track_id=track_id,
                content_id=content_id,
                limit=share,
                redact=not responses,
            )
            payload["active_errors"] = reports
            if total > len(reports):
                omissions.append(
                    Omission(
                        section="active_errors",
                        available=total,
                        included=len(reports),
                        reason=(
                            f"{total} live error(s) exist and the highest-severity "
                            f"{len(reports)} are shown"
                        ),
                    )
                )
            if not responses and reports:
                warnings.append(
                    "error occurrences are omitted: they hold the learner's own words, "
                    "which need --include-responses and transcript consent"
                )
        if "due_work" in budget.sections:
            follow_ups = error_service.followups(
                database, track_id=track_id, status="open", limit=share
            )
            total_followups = int(
                database.scalar(
                    "SELECT count(*) FROM followups WHERE track_id = ? AND status = 'open'",
                    [track_id],
                )
            )
            payload["due_work"] = follow_ups
            payload["due_items"] = _due_items(database, track_id=track_id, limit=share)
            if total_followups > len(follow_ups):
                omissions.append(
                    Omission(
                        section="due_work",
                        available=total_followups,
                        included=len(follow_ups),
                        reason="the section's share of the record limit was reached",
                    )
                )
        if "recent_evidence" in budget.sections:
            entries, total = _recent_evidence(
                database,
                track_id=track_id,
                content_id=content_id,
                limit=share,
                include_responses=responses,
            )
            payload["recent_evidence"] = entries
            if total > len(entries):
                omissions.append(
                    Omission(
                        section="recent_evidence",
                        available=total,
                        included=len(entries),
                        reason=f"the newest {len(entries)} of {total} observation(s) are shown",
                    )
                )
        if "curriculum_position" in budget.sections:
            payload["curriculum_position"] = _curriculum_position(
                database, track_id=track_id, limit=share
            )
        if "exposure" in budget.sections:
            payload["exposure"] = _exposure(database, track_id=track_id)
        if "sources" in budget.sections:
            candidates = _sources(database, track_id=track_id, limit=share)
            total_sources = int(
                database.scalar(
                    "SELECT count(*) FROM source_recommendations WHERE pack_id = ?",
                    [learner_service.track_context(database, track_id).pack_id],
                )
            )
            payload["sources"] = candidates
            if total_sources > len(candidates):
                omissions.append(
                    Omission(
                        section="sources",
                        available=total_sources,
                        included=len(candidates),
                        reason="the section's share of the record limit was reached",
                    )
                )
        if "focus" in budget.sections and content_id is not None:
            payload["focus"] = knowledge_service.read_item(database, content_id, track_id=track_id)
        if "item_state" in budget.sections and content_id is not None:
            payload["item_state"] = knowledge_service.item_state(
                database, track_id=track_id, content_id=content_id
            )
        if "provenance" in budget.sections:
            payload["provenance"] = _provenance(database, track_id=track_id)
        generated_at = aware_utc(database.now()).isoformat()
    bundle = ContextBundle(
        scope=scope,
        track_id=track_id,
        generated_at=generated_at,
        record_limit=record_limit,
        token_limit=token_limit,
        records=0,
        estimated_tokens=0,
        bounded=bool(omissions),
        sections=budget.sections,
        omissions=tuple(omissions),
        responses_included=responses,
        warnings=tuple(warnings),
        **payload,
    )
    # Records first, then tokens: both are promises about the assembled bundle, and a
    # section dropped for one need not be re-examined for the other.
    bundle = _trim_records(bundle, budget=budget, record_limit=record_limit)
    bundle = _trim(bundle, budget=budget, token_limit=token_limit)
    return bundle.model_copy(update={"records": _record_count(bundle)})


__all__ = [
    "BUDGETS",
    "CHARACTERS_PER_TOKEN",
    "MAXIMUM_RECORDS",
    "MAXIMUM_TOKENS",
    "SCOPES",
    "Budget",
    "ContextBundle",
    "CurriculumPosition",
    "DueItem",
    "ExposureSummary",
    "Omission",
    "ProfileSection",
    "ProvenanceSection",
    "SourceCandidate",
    "build",
    "estimated_tokens",
]
