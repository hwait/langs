"""Deterministic hashes that pin core, database schema, and skill bundle versions."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from linguawiki import __version__, resources
from linguawiki.contracts import LockManifest, VersionPin

CORE_SOURCE_GLOB = "**/*.py"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(entries: Iterable[tuple[str, str]]) -> str:
    """Hash an ordered (relative path, file hash) listing so trees compare exactly."""

    digest = hashlib.sha256()
    for relative_path, content_hash in sorted(entries):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_hash.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def tree_entries(root: Path, *, pattern: str = "**/*", skip: Iterable[str] = ()) -> dict[str, str]:
    """List hashable files below a root, ignoring caches and generated markers."""

    skipped = set(skip)
    entries: dict[str, str] = {}
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in skipped or "__pycache__" in path.parts or path.name.startswith("."):
            continue
        entries[relative] = file_sha256(path)
    return entries


def core_source_hash() -> str:
    """Hash the installed core Python sources so a changed core is detectable."""

    return tree_sha256(tree_entries(resources.package_root(), pattern=CORE_SOURCE_GLOB).items())


def core_pin() -> VersionPin:
    return VersionPin(version=__version__, sha256=core_source_hash())


def skill_bundle_entries() -> dict[str, str]:
    return tree_entries(resources.skill_bundle_directory())


def skill_bundle_hash() -> str:
    return tree_sha256(skill_bundle_entries().items())


def skill_bundle_pin() -> VersionPin:
    return VersionPin(version=__version__, sha256=skill_bundle_hash())


def lock_manifest(*, database_schema: VersionPin) -> LockManifest:
    return LockManifest(
        core=core_pin(),
        database_schema=database_schema,
        skill_bundle=skill_bundle_pin(),
    )
