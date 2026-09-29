---
title: "C6 — Submission lifecycle and round-batching"
stage: C6
status: blocked
depends_on: [C4, C5]
---

# C6 — Submission lifecycle and round-batching

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** Rubric-scored work stops blocking, and round-trips drop roughly sevenfold — neither
at the cost of sequential adaptivity.

**Prerequisite already shipped.** The one-outstanding-task-per-dimension guard is in C3.

## 1. Waiting is a state, not a gap

- [ ] An explicit **waiting** state, distinct from open and closed, reported to the client.
- [ ] Sequential adaptivity makes *some* waiting unavoidable: when every remaining dimension is
      blocked on a judgement, there is nothing sound to serve. The requirement is that waiting
      is **never hidden**, not that it never happens.
- [ ] While any dimension is eligible, the learner keeps working.

## 2. Durable submissions

- [ ] A submission record that survives a restart, so a verdict arriving later is still
      attributable.
- [ ] **Retention is applied before the submission is persisted, not after.** Session staging
      already resolves this through `evidence_service.retain_response(response, requested=…,
      preferences=…) -> (visibility, excerpt, digest)`. Assessment recording does **not**
      inherit that boundary — it writes `response_excerpt` straight to the row. Queue records
      **and every retry record** store the retained form only.
- [ ] **When the retained form cannot be judged**, pick one and implement it explicitly: judge
      synchronously within the request and persist only the verdict, or refuse asynchronous
      judging for that track. Silently queueing text the track forbids storing is not an
      option.

## 3. Verdicts arriving after the run moved

`record` calls `_assert_running` and refuses with `assessment_run_paused` or
`assessment_run_closed`. A late verdict therefore **cannot simply be recorded**.

- [ ] **Paused** — the verdict stays queued and applies on resume. Not discarded; does not
      force the run open.
- [ ] **Abandoned** — cancelled, and the cancellation recorded rather than the verdict
      silently vanishing.
- [ ] **Finalized** — either finalization waits for outstanding judgements, or it proceeds and
      records explicitly which results it excluded. A verdict arriving afterwards is refused
      against the closed run and reported, never applied retroactively.
- [ ] A restart with an unapplied verdict resolves to one of the above, never to a queue entry
      nobody will process.

## 4. Round-batching

- [ ] Serve one task per **open** dimension in a single call.
- [ ] Never two within a dimension — `select_task` probes the boundary of the current
      `posterior_median`, so within-dimension order is strictly sequential.

## 5. Tests — `tests/integration/test_client_async_judging.py`

- [ ] Batched serving produces the same per-dimension sequence as one-at-a-time serving.
- [ ] A run cannot be finalized silently while a pending judgement would change it.
- [ ] Each lifecycle transition with a judgement outstanding: paused→resumed applies it;
      abandoned cancels and says so; finalized waited or named what it excluded; restart with
      an unapplied verdict leaves nothing unprocessable.
- [ ] A track forbidding retention never has response text in a queue or retry record.
- [ ] With all remaining dimensions blocked, the run reports waiting rather than serving
      something unsound.

## Gate

- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

While any dimension is eligible the learner keeps working; when none is, the waiting is
explicit. Estimates match a synchronous run of the same answers.
