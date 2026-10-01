# ADR 0008: A loopback client that transports, and an OpenAPI contract that is generated

Status: accepted for the learner client (C1-C7), sequenced before Stage 8

## Decision

The learner client is a local, single-user browser application served over HTTP on loopback. Its server ships in the core repository, uses only `http.server` from the standard library, and imports the service layer directly rather than subprocessing the CLI.

**Amended during C3:** the optional dependency extra (`linguawiki[client]`) this decision originally named is **not** declared. Because the server is stdlib-only, the extra would carry no dependencies: `pip install linguawiki` already ships the client, so `linguawiki[client]` would read as a boundary nothing enforces. The stdlib-only decision stands; it is expressed by the absence of a seventh runtime dependency rather than by a no-op extra.

**Amended during C3:** the server is **single-threaded**. Measured against the pinned `duckdb==1.5.5`, a second read-only connection in the *same* process beside a held writer is refused with `ConnectionException`, which `_connect` reports as the retryable `database_busy` -- a retry that can never succeed, because the holder is this process. DuckDB does not refuse a second writer in one process at all; the application `flock` does, between two open file descriptions. A threaded server would therefore manufacture its own contention and spend the retry budget on it. Serializing requests is correct for one learner and is what makes a busy state mean something.

**Decided during C3:** of the two relocation strategies below, the generator **hoists and rewrites** -- each snapshot's `$defs` lift into `components/schemas` under a namespaced name and every `#/$defs/...` pointer is rewritten. The document is self-contained, and a test asserts no `#/$defs/` reference survives assembly.

The server is a **transport, not a second implementation**. Task selection, scoring, the posterior update, the stop rule, and every refusal stay in Python where ADR 0003 already puts them. The server adds routing, presentation assembly, and nothing else.

Because DuckDB holds an exclusive lock on the database file and the application lock is non-blocking, **the server holds no connection across requests**: one reader per read, one writer per mutation, both released before responding. `writer_locked` and `database_busy` are both retryable, are retried with bounded backoff, and are surfaced plainly when retries are exhausted. A learner running a CLI command briefly makes the server's reads fail, and that is the correct trade against a server that monopolises the database.

Loopback is **not** a security boundary. The server validates the `Host` header against an allowlist, validates `Origin` on every mutating request, and requires an ephemeral launch token minted per server start and passed by the page the launch command opens. No persistent credential store is introduced.

The HTTP contract is described by an **OpenAPI 3.1** document that is **generated from the Pydantic contracts and committed**, verified stale-or-current by `scripts/generate_openapi.py --check` in the quality gate. OpenAPI 3.1 is chosen because it adopts JSON Schema 2020-12 as its schema dialect, which is exactly what `linguawiki/schema_snapshots.py` already emits, so the existing contract snapshots are reusable without dialect conversion.

Reuse is not, however, verbatim embedding. Seventeen of the committed snapshots contain `#/$defs/...` pointers, and `#` resolves to the *document* root; dropping them unchanged under `components/schemas` makes those pointers resolve against the OpenAPI document and fail. The generator therefore either rewrites them to `#/components/schemas/...` or embeds each snapshot as a separately identified schema resource carrying its own `$id`. Nested references are tested through the assembled document.

The CLI envelope's error payload is the same component schema the HTTP API returns: one error contract for both entry points.

OpenAPI describes **shapes, not sequences**. Sequencing rules — at most one outstanding task per dimension, the `waiting` state, finalization with judgements pending, scoring against the serve-time snapshot rather than the live pack row — are not expressible in it and live in the service layer and its tests. Conformance to the document is not correctness.

## Alternatives rejected

- **A web framework (FastAPI/Starlette + uvicorn) in core dependencies:** triples the runtime dependency surface of a checksum-verified distribution that ships into every learner workspace, to serve one user on one machine.
- **Subprocessing the CLI:** reparsing our own JSON error envelopes to re-raise them as `LinguaWikiError` is avoidable work, and `--format json` is shaped for humans and skills rather than a UI's read model. The saving — no second entry point — is real but smaller than the cost.
- **A separate client repository:** the client is language-agnostic and generic, so it belongs where the generic package lives; ADR 0001's topology separates core from packs and learner workspaces, not core from its own optional surfaces.
- **Reimplementing scoring or task selection in JavaScript:** would put the posterior update and the stop rule in two places. ADR 0003 exists to prevent exactly this.
- **An unauthenticated loopback server:** DNS rebinding is a documented attack against local development servers without `Host` validation. "Loopback, single-user" is not a threat model.
- **OpenAPI 3.0:** its schema object diverges from JSON Schema, so every generated contract would need hand-maintained down-conversion.
- **A hand-written OpenAPI document:** a second home for a contract that already has one, and the one that rots.
- **A hosted or multi-learner client:** learner state is local by design and the workspace has no remote; a second learner is a second workspace.

## Consequences

The server is a second entry point and must stay as honest as the first. It carries a `command` name into the audit row for every mutation, accepts operation-scoped idempotency keys bound to a canonical request hash, and refuses what the CLI refuses. That equivalence is a standing test obligation, not a convention.

The client cannot assume the database is available. Busy and retrying are states the interface must show rather than hide, because the alternative is a UI that appears to hang while another LinguaWiki process holds the file.

Button-only presentation requires pack work before any UI work: the assessment bank encodes options inside prompt strings and puts spoken text in `prompt` rather than referencing a clip. A client may not parse prose to recover structure; until a task carries a presentation record it is rendered as free text.

Deterministic scoring requires the serve-time answer-key snapshot. Without it, scoring reads whatever the pack says at scoring time, and a pack update between serving and scoring would grade a learner against content they were never shown.

An assessment claim may not outlive the evidence it rests on. Results carrying audio gain an artifact relationship so purge can find them, in the same way `pronunciation_observations` already does.

## Enforced invariants

- `scripts/generate_openapi.py --check` runs in `scripts/verify.py`; a stale committed document fails the gate, as `generate_schemas.py --check` already does. The document is committed at `schemas/openapi/linguawiki.client.v1.json` -- a subdirectory, because the contract tests glob `schemas/*.json` and hold every date-time node to a rule written for JSON Schema snapshots.
- An audit row for a mutation is written in the **service layer**, not in the server, so both entry points produce it. `actor` names the surface (`cli` or `client`); the command name does not differ between them.
- The OpenAPI document reuses the committed `schemas/*.json` snapshots as components and does not redefine them;
  their internal `$defs` pointers are relocated or re-identified on assembly, and a nested `$ref` is asserted to
  resolve through the assembled document.
- The HTTP error payload and the CLI envelope's error payload are one schema; a test asserts the server and the CLI produce the same `code` for the same refusal.
- A test asserts the server holds no reader or writer across requests, and that `writer_locked` and `database_busy` are both retried and then surfaced.
- Negative tests cover a forged `Host`, a foreign `Origin`, and an absent or stale launch token.
- A privacy test asserts the server binds loopback only.
- A test asserts a dimension holding an unanswered task is never served another, exercised by two independent
  callers rather than one, because the browser and the CLI are concurrent from the first client release.
- A test updates the pack between serving and scoring and asserts the score reflects the snapshot.
- A test asserts purging audio invalidates every assessment result resting on it, and that recomputing the
  affected run does not let it displace a newer calibration as the estimate baseline.
- A test asserts a track that forbids retaining responses never has response text persisted in a judging queue
  or a retry record.
