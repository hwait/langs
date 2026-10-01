"""Regressions for the Stage 2 review findings that need a real workspace.

Each of these was reachable through ordinary supported commands before the fix, which is
why each test drives the commands rather than the internals.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.db.integrity import check_database
from linguawiki.errors import LinguaWikiError
from linguawiki.packs.format import directory_digests, pack_content_address
from linguawiki.packs.stamp import stamp_pack
from linguawiki.provenance import FULL_INSPECTION_RUNS, SamplingPolicy
from linguawiki.services import assessment as assessment_service
from linguawiki.services import authoring as authoring_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import packs as pack_service
from tests.conftest import (
    NEXT_PILOT_VERSION,
    PILOT_PACK,
    PolishWorkspace,
    SyntheticWorkspace,
)

TEMPLATE: dict[str, Any] = {
    "schema_name": "lingua.pack.template.v1",
    "schema_version": 1,
    "template_key": "pl-office-collocations",
    "version": 1,
    "purpose": "generation",
    "intended_kinds": ["construction"],
    "body": "Draft office collocations for Polish A2 from the pack's declared themes.",
    "known_failure_modes": ["invents register"],
}


def _drafts(run: int, count: int) -> list[dict[str, Any]]:
    return [
        {
            "stable_key": f"pl.draft.run{run}.item{index}",
            "kind": "construction",
            "title": f"draft {run}.{index}",
            "body": f"Draft body {run}.{index} for review.",
            "level": "A2",
            "themes": ["praca i biuro"],
            "risk_tier": 1,
        }
        for index in range(count)
    ]


def _fully_inspect(workspace: SyntheticWorkspace, *, run: int, count: int = 2) -> Any:
    """One complete drafting run: open a batch, import into it, inspect every item."""

    batch = authoring_service.generate_draft(
        workspace.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        item_count=count,
        clock=workspace.clock,
    )
    imported = authoring_service.import_items(
        workspace.paths,
        items=_drafts(run, count),
        batch_id=batch.batch_id,
        clock=workspace.clock,
    )
    for content_id in imported.items:
        authoring_service.review(
            workspace.paths,
            content_id=content_id,
            axis="linguistic",
            state="machine-checked",
            reviewer_kind="machine",
            reviewer="checker",
            method="inspection",
            inspection="accepted",
            clock=workspace.clock,
        )
    return authoring_service.batch_report(
        workspace.paths, batch_id=batch.batch_id, clock=workspace.clock
    )


# --- Finding 2: the generation-batch lifecycle ---------------------------------------


def test_a_batch_may_not_take_more_items_than_it_was_opened_for(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """Otherwise the inspection duty is renegotiated once the output looks convincing."""

    authoring_service.validate_template(
        installed_pilot.paths, payload=TEMPLATE, clock=installed_pilot.clock
    )
    batch = authoring_service.generate_draft(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        item_count=2,
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.import_items(
            installed_pilot.paths,
            items=_drafts(1, 5),
            batch_id=batch.batch_id,
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "batch_over_capacity"
    assert batch.planned_items == 2


def test_an_import_seals_its_batch(installed_pilot: SyntheticWorkspace) -> None:
    """A second import would enlarge the duty after the fact, so it is refused."""

    authoring_service.validate_template(
        installed_pilot.paths, payload=TEMPLATE, clock=installed_pilot.clock
    )
    batch = authoring_service.generate_draft(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        item_count=4,
        clock=installed_pilot.clock,
    )
    authoring_service.import_items(
        installed_pilot.paths,
        items=_drafts(1, 2),
        batch_id=batch.batch_id,
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        authoring_service.import_items(
            installed_pilot.paths,
            items=_drafts(2, 2),
            batch_id=batch.batch_id,
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "batch_sealed"


def test_a_fully_inspected_defect_free_batch_is_accepted_and_counted(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """Nothing ever left `sampling`, so `inspected_runs` stayed at zero for ever."""

    authoring_service.validate_template(
        installed_pilot.paths, payload=TEMPLATE, clock=installed_pilot.clock
    )

    report = _fully_inspect(installed_pilot, run=1)

    assert report.status == "accepted"
    assert report.inspected_count == report.required_sample
    template = authoring_service.validate_template(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        clock=installed_pilot.clock,
    )
    assert template.inspected_runs == 1


def test_a_template_stabilizes_through_supported_commands_alone(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """The reachability the review asked for: no direct SQL anywhere in this path."""

    authoring_service.validate_template(
        installed_pilot.paths, payload=TEMPLATE, clock=installed_pilot.clock
    )
    for run in range(1, FULL_INSPECTION_RUNS + 1):
        assert _fully_inspect(installed_pilot, run=run).status == "accepted"

    stabilized = authoring_service.stabilize_template(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        clock=installed_pilot.clock,
    )

    assert stabilized.maturity == "stable"
    assert stabilized.sampling_policy == str(SamplingPolicy.SAMPLED)
    # And a stable template now samples a fifth of the batch rather than all of it.
    sampled = authoring_service.generate_draft(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        item_count=40,
        clock=installed_pilot.clock,
    )
    assert sampled.planned_items == 40
    assert sampled.required_sample == 8


def test_one_defective_item_keeps_a_batch_out_of_accepted(
    installed_pilot: SyntheticWorkspace,
) -> None:
    authoring_service.validate_template(
        installed_pilot.paths, payload=TEMPLATE, clock=installed_pilot.clock
    )
    batch = authoring_service.generate_draft(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        item_count=2,
        clock=installed_pilot.clock,
    )
    imported = authoring_service.import_items(
        installed_pilot.paths,
        items=_drafts(1, 2),
        batch_id=batch.batch_id,
        clock=installed_pilot.clock,
    )
    for index, content_id in enumerate(imported.items):
        authoring_service.review(
            installed_pilot.paths,
            content_id=content_id,
            axis="linguistic",
            state="machine-checked",
            reviewer_kind="machine",
            reviewer="checker",
            method="inspection",
            # Accept first: a defective inspection quarantines the batch, and a
            # quarantined item takes no further review.
            inspection="defective" if index else "accepted",
            finding="invented register" if index else None,
            clock=installed_pilot.clock,
        )

    report = authoring_service.batch_report(
        installed_pilot.paths, batch_id=batch.batch_id, clock=installed_pilot.clock
    )

    assert report.status != "accepted"
    template = authoring_service.validate_template(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        clock=installed_pilot.clock,
    )
    assert template.inspected_runs == 0


# --- Finding 4: the track names the pack it was created from --------------------------


def _renamed_pack(source: Path, target: Path, *, pack_key: str, name: str) -> Path:
    """A second pack for the same language, published under its own key."""

    shutil.copytree(source, target)
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["pack_key"] = pack_key
    manifest["name"] = name
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    stamp_pack(target)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = directory_digests(target)
    manifest["content_address"] = None
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    from linguawiki.contracts import PackManifest

    parsed = PackManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def test_a_track_keeps_the_pack_it_was_created_from(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """Re-deriving the binding from the language handed the track to any later pack."""

    before = learner_service.show_track(polish_workspace.paths, clock=polish_workspace.clock)
    assert before.pack_key == "pl-pilot"

    second = _renamed_pack(
        PILOT_PACK, tmp_path / "pl-second", pack_key="pl-second", name="Polish Second Pack"
    )
    pack_service.install(polish_workspace.paths, second, clock=polish_workspace.clock)

    after = learner_service.show_track(polish_workspace.paths, clock=polish_workspace.clock)

    assert after.pack_key == "pl-pilot"
    assert after.pack_version == before.pack_version
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        installed = database.query(
            "SELECT pack_key FROM language_packs WHERE language_tag = 'pl' ORDER BY pack_key"
        )
    assert [str(row[0]) for row in installed] == ["pl-pilot", "pl-second"]


# --- Finding 5: serving exposes an item ----------------------------------------------


def test_serving_a_task_records_the_exposure_even_if_it_is_never_answered(
    polish_workspace: PolishWorkspace,
) -> None:
    """An abandoned run used to hand the same task back, so the retake measured recall."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        row = database.one(
            "SELECT exposure_count, answered_count FROM assessment_item_exposures "
            "WHERE track_id = ? AND content_id = ?",
            [polish_workspace.track_id, served.content_id],
        )

    assert row is not None
    assert (int(row[0]), int(row[1])) == (1, 0)


