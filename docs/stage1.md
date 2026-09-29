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

- [x] Resolve the database path only from a validated workspace root.
- [x] Implement one-writer application locking adjacent to the database.
- [x] Use short explicit transactions and read-only report/context connections.
- [x] Return a structured retryable error when another writer holds the lock.
- [x] Reject unresolved, root-level, home-directory, and out-of-workspace destructive targets.
- [x] Add a controllable clock for tests.

### 1.2 Implement the initial migrations

Create ordered, checksummed migrations for:

- [x] schema migration history;
- [x] workspace identity and installed-version mirror;
- [x] users and support/native languages;
- [x] learning tracks, preferences, and settings;
- [x] language-pack registry and framework metadata;
- [x] domain events and audit log;
- [x] projection/job state.

Migration rules:

- [x] Historical migration files are immutable after release.
- [x] A failed migration rolls back and leaves the prior database usable.
- [x] Every migration records application version and checksum.
- [x] Migration commands create a verified backup before changing a non-empty database.

### 1.3 Implement learner-workspace generation

- [x] Create the versioned workspace template for `AGENTS.md`, `.gitignore`, `linguawiki.toml`, `pyproject.toml`, and locks.
- [x] Implement `workspace init <path>` with empty-target validation.
- [x] Create ignored `data/`, `artifacts/`, `imports/`, `drafts/`, and `exports/anki/` directories safely.
- [x] Generate committed `.agents/skills/` from the pinned core skill bundle.
- [x] Create the initial empty privacy-checked wiki projection.
- [x] Support optional local `git init`, but never create a remote, commit, or push.
- [x] Refuse to overwrite an initialized or user-modified workspace.

### 1.4 Implement locks and workspace diagnostics

- [x] Write `linguawiki.lock` atomically after successful initialization.
- [x] Mirror applied versions/hashes in DuckDB for integrity checking.
- [x] Implement `workspace status` and `workspace doctor`.
- [x] Detect modified generated skills, missing ignore rules, schema/core mismatch, and a backup root inside Git.
- [x] Warn when a configured remote has not been explicitly confirmed as private.
- [x] Ensure one learner/one primary program is the default without removing multi-track schema support.

### 1.5 Implement backup and recovery

- [x] Pin DuckDB exactly in `uv.lock`.
- [x] Implement `db check` and a consistent native backup.
- [x] Implement portable export: one Parquet file per typed table plus JSON metadata, schema/application versions, counts, and SHA-256 manifest.
- [x] Implement restore to a new path; never overwrite the active database directly.
- [x] Verify native and portable backups immediately after creation.
- [x] Add a retained fixture and a real gate for storage-format upgrade tests. The retained
      `tests/fixtures/portable-export/schema-7/` pins the export *shape*; cross-version
      compatibility is proven by `scripts/check_duckdb_upgrade.py`, which installs both DuckDB
      versions for real. See the implementation note below.

### 1.6 Implement privacy checks

- [x] Define Git-safe paths and forbidden raw/private patterns.
- [x] Implement `workspace privacy-check` for candidate Git files.
- [x] Ensure DuckDB, backups, audio, raw transcripts, imports, and Anki exports are ignored by default.
- [x] Treat generated wiki sanitization as policy filtering, not proof that a repo is safe to publish.

## Expected commands

```text
linguawiki workspace init|status|doctor|privacy-check|confirm-remote|lock-dependencies
linguawiki db init|status|migrate|check|backup|export-portable|restore
linguawiki skills install|check
```

## Verification

```bash
uv run pytest tests/workspaces tests/migrations tests/integration/test_backup_restore.py
uv run python scripts/verify.py
uv run python scripts/verify.py --clean-environment   # release gate; needs uv and an index
uv run python scripts/verify.py --duckdb-upgrade <version>   # required before a duckdb bump
uv run linguawiki workspace init <temporary-path> \
  --backup-root <path-outside-that-workspace> --history git-wiki
uv run linguawiki workspace doctor --workspace <temporary-path> --format json
uv run linguawiki db check --workspace <temporary-path> --format json
```

Also test:

