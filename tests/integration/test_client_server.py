"""The loopback server: a transport, and the refusals that keep it from being a hole.

Loopback is not a security boundary. DNS rebinding is a documented attack against local
servers that trust the `Host` header, so every rule here -- the host allowlist, the origin
check on mutations, the per-start launch token -- runs before the service layer is reached.

The other half is honesty about the database. DuckDB serves one connection per file, and a
second connection in this process beside a held writer is refused outright, which is why the
server is single-threaded and why `database_busy` and `writer_locked` are retried and then
surfaced rather than hidden behind a spinner.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from linguawiki.client import runtime as runtime_module
from linguawiki.client import server as server_module
from linguawiki.contract_validation import validate_json_contract
from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import assessment as assessment_service
from tests.conftest import PolishWorkspace


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: dict[str, str]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunningServer:
    """A test client that controls every header, including the ones urllib owns.

    `http.client` rather than `urllib.request` deliberately: setting `Request.host` retargets
    the *connection* rather than overriding the header, so a forged-Host test written with
    urllib goes off to resolve the forged name instead of reaching this server.
    """

    client: server_module.ClientServer

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.client.port}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = "",
        host: str | None = "",
        origin: str | None = "",
        raw: bytes | None = None,
        repeat: tuple[str, str] | None = None,
    ) -> Response:
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
        headers.append(("Host", f"127.0.0.1:{self.client.port}" if host == "" else str(host)))
        if host is None:
            headers = [entry for entry in headers if entry[0] != "Host"]
        if token is not None:
            headers.append(
                (server_module.TOKEN_HEADER, self.client.token if token == "" else token)
            )
        if origin is not None:
            headers.append(("Origin", self.origin if origin == "" else origin))
        if repeat is not None:
            headers.append(repeat)
        connection = http.client.HTTPConnection("127.0.0.1", self.client.port, timeout=10)
        try:
            # `skip_host` keeps `http.client` from adding its own Host beside the one under
            # test, which would make every forged-Host case a repeated-header case instead.
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(data or b"")))
            connection.endheaders()
            if data:
                connection.send(data)
            answer = connection.getresponse()
            return Response(
                status=answer.status,
                headers=dict(answer.getheaders()),
                payload=json.loads(answer.read() or b"{}"),
            )
        finally:
            connection.close()


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> Iterator[RunningServer]:
    client = server_module.build_server(polish_workspace.paths, clock=polish_workspace.clock)
    thread = threading.Thread(target=client.serve_forever, daemon=True)
    thread.start()
    try:
        yield RunningServer(client)
    finally:
        client.close()
        thread.join(timeout=5)


def _outward_address() -> str | None:
    """This machine's own non-loopback address, or `None` when it has none.

    Found by asking the routing table which source address would be used to reach a public
    address -- no packet is sent. `gethostbyname(gethostname())` is not a substitute: on
    macOS it frequently answers 127.0.0.1, which would make the test below assert nothing.
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


def test_the_server_binds_loopback_and_nothing_else(running: RunningServer) -> None:
    """A privacy property, asserted on the socket rather than on the intention."""

    assert running.client.server.server_address[0] == "127.0.0.1"
    outward = _outward_address()
    if outward is None:
        pytest.skip("this host has no non-loopback address to attempt the connection from")
    attempt = socket.socket()
    attempt.settimeout(0.5)
    try:
        with pytest.raises(OSError):
            attempt.connect((outward, running.client.port))
    finally:
        attempt.close()


def test_health_answers_without_touching_the_database(running: RunningServer) -> None:
    answer = running.request("GET", "/health")

    assert answer.status == 200
    assert answer.payload["ok"] is True
    assert answer.payload["data"]["application"] == "linguawiki"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"token": None}, "client_token_required"),
        ({"token": "   "}, "client_token_required"),
        ({"token": "not-the-token"}, "client_token_invalid"),
        ({"host": "linguawiki.example.com"}, "client_host_denied"),
        ({"host": None}, "client_host_denied"),
    ],
)
def test_every_transport_refusal_keeps_its_own_code(
    running: RunningServer, kwargs: dict[str, Any], code: str
) -> None:
    """Collapsing these would take away a page's ability to tell them apart.

    "Reload with the token" and "you are not talking to the server you think you are" send
    somebody to very different places.
    """

    answer = running.request("GET", "/health", **kwargs)

    assert answer.status == 403
    assert answer.payload["ok"] is False
    assert answer.payload["error"]["code"] == code


