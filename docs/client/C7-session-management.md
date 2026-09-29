---
title: "C7 — Session management in the browser"
stage: C7
status: blocked
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

## 1. Plan

- [ ] `plan create` with minutes, mode, energy and intent.
- [ ] Show each block's **rationale as the planner gives it**, including the weightings — "this
      area is behind for the week (+0.80)", "some targets have unmet prerequisites (−0.90)".
- [ ] Show the high-priority candidates that **missed out, and why**. That reasoning is the
      product; hiding it would leave a timetable with no explanation.

## 2. Run

- [ ] `session start`, then `session log` for batched observations.
- [ ] Staged work is **visible before the close** and unchanged by it until it happens.
- [ ] The screen states plainly that teaching happens elsewhere, so the learner is never
      waiting for an exercise the browser cannot produce.

## 3. Close

- [ ] `session close` is the **only** moment progress moves, and the interface must make that
      legible rather than blurring it into a stream of saves.
- [ ] Show what the close credited, once it has.
- [ ] `partial-close`, `abandon`, `resume` and `recover` are reachable, because interruption is
      normal.

## 4. Tests — `tests/integration/test_client_sessions.py`

- [ ] A session planned, run and closed in the browser credits **exactly once**.
- [ ] An interrupted session recovers through the existing `resume` / `partial-close` /
      `recover` paths.
- [ ] Staged work is visible before the close and unchanged until it happens.
- [ ] Nothing in the served interface implies the browser is teaching.

## Gate

- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

A session can be planned, staged and closed in the browser, the close boundary is as visible to
the learner as it is in the data, and nothing in the interface implies the browser is teaching.

## Successor, not in scope

A browser that *conducts* lessons needs a teaching-content source and a judging integration,
obeying the same provenance rules generated content already obeys. Worth a proposal of its own
once C1–C7 are in use.
