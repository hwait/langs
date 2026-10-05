"""Assemble the client's OpenAPI 3.1 document from the contracts that already exist.

OpenAPI 3.1 adopts JSON Schema 2020-12 as its schema dialect, which is exactly what
`schema_snapshots` emits, so the committed snapshots are reusable without dialect
conversion. Reuse is not verbatim embedding, though: most of them carry `#/$defs/...`
pointers, and `#` is the *document* root. Dropped unchanged under `components/schemas`,
those pointers resolve against the OpenAPI document and fail.

So each snapshot is **hoisted and rewritten**: its `$defs` lift into `components/schemas`
under a namespaced name and every pointer into them is rewritten to match. The alternative
-- giving each component its own `$id` so its pointers resolve within it -- is legal and
less code, and depends on consumers honouring `$id` inside `components/schemas`, which
tooling does unevenly. ADR 0008 records the choice.

The document describes **shapes, not sequences**. At most one outstanding task per
dimension, idempotent serving, scoring against the serve-time snapshot: none of that is
expressible here, and conformance to this document is not correctness.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from linguawiki import __version__
from linguawiki.client import responses as client_responses
from linguawiki.client import routes as client_routes
from linguawiki.client import shell as client_shell
from linguawiki.client.security import TOKEN_HEADER

#: Where the generated document is committed. A subdirectory of `schemas/`, not beside the
#: snapshots: the contract tests glob `schemas/*.json` and hold every date-time node in them
#: to a rule written for JSON Schema snapshots, which would judge an OpenAPI document by it.
#: It still rides the existing `schemas` force-include into the wheel.
DOCUMENT_RELATIVE_PATH = Path("openapi") / "linguawiki.client.v1.json"

#: The prefix every embedded pointer must end up with. `#` is the document root, which is
#: what makes the unrewritten form wrong rather than merely unusual.
COMPONENT_PREFIX = "#/components/schemas/"

#: How the security scheme is named in the document.
TOKEN_SCHEME = "launchToken"


class ComponentCollision(Exception):
    """Two different schemas claimed one component name.

    Raised rather than resolved: keeping the second would silently make one schema describe
    the other's type, and keeping the first would do the same in reverse. A collision is a
    bug in the namespacing, and the namespacing is ours to fix.
    """


def _rewrite(node: Any, *, prefix: str) -> Any:
    """Rewrite every `#/$defs/X` pointer in a schema to `#/components/schemas/<prefix>X`."""

    if isinstance(node, dict):
        rewritten = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                rewritten[key] = f"{COMPONENT_PREFIX}{prefix}{value[len('#/$defs/') :]}"
            else:
                rewritten[key] = _rewrite(value, prefix=prefix)
        return rewritten
    if isinstance(node, list):
        return [_rewrite(value, prefix=prefix) for value in node]
    return node


def relocate(name: str, schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """One snapshot as components: its root under `name`, its `$defs` under `name.<def>`.

    The namespace is the snapshot's own name, so two snapshots that both define a
    `ContentReview` keep two components rather than one of them quietly describing the
    other's rows.
    """

    working = deepcopy(schema)
    definitions = working.pop("$defs", {})
    prefix = f"{name}."
    components = {name: _rewrite(working, prefix=prefix)}
    for key, value in definitions.items():
        components[f"{prefix}{key}"] = _rewrite(value, prefix=prefix)
    return components


def model_components(model: type[BaseModel]) -> dict[str, dict[str, Any]]:
    """A pydantic model as components, with its nested models hoisted alongside it.

    `ref_template` points pydantic's own references straight at `components/schemas`, so no
    `#/$defs/` pointer is produced here in the first place -- but the `$defs` *container* is
    still emitted, and left in place it would be a block of schemas nothing could reach.
    """

    schema = model.model_json_schema(
        mode="serialization", ref_template=f"{COMPONENT_PREFIX}{{model}}"
    )
    definitions = schema.pop("$defs", {})
    components = {model.__name__: schema}
    components.update(definitions)
    return components


def merge_components(into: dict[str, dict[str, Any]], additions: dict[str, dict[str, Any]]) -> None:
    """Add components, refusing a name that already means something else.

    Identical repeats are fine and expected: two routes answering with the same report
    contribute the same component twice.
    """

    for name, schema in additions.items():
        existing = into.get(name)
        if existing is not None and existing != schema:
            raise ComponentCollision(f"two different schemas claim the component {name}")
        into[name] = schema


def _error_response(description: str, *, retry_after: bool = False) -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": f"{COMPONENT_PREFIX}linguawiki.cli.error.v1"}}
        },
    }
    if retry_after:
        response["headers"] = {
            "Retry-After": {
                "description": "Seconds to wait before retrying; the database is held.",
                "schema": {"type": "integer", "minimum": 1},
            }
        }
    return response


#: Declared on every operation, because every operation can meet them. The retryable pair is
#: the honest one: another LinguaWiki process holding the database is normal rather than
#: exceptional, and a client that has not planned for 503 has not planned for Tuesday.
COMMON_RESPONSES: dict[str, dict[str, Any]] = {
    "400": _error_response("The request was not understood."),
    "403": _error_response("Refused by the host, origin, or launch-token check."),
    "404": _error_response("No such route, or no such run."),
    "409": _error_response("An idempotency key was reused for a different request."),
    "413": _error_response("The request body is over the cap."),
    "422": _error_response("Understood, and refused by a rule of this workspace."),
    "500": _error_response("An unexpected internal error."),
    "503": _error_response("The database is held by another process.", retry_after=True),
}


def _success_response(models: tuple[type[BaseModel], ...]) -> dict[str, Any]:
    """A 200 whose envelope `data` is the report, or one of the reports, this route answers."""

    references: list[dict[str, Any]] = [
        {"$ref": f"{COMPONENT_PREFIX}{model.__name__}"} for model in models
    ]
    data: dict[str, Any] = references[0] if len(references) == 1 else {"oneOf": references}
    return {
        "description": "The operation's report, inside the shared success envelope.",
        "content": {
            "application/json": {
                "schema": {
                    "allOf": [{"$ref": f"{COMPONENT_PREFIX}linguawiki.cli.success.v1"}],
                    "properties": {"data": data},
                }
            }
        },
    }


def _path_template(pattern: str) -> str:
    """Turn the router's regex into the path template OpenAPI publishes.

    One source for both, so a route the server serves and a path the document declares
    cannot drift apart.
    """

    template = pattern.removeprefix("^").removesuffix("$")
    return (
        template.replace(client_routes.RUN_ID, "{run_id}")
        .replace(client_routes.CONTENT_ID, "{content_id}")
        .replace(client_routes.CAPTURE_ID, "{capture_id}")
    )


#: Declared on every mutating operation, because every one of them refuses its absence. In
#: prose only, a generated client sends no `Origin` at all and cannot make a single mutating
#: call -- a description is not a parameter.
ORIGIN_PARAMETER: dict[str, Any] = {
    "name": "Origin",
    "in": "header",
    "required": True,
    "description": (
        "Must be this server's own origin. Absence is refused rather than treated as "
        "permission: a cross-site form post sends no Origin in some browsers."
    ),
    "schema": {"type": "string", "pattern": "^http://(127\\.0\\.0\\.1|localhost):[0-9]{1,5}$"},
}


RUN_ID_PARAMETER: dict[str, Any] = {
    "name": "run_id",
    "in": "path",
    "required": True,
    "description": "The run this operation acts on.",
    "schema": {"type": "string", "pattern": "^asm_[0-9A-HJKMNP-TV-Z]{26}$"},
}


CONTENT_ID_PARAMETER: dict[str, Any] = {
    "name": "content_id",
    "in": "path",
    "required": True,
    "description": "A task this run served, by its content identifier.",
    "schema": {"type": "string", "pattern": "^cnt_[0-9A-HJKMNP-TV-Z]{26}$"},
}


CAPTURE_ID_PARAMETER: dict[str, Any] = {
    "name": "capture_id",
    "in": "path",
    "required": True,
    "description": (
        "The client's own identifier for one recording, minted when the learner stopped "
        "recording. It is the upload's idempotency key: the same identifier with the same "
        "bytes replays the first upload's result, and with different bytes is refused."
    ),
    "schema": {
        "type": "string",
        "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    },
}


def _binary_response(media: str) -> dict[str, Any]:
    return {
        "description": (
            "The exact bytes the snapshot names. A refusal is the JSON error envelope, "
            "never a partial body."
        ),
        "content": {media: {"schema": {"type": "string", "contentMediaType": media}}},
    }


def document(schema_directory: Path) -> dict[str, Any]:
    """The whole document, assembled from the routes and the committed snapshots."""

    components: dict[str, dict[str, Any]] = {}
    # The envelopes come from the *committed* snapshots rather than from the models, because
    # ADR 0008 makes the CLI's error payload and the API's one schema -- not two that happen
    # to agree. Reading the file is what makes that true rather than intended.
    for name in ("linguawiki.cli.error.v1", "linguawiki.cli.success.v1"):
        snapshot = json.loads((schema_directory / f"{name}.json").read_text(encoding="utf-8"))
        merge_components(components, relocate(name, snapshot))
    for route in client_routes.ROUTES:
        for model in route.response_models:
            merge_components(components, model_components(model))

    paths: dict[str, Any] = {
        "/health": {
            "get": {
                "operationId": "health",
                "summary": "Whether the server is up, without touching the database",
                "responses": {
                    "200": {
                        "description": "The server and the schema version it was built for.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"{COMPONENT_PREFIX}linguawiki.cli.success.v1"}
                            }
                        },
                    },
                    **{status: COMMON_RESPONSES[status] for status in ("403", "500")},
                },
            }
        }
    }
    for target, (_name, media) in client_shell.SHELL_FILES.items():
        # The only operations with no security requirement, and declared as such: the first
        # navigation cannot carry the token, which arrives in a fragment the browser never
        # sends. `Host` is still enforced on them.
        paths[target] = {
            "get": {
                "operationId": "shell_" + (target.strip("/").replace(".", "_") or "index"),
                "summary": "The page shell, served from the installed package",
                "security": [],
                "responses": {
                    "200": {
                        "description": "A static file of the page shell.",
                        "content": {media.split(";")[0]: {"schema": {"type": "string"}}},
                    },
                    "403": COMMON_RESPONSES["403"],
                },
            }
        }
    for route in client_routes.ROUTES:
        template = _path_template(route.pattern.pattern)
        operation: dict[str, Any] = {
            "operationId": route.command.replace(".", "_"),
            "summary": route.summary,
            "responses": {
                "200": (
                    _binary_response(route.binary)
                    if route.binary is not None
                    else _success_response(route.response_models)
                ),
                **COMMON_RESPONSES,
            },
        }
        parameters: list[dict[str, Any]] = []
        if "{run_id}" in template:
            parameters.append(RUN_ID_PARAMETER)
        if "{content_id}" in template:
            parameters.append(CONTENT_ID_PARAMETER)
        if "{capture_id}" in template:
            parameters.append(CAPTURE_ID_PARAMETER)
        if route.query_schema is not None:
            parameters.extend(
                {"name": name, "in": "query", "required": False, "schema": dict(schema)}
                for name, schema in route.query_schema["properties"].items()
            )
        if route.mutates:
            parameters.append(ORIGIN_PARAMETER)
        if parameters:
            operation["parameters"] = parameters
        if route.upload is not None:
            operation["requestBody"] = {
                "required": True,
                "description": (
                    f"The recording's bytes, at most {route.upload_limit} of them, with a "
                    "Content-Length."
                ),
                "content": {
                    route.upload: {"schema": {"type": "string", "contentMediaType": route.upload}}
                },
            }
        if route.request_schema is not None:
            operation["requestBody"] = {
                "required": False,
                "content": {"application/json": {"schema": dict(route.request_schema)}},
            }
        if route.mutates:
            operation["responses"]["405"] = _error_response("Wrong method for this path.")
            operation["description"] = (
                "Mutating: an Origin header matching this server's own origin is required, "
                "and absence is refused rather than treated as permission."
            )
        paths.setdefault(template, {})[route.method.lower()] = operation

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "LinguaWiki learner client",
            "version": __version__,
            "summary": "A loopback transport over the LinguaWiki service layer.",
            "description": (
                "Generated from the Pydantic contracts and the server's own route table; "
                "never edited by hand. This document describes **shapes, not sequences**. "
                "At most one outstanding task per dimension, idempotent serving, scoring "
                "against the serve-time snapshot rather than the live pack, and every "
                "consent rule live in the service layer and its tests. Conformance to this "
                "document is not correctness.\\n\\n"
                "Every request carries the launch token this server minted when it started; "
                f"a {TOKEN_SCHEME} is valid for one server start and is never stored."
            ),
            "license": {"name": "MIT", "identifier": "MIT"},
        },
        "servers": [
            {
                "url": "http://127.0.0.1:{port}",
                "description": (
                    "Loopback only. The port is in data/client-runtime.json; the token is "
                    "not, and arrives in the fragment of the URL the launch command opens."
                ),
                "variables": {"port": {"default": "0"}},
            }
        ],
        "security": [{TOKEN_SCHEME: []}],
        "components": {
            "securitySchemes": {
                TOKEN_SCHEME: {
                    "type": "apiKey",
                    "in": "header",
                    "name": TOKEN_HEADER,
                    "description": (
                        "Minted per server start, handed to the page through the URL "
                        "fragment, and never written to disk."
                    ),
                }
            },
            "schemas": dict(sorted(components.items())),
        },
        "paths": dict(sorted(paths.items())),
        "x-linguawiki-retryable-status": 503,
        "x-linguawiki-forbidden-codes": sorted(client_responses.FORBIDDEN_CODES),
    }


def rendered(schema_directory: Path) -> str:
    """The document exactly as it is committed, so `--check` compares bytes."""

    return (
        json.dumps(document(schema_directory), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


__all__ = [
    "COMPONENT_PREFIX",
    "DOCUMENT_RELATIVE_PATH",
    "TOKEN_SCHEME",
    "ComponentCollision",
    "document",
    "merge_components",
    "model_components",
    "relocate",
    "rendered",
]
