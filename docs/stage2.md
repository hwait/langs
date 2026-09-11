# Stage 2 — Language Packs, Onboarding, and Prior-Course Audit

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)

Previous: [Stage 1 — Learner Workspace and Storage Foundation](stage1.md)
Next: [Stage 3 — Knowledge, Evidence, and Learner Model](stage3.md)

## Outcome

Make a new learner workspace useful at a declared level and capable of a bounded calibration. This stage delivers pack authoring/validation, a thin Polish A2 pilot slice, user/track setup, framework-safe onboarding, and generic prior-course audit without claiming that pilot content supports comprehensive placement.

## Estimate

- Engineering: 8–12 days.
- Polish pilot authoring/review: 5–10 days in parallel.

## Entry criteria

- Stage 1 exit gate passes.
- An independent empty `PolishLinguaWiki` fixture exists.
- Pack provenance/review and maturity policies from the parent plan are accepted.

## Work packages

### 2.1 Implement the pack contract

- [x] Validate `manifest.json`, capabilities, supported frameworks/levels, source policy, licenses, checksums, and bundle dependencies. (JSON rather than YAML; see the implementation notes.)
- [x] Validate knowledge seed, relation, example, assessment, prompt, and resource-bundle formats.
- [x] Implement pack maturity values: `fixture`, `pilot`, `onboarding-ready`, and `placement-ready`.
- [x] Implement content-addressed immutable pack versions.
- [x] Implement `pack scaffold|validate|install|diff|update|list|coverage|publish`, plus `pack stamp`.
- [x] Make reinstall idempotent and updates dry-run first.
- [x] Never overwrite learner-created content during a pack update.

### 2.2 Implement provenance and review tooling

- [x] Support origin classes: authentic-source, learner-produced, source-derived, AI-adapted, AI-generated, synthetic-media, and human-authored.
- [x] Track linguistic, pedagogical, source-alignment, rights, and privacy review independently, each in its own state vocabulary.
- [x] Implement content lifecycle through `approved-personal`, `verified`, `publication-ready`, `needs-review`, and terminal states.
- [x] Implement risk tiers 0–4 and promotion gates, as the elementwise maximum of the tier's and the lifecycle's requirements.
- [x] Implement prompt-template versioning, first-three-run full review, stable-template sampling, and batch quarantine.
- [x] Implement dependency-hash invalidation.
- [x] Add `pack author generate-draft|import|review-queue|review|approve|reject|invalidate`.
- [x] Add `pack template validate|stabilize|quarantine`.

### 2.3 Build the thin Polish A2 pilot slice

Created as `language-packs/pl-pilot`, version `0.1.0`, maturity `pilot`, band `A2`:

- [x] 72 reviewed A2 knowledge targets across five practical themes (87 reviewed in total, including the A1 prerequisites);
- [x] 16 grammar/construction targets;
- [x] 11 pronunciation/orthography targets;
- [x] 24 objective and short-response diagnostic items across the four receptive and form dimensions;
- [x] eight productive prompts with versioned rubrics, plus seven pronunciation tasks;
- [x] six activity templates across six modes;
- [x] one reviewed source recommendation per supported media modality.

Additional requirements:

- [x] Every persistent item has origin, review axes, rights/privacy, content hash, and stable ID.
- [x] AI drafts remain visibly unverified until their required reviews pass: three `ai-generated` drafts sit at lifecycle `draft`, are excluded from every count and from preparation, and are listed by `pack coverage` as unresolved.
- [x] The pack advertises only its covered themes/dimensions; an item using an undeclared theme, level, dimension, or modality fails validation.
- [x] It refuses to label itself onboarding-ready or placement-ready, in `tests/expectations.json` and by measured coverage.
- [x] Kept in core only as a temporary dogfood pack until the contract stabilizes.

### 2.4 Implement user and track setup

- [x] Implement `user create|show|update|list`.
- [x] Implement `track create|show|update|activate|pause|archive`.
- [x] Capture timezone, target/support languages, goals, schedule, interests, correction preferences, voice capability, retention consent, and prior materials, as a typed preference set.
- [x] Validate BCP-47 target language/script/region through the installed pack.
- [x] Require one pack-declared proficiency framework.
- [x] Reject level labels from another framework *by name*; nothing in the release relates two frameworks.

