"""Presentation survives installation, and what the learner saw survives a pack update.

Two halves of one guarantee. The bank has to hold a task's presentation at all -- the
installer writes an enumerated column list, so before 0031 a pack carrying one installed
into a bank that discarded it and a serve had nothing to snapshot. And the serve has to
keep what it showed, because a pack is mutable and a run is not: new choices against an
old answer key, or a replaced recording under the same key, are both a different
question asked under the identity of the one the learner was credited for.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import assessment as assessment_service
from linguawiki.services import packs as pack_service
from tests.conftest import NEXT_PILOT_VERSION, PILOT_PACK, PolishWorkspace
from tests.language_packs.support import republish

FORM = "assessments/pl-a2-calibration.json"
#: An objective task the pilot now renders as buttons.
CHOOSER = "pl.task.grammar-control.04"


def _bank(workspace: PolishWorkspace, stable_key: str) -> str | None:
    with open_reader(workspace.paths) as database:
        row = database.one(
            "SELECT t.presentation_json FROM assessment_tasks t "
            "JOIN content_records c ON c.content_id = t.content_id "
            "WHERE c.stable_key = ?",
            [stable_key],
        )
    assert row is not None, f"{stable_key} is not installed"
    return None if row[0] is None else str(row[0])


def _edit_pack(root: Path, mutate: Any) -> None:
    path = root / FORM
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _publish_next(workspace: PolishWorkspace, tmp_path: Path, mutate: Any) -> Path:
    """A next pilot version with one edit to its assessment form."""

    from linguawiki.packs.stamp import stamp_pack

    root = tmp_path / "pl-pilot-next"
    shutil.copytree(PILOT_PACK, root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = NEXT_PILOT_VERSION
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _edit_pack(root, mutate)
    stamp_pack(root)
    republish(root)
    pack_service.install(workspace.paths, root, clock=workspace.clock, allow_update=True)
    return root


def _task(document: dict[str, Any], stable_key: str) -> dict[str, Any]:
    for task in document["tasks"]:
        if task["stable_key"] == stable_key:
            return task
    raise AssertionError(f"{stable_key} is not in the form")


def test_installing_a_pack_keeps_the_presentation_it_declares(
    polish_workspace: PolishWorkspace,
) -> None:
    stored = _bank(polish_workspace, CHOOSER)
    assert stored is not None
    shown = json.loads(stored)
    assert shown["kind"] == "multiple-choice"
    assert [choice["value"] for choice in shown["choices"]] == [
        "pięć bilety",
        "pięć biletów",
        "pięć biletu",
    ]


def test_a_task_with_no_presentation_installs_as_null_rather_than_an_empty_record(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """`{}` would be a presentation record with no kind. The column is nullable for this."""

    def drop(document: dict[str, Any]) -> None:
        _task(document, CHOOSER).pop("presentation")

    _publish_next(polish_workspace, tmp_path, drop)

    assert _bank(polish_workspace, CHOOSER) is None


def test_an_update_that_changes_the_choices_replaces_them(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """Written on insert *and* on conflict.

    Without the `DO UPDATE SET` clause the previous pack's choices stand behind a task
    that no longer has them, which is worse than not installing them at all: the bank
    then disagrees with the pack it claims to be installed from.
    """

    def rewrite(document: dict[str, Any]) -> None:
        task = _task(document, CHOOSER)
        task["presentation"]["choices"] = [
            {"value": "pięć biletów"},
            {"value": "pięciu biletów"},
        ]

    _publish_next(polish_workspace, tmp_path, rewrite)

    stored = _bank(polish_workspace, CHOOSER)
    assert stored is not None
    assert [choice["value"] for choice in json.loads(stored)["choices"]] == [
        "pięć biletów",
        "pięciu biletów",
    ]


def test_serving_a_task_snapshots_the_presentation_as_shown(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    while True:
        served = assessment_service.next_task(
            polish_workspace.paths, run=run.run_id, clock=polish_workspace.clock
        )
        assert isinstance(served, assessment_service.NextTaskReport), "bank ran out"
        if served.presentation is not None and served.presentation.choices:
            break
        assessment_service.record(
            polish_workspace.paths,
            run=run.run_id,
            content_id=served.content_id,
            score=0.5,
            assessor_kind="ai",
            clock=polish_workspace.clock,
        )

    with open_reader(polish_workspace.paths) as database:
        row = database.one(
            "SELECT presentation_json, asset_identity_json FROM assessment_run_tasks "
            "WHERE run_id = ? AND content_id = ?",
            [run.run_id, served.content_id],
        )
    assert row is not None
    snapshot = json.loads(str(row[0]))
    assert [choice["value"] for choice in snapshot["choices"]] == [
        choice.value for choice in served.presentation.choices
    ]
    # A shuffled bank is resolved once, at serve time: the stored order is the order the
    # learner saw, and `order` records that it is now settled.
    assert snapshot["order"] == "fixed"
    assert row[1] is None, "a text task heard nothing"


def _content_id(workspace: PolishWorkspace, stable_key: str) -> str:
    with open_reader(workspace.paths) as database:
        row = database.one(
            "SELECT content_id FROM content_records WHERE stable_key = ?", [stable_key]
        )
    assert row is not None, f"{stable_key} is not installed"
    return str(row[0])


def _serve_until(workspace: PolishWorkspace, run_id: str, stable_key: str) -> str:
    """Serve tasks until `stable_key` comes up, parking the others."""

    while True:
        served = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
        assert isinstance(served, assessment_service.NextTaskReport), "bank ran out"
        if served.stable_key == stable_key:
            return served.content_id
        assessment_service.record(
            workspace.paths,
            run=run_id,
            content_id=served.content_id,
            score=0.5,
            assessor_kind="ai",
            clock=workspace.clock,
        )


def test_a_task_read_back_after_a_pack_update_shows_what_it_showed(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    """The whole point of the snapshot: serve, change the pack, read it back."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    before = assessment_service.served_task(
        polish_workspace.paths, run=run.run_id, content_id=content_id
    )
    assert before.presentation is not None
    shown = [choice.value for choice in before.presentation.choices]
    assert sorted(shown) == sorted(["pięć bilety", "pięć biletów", "pięć biletu"])

    def rewrite(document: dict[str, Any]) -> None:
        _task(document, CHOOSER)["presentation"]["choices"] = [
            {"value": "pięć biletów"},
            {"value": "pięciu biletów"},
        ]

    _publish_next(polish_workspace, tmp_path, rewrite)

    after = assessment_service.served_task(
        polish_workspace.paths, run=run.run_id, content_id=content_id
    )
    assert after.presentation is not None
    assert [choice.value for choice in after.presentation.choices] == shown
    assert _bank(polish_workspace, CHOOSER) is not None
    assert "pięciu biletów" in str(_bank(polish_workspace, CHOOSER)), "the bank did change"


