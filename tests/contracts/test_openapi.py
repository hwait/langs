"""The generated contract: current, self-contained, and resolvable through itself.

Three properties, and the third is the one the hand-written alternative always loses.
Reusing the committed snapshots as components is only safe if their internal `#/$defs/`
pointers are relocated: `#` is the *document* root, so an unrewritten pointer resolves
against the OpenAPI document and fails -- quietly, in whichever consumer followed it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from linguawiki.client import routes as client_routes
from linguawiki.contracts import SCHEMA_MODELS
from linguawiki.openapi import (
    COMPONENT_PREFIX,
    DOCUMENT_RELATIVE_PATH,
    ComponentCollision,
    document,
    merge_components,
    relocate,
)
from linguawiki.schema_snapshots import hardened_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
DOCUMENT = SCHEMAS / DOCUMENT_RELATIVE_PATH


def read_document() -> dict[str, Any]:
    return dict(json.loads(DOCUMENT.read_text(encoding="utf-8")))


def refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        for key, value in node.items():
            if key != "$ref":
                found.extend(refs(value))
        return found
    if isinstance(node, list):
        return [reference for value in node for reference in refs(value)]
    return []


def test_the_committed_document_is_current() -> None:
    """Generated, not maintained: a stale document fails the gate."""

    result = subprocess.run(
        [sys.executable, "scripts/generate_openapi.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout


def test_the_document_declares_openapi_three_one() -> None:
    """3.1 is why the snapshots are reusable: it adopts JSON Schema 2020-12 as its dialect."""

    assert read_document()["openapi"].startswith("3.1")


def test_no_reference_in_the_document_resolves_against_the_document_root() -> None:
    """The property the "18 of 21 snapshots" count was standing in for.

    Counted here rather than quoted: an earlier draft of the plan said 17 and was stale
    within one stage.
    """

    document_text = json.dumps(read_document())

    assert not re.search(r'"#/\$defs/', document_text)


def test_every_reference_resolves_through_the_assembled_document() -> None:
    whole = read_document()
    components = whole["components"]["schemas"]

    for reference in set(refs(whole)):
        assert reference.startswith(COMPONENT_PREFIX), reference
        assert reference[len(COMPONENT_PREFIX) :] in components, reference


def test_a_nested_reference_resolves_through_the_assembled_document() -> None:
    """A component whose own schema points at another component, followed for real.

    A flat check passes on a document whose top-level components resolve and whose nested
    ones do not, which is exactly the failure relocation exists to prevent.
    """

    whole = read_document()
    components = whole["components"]["schemas"]
    nested = {
        name: reference
        for name, schema in components.items()
        for reference in refs(schema)
        if reference.startswith(COMPONENT_PREFIX)
    }

    assert nested, "no component references another, so this asserts nothing"
    for name, reference in nested.items():
        target = reference[len(COMPONENT_PREFIX) :]
        assert target in components, f"{name} points at the missing {target}"
    Draft202012Validator.check_schema(
        {"$defs": components, **components["linguawiki.cli.error.v1"]}
    )


def test_the_error_body_is_the_cli_s_own_schema_and_not_a_copy() -> None:
    """ADR 0008: one error contract for both entry points."""

    whole = read_document()
    committed = json.loads((SCHEMAS / "linguawiki.cli.error.v1.json").read_text("utf-8"))
    embedded = whole["components"]["schemas"]["linguawiki.cli.error.v1"]

    assert embedded["properties"].keys() == committed["properties"].keys()
    error_responses = {
        reference
        for path in whole["paths"].values()
        for operation in path.values()
        for status, response in operation["responses"].items()
        if status != "200"
        for reference in refs(response)
    }
    assert error_responses == {f"{COMPONENT_PREFIX}linguawiki.cli.error.v1"}


def test_every_route_the_server_serves_is_declared() -> None:
    """One source for the paths, so the router and the document cannot drift."""

    whole = read_document()

    for route in client_routes.ROUTES:
        template = (
            route.pattern.pattern.removeprefix("^")
            .removesuffix("$")
            .replace(client_routes.RUN_ID, "{run_id}")
            .replace(client_routes.CONTENT_ID, "{content_id}")
            .replace(client_routes.CAPTURE_ID, "{capture_id}")
        )
        assert template in whole["paths"], template
        assert route.method.lower() in whole["paths"][template], template


def test_every_operation_that_reaches_the_database_declares_the_retryable_response() -> None:
    """A client that has not planned for 503 has not planned for Tuesday.

    `/health` is excluded, and exactly because it is excluded from the hazard: it touches no
    database, which is what makes it the route a page uses to tell "up" from "busy". Declaring
    a 503 it cannot return would make that distinction useless.
    """

    whole = read_document()

    for template, path in whole["paths"].items():
        for method, operation in path.items():
            if template == "/health" or operation.get("security") == []:
                assert "503" not in operation["responses"]
                continue
            assert "503" in operation["responses"], f"{method} {template}"
    busy = whole["paths"]["/runs"]["post"]["responses"]["503"]
    assert "Retry-After" in busy["headers"]


#: The one mutating route that takes no idempotency key, and why it does not need one. A
#: status transition is idempotent by *state*: `RUN_TRANSITIONS` permits `paused -> paused`,
#: so repeating the request reaches the state it asked for rather than doing something twice.
#: Every other mutation accumulates -- a second serve spends an exposure, a second score
#: moves a posterior -- and must be keyed.
IDEMPOTENT_BY_STATE = "/runs/{run_id}/status"


def test_every_accumulating_mutation_declares_the_idempotency_key_and_its_conflict() -> None:
    keyed = []
    whole = read_document()

    for route in client_routes.ROUTES:
        if not route.mutates:
            continue
        template = (
            route.pattern.pattern.removeprefix("^")
            .removesuffix("$")
            .replace(client_routes.RUN_ID, "{run_id}")
            .replace(client_routes.CONTENT_ID, "{content_id}")
            .replace(client_routes.CAPTURE_ID, "{capture_id}")
        )
        operation = whole["paths"][template][route.method.lower()]
        if route.upload is not None:
            # An upload's body is bytes, so its key travels in the path, where a retry
            # cannot drop it -- and the document has to say that it is the key.
            keyed.append(template)
            (named,) = [
                parameter
                for parameter in operation["parameters"]
                if parameter["name"] == "capture_id"
            ]
            assert named["in"] == "path" and "idempotency key" in named["description"]
            assert "409" in operation["responses"], template
            continue
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        if template == IDEMPOTENT_BY_STATE:
            assert "idempotency_key" not in body["properties"]
            continue
        keyed.append(template)
        # The key is `idempotency_key` beside the request, or -- for a written answer -- the
        # producer's own `submission_key`; either way the document says it replays.
        assert route.key_field in body["properties"], template
        assert "replays" in body["properties"][route.key_field]["description"], template
        assert "409" in operation["responses"], template

    # Named rather than counted, so a new mutating route added without a key fails here
    # instead of joining a tally nobody reads.
    assert sorted(keyed) == [
        "/runs",
        "/runs/{run_id}/finalization",
        "/runs/{run_id}/results",
        "/runs/{run_id}/tasks",
        "/runs/{run_id}/tasks/{content_id}/captures/{capture_id}",
        "/runs/{run_id}/tasks/{content_id}/plays",
        "/runs/{run_id}/tasks/{content_id}/submission",
    ]


def test_the_token_is_required_by_the_document_not_only_by_the_server() -> None:
    whole = read_document()

    scheme = whole["components"]["securitySchemes"]
    assert whole["security"] == [{"launchToken": []}]
    assert scheme["launchToken"]["in"] == "header"
    assert scheme["launchToken"]["name"] == "X-LinguaWiki-Token"
    # Exactly the shell opts out, and nothing else does.
    public = sorted(
        template
        for template, path in whole["paths"].items()
        for operation in path.values()
        if operation.get("security") == []
    )
    assert public == ["/", "/app.css", "/app.js"]


def test_the_document_says_it_describes_shapes_rather_than_sequences() -> None:
    """Conformance is not correctness, and the document has to say so where it is read."""

    description = read_document()["info"]["description"]

    assert "shapes, not sequences" in description
    assert "Conformance to this document is not correctness" in description


# --- the relocation itself, over every snapshot rather than the ones the API uses --------


@pytest.mark.parametrize("name", sorted(SCHEMA_MODELS))
def test_relocating_any_snapshot_leaves_no_pointer_to_the_document_root(name: str) -> None:
    """Asserted over the whole set, so the machinery is not only right where it is used."""

    snapshot = hardened_schema(name, SCHEMA_MODELS[name].model_json_schema(mode="validation"))

    components = relocate(name, snapshot)

    assert name in components
    for component in components.values():
        for reference in refs(component):
            assert reference.startswith(COMPONENT_PREFIX), reference
            assert reference[len(COMPONENT_PREFIX) :] in components, reference


def test_two_snapshots_defining_one_name_keep_two_components() -> None:
    """The namespace is the point: `$defs.Thing` from two files is two types."""

    first = relocate("a.v1", {"$defs": {"Thing": {"type": "string"}}, "type": "object"})
    second = relocate("b.v1", {"$defs": {"Thing": {"type": "integer"}}, "type": "object"})

    assert set(first) == {"a.v1", "a.v1.Thing"}
    assert set(second) == {"b.v1", "b.v1.Thing"}


def test_a_component_collision_is_raised_rather_than_resolved() -> None:
    """Keeping either one would make one schema describe the other's type."""

    components: dict[str, Any] = {}
    merge_components(components, {"Thing": {"type": "string"}})

    # The same name, the same schema: a report two routes both answer with.
    merge_components(components, {"Thing": {"type": "string"}})
    with pytest.raises(ComponentCollision):
        merge_components(components, {"Thing": {"type": "integer"}})


def test_the_whole_document_assembles_without_a_collision() -> None:
    """The namespacing has to hold over the real set, not only over a constructed pair."""

    assert document(SCHEMAS)["components"]["schemas"]


def test_every_mutating_operation_declares_the_required_origin_header() -> None:
    """Prose in a description is not a parameter a generated client will send.

    Every mutation refuses an absent `Origin`, so a client built from this document without
    one is a client that cannot make a single mutating call.
    """

    whole = read_document()

    for template, path in whole["paths"].items():
        for method, operation in path.items():
            if method == "get":
                continue
            declared = [
                parameter
                for parameter in operation.get("parameters", [])
                if parameter.get("name") == "Origin" and parameter.get("in") == "header"
            ]
            assert declared, f"{method} {template} does not declare Origin"
            assert declared[0]["required"] is True, f"{method} {template}"


def test_a_binary_route_publishes_its_media_type_rather_than_an_envelope() -> None:
    whole = read_document()

    operation = whole["paths"]["/runs/{run_id}/tasks/{content_id}/audio"]["get"]

    assert set(operation["responses"]["200"]["content"]) == {"audio/*"}
    assert [parameter["name"] for parameter in operation["parameters"]] == [
        "run_id",
        "content_id",
    ]
