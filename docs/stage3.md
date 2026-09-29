# Stage 3 — Knowledge, Evidence, and Learner Model

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)

Previous: [Stage 2 — Language Packs, Onboarding, and Prior-Course Audit](stage2.md)
Next: [Stage 4 — Session Engine and First Polish Vertical](stage4.md)

## Outcome

Implement the evidence-backed learner model. At the end of this stage, LinguaWiki stores a language-agnostic knowledge graph, attempts and atomic evidence, recurring errors, reviewable proficiency estimates, and bounded context bundles. Recognition can never masquerade as spontaneous production.

## Estimate

- Engineering: 8–12 days.
- Rubric/seed refinement: 2–4 days.

## Entry criteria

- Stage 2 exit gate passes.
- A Polish pilot track and at least one synthetic track are initialized.
- Pack content has stable IDs, descriptors, tasks, provenance, and review metadata.

## Work packages

### 3.1 Implement the knowledge graph

- [x] Add migrations/repositories for knowledge items, aliases, relations, examples, tags, and track item state.
- [x] Support kinds without assuming inflection, case, alphabet, or whitespace tokenization.
- [x] Support prerequisite, form/sense, contrast, collocation, example, curriculum, and error-target relations.
- [x] Preserve pack-stable IDs across pack upgrades.
- [x] Add deterministic search by ID, alias, tag, level, and relation.
- [x] Implement `knowledge get|search|upsert|link|merge` with dry-run for merges.

### 3.2 Implement attempts and atomic evidence

- [x] Add attempt, evidence, observation, and follow-up migrations/repositories.
- [x] Capture modality, task type, target, normalized score, help level, correction mode, delay, evaluator, and confidence.
- [x] Keep recognition, comprehension, controlled production, spontaneous production, and delayed transfer distinct.
- [x] Preserve the learner response or privacy-compatible excerpt needed to justify evidence.
- [x] Validate that evidence cannot reference an incompatible task/modality.
- [x] Add immediate standalone evidence commands only for imports/repairs outside live sessions.

### 3.3 Implement the error model

- [x] Add error patterns, occurrences, successful counter-evidence, and lifecycle state.
- [x] Deduplicate through category + normalized signature + target item, with uncertain matches requiring review.
- [x] Implement observed, active, monitoring, resolved, and reactivated transitions.
- [x] Require controlled, novel, spontaneous, and delayed success according to configurable policy.
- [x] Ensure one correct response cannot resolve an error.

### 3.4 Implement mastery aggregation

- [x] Implement the configured stage progression from unseen through stable.
- [x] Weight spontaneous and delayed evidence above recognition/immediate repetition.
- [x] Discount hints and low-confidence transcription.
- [x] Apply recency decay without deleting evidence.
- [x] Require evidence diversity before increasing confidence.
- [x] Allow failure to regress item stage and reactivate errors.
- [x] Version the aggregation algorithm and support recomputation from raw evidence.

### 3.5 Implement multidimensional skill estimates

- [x] Add current estimates and immutable estimate history.
- [x] Estimate every supported dimension independently.
- [x] Preserve `not-tested` separately from low performance.
- [x] Store numeric estimate, framework band/range, uncertainty, recency, evidence count, and algorithm version.
- [x] Explain every change using linked evidence and weighting factors.
- [x] Complete placement finalization using Stage 2 posterior state and Stage 3 evidence.

### 3.6 Implement bounded context bundles

- [x] Implement `context session|assessment|source|concept`.
- [x] Include only relevant profile/preferences, estimates, due work, active errors, recent evidence, curriculum/source position, and provenance.
- [x] Enforce configurable record/token-size limits.
- [x] Return omissions/counts so an agent knows context was bounded.
- [x] Never expose raw private transcripts/audio unless the command and consent explicitly require them.

### 3.7 Complete Codex skill slices

- [x] Make the umbrella skill resolve workspace/user/track and route by explicit intent.
- [x] Complete `linguawiki-init` against actual learner-model commands.
- [x] Complete `linguawiki-assess` for calibration/baseline finalization.
- [x] Add behavioral tests proving no mastery without evidence and no global-level overclaim.