- [x] two concurrent writers, in-process and across processes;
- [x] a deleted lock file, a half-written lock payload, and a lock left by a killed process;
- [x] an interrupted initialization at each write boundary, and its retry;
- [x] a tampered, truncated, and manifest-less backup;
- [x] a backup reason that tries to escape the backup root;
- [x] a hole in the applied migration history;
- [x] a pre-existing directory at a staging-shaped path, and concurrent staging uniqueness;
- [x] an unexpected top-level Git candidate;
- [x] a `uv.lock` that is invalid, incomplete, or resolves another core release;
- [x] workspaces, backups, and exports beneath paths containing quotes, backslashes, and spaces;
- [x] a `uv.lock` that omits the workspace project or the core dependency graph;
- [x] a remote added, repointed, or confirmed before any remote existed;
- [x] `db init` against a real earlier-release database holding data, and against an unrelated
      DuckDB file holding somebody else's tables;
- [x] a lock whose workspace does not depend on the core, whose edges dangle, or whose pinned
      dependency versions are fabricated;
- [x] a remote whose push endpoint diverges from its confirmed fetch endpoint, and a remote URL
      carrying credentials;
- [x] learner data in all 15 tables written by DuckDB 1.4.1 and read, re-exported, and restored
      by 1.5.5, compared per table and by stable identity;
- [x] a payload file removed from a backup manifest, an extra file added, a byte-count change, a
      malformed manifest, and manifest entries naming `..` or absolute paths;
- [x] a lock naming only the core's direct dependencies, and one missing a single transitive
      package;
- [x] several `pushurl` values on one remote, only some of them private;
- [x] `--git-init` failing during initialization, and the retry after it;
- [x] a portable export missing a whole table, carrying an unknown table, or restoring into a
      database that would fail `db check`;
- [x] `db migrate` as well as `db init` against an unrelated DuckDB file;
- [x] a lock claiming `0.0.0` for range-constrained transitive dependencies;
- [x] `git ls-files` failing, and git missing, inside a real repository;
- [x] an export missing a column, whose Parquet lost a column, or claiming schema version 999;
- [x] a workspace nested inside another Git repository, with and without a force-added database;
- [x] a lock stripped of `source` fields and one with a duplicated package identity;
- [x] `confirm_remote` and `workspace doctor` against an unrecognized database;
- [x] a Parquet column retyped VARCHAR -> BLOB and TIMESTAMP -> BIGINT;
- [x] an export with an invented format name, a bad manifest version, no migration history, or a
      duplicated table entry;
- [x] a lock whose every `source` is an empty mapping;
- [x] a database with a corrupt checksum and one claiming a future migration: refused by writers,
      still backed up natively, refused for portable export;
- [x] a payload reference pointing outside the export, and a payload dropped from the manifest;
- [x] a lock with two versions of one package, with and without a version-qualified edge, one
      declaring an unsupported lock format version, and every malformed `dependencies` shape;
- [x] a database with an extra column, a dropped column, a retyped column, an extra table, and a
      dropped table: each damaged, unwritable, natively recoverable, and named by doctor;
- [x] the same drift inside `schema_migrations` itself, end to end through the CLI: doctor reports
      it, `db backup` produces a native layer, and `db restore` recovers it;
- [x] a `schema_migrations` table holding somebody else's migration identifiers, or none at all:
      still disowned;
- [x] tables named to inject SQL (`DROP TABLE`, `DELETE FROM`, `UPDATE`), proving `db backup`
      counts them literally, leaves the source byte-identical, and copies them faithfully;
- [x] failure during each migration boundary;
- [x] native and portable restore equivalence;
- [x] initialization retry before and after user modification;
- [x] privacy rejection of fake audio/transcript/database Git candidates;
- [x] an attempted workspace inside the core repository;
- [x] a generated workspace containing no `src/linguawiki` copy.

## Exit gate

