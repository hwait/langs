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
- A manifest entry is a claim, not a fact. A package declaring audio was believed: the
  file was never opened, so a fabricated hash and a path to nothing produced a *confirmed*
  acoustic claim, and no artifact row existed for a purge to reach. Anything a file says
  about the world outside itself is checked before it is stored, and what it describes is
  registered so later commands can act on it.
- When one operation spans transactions that cannot undo each other, every refusal happens
  before the first write. Staging a package's events and storing its transcript are
  separately idempotent by design, which makes their *order* the only thing that keeps a
  rejected package from leaving half of itself behind.
- An identifier is only unique inside the scope its producer promised. Utterance IDs are
  unique within one call, so two unrelated conversations both carrying `utt_001` collapsed
  into one and the second was skipped as already imported. Scope the uniqueness to what
  the producer actually guarantees, and refuse a reused identifier whose content drifted.
- A record that says something was deleted must mean it. "Not retained" wrote the row and
  left the bytes; `--keep-file` wrote a tombstone and left the bytes. Either way the
  learner had been told a privacy request was honoured that was not. Deleting fails loudly
  rather than being recorded optimistically, and `db check` finds a file that came back.
- A privacy control has to cover every route out, not the one it was written for. Scanning
  `wiki/**/*.md` for leaked transcripts missed AGENTS.md -- which is on the Git-safe list
  and therefore *certain* to be committed. Enumerate the candidates that can actually
  reach Git and scan all of them.
- Preserve uncertainty across a boundary or the rules that need it cannot be written. A
  transcriber's confidence was dropped at ingestion, and with it the plan's rule that
  low-confidence speech becomes a possible transcription error rather than a learner
  error: every mishearing arrived looking exactly like a mistake.
- A report that says something happened to the learner's record must be the thing that
  made it happen. `counts_against_the_learner` was returned as true by a command that
  wrote no occurrence and no link, so the promised utterance -> correction -> error-history
  chain stopped at the report object.
- Compare against a vocabulary that exists. `source.status <> 'abandoned'` was true of
  every row, because `abandoned` is a *progress* status and never a source one, so the
  filter excluded nothing and archived material went on shaping plans. A comparison
  against a value no column can hold is dead code that reads as a rule.
- Resolve every reference through the scoped resolver that already exists rather than by
  primary key. Checking existence and not ownership let one learner's source link to
  another learner's error pattern.
- Put a safeguard at the *lowest* entry point, not the one you happened to be fixing. A
  check added to `speaking.ingest` left `session ingest-package` staging package events
  with no transcript and no verified audio -- a path that had merely been lax became one
  that accepted work the close then refused. If two commands can reach the same write,
  they run the same refusals because the refusals live under both.
- A command that says what *would* happen must run the same code as the command that
  makes it happen. Two implementations of "what is wrong with this package" drifted within
  one round: review reported a problem that ingestion did not, and a reviewer told "valid"
  and then refused has been told the opposite of the truth.
- Consent bounds what evidence may be *claimed*, not only what is stored. A package
  confirming pronunciation on a track that refused audio retention was accepted, the
  recording was deleted at the door, and the claim stood on nothing. Check the consent
  where the claim is made, and make the resolver require the recording to be *kept*
  rather than merely to have a row.
- Do the irreversible thing inside the transaction that records it. A deletion after the
  commit left the row saying a file was gone beside the file.
- Skipping verification because an identifier is familiar is not an optimization. A
  package reusing an artifact ID was trusted without opening the file, and a second
  conversation's claims came to rest on the first conversation's recording.
- An early return on one input discards the others. Deduplicating by hash returned before
  retention and producer identity were considered, so "same bytes, do not keep them" kept
  them and a re-ingest could not find the row it had registered.
- A scan that skips what it cannot read reports what it did not check. Unknown suffixes
  and undecodable files made the privacy audit pass on a page holding a transcript and one
  invalid byte; they are findings, not warnings.
- An override of a safety rule is a decision with an author. Accepting a boolean from any
  caller let an AI turn a mishearing into a learner error, and storing nothing left a
  durable record indistinguishable from a confident correction. Require the reviewer who
  could have the evidence, require the reason, and put both on the row.
- A refusal must leave nothing behind, which makes the *whole* preflight read-only. Audio
  was registered at the top of the ingestion, before the track, language, session-state,
  duplicate, event-identity, and materializability checks that ran inside the writer -- so
  a package refused for being in the wrong language had already put its recording in the
  learner's workspace. Run every check that can refuse before the first write, and put the
  write after all of them.
- An idempotent retry must have the side effects of a retry, not of a first attempt. A
  re-ingest of content already present registered its audio again, so an exact retry after
  the learner purged the recording failed the audio checks instead of reporting the
  duplicate it was.
- A check that needs the database still belongs in the shared preflight. Utterance drift
  was detected by the transcript import, which runs second, so a package reusing an
  identifier for different words was refused *after* its events were staged.
