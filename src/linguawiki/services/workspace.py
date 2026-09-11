"""Learner-workspace generation, status, and diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.parse import urlsplit, urlunsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from pydantic import Field

from linguawiki import __version__, resources
from linguawiki.clock import Clock, SystemClock, aware_utc, naive_utc
from linguawiki.contracts import (
    LockManifest,
    PackPin,
    VersionPin,
    WorkspaceManifest,
    WorkspaceRuntime,
)
from linguawiki.db import locks
from linguawiki.db import migrations as migration_module
from linguawiki.db.backup import (
    backup_root_git_problems,
    resolve_backup_root,
    table_row_counts,
)
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.db.integrity import CheckResult, check_database
from linguawiki.db.state import DatabaseState, classify_database, schema_divergence
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId, WorkspaceId
from linguawiki.models import ContractModel
from linguawiki.paths import (
    PRIVATE_DIRECTORIES,
    WorkspacePaths,
    assert_safe_destructive_target,
    assert_within,
    directory_is_empty,
    find_core_repository_root,
    require_initialized_workspace,
    resolve_path,
    workspace_paths,
)
from linguawiki.services import wiki
from linguawiki.services.privacy import (
    PrivacyReport,
    check_privacy,
    missing_ignore_rules,
    workspace_policy,
)
from linguawiki.services.skills import SkillBundleReport, inspect_bundle, install_bundle
from linguawiki.versions import core_pin, skill_bundle_pin
from linguawiki.workspace_template import WorkspaceTemplateContext, render_workspace_contracts

# The frozen lingua.workspace.v1 contract admits one history policy. The other
# policies documented in the plan need a contract amendment before they can be stored.
SUPPORTED_HISTORY_POLICIES = ("git-wiki",)
DOCUMENTED_HISTORY_POLICIES = (
    "git-wiki",
    "portable-snapshot",
    "git-portable-snapshot",
    "local-only",
)
TEMPLATE_TARGETS = {
    ".gitignore.j2": ".gitignore",
    "AGENTS.md.j2": "AGENTS.md",
    "linguawiki.toml.j2": "linguawiki.toml",
    "pyproject.toml.j2": "pyproject.toml",
}
STAGING_SUFFIX = ".linguawiki-init-"
REMOTE_CONFIRMATION_KEY = "remote_privacy_confirmed"
WORKSPACE_SETTINGS_SCOPE = "workspace"


class WorkspaceInitReport(ContractModel):
    workspace: str
    workspace_id: str
    name: str
    created: bool
    history_policy: str
    timezone: str
    backup_root: str
    database: str
    database_schema_version: int
    applied_migrations: tuple[str, ...] = ()
    generated_files: tuple[str, ...] = ()
    wiki_files: tuple[str, ...] = ()
    skills: SkillBundleReport
    git_initialized: bool = False
    dependency_lock: bool = False
    warnings: tuple[str, ...] = ()


class WorkspaceStatusReport(ContractModel):
    workspace: str
    workspace_id: str
    name: str
    normalized_name: str
    created_at: str
    timezone: str
    history_policy: str
    track_policy: str
    backup_root: str
    core: VersionPin
    database_schema: VersionPin
    skill_bundle: VersionPin
    packs: tuple[PackPin, ...] = ()
    database: str
    database_present: bool
    applied_schema_version: int
    packaged_schema_version: int
    writer_lock_held: bool
    users: int
    tracks: int
    row_counts: dict[str, int] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class DoctorReport(ContractModel):
    ok: bool
    workspace: str
    checks: tuple[CheckResult, ...]
    warnings: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.status == "failed")


@dataclass(frozen=True, slots=True)
class InitOptions:
    """Everything `workspace init` needs; nothing is inferred silently."""

    path: str | Path
    backup_root: str | Path
    name: str | None = None
    timezone: str = "UTC"
    history_policy: str = "git-wiki"
    git_init: bool = False
    uv_lock: bool = False
    find_links: str | Path | None = None


def normalize_name(name: str) -> str:
    """Derive the workspace package slug without deriving identity from it."""

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise LinguaWikiError(
            "invalid_workspace_name",
            f"workspace name has no alphanumeric characters: {name!r}",
            details=(ErrorDetail(field="name", reason="cannot be normalized"),),
        )
    return slug


def _ok(name: str, message: str, **context: str) -> CheckResult:
    return CheckResult(name=name, status="ok", message=message, context=context)


def _failed(name: str, message: str, **context: str) -> CheckResult:
    return CheckResult(name=name, status="failed", message=message, context=context)


def _warning(name: str, message: str, **context: str) -> CheckResult:
    return CheckResult(name=name, status="warning", message=message, context=context)


def load_configuration(paths: WorkspacePaths) -> WorkspaceManifest:
    try:
        payload = tomllib.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LinguaWikiError(
            "workspace_configuration_invalid", f"{paths.config} could not be parsed"
        ) from exc
    return WorkspaceManifest.model_validate(payload)


def load_lock(paths: WorkspacePaths) -> LockManifest:
    if not paths.lock.is_file():
        raise LinguaWikiError(
            "workspace_lock_missing",
            f"{paths.lock} is missing; the workspace is not fully initialized",
        )
    try:
        payload = json.loads(paths.lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LinguaWikiError(
            "workspace_lock_invalid", f"{paths.lock} could not be parsed"
        ) from exc
    return LockManifest.model_validate(payload)


def write_lock(paths: WorkspacePaths, lock: LockManifest) -> None:
    """Write `linguawiki.lock` atomically so a crash never leaves a partial pin."""

    target = assert_within(paths.lock, paths.root, purpose="lock file")
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(lock.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def template_context(
    *,
    name: str,
    configuration: WorkspaceManifest,
    lock: LockManifest,
) -> WorkspaceTemplateContext:
    return WorkspaceTemplateContext(
        workspace_name=name,
        normalized_workspace_name=normalize_name(name),
        workspace_id=configuration.workspace_id,
        created_at=configuration.created_at,
        timezone=configuration.timezone,
        backup_root=configuration.backup_root,
        core_version=lock.core.version,
        core_sha256=lock.core.sha256,
        database_schema_version=lock.database_schema.version,
        database_schema_sha256=lock.database_schema.sha256,
        skill_bundle_version=lock.skill_bundle.version,
        skill_bundle_sha256=lock.skill_bundle.sha256,
    )


def render_generated_files(context: WorkspaceTemplateContext) -> dict[str, str]:
    """Render every generated workspace file keyed by its final relative name."""

    rendered = render_workspace_contracts(resources.workspace_template_directory(), context)
    files = {
        TEMPLATE_TARGETS[name]: body
        for name, body in rendered.files.items()
        if name in TEMPLATE_TARGETS
    }
    return files


def modified_generated_files(
    paths: WorkspacePaths, context: WorkspaceTemplateContext
) -> tuple[str, ...]:
    """Detect hand edits by re-rendering the templates from persisted state."""

    differences: list[str] = []
    for relative, expected in sorted(render_generated_files(context).items()):
        path = paths.root / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            differences.append(relative)
    return tuple(differences)


def _workspace_row(database: Database) -> tuple[str, str, str, str, str, datetime]:
    row = database.one(
        "SELECT workspace_id, name, normalized_name, history_policy, track_policy, created_at "
        "FROM workspaces"
    )
    if row is None:
        raise LinguaWikiError(
            "workspace_identity_missing", "the learner database has no workspace identity row"
        )
    return (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        aware_utc(row[5]),
    )


def mirror_versions(
    database: Database, *, workspace_id: WorkspaceId, lock: LockManifest, audit_event_id: str | None
) -> None:
    """Mirror the lock file inside DuckDB so integrity checks have two sources."""

    now = database.now()
    components: list[tuple[str, str, VersionPin]] = [
        ("core", "core", lock.core),
        ("database_schema", "database_schema", lock.database_schema),
        ("skill_bundle", "skill_bundle", lock.skill_bundle),
    ]
    components.extend(("pack", str(pack.pack_id), pack) for pack in lock.packs)
    database.execute("DELETE FROM workspace_versions WHERE workspace_id = ?", [str(workspace_id)])
    for component, key, pin in components:
        database.execute(
            "INSERT INTO workspace_versions "
            "(workspace_id, component, component_key, version, sha256, applied_at, audit_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [str(workspace_id), component, key, pin.version, pin.sha256, now, audit_event_id],
        )


def _record_identity(
    database: Database,
    *,
    configuration: WorkspaceManifest,
    name: str,
    lock: LockManifest,
    correlation_id: EventId,
) -> None:
    """Insert workspace identity, version mirror, and projection state in one transaction."""

    with database.transaction() as transaction:
        now = transaction.now()
        transaction.execute(
            "INSERT INTO workspaces (workspace_id, name, normalized_name, history_policy, "
            "track_policy, timezone, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(configuration.workspace_id),
                name,
                normalize_name(name),
                configuration.history_policy,
                "single",
                configuration.timezone,
                naive_utc(configuration.created_at),
                now,
            ],
        )
        transaction.execute(
            "INSERT INTO projection_state (projection, projection_version, stale, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [wiki.PROJECTION_NAME, wiki.PROJECTION_VERSION, True, now],
        )
        transaction.execute(
            "INSERT INTO settings (scope, scope_id, key, value_json, schema_version, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                WORKSPACE_SETTINGS_SCOPE,
                str(configuration.workspace_id),
                REMOTE_CONFIRMATION_KEY,
                "{}",
                1,
                now,
            ],
        )
        audit_id = migration_module.record_audit_entry(
            transaction,
            command="workspace.init",
            correlation_id=correlation_id,
            outcome="succeeded",
            affected_records_json=json.dumps([str(configuration.workspace_id)]),
            after_summary=f"initialized workspace {name}",
        )
        migration_module.record_domain_event(
            transaction,
            event_type="workspace.initialized",
            aggregate_type="workspace",
            aggregate_id=str(configuration.workspace_id),
            correlation_id=correlation_id,
            payload_json=json.dumps(
                {"history_policy": configuration.history_policy, "core_version": lock.core.version},
                sort_keys=True,
            ),
            idempotency_key=f"workspace.initialized:{configuration.workspace_id}",
        )
        mirror_versions(
            transaction,
            workspace_id=configuration.workspace_id,
            lock=lock,
            audit_event_id=str(audit_id),
        )


def _mark_projection_generated(database: Database, *, content_hash: str) -> None:
    with database.transaction() as transaction:
        now = transaction.now()
        transaction.execute(
            "UPDATE projection_state SET content_hash = ?, stale = FALSE, generated_at = ?, "
            "updated_at = ? WHERE projection = ?",
            [content_hash, now, now, wiki.PROJECTION_NAME],
        )


def _assert_outside_core_repository(paths: WorkspacePaths) -> None:
    core_root = find_core_repository_root(paths.root)
    if core_root is not None:
        raise LinguaWikiError(
            "workspace_inside_core_repository",
            "a learner workspace must not live inside the LinguaWiki core repository",
            details=(
                ErrorDetail(
                    field="workspace",
                    reason="target is inside the core repository",
                    context={"workspace": str(paths.root), "core_repository": str(core_root)},
                ),
            ),
        )


def _validate_history_policy(policy: str) -> Literal["git-wiki"]:
    if policy == "git-wiki":
        return "git-wiki"
    if policy in DOCUMENTED_HISTORY_POLICIES:
        raise LinguaWikiError(
            "unsupported_history_policy",
            f"history policy {policy!r} needs a lingua.workspace.v1 contract amendment",
            details=(
                ErrorDetail(
                    field="history_policy",
                    reason="not supported by the frozen workspace contract",
                    context={"supported": ", ".join(SUPPORTED_HISTORY_POLICIES)},
                ),
            ),
        )
    raise LinguaWikiError(
        "unsupported_history_policy",
        f"unknown history policy {policy!r}",
        details=(
            ErrorDetail(
                field="history_policy",
                reason="unknown policy",
                context={"documented": ", ".join(DOCUMENTED_HISTORY_POLICIES)},
            ),
        ),
    )


def _existing_workspace_report(
    paths: WorkspacePaths, options: InitOptions, backup_root: Path, warnings: tuple[str, ...]
) -> WorkspaceInitReport:
    """Return an idempotent no-op report, or refuse a modified workspace."""

    configuration = load_configuration(paths)
    lock = load_lock(paths)
    with open_reader(paths) as database:
        workspace_id, name, _, history_policy, _, _ = _workspace_row(database)
        schema_version = migration_module.applied_version(database)
    if str(configuration.workspace_id) != workspace_id:
        raise LinguaWikiError(
            "workspace_modified",
            "linguawiki.toml and the learner database disagree about the workspace identity",
        )
    if str(backup_root) != configuration.backup_root:
        raise LinguaWikiError(
            "workspace_already_initialized",
            "this workspace is already initialized with a different backup root",
            details=(
                ErrorDetail(
                    field="backup_root",
                    reason="differs from the recorded value",
                    context={"recorded": configuration.backup_root, "requested": str(backup_root)},
                ),
            ),
        )
    if options.name is not None and options.name != name:
        raise LinguaWikiError(
            "workspace_already_initialized",
            "this workspace is already initialized under a different name",
            details=(
                ErrorDetail(
                    field="name",
                    reason="differs from the recorded value",
                    context={"recorded": name, "requested": options.name},
                ),
            ),
        )
    context = template_context(name=name, configuration=configuration, lock=lock)
    modified = modified_generated_files(paths, context)
    skills = inspect_bundle(paths, lock=lock)
    if modified or skills.modified or skills.missing or skills.unexpected:
        raise LinguaWikiError(
            "workspace_modified",
            "refusing to reinitialize a workspace whose generated files were modified",
            details=tuple(
                ErrorDetail(field=name, reason="differs from the generated content")
                for name in (*modified, *skills.modified, *skills.missing, *skills.unexpected)
            ),
        )
    return WorkspaceInitReport(
        workspace=str(paths.root),
        workspace_id=workspace_id,
        name=name,
        created=False,
        history_policy=history_policy,
        timezone=configuration.timezone,
        backup_root=configuration.backup_root,
        database=str(paths.database),
        database_schema_version=schema_version,
        generated_files=tuple(sorted(TEMPLATE_TARGETS.values())),
        wiki_files=tuple(
            sorted(
                path.relative_to(paths.root).as_posix()
                for path in paths.wiki.rglob("*")
                if path.is_file()
            )
        ),
        skills=skills,
        warnings=(*warnings, "workspace was already initialized; nothing changed"),
    )


def staging_prefix(paths: WorkspacePaths) -> str:
    """Name prefix of the sibling directory a workspace is built in."""

    return f".{paths.root.name}{STAGING_SUFFIX}"


def abandoned_staging_directories(paths: WorkspacePaths) -> tuple[Path, ...]:
    """Staging directories left by interrupted runs. They are reported, never deleted."""

    parent = paths.root.parent
    if not parent.is_dir():
        return ()
    prefix = staging_prefix(paths)
    return tuple(
        sorted(path for path in parent.iterdir() if path.is_dir() and path.name.startswith(prefix))
    )


def create_staging_directory(paths: WorkspacePaths) -> Path:
    """Create a staging directory owned solely by this invocation.

    The name is unique, so a concurrent initializer cannot claim it and no
    pre-existing path is ever taken over or removed.
    """

    paths.root.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=staging_prefix(paths), dir=paths.root.parent))


def _build_workspace(
    staging: WorkspacePaths,
    *,
    name: str,
    configuration: WorkspaceManifest,
    lock: LockManifest,
    correlation_id: EventId,
    clock: Clock,
    git_init: bool,
    uv_lock: bool,
    find_links: str | Path | None,
) -> tuple[SkillBundleReport, tuple[str, ...], tuple[str, ...], int, bool, tuple[str, ...]]:
    """Render and populate a complete workspace inside the staging directory."""

    context = template_context(name=name, configuration=configuration, lock=lock)
    for relative, body in sorted(render_generated_files(context).items()):
        target = assert_within(staging.root / relative, staging.root, purpose="generated file")
        target.write_text(body, encoding="utf-8")
    for relative in PRIVATE_DIRECTORIES:
        assert_within(staging.root / relative, staging.root, purpose="private directory").mkdir(
            parents=True, exist_ok=True
        )
    skills = install_bundle(staging, clock=clock)
    with open_writer(staging, command="workspace.init", clock=clock, create=True) as database:
        applied = migration_module.migrate(database)
        _record_identity(
            database,
            configuration=configuration,
            name=name,
            lock=lock,
            correlation_id=correlation_id,
        )
        wiki_files = wiki.write_initial_projection(
            staging, workspace_name=name, created_at=configuration.created_at
        )
        _mark_projection_generated(
            database, content_hash=wiki.projection_content_hash(staging.root)
        )
        schema_version = migration_module.applied_version(database)
    write_lock(staging, lock)
    warnings: list[str] = []
    if uv_lock:
        try:
            write_uv_lock(staging, find_links=find_links)
        except LinguaWikiError as exc:
            # The workspace itself is complete and valid; only the optional lock failed.
            warnings.append(
                f"{exc.payload.message} (run 'linguawiki workspace lock-dependencies' to retry)"
            )
    # Git is initialized before publication so an interrupted run publishes nothing and
    # the retry is an ordinary first initialization.
    git_initialized = _git_init(staging) if git_init else False
    return (
        skills,
        tuple(migration.migration_id for migration in applied),
        wiki_files,
        schema_version,
        git_initialized,
        tuple(warnings),
    )


def initialize(options: InitOptions, *, clock: Clock | None = None) -> WorkspaceInitReport:
    """Render an independent learner workspace and publish it atomically.

    Everything is built in a staging sibling directory and moved into place with a
    single rename, so an interrupted run leaves the target untouched and the next
    attempt is an ordinary first initialization rather than a half-built workspace.
    """

    active_clock = clock or SystemClock()
    paths = workspace_paths(options.path)
    assert_safe_destructive_target(paths.root, purpose="workspace")
    _assert_outside_core_repository(paths)
    history_policy = _validate_history_policy(options.history_policy)
    backup_root = resolve_backup_root(options.backup_root, workspace_root=paths.root)
    warnings: list[str] = []
    if paths.config.is_file():
        return _existing_workspace_report(paths, options, backup_root, tuple(warnings))
    if paths.root.exists() and not directory_is_empty(paths.root):
        raise LinguaWikiError(
            "target_not_empty",
            f"workspace target must be empty or an initialized workspace: {paths.root}",
            details=(ErrorDetail(field="workspace", reason="directory is not empty"),),
        )
    name = options.name or paths.root.name
    normalize_name(name)
    backup_root.mkdir(parents=True, exist_ok=True)
    for abandoned in abandoned_staging_directories(paths):
        warnings.append(
            f"an interrupted run left a staging directory behind; remove it once you have "
            f"checked it: {abandoned}"
        )
    staging = WorkspacePaths(root=create_staging_directory(paths))
    correlation_id = EventId.new()
    configuration = WorkspaceManifest(
        workspace_id=WorkspaceId.new(),
        created_at=active_clock.now(),
        timezone=options.timezone,
        history_policy=history_policy,
        backup_root=str(backup_root),
        runtime=WorkspaceRuntime(python=">=3.12", core_version=__version__),
    )
    lock = LockManifest(
        core=core_pin(),
        database_schema=migration_module.database_schema_pin(),
        skill_bundle=skill_bundle_pin(),
    )
    try:
        skills, applied, wiki_files, schema_version, git_initialized, lock_warnings = (
            _build_workspace(
                staging,
                name=name,
                configuration=configuration,
                lock=lock,
                correlation_id=correlation_id,
                clock=active_clock,
                git_init=options.git_init,
                uv_lock=options.uv_lock,
                find_links=options.find_links,
            )
        )
        # mkdtemp creates the directory 0700 and the mode survives the rename, so a
        # learner workspace is private to its owner from the moment it is published.
        os.replace(staging.root, paths.root)
    except BaseException:
        # Only ever the directory this invocation created.
        shutil.rmtree(staging.root, ignore_errors=True)
        raise
    warnings.extend(lock_warnings)
    privacy = check_privacy(paths.root)
    return WorkspaceInitReport(
        workspace=str(paths.root),
        workspace_id=str(configuration.workspace_id),
        name=name,
        created=True,
        history_policy=history_policy,
        timezone=configuration.timezone,
        backup_root=str(backup_root),
        database=str(paths.database),
        database_schema_version=schema_version,
        applied_migrations=applied,
        generated_files=tuple(sorted(TEMPLATE_TARGETS.values())),
        wiki_files=wiki_files,
        skills=skills,
        git_initialized=git_initialized,
        dependency_lock=paths.dependency_lock.is_file(),
        warnings=(*warnings, *privacy.warnings),
    )


def write_uv_lock(paths: WorkspacePaths, *, find_links: str | Path | None = None) -> None:
    """Resolve the workspace's exact dependency set with uv.

    The workspace pins an exact core version rather than a path, so uv has to be able
    to reach that artifact: from an index, or from `find_links` for a release that is
    built but not yet published. Failure is reported, never papered over.
    """

    if shutil.which("uv") is None:
        raise LinguaWikiError(
            "uv_unavailable",
            "uv is not installed, so the workspace dependency lock cannot be resolved",
            details=(ErrorDetail(field="uv_lock", reason="uv is not on PATH"),),
        )
    command = ["uv", "lock", "--project", str(paths.root)]
    if find_links is not None:
        command.extend(["--find-links", str(resolve_path(find_links, purpose="find_links"))])
    result = subprocess.run(
        command,
        cwd=paths.root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LinguaWikiError(
            "uv_lock_failed",
            f"uv could not resolve {paths.dependency_lock}; is core {__version__} reachable "
            "from an index or --find-links?",
            details=(
                ErrorDetail(
                    field="uv_lock",
                    reason="uv lock failed",
                    context={"stderr": result.stderr.strip()[:500]},
                ),
            ),
        )
    problem = inspect_dependency_lock(paths, load_lock(paths))
    if problem is not None:
        raise LinguaWikiError(
            "uv_lock_failed",
            f"uv wrote a lock that {problem.reason}",
            details=(
                ErrorDetail(
                    field="uv_lock", reason=problem.reason, context={"detail": problem.detail}
                ),
            ),
        )
    verdict = uv_lock_verdict(paths)
    if verdict is not None and not verdict[0]:
        raise LinguaWikiError(
            "uv_lock_failed",
            f"{paths.dependency_lock} is not up to date with the workspace pyproject.toml",
            details=(
                ErrorDetail(
                    field="uv_lock", reason="uv lock --check failed", context={"detail": verdict[1]}
                ),
            ),
        )


def _git_init(paths: WorkspacePaths) -> bool:
    """Initialize a local repository only; never add a remote, commit, or push."""

    if shutil.which("git") is None:
        raise LinguaWikiError("git_unavailable", "git is not installed; skip --git-init")
    if (paths.root / ".git").exists():
        return True
    subprocess.run(["git", "init", "--quiet"], cwd=paths.root, check=True)
    return True


SCP_REMOTE_PATTERN = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")


def sanitize_remote_url(url: str) -> str:
    """Drop credentials from a remote URL so it is safe to persist and display."""

    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    scp = SCP_REMOTE_PATTERN.match(url)
    if scp is not None and not Path(url).exists():
        return f"{scp['host']}:{scp['path']}"
    return url


def remote_fingerprint(url: str) -> str:
    """Identify a remote endpoint without storing the URL or its credentials."""

    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _endpoint(url: str) -> dict[str, str]:
    return {"display": sanitize_remote_url(url), "fingerprint": remote_fingerprint(url)}


def _git(root: Path, *arguments: str) -> list[str]:
    result = subprocess.run(
        ["git", *arguments], cwd=root, check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_remotes(root: Path) -> dict[str, dict[str, list[str]]]:
    """Configured remotes as name -> {fetch: [urls], push: [urls]}.

    Both directions matter: a repository can fetch from a private mirror and push to a
    public one. Git also allows several `pushurl` values per remote, so each direction
    is a list -- keeping only the last one hid every other destination.
    """

    if not (root / ".git").exists() or shutil.which("git") is None:
        return {}
    remotes: dict[str, dict[str, list[str]]] = {}
    for name in _git(root, "remote"):
        fetch = _git(root, "remote", "get-url", "--all", name)
        push = _git(root, "remote", "get-url", "--all", "--push", name)
        remotes[name] = {"fetch": fetch, "push": push}
    return remotes


def remote_endpoints(
    remotes: dict[str, dict[str, list[str]]],
) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Sanitized, fingerprinted view of every remote endpoint."""

    return {
        name: {
            direction: [_endpoint(url) for url in sorted(urls)]
            for direction, urls in sorted(endpoints.items())
        }
        for name, endpoints in sorted(remotes.items())
    }