- [x] `PolishLinguaWiki` fixture initializes independently from a released core artifact — automated by `scripts/check_clean_environment.py`, which builds the wheel, installs it into a fresh virtual environment, and runs `workspace init`, `workspace doctor`, and `db check` with no core source on the path. The first published tag replaces the locally built wheel.
- [x] The workspace `uv.lock` resolves the exact core and its transitive dependencies — `workspace lock-dependencies [--find-links <artifacts>]` writes it, `workspace doctor` validates that it resolves the pinned core, and `scripts/check_clean_environment.py` performs the full build → install → init → lock → doctor round trip. Publishing the first tag to an index removes the need for `--find-links`; nothing else changes.
- [x] `workspace doctor` passes with pinned core/schema/skill hashes.
- [x] Initialization never overwrites real data.
- [x] Failed writes and migrations roll back completely.
- [x] Writer contention is explicit and retryable.
- [x] Native and portable backups both restore and pass integrity checks.
- [x] Private artifacts cannot become normal Git candidates.
- [x] Core remains free of real learner state.

## Shared invariants

Four properties are enforced in exactly one place each, because the recurring defect in
this stage was two commands disagreeing about the same question. Each has direct tests in
`tests/unit/test_invariants.py`, independent of any command that relies on it.

| Invariant | Home | Question it answers |
|---|---|---|
| Complete schema | `db/schema.py` (`expected_schema`, `assert_matches_schema`, `assert_known_schema_version`) | Is this layout *exactly* the schema of a version this release can build — every table, and every column in order with its type? Derived by applying the migrations, so it cannot drift from `TABLE_ORDER`. |
| Trustworthy payload | `db/backup.py` (`payload_file_name`, `resolve_payload`, `manifest_coverage`) | Does this reference name the one canonical file, inside the export, that the manifest checksums? |
| Safe SQL identifier | `db/connection.py` (`quote_identifier`, `quote_identifiers`, `Database.count`), enforced by `scripts/check_sql_identifiers.py` | Is every identifier reaching SQL quoted, given that catalog-derived names are attacker-controlled data? |
| Restorable layer | `db/backup.py` (`portable_layer_is_possible`), `db/state.py` | Can this backup layer actually restore the database it claims to preserve? |
| Recognized database | `db/state.py` (`classify_database`, `assert_writable`, `schema_divergence`) | Empty, ours-and-sound, ours-and-damaged, or somebody else's? Ownership comes from the migration history; soundness from the **complete schema**, so this invariant is built on the one above rather than duplicating a weaker version of it. Enforced inside `open_writer`. |
| Resolved dependencies | `services/workspace.py` (`PackageIdentity`, `_dependency_edges`, `_resolve_edge`, `inspect_dependency_lock`) | Does the lock, in a format version this release supports, resolve the whole transitive graph *by (name, version, source) identity* — with every edge a well-formed mapping selecting exactly one node — at versions that satisfy the core's specifiers? uv's own verdict (`uv_lock_verdict`) runs in `lock-dependencies`, where uv is already required. |
| Trustworthy Git listing | `services/privacy.py` (`effective_git_root`, `candidate_listing`) | Which repository would actually track this workspace, ancestors included, and did inspection succeed? "No repository" and "inspection failed" are distinct, and the second fails closed. |

Each was initially narrower than its name, and the recurring cause was **lossy normalization
before validation**: logical column types collapsed into physical Parquet types (VARCHAR and BLOB
are both `BYTE_ARRAY`); a table *sequence* mapped by name, hiding duplicates; uv source variants
flattened to "is a mapping"; and a damaged database flattened to "managed". Each round the fix was
to stop discarding the distinction, not to special-case the example.

## Implementation notes

Recorded while completing this stage; Stage 2 inherits these decisions.

- **`--history` accepts only `git-wiki`.** The frozen `lingua.workspace.v1` contract declares
  `history_policy` as the constant `git-wiki`, so `workspace init` refuses the other three
  policies documented in the plan (`portable-snapshot`, `git-portable-snapshot`, `local-only`)
  with a `unsupported_history_policy` error that names them. The flag is still explicit rather
  than defaulted silently, and the `workspaces.history_policy` column already admits all four,
  so widening the choice is a contract amendment plus a CLI change, not a schema migration.
- **`workspace confirm-remote` was added** beyond the expected command list. `workspace doctor`
  has to warn until a Git remote is *explicitly* confirmed private, which requires somewhere to
  record that decision; it is stored as the `workspace`-scoped `remote_privacy_confirmed`
  setting and can be revoked with `--not-private`.
