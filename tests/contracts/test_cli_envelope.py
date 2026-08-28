from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from linguawiki.cli import ContractArgumentParser, _command_name, run

ROOT = Path(__file__).resolve().parents[2]


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


def test_status_json_matches_compatibility_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["status", "--format", "json"], clock=FixedClock()) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    payload["correlation_id"] = "<event-id>"
    payload["data"]["application_version"] = "<application-version>"
    assert payload["generated_at"] == "2026-01-01T00:00:00Z"
    payload["generated_at"] = "<utc-timestamp>"
    expected = json.loads(
        (ROOT / "tests" / "contracts" / "snapshots" / "status-envelope.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert payload == expected


def test_invalid_cli_arguments_use_error_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["status", "--bad-option"], clock=FixedClock()) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert payload["ok"] is False
    assert payload["command"] == "status"
    assert payload["error"]["code"] == "invalid_arguments"
    assert payload["error"]["retryable"] is False


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_help_and_version_do_not_leak_system_exit(
    argument: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run([argument], clock=FixedClock()) == 0
    assert capsys.readouterr().out


def test_options_are_not_reported_as_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--format", "json"], clock=FixedClock()) == 2
    payload = json.loads(capsys.readouterr().err)

    assert payload["command"] == "unknown"


def test_command_name_comes_from_registered_subcommands() -> None:
    parser = ContractArgumentParser()
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("future-command")

    assert _command_name(["--future-option", "future-command"], parser) == "future-command"


def test_unexpected_exception_is_a_stable_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode() -> object:
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr("linguawiki.cli._status", explode)

    assert run(["status", "--format", "json"], clock=FixedClock()) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["details"] == [{"field": None, "reason": "RuntimeError", "context": {}}]
    assert "private implementation detail" not in captured.err
    assert "Traceback" not in captured.err


def test_debug_internal_error_prints_redacted_envelope_then_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode() -> object:
        raise RuntimeError("debug-only implementation detail")

    monkeypatch.setattr("linguawiki.cli._status", explode)
    monkeypatch.setenv("LINGUAWIKI_DEBUG", "1")

    assert run(["status", "--format", "json"], clock=FixedClock()) == 2
    lines = capsys.readouterr().err.splitlines()
    payload = json.loads(lines[0])
    assert payload["error"]["details"][0]["reason"] == "RuntimeError"
    assert lines[1].startswith("Traceback")
    assert "debug-only implementation detail" in lines[-1]


def test_output_contract_validation_error_remains_structured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("linguawiki.cli._status", lambda: {"invalid": True})

    assert run(["status", "--format", "json"], clock=FixedClock()) == 2
    payload = json.loads(capsys.readouterr().err)

    assert payload["error"]["code"] == "invalid_contract"
