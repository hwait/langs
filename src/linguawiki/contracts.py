"""Versioned machine contracts for workspaces, content, sessions, and CLI output."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from linguawiki import evidence as evidence_policy
from linguawiki import sources as source_policy
from linguawiki import transcripts as transcript_policy
from linguawiki.clock import require_utc, require_utc_if_set, validate_iana_timezone
from linguawiki.errors import ErrorDetail, ErrorPayload, LinguaWikiError
from linguawiki.ids import (
    ActivityId,
    ArtifactId,
    BlockId,
    ContentId,
    EventId,
    PackId,
    SessionId,
    TrackId,
    WorkspaceId,
)
from linguawiki.models import ContractModel

CONTRACT_SCHEMA_VERSION = 1
#: The delivery stage this release claims. `StatusData.stage` is the frozen wire
#: literal; this constant is what code and scripts read, and a contract test keeps
#: the two from drifting apart.
DELIVERY_STAGE = 5
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
    #: The transcriber's own confidence in this line, 0..1, when it reported one.
    #: Optional because a person taking notes has none -- but where it exists it must
    #: survive ingestion, because a low confidence is the difference between "the learner
    #: said this wrong" and "the machine may have misheard", and only the first is a
    #: mistake to teach from.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    _started_at_utc = field_validator("started_at")(require_utc)
    _ended_at_utc = field_validator("ended_at")(require_utc)

    @model_validator(mode="after")
    def chronological(self) -> TranscriptUtterance:
        if self.ended_at < self.started_at:
            raise ValueError("utterance ended_at precedes started_at")
        return self


class Transcriber(ContractModel):
    """Who produced the text, so a systematic mishearing can be traced to its cause.

    Added as an optional block rather than as required fields: a hand-written package has
    no transcriber, and the manual path has to stay first-class.
    """

    name: str = Field(min_length=1)
    version: str | None = None
    #: What the confidence numbers on this package's utterances mean, when they carry
    #: any. Left free-form because every transcriber scales its own differently, and a
    #: number whose scale is unrecorded is worse than no number.
    confidence_basis: str | None = None


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
    transcriber: Transcriber | None = None
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
        # An event identifier is what makes an observation *the same observation* across
        # an overlapping checkpoint export and a re-ingested package. A package that
        # repeats one inside itself is describing one observation twice, and staging it
        # twice would credit the learner for work they did once.
        event_ids = [str(event.event_id) for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event IDs must be unique within a package")
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


# --- Staged session events (lingua.session.events.v1) ------------------------------
#
# What `session log` accepts. This is the *internal* flush contract, and it is a
# different thing from `lingua.session.v1`: that one is a whole session produced
# somewhere else and handed over, while this is one bounded batch of observations from a
# session this workspace is running. They stay separate because a package arrives with a
# transcript and artifacts to be trusted only as far as their layers allow, whereas a
# flush arrives from the skill that is teaching and says directly what the learner did.
#
# Nothing here decides what an observation *proves*: the claim, the novelty, the delay,
# and the strength are all derived at close by `evidence.py`. A batch is a record of what
# happened, which is why it may be staged durably long before anybody knows what it will
# mean for the learner's model.


def _non_blank(value: str) -> str:
    """Refuse text that is only whitespace.

    `min_length=1` accepts `"   "`, which every service that stores it then refuses --
    and refusing at *close* leaves a session holding an observation nobody can credit.
    The boundary that accepts a payload has to be the boundary that can honour it.
    """

    if not value.strip():
        raise ValueError("must not be blank")
    return value


#: A string that carries something. Used wherever a staged payload's field is written
#: into a row a service requires to be meaningful.
NonBlankStr = Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """The values a field may take, declared once and enforced in one place.

    This annotates the **string**, not the field, and that is the whole point of it.
    `json_schema_extra` merges at the *field's* top level, where JSON Schema reads every
    keyword conjunctively: on a nullable field pydantic emitted
    `anyOf: [string, null]` and the extra `type: string` + `enum` beside it, so `null`
    satisfied the union and failed the siblings. A model's own valid output did not
    validate against its own published schema.

    Annotating the string puts the vocabulary inside the branch it describes -- the union
    wraps it, rather than the constraint escaping the union -- and carries the runtime
    check with it. One declaration, so the policy tuple, the published enum, and the rule
    that rejects an unknown value cannot drift apart, and a nullable field is simply a
    union of this and `None`.
    """

    values: tuple[str, ...]
    #: What to call this in a refusal. A producer using the wrong word needs to know
    #: which word, and "is not a known task type" reads better than a field path.
    subject: str

    def __get_pydantic_core_schema__(self, source: Any, handler: Any) -> CoreSchema:
        def _check(value: str) -> str:
            if value not in self.values:
                raise ValueError(
                    f"{value!r} is not a known {self.subject}; expected one of {list(self.values)}"
                )
            return value

        return core_schema.no_info_after_validator_function(_check, handler(source))

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: Any) -> Any:
        published = handler(schema)
        published["enum"] = list(self.values)
        return published


def vocabulary_of(annotation: Any) -> tuple[str, ...] | None:
    """The vocabulary an annotation declares, wherever it sits inside it.

    A union keeps its `Annotated` wrappers, a tuple keeps them in its arguments, and a
    bare annotated field has them hoisted into the field's own metadata -- so both are
    searched. Used by the parity test that walks every staged payload, which is how a
    field added later is covered without anyone remembering to cover it.
    """

    for entry in getattr(annotation, "__metadata__", ()):
        if isinstance(entry, Vocabulary):
            return entry.values
    for argument in get_args(annotation):
        found = vocabulary_of(argument)
        if found is not None:
            return found
    return None


def field_vocabulary(field: Any) -> tuple[str, ...] | None:
    """The vocabulary a model field declares, from its metadata or its annotation."""

    for entry in getattr(field, "metadata", ()):
        if isinstance(entry, Vocabulary):
            return entry.values
    return vocabulary_of(field.annotation)


TaskType = Annotated[str, Vocabulary(evidence_policy.TASK_TYPES, "task type")]
Modality = Annotated[str, Vocabulary(evidence_policy.MODALITIES, "modality")]
EvidenceClaim = Annotated[str, Vocabulary(evidence_policy.CLAIMS, "evidence claim")]
HelpLevel = Annotated[str, Vocabulary(evidence_policy.HELP_LEVELS, "help level")]
CorrectionMode = Annotated[str, Vocabulary(evidence_policy.CORRECTION_MODES, "correction mode")]
RetrievalClass = Annotated[str, Vocabulary(evidence_policy.RETRIEVAL_CLASSES, "retrieval class")]
AssessorKind = Annotated[str, Vocabulary(evidence_policy.ASSESSOR_KINDS, "assessor kind")]
ConfidenceLevel = Annotated[str, Vocabulary(evidence_policy.CONFIDENCE_LEVELS, "confidence")]
ResponseVisibility = Annotated[
    str, Vocabulary(evidence_policy.RESPONSE_VISIBILITIES, "response visibility")
]
ObservationCategory = Annotated[
    str, Vocabulary(evidence_policy.OBSERVATION_CATEGORIES, "observation category")
]
Salience = Annotated[str, Vocabulary(evidence_policy.SALIENCES, "salience")]
ComprehensionAid = Annotated[str, Vocabulary(source_policy.COMPREHENSION_AIDS, "comprehension aid")]
ComprehensionBand = Annotated[
    str, Vocabulary(source_policy.COMPREHENSION_BANDS, "comprehension band")
]
StudyMode = Annotated[str, Vocabulary(source_policy.STUDY_MODES, "study mode")]
PronunciationDimension = Annotated[
    str, Vocabulary(transcript_policy.PRONUNCIATION_DIMENSIONS, "pronunciation dimension")
]


class StagedPayloadBase(ContractModel):
    """What every staged payload may carry beyond its own kind's fields.

    Two groups, and both are decided by the workspace rather than by the caller:

    - **what of the learner's words is kept.** The retention rule runs when the event is
      staged, so these fields hold its *result*: the visibility it resolved to, the text
      that survived it, and the hash of what arrived. A staged payload is kept as the
      audit trail of what a close was given, which is exactly why the full text must not
      be in it.
    - **where the observation came from.** An ingested package sets these; a skill flush
      never does, and the ingestion path strips them from a caller's payload rather than
      trusting a file or a prompt about its own provenance.
    """

    response: str | None = None
    response_visibility: ResponseVisibility | None = None
    #: Written by the CLI when it applies the track's retention rule at flush time: the
    #: hash of the response as it arrived, which is what proves a withheld response
    #: existed once the text itself is gone.
    response_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    source: Literal["package"] | None = None
    package_id: str | None = None
    #: Which external session the utterance below belongs to. A producer's utterance IDs
    #: are unique inside one call and no further, so without this an event from one
    #: conversation resolved to another conversation's words.
    external_session_id: str | None = None
    utterance_id: str | None = Field(default=None, pattern=r"^utt_[A-Za-z0-9_-]+$")
    transcript_layer: Literal["raw", "normalized", "reviewed-hearing"] | None = None


class StagedAttemptPayload(StagedPayloadBase):
    """One observed attempt: what was asked, what help was given, how it went."""

    task_type: TaskType
    modality: Modality
    score: float = Field(ge=0.0, le=1.0)
    #: A knowledge item by stable key or content ID, a dimension, or both -- an attempt
    #: that measures neither measures nothing.
    target: str | None = None
    dimension: str | None = None
    claims: tuple[EvidenceClaim, ...] = ()
    help_level: HelpLevel = "none"
    correction_mode: CorrectionMode = "none"
    retrieval: RetrievalClass = "immediate"
    delay_hours: float | None = Field(default=None, ge=0.0)
    latency_ms: int | None = Field(default=None, ge=0)
    context: str | None = None
    assessor_kind: AssessorKind = "ai"
    assessor: str | None = None
    confidence: ConfidenceLevel = "medium"

    @model_validator(mode="after")
    def measures_something(self) -> StagedAttemptPayload:
        if self.target is None and self.dimension is None:
            raise ValueError("an attempt measures a knowledge item, a dimension, or both")
        return self


class StagedCorrectionPayload(StagedPayloadBase):
    """One correction given: the learner's form, the target form, and the pattern."""

    category: NonBlankStr
    signature: NonBlankStr
    description: NonBlankStr
    target: str | None = None
    learner_form: str | None = None
    corrected_form: str | None = None
    explanation: str | None = None
    meaning_impact: str = "minor"
    #: A mishearing or a transcription artifact is recorded and counted against nobody.
    classification: str = "learner-error"
    confidence: ConfidenceLevel = "medium"
    severity: str = "medium"
    #: File this against an existing pattern, or insist it is a new one. A signature that
    #: is close to an existing pattern without matching it is refused without one of them.
    attach_to: str | None = None
    distinct: bool = False