- **Native backups copy the checkpointed file** rather than using DuckDB's
  `COPY FROM DATABASE`, which recreates tables in catalog order and trips over the schema's
  foreign keys. The copy is taken under the writer lock and is opened and verified before it is
  trusted.
- **Timestamps are naive UTC `TIMESTAMP` columns.** DuckDB's `TIMESTAMPTZ` to Python conversion
  requires `pytz`, and binding an aware datetime to it silently converts to process-local time.
  `clock.naive_utc` / `clock.aware_utc` are the only conversion boundary.
- **Exit codes are three-valued**: `0` succeeded, `1` ran and reported failures in its own
  payload (`workspace doctor`, `db check`, `workspace privacy-check`, `skills check`), `2` the
  command itself failed and printed an error envelope on stderr.
- **The success envelope gained `warnings`** (additive, defaulted) so diagnostics can separate
  warnings from failures. `linguawiki.cli.status.v1` moved to `stage: 1`,
  `persistence: "available"`, and gained `database_schema_version`.
- **The Git-safe top-level allowlist is enforced, not merely documented.**
  `services.privacy.GIT_SAFE_TOP_LEVEL` is now checked by `workspace privacy-check` and
  `workspace doctor` alongside the private-pattern rules: an untracked `learner-notes.txt`
  previously passed with no violations. The pattern rules only catch artifacts we already know
  how to name; the allowlist catches everything else. A learner who wants a new top-level entry
  has to add it to the allowlist deliberately.
- **The workspace `uv.lock` is validated as a closed dependency graph.** `workspace doctor`
  requires the lock to resolve the workspace's own project, that project to *depend on* the core,
  the core to be pinned at the version `linguawiki.lock` names, every declared dependency edge in
  the lock to resolve inside the lock, the core's requirements to be reachable from the workspace
  project, and each dependency the core pins exactly to appear at that exact version. Three
  Four earlier versions of this check were too weak in turn: a file merely named `uv.lock`; a
  four-line stub with a `linguawiki` entry; a name-complete graph with fabricated `1.0.0` versions
  and no edge from the workspace to the core; and one that validated only the core's *direct*
  dependencies, so a lock naming those five and dropping every transitive package passed. The
  required set is now the real transitive closure, walked from the installed distributions'
  metadata with `packaging` evaluating environment markers — which is why `packaging` is a
  declared core dependency. The *whole* specifier set is retained and intersected across
  requirers, and each resolved version must satisfy it: keeping only exact `==` pins discarded
  every range, so a lock could claim `0.0.0` for the nine range-constrained packages and pass.
  The graph is keyed by **(name, version, source) identity**, the way uv identifies a node: name
  alone collapsed two legitimately resolved versions, and name-and-version alone would reject the
  same version resolved from two different sources as a duplicate. An edge narrows by whichever
  qualifiers it states — version, source, or neither — and must select exactly one node; if
  several still match, the lock is ambiguous. Edge *shapes* are validated before traversal: a
  parsable-but-wrong `dependencies = 1` used to raise a `TypeError` and surface as
  `internal_error` instead of a failed `dependency_lock` check, so every malformed shape now
  returns a named problem. The lock's own format version must also be one this release supports. Structural fields uv requires are validated too — every package needs a
  `source` that is
  *exactly one recognised uv variant* with a non-empty location, package identities must be
  unique, and an edge to a duplicated name must specify a version. Requiring merely "a mapping"
  still accepted `source = {}`, which `uv lock --check --offline` rejects as matching no variant.
  `workspace lock-dependencies` runs `uv lock --check` as its authoritative verdict. Doctor
  deliberately does not: `uv lock --check` needs an index or a warm cache, and I measured it
  rejecting a *valid* lock with `UV_OFFLINE=1` on a cold cache. A diagnostic that fails when the
  network is down is worse than the offline structural checks, so uv's verdict stays where uv is
  already a prerequisite. `lock-dependencies` additionally runs
  `uv lock --check`. The positive test uses a lock `uv` actually produced, retained at
  `tests/fixtures/workspace-uv-lock/uv.lock`; hand-written locks appear only as negative cases,
  since rejecting them is the whole point of the check.
