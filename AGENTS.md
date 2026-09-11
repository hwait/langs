# LinguaWiki core repository instructions

This repository owns the generic Python package, schemas, migrations, Codex skills, templates, and synthetic fixtures. It must never contain real learner state.

## Invariants

- Keep core language-agnostic. Language-specific knowledge and framework mappings belong in versioned language packs.
- Use DuckDB as the sole authority for mutable learner state. Markdown is a deterministic projection or controlled import surface.
- Write through one application-level writer lock and short explicit transactions; use read-only connections for reports, context, and diagnostics. `open_writer` refuses a database that is not ours or not safe to change, so no command has to remember the check.
- Treat every table and column name read back from the database as untrusted data: quote it with `db.connection.quote_identifier`, never with a hand-written `"{name}"`. `scripts/check_sql_identifiers.py` enforces this.
- Validate before normalizing. Every recurring defect in Stage 1 came from discarding a distinction first: logical types reduced to physical ones, sequences mapped to dictionaries, typed variants flattened to "a mapping", damaged state flattened to "ours".
- Treat released migration files as immutable. Add a new numbered file; never edit a released one, and never remove its recorded checksum from `tests/migrations/snapshots/released-migrations.json`.
- Never put mutable state on a row another table references. DuckDB rewrites an `UPDATE` that
  touches a unique-indexed or foreign-key column as a delete and an insert, which a
  referenced row refuses; and it will not delete a referenced row in the same transaction
  that repointed its children, so a row whose identity has to change is *superseded* and
  names its successor rather than being deleted. Split identity from installation state
  (`language_packs` / `pack_installations`), and when a column must be written after
  insertion, drop its foreign key and pair the omission with an entry in
  `integrity.ORPHAN_RELATIONS` or a named `db check`. An unenforced relation with no check is
  not acceptable. A unique index over such a column has the same effect as a foreign key, so
  a uniqueness rule on a mutable column belongs in a named check too.
- A command reports through the connection it already holds, or after that connection closes.
  DuckDB serves one connection per database file, so a writer that calls its own report
  deadlocks on its own lock. Every report has a `(database, id)` form for use inside a writer
  and a thin `(paths, ...)` wrapper for use outside.
- Decide "is this content reviewed enough?" only in `provenance.py`. Each review axis has its
  own ordered state vocabulary; compare *strengths* within an axis, never state names across
  axes, and never let a machine or AI reviewer exceed `MACHINE_CEILING`. `content_reviews`
  projects the result onto the frozen `lingua.content.v1` gate, and a test keeps the projection
  from ever accepting what v1 would reject.
- Derive pack identity; never assert it. A content ID comes from `(pack_key, kind, stable_key)`
  and a content hash from the canonical content JSON. That is what makes a reinstall idempotent
  and a learner's annotations survive a pack upgrade, so `pack stamp` writes hashes and
  `pack validate` enforces them — an edited item without a re-stamp must fail rather than
  quietly re-bind its reviews.
- Pin `duckdb` exactly. A version bump is accepted only after `uv run python scripts/verify.py --duckdb-upgrade <version>` passes; it installs both versions and proves the candidate can open, re-export, and restore data the outgoing version wrote. The retained fixtures in `tests/fixtures/portable-export/` pin the export shape only.
- Make deterministic mechanics available through typed Python and CLI contracts. Skills orchestrate those tools; they do not edit databases or reproduce business rules in prompts.
- Use UTC internally and explicit IANA timezones at user/track boundaries. DuckDB stores naive UTC timestamps; the repository layer attaches the UTC zone on read.
- Preserve typed opaque identifiers, provenance, idempotency, privacy, and explainability across changes.
- Keep only synthetic learner/session fixtures in this repository. Never add a real database, transcript, recording, backup, exported deck, or learner workspace. `language-packs/pl-pilot` is a temporary dogfood pack, not learner state; it ships in the wheel because a workspace holds no core source.
- Never claim on a learner's behalf. A declared level is a `declared-hypothesis` estimate with
  no evidence, a self-reported course unit reaches `encountered` and no further, and a
  dimension nothing could test is `not-tested` rather than failed. A level label belongs to one
  framework: refuse a label from another by name, and never relate two frameworks in code.
