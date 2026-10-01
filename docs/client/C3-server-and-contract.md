---
title: "C3 — The local server and its generated contract"
stage: C3
status: ready
depends_on: [C1, C2a, C2b]
---

# C3 — The local server and its generated contract

Parent: [Learner Client Delivery Plan](../learner-client-plan.md) · Decisions: [ADR 0008](../adr/0008-learner-client-transport.md)

**Goal.** A loopback HTTP server exposing the read model and the calibration mutations,
described by a generated OpenAPI 3.1 document. No UI; verified by contract tests and curl.

**Why the server is the smallest part of this stage.** Four of the eleven sections below
touch no HTTP at all. From this stage a browser and the CLI are live at once, and
everything that makes concurrent use safe — the outstanding-task guard, idempotent
serving, an audit row for the two mutations that write none — belongs in the shared
service layer, because a guard that lives in the server is a guard the CLI does not have.
The server is routing, retry, and origin checks over work that is already correct.

## The four facts that shape it, three of them measured here

1. **A separate-process reader beside a held writer is refused, and so is a second
   writer.** `_connect` maps DuckDB's connection failures to `database_busy`
   (`db/connection.py:161`) and `locks._locked_error` raises `writer_locked`
   (`db/locks.py:89`). Both are `retryable=True`. Reads are not free.
2. **In one process, a second read-only connect beside a held writer also fails** —
   measured against the pinned `duckdb==1.5.5`: `ConnectionException: Can't open a
   connection to same database file with a different configuration than existing
   connections`. `_connect` turns that into `database_busy`, which is honest but which
   **no amount of retrying can clear**, because the holder is this process.
3. **DuckDB does *not* stop a second writer in one process.** A second
   `duckdb.connect(path, read_only=False)` in the same process succeeds and shares the
   instance. What stops it is the application lock: `locks.acquire` opens a fresh
   descriptor each time (`db/locks.py:120`) and `flock` conflicts between two open file
   descriptions *in the same process* — measured: `EWOULDBLOCK`. So two in-process
   writers get `writer_locked`, correctly, but only because of the flock. Nothing tests
   that today, because today there is one command per process.
4. Together, 2 and 3 are the reason the server is **single-threaded** (§7). A threaded
   server would spend its retry budget on contention it created itself.

## 0. Confirm the starting point

Five of these were true when this stage was written and are the premises the rest rests
on. If any has changed, stop and re-read the stage rather than working around it.

- [ ] The migration head is `0031_task_presentation.sql`, so the new file is `0032`.
- [ ] `assessment_run_tasks` has no `permitted_help` column, and no idempotency or
      request-hash column. `assessment_tasks.permitted_help` exists
      (`0012_assessment_bank.sql:43`).
- [ ] `services/assessment.py` `next_task` takes no `idempotency_key`, and neither
      `next_task` nor `record` calls `record_audit_entry` — `start` (`:707`),
      `set_status` (`:1369`) and `finalize` (`:1437`) do.
- [ ] `record` already refuses a changed answer on an answered task, through
      `_assert_repeat` (`:1050`). **The sketch this stage replaces said it silently
      no-ops; that was fixed.** What is not fixed is §4.
- [ ] `pyproject.toml` has no `[project.optional-dependencies]`, and the six runtime
      dependencies are `duckdb`, `jinja2`, `jsonschema`, `packaging`, `pydantic`,
      `rfc3339-validator`.
- [ ] `ls schemas/*.json | wc -l` is 21 and `grep -l '#/\$defs/' schemas/*.json | wc -l`
      is 18. **Do not copy either number into code or into a test.** The sketch said 17
      and was out of date within one stage; §9 counts.

## 1. Migration `0032_served_help_allowance.sql`

One column. `permitted_help` is the last thing a client needs that the served snapshot
does not keep, and §2's re-serve is what exposes it: reading it from `assessment_tasks`
on the second serve would let a pack edit between the two change what help the learner
is offered for a task they are already credited with facing.

