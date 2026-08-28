# Stage 1 — Learner Workspace and Storage Foundation

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)

Previous: [Stage 0 — Architecture, Contracts, and Repository Boundaries](stage0.md)
Next: [Stage 2 — Language Packs and Onboarding](stage2.md)

## Outcome

Create safe independent learner workspaces backed by transactional DuckDB. At the end of this stage, `workspace init` can create a private `PolishLinguaWiki` fixture, the database can migrate and recover, private paths are excluded from Git, and core/skills/schema versions are pinned without copying core Python source.

## Estimate

- Engineering: 8–12 days.
- Content/review: none beyond synthetic fixture maintenance.

## Entry criteria

- Stage 0 exit gate passes.
- V1 workspace, session-package, content, and lock schemas are frozen.
- A backup root outside the learner repository is available in tests.

## Work packages

### 1.1 Implement database connection safety

- [ ] Resolve the database path only from a validated workspace root.
- [ ] Implement one-writer application locking adjacent to the database.
- [ ] Use short explicit transactions and read-only report/context connections.
- [ ] Return a structured retryable error when another writer holds the lock.
- [ ] Reject unresolved, root-level, home-directory, and out-of-workspace destructive targets.
- [ ] Add a controllable clock for tests.

### 1.2 Implement the initial migrations

Create ordered, checksummed migrations for:

- [ ] schema migration history;
- [ ] workspace identity and installed-version mirror;
- [ ] users and support/native languages;
- [ ] learning tracks, preferences, and settings;
- [ ] language-pack registry and framework metadata;
- [ ] domain events and audit log;
- [ ] projection/job state.

Migration rules:

- [ ] Historical migration files are immutable after release.
- [ ] A failed migration rolls back and leaves the prior database usable.
- [ ] Every migration records application version and checksum.
- [ ] Migration commands create a verified backup before changing a non-empty database.

### 1.3 Implement learner-workspace generation

- [ ] Create the versioned workspace template for `AGENTS.md`, `.gitignore`, `linguawiki.toml`, `pyproject.toml`, and locks.
- [ ] Implement `workspace init <path>` with empty-target validation.
- [ ] Create ignored `data/`, `artifacts/`, `imports/`, `drafts/`, and `exports/anki/` directories safely.
- [ ] Generate committed `.agents/skills/` from the pinned core skill bundle.
- [ ] Create the initial empty privacy-checked wiki projection.
- [ ] Support optional local `git init`, but never create a remote, commit, or push.
- [ ] Refuse to overwrite an initialized or user-modified workspace.

### 1.4 Implement locks and workspace diagnostics

- [ ] Write `linguawiki.lock` atomically after successful initialization.
- [ ] Mirror applied versions/hashes in DuckDB for integrity checking.
- [ ] Implement `workspace status` and `workspace doctor`.
- [ ] Detect modified generated skills, missing ignore rules, schema/core mismatch, and a backup root inside Git.
- [ ] Warn when a configured remote has not been explicitly confirmed as private.
- [ ] Ensure one learner/one primary program is the default without removing multi-track schema support.

### 1.5 Implement backup and recovery

- [ ] Pin DuckDB exactly in `uv.lock`.
- [ ] Implement `db check` and a consistent native backup.
- [ ] Implement portable export: one Parquet file per typed table plus JSON metadata, schema/application versions, counts, and SHA-256 manifest.
- [ ] Implement restore to a new path; never overwrite the active database directly.
- [ ] Verify native and portable backups immediately after creation.
- [ ] Add a retained old-version fixture for storage-format upgrade tests.

### 1.6 Implement privacy checks

- [ ] Define Git-safe paths and forbidden raw/private patterns.
- [ ] Implement `workspace privacy-check` for candidate Git files.
- [ ] Ensure DuckDB, backups, audio, raw transcripts, imports, and Anki exports are ignored by default.
- [ ] Treat generated wiki sanitization as policy filtering, not proof that a repo is safe to publish.

## Expected commands

```text
linguawiki workspace init|status|doctor|privacy-check
linguawiki db init|status|migrate|check|backup|export-portable|restore
linguawiki skills install|check
```

## Verification

```bash
uv run pytest tests/workspaces tests/migrations tests/integration/test_backup_restore.py
uv run linguawiki workspace init <temporary-path> --history git-wiki
uv run linguawiki workspace doctor --workspace <temporary-path> --format json
uv run linguawiki db check --workspace <temporary-path> --format json
```

Also test:

- [ ] two concurrent writers;
- [ ] failure during each migration boundary;
- [ ] native and portable restore equivalence;
- [ ] initialization retry before and after user modification;
- [ ] privacy rejection of fake audio/transcript/database Git candidates;
- [ ] an attempted workspace inside the core repository;
- [ ] a generated workspace containing no `src/linguawiki` copy.

## Exit gate

- [ ] `PolishLinguaWiki` fixture initializes independently from a tagged core fixture.
- [ ] `workspace doctor` passes with pinned core/schema/skill hashes.
- [ ] Initialization never overwrites real data.
- [ ] Failed writes and migrations roll back completely.
- [ ] Writer contention is explicit and retryable.
- [ ] Native and portable backups both restore and pass integrity checks.
- [ ] Private artifacts cannot become normal Git candidates.
- [ ] Core remains free of real learner state.

## Handoff to Stage 2

Stage 2 receives a safe empty learner workspace, an initialized database, pack/framework registry tables, version locks, backup/recovery, and Codex skill installation. It may add learner and pack content without redesigning storage ownership.
