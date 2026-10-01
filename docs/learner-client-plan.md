---
title: "Learner Client Delivery Plan"
status: draft
project: lingua-wiki
last_updated: 2026-10-01
---

# Learner Client Delivery Plan

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)
Decisions: [ADR 0008](adr/0008-learner-client-transport.md) · Rationale and review history: [Learner Client Proposal](learner-client-proposal.md)

A local, single-user browser client that makes assessment and lessons operable by **button
rather than by prose**, with machine-scorable work scored by Python and no model in the loop.

This file is the working plan: seven stages, each independently shippable, each with an exit
gate. The proposal holds the *why* and the review history; the ADR holds the decisions. Neither
needs to be read to execute this.

## Implementation documents

One per stage, each self-contained with ordered TODO steps, the files to touch, the tests to
write, and a definition of done. Take them in dependency order.

| Stage | Document | Depends on |
|---|---|---|
| C1 | [Answer-key snapshot](client/C1-answer-key-snapshot.md) ✅ shipped | — |
| C2a | [Presentation contract and pack authoring](client/C2a-presentation-contract.md) ✅ shipped | — |
| C2b | [Presentation persistence and the served snapshot](client/C2b-served-presentation-snapshot.md) ✅ shipped | C1, C2a |
| C3 | [Server and generated contract](client/C3-server-and-contract.md) | C1, C2a, C2b |
| C4 | [Assessment screen](client/C4-assessment-screen.md) | C3 |
| C5 | [Audio, and the claims that rest on it](client/C5-audio-and-evidence.md) | C4 |
| C6 | [Submission lifecycle and batching](client/C6-async-judging-and-batching.md) | C4, C5 |
| C7 | [Session management](client/C7-session-management.md) | C4 |

**C1, C2a and C2b are shipped. C3 is next and is unblocked.**

## Stage numbering

Stages here are **C1–C7** so they cannot be confused with the implementation plan's Stage 0–9.
Where this work lands relative to Stage 8 is an open decision — see *Sequencing*.

## The four constraints every stage obeys

These were established by reproducing them against the code. They are the reason the stages
are ordered as they are.

1. **Nothing may hold a database connection across a request.** DuckDB holds an exclusive file
   lock and the application lock is non-blocking. A separate-process `open_reader` beside a
   held writer returns `database_busy`; the application lock raises `writer_locked`. Both are
   retryable. Reads are *not* free, and any design assuming they are is wrong.
2. **Scoring reads a serve-time snapshot, never the live pack.** A served task **already**
   snapshots `task_type`, `level_code`, `difficulty`, `content_family`, `modality`,
   `is_anchor`, `rubric_version`, `content_hash` (migration 0016) and `target_refs_json`
   (0022). What it does not keep is the **answer key, the prompt, and the rubric body**. C1
   extends that snapshot; it does not invent it. `content_hash` is already the drift detector
   and is the reason no backfill from a changed pack is ever needed.
3. **No claim may outlive its evidence.** `assessment_results` has no artifact column, and
   purge finds dependents only through `pronunciation_observations.audio_artifact_id`. Audio
   in the client is unsafe until C5 closes this.
4. **Adaptivity is sequential within a dimension.** `select_task` probes the boundary of the
   current `posterior_median`, and nothing currently stops a dimension with an unanswered task
   from being served another. The guard ships in **C3**, with the first surface that can be
   used concurrently with the CLI; batching and async judging then build on it in C6.

## Dependency order

```
C1  answer-key snapshot ───────┬──→ C2b served-presentation snapshot ──┐
                               │                                       ├─→ C3 server + OpenAPI
C2a presentation + authoring ──┘───────────────────────────────────────┘        │
                                                                                ↓
                                   C4 assessment UI ──┬──→ C5 audio
                                                      ├──→ C6 async + batching
                                                      └──→ C7 session management
```

**C1 and C2a are independent of each other and of everything else**, and neither needs a line
of client code. C2b is the integration that joins them, and it is the only part of C2 that has
to wait.

---

## Stage C1 — Deterministic scoring with a serve-time snapshot

**Outcome.** Objective and short-response tasks are scored in Python against an immutable
snapshot of the task as served. A calibration becomes runnable from a shell script with zero
model calls.

**Depends on.** Nothing.

**Work.**
- Migration adding **only what is missing** from the existing served-task snapshot:
  `expected_json` (the answer key), `prompt_snapshot` and `rubric_json` (the body; only
  `rubric_version` is kept today). Everything else listed in constraint 2 is already there
  and must not be duplicated. The three are one group: a row has all of them or none, and a
  partial row is damage rather than a legacy row.
