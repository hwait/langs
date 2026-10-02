"""A test client for the loopback server that controls every header and keeps raw bytes.

`http.client` rather than `urllib.request`, for the reason `test_client_server` gives:
setting `Request.host` retargets the connection rather than overriding the header. This one
keeps the body as bytes, because the audio route and the public shell do not answer JSON.
"""

from __future__ import annotations

import http.client
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from linguawiki.client import server as server_module
from linguawiki.clock import Clock
from linguawiki.paths import WorkspacePaths


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def payload(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.body or b"{}")
        return document

    @property
    def data(self) -> dict[str, Any]:
        assert self.payload.get("ok") is True, self.payload
        data: dict[str, Any] = self.payload["data"]
        return data

    @property
    def code(self) -> str:
        return str(self.payload["error"]["code"])


@dataclass(slots=True)
class Client:
    server: server_module.ClientServer
    #: Every request body this client sent, by path, so a test can assert what a
    #: submission did *not* carry.
    sent: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = "",
        host: str | None = "",
        origin: str | None = "",
    ) -> Answer:
        self.sent.append((method, path, body))
        data = None if body is None else json.dumps(body).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                connection.putheader(
                    "Host", f"127.0.0.1:{self.server.port}" if host == "" else host
                )
            if token is not None:
                connection.putheader(
                    server_module.TOKEN_HEADER, self.server.token if token == "" else token
                )
            if origin is not None and method == "POST":
                connection.putheader("Origin", self.origin if origin == "" else origin)
            if data is not None:
                connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(data or b"")))
            connection.endheaders()
            if data:
                connection.send(data)
            answer = connection.getresponse()
            return Answer(
                status=answer.status,
                headers={name.lower(): value for name, value in answer.getheaders()},
                body=answer.read(),
            )
        finally:
            connection.close()

    def upload(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str = "audio/wav",
        declared_length: int | None = None,
        origin: str | None = "",
    ) -> Answer:
        """POST raw bytes, the way the page uploads a recording.

        `declared_length` lets a test declare more than it sends and then hang up, which is
        a connection dropped mid-capture.
        """

        self.sent.append(("POST", path, None))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", f"127.0.0.1:{self.server.port}")
            connection.putheader(server_module.TOKEN_HEADER, self.server.token)
            if origin is not None:
                connection.putheader("Origin", self.origin if origin == "" else origin)
            connection.putheader("Content-Type", content_type)
            connection.putheader(
                "Content-Length", str(len(data) if declared_length is None else declared_length)
            )
            connection.endheaders()
            connection.send(data)
            if declared_length is not None and declared_length > len(data):
                # Hang up mid-body. Half-closing the write side is what a browser tab
                # closed during an upload looks like to the server.
                import socket

                assert connection.sock is not None
                connection.sock.shutdown(socket.SHUT_WR)
            answer = connection.getresponse()
            return Answer(
                status=answer.status,
                headers={name.lower(): value for name, value in answer.getheaders()},
                body=answer.read(),
            )
        finally:
            connection.close()

    def post(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> Answer:
        return self.request("POST", path, {} if body is None else body, **kwargs)

    def get(self, path: str, **kwargs: Any) -> Answer:
        return self.request("GET", path, **kwargs)


def key() -> str:
    return str(uuid.uuid4())


@contextmanager
def serving(
    paths: WorkspacePaths, clock: Clock, *, run: str | None = None, recovery_wait: float = 2.0
) -> Iterator[Client]:
    server = server_module.build_server(paths, clock=clock, run=run, recovery_wait=recovery_wait)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(server)
    finally:
        server.close()
        thread.join(timeout=5)
