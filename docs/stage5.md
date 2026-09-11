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

## Not attempted, and why

- **Editions.** 5.1 lists them; `sources` carries one edition's worth of bibliographic
  fields directly. A second learner reading a different translation catalogues a second
  source, which is already how sources are scoped. Modelling editions as their own table
  would only pay off when two learners in one workspace compare them, and nothing in the
  plan asks for that yet.
- **A difficulty model.** Unit difficulty is a number a learner or a skill records, not one
  derived from the text. Deriving it would require holding the text, which the rights class
  may forbid outright.
