"""C5: a spoken answer recorded in the browser, judged, purged, and honest afterwards.

The recordings are generated tones (`tests/support/recordings.py`), never anything anybody
said. What is under test is the account the workspace keeps of them: every byte owned by a
row from the moment it exists, every verdict resting on a recording that is still there,
and every claim marked -- never deleted -- when the recording goes.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from linguawiki.db.connection import open_reader, open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import assessment as assessment_service
from linguawiki.services import learners as learner_service
from linguawiki.services import recordings as recording_service
from tests.conftest import PolishWorkspace
from tests.support.recordings import tone_wav

SPOKEN = ("pronunciation", "spoken-production")


def capture_id() -> str:
    return str(uuid.uuid4())


def spoken_bytes(seed: int) -> bytes:
    """A distinct generated recording per call site, so no two collide by accident."""

    return tone_wav(200.0 + 7.0 * seed, milliseconds=250)


def permit_recording(workspace: PolishWorkspace, **preferences: Any) -> None:
    settings = {"audio_recording_available": True, "audio_retention_consent": True}
    settings.update(preferences)
    learner_service.update_track(
        workspace.paths,
        preferences=learner_service.TrackPreferences(
            voice_available=True, transcript_retention_consent=True, **settings
        ),
        clock=workspace.clock,
    )


@pytest.fixture
def speaking(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    permit_recording(polish_workspace)
    return polish_workspace


def start_spoken(workspace: PolishWorkspace, dimensions: tuple[str, ...] = SPOKEN) -> str:
    run = assessment_service.start(
        workspace.paths,
        dimensions=list(dimensions),
        modalities=["text", "audio", "speech"],
        scoring="machine+recorded",
        clock=workspace.clock,
    )
    return run.run_id


def serve(workspace: PolishWorkspace, run_id: str) -> assessment_service.NextTaskReport:
    served = assessment_service.next_task(workspace.paths, run=run_id, clock=workspace.clock)
    assert isinstance(served, assessment_service.NextTaskReport), served
    return served


def take(
    workspace: PolishWorkspace,
    run_id: str,
    content_id: str,
    *,
    data: bytes,
    identifier: str | None = None,
) -> recording_service.CaptureReport:
    return recording_service.capture(
        workspace.paths,
        run=run_id,
        content_id=content_id,
        capture_id=identifier or capture_id(),
        data=data,
        media_type="audio/wav",
        clock=workspace.clock,
        actor="client",
    )


def judge(
    workspace: PolishWorkspace,
    run_id: str,
    content_id: str,
    artifact_id: str | None,
    *,
    score: float = 0.75,
    confidence: str = "medium",
    assessor: str | None = "synthetic-ai-judge",
    assessor_kind: str = "ai",
) -> assessment_service.AssessmentRunReport:
    return assessment_service.record(
        workspace.paths,
        run=run_id,
        content_id=content_id,
        score=score,
        audio_artifact=artifact_id,
        assessor_kind=assessor_kind,
        assessor=assessor,
        confidence=confidence,
        rubric={"accuracy": score},
        clock=workspace.clock,
    )


def rows(workspace: PolishWorkspace, sql: str, parameters: list[Any] | None = None) -> list[Any]:
    with open_reader(workspace.paths) as database:
        return database.query(sql, parameters or [])


def private_files(root: Path) -> set[str]:
    found = set()
    for top in ("artifacts", "imports", "staging"):
        base = root / top
        if base.is_dir():
            found |= {
                path.relative_to(root).as_posix() for path in base.rglob("*") if path.is_file()
            }
    return found


def assert_everything_accounted_for(workspace: PolishWorkspace) -> None:
    """No file under a private root that no row accounts for."""

    owned = {
        str(path)
        for (path,) in rows(
            workspace,
            "SELECT relative_path FROM artifacts WHERE purged_at IS NULL AND retained",
        )
    } | {
        str(path)
        for (path,) in rows(
            workspace,
            "SELECT staged_path FROM capture_stagings WHERE state IN ('staged', 'promoting') "
            "UNION SELECT final_path FROM capture_stagings WHERE state = 'promoting'",
        )
    }
    stray = private_files(workspace.root) - owned
    assert not stray, f"files nothing accounts for: {sorted(stray)}"


# --- end to end, at the service layer ------------------------------------------------------


def test_a_spoken_answer_is_captured_judged_and_counted(speaking: PolishWorkspace) -> None:
    run_id = start_spoken(speaking)
    task = serve(speaking, run_id)
    assert task.modality == "speech"

    taken = take(speaking, run_id, task.content_id, data=spoken_bytes(1))

    assert taken.state == "registered" and taken.submission is not None
    assert taken.submission.status == "pending"
    artifact = rows(
        speaking,
        "SELECT external_id, origin, retained, relative_path FROM artifacts WHERE artifact_id = ?",
        [taken.artifact_id],
    )[0]
    assert artifact[0] is None and artifact[1] == "learner-recording" and artifact[2] is True
    assert str(artifact[3]).startswith("artifacts/captures/")
    verified = artifact_service.verify(speaking.paths)
    assert verified.ok and verified.present == 1

    handed = recording_service.pending(speaking.paths, run=run_id)
    assert [entry.submission.capture_id for entry in handed.pending] == [taken.capture_id]
    assert handed.pending[0].judgeable and handed.pending[0].audio_path is not None
    assert Path(handed.pending[0].audio_path).read_bytes() == spoken_bytes(1)

    report = judge(speaking, run_id, task.content_id, taken.artifact_id)

    assert report.tasks_recorded == 1
    stored = rows(
        speaking,
        "SELECT audio_artifact_id, judgement_policy_version, assessor, confidence "
        "FROM assessment_results WHERE run_id = ?",
        [run_id],
    )
    assert stored == [(taken.artifact_id, "judgement.v1", "synthetic-ai-judge", "medium")]
    assert rows(speaking, "SELECT status FROM assessment_submissions") == [("judged",)]
    assert_everything_accounted_for(speaking)
    checked = database_check(speaking)
    assert checked.ok, [entry for entry in checked.checks if entry.status == "failed"]
    assert taken.submission.capture_id == taken.capture_id


def database_check(workspace: PolishWorkspace) -> Any:
    from linguawiki.services import database as database_service

    return database_service.check(workspace.paths)


def failed_checks(workspace: PolishWorkspace) -> dict[str, Any]:
    return {
        entry.name: entry for entry in database_check(workspace).checks if entry.status == "failed"
    }


def refused(call: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(LinguaWikiError) as failure:
        call(*args, **kwargs)
    return str(failure.value.payload.code)


# --- eligibility and consent -------------------------------------------------------------


def test_equipment_without_consent_never_serves_or_offers_speech(
    polish_workspace: PolishWorkspace,
) -> None:
    """`audio_recording_available` is equipment, not consent, and gates nothing alone."""

    permit_recording(polish_workspace, audio_retention_consent=False)
    run = assessment_service.start(
        polish_workspace.paths,
        dimensions=list(SPOKEN),
        modalities=["text", "audio", "speech"],
        scoring="machine+recorded",
        clock=polish_workspace.clock,
    )

    assert {entry.dimension: entry.status for entry in run.dimensions} == {
        "pronunciation": "not-tested",
        "spoken-production": "not-tested",
    }
    assert all("recording" in str(entry.unavailable_reason) for entry in run.dimensions)
    from linguawiki.services import assessment_view as view_service

    screen = view_service.run_screen(polish_workspace.paths, run=run.run_id)
    assert screen.recording is not None and not screen.recording.offered
    assert "kept" in str(screen.recording.reason)


def test_a_track_forbidding_recording_never_writes_a_file(
    polish_workspace: PolishWorkspace,
) -> None:
    """Consent is checked before any bytes are persisted, by the service and not the page."""

    # An `any` run serves the spoken task regardless; the capture is what must refuse.
    run = assessment_service.start(
        polish_workspace.paths,
        dimensions=["pronunciation"],
        modalities=["speech"],
        clock=polish_workspace.clock,
    )
    task = serve(polish_workspace, run.run_id)

    code = refused(take, polish_workspace, run.run_id, task.content_id, data=spoken_bytes(2))

    assert code == "capture_not_permitted"
    assert private_files(polish_workspace.root) == set()
    assert rows(polish_workspace, "SELECT count(*) FROM capture_stagings") == [(0,)]


def test_withdrawn_consent_stops_the_next_spoken_serve(speaking: PolishWorkspace) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    permit_recording(speaking, audio_retention_consent=False)

    outcome = assessment_service.next_task(speaking.paths, run=run_id, clock=speaking.clock)

    assert not isinstance(outcome, assessment_service.NextTaskReport)


def test_a_listening_task_with_no_recording_stays_unservable_under_recorded_scoring(
    speaking: PolishWorkspace,
) -> None:
    run = assessment_service.start(
        speaking.paths,
        dimensions=["listening"],
        modalities=["text", "audio", "speech"],
        scoring="machine+recorded",
        clock=speaking.clock,
    )

    assert run.dimensions[0].status == "not-tested"
    assert run.dimensions[0].unavailable_reason == "its listening tasks ship no recording"


# --- the judgement policy ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("assessor", "confidence", "code"),
    [
        (None, "medium", "assessor_required"),
        ("   ", "low", "assessor_required"),
        ("synthetic-ai-judge", "high", "assessor_confidence_ceiling"),
    ],
)
def test_an_ai_verdict_claims_no_more_than_its_ceiling(
    speaking: PolishWorkspace, assessor: str | None, confidence: str, code: str
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    taken = take(speaking, run_id, task.content_id, data=spoken_bytes(3))

    assert (
        refused(
            judge,
            speaking,
            run_id,
            task.content_id,
            taken.artifact_id,
            assessor=assessor,
            confidence=confidence,
        )
        == code
    )
    # Refused before anything was written, so the task still takes the honest verdict.
    judge(speaking, run_id, task.content_id, taken.artifact_id, confidence="medium")
    assert rows(speaking, "SELECT judgement_policy_version FROM assessment_results") == [
        ("judgement.v1",)
    ]


def test_a_human_verdict_must_name_its_judge(speaking: PolishWorkspace) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    taken = take(speaking, run_id, task.content_id, data=spoken_bytes(4))

    code = refused(
        judge,
        speaking,
        run_id,
        task.content_id,
        taken.artifact_id,
        assessor=None,
        assessor_kind="human",
        confidence="high",
    )

    assert code == "assessor_required"
    judge(
        speaking,
        run_id,
        task.content_id,
        taken.artifact_id,
        assessor="a synthetic teacher",
        assessor_kind="human",
        confidence="high",
    )


def test_a_verdict_must_name_the_submitted_recording(speaking: PolishWorkspace) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    take(speaking, run_id, task.content_id, data=spoken_bytes(5))

    assert refused(judge, speaking, run_id, task.content_id, None) == (
        "assessment_audio_artifact_required"
    )
    assert (
        refused(
            judge,
            speaking,
            run_id,
            task.content_id,
            rows_artifact(speaking),
            assessor_kind="learner",
        )
        == "assessment_judge_required"
    )


def rows_artifact(workspace: PolishWorkspace) -> str:
    return str(rows(workspace, "SELECT artifact_id FROM assessment_submissions LIMIT 1")[0][0])


# --- supersession and duplicates ---------------------------------------------------------


def test_a_second_capture_before_the_verdict_supersedes_and_purges_the_first(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    first = take(speaking, run_id, task.content_id, data=spoken_bytes(6))
    second = take(speaking, run_id, task.content_id, data=spoken_bytes(7))

    assert first.submission is not None and second.submission is not None
    assert second.superseded == (first.submission.submission_id,)
    statuses = dict(rows(speaking, "SELECT submission_id, status FROM assessment_submissions"))
    assert statuses == {
        first.submission.submission_id: "superseded",
        second.submission.submission_id: "pending",
    }
    purged = rows(
        speaking,
        "SELECT purge_reason FROM artifacts WHERE artifact_id = ?",
        [first.artifact_id],
    )
    assert purged == [("superseded",)]
    assert not any(first.capture_id in path for path in private_files(speaking.root))
    assert_everything_accounted_for(speaking)

    judge(speaking, run_id, task.content_id, second.artifact_id)
    # After the verdict the learner's answer was taken. A recording for a task already
    # judged is refused -- and the task is no longer outstanding anyway.
    code = refused(take, speaking, run_id, task.content_id, data=spoken_bytes(8))
    assert code == "assessment_task_already_judged"
    assert not failed_checks(speaking)


def test_a_capture_for_a_judged_task_is_refused_by_name(speaking: PolishWorkspace) -> None:
    """The judged branch of the preflight, reached directly: a task still `served` whose
    submission was judged cannot exist through public calls, so the check is on the rule."""

    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    taken = take(speaking, run_id, task.content_id, data=spoken_bytes(9))
    with (
        open_writer(speaking.paths, command="test.damage") as database,
        database.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE assessment_submissions SET status = 'judged' WHERE capture_id = ?",
            [taken.capture_id],
        )

    code = refused(take, speaking, run_id, task.content_id, data=spoken_bytes(10))

    assert code == "assessment_task_already_judged"


def test_byte_identical_captures_refuse_the_second_and_leave_no_file(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking)
    first_task = serve(speaking, run_id)
    second_task = serve(speaking, run_id)
    silent = spoken_bytes(11)
    first = take(speaking, run_id, first_task.content_id, data=silent)

    with pytest.raises(LinguaWikiError) as failure:
        take(speaking, run_id, second_task.content_id, data=silent)

    assert failure.value.payload.code == "capture_duplicate_bytes"
    assert first.artifact_id in failure.value.payload.message
    assert rows(speaking, "SELECT count(*) FROM assessment_submissions") == [(1,)]
    assert_everything_accounted_for(speaking)


def test_a_reused_capture_identifier_with_different_bytes_is_a_conflict(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    identifier = capture_id()
    first = take(speaking, run_id, task.content_id, data=spoken_bytes(12), identifier=identifier)

    replay = take(speaking, run_id, task.content_id, data=spoken_bytes(12), identifier=identifier)
    assert replay.replayed and replay.artifact_id == first.artifact_id
    with pytest.raises(LinguaWikiError) as failure:
        take(speaking, run_id, task.content_id, data=spoken_bytes(13), identifier=identifier)
    assert failure.value.payload.code == "capture_conflict"
    assert first.sha256 in str(failure.value.payload.details)
    assert rows(speaking, "SELECT count(*) FROM artifacts") == [(1,)]


# --- invalidation ------------------------------------------------------------------------


def judged_run(
    workspace: PolishWorkspace, *, seed: int, score: float = 0.9, finalize: bool = True
) -> tuple[str, str, str]:
    """A pronunciation run with one judged recording: `(run_id, content_id, artifact_id)`."""

    run_id = start_spoken(workspace, ("pronunciation",))
    task = serve(workspace, run_id)
    taken = take(workspace, run_id, task.content_id, data=spoken_bytes(seed))
    assert taken.artifact_id is not None
    judge(workspace, run_id, task.content_id, taken.artifact_id, score=score)
    if finalize:
        assessment_service.finalize(workspace.paths, run=run_id, clock=workspace.clock)
    return run_id, task.content_id, taken.artifact_id


def estimate(workspace: PolishWorkspace, dimension: str = "pronunciation") -> Any:
    from linguawiki.services import estimates as estimate_service

    with open_reader(workspace.paths) as database:
        track_id = learner_service.resolve_track(database, None)
        return estimate_service.read_estimate(database, track_id=track_id, dimension=dimension)


def test_purging_the_only_recording_invalidates_its_result_and_rebuilds_the_estimate(
    speaking: PolishWorkspace,
) -> None:
    from linguawiki.services import estimates as estimate_service

    run_id, content_id, artifact_id = judged_run(speaking, seed=20)
    measured = estimate(speaking)
    assert measured.estimate_status == "estimated" and measured.source_run_id == run_id

    dry = artifact_service.purge(speaking.paths, artifact=artifact_id, dry_run=True)
    assert len(dry.invalidated_results) == 1
    purged = artifact_service.purge(speaking.paths, artifact=artifact_id)

    assert purged.invalidated_results == dry.invalidated_results
    stored = rows(
        speaking,
        "SELECT invalidated_at IS NOT NULL, invalidated_reason FROM assessment_results",
    )
    assert stored[0][0] is True and "purged" in str(stored[0][1])
    # The result is marked, never deleted; the run says what it no longer knows.
    report = assessment_service.report(speaking.paths, run=run_id)
    assert report.tasks_recorded == 0 and report.results_invalidated == 1
    pronunciation = next(entry for entry in report.dimensions if entry.dimension == "pronunciation")
    assert pronunciation.tasks_used == 0 and pronunciation.status == "not-tested"
    # The current estimate falls back to the declared hypothesis -- never higher -- and
    # says the evidence was withdrawn.
    rebuilt = estimate(speaking)
    assert rebuilt.estimate_status == "provisional"
    assert rebuilt.basis == "declared-hypothesis" and rebuilt.level_code == "A2"
    assert rebuilt.evidence_count == 0
    history = estimate_service.history(speaking.paths, dimension="pronunciation")
    newest = history.snapshots[0]
    assert any(factor.name == "evidence-withdrawn" for factor in newest.factors)
    assert not newest.annotations
    # The earlier snapshot survives as it was, annotated.
    earlier = [snapshot for snapshot in history.snapshots[1:] if snapshot.annotations]
    assert earlier and earlier[0].estimate_status == "estimated"
    assert earlier[0].annotations[0].artifact_id == artifact_id
    # And a verdict for the invalidated task is refused by its own name.
    assert refused(judge, speaking, run_id, content_id, artifact_id) == (
        "assessment_result_invalidated"
    )
    assert not failed_checks(speaking)


def test_with_no_declared_level_the_estimate_returns_to_not_tested(
    speaking: PolishWorkspace,
) -> None:
    with (
        open_writer(speaking.paths, command="test.declared") as database,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE learning_tracks SET declared_level = NULL")
    _, _, artifact_id = judged_run(speaking, seed=21)

    artifact_service.purge(speaking.paths, artifact=artifact_id)

    rebuilt = estimate(speaking)
    assert rebuilt.estimate_status == "not-tested" and rebuilt.level_code is None


def test_an_invalidated_result_is_reported_but_never_folded_into_evidence(
    speaking: PolishWorkspace,
) -> None:
    """`record` writes no evidence rows, so `dimension_observations` needs no filter today.

    Pinned, so that a later change that makes it write them must add the filter too.
    """

    judged_run(speaking, seed=22)

    assert rows(speaking, "SELECT count(*) FROM evidence") == [(0,)]


def test_a_purge_during_judging_refuses_the_verdict_and_skips_the_task(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    taken = take(speaking, run_id, task.content_id, data=spoken_bytes(23))
    handed = recording_service.pending(speaking.paths, run=run_id)
    assert handed.pending[0].judgeable

    artifact_service.purge(speaking.paths, artifact=str(taken.artifact_id))
    code = refused(judge, speaking, run_id, task.content_id, taken.artifact_id)

    assert code == "assessment_audio_purged"
    assert rows(speaking, "SELECT count(*) FROM assessment_results") == [(0,)]
    assert rows(speaking, "SELECT status, withdrawn_code FROM assessment_submissions") == [
        ("withdrawn", "assessment_audio_purged")
    ]
    assert rows(
        speaking,
        "SELECT status FROM assessment_run_tasks WHERE run_id = ? AND content_id = ?",
        [run_id, task.content_id],
    ) == [("skipped",)]
    # The dimension is free again: the run serves its next task.
    assert serve(speaking, run_id).content_id != task.content_id
    assert not failed_checks(speaking)


def test_a_recording_altered_while_the_judge_listened_withdraws_the_submission(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    taken = take(speaking, run_id, task.content_id, data=spoken_bytes(24))
    path = Path(str(recording_service.pending(speaking.paths, run=run_id).pending[0].audio_path))
    path.write_bytes(path.read_bytes() + b"altered")

    code = refused(judge, speaking, run_id, task.content_id, taken.artifact_id)

    assert code == "assessment_audio_altered"
    assert rows(speaking, "SELECT status, withdrawn_code FROM assessment_submissions") == [
        ("withdrawn", "assessment_audio_altered")
    ]


def test_an_older_run_replayed_after_a_purge_does_not_displace_a_newer_calibration(
    speaking: PolishWorkspace,
) -> None:
    older, _, older_artifact = judged_run(speaking, seed=25, score=0.1)
    newer, _, _ = judged_run(speaking, seed=26, score=1.0)
    before = estimate(speaking)
    assert before.source_run_id == newer

    artifact_service.purge(speaking.paths, artifact=older_artifact)

    after = estimate(speaking)
    assert after.source_run_id == newer
    assert after.level_code == before.level_code and after.score == before.score
    assert older != newer


# --- the evidence check, at the verdict ----------------------------------------------------


def pending_task(workspace: PolishWorkspace, seed: int) -> tuple[str, str, str]:
    run_id = start_spoken(workspace, ("pronunciation",))
    task = serve(workspace, run_id)
    taken = take(workspace, run_id, task.content_id, data=spoken_bytes(seed))
    assert taken.artifact_id is not None
    return run_id, task.content_id, taken.artifact_id


def bound_path(workspace: PolishWorkspace, artifact_id: str) -> Path:
    relative = rows(
        workspace, "SELECT relative_path FROM artifacts WHERE artifact_id = ?", [artifact_id]
    )[0][0]
    return workspace.root / str(relative)


def other_artifact(
    workspace: PolishWorkspace, name: str, *, kind: str = "audio", **options: Any
) -> str:
    path = workspace.root / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(spoken_bytes(abs(hash(name)) % 97 + 300))
    return artifact_service.register(
        workspace.paths,
        relative_path=f"artifacts/{name}",
        kind=kind,
        clock=workspace.clock,
        **options,
    ).artifact_id


def test_a_verdict_on_a_recording_that_is_gone_is_refused_and_withdrawn(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, artifact_id = pending_task(speaking, 30)
    bound_path(speaking, artifact_id).unlink()

    assert refused(judge, speaking, run_id, content_id, artifact_id) == "assessment_audio_missing"
    assert rows(speaking, "SELECT withdrawn_code FROM assessment_submissions") == [
        ("assessment_audio_missing",)
    ]


def test_a_verdict_through_a_symlink_out_of_the_workspace_is_refused(
    speaking: PolishWorkspace, tmp_path: Path
) -> None:
    run_id, content_id, artifact_id = pending_task(speaking, 31)
    path = bound_path(speaking, artifact_id)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)

    assert refused(judge, speaking, run_id, content_id, artifact_id) == "assessment_audio_escaped"


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (
            lambda workspace: other_artifact(workspace, "not-kept.wav", retained=False),
            "assessment_audio_not_retained",
        ),
        (
            lambda workspace: other_artifact(workspace, "notes.txt", kind="transcript"),
            "assessment_audio_not_audio",
        ),
        (
            lambda workspace: other_artifact(workspace, "another-answer.wav"),
            "assessment_audio_not_submitted",
        ),
    ],
)
def test_a_verdict_naming_a_recording_the_learner_did_not_submit_is_refused(
    speaking: PolishWorkspace, make: Any, code: str
) -> None:
    run_id, content_id, artifact_id = pending_task(speaking, 32)
    named = make(speaking)

    assert refused(judge, speaking, run_id, content_id, named) == code
    # A wrong name is the caller's mistake, not the recording's: the submission stands.
    assert rows(speaking, "SELECT status FROM assessment_submissions") == [("pending",)]
    judge(speaking, run_id, content_id, artifact_id)


def test_a_verdict_on_another_learners_recording_is_refused(speaking: PolishWorkspace) -> None:
    run_id, content_id, _ = pending_task(speaking, 33)
    other = learner_service.create_user(
        speaking.paths,
        display_name="Druga Osoba",
        timezone="Europe/Warsaw",
        native_languages=["en"],
        support_languages=["en"],
        clock=speaking.clock,
    )
    learner_service.create_track(
        speaking.paths,
        target_language="pl",
        framework="cefr",
        user=other.user_id,
        clock=speaking.clock,
    )
    with open_reader(speaking.paths) as database:
        tracks = [str(row[0]) for row in database.query("SELECT track_id FROM learning_tracks")]
        run_track = str(
            database.scalar("SELECT track_id FROM assessment_runs WHERE run_id = ?", [run_id])
        )
    foreign = other_artifact(
        speaking, "theirs.wav", track=next(track for track in tracks if track != run_track)
    )

    assert refused(judge, speaking, run_id, content_id, foreign) == (
        "assessment_audio_out_of_scope"
    )


# --- the retention sweep -----------------------------------------------------------------


def test_delete_after_ingestion_never_sweeps_a_learners_own_capture(
    polish_workspace: PolishWorkspace,
) -> None:
    permit_recording(polish_workspace, audio_retention_policy="delete-after-ingestion")
    _, _, judged = judged_run(polish_workspace, seed=40, finalize=False)
    _, _, waiting = pending_task(polish_workspace, 41)

    swept = artifact_service.sweep(polish_workspace.paths, clock=polish_workspace.clock)

    assert swept.purged == ()
    assert {judged, waiting} <= {
        str(artifact_id)
        for (artifact_id,) in rows(
            polish_workspace, "SELECT artifact_id FROM artifacts WHERE purged_at IS NULL"
        )
    }


def test_a_rolling_window_holds_an_unheard_capture_and_purges_a_judged_one(
    polish_workspace: PolishWorkspace,
) -> None:
    permit_recording(
        polish_workspace, audio_retention_policy="rolling-days", audio_retention_days=1
    )
    _, _, judged = judged_run(polish_workspace, seed=42)
    _, _, waiting = pending_task(polish_workspace, 43)
    polish_workspace.clock.advance(timedelta(days=3))

    dry = artifact_service.sweep(polish_workspace.paths, dry_run=True, clock=polish_workspace.clock)
    assert dry.purged == (judged,) and dry.held_for_judging == (waiting,)
    assert len(dry.invalidated_results) == 1
    assert any(waiting in warning for warning in dry.warnings)
    assert any(judged in warning for warning in dry.warnings)

    swept = artifact_service.sweep(polish_workspace.paths, clock=polish_workspace.clock)

    assert swept.purged == (judged,) and swept.judged_recordings == (judged,)
    assert swept.invalidated_results == dry.invalidated_results
    assert rows(
        polish_workspace,
        "SELECT count(*) FROM assessment_results WHERE invalidated_at IS NOT NULL",
    ) == [(1,)]
    assert not failed_checks(polish_workspace)


def test_the_hold_lapses_when_the_run_closes_unjudged(polish_workspace: PolishWorkspace) -> None:
    permit_recording(
        polish_workspace, audio_retention_policy="rolling-days", audio_retention_days=1
    )
    run_id, content_id, waiting = pending_task(polish_workspace, 44)
    assessment_service.set_status(
        polish_workspace.paths, status="abandoned", run=run_id, clock=polish_workspace.clock
    )
    polish_workspace.clock.advance(timedelta(days=3))

    swept = artifact_service.sweep(polish_workspace.paths, clock=polish_workspace.clock)

    assert swept.purged == (waiting,) and len(swept.withdrawn_submissions) == 1
    # Withdrawn by the purge that removed the recording, in its transaction.
    assert rows(polish_workspace, "SELECT status, withdrawn_code FROM assessment_submissions") == [
        ("withdrawn", "assessment_audio_purged")
    ]
    assert content_id
    assert not failed_checks(polish_workspace)


# --- failure paths, beside the success path -----------------------------------------------


class Crash(BaseException):
    """A process dying mid-step. A `BaseException`, so nothing treats it as a refusal."""


def crash_at(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def die(*_args: Any, **_kwargs: Any) -> Any:
        raise Crash(name)

    monkeypatch.setattr(recording_service, name, die)


def staging_states(workspace: PolishWorkspace) -> list[tuple[str, str | None]]:
    return [
        (str(state), None if code is None else str(code))
        for state, code in rows(
            workspace, "SELECT state, refusal_code FROM capture_stagings ORDER BY created_at"
        )
    ]


def test_a_registration_refused_after_the_bytes_were_written_removes_them(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise LinguaWikiError("artifact_unreadable", "the disk refused the read")

    monkeypatch.setattr(artifact_service, "plan_registration", refuse)
    code = refused(take, speaking, run_id, task.content_id, data=spoken_bytes(50))

    assert code == "artifact_unreadable"
    assert staging_states(speaking) == [("refused", "artifact_unreadable")]
    assert private_files(speaking.root) == set()
    assert_everything_accounted_for(speaking)


def test_a_restart_with_staged_bytes_present_promotes_them(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_promote")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(51))
    assert staging_states(speaking) == [("staged", None)]
    assert any(path.startswith("staging/captures/") for path in private_files(speaking.root))
    assert database_check(speaking).ok  # unresolved is a warning: recovery will resolve it

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert len(recovered.registered) == 1 and not recovered.findings
    assert staging_states(speaking) == [("registered", None)]
    assert recording_service.pending(speaking.paths, run=run_id).pending[0].judgeable
    assert_everything_accounted_for(speaking)


def test_a_restart_with_staged_bytes_for_a_closed_run_refuses_and_removes_them(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_promote")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(52))
    assessment_service.set_status(
        speaking.paths, status="abandoned", run=run_id, clock=speaking.clock
    )

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert len(recovered.refused) == 1
    assert staging_states(speaking) == [("refused", "assessment_run_closed")]
    assert private_files(speaking.root) == set()


def test_a_crash_after_the_move_and_before_registration_resumes_at_registration(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    identifier = capture_id()
    with monkeypatch.context() as patched:
        crash_at(patched, "_register_and_bind")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(53), identifier=identifier)
    assert staging_states(speaking) == [("promoting", None)]
    assert all(path.startswith("artifacts/captures/") for path in private_files(speaking.root))
    assert_everything_accounted_for(speaking)

    # The page resends its upload: the retry finishes what the crash interrupted.
    again = take(speaking, run_id, task.content_id, data=spoken_bytes(53), identifier=identifier)

    assert again.state == "registered" and again.submission is not None
    assert rows(speaking, "SELECT count(*) FROM artifacts") == [(1,)]
    assert_everything_accounted_for(speaking)


def test_a_lost_response_after_registration_returns_the_bound_artifact(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    identifier = capture_id()
    first = take(speaking, run_id, task.content_id, data=spoken_bytes(54), identifier=identifier)

    again = take(speaking, run_id, task.content_id, data=spoken_bytes(54), identifier=identifier)

    assert again.replayed and again.artifact_id == first.artifact_id
    assert again.submission == first.submission
    assert rows(speaking, "SELECT count(*) FROM artifacts") == [(1,)]
    assert rows(speaking, "SELECT count(*) FROM assessment_submissions") == [(1,)]


def test_promoting_with_the_bytes_at_neither_path_is_refused_by_name(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_register_and_bind")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(55))
    for path in private_files(speaking.root):
        (speaking.root / path).unlink()
    assert "capture_files_accounted" in failed_checks(speaking)

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert len(recovered.refused) == 1
    assert "neither" in recovered.findings[0]
    assert staging_states(speaking) == [("refused", "capture_file_missing")]
    assert not failed_checks(speaking)


def test_a_stray_file_under_the_staging_root_is_reported_and_never_deleted(
    speaking: PolishWorkspace,
) -> None:
    stray = speaking.root / "staging" / "captures" / "nobody.wav"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(spoken_bytes(56))

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert stray.exists()
    assert any("nobody.wav" in finding for finding in recovered.findings)
    assert "capture_files_accounted" in failed_checks(speaking)


def test_a_bytes_file_whose_hash_changed_is_reported_rather_than_deleted(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_promote")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(57))
    (staged,) = [
        speaking.root / path for path in private_files(speaking.root) if path.startswith("staging/")
    ]
    staged.write_bytes(b"not the captured bytes")

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert staging_states(speaking) == [("refused", "capture_hash_mismatch")]
    assert staged.exists()
    assert any("cannot be identified" in finding for finding in recovered.findings)


def test_serving_waits_for_a_held_writer_and_then_refuses_to_start(
    speaking: PolishWorkspace,
) -> None:
    from linguawiki.client import server as server_module

    with (
        open_writer(speaking.paths, command="test.hold"),
        pytest.raises(LinguaWikiError) as failure,
    ):
        server_module.build_server(speaking.paths, clock=speaking.clock, recovery_wait=0.3)

    assert failure.value.payload.code == "writer_locked"
    assert "recover" in failure.value.payload.message


# --- end to end, through the page's surface and the judge's ------------------------------


def _cli(workspace: PolishWorkspace, capsys: pytest.CaptureFixture[str], *arguments: str) -> Any:
    import json

    from linguawiki.cli import run as run_cli

    capsys.readouterr()
    code = run_cli(
        [*arguments, "--workspace", str(workspace.root), "--format", "json"],
        clock=workspace.clock,
    )
    captured = capsys.readouterr()
    # A refusal's envelope is written to stderr, a success's to stdout.
    payload = json.loads(captured.out or captured.err)
    return code, payload


def test_end_to_end_the_page_captures_and_the_judge_records_from_the_recording(
    speaking: PolishWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import json

    from tests.support.client_http import key, serving

    with serving(speaking.paths, speaking.clock) as client:
        discovered = client.get("/runs").data
        assert discovered["recording"]["offered"] is True
        started = client.post(
            "/runs",
            {
                "dimensions": ["pronunciation"],
                "modalities": ["text", "audio", "speech"],
                "scoring": "machine+recorded",
                "idempotency_key": key(),
            },
        ).data
        run_id = started["run_id"]
        assert started["scoring"] == "machine+recorded"
        task = client.post(f"/runs/{run_id}/tasks", {"idempotency_key": key()}).data
        assert task["modality"] == "speech"
        screen = client.get(f"/runs/{run_id}/screen").data
        outstanding = screen["outstanding"][0]
        assert outstanding["answer_with"] == "recording"
        assert outstanding["state"] == "awaiting-answer"
        assert screen["recording"]["retention_policy"] == "keep"

        identifier = capture_id()
        path = f"/runs/{run_id}/tasks/{task['content_id']}/captures/{identifier}"
        uploaded = client.upload(path, spoken_bytes(60), content_type="audio/webm;codecs=opus")
        assert uploaded.status == 200, uploaded.body[:300]
        artifact_id = uploaded.data["artifact_id"]
        assert uploaded.data["submission"]["capture_id"] == identifier
        # A lost response, resent: the same answer, and nothing registered twice.
        assert (
            client.upload(path, spoken_bytes(60), content_type="audio/webm").data["artifact_id"]
            == artifact_id
        )

        waiting = client.get(f"/runs/{run_id}/screen").data["outstanding"][0]
        assert waiting["state"] == "awaiting-judge"
        assert waiting["submission"]["artifact_id"] == artifact_id

    code, listed = _cli(speaking, capsys, "assessment", "pending", "--run", run_id)
    assert code == 0
    (entry,) = listed["data"]["pending"]
    assert entry["judgeable"] and entry["submission"]["artifact_id"] == artifact_id
    assert entry["task"]["dimension"] == "pronunciation" and entry["task"]["prompt"]
    assert Path(entry["audio_path"]).read_bytes() == spoken_bytes(60)

    rubric = tmp_path / "rubric.json"
    rubric.write_text(json.dumps({"segmentals": 0.5, "prosody": 0.75}), encoding="utf-8")
    code, recorded = _cli(
        speaking,
        capsys,
        "assessment",
        "record",
        "--run",
        run_id,
        "--content",
        task["content_id"],
        "--score",
        "0.6",
        "--audio-artifact",
        artifact_id,
        "--rubric",
        str(rubric),
        "--assessor-kind",
        "ai",
        "--assessor",
        "linguawiki-assess",
        "--confidence",
        "medium",
    )
    assert code == 0, recorded
    pronunciation = next(
        entry for entry in recorded["data"]["dimensions"] if entry["dimension"] == "pronunciation"
    )
    assert pronunciation["tasks_used"] == 1
    assert rows(speaking, "SELECT rubric_json, audio_artifact_id FROM assessment_results") == [
        ('{"prosody": 0.75, "segmentals": 0.5}', artifact_id)
    ]
    assert database_check(speaking).ok


def test_a_connection_dropped_mid_upload_writes_nothing(speaking: PolishWorkspace) -> None:
    from tests.support.client_http import serving

    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with serving(speaking.paths, speaking.clock) as client:
        answer = client.upload(
            f"/runs/{run_id}/tasks/{task.content_id}/captures/{capture_id()}",
            spoken_bytes(61)[:100],
            declared_length=len(spoken_bytes(61)),
        )

    assert answer.code == "invalid_contract"
    assert rows(speaking, "SELECT count(*) FROM capture_stagings") == [(0,)]
    assert private_files(speaking.root) == set()


def test_an_upload_that_is_not_audio_is_refused_before_it_is_read(
    speaking: PolishWorkspace,
) -> None:
    from tests.support.client_http import serving

    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with serving(speaking.paths, speaking.clock) as client:
        path = f"/runs/{run_id}/tasks/{task.content_id}/captures/{capture_id()}"
        as_json = client.upload(path, b"{}", content_type="application/json")
        unknown = client.upload(path, b"RIFF", content_type="audio/x-unheard-of")
        no_origin = client.upload(path, spoken_bytes(62), origin=None)

    assert as_json.code == "invalid_contract"
    assert unknown.code == "capture_media_type_unsupported"
    assert no_origin.status == 403
    assert private_files(speaking.root) == set()


def test_rubric_and_input_are_one_payload_and_not_two(
    speaking: PolishWorkspace, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run_id, content_id, artifact_id = pending_task(speaking, 63)
    rubric = tmp_path / "rubric.json"
    rubric.write_text("{}", encoding="utf-8")

    code, refusal = _cli(
        speaking,
        capsys,
        "assessment",
        "record",
        "--run",
        run_id,
        "--content",
        content_id,
        "--score",
        "0.5",
        "--audio-artifact",
        artifact_id,
        "--rubric",
        str(rubric),
        "--input",
        str(rubric),
        "--assessor-kind",
        "ai",
        "--assessor",
        "judge",
    )

    assert code != 0 and refusal["error"]["code"] == "invalid_arguments"


# --- review findings ---------------------------------------------------------------------


def test_recovery_never_moves_a_capture_over_a_file_it_cannot_identify(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before the move, then something appears at the destination: no overwrite."""

    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_move")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(70))
    assert staging_states(speaking) == [("promoting", None)]
    final = speaking.root / str(rows(speaking, "SELECT final_path FROM capture_stagings")[0][0])
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"somebody else's file")

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert len(recovered.refused) == 1
    assert staging_states(speaking) == [("refused", "capture_path_occupied")]
    assert final.read_bytes() == b"somebody else's file"
    assert not any(path.startswith("staging/") for path in private_files(speaking.root))


