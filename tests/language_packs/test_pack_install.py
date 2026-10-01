"""Installing, reinstalling, and updating a pack inside a learner database.

The install path exists to be *idempotent* and to leave learner state attached across an
upgrade, so those are the properties tested here rather than the row counts.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.ids import ContentId
from linguawiki.packs.format import (
    KNOWLEDGE_KIND,
    content_id_for,
    directory_digests,
    load_pack,
    pack_content_address,
)
from linguawiki.packs.stamp import stamp_pack
from linguawiki.services import database as database_service
from linguawiki.services import packs as pack_service
from linguawiki.services import workspace as workspace_service
from tests.conftest import (
    FIXTURE_PACKS,
    NEXT_PILOT_VERSION,
    PILOT_PACK,
    PILOT_VERSION,
    PolishWorkspace,
    SyntheticWorkspace,
)

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"


def _republish(root: Path) -> None:
    from linguawiki.contracts import PackManifest

    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = directory_digests(root)
    manifest["content_address"] = None
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    parsed = PackManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.fixture
def editable_pilot(tmp_path: Path) -> Path:
    target = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, target)
    return target


def test_installing_a_pack_records_its_identity_files_and_content(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = pack_service.install(
        synthetic_workspace.paths, PILOT_PACK, clock=synthetic_workspace.clock
    )
    pack = load_pack(PILOT_PACK)

    assert report.created is True
    assert report.checksum == pack.content_address
    assert report.item_counts["knowledge"] == len(pack.knowledge)

    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM pack_files")) == len(pack.file_digests)
        assert int(database.scalar("SELECT count(*) FROM content_records")) == len(pack.items)
        assert int(database.scalar("SELECT count(*) FROM knowledge_relations")) == len(
            pack.relations
        )
        assert int(database.scalar("SELECT count(*) FROM assessment_tasks")) == len(pack.tasks)
        # Every content row carries provenance and one review per axis.
        assert int(database.scalar("SELECT count(*) FROM content_origins")) >= len(pack.items)
        assert int(database.scalar("SELECT count(*) FROM content_reviews")) == 5 * len(pack.items)


def test_reinstalling_the_same_version_is_an_idempotent_no_op(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    first = pack_service.install(
        synthetic_workspace.paths, PILOT_PACK, clock=synthetic_workspace.clock
    )
    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        before = database.query(
            "SELECT content_id, content_hash, lifecycle FROM content_records ORDER BY content_id"
        )

    second = pack_service.install(
        synthetic_workspace.paths, PILOT_PACK, clock=synthetic_workspace.clock
    )

    assert first.created is True
    assert second.created is False
    assert second.reinstalled is True
    assert second.diff is not None and second.diff.identical is True
    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        assert (
            database.query(
                "SELECT content_id, content_hash, lifecycle FROM content_records "
                "ORDER BY content_id"
            )
            == before
        )
        assert int(database.scalar("SELECT count(*) FROM language_packs")) == 1


def test_two_contrasting_fixture_packs_install_side_by_side(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The core must hold two structurally different languages at once."""

    for name in ("inflected", "tonal"):
        pack_service.install(
            synthetic_workspace.paths, FIXTURE_PACKS / name, clock=synthetic_workspace.clock
        )

    installed = pack_service.listing(synthetic_workspace.paths, clock=synthetic_workspace.clock)
    integrity = database_service.check(
        synthetic_workspace.paths,
        lock=workspace_service.load_lock(synthetic_workspace.paths),
        clock=synthetic_workspace.clock,
    )

    assert {entry.pack_key for entry in installed} == {"fixture-inflected", "fixture-tonal"}
    assert {entry.language_tag for entry in installed} == {"qix-Latn", "ztx-Zzzz"}
    assert integrity.ok is True


def test_a_second_content_for_the_same_version_is_refused_as_immutable(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path
) -> None:
    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    path = editable_pilot / "seed" / "knowledge.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["body"] = record["body"] + " Changed without a version bump."
    lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp_pack(editable_pilot)
    _republish(editable_pilot)

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "pack_version_conflict"