def test_answering_counts_the_answer_without_counting_a_second_exposure(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        row = database.one(
            "SELECT exposure_count, answered_count FROM assessment_item_exposures "
            "WHERE track_id = ? AND content_id = ?",
            [polish_workspace.track_id, served.content_id],
        )

    assert row is not None
    assert (int(row[0]), int(row[1])) == (1, 1)


def test_a_task_served_in_an_abandoned_run_is_not_served_again(
    polish_workspace: PolishWorkspace,
) -> None:
    first = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=first.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.set_status(
        polish_workspace.paths, run=first.run_id, status="abandoned", clock=polish_workspace.clock
    )

    second = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    outcome = assessment_service.next_task(
        polish_workspace.paths, run=second.run_id, clock=polish_workspace.clock
    )

    assert isinstance(outcome, assessment_service.NextTaskReport)
    assert outcome.content_id != served.content_id


# --- Finding 7: closed runs stay closed ----------------------------------------------


def test_an_abandoned_assessment_run_cannot_be_resumed(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="abandoned", clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as resumed:
        assessment_service.set_status(
            polish_workspace.paths,
            run=run.run_id,
            status="in-progress",
            clock=polish_workspace.clock,
        )

    assert resumed.value.payload.code == "assessment_run_closed"


def test_an_abandoned_assessment_run_cannot_be_finalized(
    polish_workspace: PolishWorkspace,
) -> None:
    """An estimate built from evidence the learner walked away from is not an estimate."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="abandoned", clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.finalize(
            polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "assessment_run_closed"


def test_pausing_and_resuming_an_open_run_still_works(
    polish_workspace: PolishWorkspace,
) -> None:
    """The transition table forbids resurrection, not the sittings it was built for."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    paused = assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="paused", clock=polish_workspace.clock
    )
    resumed = assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="in-progress", clock=polish_workspace.clock
    )

    assert paused.status == "paused"
    assert resumed.status == "in-progress"


def _abandoned_onboarding(workspace: PolishWorkspace) -> str:
    """An abandoned run, addressed explicitly: resolution by track skips abandoned runs."""

    started = onboarding_service.start(workspace.paths, declared_level="A2", clock=workspace.clock)
    onboarding_service.abandon(
        workspace.paths, onboarding=started.onboarding_id, clock=workspace.clock
    )
    return started.onboarding_id