### 2.5 Implement declared-level onboarding

- [x] Implement resumable `onboard start|record|status|finalize` state, plus `onboard abandon`.
- [x] Store declared level as a low-confidence hypothesis: one `declared-hypothesis` estimate per dimension with zero evidence.
- [x] Resolve the selected band plus prerequisites recursively, through bundle dependencies.
- [x] Import reference items as `unseen`; never infer mastery.
- [x] Build a bounded calibration queue sampling prerequisites and declared-band targets separately.
- [x] Prepare a two-week plan within the pack's actual theme coverage, labelled `pilot curriculum`.
- [x] Clearly label unsupported skills and missing resources.

### 2.6 Implement placement state and bounded calibration

- [x] Persist per-dimension priors/posteriors, task exposure, content-family coverage, budgets, and stop reason; every scored task keeps the prior and posterior it was folded into.
- [x] Implement the ordinal Bayesian staircase and fixed v1 response curve defined in the parent plan.
- [x] Enforce minimum/maximum task budgets and the 80%/one-band stop criterion, together with the coverage and boundary-probe conditions.
- [x] Prevent item reuse within six months except longitudinal anchors.
- [x] Allow pause/resume and fatigue/requested stop.
- [x] Refuse comprehensive placement where the pack is not placement-ready, listing the unmet bank requirements; run only labelled pilot calibration.

### 2.7 Implement generic prior-course audit

- [x] Implement `curriculum import|show|position|audit-start|audit-record|audit-finalize`, plus `audit-report`.
- [x] Import only legally permissible outlines or user-authored objectives, enforced by `rights_status`.
- [x] Map course objectives to pack items/descriptors from *declared* mappings, and preserve unmapped gaps.
- [x] Convert claimed completed units to at most `encountered` state with self-report provenance.
- [x] Select a risk-weighted audit sample of central prerequisites, recent units, production targets, and older material.
- [x] Produce an evidence gap/calibration queue rather than granting mastery, and report what was never probed.

### 2.8 Add Codex skills

- [x] Implement and validate `linguawiki-pack`, with references for the pack format, the review axes, and the authoring workflow.
- [x] Implement the workspace/track/onboarding slice of `linguawiki-init`, with references for onboarding modes and the prior-course audit.
- [x] Add `linguawiki-assess` over the persisted placement state, with references for the algorithm and for scoring conditions; full evidence aggregation lands in Stage 3.
- [x] Ensure skills call CLI JSON contracts and never edit pack state or DuckDB directly.
- [x] Route through the umbrella `linguawiki` skill, which dispatches to exactly one specialist.

## Verification

```bash
uv run linguawiki pack validate language-packs/pl-pilot
uv run linguawiki pack coverage language-packs/pl-pilot --format json
uv run linguawiki pack stamp    language-packs/pl-pilot --check
uv run pytest tests/language_packs tests/unit/test_provenance.py tests/unit/test_placement.py \
  tests/integration/test_learners.py tests/integration/test_onboarding.py \
  tests/integration/test_assessment.py tests/integration/test_curriculum_audit.py \
  tests/integration/test_cli_stage_two.py
uv run python scripts/validate_skills.py
uv run python scripts/verify.py

# Release gates; both need uv and a package index.
uv run python scripts/verify.py --clean-environment
uv run python scripts/verify.py --duckdb-upgrade <candidate version>
```

`--clean-environment` now covers the whole Stage 2 release path: it installs the built wheel
into a fresh environment with no core source on the path, creates a workspace, installs
`pl-pilot` **by key** from the wheel, creates a learner and an A2 track, finalizes onboarding,
serves one calibration task, and requires `workspace doctor` and `db check` to pass. That is
what makes shipping `language-packs/` in the wheel a tested property rather than a hopeful one.

`--duckdb-upgrade` seeds all 56 tables of this schema through the real services and requires
the candidate DuckDB to open, re-export, and restore the result; verified 1.4.1 -> 1.5.5
preserving 2,543 rows, every content hash, and every stable identity.