- Scope a learner reference to the track's own material. A track names the pack it is
  taught from, so every item, alias, and bank task is resolved inside that pack's content,
  that track's own notes, or content owned by neither -- and a reference outside it is
  refused with whose it is, never reported as missing. Check a content ID exactly as a
  stable key: it is the form that looks least like a guess.
- What the learner faced is read from the record of what was served, not from the pack. A
  pack is mutable and a run is not, so `assessment_run_tasks` snapshots a task's type,
  modality, dimension, difficulty, and family when it is served, and evidence resolves
  through the run -- never by re-reading `assessment_tasks`, which lets a later pack edit
  rewrite somebody's history and raise the claim they are credited with. The bank is the
  account of last resort: for an observation outside any run, or a task served before the
  snapshot existed, and the report says which account it used. A caller's value that
  contradicts either is refused with the recorded value named; silently overriding is
  worse, because the caller believes the observation it described is the one recorded.
- A run belongs to one learner. Resolve it through `assessment_runs` and require its
  `track_id` to match the track being written, or one learner's sitting lands in another
  learner's model -- checking only that the run served the task does not establish whose
  run it was.
- A validation that only runs when the data is present is not a validation. Absent is the
  case where the data is least trustworthy: an empty target list is not permission to
  attribute an observation to anything, a null column on one side of a comparison is a
  mismatch rather than a row to skip, and an inner join answers "do these agree" while
  silently passing everything with no counterpart -- so establish membership separately.
  A snapshot is whole or wholly absent; a partial one is damage, and treating it as
  history sends the reader back to the mutable source it exists to replace.
- A check is code, and inherits every weakness it was written to find. `db check` is a
  diagnostic, so it must never raise: where it reads data the schema cannot constrain --
  DuckDB refuses `ALTER TABLE ADD COLUMN` with a constraint, so an added JSON column has
  no `json_valid` -- parse it defensively in Python and report what will not parse. A
  diagnostic that aborts tells an operator less than one that lies, and a SQL predicate
  guarded by `IS NOT NULL` skips exactly the rows the write path refuses hardest.
- A compatibility guarantee has a direction, and a gate that ignores it passes by luck.
  DuckDB promises that a newer release reads an older file, never the reverse: with
  identical seeded data, 1.4.1 opening a 1.5.5 database failed three of five runs with
  `INTERNAL Error: Failed to load metadata pointer` and passed twice. So
  `scripts/check_duckdb_upgrade.py` requires native open only when the candidate is
  newer, and judges a downgrade on the portable Parquet export -- which is the recovery
  path off a bad upgrade anyway. A green run whose outcome a rerun can reverse is worse
  than a red one, because it is quoted afterwards as a guarantee.
- Re-derive a derived identity whenever its parts move. An error pattern and a relation
  edge both key on the things they describe, so repointing one without re-deriving leaves a
  key describing something else -- and the next attempt to add it derives the new key, finds
  nothing, and creates a duplicate. Where the row cannot be replaced, supersede it and name
  its successor. Assert the consequence in `db check`, not the derivation: one edge per
  (source, type, target) catches a stale identity however it arose.
- Decide what an observation may claim only in `evidence.py`, and what a claim may promote
  only in `mastery.py`. An attempt is what happened; an evidence *claim* is what it justifies,
  and each claim caps the stage it can ever reach — which is what makes "recognition cannot
  promote production" structural rather than conventional. Derive the claim, the novelty, and
  the delay from what is recorded rather than accepting them from the caller: each is a place
  a caller could otherwise talk its way into a promotion. Every policy carries a version
  string that is stored on the rows it produced, so a stage is recomputable from raw evidence
  and a policy change can be replayed rather than migrated.
- One correct answer never resolves anything. An error needs controlled, novel, spontaneous,
  and delayed success across more than one context, counter-evidence is derived from the
  evidence rows rather than asserted, and `error_model.assert_policy_is_sound` refuses a
  configuration that could accept a fix it has not seen. `controlled` and `spontaneous` are
  terms about *production*: read them from what the learner did -- produced language, not
  chose between offered options -- never off the claim or the help level alone, or receptive
  evidence retires an error the learner still makes.
