"""The promotion gate, tested directly rather than through a command.

Every rule here was a rule two places could disagree about, which is why the gate lives
in one module and is exercised on its own.
"""

from __future__ import annotations

import pytest

from linguawiki.contracts import (
    ContentItem,
    ContentProvenance,
    OriginClass,
    PackItemReview,
    ReviewerKind,
    ReviewState,
    canonical_content_hash,
)
from linguawiki.ids import ContentId
from linguawiki.provenance import (
    AXIS_STATES,
    CONTENT_ORIGIN_BY_CLASS,
    FULL_INSPECTION_RUNS,
    PROMOTED_LIFECYCLES,
    REVIEW_AXES,
    UNPROMOTED_LIFECYCLES,
    SamplingPolicy,
    axis_states,
    content_lifecycle,
    content_reviews,
    gate_problems,
    is_known_state,
    required_sample,
    required_strengths,
    review_gate_state,
    sample_size,
    sampling_policy,
    state_strength,
)


def review(state: str, *, kind: ReviewerKind = ReviewerKind.HUMAN, method: str | None = "checked"):
    return PackItemReview(state=state, reviewer_kind=kind, reviewer="reviewer", method=method)


def full_reviews(**overrides: PackItemReview) -> dict[str, PackItemReview]:
    """Every axis at its strongest state, so a test can weaken exactly one."""

    base = {
        "linguistic": review("human-verified"),
        "pedagogical": review("teacher-verified"),
        "source-alignment": review("verified"),
        "rights": review("cleared"),
        "privacy": review("shareable"),
    }
    base.update(overrides)
    return base


def test_every_axis_has_its_own_ordered_state_vocabulary() -> None:
    assert set(AXIS_STATES) == set(REVIEW_AXES)
    for axis, states in AXIS_STATES.items():
        assert states == tuple(dict.fromkeys(states)), axis
        assert [state_strength(axis, state) for state in states] == list(range(len(states)))


def test_a_state_from_another_axis_is_not_a_state_of_this_one() -> None:
    assert is_known_state("linguistic", "human-verified")
    assert not is_known_state("linguistic", "teacher-verified")
    assert not is_known_state("pedagogical", "reference-verified")
    with pytest.raises(ValueError):
        state_strength("linguistic", "cleared")


def test_not_applicable_and_a_negative_verdict_are_not_weak_passes() -> None:
    assert state_strength("source-alignment", "not-applicable") == -1
    assert state_strength("rights", "restricted") == -1


def test_an_unpromoted_lifecycle_demands_nothing_yet() -> None:
    for lifecycle in UNPROMOTED_LIFECYCLES:
        assert required_strengths(risk_tier=4, lifecycle=lifecycle) == dict.fromkeys(REVIEW_AXES, 0)
        assert (
            gate_problems(
                risk_tier=4,
                lifecycle=lifecycle,
                reviews={},
                origin_classes=[OriginClass.AI_GENERATED],
            )
            == ()
        )


def test_the_requirement_is_the_maximum_of_the_tier_and_the_lifecycle() -> None:
    """A tier-3 item promoted only to `approved-personal` still owes tier 3's review."""

    tier_three = required_strengths(risk_tier=3, lifecycle="approved-personal")
    assert tier_three["linguistic"] == state_strength("linguistic", "reference-verified")
    assert tier_three["pedagogical"] == state_strength("pedagogical", "teacher-verified")
    published = required_strengths(risk_tier=0, lifecycle="publication-ready")
    assert published["privacy"] == state_strength("privacy", "shareable")


@pytest.mark.parametrize("kind", [ReviewerKind.MACHINE, ReviewerKind.AI])
@pytest.mark.parametrize(
    ("axis", "state"),
    [
        ("linguistic", "reference-verified"),
        ("linguistic", "human-verified"),
        ("pedagogical", "learner-approved"),
        ("pedagogical", "teacher-verified"),
        ("source-alignment", "verified"),
        ("rights", "personal-use-only"),
        ("privacy", "redacted"),
    ],
)
def test_a_machine_reviewer_can_never_reach_a_human_or_reference_claim(
    kind: ReviewerKind, axis: str, state: str
) -> None:
    problems = gate_problems(
        risk_tier=0,
        lifecycle="verified",
        reviews=full_reviews(**{axis: review(state, kind=kind)}),
        origin_classes=[OriginClass.HUMAN_AUTHORED],
    )

    assert any("machine checking stops at" in problem.reason for problem in problems)


