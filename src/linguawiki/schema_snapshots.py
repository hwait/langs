"""Deterministic rendering for checked-in contract schemas."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from linguawiki.contracts import SCHEMA_MODELS


def _contains_review(axis: str) -> dict[str, Any]:
    return {
        "contains": {
            "type": "object",
            "required": ["axis", "state"],
            "properties": {"axis": {"const": axis}, "state": {"const": "passed"}},
        },
        "minContains": 1,
    }


def _harden_content_schema(schema: dict[str, Any]) -> None:
    review = schema["$defs"]["ContentReview"]
    review.setdefault("allOf", []).append(
        {
            "if": {"properties": {"state": {"enum": ["passed", "failed"]}}},
            "then": {
                "properties": {
                    "reviewed_content_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "method": {"type": "string", "minLength": 1},
                },
                "required": ["reviewed_content_hash", "method"],
            },
        }
    )
    promoted_axes = [_contains_review("linguistic"), _contains_review("pedagogical")]
    publication_axes = [
        _contains_review(axis) for axis in ("source-alignment", "rights", "privacy")
    ]
    schema.setdefault("allOf", []).extend(
        [
            {
                "if": {
                    "properties": {
                        "lifecycle": {
                            "enum": ["approved-personal", "verified", "publication-ready"]
                        }
                    }
                },
                "then": {"properties": {"reviews": {"allOf": promoted_axes}}},
            },
            {
                "if": {"properties": {"lifecycle": {"const": "publication-ready"}}},
                "then": {"properties": {"reviews": {"allOf": publication_axes}}},
            },
            {
                "if": {
                    "properties": {
                        "lifecycle": {
                            "enum": ["approved-personal", "verified", "publication-ready"]
                        },
                        "risk_tier": {"minimum": 3},
                    },
                    "required": ["lifecycle", "risk_tier"],
                },
                "then": {
                    "properties": {"reviews": {"allOf": [_contains_review("source-alignment")]}}
                },
            },
        ]
    )


def _harden_session_schema(schema: dict[str, Any]) -> None:
    layer = schema["$defs"]["TranscriptLayer"]
    layer.setdefault("allOf", []).extend(
        [
            {
                "if": {"properties": {"kind": {"const": "raw"}}},
                "then": {"properties": {"derived_from": {"type": "null"}}},
            },
            {
                "if": {"properties": {"kind": {"const": "normalized"}}},
                "then": {"properties": {"derived_from": {"const": "raw"}}},
            },
            {
                "if": {"properties": {"kind": {"const": "reviewed-hearing"}}},
                "then": {"properties": {"derived_from": {"enum": ["raw", "normalized"]}}},
            },
        ]
    )
    layers = schema["properties"]["transcript_layers"]
    layers["minItems"] = 1
    layers.setdefault("allOf", []).extend(
        {
            "contains": {"type": "object", "properties": {"kind": {"const": kind}}},
            "minContains": minimum,
            "maxContains": 1,
        }
        for kind, minimum in (("raw", 1), ("normalized", 0), ("reviewed-hearing", 0))
    )


def _require_utc_datetimes(node: Any) -> None:
    """Apply the wire-level UTC rule to every date-time in a generated schema."""

    if isinstance(node, dict):
        if node.get("format") == "date-time":
            node["pattern"] = r"(?:Z|\+00:00)$"
        for value in node.values():
            _require_utc_datetimes(value)
    elif isinstance(node, list):
        for value in node:
            _require_utc_datetimes(value)


def hardened_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(schema)
    result["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    result["x-linguawiki-semantic-model"] = name
    _require_utc_datetimes(result)
    if name == "lingua.content.v1":
        _harden_content_schema(result)
    elif name == "lingua.session.v1":
        _harden_session_schema(result)
    return result


def rendered_schemas(schema_directory: Path) -> dict[Path, str]:
    return {
        schema_directory / f"{name}.json": json.dumps(
            hardened_schema(name, model.model_json_schema(mode="validation")),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        for name, model in SCHEMA_MODELS.items()
    }