- [ ] `ALTER TABLE assessment_run_tasks ADD COLUMN permitted_help VARCHAR`, nullable.
- [ ] `NULL` means *served before this migration*, and is truthful: no snapshot held it.
      It is **not** a fourth member of 0030's or 0031's groups, and must not be folded
      into either check — a single column has no partial state, so "complete" is not a
      question it can be asked. There is no backfill: the bank's current value is not
      what an older serve used, and writing it would be a claim about a sitting nobody
      recorded.
- [ ] Unconstrained, like every added column before it: DuckDB refuses `ALTER TABLE ADD
      COLUMN` with a constraint, so there is no `length(permitted_help) > 0` here. The
      serve path writes it, every reader treats absence as absence, and
      `served_help_allowance_wellformed` (§10) asserts it over the data.
- [ ] Write the intent in a header comment: why the allowance is part of what the learner
      faced, and why it is its own column rather than a fourth member of an existing group.
- [ ] Never edit a released migration. Add the checksum to
      `tests/migrations/snapshots/released-migrations.json`; never remove a recorded one.
- [ ] Run `tests/migrations/test_migration_registry.py`. If
      `scripts/duckdb_upgrade_support.py` seeds `assessment_run_tasks` explicitly, add the
      column there too.
- [ ] In `next_task`'s `INSERT INTO assessment_run_tasks`, add `permitted_help` to the
      column list and write `str(task[4])` — the value already read from the bank for the
      report, so the stored value and the reported value cannot disagree.
- [ ] Extend `ServedTaskReport` (`services/assessment.py:1502`) with `permitted_help: str
      | None = None` and `rubric: dict[str, object]`, and extend `served_task_report` to
      select `permitted_help` and `rubric_json`. `rubric_json` has been snapshotted since
      0030 and the report was carrying only `rubric_version`, which names a body it does
      not hand over.
- [ ] `stable_key` is read live, from `content_records`, and that is correct: a content ID
      is derived from `(pack_key, kind, stable_key)`, so the key cannot change without
      changing the ID. Say so in a comment, or the next reader will snapshot it too.

## 2. The outstanding-task guard, and what a re-serve must not do

In the **shared service layer**. `_excluded_task_ids` (`:325`) excludes served *content*;
nothing excludes the *dimension*, so two serves with different idempotency keys select two
tasks in one dimension before either answer moves the posterior.

- [ ] `_outstanding_dimensions(database, run_id) -> Mapping[str, str]` — dimension to
      `content_id`, from `assessment_run_tasks WHERE run_id = ? AND status = 'served'`.
      `'skipped'` and `'answered'` are settled; only `'served'` is outstanding.
- [ ] In `next_task`, after `open_states` is sorted, prefer the dimensions that are
      **not** in that mapping. Keep the existing least-progressed-first order among them,
      so the spread rule the sort exists for is unchanged.
- [ ] If every open dimension is outstanding, **re-serve** the least-progressed one's
      outstanding task through `served_task_report`, and return a `NextTaskReport` carrying
      `served_again=True` (a new field, defaulting to `False`). The learner's screen still
      has work on it; refusing would make the only way to find that work a different call.
- [ ] A re-serve writes **nothing**. No new `sequence`, no second `_record_exposure`, no
      `_write_state`. The exposure row was written when the task was first served, inside
      `next_task`'s transaction, and counting it twice pushes an item out of the
      six-month reuse window on the strength of a task the learner never answered once.
- [ ] A re-serve therefore needs no writer at all — but `next_task` is a writer, and
      splitting it would give the CLI and the server two different entry points to the
      same question. Keep one `next_task`, under `open_writer`, and let the re-serve path
      simply open no transaction. A command that holds the writer and writes nothing is
      not a bug; a second command that answers the same question is.
- [ ] `select_task` is not consulted on a re-serve, and must not be: it would probe the
      boundary of a posterior no answer has moved and propose a *different* task.
