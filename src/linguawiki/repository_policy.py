"""Language-agnostic repository privacy policy and Git-ignore rendering."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path, PurePosixPath

from pydantic import Field

from linguawiki.models import ContractModel

GENERATED_START = "# BEGIN GENERATED LINGUAWIKI PRIVACY RULES"
GENERATED_END = "# END GENERATED LINGUAWIKI PRIVACY RULES"


class PrivacyPolicy(ContractModel):
    version: int = Field(ge=1)
    forbidden_filename_suffixes: tuple[str, ...]
    forbidden_directory_names: tuple[str, ...]
    forbidden_path_sequences: tuple[str, ...]


def load_privacy_policy(path: Path) -> PrivacyPolicy:
    return PrivacyPolicy.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def privacy_violation(path: PurePosixPath, policy: PrivacyPolicy) -> str | None:
    lowered_name = path.name.lower()
    suffixes = tuple(suffix.lower() for suffix in policy.forbidden_filename_suffixes)
    if any(lowered_name.endswith(suffix) for suffix in suffixes):
        return "private database or media extension"
    lowered_parts = tuple(part.lower() for part in path.parts)
    directory_names = {name.lower() for name in policy.forbidden_directory_names}
    if directory_names.intersection(lowered_parts):
        return "learner/private artifact directory"
    for sequence in policy.forbidden_path_sequences:
        expected = tuple(part.lower() for part in PurePosixPath(sequence).parts)
        window = len(expected)
        if any(
            lowered_parts[index : index + window] == expected for index in range(len(lowered_parts))
        ):
            return "private path sequence"
    return None


def gitignore_patterns(policy: PrivacyPolicy) -> tuple[str, ...]:
    suffixes = (f"*{suffix}" for suffix in policy.forbidden_filename_suffixes)
    directories = (f"{name}/" for name in policy.forbidden_directory_names)
    sequences = (f"**/{sequence}/" for sequence in policy.forbidden_path_sequences)
    return tuple(sorted({*suffixes, *directories, *sequences}))


def render_generated_gitignore(policy: PrivacyPolicy) -> str:
    lines = [GENERATED_START, *gitignore_patterns(policy), GENERATED_END]
    return "\n".join(lines)


def parse_nul_paths(output: bytes) -> list[PurePosixPath]:
    """Preserve spaces and newlines in Git's NUL-delimited path output."""

    return [PurePosixPath(os.fsdecode(raw)) for raw in output.split(b"\0") if raw]
