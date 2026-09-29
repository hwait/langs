"""Git-safe path rules and the workspace privacy check."""

from __future__ import annotations

import shutil
import subprocess
from enum import StrEnum
from pathlib import Path, PurePosixPath

from linguawiki import resources
from linguawiki.clock import Clock, SystemClock
from linguawiki.db.connection import Database, open_reader
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths, find_git_root
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


def check_privacy(
    root: Path,
    *,
    policy: PrivacyPolicy | None = None,
    listing: CandidateListing | None = None,
) -> PrivacyReport:
    """Report private files that could reach Git and any missing ignore rules.

    `listing` lets a caller that has already enumerated the candidates pass them in. The
    audit used to list them twice and keep only the first listing's *failure*: when the
    second came back unavailable, the content scan silently examined nothing and the audit
    reported a clean workspace.
    """

    active_policy = policy or workspace_policy()
    listing = listing if listing is not None else candidate_listing(root, active_policy)
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


# A body has to be long enough that finding it somewhere is evidence rather than
# coincidence. Short utterances -- "tak", "nie wiem" -- appear in ordinary prose, and a
# check that flagged them would be turned off within a week.
LEAK_MATCH_MINIMUM = 24

#: File types the content scan reads. Everything a learner workspace commits is text of
#: one of these kinds; a binary is governed by the path rules instead, and a suffix this
#: does not list is reported as unscanned rather than silently passed.
SCANNED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".csv",
        ".html",
        ".rst",
        ".py",
        ".sql",
        ".cfg",
        ".ini",
        ".lock",
        ".j2",
        "",
    }
)


class RetentionSummary(ContractModel):
    """What this workspace is holding of the learner, in the terms they consented in."""

    transcript_policy: str = "withheld"
    audio_consent: bool = False
    utterances_held: int = 0
    utterances_with_words: int = 0
    artifacts_held: int = 0
    artifacts_purged: int = 0
    #: Recordings whose row says they are gone -- purged, or registered as not kept --
    #: and whose bytes are still on disk. Always zero in a workspace the commands built;
    #: anything else is a privacy claim that is not true, and the audit exists to say so.
    unkept_files_still_present: tuple[str, ...] = ()
    #: What this track does with recordings once they are ingested.
    audio_retention_policy: str = "keep"
    audio_retention_days: int | None = None
    #: Claims that would stop standing if every held recording were purged today. The
    #: number a learner actually needs before choosing to delete anything.
    claims_resting_on_audio: int = 0
    sources_with_excerpts: int = 0


class PurgeConsequence(ContractModel):
    artifact_id: str
    relative_path: str
    kind: str
    invalidated_claims: int = 0
    surviving_language_evidence: int = 0


class PrivacyAuditReport(ContractModel):
    """Everything private this workspace could be leaking, in one pass."""

    ok: bool
    root: str
    track_id: str | None = None
    paths: PrivacyReport
    #: Learner speech or restricted source text found in files that are committed.
    content_leaks: tuple[PrivacyCandidate, ...] = ()
    #: The same, found in the audit log -- which is inside the database and therefore not
    #: in Git, but is read back into reports and shown to skills.
    log_leaks: tuple[PrivacyCandidate, ...] = ()
    #: Files that could be committed and could not be read. They are failures, not
    #: footnotes: a scan that skips what it cannot parse reports a clean workspace for a
    #: page holding the learner's transcript and one invalid byte.
    unscanned: tuple[PrivacyCandidate, ...] = ()
    retention: RetentionSummary
    #: What deleting each held recording would cost, before anyone deletes one.
    purge_consequences: tuple[PurgeConsequence, ...] = ()
    warnings: tuple[str, ...] = ()


def _private_bodies(database: Database) -> list[tuple[str, str]]:
    """The texts that must not appear outside the database, with what each one is.

    Two kinds. The learner's own speech, which is private because it is theirs; and text
    from a source catalogued as metadata-only, which is private because the rights say so.
    A short-excerpt source's excerpt is deliberately absent: the plan permits those in a
    wiki page, and flagging them would make the check useless for the case it exists for.

    Deliberately *not* scoped to a track. The retention figures beside it are one
    learner's, but a leak is a leak: another learner's words in a committed page are in
    the same repository and the same history, and auditing one track at a time would
    report a clean workspace to whichever of them ran the command.
    """

    bodies: list[tuple[str, str]] = []
    bodies.extend(
        (str(text), f"the learner's own words, from utterance {utterance_id}")
        for utterance_id, text in database.query(
            "SELECT utterance_id, raw_text FROM utterances WHERE visibility <> 'withheld'"
        )
        if len(str(text)) >= LEAK_MATCH_MINIMUM
    )
    bodies.extend(
        (str(text), f"a reviewed hearing of the learner, from revision {revision_id}")
        for revision_id, text in database.query(
            "SELECT revision_id, text FROM transcript_revisions WHERE visibility <> 'withheld'"
        )
        if len(str(text)) >= LEAK_MATCH_MINIMUM
    )
    bodies.extend(
        (str(excerpt), f"text from {title}, which is catalogued as metadata-only")
        for excerpt, title in database.query(
            "SELECT unit.excerpt, source.title FROM source_units unit "
            "JOIN sources source ON source.source_id = unit.source_id "
            "WHERE unit.excerpt IS NOT NULL AND source.rights = 'metadata-only'"
        )
        if len(str(excerpt)) >= LEAK_MATCH_MINIMUM
    )
    return bodies


