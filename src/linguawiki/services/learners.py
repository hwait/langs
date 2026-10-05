"""Learner and learning-track setup.

A track is the scope key for everything learner-specific, so creating one settles four
questions that later stages must never have to re-decide:

- which installed pack serves this language, script, and region;
- which single proficiency framework the track's level labels belong to;
- which level the learner *declares*, recorded as a hypothesis rather than as evidence;
- which preferences change behaviour, stored as typed rows rather than free text.

A level label from another framework is refused by name, never translated: `A2` and
`HSK2` are not interchangeable, and only explicitly reviewed pack data may relate them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from linguawiki.clock import Clock, SystemClock, aware_utc, validate_iana_timezone
from linguawiki.contracts import LANGUAGE_TAG_PATTERN, PackManifest, Vocabulary
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId, TrackId, UserId
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import recording_permitted
from linguawiki.services import packs as pack_service

SCRIPT_PATTERN = re.compile(r"^[A-Z][a-z]{3}$")
REGION_PATTERN = re.compile(r"^(?:[A-Z]{2}|\d{3})$")

CORRECTION_MODES = ("fluency", "accuracy", "exam")


#: From the implementation plan. A workspace that offers only a consent boolean cannot
#: express "keep this until the pronunciation target is met and then delete it", which is
#: the setting a learner recording themselves every week actually needs.
AUDIO_RETENTION_POLICIES: tuple[str, ...] = ("keep", "rolling-days", "delete-after-ingestion")

AudioRetentionPolicy = Annotated[
    str, Vocabulary(AUDIO_RETENTION_POLICIES, "audio retention policy")
]


class TrackPreferences(ContractModel):
    """The typed preference set a track may declare."""

    goals: tuple[str, ...] = ()
    intended_uses: tuple[str, ...] = ()
    weekly_minutes: int | None = Field(default=None, gt=0)
    session_minutes: int | None = Field(default=None, ge=20, le=180)
    sessions_per_week: int | None = Field(default=None, ge=1, le=21)
    interests: tuple[str, ...] = ()
    avoided_topics: tuple[str, ...] = ()
    preferred_source_types: tuple[str, ...] = ()
    correction_mode: str | None = None
    explanation_language: str | None = None
    accessibility_needs: tuple[str, ...] = ()
    voice_available: bool | None = None
    audio_recording_available: bool | None = None
    transcript_retention_consent: bool | None = None
    audio_retention_consent: bool | None = None
    #: What happens to a recording once it has been ingested. `keep` holds it until the
    #: learner says otherwise; `rolling-days` holds it for `audio_retention_days` and then
    #: it is swept; `delete-after-ingestion` keeps only the claims made from it. Consent
    #: says the learner *allows* a recording to be kept; this says for how long, and the
    #: two are different questions.
    audio_retention_policy: AudioRetentionPolicy | None = None
    audio_retention_days: int | None = Field(default=None, ge=1, le=3650)
    external_service_consent: bool | None = None
    prior_materials: tuple[str, ...] = ()
    anki_deck: str | None = None

    @field_validator("correction_mode")
    @classmethod
    def known_correction_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in CORRECTION_MODES:
            raise ValueError(f"correction_mode must be one of {list(CORRECTION_MODES)}")
        return value

    @model_validator(mode="after")
    def consent_is_explicit_before_retention(self) -> TrackPreferences:
        if self.audio_retention_consent and self.transcript_retention_consent is False:
            raise ValueError(
                "audio retention cannot be consented to while transcript retention is refused"
            )
        return self

    @classmethod
    def keys(cls) -> tuple[str, ...]:
        """Every preference key, derived from the model so the two cannot drift."""

        return tuple(cls.model_fields)

    def rows(self) -> dict[str, str]:
        """The preference rows to store, omitting anything the learner left unset."""

        payload = self.model_dump(mode="json")
        return {
            key: json.dumps(value, ensure_ascii=False, sort_keys=True)
            for key, value in payload.items()
            if value not in (None, (), [])
        }


class UserRecord(ContractModel):
    user_id: str
    workspace_id: str
    display_name: str
    timezone: str
    status: str
    native_languages: tuple[str, ...] = ()
    support_languages: tuple[str, ...] = ()
    tracks: tuple[str, ...] = ()
    created_at: str
    updated_at: str


class TrackRecord(ContractModel):
    track_id: str
    user_id: str
    target_language: str
    region: str | None
    script: str | None
    proficiency_framework: str
    framework_levels: tuple[str, ...] = ()
    declared_level: str | None
    current_level: str | None
    target_level: str | None
    goal: str | None
    status: str
    is_primary: bool
    weekly_minutes: int | None
    timezone: str
    #: The pack the track was created from. Framework-scoped rows key on it, so a caller
    #: validating a track against its pack needs it and not only the display key.
    pack_id: str | None = None
    pack_key: str | None = None
    pack_version: str | None = None
    pack_maturity: str | None = None
    preferences: dict[str, object] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    warnings: tuple[str, ...] = ()


def _language_subtags(tag: str) -> tuple[str, str | None, str | None]:
    """Split a BCP-47 tag into (language, script, region) without a locale library."""

    parts = tag.split("-")
    language = parts[0].lower()
    script: str | None = None
    region: str | None = None
    for part in parts[1:]:
        if SCRIPT_PATTERN.match(part.title()) and script is None and len(part) == 4:
            script = part.title()
        elif REGION_PATTERN.match(part.upper()) and region is None and len(part) in (2, 3):
            region = part.upper()
    return language, script, region


def assert_language_tag(value: str, *, field: str) -> str:
    if not re.fullmatch(LANGUAGE_TAG_PATTERN, value):
        raise LinguaWikiError(
            "invalid_language_tag",
            f"{value} is not a well-formed BCP-47 language tag",
            details=(ErrorDetail(field=field, reason="malformed language tag"),),
        )
    return value


def _workspace_id(database: Database) -> str:
    workspace_id = database.scalar("SELECT workspace_id FROM workspaces")
    if workspace_id is None:
        raise LinguaWikiError(
            "workspace_identity_missing", "the learner database has no workspace identity row"
        )
    return str(workspace_id)


def _read_user(database: Database, user_id: str) -> UserRecord:
    row = database.one(
        "SELECT user_id, workspace_id, display_name, timezone, status, created_at, updated_at "
        "FROM users WHERE user_id = ?",
        [user_id],
    )
    if row is None:
        raise LinguaWikiError(
            "user_not_found",
            f"no user with ID {user_id}",
            details=(ErrorDetail(field="user", reason="unknown user"),),
        )
    languages = database.query(
        "SELECT role, language_tag FROM user_languages WHERE user_id = ? "
        "ORDER BY role, preference_order",
        [user_id],
    )
    return UserRecord(
        user_id=str(row[0]),
        workspace_id=str(row[1]),
        display_name=str(row[2]),
        timezone=str(row[3]),
        status=str(row[4]),
        native_languages=tuple(str(tag) for role, tag in languages if role == "native"),
        support_languages=tuple(str(tag) for role, tag in languages if role == "support"),
        tracks=tuple(
            str(track_id)
            for (track_id,) in database.query(
                "SELECT track_id FROM learning_tracks WHERE user_id = ? ORDER BY created_at",
                [user_id],
            )
        ),
        created_at=str(aware_utc(row[5]).isoformat()),
        updated_at=str(aware_utc(row[6]).isoformat()),
    )


def _resolve_user(database: Database, user: str | None) -> str:
    """Resolve a user reference: an explicit ID, or the workspace's only learner."""

    if user is not None:
        return str(_read_user(database, user).user_id)
    rows = database.query("SELECT user_id FROM users WHERE status = 'active' ORDER BY created_at")
    if len(rows) == 1:
        return str(rows[0][0])
    raise LinguaWikiError(
        "user_selection_required",
        f"name a user explicitly: the workspace has {len(rows)} active learners",
        details=(ErrorDetail(field="user", reason="ambiguous user selection"),),
    )


