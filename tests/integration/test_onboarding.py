"""Declared-level onboarding, end to end, and what it must refuse to claim.

The exit gate for this stage is here: a Polish CEFR A2 track initializes inside a
workspace with Russian and English support, loads bounded prerequisites and resources
without marking anything mastered, and labels itself a pilot.
"""

from __future__ import annotations

import pytest

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import packs as pack_service
from linguawiki.services import resources as resource_service
from linguawiki.services import workspace as workspace_service
from tests.conftest import FIXTURE_PACKS, PolishWorkspace, SyntheticWorkspace


def test_the_stage_exit_gate_a_polish_a2_track_initializes_with_ru_en_support(
    polish_workspace: PolishWorkspace,
) -> None:
    track = learner_service.show_track(polish_workspace.paths, clock=polish_workspace.clock)
    user = learner_service.show_user(polish_workspace.paths, clock=polish_workspace.clock)

    assert track.target_language == "pl"
    assert track.proficiency_framework == "cefr"
    assert track.declared_level == "A2"
    assert user.native_languages == ("ru",)
    assert user.support_languages == ("en",)
    assert track.pack_key == "pl-pilot"


def test_starting_onboarding_seeds_low_confidence_hypotheses_and_no_mastery(
    polish_workspace: PolishWorkspace,
) -> None:
    report = onboarding_service.start(
        polish_workspace.paths,
        mode="declared-level",
        declared_level="A2",
        clock=polish_workspace.clock,
    )

    assert report.status == "awaiting-input"
    assert report.calibration_label == "pilot-calibration"
    assert len(report.seeded_estimates) == 7
    assert report.next_step == "finalize"

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        estimates = database.query(
            "SELECT dimension, level_code, confidence_label, basis, evidence_count "
            "FROM skill_estimates ORDER BY dimension"
        )
        stages = database.query("SELECT DISTINCT stage FROM track_item_state")

    assert estimates
    assert all(str(row[1]) == "A2" for row in estimates)
    assert all(str(row[2]) == "low" for row in estimates)
    assert all(str(row[3]) == "declared-hypothesis" for row in estimates)
    assert all(int(row[4]) == 0 for row in estimates)
    assert stages == [], "onboarding start must not create learner item state"


def test_only_the_answers_this_release_understands_are_stored(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.record_answer(
        polish_workspace.paths,
        key="can_do_summary",
        value=["order food", "ask directions"],
        clock=polish_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.record_answer(
            polish_workspace.paths,
            key="favourite_colour",
            value="blue",
            clock=polish_workspace.clock,
        )
    report = onboarding_service.status(polish_workspace.paths, clock=polish_workspace.clock)

    assert failure.value.payload.code == "unknown_answer_key"
    assert report.answers["can_do_summary"] == ["order food", "ask directions"]


def test_finalizing_prepares_bounded_resources_that_stay_unseen(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )

    report = onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)

    assert report.status == "finalized"
    assert report.resource_plan is not None
    assert report.resource_plan.plan_label == "pilot curriculum"
    assert report.resource_plan.imported_items > 0

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        states = database.query(
            "SELECT DISTINCT stage, stage_source FROM track_item_state ORDER BY stage"
        )
        estimates = database.query("SELECT DISTINCT basis FROM skill_estimates")

    assert states == [("unseen", "reference-import")]
    assert estimates == [("declared-hypothesis",)]


def test_finalizing_resolves_the_declared_band_and_its_prerequisites_only(
    polish_workspace: PolishWorkspace,
) -> None:
    """A2 must pull A1 in, and must not pull the whole pack in."""

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )

    report = onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)

    assert report.resource_plan is not None
    assert set(report.resource_plan.bundles) == {"cefr-a1-core", "cefr-a2-core"}
    assert report.resource_plan.level_codes == ("A2",)


