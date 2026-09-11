"""Prior-course import, positioning, and the audit that checks what transferred.

The rule the whole workflow exists to enforce: a claim is not evidence. Declaring units
complete reaches `encountered` and no further, and an unprobed target stays unverified
rather than becoming confirmed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import curriculum as curriculum_service
from linguawiki.services import resources as resource_service
from tests.conftest import PolishWorkspace

OUTLINE: dict[str, Any] = {
    "schema_name": "lingua.curriculum.v1",
    "schema_version": 1,
    "title": "Own A1-A2 study plan",
    "kind": "user-authored",
    "version": "1",
    "rights_status": "user-authored",
    "provenance": "written by the learner from their own notes",
    "units": [
        {
            "code": "u1",
            "title": "Greetings and introductions",
            "level": "A1",
            "objectives": [
                {
                    "objective": "Greet and take leave",
                    "mapped_kind": "knowledge",
                    "mapped_key": "pl.lex.dzien-dobry",
                    "map_confidence": "high",
                },
                {
                    "objective": "Say who I am",
                    "mapped_kind": "knowledge",
                    "mapped_key": "pl.gram.to-jest",
                    "map_confidence": "medium",
                },
                {"objective": "Spell my surname aloud"},
            ],
        },
        {
            "code": "u2",
            "title": "Travel and tickets",
            "level": "A2",
            "objectives": [
                {
                    "objective": "Buy a ticket",
                    "mapped_kind": "knowledge",
                    "mapped_key": "pl.lex.bilet",
                    "map_confidence": "high",
                },
                {
                    "objective": "Ask about a platform",
                    "mapped_kind": "knowledge",
                    "mapped_key": "pl.lex.peron",
                    "map_confidence": "high",
                },
                {
                    "objective": "Use the locative for places",
                    "mapped_kind": "knowledge",
                    "mapped_key": "pl.gram.lokatywny-miejsce",
                    "map_confidence": "high",
                },
            ],
        },
        {
            "code": "u3",
            "title": "At the doctor",
            "level": "A2",
            "parent_code": "u2",
            "objectives": [
                {
                    "objective": "Describe pain",
                    "mapped_kind": "knowledge",
                    "mapped_key": "pl.constr.boli-mnie",
                    "map_confidence": "high",
                },
                {
                    "objective": "Understand a prescription",
                    "mapped_kind": "knowledge",
                    "mapped_key": "pl.lex.recepta",
                    "map_confidence": "medium",
                },
            ],
        },
    ],
}


def _imported(workspace: PolishWorkspace) -> Any:
    return curriculum_service.import_curriculum(workspace.paths, OUTLINE, clock=workspace.clock)


def test_an_outline_is_stored_with_its_rights_and_provenance(
    polish_workspace: PolishWorkspace,
) -> None:
    report = _imported(polish_workspace)

    assert report.rights_status == "user-authored"
    assert report.provenance == "written by the learner from their own notes"
    assert [unit.code for unit in report.units] == ["u1", "u2", "u3"]
    assert report.units[2].parent_code == "u2"
    assert all(unit.state == "not-started" for unit in report.units)


def test_an_unmapped_objective_stays_an_explicit_gap(
    polish_workspace: PolishWorkspace,
) -> None:
    """The gap is the useful part of an import; it must not disappear."""

    report = _imported(polish_workspace)

    assert report.total_objectives == 8
    assert report.mapped_objectives == 7
    assert [entry.objective for entry in report.unmapped_objectives] == ["Spell my surname aloud"]
    assert any("explicit gaps" in warning for warning in report.warnings)


def test_a_mapping_to_something_the_pack_does_not_have_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    payload = {
        **OUTLINE,
        "units": [
            {
                "code": "u1",
                "title": "Unit",
                "objectives": [
                    {
                        "objective": "Do something",
                        "mapped_kind": "knowledge",
                        "mapped_key": "pl.lex.nieistniejacy",
                        "map_confidence": "high",
                    }
                ],
            }
        ],
    }

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.import_curriculum(
            polish_workspace.paths, payload, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "curriculum_mapping_unresolved"


def test_a_half_declared_mapping_is_refused_by_the_contract() -> None:
    with pytest.raises(ValidationError):
        curriculum_service.CurriculumInput.model_validate(
            {
                **OUTLINE,
                "units": [
                    {
                        "code": "u1",
                        "title": "Unit",
                        "objectives": [{"objective": "Do something", "mapped_kind": "knowledge"}],
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        curriculum_service.CurriculumInput.model_validate(
            {
                **OUTLINE,
                "units": [
                    {
                        "code": "u1",
                        "title": "Unit",
                        "objectives": [
                            {
                                "objective": "Do something",
                                "mapped_kind": "knowledge",
                                "mapped_key": "pl.lex.bilet",
                            }
                        ],
                    }
                ],
            }
        )


def test_an_outline_with_unmanageable_rights_cannot_be_stored() -> None:
    """Only a user-authored, personal-use, or cleared outline may be kept."""

    with pytest.raises(ValidationError):
        curriculum_service.CurriculumInput.model_validate(
            {**OUTLINE, "rights_status": "all rights reserved"}
        )


def test_duplicate_or_self_parented_units_are_refused() -> None:
    with pytest.raises(ValidationError):
        curriculum_service.CurriculumInput.model_validate(
            {
                **OUTLINE,
                "units": [
                    {"code": "u1", "title": "A"},
                    {"code": "u1", "title": "B"},
                ],
            }
        )
    with pytest.raises(ValidationError):
        curriculum_service.CurriculumInput.model_validate(
            {**OUTLINE, "units": [{"code": "u1", "title": "A", "parent_code": "u1"}]}
        )
    with pytest.raises(ValidationError):
        curriculum_service.CurriculumInput.model_validate(
            {**OUTLINE, "units": [{"code": "u1", "title": "A", "parent_code": "u9"}]}
        )


def test_a_level_outside_the_tracks_framework_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    payload = {
        **OUTLINE,
        "units": [{"code": "u1", "title": "Unit", "level": "HSK2", "objectives": []}],
    }

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.import_curriculum(
            polish_workspace.paths, payload, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "level_not_in_framework"


def test_importing_the_same_outline_twice_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)

    with pytest.raises(LinguaWikiError) as failure:
        _imported(polish_workspace)

    assert failure.value.payload.code == "curriculum_exists"


def test_declaring_units_complete_reaches_encountered_and_no_further(
    polish_workspace: PolishWorkspace,
) -> None:
    """A claim creates no recognition or production evidence."""

    _imported(polish_workspace)

    report = curriculum_service.position(
        polish_workspace.paths,
        completed=["u1", "u2"],
        current=["u3"],
        clock=polish_workspace.clock,
    )

    states = {unit.code: unit.state for unit in report.units}
    assert states == {"u1": "claimed-complete", "u2": "claimed-complete", "u3": "current"}
    assert report.encountered_items == 5
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        rows = database.query(
            "SELECT DISTINCT stage, stage_source FROM track_item_state ORDER BY stage"
        )
        evidence = database.query(
            "SELECT sum(positive_evidence + negative_evidence) FROM track_item_state"
        )
        provenance = database.query(
            "SELECT DISTINCT provenance FROM track_curriculum_progress WHERE state <> 'not-started'"
        )
    assert rows == [("encountered", "self-report")]
    assert int(evidence[0][0]) == 0
    assert provenance == [("self-report",)]


def test_a_claim_never_lowers_a_stage_the_learner_earned(
    polish_workspace: PolishWorkspace,
) -> None:
    """A reference import leaves `unseen`; a claim raises it, and never the reverse."""

    resource_service.prepare(polish_workspace.paths, clock=polish_workspace.clock)
    _imported(polish_workspace)

    curriculum_service.position(
        polish_workspace.paths, completed=["u1", "u2"], clock=polish_workspace.clock
    )

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        counts = dict(database.query("SELECT stage, count(*) FROM track_item_state GROUP BY stage"))
    assert counts["encountered"] == 5
    assert counts["unseen"] > 0


def test_a_unit_cannot_be_both_complete_and_current(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.position(
            polish_workspace.paths,
            completed=["u1"],
            current=["u1"],
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "invalid_arguments"


def test_an_unknown_unit_code_is_refused(polish_workspace: PolishWorkspace) -> None:
    _imported(polish_workspace)

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.position(
            polish_workspace.paths, completed=["u9"], clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "curriculum_unit_unknown"


def test_an_audit_cannot_start_before_anything_is_claimed(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.audit_start(polish_workspace.paths, clock=polish_workspace.clock)

    assert failure.value.payload.code == "curriculum_position_required"


def test_the_audit_sample_is_risk_weighted_and_says_why(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1", "u2", "u3"], clock=polish_workspace.clock
    )

    audit = curriculum_service.audit_start(
        polish_workspace.paths, sample_size=5, clock=polish_workspace.clock
    )

    assert audit.status == "in-progress"
    assert audit.sample_size == 5
    assert len(audit.items) == 5
    assert all(item.risk_reason in curriculum_service.RISK_WEIGHTS for item in audit.items)
    weights = [item.risk_weight for item in audit.items]
    assert weights == sorted(weights, reverse=True)
    assert all(item.status == "pending" for item in audit.items)


def test_the_sample_prefers_central_prerequisites(
    polish_workspace: PolishWorkspace,
) -> None:
    """A target many others depend on costs the most to leave unverified."""

    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1", "u2", "u3"], clock=polish_workspace.clock
    )

    audit = curriculum_service.audit_start(
        polish_workspace.paths, sample_size=2, clock=polish_workspace.clock
    )

    assert audit.items[0].risk_reason in {"central-prerequisite", "recent-unit"}
    assert audit.items[0].risk_weight >= audit.items[-1].risk_weight


def test_a_second_audit_is_refused_while_one_is_open(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1"], clock=polish_workspace.clock
    )
    curriculum_service.audit_start(polish_workspace.paths, clock=polish_workspace.clock)

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.audit_start(polish_workspace.paths, clock=polish_workspace.clock)

    assert failure.value.payload.code == "curriculum_audit_in_progress"


def test_an_idempotency_key_replays_the_audit_it_opened(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1"], clock=polish_workspace.clock
    )
    first = curriculum_service.audit_start(
        polish_workspace.paths, idempotency_key="audit-1", clock=polish_workspace.clock
    )

    second = curriculum_service.audit_start(
        polish_workspace.paths, idempotency_key="audit-1", clock=polish_workspace.clock
    )

    assert second.audit_id == first.audit_id


def test_recording_results_is_a_batch_and_a_repeat_is_a_no_op(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1", "u2"], clock=polish_workspace.clock
    )
    audit = curriculum_service.audit_start(
        polish_workspace.paths, sample_size=3, clock=polish_workspace.clock
    )
    results = [
        {"target_ref": audit.items[0].target_ref, "outcome": "correct"},
        {"target_ref": audit.items[1].target_ref, "outcome": "incorrect"},
    ]

    first = curriculum_service.audit_record(
        polish_workspace.paths, results=results, clock=polish_workspace.clock
    )
    second = curriculum_service.audit_record(
        polish_workspace.paths,
        results=[{"target_ref": audit.items[0].target_ref, "outcome": "incorrect"}],
        clock=polish_workspace.clock,
    )

    assert first.recorded == 2
    assert second.recorded == 2
    outcomes = {item.target_ref: item.outcome for item in second.items}
    assert outcomes[audit.items[0].target_ref] == "correct"


def test_an_outcome_outside_the_vocabulary_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1"], clock=polish_workspace.clock
    )
    audit = curriculum_service.audit_start(polish_workspace.paths, clock=polish_workspace.clock)

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.audit_record(
            polish_workspace.paths,
            results=[{"target_ref": audit.items[0].target_ref, "outcome": "brilliant"}],
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "invalid_arguments"


def test_finalizing_turns_misses_into_a_gap_queue_and_reports_what_was_not_probed(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1", "u2", "u3"], clock=polish_workspace.clock
    )
    audit = curriculum_service.audit_start(
        polish_workspace.paths, sample_size=4, clock=polish_workspace.clock
    )
    curriculum_service.audit_record(
        polish_workspace.paths,
        results=[
            {"target_ref": audit.items[0].target_ref, "outcome": "correct"},
            {"target_ref": audit.items[1].target_ref, "outcome": "incorrect"},
            {"target_ref": audit.items[2].target_ref, "outcome": "partial"},
        ],
        clock=polish_workspace.clock,
    )

    final = curriculum_service.audit_finalize(polish_workspace.paths, clock=polish_workspace.clock)

    assert final.status == "finalized"
    assert final.recorded == 3
    assert set(final.confirmed_gaps) == {
        audit.items[1].target_ref,
        audit.items[2].target_ref,
    }
    assert final.untested_targets == (audit.items[3].target_ref,)
    assert any("never probed" in warning for warning in final.warnings)
    assert len(final.queued_calibration) == 2

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        queued = database.query(
            "SELECT purpose, target_ref, rationale FROM calibration_queue_items "
            "WHERE purpose = 'audit-gap' ORDER BY target_ref"
        )
        audited = database.query(
            "SELECT count(*) FROM track_curriculum_progress WHERE state = 'audited'"
        )
    assert len(queued) == 2
    assert all("audit miss" in str(row[2]) for row in queued)
    assert int(audited[0][0]) >= 1


def test_an_audited_unit_is_marked_audited_without_granting_mastery(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1", "u2"], clock=polish_workspace.clock
    )
    audit = curriculum_service.audit_start(
        polish_workspace.paths, sample_size=3, clock=polish_workspace.clock
    )
    curriculum_service.audit_record(
        polish_workspace.paths,
        results=[{"target_ref": item.target_ref, "outcome": "correct"} for item in audit.items],
        clock=polish_workspace.clock,
    )
    curriculum_service.audit_finalize(polish_workspace.paths, clock=polish_workspace.clock)

    report = curriculum_service.show(polish_workspace.paths, clock=polish_workspace.clock)

    assert any(unit.state == "audited" for unit in report.units)
    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        stages = database.query("SELECT DISTINCT stage FROM track_item_state")
        summary = database.scalar(
            "SELECT evidence_summary_json FROM track_curriculum_progress "
            "WHERE state = 'audited' LIMIT 1"
        )
    assert stages == [("encountered",)]
    assert "confirmed" in str(summary)


def test_finalizing_twice_returns_the_same_result(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1"], clock=polish_workspace.clock
    )
    curriculum_service.audit_start(polish_workspace.paths, clock=polish_workspace.clock)
    first = curriculum_service.audit_finalize(polish_workspace.paths, clock=polish_workspace.clock)

    second = curriculum_service.audit_finalize(
        polish_workspace.paths, audit=first.audit_id, clock=polish_workspace.clock
    )

    assert second.finalized_at == first.finalized_at


def test_a_finalized_audit_takes_no_further_results(
    polish_workspace: PolishWorkspace,
) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1"], clock=polish_workspace.clock
    )
    audit = curriculum_service.audit_start(polish_workspace.paths, clock=polish_workspace.clock)
    curriculum_service.audit_finalize(polish_workspace.paths, clock=polish_workspace.clock)

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.audit_record(
            polish_workspace.paths,
            results=[{"target_ref": audit.items[0].target_ref, "outcome": "correct"}],
            audit=audit.audit_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "curriculum_audit_closed"


def test_an_unknown_curriculum_or_audit_is_named(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as curriculum:
        curriculum_service.show(
            polish_workspace.paths,
            curriculum="cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clock=polish_workspace.clock,
        )
    with pytest.raises(LinguaWikiError) as audit:
        curriculum_service.audit_report(
            polish_workspace.paths,
            audit="asm_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clock=polish_workspace.clock,
        )

    assert curriculum.value.payload.code == "curriculum_not_found"
    assert audit.value.payload.code == "curriculum_audit_not_found"


def test_a_sample_size_must_be_positive(polish_workspace: PolishWorkspace) -> None:
    _imported(polish_workspace)
    curriculum_service.position(
        polish_workspace.paths, completed=["u1"], clock=polish_workspace.clock
    )

    with pytest.raises(LinguaWikiError) as failure:
        curriculum_service.audit_start(
            polish_workspace.paths, sample_size=0, clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "invalid_arguments"
