"""Stable application errors used by CLI JSON responses."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import Field

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