> Corrected during Stage 3: this originally recorded the run as `1.5.5 -> 1.4.1`, a
> *downgrade*, and read its pass as verification. DuckDB guarantees only that a newer
> release reads an older file, and that run is a coin flip -- see the gate note in
> [stage3.md](stage3.md). The direction above is the one the gate now requires natively;
> a downgrade is judged on the portable export.

Also tested:

- [x] a pack whose directory holds an undeclared file, lacks a declared one, or whose bytes
      no longer match a declared checksum;
- [x] a pack recording a content address its own files do not produce, and one not yet
      published;
- [x] an item edited without re-stamping, a duplicate stable key, and a derived-ID collision;
- [x] an item claiming a lifecycle its reviews do not support, and one naming an
      origin or review profile the manifest does not declare;
- [x] origins that disagree about rights or privacy on one item;
- [x] a bundle disagreeing with the manifest about its dependencies, and one referencing an
      item that does not exist;
- [x] a reinstall of the same version, a same-version reinstall with different bytes, and an
      update attempted without a preview;
- [x] an update that changes an item, drops another, changes the pack's language, or reorders
      a framework's levels;
- [x] learner item state surviving a pack update of the items it points at;
- [x] a machine or AI reviewer attempting every state above its ceiling, on every axis;
- [x] a defective sample quarantining its batch, its template, an already-approved sibling,
      and the dependents of both;
- [x] a template stabilized too early, stabilized with a defect in its history, and
      quarantined before drafting again;
- [x] publication-ready AI content reviewed twice by the same identity;
- [x] a level label from another installed framework, from no framework, and a framework the
      pack does not declare;
- [x] a script the pack does not declare, a language no pack serves, and a support language
      the pack lacks;
- [x] archiving the primary track, and resolving a track when none is active;
- [x] onboarding started twice, replayed by idempotency key, abandoned, and finalized twice;
- [x] placement refused on a pilot pack and on a fixture pack, through the service and the
      CLI;
- [x] an item budget truncating a plan, an avoided topic excluding items, and a level the
      pack does not cover;
- [x] every dimension untestable for want of a modality, and one untestable for want of a
      reviewed task;
- [x] a task recorded that was never served, recorded twice, and recorded after finalization;
- [x] an item inside and outside the six-month reuse window;
- [x] an outline with unmanageable rights, a half-declared mapping, a mapping to an item the
      pack lacks, duplicate units, and a self-parented unit;
- [x] a unit claimed both complete and current, and an audit started before anything was
      claimed;
- [x] an audit finalized with a target it never probed;
- [x] `--input` payloads from a file and from stdin, and an unparsable one.

## Exit gate

- [x] Polish CEFR A2 track initializes inside `PolishLinguaWiki` with Russian/English support —
      `tests/integration/test_onboarding.py::test_the_stage_exit_gate_a_polish_a2_track_initializes_with_ru_en_support`.
- [x] Declared A2 loads bounded prerequisites/resources without marking knowledge mastered —
      the plan resolves `cefr-a2-core` plus `cefr-a1-core` and nothing else, every imported
      item is `unseen`/`reference-import`, and every seeded estimate is
      `declared-hypothesis` with zero evidence.
- [x] Pilot calibration pauses/resumes and reports uncertainty and unsupported dimensions —
      `pause` keeps every posterior, budget, family record, and boundary-probe flag, and a
      dimension the pack or the learner's equipment cannot serve is `not-tested` with its
      reason.
- [x] Comprehensive placement refuses insufficient bank coverage, and the refusal lists the
      unmet requirements per band and dimension.
- [x] Prior-course completion creates only `encountered` items plus an audit queue, and the
      audit reports what it never probed.
- [x] Every pilot item passes its risk-tier metadata gate — `pack validate` refuses a pack
      whose declared lifecycle its own review states do not support.
- [x] A defective sampled AI batch quarantines its template and dependents.
- [x] Pack reinstall is idempotent and update preview is deterministic.
- [x] The whole path works from the released artifact, with no core source on the path —
      `scripts/check_clean_environment.py`.

## Shared invariants

Stage 1 recorded four properties that had to live in exactly one place each. Stage 2 adds
three, for the same reason: each was a question two commands could answer differently.

