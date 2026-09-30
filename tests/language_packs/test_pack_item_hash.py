"""Every semantic field of every item kind must change that item's content hash.

The first implementation hashed only the title, the body, and a projection of the
provenance. Changing an assessment task's expected answers, rubric, difficulty, modality,
anchor flag, or targets -- or a knowledge item's level or themes -- therefore left the hash
untouched: `pack stamp --check` accepted the edit, the item's existing reviews stayed bound
to text nobody had reviewed, and a pack update ran no invalidation. These tests mutate one
field at a time and require the hash to move.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from linguawiki.contracts import PackManifest
from linguawiki.errors import LinguaWikiError
from linguawiki.packs.format import directory_digests, load_pack, pack_content_address
from linguawiki.packs.stamp import stamp_pack
from tests.conftest import PILOT_PACK

Mutation = Callable[[dict[str, Any]], None]


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    target = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, target)
    return target


def _republish(root: Path) -> None:
    """Re-stamp the file digests and content address, leaving item hashes alone."""

    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = directory_digests(root)
    manifest["content_address"] = None
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    parsed = PackManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _hashes(root: Path) -> dict[tuple[str, str], str]:
    return {
        (item.content_kind, item.stable_key): item.content_hash for item in load_pack(root).items
    }


def _edit_jsonl(
    root: Path, relative: str, kind: str, mutate: Mutation, *, index: int = 0
) -> tuple[str, str]:
    """Mutate one JSONL record and return the item it identifies."""

    path = root / relative
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[index])
    before = json.dumps(record, sort_keys=True)
    mutate(record)
    assert json.dumps(record, sort_keys=True) != before, "the mutation changed nothing"
    lines[index] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kind, str(record["stable_key"])


def _edit_json(
    root: Path, relative: str, collection: str, kind: str, mutate: Mutation, *, index: int = 0
) -> tuple[str, str]:
    """Mutate one item (or a file header) and return the item it identifies."""

    path = root / relative
    document = json.loads(path.read_text(encoding="utf-8"))
    target = document[collection][index] if collection else document
    before = json.dumps(document, sort_keys=True)
    mutate(target)
    assert json.dumps(document, sort_keys=True) != before, "the mutation changed nothing"
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    key = target.get("stable_key") or target.get("bundle_key") or ""
    return kind, str(key)


def _assert_moves(
    root: Path, before: dict[tuple[str, str], str], expected: tuple[str, str] | None = None
) -> set[tuple[str, str]]:
    """Editing an item must fail `--check` and move that exact item's hash."""

    with pytest.raises(LinguaWikiError) as failure:
        stamp_pack(root, write=False)
    assert failure.value.payload.code == "pack_hashes_stale"

    stamp_pack(root)
    _republish(root)
    after = _hashes(root)
    moved = {key for key in before if before[key] != after.get(key)}
    assert moved, "no item hash moved, so the edit was invisible to review binding"
    if expected is not None and expected[1]:
        assert expected in moved, f"{expected} did not move: {sorted(moved)}"
    return moved


KNOWLEDGE_MUTATIONS: dict[str, Mutation] = {
    "body": lambda record: record.update(body=record["body"] + " Revised."),
    "title": lambda record: record.update(title=record["title"] + "!"),
    "summary": lambda record: record.update(summary="A newly added summary."),
    "kind": lambda record: record.update(kind="concept"),
    "level": lambda record: record.update(level="A1"),
    "level_max": lambda record: record.update(level_max="B1"),
    "themes": lambda record: record.update(themes=["zdrowie"]),
    "features": lambda record: record.update(features=["neuter"]),
    "frequency_band": lambda record: record.update(frequency_band="top-1000"),
    "aliases": lambda record: record.update(aliases=[{"alias": "nowy", "locale": "pl"}]),
    "risk_tier": lambda record: record["provenance"].update(risk_tier=1),
    "dependencies": lambda record: record["provenance"].update(
        dependencies=[{"kind": "content", "reference": "pl.lex.biuro"}]
    ),
}


@pytest.mark.parametrize("field", sorted(KNOWLEDGE_MUTATIONS))
def test_every_semantic_knowledge_field_changes_the_hash(pack: Path, field: str) -> None:
    before = _hashes(pack)

    edited = _edit_jsonl(pack, "seed/knowledge.jsonl", "knowledge", KNOWLEDGE_MUTATIONS[field])

    assert _assert_moves(pack, before, edited) == {edited}


