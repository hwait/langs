---
title: "Learner Client Proposal"
status: proposed
project: lingua-wiki
scope:
  - local-client-transport
  - assessment-interaction
  - deterministic-scoring
  - audio-capture
revision: 2
last_updated: 2026-09-29
---

# Learner Client Proposal

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)

## Outcome

A local, single-user client makes assessment and lessons operable by **button rather than by
prose**, over the JSON contracts that already exist. Objective tasks are scored with no model
in the loop at all. The client holds no business logic: task selection, scoring, the posterior
update, the stop rule, and every refusal stay in Python, exactly where the skills boundary
already puts them.

## Why this, and why now

Four observations from the first real calibration sitting drive it:

1. **Recording an answer costs tokens it has no business costing.** A multiple-choice answer
   is a string comparison; no model needs to see it. (The existing `--assessor-kind
   deterministic` flag only *labels* a caller-supplied score — it does not compute one. See
   10.1.)
2. **Chat is the wrong instrument, and it contaminates the measurement.** A clip you cannot
   replay, a recording with an uncontrollable countdown, an answer typed as prose — those are
   chat's limits leaking into the estimate. If replaying costs a conversational round-trip,
   the learner under-replays and the listening estimate measures their patience.
3. **Nothing should block on evaluation** — within the limits set by adaptivity (see 10.7).
4. **Filler is the wrong answer to latency.** A reading or listening task offered during a
   wait is not filler, it is the next task, and only if adaptivity permits serving one.

## Revision 2

Seven findings against revision 1 were reproduced against the code before this rewrite. Four
were factual errors in the proposal, not merely gaps. They are recorded here because the
reasoning that produced them is the reasoning a reader would otherwise repeat.

| # | Finding | Status |
|---|---|---|
| 1 | Reads are **not** guaranteed to succeed beside a writer | **Reproduced.** A separate-process `open_reader` beside a held writer returns `database_busy` (retryable). Revision 1's "reads never fail" was false and its verification criterion impossible. `writer_locked` was also omitted. |
| 2 | Loopback binding is not a browser security boundary | **Accepted.** DNS rebinding is a documented attack on unauthenticated local servers. Revision 1 called skipping authentication "a deliberate choice"; it was an unexamined one. |
| 3 | Deterministic scoring needs an immutable answer-key snapshot | **Reproduced.** `assessment_run_tasks` stores only `(run_id, sequence, content_id, dimension, status, served_at)` and a live FK to `assessment_tasks`. Nothing about the task is snapshotted. |
| 4 | Purge does not invalidate assessment results | **Reproduced.** `assessment_results` has no artifact column; `artifacts.dependent_observations` finds dependents only via `pronunciation_observations.audio_artifact_id`. |
| 5 | The bank cannot support button-only presentation | **Reproduced.** `PackAssessmentTask` has `prompt: str` and `expected: dict`, with no typed choices, clip reference or replay allowance. |
| 6 | "Next task immediately" breaks adaptivity once dimensions run out | **Reproduced.** `_excluded_task_ids` excludes served *content*; nothing excludes a *dimension* holding an unanswered task. |
| 7 | Accepting idempotency keys is insufficient | **Reproduced.** `next_task` takes no key at all. `record` returns an idempotent no-op for an already-answered task *regardless of the submitted score*. |

## The concurrency model, corrected

Revision 1 asserted that reads are free. They are not, across processes.

- `open_writer` takes a **non-blocking advisory lock** beside the database and raises
  **`writer_locked`** (retryable) on contention.
- DuckDB independently holds an **exclusive file lock**. A second process opening the database
  — *including read-only* — raises **`database_busy`** (retryable).

Verified: with a writer held, a subprocess calling `open_reader` returns
`REFUSED database_busy True`.

Consequences, which are architectural rather than incidental:

- **Every database interaction is short-lived and retryable.** The server opens a reader per
  read and a writer per mutation, and holds neither across requests.
- **Both error codes are first-class.** The client distinguishes `writer_locked` (another
  LinguaWiki process is mid-command) from `database_busy` (the file is held) and retries with
  bounded backoff, surfacing a plain explanation if it persists.
- **The server has no privileged position.** A learner running a CLI command briefly makes the
  server's reads fail, and that is correct: the alternative is a server that monopolises the
  database.
- **In-process readers beside the server's own writer are not a workaround.** The server must
  not assume co-location saves it; the same code must work when the CLI is the other party.

## Decision: where the server lives and how it calls the core

Recorded as [ADR 0008](adr/0008-learner-client-transport.md), which also carries the OpenAPI
decision in 10.9 and the alternatives rejected for both.

**Same repository, optional dependency extra, server imports the service layer directly,
stdlib HTTP only.**

| | Option A: subprocess the CLI | Option B: import services | Option C: separate repository |
|---|---|---|---|
| New core dependencies | none | none | none in core |
| Latency per call | process start each time | in-process | in-process |
| Typed errors | reparsed from JSON | native `LinguaWikiError` | native |
| Second entry point to keep honest | no | **yes** | yes |

