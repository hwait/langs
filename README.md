# LinguaWiki

LinguaWiki is a local-first, language-agnostic learning system. Its Python core exposes typed CLI and JSON contracts for learner workspaces, language packs, sessions, and progress data. Persistent learner state will live in a private DuckDB-backed workspace; this core repository contains only reusable code and synthetic fixtures.

Stage 0 establishes the architecture and frozen v1 interchange contracts. See [the implementation plan](docs/LinguaWiki%20Implementation%20Plan.md) and [Stage 0](docs/stage0.md) for the detailed design and verification gate.

Run the current smoke command with:

```bash
linguawiki status --format json
```

Development verification uses:

```bash
uv run python scripts/verify.py
```
