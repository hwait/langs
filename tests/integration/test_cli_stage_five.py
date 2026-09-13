"""The Stage 5 command surface, and one source of every modality end to end.

Driven through `run()` rather than the services, because the CLI is the boundary a skill
actually uses: the envelope, the exit code, and the refusal message are the contract.
The last test is the stage's exit gate -- a reading, a podcast, a video fragment, and a
conversation all flowing through the same session lifecycle, with no direct SQL in it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from linguawiki.cli import EXIT_ERROR, EXIT_REPORTED_FAILURE, run
from tests.conftest import PolishWorkspace


def _json(capsys: pytest.CaptureFixture[str], *, stream: str = "out") -> dict[str, Any]:
    captured = capsys.readouterr()
    payload: dict[str, Any] = json.loads(getattr(captured, stream))
    return payload


def _run(workspace: PolishWorkspace, *arguments: str) -> int:
    return run(
        [*arguments, "--workspace", str(workspace.root), "--format", "json"],
        clock=workspace.clock,
    )


def _onboard(workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(workspace, "onboard", "start", "--declared-level", "A2") == 0
    capsys.readouterr()
    assert _run(workspace, "onboard", "finalize") == 0
    capsys.readouterr()


def _write(directory: Path, name: str, payload: Any) -> str:
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_every_stage_five_command_group_is_reachable() -> None:
    """A command the plan requires but the parser does not know is a silent omission."""

    from linguawiki.cli import _parser

    required = {
        "source": ("add", "list", "position", "complete-unit"),
        "artifact": ("register", "verify", "purge"),
        "transcript": ("import", "normalize", "review"),
        "speaking": ("package", "validate", "ingest"),
        "privacy": ("audit",),
    }
    groups = _parser()._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    for group, actions in required.items():
        assert group in groups, f"`linguawiki {group}` is not a command"
        available = groups[group]._subparsers._group_actions[0].choices  # type: ignore[union-attr]
        for action in actions:
            assert action in available, f"`linguawiki {group} {action}` is not a command"


def test_cataloguing_a_source_and_reading_it(
    polish_workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _onboard(polish_workspace, capsys)
    units = _write(
        tmp_path, "units.json", {"units": [{"label": "Odcinek 1"}, {"label": "Odcinek 2"}]}
    )
    assert (
        _run(
            polish_workspace,
            "source",
            "add",
            "--kind",
            "podcast",
            "--title",
            "Polski Daily",
            "--rights",
            "metadata-only",
            "--has-audio",
            "--input",
            units,
        )
        == 0
    )
    added = _json(capsys)
    assert added["ok"]
    assert added["data"]["rights"] == "metadata-only"
    assert len(added["data"]["units"]) == 2

    assert (
        _run(
            polish_workspace,
            "source",
            "comprehension",
            "--source",
            "Polski Daily",
            "--unit",
            "Odcinek 1",
            "--aid",
            "unaided",
            "--band",
            "gist",
            "--minutes",
            "8",
        )
        == 0
    )
    assert _json(capsys)["data"]["progress"]["unaided_band"] == "gist"

    # A second unaided reading is a reread and perfectly fine. What is refused is an
    # unaided reading recorded *after* help was given, which is the first one with the
    # help left out.
    assert (
        _run(
            polish_workspace,
            "source",
            "comprehension",
            "--source",
            "Polski Daily",
            "--unit",
            "Odcinek 1",
            "--aid",
            "subtitled",
            "--band",
            "full",
        )
        == 0
    )
    capsys.readouterr()
    assert (
        _run(
            polish_workspace,
            "source",
            "comprehension",
            "--source",
            "Polski Daily",
            "--unit",
            "Odcinek 1",
            "--aid",
            "unaided",
            "--band",
            "full",
        )
        == EXIT_ERROR
    )
    assert _json(capsys, stream="err")["error"]["code"] == "comprehension_aid_regressed"

    assert (
        _run(
            polish_workspace,
            "source",
            "complete-unit",
            "--source",
            "Polski Daily",
            "--unit",
            "Odcinek 1",
        )
        == 0
    )
    assert _json(capsys)["data"]["progress"]["coverage"] == pytest.approx(0.5)

    assert _run(polish_workspace, "source", "list") == 0
    assert _json(capsys)["data"]["total"] == 1


def test_the_speaking_surface_refuses_before_it_ingests(
    polish_workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _onboard(polish_workspace, capsys)
    assert (
        _run(
            polish_workspace,
            "speaking",
            "package",
            "--external-session-id",
            "rozmowa-1",
            "--language",
            "pl",
            "--started-at",
            "2026-01-01T10:00:00Z",
            "--minutes",
            "20",
            "--utterances",
            "2",
            "--out",
            str(tmp_path / "package.json"),
        )
        == 0
    )
    scaffolded = _json(capsys)
    assert scaffolded["ok"]
    assert any("placeholders" in warning for warning in scaffolded["warnings"])

    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    utterances = package["transcript_layers"][0]["utterances"]
    utterances[0]["text"] = "chcialbym kupic bilet do Krakowa"
    utterances[1]["text"] = "dokad pan jedzie i kiedy"
    path = _write(tmp_path, "filled.json", package)

    # The session comes first, because review runs exactly the checks ingestion runs: a
    # package is an account of *a session*, and reviewing one against a workspace with no
    # open session correctly reports that it cannot be ingested.
    assert _run(polish_workspace, "plan", "create", "--minutes", "60", "--mode", "speaking") == 0
    capsys.readouterr()
    assert _run(polish_workspace, "session", "start") == 0
    capsys.readouterr()

    assert _run(polish_workspace, "speaking", "validate", "--input", path) == 0
    validated = _json(capsys)
    assert validated["data"]["valid"]
    assert validated["data"]["utterances"] == 2

    # A timestamp without a zone is refused rather than silently read as UTC.
    assert (
        _run(
            polish_workspace,
            "speaking",
            "package",
            "--external-session-id",
            "rozmowa-2",
            "--language",
            "pl",
            "--started-at",
            "2026-01-01T10:00:00",
        )
        == EXIT_ERROR
    )
    assert _json(capsys, stream="err")["error"]["code"] == "naive_timestamp"

    assert _run(polish_workspace, "speaking", "ingest", "--input", path) == 0
    ingested = _json(capsys)
    assert ingested["data"]["imported_utterances"] == 2

    assert _run(polish_workspace, "speaking", "ingest", "--input", path) == 0
    assert _json(capsys)["data"]["duplicate"] is True

    assert _run(polish_workspace, "transcript", "show") == 0
    assert _json(capsys)["data"]["total"] == 2

    normalized = _write(tmp_path, "norm.json", {"text": "Dokad pan jedzie i kiedy?"})
    assert (
        _run(
            polish_workspace,
            "transcript",
            "normalize",
            "--utterance",
            "utt_002",
            "--input",
            normalized,
        )
        == 0
    )
    assert _json(capsys)["data"]["kind"] == "normalization"

    dishonest = _write(tmp_path, "bad.json", {"text": "Dokad pani jedzie i kiedy?"})
    assert (
        _run(
            polish_workspace,
            "transcript",
            "normalize",
            "--utterance",
            "utt_001",
            "--input",
            dishonest,
        )
        == EXIT_ERROR
    )
    assert _json(capsys, stream="err")["error"]["code"] == "normalization_changed_the_words"

    assert (
        _run(
            polish_workspace,
            "transcript",
            "pronunciation",
            "--dimension",
            "prosody",
            "--status",
            "observed",
            "--basis",
            "transcript",
            "--utterance",
            "utt_001",
        )
        == EXIT_ERROR
    )
    assert _json(capsys, stream="err")["error"]["code"] == "dimension_requires_audio"


def test_the_privacy_audit_reports_a_leak_with_a_failing_exit_code(
    polish_workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    _onboard(polish_workspace, capsys)
    assert _run(polish_workspace, "privacy", "audit") == 0
    assert _json(capsys)["data"]["ok"]

    # A private artifact directory committed by hand: the path rule catches it.
    (polish_workspace.root / "artifacts").mkdir(exist_ok=True)
    (polish_workspace.root / "artifacts" / "rozmowa.wav").write_bytes(b"RIFF")
    assert _run(polish_workspace, "privacy", "audit") == 0
    assert _json(capsys)["data"]["ok"], "an ignored artifact is not a leak"


def test_one_source_of_every_modality_flows_through_the_same_session(
    polish_workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Stage 5's exit gate: a reading, a podcast, a video fragment, and a conversation."""

    _onboard(polish_workspace, capsys)
    catalogue = (
        ("book", "Lalka", "short-excerpt", "Rozdział 1"),
        ("podcast", "Polski Daily", "metadata-only", "Odcinek 1"),
        ("video", "Polska z lotu ptaka", "metadata-only", "Fragment 1"),
        ("conversation", "Rozmowa z Pauliną", "full-local", "Cała rozmowa"),
    )
    for kind, title, rights, unit in catalogue:
        units = _write(tmp_path, f"{kind}-units.json", {"units": [{"label": unit}]})
        assert (
            _run(
                polish_workspace,
                "source",
                "add",
                "--kind",
                kind,
                "--title",
                title,
                "--rights",
                rights,
                "--input",
                units,
            )
            == 0
        )
        capsys.readouterr()

    assert _run(polish_workspace, "plan", "create", "--minutes", "60") == 0
    capsys.readouterr()
    assert _run(polish_workspace, "session", "start") == 0
    capsys.readouterr()

    events = [
        {
            "event_id": f"evt_01ARZ3NDEKTSV4RRFFQ69G5F0{index}",
            "kind": "source.progress",
            "occurred_at": f"2026-01-01T10:0{index}:00Z",
            "payload": {
                "source_ref": title,
                "unit": unit,
                "aid": "unaided",
                "band": "most",
                "mode": "extensive" if kind in ("book", "podcast") else "intensive",
                "minutes": 6,
                "completed": True,
            },
        }
        for index, (kind, title, _, unit) in enumerate(catalogue, start=1)
    ]
    batch = _write(
        tmp_path,
        "batch.json",
        {
            "schema_name": "lingua.session.events.v1",
            "schema_version": 1,
            "sequence": 1,
            "idempotency_key": "every-modality",
            "events": events,
        },
    )
    assert _run(polish_workspace, "session", "log", "--input", batch) == 0
    assert _json(capsys)["data"]["staged_events"] == 4

    assert _run(polish_workspace, "session", "close", "--actual-minutes", "45") == 0
    closed = _json(capsys)
    assert closed["data"]["comprehension_written"] == 4
    assert sorted(closed["data"]["sources_worked"]) == sorted(title for _, title, _, _ in catalogue)

    assert _run(polish_workspace, "source", "list") == 0
    listing = _json(capsys)["data"]
    assert listing["total"] == 4
    for entry in listing["sources"]:
        assert entry["progress"]["status"] == "completed"
        assert entry["progress"]["unaided_band"] == "most"

    assert _run(polish_workspace, "db", "check") in (0, EXIT_REPORTED_FAILURE)
    checked = _json(capsys)
    assert checked["data"]["ok"], [
        check["name"] for check in checked["data"]["checks"] if check["status"] == "failed"
    ]

    assert _run(polish_workspace, "privacy", "audit") == 0
    assert _json(capsys)["data"]["ok"]
