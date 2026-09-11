"""The published pack schemas must accept the packs that ship, and reject the rest.

Structural JSON Schema validation and Pydantic semantic validation are checked for
*parity* here: a payload the models reject must not slip through the published schema
without one of the two saying so.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from linguawiki.contract_validation import validate_json_contract
from linguawiki.contracts import (
    DELIVERY_STAGE,
    SCHEMA_MODELS,
    PackManifest,
    StatusData,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
PACKS = (
    ROOT / "language-packs" / "pl-pilot",
    ROOT / "language-packs" / "fixtures" / "inflected",
    ROOT / "language-packs" / "fixtures" / "tonal",
)
#: Which published schema validates each pack file, keyed by its declared path prefix.
FILE_CONTRACTS = (
    ("capabilities.json", "lingua.pack.capabilities.v1"),
    ("source-policy.json", "lingua.pack.source-policy.v1"),
    ("proficiency/", "lingua.pack.proficiency.v1"),
    ("assessments/", "lingua.pack.assessment.v1"),
    ("activities/", "lingua.pack.activities.v1"),
    ("references/", "lingua.pack.references.v1"),
    ("resource-bundles/", "lingua.pack.bundle.v1"),
    ("tests/expectations.json", "lingua.pack.expectations.v1"),
)
JSONL_CONTRACTS = (
    ("seed/knowledge.jsonl", "lingua.pack.knowledge.v1"),
    ("seed/relations.jsonl", "lingua.pack.relation.v1"),
    ("seed/examples.jsonl", "lingua.pack.example.v1"),
)


def validator(schema_name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{schema_name}.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("pack", PACKS, ids=lambda path: path.name)
def test_every_shipped_manifest_validates_against_the_published_schema(pack: Path) -> None:
    payload = read_json(pack / "manifest.json")

    validate_json_contract("lingua.pack.v1", payload, schema_directory=SCHEMAS)


@pytest.mark.parametrize("pack", PACKS, ids=lambda path: path.name)
def test_every_declared_pack_file_validates_against_its_own_contract(pack: Path) -> None:
    """Role assignment is by path, so the path decides which contract applies."""

    manifest = PackManifest.model_validate(read_json(pack / "manifest.json"))
    checked = 0
    for relative in sorted(manifest.files):
        contract = next(
            (name for prefix, name in FILE_CONTRACTS if relative.startswith(prefix)), None
        )
        if contract is None:
            continue
        validate_json_contract(contract, read_json(pack / relative), schema_directory=SCHEMAS)
        checked += 1
    assert checked >= len(FILE_CONTRACTS) - 1


@pytest.mark.parametrize("pack", PACKS, ids=lambda path: path.name)
def test_every_seed_line_validates_against_its_line_contract(pack: Path) -> None:
    manifest = PackManifest.model_validate(read_json(pack / "manifest.json"))
    lines = 0
    for relative, contract in JSONL_CONTRACTS:
        if relative not in manifest.files:
            continue
        for line in (pack / relative).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            validate_json_contract(contract, json.loads(line), schema_directory=SCHEMAS)
            lines += 1
    assert lines > 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda payload: payload.update(maturity="excellent"), "an invented maturity"),
        (lambda payload: payload.update(bands=["Z9"]), "a band no framework has"),
        (lambda payload: payload.update(dimension_kinds={}), "unclassified dimensions"),
        (lambda payload: payload.update(dimensions=["reading", "reading"]), "duplicate dimensions"),
        (lambda payload: payload.update(frameworks=[]), "no framework at all"),
        (lambda payload: payload.update(version="0.1"), "a malformed version"),
        (lambda payload: payload.update(pack_key="Pack Key"), "a malformed pack key"),
        (
            lambda payload: payload["files"].update({"../escaped.json": "a" * 64}),
            "a file reference escaping the pack",
        ),
        (lambda payload: payload["files"].update({"x.json": "not-a-hash"}), "a malformed digest"),
        (lambda payload: payload.update(maintainers=[]), "no maintainer"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_malformed_manifest_is_refused(mutate: Any, reason: str) -> None:
    payload = deepcopy(read_json(PACKS[0] / "manifest.json"))
    mutate(payload)

    with pytest.raises(PydanticValidationError):
        PackManifest.model_validate(payload)
    with pytest.raises((PydanticValidationError, JsonSchemaValidationError)):
        validate_json_contract("lingua.pack.v1", payload, schema_directory=SCHEMAS)


def test_a_bundle_may_not_depend_on_an_undeclared_bundle_or_itself() -> None:
    payload = deepcopy(read_json(PACKS[0] / "manifest.json"))
    payload["bundles"][0]["depends_on"] = ["cefr-b1-core"]

    with pytest.raises(PydanticValidationError):
        PackManifest.model_validate(payload)

    payload = deepcopy(read_json(PACKS[0] / "manifest.json"))
    payload["bundles"][1]["depends_on"] = [payload["bundles"][1]["bundle_key"]]

    with pytest.raises(PydanticValidationError):
        PackManifest.model_validate(payload)


def test_an_origin_derived_from_a_source_must_identify_it() -> None:
    from linguawiki.contracts import PackItemOrigin

    with pytest.raises(PydanticValidationError):
        PackItemOrigin(origin_class="ai-adapted", rights="r", privacy="public")
    with pytest.raises(PydanticValidationError):
        PackItemOrigin(
            origin_class="authentic-source", reference="a book", rights="r", privacy="public"
        )
    # An original item needs no reference at all.
    PackItemOrigin(origin_class="human-authored", rights="r", privacy="public")


def test_an_item_must_declare_exactly_one_origin_and_review_source() -> None:
    from linguawiki.contracts import PackItemProvenance

    base = {"lifecycle": "verified", "risk_tier": 2, "content_hash": "a" * 64}

    with pytest.raises(PydanticValidationError):
        PackItemProvenance.model_validate(base)
    with pytest.raises(PydanticValidationError):
        PackItemProvenance.model_validate(
            {
                **base,
                "origin_profile": "authored",
                "review_profile": "verified",
                "origins": [{"origin_class": "human-authored", "rights": "r", "privacy": "public"}],
            }
        )
    PackItemProvenance.model_validate(
        {**base, "origin_profile": "authored", "review_profile": "verified"}
    )


def test_a_scored_task_must_declare_how_it_is_scored() -> None:
    from linguawiki.contracts import PackAssessmentTask

    common = {
        "stable_key": "pl.task.x",
        "dimension": "reading",
        "level": "A2",
        "difficulty": 1.0,
        "content_family": "travel",
        "modality": "text",
        "prompt": "A prompt",
        "provenance": {
            "origin_profile": "authored",
            "review_profile": "verified",
            "lifecycle": "verified",
            "risk_tier": 2,
            "content_hash": "a" * 64,
        },
    }

    with pytest.raises(PydanticValidationError):
        PackAssessmentTask.model_validate({**common, "task_type": "objective"})
    with pytest.raises(PydanticValidationError):
        PackAssessmentTask.model_validate({**common, "task_type": "extended-productive"})
    PackAssessmentTask.model_validate(
        {**common, "task_type": "objective", "expected": {"answers": ["yes"]}}
    )
    PackAssessmentTask.model_validate(
        {**common, "task_type": "extended-productive", "rubric": {"version": 1}}
    )


def test_a_relation_names_exactly_one_target() -> None:
    from linguawiki.contracts import PackRelation

    with pytest.raises(PydanticValidationError):
        PackRelation.model_validate({"source_key": "pl.a", "relation_type": "related"})
    with pytest.raises(PydanticValidationError):
        PackRelation.model_validate(
            {
                "source_key": "pl.a",
                "relation_type": "related",
                "target_key": "pl.b",
                "target_ref": "external",
            }
        )
    PackRelation.model_validate(
        {"source_key": "pl.a", "relation_type": "related", "target_key": "pl.b"}
    )


def test_a_pack_may_not_claim_and_refuse_the_same_maturity() -> None:
    from linguawiki.contracts import PackExpectations

    with pytest.raises(PydanticValidationError):
        PackExpectations.model_validate({"maturity": "pilot", "refuses_maturity": ["pilot"]})
    with pytest.raises(PydanticValidationError):
        PackExpectations.model_validate({"maturity": "pilot", "minimum_counts": {"knowledge": -1}})


def test_source_policy_ranks_are_a_total_order() -> None:
    from linguawiki.contracts import PackSourcePolicy

    duplicate_rank = {
        "source_classes": [
            {"source_class": "a", "rank": 1, "description": "a"},
            {"source_class": "b", "rank": 1, "description": "b"},
        ]
    }
    duplicate_name = {
        "source_classes": [
            {"source_class": "a", "rank": 1, "description": "a"},
            {"source_class": "a", "rank": 2, "description": "b"},
        ]
    }

    with pytest.raises(PydanticValidationError):
        PackSourcePolicy.model_validate(duplicate_rank)
    with pytest.raises(PydanticValidationError):
        PackSourcePolicy.model_validate(duplicate_name)


def test_every_registered_schema_has_a_checked_in_snapshot() -> None:
    for name in SCHEMA_MODELS:
        assert (SCHEMAS / f"{name}.json").is_file(), name


def test_the_delivery_stage_constant_matches_the_wire_literal() -> None:
    """Scripts read the constant; the envelope carries the literal. They must agree."""

    annotation = StatusData.model_fields["stage"].annotation
    assert annotation is not None
    assert annotation.__args__ == (DELIVERY_STAGE,)  # type: ignore[attr-defined]
