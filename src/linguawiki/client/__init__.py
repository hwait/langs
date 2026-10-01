"""The local learner client: a loopback transport over the service layer.

Everything in here is routing, retry, and the refusals that keep a loopback port from being
a hole somebody else's page can reach through. No scoring, no selection, no stop rule, no
posterior update -- ADR 0003 puts those in one place and ADR 0008 keeps them there.

The package ships in the wheel with everything else. ADR 0008 originally put it behind a
`linguawiki[client]` extra; with a stdlib-only server that extra would carry no
dependencies, which makes it a boundary nothing enforces -- documentation shaped like a
constraint. The stdlib-only decision is expressed by the absence of a seventh runtime
dependency instead.
"""

from __future__ import annotations

from linguawiki.client.server import ClientServer, build_server, serve

__all__ = ["ClientServer", "build_server", "serve"]