- **A remote confirmation covers every endpoint and stores no raw URLs.** Git allows a private
  fetch URL and a public push URL on the same remote, so confirming only `(fetch)` confirmed only
  half the exposure — and it allows *several* `pushurl` values, so a `{direction: url}` mapping
  silently kept only the last one and ignored a public destination listed before it. Endpoints are
  read with `git remote get-url --all [--push]` and stored per direction as a *set* of sanitized
  display forms plus SHA-256 fingerprints; credentials embedded in a URL therefore never reach
  DuckDB, the audit log, or a doctor report, and adding, removing, or repointing any endpoint
  invalidates the confirmation.
- **A remote privacy confirmation is bound to the remotes it covered.** It records each remote's
  name and fetch URL rather than a single workspace-wide boolean, so adding a remote or
  repointing a confirmed one makes the confirmation stale and `workspace doctor` warns again.
  Confirming with no remote configured is refused (`no_remote_configured`), because it would
  record a confirmation covering nothing.
- **A backup root inside any Git repository is a failure, not a warning.** Native databases and
  portable exports hold raw learner state, so `workspace init` now refuses such a root
  (`backup_root_invalid`) and `workspace doctor` fails rather than warning. A warning left
  `doctor.ok` true, which is exactly the posture the plan forbids.
- **`--git-init` and `--uv-lock` run in staging, before publication.** Git initialization used to
  happen after the atomic publish, so a failure returned an error on an already-published
  workspace and the identical retry took the idempotent path and never initialized Git. Both
  optional steps now happen inside the staging directory, so an interrupted run publishes nothing
  and the retry is an ordinary first initialization.
- **Ownership is detected without typed decoding.** Classification decoded migration rows before
  validating the history table's own layout, so drift *inside* `schema_migrations` lost the very
  recovery path the previous fix established: dropping `application_version` disowned the database
  (`UNMANAGED`, so `db backup` refused it), and retyping `applied_at` raised an `AttributeError`
  that surfaced as `internal_error`. Ownership now comes from `recorded_migration_ids`, which
  reads `migration_id` as text and asks only whether any recorded identifier is one this release
  published; `claimed_schema_version` and `raw_history` are likewise text reads. Typed decoding
  declares its precondition (`assert_history_table_decodable`, error code
  `history_table_malformed`), and `history_table_columns` is derived from migration 0001 alone, so
  the history table's shape stays knowable even when a later migration is broken. Native
  fingerprinting compares the raw history, so a damaged database's copy is still verifiable. The
  order is now: ownership (raw) -> soundness (complete schema) -> typed history.
- **Ownership and soundness are separate questions, and soundness is the complete schema.**
  Classification checked the migration history and the table *names*, so a database with an added
  column had a valid history and was called `MANAGED`: ordinary writers mutated it, while restore
  refused the backup it produced. Worse, a *dropped table* fell to `UNMANAGED`, so a database with
  an exact LinguaWiki history could not be backed up at all — the one case where a faithful copy
  matters most. `classify_database` now establishes ownership from the history and then compares
  the complete observed schema against `expected_schema()`; any missing, extra, or retyped table
  or column is `DAMAGED`. Damaged databases refuse ordinary writes, omit the portable layer, and
  keep faithful native backup and restore. `schema_divergence` reports what actually differs, so
  `workspace doctor` can name it.
- **"Ours" and "safe to write" are separate answers.** Any migration-history error used to
  classify as `MANAGED`, after which the writer boundary let writes through: after corrupting a
  checksum, `confirm_remote` still appended an audit row, and an old core could have mutated a
  database a newer release wrote. `DatabaseState` now distinguishes `DAMAGED` (history diverges
  from this release) and `UNSUPPORTED` (history is ahead of it) from `MANAGED`, with an explicit
  `WRITABLE_STATES` set so a future state defaults to refused. Ordinary writers refuse both;
  `db backup` and `db export-portable` pass `allow_damaged=True`, because the database that needs
  repair is exactly the one most worth preserving first. Native backup verification therefore
  checks *fidelity* — that the copy reproduces the source's history, layout, and row counts
  (`database_fingerprint`) — rather than the source's health.
