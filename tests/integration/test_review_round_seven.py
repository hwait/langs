"""Regressions for the seventh review's seven findings.

Each finding gets both halves the reviewer asked for: the command must refuse the
invalid operation, and where the invalid state could still be persisted -- by a restore,
or by a build with a looser rule -- `db check` must name it.

The fixture below is the shape that made most of these visible: two tracks in one
workspace, taught from two different packs. A single-track workspace cannot expose a
scoping defect at all, which is why the original suites did not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.paths import WorkspacePaths
from linguawiki.services import context as context_service
from linguawiki.services import database as database_service
from linguawiki.services import errors as error_service
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service
from tests.conftest import FIXTURE_PACKS, SyntheticWorkspace
from tests.support.clocks import AdvancingClock

POLISH_ITEM = "pl.lex.dworzec"
POLISH_OTHER = "pl.lex.bilet"
TONAL_ITEM = "ztx.char.diamond"


@dataclass(frozen=True, slots=True)
class TwoTracks:
    """One workspace, two packs, two tracks -- the shape a scoping defect needs."""

    paths: WorkspacePaths
    clock: AdvancingClock
    polish: str
    tonal: str


@pytest.fixture
def two_tracks(installed_pilot: SyntheticWorkspace) -> TwoTracks:
    pack_service.install(
        installed_pilot.paths, FIXTURE_PACKS / "tonal", clock=installed_pilot.clock
    )
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Синтетический Учащийся",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        clock=installed_pilot.clock,
    )
    polish = learner_service.create_track(
        installed_pilot.paths,
        target_language="pl",
        framework="cefr",
        pack_key="pl-pilot",
        declared_level="A2",
        preferences=learner_service.TrackPreferences(transcript_retention_consent=True),
        clock=installed_pilot.clock,
    )
    tonal = learner_service.create_track(
        installed_pilot.paths,
        target_language="ztx-Zzzz",
        framework="fixture-bands-v1",
        pack_key="fixture-tonal",
        clock=installed_pilot.clock,
    )
    return TwoTracks(
        paths=installed_pilot.paths,
        clock=installed_pilot.clock,
        polish=polish.track_id,
        tonal=tonal.track_id,
    )


# ---------------------------------------------------------------- finding 1: item scope


@pytest.mark.parametrize("form", ["stable_key", "content_id", "alias"])
def test_a_track_cannot_record_evidence_against_another_pack_s_item(
    form: str, two_tracks: TwoTracks
) -> None:
    """A content ID was the form that looked least like a guess, and so was unchecked."""

    polish_item = knowledge_service.get(
        two_tracks.paths, item=POLISH_ITEM, track=two_tracks.polish, clock=two_tracks.clock
    )
    reference = {
        "stable_key": POLISH_ITEM,
        "content_id": polish_item.content_id,
        "alias": polish_item.aliases[0].alias,
    }[form]

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_tracks.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=reference,
            dimension="reading",
            track=two_tracks.tonal,
            clock=two_tracks.clock,
        )

    assert failure.value.payload.code in (
        "knowledge_item_out_of_scope",
        "knowledge_item_not_found",
    )


def test_the_refusal_says_whose_item_it_is_rather_than_calling_it_missing(
    two_tracks: TwoTracks,
) -> None:
    """ "Not found" would send a caller hunting for a typo in a valid identifier."""

    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.get(
            two_tracks.paths,
            item=POLISH_ITEM,
            track=two_tracks.tonal,
            clock=two_tracks.clock,
        )

    assert failure.value.payload.code == "knowledge_item_out_of_scope"
    assert "pl-pilot" in failure.value.payload.message


def test_a_search_lists_only_the_track_s_own_material(two_tracks: TwoTracks) -> None:
    """A search that lists another pack's items is how a caller finds a forbidden ID."""

    tonal = knowledge_service.search(
        two_tracks.paths, track=two_tracks.tonal, limit=100, clock=two_tracks.clock
    )
    polish = knowledge_service.search(
        two_tracks.paths, track=two_tracks.polish, limit=100, clock=two_tracks.clock
    )

    assert TONAL_ITEM in {hit.stable_key for hit in tonal.hits}
    assert POLISH_ITEM not in {hit.stable_key for hit in tonal.hits}
    assert POLISH_ITEM in {hit.stable_key for hit in polish.hits}
    assert TONAL_ITEM not in {hit.stable_key for hit in polish.hits}
    assert (
        knowledge_service.search(
            two_tracks.paths, query="dworzec", track=two_tracks.tonal, clock=two_tracks.clock
        ).total_matched
        == 0
    )


def test_a_track_still_reaches_everything_it_is_taught_from(two_tracks: TwoTracks) -> None:
    """Scoping must not lock a learner out of their own programme."""

    item = knowledge_service.get(
        two_tracks.paths, item=TONAL_ITEM, track=two_tracks.tonal, clock=two_tracks.clock
    )
    own = knowledge_service.upsert(
        two_tracks.paths,
        stable_key="learner.note",
        kind="concept",
        title="a note",
        body="The learner's own note on this track.",
        track=two_tracks.tonal,
        clock=two_tracks.clock,
    )
    linked = knowledge_service.link(
        two_tracks.paths,
        source=own.content_id,
        relation_type="related",
        target=TONAL_ITEM,
        track=two_tracks.tonal,
        clock=two_tracks.clock,
    )

    assert item.owner == "pack"
    assert linked.created is True


