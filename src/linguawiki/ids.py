"""Sortable opaque identifiers with domain-specific prefixes."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Iterable
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
    OBSERVATION = "obs"
    FOLLOWUP = "fup"
    ESTIMATE = "est"
    REVIEW = "rev"
    ASSESSMENT = "asm"
    ANKI_NOTE = "ank"
    EVENT = "evt"
    ARTIFACT = "art"
    # Stage 4: the session engine's own rows. A batch, one staged event, the record that
    # a close happened, and one ingestion of an externally produced package.
    # Stage 5: the transcript layers and what they support.
    UTTERANCE = "utt"
    REVISION = "trv"
    INTERPRETATION = "int"
    PRONUNCIATION = "prn"
    BATCH = "bat"
    STAGED_EVENT = "sev"
    FINALIZATION = "fin"
    INGESTION = "ing"


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


def derive_id(prefix: IdPrefix, *parts: str) -> str:
    """Create a deterministic identifier from a namespace path.

    Pack content is content-addressed, so reinstalling the same pack version must
    produce the same identifiers or every learner annotation would be orphaned by a
    reinstall. The digest of the joined parts is encoded in the same alphabet as a
    generated ULID, so a derived ID is indistinguishable from a random one at the type
    boundary and needs no separate validation rule. Parts are NUL-separated, so
    ('a', 'bc') and ('ab', 'c') cannot collide.
    """

    if not parts or any(not part for part in parts):
        raise ValueError("derive_id requires at least one non-empty part")
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).digest()
    # 26 Crockford characters hold 130 bits; take the leading 130 of the digest.
    value = int.from_bytes(digest[:17], "big") >> 6
    return f"{prefix}_{_encode_ulid(value)}"


def derive_ids(prefix: IdPrefix, namespaces: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """Derive several identifiers from their namespace paths."""

    return tuple(derive_id(prefix, *namespace) for namespace in namespaces)


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
    def derive(cls, *parts: str) -> Self:
        """Derive this ID deterministically from a namespace path."""

        return cls(derive_id(cls.prefix, *parts))

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


class ObservationId(OpaqueId):
    prefix = IdPrefix.OBSERVATION


class FollowUpId(OpaqueId):
    prefix = IdPrefix.FOLLOWUP


class EstimateId(OpaqueId):
    prefix = IdPrefix.ESTIMATE


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


class UtteranceId(OpaqueId):
    prefix = IdPrefix.UTTERANCE


class RevisionId(OpaqueId):
    prefix = IdPrefix.REVISION


class InterpretationId(OpaqueId):
    prefix = IdPrefix.INTERPRETATION


class PronunciationId(OpaqueId):
    prefix = IdPrefix.PRONUNCIATION


class BatchId(OpaqueId):
    prefix = IdPrefix.BATCH


class StagedEventId(OpaqueId):
    prefix = IdPrefix.STAGED_EVENT


class FinalizationId(OpaqueId):
    prefix = IdPrefix.FINALIZATION


class IngestionId(OpaqueId):
    prefix = IdPrefix.INGESTION
