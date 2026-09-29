"""Install and verify the generated Codex skill snapshot inside a workspace."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pydantic import Field

from linguawiki import resources
from linguawiki.clock import Clock
from linguawiki.contracts import LockManifest
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths, assert_within
from linguawiki.versions import skill_bundle_hash, skill_bundle_pin, tree_entries, tree_sha256

GENERATED_MARKER_NAME = ".linguawiki-generated.json"


class SkillBundleReport(ContractModel):
    """State of the committed, generated skill snapshot."""

    root: str
    installed: bool
    version: str | None
    installed_sha256: str | None
    packaged_version: str
    packaged_sha256: str
    matches_installed_core: bool
    matches_lock: bool | None
    file_count: int
    modified: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    warnings: tuple[str, ...] = Field(default=())


def _marker_payload(target: Path) -> dict[str, str] | None:
    path = target / GENERATED_MARKER_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def install_bundle(paths: WorkspacePaths, *, clock: Clock) -> SkillBundleReport:
    """Replace the generated snapshot deterministically from the pinned core bundle."""

    target = assert_within(paths.skills, paths.root, purpose="skill bundle")
    source = resources.skill_bundle_directory()
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    pin = skill_bundle_pin()
    (target / GENERATED_MARKER_NAME).write_text(
        json.dumps(
            {
                "generated": True,
                "generator": "linguawiki skills install",
                "skill_bundle_version": pin.version,
                "skill_bundle_sha256": pin.sha256,
                "generated_at": clock.now().isoformat().replace("+00:00", "Z"),
                "note": "Generated from the pinned core release. Do not edit by hand.",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return inspect_bundle(paths)


def inspect_bundle(paths: WorkspacePaths, *, lock: LockManifest | None = None) -> SkillBundleReport:
    """Compare the installed snapshot with the packaged bundle and the lock file."""

    target = paths.skills
    packaged = skill_bundle_pin()
    if not target.is_dir():
        return SkillBundleReport(
            root=str(target),
            installed=False,
            version=None,
            installed_sha256=None,
            packaged_version=packaged.version,
            packaged_sha256=packaged.sha256,
            matches_installed_core=False,
            matches_lock=None if lock is None else False,
            file_count=0,
            warnings=("the generated skill snapshot is not installed",),
        )
    installed_entries = tree_entries(target)
    packaged_entries = tree_entries(resources.skill_bundle_directory())
    installed_hash = tree_sha256(installed_entries.items())
    modified = tuple(
        sorted(
            name
            for name, digest in packaged_entries.items()
            if name in installed_entries and installed_entries[name] != digest
        )
    )
    missing = tuple(sorted(set(packaged_entries) - set(installed_entries)))
    unexpected = tuple(sorted(set(installed_entries) - set(packaged_entries)))
    marker = _marker_payload(target)
    warnings: list[str] = []
    if marker is None:
        warnings.append("the generated-snapshot marker is missing")
    if modified or missing or unexpected:
        warnings.append("the generated skill snapshot was modified after installation")
    return SkillBundleReport(
        root=str(target),
        installed=True,
        version=None if marker is None else str(marker.get("skill_bundle_version")),
        installed_sha256=installed_hash,
        packaged_version=packaged.version,
        packaged_sha256=packaged.sha256,
        matches_installed_core=installed_hash == skill_bundle_hash(),
        matches_lock=None if lock is None else installed_hash == lock.skill_bundle.sha256,
        file_count=len(installed_entries),
        modified=modified,
        missing=missing,
        unexpected=unexpected,
        warnings=tuple(warnings),
    )
