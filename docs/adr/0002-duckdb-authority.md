# ADR 0002: DuckDB authority and generated Markdown

Status: accepted for Stage 0

## Decision

From Stage 1 onward, one local DuckDB database is authoritative for every mutable learner object. Markdown is a deterministic, sanitized projection and controlled proposal/import surface. Writes use short transactions and an application-level single-writer lock. Native backups and format-independent exports live outside learner Git history.

## Alternatives rejected

- Markdown as primary storage: relationships, idempotency, migrations, and atomic session close become fragile.
- A network database: conflicts with the local-first goal and adds operations/authentication before they are needed.
- Committing DuckDB to Git: binary diffs are opaque and risk leaking raw learner data.

## Consequences

The database must be reconstructable from verified backups, not from wiki Markdown. DuckDB is pinned exactly and upgrades require native-open plus portable round-trip tests.

## Enforced invariants

- Stage 0 privacy checks reject database files in core.
- Stage 1 tests writer exclusion, rollback, native backup, portable export, and restore.
- Stage 7 tests deterministic projection rebuild and drift detection.
