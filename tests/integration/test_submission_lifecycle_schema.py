"""C6 migration 0035: the submission rebuilt with its kind, verdicts, outcomes, and their checks.

Two halves. The migration is run for real against a database built at schema 34 holding C5
submissions in every status, because the backfill is the part a fresh database never
exercises. The named checks are then held to firing on a hand-built violation and passing
on the clean data the services write -- including a written answer, which names no
recording and no staging row and must not be reported for either.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import duckdb
import pytest

from linguawiki.db import integrity
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import open_writer
from linguawiki.db.integrity import check_database
from linguawiki.ids import IdPrefix, new_id
from tests.conftest import PolishWorkspace
from tests.integration.test_client_audio import judged_run, pending_task, permit_recording, rows
from tests.integration.test_db_check import _database_from_an_earlier_release
from tests.support.clocks import AdvancingClock

SCHEMA_BEFORE = 34


@pytest.fixture
def speaking(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    permit_recording(polish_workspace)
    return polish_workspace


# --- the migration ------------------------------------------------------------------------


def _id(prefix: IdPrefix) -> str:
    return new_id(prefix)


def _seed_c5_submissions(database: Any) -> dict[str, str]:
    """Parents for four served tasks, and a C5 submission in each status.

    Returns the identifiers the assertions name. Everything a foreign key at schema 34
    requires is inserted; nothing else is.
    """

    ids = {
        "user": _id(IdPrefix.USER),
        "pack": _id(IdPrefix.PACK),
        "track": _id(IdPrefix.TRACK),
        "definition": _id(IdPrefix.ASSESSMENT),
        "run": _id(IdPrefix.ASSESSMENT),
    }
    with database.transaction() as transaction:
        now = transaction.now()
        transaction.execute(
            "INSERT INTO users (user_id, workspace_id, display_name, timezone, created_at, "
            "updated_at) VALUES (?, 'wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV', 'Synthetic', 'UTC', ?, ?)",
            [ids["user"], now, now],
        )
        transaction.execute(
            "INSERT INTO proficiency_frameworks (framework_id, name, version, created_at) "
            "VALUES ('cefr', 'CEFR', '1', ?)",
            [now],
        )
        transaction.execute(
            "INSERT INTO language_packs (pack_id, pack_key, language_tag, created_at) "
            "VALUES (?, 'synthetic', 'pl', ?)",
            [ids["pack"], now],
        )
        transaction.execute(
            "INSERT INTO learning_tracks (track_id, user_id, target_language, "
            "proficiency_framework, timezone, created_at, updated_at, pack_id) "
            "VALUES (?, ?, 'pl', 'cefr', 'UTC', ?, ?, ?)",
            [ids["track"], ids["user"], now, now, ids["pack"]],
        )
        transaction.execute(
            "INSERT INTO assessment_definitions (definition_id, pack_id, form_key, version, "
            "purpose, framework_id, level_min, level_max, title, created_at) "
            "VALUES (?, ?, 'form', 1, 'placement', 'cefr', 'A1', 'B2', 'Synthetic', ?)",
            [ids["definition"], ids["pack"], now],
        )
        transaction.execute(
            "INSERT INTO assessment_runs (run_id, track_id, definition_id, run_type, status, "
            "algorithm_version, started_at, updated_at) "
            "VALUES (?, ?, ?, 'placement', 'in-progress', 'v1', ?, ?)",
            [ids["run"], ids["track"], ids["definition"], now, now],
        )
        for sequence, name in enumerate(("retaken", "judged", "withdrawn", "spare"), start=1):
            content_id = _id(IdPrefix.CONTENT)
            ids[f"task_{name}"] = content_id
            transaction.execute(
                "INSERT INTO content_records (content_id, content_kind, pack_id, stable_key, "
                "language_tag, content_hash, lifecycle, risk_tier, created_at, updated_at) "
                "VALUES (?, 'assessment_task', ?, ?, 'pl', ?, 'verified', 1, ?, ?)",
                [content_id, ids["pack"], f"task-{name}", "a" * 64, now, now],
            )
            transaction.execute(
                "INSERT INTO assessment_tasks (content_id, definition_id, dimension, task_type, "
                "level_code, difficulty, content_family, modality, prompt, created_at) "
                "VALUES (?, ?, 'pronunciation', 'connected-speech', 'A2', 0.0, ?, 'speech', "
                "'Powiedz to', ?)",
                [content_id, ids["definition"], f"family-{name}", now],
            )
            if name == "spare":
                continue  # in the bank, never served: something a pack edit may delete
            transaction.execute(
                "INSERT INTO assessment_run_tasks (run_id, sequence, content_id, dimension, "
                "status, served_at) VALUES (?, ?, ?, 'pronunciation', ?, ?)",
                [
                    ids["run"],
                    sequence,
                    content_id,
                    {"retaken": "served", "judged": "answered", "withdrawn": "skipped"}[name],
                    now,
                ],
            )
        for name in ("superseded", "pending", "judged", "withdrawn"):
            ids[f"artifact_{name}"] = _id(IdPrefix.ARTIFACT)
            ids[f"submission_{name}"] = _id(IdPrefix.ASSESSMENT)
            transaction.execute(
                "INSERT INTO artifacts (artifact_id, track_id, kind, relative_path, sha256, "
                "created_at, updated_at) VALUES (?, ?, 'audio', ?, ?, ?, ?)",
                [
                    ids[f"artifact_{name}"],
                    ids["track"],
                    f"artifacts/captures/{name}.wav",
                    hashlib.sha256(name.encode()).hexdigest(),
                    now,
                    now,
                ],
            )
        submissions = (
            ("superseded", "task_retaken", "superseded", ids["submission_pending"], None),
            ("pending", "task_retaken", "pending", None, None),
            ("judged", "task_judged", "judged", None, None),
            ("withdrawn", "task_withdrawn", "withdrawn", None, "assessment_audio_purged"),
        )
        for name, task, status, successor, code in submissions:
            transaction.execute(
                "INSERT INTO assessment_submissions (submission_id, run_id, content_id, "
                "capture_id, artifact_id, status, superseded_by, withdrawn_code, "
                "withdrawn_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ids[f"submission_{name}"],
                    ids["run"],
                    ids[task],
                    f"capture-{name}",
                    ids[f"artifact_{name}"],
                    status,
                    successor,
                    code,
                    None if code is None else "the recording was purged",
                    now,
                    now,
                ],
            )
        ids["result"] = _id(IdPrefix.ASSESSMENT)
        transaction.execute(
            "INSERT INTO assessment_results (result_id, run_id, content_id, dimension, "
            "raw_score, rubric_json, assessor_kind, assessor, confidence, difficulty, "
            "recorded_at, response_visibility, audio_artifact_id, judgement_policy_version) "
            "VALUES (?, ?, ?, 'pronunciation', 0.75, '{\"accuracy\": 0.75}', 'ai', "
            "'synthetic-ai-judge', 'medium', 0.0, ?, 'withheld', ?, 'judgement.v1')",
            [ids["result"], ids["run"], ids["task_judged"], now, ids["artifact_judged"]],
        )
    return ids


def test_c5_submissions_migrate_to_recordings_and_judged_ones_gain_their_verdict(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    paths = _database_from_an_earlier_release(tmp_path / "C5", clock, through=SCHEMA_BEFORE)
    with open_writer(paths, command="test.seed", clock=clock) as database:
        ids = _seed_c5_submissions(database)
        recorded_at = database.scalar(
            "SELECT recorded_at FROM assessment_results WHERE result_id = ?", [ids["result"]]
        )

        applied = migration_module.migrate(database)

        assert [migration.version for migration in applied] == [SCHEMA_BEFORE + 1]
        assert database.query(
            "SELECT status, kind, artifact_id IS NOT NULL, response_text IS NULL "
            "FROM assessment_submissions ORDER BY status"
        ) == [
            ("judged", "recording", True, True),
            ("pending", "recording", True, True),
            ("superseded", "recording", True, True),
            ("withdrawn", "recording", True, True),
        ]
        # The judged one, and only it, has the verdict that judged it: rebuilt from its
        # result, received when that was recorded, under no claim, and applied to it.
        verdicts = database.query(
            "SELECT verdict.submission_id, verdict.claim_id, verdict.raw_score, "
            "verdict.rubric_json, "
            "verdict.assessor_kind, verdict.assessor, verdict.confidence, "
            "verdict.response_visibility, verdict.received_at, outcome.outcome, "
            "outcome.result_id, outcome.code, outcome.decided_at "
            "FROM assessment_verdicts verdict "
            "LEFT JOIN assessment_verdict_outcomes outcome USING (verdict_id)"
        )
        assert verdicts == [
            (
                ids["submission_judged"],
                None,
                0.75,
                # The rubric never went through retention, so it is not copied: one copy
                # stays on the result the outcome names.
                "{}",
                "ai",
                "synthetic-ai-judge",
                "medium",
                "withheld",
                recorded_at,
                "applied",
                ids["result"],
                None,
                recorded_at,
            )
        ]
        verdict_id = str(database.scalar("SELECT verdict_id FROM assessment_verdicts"))
        assert verdict_id.startswith("asm_") and len(verdict_id) == 30
        # Older than the column, so it never recorded when the learner answered.
        assert (
            database.scalar(
                "SELECT observed_at FROM assessment_results WHERE result_id = ?", [ids["result"]]
            )
            is None
        )
        # The rebuild kept the index: one capture is still one submission.
        with pytest.raises(duckdb.ConstraintException), database.transaction() as transaction:
            transaction.execute(
                "INSERT INTO assessment_submissions (submission_id, run_id, content_id, kind, "
                "capture_id, artifact_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'recording', 'capture-pending', ?, 'pending', ?, ?)",
                [
                    _id(IdPrefix.ASSESSMENT),
                    ids["run"],
                    ids["task_judged"],
                    ids["artifact_judged"],
                    transaction.now(),
                    transaction.now(),
                ],
            )
        # The rebuilt table is known to the tables it references under its own name. One
        # created under another name and renamed was not, and deleting any task from the
        # bank then failed looking for the table by the name it was created with.
        with database.transaction() as transaction:
            transaction.execute(
                "DELETE FROM assessment_tasks WHERE content_id = ?", [ids["task_spare"]]
            )
        # And the C5 data satisfies every rule 0035 brings with it.
        lifecycle = {
            check.name: check for check in integrity._submission_lifecycle_checks(database)
        }
    assert {name: check.status for name, check in lifecycle.items()} == {
        "submission_kind_shape": "ok",
        "one_applied_verdict_per_submission": "ok",
        "applied_verdicts_name_their_result": "ok",
        "held_verdicts_on_paused_runs": "ok",
        "judged_submissions_have_an_applied_verdict": "ok",
        "result_observation_times": "ok",
    }


def test_a_judged_submission_whose_result_is_missing_is_reported_rather_than_invented(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    """The backfill derives a verdict from the result; with no result it has nothing to say."""

    paths = _database_from_an_earlier_release(tmp_path / "C5", clock, through=SCHEMA_BEFORE)
    with open_writer(paths, command="test.seed", clock=clock) as database:
        ids = _seed_c5_submissions(database)
        with database.transaction() as transaction:
            transaction.execute("DELETE FROM assessment_results")
        migration_module.migrate(database)

        assert database.scalar("SELECT count(*) FROM assessment_verdicts") == 0
        lifecycle = {
            check.name: check for check in integrity._submission_lifecycle_checks(database)
        }
    unaccounted = lifecycle["judged_submissions_have_an_applied_verdict"]
    assert unaccounted.status == "failed"
    assert unaccounted.context["submissions"] == ids["submission_judged"]


# --- the rebuilt table's own constraints ---------------------------------------------------


def _text_submission(run_id: str, content_id: str, **overrides: Any) -> tuple[str, list[Any]]:
    text = "Dzień dobry, nazywam się Anna."
    values: dict[str, Any] = {
        "submission_id": _id(IdPrefix.ASSESSMENT),
        "run_id": run_id,
        "content_id": content_id,
        "kind": "text",
        "capture_id": f"key-{_id(IdPrefix.EVENT)}",
        "artifact_id": None,
        "response_visibility": "full",
        "response_text": text,
        "response_digest": hashlib.sha256(text.encode()).hexdigest(),
        "status": "pending",
        "superseded_by": None,
        "withdrawn_code": None,
        "withdrawn_reason": None,
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return (
        f"INSERT INTO assessment_submissions ({columns}, created_at, updated_at) "
        f"VALUES ({marks}, now(), now())",
        list(values.values()),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"}, id="text-with-artifact"),
        pytest.param({"kind": "recording"}, id="recording-without-artifact"),
        pytest.param({"response_digest": None}, id="text-without-digest"),
        pytest.param({"response_visibility": None}, id="text-without-visibility"),
        pytest.param({"response_text": None}, id="pending-text-without-text"),
        pytest.param({"response_text": "   "}, id="blank-text"),
        pytest.param({"response_visibility": "withheld"}, id="withheld-keeping-text"),
        pytest.param({"kind": "video"}, id="unknown-kind"),
        pytest.param(
            {"kind": "recording", "artifact_id": "art_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
            id="recording-carrying-text",
        ),
        pytest.param({"status": "superseded"}, id="superseded-naming-no-successor"),
        pytest.param(
            {"withdrawn_code": "assessment_audio_purged", "withdrawn_reason": "purged"},
            id="pending-with-a-withdrawal",
        ),
        pytest.param(
            {"status": "withdrawn", "withdrawn_code": "assessment_audio_purged"},
            id="withdrawn-without-a-reason",
        ),
    ],
)
def test_the_rebuilt_table_refuses_a_submission_not_shaped_like_its_kind(
    speaking: PolishWorkspace, overrides: dict[str, Any]
) -> None:
    run_id, content_id, _ = pending_task(speaking, seed=11)
    sql, parameters = _text_submission(run_id, content_id, **overrides)

    with (
        open_writer(speaking.paths, command="test.tamper", clock=speaking.clock) as database,
        pytest.raises(duckdb.ConstraintException),
        database.transaction() as transaction,
    ):
        transaction.execute(sql, parameters)


def test_a_withdrawn_written_answer_may_lose_its_text_and_keeps_its_digest(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, _ = pending_task(speaking, seed=12)
    sql, parameters = _text_submission(
        run_id,
        content_id,
        status="withdrawn",
        response_text=None,
        withdrawn_code="assessment_response_not_retained",
        withdrawn_reason="transcript consent withdrawn",
    )
    with open_writer(speaking.paths, command="test.write", clock=speaking.clock) as database:
        with database.transaction() as transaction:
            transaction.execute(sql, parameters)
        assert (
            database.scalar(
                "SELECT count(*) FROM assessment_submissions WHERE kind = 'text' "
                "AND response_text IS NULL AND response_digest IS NOT NULL"
            )
            == 1
        )


# --- the named checks ---------------------------------------------------------------------


def _statuses(database: Any) -> dict[str, Any]:
    return {check.name: check for check in check_database(database).checks}


def _tamper(workspace: PolishWorkspace, *statements: tuple[str, list[Any]]) -> dict[str, Any]:
    with open_writer(workspace.paths, command="test.tamper", clock=workspace.clock) as database:
        with database.transaction() as transaction:
            for sql, parameters in statements:
                transaction.execute(sql, parameters)
        return _statuses(database)


def _judged(workspace: PolishWorkspace) -> tuple[str, str, str]:
    """A judged recording: `(submission_id, result_id, verdict_id)`."""

    run_id, _, _ = judged_run(workspace, seed=21)
    (submission_id, result_id, verdict_id) = rows(
        workspace,
        "SELECT submission.submission_id, outcome.result_id, verdict.verdict_id "
        "FROM assessment_submissions submission "
        "JOIN assessment_verdicts verdict USING (submission_id) "
        "JOIN assessment_verdict_outcomes outcome USING (verdict_id) "
        "WHERE submission.run_id = ?",
        [run_id],
    )[0]
    return str(submission_id), str(result_id), str(verdict_id)


def _held_verdict(submission_id: str) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO assessment_verdicts (verdict_id, submission_id, raw_score, assessor_kind, "
        "assessor, confidence, received_at) VALUES (?, ?, 0.5, 'ai', 'judge', 'medium', now())",
        [_id(IdPrefix.ASSESSMENT), submission_id],
    )


def test_a_judged_recording_is_recorded_as_an_applied_verdict_dated_by_its_submission(
    speaking: PolishWorkspace,
) -> None:
    run_id, _, _ = judged_run(speaking, seed=20)
    pending_task(speaking, seed=19)

    stored = rows(
        speaking,
        "SELECT verdict.claim_id, verdict.raw_score, verdict.rubric_json, outcome.outcome, "
        "result.observed_at = submission.created_at, result.recorded_at > result.observed_at "
        "FROM assessment_submissions submission "
        "JOIN assessment_verdicts verdict USING (submission_id) "
        "JOIN assessment_verdict_outcomes outcome USING (verdict_id) "
        "JOIN assessment_results result ON result.result_id = outcome.result_id "
        "WHERE submission.run_id = ?",
        [run_id],
    )

    # The verdict carries the rubric as retention kept it (Task 2); only backfilled C5
    # verdicts hold '{}', and there it means "not copied", not "the judge gave none".
    assert stored == [(None, 0.9, '{"accuracy": 0.9}', "applied", True, True)]
    with open_writer(speaking.paths, command="test.read", clock=speaking.clock) as database:
        report = check_database(database)
    assert report.ok, [check for check in report.checks if check.status == "failed"]


def test_a_written_answer_is_held_to_neither_a_recording_nor_a_staging_row(
    speaking: PolishWorkspace,
) -> None:
    """A text submission names no artifact and no capture: neither may be reported missing."""

    submission_id, _, _ = _judged(speaking)
    run_id, content_id = rows(
        speaking,
        "SELECT run_id, content_id FROM assessment_submissions WHERE submission_id = ?",
        [submission_id],
    )[0]
    # Superseded by the judged recording, so the task still has exactly one live answer.
    checks = _tamper(
        speaking,
        _text_submission(
            str(run_id), str(content_id), status="superseded", superseded_by=submission_id
        ),
    )

    failed = {name: check for name, check in checks.items() if check.status == "failed"}
    assert failed == {}
    assert checks["orphan_relations"].status == "ok"
    assert checks["submission_artifacts_agree"].status == "ok"


def test_a_recording_whose_capture_is_missing_is_still_an_orphan(
    speaking: PolishWorkspace,
) -> None:
    pending_task(speaking, seed=22)
    checks = _tamper(
        speaking,
        ("DELETE FROM capture_stagings", []),
    )

    assert checks["orphan_relations"].status == "failed"
    assert (
        "assessment_submissions.capture_id -> capture_stagings"
        in (checks["orphan_relations"].context["orphans"])
    )


def _unconstrained_submissions() -> tuple[tuple[str, list[Any]], ...]:
    """A restore that predates the CHECKs: the same columns, no constraints."""

    return (
        ("CREATE TABLE submissions_copy AS SELECT * FROM assessment_submissions", []),
        ("DROP TABLE assessment_submissions", []),
        ("ALTER TABLE submissions_copy RENAME TO assessment_submissions", []),
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("kind = 'text'", "a written answer naming an artifact"),
        ("artifact_id = NULL", "a recording with no artifact"),
        ("response_digest = repeat('b', 64)", "a recording carrying text"),
        ("kind = NULL", "kind is missing"),
        ("superseded_by = submission_id", "superseded exactly when it names a successor"),
        ("status = 'withdrawn'", "withdrawn exactly when it gives a code and a reason"),
    ],
)
def test_submission_kind_shape_fires_on_a_restore_that_predates_the_constraints(
    speaking: PolishWorkspace, change: str, reason: str
) -> None:
    pending_task(speaking, seed=23)
    checks = _tamper(
        speaking,
        *_unconstrained_submissions(),
        (f"UPDATE assessment_submissions SET {change}", []),
    )

    assert checks["submission_kind_shape"].status == "failed"
    assert reason in checks["submission_kind_shape"].context["submissions"]


def test_submission_kind_shape_fires_on_a_written_answer_that_lost_its_text_while_waiting(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, _ = pending_task(speaking, seed=24)
    sql, parameters = _text_submission(
        run_id,
        content_id,
        status="withdrawn",
        response_text=None,
        withdrawn_code="assessment_response_not_retained",
        withdrawn_reason="transcript consent withdrawn",
    )
    checks = _tamper(
        speaking,
        *_unconstrained_submissions(),
        (sql, parameters),
        (
            "UPDATE assessment_submissions SET status = 'pending', withdrawn_code = NULL, "
            "withdrawn_reason = NULL WHERE kind = 'text'",
            [],
        ),
    )

    assert checks["submission_kind_shape"].status == "failed"
    assert (
        "whose text is gone while it still waits"
        in (checks["submission_kind_shape"].context["submissions"])
    )


def test_a_second_applied_verdict_for_one_submission_is_reported(
    speaking: PolishWorkspace,
) -> None:
    submission_id, result_id, _ = _judged(speaking)
    verdict_id = _id(IdPrefix.ASSESSMENT)
    checks = _tamper(
        speaking,
        (
            "INSERT INTO assessment_verdicts (verdict_id, submission_id, raw_score, "
            "assessor_kind, assessor, confidence, received_at) "
            "VALUES (?, ?, 0.2, 'ai', 'judge', 'medium', now())",
            [verdict_id, submission_id],
        ),
        (
            "INSERT INTO assessment_verdict_outcomes (verdict_id, outcome, result_id, "
            "decided_at) VALUES (?, 'applied', ?, now())",
            [verdict_id, result_id],
        ),
    )

    assert checks["one_applied_verdict_per_submission"].status == "failed"
    assert checks["one_applied_verdict_per_submission"].context["submissions"] == (
        f"{submission_id} (2)"
    )
    assert (
        "claimed by another verdict too"
        in (checks["applied_verdicts_name_their_result"].context["verdicts"])
    )


def test_an_applied_verdict_naming_another_tasks_result_is_reported(
    speaking: PolishWorkspace,
) -> None:
    _, result_id, verdict_id = _judged(speaking)
    run_id, content_id, _ = pending_task(speaking, seed=25)
    other = str(
        rows(
            speaking,
            "SELECT submission_id FROM assessment_submissions WHERE run_id = ? AND content_id = ?",
            [run_id, content_id],
        )[0][0]
    )
    checks = _tamper(
        speaking,
        (
            "UPDATE assessment_verdicts SET submission_id = ? WHERE verdict_id = ?",
            [other, verdict_id],
        ),
    )

    assert checks["applied_verdicts_name_their_result"].status == "failed"
    assert checks["applied_verdicts_name_their_result"].context["verdicts"] == (
        f"{verdict_id} (its result answers another task)"
    )


def test_an_applied_verdict_naming_a_missing_result_is_reported(
    speaking: PolishWorkspace,
) -> None:
    _, _, verdict_id = _judged(speaking)
    missing = _id(IdPrefix.ASSESSMENT)
    checks = _tamper(
        speaking,
        (
            "UPDATE assessment_verdict_outcomes SET result_id = ? WHERE verdict_id = ?",
            [missing, verdict_id],
        ),
    )

    assert checks["applied_verdicts_name_their_result"].context["verdicts"] == (
        f"{verdict_id} (its result does not exist)"
    )
    assert (
        "assessment_verdict_outcomes.result_id -> assessment_results"
        in (checks["orphan_relations"].context["orphans"])
    )


def test_a_held_verdict_is_reported_unless_its_run_is_paused(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, _ = pending_task(speaking, seed=26)
    submission_id = str(
        rows(
            speaking,
            "SELECT submission_id FROM assessment_submissions WHERE run_id = ?",
            [run_id],
        )[0][0]
    )

    checks = _tamper(speaking, _held_verdict(submission_id))

    assert checks["held_verdicts_on_paused_runs"].status == "failed"
    assert (
        "held on a run that is in-progress"
        in (checks["held_verdicts_on_paused_runs"].context["verdicts"])
    )

    paused = _tamper(
        speaking, ("UPDATE assessment_runs SET status = 'paused' WHERE run_id = ?", [run_id])
    )

    assert paused["held_verdicts_on_paused_runs"].status == "ok"
    assert content_id


def test_a_judged_submission_with_no_applied_verdict_is_reported(
    speaking: PolishWorkspace,
) -> None:
    submission_id, _, verdict_id = _judged(speaking)
    checks = _tamper(
        speaking,
        ("DELETE FROM assessment_verdict_outcomes WHERE verdict_id = ?", [verdict_id]),
    )

    assert checks["judged_submissions_have_an_applied_verdict"].status == "failed"
    assert checks["judged_submissions_have_an_applied_verdict"].context["submissions"] == (
        submission_id
    )


def test_a_result_written_after_the_migration_with_no_observation_time_is_reported(
    speaking: PolishWorkspace,
) -> None:
    _, result_id, _ = _judged(speaking)
    checks = _tamper(
        speaking,
        ("UPDATE assessment_results SET observed_at = NULL WHERE result_id = ?", [result_id]),
    )

    assert checks["result_observation_times"].status == "failed"
    assert checks["result_observation_times"].context["results"] == (
        f"{result_id} (recorded after migration 0035 with no observation time)"
    )


def test_a_bound_result_dated_by_its_verdict_rather_than_its_submission_is_reported(
    speaking: PolishWorkspace,
) -> None:
    _, result_id, _ = _judged(speaking)
    checks = _tamper(
        speaking,
        (
            "UPDATE assessment_results SET observed_at = recorded_at WHERE result_id = ?",
            [result_id],
        ),
    )

    assert checks["result_observation_times"].context["results"] == (
        f"{result_id} (observed_at is not when its submission was made)"
    )


def test_an_unbound_result_must_be_observed_when_it_was_recorded(
    speaking: PolishWorkspace,
) -> None:
    _, result_id, verdict_id = _judged(speaking)
    checks = _tamper(
        speaking,
        # Unbound: nothing applied it from a submission, so it was observed when recorded.
        ("DELETE FROM assessment_verdict_outcomes WHERE verdict_id = ?", [verdict_id]),
    )

    assert checks["result_observation_times"].context["results"] == (
        f"{result_id} (observed_at is not when it was recorded)"
    )