def create_user(
    paths: WorkspacePaths,
    *,
    display_name: str,
    timezone: str,
    native_languages: Sequence[str],
    support_languages: Sequence[str] = (),
    clock: Clock | None = None,
    command: str = "user.create",
) -> UserRecord:
    """Create one learner. `user_id` scopes data; it is not an identity boundary."""

    active_clock = clock or SystemClock()
    if not display_name.strip():
        raise LinguaWikiError("invalid_arguments", "a learner needs a display name")
    validate_iana_timezone(timezone)
    if not native_languages:
        raise LinguaWikiError(
            "invalid_arguments",
            "declare at least one native language: explanation and contrast depend on it",
        )
    for tag in (*native_languages, *support_languages):
        assert_language_tag(tag, field="language")
    ordered_native = tuple(dict.fromkeys(native_languages))
    ordered_support = tuple(
        tag for tag in dict.fromkeys(support_languages) if tag not in ordered_native
    )
    user_id = UserId.new()
    correlation_id = EventId.new()
    with open_writer(paths, command=command, clock=active_clock) as database:
        workspace_id = _workspace_id(database)
        existing = database.scalar(
            "SELECT count(*) FROM users WHERE display_name = ?", [display_name]
        )
        if int(existing):
            raise LinguaWikiError(
                "user_exists",
                f"a learner named {display_name} already exists in this workspace",
                details=(ErrorDetail(field="display_name", reason="duplicate display name"),),
            )
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO users (user_id, workspace_id, display_name, timezone, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                [str(user_id), workspace_id, display_name, timezone, now, now],
            )
            for role, tags in (("native", ordered_native), ("support", ordered_support)):
                for order, tag in enumerate(tags, start=1):
                    transaction.execute(
                        "INSERT INTO user_languages (user_id, language_tag, role, "
                        "preference_order, created_at) VALUES (?, ?, ?, ?, ?)",
                        [str(user_id), tag, role, order, now],
                    )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([str(user_id)]),
                after_summary=f"created learner {display_name}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="user.created",
                aggregate_type="user",
                aggregate_id=str(user_id),
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {"timezone": timezone, "native_languages": list(ordered_native)},
                    sort_keys=True,
                ),
                idempotency_key=f"user.created:{user_id}",
            )
        return _read_user(database, str(user_id))


