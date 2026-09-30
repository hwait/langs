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
from tests.conftest import PILOT_PACK
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
    _rewrite_catalog(pack, _catalog(path="listening-01.wav"))

    with pytest.raises(PackError) as failure:
        load_pack(pack)
    assert failure.value.payload.code == "pack_asset_misplaced"


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
    document = _catalog()
    document["assets"].append({**document["assets"][0], "path": "media/listening-02.wav"})
    _rewrite_catalog(pack, document)
    with pytest.raises(PackError) as failure:
        load_pack(pack)
    assert failure.value.payload.code == "pack_asset_duplicate"

    document = _catalog()
    document["assets"].append({**document["assets"][0], "asset_key": "pl.audio.listening-01b"})
    _rewrite_catalog(pack, document)
    with pytest.raises(PackError) as failure:
        load_pack(pack)
    assert failure.value.payload.code == "pack_asset_duplicate"


def test_a_pack_with_no_assets_directory_is_unaffected(tmp_path: Path) -> None:
    """Every shipped pack is this case, and none of them may start needing a catalog."""

    root = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, root)
    assert load_pack(root).assets == ()