def test_an_abandoned_onboarding_run_cannot_be_finalized(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_id = _abandoned_onboarding(polish_workspace)

    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.finalize(
            polish_workspace.paths, onboarding=onboarding_id, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "onboarding_closed"


def test_an_abandoned_onboarding_run_takes_no_further_answers(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_id = _abandoned_onboarding(polish_workspace)

    with pytest.raises(LinguaWikiError) as failure:
        onboarding_service.record_answer(
            polish_workspace.paths,
            onboarding=onboarding_id,
            key="weekly_availability",
            value="two evenings a week",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "onboarding_closed"


def test_abandoning_an_abandoned_onboarding_run_is_a_no_op(
    polish_workspace: PolishWorkspace,
) -> None:
    onboarding_id = _abandoned_onboarding(polish_workspace)

    repeated = onboarding_service.abandon(
        polish_workspace.paths, onboarding=onboarding_id, clock=polish_workspace.clock
    )

    assert repeated.status == "abandoned"


# --- Second review round -------------------------------------------------------------


def _republished(source: Path, target: Path, **manifest_changes: Any) -> Path:
    """A re-published copy of a pack with the given manifest fields changed."""

    from linguawiki.contracts import PackManifest

    if not target.exists():
        shutil.copytree(source, target)
    path = target / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(manifest_changes)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    stamp_pack(target)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["files"] = directory_digests(target)
    document["content_address"] = None
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    parsed = PackManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    document["content_address"] = pack_content_address(parsed, dict(parsed.files))
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


# Finding 2: a score belongs to the task the learner saw.


def test_a_score_uses_the_difficulty_of_the_task_that_was_served(
    polish_workspace: PolishWorkspace,
) -> None:
    """Selection and scoring both re-read the mutable pack, so a mid-run edit rewrote it.

    The learner's posterior then described a difficulty they never faced.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    with (
        open_writer(
            polish_workspace.paths, command="test.retune", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_tasks SET difficulty = 3.5 WHERE content_id = ?",
            [served.content_id],
        )

    assessment_service.record(
        polish_workspace.paths,
        run=run.run_id,
        content_id=served.content_id,
        score=1.0,
        clock=polish_workspace.clock,
    )

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        scored = database.one(
            "SELECT difficulty FROM assessment_results WHERE run_id = ? AND content_id = ?",
            [run.run_id, served.content_id],
        )
    assert scored is not None
    assert float(scored[0]) == served.difficulty
    assert float(scored[0]) != 3.5


def test_the_facts_a_score_needs_are_recorded_when_the_task_is_served(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        row = database.one(
            "SELECT task_type, level_code, difficulty, content_family, modality, is_anchor, "
            "rubric_version, content_hash FROM assessment_run_tasks "
            "WHERE run_id = ? AND content_id = ?",
            [run.run_id, served.content_id],
        )
    assert row is not None
    assert str(row[0]) == served.task_type
    assert str(row[1]) == served.level_code
    assert float(row[2]) == served.difficulty
    assert str(row[3]) == served.content_family
    assert str(row[4]) == served.modality
    assert int(row[6]) == served.rubric_version
    assert len(str(row[7])) == 64


def test_a_run_refuses_to_serve_from_a_pack_it_did_not_start_against(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """Two banks in one run is two runs wearing one estimate."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    first = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(first, assessment_service.NextTaskReport)
    updated = _republished(PILOT_PACK, tmp_path / "pl-pilot-next", version=NEXT_PILOT_VERSION)
    pack_service.install(
        polish_workspace.paths, updated, clock=polish_workspace.clock, allow_update=True
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.next_task(
            polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "assessment_pack_drifted"


# Finding 3: the inspection duty never shrinks to fit the import.


def test_importing_a_subset_cannot_lower_the_inspection_duty(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """Draft forty, import the two convincing ones, inspect two: the whole attack."""

    authoring_service.validate_template(
        installed_pilot.paths, payload=TEMPLATE, clock=installed_pilot.clock
    )
    batch = authoring_service.generate_draft(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        item_count=40,
        clock=installed_pilot.clock,
    )
    assert batch.required_sample == 40

    imported = authoring_service.import_items(
        installed_pilot.paths,
        items=_drafts(1, 2),
        batch_id=batch.batch_id,
        clock=installed_pilot.clock,
    )

    assert imported.item_count == 2
    assert imported.required_sample == 40
    assert imported.acceptable is False
    assert any("can no longer be discharged" in warning for warning in imported.warnings)

    for content_id in imported.items:
        authoring_service.review(
            installed_pilot.paths,
            content_id=content_id,
            axis="linguistic",
            state="machine-checked",
            reviewer_kind="machine",
            reviewer="checker",
            method="inspection",
            inspection="accepted",
            clock=installed_pilot.clock,
        )

    final = authoring_service.batch_report(
        installed_pilot.paths, batch_id=batch.batch_id, clock=installed_pilot.clock
    )
    assert final.status != "accepted"
    template = authoring_service.validate_template(
        installed_pilot.paths,
        template_key=TEMPLATE["template_key"],
        version=1,
        clock=installed_pilot.clock,
    )
    assert template.inspected_runs == 0


def test_a_batch_filled_to_its_plan_is_still_acceptable(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """The rule bites on a filtered import, not on an honest one."""

    authoring_service.validate_template(
        installed_pilot.paths, payload=TEMPLATE, clock=installed_pilot.clock
    )

    report = _fully_inspect(installed_pilot, run=1, count=3)

    assert report.planned_items == report.item_count == 3
    assert report.acceptable is True
    assert report.status == "accepted"


# Finding 4: no track is left without a pack.


def test_db_check_names_a_track_that_has_no_pack(
    polish_workspace: PolishWorkspace,
) -> None:
    """A nullable column needs a check, or the unresolvable state is simply invisible."""

    with (
        open_writer(
            polish_workspace.paths, command="test.unbind", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE learning_tracks SET pack_id = NULL")

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        report = check_database(database)

    failed = {check.name for check in report.failures}
    assert report.ok is False
    assert "track_pack_binding" in failed


def test_a_healthy_workspace_passes_the_track_pack_binding_check(
    polish_workspace: PolishWorkspace,
) -> None:
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        report = check_database(database)

    binding = next(check for check in report.checks if check.name == "track_pack_binding")
    assert binding.status == "ok"
    assert report.ok is True


# Finding 5: a paused run is not an open run.


def test_a_paused_run_serves_no_task_until_it_is_resumed(
    polish_workspace: PolishWorkspace,
) -> None:
    """`paused` has an outgoing transition, which is not the same as being open."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="paused", clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.next_task(
            polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "assessment_run_paused"
    assert "resume it first" in failure.value.payload.message


def test_a_paused_run_takes_no_score_until_it_is_resumed(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="paused", clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            score=1.0,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "assessment_run_paused"


def test_resuming_a_paused_run_lets_it_serve_again(
    polish_workspace: PolishWorkspace,
) -> None:
    """A calibration is meant to span sittings; only the unresumed state is refused."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="paused", clock=polish_workspace.clock
    )
    assessment_service.set_status(
        polish_workspace.paths, run=run.run_id, status="in-progress", clock=polish_workspace.clock
    )

    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )

    assert isinstance(served, assessment_service.NextTaskReport)


# Finding 6: a level belongs to a pack, not to a framework for ever.


def test_a_pack_that_drops_a_level_stops_offering_it(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """The global level table only grows, so membership had to move to the pack."""

    narrowed = tmp_path / "pl-pilot-narrow"
    shutil.copytree(PILOT_PACK, narrowed)
    document = json.loads((narrowed / "manifest.json").read_text(encoding="utf-8"))
    frameworks = document["frameworks"]
    frameworks[0]["levels"] = [
        level for level in frameworks[0]["levels"] if level not in {"C1", "C2"}
    ]
    _republished(PILOT_PACK, narrowed, version=NEXT_PILOT_VERSION, frameworks=frameworks)
    pack_service.install(
        polish_workspace.paths, narrowed, clock=polish_workspace.clock, allow_update=True
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            polish_workspace.paths,
            target_language="pl",
            framework="cefr",
            region="UA",
            declared_level="C2",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "level_not_in_framework"
    assert "no longer teaches" in failure.value.payload.message
    # And the framework's own order is untouched: an agreed order cannot be renegotiated.
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        globally = [
            str(code)
            for (code,) in database.query(
                "SELECT level_code FROM proficiency_framework_levels WHERE framework_id = 'cefr' "
                "ORDER BY sequence"
            )
        ]
        for_pack = [
            str(code)
            for (code,) in database.query(
                "SELECT level_code FROM pack_framework_levels WHERE framework_id = 'cefr' "
                "ORDER BY sequence"
            )
        ]
    assert globally == ["A1", "A2", "B1", "B2", "C1", "C2"]
    assert for_pack == ["A1", "A2", "B1", "B2"]


def test_a_track_reports_the_levels_its_own_pack_teaches(
    polish_workspace: PolishWorkspace,
) -> None:
    track = learner_service.show_track(polish_workspace.paths, clock=polish_workspace.clock)

    assert track.framework_levels == ("A1", "A2", "B1", "B2", "C1", "C2")


# --- Third review round --------------------------------------------------------------


def _narrowed_to(source: Path, target: Path, levels: list[str], *, version: str) -> Path:
    """The pack republished teaching only `levels` of its first framework."""

    shutil.copytree(source, target)
    document = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    frameworks = document["frameworks"]
    frameworks[0]["levels"] = levels
    return _republished(source, target, version=version, frameworks=frameworks)


def _score_every_task(workspace: PolishWorkspace, run_id: str) -> int:
    served = 0
    while True:
        outcome = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
        if not isinstance(outcome, assessment_service.NextTaskReport):
            return served
        assessment_service.record(
            workspace.paths,
            run=run_id,
            content_id=outcome.content_id,
            score=1.0,
            clock=workspace.clock,
        )
        served += 1


def test_a_scored_run_keeps_its_bands_when_the_pack_narrows(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """A level's *index* is what the posterior grid means.

    Re-reading the track's current list restated an already-scored run's C1 as B2, while
    the report still named the pack version the run had actually used.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    assert _score_every_task(polish_workspace, run.run_id)
    before = assessment_service.report(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    narrowed = _narrowed_to(
        PILOT_PACK, tmp_path / "pl-narrow", ["A1", "A2", "B1", "B2"], version=NEXT_PILOT_VERSION
    )
    # Nothing binds the levels this run used any more, so the update is allowed.
    learner_service.update_track(
        polish_workspace.paths, target_level="B2", clock=polish_workspace.clock
    )
    pack_service.install(
        polish_workspace.paths, narrowed, clock=polish_workspace.clock, allow_update=True
    )

    after = assessment_service.report(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )

    assert after.framework_levels == before.framework_levels
    assert after.pack_version == before.pack_version
    assert {entry.dimension: entry.estimated_level for entry in after.dimensions} == {
        entry.dimension: entry.estimated_level for entry in before.dimensions
    }
    assert {entry.dimension: entry.credible_high for entry in after.dimensions} == {
        entry.dimension: entry.credible_high for entry in before.dimensions
    }


def test_a_run_pins_the_level_list_it_was_opened_against(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        conditions = json.loads(
            str(
                database.scalar(
                    "SELECT conditions_json FROM assessment_runs WHERE run_id = ?",
                    [run.run_id],
                )
            )
        )

    assert conditions["framework_levels"] == ["A1", "A2", "B1", "B2", "C1", "C2"]
    assert run.framework_levels == ("A1", "A2", "B1", "B2", "C1", "C2")


def test_an_update_that_withdraws_a_level_a_track_uses_is_refused(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """Withdrawing a level is not a change learner state can absorb silently."""

    learner_service.update_track(
        polish_workspace.paths, target_level="C2", clock=polish_workspace.clock
    )
    narrowed = _narrowed_to(
        PILOT_PACK, tmp_path / "pl-narrow", ["A1", "A2", "B1", "B2"], version=NEXT_PILOT_VERSION
    )

    preview = pack_service.diff(polish_workspace.paths, narrowed, clock=polish_workspace.clock)
    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            polish_workspace.paths, narrowed, clock=polish_workspace.clock, allow_update=True
        )

    assert failure.value.payload.code == "pack_levels_in_use"
    assert [(entry.field, entry.level) for entry in preview.level_conflicts] == [
        ("target_level", "C2")
    ]
    assert all(entry.track_id == polish_workspace.track_id for entry in preview.level_conflicts)


def test_resolving_the_tracks_lets_the_narrowing_update_proceed(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """The refusal demands an explicit resolution; it does not forbid narrowing."""

    learner_service.update_track(
        polish_workspace.paths, target_level="C2", clock=polish_workspace.clock
    )
    narrowed = _narrowed_to(
        PILOT_PACK, tmp_path / "pl-narrow", ["A1", "A2", "B1", "B2"], version=NEXT_PILOT_VERSION
    )

    learner_service.update_track(
        polish_workspace.paths, target_level="B2", clock=polish_workspace.clock
    )
    report = pack_service.install(
        polish_workspace.paths, narrowed, clock=polish_workspace.clock, allow_update=True
    )

    assert report.version == NEXT_PILOT_VERSION
    assert report.diff is not None
    assert report.diff.level_conflicts == ()


def test_a_preview_of_a_narrowing_update_is_never_refused(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """A preview whose whole job is to show the conflict must not raise instead."""

    learner_service.update_track(
        polish_workspace.paths, target_level="C2", clock=polish_workspace.clock
    )
    narrowed = _narrowed_to(
        PILOT_PACK, tmp_path / "pl-narrow", ["A1", "A2", "B1", "B2"], version=NEXT_PILOT_VERSION
    )

    dry = pack_service.install(
        polish_workspace.paths,
        narrowed,
        clock=polish_workspace.clock,
        allow_update=True,
        dry_run=True,
    )

    assert dry.dry_run is True
    assert dry.diff is not None
    assert dry.diff.level_conflicts


def test_a_declared_level_the_pack_stopped_teaching_is_announced(
    polish_workspace: PolishWorkspace,
) -> None:
    """Falling back to a broad prior is right arithmetic; doing it in silence is not."""

    with (
        open_writer(
            polish_workspace.paths, command="test.withdraw", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("DELETE FROM pack_framework_levels WHERE level_code = 'A2'")

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    assert any("broad prior" in warning for warning in run.warnings)
    assert any("does not teach" in warning for warning in run.warnings)


# --- Fourth review round -------------------------------------------------------------


def _reframed(
    source: Path, target: Path, *, framework_id: str, version: str, framework_version: str = "2020"
) -> Path:
    """The pack republished teaching the same levels under a different framework ID.

    Every file naming the framework moves with it -- the proficiency descriptors, the
    resource bundles, and the assessment definitions -- because the loader refuses a pack
    whose file names a framework the manifest does not declare.
    """

    shutil.copytree(source, target)
    for relative in ("proficiency", "resource-bundles", "assessments"):
        for path in sorted((target / relative).glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("framework") != "cefr":
                continue
            document["framework"] = framework_id
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    framework = dict(manifest["frameworks"][0])
    framework["framework_id"] = framework_id
    framework["version"] = framework_version
    return _republished(source, target, version=version, frameworks=[framework])


def _reversioned(source: Path, target: Path, *, framework_version: str, version: str) -> Path:
    """The pack republished redefining its framework's version under the same ID."""

    shutil.copytree(source, target)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    frameworks = manifest["frameworks"]
    frameworks[0]["version"] = framework_version
    return _republished(source, target, version=version, frameworks=frameworks)


def test_a_track_naming_no_level_still_blocks_removal_of_its_framework(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    """The level guard could not see this: there is no level to conflict.

    A learner who has declared nothing yet has a track with all three level fields unset,
    so dropping its entire framework passed the level check untouched. The track then
    recorded a framework its own pack no longer declares, and the next run fell back to
    the framework's global level list -- an order no installed pack vouches for.
    """

    learner_service.create_user(
        installed_pilot.paths,
        display_name="Учащийся без заявленного уровня",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        support_languages=["en"],
        clock=installed_pilot.clock,
    )
    track = learner_service.create_track(
        installed_pilot.paths,
        target_language="pl",
        framework="cefr",
        clock=installed_pilot.clock,
    )
    assert (track.declared_level, track.current_level, track.target_level) == (None, None, None)
    # Re-keyed, so the pack itself is well-formed and the track guard is what refuses.
    reframed = _reframed_and_rekeyed(
        PILOT_PACK, tmp_path / "pl-reframed", version=NEXT_PILOT_VERSION
    )

    preview = pack_service.diff(installed_pilot.paths, reframed, clock=installed_pilot.clock)
    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            installed_pilot.paths, reframed, clock=installed_pilot.clock, allow_update=True
        )

    assert failure.value.payload.code == "pack_frameworks_in_use"
    # No level conflicts at all: this is exactly the gap the framework guard closes.
    assert preview.level_conflicts == ()
    assert [(entry.track_id, entry.framework_id) for entry in preview.framework_conflicts] == [
        (track.track_id, "cefr")
    ]


def test_a_track_with_levels_also_blocks_removal_of_its_framework(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    reframed = _reframed_and_rekeyed(
        PILOT_PACK, tmp_path / "pl-reframed", version=NEXT_PILOT_VERSION
    )

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            polish_workspace.paths, reframed, clock=polish_workspace.clock, allow_update=True
        )

    assert failure.value.payload.code == "pack_frameworks_in_use"


def test_a_preview_of_a_framework_removal_is_never_refused(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    reframed = _reframed(
        PILOT_PACK, tmp_path / "pl-reframed", framework_id="cefr-2024", version=NEXT_PILOT_VERSION
    )

    dry = pack_service.install(
        polish_workspace.paths,
        reframed,
        clock=polish_workspace.clock,
        allow_update=True,
        dry_run=True,
    )

    assert dry.dry_run is True
    assert dry.diff is not None
    assert dry.diff.framework_conflicts


def test_db_check_names_a_track_whose_pack_dropped_its_framework(
    polish_workspace: PolishWorkspace,
) -> None:
    """`pack install` refuses the update; this catches the state however it arrived."""

    with (
        open_writer(
            polish_workspace.paths, command="test.drop", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("DELETE FROM pack_frameworks WHERE framework_id = 'cefr'")

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        report = check_database(database)

    assert report.ok is False
    assert "track_framework_binding" in {check.name for check in report.failures}


def test_a_healthy_workspace_passes_the_track_framework_binding_check(
    polish_workspace: PolishWorkspace,
) -> None:
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        report = check_database(database)

    binding = next(check for check in report.checks if check.name == "track_framework_binding")
    assert binding.status == "ok"


def test_redefining_an_installed_frameworks_version_is_refused(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    """A framework ID names one scale, and a track records only the ID.

    Registration read the recorded version and never compared it, so `cefr` could be
    updated from 2020 to 2024 while the stored row stayed 2020 -- every level label
    already recorded against that ID silently changing meaning.
    """

    reversioned = _reversioned(
        PILOT_PACK, tmp_path / "pl-2024", framework_version="2024", version=NEXT_PILOT_VERSION
    )

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            installed_pilot.paths, reversioned, clock=installed_pilot.clock, allow_update=True
        )

    assert failure.value.payload.code == "framework_identity_conflict"
    assert "new framework ID" in failure.value.payload.message
    reasons = {detail.reason for detail in failure.value.payload.details}
    assert "version differs" in reasons
    with open_reader(installed_pilot.paths, clock=installed_pilot.clock) as database:
        recorded = database.scalar(
            "SELECT version FROM proficiency_frameworks WHERE framework_id = 'cefr'"
        )
    assert str(recorded) == "2020"


def test_renaming_an_installed_framework_is_refused(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    target = tmp_path / "pl-renamed"
    shutil.copytree(PILOT_PACK, target)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    frameworks = manifest["frameworks"]
    frameworks[0]["name"] = "Council of Europe Reference Levels"
    renamed = _republished(PILOT_PACK, target, version=NEXT_PILOT_VERSION, frameworks=frameworks)

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            installed_pilot.paths, renamed, clock=installed_pilot.clock, allow_update=True
        )

    assert failure.value.payload.code == "framework_identity_conflict"
    assert "name differs" in {detail.reason for detail in failure.value.payload.details}


def test_an_unchanged_framework_reinstalls_without_complaint(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    """The guard rejects redefinition, not republication."""

    same = _republished(PILOT_PACK, tmp_path / "pl-same", version=NEXT_PILOT_VERSION)

    report = pack_service.install(
        installed_pilot.paths, same, clock=installed_pilot.clock, allow_update=True
    )

    assert report.version == NEXT_PILOT_VERSION
    assert report.frameworks == ("cefr",)


# --- Fifth review round: the transition has to be completable -------------------------


def test_a_learner_can_be_moved_onto_a_replacement_framework(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """The whole workflow the refusal names, end to end, through supported commands.

    Refusing the update was right; naming a remediation nobody could perform was not.
    Creating the new track first failed (`framework_not_supported` -- the installed pack
    does not declare it yet), archiving the old track did not clear the refusal, and the
    replacement was rejected as a duplicate because archived tracks held the slot. The
    workspace could never adopt a replacement framework at all.
    """

    reframed = _reframed_and_rekeyed(PILOT_PACK, tmp_path / "pl-2024", version=NEXT_PILOT_VERSION)

    # 1. The update is refused while the learner is still taught in the old framework.
    with pytest.raises(LinguaWikiError) as blocked:
        pack_service.install(
            polish_workspace.paths, reframed, clock=polish_workspace.clock, allow_update=True
        )
    assert blocked.value.payload.code == "pack_frameworks_in_use"
    assert "archive those tracks" in blocked.value.payload.message

    # 2. Archive the old programme. It keeps every label it recorded.
    archived = learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="archived",
        clock=polish_workspace.clock,
    )
    assert archived.status == "archived"

    # 3. The update now applies.
    installed = pack_service.install(
        polish_workspace.paths, reframed, clock=polish_workspace.clock, allow_update=True
    )
    assert installed.frameworks == ("cefr-2024",)

    # 4. The replacement track is created under the new framework.
    replacement = learner_service.create_track(
        polish_workspace.paths,
        target_language="pl",
        framework="cefr-2024",
        declared_level="A2",
        target_level="B1",
        clock=polish_workspace.clock,
    )
    assert replacement.track_id != polish_workspace.track_id
    assert replacement.proficiency_framework == "cefr-2024"
    assert replacement.framework_levels == ("A1", "A2", "B1", "B2", "C1", "C2")

    # 5. And it is a working programme: a calibration serves a task from the new bank.
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert run.framework_id == "cefr-2024"
    assert isinstance(served, assessment_service.NextTaskReport)

    # 6. The workspace is healthy, and the old programme is still readable as history.
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        report = check_database(database)
    assert report.ok is True
    assert report.failures == ()
    history = learner_service.show_track(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert (history.status, history.proficiency_framework) == ("archived", "cefr")
    assert history.declared_level == "A2"
    # Interpretable through the global framework record, which only ever grows.
    assert history.framework_levels == ("A1", "A2", "B1", "B2", "C1", "C2")


def test_an_archived_track_does_not_hold_the_language_slot(
    polish_workspace: PolishWorkspace,
) -> None:
    """A replacement track was rejected as a duplicate of the track it replaces."""

    learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="archived",
        clock=polish_workspace.clock,
    )

    replacement = learner_service.create_track(
        polish_workspace.paths,
        target_language="pl",
        framework="cefr",
        declared_level="B1",
        clock=polish_workspace.clock,
    )

    assert replacement.track_id != polish_workspace.track_id
    assert replacement.declared_level == "B1"


def test_a_live_track_still_holds_the_language_slot(
    polish_workspace: PolishWorkspace,
) -> None:
    """Only archiving frees it; two live tracks for one language stay refused."""

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            polish_workspace.paths,
            target_language="pl",
            framework="cefr",
            declared_level="B1",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "track_exists"
    assert "archive it first" in failure.value.payload.message


def test_db_check_reports_duplicate_live_tracks(
    polish_workspace: PolishWorkspace,
) -> None:
    """Migration 0017 dropped the unique index, so the narrowed rule needs a check."""

    with (
        open_writer(
            polish_workspace.paths, command="test.duplicate", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        now = transaction.now()
        transaction.execute(
            # Region and script are copied too: the rule is scoped by them, so a
            # partial copy would land in a different group and prove nothing.
            "INSERT INTO learning_tracks (track_id, user_id, target_language, region, script, "
            "proficiency_framework, status, is_primary, timezone, pack_id, created_at, "
            "updated_at) SELECT 'trk_01ARZ3NDEKTSV4RRFFQ69G5FAV', user_id, target_language, "
            "region, script, proficiency_framework, 'active', FALSE, timezone, pack_id, ?, ? "
            "FROM learning_tracks WHERE track_id = ?",
            [now, now, polish_workspace.track_id],
        )

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        report = check_database(database)

    assert report.ok is False
    assert "active_track_uniqueness" in {check.name for check in report.failures}


def test_a_bundle_naming_an_undeclared_framework_is_refused_at_load(
    tmp_path: Path,
) -> None:
    """It used to raise a bare StopIteration from inside the install transaction."""

    from linguawiki.packs.format import PackError, load_pack

    target = tmp_path / "pl-broken"
    shutil.copytree(PILOT_PACK, target)
    bundle = sorted((target / "resource-bundles").glob("*.json"))[0]
    document = json.loads(bundle.read_text(encoding="utf-8"))
    document["framework"] = "not-a-declared-framework"
    bundle.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _republished(PILOT_PACK, target, version=NEXT_PILOT_VERSION)

    with pytest.raises(PackError) as failure:
        load_pack(target)

    assert failure.value.payload.code == "pack_framework_unknown"


# --- Sixth review round ---------------------------------------------------------------


def _rekey(value: str) -> str:
    return value.replace("cefr-", "cefr-2024-", 1) if value.startswith("cefr-") else value


def _reframed_and_rekeyed(source: Path, target: Path, *, version: str) -> Path:
    """Rename the framework to cefr-2024 *and* give its scoped records new identities.

    This is the remediation the rebinding refusal asks for. The stable keys already carry
    the framework by convention, so re-keying is mechanical; an assessment form has no
    framework in its key, so its lever is a new version, which its derived identity
    includes.
    """

    shutil.copytree(source, target)
    for folder in ("proficiency", "resource-bundles", "assessments"):
        for path in sorted((target / folder).glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("framework") != "cefr":
                continue
            document["framework"] = "cefr-2024"
            for descriptor in document.get("descriptors", []):
                descriptor["stable_key"] = descriptor["stable_key"].replace(
                    "cefr.", "cefr-2024.", 1
                )
            if "bundle_key" in document:
                document["bundle_key"] = _rekey(document["bundle_key"])
            if "depends_on" in document:
                document["depends_on"] = [_rekey(key) for key in document["depends_on"]]
            if "form_key" in document:
                document["version"] = int(document.get("version", 1)) + 1
            for entry in document.get("items", []):
                if entry.get("item_kind") == "descriptor":
                    entry["item_ref"] = entry["item_ref"].replace("cefr.", "cefr-2024.", 1)
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    framework = dict(manifest["frameworks"][0])
    framework["framework_id"] = "cefr-2024"
    framework["version"] = "2024"
    bundles = [
        {
            **bundle,
            "bundle_key": _rekey(bundle["bundle_key"]),
            "depends_on": [_rekey(key) for key in bundle.get("depends_on", [])],
        }
        for bundle in manifest["bundles"]
    ]
    return _republished(source, target, version=version, frameworks=[framework], bundles=bundles)


def _framework_ids(workspace: SyntheticWorkspace, table: str, *, where: str = "") -> set[str]:
    with open_reader(workspace.paths, clock=workspace.clock) as database:
        return {
            str(value)
            for (value,) in database.query(f"SELECT DISTINCT framework_id FROM {table} {where}")
        }


def test_a_framework_change_may_not_keep_a_scoped_records_identity(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Otherwise the record silently keeps pointing at the framework it left.

    The installers never update `framework_id` -- DuckDB rewrites such an update as a
    delete and an insert, which referencing rows refuse -- so a renamed framework left
    descriptors, bundles, and forms behind on the old one while `pack_frameworks` named
    the new. Nothing noticed, because serving does not read those columns.
    """

    reframed = _reframed(
        PILOT_PACK,
        tmp_path / "pl-same-keys",
        framework_id="cefr-2024",
        version=NEXT_PILOT_VERSION,
        framework_version="2024",
    )

    preview = pack_service.diff(installed_pilot.paths, reframed, clock=installed_pilot.clock)
    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            installed_pilot.paths, reframed, clock=installed_pilot.clock, allow_update=True
        )

    assert failure.value.payload.code == "pack_framework_rebinding"
    # All three tables, not just the one the fix was noticed in.
    assert {rebinding.record for rebinding in preview.framework_rebindings} == {
        "proficiency_descriptors",
        "resource_bundles",
        "assessment_definitions",
    }
    assert all(
        rebinding.installed_framework == "cefr" and rebinding.incoming_framework == "cefr-2024"
        for rebinding in preview.framework_rebindings
    )
    # Nothing moved: the refusal happens before any mutation.
    assert _framework_ids(installed_pilot, "pack_frameworks") == {"cefr"}
    assert _framework_ids(installed_pilot, "proficiency_descriptors") == {"cefr"}


def test_a_preview_of_a_rebinding_update_is_never_refused(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    reframed = _reframed(
        PILOT_PACK,
        tmp_path / "pl-same-keys",
        framework_id="cefr-2024",
        version=NEXT_PILOT_VERSION,
        framework_version="2024",
    )

    dry = pack_service.install(
        installed_pilot.paths,
        reframed,
        clock=installed_pilot.clock,
        allow_update=True,
        dry_run=True,
    )

    assert dry.dry_run is True
    assert dry.diff is not None
    assert dry.diff.framework_rebindings


def test_re_keying_the_scoped_records_completes_the_framework_change(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    """The remediation the refusal names, with every surface checked afterwards."""

    rekeyed = _reframed_and_rekeyed(PILOT_PACK, tmp_path / "pl-rekeyed", version=NEXT_PILOT_VERSION)

    report = pack_service.install(
        installed_pilot.paths, rekeyed, clock=installed_pilot.clock, allow_update=True
    )

    assert report.frameworks == ("cefr-2024",)
    assert _framework_ids(installed_pilot, "pack_frameworks") == {"cefr-2024"}
    # The old records survive as deprecated history under the scale they were authored
    # on; everything the pack still serves names the new framework.
    for table, key in (
        ("proficiency_descriptors", "descriptor_id"),
        ("resource_bundles", "bundle_id"),
    ):
        live = _framework_ids(
            installed_pilot,
            table,
            where=(
                f"record WHERE EXISTS (SELECT 1 FROM content_records item WHERE "
                f"item.content_id = record.{key} AND item.lifecycle <> 'deprecated')"
            ),
        )
        assert live == {"cefr-2024"}, table
    with open_reader(installed_pilot.paths, clock=installed_pilot.clock) as database:
        definitions = database.query(
            "SELECT coalesce(status, 'active'), framework_id, "
            "(SELECT count(*) FROM assessment_tasks task "
            " WHERE task.definition_id = definition.definition_id) "
            "FROM assessment_definitions definition ORDER BY framework_id"
        )
        report_check = check_database(database)
    # The withdrawn form is superseded and empty; every task followed the live form.
    assert [(str(row[0]), str(row[1]), int(row[2])) for row in definitions] == [
        ("superseded", "cefr", 0),
        ("active", "cefr-2024", 39),
    ]
    assert report_check.ok is True
    assert report_check.failures == ()


def test_withdrawing_every_assessment_form_supersedes_the_installed_bank(
    installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    """An empty incoming definition set must not turn `NOT IN` into SQL unknown."""

    replacement = _reframed_and_rekeyed(
        PILOT_PACK, tmp_path / "pl-without-assessments", version=NEXT_PILOT_VERSION
    )
    for path in sorted((replacement / "assessments").glob("*.json")):
        path.unlink()
    for path in sorted((replacement / "resource-bundles").glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        document["items"] = [
            item for item in document.get("items", []) if item.get("item_kind") != "assessment_task"
        ]
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _republished(replacement, replacement, version=NEXT_PILOT_VERSION)

    report = pack_service.install(
        installed_pilot.paths,
        replacement,
        clock=installed_pilot.clock,
        allow_update=True,
    )

    assert report.frameworks == ("cefr-2024",)
    with open_reader(installed_pilot.paths, clock=installed_pilot.clock) as database:
        definitions = database.query(
            "SELECT status, framework_id FROM assessment_definitions ORDER BY framework_id"
        )
        report_check = check_database(database)
    assert [(str(status), str(framework)) for status, framework in definitions] == [
        ("superseded", "cefr")
    ]
    assert report_check.ok is True
    assert report_check.failures == ()


def test_db_check_names_a_scoped_record_left_on_a_withdrawn_framework(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """`pack install` refuses the update; this sees the state however it arrived."""

    with (
        open_writer(
            installed_pilot.paths, command="test.withdraw", clock=installed_pilot.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("DELETE FROM pack_frameworks WHERE framework_id = 'cefr'")

    with open_reader(installed_pilot.paths, clock=installed_pilot.clock) as database:
        report = check_database(database)

    assert report.ok is False
    assert "pack_framework_scoping" in {check.name for check in report.failures}


def test_a_healthy_workspace_passes_the_framework_scoping_check(
    installed_pilot: SyntheticWorkspace,
) -> None:
    with open_reader(installed_pilot.paths, clock=installed_pilot.clock) as database:
        report = check_database(database)

    scoping = next(check for check in report.checks if check.name == "pack_framework_scoping")
    assert scoping.status == "ok"


def test_a_task_a_learner_has_answered_may_not_be_refiled(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """Moving it would rewrite the row their run points at, so it is refused."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    served = assessment_service.next_task(
        polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
    )
    assert isinstance(served, assessment_service.NextTaskReport)
    rekeyed = _reframed_and_rekeyed(PILOT_PACK, tmp_path / "pl-rekeyed", version=NEXT_PILOT_VERSION)

    preview = pack_service.diff(polish_workspace.paths, rekeyed, clock=polish_workspace.clock)
    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            polish_workspace.paths, rekeyed, clock=polish_workspace.clock, allow_update=True
        )

    assert failure.value.payload.code == "pack_task_reform_blocked"
    assert served.content_id in {conflict.content_id for conflict in preview.task_reforms}


# Finding 2: reviving an archived track has to re-earn everything archiving let go of.


def test_reviving_an_archived_track_whose_slot_is_taken_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    """Activation used to succeed and leave `db check` failing straight afterwards."""

    learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="archived",
        clock=polish_workspace.clock,
    )
    learner_service.create_track(
        polish_workspace.paths,
        target_language="pl",
        framework="cefr",
        declared_level="B1",
        clock=polish_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.set_track_status(
            polish_workspace.paths,
            track=polish_workspace.track_id,
            status="active",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "track_slot_taken"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert check_database(database).ok is True


def test_reviving_an_archived_track_after_its_framework_was_replaced_is_refused(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """Archiving is what let the framework go; reviving has to answer for it."""

    learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="archived",
        clock=polish_workspace.clock,
    )
    rekeyed = _reframed_and_rekeyed(PILOT_PACK, tmp_path / "pl-rekeyed", version=NEXT_PILOT_VERSION)
    pack_service.install(
        polish_workspace.paths, rekeyed, clock=polish_workspace.clock, allow_update=True
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.set_track_status(
            polish_workspace.paths,
            track=polish_workspace.track_id,
            status="active",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "framework_not_supported"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert check_database(database).ok is True


def test_reviving_an_archived_track_after_its_level_was_withdrawn_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="archived",
        clock=polish_workspace.clock,
    )
    with (
        open_writer(
            polish_workspace.paths, command="test.withdraw", clock=polish_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("DELETE FROM pack_framework_levels WHERE level_code = 'A2'")

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.set_track_status(
            polish_workspace.paths,
            track=polish_workspace.track_id,
            status="active",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "level_not_in_framework"


def test_reviving_an_archived_track_that_still_fits_succeeds(
    polish_workspace: PolishWorkspace,
) -> None:
    """The guard re-establishes the invariants; it does not forbid reviving."""

    learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="archived",
        clock=polish_workspace.clock,
    )

    revived = learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="active",
        clock=polish_workspace.clock,
    )

    assert revived.status == "active"
    assert revived.declared_level == "A2"
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        assert check_database(database).ok is True


def test_archiving_a_track_is_never_blocked_by_the_revival_guard(
    polish_workspace: PolishWorkspace,
) -> None:
    """Archiving is the remediation, so it must always remain available."""

    paused = learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="paused",
        clock=polish_workspace.clock,
    )
    archived = learner_service.set_track_status(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="archived",
        clock=polish_workspace.clock,
    )

    assert (paused.status, archived.status) == ("paused", "archived")
