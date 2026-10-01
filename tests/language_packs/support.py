"""Helpers for tests that build a pack variant on disk."""

from __future__ import annotations

import json
from pathlib import Path

from linguawiki.contracts import PackManifest
from linguawiki.packs.format import directory_digests, pack_content_address


def republish(root: Path) -> None:
    """Re-stamp the file digests and content address, leaving item hashes alone."""

    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = directory_digests(root)
    manifest["content_address"] = None
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    parsed = PackManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