def test_a_different_version_requires_the_update_command(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path
) -> None:
    """An update has to be previewed, so `install` refuses to perform one silently."""

    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    manifest = json.loads((editable_pilot / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = NEXT_PILOT_VERSION
    (editable_pilot / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    stamp_pack(editable_pilot)
    _republish(editable_pilot)

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock
        )
    assert failure.value.payload.code == "pack_update_required"

    preview = pack_service.install(
        synthetic_workspace.paths,
        editable_pilot,
        clock=synthetic_workspace.clock,
        allow_update=True,
        dry_run=True,
    )
    assert preview.dry_run is True
    assert preview.updated_from == PILOT_VERSION
    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        assert database.scalar("SELECT version FROM pack_installations") == PILOT_VERSION


def _bump_version_and_change_an_item(root: Path, *, remove_key: str) -> tuple[str, str]:
    """Publish a 0.3.0 that edits one item and drops another, and say which."""

    path = root / "seed" / "knowledge.jsonl"
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    changed = records[0]["stable_key"]
    records[0]["body"] = records[0]["body"] + " Revised in 0.3.0."
    kept = [record for record in records if record["stable_key"] != remove_key]
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in kept) + "\n",
        encoding="utf-8",
    )
    for bundle in sorted((root / "resource-bundles").glob("*.json")):
        document = json.loads(bundle.read_text(encoding="utf-8"))
        document["items"] = [
            item
            for item in document["items"]
            if not (item["item_kind"] == "knowledge" and item["item_ref"] == remove_key)
        ]
        bundle.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    relations = root / "seed" / "relations.jsonl"
    edges = [
        json.loads(line)
        for line in relations.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    edges = [
        edge for edge in edges if remove_key not in (edge.get("source_key"), edge.get("target_key"))
    ]
    relations.write_text(
        "\n".join(json.dumps(edge, ensure_ascii=False, sort_keys=True) for edge in edges) + "\n",
        encoding="utf-8",
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = NEXT_PILOT_VERSION
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    stamp_pack(root)
    _republish(root)
    return changed, remove_key


def test_an_update_preview_is_deterministic_and_names_every_change(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path
) -> None:
    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    changed, removed = _bump_version_and_change_an_item(editable_pilot, remove_key="pl.lex.remont")

    first = pack_service.diff(
        synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock
    )
    second = pack_service.diff(
        synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    # The edited item and the bundle that stopped listing the removed one. A bundle whose
    # item list changed is a changed bundle: the hash covers the list, so dropping a
    # reference cannot slip past the preview.
    assert [entry.stable_key for entry in first.changed] == [changed, "cefr-a2-core"]
    assert [entry.stable_key for entry in first.removed] == [removed]
    assert first.identical is False
    assert first.unchanged > 0


def test_an_update_deprecates_a_removed_item_rather_than_deleting_it(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path
) -> None:
    """Learner state may still reference it, so the row has to survive the upgrade."""

    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    _, removed = _bump_version_and_change_an_item(editable_pilot, remove_key="pl.lex.remont")
    removed_id = str(content_id_for("pl-pilot", KNOWLEDGE_KIND, removed))

    pack_service.install(
        synthetic_workspace.paths,
        editable_pilot,
        clock=synthetic_workspace.clock,
        allow_update=True,
    )

    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        row = database.one(
            "SELECT lifecycle, invalidation_reason FROM content_records WHERE content_id = ?",
            [removed_id],
        )
    assert row is not None
    assert str(row[0]) == "deprecated"
    assert f"absent from pack version {NEXT_PILOT_VERSION}" in str(row[1])


def test_an_update_keeps_learner_state_attached_to_the_items_it_changed(
    polish_workspace: object, editable_pilot: Path
) -> None:
    """Identity is derived, so a learner's row survives an upgrade of the same item."""

    from linguawiki.services import resources as resource_service

    paths = polish_workspace.paths  # type: ignore[attr-defined]
    clock = polish_workspace.clock  # type: ignore[attr-defined]
    resource_service.prepare(paths, weeks=1, item_budget=40, clock=clock)
    with open_reader(paths, clock=clock) as database:
        before = database.query(
            "SELECT content_id, stage, stage_source FROM track_item_state ORDER BY content_id"
        )
    assert before

    shutil.rmtree(editable_pilot)
    shutil.copytree(PILOT_PACK, editable_pilot)
    _bump_version_and_change_an_item(editable_pilot, remove_key="pl.lex.remont")
    pack_service.install(paths, editable_pilot, clock=clock, allow_update=True)

    with open_reader(paths, clock=clock) as database:
        after = database.query(
            "SELECT content_id, stage, stage_source FROM track_item_state ORDER BY content_id"
        )
    assert after == before


def test_a_pack_may_not_change_the_language_it_serves(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path
) -> None:
    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    manifest = json.loads((editable_pilot / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = NEXT_PILOT_VERSION
    manifest["language"] = "cs"
    (editable_pilot / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    stamp_pack(editable_pilot)
    _republish(editable_pilot)

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            synthetic_workspace.paths,
            editable_pilot,
            clock=synthetic_workspace.clock,
            allow_update=True,
        )

    assert failure.value.payload.code == "pack_language_conflict"


def test_a_framework_may_not_reorder_its_levels_between_packs(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path
) -> None:
    """A level's position is part of the framework's identity, so a clash is refused."""

    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    with (
        open_writer(
            synthetic_workspace.paths, command="test.reorder", clock=synthetic_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE proficiency_framework_levels SET sequence = 99 "
            "WHERE framework_id = 'cefr' AND level_code = 'C2'"
        )

    with pytest.raises(LinguaWikiError) as failure:
        pack_service.install(
            synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock
        )

    assert failure.value.payload.code == "framework_level_conflict"


def test_a_dry_run_install_writes_nothing(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = pack_service.install(
        synthetic_workspace.paths, PILOT_PACK, clock=synthetic_workspace.clock, dry_run=True
    )

    assert report.dry_run is True
    assert report.created is True
    assert report.diff is not None
    assert len(report.diff.added) == len(load_pack(PILOT_PACK).items)
    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        assert int(database.scalar("SELECT count(*) FROM language_packs")) == 0


def test_naming_a_pack_is_required_once_two_are_installed(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    for name in ("inflected", "tonal"):
        pack_service.install(
            synthetic_workspace.paths, FIXTURE_PACKS / name, clock=synthetic_workspace.clock
        )

    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        with pytest.raises(LinguaWikiError) as failure:
            pack_service.installed_pack(database)
        assert failure.value.payload.code == "pack_selection_required"
        assert pack_service.installed_pack(database, "fixture-tonal")["language_tag"] == (
            "ztx-Zzzz"
        )
        with pytest.raises(LinguaWikiError) as missing:
            pack_service.installed_pack(database, "pl-pilot")
        assert missing.value.payload.code == "pack_not_installed"


def _learner_card(workspace: PolishWorkspace, *, depends_on: str, stable_key: str) -> str:
    """A learner-owned record depending on one pack item.

    Written directly because no Stage 2 command authors learner-owned content yet; the
    row is the shape `_learner_dependents` exists to find, and Stage 3's authoring
    commands will produce it. Everything the test then asserts goes through `pack diff`.
    """

    content_id = str(ContentId.new())
    with (
        open_writer(
            workspace.paths, command="test.learner-card", clock=workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        now = transaction.now()
        transaction.execute(
            "INSERT INTO content_records (content_id, content_kind, pack_id, track_id, "
            "stable_key, language_tag, content_hash, lifecycle, risk_tier, batch_id, "
            "quarantined, invalidation_reason, created_at, updated_at) "
            "VALUES (?, 'anki_note', NULL, ?, ?, 'pl', ?, 'approved-personal', 1, NULL, FALSE, "
            "NULL, ?, ?)",
            [content_id, workspace.track_id, stable_key, "0" * 64, now, now],
        )
        transaction.execute(
            "INSERT INTO content_dependencies (content_id, sequence, dependency_kind, "
            "dependency_ref, expected_hash, on_change) VALUES (?, 1, 'content', ?, NULL, "
            "'needs-review')",
            [content_id, depends_on],
        )
    return content_id


def test_a_preview_names_the_learner_content_a_removal_would_break(
    polish_workspace: PolishWorkspace, editable_pilot: Path
) -> None:
    """Discovery ran over changed items only, so a removal's dependents went unreported.

    A removal is the change most likely to break something the learner built: the item it
    points at disappears entirely rather than being reworded.
    """

    removed_id = str(content_id_for("pl-pilot", KNOWLEDGE_KIND, "pl.lex.remont"))
    card = _learner_card(polish_workspace, depends_on=removed_id, stable_key="learner.card.remont")
    _bump_version_and_change_an_item(editable_pilot, remove_key="pl.lex.remont")

    preview = pack_service.diff(
        polish_workspace.paths, editable_pilot, clock=polish_workspace.clock
    )

    removal = next(entry for entry in preview.removed if entry.stable_key == "pl.lex.remont")
    assert removal.dependents == (card,)
    assert card in preview.invalidated_learner_content


def test_applying_the_update_invalidates_what_the_removal_broke(
    polish_workspace: PolishWorkspace, editable_pilot: Path
) -> None:
    card = _learner_card(
        polish_workspace,
        depends_on=str(content_id_for("pl-pilot", KNOWLEDGE_KIND, "pl.lex.remont")),
        stable_key="learner.card.remont",
    )
    _bump_version_and_change_an_item(editable_pilot, remove_key="pl.lex.remont")

    pack_service.install(
        polish_workspace.paths, editable_pilot, clock=polish_workspace.clock, allow_update=True
    )

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        row = database.one(
            "SELECT lifecycle, invalidation_reason FROM content_records WHERE content_id = ?",
            [card],
        )
    assert row is not None
    assert str(row[0]) == "needs-review"
    assert "pl-pilot" in str(row[1])


def test_reinstalling_the_same_pack_changes_nothing(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path
) -> None:
    """A no-op reinstall used to rewrite every content row on the way to saying so.

    Rewriting is not free of consequence: it re-binds reviews and touches `updated_at`
    on rows learner state points at. So the honest answer is to verify and return.
    """

    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        before = database.query(
            "SELECT content_id, content_hash, updated_at FROM content_records ORDER BY content_id"
        )
        events_before = int(database.scalar("SELECT count(*) FROM domain_events"))

    again = pack_service.install(
        synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock
    )

    assert again.reinstalled is True
    assert again.dry_run is False
    assert any("already installed" in warning for warning in again.warnings)
    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        after = database.query(
            "SELECT content_id, content_hash, updated_at FROM content_records ORDER BY content_id"
        )
        events_after = int(database.scalar("SELECT count(*) FROM domain_events"))
    assert after == before
    assert events_after == events_before


def test_a_reinstall_from_a_different_directory_still_records_the_new_source(
    synthetic_workspace: SyntheticWorkspace, editable_pilot: Path, tmp_path: Path
) -> None:
    """Same contents, different directory: later commands re-resolve from the source path."""

    pack_service.install(synthetic_workspace.paths, editable_pilot, clock=synthetic_workspace.clock)
    moved = tmp_path / "moved-pilot"
    shutil.copytree(editable_pilot, moved)

    pack_service.install(synthetic_workspace.paths, moved, clock=synthetic_workspace.clock)

    with open_reader(synthetic_workspace.paths, clock=synthetic_workspace.clock) as database:
        recorded = database.scalar("SELECT source_path FROM pack_installations")
    assert str(recorded) == str(moved)


def test_a_shipped_pack_never_republishes_a_version_it_has_already_released() -> None:
    """A released pack version is content-addressed and immutable, like a migration file.

    Re-stamping a shipped pack without bumping its version leaves every existing install
    with no way forward: `install` refuses `pack_version_conflict` before the update gate,
    so `pack update` refuses identically, and both tell the operator to publish a new
    version of a pack they do not own. The refusal is correct; shipping the state that
    provokes it is not, and no other test can see it because every test installs into a
    fresh workspace where the conflict branch is unreachable.

    `snapshots/released-packs.json` is the record, exactly as
    `tests/migrations/snapshots/released-migrations.json` is for migrations: an entry is
    added when a version ships and is never removed or edited. Changing a shipped pack's
    contents means adding a version, not rewriting one.
    """

    recorded = json.loads((SNAPSHOTS / "released-packs.json").read_text(encoding="utf-8"))

    for shipped in (PILOT_PACK, FIXTURE_PACKS / "inflected", FIXTURE_PACKS / "tonal"):
        manifest = json.loads((shipped / "manifest.json").read_text(encoding="utf-8"))
        released = recorded.get(manifest["pack_key"], {})
        previous = released.get(manifest["version"])
        assert previous is None or previous == manifest["content_address"], (
            f"{manifest['pack_key']} {manifest['version']} was released with content "
            f"address {previous} and now declares {manifest['content_address']}; "
            "a published version is immutable, so bump the version instead"
        )