def update_user(
    paths: WorkspacePaths,
    *,
    user: str | None = None,
    display_name: str | None = None,
    timezone: str | None = None,
    native_languages: Sequence[str] | None = None,
    support_languages: Sequence[str] | None = None,
    status: str | None = None,
    clock: Clock | None = None,
    command: str = "user.update",
) -> UserRecord:
    """Change a learner's profile. Language roles are replaced as a whole set."""

    active_clock = clock or SystemClock()
    if timezone is not None:
        validate_iana_timezone(timezone)
    if status is not None and status not in ("active", "archived"):
        raise LinguaWikiError("invalid_arguments", "status must be active or archived")
    for tag in (*(native_languages or ()), *(support_languages or ())):
        assert_language_tag(tag, field="language")
    with open_writer(paths, command=command, clock=active_clock) as database:
        user_id = _resolve_user(database, user)
        current = _read_user(database, user_id)
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "UPDATE users SET display_name = ?, timezone = ?, status = ?, updated_at = ? "
                "WHERE user_id = ?",
                [
                    display_name or current.display_name,
                    timezone or current.timezone,
                    status or current.status,
                    now,
                    user_id,
                ],
            )
            if native_languages is not None or support_languages is not None:
                ordered_native = tuple(
                    dict.fromkeys(
                        native_languages
                        if native_languages is not None
                        else current.native_languages
                    )
                )
                ordered_support = tuple(
                    tag
                    for tag in dict.fromkeys(
                        support_languages
                        if support_languages is not None
                        else current.support_languages
                    )
                    if tag not in ordered_native
                )
                if not ordered_native:
                    raise LinguaWikiError(
                        "invalid_arguments", "a learner keeps at least one native language"
                    )
                transaction.execute("DELETE FROM user_languages WHERE user_id = ?", [user_id])
                for role, tags in (("native", ordered_native), ("support", ordered_support)):
                    for order, tag in enumerate(tags, start=1):
                        transaction.execute(
                            "INSERT INTO user_languages (user_id, language_tag, role, "
                            "preference_order, created_at) VALUES (?, ?, ?, ?, ?)",
                            [user_id, tag, role, order, now],
                        )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([user_id]),
                before_summary=f"{current.display_name} ({current.status})",
                after_summary=f"{display_name or current.display_name} "
                f"({status or current.status})",
            )
        return _read_user(database, user_id)


def show_user(
    paths: WorkspacePaths, *, user: str | None = None, clock: Clock | None = None
) -> UserRecord:
    with open_reader(paths, clock=clock or SystemClock()) as database:
        return _read_user(database, _resolve_user(database, user))


def list_users(paths: WorkspacePaths, *, clock: Clock | None = None) -> tuple[UserRecord, ...]:
    with open_reader(paths, clock=clock or SystemClock()) as database:
        return tuple(
            _read_user(database, str(user_id))
            for (user_id,) in database.query("SELECT user_id FROM users ORDER BY created_at")
        )


