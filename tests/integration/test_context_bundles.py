"""Bundles have to be bounded, honest about what they left out, and privacy-aware.

The failure this file exists to prevent is a bundle that looks complete: an agent shown
three of eleven active errors with no omission recorded will plan as though there were
three. So every test here checks the counts and the reasons alongside the content.
"""

from __future__ import annotations

import json

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.services import context as context_service
from linguawiki.services import errors as error_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import learners as learner_service
from tests.conftest import PolishWorkspace

ITEM = "pl.lex.dworzec"
OTHER = "pl.lex.bilet"


def observe(workspace: PolishWorkspace, *, context: str, item: str = ITEM) -> object:
    return evidence_service.record(
        workspace.paths,
        task_type="objective",
        modality="text",
        score=1.0,
        target=item,
        dimension="reading",
        context=context,
        track=workspace.track_id,
        clock=workspace.clock,
    )


def record_error(workspace: PolishWorkspace, index: int) -> str:
    report = error_service.record(
        workspace.paths,
        category=f"category-{index}",
        signature=f"signature number {index}",
        description=f"A synthetic recurring error, number {index}.",
        target=OTHER,
        learner_form=f"learner form {index}",
        corrected_form=f"corrected form {index}",
        severity="high" if index == 0 else "low",
        track=workspace.track_id,
        clock=workspace.clock,
    )
    return report.error_id


