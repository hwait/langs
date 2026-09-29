"""The two synthetic fixture packs must validate through one generic contract.

They exist to prove the core is language-agnostic: same dimensions, same framework, same
contract, contrasting linguistic structure. One is inflected and whitespace-delimited, the
other tonal and non-whitespace, and neither contains material from a natural language.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linguawiki.packs.coverage import highest_supported_maturity, supported_onboarding_modes
from linguawiki.packs.format import load_pack, resolved_content_item
from linguawiki.packs.stamp import stamp_pack

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "language-packs" / "fixtures"
NAMES = ("inflected", "tonal")


def pack(name: str) -> object:
    return load_pack(FIXTURES / name)


def read_json(name: str, relative: str) -> dict[str, object]:
    return json.loads((FIXTURES / name / relative).read_text(encoding="utf-8"))


def test_contrasting_packs_share_dimensions_and_differ_in_structure() -> None:
    inflected = pack("inflected")
    tonal = pack("tonal")

    assert inflected.manifest.dimensions == tonal.manifest.dimensions
    assert inflected.manifest.dimension_kinds == tonal.manifest.dimension_kinds
    assert inflected.manifest.frameworks == tonal.manifest.frameworks
    assert inflected.manifest.language != tonal.manifest.language
    assert inflected.capabilities != tonal.capabilities
    assert inflected.capabilities.word_segmentation == "whitespace"
    assert tonal.capabilities.word_segmentation == "pack-adapter"
    assert inflected.capabilities.inflection and inflected.capabilities.grammatical_case
    assert tonal.capabilities.tone and tonal.capabilities.script_learning_required
    assert not tonal.capabilities.inflection


@pytest.mark.parametrize("name", NAMES)
def test_fixture_packs_are_explicitly_synthetic_and_never_offered_to_a_learner(
    name: str,
) -> None:
    loaded = pack(name)

    assert loaded.manifest.maturity == "fixture"
    assert str(highest_supported_maturity(loaded)) == "fixture"
    assert supported_onboarding_modes(loaded.manifest.maturity) == ()
    assert loaded.expectations is not None
    assert loaded.expectations.refuses_maturity == (
        "pilot",
        "onboarding-ready",
        "placement-ready",
    )
    for item in loaded.items:
        assert item.provenance.origin_profile == "synthetic"
        for origin in item.origins:
            assert origin.privacy == "synthetic"


@pytest.mark.parametrize("name", NAMES)
def test_fixture_packs_validate_through_the_frozen_content_contract(name: str) -> None:
    """Knowledge items are the kinds `lingua.content.v1` expresses, so they must fit it."""

    loaded = pack(name)

    assert loaded.knowledge
    for item in loaded.knowledge:
        projected = resolved_content_item(item)
        # The projection carries the *frozen* v1 hash over the field set ADR 0005 named.
        # A pack item's own hash covers everything the pack asserts about it, which is
        # strictly more, so the two are deliberately different numbers -- but each has to
        # be reproducible from what it claims to cover.
        assert projected.content_hash != item.content_hash
        assert projected.content_hash == resolved_content_item(item).content_hash
        assert projected.lifecycle == "verified"


@pytest.mark.parametrize("name", NAMES)
def test_fixture_pack_hashes_and_content_address_are_current(name: str) -> None:
    """`--check` fails rather than rewriting, so a stale fixture cannot pass silently."""

    loaded = pack(name)
    stamp_pack(FIXTURES / name, write=False)

    assert loaded.manifest.content_address == loaded.content_address
    assert set(loaded.manifest.files) == set(loaded.file_digests)


@pytest.mark.parametrize("name", NAMES)
def test_the_stage_zero_content_example_travels_with_its_pack(name: str) -> None:
    """`content.json` is the Stage 0 `lingua.content.v1` example, kept checksum-covered.

    It has no role in the pack format, so the loader never reads it -- but the manifest
    still has to account for it, which is what total checksum coverage means.
    """

    loaded = pack(name)
    example = read_json(name, "content.json")

    assert "content.json" in loaded.manifest.files
    assert example["schema_name"] == "lingua.content.v1"
    assert example["provenance"]["origin"] == "synthetic"