def _pack_for_language(
    database: Database,
    *,
    language: str,
    script: str | None,
    pack_key: str | None,
) -> tuple[Mapping[str, str], PackManifest, list[str]]:
    """Find the installed pack that serves this language, and check its capabilities."""

    row = pack_service.installed_pack(database, pack_key)
    manifest = PackManifest.model_validate(json.loads(row["manifest_json"]))
    requested, requested_script, _ = _language_subtags(language)
    pack_language, pack_script, _ = _language_subtags(manifest.language)
    if requested != pack_language:
        raise LinguaWikiError(
            "pack_language_mismatch",
            f"the installed pack {row['pack_key']} serves {manifest.language}, not {language}; "
            "install a pack for that language first",
            details=(
                ErrorDetail(
                    field="target_language",
                    reason="no installed pack serves this language",
                    context={"installed": manifest.language, "requested": language},
                ),
            ),
        )
    # A pack that declares exactly one script has already answered the question; only an
    # ambiguous pack leaves the script unset for the learner to choose.
    only_script = manifest.scripts[0] if len(manifest.scripts) == 1 else None
    chosen_script = script or requested_script or pack_script or only_script
    warnings: list[str] = []
    if chosen_script is not None and manifest.scripts and chosen_script not in manifest.scripts:
        raise LinguaWikiError(
            "pack_script_unsupported",
            f"{row['pack_key']} declares scripts {list(manifest.scripts)}, not {chosen_script}",
            details=(ErrorDetail(field="script", reason="script not declared by the pack"),),
        )
    if chosen_script is None and len(manifest.scripts) > 1:
        warnings.append(
            f"{row['pack_key']} supports {list(manifest.scripts)}; no script was chosen"
        )
    return {**row, "script_default": chosen_script or ""}, manifest, warnings


#: A track in this status is no longer taught, so its pack owes it nothing: its level
#: labels stay interpretable through the immutable global framework record.
ARCHIVED_STATUS = "archived"


def _assert_framework_level(
    database: Database,
    *,
    pack_id: str,
    framework_id: str,
    level: str | None,
    field: str,
) -> None:
    """Require a level label to belong to the track's own framework.

    A label from a different installed framework is refused by name. Core never relates
    two frameworks: only explicitly reviewed pack data may do that.
    """

    supported = {
        str(name)
        for (name,) in database.query(
            "SELECT framework_id FROM pack_frameworks WHERE pack_id = ?", [pack_id]
        )
    }
    if framework_id not in supported:
        raise LinguaWikiError(
            "framework_not_supported",
            f"the installed pack declares frameworks {sorted(supported)}, not {framework_id}",
            details=(ErrorDetail(field="framework", reason="framework not declared by the pack"),),
        )
    if level is None:
        return
    # The pack's own declared range, not the framework's accumulated one. The global
    # table only ever grows -- an agreed level order cannot be renegotiated -- so a pack
    # that narrowed its range would otherwise still appear to teach the levels it dropped.
    levels = {
        str(code)
        for (code,) in database.query(
            "SELECT level_code FROM pack_framework_levels WHERE pack_id = ? AND framework_id = ?",
            [pack_id, framework_id],
        )
    }
    if level in levels:
        return
    elsewhere = sorted(
        str(other)
        for (other,) in database.query(
            "SELECT DISTINCT framework_id FROM proficiency_framework_levels "
            "WHERE level_code = ? AND framework_id <> ?",
            [level, framework_id],
        )
    )
    dropped = bool(
        database.scalar(
            "SELECT count(*) FROM proficiency_framework_levels WHERE framework_id = ? "
            "AND level_code = ?",
            [framework_id, level],
        )
    )
    reason = (
        f"{level} is a level of {elsewhere}, not of {framework_id}; choose an installed "
        "framework or use an explicit reviewed pack mapping"
        if elsewhere
        else (
            f"the installed pack no longer teaches {framework_id} {level}; it declares "
            f"{sorted(levels)}"
            if dropped
            else f"{framework_id} has no level {level}"
        )
    )
    raise LinguaWikiError(
        "level_not_in_framework",
        reason,
        details=(
            ErrorDetail(
                field=field,
                reason="level does not belong to the selected framework",
                context={"framework": framework_id, "level": level, "other": ", ".join(elsewhere)},
            ),
        ),
    )


