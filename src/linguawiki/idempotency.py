"""Keys that identify one operation, and the hash that tells a retry from a reuse.

A key alone cannot tell them apart. Asking for a hundred minutes of grammar under the key
that planned sixty minutes of mixed work returned the old plan and told the caller nothing,
which is why `sessions` started binding its keys to a hash of the request. The same
reasoning applies to every keyed operation, so the mechanism lives here rather than inside
the first service that needed it.

Two rules, and both directions of each:

* **Same key, same request** is a retry. It replays the first call's result; it does not do
  the work again.
* **Same key, different request** is a conflict. Accepting it would discard the first
  call's observations silently, and returning the first call's result would answer a
  question nobody asked.

The second rule has a direction that is easy to miss: a key that acted on a *different
operation* is also a conflict. Handing back the result of the serve that key performed, to
a caller asking it to score something, confirms a belief that is wrong.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from linguawiki.db.connection import Database
from linguawiki.errors import ErrorDetail, LinguaWikiError

#: The payload field every keyed domain event carries. Stored inside the payload rather
#: than in a column of its own because `domain_events` cannot gain a constrained column and
#: the hash is meaningless apart from the event it describes.
REQUEST_HASH_FIELD = "request_hash"


def canonical_hash(payload: object) -> str:
    """Hash a payload by its canonical JSON form, not by the bytes it arrived in.

    Two flushes carrying the same observations are the same flush however the caller
    serialized them, and a package exported twice is one session. Content-addressing is
    what makes "retrying is safe" and "ingesting twice does nothing" the same mechanism.
    """

    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_hash(**parts: object) -> str:
    """The fingerprint of one request, from the parts that decide what it asks for.

    Pass *resolved* values, not the caller's spelling of them: `run=None` falling back to
    the active run and `run="asm_..."` naming it explicitly are the same request, and
    hashing the argument rather than the run it resolved to would make a legitimate retry
    look like a reuse.

    Never pass a learner's text. The hash is stored in `domain_events.payload_json`, which
    is never edited, so a payload written before the retention rule ran keeps whatever it
    kept for as long as the workspace exists. Pass a hash of the text instead.
    """

    return canonical_hash(parts)


def _stored_payload(raw: object) -> Mapping[str, Any]:
    """A recorded event payload, or an empty mapping when it cannot be read.

    `payload_json` carries `json_valid`, so this should not fail -- but a restore from
    another release, or a hand repair, can leave something that is valid JSON and not an
    object. An unreadable payload carries no request hash, which `resolve` then treats as a
    key it cannot vouch for rather than as a retry it can.
    """

    try:
        document = json.loads(str(raw))
    except (ValueError, RecursionError):
        return {}
    return document if isinstance(document, dict) else {}


def _conflict(message: str, *, reason: str, key: str) -> LinguaWikiError:
    return LinguaWikiError(
        "idempotency_conflict",
        message,
        details=(
            ErrorDetail(field="idempotency_key", reason=reason, context={"idempotency_key": key}),
        ),
    )


def resolve(
    database: Database, *, key: str | None, event_type: str, request_hash: str
) -> Mapping[str, Any] | None:
    """The payload this key already recorded, or `None` when the key is new.

    Call it **before** the transaction and compare the key before returning anything
    stored: a guard placed after the path it guards is not a guard. The read runs on the
    writer's own connection, after the lock is held, so it sees committed state and no
    second writer can race it -- DuckDB does not refuse a second writer in one process, but
    the application lock does.

    Raises `idempotency_conflict` when the key belongs to another operation, when it
    recorded a different request, or when what it recorded cannot be vouched for.
    """

    if key is None:
        return None
    row = database.one(
        "SELECT event_type, aggregate_id, payload_json FROM domain_events "
        "WHERE idempotency_key = ?",
        [key],
    )
    if row is None:
        return None
    stored_type, aggregate_id, raw_payload = str(row[0]), str(row[1]), row[2]
    if stored_type != event_type:
        raise _conflict(
            f"idempotency key {key} already performed {stored_type} on {aggregate_id}; "
            f"it cannot also perform {event_type}, and a different operation needs a new key",
            reason="key belongs to another operation",
            key=key,
        )
    payload = _stored_payload(raw_payload)
    stored_hash = payload.get(REQUEST_HASH_FIELD)
    if stored_hash is None:
        # A key recorded before request hashing existed, or one whose payload no longer
        # reads as an object. Sameness cannot be established, so it cannot be called a
        # retry -- and calling it one would replay a result for a request nobody compared.
        raise _conflict(
            f"idempotency key {key} already performed {stored_type} on {aggregate_id}, but "
            "what it was asked for was not recorded, so this call cannot be shown to be a "
            "retry of it; use a new key",
            reason="recorded request is unknown",
            key=key,
        )
    if str(stored_hash) != request_hash:
        raise _conflict(
            f"idempotency key {key} already performed {stored_type} on {aggregate_id} from a "
            "different request; a retry must ask for the same thing, and a different one "
            "needs a new key",
            reason="request differs from the one recorded",
            key=key,
        )
    return payload


def payload(request_hash: str, **parts: object) -> str:
    """A keyed event's stored payload: what it did, plus the request it did it for.

    One helper so the hash is never written under a second name, and never omitted -- an
    omitted hash is a key `resolve` can no longer vouch for.
    """

    return json.dumps({**parts, REQUEST_HASH_FIELD: request_hash}, sort_keys=True)


__all__ = ["REQUEST_HASH_FIELD", "canonical_hash", "payload", "request_hash", "resolve"]
