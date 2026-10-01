---
title: "C4 — The assessment screen"
stage: C4
status: blocked
depends_on: [C3]
---

# C4 — The assessment screen

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** The first learner-visible milestone: a calibration answerable by button, with zero
model calls for machine-scorable work.

**Scope.** Tasks whose *task type* the server can score by comparison (`objective`,
`short-response` — `placement.MACHINE_SCORABLE_TASK_TYPES`), in the `text` and `audio`
modalities. Audio *capture* is C5; audio *playback* is here.

## Revised after review — 2026-10-01

Six gaps against the shipped C3 code, each checked against it before revising:

1. **Listening needed backend work the plan did not name.** C3 serves no audio, `record`
   takes no replay count, and `ServedAsset` is an ID and a hash. §3 now specifies the
   playback route, what a play is, where it is stored, and how it survives reload. The pilot
   pack ships no recordings *and* its six audio tasks carry the spoken sentence in `prompt`
   (C2a §3), so the fallback is named: such a task is not servable to the browser, because
   drawing its prompt would turn a listening task into a reading one.
2. **The page could not bootstrap.** `server._dispatch` checks the token before routing, and
   the token rides in the fragment, which a browser never sends. §1 now specifies a public,
   allowlisted shell served from the installed package, never from the workspace.
3. **"Machine-scorable modalities" conflated two axes.** Modality is what the learner's
   equipment allows; scoring is what the server can decide. The default run includes
   `writing`, which needs a judge; restricting to `text` alone makes `listening` not-tested,
   because its pilot tasks are all `audio`. §2a puts the restriction in shared Python as a
   run condition, and says what the page does with a run that predates it.
4. **Idempotency ownership was backwards.** C3 accepts caller-generated keys and issues
   none. §5 makes the page the owner: one key per operation, reused unchanged on retry,
   including across a reload, plus double-clicks and replayed serves of answered tasks.
5. **A fresh page had no way to find a paused run.** Every read route needs a run ID and the
   launch URL carries only the token. §6 adds discovery and a launch-time run reference,
   and the acceptance case now includes a server restart and a new token.
6. **The tests could pass with no working page and no computed score.** An HTTP exercise
   checks no button, playback, or browser authentication, and `assessor_kind="deterministic"`
   is a label the service accepts on a supplied score. §8 adds a browser-driven test and
   asserts response-only submissions, `score_source = "computed"`, and the scoring policy
   version, with model calls ruled out separately.

## 1. Shell and bootstrap

- [ ] **The shell is public; everything else is not.** An exact-match allowlist of `GET`
      paths — `/`, `/app.js`, `/app.css` and nothing else — is answered *before* the token
      check, because the first navigation cannot carry a header and the fragment never
      reaches the server. `Host` is still checked first on every request, shell included:
      DNS rebinding is the same attack whether the response is a page or an answer.
      Every other path, including any path under the shell's directory that is not on the
      list, goes through the token check unchanged and 404s after it.
- [ ] **The static root is the installed package, never the workspace.** Files ship as
      package data in `linguawiki/client/static/` and are read with `importlib.resources` by
      their allowlisted name — no path is ever joined from the request. A workspace holds
      learner data and no core source (AGENTS.md), so a root under it is one misconfiguration
      from serving the database. A test requests `/../data/…`, `/static/../…`, an encoded
      traversal, and a workspace filename, and asserts each is refused by the token check.
- [ ] Shell responses carry `Content-Security-Policy: default-src 'self'` (no inline script),
      `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and
      `Cache-Control: no-store`, so a cached shell cannot outlive the server that minted its
      token.
- [ ] The page reads `#token=…` (and optionally `&run=…`, §6) from `location.hash`, then
      calls `history.replaceState` to remove the fragment, so the token is not left in the
      address bar or in history. It holds the token in memory only and sends it as
      `X-LinguaWiki-Token` on every API call.
