from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from linguawiki.contracts import (
    ContentItem,
    SessionPackage,
    WorkspaceManifest,
    canonical_content_hash,
)

ROOT = Path(__file__).resolve().parents[2]


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", ["inflected", "tonal"])
def test_contrasting_content_fixtures_share_one_contract(fixture: str) -> None:
    payload = load_json(ROOT / "language-packs" / "fixtures" / fixture / "content.json")

    content = ContentItem.model_validate(payload)

    assert content.provenance.privacy == "synthetic"


def test_session_fixture_preserves_layers_and_audio_evidence() -> None:
    payload = load_json(ROOT / "tests" / "fixtures" / "session-package" / "package.json")

    package = SessionPackage.model_validate(payload)

    assert [layer.kind for layer in package.transcript_layers] == ["raw", "normalized"]
    assert package.artifacts[0].retained is False


def test_confirmed_pronunciation_without_audio_is_rejected() -> None:
    payload = load_json(ROOT / "tests" / "fixtures" / "session-package" / "package.json")
    payload["artifacts"] = []

    with pytest.raises(ValidationError, match="artifact reference must resolve"):
        SessionPackage.model_validate(payload)


def test_unconfirmed_pronunciation_artifact_reference_must_resolve() -> None:
    payload = load_json(ROOT / "tests" / "fixtures" / "session-package" / "package.json")
    payload["events"][0]["payload"]["status"] = "uncertain"
    payload["events"][0]["payload"]["audio_artifact_id"] = "art_01BX5ZZKBKACTAV9WEVGEMMVRZ"

    with pytest.raises(ValidationError, match="artifact reference must resolve"):
        SessionPackage.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["transcript_layers"][1].update(derived_from="normalized"),
            "cannot derive from itself",
        ),
        (
            lambda payload: payload["transcript_layers"][0]["utterances"][0].update(
                started_at="2026-01-02T09:59:59Z"
            ),
            "within session bounds",
        ),
        (
            lambda payload: payload["events"][0].update(occurred_at="2026-01-02T10:02:01Z"),
            "within session bounds",
        ),
        (
            lambda payload: payload["events"][0]["payload"].update(utterance_id="utt_missing"),
            "must resolve in the raw transcript",
        ),
        (
            lambda payload: payload["transcript_layers"][1]["utterances"][0].update(
                utterance_id="utt_missing"
            ),
            "must resolve in their source layer",
        ),
    ],
)
def test_session_cross_reference_and_chronology_rules(mutate: object, message: str) -> None:
    payload = load_json(ROOT / "tests" / "fixtures" / "session-package" / "package.json")
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(ValidationError, match=message):
        SessionPackage.model_validate(payload)


def test_derived_layer_must_reference_an_existing_layer() -> None:
    payload = load_json(ROOT / "tests" / "fixtures" / "session-package" / "package.json")
    reviewed = deepcopy(payload["transcript_layers"][1])
    reviewed["kind"] = "reviewed-hearing"
    reviewed["derived_from"] = "normalized"
    payload["transcript_layers"] = [payload["transcript_layers"][0], reviewed]

    with pytest.raises(ValidationError, match="must reference a transcript layer"):
        SessionPackage.model_validate(payload)


def test_unknown_session_event_kind_is_rejected() -> None:
    payload = load_json(ROOT / "tests" / "fixtures" / "session-package" / "package.json")
    payload["events"][0]["kind"] = "provider.magic"

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        SessionPackage.model_validate(payload)


def test_verified_content_requires_current_linguistic_and_pedagogical_reviews() -> None:
    payload = load_json(ROOT / "language-packs" / "fixtures" / "inflected" / "content.json")
    payload["reviews"] = []

    with pytest.raises(ValidationError, match="lacks passed reviews"):
        ContentItem.model_validate(payload)


def test_completed_review_must_bind_to_current_content_hash() -> None:
    payload = load_json(ROOT / "language-packs" / "fixtures" / "inflected" / "content.json")
    payload["reviews"][0]["reviewed_content_hash"] = "f" * 64

    with pytest.raises(ValidationError, match="bind to the current content_hash"):
        ContentItem.model_validate(payload)


def test_content_hash_is_recomputed_from_canonical_content() -> None:
    payload = load_json(ROOT / "language-packs" / "fixtures" / "inflected" / "content.json")
    assert payload["content_hash"] == canonical_content_hash(payload)
    payload["body"] = "Changed after review"

    with pytest.raises(ValidationError, match="canonical content JSON v1"):
        ContentItem.model_validate(payload)


def test_promoted_high_risk_content_requires_source_alignment() -> None:
    payload = load_json(ROOT / "language-packs" / "fixtures" / "inflected" / "content.json")
    payload["risk_tier"] = 3
    payload["content_hash"] = canonical_content_hash(payload)
    for review in payload["reviews"]:
        review["reviewed_content_hash"] = payload["content_hash"]

    with pytest.raises(ValidationError, match="source-alignment"):
        ContentItem.model_validate(payload)


def test_workspace_requires_utc_and_real_iana_timezone() -> None:
    payload = {
        "workspace_id": "wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "created_at": "2026-01-01T00:00:00Z",
        "timezone": "Europe/Warsaw",
        "backup_root": "/example/external-backup",
        "runtime": {"python": ">=3.12", "core_version": "0.1.0"},
    }

    workspace = WorkspaceManifest.model_validate(payload)

    assert workspace.timezone == "Europe/Warsaw"


def test_workspace_rejects_naive_timestamp() -> None:
    payload = {
        "workspace_id": "wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "created_at": "2026-01-01T00:00:00",
        "timezone": "Europe/Warsaw",
        "backup_root": "/example/external-backup",
        "runtime": {"python": ">=3.12", "core_version": "0.1.0"},
    }

    with pytest.raises(ValidationError, match="timezone-aware"):
        WorkspaceManifest.model_validate(payload)
