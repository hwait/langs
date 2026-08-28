"""Canonical structural and semantic validation for published JSON contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel

from linguawiki.contracts import SCHEMA_MODELS


def validate_json_contract(
    schema_name: str,
    payload: dict[str, Any],
    *,
    schema_directory: Path,
) -> BaseModel:
    """Validate the published structure, then all Pydantic semantic invariants."""

    try:
        model = SCHEMA_MODELS[schema_name]
    except KeyError as exc:
        raise ValueError(f"unknown contract schema: {schema_name}") from exc
    schema_path = schema_directory / f"{schema_name}.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if schema.get("x-linguawiki-semantic-model") != schema_name:
        raise ValueError(f"schema semantic model mismatch: {schema_name}")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    return model.model_validate(payload)
