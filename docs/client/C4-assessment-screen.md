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

**Scope.** Text and machine-scorable modalities only. Audio capture is C5.

## 1. Shell

- [ ] Static files served by the C3 server from the workspace. No build step the learner has
      to run to study.
- [ ] Launch command opens the page with the ephemeral token.
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
- [ ] Submitting sends the idempotency key from C3; a retry after a lost response must not
      consume another task.

## 3. Listening

- [ ] Replay is under the learner's control — no countdown, no auto-advance.
- [ ] **Replay count is recorded** and reaches the stored result. Replays are evidence, not a
      convenience: a learner who needed six is telling you something.
- [ ] Respect the snapshotted `replay_allowance`; when it is finite, show what remains.

## 4. Progress and honesty

- [ ] Per-dimension progress against its task budget (for example 6–12), not a percentage
      invented for the bar.
- [ ] Show the **confidence label the CLI reports**. Never round a range into a level the CLI
      did not claim — the assess skill's own instruction is *"never to compute an estimate
      yourself or to round a range into a level"*, and the UI is bound by it too.
- [ ] A dimension with no estimate shows `not-tested`, which is an answer rather than a zero.

## 5. Interruption

- [ ] Pause and resume across sittings, using the run's existing lifecycle.
- [ ] **Busy and retrying are real states in the interface.** Another process holding the
      database is normal, not exceptional; the page says so rather than hanging.
- [ ] A refused action shows the error's `message`, which is written for a person.

## 6. Tests — `tests/integration/test_client_assessment_screen.py`

- [ ] A calibration of the machine-scorable dimensions completes through the HTTP surface with
      zero model calls, proven by recorded assessor kinds.
- [ ] Replay counts reach the stored result.
- [ ] A concurrent CLI command produces a retry state, not a hang or a crash.
- [ ] Estimates are identical to those the CLI would produce from the same answers.

## Gate

- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

A learner completes the text portion of a calibration in the browser, and the estimates match
what the CLI would have produced from the same answers.