def test_a_mutation_without_an_origin_is_refused(running: RunningServer) -> None:
    """Absent is the case the data is least trustworthy, not a pass.

    A cross-site form post sends no `Origin` in some browsers, so treating absence as
    permission is the whole attack.
    """

    answer = running.request("POST", "/runs", body={}, origin=None)

    assert answer.status == 403
    assert answer.payload["error"]["code"] == "client_origin_denied"


def test_a_mutation_from_a_foreign_origin_is_refused(running: RunningServer) -> None:
    answer = running.request("POST", "/runs", body={}, origin="https://evil.example.com")

    assert answer.status == 403
    assert answer.payload["error"]["code"] == "client_origin_denied"


def test_a_read_does_not_require_an_origin(running: RunningServer) -> None:
    """The token already proves the caller was handed the URL; a read changes nothing."""

    answer = running.request("GET", "/health", origin=None)

    assert answer.status == 200


def test_a_token_from_a_previous_server_start_is_refused(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    other = server_module.build_server(polish_workspace.paths, clock=polish_workspace.clock)
    try:
        stale = other.token
    finally:
        other.close()

    answer = running.request("GET", "/health", token=stale)

    assert answer.payload["error"]["code"] == "client_token_invalid"


def test_an_unknown_path_is_a_named_refusal_rather_than_a_stack_trace(
    running: RunningServer,
) -> None:
    answer = running.request("GET", "/nope")

    assert answer.status == 404
    assert answer.payload["error"]["code"] == "client_route_not_found"


def test_a_body_over_the_cap_is_refused_and_the_server_keeps_serving(
    running: RunningServer,
) -> None:
    """An unbounded read on a single-threaded server is a one-request outage."""

    oversized = json.dumps({"padding": "x" * (server_module.MAXIMUM_BODY_BYTES + 1)}).encode()

    answer = running.request("POST", "/runs", raw=oversized)

    assert answer.status == 413
    assert answer.payload["error"]["code"] == "client_body_too_large"
    assert running.request("GET", "/health").status == 200


def test_a_body_that_is_not_an_object_is_refused_by_name(running: RunningServer) -> None:
    answer = running.request("POST", "/runs", raw=b"[1, 2, 3]")

    assert answer.status == 400
    assert answer.payload["error"]["code"] == "invalid_contract"


def test_a_busy_database_is_surfaced_as_retryable_rather_than_hidden(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    """There is no "reads never fail". Another process holding the file is normal."""

    with open_writer(polish_workspace.paths, command="test.hold", clock=polish_workspace.clock):
        answer = running.request("GET", "/runs/asm_01ARZ3NDEKTSV4RRFFQ69G5FAV/screen")

    assert answer.status == 503
    assert answer.payload["error"]["retryable"] is True
    assert answer.headers["Retry-After"] == "1"
    assert answer.payload["error"]["code"] in {"database_busy", "writer_locked"}


def test_the_server_holds_no_connection_between_requests(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    assert running.request("GET", "/health").status == 200

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)

    assert running.request("GET", f"/runs/{run.run_id}").status == 200


def test_the_runtime_file_records_the_port_and_never_the_token(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    recorded = json.loads(
        runtime_module.runtime_path(polish_workspace.paths).read_text(encoding="utf-8")
    )

    assert recorded["port"] == running.client.port
    assert running.client.token not in json.dumps(recorded)
    assert "token" not in recorded


def test_closing_the_server_removes_the_runtime_file(
    polish_workspace: PolishWorkspace,
) -> None:
    client = server_module.build_server(polish_workspace.paths, clock=polish_workspace.clock)
    path = runtime_module.runtime_path(polish_workspace.paths)
    assert path.exists()

    client.close()

    assert not path.exists()


def test_the_launch_url_carries_the_token_in_the_fragment(running: RunningServer) -> None:
    """Never the query string: a fragment reaches no access log, no `Referer`, no proxy."""

    url = running.client.launch_url

    assert url.startswith(f"http://127.0.0.1:{running.client.port}/#")
    assert f"token={running.client.token}" in url.split("#", 1)[1]
    assert "?" not in url


# --- the calibration loop, and the equivalence that keeps it honest ---------------------


def _drive(running: RunningServer, run_id: str, *, score: float = 1.0) -> int:
    """Answer every task the server serves, over HTTP, until the run has nothing open."""

    for scored in range(200):
        served = running.request("POST", f"/runs/{run_id}/tasks", body={})
        assert served.status == 200, served.payload
        data = served.payload["data"]
        if "content_id" not in data:
            return scored
        answer = running.request(
            "POST",
            f"/runs/{run_id}/results",
            body={"content_id": data["content_id"], "score": score},
        )
        assert answer.status == 200, answer.payload
    raise AssertionError("the run never closed")


def test_a_calibration_runs_end_to_end_over_http_with_no_model(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    opened = running.request("POST", "/runs", body={})
    assert opened.status == 200, opened.payload
    run_id = opened.payload["data"]["run_id"]

    scored = _drive(running, run_id)

    screen = running.request("GET", f"/runs/{run_id}/screen")
    assert screen.status == 200
    assert screen.payload["data"]["outstanding"] == []
    closed = running.request("POST", f"/runs/{run_id}/finalization", body={})
    assert closed.status == 200, closed.payload
    assert closed.payload["data"]["status"] == "finalized"
    assert closed.payload["data"]["tasks_recorded"] == scored


def test_the_server_reaches_the_same_estimates_the_service_layer_would(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    """The server is a transport. Driving a run through it must not change its arithmetic."""

    opened = running.request("POST", "/runs", body={})
    run_id = opened.payload["data"]["run_id"]
    _drive(running, run_id, score=0.5)
    running.request("POST", f"/runs/{run_id}/finalization", body={})

    through_http = running.request("GET", f"/runs/{run_id}").payload["data"]
    directly = assessment_service.report(polish_workspace.paths, run=run_id)

    assert through_http["dimensions"] == [
        dimension.model_dump(mode="json") for dimension in directly.dimensions
    ]


def test_pausing_and_resuming_go_through_the_same_lifecycle(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    opened = running.request("POST", "/runs", body={})
    run_id = opened.payload["data"]["run_id"]

    paused = running.request("POST", f"/runs/{run_id}/status", body={"status": "paused"})
    assert paused.payload["data"]["status"] == "paused"
    refused = running.request("POST", f"/runs/{run_id}/tasks", body={})
    resumed = running.request("POST", f"/runs/{run_id}/status", body={"status": "in-progress"})

    assert refused.status == 422
    assert refused.payload["error"]["code"] == "assessment_run_paused"
    assert resumed.payload["data"]["status"] == "in-progress"


def test_the_server_records_the_surface_and_not_a_different_command(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    from linguawiki.db.connection import open_reader

    opened = running.request("POST", "/runs", body={})
    run_id = opened.payload["data"]["run_id"]
    served = running.request("POST", f"/runs/{run_id}/tasks", body={})
    running.request(
        "POST",
        f"/runs/{run_id}/results",
        body={"content_id": served.payload["data"]["content_id"], "score": 1.0},
    )

    with open_reader(polish_workspace.paths) as database:
        rows = [
            (str(actor), str(command))
            for actor, command in database.query(
                "SELECT actor, command FROM audit_log "
                "WHERE command IN ('assessment.next', 'assessment.record')"
            )
        ]

    assert sorted(rows) == [("client", "assessment.next"), ("client", "assessment.record")]


def test_a_retried_serve_over_http_does_not_consume_a_second_task(
    running: RunningServer,
) -> None:
    opened = running.request("POST", "/runs", body={})
    run_id = opened.payload["data"]["run_id"]
    body = {"idempotency_key": "http-serve-1"}

    first = running.request("POST", f"/runs/{run_id}/tasks", body=body)
    again = running.request("POST", f"/runs/{run_id}/tasks", body=body)

    assert again.payload["data"]["content_id"] == first.payload["data"]["content_id"]
    assert again.payload["data"]["served_again"] is True


def test_a_changed_payload_under_one_key_is_a_conflict(running: RunningServer) -> None:
    opened = running.request("POST", "/runs", body={})
    run_id = opened.payload["data"]["run_id"]
    served = running.request("POST", f"/runs/{run_id}/tasks", body={})
    content_id = served.payload["data"]["content_id"]
    running.request(
        "POST",
        f"/runs/{run_id}/results",
        body={"content_id": content_id, "score": 1.0, "idempotency_key": "http-score-1"},
    )

    conflicting = running.request(
        "POST",
        f"/runs/{run_id}/results",
        body={"content_id": content_id, "score": 0.0, "idempotency_key": "http-score-1"},
    )

    assert conflicting.status == 409
    assert conflicting.payload["error"]["code"] == "idempotency_conflict"


def test_a_repeated_decisive_header_is_refused_rather_than_resolved(
    running: RunningServer,
) -> None:
    """Two Host headers is one request two readers would resolve differently."""

    answer = running.request("GET", "/health", repeat=("Host", "evil.example.com"))

    assert answer.status == 403
    assert answer.payload["error"]["code"] == "client_header_repeated"


def test_a_path_that_answers_another_method_says_so(running: RunningServer) -> None:
    """ "Wrong verb" and "no such thing" send a caller to different places."""

    answer = running.request("GET", "/runs")

    assert answer.status == 405
    assert answer.payload["error"]["code"] == "client_method_not_allowed"
    assert answer.headers["Allow"] == "POST"


def test_an_unparseable_run_identifier_is_a_routing_miss(running: RunningServer) -> None:
    """A path that is not an identifier anybody could have meant is not a service question."""

    answer = running.request("GET", "/runs/not-a-run-id/screen")

    assert answer.status == 404
    assert answer.payload["error"]["code"] == "client_route_not_found"


def test_a_body_field_of_the_wrong_type_is_refused_rather_than_guessed(
    running: RunningServer,
) -> None:
    opened = running.request("POST", "/runs", body={})
    run_id = opened.payload["data"]["run_id"]
    served = running.request("POST", f"/runs/{run_id}/tasks", body={})

    answer = running.request(
        "POST",
        f"/runs/{run_id}/results",
        body={"content_id": served.payload["data"]["content_id"], "score": "high"},
    )

    assert answer.status == 400
    assert answer.payload["error"]["code"] == "invalid_arguments"


def test_every_refusal_the_transport_can_raise_is_classified_as_one(running: RunningServer) -> None:
    """The status mapping is by class, so a new transport code must join its class.

    `client_header_repeated` was added to the checks and not to the class, and came back as
    422 -- "understood, and refused on its own terms" -- for a request that was refused
    permission. Read out of the module rather than listed here, so the next code added to
    `security.py` cannot be classified by nobody.
    """

    import re
    from pathlib import Path as _Path

    from linguawiki.client import responses, security

    source = _Path(security.__file__).read_text(encoding="utf-8")
    raised = set(re.findall(r'"(client_[a-z_]+)"', source))

    assert raised, "no refusal codes were found in the security module"
    assert raised <= responses.FORBIDDEN_CODES
    for code in sorted(raised):
        assert responses.status_for(LinguaWikiError(code, "x")) == 403, code


# --- the equivalence ADR 0008 makes a standing obligation -------------------------------


def _cli_code(workspace: PolishWorkspace, argv: list[str]) -> str:
    """The error code the CLI reports for one refusal, from its own envelope."""

    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "linguawiki",
            *argv,
            "--workspace",
            str(workspace.root),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, result.stdout
    return str(json.loads(result.stderr)["error"]["code"])


def test_the_server_refuses_exactly_what_the_cli_refuses(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    """Asserted over the same cases, not asserted in principle.

    The server is a second entry point and has to stay as honest as the first. Comparing the
    *code* rather than the status is what makes this a test of the service layer being shared
    rather than of two mappings agreeing.
    """

    opened = running.request("POST", "/runs", body={})
    run_id = opened.payload["data"]["run_id"]
    absent = "cnt_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    missing_run = "asm_01ARZ3NDEKTSV4RRFFQ69G5FAV"

    cases: list[tuple[str, list[str], tuple[str, str, dict[str, Any]]]] = [
        (
            "a task that was never served",
            ["assessment", "record", "--run", run_id, "--content", absent, "--score", "1.0"],
            ("POST", f"/runs/{run_id}/results", {"content_id": absent, "score": 1.0}),
        ),
        (
            "a run that does not exist",
            ["assessment", "screen", "--run", missing_run],
            ("GET", f"/runs/{missing_run}/screen", {}),
        ),
        (
            "an empty response, which is a skip rather than a wrong answer",
            [
                "assessment",
                "record",
                "--run",
                run_id,
                "--content",
                absent,
                "--response",
                "   ",
            ],
            (
                "POST",
                f"/runs/{run_id}/results",
                {"content_id": absent, "response": "   "},
            ),
        ),
    ]

    for description, argv, (method, path, body) in cases:
        through_the_server = running.request(method, path, body=body or None)
        assert through_the_server.payload["ok"] is False, description
        assert through_the_server.payload["error"]["code"] == _cli_code(polish_workspace, argv), (
            description
        )


def test_both_entry_points_build_the_error_payload_from_one_contract(
    polish_workspace: PolishWorkspace, running: RunningServer
) -> None:
    """One error schema, not two that happen to agree today."""

    answer = running.request("GET", "/runs/asm_01ARZ3NDEKTSV4RRFFQ69G5FAV/screen")

    validate_json_contract(
        "linguawiki.cli.error.v1",
        answer.payload,
        schema_directory=Path(__file__).resolve().parents[2] / "schemas",
    )
