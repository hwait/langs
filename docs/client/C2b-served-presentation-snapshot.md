---
title: "C2b — Served-presentation snapshot"
stage: C2b
status: blocked
depends_on: [C1, C2a]
---

# C2b — Served-presentation snapshot

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** What the learner was *shown* is preserved beside the answer key, so a pack update
mid-run cannot show new choices against an old key.

**Why it is a separate stage.** It needs C1's snapshot to exist and C2a's contract to define
the shape. C2a's authoring must not be blocked behind either.

## 1. Migration

- [ ] Add `src/linguawiki/db/sql/0031_served_presentation.sql` — `presentation_json` and
      `asset_identity` on `assessment_run_tasks`, both nullable.
- [ ] Register the checksum in `tests/migrations/snapshots/released-migrations.json`.

## 2. Populate at serve time

- [ ] In `next_task`, snapshot the presentation exactly as served: the choices, **their order
      as presented**, the asset identity, and the replay allowance in force.
- [ ] If order may be shuffled, the shuffle happens once, at serve time, and the result is
      what is stored. A resumed task must not reshuffle.

## 3. Read path

- [ ] Every reader of a served task returns the **snapshot**, never the live pack row.
- [ ] If the snapshotted asset identity no longer resolves, the task is refused rather than
      shown with a substituted clip. A different recording is a different question.

## 4. Tests — `tests/integration/test_served_presentation.py`

- [ ] **Resume after pack update**: serve, pause, change the pack's choices and its clip,
      resume — the learner sees the served presentation, and the answer key still belongs to
      the choices shown.
- [ ] A reshuffled bank does not reorder an already-served task.
- [ ] A vanished asset produces a refusal, not a substitution.

## Gate

- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

A run paused before a pack update and resumed after it shows the learner exactly what it
showed them the first time, or refuses.
