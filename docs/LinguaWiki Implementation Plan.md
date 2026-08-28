# LinguaWiki Implementation Plan

## 1. Outcome

LinguaWiki will be a local-first, language-agnostic learning system operated through AI skills and deterministic Python tools. It will combine:

- a linked, inspectable Markdown wiki;
- a multidimensional learner model;
- adaptive lesson planning and delivery;
- reading, listening, speaking, writing, pronunciation, and media workflows;
- personal error and evidence tracking;
- reviewed Anki export and feedback;
- periodic assessment and progress reporting;
- installable language packs, initially suitable for Polish and later Chinese.

DuckDB will be the source of truth for every runtime object, including users, language tracks, knowledge entries, sources, sessions, attempts, errors, assessments, review tasks, Anki candidates, and progress. Markdown will be a deterministic projection of database state, plus a controlled draft/import surface. Static code, schemas, templates, and versioned language-pack seed data remain ordinary repository files.

The product is distributed from a general `LinguaWiki` core repository into independent private learner workspaces. A workspace such as `PolishLinguaWiki` pins the core and Polish pack, owns its DuckDB and artifacts locally, and commits only the privacy-checked wiki projection and reproducibility files.

The MVP succeeds when a fresh agent can initialize a language for a learner, establish or calibrate a level, prepare the appropriate resources, plan and conduct a lesson, close it transactionally, update the learner model, render the wiki, and generate a useful review queue without relying on prior chat history.

## 2. Decisions and assumptions

These decisions remove ambiguity before implementation.

1. **Local-first means no required hosted service.** Core workflows work with Python and a local DuckDB file. AI chat/voice, TTS, STT, web search, dictionaries, and AnkiConnect are optional adapters.
2. **DuckDB is authoritative.** Generated Markdown must never be edited in place. Human or agent edits go through an export-to-draft and validated import workflow.
3. **One database can support several users and languages.** A learning track binds one user to one target language, support languages, goals, framework, and settings.
4. **Declared level is a starting hypothesis, not proof of mastery.** Choosing A2 prepares A2-appropriate resources and creates provisional estimates. A short calibration is still scheduled.
5. **Assessment is multidimensional.** Reading, listening, spoken interaction, spoken production, writing, pronunciation/intelligibility, grammar recognition/production, and receptive/productive vocabulary are tracked independently.
6. **The core has no Polish grammar assumptions.** It must not require cases, gender, aspect, whitespace tokenization, an alphabet, or CEFR. Those concepts are data or language-pack capabilities.
7. **Agents never mutate DuckDB with ad hoc SQL.** Skills call stable CLI commands or Python application services with validated inputs.
8. **AI-generated language is untrusted until validated.** Important explanations, examples, pronunciation pairs, resource metadata, and Anki candidates carry provenance and review status.
9. **Markdown is for navigation and auditability, not duplicate state.** The wiki can be rebuilt entirely from DuckDB and checked for drift.
10. **Media files remain filesystem artifacts.** Their metadata, hashes, rights, provenance, transcripts, and relationships live in DuckDB; large binaries do not.
11. **A `user` is a local data-scoping key, not an authenticated identity.** Multi-user rows prevent data mixing and allow later household use, but the MVP has no login, authorization, or protection against another person with filesystem access.
12. **A declared level is valid only inside a pack-declared framework.** `A2` is meaningful for CEFR; an HSK-based track accepts only the HSK levels declared by that pack. Cross-framework mappings must be explicit, versioned pack data and are never guessed by core code.
13. **Codex is the primary repository-agent runtime for the MVP.** Canonical session packages and the Python CLI remain provider-independent, so a live tutor or later repository runtime can change without changing learner data.
14. **Speaking crosses a versioned session-package boundary.** The MVP supports local/external recording and transcript import; native live voice is an adapter, not a prerequisite. A package is transport, not a second source of truth: validated contents enter DuckDB and the package is retained or purged by policy.
15. **AI-assisted pack drafting is allowed with risk-based promotion.** Provenance is recorded per item and separately from linguistic, pedagogical, source-alignment, rights, and privacy review. AI self-review is machine checking, never authoritative verification.
16. **Core, language packs, and learner workspaces have separate ownership.** The `LinguaWiki` core repository contains software, generic skills, schemas, migrations, and synthetic fixtures; versioned language packs contain reusable language content; private learner repositories contain one learner/program's configuration and Git-visible progress projection.
17. **Learner repositories are generated workspaces, not ordinary forks of core.** `linguawiki workspace init` creates an independent repository pinned to a core and pack version. Core upgrades use the workspace upgrader and database migrations, avoiding repeated Git merges between software and learner history.

### 2.1 Resolved implementation defaults

Record these decisions in ADRs and `config/project.toml`:

1. **Repository runtime:** Codex, using validated skill packages and the local CLI. Other runtimes are later adapters.
2. **Voice MVP:** provider-independent `lingua.session.v1` packages created from local/external audio and transcripts, with manual package creation supported first. Native voice and a custom real-time bridge may be added without changing ingestion.
3. **Pack authoring:** AI-assisted drafts are permitted, with item-level provenance, risk-tiered review, and stronger gates for canonical/shareable content.
4. **Learner history:** use `git-wiki` for private learner workspaces: commit sanitized generated wiki/history and lock/config files, while native and portable DuckDB backups remain outside Git in a configured synced location.

Text-only role-play may test pedagogy but does not satisfy speaking/voice acceptance. Chinese variant, Anki note model, optional voice/STT/TTS provider selection, and exact backup destination may remain later decisions, but no personal data should be created until the private learner repository and backup root exist.

## 3. Scope

### MVP

- Repository and Python package bootstrap.
- DuckDB migrations, repositories, services, validation, backups, and integrity checks.
- One user with multiple target-language tracks.
- Language-pack installation and validation.
- Declared-level onboarding and placement-test onboarding.
- Resource preparation by language, level, learner goals, and support language.
- Session planning for 40–120 minutes in roughly 20-minute blocks.
- Text-led learning workflows for all major skills.
- Provider-independent speaking-session package validation/ingestion, transcript/audio evidence capture, and manual recording/transcription workflow; provider automation is optional.
- Evidence-based mastery, personal errors, review tasks, and assessments.
- Reading, podcast, video, course, and real-life source workflows.
- Anki candidate review and UTF-8 TSV export with stable IDs.
- Generated Markdown wiki and weekly reports.
- Polish language pack sufficient to exercise every extension point.

### After the MVP

- Chinese language pack for a selected script/region variant.
- AnkiConnect synchronization and review-stat imports.
- Pluggable STT, TTS, forced alignment, and pronunciation scoring.
- Optional local web UI.
- Corpus and dictionary integrations.
- Rich analytics and experiment tracking.

### Explicit non-goals for the first release

- A custom spaced-repetition scheduler that replaces Anki.
- A server, multi-device synchronization, or accounts/authentication.
- A vector database.
- Automatic downloading or storage of copyrighted books, subtitles, or course pages.
- Fully automated claims of CEFR attainment.
- Automatic publication of unreviewed cards or language explanations.
- Native-like accent scoring as a prerequisite for progress.

## 4. System architecture

```text
Text request -----------------> Codex repository agent
                                      |
Microphone -> live tutor/recorder     | LinguaWiki skills
                  |                   |
                  v                   v
          lingua.session.v1 ----> `linguawiki` Python CLI
             package                  |
                                application services
                                      |
                                staged/final writes
                                      |
                                      v
                            data/linguawiki.duckdb
                              |          |          |
                              v          v          v
                       generated wiki  reports   Anki TSV
                              |
                         Obsidian/editor
```

The live tutor is responsible for conversational flow, prompts, hints, role-play, and immediate corrections. The Codex repository agent is responsible for planning from durable state, validating/ingesting a completed package, updating learner state, producing durable content, and maintenance. They may be the same model or interface, but they communicate through explicit typed events rather than implicit chat memory.

Python is responsible for IDs, state transitions, schemas, transactions, validation, scheduling inputs, reproducible projections, and reports. A skill may ask the CLI for compact context, but should not load the whole database or wiki.

### 4.1 Write path

There are two mutation paths; session observations are deliberately different from ordinary commands.

#### Ordinary mutations

Configuration, catalog, pack, source, review, approval, and maintenance commands validate and commit their complete change in one short DuckDB transaction:

1. Skill supplies structured input and an idempotency key when retries matter.
2. Python validates IDs, enums, references, provenance, limits, and state transitions.
3. `--dry-run` returns the proposed change for ambiguous or bulk operations.
4. Apply writes domain rows, an append-only event, and an audit row atomically.
5. Affected projections are marked stale.

#### Live-session mutations

Per-utterance writes would cause excessive CLI process starts and could expose half-applied learner progress. Instead:

1. The learning skill accumulates structured events in memory during a block: learner attempts, corrections, useful utterance excerpts, error candidates, pronunciation/listening observations, source position, and follow-ups.
2. At a block boundary—or earlier if the buffer becomes large—it calls `session log --input events.json` once.
3. The command validates the whole batch and durably appends it to `session_event_batches` and `session_staged_events` in one transaction. It does **not** update mastery, canonical error patterns, curriculum/source progress, review tasks, reports, or Anki notes.
4. Each batch has a session-scoped sequence number, content hash, and idempotency key. Retrying a batch is a no-op; conflicting reuse of a key is an error.
5. `session close --outcome completed|partial` validates all unconsumed batches, materializes final attempts/evidence/errors/progress/follow-ups/candidates, recomputes estimates, consumes the batches, records a finalization row, and marks projections stale in one transaction.
6. Retrying close returns the prior result from its idempotency key. It never materializes the same staged event twice.

If the process or conversation stops, staged batches remain durable. The next invocation can resume, close the session as `partial`, or abandon it. `abandon` retains staged rows for audit/recovery but excludes them from the learner model; a later explicit recovery may clone them into a new session. The agent should flush a batch at least once per block and before any planned tool/provider transition, limiting loss to the current in-memory portion of a block.

Single-record `evidence record` and `error observe` commands remain available for observations outside a session, imports, and repairs. They commit immediately and require provenance; the learning skill must not use them to bypass session staging.

Use caller-supplied idempotency keys for event batches, close-session, assessment-finalize, import, export, and synchronization commands so retries cannot duplicate evidence or cards.

### 4.2 Read path

Skills ask for purpose-built context bundles, for example:

```bash
linguawiki context session --track pl-main --recent-sessions 3 --format json
linguawiki context concept --track pl-main --concept lwc_... --format json
linguawiki review due --track pl-main --limit 20 --format json
```

The bundle includes only the learner profile, relevant estimates, due items, active errors, recent evidence, current sources, and citations needed for the workflow. This prevents context overload and makes skill behavior testable.

### 4.3 DuckDB concurrency

DuckDB is appropriate for a personal, analytical, local system, but LinguaWiki must assume one writer at a time. All commands will:

- acquire an application-level lock adjacent to the database;
- use short transactions;
- fail with a clear retryable error rather than waiting indefinitely;
- use read-only connections for reports and context where possible;
- never keep a connection open across a long AI conversation;
- create a backup before migrations and destructive imports.

### 4.4 Speaking session-package boundary

`lingua.session.v1` is a provider-independent transport format for a completed or checkpointed speaking block. A minimal package contains:

- stable external session/package ID and schema version;
- target language, track hint, mode, timestamps, and learning targets;
- immutable timestamped raw transcript with stable utterance IDs and speaker roles;
- structured tutor/learning events such as hint, correction, self-correction, task result, pronunciation concern, and topic change;
- transcription provider/model/version and confidence when available;
- references and hashes for local full audio and selected utterance clips;
- privacy classification and retention policy;
- package checksum and producer metadata.