def describe_remotes(remotes: dict[str, dict[str, list[str]]]) -> str:
    return "; ".join(
        f"{name} {direction}={sanitize_remote_url(url)}"
        for name, endpoints in sorted(remotes.items())
        for direction, urls in sorted(endpoints.items())
        for url in sorted(urls)
    )


def confirmed_remotes(database: Database, workspace_id: str) -> dict[str, dict[str, set[str]]]:
    """The exact remote endpoints the learner confirmed, as sets of fingerprints."""

    value = database.scalar(
        "SELECT value_json FROM settings WHERE scope = ? AND scope_id = ? AND key = ?",
        [WORKSPACE_SETTINGS_SCOPE, workspace_id, REMOTE_CONFIRMATION_KEY],
    )
    try:
        payload = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    confirmed: dict[str, dict[str, set[str]]] = {}
    for name, endpoints in payload.items():
        if not isinstance(endpoints, dict):
            continue
        confirmed[str(name)] = {
            str(direction): {
                str(entry["fingerprint"])
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("fingerprint"), str)
            }
            for direction, entries in endpoints.items()
            if isinstance(entries, list)
        }
    return confirmed


def remote_confirmation_gap(
    confirmed: dict[str, dict[str, set[str]]], current: dict[str, dict[str, list[str]]]
) -> str | None:
    """Describe why a confirmation no longer covers every configured endpoint."""

    if not current:
        return None
    added = sorted(set(current) - set(confirmed))
    if added:
        return f"unconfirmed remote(s): {', '.join(added)}"
    changed = sorted(
        f"{name} {direction}"
        for name, endpoints in current.items()
        for direction, urls in endpoints.items()
        if {remote_fingerprint(url) for url in urls}
        != confirmed.get(name, {}).get(direction, set())
    )
    if changed:
        return f"remote endpoint changed since it was confirmed: {', '.join(changed)}"
    return None