def test_a_recovery_whose_cleanup_fails_refuses_to_report_success(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linguawiki.client import server as server_module

    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_promote")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(71))
    assessment_service.set_status(
        speaking.paths, status="abandoned", run=run_id, clock=speaking.clock
    )

    def cannot_delete(*_args: Any, **_kwargs: Any) -> Any:
        raise LinguaWikiError("capture_file_not_removed", "the disk refused the deletion")

    monkeypatch.setattr(recording_service, "_remove_identified", cannot_delete)

    assert refused(recording_service.recover, speaking.paths, clock=speaking.clock) == (
        "capture_unresolved"
    )
    # Still unresolved -- whichever step the refusal was reached at -- and still owning its
    # bytes, so nothing was reported as resolved that is not.
    assert staging_states(speaking)[0][0] in ("staged", "promoting")
    assert_everything_accounted_for(speaking)
    assert (
        refused(server_module.build_server, speaking.paths, clock=speaking.clock)
        == "capture_unresolved"
    )


def test_a_recorded_scoring_run_takes_no_spoken_verdict_without_a_recording(
    speaking: PolishWorkspace,
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)

    assert refused(judge, speaking, run_id, task.content_id, None) == (
        "assessment_recording_required"
    )
    assert rows(speaking, "SELECT count(*) FROM assessment_results") == [(0,)]


def test_a_verdict_repeat_naming_another_recording_is_a_conflict(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, artifact_id = pending_task(speaking, 72)
    judge(speaking, run_id, content_id, artifact_id)

    judge(speaking, run_id, content_id, artifact_id)  # an exact repeat is a repeat
    assert refused(judge, speaking, run_id, content_id, "art_nonexistent") == (
        "assessment_result_conflict"
    )
    assert refused(judge, speaking, run_id, content_id, None) == "assessment_result_conflict"


# --- second review round -----------------------------------------------------------------


def test_a_move_the_disk_refuses_names_the_capture_and_deletes_nothing(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only or cross-volume artifacts directory: recovery names the capture, keeps
    its bytes staged, and succeeds once the disk lets it."""

    import errno

    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_move")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(80))
    identifier = str(rows(speaking, "SELECT capture_id FROM capture_stagings")[0][0])

    def cross_device(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    with monkeypatch.context() as patched:
        patched.setattr(recording_service.os, "replace", cross_device)
        with pytest.raises(LinguaWikiError) as failure:
            recording_service.recover(speaking.paths, clock=speaking.clock)
    assert failure.value.payload.code == "capture_unresolved"
    assert identifier in failure.value.payload.message
    assert staging_states(speaking) == [("promoting", None)]
    assert any(path.startswith("staging/") for path in private_files(speaking.root))

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert recovered.registered == (identifier,)
    assert_everything_accounted_for(speaking)


def test_a_capture_staged_before_a_pause_is_recovered_rather_than_deleted(
    speaking: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with monkeypatch.context() as patched:
        crash_at(patched, "_promote")
        with pytest.raises(Crash):
            take(speaking, run_id, task.content_id, data=spoken_bytes(81))
    assessment_service.set_status(speaking.paths, status="paused", run=run_id, clock=speaking.clock)

    recovered = recording_service.recover(speaking.paths, clock=speaking.clock)

    assert len(recovered.registered) == 1
    assert rows(speaking, "SELECT status FROM assessment_submissions") == [("pending",)]
    assessment_service.set_status(
        speaking.paths, status="in-progress", run=run_id, clock=speaking.clock
    )
    assert recording_service.pending(speaking.paths, run=run_id).pending[0].judgeable


def test_a_new_capture_on_a_paused_run_is_still_refused(speaking: PolishWorkspace) -> None:
    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    assessment_service.set_status(speaking.paths, status="paused", run=run_id, clock=speaking.clock)

    assert refused(take, speaking, run_id, task.content_id, data=spoken_bytes(82)) == (
        "assessment_run_paused"
    )
    assert private_files(speaking.root) == set()


def test_a_sweep_whose_purge_fails_leaves_the_submission_pending(
    polish_workspace: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit_recording(
        polish_workspace, audio_retention_policy="rolling-days", audio_retention_days=1
    )
    run_id, _, _ = pending_task(polish_workspace, 83)
    assessment_service.set_status(
        polish_workspace.paths, status="abandoned", run=run_id, clock=polish_workspace.clock
    )
    polish_workspace.clock.advance(timedelta(days=3))

    def cannot_delete(*_args: Any, **_kwargs: Any) -> None:
        raise LinguaWikiError("artifact_file_not_removed", "the disk refused the deletion")

    monkeypatch.setattr(artifact_service, "_remove_file", cannot_delete)
    with pytest.raises(LinguaWikiError):
        artifact_service.sweep(polish_workspace.paths, clock=polish_workspace.clock)

    assert rows(polish_workspace, "SELECT status FROM assessment_submissions") == [("pending",)]


def test_a_judged_capture_in_a_live_run_is_held_until_the_run_closes(
    polish_workspace: PolishWorkspace,
) -> None:
    permit_recording(
        polish_workspace, audio_retention_policy="rolling-days", audio_retention_days=1
    )
    run_id, _, judged = judged_run(polish_workspace, seed=84, finalize=False)
    polish_workspace.clock.advance(timedelta(days=3))

    held = artifact_service.sweep(polish_workspace.paths, clock=polish_workspace.clock)
    assert held.purged == () and held.held_for_judging == (judged,)
    assert rows(
        polish_workspace, "SELECT count(*) FROM assessment_results WHERE invalidated_at IS NULL"
    ) == [(1,)]

    assessment_service.finalize(polish_workspace.paths, run=run_id, clock=polish_workspace.clock)
    swept = artifact_service.sweep(polish_workspace.paths, clock=polish_workspace.clock)
    assert swept.purged == (judged,)


def test_a_blocked_upload_waits_once_and_not_once_per_layer(speaking: PolishWorkspace) -> None:
    import time

    from tests.support.client_http import serving

    run_id = start_spoken(speaking, ("pronunciation",))
    task = serve(speaking, run_id)
    with serving(speaking.paths, speaking.clock) as client:
        started = time.monotonic()
        with open_writer(speaking.paths, command="test.hold"):
            answer = client.upload(
                f"/runs/{run_id}/tasks/{task.content_id}/captures/{capture_id()}",
                spoken_bytes(85),
            )
        elapsed = time.monotonic() - started

    assert answer.status == 503 and answer.code == "writer_locked"
    assert elapsed < recording_service.TRANSIENT_WAIT_SECONDS * 2, elapsed
    assert private_files(speaking.root) == set()


def test_a_pack_that_cannot_be_found_reports_itself_at_playback(
    polish_workspace: PolishWorkspace, tmp_path: Path
) -> None:
    import shutil

    from tests.support.recordings import publish_pilot_with_recordings

    root = publish_pilot_with_recordings(polish_workspace, tmp_path)
    run = assessment_service.start(
        polish_workspace.paths,
        dimensions=["listening"],
        modalities=["audio"],
        scoring="machine",
        clock=polish_workspace.clock,
    )
    task = serve(polish_workspace, run.run_id)
    shutil.move(str(root), str(tmp_path / "moved-away"))

    code = refused(
        assessment_service.served_recording,
        polish_workspace.paths,
        run=run.run_id,
        content_id=task.content_id,
    )
    assert code == "pack_not_found"


def test_db_check_and_the_service_agree_about_a_verdicts_policy_version(
    speaking: PolishWorkspace,
) -> None:
    run_id, content_id, artifact_id = pending_task(speaking, 86)
    judge(speaking, run_id, content_id, artifact_id)
    assert "judged_claims_within_policy" not in failed_checks(speaking)

    with (
        open_writer(speaking.paths, command="test.damage") as database,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE assessment_results SET judgement_policy_version = NULL")

    check = failed_checks(speaking)["judged_claims_within_policy"]
    assert "judgement.v1" in "".join(check.context.values())
