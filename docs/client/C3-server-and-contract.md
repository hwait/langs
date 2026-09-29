---
title: "C3 — The local server and its generated contract"
stage: C3
status: blocked
depends_on: [C1, C2a, C2b]
---

# C3 — The local server and its generated contract

Parent: [Learner Client Delivery Plan](../learner-client-plan.md) · Decisions: [ADR 0008](../adr/0008-learner-client-transport.md)

**Goal.** A loopback HTTP server exposing the read model and the mutations, described by a
generated OpenAPI 3.1 document. No UI; verified by contract tests and curl.

**The two facts that shape everything here.** A separate-process `open_reader` beside a held
writer returns `database_busy`; the application lock raises `writer_locked`. Both are
retryable and **reads are not free**. And from this stage a browser and the CLI are live at
once, which is why the outstanding-task guard ships here rather than in C6.

## 1. The outstanding-task guard — do this first

- [ ] In the **shared service layer**, not the server: a dimension holding an unanswered task
      is not eligible to be served again. Today `_excluded_task_ids` excludes served
      *content*; nothing excludes the *dimension*.
- [ ] Without this, two serve calls with different idempotency keys select two tasks in one
      dimension before either answer moves the posterior.
- [ ] Test with **two independent callers** — a service call and a subprocess CLI invocation —
      not one caller twice.

## 2. Idempotency

- [ ] Operation-scoped keys bound to a **canonical request hash**, for serving as well as
      recording. `next_task` takes no key today, so a retried serve consumes another task and
      burns its exposure.
- [ ] Same payload, same key → replay the original result.
- [ ] Different payload, same key → refuse as a conflict. Today `record` silently no-ops an
      already-answered task even when the score differs, so a client cannot distinguish a
      successful retry from a rejected correction.

## 3. Packaging

- [ ] Optional extra `linguawiki[client]` in `pyproject.toml`. The six pinned runtime
      dependencies stay untouched.
- [ ] Standard-library `http.server` only. No web framework.
- [ ] `scripts/check_distribution.py` must still pass; add the new package data if needed.

## 4. The server

- [ ] Import the service layer directly. **No business logic** — no scoring, no selection, no
      stop rule, no refusals of its own.
- [ ] Bind loopback only, on a port recorded in the workspace. Never `0.0.0.0`.
- [ ] One reader per read, one writer per mutation, **neither held across requests**.
- [ ] Bounded retry on `writer_locked` and `database_busy`; surface honestly when exhausted.
- [ ] A `command` name into the audit row for every mutation, so the trail is
      indistinguishable from a CLI-driven one.

## 5. Browser-origin protection

Loopback is not a security boundary; DNS rebinding is a documented attack on local servers.

- [ ] Validate `Host` against an allowlist; reject anything else.
- [ ] Validate `Origin` on every mutating request; reject cross-site.
- [ ] Mint an **ephemeral launch token** per server start, passed by the page the launch
      command opens. No persistent credential store.

## 6. Read model

- [ ] One call per screen, assembled from `open_reader`: run state per dimension, served-but-
      unscored tasks, and what each needs from the learner.
- [ ] Serve the **snapshotted** presentation from C2b, never the live pack row.
- [ ] Versioned envelope, like every other contract.

## 7. OpenAPI

- [ ] `scripts/generate_openapi.py`, document committed, `--check` added to `scripts/verify.py`
      beside `generate_schemas.py --check`.
- [ ] **Reference relocation.** 17 committed snapshots contain `#/$defs/...` pointers, and `#`
      is the *document* root — embedding them unchanged under `components/schemas` makes them
      resolve against the OpenAPI document and fail. Either rewrite to
      `#/components/schemas/...` on assembly, or embed each as a schema resource with its own
      `$id`. Pick one; record which in the ADR if it differs from what is written there.
- [ ] The CLI envelope's error payload is the **same** component schema the API returns.
- [ ] Declare per operation: the retryable error responses, the required headers, and the
      idempotency key parameter with its conflict response.

## 8. Tests — `tests/integration/test_client_server.py`

- [ ] Server and CLI concurrent: **reads and writes** may fail with `database_busy` or
      `writer_locked`; each is retried and then surfaced. (There is no "reads never fail".)
- [ ] Two independent callers cannot obtain two unanswered tasks in one dimension.
- [ ] Forged `Host`, foreign `Origin`, absent token, stale token — each rejected.
- [ ] A retried serve does not consume a second task.
- [ ] A changed-payload record is refused as a conflict.
- [ ] The server refuses exactly what the CLI refuses, asserted over the same cases.
- [ ] The committed OpenAPI document is current; every response validates against it; a
      **nested** `$ref` resolves through the assembled document.
- [ ] The server binds loopback only.

## Gate

- [ ] `./.tools/uv run python scripts/generate_openapi.py --check`
- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

A calibration can be driven end to end over HTTP with no browser, and the contract document is
generated rather than maintained.