@pytest.mark.parametrize("scope", context_service.SCOPES)
def test_every_scope_builds_a_bundle_within_its_budget(
    scope: str, polish_workspace: PolishWorkspace
) -> None:
    observe(polish_workspace, context="objective:one")

    bundle = context_service.build(
        polish_workspace.paths,
        scope=scope,
        item=ITEM if scope == "concept" else None,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.scope == scope
    assert bundle.estimated_tokens <= bundle.token_limit
    assert bundle.sections == context_service.BUDGETS[scope].sections
    assert bundle.provenance is not None


def test_a_scope_carries_only_the_sections_it_declares(
    polish_workspace: PolishWorkspace,
) -> None:
    """Relevance is a property of the code, not a habit at the call site."""

    record_error(polish_workspace, 0)
    observe(polish_workspace, context="objective:one")

    assessment = context_service.build(
        polish_workspace.paths,
        scope="assessment",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert "active_errors" not in assessment.sections
    assert "recent_evidence" not in assessment.sections
    assert assessment.active_errors == ()
    assert assessment.recent_evidence == ()
    assert assessment.exposure is not None


def test_an_assessment_bundle_never_primes_the_assessor_with_the_learner_s_mistakes(
    polish_workspace: PolishWorkspace,
) -> None:
    """An assessor that knows the usual mistakes is scoring its own expectations."""

    for index in range(3):
        record_error(polish_workspace, index)

    serialized = json.dumps(
        context_service.build(
            polish_workspace.paths,
            scope="assessment",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        ).model_dump(mode="json")
    )

    assert "signature number" not in serialized
    assert "learner form" not in serialized


def test_a_session_bundle_reports_the_errors_it_could_not_fit(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(12):
        record_error(polish_workspace, index)

    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        max_records=14,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    omitted = {omission.section: omission for omission in bundle.omissions}
    assert "active_errors" in omitted
    assert omitted["active_errors"].available == 12
    assert omitted["active_errors"].included == len(bundle.active_errors)
    assert omitted["active_errors"].included < 12
    assert bundle.bounded is True


def test_the_highest_severity_errors_are_the_ones_that_survive_the_cap(
    polish_workspace: PolishWorkspace,
) -> None:
    for index in range(12):
        record_error(polish_workspace, index)

    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        max_records=14,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.active_errors
    assert bundle.active_errors[0].severity == "high"


def test_a_bundle_that_cannot_meet_its_budget_drops_sections_whole_and_says_so(
    polish_workspace: PolishWorkspace,
) -> None:
    """Half a list looks exactly like a short one, so sections go whole."""

    for index in range(6):
        record_error(polish_workspace, index)
    observe(polish_workspace, context="objective:one")

    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        max_tokens=400,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.bounded is True
    assert bundle.omissions
    dropped = {omission.section for omission in bundle.omissions}
    assert "recent_evidence" in dropped
    assert bundle.sections != context_service.BUDGETS["session"].sections
    assert "provenance" in bundle.sections, "a bundle must stay traceable"
    assert bundle.provenance is not None


def test_a_budget_that_cannot_be_met_at_all_is_reported_not_silently_accepted(
    polish_workspace: PolishWorkspace,
) -> None:
    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        max_tokens=200,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.sections == ("provenance",)
    assert any("could not be met" in warning for warning in bundle.warnings)
    assert any("--max-tokens" in warning for warning in bundle.warnings)


def test_error_occurrences_are_withheld_without_the_request_and_the_consent(
    polish_workspace: PolishWorkspace,
) -> None:
    record_error(polish_workspace, 0)

    without = context_service.build(
        polish_workspace.paths,
        scope="session",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert without.responses_included is False
    assert without.active_errors
    assert without.active_errors[0].occurrences == ()
    assert any("own words" in warning for warning in without.warnings)
    assert "learner form" not in json.dumps(without.model_dump(mode="json"))


def test_the_learner_s_words_appear_only_when_asked_for_and_consented_to(
    polish_workspace: PolishWorkspace,
) -> None:
    record_error(polish_workspace, 0)

    with_consent = context_service.build(
        polish_workspace.paths,
        scope="session",
        include_responses=True,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert with_consent.responses_included is True
    assert with_consent.active_errors[0].occurrences


def test_asking_for_the_learner_s_words_without_consent_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    record_error(polish_workspace, 0)
    learner_service.update_track(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=polish_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        context_service.build(
            polish_workspace.paths,
            scope="session",
            include_responses=True,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "transcript_consent_required"


def test_a_concept_bundle_is_about_one_item_and_needs_to_be_told_which(
    polish_workspace: PolishWorkspace,
) -> None:
    observe(polish_workspace, context="objective:one")

    bundle = context_service.build(
        polish_workspace.paths,
        scope="concept",
        item=ITEM,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.focus is not None
    assert bundle.focus.stable_key == ITEM
    assert bundle.item_state is not None
    assert bundle.recent_evidence
    assert all(
        entry.target_content_id == bundle.focus.content_id for entry in bundle.recent_evidence
    )

    with pytest.raises(LinguaWikiError) as failure:
        context_service.build(
            polish_workspace.paths,
            scope="concept",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "context_item_required"


def test_a_concept_bundle_carries_only_that_item_s_errors(
    polish_workspace: PolishWorkspace,
) -> None:
    record_error(polish_workspace, 0)
    observe(polish_workspace, context="objective:one")

    about_other = context_service.build(
        polish_workspace.paths,
        scope="concept",
        item=ITEM,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    about_target = context_service.build(
        polish_workspace.paths,
        scope="concept",
        item=OTHER,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert about_other.active_errors == ()
    assert about_target.active_errors


def test_a_bundle_names_the_versions_its_conclusions_came_from(
    polish_workspace: PolishWorkspace,
) -> None:
    """Traceability: an agent has to be able to say which policy produced a stage."""

    observe(polish_workspace, context="objective:one")

    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.provenance is not None
    assert bundle.provenance.pack_key == "pl-pilot"
    assert bundle.provenance.pack_checksum
    assert bundle.provenance.aggregation_version.startswith("mastery.")
    assert bundle.provenance.estimate_calculation_version.startswith("estimate.")
    assert bundle.provenance.error_policy_version.startswith("error.")
    assert bundle.provenance.content_lifecycles


def test_a_session_bundle_carries_the_consent_flags_an_agent_must_respect(
    polish_workspace: PolishWorkspace,
) -> None:
    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.profile is not None
    assert bundle.profile.transcript_retention_consent is True
    assert bundle.profile.correction_mode == "accuracy"
    assert "podróże" in bundle.profile.interests


def test_a_source_bundle_carries_the_rights_a_recommendation_was_shipped_with(
    polish_workspace: PolishWorkspace,
) -> None:
    bundle = context_service.build(
        polish_workspace.paths,
        scope="source",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.sources
    assert all(candidate.rights_status for candidate in bundle.sources)
    assert "active_errors" not in bundle.sections


def test_due_work_lists_items_that_are_neither_unseen_nor_stable(
    polish_workspace: PolishWorkspace,
) -> None:
    observe(polish_workspace, context="objective:one")
    error_service.add_followup(
        polish_workspace.paths,
        kind="practice",
        action="Drill the genitive after szukać.",
        target=OTHER,
        priority=3,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert bundle.due_work
    assert bundle.due_work[0].kind == "practice"
    assert [entry.stable_key for entry in bundle.due_items] == [ITEM]


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"scope": "vibes"}, "unknown_context_scope"),
        ({"scope": "session", "max_records": 0}, "invalid_record_limit"),
        ({"scope": "session", "max_records": 10_000}, "invalid_record_limit"),
        ({"scope": "session", "max_tokens": 10}, "invalid_token_limit"),
        ({"scope": "session", "max_tokens": 10_000_000}, "invalid_token_limit"),
    ],
)
def test_an_impossible_bound_is_refused_by_name(
    arguments: dict[str, object], code: str, polish_workspace: PolishWorkspace
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        context_service.build(
            polish_workspace.paths,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
            **arguments,  # type: ignore[arg-type]
        )

    assert failure.value.payload.code == code


def test_the_token_estimate_is_deterministic_and_grows_with_the_payload() -> None:
    small = context_service.estimated_tokens({"a": 1})
    large = context_service.estimated_tokens({"a": 1, "b": "x" * 400})

    assert small == context_service.estimated_tokens({"a": 1})
    assert large > small


def test_a_section_that_was_capped_and_then_dropped_reports_one_true_count(
    polish_workspace: PolishWorkspace,
) -> None:
    """Two omissions for one section, the second under-reporting the total, would mislead.

    A section capped by the record limit records how many existed. If the token budget
    then drops it whole, that entry must be *replaced* rather than joined by a second one
    reporting the capped count as the total.
    """

    for index in range(12):
        record_error(polish_workspace, index)

    bundle = context_service.build(
        polish_workspace.paths,
        scope="session",
        max_records=14,
        max_tokens=500,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    sections = [omission.section for omission in bundle.omissions]
    assert len(sections) == len(set(sections)), "one authoritative omission per section"
    errors = next(omission for omission in bundle.omissions if omission.section == "active_errors")
    assert errors.available == 12
    assert errors.included == 0
    assert bundle.active_errors == ()
