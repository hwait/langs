"""Recompute the `content_hash` a pack file declares for each of its items.

A pack is content-addressed, so an item's hash is *derived*, never authored: the author
edits the item and then stamps, and `pack validate` refuses a pack whose declared hashes
do not match its contents. Stamping is therefore deliberate -- editing an item without
re-stamping fails validation rather than silently re-binding that item's reviews to text
nobody reviewed.

The stamper cannot use the ordinary loader, because the loader refuses a pack with a
stale hash. It re-reads the raw JSON, substitutes a placeholder hash so the contract
models validate, and resolves each item through `format.item_content_hash` -- the same
code path the loader uses -- so the two can never compute a different answer. Because the
hash covers the whole payload, the stamper needs only the parsed item and the header of
the file that frames it; it never reconstructs a title or a body, which is what let the
two drift apart before.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from linguawiki.contracts import (
    PackActivityFile,
    PackAssessmentFile,
    PackAssetFile,
    PackBundleFile,
    PackExample,
    PackItemProvenance,
    PackKnowledgeItem,
    PackManifest,
    PackProficiencyFile,
    PackReferencesFile,
)
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.packs import format as pack_format
from linguawiki.versions import file_sha256

PLACEHOLDER_HASH = "0" * 64

#: Each JSONL seed file: the model of one line, and the content kind it becomes.
JSONL_ITEMS: dict[str, tuple[type[BaseModel], str]] = {
    pack_format.KNOWLEDGE_SEED: (PackKnowledgeItem, pack_format.KNOWLEDGE_KIND),
    pack_format.EXAMPLES_SEED: (PackExample, pack_format.EXAMPLE_KIND),
}
#: Each JSON pack file: the file's model, the field holding its items, and the content
#: kind those items become.
JSON_ITEMS: tuple[tuple[str, type[BaseModel], str, str], ...] = (
    (
        pack_format.PROFICIENCY_PREFIX,
        PackProficiencyFile,
        "descriptors",
        pack_format.DESCRIPTOR_KIND,
    ),
    (pack_format.ASSESSMENTS_PREFIX, PackAssessmentFile, "tasks", pack_format.TASK_KIND),
    (pack_format.ASSETS_PREFIX, PackAssetFile, "assets", pack_format.ASSET_KIND),
    (pack_format.ACTIVITIES_PREFIX, PackActivityFile, "templates", pack_format.ACTIVITY_KIND),
    (
        pack_format.REFERENCES_PREFIX,
        PackReferencesFile,
        "recommendations",
        pack_format.RECOMMENDATION_KIND,
    ),
)
STAMPABLE_JSON_PREFIXES = (
    *(prefix for prefix, _model, _collection, _kind in JSON_ITEMS),
    pack_format.BUNDLES_PREFIX,
)


@dataclass(frozen=True, slots=True)
class StampedItem:
    relative_path: str
    stable_key: str
    previous_hash: str
    content_hash: str

    @property
    def changed(self) -> bool:
        return self.previous_hash != self.content_hash


@dataclass(frozen=True, slots=True)
class StampReport:
    pack: str
    pack_key: str
    items: tuple[StampedItem, ...]
    written: tuple[str, ...]

    @property
    def stale(self) -> tuple[StampedItem, ...]:
        return tuple(item for item in self.items if item.changed)


def _with_placeholder(record: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(record.get("provenance") or {})
    provenance["content_hash"] = PLACEHOLDER_HASH
    return {**record, "provenance": provenance}


def _load_manifest(root: Path) -> PackManifest:
    path = root / pack_format.MANIFEST_NAME
    if not path.is_file():
        raise LinguaWikiError(
            "pack_manifest_missing", f"{root} does not contain {pack_format.MANIFEST_NAME}"
        )
    return PackManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _pack_files(root: Path) -> Iterator[str]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != pack_format.MANIFEST_NAME:
            yield path.relative_to(root).as_posix()


def _identity(item: BaseModel) -> tuple[str, PackItemProvenance]:
    """One item's stable key and provenance, whichever field name it uses for the key."""

    key = (
        getattr(item, "stable_key", None)
        or getattr(item, "bundle_key", None)
        or getattr(item, "asset_key", None)
    )
    provenance = getattr(item, "provenance", None)
    assert key is not None and isinstance(provenance, PackItemProvenance), type(item).__name__
    return str(key), provenance


