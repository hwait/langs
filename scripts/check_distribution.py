#!/usr/bin/env python3
"""Verify release artifacts contain the runtime and Stage 0 resource bundles."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEEL_RESOURCES = {
    "linguawiki/resources/config/privacy-policy.toml",
    "linguawiki/resources/schemas/lingua.workspace.v1.json",
    "linguawiki/resources/schemas/lingua.session.v1.json",
    "linguawiki/resources/schemas/lingua.content.v1.json",
    "linguawiki/resources/skills/linguawiki/SKILL.md",
    "linguawiki/resources/templates/learner-workspace/linguawiki.toml.j2",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    wheels = sorted(args.dist.glob("linguawiki-*.whl"))
    source_distributions = sorted(args.dist.glob("linguawiki-*.tar.gz"))
    if len(wheels) != 1 or len(source_distributions) != 1:
        print("expected exactly one LinguaWiki wheel and source distribution")
        return 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        missing = WHEEL_RESOURCES - names
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_path).decode("utf-8")
    if missing:
        print(f"wheel is missing resources: {sorted(missing)}")
        return 1
    if "License-Expression: MIT" not in metadata or len(metadata.encode("utf-8")) > 20_000:
        print("wheel metadata must contain the MIT license and a concise project README")
        return 1
    with tarfile.open(source_distributions[0], mode="r:gz") as archive:
        names = archive.getnames()
    required_sdist_suffixes = {
        "/.agents/skills/linguawiki/SKILL.md",
        "/LICENSE",
        "/README.md",
    }
    missing_sdist = {
        suffix
        for suffix in required_sdist_suffixes
        if not any(name.endswith(suffix) for name in names)
    }
    if missing_sdist:
        print(f"source distribution is missing files: {sorted(missing_sdist)}")
        return 1
    print("Distribution resource check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
