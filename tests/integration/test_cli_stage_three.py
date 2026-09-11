"""The Stage 3 command surface: envelopes, exit codes, and the refusals a skill sees.

Driven through `run()` rather than the services, because the CLI is the boundary a skill
actually uses. What matters here is that a refusal arrives as a named code with a working
remedy in its message, and that learner text never has to travel through argv.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from linguawiki.cli import EXIT_ERROR, _command_name, _parser, run
from tests.conftest import PolishWorkspace

ITEM = "pl.lex.dworzec"
OTHER = "pl.lex.bilet"


def _json(capsys: pytest.CaptureFixture[str], *, stream: str = "out") -> dict[str, Any]:
    captured = capsys.readouterr()
    payload: dict[str, Any] = json.loads(getattr(captured, stream))
    return payload


def _run(workspace: PolishWorkspace, *arguments: str) -> int:
    return run(
        [*arguments, "--workspace", str(workspace.root), "--format", "json"],
        clock=workspace.clock,
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["knowledge", "search"], "knowledge.search"),
        (["knowledge", "merge"], "knowledge.merge"),
        (["evidence", "record"], "evidence.record"),
        (["evidence", "recompute"], "evidence.recompute"),
        (["errors", "record"], "errors.record"),
        (["errors", "queue"], "errors.queue"),
        (["estimate", "history"], "estimate.history"),
        (["context", "session"], "context.session"),
        (["context", "concept"], "context.concept"),
    ],
)
def test_every_new_command_names_itself_in_the_envelope(argv: list[str], expected: str) -> None:
    assert _command_name(argv, _parser()) == expected


def test_knowledge_search_and_get_report_through_the_success_envelope(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert _run(polish_workspace, "knowledge", "search", "--query", "dworzec") == 0
    found = _json(capsys)
    assert _run(polish_workspace, "knowledge", "get", ITEM) == 0
    item = _json(capsys)

    assert found["ok"] is True
    assert found["command"] == "knowledge.search"
    assert ITEM in {hit["stable_key"] for hit in found["data"]["hits"]}
    assert item["data"]["kind"] == "lexeme"
    assert item["data"]["owner"] == "pack"


def test_a_learner_item_arrives_through_input_not_argv(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """Learner text is arbitrary Unicode; argv would leave it in shell history."""

    payload = tmp_path / "note.json"
    payload.write_text(
        json.dumps(
            {
                "stable_key": "learner.note",
                "kind": "concept",
                "title": "notatka o dworcu",
                "body": "Na dworcu kupuję bilet w kasie.",
                "level": "A2",
                "aliases": [{"alias": "notatka dworzec", "locale": "pl"}],
                "themes": ["podróże"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert _run(polish_workspace, "knowledge", "upsert", "--input", str(payload)) == 0
    written = _json(capsys)

    assert written["data"]["created"] is True
    assert written["data"]["lifecycle"] == "approved-personal"
    assert (
        _run(
            polish_workspace,
            "knowledge",
            "link",
            "--source",
            written["data"]["content_id"],
            "--relation",
            "related",
            "--target",
            ITEM,
        )
        == 0
    )
    assert _json(capsys)["data"]["created"] is True


def test_evidence_record_reports_the_stage_it_moved(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert (
        _run(
            polish_workspace,
            "evidence",
            "record",
            "--task-type",
            "objective",
            "--modality",
            "text",
            "--score",
            "1.0",
            "--target",
            ITEM,
            "--dimension",
            "reading",
        )
        == 0
    )
    recorded = _json(capsys)

    assert recorded["data"]["outcome"] == "success"
    assert recorded["data"]["stage_after"] == "encountered"
    assert [entry["claim"] for entry in recorded["data"]["evidence"]] == ["recognition"]
    assert recorded["warnings"]


def test_a_learner_response_arrives_through_input_and_reports_its_visibility(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    payload = tmp_path / "response.json"
    payload.write_text(
        json.dumps({"response": "Kupiłem bilet w kasie na dworcu."}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert (
        _run(
            polish_workspace,
            "evidence",
            "record",
            "--task-type",
            "short-response",
            "--modality",
            "writing",
            "--score",
            "1.0",
            "--target",
            ITEM,
            "--dimension",
            "writing",
            "--claim",
            "controlled-production",
            "--input",
            str(payload),
        )
        == 0
    )
    recorded = _json(capsys)

    assert recorded["data"]["response_visibility"] == "excerpt"
    assert recorded["data"]["response_excerpt"] is not None


def test_an_incompatible_claim_fails_with_exit_two_and_a_named_code(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert (
        _run(
            polish_workspace,
            "evidence",
            "record",
            "--task-type",
            "objective",
            "--modality",
            "text",
            "--score",
            "1.0",
            "--target",
            ITEM,
            "--claim",
            "spontaneous-production",
        )
        == EXIT_ERROR
    )
    failure = _json(capsys, stream="err")

    assert failure["ok"] is False
    assert failure["error"]["code"] == "evidence_modality_incompatible"
    assert failure["error"]["retryable"] is False


def test_an_unknown_claim_is_refused_by_the_parser_before_anything_runs(
    polish_workspace: PolishWorkspace,
) -> None:
    """A closed vocabulary belongs in the parser, so a typo never reaches a database."""

    assert (
        _run(
            polish_workspace,
            "evidence",
            "record",
            "--task-type",
            "objective",
            "--modality",
            "text",
            "--score",
            "1.0",
            "--target",
            ITEM,
            "--claim",
            "fluency",
        )
        == EXIT_ERROR
    )


def test_evidence_recompute_dry_run_reports_without_writing(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    for index in range(2):
        assert (
            _run(
                polish_workspace,
                "evidence",
                "record",
                "--task-type",
                "objective",
                "--modality",
                "text",
                "--score",
                "1.0",
                "--target",
                ITEM,
                "--dimension",
                "reading",
                "--context",
                f"objective:{index}",
            )
            == 0
        )
        capsys.readouterr()

    assert _run(polish_workspace, "evidence", "recompute", "--dry-run") == 0
    dry = _json(capsys)

    assert dry["data"]["dry_run"] is True
    assert dry["data"]["aggregation_version"].startswith("mastery.")
    assert "dry run: nothing was written" in dry["warnings"]


def test_errors_record_show_and_queue_round_trip(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert (
        _run(
            polish_workspace,
            "errors",
            "record",
            "--category",
            "case-government",
            "--signature",
            "szukam bilet",
            "--description",
            "Accusative where the verb governs the genitive.",
            "--target",
            OTHER,
        )
        == 0
    )
    recorded = _json(capsys)
    error_id = recorded["data"]["error_id"]

    assert _run(polish_workspace, "errors", "show", error_id) == 0
    shown = _json(capsys)
    assert (
        _run(
            polish_workspace,
            "errors",
            "followup",
            "--kind",
            "practice",
            "--action",
            "Drill the genitive after szukać.",
            "--error",
            error_id,
        )
        == 0
    )
    queued = _json(capsys)
    assert _run(polish_workspace, "errors", "queue") == 0
    queue = _json(capsys)

    assert recorded["data"]["status"] == "observed"
    assert recorded["data"]["outstanding"]
    assert shown["data"]["occurrences"]
    assert queued["data"]["status"] == "open"
    assert [entry["followup_id"] for entry in queue["data"]["entries"]] == [
        queued["data"]["followup_id"]
    ]


def test_an_uncertain_error_match_is_refused_with_both_remedies_in_the_message(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert (
        _run(
            polish_workspace,
            "errors",
            "record",
            "--category",
            "case-government",
            "--signature",
            "szukam biletu",
            "--description",
            "Genitive after szukać.",
            "--target",
            OTHER,
        )
        == 0
    )
    capsys.readouterr()

    assert (
        _run(
            polish_workspace,
            "errors",
            "record",
            "--category",
            "case-government",
            "--signature",
            "szukam biletow",
            "--description",
            "A near miss.",
            "--target",
            OTHER,
        )
        == EXIT_ERROR
    )
    failure = _json(capsys, stream="err")

    assert failure["error"]["code"] == "error_match_uncertain"
    assert "--attach-to" in failure["error"]["message"]
    assert "--distinct" in failure["error"]["message"]
    assert failure["error"]["details"]


def test_attach_to_and_distinct_cannot_both_be_asked_for(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert (
        _run(
            polish_workspace,
            "errors",
            "record",
            "--category",
            "case-government",
            "--signature",
            "szukam biletu",
            "--description",
            "Genitive after szukać.",
            "--target",
            OTHER,
            "--attach-to",
            "err_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "--distinct",
        )
        == EXIT_ERROR
    )

    assert _json(capsys, stream="err")["error"]["code"] == "conflicting_error_match"


def test_estimate_show_lists_every_dimension_and_never_one_global_level(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert _run(polish_workspace, "estimate", "show") == 0
    plain = _json(capsys)
    assert _run(polish_workspace, "estimate", "show", "--summary") == 0
    summarised = _json(capsys)

    assert plain["data"]["summary_level"] is None
    assert plain["data"]["not_tested"]
    assert summarised["data"]["summary_level"] is None, "no evidence, so no summary"
    assert any("no dimension has evidence" in warning for warning in summarised["warnings"])


def test_estimate_history_explains_every_change(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert (
        _run(
            polish_workspace,
            "evidence",
            "record",
            "--task-type",
            "objective",
            "--modality",
            "text",
            "--score",
            "1.0",
            "--target",
            ITEM,
            "--dimension",
            "reading",
        )
        == 0
    )
    capsys.readouterr()

    assert _run(polish_workspace, "estimate", "history", "--dimension", "reading") == 0
    history = _json(capsys)

    assert history["data"]["total"] >= 1
    snapshot = history["data"]["snapshots"][0]
    assert snapshot["reason"]
    assert snapshot["factors"]
    assert snapshot["evidence_ids"]


@pytest.mark.parametrize("scope", ["session", "assessment", "source"])
def test_every_context_scope_reports_its_bounds(
    scope: str, capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert _run(polish_workspace, "context", scope) == 0
    bundle = _json(capsys)

    assert bundle["data"]["scope"] == scope
    assert bundle["data"]["estimated_tokens"] <= bundle["data"]["token_limit"]
    assert bundle["data"]["sections"]
    assert bundle["data"]["provenance"]["aggregation_version"].startswith("mastery.")


def test_a_concept_bundle_requires_the_item_it_is_about(
    polish_workspace: PolishWorkspace,
) -> None:
    """`--item` is required by the parser, so the refusal costs no database access."""

    assert _run(polish_workspace, "context", "concept") == EXIT_ERROR
    assert _run(polish_workspace, "context", "concept", "--item", ITEM) == 0


def test_a_merge_defaults_to_a_dry_run_and_only_applies_when_asked(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """A merge is not reversible, so the safe form is the default."""

    payload = tmp_path / "note.json"
    payload.write_text(
        json.dumps(
            {
                "stable_key": "learner.duplicate",
                "kind": "concept",
                "title": "duplikat",
                "body": "A duplicate the learner wrote twice.",
            }
        ),
        encoding="utf-8",
    )
    assert _run(polish_workspace, "knowledge", "upsert", "--input", str(payload)) == 0
    duplicate = _json(capsys)["data"]["content_id"]

    assert _run(polish_workspace, "knowledge", "merge", "--source", duplicate, "--into", ITEM) == 0
    planned = _json(capsys)
    assert (
        _run(
            polish_workspace,
            "knowledge",
            "merge",
            "--source",
            duplicate,
            "--into",
            ITEM,
            "--apply",
        )
        == 0
    )
    applied = _json(capsys)

    assert planned["data"]["dry_run"] is True
    assert planned["data"]["applied"] is False
    assert applied["data"]["applied"] is True


def test_a_human_readable_run_prints_lines_rather_than_json(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    assert (
        run(
            ["knowledge", "get", ITEM, "--workspace", str(polish_workspace.root)],
            clock=polish_workspace.clock,
        )
        == 0
    )
    captured = capsys.readouterr()

    assert captured.out.startswith(f"{ITEM} (lexeme, pack pl-pilot)")
    assert "lifecycle verified" in captured.out
