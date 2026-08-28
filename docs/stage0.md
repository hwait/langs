# Stage 0 — Architecture, Contracts, and Repository Boundaries

Implementation status: complete and reverified after v1 contract hardening on 2026-08-28.

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)

Next: [Stage 1 — Learner Workspace and Storage Foundation](stage1.md)

## Outcome

Create the executable architectural foundation for LinguaWiki without storing real learner data. At the end of this stage, the core repository builds, its contracts validate, Codex can invoke one smoke-test skill, and automated tests prove that core, language packs, and learner workspaces are distinct artifacts.

## Estimate

- Engineering: 5–8 days.
- Content/review: 1–2 days for synthetic fixtures and contract review.

## Entry criteria

- The architectural decisions in the parent plan are accepted.
- Codex is the primary repository-agent runtime.
- Learner history uses private `git-wiki` workspaces with portable backups outside Git.
- No production Polish or Chinese content is required yet.

## Work packages

### 0.1 Bootstrap the core repository

- [x] Create `pyproject.toml` for Python 3.12+ and the `linguawiki` CLI entry point.
- [x] Add exact runtime/development dependencies and generate `uv.lock`.
- [x] Create `src/linguawiki/`, `tests/`, `schemas/`, `.agents/skills/`, `templates/`, and `language-packs/fixtures/`.
- [x] Configure Ruff, type checking, pytest, and coverage.
- [x] Add a core `.gitignore` that rejects real DuckDB files, recordings, transcripts, imports, exports, and learner workspaces.
- [x] Add a core-development `AGENTS.md` that explicitly forbids real learner state in core fixtures.

### 0.2 Record architecture decisions

Create concise ADRs under `docs/adr/`:

- [x] `0001-repository-topology.md`: core, pack, and independent learner workspace ownership; no ordinary learner forks.
- [x] `0002-duckdb-authority.md`: DuckDB authority, Markdown projection, writer-lock model, and portable recovery.
- [x] `0003-skill-cli-boundary.md`: Codex skills make pedagogical decisions; Python owns deterministic mechanics.
- [x] `0004-speaking-session-package.md`: provider-independent `lingua.session.v1` boundary.
- [x] `0005-content-provenance.md`: AI-assisted drafts, item-level origin, independent review axes, and risk tiers.
- [x] `0006-evidence-and-mastery.md`: recognition/production separation and explainable estimates.
- [x] `0007-language-pack-contract.md`: capability-driven, framework-aware, language-agnostic packs.

Each ADR must list decision, alternatives rejected, consequences, and invariants that require tests.

### 0.3 Define common machine contracts

- [x] Define typed opaque ID prefixes for workspace, user, track, pack, content, source, session, block, activity, attempt, evidence, error, review, assessment, Anki note, event, and artifact.
- [x] Define the versioned JSON success/error envelope used by every CLI command.
- [x] Define stable error fields: code, message, retryable, field details, correlation ID.
- [x] Define UTC timestamp and user IANA-timezone rules.
- [x] Create `schemas/lingua.workspace.v1.json`.
- [x] Create `schemas/lingua.session.v1.json`.
- [x] Create `schemas/lingua.content.v1.json`.
- [x] Define `linguawiki.lock` schema with core, database schema, skill bundle, pack versions, and hashes.
- [x] Add Pydantic models and JSON-schema validation tests for every contract.

### 0.4 Create language-agnostic fixtures

- [x] Create a synthetic inflected, whitespace-delimited fixture pack.
- [x] Create a synthetic tonal, non-whitespace fixture pack.
- [x] Give both packs the same proficiency dimensions and test capabilities through different linguistic structures.
- [x] Add a synthetic `lingua.session.v1` package with transcript, event log, and fake local audio hashes.
- [x] Ensure fixtures contain no copied or personal language material.

### 0.5 Establish the Codex skill mechanism

- [x] Create the minimal umbrella `.agents/skills/linguawiki/SKILL.md`.
- [x] Add Codex metadata in `agents/openai.yaml`.
- [x] Implement only enough routing to call `linguawiki status --format json`.
- [x] Add a deterministic skill validator using the supported skill-validation tooling.
- [x] Prove a generated/isolated fixture can discover the skill and invoke the CLI.

### 0.6 Establish continuous verification

- [x] Add local commands and CI for format, lint, type check, unit tests, schema tests, and skill validation.
- [x] Add a repository scan that fails if real learner paths or likely recordings/databases are committed.
- [x] Add contract-compatibility snapshots for JSON envelopes and schemas.
- [x] Document how a schema change becomes a new version rather than silently mutating v1.

## Expected repository artifacts

```text
pyproject.toml
uv.lock
AGENTS.md
src/linguawiki/{cli,ids,clock,errors,contracts}.py
schemas/lingua.workspace.v1.json
schemas/lingua.session.v1.json
schemas/lingua.content.v1.json
.agents/skills/linguawiki/SKILL.md
.agents/skills/linguawiki/agents/openai.yaml
language-packs/fixtures/{inflected,tonal}/
tests/{unit,contracts,skills}/
docs/adr/0001-*.md ... 0007-*.md
```

## Verification

```bash
uv sync --locked
uv run python scripts/verify.py
```

The real installed-Codex discovery probe is intentionally marked `external` and is not run in CI. It was executed manually on 2026-08-28 with `LINGUAWIKI_RUN_CODEX_DISCOVERY=1`; Codex discovered `.agents/skills/linguawiki/SKILL.md` and the test passed. The deterministic parser and skill validators remain in the normal gate.

## Exit gate

- [x] A clean checkout passes every verification command.
- [x] Every architectural invariant is linked to a test, lint rule, or later-stage acceptance item.
- [x] Core contains no real learner data or production-language assumptions.
- [x] Both contrasting fixture packs validate against the same generic contract.
- [x] The synthetic speaking package validates and preserves audio-only evidence requirements.
- [x] A Codex smoke skill calls the CLI in an isolated fixture.
- [x] No Stage 1 persistence implementation is hidden in prompts or placeholder scripts.

## Handoff to Stage 1

Stage 1 receives frozen v1 contract schemas, typed IDs, the CLI envelope, repository templates, synthetic packs, and a working Codex smoke path. Contract changes after this gate require an ADR amendment and compatibility plan.
