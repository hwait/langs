"""Stable application errors used by CLI JSON responses."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import Field, ValidationError

from linguawiki.models import ContractModel


class ErrorDetail(ContractModel):
    field: str | None = None
    reason: str
    context: dict[str, Any] = Field(default_factory=dict)


class ErrorPayload(ContractModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    message: str
    retryable: bool = False
    details: tuple[ErrorDetail, ...] = ()


class LinguaWikiError(Exception):
    """Expected domain failure safe to expose through the CLI envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Sequence[ErrorDetail] = (),
    ) -> None:
        super().__init__(message)
        self.payload = ErrorPayload(
            code=code,
            message=message,
            retryable=retryable,
            details=tuple(details),
        )


def validated_contract(model: type[Any], payload: Any, *, code: str, subject: str) -> Any:
    """Validate an incoming payload and refuse it by name, with the field that failed.

    A raw `ValidationError` escaping to the CLI became a generic envelope that said the
    *output* contract had failed -- which is the opposite of what happened, and told a
    skill nothing about which part of its batch was wrong.
    """

    try:
        return model.model_validate(payload)
    except ValidationError as failure:
        details = tuple(
            ErrorDetail(
                field=".".join(str(part) for part in item["loc"]) or subject,
                reason=str(item["msg"]),
            )
            for item in failure.errors()[:10]
        )
        first = failure.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or subject
        raise LinguaWikiError(
            code,
            f"this {subject} does not satisfy its contract: {field} {first['msg'].lower()}",
            details=details,
        ) from failure