- An artifact is not a mistake. An occurrence classified anything but `learner-error` is
  recorded and counted against nobody: the pattern sits at `unconfirmed`, outside the live
  set, until an occurrence is confirmed. A mishearing taught back as a mistake is worse than
  a mishearing lost.
- While a session is running, nothing about the learner changes. `session log` stores
  provisional rows; only `session close` materializes attempts, evidence, error
  occurrences, follow-ups, and notes, in one transaction, once. That is why a writing
  service splits into a `plan_*` that decides (and reads) and a `write_*` that writes
  inside the caller's transaction: DuckDB forbids nested transactions, so a close cannot
  call a command that opens its own. A staged event's identifier becomes its attempt's
  idempotency key, so "exactly once" is enforced by a unique index rather than by the
  order somebody called things in.
- A crash has to leave a state the next invocation can name. `closing` is written in its
  own short transaction for exactly that reason: without it, a close that failed
  mid-transaction is indistinguishable from a session nobody tried to close. The stored
  close result is the other half -- a close whose response was lost is retried, and the
  retry returns the first one's report rather than doing the work again. A retry naming a
  different outcome is refused with the recorded one.
- Retrying is safe; overwriting is not. The same idempotency key with the same content is
  accepted once, and the same key with *different* content is a conflict, because
  accepting it would discard the first call's observations silently. Deduplicate by
  canonical content hash rather than by bytes, so the same conversation exported twice is
  one session however it was serialized.
- A block names a dimension *kind*, never a dimension. The dimension names belong to the
  pack (`spoken-production` in one framework, something else in another), and core
  resolves them from the pack's `dimension_kinds` plus the modalities its bank tasks use.
  Hard-coding `speaking` made every ability lookup miss silently, which is the quiet
  version of the language-specific-core failure this repository exists to prevent.
- Apply a privacy rule where the data first arrives, not where it is finally used. A
  staged event is never edited, so a payload stored before the retention rule ran keeps
  what it kept for as long as the workspace exists -- and applying the rule only at
  materialization protected the durable row while the provisional one held the whole
  transcript. Refuse at the boundary too: a caller asking to keep more than consent
  allows should be told when they ask, not when the session closes, because a refusal at
  close leaves the text stored *and* the session unclosable. Free-form fields from an
  external file are merged before the rule, never after, and provenance is set by the
  path that knows it rather than accepted from the payload.
- When something happened and when it was recorded are two facts, and derived state is
  built from the first. A flush timestamp used as an observation time silently rewrote
  every chronology: the delay class, whether a repeat was a repeat, when an error was
  last seen, which snapshot an observation fell into. Store both, order by the first, and
  check that the durable row agrees with the event it came from.
- Resolve dependent work as you go, not in a planning pass before any of it is written.
  Two corrections of one error pattern planned against the same prior state both believe
  they are the first -- a duplicate key where the pattern is new, a wrong occurrence count
  where it is not. Planning inside the caller's transaction is a read, so it sees what the
  earlier events just wrote. Name what a row *became*, too: a correction materializes an
  occurrence, and recording the pattern made two occurrences look like one row credited
  twice.
- A refusal has to leave a way forward, and the way it names must actually work. A
  batch-gap check that refused the partial close while recommending it, a lifecycle that
  permitted `planned -> abandoned` while a CHECK refused it, and an idempotency key that
  answered a different request with an older plan were the same mistake three times:
  the remedy in the message, the transition in the policy, and the retry in the contract
  all have to be reachable. Bind an idempotency key to a hash of the request, so a reused
  key with different content conflicts instead of quietly returning the wrong answer.
- Revalidate a payload before you act on it, however it was validated on the way in.
  Storage is a round trip through JSON: datetimes come back as strings, and a payload
  that was valid at the flush can be damaged, hand-edited, or restored from a different
  release before the close. Revalidating turned an unhandled `AttributeError` into a named
  refusal, and found a validator that had never run because pydantic does not validate a
  field's default.
- Keep the producer's identity for anything delivered more than once. A row's own
  primary key says nothing about whether two rows are the same observation, and every
  delivery mechanism repeats itself: a retried flush, a `checkpoint` export overlapping
  the `completed` export of one call, a re-ingested package. Store the source identifier,
  make it unique in the scope that owns it, and refuse a repeat by name -- an observation
  made once must never be credited twice. Where the scope is wider than one row's parent
  (two exports of one external session can land on two sessions), an index cannot see it
  and a check has to.
