---
title: "C5 — Audio, and the claims that rest on it"
stage: C5
status: shipped
depends_on: [C4]
---

# C5 — Audio, and the claims that rest on it

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** Pronunciation and speaking become answerable in the browser, and no assessment claim
outlives the recording it rests on.

**Two hazards, both verified.** `assessment_results` has **no artifact column**, and
`artifacts.dependent_observations` finds dependents only through
`pronunciation_observations.audio_artifact_id` — so a purge today would delete the recording
and leave the score standing. And **nothing scans private roots for unregistered files**: a
recording written but not registered is accounted for by nothing. This repository already
names that failure — *"skipping it left the bytes sitting unregistered under an ignored
directory, accounted for by nothing"* (`services/artifacts.py`) — and Stage 5 closed it for
package ingestion. Do not reopen it through the browser.

## Shipped — 2026-10-02

Implemented on `c5-audio-and-evidence`. Release gate `scripts/verify.py` after the review round: **2521
passed, 1 skipped**, branch coverage 93.39% against a 90% floor, wheel and distribution checks clean.
`linguawiki privacy audit` and `db check` are clean on a scratch workspace driven through a
crash before promotion, a crash after the move, recovery, a supersession, a duplicate, a
verdict, and a purge.

Where it went differently from the plan, and why:

- **Pack assets are updated in place, not replaced.** `pack_assets` is keyed by content
  ID, which is derived from the asset key, so a recording kept across versions keeps its
  row. Deleting and re-inserting was the first version: DuckDB refuses a delete and an
  insert of one secondary unique key, `(pack_id, asset_key)`, inside a transaction.
  An unchanged reinstall writes the rows an install older than 0034 lacks, without
  rewriting content records.
- **A tampered pack recording is refused where it is read.** Serving no longer loads the
  pack, so the served-task read cannot fail with `pack_checksum_mismatch`; playback
  hashes the bytes it hands over and refuses `assessment_asset_changed`. The serve hashes
  the one file it is about to snapshot and keeps `assessment_task_revision_drifted`.
