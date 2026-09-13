# Stage 5 — Sources, Reading, Listening, and Speaking

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)
Previous: [Stage 4](stage4.md) · Next: [Stage 6](stage6.md)

## Outcome

The learner can use books, courses, podcasts, video, and spoken conversations while preserving provenance, copyright boundaries, transcript layers, and honest evidence semantics.

## Estimate

- Engineering: 8–12 days
- Source/audio preparation and review: 5–10 days

## Entry criteria

- Stage 4 exit gate passes under real dogfooding.
- `lingua.session.v1` ingestion and block-boundary staging are stable.
- Workspace privacy rules and external artifact paths are configured.

## Work packages

### 5.1 Source and artifact model

- [x] Add sources, editions, source units, external artifacts, transcript layers, media positions, and comprehension observations.
- [x] Support course, curriculum, book, article, podcast, video, conversation, and learner-created material.
- [x] Store bibliographic metadata, canonical URI, license/rights notes, checksums, and local external references.
- [x] Never require copyrighted bodies or raw media to live in Git or DuckDB.

### 5.2 Reading and listening workflows

- [x] Implement intensive and extensive modes with distinct progress semantics.
- [x] Track unaided comprehension separately from aided comprehension.
- [x] Record position, coverage, replay/reread count, lookup use, notes, and linked knowledge/error evidence.
- [x] Adapt source difficulty and next-unit recommendations without claiming source text as pack content. *(Unfinished material now scores as `source_continuity`, per source kind, onto the block areas that kind can serve. Difficulty adaptation is the per-unit `difficulty` field and the unaided-comprehension warning; a model that predicts difficulty from the text is not attempted, because the text is the one thing the rights may forbid storing.)*

### 5.3 Speaking package workflow

- [x] Implement manual creation/import of `lingua.session.v1` packages.
- [x] Preserve immutable raw transcript, normalized transcript, reviewed-hearing layer, corrections, and evidence links.
- [x] Represent uncertainty and disagreement between transcript layers.
- [x] Permit transcript-only evidence for language production, but require linked audio for confirmed pronunciation/acoustic claims.
- [x] Implement opt-in raw-audio retention, external paths, purge, and auditable tombstones/invalidation.

### 5.4 Voice runtime integration

- [x] Keep core independent of any voice provider.
- [x] Build one export adapter for the initially selected voice application when its format is sufficiently stable. *(No voice application has been selected, so the adapter built is for a transcription **format** -- a segment list with times and text -- rather than a product. `speaking.ADAPTERS` is the whole coupling surface; adding or removing one changes nothing downstream.)*
- [x] Map provider output into the package contract; never let provider fields leak into domain tables.
- [x] Document a fully manual fallback so speaking remains usable without native Codex voice.

### 5.5 Skills

- [x] Implement `linguawiki-speak` for package creation, validation, review, ingestion, and privacy choices.
- [x] Add source-selection, reading, listening, and media-progress references to `linguawiki-learn`.
- [x] Load modality references progressively rather than expanding the umbrella skill.
- [x] Require explicit confirmation before externally uploading private recordings.

### 5.6 Copyright and privacy controls

- [x] Add checks that generated wiki pages contain metadata, short excerpts where permitted, and learner notes—not source copies.
- [x] Exclude raw recordings, raw transcripts, and local artifact caches from Git.
- [x] Redact logs and errors that might contain transcript/source bodies.
- [x] Surface retention state and purge consequences before deletion.

## Required commands

```text
linguawiki source add|list|position|complete-unit
linguawiki artifact register|verify|purge
linguawiki transcript import|normalize|review
linguawiki speaking package|validate|ingest
linguawiki privacy audit
```

## Verification

- [x] Complete one adapted reading, podcast, video fragment, and conversation block.
- [x] Verify aided and unaided comprehension remain distinct.
- [x] Verify package re-ingestion by hash is a no-op.
- [x] Verify transcript-only input cannot create confirmed pronunciation evidence.
- [x] Verify audio purge leaves a tombstone and invalidates dependent acoustic claims without deleting unrelated language evidence.
- [x] Scan Git candidates and rendered wiki for prohibited raw/private content.
- [x] Run the repository-wide quality gate.

## Exit gate

Stage 5 is complete when each major source modality can flow through the same session lifecycle, a real conversation package can be reviewed and ingested idempotently, and privacy/copyright tests prove that private or copyrighted artifacts do not enter learner Git history.

## Handoff to Stage 6

Stage 6 turns accumulated evidence and errors into a sustainable review queue and approved Anki exports.

## Delivered, and where it lives