| Invariant | Home | Question it answers |
|---|---|---|
| Promotion gate | `provenance.py` (`gate_problems`, `required_strengths`, `state_strength`) | Is this content reviewed enough to claim the lifecycle it declares? Each axis has its own ordered state vocabulary, and the requirement is the elementwise maximum of the risk tier's row and the lifecycle's row. `content_reviews` projects the result onto the frozen `lingua.content.v1` gate, and a test asserts the projection never accepts what v1 would reject. |
| Pack identity and integrity | `packs/format.py` (`load_pack`, `content_id_for`, `pack_content_address`) | Are these the bytes the manifest declares, and is every identity derived rather than asserted? Checksum coverage is *total*: an undeclared file present on disk fails, and a declared file missing from disk fails. |
| Placement stop rule | `placement.py` (`stop_decision`, `confidence_label`) | May this dimension stop, and how much confidence has it earned? All four conditions must hold — budget, coverage, precision, boundary probe — and any other stop is labelled by its reason rather than reported as a precision. |

## Implementation notes

Recorded while completing this stage; Stage 3 inherits these decisions.

- **Pack files are JSON and JSONL, not YAML.** The plan sketches `manifest.yaml`; a YAML
  parser would become a *runtime* dependency of the core, whose whole published contract
  surface, fixtures, and workspace lock are already JSON. The layout, file roles, and field
  names are the plan's. Seeds stay line-delimited so a diff shows one item per line.
- **An item's content hash is derived, never authored.** `pack stamp` computes it and
  `pack validate` enforces it, so editing an item without re-stamping *fails* rather than
  silently re-binding that item's reviews to text nobody reviewed. `pack stamp --check` is
  the form to use when reviewing someone else's pack, and the gate runs it over every
  shipped pack.
- **Content identity is derived from `(pack_key, kind, stable_key)`.** Nothing in a pack file
  asserts an ID. A reinstall therefore reproduces every identity, which is what makes learner
  annotations survive a pack upgrade — and what makes the idempotency test meaningful rather
  than tautological.
- **Pack maturity is measured, not declared.** `pack publish` refuses a maturity the measured
  coverage does not support and restores the manifest it was about to stamp. The Polish pilot
  pack passes `pilot` and fails `onboarding-ready` and `placement-ready`, which is the point
  of shipping it.
- **The per-axis review vocabularies are deliberately different.** `cleared` settles
  redistribution and says nothing about whether a pronunciation contrast is real, so the axes
  do not share a state list. Comparing state *names* across axes was the first design and it
  made `machine-checked` on rights look like progress; the ordinal strength per axis replaced
  it. `is_known_state` had to learn that `restricted` and `not-applicable` are *known* states
  as well: without them, `restricted` rights reported as an unknown state rather than the
  refusal it is.
- **`coverage` is the wider of the family and task-type counts, not their union.** A union
  treats one content family plus one task type as two kinds of coverage, which every
  single-family run satisfies for free; the stop rule then fired after six answers from one
  theme. The rule is that evidence spans two content families, *or* two task types.
- **DuckDB cannot update a row another table references, if the update touches a
  unique-indexed or foreign-key column.** This
  shaped four schema decisions rather than being worked around in the services:
  `language_packs` is immutable identity with `pack_installations` holding everything an
  update changes; `content_records` enforces "a pack item or a track item, never both" in
  `db check` instead of as a table CHECK; `onboarding_runs.assessment_run_id` and
  `resource_plan_id` are plain columns validated by the orphan check; and
  `curriculum_units.parent_unit_id` is not a foreign key because a self-referencing one makes
  a bulk portable restore fail whenever a child row precedes its parent. Each omission is
  paired with an entry in `integrity.ORPHAN_RELATIONS` or a named check, so nothing is merely
  unenforced.
- **`db check` is evaluated at the schema version the database claims.** It compared the
  tables present against this release's head, so restoring a schema-7 export — a legitimate
  recovery — reported eleven missing tables and failed. The expectation now comes from
  `tables_at_schema_version(applied)`, each domain check runs only where its tables exist, and
  `restore` passes `allow_behind_head=True` so "behind this release" is a warning telling the
  learner to migrate rather than a corruption report.
- **A command reports after its writer closes, or through the writer's own connection.**
  DuckDB serves one connection per database file, so a command that wrote and then called its
  own `report()` deadlocked on its own lock. Every report now has a `(database, id)` form used
  inside a writer and a thin `(paths, ...)` wrapper used outside — `assessment._run_report`,
  `onboarding._status`, `curriculum._audit_state`, `resources.plan_state`.
