"""How a refusal becomes a status code, and how either becomes a body.

The body is always a `linguawiki.cli.*` envelope -- the same `ErrorPayload` the CLI prints,
not a parallel one. ADR 0008 makes that explicit: one error contract for both entry points,
so a skill and a page act on the same `code`.

The status is chosen by the *class* of refusal rather than from a table of codes. A per-code
table is a second list of every refusal in the system, and the one that rots: a code added
next year would quietly become a 422 nobody chose. The classes below are small, stated, and
cover every code by construction.
"""

from __future__ import annotations

from typing import Any

from linguawiki.clock import Clock
from linguawiki.contracts import ErrorEnvelope, GenericSuccessEnvelope
from linguawiki.errors import LinguaWikiError
from linguawiki.ids import EventId

#: A refusal about the shape of the request, which the caller can fix by asking differently.
BAD_REQUEST_CODES = frozenset({"invalid_arguments", "invalid_contract", "invalid_path"})

#: Where the caller is refused permission rather than correctness. These are this server's
#: own refusals, raised before the service layer is reached.
FORBIDDEN_CODES = frozenset(
    {
        "client_header_repeated",
        "client_host_denied",
        "client_origin_denied",
        "client_token_required",
        "client_token_invalid",
    }
)

#: How long a page should wait before retrying a busy database, in seconds. One: the holder
#: is another short command, and a longer hint would make the interface feel broken.
RETRY_AFTER_SECONDS = 1


def status_for(error: LinguaWikiError) -> int:
    """The HTTP status for one refusal, from its class.

    Order matters: `retryable` comes first because it is a statement about *when* the caller
    should try again, which outranks what the refusal was about.
    """

    code = error.payload.code
    if error.payload.retryable:
        return 503
    if code in FORBIDDEN_CODES:
        return 403
    if code in BAD_REQUEST_CODES:
        return 400
    if code == "client_body_too_large":
        return 413
    if code == "internal_error":
        return 500
    if code.endswith("_conflict"):
        return 409
    if code.endswith("_not_found"):
        return 404
    # Everything else is a well-formed request this workspace refuses on its own terms: a
    # closed run, a damaged snapshot, a claim consent does not allow. 422 says the request
    # was understood and still cannot be carried out, which is exactly that.
    return 422


def success(command: str, data: Any, clock: Clock, warnings: tuple[str, ...] = ()) -> bytes:
    return (
        GenericSuccessEnvelope(
            command=command,
            correlation_id=EventId.new(),
            generated_at=clock.now(),
            warnings=warnings,
            data=data,
        )
        .model_dump_json()
        .encode("utf-8")
    )


def failure(command: str, error: LinguaWikiError, clock: Clock) -> bytes:
    return (
        ErrorEnvelope(
            command=command,
            correlation_id=EventId.new(),
            generated_at=clock.now(),
            error=error.payload,
        )
        .model_dump_json()
        .encode("utf-8")
    )


__all__ = [
    "BAD_REQUEST_CODES",
    "FORBIDDEN_CODES",
    "RETRY_AFTER_SECONDS",
    "failure",
    "status_for",
    "success",
]