- [ ] No build step the learner has to run: plain ES modules, no bundler, no CDN.
- [ ] `client serve` (already printing and opening `launch_url`) is the launch command.
- [ ] Works at a small window size; this is a study tool, not a dashboard.

## 2. Answering

- [ ] Multiple-choice renders as **buttons**, from the snapshotted presentation. No prose
      typing, and no parsing of prompt text.
- [ ] A button submits its choice's **`value`**, verbatim — never an index or an option id.
      That is what the scorer compares against the served answer key (C2a); anything else
      needs a mapping the snapshot does not carry. `display` is what is drawn, and is never
      submitted.
- [ ] `short-response` renders as a single field with the expected shape shown.
- [ ] A task with no presentation record renders as free text rather than failing.
- [ ] **A submission carries the learner's `response` and nothing that decides its score**:
      no `score`, no `rubric`, no `confidence`. `assessor_kind` is `deterministic` because
      `record` requires it to compute, not because the page vouches for anything.
- [ ] Whitespace-only input is refused in the page *and* by the server (`min_length=1`
      accepts `"   "`); the page's check is a convenience, the server's is the rule.

## 2a. Which tasks a browser run may serve — shared policy

- [ ] **`assessment.start` gains a `scoring` condition**: `"any"` (today's behaviour,
      the default for the CLI and skills) or `"machine"`. It is stored in
      `conditions_json`, included in the start request hash, and reported on the run.
      The policy lives in `placement.py` beside `MACHINE_SCORABLE_TASK_TYPES`, not in
      JavaScript and not in the route.
- [ ] Under `"machine"`, a dimension's servable candidates are those whose task type is
      machine-scorable **and** whose modality is available **and**, for `audio`, whose
      presentation carries a `ServedAsset`. A dimension left with none closes `not-tested`
      at start with a reason naming which filter emptied it — `"no machine-scorable task"`,
      `"its listening tasks ship no recording"` — never as a failure and never silently.
- [ ] `next_task` honours the run's stored condition, not the caller's: a run opened
      `"machine"` never serves a task needing a judge, whatever the bank holds later.
- [ ] The page starts runs with `scoring: "machine"` and `modalities: ["text", "audio"]`.
      It never asks for `writing` or `speech`; those dimensions are `not-tested` here, with
      the reason the server gives.
- [ ] **Resuming a run the page did not shape.** A run opened by the CLI with
      `scoring: "any"` can have a judge-scored task outstanding. `/screen` already reports
      the answer mode; the page shows such a task as *needs a judge*, does not submit a score
      for it, and names the way forward (continue in the assess skill, or pause). It never
      offers a button that would produce a refusal.

## 3. Listening

Needs a migration and two routes; C3 has neither.

- [ ] **Playback route** `GET /runs/{run_id}/tasks/{content_id}/audio`, token-authenticated
      like every other read. It serves only the asset snapshotted on *this run's* served
      task — resolved through `assessment_run_tasks`, never by re-reading the pack — and only
      while that task is `served`. It reads the file through `artifacts.classify_path` (or
      the pack-asset equivalent C5 introduces), hashes it, and refuses rather than streams
      when the bytes differ from the snapshot's `sha256` (`assessment_asset_altered`) or are
      absent (`assessment_asset_missing`): a learner must hear the recording the task was
      served with, or none.
- [ ] Because `<audio src>` cannot send a header, the page fetches the bytes with the token
      and plays them from an object URL. It fetches once per task; replaying from the blob
      costs nothing, which is why fetching cannot be what is counted.
