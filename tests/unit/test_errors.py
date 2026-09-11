"""Error identity, the uncertain-match rule, and the lifecycle.

Two properties matter more than any single case. Normalization has to work for a
language that separates words and one that does not, and the resolution policy has to be
unable to accept a fix it has not seen -- including when someone reconfigures it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from linguawiki.error_model import (
    CERTAIN_CEILING,
    DEFAULT_POLICY,
    LIVE_STATUSES,
    QUALIFICATIONS,
    STATUSES,
    UNCERTAIN_FLOOR,
    CounterEvidence,
    MatchCandidate,
    ResolutionPolicy,
    assert_known_status,
    assert_policy_is_sound,
    classify_match,
    next_status_after_occurrence,
    next_status_after_success,
    normalize_signature,
    qualifications_for,
    signature_similarity,
    uncertain_matches,
)
from linguawiki.errors import LinguaWikiError

EARLIER = datetime(2026, 8, 1, tzinfo=UTC)
LATER = datetime(2026, 9, 1, tzinfo=UTC)


def counter(**overrides: object) -> CounterEvidence:
    arguments: dict[str, object] = {
        "successes": 3,
        "contexts": 2,
        "qualifications": {"controlled": 3, "novel": 2, "spontaneous": 1, "delayed": 1},
    }
    arguments.update(overrides)
    return CounterEvidence(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Nie-ma!  CZASU", "nie ma czasu"),
        ("szukam  biletu", "Szukam biletu."),
        ("«dworzec»", "dworzec"),
        # A no-break space is a separator like any other, written as an escape so the
        # test data cannot be mistaken for an ordinary space.
        ("dworzec\u00a0kolejowy", "dworzec kolejowy"),
    ],
)
def test_punctuation_case_and_separators_do_not_make_two_errors(left: str, right: str) -> None:
    assert normalize_signature(left) == normalize_signature(right)


def test_normalization_never_splits_on_whitespace() -> None:
    """A non-whitespace language has to dedupe as reliably as a whitespace one."""

    spaced = normalize_signature("wo shi xuesheng")
    unspaced = normalize_signature("woshixuesheng")

    assert spaced == unspaced


def test_a_tone_or_diacritic_is_part_of_the_signature_not_noise() -> None:
    """In a tonal language the tone is the word; folding it away merges two errors."""

    assert normalize_signature("māma") != normalize_signature("mama")
    assert normalize_signature("zażółć") != normalize_signature("zazolc")


def test_a_decomposed_and_a_composed_spelling_are_one_signature() -> None:
    assert normalize_signature("māma") == normalize_signature("māma")


def test_a_signature_with_no_identity_is_refused() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        normalize_signature("  ...!!  ")

    assert failure.value.payload.code == "empty_error_signature"


def test_identical_signatures_are_the_same_error() -> None:
    assert signature_similarity("niemaczasu", "niemaczasu") == 1.0
    assert classify_match(1.0) == "same"


def test_unrelated_signatures_are_different_errors() -> None:
    similarity = signature_similarity("niemaczasu", "dworzeckolejowy")

    assert similarity < UNCERTAIN_FLOOR
    assert classify_match(similarity) == "different"


def test_a_near_miss_is_nobody_s_call_to_make() -> None:
    """Merging two different errors is irreversible; asking costs one flag."""

    similarity = signature_similarity("szukambiletu", "szukambiletow")

    assert UNCERTAIN_FLOOR <= similarity < CERTAIN_CEILING
    assert classify_match(similarity) == "uncertain"


def test_uncertain_candidates_come_back_strongest_first() -> None:
    candidates = (
        MatchCandidate(
            error_id="err_a", signature="szukambiletow", similarity=0.0, status="active"
        ),
        MatchCandidate(
            error_id="err_b", signature="dworzeckolejowy", similarity=0.0, status="active"
        ),
        MatchCandidate(
            error_id="err_c", signature="szukambiletach", similarity=0.0, status="active"
        ),
    )

    close = uncertain_matches("szukambiletu", candidates)

    assert [candidate.error_id for candidate in close] == ["err_a", "err_c"]
    assert close[0].similarity >= close[1].similarity


def test_a_second_occurrence_activates_a_merely_observed_error() -> None:
    transition = next_status_after_occurrence("observed", occurrences=2)

    assert transition.status == "active"
    assert transition.changed is True


def test_a_first_occurrence_leaves_the_error_observed() -> None:
    transition = next_status_after_occurrence("observed", occurrences=1)

    assert transition.status == "observed"
    assert transition.changed is False


@pytest.mark.parametrize("status", ["monitoring", "resolved"])
def test_a_recurrence_reopens_the_same_error_rather_than_starting_a_new_one(
    status: str,
) -> None:
    transition = next_status_after_occurrence(status, occurrences=5)

    assert transition.status == "reactivated"
    assert "same error" in transition.reason


def test_one_success_can_never_resolve_an_error() -> None:
    transition = next_status_after_success(
        "active", counter=counter(successes=1, contexts=1, qualifications={"controlled": 1})
    )

    assert transition.status == "active"
    assert "1/2" in transition.reason or "1/3" in transition.reason


def test_enough_controlled_success_earns_monitoring_but_not_resolution() -> None:
    transition = next_status_after_success(
        "active", counter=counter(successes=2, contexts=1, qualifications={"controlled": 2})
    )

    assert transition.status == "monitoring"
    assert "not to resolve" in transition.reason


def test_the_full_policy_resolves_and_names_what_satisfied_it() -> None:
    transition = next_status_after_success("monitoring", counter=counter())

    assert transition.status == "resolved"
    assert "novel" in transition.reason and "delayed" in transition.reason


def test_success_in_one_context_cannot_resolve_however_many_times() -> None:
    transition = next_status_after_success(
        "monitoring",
        counter=counter(
            successes=9,
            contexts=1,
            qualifications={"controlled": 9, "novel": 9, "spontaneous": 9, "delayed": 9},
        ),
    )

    assert transition.status != "resolved"
    assert "distinct context" in transition.reason


def test_a_recurrence_after_the_last_success_blocks_resolution() -> None:
    """Counter-evidence that predates the newest occurrence has been overtaken."""

    transition = next_status_after_success(
        "monitoring",
        counter=counter(last_success_at=EARLIER, last_occurrence_at=LATER),
    )

    assert transition.status != "resolved"


def test_a_resolved_error_stays_resolved_under_more_success() -> None:
    transition = next_status_after_success("resolved", counter=counter())

    assert transition.status == "resolved"
    assert transition.changed is False


def qualifications(**overrides: str) -> tuple[str, ...]:
    arguments = {
        "claim": "spontaneous-production",
        "retrieval": "delayed",
        "novelty": "novel",
        "help_level": "none",
        "modality": "writing",
        "task_type": "extended-productive",
    }
    arguments.update(overrides)
    return qualifications_for(**arguments)  # type: ignore[arg-type]


def test_qualifications_are_derived_from_the_evidence_not_asserted() -> None:
    clean = qualifications()
    hinted_repeat = qualifications(
        claim="recognition",
        retrieval="immediate",
        novelty="repeat",
        help_level="hinted",
        modality="text",
        task_type="objective",
    )

    assert set(clean) == {"controlled", "novel", "spontaneous", "delayed"}
    assert hinted_repeat == ()


def test_a_prompted_success_is_controlled_but_never_spontaneous() -> None:
    assert qualifications(
        claim="controlled-production",
        retrieval="same-session",
        novelty="repeat",
        help_level="prompted",
        task_type="short-response",
    ) == ("controlled",)


def test_recognition_is_never_controlled_production() -> None:
    """`controlled` is a term about production. An unhinted recognition is not one.

    Reading it off the help level alone let receptive evidence satisfy a production
    error's requirements, which is what the resolution policy exists to prevent.
    """

    for task_type, modality in (
        ("objective", "text"),
        ("reading-comprehension", "text"),
        ("listening-comprehension", "audio"),
    ):
        assert qualifications(
            claim="recognition",
            retrieval="immediate",
            help_level="none",
            modality=modality,
            task_type=task_type,
        ) == ("novel",)


def test_a_delayed_receptive_success_is_delayed_but_not_spontaneous() -> None:
    """`delayed-transfer` says *when* it happened, not what was demanded.

    Its permitted task types span reading and speaking alike, so treating the claim
    itself as spontaneous let three delayed reading checks retire a production error.
    """

    derived = qualifications(
        claim="delayed-transfer",
        retrieval="delayed",
        help_level="none",
        modality="text",
        task_type="reading-comprehension",
    )

    assert set(derived) == {"novel", "delayed"}
    assert "spontaneous" not in derived
    assert "controlled" not in derived


def test_a_delayed_productive_success_is_every_qualification() -> None:
    """The strongest evidence there is must still be able to retire an error."""

    derived = qualifications(
        claim="delayed-transfer",
        retrieval="delayed",
        help_level="none",
        modality="speech",
        task_type="connected-speech",
    )

    assert set(derived) == {"controlled", "novel", "spontaneous", "delayed"}


def test_filling_a_slot_is_controlled_but_not_spontaneous() -> None:
    """Spontaneity is a property of the demand: the learner chose the form themselves."""

    derived = qualifications(
        claim="controlled-production",
        retrieval="immediate",
        help_level="none",
        modality="writing",
        task_type="recall-prompt",
    )

    assert derived == ("controlled", "novel")


def test_a_written_selection_task_produces_no_production_qualification() -> None:
    """Choosing between offered options is not producing language, in any modality."""

    derived = qualifications(
        claim="recognition",
        retrieval="immediate",
        help_level="none",
        modality="writing",
        task_type="objective",
    )

    assert derived == ("novel",)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"resolution_successes": 1}, "resolution_successes"),
        ({"resolution_contexts": 1}, "resolution_contexts"),
        ({"resolution_qualifications": ()}, "resolution_qualifications"),
        ({"activate_at_occurrences": 0}, "activate_at_occurrences"),
        ({"monitoring_successes": 0}, "monitoring_successes"),
        ({"resolution_qualifications": ("vibes",)}, "qualifications"),
    ],
)
def test_a_policy_that_could_accept_a_fix_it_has_not_seen_is_refused(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        assert_policy_is_sound(ResolutionPolicy(**overrides))  # type: ignore[arg-type]

    assert failure.value.payload.code == "invalid_error_policy"
    assert field in {detail.field for detail in failure.value.payload.details}


def test_the_default_policy_is_sound() -> None:
    assert assert_policy_is_sound(DEFAULT_POLICY) is DEFAULT_POLICY


def test_an_unknown_status_is_refused_by_name() -> None:
    with pytest.raises(LinguaWikiError) as failure:
        assert_known_status("fixed")

    assert failure.value.payload.code == "unknown_error_status"


def test_the_live_statuses_are_the_ones_still_counted_against_the_learner() -> None:
    assert set(LIVE_STATUSES) <= set(STATUSES)
    assert "resolved" not in LIVE_STATUSES
    assert "monitoring" not in LIVE_STATUSES


def test_every_qualification_the_policy_names_exists() -> None:
    named = {*DEFAULT_POLICY.resolution_qualifications, *DEFAULT_POLICY.monitoring_qualifications}

    assert named <= set(QUALIFICATIONS)