| Work package | Module |
|---|---|
| 5.1 source and artifact model | `linguawiki/sources.py`, `db/sql/0024_sources_and_artifacts.sql`, `services/sources.py`, `services/artifacts.py` |
| 5.2 reading and listening | `services/sources.py`, `contracts.StagedSourceProgressPayload`, `planner.WEIGHTS["source_continuity"]` |
| 5.3 speaking packages | `linguawiki/transcripts.py`, `db/sql/0025_transcripts_and_acoustics.sql`, `services/transcripts.py`, `services/speaking.py` |
| 5.4 voice runtime | `services/speaking.py` (`ADAPTERS`, `adapt`, `scaffold`), `.agents/skills/linguawiki-speak/references/manual-fallback.md` |
| 5.5 skills | `.agents/skills/linguawiki-speak/`, `.agents/skills/linguawiki-learn/references/sources-and-material.md`, `.../reading-and-listening.md` |
| 5.6 copyright and privacy | `services/privacy.py` (`audit`), `db/integrity.py` (`_source_checks`, `_artifact_checks`, `_transcript_checks`) |

Migration 0026 rebuilds `session_staged_events` rather than altering it. Both constraints
it had to change are CHECKs, DuckDB cannot drop one, and 0023 is released -- so the table
is rebuilt with the same name, columns, and index names, and nothing outside that file can
tell.

## What round fifteen changed

Eleven defects, six of them in privacy, evidence integrity, or ingestion correctness.
Migration 0027 carries the three that needed the schema:

| Defect | Fix |
|---|---|
| Package-declared audio was believed without opening the file, and no artifact row existed for a purge to reach | The file is checked for existence, location, and hash *before* ingestion; retained audio is registered as an artifact; a package's pronunciation event now materializes into `pronunciation_observations` naming that artifact, so a purge invalidates it |
| A rejected ingest left its staged events behind | Every refusal -- layer honesty and audio availability -- runs before the first write, in `speaking.assert_package_is_ingestable`, and `speaking validate` reports the same list |
| Utterance identity was `(track, external_id)`, so two calls both using `utt_001` collapsed | Identity is `(track, external session, external_id)`; a reused identifier whose text, speaker, or timing drifted is refused |
| "Not retained" and `purge --keep-file` both left the bytes on disk | Registering as not-kept deletes the file, `--keep-file` is gone, a failed deletion refuses rather than recording optimistically, and both `db check` and `privacy audit` find a file that came back. `keep`, `rolling-days`, and `delete-after-ingestion` are now real, applied by `artifact sweep` |
| The privacy scan read only `wiki/**/*.md` | It reads every Git candidate that can hold text, AGENTS.md and configuration included |
| Transcription confidence was discarded at ingestion | `lingua.session.v1` carries optional per-utterance `confidence` and a `transcriber` block; the adapter maps the format's own certainty; a learner-error interpretation of a low-confidence line is refused unless a person overrides it explicitly |
| `counts_against_the_learner` wrote nothing anywhere | A learner-error interpretation files an occurrence through the error model and stores the `error_id` it produced |
| `total_units` could be smaller than the units catalogued, and minutes could be negative | Both refused |
| Archived sources kept boosting plans, because the filter compared against a value no column holds | The filter names the real vocabularies, and putting a source down settles its progress |
| Source links resolved error patterns by ID without checking ownership | They go through `errors.resolve_error`, which is track-scoped, and `db check` asserts it; `source link --target-kind` makes the workflow reachable |
| `review` always claimed a hearing, and a re-review deleted its predecessor | The kind is derived from what changed; a second reading supersedes the first by naming it, and both stay on the record |

## What round sixteen changed

Round fifteen fixed `speaking.ingest` and left the path beside it unfixed, which is the
shape most of these share. Migrations 0027 and 0028 carry what needed the schema.

| Defect | Fix |
|---|---|
| `session ingest-package` bypassed every new safeguard and staged events whose transcript never arrived | The refusals and the audio registration moved into `sessions.ingest_package` -- the *lower* entry point -- and the CLI command routes through `speaking.ingest`, which does both halves |
| `speaking validate` and `speaking ingest` disagreed about unretained audio | Both call the same two functions, so the list a reviewer reads is the list ingestion refuses on |
| A track without audio consent still got confirmed audio evidence | The package is refused where the claim is made; materialization resolves audio only if it is kept and unpurged; `db check` treats "never kept" as it treats "purged" |
| A failed "not retained" deletion still committed the row | The deletion happens inside the transaction that records it |
| A reused producer identifier skipped file verification | Every declared file is verified on every pass, and an identifier already naming different bytes is refused |
| Hash deduplication discarded retention and producer identity | A contradictory retention request is refused, a missing identifier is bound, and a duplicate copy on disk is removed |
| Acoustic claims resolved utterances by `(track, external_id)` after 0027 changed identity | Staged payloads carry the external session, and the close resolves with it |
| The privacy audit was fail-open for files it could not read | Unknown suffixes and undecodable files are findings, and they fail the audit |
| The low-confidence override was accepted from any caller and stored nowhere | It requires a `human` or `learner` reviewer and a reason, and 0028 stores both on the interpretation |
| The retention sweep held audio for any confirmed claim | It holds only for a claim about a target the learner has not finished |

