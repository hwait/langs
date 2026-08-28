# ADR 0001: Separate core, packs, and learner workspaces

Status: accepted for Stage 0

## Decision

LinguaWiki core is a versioned Python product containing generic code, schemas, skills, templates, and synthetic fixtures. Reusable language content is independently versioned in language packs once the pack API stabilizes. Each learner program is an independent private repository generated from a released template; it pins core, schema, skill-bundle, and pack versions and commits only sanitized configuration and wiki history.

Learner repositories do not use a normal source fork relationship with core. Upgrades change pinned dependencies, run migrations, refresh generated skills, and rebuild projections.

## Alternatives rejected

- A branch/fork per learner: product history and private learning history become entangled, and upgrades require inappropriate merges.
- One repository/database for all learners and languages: privacy, portability, and ownership become harder to reason about.
- Copying core source into each workspace: fixes and compatibility cannot be versioned cleanly.

## Consequences

Core, packs, and learner workspaces need explicit compatibility contracts. A user may still create a private Git repository named `PolishLinguaWiki`, but it is generated from a workspace template rather than maintained as a code fork.

## Enforced invariants

- `AGENTS.md`, `.gitignore`, and `scripts/check_repository_privacy.py` reject real learner artifacts in core.
- `tests/skills/test_linguawiki_skill.py` proves the installed CLI works away from the source tree.
- Stage 1 tests workspace generation without copying `src/linguawiki`; Stage 7 tests transactional upgrades.