def test_running_a_machine_check_twice_does_not_promote_it() -> None:
    """The ceiling is on the reviewer's kind, not on how many times it ran."""

    machine = review("machine-checked", kind=ReviewerKind.AI)
    problems = gate_problems(
        risk_tier=2,
        lifecycle="verified",
        reviews=full_reviews(linguistic=machine),
        origin_classes=[OriginClass.AI_GENERATED],
        source_references=["a source"],
    )

    assert [problem.axis for problem in problems] == ["linguistic"]
    assert "weaker than reference-verified" in problems[0].reason


@pytest.mark.parametrize("tier", [3, 4])
def test_high_risk_content_needs_a_verified_source_alignment_not_not_applicable(
    tier: int,
) -> None:
    problems = gate_problems(
        risk_tier=tier,
        lifecycle="verified",
        reviews=full_reviews(**{"source-alignment": review("not-applicable", method=None)}),
        origin_classes=[OriginClass.HUMAN_AUTHORED],
        source_references=["a reference"],
    )

    assert any(
        problem.axis == "source-alignment" and "not not-applicable" in problem.reason
        for problem in problems
    )


def test_canonical_content_of_purely_ai_origin_needs_an_identified_source() -> None:
    reviews = full_reviews()

    without = gate_problems(
        risk_tier=3,
        lifecycle="verified",
        reviews=reviews,
        origin_classes=[OriginClass.AI_GENERATED, OriginClass.AI_ADAPTED],
    )
    with_source = gate_problems(
        risk_tier=3,
        lifecycle="verified",
        reviews=reviews,
        origin_classes=[OriginClass.AI_GENERATED],
        source_references=["Wielki słownik poprawnej polszczyzny PWN"],
    )
    mixed_origin = gate_problems(
        risk_tier=3,
        lifecycle="verified",
        reviews=reviews,
        origin_classes=[OriginClass.AI_ADAPTED, OriginClass.AUTHENTIC_SOURCE],
    )

    assert any("identified external source" in problem.reason for problem in without)
    assert with_source == ()
    assert mixed_origin == ()


def test_a_negative_rights_verdict_is_a_failure_not_an_unfinished_review() -> None:
    problems = gate_problems(
        risk_tier=1,
        lifecycle="approved-personal",
        reviews=full_reviews(rights=review("restricted")),
        origin_classes=[OriginClass.HUMAN_AUTHORED],
    )

    assert [problem.axis for problem in problems] == ["rights"]
    assert "negative verdict" in problems[0].reason


def test_a_review_that_satisfies_a_requirement_must_say_how() -> None:
    problems = gate_problems(
        risk_tier=2,
        lifecycle="approved-personal",
        reviews=full_reviews(linguistic=review("human-verified", method=None)),
        origin_classes=[OriginClass.HUMAN_AUTHORED],
    )

    assert [(problem.axis, problem.reason) for problem in problems] == [
        ("linguistic", "a completed review must record its method")
    ]


def test_a_missing_review_is_named_rather_than_assumed() -> None:
    reviews = full_reviews()
    del reviews["pedagogical"]

    problems = gate_problems(
        risk_tier=2,
        lifecycle="approved-personal",
        reviews=reviews,
        origin_classes=[OriginClass.HUMAN_AUTHORED],
    )

    assert [(problem.axis, problem.reason) for problem in problems] == [
        ("pedagogical", "review is missing")
    ]


def test_an_unknown_axis_or_state_is_reported_before_any_strength_is_compared() -> None:
    unknown_axis = gate_problems(
        risk_tier=0,
        lifecycle="verified",
        reviews={"phonology": review("verified")},
        origin_classes=[OriginClass.HUMAN_AUTHORED],
    )
    unknown_state = gate_problems(
        risk_tier=0,
        lifecycle="verified",
        reviews=full_reviews(linguistic=review("perfect")),
        origin_classes=[OriginClass.HUMAN_AUTHORED],
    )

    assert "unknown review axes" in unknown_axis[0].reason
    assert "not a state of this axis" in unknown_state[0].reason


def test_a_weak_state_on_an_unrequired_axis_is_not_required_rather_than_passed() -> None:
    """Reporting `unchecked` as a pass would claim a check nobody performed."""

    assert review_gate_state("source-alignment", review("unchecked"), required=0) == (
        ReviewState.NOT_REQUIRED
    )
    assert review_gate_state("source-alignment", None, required=0) == ReviewState.NOT_REQUIRED
    assert review_gate_state("source-alignment", None, required=1) == ReviewState.PENDING
    assert (
        review_gate_state("source-alignment", review("not-applicable", method=None), required=0)
        == ReviewState.NOT_REQUIRED
    )