def test_finalizing_builds_a_queue_that_samples_prerequisites_as_well_as_the_band(
    polish_workspace: PolishWorkspace,
) -> None:
    """A declared level is exactly the claim that needs checking underneath."""

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )

    report = onboarding_service.finalize(
        polish_workspace.paths, calibration_sample=6, clock=polish_workspace.clock
    )

    purposes = {entry.purpose for entry in report.calibration_queue}
    assert purposes == {"prerequisite-sample", "declared-band"}
    assert len(report.calibration_queue) == 12
    assert all(entry.status == "pending" for entry in report.calibration_queue)
    assert all(entry.rationale for entry in report.calibration_queue)


def test_finalizing_opens_a_labelled_pilot_calibration(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )

    report = onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)

    assert report.assessment_run_id is not None
    assert report.calibration_label == "pilot-calibration"
    assert any("pilot pack" in warning for warning in report.warnings)


def test_finalizing_is_idempotent_and_resumable(
    polish_workspace: PolishWorkspace,
) -> None:
    """A conversation may end between any two steps; the next one continues from state."""

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    first = onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)

    second = onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)

    assert second.onboarding_id == first.onboarding_id
    assert second.assessment_run_id == first.assessment_run_id
    assert second.status == "finalized"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert (
            int(database.scalar("SELECT count(*) FROM resource_plans WHERE status = 'applied'"))
            == 1
        )


