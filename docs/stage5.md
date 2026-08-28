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

- [ ] Add sources, editions, source units, external artifacts, transcript layers, media positions, and comprehension observations.
- [ ] Support course, curriculum, book, article, podcast, video, conversation, and learner-created material.
- [ ] Store bibliographic metadata, canonical URI, license/rights notes, checksums, and local external references.
- [ ] Never require copyrighted bodies or raw media to live in Git or DuckDB.

### 5.2 Reading and listening workflows

- [ ] Implement intensive and extensive modes with distinct progress semantics.
- [ ] Track unaided comprehension separately from aided comprehension.
- [ ] Record position, coverage, replay/reread count, lookup use, notes, and linked knowledge/error evidence.
- [ ] Adapt source difficulty and next-unit recommendations without claiming source text as pack content.

### 5.3 Speaking package workflow

- [ ] Implement manual creation/import of `lingua.session.v1` packages.
- [ ] Preserve immutable raw transcript, normalized transcript, reviewed-hearing layer, corrections, and evidence links.
- [ ] Represent uncertainty and disagreement between transcript layers.
- [ ] Permit transcript-only evidence for language production, but require linked audio for confirmed pronunciation/acoustic claims.
- [ ] Implement opt-in raw-audio retention, external paths, purge, and auditable tombstones/invalidation.

### 5.4 Voice runtime integration

- [ ] Keep core independent of any voice provider.
- [ ] Build one export adapter for the initially selected voice application when its format is sufficiently stable.
- [ ] Map provider output into the package contract; never let provider fields leak into domain tables.
- [ ] Document a fully manual fallback so speaking remains usable without native Codex voice.

### 5.5 Skills

- [ ] Implement `linguawiki-speak` for package creation, validation, review, ingestion, and privacy choices.
- [ ] Add source-selection, reading, listening, and media-progress references to `linguawiki-learn`.
- [ ] Load modality references progressively rather than expanding the umbrella skill.
- [ ] Require explicit confirmation before externally uploading private recordings.

### 5.6 Copyright and privacy controls

- [ ] Add checks that generated wiki pages contain metadata, short excerpts where permitted, and learner notes—not source copies.
- [ ] Exclude raw recordings, raw transcripts, and local artifact caches from Git.
- [ ] Redact logs and errors that might contain transcript/source bodies.
- [ ] Surface retention state and purge consequences before deletion.

## Required commands

```text
linguawiki source add|list|position|complete-unit
linguawiki artifact register|verify|purge
linguawiki transcript import|normalize|review
linguawiki speaking package|validate|ingest
linguawiki privacy audit
```

## Verification

- [ ] Complete one adapted reading, podcast, video fragment, and conversation block.
- [ ] Verify aided and unaided comprehension remain distinct.
- [ ] Verify package re-ingestion by hash is a no-op.
- [ ] Verify transcript-only input cannot create confirmed pronunciation evidence.
- [ ] Verify audio purge leaves a tombstone and invalidates dependent acoustic claims without deleting unrelated language evidence.
- [ ] Scan Git candidates and rendered wiki for prohibited raw/private content.
- [ ] Run the repository-wide quality gate.

## Exit gate

Stage 5 is complete when each major source modality can flow through the same session lifecycle, a real conversation package can be reviewed and ingested idempotently, and privacy/copyright tests prove that private or copyrighted artifacts do not enter learner Git history.

## Handoff to Stage 6

Stage 6 turns accumulated evidence and errors into a sustainable review queue and approved Anki exports.