The portable representation is a manifest plus JSONL events/transcript and local artifact references. It may also be streamed directly by a future bridge. Files are an interchange/inbox format only: `session-package ingest` validates and imports them into DuckDB, records the package hash, and converts instructional events to ordinary `session_staged_events`. `session close` remains the only materialization path into mastery, errors, progress, review, and Anki.

Transcript layers must remain distinct:

1. **Raw STT:** immutable provider output, confidence, and timings.
2. **Normalized:** punctuation, paragraphing, and speaker formatting only; no silent correction of learner language.
3. **Reviewed hearing:** what a reviewer believes the learner actually said when STT is doubtful.
4. **Pedagogical correction:** the recommended target-language form, stored as an interpretation/correction rather than transcript text.

Evidence rules:

- grammar, vocabulary, meaning, and discourse may normally use transcript evidence with stated transcription confidence;
- pronunciation, intelligibility, rhythm, stress, tone, and prosody require linked audio evidence;
- a correct STT result does not prove correct pronunciation;
- low-confidence speech creates `possible-transcription-error` or `suspected-pronunciation` observations, not confirmed learner errors;
- selected audio clips are preferred over retaining full recordings indefinitely.

Retention values are `keep`, `rolling-days`, and `delete-after-ingestion`, with the number of rolling days configured separately. Full audio is private and never enters normal Git history. Selected clips may have a longer retention rule when they support an active pronunciation target. Purging an artifact records a tombstone and must invalidate dependent items that can no longer satisfy their evidence requirement.

Delivery progression:

1. Manual package from a recording and transcript.
2. Export adapter for an existing voice application.
3. Local/custom session bridge that captures learner audio, tutor text, timestamps, and events automatically.
4. Optional specialized pronunciation analysis.

The manual path is the MVP and must work before a provider-specific live integration is attempted.

## 5. Repository topology and layouts

### 5.1 Core repository: `LinguaWiki`

The core repository is product source. It contains no real learner database, transcripts, recordings, or progress wiki.

```text
LinguaWiki/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .gitignore
├── config/
│   └── logging.toml
├── src/linguawiki/
│   ├── cli.py
│   ├── config.py
│   ├── ids.py
│   ├── clock.py
│   ├── db/
│   │   ├── connection.py
│   │   ├── migrations.py
│   │   ├── repositories/
│   │   └── sql/
│   ├── models/
│   ├── services/
│   │   ├── workspace.py
│   │   ├── onboarding.py
│   │   ├── resources.py
│   │   ├── assessment.py
│   │   ├── session.py
│   │   ├── session_packages.py
│   │   ├── provenance.py
│   │   ├── evidence.py
│   │   ├── mastery.py
│   │   ├── review.py
│   │   ├── sources.py
│   │   ├── anki.py
│   │   ├── wiki.py
│   │   └── reporting.py
│   ├── policies/
│   ├── renderers/
│   └── adapters/
├── schemas/
│   ├── lingua.session.v1.json
│   ├── lingua.content.v1.json
│   └── lingua.workspace.v1.json
├── .agents/skills/
│   ├── linguawiki/
│   ├── linguawiki-init/
│   ├── linguawiki-pack/
│   ├── linguawiki-assess/
│   ├── linguawiki-learn/
│   ├── linguawiki-speak/
│   ├── linguawiki-source/
│   ├── linguawiki-review/
│   ├── linguawiki-anki/
│   └── linguawiki-maintain/
├── language-packs/
│   ├── schema/
│   ├── fixtures/
│   └── pl-pilot/                # temporary dogfood fixture; extract after pack API stabilizes
├── templates/
│   ├── workspace/
│   ├── wiki/
│   ├── reports/
│   ├── assessment/
│   └── anki/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── migrations/
│   ├── workspaces/
│   ├── language_packs/
│   ├── skills/
│   └── fixtures/
└── scripts/
    └── validate_skills.py
```

The canonical generic skills live here and ship with a tagged core release. The core repository may be public. Its tests use only synthetic learner data.

### 5.2 Language-pack repositories

Reusable production language content may begin in core while the contract stabilizes, then move to separately versioned repositories/packages:

```text
LinguaWiki-Pack-Polish/
├── manifest.yaml
├── capabilities.yaml
├── source-policy.yaml
├── proficiency/
├── seed/
├── assessments/
├── resource-bundles/
├── prompts/
└── tests/
```

Pack releases are immutable and content-addressed. A learner workspace pins exact pack versions. Learner-authored examples, errors, and exercises do not flow back into a shared pack unless they are explicitly redacted, reviewed, and contributed through the pack repository.

### 5.3 Learner workspace repositories

Create one private workspace per learner and learning program. For the initial dogfood, use `PolishLinguaWiki`:

```text
PolishLinguaWiki/
├── AGENTS.md                    # workspace operating contract
├── pyproject.toml               # thin dependency on pinned LinguaWiki core
├── uv.lock                      # exact core/dependency resolution
├── linguawiki.toml              # paths, workspace ID, non-learner runtime config
├── linguawiki.lock              # core/schema/skill/pack versions and hashes
├── .gitignore
├── .agents/skills/              # generated snapshot from pinned core; committed
├── wiki/                        # sanitized generated projection; committed
│   ├── index.md
│   ├── learner/
│   ├── languages/
│   ├── sessions/
│   ├── sources/
│   └── reports/
├── data/
│   └── linguawiki.duckdb        # authoritative; ignored
├── artifacts/                   # recordings/media; ignored
├── imports/                     # session/source inbox; ignored
├── drafts/                      # controlled import staging; ignored by default
└── exports/
    └── anki/                    # ignored unless explicitly sanitized
```

`linguawiki.toml` stores only static workspace/runtime configuration. Learner profile, preferences, progress, and all other mutable domain objects live in DuckDB. `linguawiki.lock` makes the behavior reproducible without duplicating core source.

The committed skill snapshot is generated from the pinned core release and marked as generated. `linguawiki workspace sync-skills` replaces that snapshot deterministically; learner-specific behavior belongs in database preferences or an explicitly separate local extension skill, never as edits inside generated core skills.

### 5.4 Why workspaces should not be ordinary forks

A Git fork is acceptable for a one-off prototype but is not the recommended product model:

- every learner carries the entire product source and history;
- upstream core upgrades become merge work mixed with generated progress changes;
- learners may accidentally push private state into a product pull request;
- pack and schema versions are implicit in Git ancestry rather than explicit locks;
- fixing the core in one learner fork does not establish a clean reusable release.

Instead, `linguawiki workspace init <path>` renders a small independent repository from a versioned template. A hosting service's “use template” function may be a convenience, but the CLI remains canonical because it can also initialize DuckDB, pins, privacy rules, skills, and backup configuration.

### 5.5 Workspace lifecycle and upgrades

```text
core release + pack release
          |
          v
workspace init/sync/upgrade
          |
          v
private learner repository
  - DuckDB authority (ignored)
  - generated wiki history (committed)
  - portable backups (outside Git)
```

`workspace upgrade --core <version> --pack <id@version>` must:

1. resolve and verify release/pack hashes;
2. show a dry-run of dependency, schema, pack, skill, and projection changes;
3. create and verify native plus portable database backups outside the repository;
4. install the pinned core in the workspace environment;
5. migrate DuckDB transactionally;
6. update the generated skill snapshot;
7. apply pack upgrades with their own dry-run/invalidation rules;
8. rebuild and privacy-check the wiki;
9. update `uv.lock` and `linguawiki.lock` only after success.

It does not commit or push. The learner reviews the generated diff and commits it to their private repository. Failed upgrades restore the prior environment/database/locks and leave an audit report.

Default to one learner and one primary target-language program per workspace, even though the schema supports multiple users/tracks. Thus `PolishLinguaWiki` and a later `ChineseLinguaWiki` are clean independent repositories. A future portfolio/reporting tool may aggregate sanitized exports without sharing their DuckDB files.

## 6. Python stack and engineering rules

Use Python 3.12 or newer and package the CLI as `linguawiki`.

Recommended direct dependencies:

- `duckdb` for persistence;
- `pydantic` for command and language-pack validation;
- `typer` for the CLI;
- `rich` for human output while keeping `--format json` machine-stable;
- `jinja2` for wiki/report rendering;
- `python-frontmatter` only if draft imports need it;
- `platformdirs` for optional non-repository data locations.

Development dependencies:

- `pytest`, `pytest-cov`;
- `hypothesis` for state-transition and scheduling invariants;
- `ruff` for lint/format;
- `mypy` or `pyright` for type checks.

Rules:

- Keep SQL explicit and versioned; do not introduce an ORM unless it proves necessary.
- Use UTC timestamps internally and store the user's IANA timezone on the user/track.
- Generate sortable opaque IDs with typed prefixes; do not derive identity from titles.
- Store JSON only for truly extensible payloads. Query-critical data belongs in typed columns/relations.
- Every CLI command supports `--format json`; mutation commands support `--dry-run` where practical.
- Structured errors include a stable code, message, retryability, and field details.
- Dependencies on AI, web, STT/TTS, or AnkiConnect live behind adapters and are absent from the core test suite.
- Tag core releases and publish/install them as a normal Python package or immutable Git reference; learner locks must not follow a moving branch.
- Treat workspace, schema, skill-bundle, and pack compatibility as explicit version ranges checked by `workspace doctor/upgrade`.

## 7. DuckDB data model

The precise DDL should be written as migrations, but implementation should preserve these domains and invariants.

### 7.1 Identity and configuration

| Table | Purpose and important fields |
|---|---|
| `workspaces` | Stable workspace ID, name, history mode, created time, and single/multi-track policy. It identifies the learner repository without using its path or remote URL as identity. |
| `workspace_versions` | Installed core, schema, skill-bundle, pack IDs/versions/hashes, migration time, and upgrade audit reference. Mirrors the lock file for integrity checking. |
| `users` | `user_id`, display name, timezone, created/updated timestamps, status. |
| `user_languages` | Native/support languages and ordered preference; BCP-47 tag and role. |
| `learning_tracks` | User + target language, optional region/script, proficiency framework, declared/current target, goal, status, weekly minutes, session preferences. |
| `track_preferences` | Typed preferences such as correction mode, interests, media genres, accessibility, voice availability, and per-week block targets. |
| `settings` | Versioned project or track settings with scope, key, validated JSON value, and updated timestamp. |

`learning_tracks` is the primary scope key for learner-specific data. A user studying both Polish and Chinese gets separate tracks without duplicating profile data. In the MVP, `user_id` scopes records and selects the active learner; it is not an authentication or authorization boundary.

### 7.2 Language packs and curricula

| Table | Purpose and important fields |
|---|---|
| `language_packs` | Pack ID, BCP-47 language tag, version, checksum, capabilities, installed time. |
| `resource_bundles` | Named bundle such as `cefr-a2-core`, type, level range, dependencies, license, pack version. |
| `resource_bundle_items` | Ordered references to concepts, descriptors, assessment tasks, source recommendations, or templates. |
| `curricula` | Course/book/exam framework metadata, version, source, rights status. |
| `curriculum_units` | Hierarchy, sequence, prerequisites, level, objectives. |
| `track_curriculum_progress` | Track/unit state, position, evidence summary, started/completed timestamps. |
| `proficiency_descriptors` | Framework, skill dimension, level, locale, descriptor, provenance. |
| `content_records` | Base identity and lifecycle for durable instructional content such as pack items, examples, assessment tasks, reusable exercises, and Anki notes. Domain tables reference `content_id`. |
| `content_origins` | One or more item-level origins with controlled class, source/learner/generation reference, locator, transformation, and content hash. |
| `content_reviews` | Independent review axis, state, reviewer kind/identity, method, evidence/reference, timestamp, and reviewed content hash. |
| `content_dependencies` | Content/source/template/artifact dependency plus expected hash and invalidation behavior. |
| `generation_runs` | Provider/model/version where known, prompt-template version, parameters hash, batch ID, timestamps, and privacy classification. |
| `prompt_templates` | Versioned generation/review template, intended content kinds, maturity, known failure modes, and validation history. |