- **`ON CONFLICT DO NOTHING` after a delete in the same transaction reports a duplicate that
  no longer exists.** DuckDB still sees the deleted row's entry in a *secondary* unique index.
  Pack relations are therefore deleted and plainly re-inserted, and `knowledge_relations` has
  no unique index over the edge's parts: `relation_id` is derived from them, so the primary key
  already states the rule.
- **A drafting batch fixes its inspection duty before any output exists.** `generate-draft`
  records the sampling policy and the required sample when the batch opens, so the duty cannot
  be renegotiated once the output looks convincing. Nothing is generated by the core: it holds
  no AI adapter, and `import` records whatever the agent produced as `draft`.
- **The review queue reports the gate for the promotion an item is heading for, not the one it
  already holds.** A draft satisfies `draft` trivially, so the first version of the queue
  reported "gate satisfied" for every unreviewed item — useless in a review queue. Entries are
  measured against `approved-personal`.
- **A single track resolves whatever its status.** `resolve_track` preferred the only *active*
  track, so a paused track could not be reactivated without quoting its identifier — the one
  moment a learner is least likely to have it.
- **The pilot pack's grammar and pronunciation items sit at risk tier 2, not 3.** Tier 3
  requires `reference-verified` linguistic *and* `teacher-verified` pedagogical review; the
  pack author is not a second, independent teacher, and claiming otherwise would be exactly
  the overreach the review axes exist to prevent. The tier-3 and tier-4 gates are exercised by
  tests instead, where the reviewer identities can be stated honestly.
- **The retained portable-export fixtures are now versioned as a set.**
  `tests/fixtures/portable-export/schema-7/` and `schema-18/` both have to restore; only the
  one matching this release's head is compared against the current export shape. The older
  fixture pins a shape this release no longer produces, which is precisely what makes it worth
  keeping.
- **The DuckDB upgrade seed drives the real services.** A hand-written seed drifts from the
  schema the moment a service changes how it writes, and an upgrade test on stale rows proves
  nothing about the data this release produces. `scripts/duckdb_upgrade_support.py` installs
  the pilot pack, creates a learner and a track, onboards, calibrates, imports and audits a
  course, and runs an authoring batch; only `jobs` is still filled directly, because no Stage 2
  workflow writes it.
- **`language-packs/` ships in the wheel.** A learner workspace holds no core source, so a pack
  installed by key has to travel with the release. `scripts/check_distribution.py` asserts the
  manifest and a seed file are both present — without the seed the directory would resolve and
  the install would then fail on absent declared files.
- **Pack authoring works on the workspace database; releases come from the directory.** The
  files are what ships and what `pack publish` content-addresses; the database is where
  drafting, review, and quarantine happen. Nothing promotes a database draft into a pack file
  automatically.

## Review fixes

An external review of the first Stage 2 increment returned two blockers and seven major
findings. All nine are fixed, and each carries a regression test that fails if the reasoning
is undone.

- **A pack item's hash covers everything the pack asserts about it.** The first version
  hashed a narrow projection — a title and a body — so an edit to a task's rubric, a
  descriptor's level, a bundle's item list, or a recommendation's rights left the hash
  unchanged. Reviews then re-bound to text nobody had reviewed. `pack_item_hash` now hashes
  the complete canonical payload plus origins and dependencies, excluding only the lifecycle
  and review metadata that must be free to move without re-stamping. The frozen
  `lingua.content.v1` projection keeps its own, deliberately narrower, ADR 0005 hash;
  `tests/language_packs/test_pack_item_hash.py` mutates every field of every item kind and
  requires the hash to move.
- **A generation batch takes its items once, and a discharged duty is recorded once.**
  `generate-draft` now records `planned_items`, `import` refuses a batch that is not `draft`
  and refuses more items than the batch was opened for, and a fully inspected defect-free
  batch becomes `accepted` while incrementing `prompt_templates.inspected_runs` exactly once
  — the status transition *is* the record that the run was counted. Nothing left `sampling`
  before, so `inspected_runs` stayed at zero and `pack template stabilize` was unreachable
  through the supported commands; a test had been masking that with direct SQL.
