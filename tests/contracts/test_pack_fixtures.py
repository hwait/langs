import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def manifest(name: str) -> dict[str, object]:
    path = ROOT / "language-packs" / "fixtures" / name / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_contrasting_packs_share_dimensions_and_differ_in_structure() -> None:
    inflected = manifest("inflected")
    tonal = manifest("tonal")

    assert inflected["dimensions"] == tonal["dimensions"]
    assert inflected["frameworks"] == tonal["frameworks"]
    assert inflected["capabilities"] != tonal["capabilities"]
    assert inflected["language"] != tonal["language"]


def test_fixture_packs_are_explicitly_synthetic_and_non_production() -> None:
    for name in ("inflected", "tonal"):
        pack = manifest(name)
        assert pack["maturity"] == "fixture"
        content_path = ROOT / "language-packs" / "fixtures" / name / "content.json"
        content = json.loads(content_path.read_text(encoding="utf-8"))
        assert content["provenance"]["origin"] == "synthetic"
        assert content["provenance"]["privacy"] == "synthetic"
