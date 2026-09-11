"""Git-safe path rules and the workspace privacy check."""

from __future__ import annotations

import shutil
import subprocess
from enum import StrEnum
from pathlib import Path, PurePosixPath

from linguawiki import resources
from linguawiki.models import ContractModel
from linguawiki.paths import find_git_root
from linguawiki.repository_policy import (
    GENERATED_END,
    GENERATED_START,
    PrivacyPolicy,
    gitignore_patterns,
    load_privacy_policy,
    parse_nul_paths,
    privacy_violation,
)

# Workspace directories that hold private state but are not intrinsically forbidden
# names, so they are ignored per workspace rather than through the shared policy.
WORKSPACE_IGNORED_DIRECTORIES = ("data", "drafts")
WORKSPACE_IGNORED_PATTERNS = (".venv/", *(f"{name}/" for name in WORKSPACE_IGNORED_DIRECTORIES))
# The complete set of top-level entries a learner workspace may commit. Anything
# else appearing at the top level is either private state or an unreviewed addition.
GIT_SAFE_TOP_LEVEL = (
    ".agents",
    ".gitignore",
    "AGENTS.md",
    "linguawiki.lock",
    "linguawiki.toml",
    "pyproject.toml",
    "uv.lock",
    "wiki",
)


class CandidateSource(StrEnum):
    """How the list of files that could reach Git was obtained."""

    GIT = "git"
    FILESYSTEM = "filesystem"
    UNAVAILABLE = "unavailable"


class PrivacyCandidate(ContractModel):
    path: str
    reason: str


class PrivacyReport(ContractModel):
    """Which files could enter Git and whether any of them must not."""

    ok: bool
    root: str
    source: str
    candidates_checked: int
    violations: tuple[PrivacyCandidate, ...] = ()
    missing_ignore_rules: tuple[str, ...] = ()
    git_root: str | None = None
    inspection_failure: str | None = None
    warnings: tuple[str, ...] = ()


def workspace_policy() -> PrivacyPolicy:
    return load_privacy_policy(resources.privacy_policy_path())


def required_ignore_rules(policy: PrivacyPolicy) -> tuple[str, ...]:
    """Every rule a learner workspace must keep in `.gitignore`."""

    return tuple(sorted({*WORKSPACE_IGNORED_PATTERNS, *gitignore_patterns(policy)}))


def missing_ignore_rules(gitignore: str, policy: PrivacyPolicy) -> tuple[str, ...]:
    present = {line.strip() for line in gitignore.splitlines()}
    missing = [rule for rule in required_ignore_rules(policy) if rule not in present]
    if GENERATED_START not in gitignore or GENERATED_END not in gitignore:
        missing.append(GENERATED_START)
    return tuple(sorted(missing))


class CandidateListing(ContractModel):
    """The candidate listing, and whether it can be trusted.

    "Not a Git repository" and "Git inspection failed" must never collapse into the same
    answer: the filesystem fallback deliberately skips private artifacts, so using it for
    a real repository whose inspection failed reports success for a workspace that may be
    force-adding a database.
    """

    source: CandidateSource
    git_root: str | None = None
    paths: tuple[str, ...] = ()
    failure: str | None = None


def effective_git_root(root: Path) -> Path | None:
    """The repository that would actually track this workspace, ancestors included.

    A workspace nested inside another repository is still tracked by that repository, so
    looking only for `<workspace>/.git` reported "not a Git repository" and fell through
    to a listing that skips private artifacts.
    """

    return find_git_root(root.resolve())