- [ ] A re-serve's `selection_reason` is `"outstanding"`, not the `"informativeness"`
      default. Selection did not run, and letting the default stand would credit a report
      to a computation nobody performed.
- [ ] If the outstanding task's snapshot is damaged, `served_task_report` refuses by name
      and the refusal **propagates**. Falling through to another dimension would hide the
      damage behind a task that happens to work, and leave the learner holding a task
      nothing can score.
- [ ] Nothing here changes `record`. A task is answerable whether it was served once or
      handed back five times, and `_assert_repeat` still owns what a second answer means.

## 3. An audit row for the two mutations that write none

ADR 0008 requires "a `command` name into the audit row for every mutation, so the trail is
indistinguishable from a CLI-driven one". `next_task` and `record` — the two the client
loop runs constantly — write no audit row at all. Adding them in the server would give the
server a better trail than the CLI, which is two implementations of honesty.

- [ ] `record_audit_entry` in `next_task`'s transaction and in `record`'s, beside the
      domain events they already record. Not on a re-serve: nothing was mutated, and an
      audit row for a read is a record of something that did not happen.
- [ ] Thread `actor: str = "cli"` through `next_task`, `record`, `set_status`, `finalize`
      and `start`. `audit_log.actor` already defaults to `"cli"` in `record_audit_entry`
      and is constrained only to be non-empty (`0006_domain_events_and_audit.sql:20`).
- [ ] The server passes `actor="client"` and leaves `command` as the CLI's own name
      (`assessment.next`, `assessment.record`). That is how both halves of the ADR hold at
      once: the trail reads identically, and `actor` is the one honest difference. A
      different `command` per surface would make every audit query ask twice.
- [ ] `affected_records_json` names the run and the task, as the existing three do.

## 4. Idempotency bound to a canonical request hash

`next_task` takes no key, so a retried serve consumes another task and burns its exposure.
And `record_domain_event` writes `idempotency_key` against a **globally unique index**
(`0006_domain_events_and_audit.sql:16`) with no preflight, so a reused key with different
content dies on a raw `duckdb.ConstraintException` — surfaced as `internal_error`, after
the refusals `record` carefully places before its transaction. Both are one fix.

- [ ] New module `linguawiki/idempotency.py`. Move `canonical_hash`
      (`services/sessions.py:327`) into it and have `sessions` import from there; it
      hashes a payload by its canonical JSON form and is not session-specific. Keep the
      name and the docstring.
- [ ] `request_hash(**parts: object) -> str` — `canonical_hash` over the named parts of a
      request. For `assessment.next`: run, track, and nothing else. For
      `assessment.record`: run, content_id, score, the response **hash** rather than the
      response, visibility, assessor kind, assessor, confidence, and the rubric. Never the
      learner's text: the hash is stored in `domain_events.payload_json`, and a payload
      stored before the retention rule ran keeps what it kept for the life of the
      workspace.
- [ ] `resolve_idempotent(database, *, key, event_type, request_hash) -> Mapping[str,
      object] | None` — the stored payload for an exact retry, `None` when the key is new,
      and `idempotency_conflict` otherwise. Three cases, in this order:
      - the key exists under this `event_type` with this hash → return its payload;
      - the key exists under this `event_type` with a different hash → refuse, naming the
        aggregate it already acted on;
      - the key exists under a **different** `event_type` → refuse, naming that operation.
        A key identifies one operation in both directions, and a serve key that answers a
        record call confirms a belief that is wrong.
- [ ] Call it **before** the transaction, in both commands, and compare the key before
      returning anything stored. A guard placed after the path it guards is not a guard.
- [ ] `next_task` gains `idempotency_key: str | None = None` and an
      `--idempotency-key` flag on `assessment next`. On an exact retry it returns the
      served task recorded in the stored payload, through `served_task_report`, with
      `served_again=True`. The payload needs the `content_id` and nothing more.
- [ ] `next_task` records `assessment.served` with the key whenever one was passed —
      including the call that *closes* a dimension and returns a run report, because a
      retry of that call must not then serve a task.
