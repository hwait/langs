---
title: "C1 — Deterministic scoring with a serve-time snapshot"
stage: C1
status: ready
depends_on: []
---

# C1 — Deterministic scoring with a serve-time snapshot

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** The core computes scores for `objective` and `short-response` tasks against an
immutable snapshot of the task as served. No model, no client, no server.

**Why it is shaped this way.** A served task already snapshots `task_type`, `level_code`,
`difficulty`, `content_family`, `modality`, `is_anchor`, `rubric_version`, `content_hash`
(migration 0016) and `target_refs_json` (0022). It does **not** keep the answer key, the
prompt, or the rubric body. This stage adds those four and nothing else. `content_hash` is
already the drift detector, which is why no row ever needs backfilling from a changed pack.

## 0. Confirm the starting point

- [ ] Verify the current column set, so the migration adds only what is missing:
      `SELECT column_name FROM information_schema.columns WHERE table_name='assessment_run_tasks'`
      Expect 15 columns. If it differs, stop and re-read this stage.
- [ ] Confirm the migration head is `0029_selected_clips.sql`, so the new file is `0030`.

## 1. Migration

- [ ] Add `src/linguawiki/db/sql/0030_served_answer_key.sql`:
  - `expected_json VARCHAR` — the answer key as served
  - `prompt_snapshot VARCHAR` — what the learner was shown
  - `rubric_json VARCHAR` — the rubric body (only `rubric_version` is kept today)
  - `scoring_policy_version VARCHAR`
- [ ] All four are **nullable**. DuckDB cannot `ALTER TABLE ADD COLUMN` with a constraint, and
      rows served before this migration legitimately have none. Enforce presence in the
      service layer, not in the schema.
- [ ] Write the file's intent in a header comment, as the other migrations do: why the answer
      key is snapshotted rather than read live.
- [ ] Never edit a released migration. Add the new checksum to
      `tests/migrations/snapshots/released-migrations.json`; never remove a recorded one.
- [ ] Run `tests/migrations/test_migration_registry.py` and the upgrade fixture. If the DuckDB
      upgrade support script seeds tables explicitly, add the new columns there too.

## 2. Populate the snapshot at serve time

- [ ] In `services/assessment.py`, at the insert into `assessment_run_tasks` inside
      `next_task` (around the `sequence` computation), write the four new values from the
      selected `Candidate`.
- [ ] Add `SCORING_POLICY_VERSION` as a module constant beside the other policy values in
      `linguawiki/placement.py`. Bump it whenever the scoring rules change.

## 3. Scoring

- [ ] Add a pure function — no database, no I/O — that takes the snapshot and a response and
      returns a score, in `linguawiki/placement.py` beside `select_task`.
- [ ] Handle `objective` (exact match against the key) and `short-response` (the key's
      declared accepted forms; normalise case and Unicode as the knowledge search already
      does, and nothing more).
- [ ] Refuse `extended-productive`, `pronunciation-target`, `connected-speech` with a named
      error; these need a judge.

## 4. Wire it into `record`

- [ ] In `services/assessment.py` `record()`, when `assessor_kind` is `deterministic` and no
      `score` is supplied, compute it from the snapshot.
- [ ] Record `scoring_policy_version` on the result row.
- [ ] Keep the existing behaviour when a caller supplies a score: the flag has always only
      *labelled* it, and callers relying on that must not break.

## 5. Tasks served before this migration

- [ ] If the live `assessment_tasks` row's hash equals the snapshotted `content_hash`, the
      content is provably identical and may be read.
- [ ] If it differs, refuse deterministic scoring and mark the task unscorable-without-a-judge.
- [ ] **No backfill from the pack, ever.** Write this as a comment at the branch.

## 6. CLI

- [ ] Allow `linguawiki assessment record` to omit `--score` when `--assessor-kind
      deterministic`, and refuse the combination that cannot be computed.
- [ ] Update `.agents/skills/linguawiki-assess/SKILL.md` so the skill stops supplying scores
      for machine-scorable tasks.

## 7. Tests — `tests/integration/test_deterministic_scoring.py`

- [ ] A correct and an incorrect objective answer score 1.0 and 0.0 without a model.
- [ ] **Pack drift**: serve a task, change the pack's `expected`, score — the score reflects
      the snapshot.
- [ ] A pre-migration row with a matching `content_hash` scores; one with a drifted hash is
      refused rather than guessed.
- [ ] Each of the three rubric-scored task types is refused by name.
- [ ] `scoring_policy_version` is recorded on the result.

## Gate

- [ ] `./.tools/uv run python scripts/generate_schemas.py` if any contract changed
- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

A calibration's objective and short-response tasks can be answered and scored entirely from
the CLI, the recorded provenance shows no model was involved, and no score rests on content
the learner was not shown.
