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


# --- Model output must satisfy its own published schema -----------------------------


def staged_event(kind: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "kind": kind,
        "occurred_at": "2026-01-01T09:00:00Z",
        "payload": payload,
    }


#: One of every staged event kind, each with only its required fields, so the defaults
#: are the values under test. The defect this guards was exactly a default: a nullable
#: vocabulary field dumped `null`, which its own published schema rejected.
MINIMAL_EVENTS: tuple[dict[str, object], ...] = (
    staged_event(
        "attempt.observed",
        {"task_type": "objective", "modality": "text", "score": 1.0, "dimension": "reading"},
    ),
    staged_event(
        "correction.given",
        {
            "category": "case-government",
            "signature": "szukam bilet",
            "description": "Accusative where the verb governs the genitive.",
        },
    ),
    staged_event(
        "pronunciation.assessment", {"status": "uncertain", "note": "Nasal vowel flattened."}
    ),
    staged_event("observation.noted", {"category": "fatigue", "note": "Flagging near the end."}),
    staged_event("follow_up", {"kind": "practice", "action": "Drill the genitive again."}),
)


@pytest.mark.parametrize("event", MINIMAL_EVENTS, ids=lambda event: str(event["kind"]))
def test_a_batch_the_model_accepts_satisfies_its_published_schema(
    event: dict[str, object],
) -> None:
    """Runtime validation and the published schema must agree in both directions.

    They disagreed: `response_visibility` is `str | None`, and the vocabulary was
    published as a sibling `type: string` + `enum` on a field whose own type was
    `anyOf: [string, null]`. JSON Schema reads siblings conjunctively, so `null` --
    the field's *default* -- satisfied the union and failed the constraints beside it,
    and a model's own valid output did not validate against its own contract.
    """

    from linguawiki.contracts import SessionEventBatch

    batch = SessionEventBatch.model_validate(
        {"sequence": 1, "idempotency_key": "parity", "events": [event]}
    )

    validator("lingua.session.events.v1").validate(batch.model_dump(mode="json"))


def test_a_fully_populated_batch_satisfies_its_published_schema() -> None:
    """The defaults are one half; a payload that sets every optional field is the other."""

    from linguawiki.contracts import SessionEventBatch

    batch = SessionEventBatch.model_validate(
        {
            "sequence": 2,
            "idempotency_key": "parity-full",
            "block": "blk_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "events": [
                {
                    "event_id": "evt_01ARZ3NDEKTSV4RRFFQ69G5FAW",
                    "kind": "attempt.observed",
                    "occurred_at": "2026-01-01T09:00:00Z",
                    "activity": "act_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "payload": {
                        "task_type": "extended-productive",
                        "modality": "writing",
                        "score": 0.75,
                        "target": "pl.lex.dworzec",
                        "dimension": "writing",
                        "claims": ["controlled-production"],
                        "help_level": "hinted",
                        "correction_mode": "immediate",
                        "retrieval": "delayed",
                        "delay_hours": 48.0,
                        "latency_ms": 4200,
                        "context": "essay:one",
                        "response": "Jestem na dworcu.",
                        "response_visibility": "excerpt",
                        "response_hash": "a" * 64,
                        "assessor_kind": "ai",
                        "assessor": "claude",
                        "confidence": "low",
                    },
                }
            ],
        }
    )

    validator("lingua.session.events.v1").validate(batch.model_dump(mode="json"))


def test_every_published_vocabulary_is_enforced_at_runtime_and_in_the_schema() -> None:
    """Both readers see the same declaration, so neither can accept what the other refuses.

    Walks the annotations rather than a hand-written list, so a field added later is
    covered without anyone remembering to cover it.
    """

    from linguawiki.contracts import STAGED_PAYLOAD_KINDS, field_vocabulary

    schema = read_json(SCHEMAS / "lingua.session.events.v1.json")
    checked = 0
    for kind, model in STAGED_PAYLOAD_KINDS.items():
        definition = schema["$defs"][model.__name__]
        for name, field in model.model_fields.items():
            permitted = field_vocabulary(field)
            if permitted is None:
                continue
            checked += 1
            published = definition["properties"][name]  # type: ignore[index]
            # Wherever the enum sits -- the property, a nullable branch, or an array's
            # items -- it must be there, and it must be these values.
            assert json.dumps(list(permitted)) in json.dumps(published), (
                f"{model.__name__}.{name} does not publish its vocabulary: {published}"
            )
            with pytest.raises(PydanticValidationError) as failure:
                model.model_validate(
                    {
                        **MINIMAL_PAYLOADS[kind],
                        name: ["nonsense"] if name == "claims" else "nonsense",
                    }
                )
            assert "nonsense" in str(failure.value)
            assert "not a known" in str(failure.value)
    assert checked >= 16, f"only {checked} vocabularies were checked"


MINIMAL_PAYLOADS: dict[str, dict[str, object]] = {
    "attempt.observed": {
        "task_type": "objective",
        "modality": "text",
        "score": 1.0,
        "dimension": "reading",
    },
    "correction.given": {
        "category": "case-government",
        "signature": "szukam bilet",
        "description": "Accusative where the verb governs the genitive.",
    },
    "pronunciation.assessment": {"status": "uncertain", "note": "Flattened."},
    "source.progress": {"source_ref": "Polski Daily", "band": "gist"},
    "observation.noted": {"category": "fatigue", "note": "Tired."},
    "follow_up": {"kind": "practice", "action": "Drill it."},
}
