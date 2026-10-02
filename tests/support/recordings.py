"""Synthetic recordings for the pilot's listening tasks.

The pilot ships none (C2a), so every test of playback builds a pack version that does. The
audio is generated: a few hundred milliseconds of a pure tone per task, a real WAV that a
browser can decode and play, and nothing anybody said. This repository holds no real
recording of any kind.
"""

from __future__ import annotations

import io
import json
import math
import shutil
import struct
import wave
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.conftest import PolishWorkspace

FORM = "assessments/pl-a2-calibration.json"
LISTENING_KEYS = tuple(f"pl.task.listening.0{index}" for index in range(1, 7))


def tone_wav(frequency: float, *, milliseconds: int = 300, rate: int = 8000) -> bytes:
    """A mono 16-bit PCM WAV of one sine tone."""

    frames = int(rate * milliseconds / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / rate)))
                for i in range(frames)
            )
        )
    return buffer.getvalue()


def recording_for(stable_key: str) -> bytes:
    """The bytes a listening task plays: a distinct tone per task, so no two share a hash."""

    return tone_wav(330.0 + 55.0 * LISTENING_KEYS.index(stable_key))


def add_recordings(root: Path, *, replay_allowance: int | None = 2) -> None:
    """Give every pilot listening task a generated recording to play."""

    (root / "media").mkdir(exist_ok=True)
    (root / "assets").mkdir(exist_ok=True)
    assets = []
    for stable_key in LISTENING_KEYS:
        name = stable_key.rsplit(".", 1)[1]
        (root / "media" / f"listening-{name}.wav").write_bytes(recording_for(stable_key))
        assets.append(
            {
                "asset_key": f"pl.audio.listening-{name}",
                "path": f"media/listening-{name}.wav",
                "media_type": "audio/wav",
                "duration_ms": 300,
                "transcript": "synthetic tone",
                "provenance": {
                    "origin_profile": "authored-original",
                    "review_profile": "authored-verified",
                    "lifecycle": "verified",
                    "risk_tier": 2,
                    "content_hash": "a" * 64,
                },
            }
        )
    (root / "assets" / "pl-a2-audio.json").write_text(
        json.dumps(
            {
                "schema_name": "lingua.pack.assets.v1",
                "schema_version": 1,
                "catalog_key": "pl-a2-audio",
                "assets": assets,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    path = root / FORM
    document = json.loads(path.read_text(encoding="utf-8"))
    for task in document["tasks"]:
        if task["stable_key"] in LISTENING_KEYS:
            name = task["stable_key"].rsplit(".", 1)[1]
            audio: dict[str, object] = {"asset_key": f"pl.audio.listening-{name}"}
            if replay_allowance is not None:
                audio["replay_allowance"] = replay_allowance
            task["presentation"] = {**task["presentation"], "audio": audio}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def publish_pilot_with_recordings(
    workspace: PolishWorkspace,
    tmp_path: Path,
    *,
    replay_allowance: int | None = 2,
    version: str | None = None,
) -> Path:
    """Install the next pilot version, identical but for generated listening recordings."""

    from linguawiki.packs.stamp import stamp_pack
    from linguawiki.services import packs as pack_service
    from tests.conftest import NEXT_PILOT_VERSION, PILOT_PACK
    from tests.language_packs.support import republish

    root = tmp_path / "pl-pilot-recorded"
    shutil.copytree(PILOT_PACK, root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version or NEXT_PILOT_VERSION
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    add_recordings(root, replay_allowance=replay_allowance)
    stamp_pack(root)
    republish(root)
    pack_service.install(workspace.paths, root, clock=workspace.clock, allow_update=True)
    return root
