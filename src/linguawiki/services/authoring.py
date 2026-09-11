"""Pack authoring: bounded drafting batches, per-axis review, and quarantine.

This is author tooling, not a lesson. Its job is to make AI-assisted drafting safe rather
than to make it easy, so four rules are enforced here and not left to discipline:

- **drafting never approves.** `generate_draft` opens a run and a batch with its sampling
  policy already fixed; `import_items` records items as `draft`. Promotion happens only
  through `approve`, which re-runs the promotion gate.
- **a machine reviewer has a ceiling.** An AI or machine reviewer can reach
  `machine-checked` and no further, however many times it runs.
- **one defect condemns the batch.** A substantive defect found in a sample quarantines the
  whole batch, marks its template for revalidation, and invalidates everything that
  depends on the batch's items -- because a template that produced one bad item has no
  claim to the rest.
- **a reviewer cannot be their own second opinion.** Publication-ready AI-origin content
  needs different identities on the linguistic and pedagogical axes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from linguawiki.clock import Clock, SystemClock
from linguawiki.contracts import (
    PackItemOrigin,
    PackItemReview,
    PackManifest,
    ReviewerKind,
    canonical_content_hash,
)
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import ContentId, EventId
from linguawiki.models import ContractModel
from linguawiki.packs.format import KNOWLEDGE_KIND, content_id_for
from linguawiki.paths import WorkspacePaths
from linguawiki.provenance import (
    CONTENT_ORIGIN_BY_CLASS,
    MACHINE_CEILING,
    MACHINE_REVIEWER_KINDS,
    PROMOTED_LIFECYCLES,
    REVIEW_AXES,
    SamplingPolicy,
    axis_states,
    gate_problems,
    is_known_state,
    required_strengths,
    sample_size,
    sampling_policy,
    state_strength,
)
from linguawiki.services import packs as pack_service

#: Lifecycles the review queue considers unfinished work.
QUEUE_LIFECYCLES = ("draft", "candidate", "needs-review")
DEFAULT_QUEUE_LIMIT = 25
#: The promotion an unfinished item is measured against in the review queue.
DEFAULT_PROMOTION_TARGET = "approved-personal"


class TemplateInput(ContractModel):
    """`pack template validate --input`: one versioned generation or review template."""

    schema_name: Literal["lingua.pack.template.v1"] = "lingua.pack.template.v1"
    schema_version: Literal[1] = 1
    template_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    version: int = Field(ge=1)
    purpose: Literal["generation", "review"]
    intended_kinds: tuple[str, ...] = Field(min_length=1)
    body: str = Field(min_length=1)
    known_failure_modes: tuple[str, ...] = ()


class DraftItemInput(ContractModel):
    """One drafted knowledge item entering a batch. It arrives as a draft, always."""

    stable_key: str
    kind: str
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    summary: str | None = None
    level: str = Field(min_length=1)
    themes: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    risk_tier: int = Field(ge=0, le=4)
    dependencies: tuple[str, ...] = ()
    source_references: tuple[str, ...] = ()


class TemplateReport(ContractModel):
    template_id: str
    template_key: str
    version: int
    purpose: str
    maturity: str
    intended_kinds: tuple[str, ...]
    inspected_runs: int
    runs: int
    batches: int
    quarantined_batches: int
    quarantine_reason: str | None
    known_failure_modes: tuple[str, ...]
    sampling_policy: str
    warnings: tuple[str, ...] = ()


class BatchReport(ContractModel):
    batch_id: str
    generation_run_id: str
    template_key: str
    template_version: int
    run_ordinal: int
    status: str
    sampling_policy: str
    planned_items: int
    item_count: int
    required_sample: int
    inspected_count: int
    defect_count: int
    #: Whether this batch could still reach acceptance. False once it holds fewer items
    #: than the duty fixed when it opened.
    acceptable: bool = True
    quarantine_reason: str | None = None
    items: tuple[str, ...] = ()
    invalidated: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ReviewAxisReport(ContractModel):
    axis: str
    state: str | None
    required_state: str
    reviewer_kind: str | None = None
    reviewer: str | None = None
    method: str | None = None
    satisfied: bool
    bound_to_current_hash: bool


class ContentReviewReport(ContractModel):
    content_id: str
    stable_key: str
    content_kind: str
    lifecycle: str
    #: The lifecycle the reported gate problems are measured against.
    promotion_target: str
    risk_tier: int
    content_hash: str
    quarantined: bool
    invalidation_reason: str | None
    batch_id: str | None
    origins: tuple[str, ...]
    axes: tuple[ReviewAxisReport, ...]
    gate_problems: tuple[str, ...] = ()
    dependency_centrality: int = 0
    warnings: tuple[str, ...] = ()


class ReviewQueueReport(ContractModel):
    pack_key: str
    total: int
    returned: int
    items: tuple[ContentReviewReport, ...]
    axis_debt: dict[str, int] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class InvalidationReport(ContractModel):
    reason: str
    roots: tuple[str, ...]
    invalidated: tuple[str, ...]
    quarantined_batches: tuple[str, ...] = ()
    quarantined_templates: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _template_row(database: Database, template_key: str, version: int) -> Sequence[Any]:
    row = database.one(
        "SELECT template_id, template_key, version, purpose, intended_kinds_json, body, "
        "body_sha256, maturity, known_failure_modes_json, inspected_runs, quarantine_reason "
        "FROM prompt_templates WHERE template_key = ? AND version = ?",
        [template_key, version],
    )
    if row is None:
        raise LinguaWikiError(
            "template_not_found",
            f"no prompt template {template_key} version {version}",
            details=(ErrorDetail(field="template", reason="unknown template"),),
        )
    return row


def _template_report(database: Database, template_key: str, version: int) -> TemplateReport:
    row = _template_row(database, template_key, version)
    template_id = str(row[0])
    runs = int(
        database.scalar("SELECT count(*) FROM generation_runs WHERE template_id = ?", [template_id])
    )
    batches = database.query(
        "SELECT status, count(*) FROM generation_batches WHERE template_id = ? GROUP BY status",
        [template_id],
    )
    by_status = {str(status): int(count) for status, count in batches}
    policy = sampling_policy(template_maturity=str(row[7]), inspected_runs=int(row[9]))
    warnings: list[str] = []
    if str(row[7]) == "quarantined":
        warnings.append(f"{template_key} v{version} is quarantined and cannot draft")
    elif policy is SamplingPolicy.FULL_INSPECTION:
        warnings.append(
            f"{template_key} v{version} is not stable: every persistent item of the next "
            "batch has to be inspected"
        )
    return TemplateReport(
        template_id=template_id,
        template_key=str(row[1]),
        version=int(row[2]),
        purpose=str(row[3]),
        maturity=str(row[7]),
        intended_kinds=tuple(json.loads(str(row[4]))),
        inspected_runs=int(row[9]),
        runs=runs,
        batches=sum(by_status.values()),
        quarantined_batches=by_status.get("quarantined", 0),
        quarantine_reason=None if row[10] is None else str(row[10]),
        known_failure_modes=tuple(json.loads(str(row[8]))),
        sampling_policy=str(policy),
        warnings=tuple(warnings),
    )


def validate_template(
    paths: WorkspacePaths,
    *,
    payload: Mapping[str, Any] | None = None,
    template_key: str | None = None,
    version: int | None = None,
    clock: Clock | None = None,
    command: str = "pack.template.validate",
) -> TemplateReport:
    """Register or re-validate one versioned template.

    A template body is content-addressed: changing the text of a registered version is
    refused, because the sampling history is a claim about *that* text.
    """

    active_clock = clock or SystemClock()
    if payload is None:
        if template_key is None or version is None:
            raise LinguaWikiError(
                "invalid_arguments", "give --input, or both --template-key and --version"
            )
        with open_reader(paths, clock=active_clock) as database:
            return _template_report(database, template_key, version)
    document = TemplateInput.model_validate(payload)
    digest = hashlib.sha256(document.body.encode("utf-8")).hexdigest()
    with open_writer(paths, command=command, clock=active_clock) as database:
        existing = database.one(
            "SELECT template_id, body_sha256 FROM prompt_templates WHERE template_key = ? "
            "AND version = ?",
            [document.template_key, document.version],
        )
        if existing is not None and str(existing[1]) != digest:
            raise LinguaWikiError(
                "template_body_changed",
                f"{document.template_key} v{document.version} is registered with different "
                "text; a materially changed template needs a new version so its sampling "
                "history starts again",
                details=(
                    ErrorDetail(
                        field="body",
                        reason="body hash differs",
                        context={"registered": str(existing[1])[:12], "incoming": digest[:12]},
                    ),
                ),
            )
        if existing is None:
            template_id = str(
                ContentId.derive("prompt-template", document.template_key, str(document.version))
            )
            with database.transaction() as transaction:
                now = transaction.now()
                transaction.execute(
                    "INSERT INTO prompt_templates (template_id, template_key, version, purpose, "
                    "intended_kinds_json, body, body_sha256, maturity, "
                    "known_failure_modes_json, inspected_runs, quarantine_reason, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, 0, NULL, ?, ?)",
                    [
                        template_id,
                        document.template_key,
                        document.version,
                        document.purpose,
                        json.dumps(list(document.intended_kinds)),
                        document.body,
                        digest,
                        json.dumps(list(document.known_failure_modes)),
                        now,
                        now,
                    ],
                )
                migration_module.record_audit_entry(
                    transaction,
                    command=command,
                    correlation_id=EventId.new(),
                    outcome="succeeded",
                    affected_records_json=json.dumps([template_id]),
                    after_summary=f"registered template {document.template_key} "
                    f"v{document.version}",
                )
        else:
            with database.transaction() as transaction:
                transaction.execute(
                    "UPDATE prompt_templates SET known_failure_modes_json = ?, updated_at = ? "
                    "WHERE template_key = ? AND version = ?",
                    [
                        json.dumps(list(document.known_failure_modes)),
                        transaction.now(),
                        document.template_key,
                        document.version,
                    ],
                )
        return _template_report(database, document.template_key, document.version)


def stabilize_template(
    paths: WorkspacePaths,
    *,
    template_key: str,
    version: int,
    clock: Clock | None = None,
    command: str = "pack.template.stabilize",
) -> TemplateReport:
    """Mark a template stable, once three defect-free fully inspected runs exist."""

    active_clock = clock or SystemClock()
    from linguawiki.provenance import FULL_INSPECTION_RUNS

    with open_writer(paths, command=command, clock=active_clock) as database:
        row = _template_row(database, template_key, version)
        template_id = str(row[0])
        if str(row[7]) == "quarantined":
            raise LinguaWikiError(
                "template_quarantined",
                f"{template_key} v{version} is quarantined; revalidate it as a new version",
                details=(ErrorDetail(field="template", reason="template is quarantined"),),
            )
        inspected = int(row[9])
        defects = int(
            database.scalar(
                "SELECT coalesce(sum(defect_count), 0) FROM generation_batches "
                "WHERE template_id = ?",
                [template_id],
            )
        )
        if inspected < FULL_INSPECTION_RUNS or defects:
            raise LinguaWikiError(
                "template_not_stabilizable",
                f"{template_key} v{version} needs {FULL_INSPECTION_RUNS} fully inspected, "
                f"defect-free runs; it has {inspected} inspected run(s) and {defects} defect(s)",
                details=(
                    ErrorDetail(
                        field="template",
                        reason="sampling history is insufficient",
                        context={"inspected_runs": str(inspected), "defects": str(defects)},
                    ),
                ),
            )
        with database.transaction() as transaction:
            transaction.execute(
                "UPDATE prompt_templates SET maturity = 'stable', updated_at = ? "
                "WHERE template_id = ?",
                [transaction.now(), template_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([template_id]),
                before_summary=str(row[7]),
                after_summary="stable",
            )
        return _template_report(database, template_key, version)


def _dependents_of(database: Database, content_ids: Sequence[str]) -> list[str]:
    """Content that declares a dependency on any of these items."""

    if not content_ids:
        return []
    placeholders = ", ".join("?" for _ in content_ids)
    return [
        str(content_id)
        for (content_id,) in database.query(
            "SELECT DISTINCT content_id FROM content_dependencies "
            f"WHERE dependency_kind = 'content' AND dependency_ref IN ({placeholders}) "
            "AND on_change <> 'ignore' ORDER BY 1",
            list(content_ids),
        )
    ]


def _transitive_dependents(database: Database, roots: Sequence[str]) -> list[str]:
    """Every item reachable from these roots along declared dependencies."""

    seen: set[str] = set()
    frontier = list(roots)
    while frontier:
        batch = [item for item in frontier if item not in seen]
        seen.update(batch)
        frontier = [item for item in _dependents_of(database, batch) if item not in seen]
    return sorted(seen - set(roots))


def _quarantine_batch(
    database: Database, *, batch_id: str, reason: str
) -> tuple[list[str], list[str]]:
    """Quarantine a batch, its items, and everything that depends on them."""

    now = database.now()
    items = [
        str(content_id)
        for (content_id,) in database.query(
            "SELECT content_id FROM content_records WHERE batch_id = ? ORDER BY stable_key",
            [batch_id],
        )
    ]
    dependents = _transitive_dependents(database, items)
    database.execute(
        "UPDATE generation_batches SET status = 'quarantined', quarantine_reason = ?, "
        "updated_at = ? WHERE batch_id = ?",
        [reason, now, batch_id],
    )
    for content_id in items:
        database.execute(
            "UPDATE content_records SET lifecycle = 'needs-review', quarantined = TRUE, "
            "invalidation_reason = ?, updated_at = ? WHERE content_id = ?",
            [reason, now, content_id],
        )
    for content_id in dependents:
        database.execute(
            "UPDATE content_records SET lifecycle = 'needs-review', "
            "invalidation_reason = ?, updated_at = ? WHERE content_id = ?",
            [f"depends on quarantined content: {reason}", now, content_id],
        )
    template_id = database.scalar(
        "SELECT template_id FROM generation_batches WHERE batch_id = ?", [batch_id]
    )
    if template_id is not None:
        database.execute(
            "UPDATE prompt_templates SET maturity = 'quarantined', quarantine_reason = ?, "
            "updated_at = ? WHERE template_id = ?",
            [reason, now, str(template_id)],
        )
    return items, dependents


def quarantine_template(
    paths: WorkspacePaths,
    *,
    template_key: str,
    version: int,
    reason: str,
    clock: Clock | None = None,
    command: str = "pack.template.quarantine",
) -> InvalidationReport:
    """Quarantine a template and every batch and item that came out of it."""

    active_clock = clock or SystemClock()
    if not reason.strip():
        raise LinguaWikiError("invalid_arguments", "a quarantine needs a reason")
    with open_writer(paths, command=command, clock=active_clock) as database:
        row = _template_row(database, template_key, version)
        template_id = str(row[0])
        batches = [
            str(batch_id)
            for (batch_id,) in database.query(
                "SELECT batch_id FROM generation_batches WHERE template_id = ? ORDER BY batch_id",
                [template_id],
            )
        ]
        roots: list[str] = []
        invalidated: list[str] = []
        with database.transaction() as transaction:
            for batch_id in batches:
                items, dependents = _quarantine_batch(transaction, batch_id=batch_id, reason=reason)
                roots.extend(items)
                invalidated.extend(dependents)
            transaction.execute(
                "UPDATE prompt_templates SET maturity = 'quarantined', quarantine_reason = ?, "
                "updated_at = ? WHERE template_id = ?",
                [reason, transaction.now(), template_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([template_id]),
                after_summary=f"quarantined {template_key} v{version}: {reason}",
            )
        return InvalidationReport(
            reason=reason,
            roots=tuple(sorted(set(roots))),
            invalidated=tuple(sorted(set(invalidated))),
            quarantined_batches=tuple(batches),
            quarantined_templates=(f"{template_key}@{version}",),
        )


def _batch_row(database: Database, batch_id: str) -> Sequence[Any]:
    row = database.one(
        "SELECT batch.batch_id, batch.generation_run_id, batch.template_id, batch.status, "
        "batch.sampling_policy, batch.item_count, batch.required_sample, batch.inspected_count, "
        "batch.defect_count, batch.quarantine_reason, template.template_key, template.version, "
        "run.run_ordinal, batch.planned_items FROM generation_batches batch "
        "JOIN prompt_templates template ON template.template_id = batch.template_id "
        "JOIN generation_runs run ON run.generation_run_id = batch.generation_run_id "
        "WHERE batch.batch_id = ?",
        [batch_id],
    )
    if row is None:
        raise LinguaWikiError(
            "batch_not_found",
            f"no generation batch with ID {batch_id}",
            details=(ErrorDetail(field="batch", reason="unknown batch"),),
        )
    return row


def _batch_report(database: Database, batch_id: str) -> BatchReport:
    row = _batch_row(database, batch_id)
    items = tuple(
        str(content_id)
        for (content_id,) in database.query(
            "SELECT content_id FROM content_records WHERE batch_id = ? ORDER BY stable_key",
            [batch_id],
        )
    )
    warnings: list[str] = []
    status = str(row[3])
    planned, item_count, required, inspected = int(row[13]), int(row[5]), int(row[6]), int(row[7])
    # A batch that holds fewer items than it was opened for can never discharge the duty
    # it opened with, because the duty does not shrink to match the import. That is the
    # intended outcome of a filtered import, and it is stated rather than left implicit.
    underfilled = status != "draft" and item_count < planned
    if status == "quarantined":
        warnings.append(f"batch is quarantined: {row[9]}")
    elif status == "draft":
        warnings.append(f"the batch is open for up to {planned} item(s); import them to seal it")
    elif underfilled:
        warnings.append(
            f"the batch was opened for {planned} item(s) but holds {item_count}; its duty of "
            f"{required} inspection(s) can no longer be discharged, so it can never be "
            "accepted -- open a new batch sized to what you will import"
        )
    elif status == "sampling" and inspected < required:
        warnings.append(
            f"{required - inspected} more item(s) must be inspected before this batch may "
            "be accepted"
        )
    return BatchReport(
        batch_id=str(row[0]),
        generation_run_id=str(row[1]),
        template_key=str(row[10]),
        template_version=int(row[11]),
        run_ordinal=int(row[12]),
        status=status,
        sampling_policy=str(row[4]),
        planned_items=planned,
        item_count=item_count,
        required_sample=required,
        inspected_count=inspected,
        defect_count=int(row[8]),
        acceptable=status == "accepted" or (not underfilled and item_count >= required),
        quarantine_reason=None if row[9] is None else str(row[9]),
        items=items,
        warnings=tuple(warnings),
    )


def generate_draft(
    paths: WorkspacePaths,
    *,
    template_key: str,
    version: int,
    item_count: int,
    pack_key: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    privacy_class: str = "public",
    clock: Clock | None = None,
    command: str = "pack.author.generate-draft",
) -> BatchReport:
    """Open a bounded drafting batch with its sampling duty already fixed.

    Nothing is generated here: core holds no AI adapter. The batch records what the
    agent is allowed to draft and how much of the result must be inspected, so the
    inspection duty cannot be renegotiated after the output looks convincing.
    """

    active_clock = clock or SystemClock()
    if item_count < 1:
        raise LinguaWikiError("invalid_arguments", "item_count must be positive")
    if privacy_class not in ("public", "private", "synthetic"):
        raise LinguaWikiError(
            "invalid_arguments", "privacy_class must be public, private, or synthetic"
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        pack_row = pack_service.installed_pack(database, pack_key)
        row = _template_row(database, template_key, version)
        template_id = str(row[0])
        if str(row[7]) in ("quarantined", "retired"):
            raise LinguaWikiError(
                "template_quarantined",
                f"{template_key} v{version} is {row[7]} and may not draft",
                details=(ErrorDetail(field="template", reason=str(row[7])),),
            )
        if str(row[3]) != "generation":
            raise LinguaWikiError(
                "template_not_generative",
                f"{template_key} v{version} is a {row[3]} template",
                details=(ErrorDetail(field="template", reason="wrong purpose"),),
            )
        policy = sampling_policy(template_maturity=str(row[7]), inspected_runs=int(row[9]))
        required = sample_size(policy=policy, item_count=item_count)
        ordinal = (
            int(
                database.scalar(
                    "SELECT coalesce(max(run_ordinal), 0) FROM generation_runs "
                    "WHERE template_id = ?",
                    [template_id],
                )
            )
            + 1
        )
        run_id = str(EventId.new())
        batch_id = str(EventId.new())
        parameters_hash = hashlib.sha256(
            json.dumps(dict(parameters or {}), sort_keys=True).encode("utf-8")
        ).hexdigest()
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO generation_runs (generation_run_id, template_id, provider, model, "
                "model_version, parameters_hash, privacy_class, run_ordinal, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    template_id,
                    provider,
                    model,
                    model_version,
                    parameters_hash,
                    privacy_class,
                    ordinal,
                    now,
                ],
            )
            transaction.execute(
                "INSERT INTO generation_batches (batch_id, generation_run_id, template_id, "
                "status, sampling_policy, planned_items, item_count, required_sample, "
                "inspected_count, defect_count, quarantine_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, 'draft', ?, ?, 0, ?, 0, 0, NULL, ?, ?)",
                [batch_id, run_id, template_id, str(policy), item_count, required, now, now],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([batch_id]),
                after_summary=f"opened {policy} batch for {template_key} v{version} "
                f"(run {ordinal}, up to {item_count} items, {required} to inspect)",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="pack.batch.opened",
                aggregate_type="generation_batch",
                aggregate_id=batch_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {
                        "template_key": template_key,
                        "template_version": version,
                        "run_ordinal": ordinal,
                        "sampling_policy": str(policy),
                        "planned_items": item_count,
                        "required_sample": required,
                        "pack_key": pack_row["pack_key"],
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"pack.batch.opened:{batch_id}",
            )
        return _batch_report(database, batch_id)


def _draft_reviews() -> dict[str, PackItemReview]:
    """The review state a freshly drafted item starts in: nothing claimed."""

    return {
        "linguistic": PackItemReview(state="unreviewed", reviewer_kind=ReviewerKind.NOT_APPLICABLE),
        "pedagogical": PackItemReview(
            state="unreviewed", reviewer_kind=ReviewerKind.NOT_APPLICABLE
        ),
        "source-alignment": PackItemReview(
            state="unchecked", reviewer_kind=ReviewerKind.NOT_APPLICABLE
        ),
        "rights": PackItemReview(state="unknown", reviewer_kind=ReviewerKind.NOT_APPLICABLE),
        "privacy": PackItemReview(state="private", reviewer_kind=ReviewerKind.NOT_APPLICABLE),
    }


def import_items(
    paths: WorkspacePaths,
    *,
    items: Sequence[Mapping[str, Any]],
    batch_id: str | None = None,
    pack_key: str | None = None,
    origin_class: str = "human-authored",
    rights: str = "pack content, not cleared for redistribution",
    privacy: str = "public",
    clock: Clock | None = None,
    command: str = "pack.author.import",
) -> BatchReport | tuple[ContentReviewReport, ...]:
    """Record drafted or imported items as `draft` content, preserving their references.

    Items are validated against the same contract a published pack uses, so an item that
    could never be published cannot enter the workspace either.
    """

    active_clock = clock or SystemClock()
    if not items:
        raise LinguaWikiError("invalid_arguments", "give at least one item to import")
    with open_writer(paths, command=command, clock=active_clock) as database:
        pack_row = pack_service.installed_pack(database, pack_key)
        manifest = PackManifest.model_validate(json.loads(pack_row["manifest_json"]))
        batch = None if batch_id is None else _batch_row(database, batch_id)
        if batch is not None and str(batch[3]) != "draft":
            # A batch takes its items once. Reopening it would let the inspection duty be
            # renegotiated after the output was seen, which is the whole point of fixing
            # the duty when the batch opened.
            code = "batch_quarantined" if str(batch[3]) == "quarantined" else "batch_sealed"
            raise LinguaWikiError(
                code,
                f"batch {batch_id} is {batch[3]} and takes no further items; open a new "
                "batch to draft again",
                details=(ErrorDetail(field="batch", reason=f"batch is {batch[3]}"),),
            )
        if batch is not None and len(items) > int(batch[13]):
            raise LinguaWikiError(
                "batch_over_capacity",
                f"batch {batch_id} was opened for {batch[13]} item(s); importing "
                f"{len(items)} would enlarge the inspection duty after the fact",
                details=(
                    ErrorDetail(
                        field="items",
                        reason="more items than the batch was opened for",
                        context={"planned": str(batch[13]), "offered": str(len(items))},
                    ),
                ),
            )
        effective_origin = "ai-generated" if batch is not None else origin_class
        drafts = [DraftItemInput.model_validate(item) for item in items]
        levels = {level for framework in manifest.frameworks for level in framework.levels}
        problems: list[ErrorDetail] = []
        for draft in drafts:
            if draft.level not in levels:
                problems.append(
                    ErrorDetail(field=draft.stable_key, reason=f"unknown level {draft.level}")
                )
            unknown_themes = sorted(set(draft.themes) - set(manifest.themes))
            if unknown_themes:
                problems.append(
                    ErrorDetail(
                        field=draft.stable_key,
                        reason=f"undeclared theme(s) {unknown_themes}",
                    )
                )
        if problems:
            raise LinguaWikiError(
                "draft_invalid",
                "one or more drafted items do not fit the installed pack's declarations",
                details=tuple(problems),
            )
        origin = PackItemOrigin(
            origin_class=effective_origin,  # type: ignore[arg-type]
            reference=None,
            transformation=(
                "drafted through a registered prompt template"
                if batch is not None
                else "imported by the pack author"
            ),
            rights=rights,
            privacy=privacy,  # type: ignore[arg-type]
        )
        recorded: list[str] = []
        with database.transaction() as transaction:
            now = transaction.now()
            for draft in drafts:
                content_id = str(
                    content_id_for(pack_row["pack_key"], KNOWLEDGE_KIND, draft.stable_key)
                )
                references = sorted(set(draft.source_references))
                dependencies = tuple(
                    str(content_id_for(pack_row["pack_key"], KNOWLEDGE_KIND, reference))
                    for reference in draft.dependencies
                )
                content_hash = canonical_content_hash(
                    {
                        "schema_name": "lingua.content.v1",
                        "schema_version": 1,
                        "content_id": content_id,
                        "language": manifest.language,
                        "kind": draft.kind,
                        "title": draft.title,
                        "body": draft.body,
                        "risk_tier": draft.risk_tier,
                        "provenance": {
                            "origin": str(CONTENT_ORIGIN_BY_CLASS[effective_origin]),
                            "source_references": references,
                            "generation_run_id": (None if batch is None else str(batch[1])),
                            "rights": rights,
                            "privacy": privacy,
                        },
                        "dependencies": list(dependencies),
                    }
                )
                existing = transaction.one(
                    "SELECT lifecycle FROM content_records WHERE content_id = ?", [content_id]
                )
                if existing is not None and str(existing[0]) in PROMOTED_LIFECYCLES:
                    raise LinguaWikiError(
                        "content_already_promoted",
                        f"{draft.stable_key} is already {existing[0]}; an import may not "
                        "overwrite reviewed content",
                        details=(
                            ErrorDetail(field=draft.stable_key, reason="content is promoted"),
                        ),
                    )
                if existing is None:
                    transaction.execute(
                        "INSERT INTO content_records (content_id, content_kind, pack_id, "
                        "track_id, stable_key, language_tag, content_hash, lifecycle, risk_tier, "
                        "batch_id, quarantined, invalidation_reason, created_at, updated_at) "
                        "VALUES (?, ?, ?, NULL, ?, ?, ?, 'draft', ?, ?, FALSE, NULL, ?, ?)",
                        [
                            content_id,
                            KNOWLEDGE_KIND,
                            pack_row["pack_id"],
                            draft.stable_key,
                            manifest.language,
                            content_hash,
                            draft.risk_tier,
                            batch_id,
                            now,
                            now,
                        ],
                    )
                else:
                    transaction.execute(
                        "UPDATE content_records SET content_hash = ?, lifecycle = 'draft', "
                        "risk_tier = ?, quarantined = FALSE, invalidation_reason = NULL, "
                        "updated_at = ? WHERE content_id = ?",
                        [content_hash, draft.risk_tier, now, content_id],
                    )
                    transaction.execute(
                        "DELETE FROM content_origins WHERE content_id = ?", [content_id]
                    )
                    transaction.execute(
                        "DELETE FROM content_reviews WHERE content_id = ?", [content_id]
                    )
                    transaction.execute(
                        "DELETE FROM content_dependencies WHERE content_id = ?", [content_id]
                    )
                transaction.execute(
                    "INSERT INTO content_origins (content_id, sequence, origin_class, reference, "
                    "locator, transformation, origin_hash) VALUES (?, 1, ?, ?, NULL, ?, NULL)",
                    [
                        content_id,
                        effective_origin,
                        references[0] if references else None,
                        origin.transformation,
                    ],
                )
                for axis, review in sorted(_draft_reviews().items()):
                    transaction.execute(
                        "INSERT INTO content_reviews (content_id, axis, state, reviewer_kind, "
                        "reviewer, method, evidence_reference, reviewed_content_hash, "
                        "reviewed_at, updated_at) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, "
                        "NULL, ?)",
                        [content_id, axis, review.state, str(review.reviewer_kind), now],
                    )
                for sequence, dependency in enumerate(dependencies, start=1):
                    transaction.execute(
                        "INSERT INTO content_dependencies (content_id, sequence, "
                        "dependency_kind, dependency_ref, expected_hash, on_change) "
                        "VALUES (?, ?, 'content', ?, NULL, 'needs-review')",
                        [content_id, sequence, dependency],
                    )
                transaction.execute(
                    "INSERT INTO knowledge_items (content_id, language, kind, title, body, "
                    "summary, level_min, level_max, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
                    "ON CONFLICT (content_id) DO UPDATE SET kind = excluded.kind, "
                    "title = excluded.title, body = excluded.body, summary = excluded.summary, "
                    "level_min = excluded.level_min, updated_at = excluded.updated_at",
                    [
                        content_id,
                        manifest.language,
                        draft.kind,
                        draft.title,
                        draft.body,
                        draft.summary,
                        draft.level,
                        now,
                        now,
                    ],
                )
                transaction.execute("DELETE FROM item_tags WHERE content_id = ?", [content_id])
                for tag_kind, tag_value in {
                    *(("theme", theme) for theme in draft.themes),
                    *(("feature", feature) for feature in draft.features),
                    ("level", draft.level),
                }:
                    transaction.execute(
                        "INSERT INTO item_tags (content_id, tag_kind, tag_value) VALUES (?, ?, ?)",
                        [content_id, tag_kind, tag_value],
                    )
                recorded.append(content_id)
            if batch_id is not None:
                # `required_sample` is deliberately absent from this statement. It was
                # fixed when the batch opened, against the count the author committed to
                # before seeing any output. Recomputing it from the subset they chose to
                # import is precisely the cherry-pick the duty exists to prevent: draft
                # forty, import the two convincing ones, and inspect two.
                transaction.execute(
                    "UPDATE generation_batches SET item_count = ?, status = 'sampling', "
                    "updated_at = ? WHERE batch_id = ?",
                    [len(recorded), now, batch_id],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps(recorded),
                after_summary=f"recorded {len(recorded)} draft item(s) of origin "
                f"{effective_origin}",
            )
        if batch_id is not None:
            return _batch_report(database, batch_id)
        return tuple(_content_report(database, content_id) for content_id in recorded)


def _content_row(database: Database, content_id: str) -> Sequence[Any]:
    row = database.one(
        "SELECT content_id, content_kind, stable_key, lifecycle, risk_tier, content_hash, "
        "quarantined, invalidation_reason, batch_id, pack_id, track_id "
        "FROM content_records WHERE content_id = ?",
        [content_id],
    )
    if row is None:
        raise LinguaWikiError(
            "content_not_found",
            f"no content record with ID {content_id}",
            details=(ErrorDetail(field="content", reason="unknown content"),),
        )
    return row


def _reviews_of(database: Database, content_id: str) -> dict[str, PackItemReview]:
    return {
        str(axis): PackItemReview(
            state=str(state),
            reviewer_kind=ReviewerKind(str(reviewer_kind)),
            reviewer=None if reviewer is None else str(reviewer),
            method=None if method is None else str(method),
            evidence_reference=None if evidence is None else str(evidence),
        )
        for axis, state, reviewer_kind, reviewer, method, evidence in database.query(
            "SELECT axis, state, reviewer_kind, reviewer, method, evidence_reference "
            "FROM content_reviews WHERE content_id = ? ORDER BY axis",
            [content_id],
        )
    }


def _centrality(database: Database, content_id: str) -> int:
    dependents = int(
        database.scalar(
            "SELECT count(*) FROM content_dependencies WHERE dependency_kind = 'content' "
            "AND dependency_ref = ?",
            [content_id],
        )
    )
    prerequisites = int(
        database.scalar(
            "SELECT count(*) FROM knowledge_relations WHERE relation_type = 'prerequisite' "
            "AND target_content_id = ?",
            [content_id],
        )
    )
    return dependents + prerequisites


def _content_report(database: Database, content_id: str) -> ContentReviewReport:
    row = _content_row(database, content_id)
    reviews = _reviews_of(database, content_id)
    bound = {
        str(axis): str(recorded) == str(row[5])
        for axis, recorded in database.query(
            "SELECT axis, coalesce(reviewed_content_hash, '') FROM content_reviews "
            "WHERE content_id = ?",
            [content_id],
        )
    }
    origins = tuple(
        str(origin_class)
        for (origin_class,) in database.query(
            "SELECT origin_class FROM content_origins WHERE content_id = ? ORDER BY sequence",
            [content_id],
        )
    )
    references = tuple(
        str(reference)
        for (reference,) in database.query(
            "SELECT reference FROM content_origins WHERE content_id = ? AND reference IS NOT NULL",
            [content_id],
        )
    )
    # An unpromoted item satisfies its own lifecycle trivially, which is useless in a
    # review queue. Report against the promotion it is heading for instead.
    promotion_target = (
        str(row[3]) if str(row[3]) in PROMOTED_LIFECYCLES else DEFAULT_PROMOTION_TARGET
    )
    required = required_strengths(risk_tier=int(row[4]), lifecycle=promotion_target)
    axes = tuple(
        ReviewAxisReport(
            axis=axis,
            state=None if axis not in reviews else reviews[axis].state,
            required_state=(
                axis_states(axis)[required[axis]] if required[axis] > 0 else "not required"
            ),
            reviewer_kind=None if axis not in reviews else str(reviews[axis].reviewer_kind),
            reviewer=None if axis not in reviews else reviews[axis].reviewer,
            method=None if axis not in reviews else reviews[axis].method,
            satisfied=(
                axis in reviews
                and is_known_state(axis, reviews[axis].state)
                and state_strength(axis, reviews[axis].state) >= required[axis]
            ),
            bound_to_current_hash=bound.get(axis, False),
        )
        for axis in REVIEW_AXES
    )
    problems = gate_problems(
        risk_tier=int(row[4]),
        lifecycle=promotion_target,
        reviews=reviews,
        origin_classes=list(origins),
        source_references=list(references),
    )
    warnings: list[str] = []
    stale = [entry.axis for entry in axes if entry.state and not entry.bound_to_current_hash]
    if stale:
        warnings.append(
            f"review(s) on {stale} are not bound to the current content hash and must be redone"
        )
    return ContentReviewReport(
        content_id=str(row[0]),
        stable_key=str(row[2]),
        content_kind=str(row[1]),
        lifecycle=str(row[3]),
        promotion_target=promotion_target,
        risk_tier=int(row[4]),
        content_hash=str(row[5]),
        quarantined=bool(row[6]),
        invalidation_reason=None if row[7] is None else str(row[7]),
        batch_id=None if row[8] is None else str(row[8]),
        origins=origins,
        axes=axes,
        gate_problems=tuple(str(problem) for problem in problems),
        dependency_centrality=_centrality(database, content_id),
        warnings=tuple(warnings),
    )


def review_queue(
    paths: WorkspacePaths,
    *,
    pack_key: str | None = None,
    limit: int = DEFAULT_QUEUE_LIMIT,
    clock: Clock | None = None,
) -> ReviewQueueReport:
    """Unfinished content, ordered by how much its state costs to leave unresolved."""

    if limit < 1:
        raise LinguaWikiError("invalid_arguments", "limit must be positive")
    with open_reader(paths, clock=clock or SystemClock()) as database:
        pack_row = pack_service.installed_pack(database, pack_key)
        placeholders = ", ".join("?" for _ in QUEUE_LIFECYCLES)
        candidates = [
            str(content_id)
            for (content_id,) in database.query(
                "SELECT content_id FROM content_records WHERE pack_id = ? "
                f"AND (lifecycle IN ({placeholders}) OR quarantined) ORDER BY stable_key",
                [pack_row["pack_id"], *QUEUE_LIFECYCLES],
            )
        ]
        reports = [_content_report(database, content_id) for content_id in candidates]
        debt: dict[str, int] = dict.fromkeys(REVIEW_AXES, 0)
        for report in reports:
            for axis in report.axes:
                if not axis.satisfied and axis.required_state != "not required":
                    debt[axis.axis] += 1
        # Highest risk first, then the most unmet axes, then the most depended-upon item.
        reports.sort(
            key=lambda report: (
                -report.risk_tier,
                -len([axis for axis in report.axes if not axis.satisfied]),
                -report.dependency_centrality,
                report.stable_key,
            )
        )
        warnings: list[str] = []
        quarantined = [report.stable_key for report in reports if report.quarantined]
        if quarantined:
            warnings.append(
                f"{len(quarantined)} item(s) are quarantined and must be re-drafted rather than "
                "reviewed"
            )
        return ReviewQueueReport(
            pack_key=pack_row["pack_key"],
            total=len(reports),
            returned=min(limit, len(reports)),
            items=tuple(reports[:limit]),
            axis_debt=debt,
            warnings=tuple(warnings),
        )


def review(
    paths: WorkspacePaths,
    *,
    content_id: str,
    axis: str,
    state: str,
    reviewer_kind: str,
    reviewer: str | None = None,
    method: str | None = None,
    evidence_reference: str | None = None,
    inspection: str | None = None,
    finding: str | None = None,
    clock: Clock | None = None,
    command: str = "pack.author.review",
) -> ContentReviewReport | InvalidationReport:
    """Record one axis review, bound to the content hash it actually examined.

    Passing `inspection` also records the batch inspection this review satisfies. A
    `defective` inspection quarantines the whole batch: a template that produced one bad
    item has no claim to the rest of its output.
    """

    active_clock = clock or SystemClock()
    if axis not in REVIEW_AXES:
        raise LinguaWikiError(
            "unknown_review_axis",
            f"{axis} is not a review axis",
            details=(ErrorDetail(field="axis", reason=f"expected one of {list(REVIEW_AXES)}"),),
        )
    if not is_known_state(axis, state):
        raise LinguaWikiError(
            "unknown_review_state",
            f"{state} is not a state of the {axis} axis",
            details=(ErrorDetail(field="state", reason=f"expected {list(axis_states(axis))}"),),
        )
    try:
        kind = ReviewerKind(reviewer_kind)
    except ValueError as exc:
        raise LinguaWikiError(
            "invalid_arguments",
            f"{reviewer_kind} is not a reviewer kind",
            details=(ErrorDetail(field="reviewer_kind", reason=reviewer_kind),),
        ) from exc
    if kind in MACHINE_REVIEWER_KINDS and state_strength(axis, state) > MACHINE_CEILING[axis]:
        raise LinguaWikiError(
            "machine_review_ceiling",
            f"a reviewer of kind '{kind}' cannot claim {state} on the {axis} axis; machine "
            f"checking stops at {axis_states(axis)[MACHINE_CEILING[axis]]}",
            details=(
                ErrorDetail(
                    field="state",
                    reason="machine review cannot claim human or reference verification",
                    context={"axis": axis, "state": state},
                ),
            ),
        )
    if inspection is not None and inspection not in ("accepted", "defective"):
        raise LinguaWikiError("invalid_arguments", "inspection must be accepted or defective")
    if inspection == "defective" and not finding:
        raise LinguaWikiError("invalid_arguments", "a defective inspection must record its finding")
    with open_writer(paths, command=command, clock=active_clock) as database:
        row = _content_row(database, content_id)
        if bool(row[6]) and inspection != "defective":
            raise LinguaWikiError(
                "content_quarantined",
                f"{row[2]} is quarantined; re-draft it rather than reviewing it",
                details=(ErrorDetail(field="content", reason="content is quarantined"),),
            )
        batch_id = None if row[8] is None else str(row[8])
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "UPDATE content_reviews SET state = ?, reviewer_kind = ?, reviewer = ?, "
                "method = ?, evidence_reference = ?, reviewed_content_hash = ?, "
                "reviewed_at = ?, updated_at = ? WHERE content_id = ? AND axis = ?",
                [
                    state,
                    str(kind),
                    reviewer,
                    method,
                    evidence_reference,
                    str(row[5]),
                    now,
                    now,
                    content_id,
                    axis,
                ],
            )
            if inspection is not None and batch_id is not None:
                existing = transaction.one(
                    "SELECT outcome FROM batch_inspections WHERE batch_id = ? AND content_id = ?",
                    [batch_id, content_id],
                )
                if existing is None:
                    transaction.execute(
                        "INSERT INTO batch_inspections (batch_id, content_id, outcome, axis, "
                        "finding, reviewer, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            batch_id,
                            content_id,
                            inspection,
                            axis,
                            finding,
                            reviewer or str(kind),
                            now,
                        ],
                    )
                    transaction.execute(
                        "UPDATE generation_batches SET inspected_count = inspected_count + 1, "
                        "defect_count = defect_count + ?, updated_at = ? WHERE batch_id = ?",
                        [1 if inspection == "defective" else 0, now, batch_id],
                    )
                    if inspection == "accepted":
                        _accept_batch_if_complete(transaction, batch_id=batch_id)
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([content_id]),
                after_summary=f"{axis} -> {state} by {kind}"
                + (f" ({inspection})" if inspection else ""),
            )
        if inspection == "defective" and batch_id is not None:
            reason = f"defective sample: {finding}"
            with database.transaction() as transaction:
                items, dependents = _quarantine_batch(transaction, batch_id=batch_id, reason=reason)
                template = transaction.one(
                    "SELECT template.template_key, template.version FROM generation_batches batch "
                    "JOIN prompt_templates template ON template.template_id = batch.template_id "
                    "WHERE batch.batch_id = ?",
                    [batch_id],
                )
                migration_module.record_domain_event(
                    transaction,
                    event_type="pack.batch.quarantined",
                    aggregate_type="generation_batch",
                    aggregate_id=batch_id,
                    correlation_id=EventId.new(),
                    payload_json=json.dumps(
                        {
                            "reason": reason,
                            "items": len(items),
                            "dependents": len(dependents),
                        },
                        sort_keys=True,
                    ),
                    idempotency_key=f"pack.batch.quarantined:{batch_id}",
                )
            return InvalidationReport(
                reason=reason,
                roots=tuple(items),
                invalidated=tuple(dependents),
                quarantined_batches=(batch_id,),
                quarantined_templates=(
                    () if template is None else (f"{template[0]}@{template[1]}",)
                ),
                warnings=(
                    "the batch's template is quarantined; revalidate it as a new version "
                    "before drafting again",
                ),
            )
        return _content_report(database, content_id)


def _accept_batch_if_complete(database: Database, *, batch_id: str) -> bool:
    """Accept a batch once its duty is discharged, and count the run exactly once.

    The status guard is what makes it once: the transition out of `sampling` is the
    record that the run has already been counted, so a further inspection cannot
    increment `inspected_runs` again. Without this, nothing ever left `sampling`,
    `inspected_runs` stayed at zero, and `pack template stabilize` was unreachable
    through the supported commands.
    """

    row = database.one(
        "SELECT status, sampling_policy, required_sample, inspected_count, defect_count, "
        "template_id FROM generation_batches WHERE batch_id = ?",
        [batch_id],
    )
    if row is None or str(row[0]) != "sampling":
        return False
    if int(row[4]) or int(row[3]) < int(row[2]):
        return False
    now = database.now()
    database.execute(
        "UPDATE generation_batches SET status = 'accepted', updated_at = ? WHERE batch_id = ?",
        [now, batch_id],
    )
    if str(row[1]) == SamplingPolicy.FULL_INSPECTION:
        # Only a fully inspected run counts towards stabilization: that is what the
        # first-three-runs rule is about, and a sampled batch has not seen every item.
        database.execute(
            "UPDATE prompt_templates SET inspected_runs = inspected_runs + 1, updated_at = ? "
            "WHERE template_id = ?",
            [now, str(row[5])],
        )
    return True


def approve(
    paths: WorkspacePaths,
    *,
    content_id: str,
    lifecycle: str,
    clock: Clock | None = None,
    command: str = "pack.author.approve",
) -> ContentReviewReport:
    """Promote content only if its own review states support the claim."""

    active_clock = clock or SystemClock()
    if lifecycle not in PROMOTED_LIFECYCLES:
        raise LinguaWikiError(
            "invalid_arguments",
            f"lifecycle must be one of {list(PROMOTED_LIFECYCLES)}",
            details=(ErrorDetail(field="lifecycle", reason=lifecycle),),
        )
    with open_writer(paths, command=command, clock=active_clock) as database:
        row = _content_row(database, content_id)
        if bool(row[6]):
            raise LinguaWikiError(
                "content_quarantined",
                f"{row[2]} is quarantined and cannot be approved",
                details=(ErrorDetail(field="content", reason="content is quarantined"),),
            )
        reviews = _reviews_of(database, content_id)
        stale = sorted(
            str(axis)
            for axis, recorded in database.query(
                "SELECT axis, coalesce(reviewed_content_hash, '') FROM content_reviews "
                "WHERE content_id = ?",
                [content_id],
            )
            if str(recorded) not in ("", str(row[5]))
        )
        if stale:
            raise LinguaWikiError(
                "review_hash_stale",
                f"review(s) on {stale} examined an earlier revision of {row[2]}; redo them "
                "before approving",
                details=(ErrorDetail(field="reviews", reason=", ".join(stale)),),
            )
        origins = [
            str(origin_class)
            for (origin_class,) in database.query(
                "SELECT origin_class FROM content_origins WHERE content_id = ? ORDER BY sequence",
                [content_id],
            )
        ]
        references = [
            str(reference)
            for (reference,) in database.query(
                "SELECT reference FROM content_origins WHERE content_id = ? "
                "AND reference IS NOT NULL",
                [content_id],
            )
        ]
        problems = gate_problems(
            risk_tier=int(row[4]),
            lifecycle=lifecycle,
            reviews=reviews,
            origin_classes=origins,
            source_references=references,
        )
        if problems:
            raise LinguaWikiError(
                "promotion_gate_failed",
                f"{row[2]} cannot become {lifecycle}",
                details=tuple(
                    ErrorDetail(field=problem.axis or "content", reason=problem.reason)
                    for problem in problems
                ),
            )
        _assert_independent_reviewers(
            reviews, lifecycle=lifecycle, origins=origins, key=str(row[2])
        )
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "UPDATE content_records SET lifecycle = ?, invalidation_reason = NULL, "
                "updated_at = ? WHERE content_id = ?",
                [lifecycle, now, content_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([content_id]),
                before_summary=str(row[3]),
                after_summary=lifecycle,
            )
            migration_module.record_domain_event(
                transaction,
                event_type="pack.content.approved",
                aggregate_type="content",
                aggregate_id=content_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {"lifecycle": lifecycle, "risk_tier": int(row[4])}, sort_keys=True
                ),
                idempotency_key=f"pack.content.approved:{content_id}:{row[5]}:{lifecycle}",
            )
        return _content_report(database, content_id)


def _assert_independent_reviewers(
    reviews: Mapping[str, PackItemReview], *, lifecycle: str, origins: Sequence[str], key: str
) -> None:
    """Publication-ready AI content needs two different reviewer identities.

    A reviewer cannot serve as both the independent linguistic and the independent
    pedagogical check on their own AI batch.
    """

    if lifecycle != "publication-ready":
        return
    if not any(origin.startswith("ai-") or origin == "synthetic-media" for origin in origins):
        return
    linguistic = reviews.get("linguistic")
    pedagogical = reviews.get("pedagogical")
    identities = {
        review.reviewer for review in (linguistic, pedagogical) if review and review.reviewer
    }
    if len(identities) < 2:
        raise LinguaWikiError(
            "reviewer_not_independent",
            f"{key} is AI-origin content: its linguistic and pedagogical reviews must name "
            "two different reviewers before it can be published",
            details=(
                ErrorDetail(
                    field="reviews",
                    reason="one reviewer cannot be their own second opinion",
                    context={"reviewers": ", ".join(sorted(identities))},
                ),
            ),
        )


def reject(
    paths: WorkspacePaths,
    *,
    content_id: str,
    reason: str,
    clock: Clock | None = None,
    command: str = "pack.author.reject",
) -> ContentReviewReport:
    """Reject content. Its reviews stay recorded, so the decision remains auditable."""

    active_clock = clock or SystemClock()
    if not reason.strip():
        raise LinguaWikiError("invalid_arguments", "a rejection needs a reason")
    with open_writer(paths, command=command, clock=active_clock) as database:
        row = _content_row(database, content_id)
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "UPDATE content_records SET lifecycle = 'rejected', invalidation_reason = ?, "
                "updated_at = ? WHERE content_id = ?",
                [reason, now, content_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([content_id]),
                before_summary=str(row[3]),
                after_summary=f"rejected: {reason}",
            )
        return _content_report(database, content_id)


def invalidate(
    paths: WorkspacePaths,
    *,
    reason: str,
    content_ids: Sequence[str] = (),
    batch_id: str | None = None,
    clock: Clock | None = None,
    command: str = "pack.author.invalidate",
) -> InvalidationReport:
    """Walk declared dependencies and mark everything downstream `needs-review`."""

    active_clock = clock or SystemClock()
    if not reason.strip():
        raise LinguaWikiError("invalid_arguments", "an invalidation needs a reason")
    if not content_ids and batch_id is None:
        raise LinguaWikiError("invalid_arguments", "name the content or the batch to invalidate")
    with open_writer(paths, command=command, clock=active_clock) as database:
        roots = list(content_ids)
        if batch_id is not None:
            _batch_row(database, batch_id)
            roots.extend(
                str(content_id)
                for (content_id,) in database.query(
                    "SELECT content_id FROM content_records WHERE batch_id = ? ORDER BY stable_key",
                    [batch_id],
                )
            )
        for content_id in roots:
            _content_row(database, content_id)
        dependents = _transitive_dependents(database, roots)
        with database.transaction() as transaction:
            now = transaction.now()
            for content_id in (*roots, *dependents):
                transaction.execute(
                    "UPDATE content_records SET lifecycle = 'needs-review', "
                    "invalidation_reason = ?, updated_at = ? WHERE content_id = ?",
                    [reason, now, content_id],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps(sorted({*roots, *dependents})),
                after_summary=f"invalidated {len(roots)} root(s) and {len(dependents)} "
                f"dependent(s): {reason}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="pack.content.invalidated",
                aggregate_type="content",
                aggregate_id=roots[0],
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {"reason": reason, "roots": len(roots), "dependents": len(dependents)},
                    sort_keys=True,
                ),
            )
        return InvalidationReport(
            reason=reason,
            roots=tuple(sorted(set(roots))),
            invalidated=tuple(dependents),
        )


def content_report(
    paths: WorkspacePaths, *, content_id: str, clock: Clock | None = None
) -> ContentReviewReport:
    with open_reader(paths, clock=clock or SystemClock()) as database:
        return _content_report(database, content_id)


def batch_report(
    paths: WorkspacePaths, *, batch_id: str, clock: Clock | None = None
) -> BatchReport:
    with open_reader(paths, clock=clock or SystemClock()) as database:
        return _batch_report(database, batch_id)


__all__ = [
    "BatchReport",
    "ContentReviewReport",
    "DraftItemInput",
    "InvalidationReport",
    "ReviewQueueReport",
    "TemplateInput",
    "TemplateReport",
    "approve",
    "batch_report",
    "content_report",
    "generate_draft",
    "import_items",
    "invalidate",
    "quarantine_template",
    "reject",
    "review",
    "review_queue",
    "stabilize_template",
    "validate_template",
]
