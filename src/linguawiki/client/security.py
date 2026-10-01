"""The three checks that run before any request reaches the service layer.

"Loopback, single-user" is not a threat model. A page on any website can make requests to
127.0.0.1, and DNS rebinding turns a name the browser trusts into an address the server
trusts -- which is why the `Host` header is checked against an allowlist rather than being
believed, and why its absence is a refusal rather than a default.

Each refusal keeps its own code. Collapsing them would take away a page's ability to tell
"reload with the token" from "you are not talking to the server you think you are", and
those send somebody to very different places.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping

from linguawiki.errors import ErrorDetail, LinguaWikiError

#: The header the launched page sends. Not a cookie: a cookie is attached by the browser to
#: requests the page did not make, which is the shape of the attack this is defending.
TOKEN_HEADER = "X-LinguaWiki-Token"

#: The only names that may address this server. A name that merely *resolves* to 127.0.0.1
#: is not on it -- that resolution is the attacker's to control.
ALLOWED_HOSTS = ("127.0.0.1", "localhost")

#: The headers whose *multiplicity* matters. Two `Host` headers is one request that two
#: readers would resolve differently, which is the shape of a smuggling attempt rather than a
#: quirk, so it is refused instead of one of them being picked.
SINGLE_VALUED = ("Host", "Origin", TOKEN_HEADER)


def _value(headers: Mapping[str, str], name: str) -> str | None:
    """One header, matched the way HTTP defines header names: without regard to case.

    The map arrives with its keys already folded, and this is the only place that fact is
    relied on -- so every check below asks for the canonical spelling and gets the right
    answer whatever the client sent. Reading a case-sensitive dictionary refused a perfectly
    ordinary lowercase `host` as *absent*, which is the most misleading answer available: it
    says the client sent nothing where the client sent the right thing.
    """

    return headers.get(name.lower())


def assert_single_valued(duplicated: Mapping[str, int]) -> None:
    """Refuse a request that sent any decisive header more than once."""

    repeated = sorted(name for name, count in duplicated.items() if count > 1)
    if repeated:
        raise _refuse(
            "client_header_repeated",
            f"{', '.join(repeated)} was sent more than once, so this request does not have "
            "one meaning; send each exactly once",
            field="headers",
            reason="repeated",
        )


def _refuse(code: str, message: str, *, field: str, reason: str) -> LinguaWikiError:
    return LinguaWikiError(code, message, details=(ErrorDetail(field=field, reason=reason),))


def assert_host(headers: Mapping[str, str], *, port: int) -> None:
    """Require the request to have been addressed to this server by an allowed name."""

    value = _value(headers, "Host")
    if value is None or not value.strip():
        # HTTP/1.0 permits no `Host` at all, and defaulting it to the bind address is
        # precisely how an allowlist gets bypassed: the check would always pass.
        raise _refuse(
            "client_host_denied",
            "this request carries no Host header, so it cannot be shown to have been "
            "addressed to the local client server",
            field="host",
            reason="absent",
        )
    if value.strip() not in {f"{name}:{port}" for name in ALLOWED_HOSTS}:
        raise _refuse(
            "client_host_denied",
            f"{value} is not a name this client server answers to; a page reaching it "
            "under another name is not the page the launch command opened",
            field="host",
            reason="not allowlisted",
        )


def assert_origin(headers: Mapping[str, str], *, origin: str) -> None:
    """Require a mutating request to come from this server's own page.

    Absence is a refusal. A cross-site form post sends no `Origin` in some browsers, so
    treating absence as permission is the attack rather than an edge case -- absent is the
    case where the data is least trustworthy.
    """

    value = _value(headers, "Origin")
    if value is None or not value.strip():
        raise _refuse(
            "client_origin_denied",
            "a request that changes a learner's record must say where it came from",
            field="origin",
            reason="absent",
        )
    if value.strip() != origin:
        raise _refuse(
            "client_origin_denied",
            f"{value} is not this client server's own origin, so this request did not come "
            "from the page it opened",
            field="origin",
            reason="cross-site",
        )


def assert_token(headers: Mapping[str, str], *, expected: str) -> None:
    """Require the token this server minted when it started.

    Compared with `compare_digest`, so the comparison takes the same time whatever the
    caller sent. A restart invalidates every page holding the old one, which is correct and
    is what the refusal says.
    """

    value = _value(headers, TOKEN_HEADER)
    if value is None or not value.strip():
        raise _refuse(
            "client_token_required",
            f"this request carries no {TOKEN_HEADER}; open the URL the launch command "
            "printed, which carries the token for this server start",
            field="token",
            reason="absent",
        )
    if not secrets.compare_digest(value.strip(), expected):
        raise _refuse(
            "client_token_invalid",
            "this token is not the one this server minted when it started; if the server "
            "has been restarted, relaunch to get the current URL",
            field="token",
            reason="mismatch",
        )


__all__ = ["ALLOWED_HOSTS", "TOKEN_HEADER", "assert_host", "assert_origin", "assert_token"]