## Verification

```bash
uv run pytest tests/unit/test_mastery.py tests/unit/test_errors.py tests/unit/test_estimates.py
uv run pytest tests/unit/test_evidence_claims.py tests/unit/test_language_agnostic_core.py
uv run pytest tests/integration/test_evidence_pipeline.py tests/integration/test_context_bundles.py
uv run pytest tests/integration/test_knowledge_graph.py tests/integration/test_cli_stage_three.py
uv run pytest tests/skills/test_learner_model_skills.py
uv run linguawiki evidence recompute --workspace <fixture> --dry-run --format json
uv run python scripts/validate_skills.py
uv run python scripts/verify.py
```

Use property tests for:

- [x] stage promotion/regression invariants;
- [x] idempotent recomputation;
- [x] error resolution/reactivation;
- [x] estimate uncertainty decreasing only with qualifying independent evidence;
- [x] no Polish/Chinese language-code branches in core aggregation.

## Exit gate

- [x] Recognition evidence cannot promote spontaneous production.
- [x] Delayed failure can regress stage and reactivate an error.
- [x] Every estimate is reproducible and traceable to evidence plus an algorithm version.
- [x] Unsupported modalities remain not-tested.
- [x] Knowledge graph works for both contrasting synthetic packs and Polish pilot content.
- [x] Context output is bounded, privacy-aware, and sufficient for an agent.
- [x] Init and assessment skills pass realistic behavioral tests.

## Handoff to Stage 4

Stage 4 receives a complete learner-state model and bounded planning context. It can now build and close real sessions without inventing progress rules inside the learning skill.

Three seams are already cut for it:

- `attempts.origin` accepts `session`, and `attempts` needs one added column to reference a
  session. `ALTER TABLE ADD COLUMN` works on a foreign-key-referenced table, so no table
  has to be recreated; register the relation in `integrity.ORPHAN_RELATIONS` or a named
  check, because a column written after insertion cannot carry a foreign key.
- `followups` exists with its due window and its link to an error and an attempt (work
  package 3.2 asked for it). Stage 4 adds the session link and the review-event side.
- `context session` is the planner's input. It already carries the profile, the estimates
  with their statuses, the live errors, the due work and due items, the curriculum
  position, and the pack provenance -- bounded, with `omissions` listing what did not fit.

What Stage 4 must not do is compute progress itself. `evidence record` is the only way an
observation enters the model, and the stage that follows from it is `mastery`'s decision.

## What was built, and the decisions that shaped it

The rules live in four pure modules, none of which touches a database or knows a language:

| Module | Owns | Version constant |
|---|---|---|
| `evidence.py` | what an observation may claim: modality, task type, help, delay | `STRENGTH_VERSION` |
| `mastery.py` | gates, claim ceilings, decay, diversity, regression | `AGGREGATION_VERSION` |
| `error_model.py` | error identity, uncertain matches, the resolution policy | `POLICY_VERSION` |
| `text.py` | the folding two things must agree on to be one thing | — |

Every version string is stored on the rows it produced, so a policy change can be told
apart from a data change and replayed over evidence that never moved.

### The claim, not the score, is what caps a stage

An attempt is what happened; an *evidence claim* is what it justifies. Separating them is
what makes "recognition can never promote spontaneous production" structural rather than a
convention: each claim declares the modalities and task types that can produce it, and the
highest stage it can ever justify. The gates are then applied, and the result is capped by
the strongest claim actually recorded. A gate whose weakest admitted claim could not justify
the stage it grants is refused by `assert_policy_is_sound`, so the rule cannot be
reconfigured away.

