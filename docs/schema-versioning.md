# Contract schema versioning

Checked-in files under `schemas/` are compatibility contracts generated from the Pydantic models by `scripts/generate_schemas.py`. Tests fail when the models and snapshots differ. Each schema carries `x-linguawiki-semantic-model`; consumers must use `validate_json_contract` (or an equivalent implementation) so cross-record chronology, hash binding, and reference rules are checked after structural JSON Schema validation. Standard JSON Schema cannot express those dynamic graph comparisons by itself.

Once a contract version is released:

1. Do not silently change the meaning of existing fields or validation rules.
2. Add optional backward-compatible fields only when old producers and consumers remain valid.
3. For a breaking change, introduce a new schema name/version and retain the old model while it is supported.
4. Add an explicit adapter or migration and fixtures for both versions.
5. Update compatibility declarations and locks only after old-to-new tests pass.
6. Regenerate snapshots deliberately and review their structural diff.

## Released contract changes

| Stage | Contract | Change | Compatibility |
|---|---|---|---|
| 1 | `linguawiki.cli.success.v1` | Added `warnings: string[]`, default `[]`. | Additive and defaulted; old producers stay valid and old consumers ignore it. |
| 1 | `linguawiki.cli.status.v1` | Added `database_schema_version`; `stage` is now `1`; `persistence` is now `available`. | The status envelope reports the running release, so both values move with each stage. Consumers must read them, not assume them. |
| 2 | `linguawiki.cli.status.v1` | `stage` is now `2`; `database_schema_version` is now `18`. | Both track the running release. `contracts.DELIVERY_STAGE` is the constant code and scripts read, and a contract test keeps it equal to the wire literal. |
| 3 | `linguawiki.cli.status.v1` | `stage` is now `3`; `database_schema_version` is now `22`. | Both track the running release, as before. Stage 3 added migrations 0019-0022 and no new wire contract: the learner model is reported through the generic success envelope, whose `data` is deliberately unconstrained. |
| 4 | `linguawiki.cli.status.v1` | `stage` is now `4`; `database_schema_version` is now `23`. | Both track the running release, as before. Stage 4 added migration 0023 and one new published contract, below. |
| 4 | `lingua.session.events.v1` | **New** contract: the batch `session log` accepts -- a sequence number, an idempotency key, an optional content hash, and an ordered array of `attempt.observed`, `correction.given`, `pronunciation.assessment`, `observation.noted`, or `follow_up` events. | Additive: a new schema name, no change to any existing contract. Deliberately *not* an extension of `lingua.session.v1`, which is a whole externally produced session; see below. |
| 2 | `lingua.pack.v1` and friends | **New** contracts for the language-pack directory format: `lingua.pack.v1` (manifest), `lingua.pack.capabilities.v1`, `lingua.pack.source-policy.v1`, `lingua.pack.proficiency.v1`, `lingua.pack.assessment.v1`, `lingua.pack.activities.v1`, `lingua.pack.references.v1`, `lingua.pack.bundle.v1`, `lingua.pack.expectations.v1`, and the JSONL line contracts `lingua.pack.knowledge.v1`, `lingua.pack.relation.v1`, `lingua.pack.example.v1`. | Additive: new schema names, no change to any existing contract. |

Stage 4 added one contract and left `lingua.session.v1` alone, and the split is the
point. `lingua.session.v1` is a *whole session produced elsewhere*: it arrives as a file
with transcript layers and artifacts, and it is trusted only as far as its layers allow.
`lingua.session.events.v1` is *one flush from a session this workspace is running*, and it
says directly what the learner did. Collapsing them into one schema would mean either
requiring a transcript from a skill that has none, or accepting an unverified hearing as a
direct observation. The vocabularies each payload validates against are the evidence
policy's own -- task types, modalities, claims, help levels -- published into the schema
from the single tuple that defines them, so the schema cannot drift from the rules.

Stage 3 deliberately added **no** new published contract. Attempts, evidence, errors,
mastery, estimates, and context bundles are reported through `linguawiki.cli.success.v1`,
whose `data` is unconstrained by design, and their shapes are typed in Python by
`ContractModel` subclasses. Freezing them as published schemas now would pin a learner
model that the session engine in Stage 4 still has to extend; what *is* frozen is the
vocabulary those models validate against -- the evidence claims, mastery stages, error
statuses, and estimate statuses -- each of which lives in one module and is versioned by
`evidence.STRENGTH_VERSION`, `mastery.AGGREGATION_VERSION`, `error_model.POLICY_VERSION`,
and `estimates.CALCULATION_VERSION`. Those version strings are stored on every row they
produced, so a policy change can be told apart from a data change and replayed.

`lingua.workspace.v1`, `lingua.session.v1`, `lingua.content.v1`, and `linguawiki.lock.v1` are
unchanged since Stage 0 -- including through Stage 4, which ingests `lingua.session.v1`
packages without amending them. `history_policy` in particular stays the constant `git-wiki`; the other
policies in the implementation plan need an amendment recorded here first.

Stage 2 deliberately did **not** amend `lingua.content.v1`. Its four review states
(`not-required`, `pending`, `passed`, `failed`) are the *gate* vocabulary; the richer per-axis
states the pack contract uses (`reference-verified`, `learner-approved`, `cleared`, and so on)
are projected onto them by `provenance.content_reviews`. A contract test asserts that whatever
the Stage 2 gate accepts, `lingua.content.v1` accepts too, so the v1 floor ADR 0005 froze cannot
be weakened by a policy added on top of it.

Pack files are JSON and JSONL rather than the YAML the implementation plan sketches. The reason
is dependency closure, not preference: the plan's `manifest.yaml` would make a YAML parser a
*runtime* dependency of the core, and every published schema, every fixture, and the workspace
lock are already JSON. The layout, the file roles, and the field names are the plan's; only the
serialization differs.

Use:

```bash
uv run python scripts/generate_schemas.py
uv run python scripts/generate_schemas.py --check
```

The first command is an intentional contract update; the second is the normal verification command.