class StagedPronunciationPayload(StagedPayloadBase):
    """One pronunciation observation, and what it is based on.

    `confirmed` requires audio. A correct transcript proves nothing about how something
    sounded, so a transcript-based observation is recorded as `observed` or `uncertain`
    and never becomes a confirmed pronunciation claim at close.
    """

    status: Literal["observed", "uncertain", "confirmed"]
    note: NonBlankStr
    target: str | None = None
    dimension: str = "pronunciation"
    #: *What* about the sound is being judged, as opposed to which ability dimension the
    #: observation belongs to. Separate because a learner can be perfectly intelligible
    #: and nothing like a native speaker, and because prosody and native-likeness can only
    #: ever be judged from audio -- a rule that needs to know which one this is.
    acoustic_dimension: PronunciationDimension = "intelligibility"
    audio_artifact_id: ArtifactId | None = None

    @model_validator(mode="after")
    def confirmation_requires_audio(self) -> StagedPronunciationPayload:
        if self.status == "confirmed" and self.audio_artifact_id is None:
            raise ValueError("a confirmed pronunciation observation must reference audio")
        return self


class StagedSourceProgressPayload(StagedPayloadBase):
    """What the learner did with a source during this session.

    Stage 4 left this out and said so: a session could not record that the learner read
    thirty pages, so reading happened beside the session lifecycle instead of inside it.
    It belongs here for the same reason everything else does -- the close is where a
    session becomes part of the learner, and reading is not an exception to that.

    The aid and the band are the comprehension record, and their *order* carries the
    evidence: an unaided reading that follows an aided one is refused at close, exactly as
    it is refused at the command, because it is the first reading with the help left out.
    """

    #: The catalogued source, by identifier or title. Named `source_ref` because every
    #: staged payload already carries a `source` meaning *where the observation came
    #: from*, and one field cannot mean both the provenance and the book.
    source_ref: NonBlankStr
    unit: str | None = None
    aid: ComprehensionAid = "unaided"
    band: ComprehensionBand
    mode: StudyMode = "intensive"
    replays: int = Field(default=0, ge=0)
    lookups: int = Field(default=0, ge=0)
    minutes: int | None = Field(default=None, ge=0)
    #: Whether the learner finished the unit, rather than merely worked in it. Coverage
    #: counts completions, so that rereading a chapter does not report as reading two.
    completed: bool = False
    note: str | None = None


