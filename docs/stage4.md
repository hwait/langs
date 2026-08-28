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

- [ ] Add session, block, activity, staged-event, close-result, and follow-up migrations.
- [ ] Implement `planned -> active -> closing -> completed|partial|abandoned` transitions.
- [ ] Require idempotency keys on mutations and reject invalid transitions.
- [ ] Record timestamps in UTC while preserving the learner's IANA timezone for planning and display.

### 4.2 Planner

- [ ] Accept available minutes, requested mode, energy/fatigue, resource preferences, and optional learner intent.
- [ ] Score due review, weak dimensions, curriculum continuity, recent modality balance, goals, and source continuity.
- [ ] Enforce duration, novelty, review, modality, and fatigue constraints.
- [ ] Produce an explanation for every selected block and omitted high-priority candidate.
- [ ] Validate plans at 40, 60, 80, 100, and 120 minutes.

### 4.3 Durable staging and atomic close

- [ ] Implement `session log --input events.json` for bounded batches at block boundaries.
- [ ] Give every batch a session ID, sequence number, content hash, and idempotency key.
- [ ] Store staged events immediately; do not materialize mastery mid-session.
- [ ] Make close one transaction that validates and materializes attempts, evidence, errors, source progress, follow-ups, review tasks, and projection dirtiness.
- [ ] Implement `resume`, reviewed `partial-close`, and `abandon`; preserve an audit trail in all cases.
- [ ] Make duplicate batches and repeated close calls safe no-ops returning the original result.

### 4.4 Session-package ingestion

- [ ] Validate `lingua.session.v1` packages before staging their events.
- [ ] Deduplicate by package hash and retain source-layer references.
- [ ] Refuse unsupported schema versions with a migration-oriented error.
- [ ] Keep transcript evidence separate from audio-only evidence.

### 4.5 Learning skill

- [ ] Implement the first complete `linguawiki-learn` skill.
- [ ] Add focused references for correction policy, grammar/vocabulary, reading/listening, writing/translation, and session recovery.
- [ ] Make the skill request bounded context, present the plan, teach, flush at block boundaries, and explicitly close.
- [ ] Keep orchestration in the skill and all state transitions/calculations in Python.

### 4.6 Minimal learner projection

- [ ] Render today's plan, latest session, due review, active errors, and skill estimates from DuckDB.
- [ ] Mark generated pages and store projection hashes.
- [ ] Do not accept direct edits as authoritative state.

### 4.7 Start real dogfooding

- [ ] Generate a private `PolishLinguaWiki` from the Stage 1 template.
- [ ] Pin the current core, schema, skill, and Polish-pack versions.
- [ ] Run declared CEFR A2 onboarding and limited calibration.
- [ ] Complete and document at least three sessions of different durations/modes.
- [ ] Log defects in core or pack authoring rather than patching the learner database manually.

## Required commands

```text
linguawiki plan create|show
linguawiki session start|status|log|resume|close|partial-close|abandon
linguawiki session ingest-package
linguawiki wiki build --view dashboard
```

Every mutating command supports machine-readable JSON, stable error codes, and idempotency keys.

## Verification

- [ ] Unit-test lifecycle transitions and planner constraints.
- [ ] Property-test plan duration and block ordering invariants.
- [ ] Crash-test unflushed, staged, mid-close, and response-lost states.
- [ ] Verify a duplicated batch/package/close does not duplicate durable facts.
- [ ] Verify explicit modes are honored unless impossible, in which case the reason is returned.
- [ ] Run `plan -> lesson -> close -> dashboard` in `PolishLinguaWiki` without direct SQL.
- [ ] Run the repository-wide quality gate.

## Exit gate

Stage 4 is complete only when the private Polish workspace can finish the complete declared-A2 flow and continue after a process crash using only committed learner-repo files, external backups, DuckDB state, installed skills, and the CLI. The session close must update all derived learner state exactly once.

## Handoff to Stage 5

Stage 5 extends the running session loop with real sources and speaking ingestion. It must reuse the same staging/close boundary rather than create modality-specific persistence paths.
