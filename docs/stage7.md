# Stage 7 — Wiki, Reports, Maintenance, and Upgrades

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)
Previous: [Stage 6](stage6.md) · Next: [Stage 8](stage8.md)

## Outcome

LinguaWiki becomes an operational MVP: its human-readable history is deterministic, its health is inspectable, its private learner repository is safe to commit, and core/pack upgrades do not require merging product source into learner history.

## Estimate

- Engineering: 6–10 days
- Operational review: 1–3 days

## Entry criteria

- Stage 6 exit gate passes.
- The learner workspace contains representative sessions, sources, errors, reviews, and Anki decisions.

## Work packages

### 7.1 Full wiki projection

- [ ] Render dashboard, goals, skill estimates, knowledge areas, errors, sources, sessions, reviews, Anki decisions, and provenance summaries.
- [ ] Use stable paths/order and projection hashes for readable Git diffs.
- [ ] Rebuild from DuckDB without relying on existing Markdown.
- [ ] Detect stale, missing, or manually changed generated files.

### 7.2 Controlled draft import

- [ ] Separate generated pages from explicitly editable draft/import areas.
- [ ] Parse drafts into validated proposals and show a diff before persistence.
- [ ] Route imported content through provenance and review-state rules.
- [ ] Never silently treat arbitrary Markdown edits as learner truth.

### 7.3 Reports

- [ ] Generate weekly, monthly, and milestone reports.
- [ ] Reconcile duration, activities, evidence, source progress, review load, and estimate changes with authoritative rows.
- [ ] Explain trends and uncertainty without overstating mastery.
- [ ] Keep reports sanitized and suitable for private Git history.

### 7.4 Maintenance and lint

- [ ] Check DB integrity, migration state, orphaned relations, duplicate IDs, invalid evidence, stale projections, unreviewed risky content, incomplete sessions, missing artifacts, and lock drift.
- [ ] Provide repair suggestions; require reviewed commands for mutations.
- [ ] Add a recovery guide covering staged sessions, corrupted projections, and restore-to-new-path.

### 7.5 Workspace snapshot

- [ ] Rebuild projections and reports before snapshot.
- [ ] Run privacy/ignore/staged-file checks and show the exact Git diff.
- [ ] Never commit or push automatically.
- [ ] Document committing sanitized `wiki/`, reports, config, manifests, and lock files as the learner's readable history.

### 7.6 Transactional upgrades

- [ ] Implement dry-run compatibility checks for core, schema, skill, Python environment, and packs.
- [ ] Back up the DB and current locks/skill snapshot before mutation.
- [ ] Apply migrations, refresh generated skills, rebuild projections, and run doctor/lint.
- [ ] Restore DB, environment, locks, and skills if any step fails.
- [ ] Confirm upgrades are dependency/contract upgrades, not Git merges from core.

### 7.7 Operational skill set

- [ ] Complete `linguawiki-maintain`.
- [ ] Polish the umbrella and all specialist skills introduced in Stages 0–6.
- [ ] Split learner-workspace `AGENTS.md` from the core-development contract.
- [ ] Verify a fresh Codex session discovers only the right skill surface for its repository.

## Required commands

```text
linguawiki wiki build|check|diff|import-draft
linguawiki report weekly|monthly|milestone
linguawiki lint
linguawiki workspace snapshot|doctor|upgrade --dry-run
linguawiki workspace upgrade
```

## Verification

- [ ] Delete `wiki/`, rebuild it, and compare deterministic output.
- [ ] Introduce fixture drift and verify it is detected.
- [ ] Reconcile weekly-report totals with raw fixture rows.
- [ ] Verify snapshots reject DuckDB, backups, raw audio, raw transcripts, secrets, and cache files.
- [ ] Exercise successful, incompatible, and deliberately failed upgrades.
- [ ] Start a fresh agent without chat history and complete a normal maintenance operation from skills and state alone.
- [ ] Run the repository-wide quality gate.

## Exit gate

Stage 7 is complete when the learner repo is safe and useful as readable versioned history, the database remains the sole authority, full rebuilds are deterministic, and upgrades/recovery succeed without merging or copying core source. This is the operational MVP.

## Handoff to Stage 8

Stage 8 hardens the system through sustained Polish use and promotes the Polish pack only when measured coverage and review gates pass.