- **Bundle dependencies must be a DAG.** Rejecting only self-dependency let `a -> b -> a`
  through, and the preparation resolver — which deepens a bundle every time it is reached —
  never emptied its frontier. The manifest contract now finds the cycle and names the path,
  and the resolver keeps a depth bound so it does not rely on validation having run.
- **A track names the pack it was created from.** The binding was re-derived on every read by
  matching a pack's language tag against the track's target language, so a second pack for the
  same language silently re-pointed an existing track at material its history was never built
  from. Migration `0015_track_pack_binding` adds `learning_tracks.pack_id`, checked through
  `integrity.ORPHAN_RELATIONS` because DuckDB's `ALTER TABLE` cannot add a foreign key.
- **Serving a task exposes it; answering it is a separate fact.** The exposure was recorded
  only when a score arrived, so a served-but-unanswered item stayed eligible for reuse and a
  retake could measure recall of a task the learner had already seen. `next_task` now writes
  the exposure in the same transaction as the served row, and
  `assessment_item_exposures.exposure_count` / `answered_count` count the two events apart.
- **The boundary-probe exemption belongs to the estimate.** It was read off the last served
  task's own level label, so one A1-labelled task excused the probe for an estimate sitting
  mid-framework — where a probe carries the most information. `at_framework_edge` now derives
  it from the updated posterior.
- **A closed run stays closed.** Both assessment runs and onboarding runs now have an explicit
  allowed-transition table, and both terminal states are terminal: an abandoned run cannot be
  resumed, and cannot be finalized into an estimate built from evidence the learner walked away
  from.
- **A removal's dependents are reported.** Dependent discovery ran over changed items only, so
  the change most likely to break something a learner built — the item disappearing entirely —
  named nothing. Removals now take part in discovery and carry their dependents into the
  preview.
- **An identical reinstall verifies and returns.** It used to rewrite every content record on
  the way to reporting "identical", and rewriting is not free of consequence: it re-binds
  reviews and touches `updated_at` on rows learner state points at. The early return requires
  the version, the content address, the item set, the `pack_files` digests, and the recorded
  source path all to match; the framework level-order check moved out of registration so every
  path still performs it.

## Second review round

A second review returned six findings. Five were real defects and are fixed below; one
rested on a premise the repository does not have, and is recorded here with the evidence.

- **A score belongs to the task the learner saw.** A run recorded the pack version it
  started under, but selection *and* scoring queried the currently installed pack. Editing
  a task's difficulty mid-run therefore folded the new difficulty into a posterior built
  from the old one, so the estimate described a task nobody was shown. Migration 0016 adds
  the served facts — task type, level, difficulty, family, modality, anchor flag, rubric
  version, and content hash — to `assessment_run_tasks`; `next_task` writes them when it
  serves, and `record` scores from them. The run also pins the pack's content address, and
  selection refuses with `assessment_pack_drifted` once the installed pack differs: two
  banks in one run is two runs wearing one estimate.
- **The inspection duty never shrinks to fit the import.** `import` recomputed
  `required_sample` from the subset actually imported, so a batch opened for forty items
  with a duty of forty became a duty of two after importing the two convincing outputs.
  `required_sample` is now absent from that statement entirely. A batch holding fewer items
  than it was opened for can never discharge its duty, so it can never be accepted; the
  report says so, and `acceptable` carries it as a field rather than only as prose.
- **No track is left without a pack.** 0015 added a nullable column, which left any track
  created before it with no pack at all. 0016 fills in every track whose target language has
  exactly one installed pack — the only unambiguous case — and the new named
  `track_pack_binding` check in `db check` fails for whatever is left, because choosing
  between two packs that might have taught a learner is not a repair a migration may make.
- **A paused run is not an open run.** `next_task` and `record` tested whether the status had
  any outgoing transition, and `paused` can become `in-progress`, so a paused run served and
  scored tasks without ever being resumed. Both now require `in-progress` and say
  `assessment_run_paused`; `RUN_TRANSITIONS` is back to answering only what a status may
  *become*.
