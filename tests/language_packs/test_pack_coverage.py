"""Coverage measurement and the maturity gates it feeds.

A maturity level is a promise about what onboarding may offer, so the tests here check
that the promise is *measured* -- including that the shipped pilot pack refuses the levels
it cannot support.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from linguawiki.contracts import PackMaturity
from linguawiki.errors import LinguaWikiError
from linguawiki.packs.coverage import (
    MATURITY_ORDER,
    counts,
    coverage_report,
    highest_supported_maturity,
    maturity_supported,
    per_band_counts,
    supported_onboarding_modes,
    unmet_requirements,
)
from linguawiki.packs.format import load_pack
from linguawiki.services import packs as pack_service
from tests.conftest import FIXTURE_PACKS, PILOT_PACK


def test_the_polish_pilot_pack_meets_the_pilot_gate_it_claims() -> None:
    pack = load_pack(PILOT_PACK)
    report = coverage_report(pack)

    assert report.declared_maturity == "pilot"
    assert report.declared_maturity_supported is True
    assert report.expectation_failures == ()
    assert [
        requirement for requirement in report.requirements["pilot"] if not requirement.met
    ] == []


def test_the_pilot_pack_refuses_the_levels_it_cannot_support() -> None:
    """A pilot slice must not be mistakable for a level-complete pack."""

    pack = load_pack(PILOT_PACK)

    assert str(highest_supported_maturity(pack)) == "pilot"
    assert not maturity_supported(pack, PackMaturity.ONBOARDING_READY)
    assert not maturity_supported(pack, PackMaturity.PLACEMENT_READY)
    assert pack.expectations is not None
    assert pack.expectations.refuses_maturity == ("onboarding-ready", "placement-ready")


def test_the_unmet_requirements_name_the_gap_rather_than_only_the_number() -> None:
    unmet = unmet_requirements(load_pack(PILOT_PACK), PackMaturity.PLACEMENT_READY)

    assert unmet
    assert all(requirement.required and requirement.observed for requirement in unmet)
    assert any(requirement.name == "alternate_placement_forms" for requirement in unmet)
    assert any(requirement.gap for requirement in unmet)


def test_maturity_decides_which_onboarding_modes_exist() -> None:
    assert supported_onboarding_modes("fixture") == ()
    assert supported_onboarding_modes("pilot") == ("declared-level",)
    assert supported_onboarding_modes("onboarding-ready") == ("declared-level",)
    assert supported_onboarding_modes("placement-ready") == ("declared-level", "placement")


def test_the_maturity_order_is_cumulative() -> None:
    """A stronger level's report carries everything the weaker ones required."""

    assert MATURITY_ORDER == (
        PackMaturity.FIXTURE,
        PackMaturity.PILOT,
        PackMaturity.ONBOARDING_READY,
        PackMaturity.PLACEMENT_READY,
    )
    report = coverage_report(load_pack(PILOT_PACK))

    assert set(report.requirements) == {"pilot", "onboarding-ready", "placement-ready"}
    assert unmet_requirements(load_pack(PILOT_PACK), PackMaturity.PILOT) == ()
    weaker = {
        requirement.name
        for requirement in unmet_requirements(load_pack(PILOT_PACK), PackMaturity.ONBOARDING_READY)
    }
    stronger = {
        requirement.name
        for requirement in unmet_requirements(load_pack(PILOT_PACK), PackMaturity.PLACEMENT_READY)
    }
    assert weaker <= stronger


def test_coverage_reports_qualitative_gaps_beside_the_counts() -> None:
    report = coverage_report(load_pack(PILOT_PACK))

    assert set(report.themes) >= {"podróże", "jedzenie", "zdrowie"}
    assert report.provenance["human-authored"] > report.provenance["ai-generated"]
    assert report.support_languages["ru"] > 0
    assert report.support_languages["en"] > 0
    assert report.prerequisite_connectivity["prerequisite_edges"] > 0
    assert report.descriptor_coverage
    assert report.gaps