- **Type the answer key before scoring against it.** `expected` is `dict[str, Any]` today,
  so `{"answers": []}`, `{"answers": "yes"}` and `{"unrecognized": true}` all validate. A
  malformed key must be a named refusal, not a learner's zero.
- Score `objective` and `short-response` against the snapshot. The existing
  `--assessor-kind deterministic` only *labels* a caller-supplied score; this makes the core
  compute one.
- **The answer is an input, not a stored artifact.** `record`'s only response input today is
  `--excerpt`, which it persists unchanged, so scoring off that field would make automatic
  scoring require retaining text a track may have declined. The answer arrives on its own
  argument, is scored in memory, and reaches the row only through
  `evidence_service.retain_response`.
- Record `scoring_policy_version` **on the result**, beside a `score_source` that separates a
  computed score from a caller-supplied one, so a later policy change cannot reinterpret a
  historical score and a compatibility call cannot read as the policy having run. The result
  is where the score is; the serve-time snapshot has no business carrying a second version.
- **Tasks served before this migration carry no answer key.** They are never backfilled from
  the pack, because the pack may have changed. Instead: if the live row's `content_hash` still
  equals the snapshotted one, the content is provably identical and may be read; if it
  differs, deterministic scoring is refused and the task is marked as unscorable-without-a-
  judge. No other path is permitted.
- Refuse deterministic scoring for `extended-productive`, `pronunciation-target` and
  `connected-speech`, naming the reason.

**Verification.**
- Updating the pack between serving and scoring does not change the score.
- A pre-migration served task with a matching `content_hash` scores; one with a drifted hash
  is refused rather than guessed.
- A full calibration of the machine-scorable dimensions completes with no model call, proven
  by the recorded assessor kinds.
- Refusals for the three rubric-scored task types are asserted by code.
- A track that declines transcript retention still scores, and stores no text.
- Malformed answer keys, and partial or unparseable snapshots, are refused by name and
  found by `db check`.

**Exit gate.** A calibration's objective and short-response tasks can be answered and scored
entirely from the CLI, the recorded provenance shows no model was involved, and no score rests
on content the learner was not shown.

**Estimate.** 2–3 days.

---

## Stage C2 — Presentation contract and pack authoring

**Outcome.** A task carries enough structure for a UI to render it without parsing prose,
and the structure the learner was actually shown is preserved with the answer key.

**Depends on.** Split, because the two halves have different prerequisites and the authoring
must not be blocked behind code:

- **C2a — contract and authoring: nothing.** The presentation contract, the pack asset
  catalog, the pack work, and pack validation. This is the schedule risk and can start
  immediately, in parallel with C1. It touches no database.
- **C2b — persistence and the served snapshot: C1 and C2a.** The bank migration, the
  installer, and the served-task snapshot. Extending the snapshot requires C1's snapshot to
  exist and C2a's contract to define its shape; the bank column has to come with it, because
  `assessment_tasks` has no presentation column and the installer writes an enumerated column
  list, so a pack carrying presentation would install into a bank that discards it.

**Work.**
- Versioned presentation contract: typed `choices` for multiple-choice, an asset reference and
  replay allowance for audio tasks, expected response shape for `short-response`.
- Distinguish **multiple-choice** from **deterministically scored free text**. Both are
  machine-scorable; they render differently.
- **A choice submits its `value`, and the scorer is untouched.** `score_response` folds the
  response and compares it against the served `AnswerKey`; it does not know what a choice is.
  So exactly one choice `value` must fold-match the key, and `display` is never scored. An
  option id mapped back to an answer would be a second place the right answer is recorded.
- **An optional field added to a hashed payload moves every existing hash.** `pack_item_hash`
  dumps the payload whole, so `presentation: … | None = None` writes `"presentation": null`
  into content nobody edited and detaches its reviews. The field is omitted from the canonical
  payload when absent; a blanket `exclude_none` is not the fix, because rubric-scored tasks
  already serialize `"expected": null`.
- **Pack audio has to be invented before it can be referenced.** There is no asset concept and
  no audio file in any pack, and a stable id alone can resolve to changed bytes after an
  update. C2a adds an asset catalog whose identity is `(id, digest)`, validated by opening the
  file rather than believing the entry — and six `pl-pilot` recordings are a content
  deliverable, not a checkbox. If they cannot be produced, those tasks keep their prompts and
  declare no asset; changing their `modality` to hide the gap would make a receptive dimension
  untestable, since `REQUIRED_MODALITIES` demands `audio`.