class StagedObservationPayload(StagedPayloadBase):
    """A note about the session rather than about an item: fatigue, strategy, confidence."""

    category: ObservationCategory
    note: Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_non_blank)]
    salience: Salience = "medium"


class StagedFollowUpPayload(StagedPayloadBase):
    """Something to come back to, with an optional due window."""

    kind: NonBlankStr
    action: NonBlankStr
    target: str | None = None
    error: str | None = None
    priority: int = 0
    due_from: datetime | None = None
    due_by: datetime | None = None

    _due_from_utc = field_validator("due_from")(require_utc_if_set)
    _due_by_utc = field_validator("due_by")(require_utc_if_set)


class StagedEventBase(ContractModel):
    event_id: EventId
    occurred_at: datetime
    #: Which block and activity this happened in. A flush normally covers one block, and
    #: an event that names none is still staged: losing an observation to bookkeeping is
    #: worse than an observation whose block is unknown.
    block: BlockId | None = None
    activity: ActivityId | None = None

    _occurred_at_utc = field_validator("occurred_at")(require_utc)


class StagedAttemptEvent(StagedEventBase):
    kind: Literal["attempt.observed"]
    payload: StagedAttemptPayload


class StagedCorrectionEvent(StagedEventBase):
    kind: Literal["correction.given"]
    payload: StagedCorrectionPayload


class StagedPronunciationEvent(StagedEventBase):
    kind: Literal["pronunciation.assessment"]
    payload: StagedPronunciationPayload


