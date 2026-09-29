"""The planner: what it scores, what it refuses, and what every plan must satisfy.

The property tests here are the ones the stage asks for -- duration and block ordering
across every duration the planner accepts -- and they are written against generated
candidate sets rather than one hand-built example, because the interesting failures are
the combinations nobody would think to write down.
"""

from __future__ import annotations

import itertools

import pytest

from linguawiki import planner, session
from linguawiki.errors import LinguaWikiError

DIMENSION_NAMES = {
    ("receptive", "text"): "reading",
    ("receptive", "audio"): "listening",
    ("form", "text"): "grammar-control",
    ("productive", "writing"): "writing",
    ("productive", "speech"): "spoken-production",
    ("pronunciation", "speech"): "pronunciation",
}


def target(content_id: str, **overrides: object) -> planner.CandidateTarget:
    payload: dict[str, object] = {
        "content_id": content_id,
        "title": content_id,
        "stage": "encountered",
        "novel": False,
        "due": False,
    }
    payload.update(overrides)
    return planner.CandidateTarget(**payload)  # type: ignore[arg-type]


def candidate(block_type: str, **overrides: object) -> planner.Candidate:
    policy = session.block_type(block_type)
    payload: dict[str, object] = {
        "block_type": block_type,
        "dimension": DIMENSION_NAMES[(policy.dimension_kind, policy.modality)],
        "targets": (target(f"cnt_{block_type[:4]}1", due=True), target(f"cnt_{block_type[:4]}2")),
    }
    payload.update(overrides)
    return planner.Candidate(**payload)  # type: ignore[arg-type]


def every_candidate() -> tuple[planner.Candidate, ...]:
    return tuple(
        candidate(block.name)
        for block in session.BLOCK_TYPES
        if block.name not in (session.WARM_UP_BLOCK, session.CLOSURE_BLOCK)
    )


def build(minutes: int, **overrides: object) -> planner.Plan:
    request_fields: dict[str, object] = {"minutes": minutes, "voice_available": True}
    request_fields.update(overrides)
    return planner.build_plan(
        request=planner.PlanRequest(**request_fields),  # type: ignore[arg-type]
        candidates=every_candidate(),
        deficits=session.WEEKLY_BLOCK_TARGETS,
        framing_dimension="reading",
    )


# --- The durations the stage names explicitly ---------------------------------------


@pytest.mark.parametrize("minutes", [40, 60, 80, 100, 120])
def test_the_required_durations_all_produce_valid_plans(minutes: int) -> None:
    plan = build(minutes)

    assert plan.blocks
    assert plan.planned_minutes == minutes
    assert plan.blocks[0].role == "warm-up"
    assert plan.blocks[-1].role == "closure"


# --- Property tests -----------------------------------------------------------------


@pytest.mark.parametrize("minutes", list(range(session.MINIMUM_MINUTES, 181, 7)))
def test_a_plan_never_exceeds_the_time_it_was_given(minutes: int) -> None:
    plan = build(minutes)

    assert plan.planned_minutes <= plan.requested_minutes
    if plan.planned_minutes < plan.requested_minutes:
        assert plan.warnings, "a shorter session than requested must say why"


@pytest.mark.parametrize("minutes", list(range(session.MINIMUM_MINUTES, 181, 11)))
def test_block_ordering_is_an_invariant(minutes: int) -> None:
    plan = build(minutes)
    roles = [block.role for block in plan.blocks]

    assert roles[0] == "warm-up"
    assert roles[-1] == "closure"
    assert roles.count("warm-up") == 1
    assert roles.count("closure") == 1
    assert all(role == "core" for role in roles[1:-1])
    assert [block.sequence for block in plan.blocks] == list(range(1, len(plan.blocks) + 1))


@pytest.mark.parametrize(
    ("minutes", "energy"),
    list(itertools.product([40, 60, 90, 120, 180], ["low", "normal", "high"])),
)
def test_novelty_never_exceeds_the_session_cap(minutes: int, energy: str) -> None:
    candidates = tuple(
        candidate(
            block.name,
            targets=tuple(target(f"cnt_{block.name[:4]}{index}", novel=True) for index in range(6)),
        )
        for block in session.BLOCK_TYPES
        if block.name not in (session.WARM_UP_BLOCK, session.CLOSURE_BLOCK)
    )
    plan = planner.build_plan(
        request=planner.PlanRequest(minutes=minutes, energy=energy, voice_available=True),
        candidates=candidates,
        framing_dimension="reading",
    )

    distinct = {entry.content_id for block in plan.blocks for entry in block.targets if entry.novel}
    assert len(distinct) <= plan.novel_target_cap
    assert plan.novel_targets == len(distinct)


@pytest.mark.parametrize("minutes", [40, 60, 80, 100, 120])
def test_a_framing_block_never_introduces_new_material(minutes: int) -> None:
    plan = build(minutes)

    for block in plan.blocks:
        if block.role == "core":
            continue
        assert block.novel_targets == 0
        assert not any(entry.novel for entry in block.targets)