def _read_track(database: Database, track_id: str) -> TrackRecord:
    row = database.one(
        "SELECT track_id, user_id, target_language, region, script, proficiency_framework, "
        "declared_level, current_level, target_level, goal, status, is_primary, weekly_minutes, "
        "timezone, created_at, updated_at, pack_id FROM learning_tracks WHERE track_id = ?",
        [track_id],
    )
    if row is None:
        raise LinguaWikiError(
            "track_not_found",
            f"no learning track with ID {track_id}",
            details=(ErrorDetail(field="track", reason="unknown track"),),
        )
    framework_id = str(row[5])
    pack_id = None if row[16] is None else str(row[16])
    levels = tuple(
        str(code)
        for (code,) in database.query(
            "SELECT level_code FROM pack_framework_levels WHERE pack_id = ? "
            "AND framework_id = ? ORDER BY sequence",
            [pack_id, framework_id],
        )
    )
    if not levels:
        # An archived track whose pack has since moved to a replacement framework is the
        # ordinary case here; a track predating its pack binding is the other, and
        # `db check` reports that one by name. The immutable global framework record is
        # what keeps those labels interpretable, which is what it exists for.
        levels = tuple(
            str(code)
            for (code,) in database.query(
                "SELECT level_code FROM proficiency_framework_levels WHERE framework_id = ? "
                "ORDER BY sequence",
                [framework_id],
            )
        )
    preferences = {
        str(key): json.loads(str(value))
        for key, value in database.query(
            "SELECT key, value_json FROM track_preferences WHERE track_id = ? ORDER BY key",
            [track_id],
        )
    }
    # The track names the pack it was created from. Matching on language alone would hand a
    # track over to any later pack for the same language, which is exactly the material the
    # learner's history was never built against.
    installed = (
        None
        if pack_id is None
        else database.one(
            "SELECT pack.pack_key, installation.version, installation.maturity "
            "FROM language_packs pack "
            "JOIN pack_installations installation ON installation.pack_id = pack.pack_id "
            "WHERE pack.pack_id = ?",
            [pack_id],
        )
    )
    return TrackRecord(
        track_id=str(row[0]),
        user_id=str(row[1]),
        target_language=str(row[2]),
        region=None if row[3] is None else str(row[3]),
        script=None if row[4] is None else str(row[4]),
        proficiency_framework=framework_id,
        framework_levels=levels,
        declared_level=None if row[6] is None else str(row[6]),
        current_level=None if row[7] is None else str(row[7]),
        target_level=None if row[8] is None else str(row[8]),
        goal=None if row[9] is None else str(row[9]),
        status=str(row[10]),
        is_primary=bool(row[11]),
        weekly_minutes=None if row[12] is None else int(row[12]),
        timezone=str(row[13]),
        pack_id=pack_id,
        pack_key=None if installed is None else str(installed[0]),
        pack_version=None if installed is None else str(installed[1]),
        pack_maturity=None if installed is None else str(installed[2]),
        preferences=preferences,
        created_at=str(aware_utc(row[14]).isoformat()),
        updated_at=str(aware_utc(row[15]).isoformat()),
    )


def resolve_track(database: Database, track: str | None) -> str:
    """Resolve a track reference: an explicit ID, or the workspace's only track.

    A single active track is the ordinary case. When none is active, a workspace holding
    exactly one track still resolves it: otherwise a paused track could not be
    reactivated without quoting its identifier, which is the one moment a learner is
    least likely to have it.
    """

    if track is not None:
        return str(_read_track(database, track).track_id)
    active = database.query(
        "SELECT track_id FROM learning_tracks WHERE status = 'active' ORDER BY created_at"
    )
    if len(active) == 1:
        return str(active[0][0])
    if not active:
        all_tracks = database.query("SELECT track_id FROM learning_tracks ORDER BY created_at")
        if len(all_tracks) == 1:
            return str(all_tracks[0][0])
        raise LinguaWikiError(
            "track_selection_required",
            f"name a track explicitly: the workspace has no active track and "
            f"{len(all_tracks)} track(s) in total",
            details=(ErrorDetail(field="track", reason="ambiguous track selection"),),
        )
    raise LinguaWikiError(
        "track_selection_required",
        f"name a track explicitly: the workspace has {len(active)} active tracks",
        details=(ErrorDetail(field="track", reason="ambiguous track selection"),),
    )


