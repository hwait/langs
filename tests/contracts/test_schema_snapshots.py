from pathlib import Path

from linguawiki.schema_snapshots import rendered_schemas

ROOT = Path(__file__).resolve().parents[2]


def test_checked_in_schemas_match_models() -> None:
    for path, expected in rendered_schemas(ROOT / "schemas").items():
        assert path.read_text(encoding="utf-8") == expected, path.relative_to(ROOT)