- **Framework level membership belongs to the pack that declared it.**
  `proficiency_framework_levels` is keyed by framework alone and only ever grows, because an
  agreed level order cannot be renegotiated. A pack narrowing its range from A1–C2 to A1–C1
  therefore left C2 standing and a track could still be created at it. 0016 adds
  `pack_framework_levels`, install replaces the pack's rows wholesale, and both track
  validation and `TrackRecord.framework_levels` read through the pack. The global table keeps
  shared framework *identity* only.

### Not a defect: the migration set is unreleased

The review reported that migrations 0009 and 0012 were edited in place and their recorded
checksums changed, and asked for them to be restored byte-for-byte with new migrations added
instead. That rule is right, but it did not yet apply: `ff7629a` — the only commit in this
repository — ships no `src/linguawiki/db/` directory at all, so **every** migration 0001–0016
is new in this uncommitted working tree, and
`tests/migrations/snapshots/released-migrations.json` is itself a new, untracked file. No
release carries 0009 or 0012, so no database can be at schema 9–16 and there is no previous
checksum to preserve. Restoring them would have preserved nothing and left the schema history
carrying an add-then-amend pair for no reason.

What this release does establish is the baseline: the snapshot now records 0001–0016 as the
set Stage 2 ships, and from this commit onward `test_released_migration_files_are_immutable`
makes editing any of them a test failure. Everything in this review round was therefore added
as migration 0016 rather than folded into an existing file — including the corrections to
tables 0012 and 0015 introduced, which is the discipline the finding asked for.

## Third review round

Two findings, both consequences of scoping framework levels to the pack in the previous
round. Scoping membership was right; what was missing was that a *run* and a *track* both
hold levels of their own, and neither may be restated underneath them.

- **A scored run keeps the scale it was opened on.** `estimated_level` turns a posterior
  median into a band by index, so the ordered level list is part of the run's scale, not a
  lookup to be refreshed. Four sites read the track's current list instead, so narrowing a
  pack from A1-C2 to A1-B2 restated an already-scored run's C1 as B2 -- while the report
  still named the pack version the run had actually used. `start` now pins
  `framework_levels` into `conditions_json`, and `_pinned_levels` is what serving, scoring,
  finalizing, and reporting all read. The run's bands, credible intervals, and pack version
  are now internally consistent for ever.
- **Withdrawing a level a track uses is refused, not absorbed.** A pack update replaced
  level membership without looking at the tracks bound to it, so a track could keep
  `declared_level='C2'` in a pack that had stopped teaching C2 -- and the next calibration
  then dropped the declared prior on the floor and started from a broad one without saying
  so. `pack diff` now reports `level_conflicts` naming each track, field, and level, and
  `pack install` refuses with `pack_levels_in_use` until those tracks are changed to levels
  the new pack teaches. A `--dry-run` preview still reports rather than refusing, because
  showing the conflict is the whole point of a preview. Independently, the broad-prior
  fallback now warns whenever a declared level is not one the pack teaches, so the silent
  case cannot return by another route.

## Fourth review round

Two findings, both about the framework binding rather than the levels inside it.

- **A framework is a more fundamental binding than a level.** The conflict guard examined
  `declared_level`, `current_level`, and `target_level`, so a track with all three unset --
  a learner who has declared nothing yet -- had no level to conflict, and an update could
  withdraw its entire framework unopposed. The track then recorded a framework its own pack
  no longer declares, and the next run fell back to the framework's *global* level list, an
  order no installed pack vouches for. `pack diff` now reports `framework_conflicts`
  independently of any level field, `pack install` refuses with `pack_frameworks_in_use`,
  and the new named `track_framework_binding` check in `db check` catches the state however
  it arrived.
- **A framework ID names one scale.** Registration read an existing framework's version and
  never compared it, so a pack could update `cefr` from 2020 to 2024: the manifest reported
  2024, `proficiency_frameworks` stayed 2020, and every level label already recorded against
  that ID silently changed meaning. `_assert_framework_levels` now checks name, version, and
  source alongside the level order and refuses with `framework_identity_conflict`. A changed
  scale belongs under a new framework ID, which is the same rule the level *order* has
  followed since the first round -- a track records only the ID, so the ID has to mean one
  thing for ever. Republishing an unchanged framework is unaffected.

## Fifth review round