- The boundary that accepts a payload must be the boundary that can honour it. Validate
  the *transformed, stored* form -- after retention, after any construction the ingestion
  does -- not the shape that arrived, and not later. Accepting something that can only
  fail at close leaves a session holding work nobody can credit, and the failure surfaces
  as far as possible from the mistake. The same applies to text: `min_length=1` accepts
  `"   "`, which every service that stores it refuses.
- Reach every constraint before the durable transition, not after it. A close that
  validates two of its arguments and leaves uniqueness and lengths to the schema writes
  `closing`, then dies on a raw `ConstraintException` -- and the session is left saying it
  is being finalized. Preflight what the database will refuse: key ownership, ranges,
  lengths. A refusal has to leave the thing it refused still usable.
- Documentation that looks like validation is worse than none, because nobody checks it
  again. `json_schema_extra={"enum": [...]}` shapes the published schema and is invisible
  at runtime; every field carrying one accepted anything and was refused later by the
  service that stored it. Constrain the **type**, not the field: an `Annotated[str, ...]`
  marker carries the runtime check and the published enum together, sits inside the
  branch it describes when the field is nullable, and covers a field added later without
  anyone remembering to. `json_schema_extra` merges at the field's top level, where JSON
  Schema reads it conjunctively with the field's own `anyOf` -- so a nullable field's
  default of `null` satisfied the union and failed the constraint beside it, and a valid
  model dump did not validate against its own contract.
- Where a model and a published schema both describe one payload, test that they agree on
  real output, not just that each is internally consistent. Validate a model-produced
  document against the checked-in schema -- with only required fields, so the defaults are
  what is under test, and again with every optional field set.
- An idempotency key identifies one operation, in both directions. Compare it *before*
  returning a stored result, not after: a key that closed a different session must be
  refused, and so must a key that closed nothing, because handing back somebody else's
  result confirms a belief that is wrong. A guard placed after the path it guards is not
  a guard.
- A limit is a promise about the whole result, not about each part of it. Enforce a record
  or token budget against the assembled object and report what was dropped; independent
  per-section allowances add up to more than the number the caller asked for.
- A bounded report says what it left out. A context bundle drops whole sections rather than
  truncating inside one, records the counts and the reason in `omissions`, and never carries
  a learner's own words unless the command asked *and* the track consented. Half a list looks
  exactly like a short one, and an agent cannot tell the difference.
- Apply a retention rule where the text arrives, not where it is used. A staged transcript
  kept the learner's full words for the life of a session while the close correctly
  discarded them, so a workspace that refused transcript retention held them anyway. The
  same rule applies to every later copy: a revision, an excerpt, a log summary.
- The order of observations can be the evidence. Comprehension without help is only
  meaningful before help was given, so an unaided reading recorded after an aided one is
  refused -- at the command *and* at the session close, because a close that accepted it
  would be a way around the rule rather than a second path to it.
- What may be stored from someone else's work is decided by its rights class, checked
  before the text is written and asserted again over the data. A copyright boundary
  crossed inside a learner's database cannot be found by reading the code.
- Never overwrite what arrived. A raw transcript is immutable and every later reading is a
  new row naming what it changed, because "did the learner say that, or did the machine
  hear it?" is the question that decides whether they are corrected for a mistake they
  never made. A revision that claims to change nothing but punctuation is held to it.
- A claim needs the evidence it rests on to still exist. Confirming how something sounded
  requires audio that is present; prosody and native-likeness require it at every
  confidence, because text cannot carry them at all. When audio is purged, exactly those
  claims are invalidated -- marked, not deleted -- and nothing else is, because what the
  learner *said* was established by the transcript.
- A privacy control has to run before the irreversible act. Purge consequences are
  reported by a dry run, and the audit that looks for private content searches the files
  that are actually committed: a path rule knows `artifacts/` must not be committed and
  says nothing about the same recording's transcript pasted into a wiki page.
- Learner repositories pin released core, schema, skill-bundle, and pack versions. They do not merge this repository as an upstream fork.

## Verification

Before handing off a change, run the relevant subset and normally the full gate:

```bash
uv run python scripts/verify.py
```
