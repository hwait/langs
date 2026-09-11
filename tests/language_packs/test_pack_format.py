"""Loading a pack directory: total checksum coverage, derived identity, and the gate.

These are the three properties `load_pack` establishes and nothing else does, so each is
tested against a mutated copy of a real pack rather than a hand-built stub.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.ids import ContentId, IdPrefix, derive_id
from linguawiki.packs.format import (
    KNOWLEDGE_KIND,
    content_id_for,
    directory_digests,
    load_pack,
    pack_content_address,
    relation_id_for,
    resolve_pack_path,
)
from linguawiki.packs.stamp import stamp_pack
from tests.conftest import FIXTURE_PACKS, PILOT_PACK


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    """A writable copy of the inflected fixture pack, small enough to mutate freely."""

    target = tmp_path / "fixture-inflected"
    shutil.copytree(FIXTURE_PACKS / "inflected", target)
    return target


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _republish(root: Path) -> None:
    """Re-stamp the file digests and content address after an intentional edit."""

    manifest = _manifest(root)
    manifest["files"] = directory_digests(root)
    manifest["content_address"] = None
    _write_manifest(root, manifest)
    from linguawiki.contracts import PackManifest

    parsed = PackManifest.model_validate(_manifest(root))
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    _write_manifest(root, manifest)


def test_a_shipped_pack_loads_and_reports_its_own_content_address() -> None:
    pack = load_pack(PILOT_PACK)

    assert pack.pack_key == "pl-pilot"
    assert pack.manifest.content_address == pack.content_address
    assert pack.warnings == ()
    assert pack.knowledge and pack.tasks and pack.bundles


def test_an_undeclared_file_in_the_directory_fails_checksum_coverage(pack_copy: Path) -> None:
    """A file nobody declared is a file nobody reviewed."""

    (pack_copy / "seed" / "smuggled.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_checksum_mismatch"
    assert any(
        "present but undeclared: seed/smuggled.jsonl" in detail.reason
        for detail in failure.value.payload.details
    )


def test_a_declared_file_that_is_absent_fails_rather_than_loading_empty(
    pack_copy: Path,
) -> None:
    (pack_copy / "seed" / "examples.jsonl").unlink()

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_checksum_mismatch"
    assert any(
        "declared but absent: seed/examples.jsonl" in detail.reason
        for detail in failure.value.payload.details
    )


def test_an_edited_file_fails_its_declared_checksum(pack_copy: Path) -> None:
    path = pack_copy / "capabilities.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tone"] = True
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_checksum_mismatch"
    assert any(
        "checksum differs: capabilities.json" in detail.reason
        for detail in failure.value.payload.details
    )


def test_a_required_file_cannot_simply_be_undeclared(pack_copy: Path) -> None:
    """Dropping `seed/knowledge.jsonl` from the manifest and the disk is still a failure."""

    manifest = _manifest(pack_copy)
    del manifest["files"]["seed/knowledge.jsonl"]  # type: ignore[index]
    _write_manifest(pack_copy, manifest)
    (pack_copy / "seed" / "knowledge.jsonl").unlink()

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert any(
        "required file missing: seed/knowledge.jsonl" in detail.reason
        for detail in failure.value.payload.details
    )


def test_a_recorded_content_address_its_files_do_not_produce_is_refused(
    pack_copy: Path,
) -> None:
    manifest = _manifest(pack_copy)
    manifest["content_address"] = "b" * 64
    _write_manifest(pack_copy, manifest)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_content_address_mismatch"


def test_an_unpublished_pack_loads_with_a_warning_rather_than_an_error(
    pack_copy: Path,
) -> None:
    manifest = _manifest(pack_copy)
    manifest["content_address"] = None
    _write_manifest(pack_copy, manifest)

    pack = load_pack(pack_copy)

    assert any("no recorded content address" in warning for warning in pack.warnings)


def test_the_content_address_covers_the_manifest_as_well_as_the_files(
    pack_copy: Path,
) -> None:
    """Editing a manifest field must change the address, or publication proves nothing."""

    before = load_pack(pack_copy).content_address
    manifest = _manifest(pack_copy)
    manifest["name"] = "Renamed Fixture"
    _write_manifest(pack_copy, manifest)
    _republish(pack_copy)

    assert load_pack(pack_copy).content_address != before


def test_a_stale_content_hash_is_reported_per_item(pack_copy: Path) -> None:
    """Editing an item without re-stamping must fail rather than re-bind its reviews."""

    path = pack_copy / "seed" / "knowledge.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["body"] = record["body"] + " Edited without re-stamping."
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_invalid"
    assert any("content_hash is stale" in detail.reason for detail in failure.value.payload.details)


def test_stamping_repairs_a_stale_hash_and_check_mode_refuses_to(pack_copy: Path) -> None:
    path = pack_copy / "seed" / "knowledge.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["body"] = record["body"] + " Edited."
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        stamp_pack(pack_copy, write=False)
    assert failure.value.payload.code == "pack_hashes_stale"

    report = stamp_pack(pack_copy)
    assert "seed/knowledge.jsonl" in report.written
    _republish(pack_copy)
    assert load_pack(pack_copy).knowledge


def test_stamping_a_shipped_pack_is_a_no_op() -> None:
    """Every committed pack must already declare the hashes its contents produce."""

    for root in (PILOT_PACK, FIXTURE_PACKS / "inflected", FIXTURE_PACKS / "tonal"):
        report = stamp_pack(root, write=False)
        assert report.stale == ()
        assert report.written == ()


def test_content_identity_is_derived_from_the_pack_key_and_stable_key() -> None:
    """Reinstalling must reproduce the same IDs, or learner state is orphaned."""

    pack = load_pack(PILOT_PACK)
    item = pack.knowledge[0]

    assert item.content_id == content_id_for("pl-pilot", KNOWLEDGE_KIND, item.stable_key)
    assert item.content_id == ContentId.derive("pl-pilot", KNOWLEDGE_KIND, item.stable_key)
    assert item.content_id != content_id_for("other-pack", KNOWLEDGE_KIND, item.stable_key)
    assert load_pack(PILOT_PACK).knowledge[0].content_id == item.content_id


def test_a_relation_identity_is_derived_from_its_own_edge() -> None:
    pack = load_pack(PILOT_PACK)
    relation = pack.relations[0]

    assert relation_id_for("pl-pilot", relation) == derive_id(
        IdPrefix.CONTENT,
        "pl-pilot",
        "relation",
        relation.source_key,
        relation.relation_type,
        relation.target_key or relation.target_ref or "",
    )


def test_a_duplicate_stable_key_is_refused(pack_copy: Path) -> None:
    path = pack_copy / "seed" / "knowledge.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([*lines, lines[0]]) + "\n", encoding="utf-8")
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert any("duplicate stable key" in detail.reason for detail in failure.value.payload.details)


def test_an_unresolved_relation_target_is_refused(pack_copy: Path) -> None:
    path = pack_copy / "seed" / "relations.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["target_key"] = "qix.lex.missing"
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert any(
        "relation target does not resolve" in detail.reason
        for detail in failure.value.payload.details
    )


def test_an_undeclared_theme_or_level_is_refused(pack_copy: Path) -> None:
    path = pack_copy / "seed" / "knowledge.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["themes"] = ["undeclared-theme"]
    record["level"] = "L9"
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp_pack(pack_copy)
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    reasons = " ".join(detail.reason for detail in failure.value.payload.details)
    assert "undeclared level L9" in reasons
    assert "undeclared theme undeclared-theme" in reasons


def test_an_item_claiming_a_lifecycle_its_reviews_do_not_support_is_refused(
    pack_copy: Path,
) -> None:
    manifest = _manifest(pack_copy)
    manifest["review_profiles"]["fixture-verified"]["linguistic"] = {  # type: ignore[index]
        "state": "unreviewed",
        "reviewer_kind": "not-applicable",
    }
    _write_manifest(pack_copy, manifest)
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_invalid"
    assert any("linguistic" in detail.reason for detail in failure.value.payload.details)


def test_an_item_naming_a_profile_the_manifest_does_not_declare_is_refused(
    pack_copy: Path,
) -> None:
    path = pack_copy / "seed" / "knowledge.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["provenance"]["review_profile"] = "no-such-profile"
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_profile_unknown"


def test_origins_that_disagree_about_rights_or_privacy_are_refused(pack_copy: Path) -> None:
    """A mixed-privacy item has no single answer to give the content contract."""

    manifest = _manifest(pack_copy)
    profile = manifest["origin_profiles"]["synthetic"]  # type: ignore[index]
    second = dict(profile[0])
    second["privacy"] = "public"
    profile.append(second)
    _write_manifest(pack_copy, manifest)
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_provenance_inconsistent"


def test_a_bundle_that_disagrees_with_the_manifest_about_dependencies_is_refused(
    pack_copy: Path,
) -> None:
    path = pack_copy / "resource-bundles" / "fixture-l2-core.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["depends_on"] = []
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    stamp_pack(pack_copy)
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_bundle_mismatch"


def test_a_bundle_referencing_a_missing_item_is_refused(pack_copy: Path) -> None:
    path = pack_copy / "resource-bundles" / "fixture-l2-core.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["items"].append({"item_kind": "knowledge", "item_ref": "qix.lex.absent"})
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    stamp_pack(pack_copy)
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert any(
        "references missing knowledge qix.lex.absent" in detail.reason
        for detail in failure.value.payload.details
    )


def test_a_missing_manifest_is_named_rather_than_crashing(tmp_path: Path) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        load_pack(tmp_path)

    assert failure.value.payload.code == "pack_manifest_missing"


def test_unparsable_pack_json_is_a_structured_failure(pack_copy: Path) -> None:
    (pack_copy / "seed" / "knowledge.jsonl").write_text("{not json\n", encoding="utf-8")
    _republish(pack_copy)

    with pytest.raises(LinguaWikiError) as failure:
        load_pack(pack_copy)

    assert failure.value.payload.code == "pack_file_unreadable"


def test_a_pack_reference_resolves_a_directory_or_a_bundled_pack_key() -> None:
    assert resolve_pack_path(PILOT_PACK) == PILOT_PACK.resolve()
    assert resolve_pack_path("pl-pilot").name == "pl-pilot"

    with pytest.raises(LinguaWikiError) as failure:
        resolve_pack_path("no-such-pack")
    assert failure.value.payload.code == "pack_not_found"

    with pytest.raises(LinguaWikiError) as traversal:
        resolve_pack_path("../escaped")
    assert traversal.value.payload.code == "pack_not_found"


def test_a_scaffolded_pack_validates_as_a_fixture_and_nothing_stronger(
    tmp_path: Path,
) -> None:
    """A new pack starts as a fixture and has to earn every stronger maturity."""

    from linguawiki.contracts import PackMaturity
    from linguawiki.packs.coverage import (
        highest_supported_maturity,
        maturity_supported,
        supported_onboarding_modes,
    )
    from linguawiki.services import packs as pack_service

    report = pack_service.scaffold(
        tmp_path / "new-pack",
        pack_key="new-pack",
        name="A New Pack",
        language="cs",
        framework_id="cefr",
        framework_name="Common European Framework",
        framework_version="2020",
        levels=["A1", "A2", "B1"],
        bands=["A2"],
        themes=["everyday"],
        support_languages=["en"],
    )
    pack = load_pack(tmp_path / "new-pack")

    assert report.maturity == "fixture"
    assert pack.manifest.content_address == pack.content_address
    assert pack.knowledge == ()
    assert str(highest_supported_maturity(pack)) == "fixture"
    assert not maturity_supported(pack, PackMaturity.PILOT)
    assert supported_onboarding_modes(pack.manifest.maturity) == ()
    assert stamp_pack(tmp_path / "new-pack", write=False).stale == ()
    assert any("which maturity it may claim" in warning for warning in report.warnings)
    assert any("guesses neither" in warning for warning in report.warnings)


def test_scaffolding_into_a_non_empty_directory_is_refused(tmp_path: Path) -> None:
    from linguawiki.services import packs as pack_service

    (tmp_path / "occupied").mkdir()
    (tmp_path / "occupied" / "notes.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.scaffold(
            tmp_path / "occupied",
            pack_key="new-pack",
            name="A New Pack",
            language="cs",
            framework_id="cefr",
            framework_name="CEFR",
            framework_version="2020",
            levels=["A1", "A2"],
            bands=["A2"],
            themes=["everyday"],
        )

    assert failure.value.payload.code == "pack_target_not_empty"
    assert (tmp_path / "occupied" / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_scaffolding_needs_a_band_and_a_theme(tmp_path: Path) -> None:
    from linguawiki.services import packs as pack_service

    common = {
        "pack_key": "new-pack",
        "name": "A New Pack",
        "language": "cs",
        "framework_id": "cefr",
        "framework_name": "CEFR",
        "framework_version": "2020",
        "levels": ["A1", "A2"],
    }

    with pytest.raises(LinguaWikiError):
        pack_service.scaffold(tmp_path / "a", bands=[], themes=["everyday"], **common)
    with pytest.raises(LinguaWikiError):
        pack_service.scaffold(tmp_path / "b", bands=["A2"], themes=[], **common)