- Carry the fact a decision depends on, not a proxy for it. The resolved-artifact map
  omitted `kind`, so a transcript file satisfied a `confirmed` audio claim -- the acoustic
  rule defeated by a file extension -- and omitted nothing about retention, so a recording
  the learner had declined to keep counted as available once the bytes reappeared.
- A learner's decision is not reversible by a file. A package re-declaring a recording the
  learner had chosen not to keep was accepted; a package cannot overturn consent, and the
  honest answer is to refuse and say why.
- When one operation both binds and deletes, both belong in one transaction. The producer
  identifier was committed and the duplicate deletion then failed, leaving the identifier
  bound while untracked bytes stayed on disk.
- Enumerate once and pass it down. The privacy audit listed Git candidates twice and kept
  only the first listing's failure, so a second listing that came back unavailable made the
  content scan examine nothing and report a clean workspace.
- A service guard is not a constraint. Override provenance was enforced only in Python, so
  a restore or a hand-repair could leave an AI-authored override with no reason and `db
  check` called it clean. Where a rule can be a CHECK, make it one, and pair it with a
  named check for the rebuild that drops it.
- Model the distinction a policy turns on. "Keep this while a target is unfinished" applied
  to whole conversation recordings because nothing could say "this is a thirty-second
  excerpt", which is the indefinite full-recording retention the plan exists to avoid.
- A privacy declaration is an instruction, not a note to skip. A package entry saying a
  recording "existed and was not kept" was ignored, so the bytes sat unregistered under an
  ignored directory with nothing accounting for them. Record it as not kept -- which
  deletes the file -- so the record explains the absence and the absence is real.
- Uniqueness has as many forms as the thing has identities. Registration refused one
  recording answering to two producer identifiers, but the preflight compared only
  identifiers, so a package declaring the same bytes twice passed review, wrote its first
  artifact, and was refused on its second. Check every identity the writer will check.
- A label that buys a privilege has to cost something. "This is a clip" earned longer
  retention than a whole recording gets, and a clip was anything naming another artifact --
  so an entire second conversation qualified. Require the window, the kind, and the
  provenance, in the service *and* in `db check`.
- "Duplicate" means two copies exist. Deleting the incoming file because its path differed
  from the registered one destroyed the only surviving copy when the registered file was
  already gone; the row should follow the bytes instead.
- Codes are part of the contract, so a refusal keeps the name its callers know. Collapsing
  several distinct failures into one generic code took away a skill's ability to tell "this
  names audio nobody has" from "this session is closed" -- and matching error *prose* to
  recover the code is how that breaks again. Carry the code with the problem.
- A field's name is a promise about what it counts. `audio_available` counted every resolved
  artifact, so a transcript-only package reported audio available, and the duplicate path
  omitted it entirely, so a re-ingest reported none while the recording sat there.
- A check that mirrors half a constraint passes the states the other half forbids. The
  override check examined only rows whose flag was set, so a reason recorded against an
  override that never happened read as a judgement nobody had made.
- Identify what you are about to destroy. A package declaring a file "not kept" was
  honoured by deleting whatever sat at that path, without checking it was the file the
  package described -- so a misdescribed path deleted an unrelated recording. Anything
  irreversible verifies its target first.
- "Present" is not "the file we registered". Recovery treated the copy at the registered
  path as authoritative because it *existed*, so when those bytes had been altered the
  correct recording restored elsewhere was classified as the duplicate and deleted. Compare
  the hash, not the existence.
- One structure, one question. A set that answered "have we seen these bytes in this
  package" was reused for "may a claim rest on them", and a package declaring its own audio
  unkept satisfied a confirmed acoustic claim. When a second question arrives, give it its
  own answer rather than the nearest available one.
- A field that reports availability reports the disk, not the row. `audio_available` read
  only metadata, so a recording somebody had deleted or altered by hand was still
  "available" -- and the cost of meaning it is one hash of one file.
- A review must be asked the question the action will be asked. `speaking validate` had no
  session parameter, so it approved against the active session while `ingest --session`
  targeted a closed one.
- A check keyed on one field cannot see rows that set only the others. Clip validation
  queried rows naming a source, so offsets with no source were invisible; and it never
  compared the source with the row itself, so a recording could be an excerpt of itself and
  draw the retention a clip is given.
- A conflict has two directions and a preflight owes both. Review refused a package that
  claimed a declined recording and accepted one that declined a kept recording, while
  registration refused both -- so the package was approved, wrote its first artifact, and
  was rejected on its second.
- Containment is resolved, not lexical. Refusing `..` does not catch a symlink under
  `imports/` whose target is outside the workspace; the preflight has to resolve the path
  exactly as the writer does, because the writer's refusal comes after the first write.
- Never leave bytes under a private root that nothing accounts for, and never delete a file
  you cannot identify. When both would be needed to proceed, refuse and say what the
  options are: repointing a record away from altered bytes abandoned them, and deleting
  them would have destroyed a file the workspace could not describe.