@pytest.mark.parametrize("lifecycle", PROMOTED_LIFECYCLES)
@pytest.mark.parametrize("risk_tier", [0, 1, 2, 3, 4])
def test_the_stage_two_gate_never_falls_below_the_frozen_content_contract(
    lifecycle: str, risk_tier: int
) -> None:
    """Whatever the Stage 2 gate accepts, `lingua.content.v1` must accept too.

    ADR 0005 froze a v1 floor. Stage 2 may add kind-specific policy on top of it, so the
    two are checked for agreement rather than trusted to agree.
    """

    reviews = full_reviews()
    problems = gate_problems(
        risk_tier=risk_tier,
        lifecycle=lifecycle,
        reviews=reviews,
        origin_classes=[OriginClass.HUMAN_AUTHORED],
        source_references=["a reference"],
    )
    assert problems == ()

    content_id = ContentId.derive("test", "knowledge", f"{lifecycle}.{risk_tier}")
    provenance = ContentProvenance(
        origin=CONTENT_ORIGIN_BY_CLASS[OriginClass.HUMAN_AUTHORED],
        source_references=("a reference",),
        rights="test rights",
        privacy="public",
    )
    payload = {
        "schema_name": "lingua.content.v1",
        "schema_version": 1,
        "content_id": str(content_id),
        "language": "qix-Latn",
        "kind": "lexeme",
        "title": "title",
        "body": "body",
        "risk_tier": risk_tier,
        "provenance": provenance.model_dump(mode="json"),
        "dependencies": [],
    }
    content_hash = canonical_content_hash(payload)

    item = ContentItem(
        content_id=content_id,
        language="qix-Latn",
        kind="lexeme",
        title="title",
        body="body",
        content_hash=content_hash,
        lifecycle=content_lifecycle(lifecycle),  # type: ignore[arg-type]
        risk_tier=risk_tier,
        provenance=provenance,
        reviews=content_reviews(
            risk_tier=risk_tier,
            lifecycle=lifecycle,
            reviews=reviews,
            content_hash=content_hash,
        ),
    )

    assert item.lifecycle == lifecycle
    assert {entry.axis for entry in item.reviews} == set(REVIEW_AXES)


def test_a_projected_review_binds_to_the_hash_it_examined() -> None:
    projected = content_reviews(
        risk_tier=2,
        lifecycle="verified",
        reviews=full_reviews(),
        content_hash="a" * 64,
    )

    completed = [entry for entry in projected if entry.state == ReviewState.PASSED]
    assert completed
    assert all(entry.reviewed_content_hash == "a" * 64 for entry in completed)
    assert all(entry.method for entry in completed)


def test_an_unpromoted_item_projects_as_needs_review() -> None:
    assert content_lifecycle("draft") == "needs-review"
    assert content_lifecycle("rejected") == "needs-review"
    assert content_lifecycle("verified") == "verified"


@pytest.mark.parametrize(
    ("item_count", "expected"),
    [(0, 0), (1, 1), (4, 4), (5, 5), (10, 5), (25, 5), (26, 6), (100, 20)],
)
def test_a_sample_is_the_larger_of_a_fifth_and_five_items(item_count: int, expected: int) -> None:
    assert required_sample(item_count) == expected


def test_a_template_inspects_everything_until_it_is_stable_with_three_clean_runs() -> None:
    assert (
        sampling_policy(template_maturity="new", inspected_runs=99)
        is SamplingPolicy.FULL_INSPECTION
    )
    assert (
        sampling_policy(template_maturity="stable", inspected_runs=FULL_INSPECTION_RUNS - 1)
        is SamplingPolicy.FULL_INSPECTION
    )
    assert (
        sampling_policy(template_maturity="stable", inspected_runs=FULL_INSPECTION_RUNS)
        is SamplingPolicy.SAMPLED
    )
    assert (
        sampling_policy(template_maturity="quarantined", inspected_runs=99)
        is SamplingPolicy.FULL_INSPECTION
    )
    assert sample_size(policy=SamplingPolicy.FULL_INSPECTION, item_count=40) == 40
    assert sample_size(policy=SamplingPolicy.SAMPLED, item_count=40) == 8


def test_an_unknown_axis_or_tier_is_a_programming_error_not_a_pass() -> None:
    with pytest.raises(ValueError):
        axis_states("phonology")
    with pytest.raises(ValueError):
        required_strengths(risk_tier=5, lifecycle="verified")