def create_track(
    paths: WorkspacePaths,
    *,
    target_language: str,
    framework: str,
    user: str | None = None,
    pack_key: str | None = None,
    region: str | None = None,
    script: str | None = None,
    declared_level: str | None = None,
    target_level: str | None = None,
    goal: str | None = None,
    preferences: TrackPreferences | None = None,
    clock: Clock | None = None,
    command: str = "track.create",
) -> TrackRecord:
    """Bind one learner to one target language inside one proficiency framework."""

    active_clock = clock or SystemClock()
    assert_language_tag(target_language, field="target_language")
    if script is not None and not SCRIPT_PATTERN.match(script):
        raise LinguaWikiError(
            "invalid_arguments", f"{script} is not a BCP-47 script subtag such as Latn"
        )
    if region is not None and not REGION_PATTERN.match(region):
        raise LinguaWikiError(
            "invalid_arguments", f"{region} is not a BCP-47 region subtag such as PL"
        )
    settings = preferences or TrackPreferences()
    track_id = TrackId.new()
    correlation_id = EventId.new()
    with open_writer(paths, command=command, clock=active_clock) as database:
        user_id = _resolve_user(database, user)
        profile = _read_user(database, user_id)
        row, manifest, warnings = _pack_for_language(
            database, language=target_language, script=script, pack_key=pack_key
        )
        _assert_framework_level(
            database,
            pack_id=row["pack_id"],
            framework_id=framework,
            level=declared_level,
            field="declared_level",
        )
        _assert_framework_level(
            database,
            pack_id=row["pack_id"],
            framework_id=framework,
            level=target_level,
            field="target_level",
        )
        chosen_script = row["script_default"] or None
        # Archived tracks do not occupy the slot. A track cannot be moved between
        # frameworks, so archiving one and creating its replacement is the only way a
        # learner can follow a pack onto a new framework -- and counting the archived
        # track as a duplicate made that workflow impossible to complete.
        duplicate = database.one(
            "SELECT track_id FROM learning_tracks WHERE user_id = ? AND target_language = ? "
            "AND coalesce(region, '') = ? AND coalesce(script, '') = ? "
            f"AND status <> '{ARCHIVED_STATUS}'",
            [user_id, target_language, region or "", chosen_script or ""],
        )
        if duplicate is not None:
            raise LinguaWikiError(
                "track_exists",
                f"this learner already has a {target_language} track ({duplicate[0]}); "
                "archive it first if it is being replaced",
                details=(ErrorDetail(field="track", reason="duplicate track"),),
            )
        has_primary = int(
            database.scalar(
                "SELECT count(*) FROM learning_tracks WHERE user_id = ? AND is_primary", [user_id]
            )
        )
        unsupported_languages = [
            tag
            for tag in profile.support_languages
            if manifest.support_languages and tag not in manifest.support_languages
        ]
        if unsupported_languages:
            warnings.append(
                f"{row['pack_key']} has no material for support language(s) "
                f"{unsupported_languages}; explanations will fall back to the pack's own "
                f"languages {list(manifest.support_languages)}"
            )
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "INSERT INTO learning_tracks (track_id, user_id, target_language, region, script, "
                "proficiency_framework, declared_level, current_level, target_level, goal, "
                "status, is_primary, weekly_minutes, timezone, pack_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                [
                    str(track_id),
                    user_id,
                    target_language,
                    region,
                    chosen_script,
                    framework,
                    declared_level,
                    target_level,
                    goal,
                    not has_primary,
                    settings.weekly_minutes,
                    profile.timezone,
                    row["pack_id"],
                    now,
                    now,
                ],
            )
            for key, value in settings.rows().items():
                transaction.execute(
                    "INSERT INTO track_preferences (track_id, key, value_json, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    [str(track_id), key, value, now],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([str(track_id)]),
                after_summary=f"created {target_language} track in {framework}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="track.created",
                aggregate_type="track",
                aggregate_id=str(track_id),
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {
                        "target_language": target_language,
                        "framework": framework,
                        "declared_level": declared_level,
                        "pack_key": row["pack_key"],
                        "pack_version": row["version"],
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"track.created:{track_id}",
            )
        record = _read_track(database, str(track_id))
        return record.model_copy(update={"warnings": tuple(warnings)})


def update_track(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    goal: str | None = None,
    target_level: str | None = None,
    declared_level: str | None = None,
    preferences: TrackPreferences | None = None,
    clock: Clock | None = None,
    command: str = "track.update",
) -> TrackRecord:
    """Change a track's goal, target, or preferences.

    `current_level` is never set here: a level the learner *holds* comes from evidence,
    which Stage 3 computes, and a declared level stays a hypothesis.

    Withdrawing a retention consent reaches the answers still waiting for a judge, in this
    same transaction (`withdrawal.on_consent_change`): a recording the learner no longer
    agrees to keep is purged, a written answer's text is discarded, and the verdicts held
    for them are voided. The returned record's warnings name each one.
    """

    from linguawiki.services import withdrawal

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = resolve_track(database, track)
        current = _read_track(database, track_id)
        row = pack_service.installed_pack(database, current.pack_key or None)
        for value, field in ((declared_level, "declared_level"), (target_level, "target_level")):
            _assert_framework_level(
                database,
                pack_id=row["pack_id"],
                framework_id=current.proficiency_framework,
                level=value,
                field=field,
            )
        settings = preferences.rows() if preferences is not None else {}
        before = dict(current.preferences)
        after = {**before, **{key: json.loads(value) for key, value in settings.items()}}
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "UPDATE learning_tracks SET goal = ?, target_level = ?, declared_level = ?, "
                "weekly_minutes = ?, updated_at = ? WHERE track_id = ?",
                [
                    goal if goal is not None else current.goal,
                    target_level if target_level is not None else current.target_level,
                    declared_level if declared_level is not None else current.declared_level,
                    (
                        preferences.weekly_minutes
                        if preferences is not None and preferences.weekly_minutes is not None
                        else current.weekly_minutes
                    ),
                    now,
                    track_id,
                ],
            )
            for key, value in settings.items():
                transaction.execute(
                    "INSERT INTO track_preferences (track_id, key, value_json, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (track_id, key) DO UPDATE SET "
                    "value_json = excluded.value_json, updated_at = excluded.updated_at",
                    [track_id, key, value, now],
                )
            settled = withdrawal.on_consent_change(
                transaction,
                paths.root,
                track_id=track_id,
                before=before,
                after=after,
                command=command,
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps(
                    [
                        track_id,
                        *sorted(
                            {
                                *(entry.submission_id for entry in settled.withdrawn),
                                *settled.voided_verdicts,
                            }
                        ),
                    ]
                ),
                before_summary=f"goal={current.goal}, target={current.target_level}",
                after_summary=f"goal={goal or current.goal}, "
                f"target={target_level or current.target_level}"
                + (
                    "; consent withdrawn, so withdrew "
                    + ", ".join(
                        f"{entry.submission_id} ({entry.code})" for entry in settled.withdrawn
                    )
                    if settled.withdrawn
                    else ""
                ),
            )
        record = _read_track(database, track_id)
        warnings = [
            f"withdrew submission {entry.submission_id} ({entry.content_id}) as {entry.code}: "
            f"{entry.reason}"
            + (
                f"; voided held verdict(s) {', '.join(entry.voided_verdicts)}"
                if entry.voided_verdicts
                else ""
            )
            for entry in settled.withdrawn
        ]
        if not warnings:
            return record
        return record.model_copy(update={"warnings": (*record.warnings, *warnings)})