def confirm_remote(
    path: str | Path, *, private: bool, clock: Clock | None = None
) -> WorkspaceStatusReport:
    """Record the learner's explicit statement about their Git remotes' privacy.

    The confirmation records the exact remotes it covers, so adding a remote or
    repointing one afterwards makes the confirmation stale rather than permanent.
    """

    active_clock = clock or SystemClock()
    paths = require_initialized_workspace(path)
    configuration = load_configuration(paths)
    remotes = git_remotes(paths.root)
    if private and not remotes:
        raise LinguaWikiError(
            "no_remote_configured",
            "there is no Git remote to confirm; add one first",
            details=(ErrorDetail(field="remote", reason="no remote is configured"),),
        )
    endpoints = remote_endpoints(remotes) if private else {}
    confirmed = json.dumps(endpoints, sort_keys=True)
    with (
        open_writer(paths, command="workspace.confirm-remote", clock=active_clock) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE settings SET value_json = ?, updated_at = ? "
            "WHERE scope = ? AND scope_id = ? AND key = ?",
            [
                confirmed,
                transaction.now(),
                WORKSPACE_SETTINGS_SCOPE,
                str(configuration.workspace_id),
                REMOTE_CONFIRMATION_KEY,
            ],
        )
        migration_module.record_audit_entry(
            transaction,
            command="workspace.confirm-remote",
            correlation_id=EventId.new(),
            outcome="succeeded",
            affected_records_json=json.dumps([str(configuration.workspace_id)]),
            after_summary=f"remote_privacy_confirmed={confirmed}",
        )
    return status(path, clock=active_clock)