Pack install/update must be content-addressed and idempotent. User annotations and evidence reference stable pack object IDs and must survive pack upgrades.

### 7.3 Knowledge graph

Avoid a vocabulary-only schema. Chinese and Polish need different linguistic structures.

| Table | Purpose and important fields |
|---|---|
| `knowledge_items` | Stable item/content ID, language, kind, canonical title, body/summary, level range, timestamps. Origin, lifecycle, reviews, and dependencies live in the shared content tables. Kinds include `concept`, `lexeme`, `sense`, `form`, `construction`, `grammar`, `pronunciation`, `character`, `pragmatics`, `culture`, and `skill_strategy`. |
| `knowledge_aliases` | Search/display aliases with script, locale, normalization, and uniqueness scope. |
| `knowledge_relations` | Typed directed edges: prerequisite, form-of, sense-of, contrast, collocation, government, example-of, related, error-target, and curriculum objective. |
| `examples` | Example text, translation/glosses, source, verification status, difficulty, audio/artifact reference. |
| `item_tags` | Themes, linguistic features, level, frequency band, and user-defined tags. |
| `track_item_state` | Track-specific stage, confidence, priority, first/last encounter, next rich review, and aggregate evidence counts. |

The learner stage enum is:

```text
unseen -> encountered -> recognized -> understood
       -> controlled-production -> spontaneous-production -> stable
```

Stages may regress based on delayed failures. Store the current aggregate stage in `track_item_state`, but retain all raw evidence so it can be recomputed.

### 7.4 Sources and artifacts

| Table | Purpose and important fields |
|---|---|
| `sources` | Type, title, creator, URL/catalog reference, language, estimated level, rights/license, provenance, transcript/audio availability, review status. |
| `source_segments` | Chapter/page/timestamp range, short permitted excerpt, notes, difficulty, parent segment. |
| `track_source_progress` | Current position, status, unaided/aided comprehension, dates, minutes. |
| `artifacts` | Local relative path, MIME type, size, SHA-256, origin, rights, created time. |
| `session_packages` | External package ID, schema version, producer/provider, package hash, manifest, privacy/retention policy, ingest status, session link, and timestamps. |
| `utterances` | Immutable raw STT/manual utterance, speaker, timings, confidence, audio artifact/clip reference, and package/session link. |
| `transcript_revisions` | Normalized or reviewed-hearing text linked to the immutable utterance, editor/reviewer, reason, confidence, and revision hash. |
| `utterance_interpretations` | Pedagogical correction, meaning interpretation, error-versus-transcription classification, confidence, and reviewer. |
| `pronunciation_observations` | Utterance/audio reference, dimension, suspected/confirmed status, evidence strength, reviewer, and target item. Confirmation requires available audio. |
| `source_item_links` | Connects source segments to knowledge items, examples, errors, and exercises. |

Reject absolute paths outside configured artifact roots. Store only short quotations unless the rights field explicitly permits full local storage. Package files and audio are artifacts/inbox transport; imported transcript, event, provenance, and retention state live in DuckDB.

### 7.5 Sessions, blocks, attempts, and evidence

| Table | Purpose and important fields |
|---|---|
| `sessions` | Track, planned/actual duration, mode, status, started/closed times, fatigue, summary, idempotency key. |
| `session_blocks` | Order, block type, planned/actual minutes, objective, difficulty, source/curriculum link, status. |
| `activities` | Concrete exercise/conversation/reading task and structured prompt/settings. |
| `session_event_batches` | Session, monotonic batch sequence, idempotency key, content hash, event count, validation status, created time. |
| `session_staged_events` | Durable but provisional learner events with event kind, schema version, structured payload, block/activity reference, and consumed/finalization reference. |
| `session_finalizations` | Session, outcome (`completed` or `partial`), idempotency key, consumed batch range, materialized record counts, calculation versions, timestamp. |
| `attempts` | Learner response, modality, correctness rubric, score, latency, help level, correction mode, assessor, timestamps. |
| `evidence` | Atomic claim about a skill/item: recognition, controlled use, spontaneous use, comprehension, intelligibility, etc.; polarity, strength, context, delay, attempt link. |
| `session_observations` | Fatigue, confidence, strategy, notable success, or freeform teacher note with category and salience. |
| `followups` | Action, due window, priority, status, originating session. |

Store long conversation transcripts only if the learner opts in. Otherwise stage and materialize summaries plus selected learner utterances that justify evidence and corrections. Staged payloads follow the same retention policy and must not become a privacy loophole.

### 7.6 Errors and review

| Table | Purpose and important fields |
|---|---|
| `error_patterns` | Track, category, normalized signature, description, target item, status, first/last seen, severity. |
| `error_occurrences` | Attempt/session, learner form, corrected form, explanation, meaning impact, confidence. |
| `error_evidence` | Successful controlled/spontaneous/delayed uses that support monitoring or resolution. |
| `review_tasks` | Rich task outside Anki, task kind, target, rationale, priority, due time/window, state, scheduling policy. |
| `review_events` | Completion, outcome, help, resulting evidence, reschedule reason. |

Error states are `observed`, `active`, `monitoring`, `resolved`, and `reactivated`. Resolution policy must require configurable repeated success across context and delay; one correct answer is never sufficient.

### 7.7 Assessments and proficiency estimates

| Table | Purpose and important fields |
|---|---|
| `assessment_definitions` | Versioned assessment form, purpose, framework, level range, skills, pack provenance. |
| `assessment_runs` | Track, form version, baseline/weekly/monthly/milestone type, status, conditions, timestamps. |
| `assessment_tasks` | Skill, prompt/source, rubric, target descriptors, permitted help. |
| `assessment_results` | Raw score, rubric dimensions, evidence links, assessor, confidence. |
| `assessment_item_exposures` | Track/task/form, first/last exposure, purpose, attempt count, anchor flag; used to prevent accidental retesting. |
| `placement_dimension_state` | Run/dimension, prior/posterior grid, items used, content-family coverage, stop reason, confidence label, algorithm version. |
| `skill_estimates` | Track, dimension, framework level/range, numeric internal score, uncertainty, as-of time, calculation version. |
| `estimate_history` | Immutable snapshots for progress reporting. |

An estimate must expose uncertainty and evidence recency. Never collapse the profile into one global level unless explicitly requested, and label any global result as a summary.

### 7.8 Anki

| Table | Purpose and important fields |
|---|---|
| `anki_notes` | Stable LinguaWiki ID/content ID, track, note type, structured fields, source, status, quality flags, and approval review reference. |
| `anki_cards` | Optional card-level identity returned by Anki, template/ordinal, state. |
| `anki_tags` | Normalized tags. |
| `anki_exports` | Export ID, checksum, path, note count, created time, result. |
| `anki_sync_state` | External note/card IDs, last content hash, last sync time. |
| `anki_review_stats` | Imported reviews/aggregates, ease/outcome, interval, time, import provenance. |

Statuses are `candidate`, `needs-review`, `approved`, `exported`, `synced`, `rejected`, and `retired`. A uniqueness constraint on stable ID and content hashes prevents duplicates.

### 7.9 Audit, jobs, and schema state

| Table | Purpose and important fields |
|---|---|
| `schema_migrations` | Applied version, checksum, timestamp, application version. |
| `domain_events` | Append-only event type, aggregate, payload, correlation/idempotency IDs, timestamp. |
| `audit_log` | Actor, command, affected records, before/after summary, timestamp. |
| `projection_state` | Wiki/report projection version, last event, generated time, stale flag. |
| `report_runs` | Weekly/monthly/milestone period, calculation version, source-event watermark, structured metrics, narrative summary, generated time. |
| `jobs` | Local import/export/render job status and structured error. |

Audit/event payloads must avoid needless duplication of private transcripts or media.

## 8. Language-pack contract

Each pack is a versioned directory validated against a manifest schema:

```text
language-packs/<bcp47>/
├── manifest.yaml
├── capabilities.yaml
├── source-policy.yaml
├── proficiency/
├── seed/
│   ├── knowledge.jsonl
│   ├── relations.jsonl
│   └── examples.jsonl
├── assessments/
├── resource-bundles/
├── references/
├── prompts/
└── tests/
```

`manifest.yaml` declares:

- BCP-47 tag, display names, version, license, and maintainers;
- scripts and writing direction;
- supported proficiency frameworks;
- tokenizer/normalizer capabilities, if any;
- optional morphology, segmentation, romanization, and pronunciation adapters;
- available resource bundles and dependencies;
- supported assessment modalities;
- support-language coverage;
- checksums and data format versions.

`source-policy.yaml` declares language/region-appropriate authoritative source classes and escalation rules. The core may prefer official institutions, academic grammars/dictionaries, established teaching materials, corpora, and authentic usage over crowdsourced or AI-only evidence, but individual source rankings belong to the pack and must not hard-code Polish institutions into generic logic.

The core operates on capabilities, never on language-name conditionals. Examples:

- Polish can expose inflection, grammatical case, aspect pairs, Latin spelling, and contrastive pronunciation targets.
- Mandarin Chinese can expose character/word segmentation, simplified or traditional script, pinyin/tones, measure words, and no inflectional-case capability.

Language-specific prompts should describe likely learner-transfer issues only when the user's support/native language is known. A Russian-to-Polish contrast module is a pack extension, not a core rule.

### 8.1 Minimum viable language pack

A pack is installable when it has:

- valid metadata and licenses;
- proficiency descriptors or a declared framework mapping;
- a starter assessment bank for supported skills;
- a level-indexed resource manifest;
- high-value knowledge seeds with provenance;
- pronunciation/writing-system orientation;
- prompt fragments for correction and examples;
- pack validation tests.

It does not need an exhaustive dictionary. Resource preparation may propose external sources and create a small, level-appropriate initial graph.

### 8.2 Pack update policy

- Import by stable source IDs and pack version.
- Never overwrite learner-created content.
- Mark changed seed content for review when a learner annotation or Anki note depends on it.
- Produce a dry-run diff before update.
- Retain enough version metadata to explain historical assessment results.

### 8.3 Pack maturity and coverage gates

Pack authoring is a first-class workstream and is likely to cost at least as much as the core software. A pack must declare one of these maturity levels; onboarding may promise only what that level supports.

#### `fixture`

Synthetic data for core contract tests. It is never offered to learners.

#### `pilot`

A narrow, explicitly labeled slice for early dogfooding. It may support declared-level calibration and lessons in selected themes, but not an unrestricted placement claim. A pilot slice for one band should contain at least:

- 60–100 reviewed knowledge targets across four to six practical themes;
- 15–25 grammar/construction targets;
- 10–20 pronunciation, orthography, or writing-system targets;
- 24 objective diagnostic items distributed across supported receptive/form dimensions;
- eight productive prompts with rubrics across speaking and writing;
- six reusable activity templates covering at least four learning modes;
- one reviewed starter source recommendation for each supported media modality.

#### `onboarding-ready`

A coherent level band that can support declared-level initialization and two to four weeks of study. For each incremental framework band, target:

- 300–500 lexical items, chunks, collocations, or character/word targets;
- 60–100 grammar or construction targets where relevant to the language;
- 25–50 pronunciation, orthography, script, or segmentation targets;
- 20–40 pragmatics, functions, and culture targets;
- at least two reviewed examples for every target intended for productive use;
- 8–12 exercise/activity templates per major supported mode across the pack;
- three to five reviewed external source recommendations per supported modality and band.