def _assert_revivable(database: Database, current: TrackRecord) -> None:
    """Refuse to bring an archived track back into a world it no longer fits.

    Archiving is how a track is *replaced*: it frees the language slot and is exempt from
    the pack's framework and level guards. Reviving one therefore has to re-establish
    everything archiving let go of, or the workspace ends up in a state `db check`
    rejects -- two live tracks for one language -- or a state no pack vouches for, with a
    live track taught in a framework or at a level its pack has since dropped.
    """

    occupant = database.one(
        "SELECT track_id FROM learning_tracks WHERE user_id = ? AND target_language = ? "
        "AND coalesce(region, '') = ? AND coalesce(script, '') = ? AND track_id <> ? "
        f"AND status <> '{ARCHIVED_STATUS}'",
        [
            current.user_id,
            current.target_language,
            current.region or "",
            current.script or "",
            current.track_id,
        ],
    )
    if occupant is not None:
        raise LinguaWikiError(
            "track_slot_taken",
            f"{current.target_language} is already taught by a live track "
            f"({occupant[0]}); archive that one before reviving {current.track_id}",
            details=(
                ErrorDetail(
                    field="track",
                    reason="another live track holds this language slot",
                    context={"occupant": str(occupant[0])},
                ),
            ),
        )
    if current.pack_id is None:
        raise LinguaWikiError(
            "track_pack_unbound",
            f"{current.track_id} names no pack, so there is nothing to check it against; "
            "create a new track instead",
            details=(ErrorDetail(field="track", reason="track has no pack binding"),),
        )
    _assert_framework_level(
        database,
        pack_id=current.pack_id,
        framework_id=current.proficiency_framework,
        level=None,
        field="proficiency_framework",
    )
    for field, level in (
        ("declared_level", current.declared_level),
        ("current_level", current.current_level),
        ("target_level", current.target_level),
    ):
        _assert_framework_level(
            database,
            pack_id=current.pack_id,
            framework_id=current.proficiency_framework,
            level=level,
            field=field,
        )