- Pack work on `pl-pilot`: options are currently encoded inside the prompt string
  (`"Which is correct? 'pięć bilety' / …"`) and audio tasks carry spoken text in `prompt`
  rather than referencing a clip. This is authoring and migration, not a rendering concern.
- A task without a presentation record renders as free text. The client never guesses.
- **C2b — presentation reaches the bank and then the snapshot.** One migration adds
  `presentation_json` to `assessment_tasks` and `presentation_json` + `asset_identity_json` to
  `assessment_run_tasks`; splitting them would permit a release in which the bank holds
  presentation no serve can snapshot. The installer writes the column on insert *and* on
  conflict, so an upgrade that removes a presentation clears it. The snapshot then keeps
  choices as shown, their order, the asset **identity** — id *and* digest, not merely a path —
  and the replay allowance in force. A run paused before a pack update and resumed after it
  must show the learner the same choices the answer key belongs to. This item, and its
  resume-after-pack-update test, cannot be met by C2a alone.

**Verification.**
- Every `pl-pilot` objective task exposes typed choices, or is explicitly marked free-text.
- A task with no presentation record round-trips as free text rather than failing.
- **The two fixture packs, which are not re-authored, still validate with the hashes they
  shipped with** — no re-stamp, with one derived hash pinned as a literal so the next optional
  field that moves it fails there.
- **The correct button scores.** A re-authored objective task's correct choice `value`, put
  through `score_response` against that task's key, returns 1.0; another choice returns 0.0.
- Pack validation rejects a presentation record that disagrees with its task type, an
  objective task whose choices match its key zero times or twice, colliding or blank choice
  values, and an asset whose file is missing, altered, or outside the pack root.
- **Install round trip**: presentation survives install, reopen, and a reinstall that changes
  one task's choices and removes another's.
- **Resume-after-pack-update**: serve a task, pause, change the pack's choices and assets,
  resume — the learner sees the served presentation, and the asset resolves to the served
  identity or the task is refused rather than shown with a substituted clip.

**Exit gate.** Every task the pilot pack can serve is renderable as buttons or as an honest
free-text field, decided by data rather than by parsing, and what a learner was shown survives
a pack update.

**Estimate.** 5–8 days, most of it authoring — revised upward from 3–5 because the asset
catalog and the recordings did not exist when the first estimate was made, and because the
bank migration and installer moved into C2b rather than being absent from the plan.

---

## Stage C3 — The local server and its generated contract

**Outcome.** A loopback HTTP server exposes the read model and the mutations, described by a
generated OpenAPI 3.1 document. No UI yet; verified by contract tests.

**Depends on.** C1, C2a, C2b.

**Work.**
- Server in this repository, stdlib `http.server` only, importing the service layer
  directly. No business logic, and **single-threaded**: in one process a second reader
  beside a held writer is refused by DuckDB itself, so a threaded server would spend its
  retry budget on contention it created. The `linguawiki[client]` extra is **not** declared
  — with no dependencies to install it would be a boundary nothing enforces.
- Bind loopback only, on a port recorded in `data/client-runtime.json`. The launch token is
  never written to disk; it reaches the page through the URL fragment.
- **Browser-origin protection**: `Host` allowlist, `Origin` validation on every mutation, and
  an ephemeral launch token minted per server start. Loopback is not a security boundary and
  DNS rebinding is a documented attack on local servers.
- One reader per read, one writer per mutation, neither held across requests. Bounded retry on
  `writer_locked` and `database_busy`, surfaced honestly when exhausted.
- A `command` name into the audit row for every mutation, **written in the service layer**:
  `next_task` and `record` write no audit row at all today, and adding one in the server
  would give the server a better trail than the CLI. `actor` distinguishes the surface; the
  command name stays the same.
- **Operation-scoped idempotency keys bound to a canonical request hash**, for serving as well
  as recording. `next_task` takes no key today, so a retried serve consumes another task and
  burns its exposure. A same-payload retry replays the original result; a changed-payload
  retry is a conflict, not a silent no-op.
- **One outstanding task per dimension, enforced in the shared service layer** — not in the
  server, and not deferred to C6. From C3 onward a browser and a CLI are live at once, so two
  serve calls with different idempotency keys could otherwise select two tasks in the same
  dimension before either answer moves the posterior. This is the guard that makes concurrent
  use safe, so it ships with the first concurrent surface. When no open dimension is free the
  outstanding task is **re-served from its snapshot**, writing nothing — which needs one
  migration, because `permitted_help` is the last thing a client reads that the served
  snapshot does not keep.