## What round seventeen changed

Mostly *ordering*: a check that ran after a write, or a write that happened before the
checks which could refuse it. Migrations 0028 (amended) and 0029 carry the rest.

| Defect | Fix |
|---|---|
| Audio was registered before the track, language, session-state, duplicate, event-identity, and materializability checks, so a refused package still registered it | The whole preflight is read-only and complete; registration happens after it returns, and a duplicate registers nothing |
| Utterance drift was detected by the transcript import, which runs after staging | The drift check joined the shared preflight, and `speaking validate` reports it |
| A recording the learner declined to keep counted as available once its bytes reappeared | Refused: a package cannot reverse consent |
| A transcript artifact satisfied a `confirmed` audio claim | The resolved-artifact map carries `kind`; the package check, the command, materialization, and `db check` all require audio |
| The producer-identifier binding committed before a duplicate deletion that could fail | One transaction over both |
| The privacy audit listed Git candidates twice and discarded the second failure | Enumerated once and passed to both halves; an unavailable listing is an unscanned finding |
| Override provenance was a service guard only, and a blank reason passed it | 0028 rebuilds the table with a CHECK, `db check` `override_provenance` catches a rebuild that drops it, and the reason is stripped before it is stored |
| The sweep held whole conversation recordings for as long as any target they touched was unfinished | 0029 adds clip provenance; only selected clips are held past the window, and a whole recording that evidence rests on is named in the report with `artifact clip` as the remedy |

## What round eighteen changed

Seven residuals, each a consequence of an earlier round's fix: a rule added in one place
and not its match, or a guard whose condition was almost right. No schema change.

| Defect | Fix |
|---|---|
| A package declaring audio "not kept" left the bytes unregistered under `imports/`, and re-registering declined bytes returned the old row and kept them | The declaration is honoured: the row records that the file existed and the file is deleted, at ingestion and at `register` |
| Two identifiers for identical bytes passed the preflight, then half-registered before the second was refused | The preflight compares content as well as identifiers, within the package and against what is held |
| A whole recording became a "clip" by naming another artifact, earning longer retention | A clip is audio, of this track's audio, over a window with `0 <= start < end` -- required by the service, the CLI, and `db check` |
| Deduplication deleted the incoming file when the registered copy was already missing, destroying the only copy | A duplicate means two copies exist; when the registered one is gone the row follows the bytes |
| `speaking validate` had its own partial copy of the ingestion checks | Both call `sessions.package_problems`, which returns coded problems; review now refuses what ingestion refuses and accepts the no-op retries it accepts |
| `audio_available` counted every resolved artifact, and the duplicate path omitted it | It asks whether *this package's* audio is kept, unpurged, and actually audio, on both paths |
| `override_provenance` mirrored only half its CHECK | It checks both directions, so a reason recorded against no override is found |

## What round nineteen changed

Six residuals, each a condition that was nearly right. No schema change; two of them
destroyed a learner's recording.

| Defect | Fix |
|---|---|
| A package declaring a file "not kept" deleted whatever sat at that path, and skipped the declaration when the identifier was already known | The hash is verified before the deletion, and a known identifier no longer skips the decision |
| A retained and a non-retained entry for identical bytes both passed the preflight, then half-registered | One loop over every entry, so collisions are compared on content whatever the retention says |
| Recovery deleted the correct recording when the registered path held altered bytes, and re-registering an altered file made a second row | "Present" became "present and matching"; a path registered under different bytes is reported as an alteration |
| `audio_available` read only the row | It requires the file to be there with the bytes it was registered with |
| `speaking validate` had no session parameter | It takes one, and the CLI passes it |
| Clip checking missed offsets with no source, and a recording that was an excerpt of itself | Every row with any clip field is inspected, a distinct source and the full triplet are required, and the reachable command-level case is refused |

## What round twenty changed

Four residuals. Two were the recurring shape -- a writer-side check the read-only preflight
did not run -- and two were about not leaving the workspace in a state nothing accounts for.
No schema change.

| Defect | Fix |
|---|---|
| A package declaring an already-kept recording as not kept passed review and was refused at the write | The retention conflict is checked in both directions, pointing at `artifact purge` for the decision that is the learner's |
| The preflight joined the root and the lexical path, so an `imports/` symlink out of the workspace passed review | It resolves the path through the same containment check the writer uses |
| Repointing a record away from altered bytes left them under a private root with no row, while `verify` read green | Refused, naming `artifact verify` and `artifact purge`; repointing is for a file that is genuinely gone |
| Hashing an unreadable recording raised out of a duplicate retry the command deliberately accepts | Reading failures mean "not available" in the report and "a problem" in the preflight |