- [ ] **A play is the learner starting playback**, counted when they press play — not on
      fetch, not on seek, not on resume after pause. The first hearing is a play.
      `replay_allowance` is a count of plays (C2a: "zero replays … is a task that cannot be
      heard"), so a finite allowance of 2 means two hearings.
- [ ] **Plays are recorded server-side before they happen.** `POST
      /runs/{run_id}/tasks/{content_id}/plays`, keyed like every other mutation, appends one
      row to a new `assessment_task_plays` table (`play_id`, `run_id`, `content_id`,
      `idempotency_key` unique, `played_at`). It refuses a play past a finite allowance
      (`assessment_replays_exhausted`), and the page starts playback only after it succeeds.
      Append-only rows rather than a counter on `assessment_run_tasks`: each play is a
      separately keyed event, and a counter cannot tell a retried increment from a second
      play, while a unique key per row can. When and in what order the learner listened
      stays recorded too.
- [ ] **`record` derives the count; it does not accept one.** It counts the task's play rows
      and stores `play_count` on the new `assessment_results` column. A caller-supplied count
      would be one more place a caller could talk its way into a different claim.
      `db check` asserts every audio result's `play_count` equals its play rows.
- [ ] **Reload and resume.** `/screen` reports `plays_used` and `plays_remaining` for an
      outstanding audio task, so a reloaded page, a resumed run, and a different sitting all
      show the same number. Nothing about plays lives only in the page.
- [ ] Replay is under the learner's control — no countdown, no auto-advance. A finite
      allowance shows what remains.
- [ ] **The pilot pack has no recordings.** Its audio tasks are not servable under
      `"machine"` (§2a), so a pilot calibration in the browser reports `listening` as
      `not-tested` with the reason, and `reading` carries the receptive estimate. All
      verification of playback uses a synthetic fixture pack with generated audio (a few
      hundred milliseconds of tone, stamped), never a real recording.

## 4. Progress and honesty

- [ ] Per-dimension progress against its task budget (for example 6–12), not a percentage
      invented for the bar.
- [ ] Show the **confidence label the CLI reports**. Never round a range into a level the CLI
      did not claim — the assess skill's own instruction is *"never to compute an estimate
      yourself or to round a range into a level"*, and the UI is bound by it too.
- [ ] A dimension with no estimate shows `not-tested` and the server's reason, which is an
      answer rather than a zero.

## 5. Idempotency, retries, and double submission

C3 accepts caller-generated keys and issues none, so the page owns them.

- [ ] **One key per operation**: `crypto.randomUUID()`, minted when the learner acts — one
      for a start, one per serve, one per record, one per play, one per finalize.
      `/status` takes no key (C3: idempotent by state).
- [ ] **A retry resends the same key with the byte-identical payload.** A changed payload
      under a reused key is a conflict by design; the page never edits a pending operation.
- [ ] **The pending operation survives a reload.** Before sending, the page writes
      `{operation, run_id, key, payload}` to `sessionStorage` (wrapped in try/catch; absent
      storage degrades to in-memory), clears it on a definitive answer, and on load resends
      any pending operation unchanged *before* drawing. Without this a reload during a lost
      `POST /runs` mints a new key and opens a second run.
- [ ] **One operation in flight per run.** Answer buttons disable on first press; a second
      click while pending does nothing. Two clicks are one answer.
- [ ] **A replayed serve may name a task that is already answered.** `NextTaskReport` carries
      the snapshot's status (C3 final review); when it is not `served`, the page discards
      the response, refreshes `/screen`, and serves again under a *new* key. It never draws
      a task the learner has finished.
- [ ] A serve that answers with an `AssessmentRunReport` means the last open dimension
      closed; the page goes to the results view rather than reading a missing `content_id`
      as an error.
- [ ] **Busy and retrying are real states in the interface.** `database_busy` with
      `retryable: true` shows *waiting for another LinguaWiki command* and retries with the
      server's `Retry-After`, same key. A 503 without `retryable` (C3 round three: a keyless
      mutation whose outcome is unknown) shows that the outcome is unknown and refreshes
      `/screen` instead of retrying.
- [ ] A refused action shows the error's `message`, which is written for a person.

## 6. Interruption and resume across sittings

- [ ] **Run discovery.** `GET /runs?track=<id>&status=in-progress,paused` (read-only, track
      resolved like every other route, default track when omitted) lists resumable runs
      newest first with run type, scoring condition, status, and per-dimension progress.
      Without it a fresh page holding only a token cannot find the run it should resume.