class StagedSourceProgressEvent(StagedEventBase):
    kind: Literal["source.progress"]
    payload: StagedSourceProgressPayload


class StagedObservationEvent(StagedEventBase):
    kind: Literal["observation.noted"]
    payload: StagedObservationPayload


class StagedFollowUpEvent(StagedEventBase):
    kind: Literal["follow_up"]
    payload: StagedFollowUpPayload


#: Which model validates each staged event kind. The service reads this to revalidate a
#: payload on its way into storage and again on its way out; a contract test reads it to
#: walk every payload's declared vocabularies.
STAGED_PAYLOAD_KINDS: Mapping[str, type[ContractModel]] = {
    "attempt.observed": StagedAttemptPayload,
    "correction.given": StagedCorrectionPayload,
    "pronunciation.assessment": StagedPronunciationPayload,
    "source.progress": StagedSourceProgressPayload,
    "observation.noted": StagedObservationPayload,
    "follow_up": StagedFollowUpPayload,
}


StagedEvent = Annotated[
    StagedAttemptEvent
    | StagedCorrectionEvent
    | StagedPronunciationEvent
    | StagedSourceProgressEvent
    | StagedObservationEvent
    | StagedFollowUpEvent,
    Field(discriminator="kind"),
]


class SessionEventBatch(ContractModel):
    """One flush: an ordered, bounded set of observations from one block.

    The sequence number and the idempotency key are what make a retry safe. A batch that
    arrives twice with the same key and the same content is accepted once; the same key
    with different content is a conflict rather than an overwrite, because the second
    call would otherwise silently discard the first call's observations.
    """

    schema_name: Literal["lingua.session.events.v1"] = "lingua.session.events.v1"
    schema_version: Literal[1] = 1
    session_id: SessionId | None = None
    sequence: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    #: The caller's own hash of `events`, checked against the one computed here. Optional
    #: because it protects the transport rather than the contract, but when it is given
    #: and disagrees, the batch is refused instead of stored.
    content_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    block: BlockId | None = None
    events: tuple[StagedEvent, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def events_are_unique_and_ordered(self) -> SessionEventBatch:
        identifiers = [str(event.event_id) for event in self.events]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("event IDs must be unique within a batch")
        if any(
            current.occurred_at < previous.occurred_at
            for previous, current in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("events must be ordered by occurred_at")
        return self


# --- Language-pack contract (lingua.pack.v1) ---------------------------------------
#
# A pack is a versioned directory validated against these models. Core reads
# capabilities, frameworks, and coverage; it never branches on a language tag.

PACK_STABLE_KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,127}$"
PACK_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"
PACK_VERSION_PATTERN = r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$"


class PackMaturity(StrEnum):
    """What a pack may promise. Onboarding offers only what the level supports."""

    FIXTURE = "fixture"
    PILOT = "pilot"
    ONBOARDING_READY = "onboarding-ready"
    PLACEMENT_READY = "placement-ready"


class OriginClass(StrEnum):
    """Controlled item-level origin classes from the pack-authoring workflow."""

    AUTHENTIC_SOURCE = "authentic-source"
    LEARNER_PRODUCED = "learner-produced"
    SOURCE_DERIVED = "source-derived"
    AI_ADAPTED = "ai-adapted"
    AI_GENERATED = "ai-generated"
    SYNTHETIC_MEDIA = "synthetic-media"
    HUMAN_AUTHORED = "human-authored"


class ReviewerKind(StrEnum):
    MACHINE = "machine"
    AI = "ai"
    LEARNER = "learner"
    HUMAN = "human"
    NOT_APPLICABLE = "not-applicable"


class PackItemOrigin(ContractModel):
    """One origin of a persistent pack item."""

    origin_class: OriginClass
    reference: str | None = None
    locator: str | None = None
    transformation: str | None = None
    origin_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    rights: str = Field(min_length=1)
    privacy: Literal["public", "private", "synthetic"]

    @model_validator(mode="after")
    def derived_origins_identify_their_source(self) -> PackItemOrigin:
        needs_reference = {
            OriginClass.AUTHENTIC_SOURCE,
            OriginClass.SOURCE_DERIVED,
            OriginClass.AI_ADAPTED,
        }
        if self.origin_class in needs_reference and not self.reference:
            raise ValueError(f"{self.origin_class} origin must identify its source reference")
        if self.origin_class == OriginClass.AUTHENTIC_SOURCE and not self.locator:
            raise ValueError("authentic-source origin must record an exact locator")
        return self


class PackItemReview(ContractModel):
    """One review axis of one item, in that axis's own state vocabulary.

    The state vocabulary differs per axis on purpose: `cleared` means something for
    rights and nothing for pronunciation correctness. `services.provenance` maps each
    state onto the pass/pending/fail gate the frozen content contract enforces.
    """

    state: str = Field(min_length=1)
    reviewer_kind: ReviewerKind
    reviewer: str | None = None
    method: str | None = None
    evidence_reference: str | None = None


PackReviewAxes = dict[
    Literal["linguistic", "pedagogical", "source-alignment", "rights", "privacy"], PackItemReview
]


class PackItemDependency(ContractModel):
    kind: Literal["content", "source", "template", "artifact", "policy"]
    reference: str = Field(min_length=1)
    expected_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    on_change: Literal["needs-review", "quarantine", "ignore"] = "needs-review"


class PackItemProvenance(ContractModel):
    """The provenance envelope every persistent pack item carries.

    `origin` and `reviews` may name a manifest-declared profile instead of repeating
    the same five axes on every item; the loader resolves a profile into item-level
    rows before anything is hashed or stored, so item-level provenance is never lost.
    """

    origin_profile: str | None = None
    origins: tuple[PackItemOrigin, ...] = ()
    review_profile: str | None = None
    reviews: PackReviewAxes = Field(default_factory=dict)
    dependencies: tuple[PackItemDependency, ...] = ()
    lifecycle: Literal[
        "draft",
        "candidate",
        "approved-personal",
        "verified",
        "publication-ready",
        "needs-review",
        "rejected",
        "deprecated",
    ]
    risk_tier: int = Field(ge=0, le=4)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    generation_run: str | None = None

    @model_validator(mode="after")
    def origin_is_declared_exactly_once(self) -> PackItemProvenance:
        if bool(self.origin_profile) == bool(self.origins):
            raise ValueError("declare either origin_profile or a non-empty origins list")
        if bool(self.review_profile) == bool(self.reviews):
            raise ValueError("declare either review_profile or a non-empty reviews mapping")
        return self


class PackAlias(ContractModel):
    alias: str = Field(min_length=1)
    locale: str = Field(min_length=1)
    script: str | None = None


class PackKnowledgeItem(ContractModel):
    """One knowledge target in `seed/knowledge.jsonl`."""

    stable_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
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
    summary: str | None = None
    level: str = Field(min_length=1)
    level_max: str | None = None
    themes: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    frequency_band: str | None = None
    aliases: tuple[PackAlias, ...] = ()
    provenance: PackItemProvenance


class PackRelation(ContractModel):
    """One typed edge in `seed/relations.jsonl`."""

    source_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
    relation_type: Literal[
        "prerequisite",
        "form-of",
        "sense-of",
        "contrast",
        "collocation",
        "government",
        "example-of",
        "related",
        "error-target",
        "curriculum-objective",
    ]
    target_key: str | None = Field(default=None, pattern=PACK_STABLE_KEY_PATTERN)
    target_ref: str | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> PackRelation:
        if bool(self.target_key) == bool(self.target_ref):
            raise ValueError("a relation names either target_key or target_ref, never both")
        return self


class PackExample(ContractModel):
    """One example in `seed/examples.jsonl`."""

    stable_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
    item_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
    text: str = Field(min_length=1)
    translation: str | None = None
    gloss: str | None = None
    locale: str | None = None
    difficulty: str | None = None
    provenance: PackItemProvenance


class PackDescriptor(ContractModel):
    stable_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
    level: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    locale: str = Field(min_length=1)
    descriptor: str = Field(min_length=1)
    provenance: PackItemProvenance


class PackProficiencyFile(ContractModel):
    schema_name: Literal["lingua.pack.proficiency.v1"] = "lingua.pack.proficiency.v1"
    schema_version: Literal[1] = 1
    framework: str = Field(min_length=1)
    descriptors: tuple[PackDescriptor, ...]

    @model_validator(mode="after")
    def descriptor_keys_are_unique(self) -> PackProficiencyFile:
        keys = [descriptor.stable_key for descriptor in self.descriptors]
        if len(keys) != len(set(keys)):
            raise ValueError("descriptor stable keys must be unique inside a file")
        return self


class AnswerKey(ContractModel):
    """The forms a machine-scorable task accepts, typed rather than described.

    `expected` was `dict[str, Any]` whose only rule was "not empty", so `{"answers": []}`,
    `{"answers": "yes"}` and `{"unrecognized": true}` were all valid packs. Every one of
    them reaches a scorer as an exception or a silent 0.0, which is a learner's zero
    resting on a broken pack rather than on anything they did. `ContractModel` is
    `extra="forbid"`, which is what refuses the unrecognized key.

    A blank member is refused after stripping, not by `min_length=1`: `"   "` is a string
    of length three that no comparison can ever match, so a pack carrying one declares an
    answer that cannot be given.
    """

    answers: tuple[NonBlankStr, ...] = Field(min_length=1)


def reads_as_json_object(raw: str) -> bool:
    """Whether stored text is a JSON object, answered rather than raised.

    Lives beside `parse_answer_key` because the two answer the same kind of question about
    the same kind of column: an added column cannot carry `json_valid`, so every reader of
    one parses defensively. `db check` needs the answer without raising, and the scorer
    needs the same definition of readable, so there is one.
    """

    try:
        return isinstance(json.loads(raw), dict)
    except (ValueError, RecursionError):
        # `RecursionError` is not a `ValueError`: deeply nested but otherwise valid JSON
        # exhausts the decoder's stack, and catching only the malformed case let it escape
        # a helper whose whole contract is to answer rather than raise.
        return False


def parse_answer_key(raw: str | None) -> AnswerKey:
    """Read a stored answer key, refusing by name whichever rule it breaks.

    The one place an answer key is parsed: the scorer, `pack validate`, and `db check` all
    come through here, so "what is a usable key" has a single answer. Revalidation is the
    point rather than a formality -- a serve-time snapshot is a round trip through JSON
    and can be hand-edited, damaged, or restored from another release between the serve
    and the score, and neither `assessment_run_tasks.expected_json` nor the column it was
    copied from could carry a `json_valid` constraint (DuckDB refuses a constrained
    `ALTER TABLE ADD COLUMN`).
    """

    def refuse(reason: str) -> LinguaWikiError:
        return LinguaWikiError(
            "assessment_answer_key_malformed",
            f"the answer key cannot be used to score: {reason}",
            details=(ErrorDetail(field="expected", reason=reason),),
        )

    if raw is None or not raw.strip():
        raise refuse("no answer key was recorded for this task")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise refuse(f"not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # Not a `ValueError`, so it escaped as an unhandled crash rather than a refusal:
        # out of `db check`, whose contract is never to raise, and out of the scorer, where
        # it surfaced as `internal_error` instead of naming the damaged key.
        raise refuse("nested too deeply for any parser to read") from exc
    if not isinstance(document, dict):
        raise refuse(f"not a JSON object but {type(document).__name__}")
    try:
        return AnswerKey.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "expected"
        raise refuse(f"{field} {first['msg'].lower()}") from exc


class PackAssessmentTask(ContractModel):
    """One bank item. `difficulty` is on the ordinal grid the staircase works on."""

    stable_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
    dimension: str = Field(min_length=1)
    task_type: Literal[
        "objective",
        "short-response",
        "extended-productive",
        "pronunciation-target",
        "connected-speech",
    ]
    level: str = Field(min_length=1)
    difficulty: float
    content_family: str = Field(min_length=1)
    modality: Literal["text", "audio", "speech", "writing"]
    prompt: str = Field(min_length=1)
    rubric_version: int = Field(default=1, ge=1)
    rubric: dict[str, Any] = Field(default_factory=dict)
    expected: AnswerKey | None = None
    permitted_help: str = "none"
    is_anchor: bool = False
    target_keys: tuple[str, ...] = ()
    provenance: PackItemProvenance

    @model_validator(mode="after")
    def scored_tasks_declare_how_they_are_scored(self) -> PackAssessmentTask:
        if self.task_type in {"objective", "short-response"} and self.expected is None:
            raise ValueError(f"{self.task_type} task must declare its expected answers")
        if self.task_type not in {"objective", "short-response"} and not self.rubric:
            raise ValueError(f"{self.task_type} task must declare a rubric")
        return self


class PackAssessmentFile(ContractModel):
    schema_name: Literal["lingua.pack.assessment.v1"] = "lingua.pack.assessment.v1"
    schema_version: Literal[1] = 1
    form_key: str = Field(pattern=PACK_KEY_PATTERN)
    version: int = Field(ge=1)
    purpose: Literal["pilot-calibration", "placement", "weekly", "monthly", "milestone"]
    framework: str = Field(min_length=1)
    level_min: str = Field(min_length=1)
    level_max: str = Field(min_length=1)
    title: str = Field(min_length=1)
    tasks: tuple[PackAssessmentTask, ...]

    @model_validator(mode="after")
    def task_keys_are_unique(self) -> PackAssessmentFile:
        keys = [task.stable_key for task in self.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("assessment task stable keys must be unique inside a form")
        return self


class PackActivityTemplate(ContractModel):
    stable_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
    mode: str = Field(min_length=1)
    title: str = Field(min_length=1)
    level: str = Field(min_length=1)
    minutes: int = Field(gt=0)
    structure: dict[str, Any] = Field(default_factory=dict)
    provenance: PackItemProvenance


class PackActivityFile(ContractModel):
    schema_name: Literal["lingua.pack.activities.v1"] = "lingua.pack.activities.v1"
    schema_version: Literal[1] = 1
    templates: tuple[PackActivityTemplate, ...]


class PackSourceRecommendation(ContractModel):
    stable_key: str = Field(pattern=PACK_STABLE_KEY_PATTERN)
    modality: str = Field(min_length=1)
    title: str = Field(min_length=1)
    creator: str | None = None
    locator: str | None = None
    level: str = Field(min_length=1)
    license: str = Field(min_length=1)
    rights_status: Literal["unknown", "personal-use-only", "cleared", "restricted"]
    support_language: str | None = None
    notes: str | None = None
    provenance: PackItemProvenance


class PackReferencesFile(ContractModel):
    schema_name: Literal["lingua.pack.references.v1"] = "lingua.pack.references.v1"
    schema_version: Literal[1] = 1
    recommendations: tuple[PackSourceRecommendation, ...]


class PackExpectations(ContractModel):
    """`tests/expectations.json`: what the pack author asserts about their own pack.

    A pack ships its own validation test rather than trusting the core's generic
    coverage gate to notice that a file was accidentally emptied.
    """

    schema_name: Literal["lingua.pack.expectations.v1"] = "lingua.pack.expectations.v1"
    schema_version: Literal[1] = 1
    maturity: PackMaturity
    minimum_counts: dict[str, int] = Field(default_factory=dict)
    required_themes: tuple[str, ...] = ()
    required_dimensions: tuple[str, ...] = ()
    refuses_maturity: tuple[PackMaturity, ...] = ()

    @model_validator(mode="after")
    def counts_are_positive(self) -> PackExpectations:
        if any(value < 0 for value in self.minimum_counts.values()):
            raise ValueError("minimum counts cannot be negative")
        if self.maturity in self.refuses_maturity:
            raise ValueError("a pack cannot both claim and refuse a maturity level")
        return self


class PackBundleItem(ContractModel):
    item_kind: Literal[
        "knowledge",
        "descriptor",
        "assessment_task",
        "activity_template",
        "source_recommendation",
        "example",
    ]
    item_ref: str = Field(min_length=1)


class PackBundleFile(ContractModel):
    schema_name: Literal["lingua.pack.bundle.v1"] = "lingua.pack.bundle.v1"
    schema_version: Literal[1] = 1
    bundle_key: str = Field(pattern=PACK_KEY_PATTERN)
    bundle_type: str = Field(min_length=1)
    framework: str = Field(min_length=1)
    level: str = Field(min_length=1)
    title: str = Field(min_length=1)
    license: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    items: tuple[PackBundleItem, ...]
    provenance: PackItemProvenance


class PackCapabilities(ContractModel):
    """What the language needs, asked as capability questions rather than by name."""

    schema_name: Literal["lingua.pack.capabilities.v1"] = "lingua.pack.capabilities.v1"
    schema_version: Literal[1] = 1
    word_segmentation: Literal["whitespace", "pack-adapter", "none"]
    inflection: bool
    grammatical_case: bool
    aspect: bool = False
    tone: bool = False
    romanization: bool = False
    script_learning_required: bool = False
    pronunciation_contrasts: bool = False
    adapters: dict[str, str] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)


class PackSourceClass(ContractModel):
    source_class: str = Field(min_length=1)
    rank: int = Field(ge=1)
    description: str = Field(min_length=1)


class PackSourcePolicy(ContractModel):
    """Pack-declared authoritative source classes. Core never ranks institutions."""

    schema_name: Literal["lingua.pack.source-policy.v1"] = "lingua.pack.source-policy.v1"
    schema_version: Literal[1] = 1
    source_classes: tuple[PackSourceClass, ...]
    escalation: tuple[str, ...] = ()
    prohibited: tuple[str, ...] = ()

    @model_validator(mode="after")
    def ranks_are_a_total_order(self) -> PackSourcePolicy:
        ranks = [entry.rank for entry in self.source_classes]
        if len(ranks) != len(set(ranks)):
            raise ValueError("source-class ranks must be unique")
        names = [entry.source_class for entry in self.source_classes]
        if len(names) != len(set(names)):
            raise ValueError("source-class names must be unique")
        return self


class PackFramework(ContractModel):
    framework_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: str | None = None
    levels: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def levels_are_unique_and_ordered(self) -> PackFramework:
        if len(self.levels) != len(set(self.levels)):
            raise ValueError("framework level codes must be unique")
        return self


class PackBundleDeclaration(ContractModel):
    bundle_key: str = Field(pattern=PACK_KEY_PATTERN)
    file: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()


def _bundle_cycle(dependencies: Mapping[str, Sequence[str]]) -> list[str] | None:
    """The first dependency cycle among bundles, as a readable path, or None.

    Rejecting only self-dependencies was not enough: `A -> B -> A` passed validation, and
    the preparation resolver -- which deepens a bundle every time it is reached -- then
    never emptied its frontier and looped for ever.
    """

    visiting: dict[str, bool] = {}
    path: list[str] = []

    def walk(key: str) -> list[str] | None:
        if visiting.get(key) is True:
            return [*path[path.index(key) :], key]
        if key in visiting:
            return None
        visiting[key] = True
        path.append(key)
        for dependency in dependencies.get(key, ()):
            found = walk(dependency)
            if found is not None:
                return found
        path.pop()
        visiting[key] = False
        return None

    for key in dependencies:
        cycle = walk(key)
        if cycle is not None:
            return cycle
    return None


class PackManifest(ContractModel):
    """`manifest.json`: the pack's identity, promises, and content address."""

    schema_name: Literal["lingua.pack.v1"] = "lingua.pack.v1"
    schema_version: Literal[1] = 1
    data_format_version: Literal[1] = 1
    pack_key: str = Field(pattern=PACK_KEY_PATTERN)
    name: str = Field(min_length=1)
    version: str = Field(pattern=PACK_VERSION_PATTERN)
    language: str = Field(pattern=LANGUAGE_TAG_PATTERN)
    scripts: tuple[str, ...] = ()
    writing_direction: Literal["ltr", "rtl", "ttb"] = "ltr"
    license: str = Field(min_length=1)
    maintainers: tuple[str, ...] = Field(min_length=1)
    maturity: PackMaturity
    frameworks: tuple[PackFramework, ...] = Field(min_length=1)
    #: Framework levels this pack claims to cover. Coverage gates are per band.
    bands: tuple[str, ...] = Field(min_length=1)
    dimensions: tuple[str, ...] = Field(min_length=1)
    #: What each declared dimension is, so core never classifies a dimension by name.
    dimension_kinds: dict[str, Literal["receptive", "productive", "form", "pronunciation"]]
    modalities: tuple[str, ...] = Field(min_length=1)
    support_languages: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    bundles: tuple[PackBundleDeclaration, ...] = ()
    origin_profiles: dict[str, tuple[PackItemOrigin, ...]] = Field(default_factory=dict)
    review_profiles: dict[str, PackReviewAxes] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    content_address: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def declarations_resolve(self) -> PackManifest:
        framework_ids = [framework.framework_id for framework in self.frameworks]
        if len(framework_ids) != len(set(framework_ids)):
            raise ValueError("framework identifiers must be unique")
        bundle_keys = [bundle.bundle_key for bundle in self.bundles]
        if len(bundle_keys) != len(set(bundle_keys)):
            raise ValueError("bundle keys must be unique")
        declared = set(bundle_keys)
        for bundle in self.bundles:
            missing = set(bundle.depends_on) - declared
            if missing:
                raise ValueError(f"{bundle.bundle_key} depends on undeclared {sorted(missing)}")
            if bundle.bundle_key in bundle.depends_on:
                raise ValueError(f"{bundle.bundle_key} cannot depend on itself")
        cycle = _bundle_cycle({b.bundle_key: b.depends_on for b in self.bundles})
        if cycle is not None:
            raise ValueError(f"bundle dependencies must be acyclic; found {' -> '.join(cycle)}")
        for value in (*self.files, *(bundle.file for bundle in self.bundles)):
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or value != path.as_posix():
                raise ValueError(f"pack file reference must be a safe relative path: {value}")
        for digest in self.files.values():
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("pack file checksums must be lowercase SHA-256 hex")
        if len(self.dimensions) != len(set(self.dimensions)):
            raise ValueError("declared dimensions must be unique")
        if set(self.dimension_kinds) != set(self.dimensions):
            raise ValueError("dimension_kinds must classify exactly the declared dimensions")
        levels = {level for framework in self.frameworks for level in framework.levels}
        undeclared = sorted(set(self.bands) - levels)
        if undeclared:
            raise ValueError(f"bands name levels no declared framework has: {undeclared}")
        if len(self.bands) != len(set(self.bands)):
            raise ValueError("covered bands must be unique")
        return self

    @property
    def framework_by_id(self) -> dict[str, PackFramework]:
        return {framework.framework_id: framework for framework in self.frameworks}


class SuccessEnvelope[T](ContractModel):
    schema_version: Literal[1] = 1
    ok: Literal[True] = True
    command: str
    correlation_id: EventId
    generated_at: datetime
    warnings: tuple[str, ...] = ()
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
    database_schema_version: int = Field(ge=1)
    persistence: Literal["available"] = "available"
    stage: Literal[5] = 5


class StatusEnvelope(SuccessEnvelope[StatusData]):
    data: StatusData


class GenericSuccessEnvelope(SuccessEnvelope[Any]):
    data: Any


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "lingua.workspace.v1": WorkspaceManifest,
    "lingua.session.v1": SessionPackage,
    "lingua.session.events.v1": SessionEventBatch,
    "lingua.content.v1": ContentItem,
    "lingua.pack.v1": PackManifest,
    "lingua.pack.capabilities.v1": PackCapabilities,
    "lingua.pack.source-policy.v1": PackSourcePolicy,
    "lingua.pack.proficiency.v1": PackProficiencyFile,
    "lingua.pack.assessment.v1": PackAssessmentFile,
    "lingua.pack.activities.v1": PackActivityFile,
    "lingua.pack.references.v1": PackReferencesFile,
    "lingua.pack.expectations.v1": PackExpectations,
    "lingua.pack.bundle.v1": PackBundleFile,
    "lingua.pack.knowledge.v1": PackKnowledgeItem,
    "lingua.pack.relation.v1": PackRelation,
    "lingua.pack.example.v1": PackExample,
    "linguawiki.lock.v1": LockManifest,
    "linguawiki.cli.success.v1": GenericSuccessEnvelope,
    "linguawiki.cli.status.v1": StatusEnvelope,
    "linguawiki.cli.error.v1": ErrorEnvelope,
}
