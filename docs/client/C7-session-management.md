---
title: "C7 — Session management in the browser"
stage: C7
status: shipped
depends_on: [C4]
---

# C7 — Session management in the browser

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal, deliberately narrow.** The plan, the staging, and the close become operable in the
browser. **Teaching does not move.**

**Why the narrowing.** Core holds no teaching content and is not going to.
`_activity_prompt` in `services/sessions.py` returns `f"{kind} for {block}: {objective}"`, and
its docstring is explicit: *"The skill turns this into teaching. Core states the demand — what
kind of work, on what — and nothing about how to say anything in any language."* Wrapping
`plan` and `session` commands therefore cannot deliver exercises, explanations, feedback or
scores. An earlier draft of this stage claimed it could.

**What C4 does not already supply.** C4 delivered the server, the launch token, the
pending-operation slot, and the `/runs` routes. `ROUTES` in `client/routes.py` exposes
assessment operations only. Every session route, request and response schema, read model, and
OpenAPI entry below is a deliverable of this stage.

## 1. Where observations come from

The browser neither teaches nor judges, so it cannot be where an attempt's score, task type,
modality, target, or assessor is decided. C7 makes one choice and refuses the other two:

| Route in                                                   | In C7   | Why                                                                                                                                                                                          |
| ---------------------------------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Inspect work staged elsewhere** (`session log` by a skill, `session ingest-package`) | yes     | The common case. The page reads what is staged and never re-describes it.                                                                                                                   |
| **Import a batch another producer wrote**                  | yes     | A `lingua.session.events.v1` file is the same payload `session log --input` takes. The producer's identity (key, sequence, event IDs, assessor) travels with it unchanged.                |
| **Learner-entered observations**                           | **no**  | An attempt needs a score and an assessor, and the page has neither. A learner-entered attempt would be a self-assessment and needs its own provenance rules, its own `assessor_kind = learner` caps, and a way to keep batch sequences from colliding with the skill's. That belongs to the successor proposal. |

### 1a. Import

- [x] **Operation.** `POST /sessions/{session_id}/batches` with `{"batch": <lingua.session.events.v1>}`
      calls `sessions.log(paths, batch=…, session=…)`, the same service as `session log`. Retention,
      `assert_materializable`, block and activity ownership, duplicate-event refusal and the
      batch-size cap (`MAXIMUM_BATCH_EVENTS` = 200) all apply as they already do. The import
      route has no second implementation of any of them.
- [x] **The key is the producer's, read where it lives.** The batch's `idempotency_key` sits at
      `body.batch.idempotency_key`. Both retry layers look only at the top of the body today:
      `server.py` decides attempts with `request.optional(route.key_field, str)`, and
      `app.js` `request()` sets `mayRetry` from `body.idempotency_key` or
      `body.submission_key`. Without a change, an import would be retried as a keyless
      mutation, which means not at all. C7 changes both, and copies nothing:
      - `Route.key_field` accepts a dotted path. The import route declares
        `key_field="batch.idempotency_key"`, and one helper (`Request.key(route)`) resolves it for
        the attempts rule. A path that does not resolve to a non-empty string counts as keyless,
        exactly as a missing top-level key does today.
      - `request()` takes an explicit `key` option, consulted before the body is sniffed. The
        import call passes `key: batch.idempotency_key`. The existing top-level detection is
        unchanged for every other route.
      - The key is never mirrored to the top level. Two copies could disagree, and the server
        would then have to decide which one the producer meant.
- [x] **The file keeps the producer's identity.** The page never mints or rewrites
      `idempotency_key`, `sequence`, or `event_id` for an imported batch. Those are the
      producer's count and the producer's names, and rewriting them would make one observation
      look like two. A retry resends the file unchanged: from memory within a page load, and
      from the re-chosen file after a reload, because the body is never stored (§3a).
- [x] **A declared session must be this session.** Today `log` resolves
      `session or validated.session_id`, so an explicit session silently wins over the one the
      file names. C7 makes `log` refuse a mismatch (`session_batch_wrong_session`, naming both)
      at the service, so the CLI is covered too.
- [x] **Assessor provenance is declared, never defaulted.** `StagedAttemptPayload.assessor_kind`
      defaults to `"ai"`, which is correct for a skill flush and wrong for a file of unknown
      origin. The import route reads the **raw** JSON *before* validation and refuses any
      `attempt.observed` event that does not state `assessor_kind`
      (`session_import_assessor_required`, naming the event IDs). Validating first would
      have filled in the default and erased the distinction. `log` takes this as a keyword
      (`require_declared_assessor=True`) that only the import route sets. The CLI's skill
      flush keeps its default.
- [x] **The page shows the claim; it does not certify it.** Before sending, the page validates
      the file client-side only to the extent of "is this JSON", then shows the server's
      refusal or the staged result. The staged listing names each event's `assessor_kind` and
      `evidence_basis`. Nothing on the page says an imported batch was assessed by anyone: a
      file cannot be authenticated, so the page calls it *imported*. The audit entry records
      the command as `client.session.import` with the client actor, so the transport is on
      record next to the producer's own provenance.
- [x] Provenance fields a flush may not claim (`source`, `package_id`, `utterance_id`, …) are
      stripped by `_own_payload` as today. The import report lists any stripped field in
      `warnings` by event ID, so a file that tried to describe itself as a package is told so.
