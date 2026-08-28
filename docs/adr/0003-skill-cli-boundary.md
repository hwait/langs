# ADR 0003: Skills orchestrate; Python enforces mechanics

Status: accepted for Stage 0

## Decision

Codex is the primary repository-agent runtime for the MVP. Skills handle conversational and pedagogical orchestration, request bounded context, and call a versioned CLI. Python owns identifiers, validation, calculations, state transitions, idempotency, persistence, and deterministic rendering. CLI machine output uses a stable JSON success/error envelope.

## Alternatives rejected

- Prompt-only state and calculations: behavior is neither reproducible nor independently testable.
- Direct SQL from skills: bypasses domain validation, audit, and idempotency.
- Vendor-specific domain formats: impede later runtime and voice adapters.

## Consequences

The CLI must remain usable without an agent. Skills should be short and disclose specialist guidance progressively as stages add it.

## Enforced invariants

- `.agents/skills/linguawiki/SKILL.md` calls only the Stage 0 status command and uses Codex's documented repository-discovery location.
- CLI compatibility snapshots and JSON schemas freeze the v1 envelope.
- `tests/skills/test_linguawiki_skill.py` exercises the command outside the repository directory.