- Read model: one call per screen, assembled from `open_reader`.
- `scripts/generate_openapi.py`, document committed, `--check` in the gate, sharing the CLI's
  error payload schema.
- **Reference relocation for the reused schemas.** Most of the committed snapshots contain
  `#/$defs/...` pointers — 18 of 21 at the time of writing, and the count is derived from
  `SCHEMA_MODELS` rather than quoted, because an earlier draft of this line said 17 and was
  stale within one stage. `#` is the *document* root, so embedding them unchanged under
  `components/schemas` makes those pointers resolve against the OpenAPI document and fail.
  Decided: **hoist and rewrite** — `$defs` lift into `components/schemas` under a
  namespaced name and every pointer is rewritten. A test asserts no `#/$defs/` survives and
  that a nested reference resolves through the assembled document.

**Verification.**
- Server and CLI run concurrently: reads *and* writes may fail with `database_busy` or
  `writer_locked`; each is retried and then surfaced.
- Forged `Host`, foreign `Origin`, absent and stale tokens are all rejected.
- A retried serve does not consume a second task; a changed-payload record is refused.
- **Two independent callers** — a browser request and a CLI invocation — cannot obtain two
  unanswered tasks in the same dimension.
- The server refuses exactly what the CLI refuses, asserted against the same cases.
- The committed OpenAPI document is current, every response validates against it, and a
  **nested** `$ref` resolves through the assembled document.

**Exit gate.** A calibration can be driven end to end over HTTP with no browser, and the
contract document is generated rather than maintained.

**Estimate.** 5–8 days.

---

## Stage C4 — The assessment screen

**Outcome.** The first learner-visible milestone: a calibration answerable by button.

**Depends on.** C3.

**Work.**
- Answer choices as buttons; no prose typing for multiple-choice.
- Listening: replay under the learner's control, **replay count recorded** — replays are
  evidence, not a convenience.
- Per-dimension progress against its task budget, showing the confidence label the CLI reports
  and never a level the CLI did not claim.
- Pause and resume across sittings.
- Busy and retrying shown as real states, because another process holding the database is
  normal rather than exceptional.
- Text and machine-scorable modalities only. Audio capture is C5.

**Verification.**
- A calibration completes in the browser using only machine-scorable dimensions, with zero
  model calls.
- Replay counts reach the stored result.
- A concurrent CLI command makes the UI show a retry state, not a hang or a crash.

**Exit gate.** A learner completes the text portion of a calibration in the browser, and the
estimates are identical to those the CLI would have produced from the same answers.

**Estimate.** 5–8 days.

---

## Stage C5 — Audio, and the claims that rest on it

**Outcome.** Pronunciation and speaking become answerable, and no assessment claim outlives
its recording.

**Depends on.** C4.

**Work.**
- Press to start, press to stop. No countdown.
- **Consent is checked before any bytes are persisted**, not after. A track that does not
  permit recording never produces a file to clean up.
- The recording is written under the workspace's private `imports/` root and registered
  through `artifacts.register` with its hash.
- **Capture is a two-phase operation with an owner, because registration can fail.**
  `register` refuses for many reasons — `writer_locked`, an unknown track, a hash mismatch, a
  retention conflict — and these bytes were *created by the client*, unlike an externally
  supplied file the learner already had. A failed registration must not leave a recording that
  nothing accounts for. This repository already knows the failure mode by name: *"skipping it
  left the bytes sitting unregistered under an ignored directory, accounted for by nothing"*
  (`services/artifacts.py`), which Stage 5 closed for package ingestion. C5 must not reopen it
  through the browser.
  - [ ] Bytes land first in a client-owned staging location whose ownership is recorded, not
        directly at their final path.
  - [ ] Registration promotes them; a refusal removes them and reports why.
  - [ ] A disconnection mid-upload, or a server restart with staged bytes present, is
        recoverable: either the capture is completed or the bytes are removed. Nothing is left
        for a later reader to guess about.
  - [ ] `writer_locked` and `database_busy` are retried before the capture is abandoned, since
        those are transient and the learner has already spoken.
- **Result-to-artifact relationship**: `assessment_results` gains an audio reference, and
  `artifacts.dependent_observations` learns to find it.
