---
name: linguawiki-pack
description: Create, validate, install, measure, review, and publish a LinguaWiki language pack. Use for pack authoring, AI-assisted drafting batches, per-axis content review, maturity and coverage gates, template sampling and quarantine, and pack install/diff/update. This is author tooling, not a learner lesson.
---

# LinguaWiki pack authoring

Every command below is a `linguawiki` CLI call with `--format json`. Never edit a pack's
files to make validation pass, and never write pack content into a learner database by
hand: the CLI derives identities and hashes, and a hand edit breaks both.

A pack is a *versioned directory*. Its identity is `pack_key` plus `version`; its contents
are content-addressed. Item identities are **derived** from `(pack_key, kind, stable_key)`,
so a reinstall keeps every learner annotation attached.

## Start a pack

```bash
linguawiki pack scaffold <new-dir> --pack-key <key> --name "<display name>" \
  --language <bcp47> --framework <id> --framework-name "<name>" --framework-version <v> \
  --level A1 --level A2 --level B1 --band A2 --theme "<theme>" [--support <tag>] --format json
```

`pack scaffold` writes the declared structure and **no content**. The result is a `fixture`:
it validates, is never offered to a learner, and has to earn every stronger maturity through
measured coverage. Edit `capabilities.json` and `source-policy.json` first — the scaffold
guesses neither the language's structure nor its authoritative sources — and edit
`dimension_kinds` in the manifest if this language's dimensions differ from the default set.

## Validate, measure, publish

```bash
linguawiki pack validate <pack-dir-or-key> --format json
linguawiki pack coverage <pack-dir-or-key> --format json
linguawiki pack stamp    <pack-dir> [--check] --format json
linguawiki pack publish  <pack-dir> [--maturity <level>] --format json
```

`pack validate` proves the pack is internally consistent: total checksum coverage, derived
identities, resolved references, declared levels/themes/dimensions, correct content hashes,
and a lifecycle every item's own review states support.

`pack stamp` recomputes the `content_hash` each item declares. An item's hash is derived,
never authored, so **edit the item and then stamp**. `--check` fails instead of rewriting;
that is the form to use when reviewing someone else's pack.

`pack coverage` reports counts *and* qualitative gaps: theme distribution, provenance mix,
review debt per axis, descriptor coverage, prerequisite connectivity, support-language
coverage, and which maturity level the measured coverage actually supports. Counts never
pass a pack on their own — read `gaps` and `expectation_failures`, not just the numbers.

`pack publish` stamps file checksums and the content address, and refuses when the maturity
gate is unmet. Report the unmet requirements verbatim; do not publish at a lower maturity to
get past them unless the author asked for that.

Maturity means what a pack may *promise*: `fixture` is never offered to a learner, `pilot`
supports a labelled calibration only, `onboarding-ready` supports declared-level
initialization, and `placement-ready` is the only level comprehensive placement runs at.

## Install, diff, update

```bash
linguawiki pack install --workspace <path> <pack> [--dry-run] --format json
linguawiki pack diff    --workspace <path> <pack> --format json
linguawiki pack update  --workspace <path> <pack> [--apply] --format json
linguawiki pack list    --workspace <path> --format json
```

Reinstalling the same version is an idempotent no-op that still proves the bytes match; a
*different* content address under the same version is refused, because a published version
is immutable. `pack update` previews by default and applies only with `--apply`. An item
that disappears from a newer pack is deprecated rather than deleted, and learner content
depending on a changed item is marked `needs-review` — the preview lists both.

## Authoring with AI assistance

Drafting is bounded before it starts, and nothing it produces is approved by producing it.

```bash
linguawiki pack template validate  --workspace <path> --input <template.json> --format json
linguawiki pack author generate-draft --workspace <path> \
  --template-key <key> --template-version <n> --count <n> --format json
linguawiki pack author import --workspace <path> --batch <batch-id> --input <items.json>
linguawiki pack author review-queue --workspace <path> [--limit <n>] --format json
linguawiki pack author review --workspace <path> --content <id> --axis <axis> \
  --state <state> --reviewer-kind <kind> [--reviewer <who>] [--method <how>] \
  [--inspection accepted|defective] [--finding <what>] --format json
linguawiki pack author approve  --workspace <path> --content <id> --lifecycle <target>
linguawiki pack author reject   --workspace <path> --content <id> --reason <why>
linguawiki pack author invalidate --workspace <path> --reason <why> \
  [--content <id> ...] [--batch <id>]
```

The batch tells you your inspection duty: read `sampling_policy` and `required_sample`. A
new or materially changed template is `full-inspection` until it has three inspected,
defect-free runs; after `pack template stabilize` it samples 20% or five items, whichever is
larger. The duty is fixed when the batch opens and cannot be renegotiated once the output
looks convincing.

Record an inspection with the review that performed it. A `defective` inspection quarantines
the whole batch, quarantines its template, and invalidates dependent content — that is
intended, not a bug: a template that produced one bad item has no claim to the rest.

Read `references/review-axes.md` before recording any review, and
`references/pack-format.md` before creating or editing pack files.

## Boundaries

- You may reach `machine-checked` as a machine or AI reviewer, and nothing stronger. Claiming
  `reference-verified`, `human-verified`, or `teacher-verified` for your own output is
  refused, and running the check again does not change that.
- A promotion is a claim. `pack author approve` re-runs the gate and reports each unmet axis;
  do not lower `risk_tier` to get past it.
- Risk tier 3 and 4 content needs a *verified* source alignment and an identified external
  source. AI generation alone can never support a canonical claim.
- Publication-ready AI-origin content needs two different reviewer identities on the
  linguistic and pedagogical axes. You cannot be your own second opinion.
- Never present a pack's coverage as a level claim about a learner.