- Reading a file can fail, and "cannot be read" is usually an answer rather than an error to
  propagate. Hashing a recording without catching `OSError` turned a deliberately accepted
  no-op retry into a crash; the same read in a preflight is a problem to report.
- One helper for reading a path a row names, used by every reader. Four copies of "join the
  root and hash it" trusted the path four ways: registration resolved it, `verify` did not,
  the availability check did not, and recovery did not -- so replacing a registered file
  with a symlink to matching bytes *outside* the workspace made it report present, made a
  package's audio available, and made recovery treat the outside target as canonical and
  delete a genuine in-workspace copy. `artifacts.contained_file` answers "is this a file
  this workspace holds", and absent, escaping, and unreadable are its one answer.
- A tombstone is part of the state a preflight has to see. Excluding purged rows from the
  producer map made a purged recording invisible to review and visible to registration,
  whose lookup was unfiltered -- so a package offering it again was approved, and the
  re-supplied file was deleted because a purged row is non-retained and that looked like
  "the learner declined this".
- A deletion is not undone by supplying the bytes again. The workspace holds one row per
  recording, so there is nowhere for a second life to go, and rewriting a tombstone would
  erase the record that the learner asked for the recording to go. Refuse, and leave the
  file they offered where it is.
- When a rule goes in this file, apply it everywhere in the same change. "Reading a file can
  fail, and cannot-be-read is usually an answer" was recorded here and then implemented in
  two of the four places that read a file: `artifact verify` -- the command whose entire job
  is to report on these files -- still raised `PermissionError` instead of reporting.
- A helper that answers a yes/no question invites its callers to guess at the no. Six
  readers went through `contained_file`, and two of them then had to work out whether the
  file was absent, escaping, or unreadable -- one reported an unreadable file as escaping,
  which sends somebody to fix a symlink that does not exist. `classify_path` says which, and
  `contained_file` is the yes/no face of it.
- A tombstone records where a recording *used to be*. It is not a claim on that path, so a
  learner who purges a conversation and records a new one under the same filename must not
  be told forever that the deleted one came back. The live row owns the path.
- Scope a rule to the thing it is about. An artifact *row* belongs to a track; the *file* it
  names belongs to the workspace. Scoping path ownership to the track let two learners
  register one file, and then either one's purge or retention sweep deleted the other's
  recording. Pair the rule with a `db check`, because a restore can present a collision the
  rule now refuses.
- A predicate that excuses something must check every condition the excuse rests on.
  "A live row owns this path" excused a tombstone's old path without asking whether what is
  *there* is the owner's recording -- so restoring the purged recording over the new one
  passed the privacy audit.
- Resolving a path is itself an operation that fails. `Path.resolve` raises `RuntimeError`
  on a symlink loop under Python 3.12, which no `LinguaWikiError` handler catches; the shared
  containment helper now refuses an unresolvable path instead of letting it crash whichever
  reader touched it, and "cannot be resolved" is reported separately from "resolves outside",
  because the two send an operator to different places.
- Orthogonal facts are recorded independently. A tombstone stopped being counted as purged
  when its old path became strange, because the path check returned early -- what a row *is*
  does not depend on what its path has since become.
- A count in a warning is not actionable. Human output names the artifacts it is reporting
  on, so an operator knows which file to go and look at.
- When a rule's *scope* changes, the change is only half done until every reader of that
  rule agrees. Making path ownership workspace-wide in the writer while the package preflight
  still loaded one track's artifacts put the preflight back behind the writer -- the same
  defect the shared preflight was built to end -- and narrowing ownership to live, retained
  rows in the readers while the writer counted every non-purged row made a declined
  recording own its filename forever. One function answers the question; every caller asks
  it.
- A summary must not nest counts that are independent. "0 files present, 1 of them altered"
  is a report arguing with itself: a tombstoned recording whose file has come back is
  altered and is, by definition, not among the present files.
- A preflight mirrors the writer's whole predicate, not its newest clause. Adding
  workspace-wide path ownership caught another track's path but still admitted altered
  bytes on this track under a new producer ID, so a multi-file package could again write
  its first artifact and fail on its second.
- An explicit instruction and a durable privacy decision may conflict; the conflict is a
  refusal, never permission to apply one silently. In particular, a file offered with a
  request to retain it must not be deleted because an older row says the same bytes were
  not kept.
- A local identity used as a lookup fallback is not automatically a producer identity.
  Registration supports binding the first producer ID to an unbound artifact, so package
  review must distinguish that bind from a second producer trying to rename an already
  bound recording.
- Learner repositories pin released core, schema, skill-bundle, and pack versions. They do not merge this repository as an upstream fork.

## Verification

Before handing off a change, run the relevant subset and normally the full gate:

```bash
uv run python scripts/verify.py
```