## What round twenty-one changed

Three defects, one root cause: a registered path was read by four pieces of code that each
trusted it differently. No schema change.

| Defect | Fix |
|---|---|
| A purged recording was invisible to the preflight and found by registration, which deleted the re-supplied file because a purged row is non-retained | The producer map carries tombstones; both review and registration refuse resurrection by name, and leave the offered file alone |
| A registered path replaced by a symlink out of the workspace was reported present by `verify`, counted as available audio, and treated as canonical by recovery -- which deleted a genuine in-workspace copy | Every read goes through `artifacts.contained_file`, which resolves containment; `verify` reports `escaped`, and recovery refuses rather than acting on it |
| `artifact verify` and direct registration raised `OSError` on an unreadable file | `verify` reports `unreadable` as its own category and registration refuses by name -- the rule `AGENTS.md` already recorded, now applied in all four readers |

## What round twenty-two changed

Three smaller gaps, and a structural correction to last round's fix. No schema change.

| Defect | Fix |
|---|---|
| Registering a new recording at a purged recording's filename made a permanent integrity failure that nothing could clear | `artifacts.active_paths`: a live row owns the path, and a tombstone's path is history |
| `artifact listing` called an escaping symlink present and the privacy audit called an outside target a file still held locally | Both read through the shared resolver, so they agree with `artifact verify` |
| `register` raised a raw `OSError` when it could not look at a file | It refuses by name -- and `artifact_unreadable` rather than `artifact_file_missing`, because the next move differs |

`contained_file` also gained `classify_path` beside it. The yes/no helper left its callers
guessing which kind of "no" they had, and `verify` guessed wrong: it reported an unreadable
file as one whose path escaped the workspace.

## What round twenty-three changed

Four defects. Two were a scope mismatch -- rows are per track, files are per workspace -- and
no schema change was needed.

| Defect | Fix |
|---|---|
| Two tracks could register one file, so either learner's purge deleted the other's recording | Path ownership is workspace-wide, refused by name, with `db check` `artifact_path_ownership` for collisions a restore can present |
| A live row owning a path excused a tombstone whatever was at it, so restoring the purged recording over a new one passed the privacy audit | `tombstone_path_is_excused` requires the bytes to be the owner's, and `verify` and the audit share it |
| `Path.resolve` raises `RuntimeError` on a symlink loop under Python 3.12, crashing `verify`, `listing`, and the audio check | `assert_within` refuses an unresolvable path, and `classify_path` reports it as unreadable rather than as escaping |
| An escaped or unreadable tombstone was not counted as purged, and the CLI printed counts without identifiers | The tombstone is recorded first and unconditionally; `artifact verify` names every escaped and unreadable artifact |

## What round twenty-four changed

Three gaps, two of them opposite halves of the previous round's fix. No schema change.

| Defect | Fix |
|---|---|
| The package preflight loaded one track's artifacts while registration checked all of them, so a package could validate, register its first recording, and be refused on its second | `artifacts.path_owners` is the single workspace-wide answer, asked by both |
| The writer treated every non-purged row as a path owner, so a declined recording owned its filename forever although its bytes had been deleted | Ownership is live *and* retained in the writer too, matching the readers and the integrity check |
| `artifact verify` rendered independent counts as nested ones: "0 files present, 1 of them altered" | The counts read independently, because a tombstoned recording whose file returned is not among the present files |

## What round twenty-five changed

Three final mismatches between package review and artifact registration. No schema change.

| Defect | Fix |
|---|---|
| A same-track path holding altered bytes passed package review under a new producer ID, so registration could refuse after an earlier package artifact had been written | The shared preflight compares the workspace-wide owner's hash as well as its track before any write |
| Registering previously declined bytes with a request to retain them deleted the offered file under the old decision | Contradictory retention decisions refuse by name and leave the file untouched; reaffirming `retained=false` still removes bytes that came back |
| Package review called an unbound local artifact a competing producer identity, although registration supports binding the first producer ID to it | The producer map carries whether an external identity is bound; an unbound row may be bound only when its content, kind, and retention agree |

## Not attempted, and why

- **Editions.** 5.1 lists them; `sources` carries one edition's worth of bibliographic
  fields directly. A second learner reading a different translation catalogues a second
  source, which is already how sources are scoped. Modelling editions as their own table
  would only pay off when two learners in one workspace compare them, and nothing in the
  plan asks for that yet.
- **A difficulty model.** Unit difficulty is a number a learner or a skill records, not one
  derived from the text. Deriving it would require holding the text, which the rights class
  may forbid outright.
