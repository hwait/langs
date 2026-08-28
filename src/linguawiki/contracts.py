"""Versioned machine contracts for workspaces, content, sessions, and CLI output."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from linguawiki.clock import require_utc, validate_iana_timezone
from linguawiki.errors import ErrorPayload
from linguawiki.ids import (
    ArtifactId,
    ContentId,
    EventId,
    PackId,
    SessionId,
    TrackId,
    WorkspaceId,
)
from linguawiki.models import ContractModel

CONTRACT_SCHEMA_VERSION = 1
SHA256_PATTERN = r"^[a-f0-9]{64}$"
LANGUAGE_TAG_PATTERN = r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
CONTENT_HASH_FIELDS = (
    "schema_name",
    "schema_version",
    "content_id",
    "language",
    "kind",
    "title",
    "body",
    "risk_tier",
    "provenance",
    "dependencies",
)


def canonical_content_hash(payload: Mapping[str, Any]) -> str:
    """Hash LinguaWiki Canonical Content JSON v1 from a wire-shaped mapping."""

    canonical_fields = {field: payload[field] for field in CONTENT_HASH_FIELDS}
    canonical_bytes = json.dumps(
        canonical_fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def _artifact_references(value: Any) -> tuple[ArtifactId, ...]:
    """Find typed artifact references recursively in a validated event payload."""

    if isinstance(value, ArtifactId):
        return (value,)
    if isinstance(value, BaseModel):
        return tuple(
            reference
            for field_name in type(value).model_fields
            for reference in _artifact_references(getattr(value, field_name))
        )
    if isinstance(value, Mapping):
        return tuple(
            reference for item in value.values() for reference in _artifact_references(item)
        )
    if isinstance(value, tuple | list | set | frozenset):
        return tuple(reference for item in value for reference in _artifact_references(item))
    return ()


class VersionPin(ContractModel):
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


class PackPin(VersionPin):
    pack_id: PackId


class LockManifest(ContractModel):
    schema_name: Literal["linguawiki.lock.v1"] = "linguawiki.lock.v1"
    schema_version: Literal[1] = 1
    core: VersionPin
    database_schema: VersionPin
    skill_bundle: VersionPin
    packs: tuple[PackPin, ...] = ()


class WorkspaceRuntime(ContractModel):
    python: str = Field(pattern=r"^>=3\.12(?:,[^\s]+)?$")
    core_version: str = Field(min_length=1)


class WorkspaceManifest(ContractModel):
    schema_name: Literal["lingua.workspace.v1"] = "lingua.workspace.v1"
    schema_version: Literal[1] = 1
    workspace_id: WorkspaceId
    created_at: datetime
    timezone: str
    history_policy: Literal["git-wiki"] = "git-wiki"
    backup_root: str = Field(min_length=1)
    runtime: WorkspaceRuntime

    _created_at_utc = field_validator("created_at")(require_utc)
    _valid_timezone = field_validator("timezone")(validate_iana_timezone)


class ContentOrigin(StrEnum):
    AUTHENTIC = "authentic"
    HUMAN_AUTHORED = "human-authored"
    AI_ADAPTED = "ai-adapted"
    AI_GENERATED = "ai-generated"
    LEARNER_PRODUCED = "learner-produced"
    SOURCE_DERIVED = "source-derived"
    SYNTHETIC = "synthetic"


class ReviewAxis(StrEnum):
    LINGUISTIC = "linguistic"
    PEDAGOGICAL = "pedagogical"
    SOURCE_ALIGNMENT = "source-alignment"
    RIGHTS = "rights"
    PRIVACY = "privacy"


class ReviewState(StrEnum):
    NOT_REQUIRED = "not-required"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ContentReview(ContractModel):
    axis: ReviewAxis
    state: ReviewState
    reviewed_content_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    method: str | None = None

    @model_validator(mode="after")
    def completed_review_has_revision_and_method(self) -> ContentReview:
        if self.state in {ReviewState.PASSED, ReviewState.FAILED} and (
            self.reviewed_content_hash is None or not self.method
        ):
            raise ValueError("completed review requires reviewed_content_hash and method")
        return self


class ContentProvenance(ContractModel):
    origin: ContentOrigin
    source_references: tuple[str, ...] = ()
    generation_run_id: str | None = None
    rights: str
    privacy: Literal["public", "private", "synthetic"]


class ContentItem(ContractModel):
    schema_name: Literal["lingua.content.v1"] = "lingua.content.v1"
    schema_version: Literal[1] = 1
    content_id: ContentId
    language: str = Field(pattern=LANGUAGE_TAG_PATTERN)
    kind: Literal[
        "concept",
        "lexeme",
        "sense",
        "form",
        "construction",
        "grammar",
        "pronunciation",
        "character",
        "pragmatics",
        "culture",
        "skill_strategy",
    ]
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    lifecycle: Literal["needs-review", "approved-personal", "verified", "publication-ready"]
    risk_tier: int = Field(ge=0, le=4)
    provenance: ContentProvenance
    reviews: tuple[ContentReview, ...]
    dependencies: tuple[ContentId, ...] = ()

    @model_validator(mode="after")
    def reviews_are_independent(self) -> ContentItem:
        expected_hash = canonical_content_hash(self.model_dump(mode="json"))
        if self.content_hash != expected_hash:
            raise ValueError("content_hash does not match canonical content JSON v1")
        axes = [review.axis for review in self.reviews]
        if len(axes) != len(set(axes)):
            raise ValueError("review axes must be unique")
        completed = [
            review
            for review in self.reviews
            if review.state in {ReviewState.PASSED, ReviewState.FAILED}
        ]
        if any(review.reviewed_content_hash != self.content_hash for review in completed):
            raise ValueError("every completed review must bind to the current content_hash")
        if self.lifecycle != "needs-review" and any(
            review.state in {ReviewState.PENDING, ReviewState.FAILED} for review in self.reviews
        ):
            raise ValueError("promoted content cannot have pending or failed reviews")
        passed_axes = {review.axis for review in self.reviews if review.state == ReviewState.PASSED}
        required_axes: set[str] = set()
        if self.lifecycle in {"approved-personal", "verified", "publication-ready"}:
            required_axes.update({ReviewAxis.LINGUISTIC, ReviewAxis.PEDAGOGICAL})
        if self.lifecycle == "publication-ready":
            required_axes.update(ReviewAxis)
        if self.lifecycle != "needs-review" and self.risk_tier >= 3:
            required_axes.add(ReviewAxis.SOURCE_ALIGNMENT)
        missing = required_axes - passed_axes
        if missing:
            raise ValueError(f"{self.lifecycle} content lacks passed reviews: {sorted(missing)}")
        return self


class ArtifactKind(StrEnum):
    AUDIO = "audio"
    TRANSCRIPT = "transcript"
    OTHER = "other"


class SessionArtifact(ContractModel):
    artifact_id: ArtifactId
    kind: ArtifactKind
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    retained: bool

    @field_validator("relative_path")
    @classmethod
    def path_must_be_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact path must be a safe relative POSIX path")
        return value


class SpeakerRole(StrEnum):
    LEARNER = "learner"
    TUTOR = "tutor"
    OTHER = "other"


class TranscriptUtterance(ContractModel):
    utterance_id: str = Field(pattern=r"^utt_[A-Za-z0-9_-]+$")
    speaker: SpeakerRole
    started_at: datetime
    ended_at: datetime
    text: str

    _started_at_utc = field_validator("started_at")(require_utc)
    _ended_at_utc = field_validator("ended_at")(require_utc)

    @model_validator(mode="after")
    def chronological(self) -> TranscriptUtterance:
        if self.ended_at < self.started_at:
            raise ValueError("utterance ended_at precedes started_at")
        return self


class TranscriptLayer(ContractModel):
    kind: Literal["raw", "normalized", "reviewed-hearing"]
    derived_from: Literal["raw", "normalized"] | None = None
    utterances: tuple[TranscriptUtterance, ...]

    @model_validator(mode="after")
    def valid_derivation(self) -> TranscriptLayer:
        if self.kind == "raw" and self.derived_from is not None:
            raise ValueError("raw transcript cannot be derived from another layer")
        if self.kind != "raw" and self.derived_from is None:
            raise ValueError("derived transcript layers must name their source layer")
        if self.derived_from == self.kind:
            raise ValueError("transcript layer cannot derive from itself")
        utterance_ids = [utterance.utterance_id for utterance in self.utterances]
        if len(utterance_ids) != len(set(utterance_ids)):
            raise ValueError("utterance IDs must be unique within a transcript layer")
        if any(
            current.started_at < previous.started_at
            for previous, current in zip(self.utterances, self.utterances[1:], strict=False)
        ):
            raise ValueError("utterances must be ordered by started_at")
        return self


class SessionEventBase(ContractModel):
    event_id: EventId
    occurred_at: datetime

    _occurred_at_utc = field_validator("occurred_at")(require_utc)


class UtteranceLinkedPayload(ContractModel):
    utterance_id: str = Field(pattern=r"^utt_[A-Za-z0-9_-]+$")
    details: dict[str, Any] = Field(default_factory=dict)


class FollowUpPayload(ContractModel):
    summary: str = Field(min_length=1)


class UnconfirmedPronunciationPayload(ContractModel):
    status: Literal["observed", "uncertain"]
    utterance_id: str = Field(pattern=r"^utt_[A-Za-z0-9_-]+$")
    audio_artifact_id: ArtifactId | None = None


class ConfirmedPronunciationPayload(ContractModel):
    status: Literal["confirmed"]
    utterance_id: str = Field(pattern=r"^utt_[A-Za-z0-9_-]+$")
    audio_artifact_id: ArtifactId


PronunciationPayload = Annotated[
    UnconfirmedPronunciationPayload | ConfirmedPronunciationPayload,
    Field(discriminator="status"),
]


class AttemptObservedEvent(SessionEventBase):
    kind: Literal["attempt.observed"]
    payload: UtteranceLinkedPayload


class CorrectionGivenEvent(SessionEventBase):
    kind: Literal["correction.given"]
    payload: UtteranceLinkedPayload


class PronunciationAssessmentEvent(SessionEventBase):
    kind: Literal["pronunciation.assessment"]
    payload: PronunciationPayload


class FollowUpEvent(SessionEventBase):
    kind: Literal["follow_up"]
    payload: FollowUpPayload


SessionEvent = Annotated[
    AttemptObservedEvent | CorrectionGivenEvent | PronunciationAssessmentEvent | FollowUpEvent,
    Field(discriminator="kind"),
]


class SessionPackage(ContractModel):
    schema_name: Literal["lingua.session.v1"] = "lingua.session.v1"
    schema_version: Literal[1] = 1
    package_id: str = Field(pattern=r"^pkg_[A-Za-z0-9_-]+$")
    external_session_id: str = Field(min_length=1)
    session_id: SessionId | None = None
    track_hint: TrackId | None = None
    target_language: str = Field(pattern=LANGUAGE_TAG_PATTERN)
    mode: Literal["checkpoint", "completed"]
    started_at: datetime
    ended_at: datetime
    learning_targets: tuple[ContentId, ...] = ()
    transcript_layers: tuple[TranscriptLayer, ...]
    events: tuple[SessionEvent, ...] = ()
    artifacts: tuple[SessionArtifact, ...] = ()

    _started_at_utc = field_validator("started_at")(require_utc)
    _ended_at_utc = field_validator("ended_at")(require_utc)

    @model_validator(mode="after")
    def validate_package(self) -> SessionPackage:
        if self.ended_at < self.started_at:
            raise ValueError("session ended_at precedes started_at")
        layers = [layer.kind for layer in self.transcript_layers]
        if layers.count("raw") != 1 or len(layers) != len(set(layers)):
            raise ValueError("package requires exactly one raw layer and unique layer kinds")
        layer_by_kind = {layer.kind: layer for layer in self.transcript_layers}
        raw_utterance_ids = {
            utterance.utterance_id for utterance in layer_by_kind["raw"].utterances
        }
        for layer in self.transcript_layers:
            for utterance in layer.utterances:
                if utterance.started_at < self.started_at or utterance.ended_at > self.ended_at:
                    raise ValueError("utterance chronology must stay within session bounds")
            if layer.derived_from is None:
                continue
            source_layer = layer_by_kind.get(layer.derived_from)
            if source_layer is None:
                raise ValueError("derived_from must reference a transcript layer in the package")
            source_ids = {utterance.utterance_id for utterance in source_layer.utterances}
            derived_ids = {utterance.utterance_id for utterance in layer.utterances}
            if not derived_ids <= source_ids:
                raise ValueError("derived transcript utterances must resolve in their source layer")
        artifact_by_id = {str(artifact.artifact_id): artifact for artifact in self.artifacts}
        if len(artifact_by_id) != len(self.artifacts):
            raise ValueError("artifact IDs must be unique")
        for event in self.events:
            if not self.started_at <= event.occurred_at <= self.ended_at:
                raise ValueError("event chronology must stay within session bounds")
            if (
                hasattr(event.payload, "utterance_id")
                and event.payload.utterance_id not in raw_utterance_ids
            ):
                raise ValueError("event utterance_id must resolve in the raw transcript")
            for artifact_id in _artifact_references(event.payload):
                if str(artifact_id) not in artifact_by_id:
                    raise ValueError("event artifact reference must resolve in artifacts")
            if isinstance(event, PronunciationAssessmentEvent):
                audio_artifact_id = event.payload.audio_artifact_id
                if (
                    audio_artifact_id is not None
                    and artifact_by_id[str(audio_artifact_id)].kind != ArtifactKind.AUDIO
                ):
                    raise ValueError("pronunciation assessment must reference an audio artifact")
        return self


class SuccessEnvelope[T](ContractModel):
    schema_version: Literal[1] = 1
    ok: Literal[True] = True
    command: str
    correlation_id: EventId
    generated_at: datetime
    data: T

    _generated_at_utc = field_validator("generated_at")(require_utc)


class ErrorEnvelope(ContractModel):
    schema_version: Literal[1] = 1
    ok: Literal[False] = False
    command: str
    correlation_id: EventId
    generated_at: datetime
    error: ErrorPayload

    _generated_at_utc = field_validator("generated_at")(require_utc)


class StatusData(ContractModel):
    application: Literal["linguawiki"] = "linguawiki"
    application_version: str
    contract_schema_version: Literal[1] = 1
    persistence: Literal["not-initialized"] = "not-initialized"
    stage: Literal[0] = 0


class StatusEnvelope(SuccessEnvelope[StatusData]):
    data: StatusData


class GenericSuccessEnvelope(SuccessEnvelope[Any]):
    data: Any


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "lingua.workspace.v1": WorkspaceManifest,
    "lingua.session.v1": SessionPackage,
    "lingua.content.v1": ContentItem,
    "linguawiki.lock.v1": LockManifest,
    "linguawiki.cli.success.v1": GenericSuccessEnvelope,
    "linguawiki.cli.status.v1": StatusEnvelope,
    "linguawiki.cli.error.v1": ErrorEnvelope,
}