def test_unreviewed_drafts_are_reported_as_unresolved_and_excluded_from_the_counts() -> None:
    pack = load_pack(PILOT_PACK)
    report = coverage_report(pack)
    totals = counts(pack)

    assert len(report.unresolved_items) == 3
    assert all(key.startswith("knowledge/pl.draft.") for key in report.unresolved_items)
    assert totals["reviewed_knowledge"] == totals["knowledge"] - 3


def test_per_band_counts_are_reported_for_each_declared_band() -> None:
    pack = load_pack(PILOT_PACK)
    bands = per_band_counts(pack)

    assert set(bands) == set(pack.manifest.bands) == {"A2"}
    assert bands["A2"]["grammar"] >= 15
    assert bands["A2"]["script"] >= 10
    assert bands["A2"]["reviewed_knowledge"] >= 60


def test_a_fixture_pack_supports_no_onboarding_mode_at_all() -> None:
    for name in ("inflected", "tonal"):
        pack = load_pack(FIXTURE_PACKS / name)
        assert str(highest_supported_maturity(pack)) == "fixture"
        assert supported_onboarding_modes(pack.manifest.maturity) == ()


def test_a_packs_own_expectations_are_checked_as_its_self_test(tmp_path: Path) -> None:
    """The generic gate cannot notice that a file was accidentally emptied; this can."""

    target = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, target)
    path = target / "tests" / "expectations.json"
    expectations = json.loads(path.read_text(encoding="utf-8"))
    expectations["minimum_counts"]["reviewed_knowledge"] = 10_000
    expectations["required_themes"] = [*expectations["required_themes"], "nieistniejący temat"]
    path.write_text(json.dumps(expectations, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    from linguawiki.contracts import PackManifest
    from linguawiki.packs.format import directory_digests, pack_content_address

    manifest["files"] = directory_digests(target)
    manifest["content_address"] = None
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    parsed = PackManifest.model_validate(
        json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    )
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    report = coverage_report(load_pack(target))

    assert any("reviewed_knowledge" in failure for failure in report.expectation_failures)
    assert any("required themes absent" in failure for failure in report.expectation_failures)


def test_validate_and_coverage_agree_about_whether_a_pack_is_usable() -> None:
    validation = pack_service.validate(PILOT_PACK)
    coverage = pack_service.coverage(PILOT_PACK)

    assert validation.ok is True
    assert coverage.ok is True
    assert coverage.declared_maturity == validation.maturity
    assert coverage.version == validation.version


def test_publishing_at_a_maturity_the_pack_cannot_support_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, target)
    before = (target / "manifest.json").read_text(encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.publish(target, maturity="placement-ready")

    assert failure.value.payload.code == "pack_maturity_gate_failed"
    assert failure.value.payload.details
    # The refused publish must not leave the manifest stamped as placement-ready.
    assert (target / "manifest.json").read_text(encoding="utf-8") == before


def test_publishing_at_the_declared_maturity_is_a_no_op_for_a_published_pack(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, target)

    report = pack_service.publish(target)

    assert report.stamped is False
    assert report.maturity == "pilot"
    assert report.content_address == load_pack(target).content_address


def test_every_shipped_pack_item_passes_its_own_risk_tier_gate() -> None:
    """The stage exit gate, asserted item by item rather than trusted to the loader."""

    from linguawiki.provenance import gate_problems

    for root in (PILOT_PACK, FIXTURE_PACKS / "inflected", FIXTURE_PACKS / "tonal"):
        pack = load_pack(root)
        for item in pack.items:
            problems = gate_problems(
                risk_tier=item.risk_tier,
                lifecycle=item.lifecycle,
                reviews=item.reviews,
                origin_classes=[origin.origin_class for origin in item.origins],
                source_references=[origin.reference for origin in item.origins if origin.reference],
            )
            assert problems == (), f"{root.name}/{item.stable_key}: {problems}"
            assert item.hash_matches, item.stable_key
            assert item.origins
            assert set(item.reviews) == {
                "linguistic",
                "pedagogical",
                "source-alignment",
                "rights",
                "privacy",
            }