- **The writer boundary enforces the classification.** Sharing `classify_database` was not
  enough while each command had to remember to call it: `confirm_remote` opened a writer and
  updated settings and audit rows in an unrecognized database. `open_writer` now refuses an
  `UNMANAGED` database itself, and refuses an empty one unless the caller is explicitly creating
  or initializing it. `workspace doctor` likewise classifies and runs integrity checks *before*
  reading application tables, so an unrecognized file yields `database_recognized: failed`
  instead of a raw catalog error.
- **Every writing command shares one database classification.** `db init` used to apply pending
  migrations directly, so a populated database could be upgraded with no backup. Then "populated"
  meant only "has LinguaWiki migrations", so an unrelated DuckDB file was treated as an empty slot
  — and fixing that in `db init` alone left `db migrate` still writing seven migrations into a
  stranger's database. `db/state.py` now answers the question once: a file is `MANAGED` only when
  its history matches this release *and* its tables are exactly what that history should have
  produced. `db init` and `db migrate` both refuse `UNMANAGED` (`database_not_empty`); `db init`
  additionally defers a populated database to `db migrate` (`migration_required`), which backs up
  first.
- **The DuckDB upgrade gate installs two DuckDB versions for real.** The retained Parquet fixture
  cannot prove cross-version compatibility, because its Parquet is always rebuilt by whichever
  DuckDB is installed — it pins the export shape and nothing more, and the documentation and test
  name now say so. `scripts/check_duckdb_upgrade.py` writes learner data with the outgoing version
  and requires the candidate to open the native database, re-export it, and restore both the
  native and the portable backup. It first seeds *every* Stage 1 table
  (`scripts/duckdb_upgrade_support.py`) and refuses to proceed if any table is left empty, because
  an empty-workspace backup proves nothing about tracks, packs, or jobs. Each step is then compared
  per table — row count, column list, and a content hash of the ordered rows — plus the stable
  identities (workspace, user, track, pack checksum, event IDs, and the wiki projection row);
  comparing aggregate row totals hid a table that silently emptied. Verified in both directions:
  1.4.1 -> 1.5.5 preserves all 33 rows across 15 tables; 1.5.5 -> 1.3.2 fails, as it must. Run it
  via `scripts/verify.py --duckdb-upgrade <version>` before changing the `duckdb` pin.