def status(path: str | Path, *, clock: Clock | None = None) -> WorkspaceStatusReport:
    """Summarize workspace identity, pins, and database state."""

    active_clock = clock or SystemClock()
    paths = require_initialized_workspace(path)
    configuration = load_configuration(paths)
    lock = load_lock(paths)
    warnings: list[str] = []
    database_present = paths.database.exists()
    lock_held = locks.held(paths.database) is not None
    if not database_present:
        raise LinguaWikiError(
            "database_not_found",
            f"learner database is missing: {paths.database}",
            details=(ErrorDetail(field="database", reason="missing database"),),
        )
    with open_reader(paths, clock=active_clock) as database:
        workspace_id, name, normalized_name, history_policy, track_policy, _ = _workspace_row(
            database
        )
        users = int(database.scalar("SELECT count(*) FROM users"))
        tracks = int(
            database.scalar("SELECT count(*) FROM learning_tracks WHERE status = 'active'")
        )
        applied = migration_module.applied_version(database)
        row_counts = dict(table_row_counts(database))
    if applied < migration_module.head_version():
        warnings.append(
            "the database schema is behind this core release; run 'linguawiki db migrate'"
        )
    if track_policy == "single" and tracks > 1:
        warnings.append(
            "this workspace defaults to one primary program but holds several active tracks"
        )
    return WorkspaceStatusReport(
        workspace=str(paths.root),
        workspace_id=workspace_id,
        name=name,
        normalized_name=normalized_name,
        created_at=configuration.created_at.isoformat().replace("+00:00", "Z"),
        timezone=configuration.timezone,
        history_policy=history_policy,
        track_policy=track_policy,
        backup_root=configuration.backup_root,
        core=lock.core,
        database_schema=lock.database_schema,
        skill_bundle=lock.skill_bundle,
        packs=lock.packs,
        database=str(paths.database),
        database_present=database_present,
        applied_schema_version=applied,
        packaged_schema_version=migration_module.head_version(),
        writer_lock_held=lock_held,
        users=users,
        tracks=tracks,
        row_counts=row_counts,
        warnings=tuple(warnings),
    )