#: The pilot's first free-text task. The mutations below run against *it* rather than
#: against task 0, which is multiple-choice: a task's presentation now has to agree with
#: its type and its answer key, so changing one field of a chooser in isolation produces
#: a task the contract refuses rather than a hash to compare.
FREE_TEXT_TASK = 3

TASK_MUTATIONS: dict[str, Mutation] = {
    "prompt": lambda record: record.update(prompt=record["prompt"] + " Now answer."),
    "expected": lambda record: record.update(expected={"answers": ["a completely other answer"]}),
    "rubric": lambda record: record.update(rubric={"version": 2, "dimensions": ["accuracy"]}),
    "rubric_version": lambda record: record.update(rubric_version=2),
    "difficulty": lambda record: record.update(difficulty=2.5),
    "level": lambda record: record.update(level="B1"),
    "dimension": lambda record: record.update(dimension="listening"),
    "task_type": lambda record: record.update(task_type="objective"),
    "modality": lambda record: record.update(modality="audio"),
    "content_family": lambda record: record.update(content_family="zdrowie"),
    "permitted_help": lambda record: record.update(permitted_help="dictionary-allowed"),
    "is_anchor": lambda record: record.update(is_anchor=True),
    "target_keys": lambda record: record.update(target_keys=["pl.lex.bilet"]),
    "presentation": lambda record: record.update(
        presentation={"kind": "free-text", "response_shape": "a different shape entirely"}
    ),
}


@pytest.mark.parametrize("field", sorted(TASK_MUTATIONS))
def test_every_semantic_assessment_field_changes_the_hash(pack: Path, field: str) -> None:
    """These are the fields that decide what a task asks and how it is scored."""

    before = _hashes(pack)

    edited = _edit_json(
        pack,
        "assessments/pl-a2-calibration.json",
        "tasks",
        "assessment_task",
        TASK_MUTATIONS[field],
        index=FREE_TEXT_TASK,
    )

    assert _assert_moves(pack, before, edited) == {edited}


def test_the_form_a_task_belongs_to_is_part_of_its_hash(pack: Path) -> None:
    """A task's purpose and level range come from its form, so the form frames it."""

    before = _hashes(pack)

    _edit_json(
        pack,
        "assessments/pl-a2-calibration.json",
        "",
        "assessment_task",
        lambda document: document.update(purpose="placement", level_max="B2"),
    )

    moved = _assert_moves(pack, before)
    assert all(kind == "assessment_task" for kind, _key in moved)
    assert len(moved) == 39


EXAMPLE_MUTATIONS: dict[str, Mutation] = {
    "text": lambda record: record.update(text=record["text"] + " I jeszcze raz."),
    "translation": lambda record: record.update(translation="A different translation."),
    "gloss": lambda record: record.update(gloss="a newly added gloss"),
    "locale": lambda record: record.update(locale="ru"),
    "difficulty": lambda record: record.update(difficulty="hard"),
    "item_key": lambda record: record.update(item_key="pl.lex.biuro"),
}


@pytest.mark.parametrize("field", sorted(EXAMPLE_MUTATIONS))
def test_every_semantic_example_field_changes_the_hash(pack: Path, field: str) -> None:
    before = _hashes(pack)

    edited = _edit_jsonl(pack, "seed/examples.jsonl", "example", EXAMPLE_MUTATIONS[field])

    assert _assert_moves(pack, before, edited) == {edited}


DESCRIPTOR_MUTATIONS: dict[str, Mutation] = {
    "descriptor": lambda record: record.update(descriptor="A different can-do statement."),
    # C1, so the mutation is a real change whichever descriptor comes first in the file.
    "level": lambda record: record.update(level="C1"),
    "dimension": lambda record: record.update(dimension="writing"),
    "locale": lambda record: record.update(locale="ru"),
}


@pytest.mark.parametrize("field", sorted(DESCRIPTOR_MUTATIONS))
def test_every_semantic_descriptor_field_changes_the_hash(pack: Path, field: str) -> None:
    before = _hashes(pack)

    edited = _edit_json(
        pack, "proficiency/cefr.json", "descriptors", "descriptor", DESCRIPTOR_MUTATIONS[field]
    )

    assert _assert_moves(pack, before, edited) == {edited}