- [ ] `record`'s existing `record_domain_event` call stays where it is; what changes is
      that the key was checked first, so the unique index is never the thing that refuses.
- [ ] The preflight is a read on the writer's own connection, after the lock is held, so
      it sees committed state and no second writer can race it (fact 3).

## 5. The read model

- [ ] New module `services/assessment_view.py` — one purpose, and `services/assessment.py`
      is already 1880 lines. It imports from `assessment`, never the reverse.
- [ ] `run_screen_report(database, run_id) -> RunScreen` and a thin `run_screen(paths, *,
      run=None, track=None, clock=None)` wrapper that opens a reader. Both forms, because
      a writer that calls its own report deadlocks on its own lock, and §2's re-serve
      wants the `(database, id)` form.
- [ ] `RunScreen` carries: the run's identity and status, the per-dimension state
      `AssessmentRunReport` already models, the outstanding task per dimension as a
      `ServedTaskReport`, and what each needs from the learner — the presentation kind,
      and whether an answer is a choice or text.
- [ ] Outstanding tasks come from the **snapshot**, via `served_task_report`, never the
      live bank row. That is the whole point of C2b, and `served_task_report` already
      refuses a partial or damaged snapshot by name.
- [ ] Show an outstanding task whatever its dimension's status, including closed. §2's
      guard runs only while a dimension is open, so a task served just before its dimension
      stopped is never handed back by `next_task` — and `record` still accepts it. A screen
      that hid it would leave answerable work with no surface that mentions it.
- [ ] Never the learner's own words. No `response_excerpt`, no response text, no
      transcript — not under a flag, because this screen has no consent parameter to check
      one against.
- [ ] No bounding and no `omissions`, and say why in the docstring: after §2 there is at
      most one outstanding task per dimension and a run has a handful of dimensions, so
      there is no list that can outgrow a budget. If a later stage adds one, it gets
      `omissions` then.
- [ ] A `linguawiki assessment screen --run ...` command, printing the same report. Not a
      convenience: §11's equivalence tests need the CLI to be able to ask every question
      the server answers, or "the server refuses exactly what the CLI refuses" is
      untestable for the read model.

## 6. Packaging, and the extra that is not declared

- [ ] **No `linguawiki[client]` extra.** The server is stdlib `http.server` only, so the
      extra would carry zero dependencies: `pip install linguawiki` already ships the
      client, and `linguawiki[client]` would read as a boundary nothing enforces — the
      "documentation that looks like validation" failure, in the packaging metadata.
- [ ] ADR 0008 **is already amended** for this, and for the single-threaded decision in §7
      and the relocation strategy in §9. Read it before starting; the alternatives it
      rejected (a web framework, a separate repository) are unaffected.
- [ ] The six pinned runtime dependencies stay untouched. Nothing in §7 needs a seventh.
- [ ] `src/linguawiki/client/` is a package under the existing wheel `packages` entry, so
      no new `force-include` is needed. Add one `client/` module path to
      `WHEEL_RESOURCES` in `scripts/check_distribution.py` anyway — a wheel that ships the
      CLI and not the server would otherwise pass the gate.

## 7. The server

- [ ] `src/linguawiki/client/` with one responsibility each:
      `runtime.py` (the runtime file and the launch token), `security.py` (§8),
      `retry.py`, `responses.py` (status mapping and envelope writing), `routes.py` (the
      route table), `server.py` (the wiring and the handler). Not `http.py`: a module of
      that name beside code that imports stdlib `http` is a trap for a future reader.
- [ ] Import the service layer directly. **No business logic** — no scoring, no selection,
      no stop rule, no refusal of its own beyond §8's. A handler resolves arguments,
      calls one service function, and writes the envelope.