These are coverage gates, not claims that a fixed item count defines a proficiency level. A pack coverage report must also show descriptor coverage, prerequisite connectivity, theme distribution, provenance, support-language coverage, and unresolved review debt.

#### `placement-ready`

In addition to onboarding-ready coverage, every tested band must have enough independent items to bracket a learner without immediate reuse:

- at least 24 objective or short-response tasks per receptive/form dimension and band, spread across at least four content families;
- at least 12 extended, rubric-scored prompts per productive dimension and band;
- for pronunciation, at least 12 perception/word targets and six connected-speech prompts per relevant band;
- at least three alternate placement forms;
- rubric anchors and reviewed expected-answer variants;
- exposure metadata so a learner does not receive the same placement item again for six months, except explicitly designated longitudinal anchors.

A full Polish A1–B1 pack is therefore roughly 1,200–2,100 reviewed knowledge targets plus assessment, examples, relationships, prompts, and source curation. The exact total may differ when the language organizes competence differently. `pack coverage` must report both counts and qualitative gaps; it must never turn these targets into an automatic pass.

### 8.4 Pack authoring and review workflow

Pack content may enter through cited import, learner production, manual authoring, transformation, or AI-assisted drafting. Provenance and verification are independent: an authentic quotation can be pedagogically poor, while an original AI drill can be correct but still lack authoritative verification.

Controlled origin classes:

- `authentic-source`: verbatim or near-verbatim content with source ID and exact locator;
- `learner-produced`: utterance, writing, answer, or self-correction with session/attempt reference;
- `source-derived`: mechanically transformed source content such as a cloze or reordering task;
- `ai-adapted`: substantial AI rewrite of an identified source, with target level and transformation;
- `ai-generated`: new synthetic text/task based on declared knowledge targets but no direct textual source;
- `synthetic-media`: generated audio/image/media with generator metadata and source-text hash;
- `human-authored`: original human content with author role.

Each persistent item carries independent review axes:

| Axis | Representative states | Meaning |
|---|---|---|
| Linguistic | `unreviewed`, `machine-checked`, `reference-verified`, `human-verified` | Correctness, idiomaticity, register, pronunciation representation. |
| Pedagogical | `unreviewed`, `machine-checked`, `learner-approved`, `teacher-verified` | Level, usefulness, clarity, load, and answerability. |
| Source alignment | `not-applicable`, `unchecked`, `machine-checked`, `verified` | Faithfulness and answer support for derived/adapted content. |
| Rights | `unknown`, `personal-use-only`, `cleared`, `restricted` | Storage and redistribution permission. |
| Privacy | `private`, `redacted`, `shareable` | Learner-data exposure and distribution scope. |

AI self-review and second-model review may reach `machine-checked`; neither can claim `reference-verified`, `human-verified`, or `teacher-verified`.

Content lifecycle is orthogonal to those axes:

```text
draft -> candidate -> approved-personal -> verified -> publication-ready
   |          |              |              |
   +----------+--------------+--------------+-> needs-review
   +-----------------------------------------> rejected
                         verified/publication-ready -> deprecated
```

- `approved-personal` means useful and sufficiently checked for this learner, not safe to publish.
- `verified` meets the content-kind's linguistic and pedagogical policy.
- `publication-ready` additionally passes rights, privacy, source-alignment, and distribution review.
- `needs-review` is entered automatically when a dependency hash, source edition, prompt template, answer key, recording, or governing policy changes.

Review effort follows risk:

| Tier | Typical content | Minimum gate |
|---|---|---|
| 0 — ephemeral | Warm-up question, one-time role-play, temporary drill | Schema and obvious sanity checks; do not persist automatically. |
| 1 — session derived | Comprehension question or cloze from a known segment | Source locator, alignment, and answerability. |
| 2 — persistent personal | Anki note, reusable personal drill, recurring-error exercise | Machine linguistic check, deduplication, learner approval, reference check for uncertain/normative claims. |
| 3 — canonical | Grammar, pronunciation, government, idiom/register, core example | Reliable reference/authentic evidence, independent linguistic review, verified examples, scope/exceptions. |
| 4 — shared/published | Redistributable language-pack release | Full linguistic, pedagogical, source/rights, and privacy gates with reviewer evidence. |

Additional rules:

- Canonical grammar/factual claims cannot rely solely on AI generation. Contemporary, institutional, legal, cultural, and examination claims require an identified external source.
- Minimal pairs and subtle pronunciation contrasts require verified words/contrast plus audio or phonetic evidence; synthetic audio must be labeled and cannot be the sole normative authority.
- Personal-error content preserves the chain from utterance/audio through raw/reviewed hearing, correction, error candidate, and Anki note.
- A reviewer cannot approve their own AI batch as both independent linguistic and pedagogical reviewer for placement-ready or publication-ready content.
- Pack publication fails on broken prerequisites, duplicate stable IDs, unsupported framework levels, missing licenses, inadequate bank coverage, stale dependencies, or unresolved high-severity review flags.

Generation-template sampling policy:

1. Fully inspect persistent items from the first three runs of a new or materially changed prompt template.
2. After a template is marked stable, automatically validate every item and manually inspect at least 20% or five items, whichever is larger.
3. Inspect every Tier 3/4 item and every small-pack Anki note regardless of sampling.
4. If a sample has a substantive linguistic, alignment, rights, or answerability error, quarantine the entire batch, mark the template for revalidation, and find/invalidate dependent content.

Tooling:

- `pack scaffold` creates only the declared pack structure and schemas.
- `pack author import` validates JSONL/YAML/CSV input and preserves source references.
- `pack author generate-draft` prepares bounded AI drafting batches; it never approves output.
- `pack author review-queue` sorts by tier, review-axis gaps, dependency centrality, level, and missing coverage.
- `pack author review|approve|reject` records axis, reviewer kind, evidence/reference, reviewed hash, and reason.
- `pack author invalidate` walks dependency hashes and marks affected items `needs-review`.
- `pack template validate|stabilize|quarantine` manages sampling history and known failure modes.
- `pack coverage` emits count, graph, provenance, framework, assessment-bank, and review-debt reports.
- `pack publish` creates a content-addressed immutable version only after the selected maturity gate passes.

Estimated content effort, separate from Python engineering:

- narrow Polish A2 pilot slice: 5–10 focused author/reviewer days;
- Polish A1–B1 onboarding-ready pack: approximately 30–60 author/reviewer days;
- placement-ready assessment coverage: an additional 20–40 days, strongly dependent on audio production and licensing;
- first Chinese pilot: 10–20 days after core portability is proven.

These are planning ranges for one domain-capable author using tooling and optional AI drafts, not delivery promises. A source-only policy or independent native-speaker review can increase them substantially.

## 9. Skill architecture

Use one small umbrella skill and nine focused workflow skills. Speaking earns a separate skill because audio evidence, transcript uncertainty, package ingestion, and retention materially differ from ordinary lesson modes; the other lesson modes remain references under `linguawiki-learn`.

| Skill | Activates for | Main responsibilities | Key Python commands |
|---|---|---|---|
| `linguawiki` | General requests, ambiguous commands, “learn”, “continue” | Resolve user/track, inspect state, route to exactly one primary workflow, enforce common local/privacy rules. | `status`, `context`, then delegated commands. |
| `linguawiki-init` | New learner workspace, learner, language, or track | Generate/pin a private workspace; bootstrap DB; capture goals/preferences; install pack; run onboarding; import/audit prior curriculum; prepare resources. | `workspace init/doctor`, `db init`, `user create`, `pack install`, `track create`, `onboard ...`, `curriculum ...`, `resources prepare`. |
| `linguawiki-pack` | Create, expand, review, validate, or publish a language pack | Scaffold pack; import/source or AI-draft bounded batches; review high-risk content; measure coverage/maturity; publish immutable versions. This is author tooling, not a learner lesson. | `pack scaffold`, `pack author ...`, `pack coverage`, `pack publish`. |
| `linguawiki-assess` | Baseline, weekly/monthly/milestone test, level check | Select test form, enforce exam conditions, collect/scoring evidence, update estimates with uncertainty. | `assessment start/task/record/finalize`. |
| `linguawiki-learn` | Structured lesson or grammar/reading/listening/writing/mixed mode | Plan blocks, teach, correct, adapt, batch provisional observations, coordinate speaking blocks through `linguawiki-speak`, run retrieval, and finalize. | `session plan/start/log/staged/close/abandon/recover`; never standalone evidence/error writes for live-session events. |
| `linguawiki-speak` | Prepare, conduct/handoff, ingest, or review a speaking session | Create tutor brief/package skeleton; validate and ingest provider-independent packages; distinguish raw/heard/corrected text; require audio for pronunciation claims; apply retention. | `session-package ...`, `transcript ...`, `artifact ...`, then staged `session close`. |
| `linguawiki-source` | Add/use a course, book, podcast, video, transcript, or real-life incident | Catalog legally usable metadata, select segments, track progress, extract reviewed items, create activities. | `source add/segment/progress`, `artifact add`, `transcript import`. |
| `linguawiki-review` | Due review, errors, retrieval, consolidation | Build bounded review session, record delayed evidence, manage rich review queue and error lifecycle. | `review due/start/record`, `error transition`. |
| `linguawiki-anki` | Candidate generation/review/export/sync | Apply usefulness and card-quality filters, deduplicate, request approval, export/sync, import performance. | `anki candidate/validate/approve/export/import-stats`. |
| `linguawiki-maintain` | Reports, workspace snapshots/upgrades, wiki rendering, backups, lint, pack update | Generate projections/reports, privacy-check commit candidates, check integrity/drift/staleness, back up and migrate safely. | `workspace snapshot/doctor/upgrade`, `wiki build/check`, `report weekly`, `lint`, `db backup/check/migrate`, `pack update`. |

### 9.1 Skill package design

Every skill contains a concise `SKILL.md`, UI metadata where supported, and only the references it needs. Suggested progressive disclosure:

```text
.agents/skills/linguawiki-learn/
├── SKILL.md
└── references/
    ├── session-contract.md
    ├── correction-modes.md
    ├── grammar-and-vocabulary.md
    ├── reading-and-media.md
    └── writing-and-translation.md

.agents/skills/linguawiki-speak/
├── SKILL.md
└── references/
    ├── session-package.md
    ├── transcript-review.md
    ├── pronunciation-evidence.md
    └── privacy-and-retention.md
```

Codex is the primary repository runtime for the MVP, so each package includes appropriate `agents/openai.yaml` metadata and is validated in Codex. Canonical workflow instructions, CLI schemas, and session packages remain portable; a later repository runtime supplies its own discovery/invocation adapter rather than forking learning logic.

Do not duplicate database schemas or CLI help across skills. Link to a single machine/tool contract reference generated from the CLI schemas. Skills should state desired decisions and invariants; Python should encode repetitive mechanics.

### 9.2 Umbrella routing

The umbrella skill should:

1. Run `linguawiki status --format json`.
2. If no database/user/track exists, route to initialization.
3. Resolve an explicit mode first (`assess`, `review`, `reading`, `speaking`, `anki`, etc.); speaking preparation, package ingestion, and audio review route to `linguawiki-speak`.
4. Route source ingestion separately from a lesson using an already-cataloged source.
5. For “learn” or “continue”, route to `linguawiki-learn`, which uses due work and skill balance.
6. For maintenance requests, never start a learning session.
7. Keep automatic discovery enabled; explicit `$skill-name` invocation remains available.

### 9.3 Common skill invariants