- **Retained-audio validation at judging time**: a pronunciation or spoken-production score
  may not be recorded against audio that is absent or not retained.
- Invalidation semantics: the result is marked invalidated rather than deleted, the
  dimension's posterior is recomputed from surviving results, and any finalized estimate
  derived from it is marked as resting on withdrawn evidence.
- **Baseline selection must preserve assessment chronology.** `estimates.py` picks a baseline
  with `ORDER BY state.updated_at DESC, state.run_id DESC`. Recomputing an *older* run's state
  after a purge refreshes `updated_at`, which would let that older run displace a newer
  calibration. Baseline order must follow the run's own chronology, not the state row's
  modification time.
- Consent and retention remain the track's existing settings; the client surfaces and never
  overrides them.

**Verification.**
- Audio recorded in the browser lands as an artifact `artifact verify` reports as present.
- Purging that audio invalidates every assessment result resting on it, and the affected
  estimates say so.
- Recording a pronunciation score against absent or non-retained audio is refused.
- **Two-run purge regression**: an older run, recomputed after a purge, does not become the
  baseline over a newer calibration.
- **Failure paths, tested beside the success path**: registration refused after the bytes were
  written leaves nothing behind; a disconnection mid-capture leaves nothing behind; a server
  restart with staged bytes present resolves them in one direction or the other. After each,
  no file exists under a private root that no row accounts for.
- A track that forbids recording never reaches the point of writing a file.

**Exit gate.** A pronunciation task is answered in the browser, purged afterwards, and the
resulting state is honest about what it no longer knows.

**Estimate.** 5–8 days.

---

## Stage C6 — Submission lifecycle and round-batching

**Outcome.** Rubric-scored work no longer blocks, and round-trips drop roughly sevenfold —
both without breaking sequential adaptivity.

**Depends on.** C4 (C5 for the audio task types).

**Work.**
- An explicit **waiting** state, distinct from open and closed, reported to the client. (The
  one-outstanding-task-per-dimension guard itself ships in C3.)
- A durable submission record, so a verdict arriving after a crash is still attributable.
- **Retention is applied before the submission is persisted, not after.** A recoverable
  writing submission needs the learner's text, and a track may forbid retaining it. Session
  staging already resolves this through `evidence_service.retain_response`, which returns
  `(visibility, excerpt, digest)`; assessment recording did **not** inherit that boundary
  until C1, which routes both the answer and a caller-supplied excerpt through it. Queue
  records, and every retry record, store the retained form only.
- **Specify the supported path when the retained form cannot be judged.** If a track's policy
  withholds the text, the choice is explicit: judge synchronously within the request and
  persist only the verdict, or refuse asynchronous judging for that track. Silently queueing
  text the track forbids storing is not an option.
- **Rules for a verdict that arrives after the run's state has moved.** This cannot be left
  implicit, because `record` calls `_assert_running` and refuses outright with
  `assessment_run_paused` or `assessment_run_closed`. A judgement returning after the learner
  has paused therefore *cannot simply be recorded*. Define and implement each transition:
  - [ ] **Paused** — the verdict stays queued and is applied on resume. It is not discarded,
        and it does not force the run open.
  - [ ] **Abandoned** — the verdict is cancelled, and the cancellation is recorded rather than
        the verdict silently vanishing.
  - [ ] **Finalized** — either finalization waits for outstanding judgements, or it proceeds
        and records explicitly which results it excluded. A verdict arriving afterwards is
        refused against the closed run and reported, never applied retroactively.
  - [ ] A restart with an unapplied verdict resolves to one of the above, not to a queue entry
        nobody will ever process.
- **Round-batched serving**: one task per *open* dimension in a single call, never two within
  a dimension.

**Verification.**
- Batched serving produces the same per-dimension sequence as one-at-a-time serving.
- A run cannot be finalized silently while a pending judgement would change it.
- A verdict arriving after a restart is attributed correctly.
- A track that forbids retaining responses never has response text persisted in a queue or
  retry record, and the supported judging path is exercised.
- **Each lifecycle transition with a judgement outstanding is tested**: paused then resumed
  applies it; abandoned cancels it and says so; finalized either waited or names what it
  excluded; and a restart with an unapplied verdict leaves no entry nobody will process.

**Exit gate.** While any dimension is eligible the learner keeps working rather than waiting.
When every remaining dimension is blocked on a judgement, the run reports an explicit waiting
state rather than appearing stalled — sequential adaptivity makes some waiting unavoidable,
and the gate is that it is *never hidden*, not that it never happens. Estimates match a
synchronous run of the same answers.