- [ ] **Single-threaded `http.server.HTTPServer`**, not `ThreadingHTTPServer`. Facts 2 and
      3: two overlapping requests in one process produce `database_busy` or
      `writer_locked` that no retry can clear, because this process is the holder.
      Serializing them is not a limitation for one learner — it is what makes the retry
      budget mean something. Put the reason in the module docstring, because
      `ThreadingHTTPServer` is what a reviewer will reach for.
- [ ] Bind `127.0.0.1` explicitly. Never `0.0.0.0`, and never a hostname that could
      resolve outward.
- [ ] One reader per read, one writer per mutation, **neither held across requests**: the
      `with open_reader(...)` / `with open_writer(...)` block closes inside the handler,
      before the response is written.
- [ ] `retry.py`: `with_retry(operation, *, sleep, attempts=4, base_delay=0.05)` retries
      only a `LinguaWikiError` whose `retryable` is true, with exponential backoff, and
      re-raises the **last** error when attempts run out. `sleep` is injected so a test
      exhausts the budget without waiting. A caller that retries on the error's *code*
      would need the list of retryable codes in two places.
- [ ] `responses.py` maps a refusal to a status by **class, not by code**, because a
      per-code table rots: `retryable` → 503 with `Retry-After`; `invalid_arguments`,
      `invalid_contract`, `invalid_path` → 400; a code ending `_conflict`, plus
      `idempotency_conflict` → 409; a code ending `_not_found` → 404; `internal_error`
      → 500; everything else → 422. The body is always the `linguawiki.cli.error.v1`
      envelope that `cli._failure` builds — the same `ErrorPayload`, not a parallel one.
- [ ] A request body cap — 256 KiB, read against `Content-Length` and enforced while
      reading rather than trusted from the header. A loopback page is still a page, and an
      unbounded `rfile.read` on a single-threaded server is a one-request outage. Over the
      cap is 413 with its own code; a body that is not an object is 400 through the
      existing `invalid_contract` path.
- [ ] An absent `Host` header is a refusal, not a default. HTTP/1.0 permits its absence,
      and defaulting it to the bind address is how §8's allowlist gets bypassed.
- [ ] A `linguawiki client serve` command: binds, mints the token, writes the runtime
      file, prints the URL, and opens it unless `--no-open` is passed. It runs in the
      foreground; a daemon is a second lifecycle nobody asked for.
- [ ] `data/client-runtime.json` holds `port`, `pid`, `started_at`, and the schema
      version. `data/` is already in `paths.PRIVATE_DIRECTORIES` and the rendered
      gitignore, so no privacy surface changes. **The token is never written to it.**
- [ ] The file is removed on clean shutdown, and a stale one is overwritten on the next
      start after checking whether its `pid` is still alive. A port claimed by nothing is
      a worse answer than no answer.

## 8. Browser-origin protection

Loopback is not a security boundary; DNS rebinding is a documented attack on local
servers. Every rule here is refused before the service layer is reached.

- [ ] `Host` must match `127.0.0.1:<port>` or `localhost:<port>` exactly. Anything else is
      403, including a name that resolves to 127.0.0.1 — that is the attack.
- [ ] `Origin` is validated on **every** mutating request and must be the server's own
      origin. Absent is a refusal, not a pass: a cross-site form post sends no `Origin` in
      some browsers, and "absent is the case where the data is least trustworthy".
- [ ] An ephemeral launch token per server start, `secrets.token_urlsafe(32)`, compared
      with `secrets.compare_digest`, carried in `X-LinguaWiki-Token` on every request
      including reads. No persistent credential store.
- [ ] The launch command opens `http://127.0.0.1:<port>/#token=…` — the **fragment**, so
      the token never reaches an access log, a `Referer`, or a proxy. The page reads it
      and sends the header.
- [ ] Each refusal has its own code so a client can tell them apart: `client_host_denied`,
      `client_origin_denied`, `client_token_required`, `client_token_invalid`. Collapsing
      them into one would take away a page's ability to tell "reload with the token" from
      "you are not talking to the server you think you are".