- **Filesystem paths never reach DuckDB as SQL string literals.** Parquet export, portable
  verification, and portable restore pass paths as query parameters, so a workspace or backup
  under a directory such as `O'Brien` works. The same class of defect existed in the workspace
  templates: `linguawiki.toml` interpolated `backup_root` raw, so a path containing `"` or `\`
  produced unparseable TOML and made initialization impossible. Templates now escape through
  `tomlstr` / `jsonstr` Jinja filters.
- **Git inspection asks the repository that would actually track the workspace.** Looking for
  `<workspace>/.git` reported "not a Git repository" for a workspace nested inside another
  repository and fell through to the filesystem listing, which skips private artifacts — so a
  force-added database in a parent repository was reported safe. The effective root is now found
  through ancestors and inspected with a workspace-scoped pathspec; `workspace doctor` also warns
  that the workspace is nested, since a learner workspace is normally its own repository.
- **A failed Git inspection fails closed.** `check_privacy` returned the same value for "not a
  Git repository" and "`git ls-files` failed", then fell back to a filesystem listing that
  deliberately skips private artifacts — so a workspace force-adding its DuckDB file was reported
  safe when git errored. The listing now carries an explicit source, `unavailable` is never
  treated as a successful scan, and `workspace doctor` fails on `privacy_inspection`. Related: the
  contract models use `use_enum_values`, so enum comparisons must use `==`; an `is` comparison
  silently re-opened this exact fail-open path until a test caught it.
- **`data/` and `drafts/`** are ignored per workspace rather than through the shared privacy
  policy, because those directory names are not intrinsically private in the core repository.
  `services.privacy.required_ignore_rules` is the single list `workspace doctor` checks.
- **The writer lock is a non-blocking `fcntl.flock` on an open descriptor**, and the lock file
  is never unlinked. An earlier read-then-unlink stale-recovery scheme let two callers both
  declare a lock abandoned, let one delete the other's live lock, and treated a half-written
  payload as free; the payload is now diagnostics only and never decides ownership. A lock left
  by a dead process is released by the OS. DuckDB's own exclusive database lock is the second
  layer: deleting the lock file yields a retryable `database_busy`, not a second writer.
- **`workspace init` stages and publishes atomically.** The workspace is built in a uniquely
  named `.<name>.linguawiki-init-*` sibling (`tempfile.mkdtemp`) and moved into place with one
  `os.replace`, because writing directly into the target left an unrecoverable half-initialized
  workspace when a run was interrupted — retrying failed with `workspace_lock_missing`. Only the
  directory this invocation created is ever removed: an earlier fixed staging path was deleted
  unconditionally, which destroyed anything already there and let concurrent initializers delete
  each other's live staging. Staging directories left by interrupted runs are reported as
  warnings for the learner to inspect, never removed. `mkdtemp` creates the directory `0700` and
  the mode survives the rename, so a workspace is private to its owner from publication.
- **Catalog-derived identifiers are data, and every one is quoted.** `database_fingerprint`
  iterates every table in the catalog, so it reached tables outside the schema registry — and
  anyone who can add a table chooses its name. Wrapping a name in a bare `"{table}"` let a table
  called `x"; DROP TABLE jobs; --` terminate the identifier and append statements, so `db backup`,
  a *recovery* command, could destroy the data it was preserving. `quote_identifier` doubles
  embedded quotes (and refuses NUL bytes), `Database.count` is the safe counting primitive, and
  every interpolation in `db/` now routes through them — including `raw_history`, whose column
  names come from a possibly-drifted table, and the orphan-relation joins. The allowlist in
  `_registered_table` is kept as defence in depth, not as the escaping mechanism.
  `scripts/check_sql_identifiers.py` fails the gate on any hand-quoted placeholder in interpolated
  SQL, since the property is syntactic and a reviewer should not have to spot it again.
- **A payload reference is a name, not a path.** `TableExport.file` was free-form, so metadata
  could point a table at an absolute path outside the export — restoring unchecksummed data while
  reporting `verified: true`. There is now exactly one legal value, `<table>.parquet`, enforced by
  the model; `resolve_payload` confirms the file resolves inside the export directory *and* is
  covered by the manifest, so the metadata's references and the manifest's entries can no longer
  describe different file sets. `manifest_coverage` finds the manifest whether the export stands
  alone or sits inside a `db backup` directory.