- [x] Packages (`session ingest-package`) are **not** importable through the page in C7. They
      carry audio and transcripts whose preflight is C5's, and the page has no upload path for
      them. Their staged events are visible like any other.

## 2. HTTP contract and read model

All routes are added to `ROUTES` with `request_schema`, `response_models`, and a summary, and
appear in the generated OpenAPI document. A route that names a session resolves the track **from
the session row**, not from `resolve_track(None)`. The current services call
`learner_service.resolve_track(database, track)` first, and on a workspace with two active
tracks that refuses `track_selection_required` before it ever reads the session. A `track` sent
alongside a session must match it (`session_track_mismatch`). The page always names sessions by
ID and never relies on "the open one", so `ambiguous_session` cannot reach it.

| Method | Path                                   | Operation           | Service                         | Retry (§3)     |
| ------ | -------------------------------------- | ------------------- | ------------------------------- | -------------- |
| GET    | `/tracks`                              | `track.discover`    | new, over `learners.list_tracks` | read          |
| GET    | `/sessions?track=&state=open,recoverable` | `session.discover` | new `sessions.discover`         | read           |
| POST   | `/sessions`                            | `plan.create`       | `sessions.create`               | replay (key)   |
| GET    | `/sessions/{id}/screen`                | `session.screen`    | new `sessions.screen`           | read           |
| GET    | `/sessions/{id}/staged?offset=&limit=` | `session.staged`    | `sessions.staged` → `StagedListing` | read       |
| POST   | `/sessions/{id}/start`                 | `session.start`     | `sessions.start`                | state-idempotent |
| POST   | `/sessions/{id}/resume`                | `session.resume`    | `sessions.resume`               | state-idempotent |
| POST   | `/sessions/{id}/batches`               | `session.import`    | `sessions.log`                  | replay (key)   |
| POST   | `/sessions/{id}/close`                 | `session.close`     | `sessions.close` (`outcome` in body) | replay (key, hashed) |
| POST   | `/sessions/{id}/abandon`               | `session.abandon`   | `sessions.abandon`              | replay (key, hashed) — new |
| POST   | `/sessions/{id}/recover`               | `session.recover`   | `sessions.recover` (`{id}` is the source) | replay (key, hashed) — new |

- [x] **`plan.create` body:** `track`, `minutes`, `mode`, `energy`, `intent`, `correction_mode`
      (optional), `idempotency_key` (required from the page). Its response is `SessionReport`.
- [x] **`session.close` body:** `outcome` (`completed` | `partial`), `actual_minutes`,
      `fatigue`, `summary`, `discard_blocks` (block IDs), `expected_staging` (the `staging.digest`
      the learner confirmed; required from the page, optional for the CLI, §3d),
      `idempotency_key` (required from the page). Its response is `CloseReport`.
- [x] **`session.recover` body:** `into` (target session ID, required: the page never lets the
      service pick), `events` (staged event IDs, **required and non-empty** from the page, §4c),
      `idempotency_key`. Its response is `RecoverReport`.
- [x] **`sessions.staged` returns `StagedListing`**, not a bare tuple: `total` is the count
      before `limit`, and `offset` is added. A listing that stops at 100 without saying so looks
      exactly like a session holding 100 events.
- [x] **`sessions.staged` takes a `status` filter** (`staged` | `materialized` | `discarded` |
      `rejected`; default all). The filter is applied in SQL, and `total`, `offset`, and `limit`
      all count the **filtered** set. Today's reader returns every row a session ever staged,
      so a recovery review drawn from it would preselect events that have already moved
      elsewhere. The recovery review always asks for `status=staged`. The run screen asks for
      all statuses and labels each row with its status.

### 2a. `GET /sessions` — discovery

- [x] `sessions.discover(paths, track, states) -> SessionListReport`, newest first. Each entry
      has `session_id`, `status`, `mode`, `planned_minutes`, `planned_at`, `staged_events`,
      `batches`, `finalized`, and `recoverable`.
- [x] `state=open` means `planned`, `active`, or `closing`. `state=recoverable` means terminal
      status (`completed`, `partial`, `abandoned`) **and** at least one `staged`-status event.
      These are exactly what `recover` will accept as a source, decided by one predicate
      that `recover` and `discover` share. In practice that means an abandoned session: a close
      moves every `staged` row to `materialized` or `discarded` in its own transaction. The
      predicate is still stated on rows rather than as "was abandoned", so a restored or
      hand-repaired workspace is described by what it holds.
- [x] The default is both states. The page reads it at boot; nothing about a session lives only
      in the tab.
- [x] Discovery is **per track**, never cross-track. A run and a session belong to one
      learner, and a list mixing two learners' sessions would invite recovering one learner's
      work into another's plan. `track` is optional in the schema so the CLI keeps its
      single-track fallback, but the page always sends it (§4a). Without it, a workspace with
      two active tracks gets `track_selection_required`, unchanged, with the details naming
      the tracks so a caller can recover.

### 2c. `GET /tracks` — choosing a track

