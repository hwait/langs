---
title: "C1 — Deterministic scoring with a serve-time snapshot"
stage: C1
status: shipped
depends_on: []
---

# C1 — Deterministic scoring with a serve-time snapshot

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

## Review clearance — 2026-09-30

**Cleared.** Implementation `f1eebca` was approved after both review rounds; merge
`2c383e5` preserves that implementation. Subsequent source changes are comments only.
No C1 findings remain open. The checklist below records the reviewed implementation,
including the implementation details corrected during review; §0 records the historical
starting point, not prerequisites to run against the shipped database.

Independent verification in the final review: **88 targeted tests passed** across
deterministic scoring, scoring policy, verification-gate dispatch, and template-fixture
equivalence. The implementer reported both full gates green: **2,083 tests passed,
93.68% coverage**. Those full gates were not independently rerun for this clearance.

Two privacy gaps remain explicitly deferred to [Stage 7.4a](../stage7.md#74a-retention-the-assessment-path-cannot-yet-honour):
withdrawing stored assessment text and applying retention to learner text in rubric
`--input` payloads. This clearance covers C1's response and excerpt paths, not a claim
that all assessment text can be withdrawn or all rubric text is consent-filtered.

Invisible characters remain significant under `scoring.v1`. Explaining a mismatch can be
added without changing the policy; accepting additional forms requires a policy-version
change or reviewed pack answers.

## Reviewed scope

**Goal.** The core computes scores for `objective` and `short-response` tasks against an
immutable snapshot of the task as served. No model, no client, no server.

**Why it is shaped this way.** A served task already snapshots `task_type`, `level_code`,
`difficulty`, `content_family`, `modality`, `is_anchor`, `rubric_version`, `content_hash`
(migration 0016) and `target_refs_json` (0022). It does **not** keep the answer key, the
prompt, or the rubric body. This stage adds those three to the snapshot and nothing else.
`content_hash` is already the drift detector, which is why no row ever needs backfilling
from a changed pack.

The following describes the pre-C1 state and the reasons for the implementation:

- **The answer key is untyped today.** `PackAssessmentTask.expected` is `dict[str, Any]`
  and the only rule is that `objective` and `short-response` declare a non-empty one, so
  `{"answers": []}`, `{"answers": "yes"}` and `{"unrecognized": true}` all validate. A
  scorer reading that either raises or silently scores 0.0 — a learner's zero standing on
  a malformed pack. The key gets a type before anything scores against it.
- **The score's provenance lives on the result, not in the snapshot.** The policy that
  matters is the one that *computed* the score, which runs at `record` time; a serve-time
  copy would be a second version nobody can act on. So `scoring_policy_version` is a
  column on `assessment_results`, paired with `score_source`, because a caller-supplied
  score must never read as the scoring policy having run.
- **The response is input, not storage.** Scoring needs the learner's whole answer;
  retention decides what of it survives. The only response input `record` has today is
  `--excerpt`, which it persists unchanged with no consent check — scoring off that field
  would make automatic scoring require retaining text a track may have declined. The
  answer arrives as `--response`, is scored in memory, and reaches the database only
  through `retain_response`.

## 0. Confirm the starting point

- [ ] Verify the current column set, so the migration adds only what is missing:
      `SELECT column_name FROM information_schema.columns WHERE table_name='assessment_run_tasks'`
      Expect 15 columns. If it differs, stop and re-read this stage.
- [ ] Same for `assessment_results`: expect 14 columns, and none of
      `scoring_policy_version`, `score_source`, `response_visibility`, `response_hash`.
- [ ] Confirm the migration head is `0029_selected_clips.sql`, so the new file is `0030`.
- [ ] Confirm every `objective` and `short-response` task in `language-packs/` declares
      `expected` as `{"answers": [<non-blank strings>]}` — all three shipped packs do, so
      §2 tightens the contract without rewriting content. If one does not, fix the pack in
      this stage and re-stamp it.

## 1. Migration `0030_served_answer_key.sql`

- [ ] On `assessment_run_tasks`, the answer key as served:
  - `expected_json VARCHAR` — the key, exactly as the pack held it when served
  - `prompt_snapshot VARCHAR` — what the learner was shown
  - `rubric_json VARCHAR` — the rubric body (only `rubric_version` is kept today)
- [ ] These three are one group. A row has all three or none: none means the row predates
      this migration, and §7 decides what may still be read for it. Any other combination
      is damage, refused at read and reported by `db check` — a partial snapshot sends the
      reader back to the mutable bank the snapshot exists to replace.
- [ ] On `assessment_results`, what the score rests on:
  - `scoring_policy_version VARCHAR` — the policy that computed the score; `NULL` when
    none did
  - `score_source VARCHAR` — `computed` or `supplied`
  - `response_visibility VARCHAR` — `withheld`, `excerpt`, or `full`, the vocabulary
    `attempts` already uses
  - `response_hash VARCHAR` — the response's SHA-256, so a withheld answer can still be
    checked against one offered later
- [ ] Backfill the two columns whose answer is knowable from the row itself, so neither
      has a legacy hole a check has to guard around:
      `UPDATE assessment_results SET score_source = 'supplied'` — no computed path existed
      before this migration — and
      `response_visibility = CASE WHEN response_excerpt IS NULL THEN 'withheld' ELSE 'excerpt' END`,
      which states what is stored, not what was consented to. `0021` sets the precedent.
      Leave `scoring_policy_version` and `response_hash` null: inventing either would be a
      claim about work nobody did.
- [ ] All seven columns are **nullable and unconstrained**. DuckDB refuses `ALTER TABLE
      ADD COLUMN` with a constraint, so there is no `json_valid` on `expected_json` or
      `rubric_json` and no vocabulary check on `score_source` or `response_visibility`.
      Enforce them in the service layer and assert them in `db check` (§9); parse the two
      JSON columns defensively in Python wherever they are read.
- [ ] Write the file's intent in a header comment, as the other migrations do: why the
      answer key is snapshotted rather than read live, and why the score's provenance sits
      on the result rather than on the served row.
- [ ] Never edit a released migration. Add the new checksum to
      `tests/migrations/snapshots/released-migrations.json`; never remove a recorded one.
- [ ] Run `tests/migrations/test_migration_registry.py` and the upgrade fixture. If the
      DuckDB upgrade support script seeds these tables explicitly, add the new columns
      there too.

## 2. Type the answer key

- [ ] Add an `AnswerKey` contract model in `linguawiki/contracts.py`. `ContractModel` is
      already `extra="forbid"`, which is what refuses `{"unrecognized": true}`:
  - `answers: tuple[str, ...]`, at least one entry
  - every entry non-blank *after* stripping — `min_length=1` accepts `"   "`, which no
    comparison can ever match
- [ ] Type the field rather than the shape at the call site: `PackAssessmentTask.expected`
      becomes `AnswerKey | None`, and the existing
      `scored_tasks_declare_how_they_are_scored` validator keeps requiring it for
      `objective` and `short-response` and continues to require a rubric for the other
      three. A `json_schema_extra` note describing the shape would be invisible at
      runtime.
- [ ] Stored-key parsing happens in one function — `parse_answer_key(raw: str | None)` —
      used by the scorer and `db check`. Pack validation uses the same `AnswerKey` model
      through `PackAssessmentTask.expected`. The stored-key parser refuses with
      `assessment_answer_key_malformed`, naming which rule failed: not JSON, not an
      object, unknown key, `answers` not a list, empty list, non-string member, blank
      member.
- [ ] Revalidate on the way out of the database, not only on the way in. The snapshot is a
      round trip through JSON and can be hand-edited, damaged, or restored from another
      release between the serve and the score.
- [ ] Regenerate `schemas/lingua.pack.assessment.v1.json`
      (`scripts/generate_schemas.py`) and check the published file in. Add a contract test
      that a model-produced task validates against the checked-in schema — once with only
      required fields, once with every optional field set.

## 3. Populate the snapshot at serve time

- [ ] In `services/assessment.py` `next_task`, the `SELECT` that already reads
      `task.prompt`, `task.rubric_json`, `task.rubric_version` and `record.content_hash`
      (around the `sequence` computation) gains `task.expected_json`. Write all three new
      values from **that** row.
- [ ] Not from the selected `Candidate`. `Candidate` carries "everything selection is
      allowed to consider", and the answer key is not among it; putting the key there
      would let item selection see it.
- [ ] Extend the `SELECT` in `record()` that reads `assessment_run_tasks` to return the
      three new columns, so scoring reads the snapshot on the connection it already holds.

## 4. Scoring

- [ ] Add `SCORING_POLICY_VERSION` as a module constant beside the other policy values in
      `linguawiki/placement.py`. Bump it whenever the rules below change.
- [ ] Add a pure function — no database, no I/O — in `linguawiki/placement.py` beside
      `select_task`, taking the task type, validated answers resolved from the served
      record, and the response, and returning a score.
- [ ] One comparison serves both scorable types, and it is the whole rule: `fold` from
      `linguawiki/text.py` (NFKC, then case-fold), with surrounding whitespace stripped
      and internal runs collapsed to one space, applied to the response and to each
      accepted answer. Nothing else — no transliteration, no punctuation stripping, no
      diacritic folding. In an inflected or tonal language a dropped mark is a different
      word, and any form a pack wants accepted belongs in `answers` where a reviewer can
      see it.
- [ ] `normalize_identity` is the wrong tool here: it drops punctuation and separators, so
      it would score "nie wiem" and "nie, wiem" the same.
- [ ] A response that is empty or whitespace-only is refused with `invalid_arguments` and
      an `ErrorDetail` on `response`, not scored 0.0. A non-answer is a skip, and the run
      task already has a `skipped` status for it.
- [ ] Refuse `extended-productive`, `pronunciation-target`, `connected-speech` with
      `assessment_not_machine_scorable`; these need a judge.

## 5. The response is input, not a stored artifact

- [ ] `record()` gains `response: str | None` and `response_visibility: str | None`;
      `response_excerpt` stays for callers that have already excerpted.
- [ ] Scoring uses `response` in memory. Persistence goes through
      `evidence_service.retain_response(response, requested=..., preferences=...)` with
      the track's preferences, and what it returns is what is written:
      `response_visibility`, `response_excerpt`, `response_hash`. This is the boundary
      where the text arrives, so it is the boundary that applies the rule.
- [ ] A caller-supplied `--excerpt` goes through the same call, so the consent rule covers
      both routes into the column rather than the one this stage happened to add.
- [ ] Declined retention does not block scoring. `transcript_retention_consent = false`
      yields `withheld` with the hash kept and no text; the score still stands, because it
      rests on the comparison against the snapshot, and the hash is what lets a response
      offered later be checked against the one that was scored.
- [ ] Asking for `full` without consent is refused by `retain_response` with
      `transcript_consent_required` — reuse the existing code rather than inventing one,
      and let the refusal happen before the first write, so the task stays `served` and
      the caller can record it again.

## 6. Wire it into `record`

- [ ] When `assessor_kind` is `deterministic` and no `score` is supplied, compute it from
      the snapshot and record `score_source = 'computed'` with
      `scoring_policy_version = SCORING_POLICY_VERSION`.
- [ ] When a caller supplies a score, keep the existing behaviour and record
      `score_source = 'supplied'` with a null `scoring_policy_version`. The flag has
      always only *labelled* the score; a compatibility call must not read afterwards as
      the policy having run.
- [ ] Supplying both a `--score` and a `--response` for a machine-scorable task is
      accepted — the supplied score wins and is labelled `supplied` — but the response
      still goes through retention. Scoring in the background and discarding the result
      would make the stored score unexplainable.
- [ ] Every refusal in §4, §5 and §7 runs before the insert, inside the writer and before
      the transaction opens. A refusal must leave the task still answerable.

## 7. Tasks served before this migration

- [ ] If all three snapshot columns are null, the row predates this migration. If the live
      `assessment_tasks` row's hash equals the snapshotted `content_hash`, the content is
      provably identical and may be read from the bank; the result records which account
      it used.
- [ ] If the hash differs, refuse deterministic scoring with `assessment_score_required`
      and explain that a supplied score is required; leave the task answerable.
- [ ] If some of the three are present and some are not, refuse with
      `assessment_snapshot_partial` and do **not** fall back to the bank. A partial
      snapshot is damage, not a legacy row, and treating it as one lets the mutable pack
      answer for facts the record actually held.
- [ ] If `expected_json` or `rubric_json` is present but will not parse, refuse with
      `assessment_answer_key_malformed`. Neither has a `json_valid` constraint to have
      caught it.
- [ ] **No backfill from the pack, ever.** Write this as a comment at the branch.

## 8. CLI

- [ ] `linguawiki assessment record` gains `--response` and `--response-visibility`
      (`withheld` | `excerpt` | `full`), and `--score` becomes optional.
- [ ] Refuse the combinations that cannot be computed, each by its own code: no `--score`
      and no `--response`; no `--score` with a rubric-scored task type; no `--score` where
      §7 refuses the snapshot.
- [ ] `--response` is argv; a long written answer uses `--response-file <path>` (or `-`
      for stdin). Keep the same refusal for blank text on both paths. `--input` remains
      the rubric payload and is not a response input.
- [ ] Update `.agents/skills/linguawiki-assess/SKILL.md` so the skill stops supplying
      scores for machine-scorable tasks, and passes the learner's answer rather than an
      excerpt it chose.

## 9. `db check`

The schema cannot carry any of this, so each rule below is a named check that reads
defensively and never raises.

- [ ] `served_answer_key_complete` — classify the three snapshot columns through
      `contracts.served_snapshot_state`, shared with the scorer: all NULL is absent;
      all nonblank is whole; anything else is partial. Keep this separate from
      `served_snapshot_complete`: 0016's group and this one are independent, and a row
      may legitimately be whole in one and absent in the other.
- [ ] `served_answer_key_wellformed` — a non-null `expected_json` must parse as a
      supported answer key for a machine-scorable task, or a JSON object for a rubric-scored
      task (whose absent key is stored as `{}`). Every non-null `rubric_json` must be a
      JSON object. Report unreadable rows rather than aborting, including decoder
      `ValueError` and `RecursionError` failures.
- [ ] `result_score_provenance` — `score_source` is `computed` or `supplied` on every row,
      and `computed` holds exactly when `scoring_policy_version` is present. Check both
      directions: a version recorded against a supplied score is a claim nobody made.
- [ ] `result_response_retention` — `response_visibility` is one of the three on every
      row; `excerpt` and `full` have an excerpt present; `withheld` has none. The
      migration's backfill is what makes this total rather than guarded by `IS NOT NULL`.
- [ ] Every failure names the rows it found, not a count.

## 10. Tests — `tests/integration/test_deterministic_scoring.py`

- [ ] A correct and an incorrect objective answer score 1.0 and 0.0 without a model.
- [ ] Short-response normalization: case, NFKC (decomposed vs precomposed `ś`), leading
      and trailing whitespace, and a collapsed internal run all match; a dropped diacritic
      (`srode` for `środę`) and a differently punctuated form do not.
- [ ] An empty and a whitespace-only response are refused, and the task stays `served`.
- [ ] Malformed keys are each refused by name: `{"answers": []}`, `{"answers": "yes"}`,
      `{"unrecognized": true}`, a non-string member, a blank member, and unparseable JSON.
- [ ] **Pack drift**: serve a task, change the pack's `expected`, score — the score
      reflects the snapshot.
- [ ] A pre-migration row with a matching `content_hash` scores; one with a drifted hash is
      refused; one with a partial snapshot is refused as damage rather than read from the
      bank.
- [ ] Each of the three rubric-scored task types is refused by name.
- [ ] A computed result records `score_source = 'computed'` and the policy version; a
      caller-supplied one records `supplied` and no version.
- [ ] **Retention declined**: with `transcript_retention_consent = false`, a correct
      answer still scores 1.0, and the result holds `withheld`, no excerpt, and the hash.
- [ ] `--response-visibility full` without consent is refused and writes nothing: no
      result row, and the task still `served`.
- [ ] `db check` finds each of the four failures above from hand-made rows, and reports
      rather than raising on the unparseable ones.

## Gate

- [ ] Published schema regenerated and checked in; validate it with
      `./.tools/uv run python scripts/generate_schemas.py --check`.
- [ ] `./.tools/uv run python scripts/verify.py` — implementer-reported release-gate pass;
      independent review verification is recorded above.

## Done when

A calibration's objective and short-response tasks can be answered and scored entirely from
the CLI, the recorded provenance shows no model was involved and which policy computed the
score, no score rests on content the learner was not shown, and no score required keeping
words the track declined to keep.