Option B, because reparsing our own error envelopes to re-raise them is avoidable work, and
`--format json` is shaped for humans and skills rather than a UI's read model. Its cost is
real: the server becomes a second entry point and must carry what the CLI carries — a
`command` name for the audit row, idempotency, and the same refusals. That is a test
obligation in Verification.

Packaging as an optional extra (`linguawiki[client]`) keeps the six pinned runtime
dependencies untouched for workspaces that do not want a UI. `http.server` from the standard
library is adequate for one local user; adding a web framework to a checksum-verified
distribution buys nothing.

Rejected: a web framework in core dependencies; reimplementing scoring or selection in
JavaScript (the architecture exists to prevent exactly this); a hosted client.

## Work packages

### 10.1 Deterministic scoring, with a snapshot (no client needed; ship first)

The existing `--assessor-kind deterministic` only labels a score the caller supplies. Real
deterministic scoring requires the core to hold the answer key **as it was when the task was
served**.

- [ ] Migration: snapshot onto `assessment_run_tasks` at serve time — `expected_json`,
      `prompt`, `rubric_json`, `rubric_version`, `difficulty`, `task_type`, and the
      `scoring_policy_version` in force.
- [ ] Score objective and `short-response` tasks in Python against the **snapshot**, never
      against the live `assessment_tasks` row.
- [ ] Record `scoring_policy_version` on the result, so a later policy change cannot silently
      reinterpret a historical score.
- [ ] Refuse deterministic scoring for `extended-productive`, `pronunciation-target` and
      `connected-speech`, naming the reason.
- [ ] Test: update the pack between serving and scoring; the score must reflect the snapshot,
      and the run must remain internally consistent.

### 10.2 Round-batched serving

- [ ] Serve **one task per open dimension** in a single call.
- [ ] Preserve adaptivity exactly: `placement.select_task` probes the boundary of the current
      `posterior_median`, so tasks *within* a dimension stay strictly sequential. Only the
      cross-dimension round is batched.
- [ ] A dimension holding an unanswered task is **not eligible** to be served again (see
      10.7). Test this directly.

### 10.3 Presentation contract and pack work

A read model cannot supply facts the bank does not hold.

- [ ] Versioned presentation contract: typed `choices` for multiple-choice tasks, an asset
      reference and replay allowance for audio tasks, expected response shape for
      `short-response`.
- [ ] Distinguish **multiple-choice** from **deterministically scored free text**: both are
      machine-scorable, they render differently, and conflating them was revision 1's error.
- [ ] Pack work: `pl-pilot` currently encodes options inside the prompt string
      (`"Which is correct? 'pięć bilety' / …"`) and puts spoken text in `prompt` rather than
      referencing a clip. Authoring and migration are required, not just a UI.
- [ ] Until a task carries a presentation record, the client renders it as free text rather
      than guessing — no prose parsing, ever.

### 10.4 The local server

- [ ] Bind **loopback only**, on a port recorded in the workspace, never `0.0.0.0`.
- [ ] **Browser-origin protection**, because loopback is not a security boundary and DNS
      rebinding is a known attack on local servers:
  - [ ] Validate the `Host` header against an allowlist; reject anything else.
  - [ ] Validate `Origin` on every mutating request; reject cross-site.
  - [ ] An **ephemeral launch token**, minted per server start, passed by the page that the
        launch command opens. No persistent credential store, so no new private state.
  - [ ] Negative tests: forged `Host`, foreign `Origin`, absent and stale token.
- [ ] One reader per read, one writer per mutation, neither held across requests; bounded
      retry on `writer_locked` and `database_busy`, surfaced honestly when exhausted.
- [ ] Pass a `command` name for every mutation so the audit trail is indistinguishable from a
      CLI-driven one.
- [ ] Serve the built SPA as static files from the workspace.

### 10.5 Idempotency that survives a lost response

- [ ] **Operation-scoped keys bound to a canonical request hash**, for `next` as well as
      `record`. `next_task` currently takes no key, so a retried serve consumes another task
      and burns its exposure.
- [ ] A retry with the **same** payload replays the original result.
- [ ] A retry with a **different** payload is refused as a conflict. Today `record` silently
      no-ops an already-answered task even when the score differs, so a client cannot tell a
      successful retry from a rejected correction.
- [ ] Tests: lost response then retry; changed-payload retry; concurrent duplicate submits.

### 10.6 Audio capture, and the claims that rest on it

- [ ] A recording is written under the workspace's private `imports/` root and registered
      through `artifacts.register` with its hash.
- [ ] **Result-to-artifact relationship**: `assessment_results` gains an audio reference, and
      `artifacts.dependent_observations` learns to find it. Without this a purge deletes the
      recording and leaves the pronunciation score and its derived estimate standing — the
      same defect class Stage 5 closed for `pronunciation_observations`.
- [ ] **Retained-audio validation at judging time**: a pronunciation or spoken-production
      score may not be recorded against audio that is absent or not retained.