def _exact_pin(requirement: Requirement) -> str | None:
    """The single `==` version a requirement pins, if it pins exactly one."""

    specifiers = list(requirement.specifier)
    if len(specifiers) == 1 and specifiers[0].operator == "==":
        return specifiers[0].version
    return None


def core_dependency_constraints() -> dict[str, SpecifierSet]:
    """Every distribution the installed core transitively needs, with its full constraints.

    The whole specifier set is kept, not just exact `==` pins: discarding ranges meant a
    lock could assign `0.0.0` to a range-constrained dependency and still pass. Constraints
    from several requirers are intersected, which is what a resolver does.
    """

    constraints: dict[str, SpecifierSet] = {}
    frontier = ["linguawiki"]
    visited: set[str] = set()
    while frontier:
        name = _normalize_package_name(frontier.pop())
        if name in visited:
            continue
        visited.add(name)
        try:
            declared = metadata.requires(name) or ()
        except metadata.PackageNotFoundError:  # pragma: no cover - environment specific
            continue
        for raw in declared:
            try:
                requirement = Requirement(raw)
            except InvalidRequirement:  # pragma: no cover - malformed third-party metadata
                continue
            # An unset `extra` makes optional-extra requirements evaluate false, which
            # is what we want: only the unconditional runtime graph is required.
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            dependency = _normalize_package_name(requirement.name)
            constraints[dependency] = (
                constraints.get(dependency, SpecifierSet()) & requirement.specifier
            )
            frontier.append(dependency)
    constraints.pop("linguawiki", None)
    return constraints


def core_dependency_closure() -> dict[str, str | None]:
    """Transitive dependency names, with an exact pin where the graph pins one exactly."""

    return {
        name: next(
            (
                specifier.version
                for specifier in specifiers
                if specifier.operator == "==" and len(list(specifiers)) == 1
            ),
            None,
        )
        for name, specifiers in core_dependency_constraints().items()
    }