- [ ] The token lives in the server process only. A restart invalidates every page holding
      the old one, which is correct, and the refusal says to relaunch.

## 9. The generated OpenAPI document

- [ ] `linguawiki/openapi.py` assembles the document; `scripts/generate_openapi.py` is
      thin and mirrors `generate_schemas.py` — `--check` compares, no flag writes.
- [ ] Committed at `schemas/openapi/linguawiki.client.v1.json`. A **subdirectory**,
      because `tests/contracts/test_json_schemas.py:214` globs `schemas/*.json` and holds
      every date-time node to the UTC pattern — a rule written for JSON Schema snapshots,
      which would judge an OpenAPI document by it. The subdirectory also rides the
      existing `schemas` force-include into the wheel.
- [ ] **Hoist and rewrite.** Each reused snapshot becomes a component; its `$defs` lift
      into `components/schemas` under a namespaced name (`lingua.content.v1.ContentReview`)
      and every `#/$defs/X` is rewritten to `#/components/schemas/...`. `#` is the
      *document* root, so an unrewritten pointer resolves against the OpenAPI document and
      fails.
- [ ] Refuse a name collision rather than resolving it: two snapshots with a `$def` of the
      same name under one component name is a bug in the namespacing, and silently keeping
      the second would make one schema describe the other's type.
- [ ] **Derive the set of snapshots from `SCHEMA_MODELS`; never hard-code a count.** The
      sketch's "17" was wrong within one stage. A test asserts that no `$ref` anywhere in
      the assembled document begins `#/$defs/`, which is the property the number was
      standing in for.
- [ ] The error payload is the **same** component the CLI envelope uses: one schema, from
      `ErrorPayload`, referenced by every error response.
- [ ] Declare per operation: the retryable error responses, the required headers
      (`X-LinguaWiki-Token`, `Origin` on mutations), and the idempotency key parameter
      with its 409.
- [ ] `--check` into `scripts/verify.py` beside `generate_schemas.py --check` (`:77`).
- [ ] The document describes shapes, not sequences. Say so in its `info.description`: the
      guard in §2, idempotent serving, and scoring against the snapshot are not expressible
      in it and live in the service layer and its tests. Conformance is not correctness.

## 10. `db check`

One new named check. The column §1 adds is unconstrained, and nothing else in this stage
touches the schema.

- [ ] `served_help_allowance_wellformed` — every non-null
      `assessment_run_tasks.permitted_help` is non-blank. Read it in Python and report the
      rows that fail; a diagnostic must never raise, and a SQL predicate guarded by `IS
      NOT NULL` skips exactly the rows the write path refuses hardest.
- [ ] It names the rows it found, not a count.
- [ ] No check for the audit rows §3 adds. Every row written before this stage legitimately
      has none, so a check could only report history as damage — and a check that mirrors
      half a constraint passes the states the other half forbids. The obligation is a test
      (§11), not a check.
- [ ] Nothing about the server is checkable here: the runtime file is not learner state and
      the token is never stored.

## 11. Tests

`tests/integration/test_client_server.py` for the server, with the service-layer work
tested where it lives.

**The guard and the re-serve** — `tests/integration/test_assessment.py`:

- [ ] Two **independent callers** — a direct service call and a `subprocess` CLI
      invocation — cannot obtain two unanswered tasks in one dimension. Not one caller
      twice: that passes against an in-process cache.
- [ ] A dimension with an outstanding task is skipped while another open dimension is
      free, and the task served comes from the other dimension.
- [ ] When every open dimension is outstanding, `next_task` returns the least-progressed
      one's task with `served_again=True`, `assessment_run_tasks` gains no row, and
      `assessment_item_exposures` is unchanged — assert the exposure count and the
      `last_exposed_at`, because an incremented count is the quiet half of this bug.
- [ ] A re-served task's `permitted_help`, prompt, rubric and presentation are the
      snapshot's, after the pack has been edited to say otherwise.

**Idempotency** — same file:

- [ ] A retried serve with the same key and the same request returns the same
      `content_id` and consumes no second task.
