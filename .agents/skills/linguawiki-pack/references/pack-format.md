# Pack directory format

A pack is a directory whose `manifest.json` declares its identity, its promises, and a
checksum for **every other file in the directory**. Checksum coverage is total: an
undeclared file present on disk fails validation, and a declared file missing from disk
fails too. Roles are assigned by path.

```text
<pack>/
├── manifest.json                     # lingua.pack.v1
├── capabilities.json                 # lingua.pack.capabilities.v1   (required)
├── source-policy.json                # lingua.pack.source-policy.v1  (required)
├── proficiency/<framework>.json      # lingua.pack.proficiency.v1
├── seed/knowledge.jsonl              # lingua.pack.knowledge.v1, one item per line (required)
├── seed/relations.jsonl              # lingua.pack.relation.v1
├── seed/examples.jsonl               # lingua.pack.example.v1
├── assessments/<form>.json           # lingua.pack.assessment.v1
├── activities/<file>.json            # lingua.pack.activities.v1
├── references/<file>.json            # lingua.pack.references.v1
├── resource-bundles/<bundle>.json    # lingua.pack.bundle.v1
└── tests/expectations.json           # lingua.pack.expectations.v1
```

Every schema is published under `schemas/` in the core repository. Validate against the
schema first and the CLI second; the CLI additionally checks the cross-file rules JSON
Schema cannot express.

## What the manifest decides

- `pack_key` is the pack's stable identity and part of every derived item ID. Renaming it
  changes every identity and hash in the pack, so treat it as immutable after first use.
- `version` follows `MAJOR.MINOR.PATCH`. A published version is immutable: change the
  contents and the version together.
- `frameworks` declares each supported framework with its **ordered** levels. The order is
  part of the framework's identity — an installed framework whose level sequence differs is
  refused rather than merged.
- `bands` names the levels the pack claims to cover. Coverage gates are evaluated per band.
- `dimensions` names what the pack can test, and `dimension_kinds` says what each dimension
  *is* (`receptive`, `productive`, `form`, `pronunciation`). Core never classifies a
  dimension by its name, so a missing or wrong classification silently changes which task
  types and budgets apply.
- `themes` and `modalities` are closed lists: an item using an undeclared theme, or a task
  needing an undeclared modality, fails validation.
- `origin_profiles` and `review_profiles` are named provenance shorthands. An item names one
  or declares its own; the loader resolves the profile into item-level rows before anything
  is hashed or stored, so item-level provenance is never lost.
- `files` and `content_address` are written by `pack publish` only. Never hand-edit them.

## Identity and hashing

- An item is identified by `stable_key` inside its kind. Keys are lowercase, dot-separated,
  and permanent: a renamed key is a *new* item, and the old one's learner state is orphaned.
- `content_id` is derived from `(pack_key, content kind, stable_key)`. No pack file ever
  asserts an ID.
- `provenance.content_hash` is derived from LinguaWiki Canonical Content JSON v1 over the
  item's identity, text, risk tier, provenance, and content dependencies. Reviews and
  lifecycle are deliberately *excluded*, so recording a review does not change the hash.
- Run `linguawiki pack stamp` after editing an item, and `--check` in review.

## Bundles and preparation

A bundle lists what a level's preparation may draw on, and `depends_on` names its
prerequisite bundles. Preparation resolves the requested band's bundles plus their
prerequisites recursively, and nothing else — so a level that should not pull in the whole
pack simply must not depend on it. A bundle's `framework` and `level` are part of its
identity and are not updated in place; correct them by publishing a new bundle key.

Only items whose lifecycle is promoted (`approved-personal`, `verified`,
`publication-ready`) and which are not quarantined ever reach a learner. Listing a draft in
a bundle is therefore safe and useful: it documents the intent while the filter keeps the
draft out.