def test_new_material_is_always_paired_with_productive_use() -> None:
    """The plan's own constraint, checked over every mode that could break it."""

    for mode in sorted(session.MODES):
        candidates = tuple(
            candidate(
                block.name,
                targets=(target(f"cnt_{block.name[:4]}n", novel=True),),
            )
            for block in session.BLOCK_TYPES
            if block.name not in (session.WARM_UP_BLOCK, session.CLOSURE_BLOCK)
        )
        try:
            plan = planner.build_plan(
                request=planner.PlanRequest(minutes=80, mode=mode, voice_available=True),
                candidates=candidates,
                framing_dimension="reading",
            )
        except LinguaWikiError as failure:  # a mode with no available candidate
            assert failure.payload.code == "session_mode_unavailable"
            continue
        if plan.novel_targets:
            assert any(session.block_type(block.block_type).productive for block in plan.blocks), (
                f"{mode} introduced new material with nowhere to use it"
            )


# --- Constraints and refusals -------------------------------------------------------


def test_an_explicit_mode_is_honoured() -> None:
    plan = build(80, mode="reading")

    assert plan.mode == "reading"
    assert {block.block_type for block in plan.blocks if block.role == "core"} <= set(
        session.MODES["reading"]
    )


def test_an_impossible_mode_is_refused_with_the_reason() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        planner.build_plan(
            request=planner.PlanRequest(minutes=60, mode="speaking", voice_available=False),
            candidates=(candidate("speaking"), candidate("pronunciation")),
            framing_dimension="reading",
        )

    assert failure.value.payload.code == "session_mode_unavailable"
    assert "voice" in failure.value.payload.message
    assert failure.value.payload.details


def test_a_failed_task_is_not_rescheduled_unchanged() -> None:
    plan = planner.build_plan(
        request=planner.PlanRequest(minutes=60),
        candidates=(
            candidate(
                "rich-review",
                targets=(
                    target("cnt_unchanged", due=True, last_outcome="failure"),
                    target("cnt_varied", due=True, last_outcome="failure", variation=True),
                ),
            ),
        ),
        framing_dimension="reading",
    )

    scheduled = {entry.content_id for block in plan.blocks for entry in block.targets}
    assert "cnt_varied" in scheduled
    assert "cnt_unchanged" not in scheduled
    assert any("not rescheduled unchanged" in warning for warning in plan.warnings)


def test_every_high_priority_omission_carries_a_reason() -> None:
    plan = build(40)

    assert plan.omissions, "a 40-minute session cannot hold every candidate"
    for omission in plan.omissions:
        assert omission.reason
        assert omission.score >= planner.OMISSION_THRESHOLD


def test_a_block_with_nothing_to_work_on_is_not_scheduled() -> None:
    plan = planner.build_plan(
        request=planner.PlanRequest(minutes=100, voice_available=True),
        candidates=(
            candidate("rich-review", targets=(target("cnt_a", due=True),)),
            candidate("reading", targets=()),
        ),
        framing_dimension="reading",
    )

    assert "reading" not in {block.block_type for block in plan.blocks}


def test_a_review_block_with_follow_ups_and_no_items_is_still_work() -> None:
    plan = planner.build_plan(
        request=planner.PlanRequest(minutes=60),
        candidates=(candidate("rich-review", targets=(), due_followups=3, active_errors=2),),
        framing_dimension="reading",
    )

    assert "rich-review" in {block.block_type for block in plan.blocks}


def test_the_same_plan_is_produced_from_the_same_inputs() -> None:
    """Two candidates that tie must not depend on the order the database returned them."""

    forward = every_candidate()
    backward = tuple(reversed(forward))
    first = planner.build_plan(
        request=planner.PlanRequest(minutes=100, voice_available=True),
        candidates=forward,
        framing_dimension="reading",
    )
    second = planner.build_plan(
        request=planner.PlanRequest(minutes=100, voice_available=True),
        candidates=backward,
        framing_dimension="reading",
    )

    assert [block.block_type for block in first.blocks] == [
        block.block_type for block in second.blocks
    ]


def test_a_repeat_covers_targets_the_first_block_did_not() -> None:
    plan = planner.build_plan(
        request=planner.PlanRequest(minutes=120),
        candidates=(
            candidate(
                "rich-review",
                targets=tuple(target(f"cnt_r{index}", due=True) for index in range(9)),
            ),
        ),
        framing_dimension="reading",
    )
    blocks = [block for block in plan.blocks if block.block_type == "rich-review"]

    assert len(blocks) == 2
    assert blocks[1].repeated
    first_targets = {entry.content_id for entry in blocks[0].targets}
    second_targets = {entry.content_id for entry in blocks[1].targets}
    assert not first_targets & second_targets


def test_an_explanation_is_derived_from_the_score_that_selected_the_block() -> None:
    scored = planner.score_candidate(
        candidate("listening", targets=(target("cnt_x", due=True),), uncertainty=0.8),
        request=planner.PlanRequest(minutes=60),
        deficits={"listening": 2},
        remaining_novelty=4,
    )
    reasons = planner.explain(scored)

    assert reasons
    for name, contribution in scored.contributions()[: len(reasons)]:
        assert f"{contribution:+.2f}" in " ".join(reasons)
        if name in planner.PENALTIES:
            assert any(reason.startswith("against: ") for reason in reasons)


def test_penalties_actually_subtract() -> None:
    """A sign error in the weights would silently invert the ranking."""

    for name in planner.PENALTIES:
        assert planner.WEIGHTS[name] < 0
    for name, weight in planner.WEIGHTS.items():
        if name not in planner.PENALTIES:
            assert weight > 0