def test_starting_twice_is_refused_while_a_run_is_open(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.start(
            polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "onboarding_in_progress"


def test_an_idempotency_key_returns_the_same_run(polish_workspace: PolishWorkspace) -> None:
    first = onboarding_service.start(
        polish_workspace.paths,
        declared_level="A2",
        idempotency_key="onboard-1",
        clock=polish_workspace.clock,
    )
    second = onboarding_service.start(
        polish_workspace.paths,
        declared_level="A2",
        idempotency_key="onboard-1",
        clock=polish_workspace.clock,
    )

    assert second.onboarding_id == first.onboarding_id


def test_abandoning_a_run_keeps_its_answers_for_audit(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.record_answer(
        polish_workspace.paths, key="known_gaps", value=["cases"], clock=polish_workspace.clock
    )

    abandoned = onboarding_service.abandon(polish_workspace.paths, clock=polish_workspace.clock)

    assert abandoned.status == "abandoned"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM onboarding_answers")) == 1
    # A new run may start once the old one is abandoned.
    fresh = onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    assert fresh.onboarding_id != abandoned.onboarding_id


def test_declared_level_onboarding_needs_a_level(polish_workspace: PolishWorkspace) -> None:
    learner_service.update_track(
        polish_workspace.paths, clock=polish_workspace.clock, declared_level=None
    )
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock):
        pass

    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.start(
            polish_workspace.paths, declared_level="Z9", clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "level_not_in_framework"


def test_placement_mode_is_refused_on_a_pilot_pack(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.start(
            polish_workspace.paths, mode="placement", clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "onboarding_mode_unsupported"
    assert failure.value.payload.details[0].context["maturity"] == "pilot"


def test_a_fixture_pack_serves_no_onboarding_mode_at_all(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    pack_service.install(
        synthetic_workspace.paths, FIXTURE_PACKS / "inflected", clock=synthetic_workspace.clock
    )
    learner_service.create_user(
        synthetic_workspace.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["en"],
        clock=synthetic_workspace.clock,
    )
    learner_service.create_track(
        synthetic_workspace.paths,
        target_language="qix",
        framework="fixture-bands-v1",
        declared_level="L2",
        clock=synthetic_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.start(
            synthetic_workspace.paths, declared_level="L2", clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "onboarding_mode_unsupported"


def test_a_plan_is_a_dry_run_until_it_is_prepared(polish_workspace: PolishWorkspace) -> None:
    plan = resource_service.plan(polish_workspace.paths, clock=polish_workspace.clock)

    assert plan.dry_run is True
    assert plan.plan_id is None
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM resource_plans")) == 0


def test_an_item_budget_truncates_the_plan_and_says_what_it_dropped(
    polish_workspace: PolishWorkspace,
) -> None:
    """A truncated plan must never look like a complete one."""

    plan = resource_service.plan(
        polish_workspace.paths, item_budget=20, clock=polish_workspace.clock
    )

    knowledge = [item for item in plan.items if item.item_kind == "knowledge"]
    dropped = [item for item in plan.skipped if item.item_kind == "knowledge"]
    assert len(knowledge) == 20
    assert dropped
    assert all("item budget of 20" in item.reason for item in dropped)
    # Prerequisites survive truncation; the newest material is what goes.
    assert any(item.bundle_key == "cefr-a1-core" for item in knowledge)


def test_an_avoided_topic_is_excluded_with_its_reason(
    polish_workspace: PolishWorkspace,
) -> None:
    learner_service.update_track(
        polish_workspace.paths,
        preferences=learner_service.TrackPreferences(avoided_topics=("zdrowie",)),
        clock=polish_workspace.clock,
    )

    plan = resource_service.plan(polish_workspace.paths, clock=polish_workspace.clock)

    excluded = [item for item in plan.skipped if "avoided-topics" in item.reason]
    assert excluded
    assert all(item.action == "skip" for item in excluded)


def test_external_sources_are_proposed_never_imported(
    polish_workspace: PolishWorkspace,
) -> None:
    plan = resource_service.plan(polish_workspace.paths, clock=polish_workspace.clock)

    assert plan.proposed_sources
    assert all(item.action == "propose" for item in plan.proposed_sources)
    assert all("never downloaded" in item.reason for item in plan.proposed_sources)
    assert not any(item.item_kind == "source_recommendation" for item in plan.items)


def test_preparing_twice_supersedes_the_earlier_plan_without_relearning_state(
    polish_workspace: PolishWorkspace,
) -> None:
    first = resource_service.prepare(polish_workspace.paths, clock=polish_workspace.clock)
    second = resource_service.prepare(polish_workspace.paths, clock=polish_workspace.clock)

    assert first.imported_items > 0
    assert second.imported_items == first.imported_items
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        statuses = database.query("SELECT status, count(*) FROM resource_plans GROUP BY status")
    assert dict(statuses) == {"applied": 1, "superseded": 1}


def test_preparation_reports_what_the_pack_cannot_serve(
    polish_workspace: PolishWorkspace,
) -> None:
    plan = resource_service.plan(polish_workspace.paths, clock=polish_workspace.clock)

    assert plan.pack_maturity == "pilot"
    assert plan.plan_label == "pilot curriculum"
    # The pilot pack tests every dimension it declares, so there is nothing to report;
    # the field exists and is empty rather than absent.
    assert plan.unsupported_dimensions == ()
    assert plan.missing_modalities == ()


def test_a_level_the_pack_does_not_cover_is_a_warning_not_a_silent_gap(
    polish_workspace: PolishWorkspace,
) -> None:
    plan = resource_service.plan(
        polish_workspace.paths, level_codes=["A1"], clock=polish_workspace.clock
    )

    assert any("does not claim to cover" in warning for warning in plan.warnings)
    assert plan.bundles == ("cefr-a1-core",)


def test_a_level_with_no_bundle_is_refused(polish_workspace: PolishWorkspace) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        resource_service.plan(
            polish_workspace.paths, level_codes=["C2"], clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "no_bundle_for_level"


def test_the_onboarded_workspace_still_passes_every_integrity_check(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)

    report = database_service.check(
        polish_workspace.paths,
        lock=workspace_service.load_lock(polish_workspace.paths),
        clock=polish_workspace.clock,
    )

    assert report.ok is True, [check.message for check in report.failures]


def test_a_status_query_before_any_run_says_so_plainly(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.status(polish_workspace.paths, clock=polish_workspace.clock)

    assert failure.value.payload.code == "onboarding_not_started"