def test_a_shuffled_bank_does_not_reorder_an_already_served_task(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    first = assessment_service.served_task(
        polish_workspace.paths, run=run.run_id, content_id=content_id
    )
    assert first.presentation is not None
    # The order is settled, not re-declared: `shuffled` in the pack, `fixed` once served.
    assert first.presentation.order == "fixed"

    def reorder(document: dict[str, Any]) -> None:
        task = _task(document, CHOOSER)
        task["presentation"]["choices"] = list(reversed(task["presentation"]["choices"]))

    _publish_next(polish_workspace, tmp_path, reorder)

    again = assessment_service.served_task(
        polish_workspace.paths, run=run.run_id, content_id=content_id
    )
    assert again.presentation is not None
    assert [choice.value for choice in again.presentation.choices] == [
        choice.value for choice in first.presentation.choices
    ]


def _damage(workspace: PolishWorkspace, run_id: str, content_id: str, **columns: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with (
        open_writer(workspace.paths, command="test.damage") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            f"UPDATE assessment_run_tasks SET {assignments} WHERE run_id = ? AND content_id = ?",
            [*columns.values(), run_id, content_id],
        )


def test_a_damaged_snapshot_is_refused_by_name_rather_than_read(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    _damage(polish_workspace, run.run_id, content_id, presentation_json='{"kind": "unheard-of"}')

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.served_task(
            polish_workspace.paths, run=run.run_id, content_id=content_id
        )
    assert failure.value.payload.code == "assessment_presentation_malformed"


def test_half_a_presentation_snapshot_is_damage_rather_than_a_legacy_row(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)

    # An identity with no presentation: nothing says how the recording was framed.
    _damage(
        polish_workspace,
        run.run_id,
        content_id,
        presentation_json=None,
        asset_identity_json=json.dumps({"content_id": "cnt_x", "sha256": "a" * 64}),
    )
    with pytest.raises(LinguaWikiError) as orphaned:
        assessment_service.served_task(
            polish_workspace.paths, run=run.run_id, content_id=content_id
        )
    assert orphaned.value.payload.code == "assessment_presentation_partial"

    # An audio presentation with no identity: nothing says which bytes were played.
    _damage(
        polish_workspace,
        run.run_id,
        content_id,
        presentation_json=json.dumps({"kind": "free-text", "audio": {"asset_key": "pl.audio.x"}}),
        asset_identity_json=None,
    )
    with pytest.raises(LinguaWikiError) as unheard:
        assessment_service.served_task(
            polish_workspace.paths, run=run.run_id, content_id=content_id
        )
    assert unheard.value.payload.code == "assessment_presentation_partial"


def test_a_null_snapshot_against_an_unchanged_bank_row_is_damage(
    polish_workspace: PolishWorkspace,
) -> None:
    """Null is truthful only for a row served before the column existed.

    If the bank row still hashes to what was served and it *has* a presentation, the
    content is provably identical and cannot have been served without one. Reading past
    that would take the answer from the mutable bank, which is the substitution the
    snapshot exists to prevent.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    _damage(polish_workspace, run.run_id, content_id, presentation_json=None)

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.served_task(
            polish_workspace.paths, run=run.run_id, content_id=content_id
        )
    assert failure.value.payload.code == "assessment_presentation_partial"


def test_a_row_that_predates_the_column_reads_as_free_text(
    polish_workspace: PolishWorkspace,
) -> None:
    """The same null, with no content hash to contradict it, is a legacy row."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    _damage(polish_workspace, run.run_id, content_id, presentation_json=None, content_hash=None)

    assert (
        assessment_service.served_task(
            polish_workspace.paths, run=run.run_id, content_id=content_id
        ).presentation
        is None
    )


@pytest.mark.parametrize(
    ("sha256", "code"),
    [
        ("a" * 64, "assessment_asset_unavailable"),
        ("b" * 64, "assessment_asset_unavailable"),
    ],
)
def test_an_unresolvable_recording_is_refused_rather_than_substituted(
    polish_workspace: PolishWorkspace, sha256: str, code: str
) -> None:
    """No pack ships audio, so every identity is unresolvable -- and says so by name.

    A substituted clip would be a different question asked under the identity of the one
    the learner answered, which is worse than no answer at all.
    """

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    _damage(
        polish_workspace,
        run.run_id,
        content_id,
        presentation_json=json.dumps({"kind": "free-text", "audio": {"asset_key": "pl.audio.x"}}),
        asset_identity_json=json.dumps({"content_id": "cnt_missing", "sha256": sha256}),
    )

    with pytest.raises(LinguaWikiError) as failure:
        assessment_service.served_task(
            polish_workspace.paths, run=run.run_id, content_id=content_id
        )
    assert failure.value.payload.code == code


def _checks(workspace: PolishWorkspace) -> dict[str, Any]:
    from linguawiki.db.integrity import check_database

    with open_reader(workspace.paths) as database:
        report = check_database(database)
    return {check.name: check for check in report.checks}


PRESENTATION_CHECKS = (
    "bank_presentation_wellformed",
    "served_presentation_complete",
    "served_presentation_wellformed",
)


def test_a_healthy_workspace_passes_every_presentation_check(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    _serve_until(polish_workspace, run.run_id, CHOOSER)

    checks = _checks(polish_workspace)

    for name in PRESENTATION_CHECKS:
        assert name in checks, f"{name} did not run"
        assert checks[name].status == "ok", checks[name].message


def test_db_check_finds_a_bank_presentation_that_contradicts_its_task(
    polish_workspace: PolishWorkspace,
) -> None:
    """The ALTER could carry no CHECK, so the rule lives here or nowhere."""

    content_id = _content_id(polish_workspace, CHOOSER)
    with (
        open_writer(polish_workspace.paths, command="test.damage") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_tasks SET task_type = 'extended-productive' WHERE content_id = ?",
            [content_id],
        )

    check = _checks(polish_workspace)["bank_presentation_wellformed"]

    assert check.status == "failed"
    assert content_id in "".join(check.context.values())


def test_db_check_finds_half_a_served_presentation_and_names_the_row(
    polish_workspace: PolishWorkspace,
) -> None:
    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    _damage(
        polish_workspace,
        run.run_id,
        content_id,
        presentation_json=None,
        asset_identity_json=json.dumps({"content_id": "cnt_x", "sha256": "a" * 64}),
    )

    check = _checks(polish_workspace)["served_presentation_complete"]

    assert check.status == "failed"
    assert content_id in "".join(check.context.values())


def test_db_check_reports_an_unreadable_presentation_rather_than_raising(
    polish_workspace: PolishWorkspace,
) -> None:
    """A diagnostic that aborts on damaged input tells an operator less than one that lies."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    _damage(
        polish_workspace,
        run.run_id,
        content_id,
        presentation_json="{not json at all",
        asset_identity_json=json.dumps({"content_id": "cnt_x"}),
    )

    check = _checks(polish_workspace)["served_presentation_wellformed"]

    assert check.status == "failed"
    reported = "".join(check.context.values())
    assert "presentation cannot be read" in reported
    assert "asset identity" in reported


def test_db_check_finds_an_asset_identity_missing_its_digest(
    polish_workspace: PolishWorkspace,
) -> None:
    """An identity with a key alone cannot answer the question it exists for."""

    run = assessment_service.start(polish_workspace.paths, clock=polish_workspace.clock)
    content_id = _serve_until(polish_workspace, run.run_id, CHOOSER)
    _damage(
        polish_workspace,
        run.run_id,
        content_id,
        presentation_json=json.dumps({"kind": "free-text", "audio": {"asset_key": "pl.audio.x"}}),
        asset_identity_json=json.dumps({"content_id": "cnt_x"}),
    )

    check = _checks(polish_workspace)["served_presentation_wellformed"]

    assert check.status == "failed"
    assert content_id in "".join(check.context.values())