def core_requirements() -> dict[str, str | None]:
    """Direct runtime requirements the installed core declares, as name -> exact pin."""

    requirements: dict[str, str | None] = {}
    for raw in metadata.requires("linguawiki") or ():
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:  # pragma: no cover - malformed metadata
            continue
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        requirements[_normalize_package_name(requirement.name)] = _exact_pin(requirement)
    return requirements


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def workspace_project_name(paths: WorkspacePaths) -> str | None:
    """The workspace's own project name, which its uv.lock must resolve as the root."""

    try:
        payload = tomllib.loads((paths.root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = payload.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return _normalize_package_name(name) if isinstance(name, str) else None


class DependencyLockProblem(ContractModel):
    reason: str
    detail: str


# The source variants a uv lock entry may declare. `source = {}` matches none of them.
UV_SOURCE_VARIANTS: tuple[frozenset[str], ...] = (
    frozenset({"registry"}),
    frozenset({"git"}),
    frozenset({"url"}),
    frozenset({"path"}),
    frozenset({"directory"}),
    frozenset({"editable"}),
    frozenset({"virtual"}),
)


def _source_variant_problem(source: object) -> str | None:
    """Name the reason a package's `source` is not one recognised uv variant."""

    if not isinstance(source, dict):
        return "resolves a package without a source"
    keys = frozenset(str(key) for key in source)
    if not keys:
        return "resolves a package with an empty source"
    if keys not in UV_SOURCE_VARIANTS:
        return "resolves a package whose source is not a recognised uv variant"
    if not all(isinstance(value, str) and value for value in source.values()):
        return "resolves a package whose source has no location"
    return None


#: uv lock format versions this release can interpret.
SUPPORTED_UV_LOCK_VERSIONS = frozenset({1})


def canonical_source(source: object) -> str:
    """A stable string form of a uv source, used as part of a package's identity."""

    if not isinstance(source, dict):
        return ""
    return ",".join(f"{key}={source[key]}" for key in sorted(str(key) for key in source))


class PackageIdentity(NamedTuple):
    """How uv identifies a resolved node: name, version, and source.

    Name alone collapses two legitimately resolved versions; name and version alone
    collapse the same version resolved from different sources, so a valid
    source-distinguished pair would be rejected as a duplicate.
    """

    name: str
    version: str
    source: str

    def __str__(self) -> str:
        return f"{self.name} {self.version}" + (f" [{self.source}]" if self.source else "")


class DependencyEdge(NamedTuple):
    """An edge, with whichever qualifiers the lock chose to state."""

    name: str
    version: str | None
    source: str | None

    def __str__(self) -> str:
        qualifiers = " ".join(part for part in (self.version, self.source) if part)
        return f"{self.name} {qualifiers}".strip()


def _dependency_edges(
    package: dict[str, Any],
) -> tuple[DependencyEdge, ...] | str:
    """Parse a package's declared edges, or describe why they are malformed.

    `dependencies` was assumed to be a list of mappings, so a parsable but wrong shape
    such as `dependencies = 1` raised a TypeError and surfaced as `internal_error`
    instead of a dependency-lock diagnostic. Every malformed shape is now named.
    """

    declared = package.get("dependencies", [])
    if not isinstance(declared, list):
        return "declares dependencies that are not a list"
    edges: list[DependencyEdge] = []
    for entry in declared:
        if not isinstance(entry, dict):
            return "declares a dependency that is not a mapping"
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return "declares a dependency without a name"
        version = entry.get("version")
        if version is not None and not isinstance(version, str):
            return "declares a dependency whose version is not a string"
        if "source" in entry and not isinstance(entry["source"], dict):
            return "declares a dependency whose source is not a mapping"
        edges.append(
            DependencyEdge(
                name=_normalize_package_name(name),
                version=version,
                source=canonical_source(entry["source"]) if "source" in entry else None,
            )
        )
    return tuple(edges)


def _resolve_edge(
    edge: DependencyEdge, by_name: dict[str, list[PackageIdentity]]
) -> PackageIdentity | None:
    """The identity an edge selects, or None when it does not select exactly one.

    An edge narrows by whichever qualifiers it states; if more than one node still
    matches, the lock is ambiguous and nothing is selected.
    """

    candidates = [
        identity
        for identity in by_name.get(edge.name, [])
        if (edge.version is None or identity.version == edge.version)
        and (edge.source is None or identity.source == edge.source)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _reachable(
    graph: dict[PackageIdentity, tuple[DependencyEdge, ...]],
    by_name: dict[str, list[PackageIdentity]],
    root: PackageIdentity,
) -> set[PackageIdentity]:
    """Identities reachable from the root through the edges the lock actually declares.

    Traversing by name collapsed duplicate versions of a package, so an edge pointing at
    a stub version was satisfied by a different entry later in the file.
    """

    seen: set[PackageIdentity] = set()
    frontier = [root]
    while frontier:
        identity = frontier.pop()
        if identity in seen or identity not in graph:
            continue
        seen.add(identity)
        for edge in graph[identity]:
            selected = _resolve_edge(edge, by_name)
            if selected is not None:
                frontier.append(selected)
    return seen


def inspect_dependency_lock(
    paths: WorkspacePaths, lock: LockManifest
) -> DependencyLockProblem | None:
    """Check that uv.lock resolves this workspace's real, closed dependency graph.

    The graph is keyed by (name, version) identity, because a lock may legitimately
    resolve two versions of a package and an edge may name which one it selects. Keying
    by name alone let a stub entry be silently replaced by a later real one.
    """

    try:
        payload = tomllib.loads(paths.dependency_lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return DependencyLockProblem(reason="is not a parsable TOML lock file", detail="")
    format_version = payload.get("version")
    if not isinstance(format_version, int):
        return DependencyLockProblem(reason="has no uv lock format version", detail="")
    if format_version not in SUPPORTED_UV_LOCK_VERSIONS:
        return DependencyLockProblem(
            reason="uses a uv lock format version this release does not support",
            detail=str(format_version),
        )
    packages = payload.get("package")
    if not isinstance(packages, list):
        return DependencyLockProblem(reason="resolves no packages", detail="")

    graph: dict[PackageIdentity, tuple[DependencyEdge, ...]] = {}
    by_name: dict[str, list[PackageIdentity]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            return DependencyLockProblem(reason="resolves a package without a name", detail="")
        name = _normalize_package_name(str(package["name"]))
        if not isinstance(package.get("version"), str):
            return DependencyLockProblem(reason="resolves a package without a version", detail=name)
        source_problem = _source_variant_problem(package.get("source"))
        if source_problem is not None:
            return DependencyLockProblem(reason=source_problem, detail=name)
        identity = PackageIdentity(
            name=name,
            version=str(package["version"]),
            source=canonical_source(package.get("source")),
        )
        if identity in graph:
            return DependencyLockProblem(
                reason="resolves the same package identity twice", detail=str(identity)
            )
        edges = _dependency_edges(package)
        if isinstance(edges, str):
            return DependencyLockProblem(reason=edges, detail=str(identity))
        graph[identity] = edges
        by_name.setdefault(name, []).append(identity)

    project = workspace_project_name(paths)
    if project is None:
        return DependencyLockProblem(reason="cannot be matched to this workspace", detail="")
    project_identities = by_name.get(project, [])
    if not project_identities:
        return DependencyLockProblem(
            reason="does not resolve this workspace project", detail=project
        )
    if len(project_identities) > 1:
        return DependencyLockProblem(
            reason="resolves this workspace project more than once", detail=project
        )
    root = project_identities[0]

    # Every edge must select exactly one resolved identity.
    dangling = sorted(
        f"{identity} -> {edge}"
        for identity, edges in graph.items()
        for edge in edges
        if _resolve_edge(edge, by_name) is None
    )
    if dangling:
        return DependencyLockProblem(
            reason="has dependency edges that resolve to nothing", detail=", ".join(dangling)
        )

    core_identities = by_name.get("linguawiki", [])
    if not core_identities:
        return DependencyLockProblem(reason="does not resolve the core package", detail="")
    if not any(edge.name == "linguawiki" for edge in graph[root]):
        return DependencyLockProblem(
            reason="does not make this workspace depend on the core", detail=project
        )
    core = _resolve_edge(next(edge for edge in graph[root] if edge.name == "linguawiki"), by_name)
    if core is None or core.version != lock.core.version:
        return DependencyLockProblem(
            reason="resolves a different core release than linguawiki.lock",
            detail=f"{core.version if core else 'unresolved'} != {lock.core.version}",
        )

    reachable = _reachable(graph, by_name, root)
    constraints = core_dependency_constraints()
    selected: dict[str, list[str]] = {}
    for identity in reachable:
        selected.setdefault(identity.name, []).append(identity.version)
    unreachable = sorted(set(constraints) - set(selected))
    if unreachable:
        return DependencyLockProblem(
            reason="does not reach the whole core dependency graph from this workspace",
            detail=", ".join(unreachable),
        )
    unsatisfied = sorted(
        f"{name} {version} does not satisfy {specifiers}"
        for name, specifiers in constraints.items()
        for version in selected[name]
        if not specifiers.contains(version, prereleases=True)
    )
    if unsatisfied:
        return DependencyLockProblem(
            reason="resolves dependency versions that do not satisfy the core's requirements",
            detail="; ".join(unsatisfied),
        )
    return None


def dependency_lock_pin(paths: WorkspacePaths) -> str | None:
    """The core version a valid workspace lock resolves, or None if it is not valid."""

    try:
        payload = tomllib.loads(paths.dependency_lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    packages = payload.get("package")
    if not isinstance(packages, list):
        return None
    for package in packages:
        if isinstance(package, dict) and package.get("name") == "linguawiki":
            version = package.get("version")
            return version if isinstance(version, str) else None
    return None


def uv_lock_verdict(paths: WorkspacePaths) -> tuple[bool, str] | None:
    """uv's own verdict on whether the lock is current, or None if uv is unavailable.

    This is uv's semantics, which knows fields and rules this parser never will, so it
    runs wherever uv is already required: `workspace lock-dependencies`. It is
    deliberately *not* part of `workspace doctor`, because it needs an index or a warm
    cache and a diagnostic that fails when the network is down would be worse than the
    offline structural checks it would supplement.
    """

    if shutil.which("uv") is None:
        return None
    result = subprocess.run(
        ["uv", "lock", "--project", str(paths.root), "--check"],
        cwd=paths.root,
        check=False,
        capture_output=True,
        text=True,
    )
    detail = result.stderr.strip().splitlines()
    return result.returncode == 0, (detail[-1].strip() if detail else "")


def _dependency_lock_check(paths: WorkspacePaths, lock: LockManifest) -> CheckResult:
    """A workspace is only fully reproducible once uv has resolved its dependencies."""

    name = paths.dependency_lock.name
    if not paths.dependency_lock.is_file():
        return _warning(
            "dependency_lock",
            f"{name} is absent, so transitive dependencies are not pinned; "
            "run 'linguawiki workspace lock-dependencies'",
        )
    problem = inspect_dependency_lock(paths, lock)
    if problem is not None:
        return _failed(
            "dependency_lock",
            f"{name} {problem.reason}; regenerate it with 'linguawiki workspace lock-dependencies'",
            detail=problem.detail,
        )
    return _ok(
        "dependency_lock",
        f"{name} resolves core {lock.core.version} and its full dependency graph",
    )


def _core_source_check(paths: WorkspacePaths) -> CheckResult:
    copied = paths.root / "src" / "linguawiki"
    if copied.exists():
        return _failed(
            "no_core_source_copy",
            "the workspace contains a copy of the core Python package",
            path=str(copied),
        )
    return _ok("no_core_source_copy", "the workspace pins core instead of copying its source")


def _backup_root_check(
    paths: WorkspacePaths, configuration: WorkspaceManifest
) -> list[CheckResult]:
    try:
        root = resolve_backup_root(configuration.backup_root, workspace_root=paths.root)
    except LinguaWikiError as exc:
        return [_failed("backup_root", exc.payload.message, code=exc.payload.code)]
    checks = [_ok("backup_root", f"backup root {root} is outside the workspace")]
    if not root.exists():
        checks.append(_warning("backup_root_present", f"backup root does not exist yet: {root}"))
    problems = backup_root_git_problems(root, workspace_root=paths.root)
    checks.extend(_failed("backup_root_git", problem) for problem in problems)
    if not problems:
        checks.append(_ok("backup_root_git", "the backup root is outside every Git repository"))
    return checks


def _pin_checks(lock: LockManifest) -> list[CheckResult]:
    installed = {
        "core": core_pin(),
        "database_schema": migration_module.database_schema_pin(),
        "skill_bundle": skill_bundle_pin(),
    }
    checks: list[CheckResult] = []
    for component, pin in installed.items():
        recorded: VersionPin = getattr(lock, component)
        if (recorded.version, recorded.sha256) == (pin.version, pin.sha256):
            checks.append(_ok(f"pin_{component}", f"{component} matches the pinned {pin.version}"))
        else:
            checks.append(
                _failed(
                    f"pin_{component}",
                    f"the installed {component} differs from linguawiki.lock",
                    pinned=f"{recorded.version}@{recorded.sha256[:12]}",
                    installed=f"{pin.version}@{pin.sha256[:12]}",
                )
            )
    return checks


def _privacy_checks(paths: WorkspacePaths, privacy: PrivacyReport) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if privacy.inspection_failure is not None:
        checks.append(
            _failed("privacy_inspection", privacy.inspection_failure, source=privacy.source)
        )
    if privacy.violations:
        checks.append(
            _failed(
                "privacy_candidates",
                "private artifacts are Git candidates in this workspace",
                paths="; ".join(f"{item.path} ({item.reason})" for item in privacy.violations),
            )
        )
    else:
        checks.append(
            _ok("privacy_candidates", f"{privacy.candidates_checked} Git candidates are all safe")
        )
    if privacy.missing_ignore_rules:
        checks.append(
            _failed(
                "ignore_rules",
                "required privacy ignore rules are missing from .gitignore",
                missing=", ".join(privacy.missing_ignore_rules),
            )
        )
    else:
        checks.append(_ok("ignore_rules", "every required privacy ignore rule is present"))
    if privacy.git_root is not None and Path(privacy.git_root) != paths.root:
        checks.append(
            _warning(
                "workspace_nesting",
                "this workspace is inside another Git repository, which therefore tracks it; "
                "a learner workspace is normally its own private repository",
                git_root=privacy.git_root,
            )
        )
    checks.append(
        _warning(
            "privacy_scope",
            "generated wiki sanitization is policy filtering, not proof this repository is "
            "safe to publish",
        )
    )
    return checks


def doctor(path: str | Path, *, clock: Clock | None = None) -> DoctorReport:
    """Run every workspace diagnostic and report failures and warnings separately."""

    active_clock = clock or SystemClock()
    paths = require_initialized_workspace(path)
    checks: list[CheckResult] = []
    try:
        configuration = load_configuration(paths)
    except LinguaWikiError as exc:
        return DoctorReport(
            ok=False,
            workspace=str(paths.root),
            checks=(_failed("configuration", exc.payload.message, code=exc.payload.code),),
        )
    checks.append(_ok("configuration", "linguawiki.toml validates against lingua.workspace.v1"))
    try:
        lock = load_lock(paths)
    except LinguaWikiError as exc:
        return DoctorReport(
            ok=False,
            workspace=str(paths.root),
            checks=(*checks, _failed("lock", exc.payload.message, code=exc.payload.code)),
        )
    checks.append(_ok("lock", "linguawiki.lock validates against linguawiki.lock.v1"))
    checks.extend(_pin_checks(lock))
    checks.append(_core_source_check(paths))
    checks.append(_dependency_lock_check(paths, lock))
    checks.extend(_backup_root_check(paths, configuration))
    skills = inspect_bundle(paths, lock=lock)
    if skills.modified or skills.missing or skills.unexpected:
        checks.append(
            _failed(
                "generated_skills",
                "the generated skill snapshot was modified; run 'linguawiki skills install'",
                modified=", ".join(skills.modified),
                missing=", ".join(skills.missing),
                unexpected=", ".join(skills.unexpected),
            )
        )
    elif skills.matches_lock is False:
        checks.append(
            _failed("generated_skills", "the installed skill snapshot does not match the lock")
        )
    else:
        checks.append(_ok("generated_skills", f"{skills.file_count} generated skill files match"))
    if not paths.database.exists():
        checks.append(_failed("database", f"learner database is missing: {paths.database}"))
        privacy = check_privacy(paths.root)
        checks.extend(_privacy_checks(paths, privacy))
        return DoctorReport(
            ok=False,
            workspace=str(paths.root),
            checks=tuple(checks),
            warnings=tuple(check.message for check in checks if check.status == "warning"),
        )
    with open_reader(paths, clock=active_clock) as database:
        # Classify and check the database *before* reading application tables: querying
        # `workspaces` in an unrecognized file raised a raw catalog error instead of a
        # diagnostic.
        state = classify_database(database)
        if state is not DatabaseState.MANAGED:
            if state is DatabaseState.UNMANAGED:
                checks.append(
                    _failed(
                        "database_recognized",
                        f"{paths.database} holds data LinguaWiki did not create",
                        tables=", ".join(sorted(database.table_names())[:10]),
                    )
                )
            else:
                checks.append(
                    _failed(
                        "database_state",
                        f"{paths.database} is a LinguaWiki database in the {state} state; "
                        "back it up before repairing it",
                        state=str(state),
                        divergence=schema_divergence(database) or "",
                    )
                )
            privacy = check_privacy(paths.root)
            checks.extend(_privacy_checks(paths, privacy))
            return DoctorReport(
                ok=False,
                workspace=str(paths.root),
                checks=tuple(checks),
                warnings=tuple(check.message for check in checks if check.status == "warning"),
            )
        checks.append(_ok("database_recognized", "the learner database is a LinguaWiki database"))
        integrity = check_database(database, lock=lock)
        checks.extend(integrity.checks)
        if any(check.status == "failed" for check in integrity.checks):
            privacy = check_privacy(paths.root)
            checks.extend(_privacy_checks(paths, privacy))
            return DoctorReport(
                ok=False,
                workspace=str(paths.root),
                checks=tuple(checks),
                warnings=tuple(check.message for check in checks if check.status == "warning"),
            )
        workspace_id, name, _, _, track_policy, _ = _workspace_row(database)
        confirmed = confirmed_remotes(database, workspace_id)
        active_tracks = int(
            database.scalar("SELECT count(*) FROM learning_tracks WHERE status = 'active'")
        )
    if str(configuration.workspace_id) == workspace_id:
        checks.append(_ok("workspace_identity_match", "the lock, config, and database agree"))
    else:
        checks.append(
            _failed(
                "workspace_identity_match",
                "linguawiki.toml and the learner database disagree about the workspace identity",
            )
        )
    context = template_context(name=name, configuration=configuration, lock=lock)
    modified = modified_generated_files(paths, context)
    if modified:
        checks.append(
            _failed(
                "generated_files",
                "generated workspace files were modified by hand",
                files=", ".join(modified),
            )
        )
    else:
        checks.append(_ok("generated_files", "generated workspace files match their templates"))
    remotes = git_remotes(paths.root)
    gap = remote_confirmation_gap(confirmed, remotes)
    if not remotes:
        checks.append(_ok("git_remote_privacy", "no Git remote is configured"))
    elif gap is not None:
        checks.append(
            _warning(
                "git_remote_privacy",
                f"{gap}; run 'linguawiki workspace confirm-remote --private' after checking "
                "that every remote is private",
                remotes=describe_remotes(remotes),
            )
        )
    else:
        checks.append(
            _ok(
                "git_remote_privacy",
                f"every configured remote endpoint is confirmed private ({len(remotes)})",
            )
        )
    if track_policy == "single" and active_tracks > 1:
        checks.append(
            _warning(
                "single_program",
                "this workspace defaults to one primary program but holds several active tracks",
                active_tracks=str(active_tracks),
            )
        )
    else:
        checks.append(
            _ok(
                "single_program",
                f"track policy {track_policy} with {active_tracks} active track(s)",
            )
        )
    privacy = check_privacy(paths.root)
    checks.extend(_privacy_checks(paths, privacy))
    return DoctorReport(
        ok=all(check.status != "failed" for check in checks),
        workspace=str(paths.root),
        checks=tuple(checks),
        warnings=tuple(check.message for check in checks if check.status == "warning"),
    )


def lock_dependencies(
    path: str | Path, *, find_links: str | Path | None = None
) -> WorkspaceStatusReport:
    """Resolve and write the workspace's uv.lock, then report the workspace state."""

    paths = require_initialized_workspace(path)
    write_uv_lock(paths, find_links=find_links)
    return status(paths.root)


def privacy_check(path: str | Path) -> PrivacyReport:
    paths = require_initialized_workspace(path)
    return check_privacy(paths.root)


def ignore_rule_gaps(path: str | Path) -> tuple[str, ...]:
    paths = require_initialized_workspace(path)
    if not paths.gitignore.is_file():
        return ("<missing .gitignore>",)
    return missing_ignore_rules(paths.gitignore.read_text(encoding="utf-8"), workspace_policy())
