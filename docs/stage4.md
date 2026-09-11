# Stage 4 — Session Engine and First Polish Vertical

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)
Previous: [Stage 3](stage3.md) · Next: [Stage 5](stage5.md)

## Outcome

A learner can request a lesson, work through it, safely record observations in batches, close or recover the session, and see the resulting progress. This is the first genuinely usable Polish vertical and the point where dogfooding begins.

## Estimate

- Engineering: 8–12 days
- Pilot use and content refinement: 2–5 days

## Entry criteria

- Stage 3 exit gate passes.
- The thin Polish A2 pack is installed in an independent learner-workspace fixture.
- Evidence, error, estimate, and context APIs are stable enough for the planner.

## Work packages

### 4.1 Session lifecycle

- [x] Add session, block, activity, staged-event, close-result, and follow-up migrations.
- [x] Implement `planned -> active -> closing -> completed|partial|abandoned` transitions.
- [x] Require idempotency keys on mutations and reject invalid transitions.
- [x] Record timestamps in UTC while preserving the learner's IANA timezone for planning and display.

### 4.2 Planner

- [x] Accept available minutes, requested mode, energy/fatigue, resource preferences, and optional learner intent.
- [ ] Score due review, weak dimensions, curriculum continuity, recent modality balance, goals, and source continuity. *(Source continuity is not scored: see [Deferred, and why](#deferred-and-why).)*
- [x] Enforce duration, novelty, review, modality, and fatigue constraints.
- [x] Produce an explanation for every selected block and omitted high-priority candidate.
- [x] Validate plans at 40, 60, 80, 100, and 120 minutes.

### 4.3 Durable staging and atomic close

- [x] Implement `session log --input events.json` for bounded batches at block boundaries.
- [x] Give every batch a session ID, sequence number, content hash, and idempotency key.
- [x] Store staged events immediately; do not materialize mastery mid-session.
- [ ] Make close one transaction that validates and materializes attempts, evidence, errors, source progress, follow-ups, review tasks, and projection dirtiness. *(Source progress and review tasks are not materialized: see [Deferred, and why](#deferred-and-why).)*
- [x] Implement `resume`, reviewed `partial-close`, and `abandon`; preserve an audit trail in all cases.
- [x] Make duplicate batches and repeated close calls safe no-ops returning the original result.

### 4.4 Session-package ingestion

- [x] Validate `lingua.session.v1` packages before staging their events.
- [x] Deduplicate by package hash and retain source-layer references.
- [x] Refuse unsupported schema versions with a migration-oriented error.
- [x] Keep transcript evidence separate from audio-only evidence.

### 4.5 Learning skill

- [x] Implement the first complete `linguawiki-learn` skill.
- [x] Add focused references for correction policy, grammar/vocabulary, reading/listening, writing/translation, and session recovery.
- [x] Make the skill request bounded context, present the plan, teach, flush at block boundaries, and explicitly close.
- [x] Keep orchestration in the skill and all state transitions/calculations in Python.

### 4.6 Minimal learner projection

- [x] Render today's plan, latest session, due review, active errors, and skill estimates from DuckDB.
- [x] Mark generated pages and store projection hashes.
- [x] Do not accept direct edits as authoritative state.

### 4.7 Start real dogfooding

- [x] Generate a private `PolishLinguaWiki` from the Stage 1 template.
- [x] Pin the current core, schema, skill, and Polish-pack versions.
- [x] Run declared CEFR A2 onboarding and limited calibration.
- [x] Complete and document at least three sessions of different durations/modes.
- [x] Log defects in core or pack authoring rather than patching the learner database manually.

## Required commands

```text
linguawiki plan create|show
linguawiki session start|status|log|resume|close|partial-close|abandon
linguawiki session ingest-package
linguawiki wiki build --view dashboard
```

Every mutating command supports machine-readable JSON, stable error codes, and idempotency keys.

Two spellings differ from the plan's own §10 command list, deliberately:

- planning is `plan create|show`, as this stage doc names it, rather than `session plan`.
  One command with two names would be one command whose two names drift apart.
- `session staged` and `session recover` are implemented as this stage's checklist
  requires (`resume`, reviewed `partial-close`, `abandon`, and recovery of staged work);
  `session-package validate|inspect|export|purge` belongs to `linguawiki-speak` in Stage 5,
  and this stage ships only the `session ingest-package` half of it.

## What the eleventh review changed

Ten defects, seven of them reproduced against a fresh workspace before anything was
fixed. Four are worth reading as lessons rather than as entries.

**Retention has to happen where the text arrives.** The staged payload held the learner's
full response even on a track that had explicitly refused transcript retention, and it
kept holding it after the close, because a staged row is deliberately never edited: it is
the audit trail of what the close was given. Applying the rule only when the *attempt*
was written protected the attempt and nothing else. Worse, the close then refused the
payload it had been handed -- so the text was retained *and* the session was stuck in
`closing`. Retention now runs once, at the boundary where the text first appears, using
the same `evidence.retain_response` an attempt uses, and a caller asking for more than
consent allows is refused at flush time with the session still runnable. The package half
of the same defect was an ordering mistake: a producer's free-form `details` were merged
*after* the filter, so a file could set its own `response_visibility`. Details are now
merged first and can never reach a protected field, and a skill flush cannot claim
package provenance either.

**A timestamp is two facts.** Staged events recorded only when they were flushed, and the
close used that as the observation time -- so an event dated 20 December produced an
attempt dated 1 January. Everything derived from chronology was wrong with it: the delay
class, whether a second attempt on an item was a repeat, when an error was last seen,
which estimate snapshot the observation fell into. `session_staged_events.occurred_at` is
now stored beside `created_at`, ordering, materialization, and recovery all use it, and
`db check` compares the attempt with the event it came from.

**Resolve dependent work as you go, not all at once.** Every staged event was planned
before any was written, so two corrections of the same pattern both believed they were
the first: a duplicate-key failure where the pattern was new, and a silently wrong
occurrence count where it was not. Each event is now resolved *inside* the close's
transaction, in the order it happened, so the second sees the first. The related half:
a correction recorded the *pattern* as what it materialized, which made two occurrences
look like one row credited twice; it now records the occurrence, and the kind vocabulary
says `error-occurrence` so the distinction is in the schema rather than in a comment.

**A refusal has to leave a way forward.** The batch-gap check refused the partial close
in the same breath as recommending it -- the message said "close as partial once its work
has been reviewed", and that command hit the same check. A completed close still refuses,
because crediting a session as whole while a block's observations are missing is the
thing worth refusing; a partial close now credits what arrived and warns about what did
not. Two more of the same shape: a plan the learner never showed up for could not be
abandoned (the lifecycle allowed it and a schema CHECK refused it, with a raw
`ConstraintException`), and a reused idempotency key silently answered a 100-minute
grammar request with the 60-minute mixed plan it had made earlier. Plans are now
fingerprinted by their request, so a reused key with different content is an
`idempotency_conflict` -- the same treatment a reused batch key already got.

Revalidating staged payloads through their own contracts before materialization fixed the
crash a due-window follow-up caused (`'str' object has no attribute 'tzinfo'`, session
left in `closing`) and surfaced a latent contract bug behind it: `require_utc` on an
optional field never ran while the field was omitted, and raised `AttributeError` the
moment the same payload came back with an explicit `null`. That is now
`require_utc_if_set`.

Four new named checks carry what the write path now refuses:
`staged_event_materialized_target`, `session_attempt_occurrence_time`,
`session_activity_block`, and `session_package_track`.

## What the twelfth review changed

Three defects, and all three were the same mistake in different clothes: **a boundary
accepted something it could not honour later.**

**An observation delivered twice was two observations.** A staged row's primary key is
minted here, so nothing recorded *who the producer said this was* -- and every delivery
of one event became a new row. Three ways in: a package repeating an event inside itself,
a provider's `checkpoint` export overlapping the `completed` export of the same call
(which is how such providers normally work, not an exotic case), and a skill re-sending an
event under a new batch key. `session_staged_events.source_event_id` now carries the
producer's own identifier, with a unique index per session and a named refusal in front
of it. The external-session scope needs more than an index -- two exports of one call can
land on two LinguaWiki sessions -- so ingestion looks the identity up across that external
session's packages, and `external_event_uniqueness` reports the same thing over restored
data. The refusal says what to do: ingest the completed export into the session that
holds the checkpoint, or export only the events after it.

**A "validated" package could contain events that could not be materialized.** A package
event carries an utterance plus free-form details, so the payload a close must
materialize is one the ingestion *constructs* -- and it was validated only when the close
read it back. An attempt with empty details was accepted, and then the close refused it
and left the session in `closing` holding work nobody could credit. Every transformed,
retention-filtered payload is now validated before it is stored, on both paths, by the
same `assert_materializable` the close uses, and the message names the missing field.

While writing the regression for that I found the same shape one layer down: `min_length=1`
accepts `"   "`, so a whitespace-only note passed the contract and was refused by the
service at close -- session stuck again. Staged text fields now require something other
than whitespace, at the boundary.

**A close refused by the database had already written `closing`.** The service checked
the outcome and the fatigue label, and left uniqueness and metadata to the schema: a
reused close key, `actual_minutes=-1`, and an over-long summary each raised a raw
`ConstraintException` *after* the durable transition, leaving a session that said it was
being finalized. All three are now preflight checks with named codes, so a refused close
leaves a session that can still be closed.

One incidental correction: a `ValidationError` escaping to the CLI produced an envelope
saying the **output** contract had failed, which sent a caller whose input was malformed
looking in the wrong place. Batches and packages are now refused as
`invalid_session_batch` and `invalid_session_package`, naming the field.

## What the thirteenth review changed

Two bugs, and both are the twelfth round's lesson unlearned somewhere else: a boundary
that looked like it validated and did not.

**A published vocabulary validated nothing.** `json_schema_extra={"enum": [...]}` shapes
the generated JSON Schema and is invisible to pydantic, so `task_type`, `modality`,
`help_level`, `correction_mode`, `retrieval`, `assessor_kind`, `confidence`,
`response_visibility`, `claims`, `category`, and `salience` were all plain strings at
runtime. Every one of them staged a nonsense value happily and was refused at close --
the session left in `closing`, holding an observation nobody could credit, which is
exactly the failure the previous round fixed in three other places.

The fix is not a validator per field. A `Vocabulary` marker annotates the **string**, so
one declaration produces the runtime check and the published enum together, and a field
added later is enforced without anyone remembering to enforce it. That last property is
the point -- per-field validators would have been the same defect waiting for the next
field.

*(The first attempt at this published the enum through `json_schema_extra`, which the
fourteenth review then found to be in the wrong place; see below.)*

**A close key was compared after the replay it guarded.** A finalized session returned
its stored result before the ownership check ran, so closing A under `key-A` and B under
`key-B` and then retrying B under `key-A` returned B's result marked `replayed`: one key
successfully identifying two different closes. Ownership is now checked first, in both
directions -- a key that closed a *different* session is refused, and so is a key that
closed *nothing*, because returning this session's result to a caller retrying a call
they never made would confirm a belief that is wrong.

## What the fourteenth review changed

One defect, and it is the thirteenth round's own fix examined one level down.

Publishing a vocabulary through `json_schema_extra` merges it at the **field's** top
level. For a plain string that is harmless duplication. For `response_visibility`, which
is `str | None`, pydantic emits `anyOf: [{type: string}, {type: null}]` and the extra
`type: string` + `enum` land *beside* it -- and JSON Schema reads siblings conjunctively.
So `null`, the field's own default, satisfied the union and failed the constraints next
to it: a model's valid output did not validate against the model's own published schema.
Runtime accepted what the contract rejected, which is the same disagreement the previous
round was about, in the opposite direction.

The vocabulary now annotates the **string** rather than the field, so it sits inside the
branch it describes and a nullable field is simply a union of that and `None`. The marker
carries the runtime check with it, so the two still come from one declaration -- and
`VocabularyModel`, which read the enum back out of `json_schema_extra`, is gone: the check
travels with the type instead of being re-derived from the schema hint.

Two parity tests now hold the boundary from both sides. One validates a model-produced
batch against the checked-in schema, for every event kind with only its required fields
(the defect was a *default*) and once with every optional field set. The other walks every
staged payload's declared vocabularies -- sixteen of them -- and requires each to be
published in the schema *and* refused at runtime. Both read the declaration rather than
repeating it, so a field added later is covered by them automatically.

## Deferred, and why

Two clauses of this stage's own checklist are **not** implemented, and the boxes above
stay unticked rather than being claimed:

| Claimed | Actual state |
|---|---|
| close materializes **source progress** | There are no source tables. `sources`, `source_segments`, and `track_source_progress` are Stage 5's, and there is no staged event kind that could carry a position in a source. |
| close materializes **review tasks** | There is no `review_tasks` table. Rich review scheduling is Stage 6's; what this stage has is `followups`, which the close does materialize. |
| planner scores **source continuity** | Not computed. `curriculum_continuity` is real -- it reads `track_curriculum_progress` -- but a catalogued source has nothing to read, so a component for it would be a weight applied to a constant. |

The honest version of "implemented" for these would be an event kind nobody can produce
and a score that is always zero, which is worse than an unticked box: it would read as
working. Stage 5 adds the source tables and the progress events, Stage 6 adds review
tasks, and both extend *this* close rather than adding a second one -- the staging and
close boundary is the thing the handoff asks them to reuse.

## Verification

- [x] Unit-test lifecycle transitions and planner constraints.
- [x] Property-test plan duration and block ordering invariants.
- [x] Crash-test unflushed, staged, mid-close, and response-lost states.
- [x] Verify a duplicated batch/package/close does not duplicate durable facts.
- [x] Verify explicit modes are honored unless impossible, in which case the reason is returned.
- [x] Run `plan -> lesson -> close -> dashboard` in `PolishLinguaWiki` without direct SQL.
- [x] Run the repository-wide quality gate.

## What was built, and the decisions that shaped it

**The close boundary is the whole design.** While a session is running, nothing about the
learner changes. `session log` writes provisional rows; only `session close` turns them
into attempts, evidence, error occurrences, follow-ups, and notes -- in one transaction,
once. Everything else here exists to make that literally true rather than approximately
true:

- **`evidence.plan_attempt` / `write_attempt`, and the same split for errors and
  follow-ups.** DuckDB serves one connection per database and forbids nested
  transactions, so a close cannot call a command that opens its own. The decision (which
  claims an observation supports, which pattern a correction belongs to) is separated
  from the write, and only the write is inside the close's transaction. The rules did not
  move: they still live in `evidence.py`, `mastery.py`, and `error_model.py`.
- **A staged event's identifier becomes its attempt's idempotency key.** `attempts` has a
  unique index on that column, so one staged event cannot become two attempts even if a
  close were somehow run twice. The database enforces "exactly once" rather than the call
  order.
- **`closing` is a durable status.** Writing it in its own short transaction is what makes
  a crash *inside* the close distinguishable from a session nobody tried to close. The
  state machine refuses `active -> completed` for the same reason.
- **The close's result is stored on the finalization row.** A close whose response never
  reached the caller is retried, and the retry returns the first call's report marked
  `replayed` instead of doing the work again. A retry naming a *different* outcome is
  refused with the recorded one.

**`origin = 'session'` is refused by `evidence record`.** The schema has allowed that
origin since migration 0019; what Stage 4 adds is that the session engine is the only
thing that may write it. An attempt recorded by hand would be an attempt no staged event
produced and no finalization credited, which is exactly the state `db check` now looks
for.

**A block names a dimension *kind*, never a dimension.** This was a defect in the first
implementation and it is worth recording as one: the block types hard-coded `speaking`,
`grammar`, `reading`, while the pilot pack declares `spoken-production`,
`grammar-control`, `reading`. Every lookup silently missed, so ability and uncertainty
came back as "unmeasured" for every block. A block now declares one of the placement
policy's dimension *kinds* plus a modality, and the service resolves the track's own
dimension from the pack's `dimension_kinds` and the modalities its bank tasks use. A pack
that declares no productive dimension in speech makes a speaking block unavailable, with
that as the reason.

**The planner explains itself from its own arithmetic.** A block's rationale is derived
from the score components that selected it, so the explanation cannot drift from the
decision, and an omission carries the constraint that stopped it. Two constraints outrank
the score and are applied afterwards: new material must be paired with productive use, and
the novelty cap is enforced over the assembled plan rather than at each step that built it
-- a property test caught the productive-use exchange bypassing the budget it had been
charged against.

**A shorter session is an answer, not a rounding error.** Low reported energy, or too
little available material, produces fewer blocks and a plan whose `planned_minutes` is
below `requested_minutes`, with a warning saying which. The first implementation spread
the leftover minutes over the surviving blocks, which handed a learner who said they were
tired two fifty-four-minute blocks. Time nobody could fill is given back.

**Pronunciation is recorded, never confirmed, in this stage.** A `pronunciation.assessment`
event becomes a note, and the note says whether it rests on audio or only on a transcript.
Confirming how something sounded needs the utterance-and-audio model Stage 5 owns, and
pronunciation *evidence* comes from attempts in a pronunciation block. The event's basis
is recorded when it is staged rather than decided at close, when the package it came from
is no longer in front of us.

**Deduplication is by content, everywhere.** A batch is identified by its idempotency key
and its canonical content hash; a package by the canonical hash of the validated package.
The same conversation exported twice is one session however the bytes were arranged, and a
retry that carries different events under the same key is a conflict rather than an
overwrite -- because the second call would otherwise discard the first call's observations
silently.

## How 4.7 is satisfied, and where the line is

The dogfood workspace itself cannot live here: this repository holds no learner state, and
a private `PolishLinguaWiki` is a learner's own repository. What lives here is everything
that makes it work, and the property that it does:

| Checklist item | Where it is satisfied |
|---|---|
| Generate a private workspace from the template | `workspace init` renders it; `tests/workspaces/` exercises the template, and every session test builds one outside the repository. |
| Pin core, schema, skill, and pack versions | `workspace init` writes `linguawiki.lock`; `workspace doctor` and `db check`'s version mirror hold it. |
| Declared A2 onboarding and limited calibration | `onboard start`/`finalize` plus `assessment start`/`next`, covered by the Stage 2 and 3 suites and re-run by the clean-environment gate. |
| Three sessions of different durations and modes | `tests/integration/test_cli_stage_four.py` runs 40-minute mixed, 60-minute grammar, and 100-minute mixed sessions end to end through the CLI, each closing and rendering the dashboard. |
| Log defects rather than patching the database | There is no command that edits derived state, and the defects this stage found were fixed in core: the dimension-kind resolution above, the novelty-budget bypass, the energy-stretched blocks, and the empty block the first planner would have scheduled. |

The clean-environment release gate now runs the whole vertical from the built wheel with
no core source on the path: workspace, pack install, onboarding, a served calibration
task, one recorded observation, a context bundle, **a session planned, flushed, and
closed**, and the learner dashboard.

## What `db check` learned to see

Every invariant the engine enforces when it writes is also asserted over the data,
because a restore, a hand repair, or a build under a looser rule can present a state no
command would produce:

| Check | What it refuses to accept |
|---|---|
| `session_finalization_pairing` | A completed or partial session without exactly one close, or an unfinished one with any. |
| `session_outcome_agreement` | A close whose recorded outcome is not the session's status. |
| `session_batch_sequence` | A gap in a session's flush sequence: a lost batch means missing observations. |
| `staged_event_materialized_once` | One durable row claimed by two staged events -- one observation credited twice. |
| `staged_event_finalization` | A materialized event naming a close that does not exist, or one that closed another session. |
| `closed_session_staging_resolved` | A closed session still holding staged events its close never decided about. |
| `session_attempt_track` | An attempt materialized by a session that belongs to another learner's track. |
| `session_novelty_cap` | A session planning more new targets than its own cap allows. |
| `session_framing_blocks` | A warm-up or closure block carrying new material. |
| `session_package_uniqueness` | The same package content ingested twice. |

Two of these could only be *injected* in the tests by inserting a new row rather than by
updating an existing one, and the reason is the DuckDB limit this schema is shaped around:
updating a foreign-key or unique-indexed column on a referenced row is rewritten as a
delete and an insert, which the referenced row refuses. That is also why
`session_staged_events.materialized_id` is not a foreign key -- it is written by the close,
after the row exists, and it points at one of four tables depending on what the event
became.

## Exit gate

Stage 4 is complete only when the private Polish workspace can finish the complete declared-A2 flow and continue after a process crash using only committed learner-repo files, external backups, DuckDB state, installed skills, and the CLI. The session close must update all derived learner state exactly once.

## Handoff to Stage 5

Stage 5 extends the running session loop with real sources and speaking ingestion. It must reuse the same staging/close boundary rather than create modality-specific persistence paths.
