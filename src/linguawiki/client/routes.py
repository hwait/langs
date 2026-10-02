"""The route table: eleven operations, each one service call wide.

A handler resolves its arguments, calls one service function, and returns the report. There
is no business logic here and there must not be -- selection, scoring, the stop rule, the
posterior update and every refusal stay in Python where ADR 0003 puts them, and a second
implementation in the transport is the failure that ADR exists to prevent.

The `command` each route carries is the CLI's own name, so an audit row written through the
server reads exactly like one written through the CLI. `actor` is what distinguishes them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from linguawiki.clock import Clock
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import DEFAULT_SCORING, SCORING_CONDITIONS
from linguawiki.services import assessment as assessment_service
from linguawiki.services import assessment_view as view_service
from linguawiki.services import recordings as recording_service

#: The actor recorded against every mutation this server drives. The command name does not
#: change between entry points -- that would make every audit query ask twice -- so this is
#: the one place the surface is named.
ACTOR = "client"

#: Every mutating route takes one, and it means the same thing on all of them: retrying is
#: safe, and reusing it for a different request is a conflict rather than a replay.
IDEMPOTENCY_KEY = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Operation-scoped key bound to a hash of this request. The same key with the same "
        "request replays the first call's result; the same key with a different request, or "
        "one recorded against another operation, is refused with idempotency_conflict."
    ),
}


def _body(properties: Mapping[str, Any], *, required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
    }
    if required:
        schema["required"] = list(required)
    return schema


#: A run identifier in a path. Matched narrowly so a path that is not one is a routing miss
#: rather than a service-layer refusal about an identifier nobody could have meant.
RUN_ID = r"(?P<run_id>asm_[0-9A-HJKMNP-TV-Z]{26})"
#: A served task's content identifier in a path, matched as narrowly as a run's.
CONTENT_ID = r"(?P<content_id>cnt_[0-9A-HJKMNP-TV-Z]{26})"
#: The page's own identifier for one recording: a lower-case v4 UUID it minted when the
#: learner pressed stop. It is the upload's idempotency key, so it sits in the path where
#: a retry cannot drop it.
CAPTURE_ID = r"(?P<capture_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"


@dataclass(frozen=True, slots=True)
class Request:
    """One parsed request: what the path named, and what the body asked for."""

    path_values: Mapping[str, str]
    body: Mapping[str, Any]
    clock: Clock
    paths: WorkspacePaths
    #: The query string, one value per name. Only a route that publishes a query schema
    #: receives one; every other route refuses a query rather than ignoring it.
    query: Mapping[str, str] = field(default_factory=dict)
    #: The raw body and its declared type, for a route that takes bytes rather than JSON.
    raw: bytes = b""
    content_type: str = ""

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
    #: A short summary for the generated contract.
    summary: str = ""
    #: The body this route accepts, as a JSON Schema object, and the reports it can answer
    #: with. Both live here rather than in the generator so the published contract and the
    #: handler that honours it are edited in the same place: a document assembled from a
    #: separate table is a second description of one thing, and the one that goes stale.
    request_schema: Mapping[str, Any] | None = None
    response_models: tuple[type[BaseModel], ...] = ()
    #: The media type family a route answers with when it answers with bytes rather than
    #: an envelope. Its handler returns an object carrying `media_type` and `data`; a
    #: refusal is still the JSON error envelope, so a client reads failures one way.
    binary: str | None = None
    #: The query parameters this route reads, as a JSON Schema object over string values.
    #: Validated like a body: a parameter the document does not publish is refused.
    query_schema: Mapping[str, Any] | None = None
    #: The media type family a route *accepts* bytes in, instead of a JSON body, and the
    #: most it reads. The cap is the route's, because a recording is not a form field.
    upload: str | None = None
    upload_limit: int = 0


def _start(request: Request) -> Any:
    return assessment_service.start(
        request.paths,
        track=request.optional("track", str),
        run_type=request.optional("run_type", str) or "pilot-calibration",
        dimensions=request.strings("dimensions") or None,
        modalities=request.strings("modalities") or None,
        scoring=request.optional("scoring", str) or DEFAULT_SCORING,
        idempotency_key=request.optional("idempotency_key", str),
        clock=request.clock,
        command="assessment.start",
        actor=ACTOR,
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
        actor=ACTOR,
    )


def _finalize(request: Request) -> Any:
    return assessment_service.finalize(
        request.paths,
        run=request.path_values["run_id"],
        reason=request.optional("reason", str) or "completed",
        exclude_outstanding=request.optional("exclude_outstanding", bool) is True,
        idempotency_key=request.optional("idempotency_key", str),
        clock=request.clock,
        command="assessment.finalize",
        actor=ACTOR,
    )


def _discover(request: Request) -> Any:
    status = request.query.get("status")
    return assessment_service.resumable_runs(
        request.paths,
        track=request.query.get("track"),
        statuses=None if status is None else status.split(","),
        clock=request.clock,
    )


def _recording(request: Request) -> Any:
    return assessment_service.served_recording(
        request.paths,
        run=request.path_values["run_id"],
        content_id=request.path_values["content_id"],
        clock=request.clock,
    )


def _play(request: Request) -> Any:
    return assessment_service.record_play(
        request.paths,
        run=request.path_values["run_id"],
        content_id=request.path_values["content_id"],
        idempotency_key=request.required("idempotency_key", str),
        clock=request.clock,
        command="assessment.play",
        actor=ACTOR,
    )


def _capture(request: Request) -> Any:
    return recording_service.capture(
        request.paths,
        run=request.path_values["run_id"],
        content_id=request.path_values["content_id"],
        capture_id=request.path_values["capture_id"],
        data=request.raw,
        media_type=request.content_type,
        clock=request.clock,
        actor=ACTOR,
        command="assessment.capture",
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
    Route(
        "POST",
        re.compile(r"^/runs$"),
        "assessment.start",
        _start,
        mutates=True,
        summary="Open a bounded calibration or placement run",
        request_schema=_body(
            {
                "track": {"type": "string", "minLength": 1},
                "run_type": {"enum": ["pilot-calibration", "placement"]},
                "dimensions": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "modalities": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "scoring": {"enum": list(SCORING_CONDITIONS)},
                "idempotency_key": IDEMPOTENCY_KEY,
            }
        ),
        response_models=(assessment_service.AssessmentRunReport,),
    ),
    Route(
        "GET",
        re.compile(r"^/runs$"),
        "assessment.discover",
        _discover,
        mutates=False,
        summary="The runs a page holding only its launch token can resume, newest first",
        query_schema=_body(
            {
                "track": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "pattern": "^(in-progress|paused)(,(in-progress|paused))*$",
                    "description": "Comma-separated; resumable statuses only.",
                },
            }
        ),
        response_models=(assessment_service.RunListReport,),
    ),
    Route(
        "GET",
        re.compile(rf"^/runs/{RUN_ID}$"),
        "assessment.report",
        _report,
        mutates=False,
        summary="A run's per-dimension estimates and budgets",
        response_models=(assessment_service.AssessmentRunReport,),
    ),
    Route(
        "GET",
        re.compile(rf"^/runs/{RUN_ID}/screen$"),
        "assessment.screen",
        _screen,
        mutates=False,
        summary="Everything a client needs to draw the run, in one read",
        response_models=(view_service.RunScreen,),
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/tasks$"),
        "assessment.next",
        _serve,
        mutates=True,
        summary="Serve the next task, or hand back the one already outstanding",
        request_schema=_body({"idempotency_key": IDEMPOTENCY_KEY}),
        # Two reports, because a serve that closes the last open dimension answers with the
        # run rather than with a task. A client that assumed a task would read a missing
        # `content_id` as a malformed answer.
        response_models=(
            assessment_service.NextTaskReport,
            assessment_service.AssessmentRunReport,
        ),
    ),
    Route(
        "GET",
        re.compile(rf"^/runs/{RUN_ID}/tasks/{CONTENT_ID}/audio$"),
        "assessment.recording",
        _recording,
        mutates=False,
        summary="The recording an outstanding task was served with, as those exact bytes",
        binary="audio/*",
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/tasks/{CONTENT_ID}/plays$"),
        "assessment.play",
        _play,
        mutates=True,
        summary="Record one play of an outstanding task's recording, before it is heard",
        request_schema=_body({"idempotency_key": IDEMPOTENCY_KEY}, required=["idempotency_key"]),
        response_models=(assessment_service.PlayReport,),
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/tasks/{CONTENT_ID}/captures/{CAPTURE_ID}$"),
        "assessment.capture",
        _capture,
        mutates=True,
        summary=("Submit the learner's recorded answer to an outstanding spoken task, for a judge"),
        response_models=(recording_service.CaptureReport,),
        upload="audio/*",
        upload_limit=recording_service.MAXIMUM_CAPTURE_BYTES,
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/results$"),
        "assessment.record",
        _record,
        mutates=True,
        summary="Score one served task and fold it into its dimension's posterior",
        request_schema=_body(
            {
                "content_id": {"type": "string", "minLength": 1},
                "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "response": {"type": "string", "minLength": 1},
                "response_excerpt": {"type": "string", "minLength": 1},
                "response_visibility": {"enum": ["withheld", "excerpt", "full"]},
                "rubric": {"type": "object"},
                "assessor_kind": {"enum": ["deterministic", "ai", "learner", "human"]},
                "assessor": {"type": "string", "minLength": 1},
                "confidence": {"enum": ["low", "medium", "high"]},
                "idempotency_key": IDEMPOTENCY_KEY,
            },
            required=["content_id"],
        ),
        response_models=(assessment_service.AssessmentRunReport,),
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/status$"),
        "assessment.status",
        _set_status,
        mutates=True,
        summary="Pause, resume, or abandon a run",
        request_schema=_body(
            {"status": {"enum": ["in-progress", "paused", "abandoned"]}}, required=["status"]
        ),
        response_models=(assessment_service.AssessmentRunReport,),
    ),
    Route(
        "POST",
        re.compile(rf"^/runs/{RUN_ID}/finalization$"),
        "assessment.finalize",
        _finalize,
        mutates=True,
        summary="Close a run and write one estimate per tested dimension",
        request_schema=_body(
            {
                "reason": {"type": "string", "minLength": 1},
                "exclude_outstanding": {"type": "boolean"},
                "idempotency_key": IDEMPOTENCY_KEY,
            }
        ),
        response_models=(assessment_service.AssessmentRunReport,),
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
