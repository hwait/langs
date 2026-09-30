---
title: "C2b — Presentation persistence and the served snapshot"
stage: C2b
status: shipped
depends_on: [C1, C2a]
---

# C2b — Presentation persistence and the served snapshot

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

## Shipped — 2026-09-30

Implemented on `c2-presentation-contract`. Stage gate `scripts/verify.py --fast`: **2223
passed, 1 skipped**. Two things went differently:

- **`assessment_service.served_task` was added** (§4). The stage requires "every reader
  of a served task returns the snapshot" and "a vanished asset produces a refusal", and
  there was no reader to do either: `next_task` never re-serves an outstanding task, and
  that guard belongs to C3. The reader is minimal and is what C3's read model builds on.
- **Pack assets are not installed into a table** (§2). No pack ships a recording, so the
  table would have no rows and no consumer. A served identity resolves against the pack
  directory `pack_installations.source_path` already records, and what comes back is
  checked against the snapshotted digest rather than trusted for being in the right
  place. **C5 should replace this with an installed-asset table**: it loads the whole
  pack to read one digest, which is correct but wasteful once audio exists.

**Goal.** The presentation C2a put in the pack files reaches the bank, and what the learner was
*shown* is preserved beside the answer key, so a pack update mid-run cannot show new choices
against an old key.

**Why it is a separate stage.** It needs C1's snapshot to exist and C2a's contract to define
the shape. C2a's authoring must not be blocked behind either.

**Why persistence is here and not in C2a.** `assessment_tasks` has no presentation column and
the installer writes an enumerated column list, so a pack carrying presentation installs into
a bank that discards it — and a serve-time snapshot would have nothing to snapshot. The
migration, the installer, and the read contract belong with the stage that consumes them.

## 0. Confirm the starting point

- [ ] The migration head is `0030_served_answer_key.sql`, so the new file is `0031`.
- [ ] `assessment_tasks` has no `presentation_json`; `assessment_run_tasks` has neither
      `presentation_json` nor `asset_identity_json`. If any exists, stop and re-read this stage.
- [ ] C2a shipped: `PackAssessmentTask` carries a presentation record, the asset catalog
      resolves `(asset id, digest)`, and the fixture packs still validate unchanged.

## 1. Migration `0031_task_presentation.sql`

One file covers both tables. Splitting them permits a released state in which the bank holds
presentation that no serve can snapshot, which is the half-state this stage exists to avoid.

- [ ] On `assessment_tasks`: `presentation_json VARCHAR`, nullable. `NULL` means *this task
      renders as free text* — which is what every task shipped before this migration in fact
      did, so there is no legacy hole and no backfill.
- [ ] On `assessment_run_tasks`: `presentation_json VARCHAR` and `asset_identity_json VARCHAR`,
      both nullable. These two are one group, on the pattern 0030 set: an asset identity with
      no presentation, or an audio presentation with no asset identity, is damage rather than a
      legacy row.
- [ ] All three columns are **nullable and unconstrained**. DuckDB refuses `ALTER TABLE ADD
      COLUMN` with a constraint, so there is no `json_valid` on any of them and no vocabulary
      check on the presentation kind. Enforce them in the service layer, parse defensively in
      Python wherever they are read, and assert them in `db check` (§5).
- [ ] Write the file's intent in a header comment: why presentation is snapshotted rather than
      read live, and why the asset's digest is stored beside its id.
- [ ] Never edit a released migration. Add the new checksum to
      `tests/migrations/snapshots/released-migrations.json`; never remove a recorded one.
- [ ] Run `tests/migrations/test_migration_registry.py` and the upgrade fixture. If the DuckDB
      upgrade support script seeds these tables explicitly, add the new columns there too.

## 2. Installation writes presentation to the bank

- [ ] In `services/packs.py`, add `presentation_json` to the `INSERT INTO assessment_tasks`
      column list **and** to its `ON CONFLICT (content_id) DO UPDATE SET` clause. Both: an
      upgrade that removes a task's presentation must clear the column, not leave the previous
      pack's choices standing behind a task that no longer has any.
- [ ] Serialize canonically — `sort_keys=True`, `ensure_ascii=False` — as the neighbouring
      `rubric_json` and `expected_json` writes do, so a reinstall of unchanged content writes
      identical bytes.
- [ ] A task with no presentation writes `NULL`, not `'{}'`. `expected_json` uses `{}` because
      its column is `NOT NULL` and `{}` has always meant "nothing to compare against" there;
      this column is nullable and `{}` would be a presentation record with no kind.