- Read state through bounded CLI context, not generated Markdown.
- Never edit `wiki/` directly.
- Never use direct SQL.
- Record observed performance, not inferred success from merely presenting an explanation.
- Distinguish recognition, controlled production, spontaneous production, and delayed transfer.
- Respect fluency, accuracy, and exam correction modes.
- Cite source/provenance for durable language claims.
- Keep origin class separate from all review axes; never upgrade review merely because content is authentic or AI-checked.
- Do not confirm pronunciation/prosody errors from transcript text alone.
- Limit durable content and Anki candidates using usefulness filters.
- Preview uncertain bulk changes and stop on validation failures.
- Close or explicitly abandon every started session.

## 10. CLI and Python tool surface

The CLI should be composable for both people and agents.

```text
linguawiki workspace init|status|doctor|sync-skills|upgrade|snapshot|privacy-check
linguawiki db init|status|migrate|check|backup|export-portable|restore
linguawiki user create|show|update|list
linguawiki pack scaffold|validate|install|diff|update|list|coverage|publish
linguawiki pack author generate-draft|import|review-queue|review|approve|reject|invalidate
linguawiki pack template validate|stabilize|quarantine
linguawiki track create|show|update|activate|archive
linguawiki onboard start|record|status|finalize
linguawiki resources plan|prepare|status
linguawiki curriculum import|show|position|audit-start|audit-record|audit-finalize
linguawiki knowledge get|search|upsert|link|merge
linguawiki source add|show|segment|progress|list
linguawiki artifact add|verify|list
linguawiki assessment start|next|record|finalize|report
linguawiki session plan|start|log|staged|status|close|abandon|recover
linguawiki session-package validate|ingest|inspect|export|purge
linguawiki transcript normalize|review|show
linguawiki evidence record|list|recompute
linguawiki error observe|show|transition|list
linguawiki review due|start|record|defer
linguawiki anki candidate|validate|approve|reject|export|import-stats
linguawiki wiki build|check|export-draft|import-draft
linguawiki report weekly|monthly|milestone
linguawiki context session|assessment|source|concept
linguawiki lint
linguawiki skills install|check
```

### 10.1 Agent-friendly command contract

JSON output envelopes use:

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "session.plan",
  "data": {},
  "warnings": [],
  "next_actions": []
}
```

Mutation input should accept either named CLI flags for small commands or `--input <json-file>` / stdin for structured payloads. Large learner text must not be passed in shell arguments where quoting or command history is unsafe. `session log` accepts an ordered event array and is normally called once per block; single-event input exists for edge cases and tests, not as the conversational hot path.

### 10.2 High-value deterministic services

- ID creation and normalization.
- Language-pack schema validation and content hashing.
- Resource dependency resolution.
- Session block eligibility and scoring.
- State-transition validation.
- Session-package schema/file/hash validation and transcript-layer preservation.
- Content-origin validation, risk-tier gates, template sampling, and dependency invalidation.
- Evidence aggregation and estimate recomputation.
- Error signature deduplication.
- Anki stable-ID/content-hash deduplication and TSV escaping.
- Markdown rendering and link checking.
- Weekly aggregation.
- Migration, backup, integrity, and drift checks.

AI should not be used for any of these mechanical tasks.

## 11. Initialization and language onboarding

### 11.1 Learner-workspace bootstrap

From an installed/tagged core release, create the first personal repository with a command such as:

```bash
linguawiki workspace init ../PolishLinguaWiki \
  --target-language pl \
  --history git-wiki
```

The command:

1. validates that the target is new/empty and creates an independent workspace ID;
2. renders `AGENTS.md`, `.gitignore`, `linguawiki.toml`, minimal `pyproject.toml`, and initial locks from the core template;
3. pins the exact core, schema, skill, and selected pack versions/hashes;
4. materializes the generated skill snapshot and validates it for Codex;
5. creates `data/`, `artifacts/`, `imports/`, and `drafts/` with safe ignore rules;
6. applies all migrations to a temporary DuckDB, checks it, and atomically promotes it to `data/linguawiki.duckdb`;
7. configures and verifies a backup root outside the learner Git repository;
8. renders the empty sanitized wiki and runs the staged-file privacy check;
9. initializes a local Git repository when requested, but does not create a remote, commit, or push without an explicit separate action.

`workspace init` is idempotent only while the target remains an untouched matching scaffold; it refuses to overwrite a real workspace. The lower-level `db init` remains available for tests and recovery and refuses to overwrite a non-empty database.

`workspace doctor` verifies that the current directory is a learner workspace rather than the core repository, versions match `linguawiki.lock`, generated skills are clean, private paths are ignored, the backup root is outside Git, and any configured remote is intended to be private. It warns rather than assuming that a repository named “private” is actually protected.

### 11.2 Create learner and track

Collect only information that changes behavior:

- display name and timezone;
- target BCP-47 language and desired script/region;
- native/support languages and explanation preference;
- goals, intended uses, and target framework/level;
- weekly availability and typical session duration;
- interests, disliked topics, and preferred source types;
- accessibility needs and available voice/audio tools;
- existing course progress, sources, and Anki deck information;
- consent for transcript/audio retention and optional external services.

The selected pack declares its supported proficiency frameworks, their ordered levels, descriptors, and optional mappings. Track creation requires one framework and accepts only a level key from that framework. For example, a Polish CEFR track may accept `A2`; a Chinese pack configured for HSK may accept `HSK2` or another version-specific key. If a learner supplies a level from another framework, onboarding must ask them to select an installed framework or use an explicit pack mapping; it must not silently translate the label.

### 11.3 Choose onboarding mode

#### Placement mode

Use when the learner wants personalization or has uncertain/mixed prior study.

1. Self-report and can-do survey establish a broad prior, not evidence of mastery.
2. A course audit contributes `encountered` items and chooses calibration targets if prior materials exist.
3. Adaptive diagnostic tasks run independently for each supported dimension.
4. Spoken, written, listening, and pronunciation tasks are used only when the required modality is available.
5. The result contains separate estimates, credible ranges, bank coverage, and untested gaps.
6. The learner confirms priorities before resource preparation.

##### Placement algorithm v1

Use a small, transparent ordinal Bayesian staircase rather than an opaque model:

1. Represent each framework level and within-level difficulty bucket on an ordered numeric grid. Pack authors assign and review task difficulty.
2. Initialize a discrete ability prior per dimension. Center it on the declared/self-reported level with a spread of one band, or use a broad prior when no level is supplied.
3. Select an unseen task near the posterior median that maximizes expected uncertainty reduction, subject to content-family diversity, modality availability, prerequisite, and exposure constraints.
4. Score each task from `0.0` to `1.0` using its versioned rubric. For v1, encode adjacent framework bands one unit apart, allow reviewed half-band item difficulties, and use `p(success | ability, difficulty) = 1 / (1 + exp(-1.7 * (ability - difficulty)))`. For fractional score `s`, multiply each ability-grid point by `p^s * (1-p)^(1-s)`, then normalize. Store the prior, item difficulty, score, posterior, and algorithm version for replay.
5. A strong result moves the next probe upward; a weak result moves it downward; ambiguous results trigger a same-band task from a different content family.
6. Never reuse a placement item seen by this learner within six months unless it is marked as a longitudinal anchor.

Default per-dimension budgets:

| Dimension/task type | Minimum before stopping | Maximum |
|---|---:|---:|
| Objective or short response | 6 | 12 |
| Extended productive rubric | 3 | 5 |
| Pronunciation | 3 targeted tasks plus one connected sample | 6 plus two connected samples |

Stop a dimension when all of the following hold:

- its minimum task budget is met;
- evidence covers at least two content families or task types;
- at least 80% of posterior probability lies inside an interval no wider than one adjacent framework band;
- the bank has provided at least one boundary probe above or below the estimated band, unless the estimate is at the framework edge.

Stop instead at the maximum budget, learner request, or fatigue threshold. Such a result is labeled low/medium confidence with the reason; it is not forced into a precise band. Unsupported modalities are `not-tested`, never failed. The first release keeps response-curve parameters fixed and expert-authored; it may calibrate them from aggregate usage only in a later, explicitly versioned model.

#### Declared-level mode

Use when the learner says, for example, “start at A2.”

1. Store A2 as `declared_start_level`.
2. Seed provisional skill estimates centered on A2 with low confidence.
3. Install A2 core plus prerequisite bundles, not the entire language pack.
4. Create a short calibration queue sampling A1 prerequisites and A2 targets.
5. Prepare the first two weeks of sources and activities.
6. Never mark A1/A2 knowledge as stable merely because of the declared level.

### 11.4 Prior-course import and audit

The generic workflow is: “I completed N units of course X; determine what transferred.” It applies to Hurra, another textbook, a class syllabus, or a user-authored curriculum.

1. `curriculum import` ingests a legally permissible outline or user-authored unit list, records source/version/rights, and maps units to pack knowledge items and descriptors. Unmapped objectives remain explicit gaps.
2. `curriculum position` records the learner's declared completed/current units with self-report provenance.
3. Declared completion sets mapped items to at most `encountered`; it never creates recognition or production evidence.
4. `curriculum audit-start` selects a risk-weighted sample: central prerequisites, recent units, likely high-value production targets, and a smaller sample of older supposedly known material.
5. `audit-record` batches results using the same attempt/evidence rules as assessment.
6. `audit-finalize` updates curriculum progress, creates active gaps and a calibration/review queue, and reports what was not tested.

If the course outline cannot legally be distributed in a language pack, the local learner may provide their copy or enter unit objectives manually. The database stores metadata, mappings, learner evidence, and short permissible references—not copied textbook content.

### 11.5 Resource preparation

`resources prepare` performs a dry-run plan before importing:

- resolve pack and framework versions;
- verify that the pack maturity supports the requested onboarding mode and level range;
- include prerequisite bundles recursively;
- filter by learner goals, age/content preferences, support language, modalities, and available time;
- load proficiency descriptors and starter knowledge seeds;
- load assessment tasks and exercise templates;
- propose—not automatically download—external books, courses, podcasts, and media;
- record licenses, URLs, provenance, checksums, and review status;
- create a two-week starter curriculum and calibration queue;
- render a learner-facing dashboard.

Level preparation must be bounded. A2 initialization should not create thousands of active knowledge states or hundreds of cards. Imported reference items remain `unseen` until actual evidence is recorded.

A `pilot` pack may prepare only its declared themes and must label the result as a pilot curriculum. An `onboarding-ready` pack may serve declared-level initialization. Full adaptive placement is enabled only across bands for which `placement-ready` coverage passes; otherwise onboarding falls back to declared-level calibration or clearly reports the uncovered dimensions.

## 12. Learner model and mastery

### 12.1 Evidence first

Every progress change derives from evidence with:

- target item and/or skill dimension;
- modality and task type;
- recognized/controlled/spontaneous category;
- success, partial success, or failure;
- help level from none through full answer;
- immediacy versus delayed retrieval;
- source difficulty and assessment conditions;
- evaluator and confidence;
- timestamp and session/attempt link.

### 12.2 Estimate calculation

Start with a transparent rule-based model rather than opaque ML:

- weight spontaneous production above controlled production and recognition;
- weight delayed success above immediate repetition;
- discount heavily hinted attempts;
- apply recency decay without deleting historical evidence;
- require evidence diversity before raising confidence;
- lower uncertainty with repeated, independent observations;
- allow failures to reactivate errors and lower item state;
- map internal numeric estimates to proficiency bands using versioned pack descriptors.

The report must explain why an estimate changed. Store algorithm version so estimates can be recomputed after policy changes.

### 12.3 Mastery gates

Suggested default promotion requirements:

- `encountered`: presented or observed once;
- `recognized`: successful recognition in two contexts;
- `understood`: explanation/comprehension plus transfer question;
- `controlled-production`: two unprompted controlled successes;
- `spontaneous-production`: successful use in a meaning-focused task;
- `stable`: spontaneous or comprehension success after delay in at least two contexts.

These defaults are configurable by item kind and language pack. They are not universal linguistic truths.

## 13. Session engine

### 13.1 Planning inputs

- available minutes and requested mode;
- due review tasks and active errors;
- curriculum/source position;
- current skill estimates and uncertainty;
- weekly skill-balance deficits;
- prerequisites and target level;
- recent block history to avoid monotony;
- user goals and interests;
- recent difficulty, fatigue, and session completion;
- required assessment/calibration tasks.

### 13.2 Block construction

Default mapping remains two to six approximately 20-minute blocks for 40–120 minutes. The planner should support other durations by creating a warm-up, whole blocks, and a short closure without pretending every lesson fits perfectly.

Candidate blocks receive a score based on:

```text
due urgency
+ skill-balance deficit
+ goal relevance
+ curriculum continuity
+ uncertainty reduction
+ learner interest
+ transfer value
- recent repetition
- prerequisite gap
- overload/difficulty risk
- novelty excess
```

Hard constraints:

- reserve final retrieval/closure time;
- do not schedule mutually incompatible modes in one block;
- cap novel targets per session;
- include productive use when new grammar/vocabulary is introduced;
- include native input over the week when suitable resources exist;
- avoid repeating a failed task unchanged;
- honor an explicit learner mode unless unsafe or impossible.

The CLI returns ranked block candidates and reasons. The skill turns the selected plan into natural instruction.

Unless the learner overrides it, the weekly policy targets five sessions and balances the minimum ten blocks approximately as follows:

| Area | Minimum blocks/week |
|---|---:|
| Retrieval and rich review | 2 |
| Listening | 2 |
| Speaking | 2 |
| Reading | 1 |
| Writing | 1 |
| Grammar/form focus | 1 |
| Pronunciation | 1 |

Vocabulary is integrated across blocks rather than receiving an isolated quota. Missed sessions create deficits for future ranking; they do not cause the next session to be overloaded in an attempt to “catch up.” Longer sessions may add blocks, but the planner should preserve variety and fatigue limits.

### 13.3 Session state machine

```text
planned -> active -> closing -> completed
   |          |        \-> partial
   +----------+------------> abandoned
