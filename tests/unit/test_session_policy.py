"""The session lifecycle and shape policy, on its own.

These are the rules a reviewer or a learner may want to read, so they are tested as
rules: which move is legal, how a duration is divided, what may share a block, and how
much new material one sitting may carry.
"""

from __future__ import annotations

import pytest

from linguawiki import evidence as evidence_policy
from linguawiki import placement, session
from linguawiki.errors import LinguaWikiError


def test_the_policy_is_self_consistent() -> None:
    """The soundness check is the first line of defence and must pass as shipped."""

    session.assert_policy_is_sound()


def test_every_weekly_target_names_an_area_some_block_produces() -> None:
    produced = {block.area for block in session.BLOCK_TYPES}

    assert set(session.WEEKLY_BLOCK_TARGETS) <= produced
    assert "closure" not in session.WEEKLY_BLOCK_TARGETS, (
        "closure time is session overhead and must not consume a weekly practice quota"
    )


def test_block_dimension_kinds_are_the_placement_policy_s_own() -> None:
    """A block names a dimension *kind*, and the kinds belong to the placement policy.

    If these drifted, a block would claim a kind no pack can declare, and the service
    would silently fail to resolve a dimension for it.
    """

    assert set(session.DIMENSION_KINDS) == set(placement.BUDGETS)
    assert {block.dimension_kind for block in session.BLOCK_TYPES} <= set(session.DIMENSION_KINDS)


def test_block_modalities_are_the_evidence_policy_s_own() -> None:
    assert set(session.MODALITIES) == set(evidence_policy.MODALITIES)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("planned", "active"),
        ("planned", "abandoned"),
        ("active", "closing"),
        ("active", "abandoned"),
        ("closing", "completed"),
        ("closing", "partial"),
        ("closing", "abandoned"),
        # A close that crashed mid-transaction is retried, and the retry is the same move.
        ("closing", "closing"),
    ],
)
def test_legal_transitions_are_permitted(current: str, target: str) -> None:
    assert session.assert_transition(current=current, target=target, session_id="ses_x") == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("planned", "completed"),
        ("planned", "closing"),
        ("active", "completed"),
        ("completed", "active"),
        ("partial", "completed"),
        ("abandoned", "active"),
    ],
)
def test_illegal_transitions_are_refused_and_say_what_the_session_is(
    current: str, target: str
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        session.assert_transition(current=current, target=target, session_id="ses_x")

    assert failure.value.payload.code == "invalid_session_transition"
    assert current in failure.value.payload.message


def test_a_session_cannot_skip_the_closing_state() -> None:
    """`active -> completed` is the shortcut that would make a crash unrecoverable.

    The durable `closing` marker is the only thing that distinguishes "a close was
    interrupted" from "nobody ever tried", so the state machine refuses to bypass it.
    """

    with pytest.raises(LinguaWikiError):
        session.assert_transition(current="active", target="completed", session_id="ses_x")


@pytest.mark.parametrize("minutes", [20, 25, 40, 47, 60, 75, 80, 100, 120, 150, 180])
def test_a_shape_spends_exactly_the_minutes_requested(minutes: int) -> None:
    slots = session.shape_for(minutes)

    assert sum(slot.minutes for slot in slots) == minutes
    assert [slot.sequence for slot in slots] == list(range(1, len(slots) + 1))
    assert slots[0].role == "warm-up"
    assert slots[-1].role == "closure"
    assert all(slot.minutes > 0 for slot in slots)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(40, 2), (60, 3), (80, 4), (100, 5), (120, 6)],
)
def test_the_plan_s_own_duration_table_is_honoured(minutes: int, expected: int) -> None:
    """40, 60, 80, 100, and 120 minutes map onto two to six blocks, warm-up included."""

    slots = session.shape_for(minutes)
    practice = [slot for slot in slots if slot.role in ("warm-up", "core")]

    assert len(practice) == expected


@pytest.mark.parametrize("minutes", [0, 19, 181, 600])
def test_an_impossible_duration_is_refused(minutes: int) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        session.shape_for(minutes)

    assert failure.value.payload.code == "invalid_session_duration"


def test_the_closure_is_always_reserved_before_any_core_block() -> None:
    """The plan's hard constraint: closing retrieval time is taken out first."""

    for minutes in range(session.MINIMUM_MINUTES, session.MAXIMUM_MINUTES + 1):
        slots = session.shape_for(minutes)
        closure = slots[-1]
        assert closure.role == "closure"
        assert closure.minutes >= 2


def test_exam_conditions_cannot_share_a_block_with_coaching() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        session.assert_activities_compatible(
            block="writing",
            activities=("exam-task", "graduated-hints"),
            correction_mode="accuracy",
        )

    assert failure.value.payload.code == "incompatible_activities"


def test_exam_mode_forbids_the_activities_that_help() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        session.assert_activities_compatible(
            block="grammar-focus",
            activities=("elicitation", "controlled-practice"),
            correction_mode="exam",
        )

    assert failure.value.payload.code == "activity_forbidden_by_mode"


def test_compatible_activities_are_allowed() -> None:
    session.assert_activities_compatible(
        block="writing",
        activities=("controlled-practice", "free-production"),
        correction_mode="accuracy",
    )


@pytest.mark.parametrize("energy", ["low", "normal", "high"])
def test_novelty_scales_with_the_session_and_never_past_the_ceiling(energy: str) -> None:
    caps = [session.novel_target_cap(core_blocks=blocks, energy=energy) for blocks in range(1, 8)]

    assert caps == sorted(caps), "a longer session never introduces less new material"
    assert max(caps) <= session.MAXIMUM_NOVEL_TARGETS


def test_low_energy_reduces_both_the_blocks_and_the_new_material() -> None:
    slots = session.shape_for(120)

    assert session.core_block_limit(slots=slots, energy="low") < session.core_block_limit(
        slots=slots, energy="normal"
    )
    assert session.novel_target_cap(core_blocks=5, energy="low") < session.novel_target_cap(
        core_blocks=5, energy="normal"
    )


def test_a_block_holds_only_what_its_minutes_can_reach() -> None:
    assert session.block_target_limit(5) == 1
    assert session.block_target_limit(20) == 4
    assert session.block_target_limit(120) == session.MAXIMUM_BLOCK_TARGETS


def test_unknown_vocabulary_is_refused_by_name() -> None:
    for call, code in (
        (lambda: session.assert_known_mode("hypnosis"), "unknown_session_mode"),
        (lambda: session.assert_known_energy("caffeinated"), "unknown_energy_level"),
        (lambda: session.assert_known_status("napping"), "unknown_session_status"),
        (lambda: session.block_type("interpretive-dance"), "unknown_block_type"),
    ):
        with pytest.raises(LinguaWikiError) as failure:
            call()
        assert failure.value.payload.code == code