- [ ] Define what invalidation does to assessment state: the result is marked invalidated
      rather than deleted, the dimension's posterior is recomputed from surviving results, and
      any finalized estimate derived from it is marked as resting on withdrawn evidence.
- [ ] Consent and retention remain the track's existing settings; the client surfaces and
      never overrides them.

### 10.7 Submission lifecycle for asynchronously judged work

Serving "the next task immediately" is unsound once every open dimension is awaiting a
judgement: selection would run against a stale posterior.

- [ ] **At most one outstanding task per dimension.**
- [ ] An explicit **waiting** state, distinct from open and closed, reported to the client so
      it can say what is happening rather than appearing stalled.
- [ ] A durable submission record, so a verdict arriving after a crash is still attributable.
- [ ] Rules for `pause`, `finalize` and `abandon` with judgements outstanding. Finalization
      must not close a run whose pending results would have changed it; either it waits, or it
      finalizes explicitly without them and records that it did.

### 10.8 Lessons, once assessment works

- [ ] The same shell over `plan create` / `session start` / `session log` / `session close`.
- [ ] Staging stays durable and credits nothing; the close remains the only moment progress
      moves. The client must not blur that boundary.

### 10.9 The HTTP contract, as a generated OpenAPI 3.1 document

Lands with 10.4; the server is not reviewable without it.

- [ ] **OpenAPI 3.1**, because it adopts JSON Schema 2020-12 as its schema dialect — which is
      exactly what `schema_snapshots.py` already emits from `model_json_schema()`. The
      existing contract snapshots become `components/schemas` with no translation. OpenAPI 3.0
      is rejected: its divergent schema subset would mean hand-maintained down-conversion.
- [ ] **Generated, never hand-written.** `scripts/generate_openapi.py`, built from the same
      Pydantic models plus a route table, with the document committed and
      `generate_openapi.py --check` in the quality gate — the pattern
      `generate_schemas.py --check` already establishes. A hand-maintained spec would be a
      second home for the contract.
- [ ] **No new dependency.** Pydantic does the schema work; assembling paths and components
      is a dict and a `json.dump`.
- [ ] **One error contract for both entry points.** The CLI envelope's error payload
      (`code`, `message`, `retryable`, `details`) is the same component schema the HTTP API
      returns. This is also how the server demonstrates it refuses exactly what the CLI
      refuses.
- [ ] The document carries what the JSON Schemas cannot: declared error responses with the
      retry contract for `writer_locked` and `database_busy` (10.4), the required `Host`,
      `Origin` and launch-token headers as a security scheme (10.4), and per-operation
      idempotency key parameters with their conflict response (10.5).
- [ ] The frontend consumes a client generated from the document, so the SPA cannot drift
      from the server.
- [ ] **Recorded limitation:** OpenAPI describes shapes, not sequences. One outstanding task
      per dimension, the `waiting` state, finalization with judgements pending, and scoring
      against the serve-time snapshot are **not** expressible in it and remain in the service
      layer and its tests. Conformance to the document is not correctness, and the ADR says so.

## Verification

- [ ] Server and CLI run concurrently: **both** reads and writes may fail with
      `database_busy` or `writer_locked`, and each is retried and then surfaced honestly.
      (Revision 1's "reads never fail" criterion was impossible and is withdrawn.)
- [ ] A full calibration completes through the client with **zero model calls** for objective
      and `short-response` tasks, proven by the recorded assessor kinds.
- [ ] Updating the pack between serving and scoring does not change the score.
- [ ] Batched serving produces the same per-dimension task sequence as one-at-a-time serving.
- [ ] A dimension with an unanswered task is never served another.
- [ ] Forged `Host`, foreign `Origin`, and missing or stale launch tokens are all rejected.
- [ ] A retried `next` does not consume a second task; a changed-payload `record` is refused.
- [ ] Purging audio invalidates every assessment result resting on it, and the affected
      estimates say so.
- [ ] The server refuses exactly what the CLI refuses, asserted against the same cases.
- [ ] The committed OpenAPI document is regenerable and current (`--check` in the gate), and
      every response the server emits validates against it.
- [ ] The repository-wide quality gate.

## Exit gate

A learner completes a calibration and a lesson end to end in the browser, spending no tokens
on machine-scorable work, while an AI assistant and the CLI remain usable in the same
workspace. No scoring, selection, or stop-rule logic exists outside Python, and no assessment
claim outlives the evidence it rests on.

## Sequencing

Proposed **before Stage 8**. Stage 8 is four weeks of real Polish learning; doing that through
chat would spend a large budget on string comparison and would measure the interface as much
as the learner.

10.1 and 10.3 are the critical path: deterministic scoring is worthless without the snapshot,
and button presentation is impossible without the pack work. Neither needs the server.

## Not attempted, and why

- **A mobile or hosted client.** Learner state is local and the workspace has no remote.
- **Offline model inference for rubric scoring.** The queue in 10.7 is indifferent to who
  answers it.
- **Multi-learner support in one server.** A workspace is one learner; a second workspace runs
  its own.
