"""The Stage 2 command surface: JSON envelopes, exit codes, and `--input` payloads.

These tests drive `run()` rather than the services, because the CLI is the machine
boundary a skill actually uses: its exit codes, its envelope, and its refusal to take
learner text through argv are part of the contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from linguawiki.cli import EXIT_ERROR, EXIT_REPORTED_FAILURE, _command_name, _parser, run
from linguawiki.contract_validation import validate_json_contract
from tests.conftest import FIXTURE_PACKS, PILOT_PACK, PolishWorkspace, SyntheticWorkspace

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"


def _json(capsys: pytest.CaptureFixture[str], *, stream: str = "out") -> dict[str, Any]:
    captured = capsys.readouterr()
    payload: dict[str, Any] = json.loads(getattr(captured, stream))
    return payload


def _write(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pack", "scaffold", "x"], "pack.scaffold"),
        (["pack", "validate", "x"], "pack.validate"),
        (["pack", "author", "review-queue"], "pack.author.review-queue"),
        (["pack", "template", "stabilize"], "pack.template.stabilize"),
        (["user", "create"], "user.create"),
        (["track", "archive"], "track.archive"),
        (["onboard", "finalize"], "onboard.finalize"),
        (["resources", "prepare"], "resources.prepare"),
        (["curriculum", "audit-finalize"], "curriculum.audit-finalize"),
        (["assessment", "next"], "assessment.next"),
    ],
)
def test_every_command_names_itself_in_the_envelope(argv: list[str], expected: str) -> None:
    assert _command_name(argv, _parser()) == expected


def test_pack_validate_and_coverage_report_through_the_success_envelope(
    capsys: pytest.CaptureFixture[str], synthetic_workspace: SyntheticWorkspace
) -> None:
    assert run(["pack", "validate", str(PILOT_PACK), "--format", "json"]) == 0
    validation = _json(capsys)
    assert run(["pack", "coverage", str(PILOT_PACK), "--format", "json"]) == 0
    coverage = _json(capsys)

    validate_json_contract("linguawiki.cli.success.v1", validation, schema_directory=SCHEMAS)
    assert validation["command"] == "pack.validate"
    assert validation["data"]["pack_key"] == "pl-pilot"
    assert coverage["data"]["declared_maturity_supported"] is True
    assert coverage["data"]["supported_onboarding_modes"] == ["declared-level"]


def test_pack_stamp_check_passes_on_a_shipped_pack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["pack", "stamp", str(PILOT_PACK), "--check", "--format", "json"]) == 0
    payload = _json(capsys)

    assert payload["data"]["restamped"] == []
    assert payload["data"]["written"] == []
    assert payload["data"]["check"] is True


def test_a_pack_may_be_named_by_its_bundled_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["pack", "validate", "pl-pilot", "--format", "json"]) == 0

    assert _json(capsys)["data"]["pack_key"] == "pl-pilot"


def test_pack_install_list_and_diff_through_the_cli(
    capsys: pytest.CaptureFixture[str], synthetic_workspace: SyntheticWorkspace
) -> None:
    root = str(synthetic_workspace.root)

    assert run(["pack", "install", "--workspace", root, "pl-pilot", "--format", "json"]) == 0
    installed = _json(capsys)
    assert run(["pack", "list", "--workspace", root, "--format", "json"]) == 0
    listing = _json(capsys)
    assert run(["pack", "diff", "--workspace", root, "pl-pilot", "--format", "json"]) == 0
    difference = _json(capsys)

    assert installed["data"]["created"] is True
    assert [entry["pack_key"] for entry in listing["data"]["packs"]] == ["pl-pilot"]
    assert difference["data"]["identical"] is True


def test_pack_install_dry_run_writes_nothing(
    capsys: pytest.CaptureFixture[str], synthetic_workspace: SyntheticWorkspace
) -> None:
    root = str(synthetic_workspace.root)

    assert (
        run(["pack", "install", "--workspace", root, "pl-pilot", "--dry-run", "--format", "json"])
        == 0
    )
    payload = _json(capsys)
    assert run(["pack", "list", "--workspace", root, "--format", "json"]) == 0

    assert payload["data"]["dry_run"] is True
    assert _json(capsys)["data"]["packs"] == []


def test_an_invalid_pack_directory_prints_an_error_envelope_on_stderr(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert run(["pack", "validate", str(tmp_path), "--format", "json"]) == EXIT_ERROR
    payload = _json(capsys, stream="err")

    validate_json_contract("linguawiki.cli.error.v1", payload, schema_directory=SCHEMAS)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "pack_manifest_missing"


def test_user_and_track_commands_round_trip_through_the_cli(
    capsys: pytest.CaptureFixture[str], installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    root = str(installed_pilot.root)
    preferences = _write(
        tmp_path / "preferences.json",
        {
            "goals": ["work conversation"],
            "interests": ["podróże"],
            "correction_mode": "accuracy",
            "voice_available": True,
        },
    )

    assert (
        run(
            [
                "user",
                "create",
                "--workspace",
                root,
                "--name",
                "Синтетический Учащийся",
                "--timezone",
                "Europe/Warsaw",
                "--native",
                "ru",
                "--support",
                "en",
                "--format",
                "json",
            ]
        )
        == 0
    )
    user = _json(capsys)
    assert (
        run(
            [
                "track",
                "create",
                "--workspace",
                root,
                "--target-language",
                "pl",
                "--framework",
                "cefr",
                "--declared-level",
                "A2",
                "--target-level",
                "B1",
                "--goal",
                "Rozmawiać po polsku",
                "--input",
                preferences,
                "--format",
                "json",
            ]
        )
        == 0
    )
    track = _json(capsys)
    assert run(["track", "show", "--workspace", root, "--format", "json"]) == 0
    shown = _json(capsys)
    assert run(["user", "list", "--workspace", root, "--format", "json"]) == 0
    users = _json(capsys)

    assert user["data"]["native_languages"] == ["ru"]
    assert track["data"]["declared_level"] == "A2"
    assert shown["data"]["preferences"]["correction_mode"] == "accuracy"
    assert [entry["display_name"] for entry in users["data"]["users"]] == ["Синтетический Учащийся"]


def test_a_level_from_another_framework_is_refused_through_the_cli(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    root = str(polish_workspace.root)

    assert (
        run(
            ["track", "update", "--workspace", root, "--declared-level", "HSK2", "--format", "json"]
        )
        == EXIT_ERROR
    )
    payload = _json(capsys, stream="err")

    assert payload["error"]["code"] == "level_not_in_framework"


def test_track_status_transitions_through_the_cli(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    root = str(polish_workspace.root)

    for action, expected in (("pause", "paused"), ("activate", "active"), ("archive", "archived")):
        assert run(["track", action, "--workspace", root, "--format", "json"]) == 0
        assert _json(capsys)["data"]["status"] == expected


def test_the_onboarding_and_calibration_flow_through_the_cli(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    root = str(polish_workspace.root)
    answer = _write(tmp_path / "answer.json", "A2")

    assert (
        run(["onboard", "start", "--workspace", root, "--declared-level", "A2", "--format", "json"])
        == 0
    )
    started = _json(capsys)
    assert (
        run(
            [
                "onboard",
                "record",
                "--workspace",
                root,
                "--key",
                "self_reported_level",
                "--input",
                answer,
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert run(["resources", "plan", "--workspace", root, "--format", "json"]) == 0
    plan = _json(capsys)
    assert run(["onboard", "finalize", "--workspace", root, "--format", "json"]) == 0
    finalized = _json(capsys)
    assert run(["assessment", "next", "--workspace", root, "--format", "json"]) == 0
    served = _json(capsys)
    assert (
        run(
            [
                "assessment",
                "record",
                "--workspace",
                root,
                "--content",
                served["data"]["content_id"],
                "--score",
                "1.0",
                "--format",
                "json",
            ]
        )
        == 0
    )
    recorded = _json(capsys)
    assert run(["assessment", "finalize", "--workspace", root, "--format", "json"]) == 0
    final = _json(capsys)

    assert started["data"]["calibration_label"] == "pilot-calibration"
    assert plan["data"]["dry_run"] is True
    assert plan["data"]["plan_label"] == "pilot curriculum"
    assert finalized["data"]["status"] == "finalized"
    assert served["data"]["prompt"]
    assert recorded["data"]["tasks_recorded"] == 1
    assert final["data"]["status"] == "finalized"
    assert any("pilot pack" in warning for warning in finalized["warnings"])


def test_placement_is_refused_through_the_cli_with_the_bank_gaps(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    root = str(polish_workspace.root)

    status = run(
        ["assessment", "start", "--workspace", root, "--run-type", "placement", "--format", "json"]
    )
    payload = _json(capsys, stream="err")

    assert status == EXIT_ERROR
    assert payload["error"]["code"] == "placement_bank_insufficient"
    assert len(payload["error"]["details"]) > 1


def test_resources_prepare_reports_a_bounded_plan(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    root = str(polish_workspace.root)

    assert (
        run(
            [
                "resources",
                "prepare",
                "--workspace",
                root,
                "--item-budget",
                "15",
                "--weeks",
                "1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    prepared = _json(capsys)
    assert run(["resources", "status", "--workspace", root, "--format", "json"]) == 0
    status = _json(capsys)

    assert prepared["data"]["newly_imported"] == 15
    assert prepared["data"]["item_budget"] == 15
    assert [item for item in prepared["data"]["skipped"] if item["item_kind"] == "knowledge"]
    assert status["data"]["status"] == "applied"


def test_the_curriculum_flow_through_the_cli(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    root = str(polish_workspace.root)
    outline = _write(
        tmp_path / "outline.json",
        {
            "schema_name": "lingua.curriculum.v1",
            "schema_version": 1,
            "title": "Own plan",
            "kind": "user-authored",
            "version": "1",
            "rights_status": "user-authored",
            "provenance": "written by the learner",
            "units": [
                {
                    "code": "u1",
                    "title": "Travel",
                    "level": "A2",
                    "objectives": [
                        {
                            "objective": "Buy a ticket",
                            "mapped_kind": "knowledge",
                            "mapped_key": "pl.lex.bilet",
                            "map_confidence": "high",
                        },
                        {"objective": "Read a timetable aloud"},
                    ],
                }
            ],
        },
    )

    assert (
        run(["curriculum", "import", "--workspace", root, "--input", outline, "--format", "json"])
        == 0
    )
    imported = _json(capsys)
    assert (
        run(
            ["curriculum", "position", "--workspace", root, "--completed", "u1", "--format", "json"]
        )
        == 0
    )
    positioned = _json(capsys)
    assert (
        run(
            [
                "curriculum",
                "audit-start",
                "--workspace",
                root,
                "--sample-size",
                "1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    audit = _json(capsys)
    results = _write(
        tmp_path / "results.json",
        {
            "results": [
                {"target_ref": audit["data"]["items"][0]["target_ref"], "outcome": "incorrect"}
            ]
        },
    )
    assert (
        run(
            [
                "curriculum",
                "audit-record",
                "--workspace",
                root,
                "--input",
                results,
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert run(["curriculum", "audit-finalize", "--workspace", root, "--format", "json"]) == 0
    final = _json(capsys)

    assert imported["data"]["mapped_objectives"] == 1
    assert len(imported["data"]["unmapped_objectives"]) == 1
    assert positioned["data"]["encountered_items"] == 1
    assert final["data"]["status"] == "finalized"
    assert len(final["data"]["confirmed_gaps"]) == 1
    assert len(final["data"]["queued_calibration"]) == 1


def test_the_pack_authoring_flow_through_the_cli(
    capsys: pytest.CaptureFixture[str], installed_pilot: SyntheticWorkspace, tmp_path: Path
) -> None:
    root = str(installed_pilot.root)
    template = _write(
        tmp_path / "template.json",
        {
            "schema_name": "lingua.pack.template.v1",
            "schema_version": 1,
            "template_key": "pl-office",
            "version": 1,
            "purpose": "generation",
            "intended_kinds": ["construction"],
            "body": "Draft office collocations.",
            "known_failure_modes": ["invents register"],
        },
    )

    assert (
        run(
            [
                "pack",
                "template",
                "validate",
                "--workspace",
                root,
                "--input",
                template,
                "--format",
                "json",
            ]
        )
        == 0
    )
    registered = _json(capsys)
    assert (
        run(
            [
                "pack",
                "author",
                "generate-draft",
                "--workspace",
                root,
                "--template-key",
                "pl-office",
                "--template-version",
                "1",
                "--count",
                "1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    batch = _json(capsys)
    drafts = _write(
        tmp_path / "drafts.json",
        {
            "items": [
                {
                    "stable_key": "pl.draft.cli",
                    "kind": "construction",
                    "title": "wysłać maila",
                    "body": "Draft used by the CLI test.",
                    "level": "A2",
                    "themes": ["praca i biuro"],
                    "risk_tier": 2,
                }
            ]
        },
    )
    assert (
        run(
            [
                "pack",
                "author",
                "import",
                "--workspace",
                root,
                "--batch",
                batch["data"]["batch_id"],
                "--input",
                drafts,
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        run(
            [
                "pack",
                "author",
                "review-queue",
                "--workspace",
                root,
                "--limit",
                "50",
                "--format",
                "json",
            ]
        )
        == 0
    )
    queue = _json(capsys)
    content_id = next(
        entry["content_id"]
        for entry in queue["data"]["items"]
        if entry["stable_key"] == "pl.draft.cli"
    )

    assert registered["data"]["sampling_policy"] == "full-inspection"
    assert batch["data"]["required_sample"] == 1
    assert queue["data"]["axis_debt"]["linguistic"] >= 1

    # A machine reviewer's overreach is an error envelope, not a silent pass.
    status = run(
        [
            "pack",
            "author",
            "review",
            "--workspace",
            root,
            "--content",
            content_id,
            "--axis",
            "linguistic",
            "--state",
            "human-verified",
            "--reviewer-kind",
            "ai",
            "--reviewer",
            "example-model",
            "--method",
            "self-review",
            "--format",
            "json",
        ]
    )
    assert status == EXIT_ERROR
    assert _json(capsys, stream="err")["error"]["code"] == "machine_review_ceiling"

    # A defective sample is a reported failure: exit 1 with a payload, not exit 2.
    status = run(
        [
            "pack",
            "author",
            "review",
            "--workspace",
            root,
            "--content",
            content_id,
            "--axis",
            "linguistic",
            "--state",
            "unreviewed",
            "--reviewer-kind",
            "human",
            "--reviewer",
            "pack-author",
            "--method",
            "manual inspection",
            "--inspection",
            "defective",
            "--finding",
            "wrong register",
            "--format",
            "json",
        ]
    )
    quarantine = _json(capsys)
    assert status == EXIT_REPORTED_FAILURE
    assert quarantine["data"]["quarantined_templates"] == ["pl-office@1"]

    assert (
        run(
            [
                "pack",
                "template",
                "quarantine",
                "--workspace",
                root,
                "--template-key",
                "pl-office",
                "--template-version",
                "1",
                "--reason",
                "unusable register",
                "--format",
                "json",
            ]
        )
        == EXIT_REPORTED_FAILURE
    )


def test_structured_payloads_are_read_from_stdin(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    polish_workspace: PolishWorkspace,
) -> None:
    """Learner text must never have to travel through argv."""

    import io

    root = str(polish_workspace.root)
    run(["onboard", "start", "--workspace", root, "--declared-level", "A2", "--format", "json"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO('["order food", "ask directions"]'))

    assert (
        run(
            [
                "onboard",
                "record",
                "--workspace",
                root,
                "--key",
                "can_do_summary",
                "--input",
                "-",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = _json(capsys)

    assert payload["data"]["answers"]["can_do_summary"] == ["order food", "ask directions"]


def test_an_unparsable_input_payload_is_a_structured_error(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    status = run(
        [
            "curriculum",
            "import",
            "--workspace",
            str(polish_workspace.root),
            "--input",
            str(path),
            "--format",
            "json",
        ]
    )

    assert status == EXIT_ERROR
    assert _json(capsys, stream="err")["error"]["code"] == "invalid_input"


def test_human_output_is_produced_for_every_new_group(
    capsys: pytest.CaptureFixture[str], polish_workspace: PolishWorkspace
) -> None:
    """The human format is the default, so it must not crash on any group."""

    root = str(polish_workspace.root)
    for argv in (
        ["pack", "list", "--workspace", root],
        ["pack", "validate", str(FIXTURE_PACKS / "tonal")],
        ["user", "show", "--workspace", root],
        ["track", "list", "--workspace", root],
        ["resources", "plan", "--workspace", root],
        ["onboard", "start", "--workspace", root, "--declared-level", "A2"],
        ["onboard", "status", "--workspace", root],
        ["assessment", "start", "--workspace", root],
        ["assessment", "report", "--workspace", root],
        ["pack", "author", "review-queue", "--workspace", root],
    ):
        assert run(argv) == 0, argv
        assert capsys.readouterr().out.strip(), argv


def test_a_status_envelope_reports_the_current_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from linguawiki.contracts import DELIVERY_STAGE

    assert run(["status", "--format", "json"]) == 0
    payload = _json(capsys)

    validate_json_contract("linguawiki.cli.status.v1", payload, schema_directory=SCHEMAS)
    assert payload["data"]["stage"] == DELIVERY_STAGE


def test_pack_scaffold_creates_a_validatable_fixture_through_the_cli(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "cs-new"

    status = run(
        [
            "pack",
            "scaffold",
            str(target),
            "--pack-key",
            "cs-new",
            "--name",
            "Czech starter",
            "--language",
            "cs",
            "--framework",
            "cefr",
            "--framework-name",
            "CEFR",
            "--framework-version",
            "2020",
            "--level",
            "A1",
            "--level",
            "A2",
            "--level",
            "B1",
            "--band",
            "A2",
            "--theme",
            "everyday",
            "--support",
            "en",
            "--format",
            "json",
        ]
    )
    scaffolded = _json(capsys)
    assert status == 0
    assert run(["pack", "validate", str(target), "--format", "json"]) == 0
    validated = _json(capsys)

    assert scaffolded["data"]["maturity"] == "fixture"
    assert "manifest.json" in scaffolded["data"]["files"]
    assert "seed/knowledge.jsonl" in scaffolded["data"]["files"]
    assert validated["data"]["counts"]["knowledge"] == 0
    assert scaffolded["warnings"]


def test_pack_update_previews_by_default_and_applies_with_a_flag(
    capsys: pytest.CaptureFixture[str], synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """An update is previewed unless the caller asks for it to be applied."""

    import shutil

    from linguawiki.contracts import PackManifest
    from linguawiki.packs.format import directory_digests, pack_content_address
    from linguawiki.packs.stamp import stamp_pack

    root = str(synthetic_workspace.root)
    pack = tmp_path / "pl-pilot"
    shutil.copytree(FIXTURE_PACKS / "inflected", pack)
    assert run(["pack", "install", "--workspace", root, str(pack), "--format", "json"]) == 0
    capsys.readouterr()

    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.2.0"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    stamp_pack(pack)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = directory_digests(pack)
    manifest["content_address"] = None
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    parsed = PackManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    manifest["content_address"] = pack_content_address(parsed, dict(parsed.files))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    assert run(["pack", "update", "--workspace", root, str(pack), "--format", "json"]) == 0
    preview = _json(capsys)
    assert run(["pack", "list", "--workspace", root, "--format", "json"]) == 0
    before = _json(capsys)
    assert (
        run(["pack", "update", "--workspace", root, str(pack), "--apply", "--format", "json"]) == 0
    )
    applied = _json(capsys)
    assert run(["pack", "list", "--workspace", root, "--format", "json"]) == 0
    after = _json(capsys)

    assert preview["data"]["dry_run"] is True
    assert preview["data"]["updated_from"] == "0.1.0"
    assert before["data"]["packs"][0]["version"] == "0.1.0"
    assert applied["data"]["dry_run"] is False
    assert after["data"]["packs"][0]["version"] == "0.2.0"
