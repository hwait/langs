# LinguaWiki core repository instructions

This repository owns the generic Python package, schemas, migrations, Codex skills, templates, and synthetic fixtures. It must never contain real learner state.

## Invariants

- Keep core language-agnostic. Language-specific knowledge and framework mappings belong in versioned language packs.
- Use DuckDB as the sole authority for mutable learner state once persistence is introduced. Markdown is a deterministic projection or controlled import surface.
- Make deterministic mechanics available through typed Python and CLI contracts. Skills orchestrate those tools; they do not edit databases or reproduce business rules in prompts.
- Use UTC internally and explicit IANA timezones at user/track boundaries.
- Preserve typed opaque identifiers, provenance, idempotency, privacy, and explainability across changes.
- Keep only synthetic learner/session fixtures in this repository. Never add a real database, transcript, recording, backup, exported deck, or learner workspace.
- Learner repositories pin released core, schema, skill-bundle, and pack versions. They do not merge this repository as an upstream fork.

## Verification

Before handing off a change, run the relevant subset and normally the full gate:

```bash
uv run python scripts/verify.py
```