- [x] `tracks.discover` → `TrackListReport` over `learners.list_tracks`. Each entry has
      `track_id`, the owning user's `display_name` (`TrackRecord` has no label of its own),
      `target_language`, `proficiency_framework`, `pack_key`, `status`, `is_primary`, and `selectable`
      (`active`, or the workspace's only track, mirroring `resolve_track`). It carries no
      preferences, consent settings, or learner text: the picker needs a name, not a profile.
      The report also carries `default_track_id`: what `resolve_track(None)` would choose, or
      `null` when that would refuse.
- [x] The assessment page has the same gap today (`GET /runs` without `track` on a two-track
      workspace). C7 adds the route and the picker for sessions only. Pointing the assessment
      page at the same picker is a one-line follow-up, recorded rather than done here.

### 2b. `GET /sessions/{id}/screen` — one read for the page

`SessionScreen`, built on one reader connection:

- [x] `session`: the `SessionReport` as `show` returns it, including the plan, each block's
      `rationale`, `omissions`, `resume_from`, and `finalization`.
- [x] `staged`: the first page of `StagedListing` (`total` included). Summaries come from
      `_staged_summary` and never quote the learner.
- [x] `staged_by_block`: count of `staged` events per block ID, plus an `unattributed` count for
      events with no block. That count includes every recovered event (§4c), which the
      partial-close review needs.
- [x] `batches`: each batch's `sequence`, `idempotency_key`, `content_hash`, `event_count`, and
      `created_at`. These are producer identifiers, never learner text. The page uses them to
      word its question after a reload interrupted an import (§3a), never to confirm one.
- [x] `staging`: `{digest, count}` for the events a close would consume now, where
      `digest = staging_digest(session)` (§3d). The confirmation in §4b is drawn from the same
      read that produced it.
- [x] `missing_batch_sequences`: from `_missing_batch_sequences`. The page uses it to say
      *before* the learner presses "close as complete" that it will be refused, and that a
      partial close is the remedy.
- [x] `actions`: the operations legal now, as route operation IDs (`session.start`,
      `session.import`, `session.close`, …), derived from the same status table as
      `_next_actions`. The page draws buttons from this list and never re-derives the lifecycle.
      `_next_actions` is refactored to return operation IDs, with the CLI rendering its
      existing words from them, so the two cannot drift.
- [x] `closing_interrupted`: true for `status = closing` with no finalization (§4b).

## 3. Retry semantics, per operation

The page keeps the C4 rules for every operation except import: one operation in flight, the
whole request (method, path, body, key) is written to the `linguawiki.pending` sessionStorage
slot **before** it is sent, it is resent unchanged on reload, and it is cleared on any
definitive answer, refusal included. Import is the exception, because its body is learner
text that retention has not yet seen (§3a). What differs between operations is what each
service does with a resend whose first send already committed. Before C7 the answer is
inconsistent, and the table fixes it:

| Operation         | Identity                                  | Lost response, same request resent                          | Same key, different request |
| ----------------- | ----------------------------------------- | ------------------------------------------------------------ | --------------------------- |
| `plan.create`     | key + request hash (exists)               | replays the plan                                             | `idempotency_conflict` (exists) |
| `session.start`   | none; the status is the identity          | `active` → returns the report (exists)                       | n/a                         |
| `session.resume`  | none; the status is the identity          | `active` → returns; terminal → refusal, page re-reads screen | n/a                         |
| `session.import`  | producer's key + content hash (exists)    | `duplicate: true`, looked up **before** the loggable check, so a replay still answers after the session has moved to `closing` or beyond | `idempotency_conflict` (exists) |
| `session.close`   | key + **request hash** (new)              | replays the stored `CloseReport`, `replayed: true`           | `idempotency_conflict` naming the differing fields (new) |
| `session.abandon` | key + request hash (**new**)              | replays the current report, `replayed: true`                 | `idempotency_conflict`      |
| `session.recover` | key + request hash (**new**)              | replays the stored `RecoverReport`, `replayed: true`         | `idempotency_conflict`      |

- [x] **Close binds its key to the whole request.** Today a finalized session replays its stored
      result if only `outcome` matches. A retry with different `actual_minutes`, `fatigue`,
      `summary`, or `discard_blocks` is answered as if it were the same close, and the caller
      believes the blocks it just excluded were excluded. `close` computes
      `idempotency.request_hash(session_id, outcome, actual_minutes, fatigue, summary_hash,
      discard_blocks=sorted(...), expected_staging)` and writes it with `idempotency.payload` into the
      `session.closed` domain event it already records under that key. Keyless closes (the
      CLI's default) keep today's outcome-only comparison. The summary is hashed and never
      stored in the event, because it is the learner's text. The order and the legacy branch
      are in §3b.
- [x] **Abandon gains an optional key.** Today a resend after a lost response meets
      `invalid_session_transition` (`abandoned` → `abandoned`), which looks to the page like a
      failure of something that succeeded. With a key, the hash covers `session_id` and the
      reason's hash and is stored on `session.abandoned`. A matching retry replays. A key on a
      session abandoned by somebody else (no event under that key) is still the transition
      refusal, which now names the existing `abandoned` status and the time it was set.
- [x] **Recover gains an optional key, and a retry no longer depends on what moved.** Today a
      resend with explicit events fails `staged_event_not_recoverable`, because they are now
      `discarded`. A resend with no events "recovers" 0 and says the session holds nothing,
      when in fact everything went where it was asked to. The rules are in §3c.
- [x] **The import sequence is the producer's.** `batch_sequence_taken` therefore means the
      producer and somebody else disagree about the count. It is shown verbatim and never
      "fixed" by renumbering. `duplicate_session_event` means the observations are already
      staged, for example from the CLI or another tab, and the page says so and draws the
      screen.
- [x] **A refused operation is cleared from the pending slot, and its remedy works.** After
      `session_batch_gap` the page offers a partial close, which is a new request with a new
      key. After `idempotency_conflict` it offers nothing automatic and shows both requests.
- [x] **Unreachable is the only pending state.** As in C4, only `client_unreachable` leaves the
      slot set. On reload the slot is resent before anything is drawn. Then the screen is read
      again and drawn from the server, never from the replayed response alone.

### 3a. An import's body never reaches browser storage

An imported batch can carry a learner's full responses. Retention runs on the server, at
`_retained_payload`, so a copy written to sessionStorage before sending is a copy no consent
rule ever reaches. It would also survive every reload for as long as the import stayed pending.
The rule is not conditional on the track's consent. The page cannot know what retention
will keep until the server has applied it, and a rule that is the same for every track cannot
drift from the one the server applies.

- [x] **What is stored.** Before sending, the page writes only a marker to its own slot,
      `linguawiki.import`: `{session_id, idempotency_key, sequence, event_count, file_sha256}`.
      `file_sha256` is the SHA-256 of the file's bytes (`crypto.subtle`). It is not the
      server's canonical content hash, which the page cannot reproduce. These are producer
      identifiers and a digest, never a payload, a response, or a file name. Nothing goes to
      `localStorage`, IndexedDB, or the URL.
- [x] **In-page retry.** The body is held in memory for the life of the request only, and a
      busy refusal or dropped connection within the same page load resends it from memory under
      the same key (§1a). The reference is dropped on a definitive answer.
- [x] **Settling after a reload: a key is not content.** Finding the marker's key in `batches`
      proves only that *some* batch owns that key. Suppose batch A already held key K and the
      import was batch B reusing K with different content. `log` correctly refused B as
      `idempotency_conflict`, the response was lost, and K is in `batches` because of A.
      Confirming from the key would report B as imported when it was refused. The page cannot
      compare content itself: `batches.content_hash` is the server's canonical hash of the
      validated events, which the page cannot reproduce, and the marker's file digest has no
      counterpart on the server. So **content is verified by `log` itself, never by the
      page**:
      - With a marker present, the page reads the session screen. The server is a
        single-threaded `HTTPServer`, so this read is answered only after any in-flight import
        has finished. The page uses `batches` only to choose its wording: "a batch under this
        key is on record; choose the file again to confirm it is the one you imported", or "the
        import did not land; choose the file again to send it". It never confirms or clears from
        this read.
      - A re-chosen file whose SHA-256 matches the marker is sent through `POST …/batches`
        under the marker's key, as an ordinary import. `log` decides: `duplicate: true` means
        the stored batch has this content, so the import is confirmed. A fresh `BatchReport`
        means it had not landed and now has. `idempotency_conflict` means the key belongs to
        different content, so the import is reported **refused**, naming the conflict, and
        nothing is confirmed. The marker is cleared on any of these definitive answers.
      - A re-chosen file whose SHA-256 does not match is a different file. The page says the
        original import's outcome is still unknown and offers to send this file as a new
        import; the marker is cleared only if the learner accepts.
      - Dismissing the marker clears it, and the page says the outcome was never established.
      - The page never sends a file digest for the server to store. A digest the page supplies
        is the page's claim about its own upload, and `log`'s comparison of the content it
        actually received is the check that already exists.
- [x] **Other persisted bodies.** `summary` (close) and `reason` (abandon) are stored by the
      server verbatim, on `sessions.summary`, with no retention step between, so the pending
      slot keeps no more than the workspace will. If either ever gains a retention rule, it
      moves under this section's rule in the same change.

### 3b. Close: the order of the checks, and the legacy branch

`idempotency.resolve` refuses a recorded event with no `request_hash` as unvouchable, and it is
right to: the hash is missing from a damaged payload exactly as it is from an old one. So
`close` does not route old finalizations through it. It decides which case it is in first,
on two independent markers:

- [x] C7 adds `"close_request": "1"` to `_calculation_versions()`. It is stored on every new
      finalization (`calculation_versions_json`, and inside `result_json`), and the
      `session.closed` payload written through `idempotency.payload` always carries
      `request_hash`.
- [x] **Order, for a keyed close:**
      1. `_assert_close_key_belongs`, unchanged and first. A key that closed another session,
         or this session closed under another key, is `idempotency_conflict` before anything
         stored is read. This keeps session/key ownership in both directions.
      2. No finalization for this session: `idempotency.resolve(key, "session.closed", hash)`,
         which still refuses a key that performed some other operation, then the normal
         close. A `closing` session retried under its own key arrives here, because nothing
         committed.
      3. A finalization under this key whose `calculation_versions` carries `close_request`:
         `idempotency.resolve`, which replays on a matching hash and refuses
         (`idempotency_conflict`) on a different hash or a missing or unreadable one.
      4. **Legacy:** a finalization whose `calculation_versions` lacks `close_request`, **and**
         whose `session.closed` event under this key reads as an object whose keys are exactly
         the pre-C7 set (`attempts`, `errors`, `evidence`, `outcome`, `staged_consumed`).
         Today's rule applies: the same outcome replays and a different one is
         `session_already_finalized`. The replay's `warnings` say the close predates request
         hashing, so only its outcome was compared.
      5. Anything else, such as a missing event, a payload that will not parse, legacy versions
         with a C7-shaped payload, or the reverse, is `idempotency_conflict` with reason
         `recorded request is unknown`. Damage is never read as age.
- [x] `idempotency.resolve` itself is unchanged. Abandon and recover need no legacy branch:
      their events carried no idempotency key before C7, so no old key can be looked up.

### 3c. Recover: hash the request, snapshot the result, replay before checking anything

Hashing the *resolved* event IDs makes a retry unrecognisable. The first call with no explicit
events resolves every staged event, and a retry resolves none because they have all moved. The
hashes differ, and the promised replay becomes a conflict.

- [x] **The hash is the request as asked:** `request_hash(source_session_id, target, selection,
      events)`. `target` is the explicit ID, or `null` meaning "the open session". `selection` is
      `all` (no events given) or `explicit`. `events` is the sorted explicit IDs, or empty. No
      part of the hash depends on what the database holds at the time of the call.
- [x] **The expansion is a snapshot.** The `session.staged_recovered` payload, written through
      `idempotency.payload`, records what the request resolved to: the resolved
      `target_session_id`, `batch_id`, each recovered pair `(source staged_event_id → new
      staged_event_id)`, `skipped`, and the warnings. `RecoverReport` gains
      `recovered_events` and `replayed`. A replay rebuilds the report from the snapshot.
- [x] **Order:** open the writer, resolve the track, resolve the source session (existence and
      track ownership only), then `idempotency.resolve`. A replay returns **before** the
      source-terminal check, before the target is resolved or checked as loggable, and before
      availability is read. A retry therefore still answers after the target has closed, or
      when the CLI's implicit "open session" no longer exists.
- [x] The recovery batch's idempotency key becomes the caller's key when one is given. It is
      `recovery:{source}:{sequence}` otherwise, unchanged. A keyless recover behaves exactly as
      today.

### 3d. Close credits what was confirmed, or nothing

The request hash protects a retry. It does not protect against the work changing underneath
the learner. A skill can `session log` another batch, or a `recover` can land in this session,
after the page read the screen and before `close` takes the writer lock. `_staged_rows` would
then materialize events the confirmation never mentioned.

- [x] **One definition of "what a close would consume".** `staging_digest(database,
      session_id)` is `canonical_hash` of the sorted `staged_event_id`s with `status = 'staged'`
      on the session. That is the same predicate `_staged_rows` reads, and the two share one
      query so they cannot drift. `discard_blocks` is not part of it, because it is already in
      the request. The screen reports it as `staging.digest` with the count (§2b).
- [x] **Checked before anything is written.** `close` takes `expected_staging`. In the order of
      §3b, the check runs after key ownership and after any replay, because a finalized
      session's replay must answer however its staging looks now. It runs after the terminal
      and batch-gap refusals, inside the writer, and **before `closing` is written**. A mismatch
      is `session_staging_changed`, naming the expected and current digests and the current
      count. Nothing is written, so the session stays `active` and still closable.
- [x] A session already `closing` cannot have changed: `_assert_loggable` refuses it as a
      `log` destination and as a `recover` target. A retry of an interrupted close under its
      original `expected_staging` therefore passes.
- [x] `expected_staging` is in the close request hash (§3), so a close re-confirmed after this
      refusal is a new request with a new key, and never a replay of the old one.
- [x] **The page re-confirms.** On `session_staging_changed` it re-reads the screen and redraws
      the confirmation from the new read. It states that the count went from the one the learner
      confirmed to the current one, and it sends nothing until the learner confirms again.
- [x] The CLI gains `--expect-staging <digest>`, and `session status` prints the digest. Without
      the flag the CLI behaves as today. The skill, which writes and closes in one process,
      has no window to protect.

## 4. Flows

### 4a. Fresh launch

- [x] Boot order: resend any pending operation (§3). Settle an import marker (§3a). Choose the
      track. Resume a recovery workflow record (§4c). Then `GET /sessions?track=<id>`. Each step
      reads the server afresh rather than trusting the previous step's response.
- [x] **Choosing the track.** The page reads `GET /tracks` (§2c). If it holds a stored
      `linguawiki.track` that the listing still reports as selectable, that is the track.
      Otherwise, exactly one active track (or, with none active, exactly one track, mirroring
      `resolve_track`) is chosen without asking. Anything else draws a picker listing each
      track's user, language, framework, and status, and nothing session-related is
      requested until the learner picks. The choice goes to `linguawiki.track` and is shown on
      every screen, with a "switch track" control that clears the session slot. A stored track
      the listing no longer reports, or reports as unselectable, is dropped with a message
      naming it, and the picker is drawn.
- [x] The page sends `track` explicitly on `GET /sessions` and `POST /sessions`. It never leaves
      the track to `resolve_track(None)`, which is what refuses `track_selection_required`
      on a workspace with two active tracks.
- [x] Exactly one open session: draw its screen. Several: list them, each with status and
      staged count, and the learner picks. None: offer "plan a session" and list recoverable
      sessions, if any, with their staged counts.
- [x] The session being drawn is kept in sessionStorage (`linguawiki.session`), alongside the
      C4 run slot, and never in the URL.

### 4b. Run and close

- [x] `planned` → "start". `active` → import a batch, view staged work, close, partial close,
      abandon. The screen states plainly that teaching happens elsewhere (the skill, or a
      session package), so the learner is never waiting for an exercise the browser cannot produce.
- [x] Staged work is listed with its batch sequence, kind, block, evidence basis, assessor, and
      summary, and is labelled **not yet credited** until the session is finalized. Nothing on
      the page shows a stage change, an error, or a score as having happened before the close.
- [x] Close is one deliberate act with its own confirmation, which says what will be credited:
      `staging.count` staged events across N blocks, minus the discarded ones. Both the text and
      the `expected_staging` the close sends come from the same screen read, and a change in
      between is refused and re-confirmed (§3d). After it, the page draws
      the `CloseReport`: stage changes with their explanations, attempts, evidence, errors,
      follow-ups, and every warning, including "N of M planned core blocks produced
      observations" and the batch-gap warning on a partial close.
- [x] **Partial close with exclusions.** The learner ticks blocks to discard from
      `staged_by_block`. The `unattributed` count is shown separately and cannot be excluded,
      because `discard_blocks` matches on `block_id`. The page says those events will be credited.
      To exclude them, abandon instead and recover a reviewed subset (§4c). The confirmation
      also says that a discarded block's events are kept for audit and **cannot be recovered
      later**: the close marks them `discarded`, and `recover` takes only `staged` rows.
- [x] **A closing session** (`closing_interrupted`). If the pending slot holds the close, it was
      already resent at boot (§3). Otherwise the page offers "finish closing", a new close
      with a new key whose arguments the learner sets again (nothing of the first attempt
      committed, so there is nothing to conflict with), and "abandon". It never offers import,
      since `_assert_loggable` refuses a closing session.

### 4c. Recovery

- [x] **Source.** A session from `GET /sessions?state=recoverable`. Its still-recoverable events
      are loaded **in full**, page by page through `/staged?status=staged`, before any recover
      control is enabled. `total` counts that filtered set, so "all loaded" is checkable. A
      selection made from a partial listing would silently leave events behind, and one made
      from an unfiltered listing would preselect events an earlier recovery already moved.
- [x] **Review.** The learner selects events (all are preselected, each shown as in §4b). The page
      always sends the explicit, non-empty list it showed. It never sends an empty list, which the
      service reads as "everything", including anything the page did not show.
- [x] **Destination.** `recover` needs a target that is `active` and is not the source. The page
      offers the open sessions from discovery: a `planned` one is started first, an `active` one
      is used as is, and a `closing` one is not offered. If there is none, the page plans a new
      session with the learner's minutes and mode, starts it, and recovers into it.
- [x] **The workflow is its own persisted record.** The pending slot holds one request and is
      cleared the moment that request is answered, so after the plan is acknowledged a
      reload would remember neither the source, nor the selection, nor what was left to do.
      Following C6's batch slot, the page keeps `linguawiki.recovery`:
      `{track_id, source_session_id, selected_event_ids, destination: {kind: "existing" | "new",
      session_id | null, plan: {minutes, mode, energy} | null}, keys: {plan, recover},
      steps: {planned, started, recovered}}`.
      - `track_id` is the source's track, read from the source session, and is the `track`
        sent to `plan.create` for a new destination. Changing tracks while a record exists
        asks whether to finish or cancel the recovery first. On resume, the record's track is
        selected before its steps run, whatever `linguawiki.track` says. An existing destination
        is offered only from discovery on that track, and `recover`'s same-track resolution
        refuses anything else regardless.
      - It is written **before the first request**, with both keys minted then, and updated
        after **each acknowledged** step: the plan's `session_id` is recorded the moment
        `plan.create` answers. It holds identifiers and choices only, never learner text.
      - On boot it is resumed from the first step not marked done, each step under its stored
        key: plan (replays to the same session), then start (state-idempotent), then recover
        (replays by key, §3c). A step marked done is not re-sent, but the target's state is
        re-read before the next step.
      - It is cleared when recover is acknowledged (first answer or replay) and the target's
        screen has been drawn, or when the learner cancels. Cancelling after the plan step leaves
        the planned session in place, and the page says so.
      - **A refusal mid-flow keeps the choices.** If `staged_event_not_recoverable` comes back
        because somebody else moved events, the page re-reads the source with `status=staged` and
        asks for review again. It keeps the destination and mints a new recover key, because the
        selection is a new request. If the destination is no longer loggable, the page asks for
        another and keeps the selection.
- [x] Recovered events carry no block (`recover` does not copy `block_id`), so they count as
      `unattributed` on the target. The review above is where they are excluded, not the
      target's partial close.
- [x] After recovery the target's screen shows the recovered events as staged and uncredited,
      and the `RecoverReport` warning that they are credited to nothing until the target closes.

## 5. Tests

### `tests/integration/test_client_sessions.py` (HTTP and service)

- [x] Every session route is in the OpenAPI document, and a model-produced response for each
      validates against it, first with only required fields and again with every optional
      field set.
- [x] A session named by ID on a workspace with two active tracks resolves through the session.
      A mismatched `track` is refused.
- [x] Discovery: open and recoverable states are disjoint. A terminal session with zero staged
      events is not recoverable, and one `recover` accepts is.
- [x] Import: staged through `log` with retention applied; producer key, sequence, and event IDs
      are stored unchanged; a missing `assessor_kind` is refused before anything is written and
      names the events; a file naming another session is refused (CLI path too); stripped
      provenance fields are named in warnings; a resent file is `duplicate: true` after the
      session has moved to `closing` and after it has closed.
- [x] Import key path: the server's attempts rule treats `batch.idempotency_key` as the key. An
      import that meets a held writer lock is retried by the server and, past its budget,
      answered `database_busy` with `Retry-After`. A body whose nested key is missing or
      blank is treated as keyless. No route other than import reads a nested key.
- [x] Staged listing filter: `status=staged` returns only recoverable rows, and `total` counts
      the filtered set across `offset` pages. After recovering a subset, the source's
      `status=staged` listing holds exactly the remainder, and recovering the remainder by
      explicit IDs succeeds.
- [x] Close: the same key and request after a committed close whose response was dropped replays
      with `replayed: true` and writes nothing. The same key with different `discard_blocks`,
      `actual_minutes`, `fatigue`, or `summary` is `idempotency_conflict`. A keyless retry
      behaves as before. The domain event never contains the summary text.
- [x] Close legacy branch (§3b): a finalization written in the pre-C7 shape (no
      `close_request`, legacy event keys) replays on outcome with the warning and refuses a
      different outcome. Each kind of damage is `idempotency_conflict`, not a replay: the
      C7 versions with the hash removed, the legacy versions with an extra payload key, an
      unparseable payload, and a missing event. Ownership still comes first: a legacy
      finalization retried under a key that closed another session, or a key this session was
      not closed under, is refused.
- [x] Close staging (§3d): the screen's `staging.digest` and the set `_staged_rows` consumes
      agree. A batch logged, and separately a recover landing, between the screen read and
      the close make the close refuse `session_staging_changed`, with the session still `active`,
      no `closing` written, and nothing materialized. A close re-sent with the new digest and a
      new key credits the full set. A retry of an interrupted `closing` close under its original
      digest finishes. A finalized session's keyed replay answers whatever the digest says. A
      keyless CLI close without `--expect-staging` behaves as before.
- [x] Tracks (§2c): `GET /tracks` lists both tracks on a two-track workspace with
      `default_track_id: null` and no preferences or consent fields. `GET /sessions` without
      `track` there is `track_selection_required`, naming both tracks. With `track`, it lists
      only that track's sessions.
- [x] Abandon: a keyed retry after a lost response replays. A different reason under the same
      key conflicts.
- [x] Recover (§3c): a keyed retry after a lost response replays the original report from its
      snapshot, `replayed: true`, for an explicit selection and for an `all` selection (whose
      retry would now resolve nothing). It still replays after the target has since closed, and
      after the implicit open session no longer exists. A different selection, mode, or target
      under the same key conflicts. A planned target is refused by the service, so the page's
      start step is what makes the flow work.
- [x] Credited exactly once: plan → start → import two batches → close. A second close with the
      same key, a reload-replayed import, and a recover of the closed session's (now
      materialized) events each leave attempt and evidence counts unchanged.
- [x] `session_batch_gap` on a completed close, then a partial close with a new key, succeeds.
- [x] Nothing in the served interface implies the browser is teaching: the served HTML/JS
      contain no route or control that produces a score, and every staged event shown carries the
      producer's assessor.

### `tests/browser/test_client_sessions_browser.py` (Playwright, required)

HTTP tests cannot show that a control is reachable, that staged work is visible, or that a
reload recovers, so each of these runs in a real browser against `client serve`, using the
fixtures the C6 browser tests use:

- [x] **Plan → stage → close.** Plan in the page, see the rationale and the omissions, start,
      import a batch file, see each event listed as not yet credited, close, see the
      `CloseReport`, and confirm through the CLI that attempts were written once.
- [x] **Uncredited work is visible before closing.** A batch flushed by the CLI while the page is
      open appears on the next screen read. Its events show as staged and uncredited, and no
      stage change is drawn.
- [x] **Interrupted close, response lost.** Intercept `POST …/close` with Playwright `route` so the
      request reaches the server and the response is aborted. Reload. The pending close is
      resent with the same key, the page draws the replayed report, and the CLI shows one
      finalization.
- [x] **Interrupted close, closing marker left.** Put a session in `closing` with no finalization
      (fault injected in the materialize step). Launch the page fresh. It offers "finish closing"
      and "abandon", offers no import, and finishing it credits once.
- [x] **Work arrives during confirmation.** Open the close confirmation, then flush a batch
      through the CLI before confirming. Confirming is refused. The page redraws the
      confirmation with the new count and sends nothing until the learner confirms again, and
      then the close credits every event, the late batch included.
- [x] **Fresh launch with two active tracks.** No stored track: the page draws the picker and
      makes no `/sessions` request until a track is picked. After picking track B it lists only
      B's sessions, and a reload keeps B. Recover an abandoned B session into a new session: the
      new session is planned on B. Interrupt that flow after the plan is acknowledged, set
      `linguawiki.track` to A, and reload: the page selects B from the recovery record before
      resuming, and no session is planned on A. A stored track that has since been
      paused, on a workspace that still has another active track, is dropped with a message
      and the picker is shown.
- [x] **Partial close with a discarded block.** Exclude one block. The unattributed count is shown
      as not excludable, and the report's discarded count matches.
- [x] **Import on a no-retention track, response lost.** On a track that refuses transcript
      retention, import a batch whose attempts carry full `response` text. Let the request reach
      the server and abort the response. Before reloading, read `sessionStorage`,
      `localStorage`, and IndexedDB: none holds the response text or any event payload, and
      `linguawiki.import` holds only the marker fields. Reload: the page finds the key in
      `batches`, reports the import confirmed, and clears the marker. The staged row holds the
      retained form only.
- [x] **Conflicting key, response lost.** Stage batch A under key K through the CLI. In the page,
      import batch B, which carries K with different events, and abort the response. Reload:
      the page does **not** report B imported even though K is in `batches`. It asks for the
      file, sends B under K, and reports the `idempotency_conflict` as a refusal. A's events
      are the only ones staged, and the marker is cleared.
- [x] **Import that never landed.** Abort the import *request* (it never reaches the server),
      then reload. The page asks for the file again. The same file is sent under the marker's
      key and staged once. A different file clears the marker and imports as new.
- [x] **Import meets a busy writer.** Hold the writer lock (as the C6 poll test does) during an
      import. The page shows the waiting status, resends from memory under the same key once
      the lock clears, and the batch is staged once. Abort the first response of a second import
      within the same page load: the in-page retry replays (`duplicate: true`) rather than
      staging twice.
- [x] **Reviewed recovery.** Abandon a session holding staged work. On a fresh launch it is listed
      as recoverable. Review it, deselect one event, and recover into a newly planned session.
      The recovered events appear staged on the target, and the deselected one stays on the
      source. Then revisit the source, which is still recoverable: only the deselected event is
      listed and preselected, and recovering it succeeds.
- [x] **Interrupted between acknowledged steps.** For each boundary (after the plan is
      acknowledged and before start is sent; after start is acknowledged and before recover is
      sent), hold the next request with Playwright `route` so it never reaches the server, and
      reload. The `linguawiki.recovery` record survives, and the flow resumes at the next step
      with the same source, selection, destination, and keys. Exactly one session is planned and
      one recovery batch written.
- [x] **Recovery retry.** Abort the recover response, then reload. The replay reports the
      original counts and events, not "nothing to recover", and the record is cleared.
- [x] **No teaching implied.** The run screen shows the "teaching happens elsewhere" notice and
      no exercise, prompt input, or answer control.

## Gate

- [x] Release gate, because this stage adds routes and published schemas and changes CLI output
      (`_next_actions`): `./.tools/uv run python scripts/verify.py`. The OpenAPI document and
      CLI snapshots move. Regenerate them and review the diff rather than accepting it. No
      migration: the new request hashes live in `domain_events` payloads, through
      `idempotency.payload`.

## As built: deviations and deferred items

Rulings made during implementation, where the code differs from the text above:

- The page is `/sessions.html`, not `/sessions`: the shell answers exact paths before the token
  check, and `/sessions` is the discovery route.
- The import's audit row keeps the CLI's command name, `session.log`, with `actor = client`, which
  is the convention `client/routes.py` documents. The route's operation ID is `session.import`.
- The session batch report is published as `SessionBatchReport` (`BatchReport` remains an alias),
  because the assessment batch report already owns that component name.
- A recovery's batch key is `recovery:<caller key>`, and is checked before the write, because batch
  keys share one namespace with every flush.
- `SessionReport` carries `staging_digest`, so a JSON caller of `session status` can pass
  `--expect-staging`.
- `SessionListReport` carries the planner's `modes` and `energy_levels`, so the page keeps no copy of
  either. For the same reason the page's close form does not offer fatigue, which stays a CLI flag.
- `StagedEventReport` carries the producer's `assessor_kind`, shown as the producer's claim.
- The request transport is `transport.js`, shared by the assessment page and the session page. Each
  page keeps its own pending slot.
- Found in final review and fixed: a batch key that another operation already used, a recovery that
  names one event twice, and a recovery whose derived batch key is taken are each refused by name
  rather than reaching the unique index.

Deferred, with nothing in the release gate depending on them:

- A keyed abandon of a session that is already abandoned still raises the unchanged
  `invalid_session_transition`. §3 promised it would name the time the status was set.
- `SessionScreen.actions` lists `session.partial-close`, which is an outcome of `session.close`
  rather than a route operation ID.
- `staging_digest` parses every staged payload on each session read, and `screen` computes it
  twice.
- The import route publishes `batch` as an object rather than referencing
  `lingua.session.events.v1`.
- The recovery review pages by offset over a set that can shrink. The server's refusal and
  re-review cover it.
- The assessment page still discovers runs without a track (§2c).

## Done when

A session can be planned, staged by import or by work done elsewhere, and closed in the browser.
Every keyed operation survives a lost response by replaying, and a closing or abandoned session
found on a fresh launch has a working way forward. The close boundary is as visible to the
learner as it is in the data. Nothing in the interface implies the browser is teaching or
assessing.

## Successor, not in scope

- A browser that *conducts* lessons needs a teaching-content source and a judging integration,
  obeying the same provenance rules generated content already obeys.
- Learner-entered observations (self-assessed attempts, notes) need `assessor_kind = learner`
  caps, a batch origin the schema can record, and a sequencing rule that does not collide with
  the skill's own flush count.
- Importing session packages through the page needs C5's audio preflight behind an upload.

Each is worth a proposal of its own once C1–C7 are in use.
