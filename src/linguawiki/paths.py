"""Path resolution and destructive-target safety rules shared by every command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from linguawiki.errors import ErrorDetail, LinguaWikiError

WORKSPACE_CONFIG_NAME = "linguawiki.toml"
WORKSPACE_LOCK_NAME = "linguawiki.lock"
DEPENDENCY_LOCK_NAME = "uv.lock"
DATABASE_RELATIVE_PATH = Path("data") / "linguawiki.duckdb"
#: Directories a workspace keeps out of Git. `staging` holds bytes a client created and has
#: not yet handed to a row that owns them -- browser captures before their registration --
#: which is the one place a recording can exist before any command accounts for it.
PRIVATE_DIRECTORIES = ("data", "artifacts", "imports", "drafts", "exports/anki", "staging")
WIKI_DIRECTORIES = ("wiki", "wiki/learner", "wiki/languages", "wiki/sessions", "wiki/reports")
SKILL_BUNDLE_RELATIVE_PATH = Path(".agents") / "skills"


def resolve_path(value: str | Path, *, purpose: str) -> Path:
    """Expand and fully resolve a caller-supplied path before it is ever used."""

    text = str(value).strip()
    if not text:
        raise LinguaWikiError(
            "invalid_path",
            f"{purpose} path must not be empty",
            details=(ErrorDetail(field=purpose, reason="empty path"),),
        )
    candidate = Path(text).expanduser()
    if not candidate.is_absolute() and not candidate.parts:
        raise LinguaWikiError("invalid_path", f"{purpose} path could not be resolved")
    return Path(candidate).resolve()


def assert_safe_destructive_target(path: Path, *, purpose: str) -> Path:
    """Refuse root-level, home-directory, and otherwise catastrophic write targets."""

    resolved = path if path.is_absolute() else path.resolve()
    home = Path.home().resolve()
    reason: str | None = None
    if resolved == Path(resolved.anchor):
        reason = "filesystem root is never a valid target"
    elif len(resolved.parts) <= 2 and resolved != home:
        reason = "top-level system directory is never a valid target"
    elif resolved == home:
        reason = "home directory itself is never a valid target"
    elif resolved in home.parents:
        reason = "parent of the home directory is never a valid target"
    if reason is not None:
        raise LinguaWikiError(
            "unsafe_target",
            f"refusing to use {resolved} as a {purpose} target",
            details=(ErrorDetail(field=purpose, reason=reason, context={"path": str(resolved)}),),
        )
    return resolved


def assert_within(path: Path, root: Path, *, purpose: str) -> Path:
    """Require a destructive target to stay inside its owning workspace.

    Resolution itself can fail: `Path.resolve` raises `RuntimeError` on a symlink loop under
    Python 3.12 and `OSError` for other filesystem trouble. A path whose location cannot be
    established has not been shown to be inside the workspace, so it is refused here rather
    than crashing whichever caller happened to touch it.
    """

    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError) as failure:
        raise LinguaWikiError(
            "unresolvable_target",
            f"{purpose} target at {path} cannot be resolved to a location, so it cannot be "
            f"shown to stay inside {root}: {failure}",
            details=(
                ErrorDetail(
                    field=purpose,
                    reason="unresolvable",
                    context={"path": str(path), "workspace": str(root)},
                ),
            ),
        ) from failure
    if resolved != root and root not in resolved.parents:
        raise LinguaWikiError(
            "unsafe_target",
            f"{purpose} target must stay inside {root}",
            details=(
                ErrorDetail(
                    field=purpose,
                    reason="outside workspace",
                    context={"path": str(resolved), "workspace": str(root)},
                ),
            ),
        )
    return resolved


def assert_outside(path: Path, root: Path, *, purpose: str) -> Path:
    """Require a path such as a backup root to stay outside a repository."""

    resolved = path.resolve()
    if resolved == root or root in resolved.parents:
        raise LinguaWikiError(
            "backup_root_invalid",
            f"{purpose} must stay outside {root}",
            details=(
                ErrorDetail(
                    field=purpose,
                    reason="inside repository",
                    context={"path": str(resolved), "repository": str(root)},
                ),
            ),
        )
    return resolved


def find_ancestor_containing(path: Path, *markers: str) -> Path | None:
    """Return the closest ancestor (inclusive) that contains every marker."""

    for candidate in (path, *path.parents):
        if all((candidate / marker).exists() for marker in markers):
            return candidate
    return None


def find_core_repository_root(path: Path) -> Path | None:
    """Detect the LinguaWiki product repository so workspaces never nest inside it."""

    return find_ancestor_containing(
        path, "src/linguawiki/contracts.py", "config/privacy-policy.toml"
    )


def find_git_root(path: Path) -> Path | None:
    return find_ancestor_containing(path, ".git")


def directory_is_empty(path: Path) -> bool:
    return not any(path.iterdir())


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """Resolved layout of one learner workspace."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / WORKSPACE_CONFIG_NAME

    @property
    def lock(self) -> Path:
        return self.root / WORKSPACE_LOCK_NAME

    @property
    def gitignore(self) -> Path:
        return self.root / ".gitignore"

    @property
    def dependency_lock(self) -> Path:
        return self.root / DEPENDENCY_LOCK_NAME

    @property
    def database(self) -> Path:
        return self.root / DATABASE_RELATIVE_PATH

    @property
    def skills(self) -> Path:
        return self.root / SKILL_BUNDLE_RELATIVE_PATH

    @property
    def wiki(self) -> Path:
        return self.root / "wiki"


def workspace_paths(value: str | Path) -> WorkspacePaths:
    """Resolve a workspace root without requiring it to exist yet."""

    return WorkspacePaths(root=resolve_path(value, purpose="workspace"))


def require_initialized_workspace(value: str | Path) -> WorkspacePaths:
    """Resolve a workspace root that must already hold a validated configuration."""

    paths = workspace_paths(value)
    if not paths.root.is_dir():
        raise LinguaWikiError(
            "workspace_not_found", f"workspace directory does not exist: {paths.root}"
        )
    if not paths.config.is_file():
        raise LinguaWikiError(
            "workspace_not_initialized",
            f"{WORKSPACE_CONFIG_NAME} is missing; run 'linguawiki workspace init' first",
            details=(
                ErrorDetail(
                    field="workspace",
                    reason="missing configuration",
                    context={"path": str(paths.root)},
                ),
            ),
        )
    return paths
