"""Sortable opaque identifiers with domain-specific prefixes."""

from __future__ import annotations

import re
import secrets
import time
from enum import StrEnum
from typing import Any, ClassVar, Self

from pydantic_core import CoreSchema, core_schema

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class IdPrefix(StrEnum):
    WORKSPACE = "wsp"
    USER = "usr"
    TRACK = "trk"
    PACK = "pak"
    CONTENT = "cnt"
    SOURCE = "src"
    SESSION = "ses"
    BLOCK = "blk"
    ACTIVITY = "act"
    ATTEMPT = "att"
    EVIDENCE = "evd"
    ERROR = "err"
    REVIEW = "rev"
    ASSESSMENT = "asm"
    ANKI_NOTE = "ank"
    EVENT = "evt"
    ARTIFACT = "art"


def _encode_ulid(value: int) -> str:
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(encoded)


def new_id(prefix: IdPrefix, *, timestamp_ms: int | None = None) -> str:
    """Create a timestamp-sortable opaque identifier."""

    current_ms = int(time.time_ns() // 1_000_000) if timestamp_ms is None else timestamp_ms
    if not 0 <= current_ms < 2**48:
        raise ValueError("timestamp_ms must fit in 48 bits")
    payload = (current_ms << 80) | secrets.randbits(80)
    return f"{prefix}_{_encode_ulid(payload)}"


def validate_id(value: str, prefix: IdPrefix) -> str:
    """Validate an identifier for an expected domain prefix."""

    marker = f"{prefix}_"
    if not value.startswith(marker) or not _ULID_PATTERN.fullmatch(value[len(marker) :]):
        raise ValueError(f"expected {prefix}_ followed by a 26-character ULID")
    return value


class OpaqueId(str):
    """Pydantic-compatible base for distinct domain ID types."""

    prefix: ClassVar[IdPrefix]

    def __new__(cls, value: str) -> Self:
        return str.__new__(cls, validate_id(value, cls.prefix))

    @classmethod
    def new(cls) -> Self:
        return cls(new_id(cls.prefix))

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type: Any, _handler: Any) -> CoreSchema:
        pattern = rf"^{cls.prefix}_[0-9A-HJKMNP-TV-Z]{{26}}$"
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(pattern=pattern)
        )


class WorkspaceId(OpaqueId):
    prefix = IdPrefix.WORKSPACE


class UserId(OpaqueId):
    prefix = IdPrefix.USER


class TrackId(OpaqueId):
    prefix = IdPrefix.TRACK


class PackId(OpaqueId):
    prefix = IdPrefix.PACK


class ContentId(OpaqueId):
    prefix = IdPrefix.CONTENT


class SourceId(OpaqueId):
    prefix = IdPrefix.SOURCE


class SessionId(OpaqueId):
    prefix = IdPrefix.SESSION


class BlockId(OpaqueId):
    prefix = IdPrefix.BLOCK


class ActivityId(OpaqueId):
    prefix = IdPrefix.ACTIVITY


class AttemptId(OpaqueId):
    prefix = IdPrefix.ATTEMPT


class EvidenceId(OpaqueId):
    prefix = IdPrefix.EVIDENCE


class ErrorId(OpaqueId):
    prefix = IdPrefix.ERROR


class ReviewId(OpaqueId):
    prefix = IdPrefix.REVIEW


class AssessmentId(OpaqueId):
    prefix = IdPrefix.ASSESSMENT


class AnkiNoteId(OpaqueId):
    prefix = IdPrefix.ANKI_NOTE


class EventId(OpaqueId):
    prefix = IdPrefix.EVENT


class ArtifactId(OpaqueId):
    prefix = IdPrefix.ARTIFACT
