---
title: "C6 — Submission lifecycle and round-batching"
stage: C6
status: ready
depends_on: [C4, C5]
---

# C6 — Submission lifecycle and round-batching

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** Rubric-scored work stops blocking, and round-trips drop roughly sevenfold — neither
at the cost of sequential adaptivity.

**Prerequisite already shipped.** The one-outstanding-task-per-dimension guard is in C3. C5
shipped `assessment_submissions` (0034), supersession with purge, `withdrawal.withdraw_submission`,
`assessment pending`, and `record --audio-artifact` re-checking the recording inside its writer.

**What C6 does not add.** A judge. Core never judges: the client server is a single-threaded
`HTTPServer`, a writer is one lock over one DuckDB file, and judging takes seconds to minutes.
C6 extends the C5 *external-judge* interface into a durable, leased, idempotent protocol. The
judge stays outside core (the assess skill, or anything else that speaks the CLI contract) and
holds **no** database connection while it judges.

## 1. Waiting is a state, not a gap

- [x] An explicit **waiting** state, distinct from open and closed, reported per dimension and
      for the run by `assessment report`, `assessment screen`, and `/screen`. It is a `progress`
      field, not a new stored `status` value (`status` is stored and compared by the page):
      a dimension is `open`, `waiting`, or `closed`; the run is `working`, `waiting`,
      `complete` (nothing left and no judgement owed), or `closed`. `next` returns the run
      report rather than a task while a run is `waiting`, so a report is not the signal to
      finalize -- `progress == complete` is.
- [x] Sequential adaptivity makes *some* waiting unavoidable: when every remaining dimension is
      blocked on a judgement, there is nothing sound to serve. The requirement is that waiting
      is **never hidden**, not that it never happens.
- [x] While any dimension is eligible, the learner keeps working.
- [x] Waiting says what it is waiting *on*: per submission, `unclaimed since <t>`,
      `claimed by <judge> until <t>`, or `held until the run resumes` (§6). A queue nobody
      has picked up looks different from one being worked. A third derived state, `lapsed`,
      names an answer whose attempts have run out and which no writer has settled yet; it
      blocks nothing.

### 1a. How the page learns a verdict arrived

Today `draw()` (`src/linguawiki/client/static/app.js`, the `!free && waiting.length` branch)
shows the wait and never asks again, so a judge can unblock a dimension while the page keeps
waiting.

- [x] **Polling, not notification.** While the screen reports anything outstanding, the page
      re-reads `GET /runs/{id}/screen` on a backoff (2 s, doubling to a 30 s cap; reset on any
      change). The server is single-threaded and has no push channel; a long-poll would hold
      the only handler.
- [x] **Change detection by diff, not a counter.** Submission status changes are in-place
      updates, so there is nothing to count. `/screen` already reports dimension statuses and
      awaiting-judge tasks; it gains an `outstanding_judgements` list (`submission_id`, `kind`,
      claim state, held verdict yes/no; `outstanding` was already taken by the
      next-tasks-owed field the page reads) and the `progress` fields above. The page compares
      that list, the progress fields, and the dimension statuses with what it drew; equal means
      no redraw.
- [x] **A poll never touches the task the learner is on.** `draw()` rebuilds the whole DOM and
      discards a live recorder when the answerable task changes. When a task view is showing,
      a changed poll updates only the progress bar and the waiting note; the task view node is
      kept, generalizing the existing `keepPlayer` precedent to the whole view (typed text,
      a live recorder, a playing asset). A dimension the poll found open is served when the
      learner finishes the current task, through the normal post-answer draw. A full redraw from
      a poll happens only from the waiting branch, where there is no task view to lose.
- [x] Polling pauses on `visibilitychange` → hidden and resumes immediately on visible. It stops
      when nothing is outstanding, the run leaves `in-progress`, or the page is refusing (no loop
      on a repeating error — the existing rule for serving).