```

While a session is active, `session log` durably stores validated provisional batches but does not change learner aggregates. Only `session close --outcome completed|partial` may atomically finalize attempts, evidence, error occurrences, source/curriculum progress, follow-ups, Anki candidates, estimates, and projection staleness. `partial` preserves valid work from an intentionally shortened session while preventing uncompleted block objectives from being credited.

If conversation ends unexpectedly, the next invocation detects the active session and its highest staged batch sequence, then offers:

- resume from the next uncompleted activity;
- inspect and close valid staged work as `partial`;
- abandon it, retaining staged data for audit but excluding it from progress;
- recover selected staged events into a new session after explicit review.

The skill flushes once per block by default, not once per observation. It may flush earlier before a voice/provider handoff or when the event buffer reaches a configured size. Tests must cover a crash before batch write, after batch write, during close, and after committed close but before the agent receives the response.

### 13.4 Lesson behavior

- Elicit before explaining.
- Use graduated hints except when direct explanation is requested.
- Fluency mode responds to meaning first and corrects at most three high-value errors after the turn.
- Accuracy mode gives the learner a self-correction chance, then corrects the target pattern promptly.
- Exam mode records silently and gives feedback only after task completion.
- Writing feedback separates communication, correctness, and naturalness and requires a rewrite.
- Pronunciation distinguishes intelligibility, phonetic accuracy, naturalness, and native-likeness.
- Every lesson ends with brief retrieval and no more than three priority weaknesses.

Detailed reading, podcast, video, shadowing, translation, teach-back, and role-play procedures from the source specification belong in the `linguawiki-learn` references, expressed without Polish-specific assumptions.

## 14. Assessment design

### 14.1 Dimensions

At minimum:

- reading;
- listening;
- spoken interaction;
- spoken production;
- writing;
- pronunciation/intelligibility;
- grammar recognition;
- grammar production;
- receptive vocabulary;
- productive vocabulary;
- optional writing-system/character knowledge where relevant.

### 14.2 Forms and cadence

- Baseline: onboarding or declared-level calibration.
- Weekly: short mixed retrieval, focused on transfer and active gaps.
- Monthly: broader skills sample using anchored rubrics.
- Milestone: every 8–12 weeks or before a claimed target level.

Assessment definitions and rubrics are immutable after use; revisions create a new version. Reuse anchor tasks sparingly so improvement is not just memorization.

### 14.3 AI scoring safeguards

- Store rubric dimension scores and rationale, not only a total.
- Preserve the learner response or a consent-compatible evidence excerpt.
- Mark scorer type/model or human reviewer.
- Require human or independent review before high-stakes level claims.
- Separate “not tested” from “failed.”
- Never infer listening skill from reading a transcript or speaking from a written answer.

## 15. Source, media, and copyright workflow

Source lifecycle:

```text
proposed -> cataloged -> reviewed -> active -> completed -> archived
                    \-> rejected
```

For books, courses, podcasts, video, and real-life material:

1. Record metadata, level estimate, access method, and rights.
2. Link or store the original outside the generated wiki.
3. Select bounded chapters/pages/timestamps.
4. Record unaided attempt before transcript/subtitle assistance when pedagogically relevant.
5. Extract only a small number of useful items.
6. Link evidence and progress back to the source segment.
7. Preserve short quotations only where legally appropriate.

The core must support intensive and extensive modes. Extensive mode intentionally records broad progress and comprehension without line-by-line extraction.

## 16. Review and error loop

The rich review queue complements Anki. It contains tasks that require context: retelling, conversation transfer, listening discrimination, writing revision, shadowing, or error correction.

Prioritization considers:

- due window;
- error frequency and meaning impact;
- failure after delay;
- relevance to current goals/curriculum;
- prerequisite role;
- repeated Anki failure;
- over-review penalty.

Error deduplication uses category + normalized signature + target item, with agent review for uncertain matches. Resolution requires successful controlled, novel, spontaneous, and delayed evidence according to policy. A later recurrence transitions `resolved` to `reactivated` while preserving history.

## 17. Anki integration

DuckDB owns note content and stable IDs; Anki owns card scheduling.

### MVP flow

```text
session evidence
  -> candidate generation
  -> usefulness filter
  -> duplicate/ambiguity validation
  -> learner approval
  -> UTF-8 TSV export
  -> manual Anki import