- [ ] **Launch-time reference.** `client serve --run <run_id>` appends `&run=<run_id>` to the
      fragment. When present the page opens that run (refusing by name if it is not
      resumable); when absent it uses discovery, offers the newest resumable run, and only
      then offers to start one.
- [ ] Resuming restores outstanding work through `GET /runs/{run_id}/screen` alone — the
      outstanding task, its presentation snapshot, and its plays — never from page state.
- [ ] Pause and resume use the run's existing `/status` lifecycle.
- [ ] **A stale token is a named state.** After `client serve` restarts, the old page's
      calls are refused with the token code; the page says the server was restarted and to
      reopen it from the launch command, and stops retrying.

## 7. Browser code layout

- [ ] `src/linguawiki/client/static/` holds the shell; `pyproject.toml` includes it as
      package data and the distribution check asserts it is in the wheel.
- [ ] The page consumes the generated OpenAPI contract's shapes; it adds no field the
      contract does not carry.

## 8. Tests

`tests/integration/test_client_assessment_screen.py` (HTTP and service level):

- [ ] A `"machine"` calibration of the synthetic fixture pack completes through the HTTP
      surface. **Every captured request to `/results` carries `response` and no `score`**,
      and every stored result has `score_source = "computed"` and
      `scoring_policy_version = placement.SCORING_POLICY_VERSION`.
- [ ] **No model call**, asserted separately from scoring provenance: the run executes with
      no model credentials in the environment and a socket guard that fails the test on any
      non-loopback connection.
- [ ] A `"machine"` run never serves a judge-scored task, a `writing` dimension is
      `not-tested` with its reason, and the pilot pack's `listening` is `not-tested` because
      it ships no recording.
- [ ] Plays: the count reaching `assessment_results.play_count` equals the play rows; a play
      past a finite allowance is refused; `/screen` reports the same `plays_remaining` after a
      simulated reload; `record` refuses a caller-supplied play count.
- [ ] Playback refuses an altered or missing asset, an asset for a task not served in this
      run, and a task already scored.
- [ ] Idempotency: a lost-response retry of start, serve, record, play, and finalize with the
      same key and payload returns the first result and creates nothing; a replayed serve
      whose task is answered is reported with that status.
- [ ] Shell: the allowlisted paths answer without a token, everything else (including each
      traversal form in §1) is refused by the token check, and `Host` is enforced on the shell.
- [ ] A concurrent CLI command holding the writer produces a retryable 503, not a hang.
- [ ] Estimates are identical to those `linguawiki assessment report` produces from the same
      answers.
- [ ] Discovery lists only resumable runs for the resolved track.

`tests/browser/test_client_assessment_browser.py` (browser-driven, Playwright/Chromium,
added as a dev dependency):

- [ ] Opens `launch_url` exactly as printed, and asserts the fragment is gone from the
      address bar after load.
- [ ] Answers multiple-choice by clicking buttons and short-response by typing, and asserts
      the submitted request bodies carry the choice `value`, not an index or `display`.
- [ ] Plays a synthetic recording, replays it until a finite allowance is exhausted, sees the
      play button disable with the remaining count shown, and finds the count on the stored
      result.
- [ ] Double-clicks an answer and finds one result.
- [ ] Holds the writer from a second process and sees the *waiting* state, then completion
      when it is released.
- [ ] **Cross-sitting resume:** pauses mid-run, stops the server, restarts it (new token),
      sees the old page report the restart, opens the new `launch_url`, is offered the
      paused run by discovery, and finishes it with outstanding work and plays intact.
- [ ] The browser test runs in the release gate. If the browser cannot be launched the gate
      **fails**; a skipped acceptance test is a gate that passes by not looking.

## Gate

A migration, two new routes, a published contract change, and package data in the wheel:
the release gate, not `--fast`.

- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

A learner opens the page from `client serve`, completes the machine-scorable portion of a
calibration by button — pausing, restarting the server, and resuming in between — and the
estimates match what the CLI reports from the same answers, with every score computed by the
server and no model called.