def set_track_status(
    paths: WorkspacePaths,
    *,
    status: str,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "track.activate",
) -> TrackRecord:
    """Activate, pause, or archive a track."""

    if status not in ("active", "paused", "archived"):
        raise LinguaWikiError("invalid_arguments", "status must be active, paused, or archived")
    with open_writer(paths, command=command, clock=clock or SystemClock()) as database:
        track_id = resolve_track(database, track)
        current = _read_track(database, track_id)
        if current.status == ARCHIVED_STATUS and status != ARCHIVED_STATUS:
            _assert_revivable(database, current)
        with database.transaction() as transaction:
            now = transaction.now()
            transaction.execute(
                "UPDATE learning_tracks SET status = ?, updated_at = ? WHERE track_id = ?",
                [status, now, track_id],
            )
            if status == "archived" and current.is_primary:
                # The primary flag has to move, or 'db check' would see a user whose only
                # primary program is archived.
                successor = transaction.one(
                    "SELECT track_id FROM learning_tracks WHERE user_id = ? AND track_id <> ? "
                    "AND status = 'active' ORDER BY created_at",
                    [current.user_id, track_id],
                )
                transaction.execute(
                    "UPDATE learning_tracks SET is_primary = FALSE, updated_at = ? "
                    "WHERE track_id = ?",
                    [now, track_id],
                )
                if successor is not None:
                    transaction.execute(
                        "UPDATE learning_tracks SET is_primary = TRUE, updated_at = ? "
                        "WHERE track_id = ?",
                        [now, str(successor[0])],
                    )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([track_id]),
                before_summary=current.status,
                after_summary=status,
            )
        return _read_track(database, track_id)


def show_track(
    paths: WorkspacePaths, *, track: str | None = None, clock: Clock | None = None
) -> TrackRecord:
    with open_reader(paths, clock=clock or SystemClock()) as database:
        return _read_track(database, resolve_track(database, track))


def list_tracks(paths: WorkspacePaths, *, clock: Clock | None = None) -> tuple[TrackRecord, ...]:
    with open_reader(paths, clock=clock or SystemClock()) as database:
        return tuple(
            _read_track(database, str(track_id))
            for (track_id,) in database.query(
                "SELECT track_id FROM learning_tracks ORDER BY created_at"
            )
        )


def track_context(database: Database, track_id: str) -> TrackRecord:
    """A track record read inside a caller's connection."""

    return _read_track(database, track_id)


class RecordingPolicy(ContractModel):
    """Whether this track lets a learner record an answer for a judge, and for how long
    the recording is kept.

    `offered` is the gate: equipment *and* consent, never one standing in for the other.
    The retention policy is reported so a page can say it, and never overridden.
    """

    offered: bool
    reason: str | None = None
    recording_available: bool = False
    retention_consent: bool = False
    retention_policy: str = "keep"
    retention_days: int | None = None


def recording_policy(preferences: Mapping[str, object]) -> RecordingPolicy:
    available = preferences.get("audio_recording_available") is True
    consented = preferences.get("audio_retention_consent") is True
    days = preferences.get("audio_retention_days")
    reason = None
    if not available and not consented:
        reason = "this track has not said it can record audio, nor agreed to a recording being kept"
    elif not available:
        reason = "this track has not said it can record audio"
    elif not consented:
        reason = (
            "this track has not agreed to a recording being kept, and a judge needs the "
            "recording to still exist"
        )
    return RecordingPolicy(
        offered=recording_permitted(preferences),
        reason=reason,
        recording_available=available,
        retention_consent=consented,
        retention_policy=str(preferences.get("audio_retention_policy") or "keep"),
        retention_days=None if days is None else int(str(days)),
    )


def track_recording_policy(database: Database, track_id: str) -> RecordingPolicy:
    """The recording gate for one track, from the preferences it holds now."""

    record = track_context(database, track_id)
    preferences = record.preferences if isinstance(record.preferences, dict) else {}
    return recording_policy(preferences)


__all__ = [
    "CORRECTION_MODES",
    "RecordingPolicy",
    "TrackPreferences",
    "TrackRecord",
    "UserRecord",
    "create_track",
    "create_user",
    "list_tracks",
    "list_users",
    "recording_policy",
    "resolve_track",
    "set_track_status",
    "show_track",
    "show_user",
    "track_context",
    "track_recording_policy",
    "update_track",
    "update_user",
]