```

Requirements:

- support recognition, production, cloze, contrast, correction, audio, pronunciation, and transformation notes;
- one answerable unit per card;
- carry source, session, level, skill, and error tags;
- carry item-level origin and the review state that justified approval;
- use a stable LinguaWiki ID field;
- escape tabs/newlines/HTML safely;
- write an export manifest and checksum;
- re-export changed notes as updates, not duplicates;
- cap cards by session type; ceilings are not targets;
- never export `candidate` or `needs-review` notes.
- preserve the utterance/review/correction chain for personal-error notes without placing private audio or transcript text on the card unless explicitly approved.

AnkiConnect comes only after field and note-type schemas are stable. Its adapter must preview creates/updates/retirements and never delete external notes by default.

## 18. Wiki projection

The generated wiki preserves the linked-knowledge experience requested by the source document without becoming a second database.

Generated pages include:

- dashboard/current track;
- learner profile and multidimensional skill map;
- weekly plan and due rich reviews;
- knowledge pages and graph links;
- current curriculum/source progress;
- active/monitoring/resolved errors;
- session summaries;
- assessments and reports;
- pending/approved Anki summaries;
- provenance and source catalog.

Each page includes generated frontmatter:

```yaml
generated: true
entity_id: lwc_...
projection_version: 1
source_event_id: lwe_...
generated_at: ...
```

`wiki check` verifies that files match database hashes, links resolve, IDs are unique, and no hand edits occurred. To edit durable content:

1. `wiki export-draft <entity-id>` creates an editable file under `drafts/`.
2. Human/agent edits it.
3. `wiki import-draft --dry-run` validates and previews the database update.
4. `wiki import-draft --apply` writes a revision with provenance.
5. `wiki build` regenerates the canonical page.

## 19. Privacy, safety, and reliability

- Ignore the DuckDB file, backups, imports, raw media, and recordings in Git by default.
- Keep only code, migrations, templates, pack seed data with compatible licensing, and synthetic fixtures in version control.
- Never store API keys in DuckDB or repository config; use environment/keychain adapters.
- Make external uploads explicit, especially learner voice, transcripts, and copyrighted content.
- Provide retention settings for full transcripts, excerpts, audio, and assessment samples.
- Default imported speaking packages and full audio to private; retain selected clips only when they support active evidence and the learner permits it.
- Purge expired artifacts through recorded retention jobs/tombstones and invalidate evidence/content whose required audio or source is gone.
- Redact sensitive text from logs; logs identify record IDs rather than responses.
- Back up before migrations/imports and verify backup readability.
- Restore to a new path first; never overwrite the active database without explicit confirmation.
- Use relative artifact paths plus SHA-256 checksums.
- Provide export/delete commands scoped to a user or track, with dry-run summaries.

### 19.1 History and format-independent recovery

This policy applies to learner workspaces, not the `LinguaWiki` core or shared pack repositories. The selected default is `git-wiki`: `PolishLinguaWiki` is private, commits its sanitized wiki, workspace/pack locks, configuration, `AGENTS.md`, and generated skill snapshot, while the DuckDB file and portable/native backups stay outside Git.

The initializer still supports these policies for other learners:

| Policy | Git contents | Recovery characteristics |
|---|---|---|
| `git-wiki` | Commit generated wiki, schema manifests, and sanitized reports; exclude raw responses, transcripts, audio, and DuckDB. | Human-readable progress diffs plus required portable snapshots in a configured backup location outside Git. |
| `portable-snapshot` | Keep wiki optional; write periodic table exports and manifest to a configured local/synced backup root outside Git by default. | Full structured recovery without depending on DuckDB's storage format. |
| `git-portable-snapshot` | Commit encrypted or explicitly approved portable snapshots plus generated wiki. | Strongest version history, highest privacy/repository-size cost. |
| `local-only` | Commit neither learner projection nor snapshots. | Native and portable backups remain required but may stay on the same machine; warn that this does not protect against device loss. |

Do not silently pick a privacy posture for someone else's workspace. The initializer explains that Git history is difficult to redact and records the learner's choice. All modes still create native and portable backups; the choice controls whether human-readable history or encrypted snapshots enter Git and where backups are retained. If no choice is available in an unattended test, use `portable-snapshot` with a temporary backup root—not `local-only`.

`workspace snapshot` closes any active projection job, rebuilds the wiki, compares it with DuckDB, runs privacy and generated-file checks on the staged Git candidates, and prints the exact files suitable for committing. It never stages, commits, creates a remote, or pushes automatically. “Sanitized” means filtered according to configured policy; it is not a guarantee that the repository is suitable for public visibility.

Pin the exact DuckDB package version in `uv.lock`. Each `db backup` creates:

1. a native consistent database copy for fast restore;
2. a format-independent export directory containing one Parquet file per typed table, JSON for metadata that cannot round-trip cleanly, schema/migration/application versions, row counts, and SHA-256 checksums;
3. a restore manifest and verification result.

`db export-portable` can run independently and must read from a consistent snapshot. CI tests restore the portable export into a fresh database and compare table counts, stable IDs, selected hashes, and projection output. A DuckDB dependency upgrade is accepted only after native-open, portable-export, and portable-restore tests pass on the latest retained fixture.

## 20. Testing and evaluation strategy

### 20.1 Unit tests

- ID/normalization utilities.
- workspace/lock rendering, version comparison, and ignore/privacy policy;
- State machines and invalid transitions.
- block scoring and hard constraints;
- placement posterior updates, item-selection constraints, budgets, and stop conditions;
- session batch sequencing, hashes, idempotency, and materialization;
- session-package schema, transcript-layer immutability, retention, and audio-evidence gates;
- provenance/review-axis transitions, template sampling, and dependency invalidation;
- mastery promotion/regression;
- error resolution/reactivation;
- Anki filters, stable IDs, escaping, and deduplication;
- resource dependency resolution;
- Markdown link generation.

### 20.2 Integration tests

Use a fresh temporary DuckDB per test:

- core checkout -> independent learner workspace init -> pinned skills/core/pack -> empty wiki;
- learner workspace upgrade dry-run/success/failure rollback without a Git merge;
- workspace snapshot rejects raw transcript/audio/private fields from commit candidates;
- bootstrap -> pack install -> track creation;
- declared A2 -> resources -> session -> evidence -> wiki;
- placement run -> estimates -> plan;
- prior-course position -> encountered items -> audit queue -> evidence;
- interrupted session resume/partial/abandon/recover;
- duplicate batch and response-lost close retries;
- manual session package -> idempotent ingest -> staged events -> partial/completed close;
- raw STT -> normalized -> reviewed hearing -> pedagogical correction without overwriting prior layers;
- source catalog/progress;
- Anki approval/export/re-export;
- weekly report aggregation;
- native backup plus portable export and restore.

### 20.3 Migration tests

- Apply every migration from empty.
- Upgrade fixtures from each released schema version.
- Verify checksums and reject edited historical migrations.
- Test failed migration rollback and pre-migration backup.
- Upgrade the pinned DuckDB fixture, export it portably, restore it, and compare invariants.
- Upgrade learner-workspace fixtures from each supported core/pack lock combination and verify that failed upgrades preserve prior DB, environment, skills, and locks.

### 20.4 Language-agnostic contract tests

Run the same core scenarios against two deliberately different fixture packs:

- a synthetic inflected, whitespace-delimited language;
- a synthetic tonal, non-whitespace language.

Then run real pack tests for Polish and Chinese. Fail tests if core code branches on `pl` or `zh` except inside registered adapters.

Pack tests also enforce declared framework levels, maturity/coverage gates, author/reviewer separation, source/license metadata, prerequisite closure, assessment-bank exposure rules, and refusal to advertise placement when coverage is insufficient.

### 20.5 Skill behavioral tests

For each skill, use realistic prompts and a temporary repository/database. Assert observable effects rather than exact wording:

- correct routing;
- bounded context use;
- no direct SQL or generated-wiki edits;
- accurate command selection;
- no mastery without evidence;
- no raw Anki export without approval;
- no pronunciation confirmation without audio evidence;
- no persistent item without origin/review metadata appropriate to its risk tier;
- session closure completeness;
- respect for correction and exam modes.

### 20.6 Quality gates

Every merge should pass:

```bash
uv run python scripts/verify.py
```

## 21. Delivery phases

Each phase produces a usable vertical increment and has a hard acceptance gate.

The phase summaries below are expanded into executable checklists. Treat these stage documents as the implementation handoff for each increment:

- [Stage 0 — Architecture, Contracts, and Repository Boundaries](stage0.md)
- [Stage 1 — Learner Workspace and Storage Foundation](stage1.md)
- [Stage 2 — Language Packs, Onboarding, and Prior-Course Audit](stage2.md)
- [Stage 3 — Knowledge, Evidence, and Learner Model](stage3.md)
- [Stage 4 — Session Engine and First Polish Vertical](stage4.md)
- [Stage 5 — Sources, Reading, Listening, and Speaking](stage5.md)
- [Stage 6 — Review and Anki MVP](stage6.md)
- [Stage 7 — Wiki, Reports, Maintenance, and Upgrades](stage7.md)
- [Stage 8 — Four-Week Polish Hardening and Pack Maturity](stage8.md)
- [Stage 9 — Chinese Portability Proof](stage9.md)

Rough sizing assumes one experienced Python engineer working with an AI coding agent, local-only infrastructure, and timely product decisions. Content estimates are separate and assume a domain-capable author/reviewer. Ranges are planning guidance, not commitments.

| Phase | Engineering effort | Separate content/review effort | Usability milestone |
|---|---:|---:|---|
| 0 — contracts/workspaces | 5–8 days | 1–2 days | Core/pack/workspace boundaries and templates locked. |
| 1 — storage/recovery | 8–12 days | — | Safe learner workspace, local database, locks, and recovery. |
| 2 — packs/onboarding | 8–12 days | 5–10 days for thin Polish slice | Declared A2 and limited calibration work. |
| 3 — learner model | 8–12 days | 2–4 days of rubric/seed refinement | Evidence and estimates are traceable. |
| 4 — session vertical | 8–12 days | 2–5 pilot days | Thin Polish end-to-end dogfooding starts. |
| 5 — sources/voice | 8–12 days | 5–10 source/audio days | Real reading/listening/speaking use. |
| 6 — review/Anki | 5–8 days | 2–4 review days | Daily review loop works. |
| 7 — wiki/maintenance | 6–10 days | 1–3 days | Operational MVP and recoverable history. |
| 8 — hardening/full Polish | 5–10 engineering days over four calendar weeks | remaining 30–60+ days for A1–B1 maturity | Production-quality Polish use. |
| 9 — Chinese proof | 8–15 days | 10–20 days for pilot pack | Cross-language portability proven. |

A thin usable vertical should exist after roughly 37–56 engineering days plus 10–20 content/review days. The operational MVP through Phase 7 is roughly 56–86 engineering days. A placement-ready A1–B1 pack is a parallel content program and must not be hidden inside those engineering totals.

### Phase 0 — architecture fixtures and contracts

Deliver:

- ADRs recording Codex as repository runtime, `lingua.session.v1` as speaking boundary, AI-assisted risk-based pack authoring, and the selected learner-history policy;
- ADR for core/pack/learner-workspace ownership and no-fork upgrade model;
- ADRs for DuckDB authority, wiki projection, skill/tool boundary, evidence model, and pack contract;
- repository skeleton and Python tooling;
- `lingua.workspace.v1`, learner-workspace template, and lock-file contract;
- JSON command envelope and typed ID conventions;
- synthetic language packs used by tests;
- working Codex skill-install/discovery strategy and one smoke-test skill;
- `lingua.session.v1` schema, validator contract, and a synthetic manual-package fixture.

Acceptance:

- architectural invariants have executable tests or lint rules;
- no language-specific field is required by core models;
- a minimal skill can call the CLI in an isolated fixture;
- core tests contain no real learner state, and a generated learner workspace has no copy of core Python source;
- speaking acceptance tests use the package fixture and distinguish transcript from audio-only evidence.

### Phase 1 — learner workspace and storage foundation

Deliver:

- initial migrations for identity, tracks, pack registry, audit, and projection state;
- connection/lock/transaction layer;
- migration, backup, restore-to-new-path, and integrity commands;
- native plus format-independent portable backup/export;
- JSON output/error contract.
- workspace init/status/doctor and privacy/ignore checks.

Acceptance:

- initialization is idempotent;
- failed writes roll back completely;
- concurrent writer receives a clear retryable error;
- backup can be opened and checked;
- portable exports restore into a fresh database and survive the pinned DuckDB upgrade fixture.
- an independent `PolishLinguaWiki` fixture pins core/skills/packs and passes `workspace doctor`.

### Phase 2 — language packs and onboarding

Deliver:

- pack schemas, validator, installer, diff/update preview;
- pack-authoring/review/coverage tools;
- item-level origin, independent review axes, dependency invalidation, and template sampling/quarantine;
- functional `linguawiki-pack` skill for Codex;
- thin Polish A2 pilot pack, explicitly marked `pilot`;
- user/track setup;
- framework-valid declared-level A2 flow and placement algorithm/state machine;
- generic prior-course import and audit;
- bounded resource dependency planning.

Acceptance:

- a `pl` track can be created with Russian/English support inside the independent `PolishLinguaWiki` workspace;
- choosing A2 installs A2 + prerequisites without marking mastery;
- pilot calibration can pause/resume and produces separate estimates with uncertainty, while comprehensive placement refuses to run until bank coverage is `placement-ready`;
- imported course completion creates only `encountered` state plus an audit queue;
- every persistent pilot item has item-level origin and required review-axis states;
- a defective sampled generation invalidates/quarantines its batch and dependents;
- pack reinstall is idempotent.

### Phase 3 — knowledge, evidence, and learner model

Deliver:

- knowledge graph and source tables;
- attempts/evidence/error schemas;
- transparent mastery and skill-estimate service;
- context bundle commands;
- the first functional `linguawiki`, `linguawiki-init`, and `linguawiki-assess` skill slices for Codex.

Acceptance:

- recognition does not promote spontaneous production;
- delayed failures can regress state/reactivate errors;
- every estimate is traceable to evidence and algorithm version;
- context output is bounded and sufficient for an agent;
- the initialization and assessment skills pass behavioral tests against the thin Polish fixture.

### Phase 4 — session engine and learning skill

Deliver:

- session/block/activity lifecycle;
- planner with time, balance, due-work, continuity, and fatigue inputs;
- `linguawiki-learn` references for all modalities;
- transactional session closure and interruption recovery;
- durable batched session staging and crash recovery;
- validated session-package import into staged events;
- minimal wiki/dashboard projection needed for dogfooding.

Acceptance:

- 40, 60, 80, 100, and 120-minute requests produce valid plans;
- an explicit mode is honored;
- a completed session updates evidence, errors, progress, follow-ups, and projection state once;
- retrying a block batch or close is idempotent;
- retrying package ingestion by package hash is idempotent;
- crash tests cover unflushed, staged, mid-close, and response-lost states;
- a thin Polish declared-A2 track in `PolishLinguaWiki` completes plan -> lesson -> close -> dashboard, and dogfooding begins there rather than in core.

### Phase 5 — sources, reading, listening, and voice hooks

Deliver:

- course/book/podcast/video/source workflows;
- artifact/transcript adapters;
- manual speaking-package workflow and an export adapter for the chosen initial voice application when practical;
- intensive/extensive progress models;
- speaking/pronunciation capture and privacy settings;
- functional `linguawiki-speak` skill;
- corresponding source and modality references added to the already-running learning skill.

Acceptance:

- one adapted reading, podcast, video fragment, and conversation block can be completed;
- unaided and aided comprehension remain distinct;
- raw copyrighted material is not copied into the wiki;
- voice evidence can be recorded with or without retained audio;
- dogfooding can ingest a real recording/transcript package, retain raw/normalized/reviewed layers, and confirm pronunciation only from linked audio.
- repeated ingestion of the same package hash is a no-op, and retention purge leaves auditable tombstones/invalidation.

### Phase 6 — review and Anki MVP

Deliver:

- rich review scheduling and error lifecycle;
- Anki candidate, validation, approval, rejection, and TSV export;
- export manifests and re-export behavior;
- functional `linguawiki-review` and `linguawiki-anki` skills.

Acceptance:

- candidates are limited and deduplicated;
- only approved notes export;
- stable IDs update rather than duplicate;
- repeated failure can create a rich review task.

### Phase 7 — wiki, reports, and maintenance

Deliver:

- full wiki renderer and controlled draft import;
- weekly/monthly reports;
- lint for DB integrity, orphan relations, stale projections, duplicate IDs, unreviewed content, and incomplete sessions;
- distinct core-development and learner-workspace `AGENTS.md` contracts;
- complete `linguawiki-maintain` skill and polish all previously introduced skill slices.
- workspace snapshot/privacy workflow and dry-run/transactional core/pack upgrade with rollback.

Acceptance:

- deleting `wiki/` and rebuilding yields the same projection;
- drift is detected;
- weekly report reconciles with raw sessions/attempts;
- the learner repo contains readable sanitized progress history but no DuckDB, backup, raw audio, or raw transcript files;
- a core/pack upgrade refreshes locks and generated skills without merging core source into learner history;
- a failed upgrade restores the prior DB, environment, locks, and skill snapshot.
- a fresh agent can operate solely from skills, CLI, and database state.

### Phase 8 — hardening and full Polish maturity

Deliver:

- four-week continuation of the pilot that began after Phase 4;
- expansion from the thin pack toward the selected A1–B1 maturity gate;
- data and skill eval results;
- card-load, context-size, planner, estimate, and pack refinements;
- migration/backup rehearsal and recovery guide.

Acceptance:

- five sessions/week can be run without state corruption or manual DB edits;
- planner adapts to actual time and missed sessions;
- learner can inspect why each priority and estimate exists;
- maintenance identifies stale or contradictory items;
- the Polish pack passes the chosen onboarding-ready or placement-ready A1–B1 coverage gate; any unsupported dimensions remain explicitly labeled.

### Phase 9 — Chinese portability proof

Deliver:

- selected `zh-Hans` or `zh-Hant` pack with region/pronunciation choices;
- segmentation, characters, pinyin/tones, and classifier/measure-word extensions;
- cross-pack contract test results.

Acceptance:

- no Polish schema or workflow assumption blocks Chinese;
- script/romanization/word-segmentation data is preserved correctly;
- the same onboarding, session, evidence, review, Anki, and wiki services work unchanged.

## 22. MVP end-to-end acceptance scenario

Starting from a tagged core release, not a learner-data branch of core:

1. Run `workspace init` to create an independent private `PolishLinguaWiki` repository using `git-wiki` history and a backup root outside Git.
2. Verify that the workspace pins core/schema/skills/pack versions, contains no core Python source, and passes `workspace doctor`.
3. Initialize DuckDB inside the workspace and create a learner with their configured IANA timezone, native Russian, support English, and real-life use in Poland. Location and timezone are separate inputs.
4. Add Polish, goal B1, declared CEFR A2.
5. Install the pinned Polish pack and prepare A2 + prerequisites.
6. Complete a short calibration that yields separate skill estimates.
7. Ask, “I have 60 minutes; let’s continue.”
8. Receive three justified blocks connected to due review, current curriculum, and skill balance.
9. Complete text and speaking tasks with fluency or accuracy correction.
10. For speaking, create and validate a `lingua.session.v1` package, ingest it idempotently, preserve raw/normalized/reviewed transcript layers, and inspect audio for any confirmed pronunciation claim.
11. Flush/import observations as batched staged events, then close the session and atomically materialize attempts, evidence, errors, source progress, follow-ups, and candidates.
12. Review and approve a small Anki set whose persistent items include origin/review metadata; export valid UTF-8 TSV.
13. Run `workspace snapshot`, review the privacy-checked generated wiki/report diff, and commit it to the private learner repository.
14. Verify native and portable DuckDB backups outside Git.
15. Restart the agent with no chat context and continue from database state.
16. Dry-run a core/pack upgrade and confirm it proposes migrations/skill refresh rather than a Git merge from the core repository.

The scenario fails if learner state enters the core repository, a core upgrade requires merging product source into learner history, private/raw artifacts become Git candidates, the system assumes all A2 content is mastered, duplicates event batches/cards on retry, loses staged work after a completed block, needs direct SQL, loses source provenance, edits generated Markdown as state, or cannot explain progress estimates.

## 23. Implementation order inside each phase

For each vertical slice:

1. Specify command input/output schemas and state transitions.
2. Add/modify migrations and migration tests.
3. Implement repositories and domain services.
4. Add CLI commands with JSON and human output.
5. Write focused skill instructions and references.
6. Add integration and behavioral tests.
7. Render/update wiki views only after authoritative writes work.
8. Run an end-to-end fixture and document observed limitations.

This order keeps skills thin and prevents prompt text from becoming a substitute for missing domain logic.

## 24. Risks and mitigations

| Risk | Mitigation |
|---|---|
| DuckDB and Markdown diverge | One-way generated projection, hashes, drift checker, controlled draft import. |
| Learner workspace drifts from core | Explicit core/schema/skill/pack lock, doctor checks, transactional upgrader, generated skill refresh—not upstream Git merges. |
| Personal data leaks through Git | Private learner repo, strict ignores, staged-file privacy scan, no automatic commit/push, raw media/transcripts/backups outside Git. |
| Agent corrupts state or retries a write | No direct SQL, Pydantic validation, transactions, idempotency keys, audit log. |
| Conversation ends before close | Durable idempotent batches at block boundaries; resume, partial close, abandon, and reviewed recovery paths. |
| Per-observation CLI overhead | Buffer structured events and call `session log` once per block or bounded batch. |
| “A2” creates false confidence | Provisional low-confidence estimates, prerequisite sampling, calibration queue. |
| Core accidentally encodes Polish | Capability-based packs, contrasting synthetic tests, Chinese portability phase. |
| AI invents examples or pronunciation pairs | Provenance/review states, verified sources, pack tests, no automatic approval. |
| Pack work dominates the schedule | Explicit maturity gates, coverage tooling, separate content estimates, thin pilot before full A1–B1 scope. |
| Learner accumulates too much content | Resource and novelty budgets, usefulness filters, inactive reference items, card caps. |
| Assessment scores are unstable | Versioned rubrics/forms, evidence excerpts, scorer metadata, uncertainty, review for claims. |
| Voice data harms privacy | Opt-in retention, local artifacts, explicit external upload, redacted logs. |
| STT hides or invents learner errors | Immutable raw transcript, reviewed-hearing layer, audio-linked uncertainty, and no pronunciation judgment from text alone. |
| Defective AI template contaminates durable content | Item-level origin/reviews, first-three-run full inspection, sampling, batch quarantine, and dependency invalidation. |
| DuckDB writer contention | Short commands, application lock, one writer, no connection across conversations. |
| DuckDB upgrade prevents native open | Exact dependency pin plus verified Parquet/JSON portable snapshots and restore fixtures. |
| Pack update breaks references | Stable IDs, dry-run diff, content versions, never overwrite learner content. |
| Anki becomes a second truth | DuckDB note source, Anki scheduling only, stable IDs and sync hashes. |

## 25. First implementation backlog

Create tickets as vertical slices, introducing each skill when its commands become usable:

1. Record Phase 0 ADRs: core/pack/workspace ownership, Codex repository runtime, provider-independent session packages, AI-assisted risk-based authoring, and private `git-wiki` learner history.
2. Bootstrap the `LinguaWiki` core package, tests/lint/types, glossary, typed IDs, clock, JSON envelope, and structured errors—without any real learner directories.
3. Define `lingua.workspace.v1`, `linguawiki.lock`, the learner template, ignore/privacy rules, and `workspace init|status|doctor`.
4. Implement database location, connection, writer lock, transactions, migrations, workspace version mirror, audit, native backup, portable export/restore, and upgrade fixtures.
5. Add the Codex skill installer/snapshot plus a smoke-test umbrella skill that can call `status` from a generated workspace.
6. Define item-level content origin, review axes, dependencies/invalidation, template sampling/quarantine, language-pack schemas, synthetic packs, maturity gates, and `linguawiki-pack`.
7. Start a thin Polish A2 pilot pack; keep it in core only until the pack API stabilizes, then publish/extract it as a separately pinned pack.
8. Implement user, track, framework validation, declared-level onboarding, resource planning, generic curriculum import/audit, and `linguawiki-init`.
9. Implement the placement algorithm, bank exposure tracking, pause/resume, confidence reporting, and `linguawiki-assess`.
10. Add knowledge/content provenance, session-package, transcript-layer, staged-session, attempts, evidence, error, estimate, and context-bundle schemas/services.
11. Implement `lingua.session.v1` validation/idempotent ingest, session planning, batched `session log`, close/partial/abandon/recover, and crash tests.
12. Add `linguawiki-learn`, minimum dashboard projection, and create the independent private `PolishLinguaWiki` dogfood workspace.
13. Implement source/artifact/curriculum-progress, reading/listening/media, manual speaking packages, transcript/audio review, retention, and `linguawiki-speak`.
14. Implement rich review/error lifecycle and add `linguawiki-review`.
15. Implement Anki candidate validation/approval/export and add `linguawiki-anki`.
16. Complete wiki projection, controlled draft import, reports, full lint, `workspace snapshot`, privacy checks, and `linguawiki-maintain`.
17. Implement dry-run/transactional `workspace upgrade`, generated-skill refresh, pack upgrade, rollback, and cross-version fixtures.
18. Continue four weeks of Polish dogfooding while expanding toward the selected A1–B1 pack maturity gate.
19. Build the chosen Chinese pilot pack and create an independent `ChineseLinguaWiki` workspace using the same core contracts.

## 26. Open decisions

### Blocking before Phase 0

None. The plan now selects private `git-wiki` learner workspaces with portable backups outside Git.

### Deferrable until the named feature

- Chinese variant: `zh-Hans` and Mainland Mandarin, or `zh-Hant` with another region/standard (before Phase 9).
- Whether the Polish pack distributes a licensed Hurra course map or uses only a local user-supplied adapter (before course-specific pack content).
- Existing versus bundled Anki note model (before Phase 6).
- Initial voice application/STT/TTS providers and whether an export adapter is practical; the manual session-package path does not depend on this choice (before Phase 5 provider automation).
- Default full-audio/transcript retention duration and consent wording (before storing real speaking data).
- Backup encryption and destination/retention details beyond the required portable export (before personal data is created).
- Whether a future UI exposes more than one local user; the storage schema already supports it without treating users as authenticated identities.

No acceptance scenario assumes a timezone from the development environment. The learner's configured IANA timezone and real-life location are separate values.

## 27. Definition of done

LinguaWiki is complete for its first production-quality release when:

- the core repository contains product code, generic skills, schemas, fixtures, and templates but no real learner state;
- `PolishLinguaWiki` and later learner workspaces are independent private repositories generated from a versioned template rather than forks carrying core source;
- learner workspaces pin core/schema/skill/pack hashes and upgrade transactionally without upstream Git merges;
- sanitized wiki history is commit-ready while DuckDB, backups, raw transcripts, recordings, and private imports remain outside Git;
- DuckDB is the recoverable, migrated, integrity-checked authority for all persistent runtime objects;
- native and portable backups restore successfully under the pinned DuckDB upgrade test;
- Markdown is rebuildable, linked, readable, and drift-detectable;
- the umbrella and specialist skills are valid, discoverable, progressively disclosed, and behaviorally tested;
- Codex skills are the supported MVP repository interface while CLI/session schemas remain runtime/provider portable;
- the Python CLI is typed, transactional, idempotent where retries matter, and usable without an agent;
- language initialization supports both declared-level and placement modes;
- declared levels are validated inside pack-declared frameworks and comprehensive placement runs only with adequate bank coverage;
- level-specific resource preparation is bounded, provenance-aware, and reversible;
- learner progress is multidimensional, evidence-backed, uncertainty-aware, and explainable;
- session planning and closure support the full learning loop in the source specification;
- live observations are batched durably, crash-recoverable, and materialized exactly once at completed or partial close;
- speaking packages preserve raw, normalized, reviewed-hearing, and pedagogical-correction layers, with audio required for confirmed pronunciation/prosody evidence;
- every persistent instructional item has item-level origin, independent review axes, lifecycle state, and dependency invalidation appropriate to its risk tier;
- sources, errors, reviews, Anki, assessments, and reports connect through stable IDs;
- Polish works end to end and passes its explicitly selected pack-maturity gate;
- a Chinese pack proves that the core is genuinely language-agnostic;
- a fresh agent can continue learning safely with no prior conversation context.