def _git_listing(root: Path) -> CandidateListing | None:
    """List files Git would consider committing, including force-added private files."""

    git_root = effective_git_root(root)
    if git_root is None:
        return None
    if shutil.which("git") is None:
        return CandidateListing(
            source=CandidateSource.UNAVAILABLE,
            git_root=str(git_root),
            failure="this workspace is inside a Git repository but git is not installed, so "
            "the files that could reach Git cannot be listed",
        )
    relative = root.resolve().relative_to(git_root)
    pathspec = relative.as_posix() if relative.parts else "."
    result = subprocess.run(
        [
            "git",
            "-C",
            str(git_root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            pathspec,
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        return CandidateListing(
            source=CandidateSource.UNAVAILABLE,
            git_root=str(git_root),
            failure=(
                "git could not list the files that could reach Git "
                f"(exit {result.returncode}: {detail[-1] if detail else 'no output'})"
            ),
        )
    # Paths come back relative to the repository root; report them workspace-relative.
    prefix = PurePosixPath(relative.as_posix()) if relative.parts else None
    paths: list[str] = []
    for path in parse_nul_paths(result.stdout):
        if prefix is None:
            paths.append(path.as_posix())
        elif path.is_relative_to(prefix):
            paths.append(path.relative_to(prefix).as_posix())
    return CandidateListing(source=CandidateSource.GIT, git_root=str(git_root), paths=tuple(paths))


def candidate_listing(root: Path, policy: PrivacyPolicy) -> CandidateListing:
    """Obtain the candidate listing, or say plainly that it could not be obtained."""

    listing = _git_listing(root)
    if listing is not None:
        return listing
    return CandidateListing(
        source=CandidateSource.FILESYSTEM,
        paths=tuple(path.as_posix() for path in _filesystem_candidates(root, policy)),
    )


def _is_ignored_by_workspace_layout(relative: PurePosixPath) -> bool:
    head = relative.parts[0] if relative.parts else ""
    return head in {".git", *WORKSPACE_IGNORED_DIRECTORIES, ".venv"}


def _filesystem_candidates(root: Path, policy: PrivacyPolicy) -> list[PurePosixPath]:
    """Fallback listing for a workspace that is not a Git repository yet."""

    candidates: list[PurePosixPath] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _is_ignored_by_workspace_layout(relative) or privacy_violation(relative, policy):
            continue
        candidates.append(relative)
    return candidates


def unsafe_top_level(path: PurePosixPath) -> str | None:
    """Reject a Git candidate whose top-level entry is not on the allowlist.

    The private-pattern rules only catch artifacts we already know how to name. The
    allowlist is the other half of the contract: anything else at the top level of a
    learner workspace is unreviewed and must be looked at before it is committed.
    """

    head = path.parts[0] if path.parts else ""
    return None if head in GIT_SAFE_TOP_LEVEL else "outside the Git-safe top level"


def candidate_violation(path: PurePosixPath, policy: PrivacyPolicy) -> str | None:
    return privacy_violation(path, policy) or unsafe_top_level(path)


def check_privacy(root: Path, *, policy: PrivacyPolicy | None = None) -> PrivacyReport:
    """Report private files that could reach Git and any missing ignore rules."""

    active_policy = policy or workspace_policy()
    listing = candidate_listing(root, active_policy)
    warnings: list[str] = []
    if listing.source == CandidateSource.FILESYSTEM:
        warnings.append(
            "this workspace is not a Git repository, so candidates were listed from disk"
        )
    violations = tuple(
        PrivacyCandidate(path=candidate, reason=reason)
        for candidate in listing.paths
        if (reason := candidate_violation(PurePosixPath(candidate), active_policy)) is not None
    )
    gitignore = root / ".gitignore"
    missing = (
        missing_ignore_rules(gitignore.read_text(encoding="utf-8"), active_policy)
        if gitignore.is_file()
        else required_ignore_rules(active_policy)
    )
    if not gitignore.is_file():
        warnings.append(".gitignore is missing from this workspace")
    return PrivacyReport(
        ok=listing.source != CandidateSource.UNAVAILABLE and not violations and not missing,
        root=str(root),
        source=str(listing.source),
        candidates_checked=len(listing.paths),
        violations=violations,
        missing_ignore_rules=missing,
        git_root=listing.git_root,
        inspection_failure=listing.failure,
        warnings=tuple(warnings),
    )
