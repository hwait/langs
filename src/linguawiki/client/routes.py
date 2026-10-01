"""The route table: eight operations, each one service call wide.

A handler resolves its arguments, calls one service function, and returns the report. There
is no business logic here and there must not be -- selection, scoring, the stop rule, the
posterior update and every refusal stay in Python where ADR 0003 puts them, and a second
implementation in the transport is the failure that ADR exists to prevent.

The `command` each route carries is the CLI's own name, so an audit row written through the
server reads exactly like one written through the CLI. `actor` is what distinguishes them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from linguawiki.clock import Clock
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.paths import WorkspacePaths
from linguawiki.services import assessment as assessment_service
from linguawiki.services import assessment_view as view_service

#: The actor recorded against every mutation this server drives. The command name does not
#: change between entry points -- that would make every audit query ask twice -- so this is
#: the one place the surface is named.
ACTOR = "client"

#: A run identifier in a path. Matched narrowly so a path that is not one is a routing miss
#: rather than a service-layer refusal about an identifier nobody could have meant.
RUN_ID = r"(?P<run_id>asm_[0-9A-HJKMNP-TV-Z]{26})"


@dataclass(frozen=True, slots=True)
class Request:
    """One parsed request: what the path named, and what the body asked for."""

    path_values: Mapping[str, str]
    body: Mapping[str, Any]
    clock: Clock
    paths: WorkspacePaths

    def optional(self, name: str, kind: type[Any]) -> Any:
        """A body field of the expected type, or `None` when it is absent.

        A field of the wrong type is a refusal rather than a coercion: `score: "1.0"` and
        `score: 1.0` are the same intention, but `score: "high"` is not a number and
        guessing what it meant is how a learner gets a mark nobody computed.
        """

        if name not in self.body or self.body[name] is None:
            return None
        value = self.body[name]
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
            raise LinguaWikiError(
                "invalid_arguments",
                f"{name} must be {kind.__name__}",
                details=(ErrorDetail(field=name, reason=f"not {kind.__name__}"),),
            )
        return value

    def required(self, name: str, kind: type[Any]) -> Any:
        value = self.optional(name, kind)
        if value is None:
            raise LinguaWikiError(
                "invalid_arguments",
                f"{name} is required",
                details=(ErrorDetail(field=name, reason="absent"),),
            )
        return value

    def strings(self, name: str) -> list[str]:
        value = self.body.get(name)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise LinguaWikiError(
                "invalid_arguments",
                f"{name} must be a list of strings",
                details=(ErrorDetail(field=name, reason="not a list of strings"),),
            )
        return list(value)


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    command: str
    handler: Callable[[Request], Any]
    #: Whether this route may change a learner's record. It decides two things at once: the
    #: `Origin` check, and whether a writer is taken -- so a route cannot be guarded as a
    #: read and then write.
    mutates: bool


def _start(request: Request) -> Any:
    return assessment_service.start(
        request.paths,
        track=request.optional("track", str),
        run_type=request.optional("run_type", str) or "pilot-calibration",
        dimensions=request.strings("dimensions") or None,
        modalities=request.strings("modalities") or None,
        idempotency_key=request.optional("idempotency_key", str),
        clock=request.clock,
        command="assessment.start",
    )


def _serve(request: Request) -> Any:
    return assessment_service.next_task(
        request.paths,
        run=request.path_values["run_id"],
        clock=request.clock,
        command="assessment.next",
        actor=ACTOR,
        idempotency_key=request.optional("idempotency_key", str),
    )


def _record(request: Request) -> Any:
    return assessment_service.record(
        request.paths,
        run=request.path_values["run_id"],
        content_id=request.required("content_id", str),
        score=request.optional("score", float),
        response=request.optional("response", str),
        response_visibility=request.optional("response_visibility", str),
        response_excerpt=request.optional("response_excerpt", str),
        rubric=request.optional("rubric", dict),
        assessor_kind=request.optional("assessor_kind", str) or "deterministic",
        assessor=request.optional("assessor", str),
        confidence=request.optional("confidence", str) or "medium",
        idempotency_key=request.optional("idempotency_key", str),
        clock=request.clock,
        command="assessment.record",
        actor=ACTOR,
    )


def _set_status(request: Request) -> Any:
    return assessment_service.set_status(
        request.paths,
        run=request.path_values["run_id"],
        status=request.required("status", str),
        clock=request.clock,
        command="assessment.status",
    )


def _finalize(request: Request) -> Any:
    return assessment_service.finalize(
        request.paths,
        run=request.path_values["run_id"],
        reason=request.optional("reason", str) or "completed",
        idempotency_key=request.optional("idempotency_key", str),
        clock=request.clock,
        command="assessment.finalize",
    )


def _screen(request: Request) -> Any:
    return view_service.run_screen(
        request.paths, run=request.path_values["run_id"], clock=request.clock
    )


def _report(request: Request) -> Any:
    return assessment_service.report(
        request.paths, run=request.path_values["run_id"], clock=request.clock
    )


ROUTES: tuple[Route, ...] = (
    Route("POST", re.compile(r"^/runs$"), "assessment.start", _start, mutates=True),
    Route("GET", re.compile(rf"^/runs/{RUN_ID}$"), "assessment.report", _report, mutates=False),
    Route(
        "GET", re.compile(rf"^/runs/{RUN_ID}/screen$"), "assessment.screen", _screen, mutates=False
    ),
    Route("POST", re.compile(rf"^/runs/{RUN_ID}/tasks$"), "assessment.next", _serve, mutates=True),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/results$"),
        "assessment.record",
        _record,
        mutates=True,
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/status$"),
        "assessment.status",
        _set_status,
        mutates=True,
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/finalization$"),
        "assessment.finalize",
        _finalize,
        mutates=True,
    ),
)


def match(method: str, path: str) -> tuple[Route, Mapping[str, str]] | None:
    """The route for this request, or `None` when nothing serves it.

    A path that matches under another method is still a miss here. The server reports it as
    405 rather than 404, because "wrong verb" and "no such thing" send a caller to different
    places.
    """

    for route in ROUTES:
        found = route.pattern.match(path)
        if found is not None and route.method == method:
            return route, found.groupdict()
    return None


def methods_for(path: str) -> tuple[str, ...]:
    """Which methods this path does serve, for a 405's `Allow` header."""

    return tuple(route.method for route in ROUTES if route.pattern.match(path) is not None)


__all__ = ["ACTOR", "ROUTES", "Request", "Route", "match", "methods_for"]