Three things are derived rather than accepted from the caller, because each is a place a
caller could otherwise talk its way into a promotion: the default claim (the *weakest* the
task supports), novelty (read from the item's own history), and the delay (`--retrieval
delayed` needs a real 12-hour gap).

### Failure has to be able to take a stage away

Positive and negative evidence are weighed per claim, and a claim whose most recent
observation is a failure stops supporting its ceiling regardless of older mass. That is what
lets a delayed failure pull an item off `stable`, which the exit gate requires. A failure at
a *weaker* claim while a stronger one still stands is contradictory rather than decisive: it
costs confidence, not the stage.

### `not-tested` is a third thing

A dimension nothing could test is neither a good result nor a bad one. `estimate_status`
carries the distinction — `not-tested`, `provisional`, `estimated` — because a single
confidence label conflated "we did not look" with "they cannot do it". A `db check` refuses
a row whose status contradicts its own evidence count.

### Two DuckDB limits shaped the schema

Both were confirmed by experiment rather than assumed, and
`tests/unit/test_duckdb_update_limits.py` now states them as executable facts so a
`duckdb` bump that changes either one fails at the reason rather than somewhere downstream:

- **Updating a foreign-key column on a row that is itself referenced fails.** `knowledge
  merge` has to repoint a duplicate item's attempts, evidence, and errors, so
  `target_content_id` is not a foreign key on those three tables; `integrity.ORPHAN_RELATIONS`
  carries each relation and `db check` reports it. A unique index over the same column has
  the same effect, which is why the "one claim per attempt per target" rule is a named check
  rather than an index.
- **A referenced row cannot be deleted in the transaction that repointed its children.** An
  error's identity is derived from its target, so a merge must re-derive it; the old pattern
  is therefore `superseded` and names its successor rather than being deleted. It matches how
  the rest of the repository treats history, and `db check` requires a superseded pattern to
  be empty and to name what it became.

Measuring them also narrowed a claim Stage 2 made. A multi-column `CHECK` does *not* by
itself force the delete-and-insert rewrite; the foreign key and the unique index do. The
Stage 2 decisions that cited it are unaffected — the columns in question are foreign keys
anyway — so the correction is recorded in the test, in `AGENTS.md`, and in the `db check`
docstring, and `0009_content_provenance.sql` keeps its original comment: editing a
migration changes its checksum, which would invalidate the `schema_migrations` rows in
both retained portable-export fixtures for the sake of a comment whose conclusion is right.

### Scope is a property of the track, not of the reference

A track names the pack it is taught from, and every level label, dimension, and item
identity is relative to that pack. So every item reference is resolved *within* the
track's own material -- its pack's content, its own notes, or content owned by neither --
and a reference reaching outside it is refused with whose it is rather than reported as
missing. A content ID is checked exactly as a stable key is: it is the form that looks
least like a guess, which is why it was the one that let a track record evidence against
another language's item.

The same rule holds for the assessment bank, and there it goes further: a bank task's
type, modality, dimension, difficulty and targets are facts the pack decided, so they are
read from the bank rather than taken from the caller, and a caller's value that the bank
contradicts is refused with the stored value named. Silently overriding would be worse --
the caller would believe the observation it described was the one recorded.

### Bundles report what they left out

Each context scope declares the sections it may carry, so relevance is a property of the
code: an `assessment` bundle holds no errors and no per-item evidence, because an assessor
that knows the learner's usual mistakes is scoring its own expectations. When a budget is
hit, whole sections are dropped and recorded in `omissions` with their counts — half a list
looks exactly like a short one, and an agent cannot tell the difference.

### What Stage 3 deliberately did not do

- **No new published contract.** The learner model travels through
  `linguawiki.cli.success.v1`, whose `data` is unconstrained by design. Freezing these
  shapes now would pin a model the session engine still has to extend; the *vocabularies*
  are what is fixed, each in one module with a version string.
- **No session origin.** `attempts.origin` lists `session` so Stage 4 needs no new
  constraint (DuckDB cannot drop a CHECK), but this stage cannot produce one: there is no
  session row to reference. Standalone recording is for imports and repairs, as the work
  package says.
- **No source progress.** `context source` reports what a pack recommends and the rights it
  ships with. Reading and listening position arrive with the source pipeline in Stage 5.

## What the seventh review changed

Seven defects, all reproducible through the public services and all invisible to a
single-track workspace or a single-context estimate. The fixes, and the rule each one
restored:

| Defect | Rule restored |
|---|---|
| Any content ID, stable key, or alias resolved for any track | An item reference is resolved inside the track's own material |
| A caller's account of a bank task overrode the bank's | The pack decides what its task demanded |
| An unhinted recognition counted as `controlled`, and any delayed claim as `spontaneous` | Both are terms about production |
| A transcription artifact activated a learner error | An artifact is on the record and counted against nobody |
| Repeating one context narrowed the interval | One context is one observation |
| A merge left an edge's derived identity describing an item it no longer touched | An identity is computed from the parts it describes |
| `--max-records` bounded each section independently | The limit is a promise about the bundle |

Three of them could persist in a database a command would now refuse -- through a restore,
or a build with the looser rule -- so each gained a named `db check`: `track_item_scope`,
`error_confirmation`, and `knowledge_relation_uniqueness`. The bank-authority, qualification,
uncertainty, and record-limit defects cannot survive as *state*: they are decisions made at
write time, and the regressions cover the refusals instead.

## What the eighth review changed

Two more, both about *provenance* rather than validation — the facts were being checked
against the wrong source:

- **Evidence re-read the mutable bank instead of the run's snapshot.**
  `assessment_run_tasks` has snapshotted the served facts since migration 0016, precisely
  because a pack is mutable and a run is not, and the code that wrote it says so. Reading
  `assessment_tasks` back let a later pack edit rewrite what somebody answered: a text
  short-response became an extended-productive writing task, and the default claim rose
  from recognition to controlled production for an observation nobody made. Run-backed
  tasks now resolve through `assessment_runs` and `assessment_run_tasks`; the bank is the
  account of last resort, for an observation outside any run or a task served before the
  snapshot existed, and the report names which account it used. Where the two disagree,
  the observation records what the learner faced and *warns* — drift detected fact by fact,
  not only by content hash, since a hash covers what the pack declared rather than what
  the row now holds.
- **A run was not bound to the learner.** Checking only that the run served the task
  established nothing about whose run it was, so one learner's sitting attached to
  another's evidence on the same pack. The run's own `track_id` must match.

Both could persist through a restore, so both gained a named check: `attempt_run_track`
and `attempt_served_facts`.

The lesson is narrower than the previous round's and worth stating on its own: **a
snapshot exists because its source is mutable, so reading the source instead defeats the
only reason it was taken.** The comment in `assessment.py` said "not re-read later"; the
evidence path re-read it anyway, and no test compared the two because every test used a
pack nobody had edited. The regressions now edit the bank between serving and recording.

## What the ninth review changed

Four defects adjacent to that fix, all of them the *boundaries* of the snapshot rather
than its centre:

- **An empty or unknown target list acted as a wildcard.** Validation ran only when the
  targets were non-empty, so a shipped task declaring none vouched for any item in the
  pack -- and a drifted task, whose targets were deliberately blanked, did the same.
  Migration 0022 snapshots the targets when a task is served, closing the last fact that
  still had to be read back from the bank; and unknown or empty targets now *refuse* an
  item-targeted observation, because absence of information is not permission. The task
  measured a dimension, and that is what can be recorded from it.
- **A caller's `difficulty` still won.** `_assert_task_facts` did not compare it, and the
  resolver preferred it. Difficulty is where an observation lands on the ability grid, so
  a caller's number moves the estimate to a level the task never tested. Now refused at
  the write, and the served number is used unconditionally.
- **Any partial snapshot was treated as pre-0016.** Migration 0016 added every snapshot
  column at once, so a legacy row has all of them null and a modern row has none: a mix is
  damage. Falling back on a partial row sent the resolver to the bank for facts the record
  actually held -- the same defect again, on a record that was almost intact. The rule is
  now all-or-none, including the content hash, and a mix is refused by name.
- **`attempt_served_facts` never established that the task was served.** Its inner joins
  answered "do the facts match" and silently passed an attempt whose run/task pair had no
  served row at all. Membership is now a separate check, `attempt_run_membership`; a null
  attempt difficulty against a served one counts as a mismatch rather than being skipped;
  and `served_snapshot_complete` reports a partial record.

The pattern across all four is one thing: **a validation that only runs when data is
present is not a validation.** Empty targets, a null snapshot column, a missing join row
and an absent difficulty each turned a check into a no-op precisely where the data was
least trustworthy. The regressions now assert the refusal for each *absent* case, not
only the mismatched one.

## What the tenth review changed

One defect, and it is the ninth review's own lesson applied to the write path but not to
the check beside it. The served-target comparison was a SQL predicate guarded by
`target_refs_json IS NOT NULL`, so:

- a restored attempt with an item target and no target snapshot passed, although the
  write path refuses that state more firmly than any other;
- a malformed snapshot passed while unused, and **aborted `db check`** with an
  `InvalidInputException` the moment a targeted attempt made `from_json` run. A diagnostic
  that crashes reports less than one that lies: the operator learns nothing at all.

Migration 0022 could not prevent the second, and the migration now says why: DuckDB
refuses `ALTER TABLE ... ADD COLUMN` with any constraint, so the `CHECK (json_valid(...))`
every other JSON column in this schema carries is impossible here, and a table this
widely referenced cannot be recreated to get one.

So the guarantee moved into the check, and out of SQL. `served_target_list` parses the
text in Python and returns `None` for anything that is not a JSON array of content
identifiers; `served_targets_wellformed` reports every malformed snapshot whether or not
an attempt uses it, and `attempt_served_targets` reports a targeted attempt whose
snapshot is null, unreadable, or does not contain it. The parser is tested directly,
because it is the thing standing between corruption and a crash.

The lesson is the ninth review's, one level out: **a check is code, and inherits every
weakness it was written to find.** The rule about absent data had just been added to
`AGENTS.md`; the SQL predicate implementing the previous fix broke it on the same day.
Where a check must read data the schema cannot constrain, it parses defensively and
reports -- it never trusts the shape and never raises.

## What the release gate measured

Running the two network gates after the tenth fix, the DuckDB gate failed -- not on
anything Stage 3 wrote. `db check` never ran: DuckDB 1.4.1 could not *open* the database
1.5.5 had written, dying at connect with `INTERNAL Error: Failed to load metadata pointer
(id 64, idx 3, ptr 216172782113783872)`, while 1.5.5 opened the same file. Reproduced
three times, then the identical command passed twice. Nothing in the tree had changed
between the failures and the passes.

That is the whole finding: the gate had been run in the direction DuckDB does not
guarantee. A newer release reads an older file; the reverse is not promised, and
`docs/stage1.md` had said so from the start (`1.5.5 -> 1.3.2` fails, "as it must"). The
standing command each round was `--to 1.4.1` -- a downgrade -- and its passes were luck.
The suite's own external test had it right all along (`--from 1.4.1 --to <pinned>`).

`scripts/check_duckdb_upgrade.py` now orders the two releases and branches:

- a **newer** candidate must still open the native database, re-export it, and restore
  both backups, compared per table and by stable identity;
- an **older** candidate is judged on the portable Parquet export, which it must restore
  to identical content -- and that is the real recovery path off a bad upgrade, so the
  downgrade run now tests something that holds instead of something that happens.

Verified after the change: `1.4.1 -> 1.5.5` green on the native path, `1.5.5 -> 1.4.1`
green twice on the portable path. `docs/stage2.md`'s record of a verified `1.5.5 -> 1.4.1`
is corrected there. The lesson joins the others in `AGENTS.md`: **a green run whose
outcome a rerun can reverse is worse than a red one**, because it gets quoted afterwards
as a guarantee.

Two lessons generalise beyond the individual fixes:

- **A single-track, single-context fixture cannot see a scoping defect.** The regressions
  for this round build a workspace with two tracks on two packs, because that is the
  smallest shape in which "which pack does this belong to" has an answer that can be wrong.
- **A derived identity has to be re-derived whenever its parts move.** That was already
  true of error patterns after the sixth review; it was equally true of relation edges and
  was missed because nothing compared an edge's identity with its own parts. `db check`
  now asserts the consequence -- one edge per (source, type, target) -- rather than the
  derivation, which is the form that catches a stale identity however it arose.