def audit(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    clock: Clock | None = None,
) -> PrivacyAuditReport:
    """Answer the only privacy question that matters: what is about to escape.

    The path check alone was never enough. It knows that `artifacts/` must not be
    committed, which stops a recording reaching Git as a file -- and says nothing about
    the same recording's transcript being pasted into a wiki page, which is committed by
    design. So this searches the committed files for the bodies the database is holding,
    and reports what it finds with what each thing is.

    It also states the retention position plainly, because "surface retention state and
    purge consequences before deletion" is a promise that can only be kept *before*: after
    the learner has deleted a month of recordings, telling them what it cost is not a
    privacy control, it is an apology.
    """

    from linguawiki.services import artifacts as artifact_service
    from linguawiki.services import learners as learner_service
    from linguawiki.services import transcripts as transcript_service

    active_clock = clock or SystemClock()
    root = paths.root
    # Enumerated once and used for both halves: which files could be committed, and what
    # is inside them. Two listings meant two answers, and the scan trusted the one whose
    # failure it had discarded.
    candidates = candidate_listing(root, workspace_policy())
    path_report = check_privacy(root, listing=candidates)
    warnings: list[str] = []
    leaks: list[PrivacyCandidate] = []
    log_leaks: list[PrivacyCandidate] = []
    unscanned: list[PrivacyCandidate] = []
    with open_reader(paths, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        preferences = record.preferences if isinstance(record.preferences, dict) else {}
        bodies = _private_bodies(database)
        summaries = [
            (str(audit_id), " ".join(str(part) for part in (before, after) if part))
            for audit_id, before, after in database.query(
                "SELECT audit_id, before_summary, after_summary FROM audit_log "
                "WHERE before_summary IS NOT NULL OR after_summary IS NOT NULL"
            )
        ]
        # Counted from the disk rather than from the flag. A row saying a recording was
        # not kept, or was purged, while the bytes are still there is the one state the
        # retention record must never be in -- and it is exactly the state the audit would
        # otherwise report as a workspace holding nothing.
        # `contained_file`, like every other read of a stored path: a symlink to a file
        # outside the workspace is not this recording sitting here, and reporting it as one
        # contradicted `artifact verify` about the same row.
        #
        # And a path a *live* row owns is that recording, not this tombstone's returning: a
        # learner who purged a conversation and recorded a new one under the same filename
        # would otherwise be told forever that the deleted one was still here.
        owned = artifact_service.active_paths(database)
        unkept = tuple(
            str(artifact_id)
            for artifact_id, relative_path in database.query(
                "SELECT artifact_id, relative_path FROM artifacts WHERE track_id = ? "
                "AND (purged_at IS NOT NULL OR NOT retained) ORDER BY artifact_id",
                [track_id],
            )
            # One predicate shared with `artifact verify`, so the two cannot disagree
            # about whether a tombstone's old path is explained or the recording is back.
            if not artifact_service.tombstone_path_is_excused(root, str(relative_path), owned=owned)
            and artifact_service.contained_file(root, str(relative_path)) is not None
        )
        retention = RetentionSummary(
            transcript_policy=transcript_service.retention_policy(preferences),
            audio_consent=bool(preferences.get("audio_retention_consent")),
            utterances_held=int(
                database.scalar("SELECT count(*) FROM utterances WHERE track_id = ?", [track_id])
            ),
            utterances_with_words=int(
                database.scalar(
                    "SELECT count(*) FROM utterances WHERE track_id = ? AND visibility <> "
                    "'withheld'",
                    [track_id],
                )
            ),
            artifacts_held=int(
                database.scalar(
                    "SELECT count(*) FROM artifacts WHERE track_id = ? AND purged_at IS NULL "
                    "AND retained",
                    [track_id],
                )
            ),
            artifacts_purged=int(
                database.scalar(
                    "SELECT count(*) FROM artifacts WHERE track_id = ? AND purged_at IS NOT NULL",
                    [track_id],
                )
            ),
            audio_retention_policy=str(preferences.get("audio_retention_policy") or "keep"),
            audio_retention_days=(
                int(str(preferences["audio_retention_days"]))
                if preferences.get("audio_retention_days") is not None
                else None
            ),
            claims_resting_on_audio=int(
                database.scalar(
                    "SELECT count(*) FROM pronunciation_observations WHERE track_id = ? "
                    "AND basis = 'audio' AND invalidated_at IS NULL",
                    [track_id],
                )
            ),
            unkept_files_still_present=unkept,
            sources_with_excerpts=int(
                database.scalar(
                    "SELECT count(DISTINCT unit.source_id) FROM source_units unit "
                    "JOIN sources source ON source.source_id = unit.source_id "
                    "WHERE unit.excerpt IS NOT NULL AND source.track_id = ?",
                    [track_id],
                )
            ),
        )
        held = database.query(
            "SELECT artifact_id, relative_path, kind FROM artifacts WHERE track_id = ? "
            "AND purged_at IS NULL AND retained ORDER BY artifact_id",
            [track_id],
        )
        surviving = int(
            database.scalar(
                "SELECT count(*) FROM pronunciation_observations WHERE track_id = ? "
                "AND basis <> 'audio'",
                [track_id],
            )
        )
        consequences = [
            PurgeConsequence(
                artifact_id=str(artifact_id),
                relative_path=str(relative_path),
                kind=str(kind),
                invalidated_claims=len(
                    _dependent_claim_ids(database, artifact_id=str(artifact_id))
                ),
                surviving_language_evidence=surviving,
            )
            for artifact_id, relative_path, kind in held
        ]
    for audit_id, summary in summaries:
        for body, description in bodies:
            if body in summary:
                log_leaks.append(
                    PrivacyCandidate(
                        path=f"audit_log/{audit_id}",
                        reason=f"the audit log holds {description}",
                    )
                )
                break
    # Every file that could reach Git, not only the generated pages. Stage 5 searched
    # `wiki/**/*.md`, and a transcript pasted into AGENTS.md -- which is on the Git-safe
    # list and therefore *certain* to be committed -- was reported as a clean workspace.
    # The path rules say which files may be committed; this says what is inside them.
    if candidates.source == CandidateSource.UNAVAILABLE:
        # The path report already fails on this. Saying it here too is what keeps the
        # *content* half from reading as "searched and found nothing".
        unscanned.append(
            PrivacyCandidate(
                path=str(root),
                reason="the list of files that could be committed could not be obtained, "
                f"so nothing was scanned: {candidates.failure}",
            )
        )
    for relative in candidates.paths:
        candidate = root / relative
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in SCANNED_SUFFIXES:
            unscanned.append(
                PrivacyCandidate(
                    path=relative,
                    reason="this file could be committed and is not a kind the content "
                    "scan reads, so nothing here says what is inside it",
                )
            )
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as failure:
            unscanned.append(
                PrivacyCandidate(
                    path=relative,
                    reason=f"this file could be committed and could not be read: {failure}",
                )
            )
            continue
        for body, description in bodies:
            if body in text:
                leaks.append(
                    PrivacyCandidate(
                        path=relative,
                        reason=f"this file could be committed and holds {description}",
                    )
                )
                break
    if not bodies:
        warnings.append(
            "there is nothing private stored yet, so the content scan proves nothing about "
            "what this workspace would do once there is"
        )
    if retention.unkept_files_still_present:
        warnings.append(
            f"{len(retention.unkept_files_still_present)} recording(s) are recorded as not "
            "kept while their files are still on disk; the learner has been told these "
            "were removed"
        )
    if unscanned:
        warnings.append(
            f"{len(unscanned)} file(s) that could be committed were not read, so nothing "
            "here says whether they hold private content"
        )
    return PrivacyAuditReport(
        ok=path_report.ok
        and not leaks
        and not log_leaks
        and not unscanned
        and not retention.unkept_files_still_present,
        root=str(root),
        track_id=track_id,
        paths=path_report,
        content_leaks=tuple(leaks),
        log_leaks=tuple(log_leaks),
        unscanned=tuple(unscanned),
        retention=retention,
        purge_consequences=tuple(consequences),
        warnings=tuple((*path_report.warnings, *warnings)),
    )


def _dependent_claim_ids(database: Database, *, artifact_id: str) -> tuple[str, ...]:
    from linguawiki.services.artifacts import dependent_observations

    return tuple(
        observation_id
        for observation_id, _ in dependent_observations(database, artifact_id=artifact_id)
    )