- [x] A poll answered `busy` (writer lock held — typically by the judge's own `record`) keeps
      the current screen and retries at the next tick; it never shows the database-waiting
      status to the learner.

## 2. Durable submissions

- [x] C5's `assessment_submissions` row **is** the durable record; C6 adds no parallel queue
      identity. Everything queued — a claim, a verdict, a held verdict — names `submission_id`,
      never `(run_id, content_id)`, because a `(run, content)` pair outlives the recording that
      supersession purged.
- [x] **Retention is applied before the submission is persisted, not after.** Session staging
      already resolves this through `evidence_service.retain_response(response, requested=…,
      preferences=…) -> (visibility, excerpt, digest)`. Assessment recording inherits that
      boundary in C1. Submission, verdict, and every retry record store the retained form only.

### 2a. Judged written answers

Today the only way to hand in a judged task is `record`, which refuses one without a score
(`assessment_score_required`). C6 adds a submit operation.

- [x] **Eligibility, as a predicate.** A judged text task is servable under the async scoring
      condition only when `transcript_retention_consent is True` **and** `retain_response` would
      keep the full text (`visibility = full`). An excerpt-only track — the default, which keeps
      `EXCERPT_LIMIT` (240) characters — would have long writing judged on a fragment, so it is
      excluded, and the start names the filter exactly as C5's `machine+recorded` does for speech
      without audio consent. The scoring condition that admits written answers is
      `machine+judged` (it serves what `machine+recorded` serves and judged written tasks too);
      `machine+recorded` is unchanged and still excludes writing. `GET /runs` reports
      `written_offered`, the server's own eligibility, so the page picks `machine+judged`
      without recomputing it. Synchronous in-request judging is rejected: it would hold the only
      HTTP handler and the writer for the length of a judgement.
- [x] **Operation.** `POST /runs/{id}/tasks/{content_id}/submission` with
      `{"submission_key": …, "response": …}`, and `assessment submit --run … --content-id …
      --submission-key … (--response … | --response-file …)`. One writer transaction: preflight
      (task served in this run, judged text type, not already judged or submitted, eligibility
      still holds), `retain_response`, insert the submission. Registration does not score: the
      task stays `served` and reports `awaiting-judge`, exactly as a recording does.
- [x] **Identity.** `submission_key` is the producer's identifier — the page generates it per
      answer, as it does a capture ID — and is stored in `capture_id`, which the rebuilt table
      requires for both kinds. So the existing unique index deduplicates a resent answer with no
      new mechanism. Request hash: `run_id`, `content_id`, `kind`, and the canonical hash of the
      full response. Same key and hash → replay the submission; same key, different hash →
      `idempotency_conflict` naming the recorded digest.
- [x] **No text supersession.** A second text submission for a task already submitted is refused
      `assessment_task_already_submitted`. A typed answer is handed in deliberately, unlike a
      retaken recording, and keeping one live text per task avoids clearing text in place.
- [x] **What the judge and the page see.** `pending` and `claim` return `response_text` (the
      retained full text) where a recording returns `audio_path`/`media_type`/`sha256`;
      `PendingJudgement` gains `kind`. `/screen` reports the task `awaiting-judge` with
      `kind: text`, and never the text itself — the page already has it, and the read model
      does not carry a learner's words. The verdict row for a written answer stores no text
      either: it is `withheld` with a NULL excerpt and the submission's digest, and applying
      it re-reads the submission's own text and retains against the preferences then in force,
      so the submission is the one scrubbable copy. The judge's own request to keep less is
      held in `assessment_verdicts.requested_visibility`, separate from
      `response_visibility`, which records what the row kept.
- [x] **Migration 0035 rebuilds `assessment_submissions`** (no foreign key references it):
      `kind` (`recording` | `text`); `capture_id` required for both; `artifact_id` required iff
      `recording`; `response_visibility`, `response_text`, `response_digest` required iff
      `text`; existing rows become `recording`. Each rule a CHECK on the rebuilt table plus a
      named `db check` for a restore that predates it. The rebuild recreates
      `assessment_submissions_capture` (unique on `capture_id`). DuckDB accepts repeated NULLs
      under a unique index (checked against the pinned version), but with `capture_id` now
      required for both kinds nothing relies on that.

## 3. Processing: who dispatches, retries, and gives up

The judge pulls; core keeps the ledger. Derived status (claimed, expired, exhausted) is computed
from rows, never stored.

- [x] **Dispatch — `assessment claim --run … --judge <name> [--lease 600] [--limit n]`.** One
      short writer transaction inserts a `judging_claims` row per submission (`claim_id`,
      `submission_id`, `judge`, `claimed_at`, `lease_expires_at`) and returns, per claim,
      everything `pending` returns plus `claim_id`. The connection closes before the judge
      starts. Only `pending` submissions with no live lease are claimable; `pending` stays the
      read-only listing and now shows lease and attempt count.
- [x] **Verdict — `assessment record --submission <id> --claim <id> --idempotency-key <claim_id> …`**
      (§5). The assess skill passes the claim ID as the key, so each attempt has its own key and
      the request hash binds it to the verdict's content. A verdict on an expired lease is still
      accepted if nothing else has happened to the submission — the lease schedules, it does not
      decide correctness — and of two claims' verdicts the first to commit wins; the second is
      `assessment_verdict_conflict` unless its content is identical (§5).
- [x] **Retry.** `assessment release --claim <id> --reason …` returns a submission to the queue
      (judge crashed, timed out). An expired lease is the same thing, derived from time with no
      write. Attempts = claims made for the submission.
- [x] **Terminal failure.** `assessment release --claim … --terminal --code <code> --reason …`
      withdraws the submission through `withdrawal.withdraw_submission` (task skipped, dimension
      unblocked, reason on the submission). Exhausting `JUDGING_POLICY.max_attempts` (= 3; versioned
      constant, version stored on the withdrawal) withdraws with `assessment_judging_exhausted`.
      **This costs the learner an observation for a judge's failure**, deliberately: the
      alternative is a submission nobody can claim holding its dimension forever. A judge's
      `--code` may not borrow one the system uses for its own withdrawals
      (`assessment_release_code_reserved`).
- [x] **Who settles an expired last attempt.** No reader writes and there is no daemon, so
      `settle_lapsed(database, transaction, run_id)` runs inside every writer that touches the
      run — `claim`, `submit`, `record`, `release`, `set_status`, `finalize`, serving — before its
      own work, in its own transaction, and the writer names what it withdrew in its report's
      `warnings` (or in the refusal's details if it then refuses). `db check` reports a pending submission whose attempts are exhausted and
      unsettled, so a workspace nobody has touched since says so.
- [x] **Restart recovery.** Nothing is held in memory: claims, leases, verdicts, and outcomes are
      rows, and the next invocation reads them. A server or judge restart is the expired-lease
      case.

## 4. The submission lifecycle

The status set stays `pending | judged | superseded | withdrawn`. C6 adds the transitions that
can happen while a judgement is in flight, and the rule that every verdict is revalidated against
its submission **at the moment it is applied**, not when it was claimed or received.

| Between dispatch and application         | Effect on the submission                                         | A verdict naming it                                              |
| ---------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------- |
| Learner records again (supersession, C5) | `superseded`, names successor; recording purged                  | refused `assessment_submission_superseded`, naming the successor |
| Recording purged (learner, sweep)        | `withdrawn`, `assessment_audio_purged` (C5)                      | refused `assessment_submission_withdrawn`, with the code         |
| Audio-retention consent withdrawn        | purged → `withdrawn`, `assessment_audio_purged`                  | refused, same                                                    |
| Transcript-retention consent withdrawn   | text submission `withdrawn`, `assessment_response_not_retained`  | refused, same                                                    |
| Run abandoned                            | `withdrawn`, `assessment_run_abandoned`                          | refused, same                                                    |
| Run finalized excluding it (§6)          | `withdrawn`, `assessment_run_finalized`                          | refused, same                                                    |
| Run paused                               | unchanged, `pending`                                             | **held** (§6), applied on resume after revalidation              |

- [x] **Consent withdrawal has one home.** `learners.update_track` stays a preferences upsert; in
      the same writer transaction it calls a new `withdrawal.on_consent_change(database,
      transaction, track_id, before, after)`. Audio consent turning off purges every `pending`
      recording submission's artifact through the existing `artifact_service.write_purge` — file
      deletion inside the transaction that records it, as every purge already does — and the
      purge withdraws the submission with **`assessment_audio_purged`**. One event, one code;
      the purge's `reason` is `learner-request` and its detail, like the submission's withdrawal
      reason and the audit entry, says `consent withdrawn`, so the why is not lost. Transcript
      consent turning off withdraws pending text submissions and clears their `response_text`
      in the same statement (the row is already mutable; the digest stays).
- [x] **Revalidation at application** is `plan_verdict` (§5) and checks: the submission is
      `pending` and is the run's live submission for that task; its run belongs to the track
      being written; for a recording, `assert_judgeable` passes and the bytes match the sha the
      claim handed out; for text, eligibility (§2a) still holds; the claim, if named, is for this
      submission. A held verdict applied on resume goes through the same function.
- [x] A superseded submission's open claims and held verdicts end with it: the claim's verdict is
      refused and the held verdict voided, each naming the successor. The successor is claimable
      immediately.

## 5. Applying a verdict: one commit, one key

`record` today opens its own writer and transaction, so nothing else holding a writer can call
it. C6 splits it, following the `plan_*`/`write_*` pattern `session close` uses.

- [x] **`plan_verdict(database, request) -> VerdictPlan | Settlement`.** It reads, and it refuses,
      but it is **not** purely read-only, and C6 keeps C5's reason why: when the recording itself
      is unjudgeable, `_judged_recording` withdraws the submission (task skipped, dimension
      unblocked) and *then* raises, because a refusal must leave a way forward. That branch
      becomes an explicit `Settlement` value; nothing inside `plan_verdict` writes.
- [x] **`settle(transaction, settlement)`** is the one writer of settlements — shared with
      `settle_lapsed` and `on_consent_change`. In `record`, a `Settlement` is committed in its own
      transaction and the refusal raised after it, as today. In the resume path (§6), it is
      written in the resume transaction, the held verdict is voided, and the resume continues —
      a paused run gets the same treatment, not a refusal that aborts the resume.
- [x] **`write_verdict(transaction, plan)`** writes the result, sets the submission `judged`,
      folds the posterior, and inserts the verdict outcome. No commit of its own.
- [x] **`record`** = `open_writer` → `settle_lapsed` → `idempotency.resolve` → `plan_verdict` →
      one transaction { verdict row, `write_verdict` *or* hold, domain event }. Receipt and
      application are one commit; there is no separate acknowledgement to lose. A claim ends
      because a verdict naming it committed.
- [x] **One home for the key.** The idempotency key and request hash live where they live today:
      `domain_events`, through `idempotency.resolve`. `assessment_verdicts` carries no key column;
      the event's payload names the `verdict_id`, which is how a replay finds what it is replaying.
      The request hash covers every argument a refusal or the result turns on — what `record`
      hashes today plus `submission_id` and `claim_id`.
- [x] **No default key, so no behaviour change.** A keyless `record` keeps `_assert_repeat`: the
      same verdict again is a repeat, a different one is `assessment_result_conflict` — extended to
      compare against a held verdict too. A keyed record replays (same hash, whatever the run has
      become since — held, applied, or voided, and the replay says which) or conflicts
      (`idempotency_conflict`). A second, different keyed verdict for a submission that already
      has an applied or held one is `assessment_verdict_conflict`, naming the recorded score.
- [x] **Storage and relations.** `assessment_verdicts` (insert-only: `verdict_id`,
      `submission_id`, `claim_id`, score, assessor, confidence, rubric, retained response fields,
      `received_at`, and for a judge's narrower retention request `requested_visibility`) and
      `assessment_verdict_outcomes` (insert-only, unique on `verdict_id`:
      `applied` with `result_id`, or `void` with code and reason, `decided_at`). A held verdict is
      a verdict with no outcome.
- [x] **The rubric goes through retention.** The verdict row would otherwise be a second
      unfiltered copy of the rubric JSON — the deferred C1 gap, widened. `plan_verdict` runs the
      rubric's free-text fields through `retain_response` for every submission-bound verdict, held
      or applied, so the result row it produces is retained too. Keyless, non-submission `record`
      calls keep the deferred gap as it stands; C6 does not widen it.
- [x] **Observation time is a new column.** `assessment_results.observed_at` (unconstrained
      `ALTER ADD`, asserted by a named check): the submission's `created_at` for a submission-bound
      result, `recorded_at` otherwise, NULL on rows older than 0035. `recorded_at` stays the write
      time and keeps every current reader — withdrawal replay and estimate-history ordering — so
      nothing is reordered by this stage. Readers that should move to `observed_at` (delay class,
      "last seen") are listed in the implementation and moved by name, not by renaming.
- [x] **Crash on either side of the commit.** Before it: nothing written; the lease expires and
      the judge retries with the same key, which applies. After it, response lost: the retry
      replays. Neither needs reconciliation.

### Relations

DuckDB rewrites an update touching an indexed column as delete + insert, which a referenced row
refuses; a row written in place therefore must not be referenced by a foreign key. Rows written
in place: `assessment_submissions` (`status`, `superseded_by`, withdrawal fields, `response_text`),
`assessment_results` (`invalidated_*`), `assessment_runs` (`status`). References to
`assessment_runs` and `assessment_tasks` follow 0034's existing foreign keys and are unchanged.

| Child column                              | Parent                                | Kind                                  |
| ----------------------------------------- | ------------------------------------- | ------------------------------------- |
| `judging_claims.submission_id`            | `assessment_submissions`              | checked — `ORPHAN_RELATIONS`          |
| `judging_releases.claim_id`               | `judging_claims`                      | checked — `ORPHAN_RELATIONS`          |
| `assessment_verdicts.submission_id`       | `assessment_submissions`              | checked — `ORPHAN_RELATIONS`          |
| `assessment_verdicts.claim_id` (nullable) | `judging_claims`                      | checked — `ORPHAN_RELATIONS`          |
| `assessment_verdict_outcomes.verdict_id`  | `assessment_verdicts`                 | checked — `ORPHAN_RELATIONS`          |
| `assessment_verdict_outcomes.result_id`   | `assessment_results`                  | checked — `ORPHAN_RELATIONS`          |
| `assessment_batch_tasks.batch_id`         | `assessment_batches`                  | **foreign key**                       |

Claims, verdicts, and outcomes are insert-only today, but they are checked relations rather than
foreign keys so that the first later stage to write one in place does not have to rebuild a
table. Batch tasks and batches are created and never touched again together, which is the one
case a foreign key is safe for. Beyond orphans, `db check` asserts: one applied outcome per
submission; an applied outcome's result names the same submission; no held verdict on a run that
is not `paused`; a `judged` submission has an applied outcome.

## 6. Verdicts arriving after the run moved

- [x] **Paused** — `record` stores the verdict *held* (no outcome), returns `held: true`, and does
      not force the run open. `set_status(in-progress)` runs `settle_lapsed`, then applies held
      verdicts in `received_at` order inside the resume transaction via `plan_verdict` +
      `write_verdict`; a held verdict that no longer revalidates is settled and voided (§5) and the
      resume still succeeds. The resume report lists applied and voided verdicts.
- [x] **Abandoned** — the abandon transaction withdraws every `pending` submission
      (`assessment_run_abandoned`, task skipped) and voids every held verdict. Both are on the rows
      and in the report; nothing silently vanishes.
- [x] **Finalized** — finalization **refuses** with `assessment_judgement_outstanding`, listing
      pending submissions and held verdicts, unless called with `--exclude-outstanding`, which
      withdraws them (`assessment_run_finalized`) in the finalize transaction and lists them in the
      report's `excluded`. **`exclude_outstanding` joins `run_id` and `reason` in the finalize
      request hash**, so a retry with the flag after a refusal without it is a new request rather
      than a replayed refusal — the remedy the refusal names has to work. The page offers "wait"
      and "finish without these". A verdict arriving afterwards is refused against the closed run
      and reported, never applied retroactively.
- [x] A restart with an unapplied verdict resolves to one of the above: a held verdict lives on a
      paused run until resume or abandon, and `db check` refuses any other state.

## 7. Round-batching

- [x] **`POST /runs/{id}/batch`** / `assessment next --batch`, **idempotency key required**: serve
      one task per currently **open** dimension in a single call. The page serves through it.
- [x] Never two within a dimension — `select_task` probes the boundary of the current
      `posterior_median`, so within-dimension order is strictly sequential.
- [x] **Atomic.** `next_task` today commits served-task and exposure rows in separate transactions,
      so looping it would persist the first dimensions before a later one refused. C6 splits it
      into `plan_serve` / `write_serve` and serves a batch in **one** transaction. A refusal in any
      dimension writes nothing and names the dimension. A dimension with nothing sound to serve is
      not a refusal: it is reported as `outstanding` (it already holds a task, unanswered or
      awaiting a judge) or `exhausted` (the bank has nothing left; closed). `waiting` is
      reserved for `progress`.
- [x] **Exclusions are recomputed per dimension.** `next_task` computes `_excluded_task_ids` once
      before its loop. In a batch, after each dimension's `write_serve` the served set, exposure,
      and content-family exclusions are re-read inside the transaction before the next dimension is
      planned, so dimension *n* sees what dimensions 1…n−1 just served. Family diversity is per dimension
      and a task has one dimension, so no cross-dimension family rule exists; the recompute
      keeps a batch equal to sequential serving, and adding such a rule would be a new
      policy for single serving too.
- [x] **Deterministic order.** Dimensions are planned in the run's recorded (sorted) dimension order, so a
      retry and the one-at-a-time comparison test resolve cross-dimension exclusions identically.
- [x] **Membership is persisted.** `assessment_batches` (`batch_id`, `run_id`, `idempotency_key`
      unique, `request_hash`, `created_at`) and `assessment_batch_tasks` (`batch_id`, `content_id`,
      `dimension`, `position`), insert-only, written in the batch's transaction. A retry with the
      key returns **the same membership**, each task with its current state (`served`, `answered`,
      `awaiting-judge`, `skipped`) — even if some answers arrived since — and serves nothing new.
      A dimension that has since opened is not added; that is a new batch with a new key.
- [x] **The page holds a batch separately from its pending slot.** The pending slot is cleared
      as soon as an operation gets a definitive answer, so it cannot carry a key for a whole
      round. The page stores `{batch_id, idempotency_key}` under its own `sessionStorage` key,
      works through the batch locally, and clears it when every batch task is settled. On reload,
      a stored batch is replayed by key before anything new is served.

## 8. Tests — `tests/integration/test_client_async_judging.py` and `tests/browser/`

Batching
- [x] Batched serving produces the same per-dimension sequence as one-at-a-time serving.
- [x] Exclusion recompute: a round in which two dimensions share a content family serves what the
      sequential path would (batched == sequential; there is no cross-dimension family rule).
- [x] Mixed outstanding: a batch with some dimensions awaiting a judge serves only the open ones
      and reports the rest as outstanding.
- [x] Partial failure: a dimension whose plan refuses (injected) leaves no served or exposure row
      for any dimension in the batch.
- [x] Retry after a lost response returns identical membership after some batch tasks were
      answered and one was judged; same key with a different request conflicts.

Text submissions
- [x] Submit → `awaiting-judge` → claim returns `response_text` → record applies.
- [x] Resent submit (same key, same text) replays; same key, different text conflicts; second
      submission for the task is `assessment_task_already_submitted`.
- [x] An excerpt-only track's start excludes judged text tasks and names the filter.

Processing
- [x] Claim → no connection held while "judging" (a second writer succeeds during it) → record.
- [x] Expired lease is reclaimable; release returns to the queue; terminal release withdraws and
      unblocks the dimension; exhausted attempts withdraw via `settle_lapsed` in an unrelated
      writer; `db check` flags an exhausted unsettled submission.

Lifecycle (each between claim and record, and between a held verdict and resume)
- [x] Supersession: verdict refused naming the successor; held verdict voided on resume; the
      successor is claimable and its verdict applies.
- [x] Purge: refused / voided with `assessment_audio_purged`.
- [x] Audio-consent withdrawal: pending recordings purged in the preference transaction, code
      `assessment_audio_purged`, purge reason `learner-request`, withdrawal reason `consent
      withdrawn`; verdict refused; held verdict voided.
- [x] Transcript-consent withdrawal: pending text withdrawn and its text cleared.
- [x] Unjudgeable recording on resume: settled and voided, resume succeeds.

Commit boundary
- [x] Duplicate keyed delivery replays and applies once; held-then-replayed after resume reports
      applied.
- [x] Keyless repeat is accepted; keyless different verdict is still `assessment_result_conflict`.
- [x] Conflicts: same key different content; different key same submission.
- [x] Crash injected before the commit (nothing written; retry applies) and after it, before the
      response (retry replays, no second result).
- [x] `observed_at` equals the submission's `created_at` for a verdict applied a "week" later;
      `recorded_at` is the write time.

Run transitions
- [x] paused→resumed applies held verdicts in order and voids the ones that no longer revalidate,
      without failing the resume.
- [x] Abandoned withdraws and voids, and says so in the report.
- [x] Finalize refuses while anything is outstanding; a retry with `--exclude-outstanding` under the
      same key is not a replay of the refusal; the report names what it excluded; a later verdict
      is refused against the closed run.
- [x] Restart with an unapplied verdict leaves nothing unprocessable (`db check` clean).

Retention and waiting
- [x] A track forbidding retention never has response text in a submission, verdict, rubric, or
      retry record.
- [x] With all remaining dimensions blocked, the run reports `progress: waiting` rather than serving
      something unsound.

Browser
- [x] A late verdict recorded by the CLI while the page shows the waiting state makes the newly
      open dimension's task appear **without reload or pause/resume**.
- [x] A verdict that opens another dimension while the learner is typing an answer, and while
      recording one, leaves the text and the live recorder intact.
- [x] A poll that meets the writer lock keeps the screen and recovers on the next tick.
- [x] Hidden tab stops polling; visible resumes it.
- [x] Reload mid-batch replays the stored batch rather than serving a new one.

## Gate

- [ ] Release gate, because this stage adds a migration, routes, and CLI commands:
      `./.tools/uv run python scripts/verify.py`. The OpenAPI document, CLI snapshots, and the
      schema-version snapshot move; regenerate them and review the diff rather than accepting it.

## Done when

While any dimension is eligible the learner keeps working; when none is, the waiting is
explicit and the page notices a verdict on its own without disturbing the task in hand. Every
submission ends judged, superseded, or withdrawn with a reason — never pending with nobody able to
process it. Estimates match a synchronous run of the same answers.