ACTIVITY_MUTATIONS: dict[str, Mutation] = {
    "structure": lambda record: record.update(structure={"steps": ["one step only"]}),
    "mode": lambda record: record.update(mode="translation"),
    "title": lambda record: record.update(title="A different activity"),
    "minutes": lambda record: record.update(minutes=45),
    "level": lambda record: record.update(level="A1"),
}


@pytest.mark.parametrize("field", sorted(ACTIVITY_MUTATIONS))
def test_every_semantic_activity_field_changes_the_hash(pack: Path, field: str) -> None:
    before = _hashes(pack)

    edited = _edit_json(
        pack,
        "activities/pl-a2-activities.json",
        "templates",
        "activity_template",
        ACTIVITY_MUTATIONS[field],
    )

    assert _assert_moves(pack, before, edited) == {edited}


RECOMMENDATION_MUTATIONS: dict[str, Mutation] = {
    "title": lambda record: record.update(title="A different source"),
    "locator": lambda record: record.update(locator="https://example.invalid"),
    "creator": lambda record: record.update(creator="Somebody else"),
    "modality": lambda record: record.update(modality="speech"),
    "level": lambda record: record.update(level="B1"),
    "license": lambda record: record.update(license="all rights reserved"),
    "rights_status": lambda record: record.update(rights_status="restricted"),
    "support_language": lambda record: record.update(support_language="ru"),
    "notes": lambda record: record.update(notes="Different guidance for the learner."),
}


@pytest.mark.parametrize("field", sorted(RECOMMENDATION_MUTATIONS))
def test_every_semantic_recommendation_field_changes_the_hash(pack: Path, field: str) -> None:
    """Rights and locator especially: they decide what a learner may store."""

    before = _hashes(pack)

    edited = _edit_json(
        pack,
        "references/pl-a2-sources.json",
        "recommendations",
        "source_recommendation",
        RECOMMENDATION_MUTATIONS[field],
    )

    assert _assert_moves(pack, before, edited) == {edited}


BUNDLE_MUTATIONS: dict[str, Mutation] = {
    "title": lambda document: document.update(title="A different bundle"),
    "bundle_type": lambda document: document.update(bundle_type="supplementary"),
    "level": lambda document: document.update(level="A1"),
    "license": lambda document: document.update(license="all rights reserved"),
    "items": lambda document: document.update(items=document["items"][:3]),
}


@pytest.mark.parametrize("field", sorted(BUNDLE_MUTATIONS))
def test_every_semantic_bundle_field_changes_the_hash(pack: Path, field: str) -> None:
    """A bundle's item list decides what preparation may draw on."""

    before = _hashes(pack)

    edited = _edit_json(
        pack, "resource-bundles/cefr-a2-core.json", "", "resource_bundle", BUNDLE_MUTATIONS[field]
    )

    assert _assert_moves(pack, before, edited) == {edited}


def test_editing_a_manifest_origin_profile_changes_every_item_that_uses_it(
    pack: Path,
) -> None:
    """The hash covers the *resolved* origins, not the name of the profile."""

    before = _hashes(pack)
    path = pack / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["origin_profiles"]["authored-lexical"][0]["rights"] = "all rights reserved"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(LinguaWikiError) as failure:
        stamp_pack(pack, write=False)
    assert failure.value.payload.code == "pack_hashes_stale"

    stamp_pack(pack)
    _republish(pack)
    after = _hashes(pack)
    moved = {key for key in before if before[key] != after.get(key)}
    assert len(moved) > 30, "a changed rights claim must reach every item that inherits it"


def test_review_workflow_fields_deliberately_do_not_change_the_hash(pack: Path) -> None:
    """ADR 0005: recording a review must not change the revision it examined."""

    before = _hashes(pack)
    path = pack / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["review_profiles"]["authored-verified"]["linguistic"]["method"] = "a new method"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _republish(pack)

    assert stamp_pack(pack, write=False).stale == ()
    assert _hashes(pack) == before


def test_changing_only_an_items_lifecycle_does_not_change_its_hash(pack: Path) -> None:
    before = _hashes(pack)
    _edit_jsonl(
        pack,
        "seed/knowledge.jsonl",
        "knowledge",
        lambda record: record["provenance"].update(lifecycle="approved-personal"),
    )
    _republish(pack)

    assert stamp_pack(pack, write=False).stale == ()
    assert _hashes(pack) == before