One finding, and the sharpest one: the refusal added in the fourth round named a
remediation nobody could perform. Creating the replacement track first failed because the
installed pack did not declare the new framework yet; archiving the old track did not clear
the refusal; the replacement was then rejected as a duplicate, because archived tracks held
the language slot; and `track update` cannot change a framework. Once a track used a
framework, the workspace could never adopt a replacement.

Moving a track between frameworks stays forbidden -- every level label a track records is a
label of *that* framework, and core never relates two frameworks -- so the fix is to make
the archive-and-replace path actually work.

- **An archived track is history, not a programme.** It is exempt from the framework and
  level conflict guards and from the `track_framework_binding` check, because it is no
  longer taught and the labels it keeps stay interpretable through the global framework
  record, which only ever grows. That is what the global table is for.
- **An archived track no longer holds the language slot.** Migration 0017 drops the
  `learning_tracks_target` unique index, which counted archived tracks. Uniqueness now
  means "at most one track a learner is still taught in, per language, region, and script",
  enforced in `create_track` and by the named `active_track_uniqueness` check -- DuckDB has
  no partial unique index, so this is the same treatment `is_primary` already had.
- **The refusal now describes the workflow that exists**: archive those tracks, apply the
  update, then create their replacements against a framework this version declares. An
  end-to-end test walks all five steps and then asserts the workspace is healthy, the new
  track calibrates against the new bank, and the archived track still reads back with its
  own framework and declared level.

Found while building the fixture for that test: a resource bundle naming a framework the
manifest does not declare raised a bare `StopIteration` from inside the install
transaction. Proficiency files were already checked for this; bundles now are too, and
`_install_bundles` looks the framework up by key instead of scanning for it.

## Sixth review round

Two findings, both about state that a pack update leaves behind rather than about what it
refuses.

- **A framework-scoped record may not keep its identity while changing framework.** The
  descriptor, bundle, and assessment-definition upserts deliberately never write
  `framework_id`: DuckDB rewrites an update of a foreign-key column as a delete and an
  insert, which referencing rows refuse. So renaming a pack's framework left all three
  tables pointing at the old framework while `pack_frameworks` named the new one, and
  nothing noticed because serving does not read those columns. Since the stable keys
  already carry the framework by convention -- `cefr.a1.reading.en`, `cefr-a1-core` -- a
  record that changes framework should change identity, and then it is an ordinary
  added-and-removed pair with the old row deprecated and learner state preserved. Keeping
  the identity is the authoring error, so `pack install` refuses it with
  `pack_framework_rebinding` and `pack diff` reports every affected record. The named
  `pack_framework_scoping` check in `db check` covers all three tables and sees the state
  however it arrived.
- **Reviving an archived track has to re-earn what archiving let go of.** Archiving frees
  the language slot and exempts a track from the pack's framework and level guards, so
  `set_track_status` could reactivate a predecessor straight into a state `db check`
  rejects -- two live tracks for one language -- or into a framework or level its pack no
  longer declares. An archived to active/paused transition now validates the language
  slot, the pack's framework membership, and every populated level field before it applies.
  Archiving itself is never blocked, because archiving is the remediation.

Two adjacent defects surfaced while building the fixture for the first finding, and are
fixed here:

- **A withdrawn assessment form was indistinguishable from a live one.** Descriptors and
  bundles are content records, so a dropped one is marked `deprecated`; a definition is
  not, and had no marker at all. Migration 0018 adds `assessment_definitions.status`, and
  install marks the definitions a pack has stopped shipping `superseded`.
- **Re-versioning a form left it with no tasks.** `assessment_tasks.definition_id` is a
  foreign key and was excluded from its upsert for the same DuckDB reason, so every task
  stayed filed under the old form while the new one was empty. The installer now deletes
  and reinserts a task whose form changed, which actually moves it; a task a learner has
  already been served or scored on cannot be moved -- the row their run points at would
  have to be rewritten -- so that case is refused with `pack_task_reform_blocked` and
  wants a new stable key.

## Handoff to Stage 3

Stage 3 receives an initialized learner/track, installed pilot pack, curriculum position, calibration tasks, provenance-aware durable content, and persisted assessment state. It adds the evidence and mastery machinery that turns performance into explainable progress.