**Estimate.** 5–8 days.

---

## Stage C7 — Session management in the browser

**Outcome, deliberately narrowed.** The plan, the staging, and the close become operable in
the browser. **Teaching does not move.**

Core holds no teaching content and is not going to. `_activity_prompt` in
`services/sessions.py` returns `f"{kind} for {block}: {objective}"`, and its docstring is
explicit: *"The skill turns this into teaching. Core states the demand — what kind of work, on
what — and nothing about how to say anything in any language."* Wrapping `plan` / `session`
commands therefore cannot deliver exercises, explanations, feedback or scores, and an earlier
draft of this stage claimed it could.

So C7 delivers session *management*, and the lesson itself continues to come from an assistant
alongside it. Making the browser conduct a lesson would require a specified teaching-content
and judging integration — a larger piece of work, listed below as a successor rather than
folded in here.

**Depends on.** C4.

**Work.**
- `plan create` / `session start` / `session log` / `session close` over the read model.
- Block rationales shown as the planner gives them, including the candidates that missed out
  and why.
- Staged observations visible before the close; the close remains the only moment progress
  moves.
- The screen states plainly that the teaching happens elsewhere, so the learner is never
  waiting for an exercise the browser cannot produce.

**Verification.**
- A session planned, run, and closed in the browser credits exactly once.
- An interrupted session recovers through the existing `resume` / `partial-close` / `recover`
  paths.
- Staged work is visible before the close and unchanged by it until it happens.

**Exit gate.** A session can be planned, staged, and closed in the browser, the close boundary
is as visible to the learner as it is in the data, and nothing in the interface implies the
browser is teaching.

**Successor, not in scope.** A browser that conducts lessons needs a teaching-content source
and a judging integration, with the same provenance rules generated content already obeys.
Worth a proposal of its own once C1–C7 are in use.

**Estimate.** 5–8 days.

---

## Sequencing — decided

**C1–C7 land before Stage 8.** Stage 8 is four weeks of real Polish learning; running it
through chat would spend a large budget on string comparison and would measure the interface
as much as the learner.

The decision rests on where the requirements came from. Every observation behind this plan —
the token cost of recording an answer, the clip that could not be replayed, the countdown
nobody controlled — surfaced within **an hour** of actual learning, not from speculation about
what a client might need. That is the kind of evidence Stage 8 exists to produce, and it
arrived early enough to act on. Building the interface first means Stage 8 measures the
learner rather than the tooling.

Stage 8 resumes once C7 is done.

### Overlap to exploit rather than sequence twice

C2a (pack authoring: typed choices, clip references) and Stage 8's own pack growth toward an
A1–B1 maturity gate touch the same files and the same bank. Doing C2a as the first slice of
that growth, rather than as a separate pass, avoids authoring the bank twice.

## Risks

- **C2a is the schedule risk.** It is content authoring, which is slower and less predictable
  than code, and the pilot pack is thin across the board. It may be worth doing alongside the
  pack growth Stage 8 implies rather than as a separate push.
- **Six Polish recordings are on the critical path for audio, and nobody has made them.** They
  are a production deliverable with rights and provenance, not a code task. C2a names the
  fallback — no asset reference, prompts unchanged — but taking it leaves C5's listening work
  resting on a bank that still cannot play anything.
- **A second entry point can drift.** The server must refuse what the CLI refuses; the
  equivalence tests in C3 are the only thing keeping that true.
- **OpenAPI conformance is not correctness.** The sequencing rules in C6, the snapshot rule in
  C1, and the invalidation rule in C5 are not expressible in the document and live in tests.
- **Estimates assume the constraints hold as reproduced.** Each was verified against the code
  at revision 2 of the proposal; a migration that changes the assessment tables invalidates
  the C1 and C6 estimates.
- **Adding a field to a hashed pack payload is a cross-cutting change.** The hash covers the
  whole payload by design, so any stage that extends a pack contract owes the same
  unchanged-pack test C2a adds, or it moves hashes for content nobody edited.

## Not attempted, and why

- **A mobile or hosted client.** Learner state is local and the workspace has no remote.
- **Offline model inference for rubric scoring.** The C6 queue is indifferent to who answers.
- **Multi-learner support in one server.** A workspace is one learner; a second workspace runs
  its own.
- **A general-purpose API.** The read model serves this client's screens. Anything broader is
  speculative until a second consumer exists.