- **A pack recording stays out of `content_records`** (§0's open question): nothing in a
  learner's history is about it, its rights are the pack's, and a purge has nothing to
  decide about it.
- **Every capture refusal that can be decided from the request is decided before the
  first write**: consent, a closed run or settled task, a judged task, duplicate bytes, an
  occupied path. Those leave no staging row and no file. The staging row is for what can
  only fail later -- a crash, a hash that changed on disk, a registration the disk refused
  -- and those refusals remove only bytes whose hash identifies them.
- **The upload is its own route**, `POST /runs/{run}/tasks/{content}/captures/{capture}`:
  raw `audio/*` bytes with their own 16 MiB cap, keyed by the capture ID in the path. A
  recording is stored under `artifacts/captures/<track>/` with `rights="full-local"`.
- **The screen says three new things**: `answer_with: "recording"` for a spoken task,
  `state: "awaiting-judge"` with the `submission` once one is in, and the track's
  `recording` policy. Run discovery carries the policy too, because a page has to choose
  which run to start before it has one.
- **`assessment_task_already_judged` is checked before "settled"**, because a judged task
  is also answered and "the learner's answer was taken" is the reason a caller can act
  on. Likewise `assessment_result_invalidated` is checked before the run's own state, so a
  finalized run still says why the verdict cannot land.
- **New refusal codes on `record`**: `assessment_audio_artifact_required` (a recording
  answers this task; name it) and `assessment_judge_required` (a recording is judged by an
  `ai` or `human` assessor). `assert_judgeable`'s failures each keep their own code.
- **`dependent_observations` was left alone**; `withdrawal.dependent_results` sits beside
  it, and `purge --dry-run` reports both. Purge is split into `write_purge`, which a
  supersession runs inside the transaction that binds the new recording.
- **A purge also withdraws a pending submission** bound to the recording, rather than only
  `record` doing so when the verdict arrives: a pending submission resting on nothing is
  damage `db check` reports. The sweep withdraws one whose run closed unjudged with code
  `assessment_run_closed`.
- **Annotations are their own append-only table**, `estimate_annotations`, rather than
  columns on `estimate_history`, so history stays exactly as it was written.
- **The withdrawn-mode fallback** is the newest finalized run that still measured the
  dimension (by the run's own chronology), else the declared hypothesis, else
  `not-tested`.
- **The disk half of `db check`** -- staged bytes nothing accounts for, and verdicts whose
  recording is no longer the one judged -- runs in `services/database.check`, which has the
  workspace root; `check_database` is given only a connection.
- **A browser test records through Chromium's fake microphone**
  (`tests/browser/test_client_audio_browser.py`), beside the HTTP and service tests.

Closed in the review round, each with a test that failed before its fix:

- **Pausing stopped the screen and not the microphone.** The page now stops the recorder
  and every track whenever it leaves the task being recorded -- pause, a refusal, a settled
  task, a stale token -- and a browser test asserts the track has `ended`.
- **Recovery could move a capture over a file it could not identify.** `_move` itself now
  requires the staged bytes to be contained and to hash to the capture, and the destination
  to be empty; otherwise the capture is refused as `capture_path_occupied` and the
  unrelated file is left where it is.
- **A `machine+recorded` run took a spoken verdict with nothing submitted**, which made a
  result no purge could reach. It is refused as `assessment_recording_required`.
- **Recovery reported a capture as refused when its cleanup had failed** and the row was
  still `staged`, so the server started over it. Recovery re-reads the row and refuses to
  start with `capture_unresolved`.
- **A verdict repeat ignored the recording it named.** The stored result's
  `audio_artifact_id` is part of the repeat comparison, so naming another recording -- or
  none -- is `assessment_result_conflict`.
- **The page promised a deletion the sweep never makes.** Under `delete-after-ingestion` a
  recording made in the page is not swept, and the page now says it is kept until the
  learner removes it; under a rolling window, that it is not removed before a judge hears it.

Deferred:

- The pilot's six listening recordings (§0) are still not produced. They need real
  recordings with a rights and origin class, and this repository holds only synthetic
  audio.
- Evidence recorded separately with `evidence record --assessment-run --task` for a judged
  spoken task is not withdrawn when the recording is purged. `record` writes no evidence,
  and a test pins that, but this path writes it.
- A pending submission in a run that is finalized or abandoned stays pending until a sweep
  lapses it; neither command withdraws it.
- A recording made in the page and not yet acknowledged lives in memory only, so a reload
  loses it and the learner records again. A capture already registered is not lost.
- One run of the browser suite during the stage failed three C4 tests that passed on every
  later run (39 of 39 across three repeats). It coincided with the disk filling: the suite
  leaves about 50 GB of pytest temp per run, and an earlier gate run stalled and was
  killed for the same reason. Unexplained beyond that, and worth watching.

## 0. Inherited from C2 — pack-shipped recordings

C2 built the pack side of audio and left two things here, deliberately.

- [x] **Install pack assets into a table.** `served_task` resolves a served recording by
      loading the whole pack from `pack_installations.source_path` to read one digest.
      That is correct -- what comes back is checked against the snapshotted digest rather
      than trusted -- and it was cheap while no pack shipped audio. Once one does, every
      audio task read pays for a full `load_pack`. Store `(content_id, path, sha256,
      media_type, duration_ms)` at install time and resolve from there, keeping the
      digest comparison: the point was never where the file is, it is whether the bytes
      are the ones the learner heard.
- [x] **Decide whether an asset becomes learner content.** C2 deliberately keeps a
      recording out of `content_records`: its `content_kind` is a closed CHECK on the
      most-referenced table in the schema, DuckDB cannot re-constrain it, and rebuilding
      it for zero rows was not worth doing while nothing in a learner database referenced
      a recording. `LoadedPack.installable_items` is where that split is stated. If C5
      wants `db check`, a rights audit, or a purge to reason about pack audio from the
      database, that is the migration to weigh.
- [ ] **The pilot's six listening recordings.** `pl-pilot` 0.2.0 ships none, so its six
      audio tasks still carry the spoken sentence in the prompt and declare no asset --
      what they measure today is reading a sentence somebody transcribed. The contract,
      the catalog, the validation and the refusals are all in place and tested;
      `tests/language_packs/test_pilot_presentation.py` pins the gap so it stays visible.
      Producing the recordings, with their rights and origin class, is what closes it.

## 1. Capture, with consent first

Two different facts gate capture, and they live under different keys in the track's
preferences. **Consent** is `audio_retention_consent`; **equipment** is
`audio_recording_available` (and the older `voice_available`). `_available_modalities`
(`services/assessment.py:647`) adds `speech` from the equipment flags alone, which is right for
what it answers and wrong as a consent check. Nothing in C5 may gate on availability and call it
consent.

- [x] Press to start, press to stop. **No countdown.**
- [x] Capture is offered only when `audio_recording_available` is true **and**
      `audio_retention_consent is True`. Retention consent is the gate, not an extra:
      `register` deletes a non-retained recording at the door, and a judge needs the
      recording to still exist. A track without both never reaches the point of persisting
      bytes, so it never produces a file to clean up.
- [x] Surface the track's retention policy (`keep`, `rolling-days`, `delete-after-ingestion`)
      on the screen; never override it.

## 2. Two-phase capture with an owner

Registration can fail — `writer_locked`, unknown track, hash mismatch, retention conflict —
and these bytes were created by the client rather than supplied by the learner.

`artifacts.register` *records that a file exists, without moving it*. It is not a promotion
and must not become one: every other caller relies on it registering the path it is given.
The move is owned by a new service, `services/recordings.py`.

**Identity.** A capture is a learner's own recording, not something a package brought in, so
it is registered with `origin="learner-recording"` and **`external_id` left null**.
`artifacts.sweep` reads a non-null `external_id` as "arrived with a package" and deletes it
under `delete-after-ingestion` (`services/artifacts.py:1622`); a capture ID there would have the
sweep remove browser recordings under a policy that is not about them. The capture ID lives on
`capture_stagings` and on the submission (§3), never in the producer-identity column.

- [x] **Staging root.** A new private directory, `staging/captures/`. It is outside
      `ARTIFACT_ROOTS` (`artifacts`, `imports`), so `register` cannot be pointed at a staged
      file by mistake. Adding it means: `paths.PRIVATE_DIRECTORIES`, the workspace template's
      ignore list, `workspace` creation, the privacy audit's candidate logic
      (`services/privacy.py:_filesystem_candidates`), and a `db check` that reports any file
      under it that no `capture_stagings` row names.
- [x] **Staging.** A `capture_stagings` row is written first: capture ID (unique), track, run,
      served `content_id`, staged path, expected sha256, byte count, final relative path, and
      state `staged`. The file is written only after the row commits, so the bytes are
      accounted for from the moment they exist.
- [x] **Promotion, in this order.** (1) Hash the staged file with `artifacts.digest_or_none`
      — after `classify_path` says it is `present` — and compare with the recorded sha256; a
      mismatch is refused. (2) Set the row to `promoting` in its own short transaction,
      naming the final path. (3) `os.replace` the staged file to the final path (same
      filesystem, atomic). (4) Register and bind **in one transaction**: split `register`
      into a `plan_registration` / `write_registration(database, …)` pair, as the session
      close does, and run `write_registration`, the staging row's move to `registered`
      (naming `artifact_id`), and the submission insert (§3) in the same transaction. From
      that commit the artifact row owns the bytes and the staging row is history; before it,
      the staging row owns them wherever they are. Because the three writes commit together,
      "registered but the staging row does not know" is not a state recovery has to guess
      about — `db check` reports it as damage.
- [x] A refusal **removes the bytes and reports why**: the staging row goes to `refused`
      with the error code, and the file at whichever path the row names — staged or final —
      is removed after its hash is checked. A file whose hash no longer matches is not
      deleted; it is reported, per the rule against deleting what you cannot identify.
- [x] **Same bytes, two captures.** `artifacts_content` is unique on `(track_id, sha256)`,
      so two byte-identical captures (a silent clip from a deterministic encoder) collide.
      The second is refused as `capture_duplicate_bytes`, naming the existing artifact; its
      staged file is removed only after its hash is confirmed to be that artifact's, and the
      way forward is to record again. It never binds to the first capture's artifact.
- [x] Retry `writer_locked` and `database_busy` before abandoning: they are transient and the
      learner has already spoken. The bytes stay `staged` (or `promoting`) during the retry.
- [x] **Recovery** — `recordings.recover`, run by `client serve` before it accepts a request.
      It needs the writer lock: it retries `writer_locked` for a bounded interval and then
      refuses to start with `writer_locked`, naming recovery as what it was waiting for —
      serving requests over unresolved captures is the guess this section exists to prevent.
      `db check` runs the same classification as a diagnostic that reports and never
      repairs. Every non-terminal row resolves to `registered` or `refused`:
      - `staged`, file present with the right hash → promote, or refuse if the capture's run
        is no longer running; file absent → `refused`, nothing to remove.
      - `promoting`, file at the staged path → the crash was before the move; resume from (3).
      - `promoting`, file at the final path → the crash was between the move and the
        registration transaction; resume from (4).
      - `promoting`, file at **neither** path → with `os.replace` this means a deletion from
        outside; resolve to `refused` with a finding naming the capture, never fall through.
      - A file under the staging root that no row names is a finding, never silently deleted.
- [x] **Idempotent upload.** The client sends the capture ID it generated. A re-sent upload
      for a capture already `registered` returns its artifact and submission — this is the
      "registration committed, response lost" case — and registers nothing again. The same
      capture ID with different bytes is a conflict naming the recorded hash.

## 3. Reaching a judge

C4 starts browser runs with `scoring: "machine"`, which never serves a task needing a judge
and excludes `speech`; `record` refuses an unscored answer to a judged task
(`assessment_score_required`). C6 builds queue mechanics but supplies no judge, and depends on
C5. So without this section, the browser acceptance case is unreachable. C5 supplies the
**synchronous, minimal** path; C6 generalizes it into durable asynchronous submissions.

- [x] **Eligibility.** A new scoring condition, `"machine+recorded"` (name to settle in
      `placement.py` beside `SCORING_CONDITIONS`). A candidate is servable if either:
      - it is servable under `"machine"` — machine-scorable task type, available modality,
        and, for `audio`-modality tasks, **the same recorded-asset check C4 applies**; or
      - its modality is `speech`, its task type is one a recorded judge can score, and the
        track satisfies §1 (`audio_recording_available` and `audio_retention_consent is
        True`) — not merely `_available_modalities`.
      A start that empties a dimension names the filter, as C4 does. The page starts runs
      with this condition when §1 holds and with `"machine"` otherwise.
- [x] **Where the binding lives.** A new table, `assessment_submissions`: `submission_id`,
      `run_id`, `content_id`, `capture_id` (unique), `artifact_id`, `status`
      (`pending` | `judged` | `superseded` | `withdrawn`), `superseded_by`, `created_at`.
      **At most one non-superseded submission per `(run_id, content_id)`.** `status` is
      mutable, so that rule is a named `db check`, not a unique index (a unique index over a
      mutable column has the effect of a foreign key — see the repository invariants). The
      row is inserted in the registration transaction of §2 (4). `/screen`, `pending`,
      `assert_judgeable`, and `record` all read the binding from here.
- [x] **A second capture for the same served task.** Before a verdict, it **supersedes**:
      the new submission is inserted, the old one goes to `superseded` naming its successor,
      and the old recording is purged in the same operation — it is evidence of nothing, and
      keeping it would be retention nobody asked for. After a verdict, a new capture is
      refused as `assessment_task_already_judged`; the learner's answer was taken.
- [x] **Submission.** Answering a speech task in the page *is* the §2 capture. Registration
      does not score: the served task stays `served`, and `/screen` reports it as
      `awaiting-judge` with the submission's artifact ID. The one-outstanding-task-per-
      dimension guard (C3) keeps that dimension waiting; other dimensions keep serving.
      Surfacing *waiting* properly is C6; C5 only has to not hide it.
- [x] **Delivery to a judge.** New command `linguawiki assessment pending --run …` lists
      pending submissions with the artifact, its contained path, the snapshotted task
      (prompt, rubric, dimension), and the track's consent. The judge is the
      `linguawiki-assess` skill (an `ai` assessor) or a human; it reads the audio through
      that contract, never by guessing a path.
- [x] **Verdict.** `assessment record --content … --score … --audio-artifact …
      --rubric … --assessor-kind ai|human --assessor …`. `--audio-artifact` is a **new**
      flag; `--rubric` is new on the CLI (the service already takes `rubric`). The artifact
      must be the one bound by the live submission; a different one is refused with the
      bound one named. The submission moves to `judged` in the result's transaction.
- [x] **What an `ai` verdict may claim.** Nothing holds it to anything today: `record`
      accepts any `confidence` for any `assessor_kind`, and `provenance.MACHINE_CEILING`
      caps *content review* axes, not scores, so it does not apply. C5 defines the rule,
      in `evidence.py` beside the other claim rules, with a version string stored on the
      result: a score on a pronunciation or spoken-production task from an `ai` assessor
      **must name the assessor** (`assessor_required`) and **may not exceed `medium`
      confidence** (`assessor_confidence_ceiling`). A `human` assessor must also be named.
      Whether an `ai`-judged result should carry a weaker basis into the estimate is
      deliberately left out of C5; if it is wanted, it is a policy version, not a patch.
- [x] Skill change: `linguawiki-assess` gains the pending → listen → record loop and
      orchestrates only those CLI contracts.

## 4. Result-to-artifact relationship

- [x] Migration: `assessment_results.audio_artifact_id`, `invalidated_at`,
      `invalidated_reason` (mutable columns on a referenced table — no foreign key; pair
      with an `integrity.ORPHAN_RELATIONS` entry and a named `db check`), plus
      `capture_stagings` and `assessment_submissions`.
- [x] Teach `artifacts.dependent_observations` to find assessment results as well as
      pronunciation observations, and make `purge --dry-run` report both.
- [x] **One evidence check, in the shared recording service** —
      `recordings.assert_judgeable(database, root, *, artifact_id, run_id, content_id)`,
      used by `assessment pending`, by `record`, and by `db check`. "Present and retained" is
      not enough; an artifact satisfies a pronunciation or spoken-production score only if:
      - it belongs to the **run's own track** (resolved through `assessment_runs`, never the
        caller's track);
      - its `kind` is `audio` — a transcript artifact cannot carry an acoustic claim;
      - it is **retained** and not purged or tombstoned;
      - `artifacts.classify_path` says `present`, and `digest_or_none` of its bytes equals
        the registered sha256 — present is not "the file we registered";
      - it is the artifact of the live submission for `(run_id, content_id)`.
      Each failure keeps its own error code.
- [x] **Revalidate at commit.** `record` runs `assert_judgeable` inside the writer
      transaction that inserts the result, after the judge's verdict arrived — not only when
      `pending` handed the task out. A purge, an altered file, or a retention change while
      the judge was listening refuses the verdict with the reason. The submission goes to
      `withdrawn` and the served task is set to **`skipped`** —
      `assessment_run_tasks.status` holds only `served`, `answered`, `skipped`, and DuckDB
      cannot alter that CHECK, so there is no "unjudgeable" status; the reason lives on the
      submission. Because purge and record both take the writer lock, one of them is first
      and the other sees its result.
- [x] **Teach the retention sweep about captures.** `sweep` holds back only clips that
      support confirmed pronunciation observations; it never consults assessment results.
      - A recording bound to a **pending** submission is held back under every policy, with
        a warning naming it: deleting a recording a judge has not yet heard discards the
        learner's answer, and the run is still live. Once the run is no longer running the
        hold lapses and the submission is `withdrawn`.
      - A recording a **judged** result rests on is treated as `supporting` evidence: due
        under its policy like any whole recording, named in the warning, and removed through
        `purge`, so §5 invalidation runs exactly as it does for an explicit purge.
      - `sweep --dry-run` reports the assessment results it would invalidate.

## 5. Invalidation

- [x] The result is **marked invalidated, not deleted** — a learner told their vowel was wrong
      deserves to see the evidence is gone.
- [x] **An invalidated task is settled.** `assessment_results` is unique on
      `(run_id, content_id)` and `_assert_repeat` compares a new verdict with the stored row,
      so an invalidated task cannot be re-judged in that run, and C5 keeps it that way: a
      fresh measurement belongs to a fresh serve. A verdict for it is refused as
      `assessment_result_invalidated`, not reported as a conflicting repeat.
- [x] **Every reader of `assessment_results` decides about `invalidated_at`.** Exclude
      invalidated rows from: the run's posterior replay (below); `finalize`'s inputs; the
      run report's `tasks_recorded` (`services/assessment.py:2657`), which reports
      invalidated results as their own count. Keep them, deliberately, in:
      `_assert_repeat` (`:1447`), so an invalidated task stays settled; and the `db check`
      integrity readers (`db/integrity.py:985`, `:1114`–`:1156`, the `served_answer_key`
      relation at `:2734`), which check every row's consistency whatever its state.
      `record` writes no `evidence` rows, so `estimates.dimension_observations` needs no
      filter today — and a test pins that, so a later change that adds them must add the
      filter too.
- [x] The run's dimension posterior is **replayed** from its surviving results, starting at
      the prior the run recorded for that dimension.
- [x] **The current estimate is rebuilt, not preserved.** `estimates.recompute_dimension`
      today returns *"no usable evidence arrived; the existing estimate stands"* when nothing
      usable remains, so purging a dimension's only recording would leave its old level in
      `skill_estimates`. A recompute triggered by invalidation must instead rebuild the
      current estimate from what survives — and, when nothing survives, write it back to
      `not-tested` (or to the declared hypothesis, if one is recorded, never higher), with a
      factor naming the withdrawn evidence. Add an explicit mode (e.g.
      `reason="evidence-withdrawn"`) rather than changing the default, whose
      no-downgrade rule is right for ordinary recomputation.
- [x] Prior estimates stay in `estimate_history` as they were, and the snapshots derived
      from the withdrawn result are **annotated** as resting on withdrawn evidence — history
      is marked, never rewritten. A finalized estimate derived from it says so too.
- [x] **Baseline selection must preserve assessment chronology.** `estimates.py` picks a
      baseline with `ORDER BY state.updated_at DESC, state.run_id DESC`. Recomputing an older
      run after a purge refreshes `updated_at` and would let it displace a newer calibration.
      Order by the run's own chronology instead.

## 6. Tests — `tests/integration/test_client_audio.py`

- [x] Audio recorded through the client lands as an artifact `artifact verify` reports present,
      with `external_id` null and a submission naming its capture ID.
- [x] **End to end through the judge**: a `"machine+recorded"` run serves a speech task, the
      page uploads a capture, `/screen` reports `awaiting-judge`, `assessment pending` lists
      it, a verdict via `assessment record --audio-artifact` lands, and the dimension updates.
- [x] Eligibility: a track with `audio_recording_available` but no `audio_retention_consent`
      is never served a speech task and never offered capture; an audio-modality task with
      no recorded asset stays unservable under `"machine+recorded"`.
- [x] An `ai` verdict without `--assessor`, or above `medium` confidence, is refused with its
      code; the stored result carries the policy version.
- [x] A second capture before the verdict supersedes the first and purges its recording; a
      capture after the verdict is refused as `assessment_task_already_judged`.
- [x] Two byte-identical captures: the second is refused as `capture_duplicate_bytes` and
      leaves no unaccounted file.
- [x] Purging it invalidates every assessment result resting on it, and the affected estimates
      say so; a verdict for the invalidated task is refused as `assessment_result_invalidated`.
- [x] **Only recording purged**: the dimension's current `skill_estimates` row returns to
      `not-tested` (or the declared hypothesis), and its earlier history rows survive,
      annotated.
- [x] **Sweep**: under `delete-after-ingestion` a capture is not swept; under `rolling-days` a
      pending capture is held back with a warning, and a judged one is removed through
      `purge` with its result invalidated.
- [x] Recording a score is refused against audio that is: absent; not retained; another
      track's; a `transcript`-kind artifact; altered on disk (hash mismatch); a symlink
      escaping the workspace; not the artifact of the live submission.
- [x] **Purge during judging**: `pending` hands out the task, the artifact is purged, then
      `record` is refused, no result exists, the submission is `withdrawn` and the task
      `skipped`.
- [x] **Two-run purge regression**: an older run recomputed after a purge does not become the
      baseline over a newer calibration.
- [x] **Failure paths, beside the success path** — registration refused after the bytes were
      written; disconnection mid-capture; restart with staged bytes present; **crash after
      the move and before the registration transaction**; **crash after registration
      committed and before its response** (a re-sent upload returns the bound artifact and
      registers nothing twice); `promoting` with the file at neither path. After each, assert
      **no file exists under a private root that no row accounts for**.
- [x] `client serve` with a CLI holding the writer lock retries, then refuses to start with
      `writer_locked` rather than serving over unrecovered captures.
- [x] A track forbidding recording never reaches the point of writing a file, and its runs
      never serve a speech task.

## Gate

- [x] `./.tools/uv run python scripts/verify.py` — the release gate, because C5 adds a
      migration and changes a published contract
- [x] `linguawiki privacy audit` on a scratch workspace after the failure-path tests

## Done when

A pronunciation task is answered in the browser, judged, purged afterwards, and the resulting
state is honest about what it no longer knows — with nothing left on disk that nothing accounts
for.
