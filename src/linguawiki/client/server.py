"""A single-threaded loopback HTTP server over the service layer.

**Why single-threaded.** Measured against the pinned `duckdb==1.5.5`: a second *read-only*
connection in the same process, beside a held writer, is refused with
`ConnectionException: Can't open a connection to same database file with a different
configuration than existing connections` -- which `_connect` reports as the retryable
`database_busy`, and no amount of retrying can clear it, because the holder is this process.
A second *writer* in one process DuckDB does not refuse at all; the application `flock`
does, between two open file descriptions. So a threaded server would manufacture its own
contention and spend the retry budget on it. Serializing requests is correct for one learner
and is what makes a busy state mean something when it is reported.

**What it is not.** It is not a second implementation. Every refusal below belongs to the
transport -- the host allowlist, the origin check, the token, the body cap, routing -- and
everything else comes from the service layer unchanged.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qsl

from jsonschema import Draft202012Validator

from linguawiki import __version__
from linguawiki.client import responses, routes, runtime, security, shell
from linguawiki.client.security import TOKEN_HEADER
from linguawiki.clock import Clock, SystemClock
from linguawiki.db import migrations as migration_module
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.paths import WorkspacePaths
from linguawiki.retrying import DEFAULT_ATTEMPTS, with_retry
from linguawiki.services import recordings as recording_service

#: How long a connection may be silent before it is dropped. It bounds the wait for the
#: request line as well as for the body: a socket that connects and says nothing is not
#: distinguishable from one that is about to, and on a server that handles one request at a
#: time the difference does not matter -- both hold the learner's only surface.
REQUEST_TIMEOUT_SECONDS = 10

#: The most a request body may be. A loopback page is still a page, and an unbounded
#: `rfile.read` on a single-threaded server is a one-request outage: nothing else is served
#: while it runs. 256 KiB is far more than any route here needs -- the largest is a written
#: response -- and small enough that refusing costs nothing.
MAXIMUM_BODY_BYTES = 256 * 1024

#: What the launch command opens. The token rides in the **fragment**: a fragment is never
#: sent to a server, so it reaches no access log, no `Referer`, and no proxy.
LAUNCH_FRAGMENT = "token="
#: The optional launch-time run reference, after the token in the same fragment.
LAUNCH_RUN = "run="


def _health() -> dict[str, object]:
    """The one route that touches no database, so a page can tell "up" from "busy"."""

    return {
        "application": "linguawiki",
        "application_version": __version__,
        "contract_schema_version": 1,
        "database_schema_version": migration_module.head_version(),
    }


@dataclass(slots=True)
class ClientServer:
    """A bound server, its per-start token, and the record it wrote of itself.

    Binding and serving are separate: `build_server` binds and records, and nothing says the
    caller will go on to serve. `close` has to work either way, which is why `serving` is
    tracked here -- `HTTPServer.shutdown` waits on an event that only `serve_forever` ever
    sets, so calling it on a server that was bound and never served blocks forever.
    """

    server: HTTPServer
    paths: WorkspacePaths
    token: str
    #: A run the launch command asked the page to open, carried in the fragment beside the
    #: token. Without it a fresh page uses discovery; with it, the page opens this run or
    #: says by name why it cannot.
    run: str | None = None
    #: What capture recovery did before this server accepted its first request.
    recovery: recording_service.RecoveryReport | None = None
    serving: threading.Event = field(default_factory=threading.Event)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def launch_url(self) -> str:
        reference = "" if self.run is None else f"&{LAUNCH_RUN}{self.run}"
        return f"{self.origin}/#{LAUNCH_FRAGMENT}{self.token}{reference}"

    def serve_forever(self) -> None:
        """Serve until `close`, recording that `shutdown` is now safe to call."""

        self.serving.set()
        try:
            self.server.serve_forever()
        finally:
            self.serving.clear()

    def close(self) -> None:
        """Stop serving and leave no record of a server that is not there."""

        if self.serving.is_set():
            self.server.shutdown()
        self.server.server_close()
        runtime.clear(self.paths)


def _handler_class(client: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Build the handler bound to one server's paths, clock, and token.

    A closure rather than class attributes: two servers in one process (a test starting a
    second to mint a second token) must not share state, and a class attribute is shared by
    construction.
    """

    paths: WorkspacePaths = client["paths"]
    clock: Clock = client["clock"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"linguawiki/{__version__}"
        #: Bound, because an unbounded wait on a socket that has sent nothing yet is the same
        #: outage as an unbounded read: this server handles one request at a time, so a client
        #: that connects and goes quiet would hold the only surface the learner has.
        timeout = REQUEST_TIMEOUT_SECONDS

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default stderr access log.

            Not only noise: the default logs the request line, and a page that put a token
            in a query string would have it written to the terminal. The token travels in a
            header and a fragment, and this keeps that true even if that ever changes.
            """

        # --- the request pipeline -------------------------------------------------

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            command = "client.request"
            try:
                # Transport refusals first, before anything reads the body or the database.
                # A request that may not be made is refused without being understood.
                headers, repeated = _headers(self)
                security.assert_single_valued(repeated)
                security.assert_host(headers, port=int(client["port"]))
                # The shell, and only the shell, before the token: the first navigation
                # cannot carry a header, and the token arrives in a fragment the browser
                # never sends. Matched on the raw target, so no other spelling is the shell.
                public = shell.shell_file(self.path) if method == "GET" else None
                if public is not None:
                    command = "client.shell"
                    self._respond(
                        200, public[0], content_type=public[1], extra_headers=shell.HEADERS
                    )
                    return
                security.assert_token(headers, expected=str(client["token"]))
                path, query = _split_target(self.path)
                if path == "/health" and method == "GET":
                    command = "client.health"
                    self._respond(200, responses.success(command, _health(), clock))
                    return
                found = routes.match(method, path)
                if found is None:
                    self._route_miss(method)
                    return
                route, values = found
                command = route.command
                if route.mutates:
                    security.assert_origin(headers, origin=str(client["origin"]))
                if route.upload is not None:
                    raw, content_type = self._read_upload(route)
                    body: dict[str, Any] = {}
                else:
                    raw, content_type = b"", ""
                    body = self._read_body()
                    _assert_declared_shape(body, route)
                _assert_declared_query(query, route)
                request = routes.Request(
                    path_values=values,
                    body=body,
                    clock=clock,
                    paths=paths,
                    query=query,
                    raw=raw,
                    content_type=content_type,
                )
                # One reader per read, one writer per mutation, neither held across
                # requests: the service call opens and closes its own connection inside
                # this block, and the response is written after it has closed.
                #
                # A read is retried freely. A mutation is retried only when it carries an
                # idempotency key, and that is the whole rule: a retryable refusal does not
                # say whether anything was written before it, so a blind retry of a mutation
                # can repeat work that already landed -- it did, turning one `POST /runs`
                # into two runs when the trailing report read was refused. The key is what
                # makes a second attempt a replay rather than a repeat, so where there is no
                # key there is no retry.
                #
                # The service layer closes the common case from its own side: a command that
                # commits and then opens a reader for its report retries that *read* itself,
                # so brief contention no longer reports a landed mutation as a failure. This
                # rule is the backstop for when that budget is exhausted.
                # An upload is keyed by the capture identifier in its path, which is what
                # makes its retry a replay.
                attempts = (
                    DEFAULT_ATTEMPTS
                    if not route.mutates
                    or route.upload is not None
                    or request.optional("idempotency_key", str) is not None
                    else 1
                )
                report = with_retry(lambda: route.handler(request), attempts=attempts)
                if route.binary is not None:
                    self._respond(200, report.data, content_type=report.media_type)
                    return
                warnings = tuple(getattr(report, "warnings", ()) or ())
                self._respond(
                    200,
                    responses.success(command, report.model_dump(mode="json"), clock, warnings),
                )
            except LinguaWikiError as failure:
                self._refuse(command, failure)
            except Exception as unexpected:
                # The server is one learner's only surface. An unhandled exception here
                # would close the connection with no body at all, which a page cannot tell
                # from the server having died.
                self._refuse(
                    command,
                    LinguaWikiError(
                        "internal_error",
                        "an unexpected internal error occurred",
                        details=(ErrorDetail(reason=type(unexpected).__name__),),
                    ),
                )

        def _route_miss(self, method: str) -> None:
            allowed = routes.methods_for(_split_target(self.path)[0])
            if allowed:
                # "Wrong verb" and "no such thing" send a caller to different places.
                self._refuse(
                    "client.request",
                    LinguaWikiError(
                        "client_method_not_allowed",
                        f"{self.path} does not answer {method}",
                        details=(ErrorDetail(field="method", reason="not allowed"),),
                    ),
                    extra_headers={"Allow": ", ".join(sorted(set(allowed)))},
                    status=405,
                )
                return
            self._refuse(
                "client.request",
                LinguaWikiError(
                    "client_route_not_found",
                    f"{self.path} is not a route this client server serves",
                    details=(ErrorDetail(field="path", reason="unknown route"),),
                ),
            )

        def _read_body(self) -> dict[str, Any]:
            """Read at most the cap, and enforce it while reading rather than from the header.

            A `Content-Length` is the caller's claim about the body, so believing it is how
            the cap gets bypassed: the read itself is bounded, and a body that keeps coming
            after the cap is refused.
            """

            declared = self.headers.get("Content-Length")
            if declared is None and self.headers.get("Transfer-Encoding") is not None:
                # The cap is enforced against `Content-Length`, so a body that does not
                # declare one is not read at all. Dropping it silently turned a correct
                # chunked request into "content_id is required", which sends a client
                # looking at the wrong field entirely.
                raise LinguaWikiError(
                    "invalid_contract",
                    "a request body must declare its Content-Length; this server does not "
                    "read chunked bodies, because the size cap is what bounds the read",
                    details=(ErrorDetail(field="body", reason="no Content-Length"),),
                )
            length = int(declared) if declared is not None and declared.isdigit() else 0
            if length > MAXIMUM_BODY_BYTES:
                raise _too_large()
            raw = self.rfile.read(min(length, MAXIMUM_BODY_BYTES + 1))
            if len(raw) > MAXIMUM_BODY_BYTES:
                raise _too_large()
            if not raw:
                return {}
            try:
                document = json.loads(raw)
            except (ValueError, RecursionError) as failure:
                raise LinguaWikiError(
                    "invalid_contract",
                    f"the request body is not readable JSON: {failure}",
                    details=(ErrorDetail(field="body", reason="not JSON"),),
                ) from failure
            if not isinstance(document, dict):
                raise LinguaWikiError(
                    "invalid_contract",
                    "the request body must be a JSON object",
                    details=(ErrorDetail(field="body", reason="not an object"),),
                )
            return document

        def _read_upload(self, route: routes.Route) -> tuple[bytes, str]:
            """Read a route's byte body, bounded by the route's own cap while reading.

            The declared type has to be in the route's family: a recording route refuses a
            JSON body rather than storing it as audio.
            """

            content_type = str(self.headers.get("Content-Type") or "").strip()
            family = str(route.upload or "").split("/", 1)[0] + "/"
            if not content_type.lower().startswith(family):
                raise LinguaWikiError(
                    "invalid_contract",
                    f"{route.command} takes a body of type {route.upload}, and this one is "
                    f"{content_type or 'undeclared'}",
                    details=(ErrorDetail(field="Content-Type", reason=content_type or "absent"),),
                )
            declared = self.headers.get("Content-Length")
            if declared is None or not declared.isdigit():
                raise LinguaWikiError(
                    "invalid_contract",
                    "an upload must declare its Content-Length, because the size cap is what "
                    "bounds the read",
                    details=(ErrorDetail(field="body", reason="no Content-Length"),),
                )
            length = int(declared)
            if length > route.upload_limit:
                raise _too_large(route.upload_limit)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise LinguaWikiError(
                    "invalid_contract",
                    f"the upload ended after {len(raw)} of the {length} bytes it declared",
                    details=(ErrorDetail(field="body", reason="truncated"),),
                )
            return raw, content_type

        # --- writing ---------------------------------------------------------------

        def _refuse(
            self,
            command: str,
            error: LinguaWikiError,
            *,
            extra_headers: dict[str, str] | None = None,
            status: int | None = None,
        ) -> None:
            headers = dict(extra_headers or {})
            if error.payload.retryable:
                headers["Retry-After"] = str(responses.RETRY_AFTER_SECONDS)
            self._respond(
                status or responses.status_for(error),
                responses.failure(command, error, clock),
                extra_headers=headers,
            )

        def _respond(
            self,
            status: int,
            body: bytes,
            *,
            extra_headers: dict[str, str] | None = None,
            content_type: str = "application/json; charset=utf-8",
        ) -> None:
            # One request per connection. HTTP/1.1 keeps a connection alive by default, and
            # this server handles one at a time: the first client's idle socket then sat in
            # the only handler slot waiting for a second request it had no intention of
            # sending, and every other connection queued behind it. Keeping the version at
            # 1.1 preserves the `Content-Length` semantics the body cap depends on; closing
            # after each response is what keeps serializing *requests* from serializing
            # *clients*. A loopback handshake per request costs nothing worth measuring.
            self.close_connection = True
            self.send_response(status)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # A local page, and nothing else, may read these answers. `Access-Control-*` is
            # deliberately absent: granting a cross-origin read would undo the origin check.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _headers(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, int]]:
    """The request's headers as a plain map, plus how often each decisive one appeared.

    `email.message.Message` is not a mapping and permits repeats, so the count is taken here
    rather than being lost in a `dict()` that silently keeps the last value. The keys are
    **folded to lower case**, because header names are case-insensitive and a plain `dict()`
    of them is not: a client sending `host` and `x-linguawiki-token` -- which it is entitled
    to do -- was refused as having sent neither, which is the most misleading answer
    available. It says the client sent nothing where the client sent the right thing.
    """

    values = {name.lower(): str(value) for name, value in handler.headers.items()}
    repeated = {name: len(handler.headers.get_all(name) or ()) for name in security.SINGLE_VALUED}
    return values, repeated


def _assert_declared_shape(body: dict[str, Any], route: routes.Route) -> None:
    """Hold the request to the schema the published document says it must satisfy.

    Nothing enforced it. The document declares `additionalProperties: false`, and a body
    carrying `idempotency_keey` created a run with no key at all -- so a caller that believed
    its retries were deduplicated opened another run with every one of them. A published
    constraint nothing checks is documentation shaped like a validation, which is worse than
    none because nobody looks at it twice.

    The error names the failing path, so a caller is told *which* field is wrong rather than
    that something is.
    """

    if route.request_schema is None:
        return
    failures = sorted(
        Draft202012Validator(dict(route.request_schema)).iter_errors(body),
        key=lambda failure: list(failure.path),
    )
    if not failures:
        return
    raise LinguaWikiError(
        "invalid_contract",
        f"the request body does not match the published schema for {route.command}: "
        f"{failures[0].message}",
        details=tuple(
            ErrorDetail(
                field=".".join(str(part) for part in failure.path) or "body",
                reason=failure.message,
            )
            for failure in failures
        ),
    )


def _split_target(target: str) -> tuple[str, dict[str, str]]:
    """The path a request names, and its query as one value per name.

    Routing reads the path alone, so a query never hides a route. A name sent twice is
    refused rather than resolved, for the reason a repeated header is: two readers would
    pick different values.
    """

    path, _, raw = target.partition("?")
    pairs = parse_qsl(raw, keep_blank_values=True)
    names = [name for name, _ in pairs]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise LinguaWikiError(
            "invalid_contract",
            f"query parameter {', '.join(repeated)} was sent more than once",
            details=(ErrorDetail(field="query", reason="repeated"),),
        )
    return path, dict(pairs)


def _assert_declared_query(query: dict[str, str], route: routes.Route) -> None:
    """A query is validated like a body, and a route that publishes none refuses one."""

    if route.query_schema is None:
        if query:
            raise LinguaWikiError(
                "invalid_contract",
                f"{route.command} takes no query parameters, and {sorted(query)} were sent",
                details=(ErrorDetail(field="query", reason="not published"),),
            )
        return
    failures = list(Draft202012Validator(dict(route.query_schema)).iter_errors(query))
    if failures:
        raise LinguaWikiError(
            "invalid_contract",
            f"the query does not match the published schema for {route.command}: "
            f"{failures[0].message}",
            details=tuple(
                ErrorDetail(
                    field=".".join(str(part) for part in failure.path) or "query",
                    reason=failure.message,
                )
                for failure in failures
            ),
        )


def _too_large(limit: int = MAXIMUM_BODY_BYTES) -> LinguaWikiError:
    return LinguaWikiError(
        "client_body_too_large",
        f"a request body may be at most {limit} bytes",
        details=(ErrorDetail(field="body", reason="over the cap"),),
    )


def build_server(
    paths: WorkspacePaths,
    *,
    port: int = 0,
    clock: Clock | None = None,
    run: str | None = None,
    recovery_wait: float = recording_service.RECOVERY_WAIT_SECONDS,
) -> ClientServer:
    """Bind a server on loopback, mint its token, and record where it is listening.

    `port=0` asks the operating system for a free one, which is what a local client wants:
    a fixed port is one more thing to collide with and nothing depends on it, because the
    port is discoverable from the runtime file.

    Captures a crash left unresolved are recovered first, before anything is bound: a
    request served over a recording that is neither registered nor refused would be
    answered from a guess. Recovery needs the writer, waits `recovery_wait` seconds for
    it, and then refuses to start with `writer_locked`.
    """

    active_clock = clock or SystemClock()
    if run is not None and re.fullmatch(routes.RUN_ID, run) is None:
        # Checked before binding: a reference that could never name a run would open a page
        # that refuses on its first request, after the learner has been sent to it.
        raise LinguaWikiError(
            "invalid_arguments",
            f"{run} is not a run identifier",
            details=(ErrorDetail(field="run", reason="not a run identifier"),),
        )
    recovered = recording_service.recover(paths, clock=active_clock, wait_seconds=recovery_wait)
    token = runtime.mint_token()
    state: dict[str, Any] = {"paths": paths, "clock": active_clock, "token": token}
    # `HTTPServer`, not `ThreadingHTTPServer`. See the module docstring: the threaded form
    # would create the contention it then retries.
    server = HTTPServer(("127.0.0.1", port), _handler_class(state))
    bound = int(server.server_address[1])
    # The handler reads the port and origin from here rather than from `server_address`,
    # which is typed as holding anything a socket family might use.
    state["port"] = bound
    state["origin"] = f"http://127.0.0.1:{bound}"
    runtime.write(paths, port=bound, clock=active_clock)
    return ClientServer(server=server, paths=paths, token=token, run=run, recovery=recovered)


def serve(
    paths: WorkspacePaths,
    *,
    port: int = 0,
    clock: Clock | None = None,
    open_browser: bool = True,
    run: str | None = None,
) -> ClientServer:
    """Build a server and run it in the foreground until interrupted.

    Returns rather than exits, so the caller decides what to print. A daemon is a second
    lifecycle nobody asked for and a second way to leave a stale runtime record behind.
    """

    client = build_server(paths, port=port, clock=clock, run=run)
    if open_browser:
        import webbrowser

        webbrowser.open(client.launch_url)
    try:
        client.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return client


__all__ = [
    "LAUNCH_FRAGMENT",
    "LAUNCH_RUN",
    "MAXIMUM_BODY_BYTES",
    "TOKEN_HEADER",
    "ClientServer",
    "build_server",
    "serve",
]