- [ ] A serve key reused with a different run refuses `idempotency_conflict`.
- [ ] A record key reused with a different score refuses `idempotency_conflict`, **not**
      `internal_error` — the regression test for today's raw `ConstraintException`.
- [ ] A serve key reused on `assessment record` refuses, naming the operation it belongs to.
- [ ] A key that was never used returns `None` from the preflight and nothing is written
      by the lookup itself.

**Audit** — same file:

- [ ] A serve and a record each write exactly one `audit_log` row, with the CLI's command
      name and `actor = 'cli'`; the same two through the server carry `actor = 'client'`
      and the same command.
- [ ] A re-serve writes no audit row.

**The server** — `tests/integration/test_client_server.py`:

- [ ] A calibration driven end to end over HTTP: start, screen, next, record, finalize,
      with zero model calls, reaching the same estimates the CLI reaches from the same
      answers.
- [ ] Server and CLI concurrent: a read *and* a write each fail with `database_busy` or
      `writer_locked` while a CLI command holds the database, are retried, and are
      surfaced as 503 with `Retry-After` when the budget is exhausted. There is no "reads
      never fail".
- [ ] No connection is held across requests: assert a CLI command succeeds between two
      server requests.
- [ ] Forged `Host`, an absent `Host`, a `Host` naming a domain that resolves to
      127.0.0.1, foreign `Origin`, absent `Origin` on a mutation, absent token, an empty
      token header, and a token from a previous server start — each refused, each with its
      own code.
- [ ] A body over the cap is refused with 413 and the server answers the next request; a
      `Content-Length` that understates the body does not get past the cap either.
- [ ] A re-serve whose snapshot is damaged refuses by name rather than serving a task from
      another dimension, through the server and through the CLI alike.
- [ ] The server binds loopback only: assert the listening socket's address, and that a
      connection to a non-loopback local address is refused.
- [ ] The token does not appear in `data/client-runtime.json`, and the runtime file is
      gone after a clean shutdown.
- [ ] **Refusal equivalence**: a table of cases — a task not served, a finalized run, a
      malformed presentation, a vanished asset, a changed asset, a conflicting
      idempotency key — asserted to produce the same `code` through the CLI and through
      the server. The mapping in §7 turns codes into statuses; this is what keeps the code
      itself from drifting.

**The contract** — `tests/contracts/test_openapi.py`:

- [ ] The committed document is current (`generate_openapi.py --check` exits 0) and is a
      valid OpenAPI 3.1 document.
- [ ] No `$ref` anywhere in it begins `#/$defs/`.
- [ ] A **nested** reference resolves through the assembled document — pick a component
      whose `$def` itself references another `$def`, and resolve it with a resolver rooted
      at the document.
- [ ] Every response the integration tests produced validates against the operation's
      declared schema, success and error alike.
- [ ] The error component and the CLI envelope's error payload are the same schema, not
      two equal ones: assert they are the same `$ref` target.

## Gate

- [ ] `./.tools/uv run python scripts/generate_openapi.py --check`
- [ ] `./.tools/uv run python scripts/verify.py`

The release gate, not `--fast`: this stage touches a migration, a published contract, and
the wheel's contents.

## Done when

A calibration can be driven end to end over HTTP with no browser and no model, two
independent callers cannot get two unanswered tasks in one dimension, a retried serve
returns the task it first served rather than consuming another, and the contract document
is generated rather than maintained.

## Decisions already recorded in ADR 0008

Three, amended there when this stage was written rather than when it ships, so the ADR does
not contradict the plan for the length of the stage.

- The `linguawiki[client]` extra is **not** declared (§6). An extra with no dependencies is
  a gate that does not gate.
- The server is **single-threaded** (§7), on the two measurements at the top of this file.
- The reused snapshots are **hoisted and rewritten**, not embedded with their own `$id`
  (§9), and the document lands in `schemas/openapi/` rather than beside the snapshots.
