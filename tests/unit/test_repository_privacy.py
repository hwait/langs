from pathlib import Path

import pytest

from linguawiki.repository_policy import load_privacy_policy, parse_nul_paths, privacy_violation

ROOT = Path(__file__).resolve().parents[2]
POLICY = load_privacy_policy(ROOT / "config" / "privacy-policy.toml")


def test_private_artifact_paths_are_rejected() -> None:
    assert privacy_violation(Path("data/learner.duckdb"), POLICY) is not None
    assert privacy_violation(Path("data/learner.duckdb.wal"), POLICY) is not None
    assert privacy_violation(Path("recordings/lesson.wav"), POLICY) is not None
    assert privacy_violation(Path("nested/transcripts/raw/session.json"), POLICY) is not None
    assert privacy_violation(Path("Artifacts/export.bin"), POLICY) is not None
    assert privacy_violation(Path("Recordings/lesson.json"), POLICY) is not None
    assert privacy_violation(Path("nested/Transcripts/Raw/session.json"), POLICY) is not None


@pytest.mark.parametrize("suffix", [".parquet", ".apkg", ".opus", ".webm", ".mp4"])
def test_portable_exports_and_additional_media_are_rejected(suffix: str) -> None:
    assert privacy_violation(Path(f"nested/private{suffix}"), POLICY) is not None


def test_synthetic_json_fixture_is_allowed() -> None:
    assert privacy_violation(Path("tests/fixtures/session-package/package.json"), POLICY) is None


def test_privacy_policy_is_language_agnostic() -> None:
    serialized = (ROOT / "config" / "privacy-policy.toml").read_text(encoding="utf-8")

    assert "Polish" not in serialized
    assert "Chinese" not in serialized


def test_nul_path_parser_preserves_embedded_newlines() -> None:
    assert parse_nul_paths(b"normal.txt\0odd\nname.duckdb.wal\0") == [
        Path("normal.txt"),
        Path("odd\nname.duckdb.wal"),
    ]
