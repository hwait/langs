"""A pack that references a recording has to be able to produce it.

Before this there was no asset concept and no audio file in any pack, so "give the audio
tasks a clip reference" named a resolver that did not exist. A stable key alone is not
enough either: it can resolve to different bytes after an update, which is exactly the
guarantee the served snapshot is supposed to make -- the same recording, or a refusal.

Identity is therefore `(content id, sha256)`. The digest is not declared by the catalog:
the manifest already names every file in the pack at its exact digest and the loader
hashes them all, so a second declaration would be a second source of truth that can
disagree with the first.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.packs.format import PackError, load_pack
from linguawiki.packs.stamp import stamp_pack
from tests.conftest import PILOT_PACK, SyntheticWorkspace
from tests.language_packs.support import republish

CLIP = b"RIFF....WAVEfmt not really a wav, but bytes with a digest\n"
LISTENING_TASK = "pl.task.listening.01"


def _catalog(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "asset_key": "pl.audio.listening-01",
        "path": "media/listening-01.wav",
        "media_type": "audio/wav",
        "duration_ms": 3200,
        "transcript": "Poproszę herbatę i wodę.",
        "provenance": {
            "origin_profile": "authored-original",
            "review_profile": "authored-verified",
            "lifecycle": "verified",
            "risk_tier": 2,
            "content_hash": "a" * 64,
        },
    }
    entry.update(overrides)
    return {
        "schema_name": "lingua.pack.assets.v1",
        "schema_version": 1,
        "catalog_key": "pl-a2-audio",
        "assets": [entry],
    }


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    """`pl-pilot` with one recording, one catalog entry, and one task that plays it."""

    root = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, root)
    (root / "media").mkdir()
    (root / "media" / "listening-01.wav").write_bytes(CLIP)
    (root / "assets").mkdir()
    (root / "assets" / "pl-a2-audio.json").write_text(
        json.dumps(_catalog(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _play(root, LISTENING_TASK, {"asset_key": "pl.audio.listening-01", "replay_allowance": 3})
    stamp_pack(root)
    republish(root)
    return root


def _play(root: Path, stable_key: str, audio: dict[str, Any] | None) -> None:
    path = root / "assessments" / "pl-a2-calibration.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for task in document["tasks"]:
        if task["stable_key"] == stable_key:
            task["presentation"] = {"kind": "free-text", "audio": audio}
            break
    else:  # pragma: no cover - a typo in the test, not a behaviour
        raise AssertionError(f"{stable_key} is not in the pilot form")
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def _rewrite_catalog(root: Path, document: dict[str, Any]) -> None:
    (root / "assets" / "pl-a2-audio.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    stamp_pack(root)
    republish(root)


def test_an_asset_resolves_to_a_content_id_and_the_digest_of_its_bytes(pack: Path) -> None:
    import hashlib

    loaded = load_pack(pack)
    resolved = {asset.asset_key: asset for asset in loaded.assets}
    assert set(resolved) == {"pl.audio.listening-01"}
    entry = resolved["pl.audio.listening-01"]
    assert entry.sha256 == hashlib.sha256(CLIP).hexdigest()
    assert entry.sha256 == loaded.file_digests["media/listening-01.wav"]
    assert str(entry.content_id).startswith("cnt_")
    # The recording is an item like any other: it has an origin, a rights class, and a
    # review state, because what may be kept from somebody else's voice is decided by
    # the same machinery that decides it for their text.
    assert entry.item.content_kind == "asset"
    assert entry.item.hash_matches


def test_a_task_may_only_play_a_recording_the_pack_holds(pack: Path) -> None:
    _play(pack, LISTENING_TASK, {"asset_key": "pl.audio.nobody-recorded"})
    stamp_pack(pack)
    republish(pack)

    with pytest.raises(PackError) as failure:
        load_pack(pack)
    assert failure.value.payload.code == "pack_asset_unknown"


def test_a_catalog_entry_whose_file_is_absent_is_refused(pack: Path) -> None:
    """Both commands refuse it, by the same name.

    A reviewer told "valid" by one and refused by the other has been told the opposite
    of the truth, and hashing a file is the first thing `pack stamp` does with an asset
    -- so without this it was a `FileNotFoundError` out of a command whose whole job is
    to report on a pack.
    """

    (pack / "assets" / "pl-a2-audio.json").write_text(
        json.dumps(_catalog(path="media/never-recorded.wav"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    republish(pack)

    with pytest.raises(LinguaWikiError) as stamping:
        stamp_pack(pack)
    assert stamping.value.payload.code == "pack_asset_missing"

    with pytest.raises(PackError) as loading:
        load_pack(pack)
    assert loading.value.payload.code == "pack_asset_missing"


def test_a_recording_must_live_under_the_media_root(pack: Path) -> None:
    """Role is assigned by declared path everywhere else in a pack; media is no different.

    It also keeps a catalog out of its own asset list: `assets/` holds JSON the loader
    parses, and a recording there would be read as a catalog.
    """

    shutil.move(pack / "media" / "listening-01.wav", pack / "listening-01.wav")
    _rewrite_catalog_only(pack, _catalog(path="listening-01.wav"))
    republish(pack)

    with pytest.raises(LinguaWikiError) as stamping:
        stamp_pack(pack)
    assert stamping.value.payload.code == "pack_asset_misplaced"
    with pytest.raises(PackError) as loading:
        load_pack(pack)
    assert loading.value.payload.code == "pack_asset_misplaced"


def test_a_recording_that_is_a_symlink_out_of_the_pack_is_refused(
    pack: Path, tmp_path: Path
) -> None:
    """Containment is resolved, not lexical: the path holds no `..` at all.

    The bytes would still match the manifest -- a symlink is read through -- so checksum
    coverage cannot see this. A pack that is not self-contained does not survive being
    copied, and what it points at is outside anything the pack's rights class covers.
    """

    outside = tmp_path / "somewhere-else.wav"
    outside.write_bytes(CLIP)
    target = pack / "media" / "listening-01.wav"
    target.unlink()
    target.symlink_to(outside)
    republish(pack)

    with pytest.raises(PackError) as failure:
        load_pack(pack)
    assert failure.value.payload.code == "pack_asset_not_contained"


def test_one_recording_answers_to_one_key_and_one_key_to_one_recording(pack: Path) -> None:
    """Uniqueness has as many forms as the thing has identities."""

    (pack / "media" / "listening-02.wav").write_bytes(CLIP + b"different")
    for duplicate in ({"path": "media/listening-02.wav"}, {"asset_key": "pl.audio.listening-01b"}):
        document = _catalog()
        document["assets"].append({**document["assets"][0], **duplicate})
        _rewrite_catalog_only(pack, document)
        republish(pack)
        # Both commands, by the same name: a reviewer told "valid" by one and refused by
        # the other has been told the opposite of the truth.
        with pytest.raises(LinguaWikiError) as stamping:
            stamp_pack(pack)
        assert stamping.value.payload.code == "pack_asset_duplicate"
        with pytest.raises(PackError) as loading:
            load_pack(pack)
        assert loading.value.payload.code == "pack_asset_duplicate"


def test_a_pack_with_no_assets_directory_is_unaffected(tmp_path: Path) -> None:
    """Every shipped pack is this case, and none of them may start needing a catalog."""

    root = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, root)
    assert load_pack(root).assets == ()


def test_a_pack_that_ships_a_recording_installs(
    pack: Path, synthetic_workspace: SyntheticWorkspace
) -> None:
    """`pack validate` and `pack install` have to agree about what a pack is.

    An asset is a pack item: it is hashed, gate-checked and refused like any other. It is
    not learner content, and `content_records` has a closed vocabulary that does not hold
    it -- so installing one raised a raw `ConstraintException` out of DuckDB, with no
    code, after `validate` and `install --dry-run` had both called the pack good.
    """

    from linguawiki.db.connection import open_reader
    from linguawiki.services import packs as pack_service

    paths = synthetic_workspace.paths
    clock = synthetic_workspace.clock

    preview = pack_service.install(paths, pack, clock=clock, dry_run=True)
    installed = pack_service.install(paths, pack, clock=clock)

    assert installed.pack_key == "pl-pilot"
    # Neither the preview nor the install may claim a recording became learner content.
    assert "asset" not in installed.item_counts
    assert preview.diff is not None
    assert not [change for change in preview.diff.added if change.content_kind == "asset"]
    with open_reader(paths, clock=clock) as database:
        assert (
            int(
                database.scalar("SELECT count(*) FROM content_records WHERE content_kind = 'asset'")
            )
            == 0
        )
        assert int(database.scalar("SELECT count(*) FROM assessment_tasks")) == 39


def test_stamping_refuses_a_path_that_escapes_the_pack(pack: Path, tmp_path: Path) -> None:
    """`pack stamp` resolves a path exactly as `load_pack` does, or it is the way in.

    The loader refuses a symlink out of the pack; stamping hashed straight through it,
    so the digest of a file outside the pack -- any file the process can read, since
    `Path.__truediv__` with an absolute string discards the root -- was written into the
    pack's own item hash.
    """

    outside = tmp_path / "somewhere-else.wav"
    outside.write_bytes(CLIP + b"outside")
    target = pack / "media" / "listening-01.wav"
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(LinguaWikiError) as failure:
        stamp_pack(pack)
    assert failure.value.payload.code == "pack_asset_not_contained"


def test_stamping_refuses_an_absolute_path(pack: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("not this pack's", encoding="utf-8")
    _rewrite_catalog_only(pack, _catalog(path=str(secret)))

    with pytest.raises(LinguaWikiError) as failure:
        stamp_pack(pack)
    assert failure.value.payload.code in {"pack_asset_misplaced", "pack_asset_not_contained"}


def _rewrite_catalog_only(root: Path, document: dict[str, Any]) -> None:
    (root / "assets" / "pl-a2-audio.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_an_unreadable_recording_is_reported_by_both_commands(pack: Path) -> None:
    """Cannot-be-read is an answer, and it is the same answer from both commands.

    The loader hashes every file in the directory, so an unreadable one surfaced as a
    bare `PermissionError` out of `pack validate` -- a command whose whole job is to
    report on a pack. `pack stamp` already reported it; the rule went in one of the two
    places that needed it.
    """

    target = pack / "media" / "listening-01.wav"
    target.chmod(0o000)
    try:
        with pytest.raises(LinguaWikiError) as loading:
            load_pack(pack)
        with pytest.raises(LinguaWikiError) as stamping:
            stamp_pack(pack)
    finally:
        target.chmod(0o644)

    assert loading.value.payload.code == "pack_file_unreadable"
    assert stamping.value.payload.code == "pack_file_unreadable"


def test_replacing_a_recording_restamps_the_task_that_plays_it(pack: Path) -> None:
    """The recording *is* the question a listening task asks.

    Covering the digest in the asset's own hash detaches the asset's reviews, which is
    right and is not enough: a pack author could swap the voice, and the task keeping its
    hash kept its `authored-verified` review on a question nobody had heard.
    """

    def hashes() -> dict[tuple[str, str], str]:
        return {
            (item.content_kind, item.stable_key): item.content_hash
            for item in load_pack(pack).items
        }

    before = hashes()
    (pack / "media" / "listening-01.wav").write_bytes(CLIP + b" a different voice")
    stamp_pack(pack)
    republish(pack)
    after = hashes()

    assert before[("asset", "pl.audio.listening-01")] != after[("asset", "pl.audio.listening-01")]
    assert before[("assessment_task", LISTENING_TASK)] != after[("assessment_task", LISTENING_TASK)]
    # And only those two: nothing else in the pack depends on this recording.
    moved = {key for key in before if before[key] != after[key]}
    assert moved == {("asset", "pl.audio.listening-01"), ("assessment_task", LISTENING_TASK)}


def test_a_new_recording_can_be_stamped_without_inventing_its_hash(
    pack: Path, tmp_path: Path
) -> None:
    """`pack stamp` is what *produces* a content hash; it cannot require one first.

    A task's hash covers the recording it plays, so stamping an assessment file reads
    the asset catalogs -- and reading them strictly meant an author adding a recording
    had to write a plausible sha256 into its provenance before the tool that computes
    hashes would run. The stamper already substitutes a placeholder for the items it is
    stamping; catalog discovery has to do the same, while `load_pack` stays strict.
    """

    catalog = _catalog()
    del catalog["assets"][0]["provenance"]["content_hash"]
    _rewrite_catalog_only(pack, catalog)
    republish(pack)

    stamp_pack(pack)
    republish(pack)

    loaded = load_pack(pack)
    assert [asset.asset_key for asset in loaded.assets] == ["pl.audio.listening-01"]
    assert all(item.hash_matches for item in loaded.items)


def test_loading_still_refuses_a_catalog_that_declares_no_hash(pack: Path) -> None:
    """Placeholder tolerance belongs to stamping alone."""

    catalog = _catalog()
    del catalog["assets"][0]["provenance"]["content_hash"]
    _rewrite_catalog_only(pack, catalog)
    republish(pack)

    with pytest.raises(PackError) as failure:
        load_pack(pack)
    assert failure.value.payload.code == "pack_contract_invalid"