def test_a_merge_cannot_reach_another_track_s_learner_content(two_tracks: TwoTracks) -> None:
    mine = knowledge_service.upsert(
        two_tracks.paths,
        stable_key="learner.note",
        kind="concept",
        title="a note",
        body="Authored on the Polish track.",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.merge(
            two_tracks.paths,
            source=mine.content_id,
            into=TONAL_ITEM,
            track=two_tracks.tonal,
            clock=two_tracks.clock,
        )

    assert failure.value.payload.code == "knowledge_item_out_of_scope"


def test_db_check_names_learner_state_pointing_outside_its_own_pack(
    two_tracks: TwoTracks,
) -> None:
    """The commands refuse it; a restore from a looser build would not have."""

    polish_item = knowledge_service.get(
        two_tracks.paths, item=POLISH_ITEM, track=two_tracks.polish, clock=two_tracks.clock
    )
    assert database_service.check(two_tracks.paths, clock=two_tracks.clock).ok is True

    with (
        open_writer(two_tracks.paths, command="test.corrupt", clock=two_tracks.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "INSERT INTO track_item_state (track_id, content_id, stage, stage_source, "
            "confidence, priority, first_encounter_at, last_encounter_at, next_review_at, "
            "positive_evidence, negative_evidence, updated_at, aggregation_version, "
            "computed_at, explanation_json, gated_stage, evidence_ceiling) "
            "VALUES (?, ?, 'recognized', 'evidence', 0.5, 0, NULL, NULL, NULL, 2, 0, "
            "now(), 'mastery.v1', now(), '[]', 'recognized', 'recognized')",
            [two_tracks.tonal, polish_item.content_id],
        )

    report = database_service.check(two_tracks.paths, clock=two_tracks.clock)

    assert [check.name for check in report.failures] == ["track_item_scope"]
    assert "not taught from" in report.failures[0].message


# ------------------------------------------------------- finding 2: bank task authority


def served_task(two_tracks: TwoTracks) -> tuple[str, object]:
    """Open a run and serve one text task, returning the run and the task report."""

    from linguawiki.services import assessment as assessment_service

    run = assessment_service.start(
        two_tracks.paths,
        track=two_tracks.polish,
        run_type="pilot-calibration",
        clock=two_tracks.clock,
    )
    for _ in range(12):
        served = assessment_service.next_task(
            two_tracks.paths, run=run.run_id, clock=two_tracks.clock
        )
        if not isinstance(served, assessment_service.NextTaskReport):
            break
        if served.modality == "text":
            return (run.run_id, served)
        assessment_service.record(
            two_tracks.paths,
            run=run.run_id,
            content_id=served.content_id,
            score=1.0,
            clock=two_tracks.clock,
        )
    pytest.skip("the pilot bank served no text task")


def test_a_bank_task_s_own_facts_cannot_be_overridden(two_tracks: TwoTracks) -> None:
    """The overclaim this stage exists to prevent, reached by describing the task wrongly."""

    run_id, served = served_task(two_tracks)

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_tracks.paths,
            task_type="meaning-focused-exchange",
            modality="speech",
            score=1.0,
            dimension=served.dimension,  # type: ignore[attr-defined]
            claims=["spontaneous-production"],
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    assert failure.value.payload.code == "assessment_task_facts_conflict"
    assert {detail.field for detail in failure.value.payload.details} >= {
        "task_type",
        "modality",
    }


def test_the_bank_supplies_the_shape_when_the_caller_does_not(two_tracks: TwoTracks) -> None:
    run_id, served = served_task(two_tracks)

    recorded = evidence_service.record(
        two_tracks.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    assert recorded.task_type == served.task_type  # type: ignore[attr-defined]
    assert recorded.modality == served.modality  # type: ignore[attr-defined]
    assert recorded.dimension == served.dimension  # type: ignore[attr-defined]


def test_a_run_cannot_claim_a_task_it_never_served(two_tracks: TwoTracks) -> None:
    from linguawiki.db.connection import open_reader

    run_id, _served = served_task(two_tracks)
    with open_reader(two_tracks.paths, clock=two_tracks.clock) as database:
        unserved = str(
            database.query(
                "SELECT content_id FROM assessment_tasks WHERE content_id NOT IN "
                "(SELECT content_id FROM assessment_run_tasks) ORDER BY content_id LIMIT 1"
            )[0][0]
        )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_tracks.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=unserved,
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    assert failure.value.payload.code == "assessment_task_not_served"


def test_a_bank_task_from_another_pack_is_out_of_scope(two_tracks: TwoTracks) -> None:
    from linguawiki.db.connection import open_reader

    with open_reader(two_tracks.paths, clock=two_tracks.clock) as database:
        polish_task = str(
            database.query(
                "SELECT bank.content_id FROM assessment_tasks bank "
                "JOIN assessment_definitions form ON form.definition_id = bank.definition_id "
                "JOIN language_packs pack ON pack.pack_id = form.pack_id "
                "WHERE pack.pack_key = 'pl-pilot' ORDER BY bank.content_id LIMIT 1"
            )[0][0]
        )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_tracks.paths,
            score=1.0,
            task=polish_task,
            dimension="reading",
            track=two_tracks.tonal,
            clock=two_tracks.clock,
        )

    assert failure.value.payload.code == "assessment_task_out_of_scope"


def test_an_observation_outside_the_bank_still_has_to_say_what_was_demanded(
    two_tracks: TwoTracks,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_tracks.paths,
            score=1.0,
            target=POLISH_ITEM,
            dimension="reading",
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    assert failure.value.payload.code == "task_shape_required"
    assert "--task-type" in failure.value.payload.message


def test_a_task_target_the_bank_does_not_declare_is_refused() -> None:
    """Checked directly: no shipped pack declares task targets, so no workspace can."""

    from linguawiki.services.evidence import ServedTask, _assert_task_facts

    served = ServedTask(
        content_id="cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        run_id=None,
        task_type="short-response",
        modality="text",
        dimension="reading",
        difficulty=1.0,
        content_family="travel",
        targets=("cnt_01ARZ3NDEKTSV4RRFFQ69G5FAW",),
        source="bank",
    )

    _assert_task_facts(
        served,
        task_type=None,
        modality=None,
        dimension=None,
        target_content_id="cnt_01ARZ3NDEKTSV4RRFFQ69G5FAW",
    )
    with pytest.raises(LinguaWikiError) as failure:
        _assert_task_facts(
            served,
            task_type=None,
            modality=None,
            dimension=None,
            target_content_id="cnt_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        )

    assert failure.value.payload.code == "assessment_task_target_mismatch"


# ------------------------------------------------- finding 3: production counter-evidence


def production_error(two_tracks: TwoTracks) -> str:
    report = error_service.record(
        two_tracks.paths,
        category="production-case-ending",
        signature="szukam bilet",
        description="Produces the accusative where the genitive is required.",
        target=POLISH_OTHER,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )
    return report.error_id


def test_receptive_evidence_can_never_resolve_a_production_error(
    two_tracks: TwoTracks,
) -> None:
    """Three delayed reading checks retired a named production error. They must not."""

    error_id = production_error(two_tracks)
    for index in range(3):
        evidence_service.record(
            two_tracks.paths,
            task_type="reading-comprehension",
            modality="text",
            score=1.0,
            target=POLISH_OTHER,
            dimension="reading",
            claims=["delayed-transfer"],
            retrieval="delayed",
            delay_hours=48.0,
            context=f"reading:{index}",
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    after = error_service.show(
        two_tracks.paths, error=error_id, track=two_tracks.polish, clock=two_tracks.clock
    )

    assert after.status != "resolved"
    assert "a spontaneous success" in after.outstanding


def test_production_evidence_still_resolves_a_production_error(
    two_tracks: TwoTracks,
) -> None:
    """The rule must not be so strict that nothing can retire an error."""

    error_id = production_error(two_tracks)
    for index, (retrieval, delay) in enumerate(
        (("immediate", None), ("delayed", 48.0), ("immediate", None))
    ):
        evidence_service.record(
            two_tracks.paths,
            task_type="extended-productive",
            modality="writing",
            score=1.0,
            target=POLISH_OTHER,
            dimension="writing",
            claims=["delayed-transfer"] if retrieval == "delayed" else ["spontaneous-production"],
            retrieval=retrieval,
            delay_hours=delay,
            context=f"essay:{index}",
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    after = error_service.show(
        two_tracks.paths, error=error_id, track=two_tracks.polish, clock=two_tracks.clock
    )

    assert after.status == "resolved"
    assert after.outstanding == ()


# ------------------------------------------------- finding 4: transcription artifacts


def artifact(two_tracks: TwoTracks, **overrides: str) -> error_service.ErrorReport:
    arguments = {
        "category": "mishearing",
        "signature": "peron dwa",
        "description": "Possibly a microphone artifact.",
        "target": "pl.lex.peron",
        "classification": "transcription-artifact",
    }
    arguments.update(overrides)
    return error_service.record(
        two_tracks.paths,
        track=two_tracks.polish,
        clock=two_tracks.clock,
        **arguments,  # type: ignore[arg-type]
    )


def test_transcription_artifacts_never_become_a_live_learner_error(
    two_tracks: TwoTracks,
) -> None:
    """A mishearing taught back as a mistake is the outcome the classification prevents."""

    first = artifact(two_tracks)
    second = artifact(two_tracks)

    assert first.status == "unconfirmed"
    assert second.status == "unconfirmed"
    assert second.occurrence_count == 0
    assert second.outstanding == ()
    assert any("not counted against" in warning for warning in second.warnings)
    live = error_service.listing(
        two_tracks.paths, track=two_tracks.polish, live_only=True, clock=two_tracks.clock
    )
    assert live.live == 0
    assert live.entries == ()


def test_an_unconfirmed_pattern_is_still_visible_when_asked_for(
    two_tracks: TwoTracks,
) -> None:
    """Recorded, not hidden: the artifact is on the record for whoever reviews it."""

    artifact(two_tracks)

    listed = error_service.listing(
        two_tracks.paths, track=two_tracks.polish, clock=two_tracks.clock
    )
    by_status = error_service.listing(
        two_tracks.paths,
        track=two_tracks.polish,
        status="unconfirmed",
        clock=two_tracks.clock,
    )

    shown = error_service.show(
        two_tracks.paths,
        error=by_status.entries[0].error_id,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    assert [entry.status for entry in listed.entries] == ["unconfirmed"]
    assert len(by_status.entries) == 1
    # A listing omits occurrence history by design; `show` is where the artifact is
    # readable, which is the point of recording it rather than discarding it.
    assert [entry.classification for entry in shown.occurrences] == ["transcription-artifact"]


def test_confirming_the_error_later_activates_the_pattern(two_tracks: TwoTracks) -> None:
    artifact(two_tracks)
    confirmed = artifact(two_tracks, classification="learner-error")

    assert confirmed.status == "observed"
    assert confirmed.occurrence_count == 1
    assert confirmed.transitioned is True
    again = artifact(two_tracks, classification="learner-error")
    assert again.status == "active"
    assert again.occurrence_count == 2


def test_an_uncertain_classification_is_also_not_counted(two_tracks: TwoTracks) -> None:
    """Nobody settled whether it was the learner's, so nothing is claimed either way."""

    uncertain = artifact(two_tracks, classification="uncertain")

    assert uncertain.status == "unconfirmed"
    assert uncertain.occurrence_count == 0


def test_an_artifact_cannot_reopen_a_resolved_error(two_tracks: TwoTracks) -> None:
    error_id = production_error(two_tracks)
    for index in range(3):
        evidence_service.record(
            two_tracks.paths,
            task_type="extended-productive",
            modality="writing",
            score=1.0,
            target=POLISH_OTHER,
            dimension="writing",
            claims=["spontaneous-production"] if index != 1 else ["delayed-transfer"],
            retrieval="immediate" if index != 1 else "delayed",
            delay_hours=None if index != 1 else 48.0,
            context=f"essay:{index}",
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )
    assert (
        error_service.show(
            two_tracks.paths, error=error_id, track=two_tracks.polish, clock=two_tracks.clock
        ).status
        == "resolved"
    )

    reopened = error_service.record(
        two_tracks.paths,
        category="production-case-ending",
        signature="szukam bilet",
        description="Produces the accusative where the genitive is required.",
        target=POLISH_OTHER,
        classification="transcription-artifact",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    assert reopened.status == "resolved"
    assert reopened.transitioned is False


def test_db_check_names_a_live_error_with_no_confirmed_occurrence(
    two_tracks: TwoTracks,
) -> None:
    error_id = production_error(two_tracks)
    assert database_service.check(two_tracks.paths, clock=two_tracks.clock).ok is True

    with (
        open_writer(two_tracks.paths, command="test.corrupt", clock=two_tracks.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE error_occurrences SET classification = 'transcription-artifact' "
            "WHERE error_id = ?",
            [error_id],
        )

    report = database_service.check(two_tracks.paths, clock=two_tracks.clock)

    assert "error_confirmation" in {check.name for check in report.failures}


# ------------------------------------------------------- finding 5: repeated contexts


def test_repeating_one_context_does_not_narrow_the_interval(two_tracks: TwoTracks) -> None:
    """An interval is a claim about how much is known. Repetition adds no knowledge."""

    readings = []
    for _ in range(3):
        evidence_service.record(
            two_tracks.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=POLISH_ITEM,
            dimension="listening",
            context="objective:one-and-only",
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )
        entry = next(
            estimate
            for estimate in estimate_service.report(
                two_tracks.paths, track=two_tracks.polish, clock=two_tracks.clock
            ).estimates
            if estimate.dimension == "listening"
        )
        readings.append((entry.evidence_count, entry.uncertainty, entry.estimate_status))

    assert [count for count, _, _ in readings] == [1, 1, 1]
    assert len({uncertainty for _, uncertainty, _ in readings}) == 1
    assert {status for _, _, status in readings} == {"provisional"}


def test_a_second_independent_context_does_narrow_it(two_tracks: TwoTracks) -> None:
    for context in ("objective:one", "objective:one", "reading:two"):
        evidence_service.record(
            two_tracks.paths,
            task_type="objective" if context.startswith("objective") else "reading-comprehension",
            modality="text",
            score=1.0,
            target=POLISH_ITEM,
            dimension="listening",
            context=context,
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    entry = next(
        estimate
        for estimate in estimate_service.report(
            two_tracks.paths, track=two_tracks.polish, clock=two_tracks.clock
        ).estimates
        if estimate.dimension == "listening"
    )

    assert entry.evidence_count == 2
    assert entry.estimate_status == "estimated"


def test_the_repetitions_are_reported_as_corroborating_rather_than_dropped(
    two_tracks: TwoTracks,
) -> None:
    """An estimate that silently ignored half its evidence would not be an estimate."""

    for _ in range(3):
        evidence_service.record(
            two_tracks.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=POLISH_ITEM,
            dimension="listening",
            context="objective:same",
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    recomputed = evidence_service.recompute(
        two_tracks.paths, track=two_tracks.polish, dry_run=True, clock=two_tracks.clock
    )
    assert recomputed.estimates is not None
    listening = [
        change for change in recomputed.estimates.changes if change.dimension == "listening"
    ]
    unchanged = "listening" in recomputed.estimates.unchanged

    assert unchanged or listening
    if listening:
        assert len(listening[0].corroborating_evidence) == 2


def test_the_newest_observation_in_a_context_is_the_one_that_counts(
    two_tracks: TwoTracks,
) -> None:
    """So a later failure in a setting is not outvoted by an earlier success there."""

    from datetime import timedelta

    evidence_service.record(
        two_tracks.paths,
        task_type="objective",
        modality="text",
        score=1.0,
        target=POLISH_ITEM,
        dimension="listening",
        context="objective:same",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )
    two_tracks.clock.advance(timedelta(days=2))
    evidence_service.record(
        two_tracks.paths,
        task_type="objective",
        modality="text",
        score=0.0,
        target=POLISH_ITEM,
        dimension="listening",
        context="objective:same",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    observations = None
    from linguawiki.db.connection import open_reader

    with open_reader(two_tracks.paths, clock=two_tracks.clock) as database:
        observations = estimate_service.dimension_observations(
            database, track_id=two_tracks.polish, dimension="listening"
        )
    entry = next(
        estimate
        for estimate in estimate_service.report(
            two_tracks.paths, track=two_tracks.polish, clock=two_tracks.clock
        ).estimates
        if estimate.dimension == "listening"
    )

    assert len(observations) == 2
    assert entry.evidence_count == 1
    assert entry.score is not None


# ------------------------------------------------------ finding 6: relation identity


def test_a_merge_re_derives_relation_identity_so_the_edge_stays_findable(
    two_tracks: TwoTracks,
) -> None:
    """Otherwise re-adding the edge derives a new identity and creates a duplicate."""

    duplicate = knowledge_service.upsert(
        two_tracks.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate",
        body="Written twice by the learner.",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )
    first = knowledge_service.link(
        two_tracks.paths,
        source=duplicate.content_id,
        relation_type="related",
        target="pl.lex.peron",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )
    knowledge_service.merge(
        two_tracks.paths,
        source=duplicate.content_id,
        into=POLISH_ITEM,
        dry_run=False,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    again = knowledge_service.link(
        two_tracks.paths,
        source=POLISH_ITEM,
        relation_type="related",
        target="pl.lex.peron",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )
    item = knowledge_service.get(
        two_tracks.paths, item=POLISH_ITEM, track=two_tracks.polish, clock=two_tracks.clock
    )
    edges = [
        edge
        for edge in item.relations
        if edge.relation_type == "related" and edge.direction == "outgoing"
    ]

    assert again.created is False, "the moved edge must be found, not duplicated"
    assert len(edges) == 1
    assert edges[0].relation_id != first.relation_id
    assert edges[0].relation_id == again.relation_id
    assert database_service.check(two_tracks.paths, clock=two_tracks.clock).ok is True


def test_a_merge_collapsing_both_ends_of_an_edge_removes_it(two_tracks: TwoTracks) -> None:
    """An item is not related to itself, so the edge simply ceases to exist."""

    duplicate = knowledge_service.upsert(
        two_tracks.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate",
        body="Written twice by the learner.",
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )
    knowledge_service.link(
        two_tracks.paths,
        source=duplicate.content_id,
        relation_type="related",
        target=POLISH_ITEM,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    knowledge_service.merge(
        two_tracks.paths,
        source=duplicate.content_id,
        into=POLISH_ITEM,
        dry_run=False,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    item = knowledge_service.get(
        two_tracks.paths, item=POLISH_ITEM, track=two_tracks.polish, clock=two_tracks.clock
    )
    assert not [edge for edge in item.relations if edge.other_content_id == item.content_id]
    assert database_service.check(two_tracks.paths, clock=two_tracks.clock).ok is True


def test_db_check_names_a_duplicated_edge(two_tracks: TwoTracks) -> None:
    with (
        open_writer(two_tracks.paths, command="test.corrupt", clock=two_tracks.clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "INSERT INTO knowledge_relations (relation_id, source_content_id, relation_type, "
            "target_content_id, target_ref, created_at) "
            "SELECT 'cnt_STALEDUPLICATE00000000A', source_content_id, relation_type, "
            "target_content_id, target_ref, created_at FROM knowledge_relations "
            "WHERE target_content_id IS NOT NULL ORDER BY relation_id LIMIT 1"
        )

    report = database_service.check(two_tracks.paths, clock=two_tracks.clock)

    assert "knowledge_relation_uniqueness" in {check.name for check in report.failures}


# --------------------------------------------------------- finding 7: record bounding


@pytest.mark.parametrize("scope", context_service.SCOPES)
@pytest.mark.parametrize("limit", [1, 2, 5, 20])
def test_the_record_limit_bounds_the_whole_bundle(
    scope: str, limit: int, two_tracks: TwoTracks
) -> None:
    """Per-section allowances were independent, so a bundle asked for one returned five."""

    for index in range(4):
        error_service.record(
            two_tracks.paths,
            category=f"category-{index}",
            signature=f"signature number {index}",
            description=f"A synthetic error, number {index}.",
            target=POLISH_OTHER,
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )
        evidence_service.record(
            two_tracks.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=POLISH_ITEM,
            dimension="reading",
            context=f"objective:{index}",
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    unbounded = context_service.build(
        two_tracks.paths,
        scope=scope,
        item=POLISH_ITEM if scope == "concept" else None,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )
    bundle = context_service.build(
        two_tracks.paths,
        scope=scope,
        item=POLISH_ITEM if scope == "concept" else None,
        max_records=limit,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    assert bundle.records <= bundle.record_limit == limit
    assert bundle.provenance is not None, "provenance keeps the rest traceable"
    if unbounded.records > limit:
        assert bundle.bounded is True
        assert bundle.omissions, "a bundle that dropped something must say so"


def test_a_record_limited_bundle_reports_what_each_dropped_section_held(
    two_tracks: TwoTracks,
) -> None:
    for index in range(4):
        error_service.record(
            two_tracks.paths,
            category=f"category-{index}",
            signature=f"signature number {index}",
            description=f"A synthetic error, number {index}.",
            target=POLISH_OTHER,
            track=two_tracks.polish,
            clock=two_tracks.clock,
        )

    bundle = context_service.build(
        two_tracks.paths,
        scope="session",
        max_records=2,
        track=two_tracks.polish,
        clock=two_tracks.clock,
    )

    sections = [omission.section for omission in bundle.omissions]
    errors = next(omission for omission in bundle.omissions if omission.section == "active_errors")

    assert len(sections) == len(set(sections)), "one authoritative omission per section"
    # Capped or dropped whole, `available` is the real total and `included` is what the
    # bundle actually holds -- the pair a caller has to be able to reason from.
    assert errors.available == 4
    assert errors.included == len(bundle.active_errors)
    assert bundle.records <= bundle.record_limit


# ================================================ round eight: assessment provenance


@dataclass(frozen=True, slots=True)
class TwoLearners:
    """Two learners on one pack -- the shape in which a run can reach the wrong model."""

    paths: WorkspacePaths
    clock: AdvancingClock
    first: str
    second: str


@pytest.fixture
def two_learners(installed_pilot: SyntheticWorkspace) -> TwoLearners:
    tracks = []
    for name in ("Учащийся A", "Учащийся B"):
        user = learner_service.create_user(
            installed_pilot.paths,
            display_name=name,
            timezone="Europe/Warsaw",
            native_languages=["ru"],
            clock=installed_pilot.clock,
        )
        tracks.append(
            learner_service.create_track(
                installed_pilot.paths,
                user=user.user_id,
                target_language="pl",
                framework="cefr",
                declared_level="A2",
                clock=installed_pilot.clock,
            ).track_id
        )
    return TwoLearners(
        paths=installed_pilot.paths,
        clock=installed_pilot.clock,
        first=tracks[0],
        second=tracks[1],
    )


def open_run_with_a_text_task(
    paths: WorkspacePaths, *, track: str, clock: AdvancingClock
) -> tuple[str, object]:
    from linguawiki.services import assessment as assessment_service

    run = assessment_service.start(paths, track=track, run_type="pilot-calibration", clock=clock)
    for _ in range(12):
        served = assessment_service.next_task(paths, run=run.run_id, clock=clock)
        if not isinstance(served, assessment_service.NextTaskReport):
            break
        if served.modality == "text":
            return (run.run_id, served)
        assessment_service.record(
            paths, run=run.run_id, content_id=served.content_id, score=1.0, clock=clock
        )
    pytest.skip("the pilot bank served no text task")


def mutate_the_bank(paths: WorkspacePaths, *, task: str, clock: AdvancingClock) -> None:
    """Change the bank item behind a served task, as a pack update would."""

    with (
        open_writer(paths, command="test.pack-drift", clock=clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_tasks SET task_type = 'extended-productive', "
            "modality = 'writing', dimension = 'writing', difficulty = 3.5 "
            "WHERE content_id = ?",
            [task],
        )


def test_evidence_records_what_the_run_served_not_what_the_pack_now_says(
    two_learners: TwoLearners,
) -> None:
    """A pack is mutable and a run is not, which is why the run snapshots what it served.

    Reading the bank let a later edit rewrite history: the learner answered a text
    short-response, and the default claim rose from recognition to controlled production
    for an observation nobody made.
    """

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    mutate_the_bank(
        two_learners.paths,
        task=served.content_id,  # type: ignore[attr-defined]
        clock=two_learners.clock,
    )

    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )

    assert recorded.task_type == served.task_type  # type: ignore[attr-defined]
    assert recorded.modality == served.modality  # type: ignore[attr-defined]
    assert recorded.dimension == served.dimension  # type: ignore[attr-defined]
    assert [entry.claim for entry in recorded.evidence] == ["recognition"]
    assert any("has changed since the run served it" in warning for warning in recorded.warnings)


def test_a_drifted_bank_still_refuses_a_claim_the_served_task_cannot_support(
    two_learners: TwoLearners,
) -> None:
    """The mutated bank would have permitted it; what the learner faced does not."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    mutate_the_bank(
        two_learners.paths,
        task=served.content_id,  # type: ignore[attr-defined]
        clock=two_learners.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            claims=["spontaneous-production"],
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            track=two_learners.first,
            clock=two_learners.clock,
        )

    assert failure.value.payload.code == "evidence_modality_incompatible"


def test_the_served_difficulty_is_the_one_the_estimate_folds_in(
    two_learners: TwoLearners,
) -> None:
    """A difficulty read from a drifted bank would place the learner at another level."""

    from linguawiki.db.connection import open_reader

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    mutate_the_bank(
        two_learners.paths,
        task=served.content_id,  # type: ignore[attr-defined]
        clock=two_learners.clock,
    )

    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )

    with open_reader(two_learners.paths, clock=two_learners.clock) as database:
        difficulty = float(
            database.scalar(
                "SELECT source_difficulty FROM attempts WHERE attempt_id = ?",
                [recorded.attempt_id],
            )
        )

    assert difficulty == pytest.approx(served.difficulty)  # type: ignore[attr-defined]
    assert difficulty != pytest.approx(3.5)


def test_another_learner_s_run_cannot_be_attached_to_this_learner_s_evidence(
    two_learners: TwoLearners,
) -> None:
    """Checking only that the run served the task let one model contaminate another."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            track=two_learners.second,
            clock=two_learners.clock,
        )

    assert failure.value.payload.code == "assessment_run_not_on_track"
    assert failure.value.payload.details[0].context["run_track"] == two_learners.first


def test_an_unknown_run_is_refused_by_name(two_learners: TwoLearners) -> None:
    _run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run="asm_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            task=served.content_id,  # type: ignore[attr-defined]
            track=two_learners.first,
            clock=two_learners.clock,
        )

    assert failure.value.payload.code == "assessment_run_not_found"


def test_the_run_s_own_track_is_still_allowed(two_learners: TwoLearners) -> None:
    """Scoping must not stop a run recording evidence for the learner who did it."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )

    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )

    assert recorded.track_id == two_learners.first
    assert database_service.check(two_learners.paths, clock=two_learners.clock).ok is True


def test_db_check_names_an_attempt_attributed_to_another_track_s_run(
    two_learners: TwoLearners,
) -> None:
    """The command refuses it; a restore from a looser build would not have."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    own = evidence_service.record(
        two_learners.paths,
        task_type="objective",
        modality="text",
        score=1.0,
        target=POLISH_ITEM,
        dimension="reading",
        track=two_learners.second,
        clock=two_learners.clock,
    )
    assert database_service.check(two_learners.paths, clock=two_learners.clock).ok is True

    with (
        open_writer(two_learners.paths, command="test.corrupt", clock=two_learners.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "INSERT INTO attempts SELECT * REPLACE ("
            "'att_CORRUPTRUNTRACK0000000AA' AS attempt_id, ? AS assessment_run_id, "
            "? AS task_content_id) FROM attempts WHERE attempt_id = ?",
            [run_id, served.content_id, own.attempt_id],  # type: ignore[attr-defined]
        )

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert "attempt_run_track" in {check.name for check in report.failures}


def test_db_check_names_an_attempt_carrying_facts_the_run_never_served(
    two_learners: TwoLearners,
) -> None:
    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )
    assert database_service.check(two_learners.paths, clock=two_learners.clock).ok is True

    with (
        open_writer(two_learners.paths, command="test.corrupt", clock=two_learners.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_run_tasks SET task_type = 'connected-speech' "
            "WHERE run_id = ? AND content_id = ?",
            [run_id, served.content_id],  # type: ignore[attr-defined]
        )

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert "attempt_served_facts" in {check.name for check in report.failures}
    assert "later pack edit" in next(
        check.message for check in report.failures if check.name == "attempt_served_facts"
    )


def test_a_bank_task_without_a_run_still_uses_the_pack_s_current_record(
    two_learners: TwoLearners,
) -> None:
    """There is no snapshot to prefer, and the facts still cannot be overridden."""

    from linguawiki.db.connection import open_reader

    with open_reader(two_learners.paths, clock=two_learners.clock) as database:
        task = str(
            database.query(
                "SELECT content_id FROM assessment_tasks WHERE modality = 'text' "
                "ORDER BY content_id LIMIT 1"
            )[0][0]
        )

    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="import",
        task=task,
        track=two_learners.first,
        clock=two_learners.clock,
    )

    assert recorded.modality == "text"
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            task_type="connected-speech",
            modality="speech",
            score=1.0,
            origin="import",
            task=task,
            context="second:attempt",
            track=two_learners.first,
            clock=two_learners.clock,
        )
    assert failure.value.payload.code == "assessment_task_facts_conflict"


# ============================================ round nine: the snapshot's own boundaries


def test_a_task_naming_no_targets_cannot_carry_an_item_observation(
    two_learners: TwoLearners,
) -> None:
    """Absence of information is not permission.

    A task that declares no targets makes no claim about which item it tests, so an
    item-targeted observation attributed to it is a claim nobody made. Treating an empty
    target list as a wildcard let a shipped task vouch for any item in the pack.
    """

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            target=POLISH_ITEM,
            track=two_learners.first,
            clock=two_learners.clock,
        )

    assert failure.value.payload.code == "assessment_task_target_unknown"
    assert "--origin import" in failure.value.payload.message
    # The dimension observation the task *did* make is still recordable.
    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )
    assert recorded.target_content_id is None
    assert recorded.dimension == served.dimension  # type: ignore[attr-defined]


def snapshot_targets(
    two_learners: TwoLearners, *, run_id: str, task: str, targets: list[str] | None
) -> None:
    """Set a served record's target snapshot, as a pack declaring `target_keys` would."""

    with (
        open_writer(two_learners.paths, command="test.targets", clock=two_learners.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_run_tasks SET target_refs_json = ? "
            "WHERE run_id = ? AND content_id = ?",
            [None if targets is None else json.dumps(targets), run_id, task],
        )


def test_a_declared_target_is_accepted_and_another_is_refused(
    two_learners: TwoLearners,
) -> None:
    """The snapshot is what decides, so a later pack edit cannot widen or narrow it."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    item = knowledge_service.get(
        two_learners.paths, item=POLISH_ITEM, track=two_learners.first, clock=two_learners.clock
    )
    snapshot_targets(
        two_learners,
        run_id=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        targets=[item.content_id],
    )

    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        target=POLISH_ITEM,
        track=two_learners.first,
        clock=two_learners.clock,
    )

    assert recorded.target_content_id == item.content_id
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            target=POLISH_OTHER,
            context="other:target",
            track=two_learners.first,
            clock=two_learners.clock,
        )
    assert failure.value.payload.code == "assessment_task_target_mismatch"


def test_a_task_served_before_targets_were_snapshotted_refuses_an_item_observation(
    two_learners: TwoLearners,
) -> None:
    """Unknown is not the same as none, and neither is permission."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    snapshot_targets(
        two_learners,
        run_id=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        targets=None,
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            target=POLISH_ITEM,
            track=two_learners.first,
            clock=two_learners.clock,
        )

    assert failure.value.payload.code == "assessment_task_target_unknown"
    assert "not knowable" in failure.value.payload.message


def test_a_caller_s_difficulty_cannot_override_the_served_one(
    two_learners: TwoLearners,
) -> None:
    """Difficulty is where the observation lands on the ability grid.

    A caller's number moves the learner's estimate to a level the task never tested, so
    it is refused at the write rather than reported afterwards.
    """

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            difficulty=served.difficulty + 2.0,  # type: ignore[attr-defined]
            track=two_learners.first,
            clock=two_learners.clock,
        )

    assert failure.value.payload.code == "assessment_task_facts_conflict"
    assert "difficulty" in {detail.field for detail in failure.value.payload.details}


def test_the_served_difficulty_is_what_the_attempt_records(
    two_learners: TwoLearners,
) -> None:
    from linguawiki.db.connection import open_reader

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        # The same number, passed explicitly, must be accepted rather than refused.
        difficulty=served.difficulty,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )

    with open_reader(two_learners.paths, clock=two_learners.clock) as database:
        stored = float(
            database.scalar(
                "SELECT source_difficulty FROM attempts WHERE attempt_id = ?",
                [recorded.attempt_id],
            )
        )

    assert stored == pytest.approx(served.difficulty)  # type: ignore[attr-defined]


def null_snapshot_facts(
    two_learners: TwoLearners, *, run_id: str, task: str, columns: tuple[str, ...]
) -> None:
    assignments = ", ".join(f"{column} = NULL" for column in columns)
    with (
        open_writer(two_learners.paths, command="test.snapshot", clock=two_learners.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            f"UPDATE assessment_run_tasks SET {assignments} WHERE run_id = ? AND content_id = ?",
            [run_id, task],
        )


def test_a_partially_null_served_record_is_damage_not_history(
    two_learners: TwoLearners,
) -> None:
    """Migration 0016 added every one of these at once, so a mix cannot be a legacy row.

    Treating one as legacy sent the resolver to the bank for facts the record actually
    held -- reinstating the very defect the snapshot exists to prevent, on a record that
    was almost intact.
    """

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    null_snapshot_facts(
        two_learners,
        run_id=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        columns=("content_family",),
    )
    mutate_the_bank(
        two_learners.paths,
        task=served.content_id,  # type: ignore[attr-defined]
        clock=two_learners.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            track=two_learners.first,
            clock=two_learners.clock,
        )

    assert failure.value.payload.code == "assessment_snapshot_incomplete"
    assert "content_family" in failure.value.payload.details[0].context["missing"]
    assert "without --assessment-run" in failure.value.payload.message


def test_a_wholly_absent_served_record_is_a_genuine_legacy_row(
    two_learners: TwoLearners,
) -> None:
    """All of them null is what a pre-0016 row looks like, and the bank is all there is."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    null_snapshot_facts(
        two_learners,
        run_id=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        columns=("task_type", "modality", "difficulty", "content_family", "content_hash"),
    )

    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )

    assert recorded.task_type == served.task_type  # type: ignore[attr-defined]
    assert any("did not record what it served" in warning for warning in recorded.warnings)
    # And the bank cannot vouch for a target either: which item it tested is unknowable.
    with pytest.raises(LinguaWikiError) as failure:
        evidence_service.record(
            two_learners.paths,
            score=1.0,
            origin="assessment",
            assessment_run=run_id,
            task=served.content_id,  # type: ignore[attr-defined]
            target=POLISH_ITEM,
            context="legacy:target",
            track=two_learners.first,
            clock=two_learners.clock,
        )
    assert failure.value.payload.code == "assessment_task_target_unknown"


def test_db_check_names_an_attempt_the_run_has_no_record_of_serving(
    two_learners: TwoLearners,
) -> None:
    """An inner join answers only "do the facts match", never "was it served at all"."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )
    assert database_service.check(two_learners.paths, clock=two_learners.clock).ok is True

    with (
        open_writer(two_learners.paths, command="test.corrupt", clock=two_learners.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "DELETE FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
            [run_id, served.content_id],  # type: ignore[attr-defined]
        )

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert "attempt_run_membership" in {check.name for check in report.failures}


def test_db_check_names_an_attempt_that_lost_its_difficulty(
    two_learners: TwoLearners,
) -> None:
    """A null on the attempt side is a mismatch, not a fact to skip comparing."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        track=two_learners.first,
        clock=two_learners.clock,
    )

    with (
        open_writer(two_learners.paths, command="test.corrupt", clock=two_learners.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE attempts SET source_difficulty = NULL WHERE attempt_id = ?",
            [recorded.attempt_id],
        )

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert "attempt_served_facts" in {check.name for check in report.failures}


def test_db_check_names_a_partial_served_record(two_learners: TwoLearners) -> None:
    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    null_snapshot_facts(
        two_learners,
        run_id=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        columns=("content_family",),
    )

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert "served_snapshot_complete" in {check.name for check in report.failures}


def test_a_task_served_now_carries_its_targets_in_the_run_s_record(
    two_learners: TwoLearners,
) -> None:
    """Migration 0022: the last fact that still had to be read back from the bank."""

    from linguawiki.db.connection import open_reader

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )

    with open_reader(two_learners.paths, clock=two_learners.clock) as database:
        snapshotted = database.scalar(
            "SELECT target_refs_json FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
            [run_id, served.content_id],  # type: ignore[attr-defined]
        )
        declared = database.scalar(
            "SELECT target_refs_json FROM assessment_tasks WHERE content_id = ?",
            [served.content_id],  # type: ignore[attr-defined]
        )

    assert snapshotted is not None, "a task served by this release records its targets"
    assert json.loads(str(snapshotted)) == json.loads(str(declared))


# ================================ round ten: the check's own treatment of absent data


def set_served_targets(
    two_learners: TwoLearners, *, run_id: str, task: str | None, value: object
) -> None:
    """Write a raw target snapshot, including values no command would ever produce."""

    scope = "" if task is None else "AND content_id = ?"
    parameters = [value, run_id] if task is None else [value, run_id, task]
    with (
        open_writer(two_learners.paths, command="test.raw-targets", clock=two_learners.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            f"UPDATE assessment_run_tasks SET target_refs_json = ? WHERE run_id = ? {scope}",
            parameters,
        )


def targeted_run_attempt(two_learners: TwoLearners) -> tuple[str, str, str]:
    """A legitimate run-backed attempt about an item the served task targeted."""

    run_id, served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    item = knowledge_service.get(
        two_learners.paths, item=POLISH_ITEM, track=two_learners.first, clock=two_learners.clock
    )
    set_served_targets(
        two_learners,
        run_id=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        value=json.dumps([item.content_id]),
    )
    recorded = evidence_service.record(
        two_learners.paths,
        score=1.0,
        origin="assessment",
        assessment_run=run_id,
        task=served.content_id,  # type: ignore[attr-defined]
        target=POLISH_ITEM,
        track=two_learners.first,
        clock=two_learners.clock,
    )
    assert database_service.check(two_learners.paths, clock=two_learners.clock).ok is True
    return (run_id, str(served.content_id), recorded.attempt_id)  # type: ignore[attr-defined]


def test_db_check_names_a_targeted_attempt_whose_snapshot_says_nothing(
    two_learners: TwoLearners,
) -> None:
    """The write path refuses this state, so only a restore can produce it -- and did.

    The comparison was guarded by `IS NOT NULL`, so the one case the write path cares
    about most was the one case the check skipped.
    """

    run_id, task, _attempt = targeted_run_attempt(two_learners)
    set_served_targets(two_learners, run_id=run_id, task=task, value=None)

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert "attempt_served_targets" in {check.name for check in report.failures}
    assert "does not say what was targeted" in next(
        check.context["attempts"]
        for check in report.failures
        if check.name == "attempt_served_targets"
    )


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("not json", "not json at all"),
        ("an object rather than a list", '{"target": "cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV"}'),
        ("a list of numbers", "[1, 2]"),
        ("a list of the wrong identifiers", '["trk_01ARZ3NDEKTSV4RRFFQ69G5FAV"]'),
        ("a truncated identifier", '["cnt_01ARZ"]'),
    ],
)
def test_db_check_reports_a_malformed_target_snapshot_rather_than_aborting(
    label: str, value: str, two_learners: TwoLearners
) -> None:
    """DuckDB cannot constrain an added column, so nothing guarantees this is even JSON.

    Parsing it in SQL made `db check` abort on the first malformed value, and a
    diagnostic that crashes reports less than one that lies.
    """

    run_id, task, _attempt = targeted_run_attempt(two_learners)
    set_served_targets(two_learners, run_id=run_id, task=task, value=value)

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert report.ok is False, label
    assert "served_targets_wellformed" in {check.name for check in report.failures}
    assert "attempt_served_targets" in {check.name for check in report.failures}


def test_a_malformed_snapshot_is_reported_even_when_no_attempt_uses_it(
    two_learners: TwoLearners,
) -> None:
    """Corruption nobody has read yet is still corruption."""

    run_id, _served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    assert database_service.check(two_learners.paths, clock=two_learners.clock).ok is True

    set_served_targets(two_learners, run_id=run_id, task=None, value="not json at all")

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert [check.name for check in report.failures] == ["served_targets_wellformed"]


def test_a_well_formed_empty_target_list_is_not_a_failure(
    two_learners: TwoLearners,
) -> None:
    """A task that targets nothing is a normal task; only an *attempt* about an item isn't."""

    run_id, _served = open_run_with_a_text_task(
        two_learners.paths, track=two_learners.first, clock=two_learners.clock
    )
    set_served_targets(two_learners, run_id=run_id, task=None, value="[]")

    assert database_service.check(two_learners.paths, clock=two_learners.clock).ok is True


def test_db_check_names_a_targeted_attempt_outside_a_well_formed_snapshot(
    two_learners: TwoLearners,
) -> None:
    run_id, task, _attempt = targeted_run_attempt(two_learners)
    other = knowledge_service.get(
        two_learners.paths, item=POLISH_OTHER, track=two_learners.first, clock=two_learners.clock
    )
    set_served_targets(two_learners, run_id=run_id, task=task, value=json.dumps([other.content_id]))

    report = database_service.check(two_learners.paths, clock=two_learners.clock)

    assert "attempt_served_targets" in {check.name for check in report.failures}
    assert "not among the served targets" in next(
        check.context["attempts"]
        for check in report.failures
        if check.name == "attempt_served_targets"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (123, None),
        ("", None),
        ("null", None),
        ("[]", ()),
        ('["cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV"]', ("cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV",)),
        ('["cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV", null]', None),
        ('{"a": 1}', None),
        ("[[]]", None),
    ],
)
def test_the_target_parser_never_raises_and_names_only_content_ids(
    raw: object, expected: tuple[str, ...] | None
) -> None:
    """Directly, because the parser is the thing standing between corruption and a crash."""

    from linguawiki.db.integrity import served_target_list

    assert served_target_list(raw) == expected
