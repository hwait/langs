"""Regressions for the Stage 2 review findings that need no database.

Each test names the mistake it prevents rather than the code it calls, because the point
of a regression is to fail if the reasoning behind the fix is ever undone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from linguawiki.contracts import PackManifest
from linguawiki.errors import LinguaWikiError
from linguawiki.placement import (
    MINIMUM_FAMILIES,
    PRECISION_WIDTH,
    Candidate,
    at_framework_edge,
    credible_interval,
    initial_state,
    record_score,
    stop_decision,
)
from linguawiki.services.resources import resolve_bundles

PILOT_MANIFEST = Path("language-packs/pl-pilot/manifest.json")
CEFR = ("A1", "A2", "B1", "B2", "C1", "C2")


def _manifest_with_bundles(bundles: list[dict[str, object]]) -> dict[str, object]:
    document = json.loads(PILOT_MANIFEST.read_text(encoding="utf-8"))
    document["bundles"] = bundles
    return document


def test_a_bundle_cycle_is_refused_by_the_manifest_contract() -> None:
    """Rejecting only self-dependency let `a -> b -> a` through, and it hung the resolver."""

    document = _manifest_with_bundles(
        [
            {
                "bundle_key": "pl-a2-alpha",
                "file": "resource-bundles/a.json",
                "depends_on": ["pl-a2-beta"],
            },
            {
                "bundle_key": "pl-a2-beta",
                "file": "resource-bundles/b.json",
                "depends_on": ["pl-a2-alpha"],
            },
        ]
    )

    with pytest.raises(ValidationError) as failure:
        PackManifest.model_validate(document)

    assert "acyclic" in str(failure.value)
    assert "pl-a2-alpha -> pl-a2-beta -> pl-a2-alpha" in str(failure.value)


def test_a_three_bundle_cycle_is_refused_too() -> None:
    document = _manifest_with_bundles(
        [
            {"bundle_key": "pl-one", "file": "resource-bundles/1.json", "depends_on": ["pl-two"]},
            {"bundle_key": "pl-two", "file": "resource-bundles/2.json", "depends_on": ["pl-three"]},
            {"bundle_key": "pl-three", "file": "resource-bundles/3.json", "depends_on": ["pl-one"]},
        ]
    )

    with pytest.raises(ValidationError) as failure:
        PackManifest.model_validate(document)

    assert "acyclic" in str(failure.value)


def test_a_bundle_chain_that_is_a_dag_is_accepted() -> None:
    """The guard rejects cycles, not depth: a diamond is a legitimate declaration."""

    document = _manifest_with_bundles(
        [
            {"bundle_key": "pl-base", "file": "resource-bundles/base.json", "depends_on": []},
            {
                "bundle_key": "pl-left",
                "file": "resource-bundles/left.json",
                "depends_on": ["pl-base"],
            },
            {
                "bundle_key": "pl-right",
                "file": "resource-bundles/right.json",
                "depends_on": ["pl-base"],
            },
            {
                "bundle_key": "pl-top",
                "file": "resource-bundles/top.json",
                "depends_on": ["pl-left", "pl-right"],
            },
        ]
    )

    manifest = PackManifest.model_validate(document)

    assert [bundle.bundle_key for bundle in manifest.bundles] == [
        "pl-base",
        "pl-left",
        "pl-right",
        "pl-top",
    ]


def test_the_resolver_refuses_a_cycle_rather_than_looping_for_ever() -> None:
    """A defence that does not depend on manifest validation having run first."""

    with pytest.raises(LinguaWikiError) as failure:
        resolve_bundles({"a": ("A2", ("b",)), "b": ("A2", ("a",))}, level_codes=["A2"])

    assert failure.value.payload.code == "bundle_dependency_cycle"


def test_the_resolver_still_deepens_a_legitimate_chain() -> None:
    bundles = {
        "top": ("A2", ("middle",)),
        "middle": ("B1", ("base",)),
        "base": ("B1", ()),
    }

    order = resolve_bundles(bundles, level_codes=["A2"])

    # Deepest prerequisite first, so preparation can be applied in the order returned.
    assert [key for key, _ in order] == ["base", "middle", "top"]


def _reading_state(declared: float | None = 1.0):
    return initial_state(
        dimension="reading",
        dimension_kind="receptive",
        level_count=len(CEFR),
        declared_index=declared,
    )


def _task(
    identifier: str, *, difficulty: float, family: str = "travel", level_code: str | None = None
) -> Candidate:
    return Candidate(
        content_id=identifier,
        dimension="reading",
        task_type="objective",
        difficulty=difficulty,
        content_family=family,
        modality="text",
        level_code=level_code or CEFR[min(len(CEFR) - 1, round(difficulty))],
        is_anchor=False,
    )


def test_an_edge_labelled_task_does_not_excuse_the_boundary_probe() -> None:
    """The exemption belongs to the estimate, not to the last task's level label.

    Reading it off the candidate let a run whose estimate sits mid-framework skip the
    probe entirely as soon as one A1-labelled task happened to be served, which is
    exactly where a probe carries the most information.
    """

    state = _reading_state()
    for index in range(state.minimum_tasks):
        # Every task carries the framework's lowest label while sitting at the top of that
        # band on the half-band grid, and every task sits on the estimate, so no probe
        # above or below has been made. Coverage and precision are both satisfied.
        state = record_score(
            state,
            _task(f"cnt_{index}", difficulty=1.0, family=f"family-{index % 3}", level_code="A1"),
            score=0.5,
            level_count=len(CEFR),
        )

    assert not at_framework_edge(state, level_count=len(CEFR))
    assert not state.boundary_probed
    assert state.coverage >= MINIMUM_FAMILIES
    low, high = credible_interval(state.grid, state.posterior)
    assert high - low <= PRECISION_WIDTH
    # Three of the four conditions hold. Only the missing boundary probe keeps it open,
    # and reading the exemption off an A1-labelled task used to hand it over anyway.
    assert state.status == "open"
    assert stop_decision(state, level_count=len(CEFR)) == (False, None)


def test_an_estimate_in_the_lowest_band_is_at_the_edge() -> None:
    state = _reading_state(declared=0.0)
    for index in range(state.minimum_tasks):
        state = record_score(
            state,
            _task(f"cnt_{index}", difficulty=2.0, family=f"family-{index % 3}"),
            score=0.0,
            level_count=len(CEFR),
        )

    assert at_framework_edge(state, level_count=len(CEFR))


def test_a_dimension_with_no_evidence_is_not_at_the_edge() -> None:
    """A prior is not an estimate, so it earns no exemption from anything."""

    assert not at_framework_edge(_reading_state(declared=0.0), level_count=len(CEFR))