def _stamp_one(
    manifest: PackManifest,
    *,
    record: dict[str, Any],
    item: BaseModel,
    relative: str,
    content_kind: str,
    context: dict[str, Any] | None = None,
) -> StampedItem:
    stable_key, provenance = _identity(item)
    computed = pack_format.item_content_hash(
        manifest,
        content_kind=content_kind,
        stable_key=stable_key,
        provenance=provenance,
        payload=item,
        where=relative,
        context=context,
    )
    previous = str(record.get("provenance", {}).get("content_hash", ""))
    record["provenance"]["content_hash"] = computed
    return StampedItem(
        relative_path=relative,
        stable_key=stable_key,
        previous_hash=previous,
        content_hash=computed,
    )


def _stamp_jsonl(
    root: Path, manifest: PackManifest, relative: str
) -> tuple[list[StampedItem], str]:
    model, content_kind = JSONL_ITEMS[relative]
    stamped: list[StampedItem] = []
    output: list[str] = []
    for line in (root / relative).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        item = model.model_validate(_with_placeholder(record))
        stamped.append(
            _stamp_one(
                manifest,
                record=record,
                item=item,
                relative=relative,
                content_kind=content_kind,
            )
        )
        output.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return stamped, "\n".join(output) + "\n"


def _asset_digest(root: Path, relative: str, *, where: str) -> str:
    """Hash the bytes an asset names, or refuse by the name `pack validate` uses.

    Reading a file can fail, and "cannot be read" is an answer rather than an error to
    propagate: this is the first thing stamping does with an asset, so an unreadable or
    absent recording surfaced as a bare `OSError` out of a command whose whole job is
    to report on a pack.
    """

    try:
        return file_sha256(root / relative)
    except OSError as exc:
        raise LinguaWikiError(
            "pack_asset_missing",
            f"{where} names a recording that cannot be read",
            details=(ErrorDetail(field=relative, reason=str(exc)),),
        ) from exc


def _stamp_json(root: Path, manifest: PackManifest, relative: str) -> tuple[list[StampedItem], str]:
    path = root / relative
    document = json.loads(path.read_text(encoding="utf-8"))
    entry = next((spec for spec in JSON_ITEMS if relative.startswith(spec[0])), None)
    if entry is None:
        bundle = PackBundleFile.model_validate(_with_placeholder(document))
        stamped = [
            _stamp_one(
                manifest,
                record=document,
                item=bundle,
                relative=relative,
                content_kind=pack_format.BUNDLE_KIND,
            )
        ]
    else:
        _prefix, model, collection, content_kind = entry
        records = document.get(collection, [])
        parsed = model.model_validate(
            {**document, collection: [_with_placeholder(record) for record in records]}
        )
        file_header = pack_format.file_context(parsed, collection)
        items = getattr(parsed, collection)
        # An asset's hash covers the bytes it names, so its context is per item rather
        # than per file. Both the digest and its shape come from `pack_format`, because
        # a second derivation here is a pack that is stale the moment it is stamped.
        contexts = [
            pack_format.asset_context(file_header, _asset_digest(root, item.path, where=relative))
            if content_kind == pack_format.ASSET_KIND
            else file_header
            for item in items
        ]
        stamped = [
            _stamp_one(
                manifest,
                record=record,
                item=item,
                relative=relative,
                content_kind=content_kind,
                context=context,
            )
            for record, item, context in zip(records, items, contexts, strict=True)
        ]
    return stamped, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def stamp_pack(root: str | Path, *, write: bool = True) -> StampReport:
    """Recompute every declared content hash in a pack directory."""

    directory = Path(str(root)).expanduser().resolve()
    manifest = _load_manifest(directory)
    items: list[StampedItem] = []
    written: list[str] = []
    for relative in _pack_files(directory):
        if relative in JSONL_ITEMS:
            stamped, text = _stamp_jsonl(directory, manifest, relative)
        elif relative.startswith(STAMPABLE_JSON_PREFIXES):
            stamped, text = _stamp_json(directory, manifest, relative)
        else:
            continue
        items.extend(stamped)
        path = directory / relative
        if path.read_text(encoding="utf-8") != text:
            if write:
                path.write_text(text, encoding="utf-8")
            written.append(relative)
    report = StampReport(
        pack=str(directory),
        pack_key=manifest.pack_key,
        items=tuple(items),
        written=tuple(written),
    )
    if not write and report.stale:
        raise LinguaWikiError(
            "pack_hashes_stale",
            f"{manifest.pack_key} declares content hashes its contents do not produce",
            details=tuple(
                ErrorDetail(
                    field=item.relative_path,
                    reason=f"{item.stable_key} declares {item.previous_hash[:12]}, computed "
                    f"{item.content_hash[:12]}",
                )
                for item in report.stale[:20]
            ),
        )
    return report


__all__ = ["StampReport", "StampedItem", "stamp_pack"]
