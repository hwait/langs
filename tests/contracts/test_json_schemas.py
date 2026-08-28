from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from linguawiki.cli import run
from linguawiki.contract_validation import validate_json_contract
from linguawiki.contracts import ContentItem, SessionPackage, canonical_content_hash

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def validator(schema_name: str) -> Draft202012Validator:
    schema = read_json(SCHEMAS / f"{schema_name}.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def date_time_nodes(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        found = [value] if value.get("format") == "date-time" else []
        return found + [node for child in value.values() for node in date_time_nodes(child)]
    if isinstance(value, list):
        return [node for child in value for node in date_time_nodes(child)]
    return []


@pytest.mark.parametrize(
    ("schema_name", "fixture_path"),
    [
        ("lingua.content.v1", "language-packs/fixtures/inflected/content.json"),
        ("lingua.content.v1", "language-packs/fixtures/tonal/content.json"),
        ("lingua.session.v1", "tests/fixtures/session-package/package.json"),
    ],
)
def test_json_fixtures_validate_against_checked_in_schemas(
    schema_name: str, fixture_path: str
) -> None:
    validator(schema_name).validate(read_json(ROOT / fixture_path))


def test_workspace_and_lock_json_schemas() -> None:
    workspace = {
        "schema_name": "lingua.workspace.v1",
        "schema_version": 1,
        "workspace_id": "wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "created_at": "2026-01-01T00:00:00Z",
        "timezone": "Europe/Warsaw",
        "history_policy": "git-wiki",
        "backup_root": "/example/external-backup",
        "runtime": {"python": ">=3.12", "core_version": "0.1.0"},
    }
    lock = {
        "schema_name": "linguawiki.lock.v1",
        "schema_version": 1,
        "core": {"version": "0.1.0", "sha256": "0" * 64},
        "database_schema": {"version": "0", "sha256": "1" * 64},
        "skill_bundle": {"version": "0.1.0", "sha256": "2" * 64},
        "packs": [],
    }

    validator("lingua.workspace.v1").validate(workspace)
    validator("linguawiki.lock.v1").validate(lock)


def test_success_and_error_cli_envelopes_validate(capsys: object) -> None:
    assert run(["status", "--format", "json"]) == 0
    output = capsys.readouterr()  # type: ignore[attr-defined]
    validator("linguawiki.cli.success.v1").validate(json.loads(output.out))
    validator("linguawiki.cli.status.v1").validate(json.loads(output.out))

    assert run(["missing"]) == 2
    output = capsys.readouterr()  # type: ignore[attr-defined]
    validator("linguawiki.cli.error.v1").validate(json.loads(output.err))


def test_success_envelope_is_not_frozen_to_status_payload() -> None:
    future_command = {
        "schema_version": 1,
        "ok": True,
        "command": "future-command",
        "correlation_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generated_at": "2026-01-01T00:00:00Z",
        "data": {"future": "payload"},
    }

    validator("linguawiki.cli.success.v1").validate(future_command)
    with pytest.raises(JsonSchemaValidationError):
        validator("linguawiki.cli.status.v1").validate(future_command)


@pytest.mark.parametrize(
    ("schema_name", "fixture_path", "mutator", "model"),
    [
        (
            "lingua.content.v1",
            "language-packs/fixtures/inflected/content.json",
            lambda payload: payload.update(reviews=[]),
            ContentItem,
        ),
        (
            "lingua.content.v1",
            "language-packs/fixtures/inflected/content.json",
            lambda payload: payload["reviews"][0].update(reviewed_content_hash="f" * 64),
            ContentItem,
        ),
        (
            "lingua.session.v1",
            "tests/fixtures/session-package/package.json",
            lambda payload: payload["events"][0].update(occurred_at="2026-01-02T10:03:00Z"),
            SessionPackage,
        ),
        (
            "lingua.session.v1",
            "tests/fixtures/session-package/package.json",
            lambda payload: payload["events"][0]["payload"].update(utterance_id="utt_missing"),
            SessionPackage,
        ),
    ],
)
def test_canonical_json_validation_has_negative_parity_with_models(
    schema_name: str,
    fixture_path: str,
    mutator: object,
    model: object,
) -> None:
    payload = deepcopy(read_json(ROOT / fixture_path))
    mutator(payload)  # type: ignore[operator]

    with pytest.raises(PydanticValidationError):
        model.model_validate(payload)  # type: ignore[attr-defined]
    with pytest.raises((PydanticValidationError, JsonSchemaValidationError)):
        validate_json_contract(schema_name, payload, schema_directory=SCHEMAS)


def _passed_review_without_hash(payload: dict[str, object]) -> None:
    del payload["reviews"][0]["reviewed_content_hash"]  # type: ignore[index]


def _publication_missing_review_axes(payload: dict[str, object]) -> None:
    payload["lifecycle"] = "publication-ready"


def _high_risk_missing_source_alignment(payload: dict[str, object]) -> None:
    payload["risk_tier"] = 3
    payload["content_hash"] = canonical_content_hash(payload)
    for review in payload["reviews"]:  # type: ignore[union-attr]
        review["reviewed_content_hash"] = payload["content_hash"]


def _self_derivation(payload: dict[str, object]) -> None:
    payload["transcript_layers"][1]["derived_from"] = "normalized"  # type: ignore[index]


def _duplicate_layer(payload: dict[str, object]) -> None:
    payload["transcript_layers"].append(deepcopy(payload["transcript_layers"][0]))  # type: ignore[union-attr,index]


@pytest.mark.parametrize(
    ("schema_name", "fixture_path", "mutator"),
    [
        (
            "lingua.content.v1",
            "language-packs/fixtures/inflected/content.json",
            _passed_review_without_hash,
        ),
        (
            "lingua.content.v1",
            "language-packs/fixtures/inflected/content.json",
            _publication_missing_review_axes,
        ),
        (
            "lingua.content.v1",
            "language-packs/fixtures/inflected/content.json",
            _high_risk_missing_source_alignment,
        ),
        ("lingua.session.v1", "tests/fixtures/session-package/package.json", _self_derivation),
        ("lingua.session.v1", "tests/fixtures/session-package/package.json", _duplicate_layer),
    ],
)
def test_schema_only_rejects_expressible_semantic_failures(
    schema_name: str, fixture_path: str, mutator: object
) -> None:
    payload = deepcopy(read_json(ROOT / fixture_path))
    mutator(payload)  # type: ignore[operator]

    with pytest.raises(JsonSchemaValidationError):
        validator(schema_name).validate(payload)


@pytest.mark.parametrize("invalid_timestamp", ["not-a-date", "2026-01-02T10:00:00+04:00"])
def test_session_schema_rejects_invalid_or_non_utc_datetimes(invalid_timestamp: str) -> None:
    payload = read_json(ROOT / "tests/fixtures/session-package/package.json")
    payload["started_at"] = invalid_timestamp

    with pytest.raises(JsonSchemaValidationError):
        validator("lingua.session.v1").validate(payload)


def test_every_published_date_time_is_structurally_restricted_to_utc() -> None:
    nodes = [
        node
        for schema_path in SCHEMAS.glob("*.json")
        for node in date_time_nodes(read_json(schema_path))
    ]

    assert nodes
    assert all(node.get("pattern") == r"(?:Z|\+00:00)$" for node in nodes)