- **A backup layer is only produced when it could restore.** A database whose migration history
  this release cannot replay has no valid portable form, because a portable restore is a migration
  replay — yet `db backup` produced one and called it verified, and the default restore then chose
  it and failed. Damaged and unsupported databases now get a **native-only** recovery backup with
  the omission reported in `skipped_layers`; a standalone `db export-portable` refuses them
  (`portable_export_unsupported`); and a portable-only backup of such a database is an error rather
  than a silent no-op. Native restore accepts them — a recovery backup you can never restore is
  pointless — and reports the preserved `state`, keeping fidelity ("the copy reproduces the
  source") separate from health ("the result is sound"), the same split already used for native
  backup verification.
- **Column types are compared as DuckDB infers them, not as Parquet stores them.** A mapping from
  schema types to physical Parquet types made VARCHAR and BLOB indistinguishable (both
  `BYTE_ARRAY`), as well as TIMESTAMP and BIGINT (both `INT64`), so a text column could be
  rewritten as binary and restore as an escaped byte string — `Zażółć LinguaWiki` became
  `Za\xC5\xBC...`. `parquet_columns` now reads DuckDB's own inferred logical types via
  `DESCRIBE SELECT * FROM read_parquet(?)`, which is the type the restore will actually insert;
  the physical-type table is gone.
- **The backup formats are enforced contracts.** `schema_name` and `schema_version` were ordinary
  fields, the manifest's version was never read, and mapping the table sequence by name hid
  duplicates — so an export with an invented format name, manifest version 999, no migration
  history, and a duplicated `workspaces` entry restored as `verified: true` with 16 tables and an
  inflated row count. They are now `Literal` fields validated on read
  (`portable_metadata_invalid`), the manifest's name *and* version are checked, duplicate table
  entries and duplicate files are rejected in the model before anything maps them, and the
  recorded migration history must be exactly this release's prefix for the declared schema
  version (`assert_exact_migration_prefix`).
- **A portable export must match the complete schema of a known version.** Validating only the
  tables the metadata happened to name let a whole table be removed and restored as silently
  empty, still reported `verified: true`; validating table *names* then let a column be removed
  the same way, replacing real data with a column default; and the recorded schema version was
  never checked at all, so an export could claim version 999 and restore as "verified". The
  expected layout — tables, and each table's ordered column names and types — is now derived from
  the migrations (`expected_schema`) and enforced at export, at verification, after restore, and
  on native backups, with duplicates, unknown tables, and unknown schema versions refused. The
  metadata must also describe the columns its own Parquet files actually have. A restore
  additionally has to pass the same integrity checks as a live database, and a schema violation
  during load is reported as `restore_verification_failed` rather than escaping as an unexpected
  error.
- **Restore verifies the checksum manifest first, and the manifest must cover every file.**
  Row-count verification alone accepted a tampered or truncated backup. Verifying only the entries
  the manifest happened to list was still exploitable: a payload file could be deleted from the
  manifest and then rewritten. The manifest entry set and the actual payload file set must now
  match exactly, byte counts are checked alongside checksums, the manifest's own structure is
  validated, and entries naming absolute or `..` paths are refused. A managed backup directory
  without a `manifest.json` is refused outright (`backup_manifest_missing`); a bare database file,
  which no manifest covers, is still restorable.
- **`--reason` is a validated slug.** It names the backup directory, so an unconstrained value
  such as `../../../escaped` wrote outside the configured backup root. The final directory is
  also asserted to be under that root.
- **Migration history must be the exact packaged prefix.** Validating each applied row on its own
  accepted a history with a hole in it — deleting migration 3 from a schema-7 database still
  reported `ok`, while that migration's tables were absent. A gap is now
  `migration_history_incomplete`.
- **The workspace `uv.lock` is resolved by `uv`, never fabricated.** `workspace init --uv-lock`
  and `workspace lock-dependencies` shell out to `uv lock`; both fail loudly (`uv_unavailable`,
  `uv_lock_failed`) rather than silently skipping. The workspace pins an exact core *version*
  rather than a path, so uv has to be able to reach that artifact: from an index, or via
  `--find-links` for a release that is built but not yet published. `workspace init --uv-lock`
  reports a failure as an envelope warning instead of an error, since the workspace it created is
  complete and valid; `workspace lock-dependencies` errors, because resolving the lock is its
  only job. `workspace doctor` warns while the lock is
  absent, so the gap in reproducibility is visible rather than invisible.
- **`scripts/check_clean_environment.py`** installs the built wheel into a fresh `uv` virtual
  environment and runs `workspace init`, `workspace doctor`, and `db check` against it. The
  archive-contents check in `scripts/check_distribution.py` never proved the artifact worked.
  It also resolves the workspace `uv.lock` from the built wheel via `--find-links` and asserts
  `workspace doctor` accepts it, which is how the reproducibility gate is closed without
  publishing to a public index. It discovers `uv` from `--uv`, then `PATH`, then the vendored
  `.tools/uv`, and puts it on the child environment's `PATH` so the installed CLI's own `uv`
  calls resolve too — an earlier version only used `uv` for its own setup steps and failed at
  `workspace lock-dependencies` unless the caller had already exported it. Subprocess stderr is
  printed on failure. It runs via `scripts/verify.py --clean-environment` and as an
  `external`-marked test, since it needs a package index.
- **Retained storage-format fixtures** live in `tests/fixtures/portable-export/schema-<n>/` as
  the real export metadata plus one JSON row file per table, because committed Parquet would be
  rejected by the repository privacy scan. `tests/support/portable_fixture.py` rebuilds them
  into real Parquet using only the recorded column types.

## Handoff to Stage 2

Stage 2 receives a safe empty learner workspace, an initialized database, pack/framework registry tables, version locks, backup/recovery, and Codex skill installation. It may add learner and pack content without redesigning storage ownership.
