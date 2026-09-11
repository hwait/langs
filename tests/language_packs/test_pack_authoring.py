"""Pack authoring: bounded batches, per-axis review, quarantine, and invalidation.

The rules under test are the ones that make AI-assisted drafting safe rather than easy:
a drafting batch fixes its inspection duty before any output exists, a machine reviewer
has a ceiling, one defective sample condemns the whole batch, and a promotion re-runs the
gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.packs.format import KNOWLEDGE_KIND, content_id_for
from linguawiki.provenance import FULL_INSPECTION_RUNS, SamplingPolicy, required_sample
from linguawiki.services import authoring as authoring_service
from tests.conftest import SyntheticWorkspace

TEMPLATE = {
    "schema_name": "lingua.pack.template.v1",
    "schema_version": 1,
    "template_key": "pl-office-collocations",
    "version": 1,
    "purpose": "generation",
    "intended_kinds": ["construction"],
    "body": "Draft office collocations for Polish A2 from the pack's declared themes.",
    "known_failure_modes": ["invents register", "copies dictionary examples"],
}
DRAFTS: list[dict[str, Any]] = [
    {
        "stable_key": "pl.draft.wyslac-maila",
        "kind": "construction",
        "title": "wysłać maila",
        "body": "Draft: 'wysłać maila do klienta' -- send an email to a client.",
        "level": "A2",
        "themes": ["praca i biuro"],
        "risk_tier": 2,
        "dependencies": ["pl.lex.biuro"],
    },
    {
        "stable_key": "pl.draft.zwolac-spotkanie",
        "kind": "construction",
        "title": "zwołać spotkanie",
        "body": "Draft: 'zwołać spotkanie na piątek' -- call a meeting for Friday.",
        "level": "A2",
        "themes": ["praca i biuro"],
        "risk_tier": 2,
    },
]


def _register(workspace: SyntheticWorkspace, **overrides: Any) -> Any:
    return authoring_service.validate_template(
        workspace.paths, payload={**TEMPLATE, **overrides}, clock=workspace.clock
    )


def _batch(workspace: SyntheticWorkspace, *, count: int = 2) -> Any:
    return authoring_service.generate_draft(
        workspace.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        item_count=count,
        provider="example",
        model="example-model",
        clock=workspace.clock,
    )


def _drafted(workspace: SyntheticWorkspace, stable_key: str) -> str:
    queue = authoring_service.review_queue(workspace.paths, limit=100, clock=workspace.clock)
    return next(entry.content_id for entry in queue.items if entry.stable_key == stable_key)


def test_a_new_template_starts_unstable_and_must_inspect_everything(
    installed_pilot: SyntheticWorkspace,
) -> None:
    report = _register(installed_pilot)

    assert report.maturity == "new"
    assert report.sampling_policy == str(SamplingPolicy.FULL_INSPECTION)
    assert any("every persistent item" in warning for warning in report.warnings)


def test_changing_a_registered_templates_text_is_refused(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """The sampling history is a claim about *that* text, so a change needs a version."""

    _register(installed_pilot)

    with pytest.raises(LinguaWikiError) as failure:
        _register(installed_pilot, body="A materially different prompt.")

    assert failure.value.payload.code == "template_body_changed"

    bumped = _register(installed_pilot, version=2, body="A materially different prompt.")
    assert bumped.version == 2
    assert bumped.inspected_runs == 0


def test_a_batch_fixes_its_inspection_duty_before_any_output_exists(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)

    batch = _batch(installed_pilot, count=40)

    assert batch.sampling_policy == str(SamplingPolicy.FULL_INSPECTION)
    assert batch.required_sample == 40
    assert batch.item_count == 0
    assert batch.run_ordinal == 1


def test_drafting_records_items_as_drafts_with_nothing_claimed(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)

    result = authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )

    assert result.item_count == 2
    report = authoring_service.content_report(
        installed_pilot.paths,
        content_id=_drafted(installed_pilot, "pl.draft.wyslac-maila"),
        clock=installed_pilot.clock,
    )
    assert report.lifecycle == "draft"
    assert report.origins == ("ai-generated",)
    assert report.batch_id == batch.batch_id
    assert {axis.axis: axis.satisfied for axis in report.axes}["linguistic"] is False
    assert report.gate_problems


def test_a_draft_dependency_is_stored_as_a_derived_content_reference(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")

    with open_reader(installed_pilot.paths, clock=installed_pilot.clock) as database:
        references = database.query(
            "SELECT dependency_ref FROM content_dependencies WHERE content_id = ?", [content_id]
        )

    assert [str(row[0]) for row in references] == [
        str(content_id_for("pl-pilot", KNOWLEDGE_KIND, "pl.lex.biuro"))
    ]


def test_a_draft_using_an_undeclared_level_or_theme_is_refused(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.import_items(
            installed_pilot.paths,
            items=[{**DRAFTS[0], "level": "Z9", "themes": ["nieistniejący"]}],
            batch_id=batch.batch_id,
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "draft_invalid"
    reasons = " ".join(detail.reason for detail in failure.value.payload.details)
    assert "unknown level Z9" in reasons
    assert "undeclared theme" in reasons


def test_an_import_may_not_overwrite_content_that_was_already_reviewed(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """The pilot pack's own reviewed items must not be replaceable by a draft."""

    _register(installed_pilot)
    batch = _batch(installed_pilot)

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.import_items(
            installed_pilot.paths,
            items=[{**DRAFTS[0], "stable_key": "pl.lex.biuro"}],
            batch_id=batch.batch_id,
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "content_already_promoted"


def test_the_review_queue_puts_the_costliest_unfinished_item_first(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )

    queue = authoring_service.review_queue(
        installed_pilot.paths, limit=100, clock=installed_pilot.clock
    )

    tiers = [entry.risk_tier for entry in queue.items]
    assert tiers == sorted(tiers, reverse=True)
    assert all(entry.promotion_target == "approved-personal" for entry in queue.items)
    # The pack's three AI drafts plus the two just imported.
    assert queue.total == 5
    assert queue.axis_debt["linguistic"] == 5


@pytest.mark.parametrize("kind", ["machine", "ai"])
@pytest.mark.parametrize(
    ("axis", "state"),
    [("linguistic", "human-verified"), ("pedagogical", "teacher-verified"), ("rights", "cleared")],
)
def test_a_machine_reviewer_cannot_claim_a_human_verification(
    installed_pilot: SyntheticWorkspace, kind: str, axis: str, state: str
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.review(
            installed_pilot.paths,
            content_id=content_id,
            axis=axis,
            state=state,
            reviewer_kind=kind,
            reviewer="example-model",
            method="self-review",
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "machine_review_ceiling"


def test_an_unknown_axis_or_state_is_refused_with_the_alternatives(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")

    with pytest.raises(LinguaWikiError) as axis_failure:
        authoring_service.review(
            installed_pilot.paths,
            content_id=content_id,
            axis="phonology",
            state="verified",
            reviewer_kind="human",
            clock=installed_pilot.clock,
        )
    with pytest.raises(LinguaWikiError) as state_failure:
        authoring_service.review(
            installed_pilot.paths,
            content_id=content_id,
            axis="linguistic",
            state="teacher-verified",
            reviewer_kind="human",
            clock=installed_pilot.clock,
        )

    assert axis_failure.value.payload.code == "unknown_review_axis"
    assert state_failure.value.payload.code == "unknown_review_state"


def _review_to_approvable(
    workspace: SyntheticWorkspace, content_id: str, *, inspection: str | None = "accepted"
) -> None:
    """Bring one draft up to what `approved-personal` at risk tier 2 requires."""

    for axis, state, kind in (
        ("linguistic", "reference-verified", "human"),
        ("pedagogical", "learner-approved", "learner"),
        ("source-alignment", "machine-checked", "machine"),
        ("rights", "personal-use-only", "human"),
        ("privacy", "shareable", "human"),
    ):
        authoring_service.review(
            workspace.paths,
            content_id=content_id,
            axis=axis,
            state=state,
            reviewer_kind=kind,
            reviewer=f"{kind}-reviewer",
            method="pilot review",
            inspection=inspection if axis == "linguistic" else None,
            clock=workspace.clock,
        )


def test_a_promotion_runs_the_gate_and_names_every_unmet_axis(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.approve(
            installed_pilot.paths,
            content_id=content_id,
            lifecycle="approved-personal",
            clock=installed_pilot.clock,
        )
    assert failure.value.payload.code == "promotion_gate_failed"
    assert {detail.field for detail in failure.value.payload.details} >= {
        "linguistic",
        "pedagogical",
        "rights",
    }

    _review_to_approvable(installed_pilot, content_id)
    approved = authoring_service.approve(
        installed_pilot.paths,
        content_id=content_id,
        lifecycle="approved-personal",
        clock=installed_pilot.clock,
    )

    assert approved.lifecycle == "approved-personal"
    assert approved.gate_problems == ()


def test_publication_ready_ai_content_needs_two_different_reviewers(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """One reviewer cannot be their own second opinion on their own AI batch."""

    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")
    for axis, state, kind in (
        ("linguistic", "human-verified", "human"),
        ("pedagogical", "teacher-verified", "human"),
        ("source-alignment", "verified", "human"),
        ("rights", "cleared", "human"),
        ("privacy", "shareable", "human"),
    ):
        authoring_service.review(
            installed_pilot.paths,
            content_id=content_id,
            axis=axis,
            state=state,
            reviewer_kind=kind,
            reviewer="the-same-person",
            method="review",
            clock=installed_pilot.clock,
        )

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.approve(
            installed_pilot.paths,
            content_id=content_id,
            lifecycle="publication-ready",
            clock=installed_pilot.clock,
        )
    assert failure.value.payload.code == "reviewer_not_independent"

    authoring_service.review(
        installed_pilot.paths,
        content_id=content_id,
        axis="pedagogical",
        state="teacher-verified",
        reviewer_kind="human",
        reviewer="an-independent-teacher",
        method="review",
        clock=installed_pilot.clock,
    )
    published = authoring_service.approve(
        installed_pilot.paths,
        content_id=content_id,
        lifecycle="publication-ready",
        clock=installed_pilot.clock,
    )
    assert published.lifecycle == "publication-ready"


def test_a_defective_sample_quarantines_the_batch_its_template_and_its_dependents(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    first = _drafted(installed_pilot, "pl.draft.wyslac-maila")
    second = _drafted(installed_pilot, "pl.draft.zwolac-spotkanie")
    # Approve the sibling first, so quarantine is shown to reach accepted work too.
    _review_to_approvable(installed_pilot, second, inspection=None)
    authoring_service.approve(
        installed_pilot.paths,
        content_id=second,
        lifecycle="approved-personal",
        clock=installed_pilot.clock,
    )

    outcome = authoring_service.review(
        installed_pilot.paths,
        content_id=first,
        axis="linguistic",
        state="unreviewed",
        reviewer_kind="human",
        reviewer="pack-author",
        method="manual inspection",
        inspection="defective",
        finding="register is wrong in this frame",
        clock=installed_pilot.clock,
    )

    assert isinstance(outcome, authoring_service.InvalidationReport)
    assert set(outcome.roots) == {first, second}
    assert outcome.quarantined_batches == (batch.batch_id,)
    assert outcome.quarantined_templates == ("pl-office-collocations@1",)

    for content_id in (first, second):
        report = authoring_service.content_report(
            installed_pilot.paths, content_id=content_id, clock=installed_pilot.clock
        )
        assert report.quarantined is True
        assert report.lifecycle == "needs-review"
        assert "defective sample" in (report.invalidation_reason or "")

    template = authoring_service.validate_template(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        clock=installed_pilot.clock,
    )
    assert template.maturity == "quarantined"


def test_a_quarantined_template_may_not_draft_again(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    authoring_service.quarantine_template(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        reason="produced unusable register",
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        _batch(installed_pilot)

    assert failure.value.payload.code == "template_quarantined"


def test_a_quarantined_item_is_re_drafted_rather_than_reviewed(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")
    authoring_service.review(
        installed_pilot.paths,
        content_id=content_id,
        axis="linguistic",
        state="unreviewed",
        reviewer_kind="human",
        reviewer="pack-author",
        method="manual inspection",
        inspection="defective",
        finding="wrong",
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as review_failure:
        authoring_service.review(
            installed_pilot.paths,
            content_id=content_id,
            axis="pedagogical",
            state="learner-approved",
            reviewer_kind="learner",
            method="review",
            clock=installed_pilot.clock,
        )
    with pytest.raises(LinguaWikiError) as approve_failure:
        authoring_service.approve(
            installed_pilot.paths,
            content_id=content_id,
            lifecycle="approved-personal",
            clock=installed_pilot.clock,
        )

    assert review_failure.value.payload.code == "content_quarantined"
    assert approve_failure.value.payload.code == "content_quarantined"


def test_a_template_stabilizes_only_after_three_clean_inspected_runs(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.stabilize_template(
            installed_pilot.paths,
            template_key=TEMPLATE["template_key"],
            version=1,
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "template_not_stabilizable"
    assert failure.value.payload.details[0].context["inspected_runs"] == "0"
    assert str(FULL_INSPECTION_RUNS) in failure.value.payload.message


def test_a_defect_in_the_history_blocks_stabilization(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    authoring_service.review(
        installed_pilot.paths,
        content_id=_drafted(installed_pilot, "pl.draft.wyslac-maila"),
        axis="linguistic",
        state="unreviewed",
        reviewer_kind="human",
        reviewer="pack-author",
        method="manual inspection",
        inspection="defective",
        finding="wrong",
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.stabilize_template(
            installed_pilot.paths,
            template_key=TEMPLATE["template_key"],
            version=1,
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "template_quarantined"


def test_a_stable_template_samples_a_fifth_of_its_batch(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """Read from the sampling rule rather than pinned, since the rule is the contract."""

    _register(installed_pilot)
    with open_reader(installed_pilot.paths, clock=installed_pilot.clock):
        pass
    # Fabricate the history a stabilized template would have earned.
    from linguawiki.db.connection import open_writer

    with (
        open_writer(
            installed_pilot.paths, command="test.stabilize", clock=installed_pilot.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE prompt_templates SET inspected_runs = ? WHERE template_key = ?",
            [FULL_INSPECTION_RUNS, TEMPLATE["template_key"]],
        )
    stabilized = authoring_service.stabilize_template(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        clock=installed_pilot.clock,
    )
    batch = _batch(installed_pilot, count=40)

    assert stabilized.maturity == "stable"
    assert batch.sampling_policy == str(SamplingPolicy.SAMPLED)
    assert batch.required_sample == required_sample(40)


def test_invalidation_walks_declared_dependencies(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """A changed dependency must reach everything downstream, not only its dependents."""

    _register(installed_pilot)
    batch = _batch(installed_pilot, count=3)
    chained = [
        DRAFTS[0],
        {**DRAFTS[1], "dependencies": ["pl.draft.wyslac-maila"]},
    ]
    authoring_service.import_items(
        installed_pilot.paths,
        items=chained,
        batch_id=batch.batch_id,
        clock=installed_pilot.clock,
    )
    root = _drafted(installed_pilot, "pl.draft.wyslac-maila")
    dependent = _drafted(installed_pilot, "pl.draft.zwolac-spotkanie")

    report = authoring_service.invalidate(
        installed_pilot.paths,
        reason="the cited reference changed edition",
        content_ids=[root],
        clock=installed_pilot.clock,
    )

    assert report.roots == (root,)
    assert report.invalidated == (dependent,)
    for content_id in (root, dependent):
        entry = authoring_service.content_report(
            installed_pilot.paths, content_id=content_id, clock=installed_pilot.clock
        )
        assert entry.lifecycle == "needs-review"
        assert "changed edition" in (entry.invalidation_reason or "")


def test_invalidating_a_whole_batch_reaches_every_item_it_produced(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )

    report = authoring_service.invalidate(
        installed_pilot.paths,
        reason="the template's answer key was wrong",
        batch_id=batch.batch_id,
        clock=installed_pilot.clock,
    )

    assert len(report.roots) == 2


def test_invalidation_needs_a_reason_and_a_target(
    installed_pilot: SyntheticWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as no_target:
        authoring_service.invalidate(
            installed_pilot.paths, reason="because", clock=installed_pilot.clock
        )
    with pytest.raises(LinguaWikiError) as no_reason:
        authoring_service.invalidate(
            installed_pilot.paths, reason="  ", content_ids=["cnt_x"], clock=installed_pilot.clock
        )

    assert no_target.value.payload.code == "invalid_arguments"
    assert no_reason.value.payload.code == "invalid_arguments"


def test_a_rejection_keeps_its_reviews_so_the_decision_stays_auditable(
    installed_pilot: SyntheticWorkspace,
) -> None:
    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")
    _review_to_approvable(installed_pilot, content_id)

    rejected = authoring_service.reject(
        installed_pilot.paths,
        content_id=content_id,
        reason="a better item already covers this",
        clock=installed_pilot.clock,
    )

    assert rejected.lifecycle == "rejected"
    assert [axis.state for axis in rejected.axes if axis.axis == "linguistic"] == [
        "reference-verified"
    ]


def test_a_review_bound_to_an_earlier_revision_blocks_approval(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """Re-importing changes the hash; the reviews then examined text nobody kept."""

    _register(installed_pilot)
    batch = _batch(installed_pilot)
    authoring_service.import_items(
        installed_pilot.paths, items=DRAFTS, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    content_id = _drafted(installed_pilot, "pl.draft.wyslac-maila")
    _review_to_approvable(installed_pilot, content_id)
    before = authoring_service.content_report(
        installed_pilot.paths, content_id=content_id, clock=installed_pilot.clock
    )

    # A second import of a revised body replaces the reviews, which is the safe outcome:
    # a stale review is discarded rather than carried across a rewrite. The revision has
    # to come from its own batch, because the first one is sealed by its import.
    revision = _batch(installed_pilot, count=1)
    authoring_service.import_items(
        installed_pilot.paths,
        items=[{**DRAFTS[0], "body": DRAFTS[0]["body"] + " Revised."}],
        batch_id=revision.batch_id,
        clock=installed_pilot.clock,
    )
    after = authoring_service.content_report(
        installed_pilot.paths, content_id=content_id, clock=installed_pilot.clock
    )

    assert after.content_hash != before.content_hash
    assert after.lifecycle == "draft"
    assert all(
        axis.state in {"unreviewed", "unchecked", "unknown", "private"} for axis in after.axes
    )
    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.approve(
            installed_pilot.paths,
            content_id=content_id,
            lifecycle="approved-personal",
            clock=installed_pilot.clock,
        )
    assert failure.value.payload.code == "promotion_gate_failed"


def test_an_unknown_template_batch_or_content_id_is_named(
    installed_pilot: SyntheticWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as template:
        authoring_service.validate_template(
            installed_pilot.paths, template_key="absent", version=1, clock=installed_pilot.clock
        )
    with pytest.raises(LinguaWikiError) as batch:
        authoring_service.batch_report(
            installed_pilot.paths,
            batch_id="evt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clock=installed_pilot.clock,
        )
    with pytest.raises(LinguaWikiError) as content:
        authoring_service.content_report(
            installed_pilot.paths,
            content_id="cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clock=installed_pilot.clock,
        )

    assert template.value.payload.code == "template_not_found"
    assert batch.value.payload.code == "batch_not_found"
    assert content.value.payload.code == "content_not_found"