- [ ] Assets are installed too, so the serve path can resolve one without reading the pack
      directory: register each catalog entry with its digest, keyed by its content id.

## 3. Read contract

- [ ] One parser — `parse_task_presentation(raw: str | None)` — used by the bank reader, the
      serve path, and `db check`, exactly as C1's `parse_answer_key` is. It refuses with
      `assessment_presentation_malformed`, naming which rule failed: not JSON, not an object,
      unknown key, unknown kind, choices not a list, a blank or duplicated choice value.
- [ ] Revalidate on the way out of the database, not only on the way in. The column is a round
      trip through JSON and can be hand-edited, damaged, or restored from another release
      between the install and the serve.
- [ ] `db check` and the serve path use the same parser. Two implementations of "what is wrong
      with this record" drift, and a reader told "valid" and then refused has been told the
      opposite of the truth.

## 4. Populate at serve time, and read the snapshot

- [ ] In `next_task`, snapshot the presentation exactly as served: the choices, **their order
      as presented**, the asset identity, and the replay allowance in force. Write it from the
      same `SELECT` on `assessment_tasks` that 0030's three columns are written from — not from
      the selected `Candidate`, which carries only what selection is allowed to consider.
- [ ] If order may be shuffled, the shuffle happens once, at serve time, and the realized order
      is what is stored. A resumed task must not reshuffle.
- [ ] The asset identity snapshot carries **both** the asset's content id and its digest. The
      id answers which recording was meant; only the digest answers whether it is the one the
      learner heard.
- [ ] Every reader of a served task returns the **snapshot**, never the live pack row.
- [ ] Resolving a served asset compares the digest of the bytes on disk against the snapshotted
      digest, and refuses rather than substituting. Two distinct codes, because they send
      somebody to different places: `assessment_asset_unavailable` when nothing resolves, and
      `assessment_asset_changed` when something does and it is not the same recording. A
      different recording is a different question.
- [ ] A row served before this migration has a `NULL` presentation, which is truthful: the bank
      held none. **But** if that row's `content_hash` still equals its live `assessment_tasks`
      row's, and that bank row *does* hold a presentation, the snapshot is damage rather than a
      legacy row — the content is provably identical and cannot have been served without one.
      Refuse it by name and do not fall back to the bank, on 0030's rule.

## 5. `db check`

The schema cannot carry any of this, so each rule is a named check that reads defensively and
never raises.

- [ ] `bank_presentation_wellformed` — every non-null `assessment_tasks.presentation_json`
      parses, and agrees with its row's `task_type` and `modality`: choices only on
      `objective`, an asset reference only where the modality is `audio`.
- [ ] `served_presentation_complete` — the two `assessment_run_tasks` columns are both absent
      or consistent: an asset identity with no presentation, and an audio presentation with no
      asset identity, are each reported. Keep this separate from 0016's and 0030's groups; a
      row may legitimately be whole in one and absent in another.
- [ ] `served_presentation_wellformed` — every non-null snapshot parses, and every asset
      identity carries both an id and a digest. An identity with an id alone is a snapshot that
      cannot answer the question it exists for.
- [ ] Every failure names the rows it found, not a count.

## 6. Tests — `tests/integration/test_served_presentation.py`

- [ ] **Install round trip**: install a pack whose tasks carry presentation, reopen the
      workspace, read the bank — the presentation is what the pack declared. Then change a
      task's choices, remove another task's presentation entirely, reinstall, and read again:
      the first reflects the new choices and the second is `NULL`, not the previous pack's.
- [ ] **Resume after pack update**: serve, pause, change the pack's choices and its recording,
      resume — the learner sees the served presentation, and the answer key still belongs to
      the choices shown.
- [ ] A reshuffled bank does not reorder an already-served task.
- [ ] A vanished asset produces `assessment_asset_unavailable`; an asset whose bytes changed
      under the same id produces `assessment_asset_changed`. Neither substitutes.
- [ ] A task with no presentation serves and scores as free text, end to end.
- [ ] A pre-0031 served row reads as free text; the same row whose live bank task holds a
      presentation at a matching `content_hash` is refused as damage.
- [ ] A hand-damaged `presentation_json` — unparseable, unknown kind, duplicate choice value —
      is refused by name at read, and `db check` reports it rather than raising.

## Gate

- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

A pack's presentation survives installation and a reinstall, a run paused before a pack update
and resumed after it shows the learner exactly what it showed them the first time, and an
audio task whose recording has been replaced is refused rather than asked again with a
different clip.
