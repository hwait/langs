"""The session page in a real browser: plan, stage, close, recover, and every interruption.

HTTP tests cannot show that a control is reachable, that staged work is visible, or that a
reload recovers -- so each of these drives `/sessions.html` against a real server. A lost
response is made with Playwright's `route`: `fetch` lets the request reach the server and
`abort` then drops the answer, which is exactly the case a key exists for. A request that
never reaches the server is `abort` alone.

Part of the release gate, and it **fails** when the browser cannot be launched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright import sync_api as playwright_api

from linguawiki.client import server as server_module
from linguawiki.db.connection import open_reader
from linguawiki.retrying import with_retry
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import sessions as session_service
from tests.browser.test_client_assessment_browser import HOLD
from tests.conftest import PolishWorkspace

EVENT_IDS = [f"evt_01ARZ3NDEKTSV4RRFFQ69G5F{n:02d}" for n in range(40)]
#: Longer than any excerpt a track keeps, so its absence from storage is the whole of it.
LEARNER_WORDS = (
    "Wczoraj poszłam do biura bardzo wcześnie, bo musiałam przygotować prezentację dla "
    "nowego klienta, a potem rozmawiałam z kolegą o projekcie, który kończymy w przyszłym "
    "tygodniu, i jeszcze napisałam długi raport o wszystkim, co się wydarzyło w zespole."
) * 2


def _rows(workspace: PolishWorkspace, sql: str, parameters: list[object] | None = None) -> Any:
    def read() -> Any:
        with open_reader(workspace.paths) as database:
            return database.query(sql, parameters or [])

    return with_retry(read, attempts=40, base_delay=0.05)


def _count(workspace: PolishWorkspace, sql: str, parameters: list[object] | None = None) -> int:
    return int(_rows(workspace, sql, parameters)[0][0])


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch()
        except playwright_api.Error as failure:
            pytest.fail(
                "Chromium could not be launched, and this acceptance test does not skip: "
                f"run `uv run playwright install chromium`. ({failure})"
            )
        yield launched
        launched.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    context = browser.new_context(viewport={"width": 480, "height": 900})
    opened = context.new_page()
    yield opened
    context.close()


@pytest.fixture
def onboarded(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    return polish_workspace


class Served:
    def __init__(self, workspace: PolishWorkspace) -> None:
        self.server = server_module.build_server(workspace.paths, clock=workspace.clock)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.close()
        self.thread.join(timeout=5)


@pytest.fixture
def served(onboarded: PolishWorkspace) -> Iterator[Served]:
    running = Served(onboarded)
    yield running
    running.stop()


def launch(page: Any, served: Served) -> None:
    page.goto(served.server.sessions_url)
    settle(page)


def settle(page: Any) -> None:
    page.wait_for_selector("#app[data-busy=false]", timeout=20000)


def core_blocks(
    workspace: PolishWorkspace, session: str
) -> list[session_service.SessionBlockReport]:
    report = session_service.show(workspace.paths, session=session, clock=workspace.clock)
    return [block for block in report.blocks if block.role == "core"]


def attempt(
    block: session_service.SessionBlockReport, event: int, *, response: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_type": "short-response",
        "modality": block.modality,
        "dimension": block.dimension,
        "score": 1.0,
        "claims": ["controlled-production"],
        "assessor_kind": "ai",
    }
    if block.targets:
        payload["target"] = block.targets[0].content_id
    if response is not None:
        payload["response"] = response
    return {
        "event_id": EVENT_IDS[event],
        "kind": "attempt.observed",
        "occurred_at": f"2026-01-01T09:{event:02d}:00Z",
        "payload": payload,
    }


def batch(
    block: session_service.SessionBlockReport,
    events: list[int],
    *,
    sequence: int = 1,
    key: str = "batch-1",
    response: str | None = None,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "idempotency_key": key,
        "block": block.block_id,
        "events": [attempt(block, event, response=response) for event in events],
    }


def write(tmp_path: Path, name: str, body: dict[str, Any]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def running(workspace: PolishWorkspace, key: str, track: str | None = None) -> str:
    report = session_service.create(
        workspace.paths,
        minutes=60,
        mode="mixed",
        track=track or workspace.track_id,
        idempotency_key=key,
        clock=workspace.clock,
    )
    session_service.start(workspace.paths, session=report.session_id, clock=workspace.clock)
    return report.session_id


def abandoned(
    workspace: PolishWorkspace, key: str, events: list[int], track: str | None = None
) -> str:
    session = running(workspace, key, track)
    block = core_blocks(workspace, session)[0]
    session_service.log(
        workspace.paths,
        batch=batch(block, events, key=f"{key}-batch"),
        session=session,
        clock=workspace.clock,
    )
    session_service.abandon(workspace.paths, session=session, clock=workspace.clock)
    return session


def lose_responses(page: Any, pattern: str) -> None:
    """Let each request reach the server, then drop its answer."""

    def handler(route: Any) -> None:
        route.fetch()
        route.abort()

    page.route(pattern, handler)


def never_send(page: Any, pattern: str) -> None:
    page.route(pattern, lambda route: route.abort())


def everything_stored(page: Any) -> str:
    return str(
        page.evaluate(
            """async () => {
                const dump = {session: {}, local: {}, idb: []};
                for (let i = 0; i < sessionStorage.length; i++) {
                  const k = sessionStorage.key(i); dump.session[k] = sessionStorage.getItem(k);
                }
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i); dump.local[k] = localStorage.getItem(k);
                }
                dump.idb = indexedDB.databases ? await indexedDB.databases() : [];
                return JSON.stringify(dump);
            }"""
        )
    )


# --- Plan, stage, close --------------------------------------------------------------


def test_a_session_planned_staged_and_closed_in_the_page_credits_once(
    page: Any, served: Served, onboarded: PolishWorkspace, tmp_path: Path
) -> None:
    launch(page, served)
    page.fill("[data-role=minutes]", "60")
    page.click("[data-role=plan]")
    page.wait_for_selector("[data-role=session][data-status=planned]")
    assert page.locator("[data-role=rationale] li").count() > 0
    session = page.get_attribute("[data-role=session]", "data-session")
    shown = session_service.show(onboarded.paths, session=session, clock=onboarded.clock)
    if shown.omissions:
        assert page.locator("[data-role=omissions] li").count() == len(shown.omissions)
    page.click("[data-role=start]")
    page.wait_for_selector("[data-role=session][data-status=active]")
    block = core_blocks(onboarded, session)[0]
    page.set_input_files("[data-role=import]", write(tmp_path, "b.json", batch(block, [0, 1])))
    page.wait_for_selector("[data-role=staged][data-count='2']")
    assert page.locator("[data-role=staged-event][data-status=staged]").count() == 2
    assert "not yet credited" in page.inner_text("[data-role=staged]")
    assert "assessed by: ai (as the producer says)" in page.inner_text("[data-role=staged]")
    assert _count(onboarded, "SELECT count(*) FROM attempts") == 0

    page.click("[data-role=close]")
    page.wait_for_selector("[data-role=close-count][data-count='2']")
    page.click("[data-role=confirm-close]")
    page.wait_for_selector("[data-role=close-report]")

    assert "2 attempt(s)" in page.inner_text("[data-role=close-report]")
    assert _count(onboarded, "SELECT count(*) FROM attempts") == 2
    assert _count(onboarded, "SELECT count(*) FROM session_finalizations") == 1


def test_uncredited_work_flushed_elsewhere_is_visible_before_the_close(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    session = running(onboarded, "plan-1")
    launch(page, served)
    page.wait_for_selector("[data-role=session][data-status=active]")
    block = core_blocks(onboarded, session)[0]
    session_service.log(
        onboarded.paths, batch=batch(block, [0]), session=session, clock=onboarded.clock
    )

    page.reload()
    settle(page)

    page.wait_for_selector("[data-role=staged-event][data-status=staged]")
    assert "not yet credited" in page.inner_text("[data-role=staged-event]")
    assert page.locator("[data-role=close-report]").count() == 0
    assert page.locator("[data-role=stage-changes]").count() == 0


def test_a_close_whose_answer_was_lost_is_replayed_after_a_reload(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    session = running(onboarded, "plan-1")
    block = core_blocks(onboarded, session)[0]
    session_service.log(
        onboarded.paths, batch=batch(block, [0]), session=session, clock=onboarded.clock
    )
    launch(page, served)
    page.click("[data-role=close]")
    lose_responses(page, "**/sessions/*/close")
    page.click("[data-role=confirm-close]")
    page.wait_for_selector("#status[data-state=offline]", timeout=20000)

    page.unroute("**/sessions/*/close")
    page.reload()
    settle(page)

    page.wait_for_selector("[data-role=close-report][data-replayed=true]")
    assert _count(onboarded, "SELECT count(*) FROM session_finalizations") == 1
    assert _count(onboarded, "SELECT count(*) FROM attempts") == 1


def test_a_session_left_closing_offers_to_finish_or_abandon_and_credits_once(
    page: Any, served: Served, onboarded: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = running(onboarded, "plan-1")
    block = core_blocks(onboarded, session)[0]
    session_service.log(
        onboarded.paths, batch=batch(block, [0]), session=session, clock=onboarded.clock
    )
    original = session_service._materialize

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("power cut")

    monkeypatch.setattr(session_service, "_materialize", crash)
    with pytest.raises(RuntimeError):
        session_service.close(onboarded.paths, session=session, clock=onboarded.clock)
    monkeypatch.setattr(session_service, "_materialize", original)

    launch(page, served)

    page.wait_for_selector("[data-role=closing-interrupted]")
    assert page.locator("[data-role=import]").count() == 0
    assert page.inner_text("[data-role=close]") == "Finish closing"
    assert page.locator("[data-role=abandon]").count() == 1
    page.click("[data-role=close]")
    page.click("[data-role=confirm-close]")
    page.wait_for_selector("[data-role=close-report]")
    assert _count(onboarded, "SELECT count(*) FROM attempts") == 1


def test_a_partial_close_excludes_a_block_and_says_what_it_cannot_exclude(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    source = abandoned(onboarded, "plan-src", [10])
    session = running(onboarded, "plan-1")
    blocks = core_blocks(onboarded, session)
    session_service.log(
        onboarded.paths, batch=batch(blocks[0], [0]), session=session, clock=onboarded.clock
    )
    session_service.log(
        onboarded.paths,
        batch=batch(blocks[1], [1], sequence=2, key="batch-2"),
        session=session,
        clock=onboarded.clock,
    )
    session_service.recover(onboarded.paths, source=source, target=session, clock=onboarded.clock)
    launch(page, served)

    page.click("[data-role=partial-close]")
    page.wait_for_selector("[data-role=unattributed][data-count='1']")
    page.check(f"[data-role=discard-block][data-block={blocks[1].block_id}]")
    page.click("[data-role=confirm-close]")
    page.wait_for_selector("[data-role=close-report]")

    (discarded,) = _rows(onboarded, "SELECT staged_discarded FROM session_finalizations")[0]
    assert discarded == 1
    assert _count(onboarded, "SELECT count(*) FROM attempts") == 2


def test_work_arriving_during_the_confirmation_is_shown_before_anything_is_credited(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    session = running(onboarded, "plan-1")
    block = core_blocks(onboarded, session)[0]
    session_service.log(
        onboarded.paths, batch=batch(block, [0]), session=session, clock=onboarded.clock
    )
    launch(page, served)
    page.click("[data-role=close]")
    page.wait_for_selector("[data-role=close-count][data-count='1']")
    session_service.log(
        onboarded.paths,
        batch=batch(block, [1], sequence=2, key="batch-2"),
        session=session,
        clock=onboarded.clock,
    )

    page.click("[data-role=confirm-close]")

    page.wait_for_selector("[data-role=staging-changed]")
    page.wait_for_selector("[data-role=close-count][data-count='2']")
    assert _count(onboarded, "SELECT count(*) FROM attempts") == 0
    page.click("[data-role=confirm-close]")
    page.wait_for_selector("[data-role=close-report]")
    assert _count(onboarded, "SELECT count(*) FROM attempts") == 2


# --- Import --------------------------------------------------------------------------


def test_an_import_on_a_no_retention_track_never_reaches_browser_storage(
    page: Any, served: Served, onboarded: PolishWorkspace, tmp_path: Path
) -> None:
    learner_service.update_track(
        onboarded.paths,
        track=onboarded.track_id,
        preferences=learner_service.TrackPreferences(transcript_retention_consent=False),
        clock=onboarded.clock,
    )
    session = running(onboarded, "plan-1")
    block = core_blocks(onboarded, session)[0]
    file = write(tmp_path, "b.json", batch(block, [0], response=LEARNER_WORDS))
    launch(page, served)
    lose_responses(page, "**/sessions/*/batches")
    page.set_input_files("[data-role=import]", file)
    page.wait_for_selector("#status[data-state=offline]", timeout=20000)

    dump = everything_stored(page)
    assert LEARNER_WORDS[:60] not in dump
    assert "payload" not in dump
    marker = json.loads(page.evaluate("sessionStorage.getItem('linguawiki.import')"))
    assert set(marker) == {
        "session_id",
        "idempotency_key",
        "sequence",
        "event_count",
        "file_sha256",
    }

    page.unroute("**/sessions/*/batches")
    page.reload()
    settle(page)
    page.wait_for_selector("[data-role=import-prompt][data-on-record=true]")
    page.set_input_files("[data-role=import-again]", file)
    page.wait_for_selector("[data-role=notice]")

    assert "confirmed" in page.inner_text("[data-role=notice]")
    assert page.evaluate("sessionStorage.getItem('linguawiki.import')") is None
    (stored,) = _rows(onboarded, "SELECT payload_json FROM session_staged_events")[0]
    assert LEARNER_WORDS not in str(stored)
    assert _count(onboarded, "SELECT count(*) FROM session_staged_events") == 1


def test_an_import_that_never_landed_is_sent_again_from_the_chosen_file(
    page: Any, served: Served, onboarded: PolishWorkspace, tmp_path: Path
) -> None:
    session = running(onboarded, "plan-1")
    block = core_blocks(onboarded, session)[0]
    same = write(tmp_path, "a.json", batch(block, [0]))
    other = write(tmp_path, "b.json", batch(block, [1], sequence=2, key="batch-2"))
    launch(page, served)
    never_send(page, "**/sessions/*/batches")
    page.set_input_files("[data-role=import]", same)
    page.wait_for_selector("#status[data-state=offline]", timeout=20000)
    page.unroute("**/sessions/*/batches")
    page.reload()
    settle(page)

    page.wait_for_selector("[data-role=import-prompt][data-on-record=false]")
    page.set_input_files("[data-role=import-again]", same)
    page.wait_for_selector("[data-role=notice]")
    assert _count(onboarded, "SELECT count(*) FROM session_staged_events") == 1

    never_send(page, "**/sessions/*/batches")
    page.set_input_files("[data-role=import]", same)
    page.wait_for_selector("#status[data-state=offline]", timeout=20000)
    page.unroute("**/sessions/*/batches")
    page.reload()
    settle(page)
    page.set_input_files("[data-role=import-again]", other)
    page.wait_for_selector("[data-role=import-mismatch]")
    page.click("[data-role=import-as-new]")
    page.wait_for_selector("[data-role=staged][data-count='2']")
    assert _count(onboarded, "SELECT count(*) FROM session_staged_events") == 2


def test_a_lost_import_under_a_key_owned_by_other_content_is_reported_refused(
    page: Any, served: Served, onboarded: PolishWorkspace, tmp_path: Path
) -> None:
    session = running(onboarded, "plan-1")
    block = core_blocks(onboarded, session)[0]
    session_service.log(
        onboarded.paths, batch=batch(block, [0], key="K"), session=session, clock=onboarded.clock
    )
    conflicting = write(tmp_path, "b.json", batch(block, [5], sequence=2, key="K"))
    launch(page, served)
    lose_responses(page, "**/sessions/*/batches")
    page.set_input_files("[data-role=import]", conflicting)
    page.wait_for_selector("#status[data-state=offline]", timeout=20000)
    page.unroute("**/sessions/*/batches")
    page.reload()
    settle(page)

    page.wait_for_selector("[data-role=import-prompt][data-on-record=true]")
    assert page.locator("[data-role=notice]").count() == 0
    page.set_input_files("[data-role=import-again]", conflicting)
    page.wait_for_selector(".error[data-code=idempotency_conflict]")

    assert "refused" in page.inner_text(".error")
    assert page.evaluate("sessionStorage.getItem('linguawiki.import')") is None
    staged = [
        str(row[0]) for row in _rows(onboarded, "SELECT source_event_id FROM session_staged_events")
    ]
    assert staged == [EVENT_IDS[0]]


def test_an_import_that_meets_a_busy_writer_waits_and_lands_once(
    page: Any, served: Served, onboarded: PolishWorkspace, tmp_path: Path
) -> None:
    session = running(onboarded, "plan-1")
    block = core_blocks(onboarded, session)[0]
    launch(page, served)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(onboarded.root), "4"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        page.set_input_files("[data-role=import]", write(tmp_path, "a.json", batch(block, [0])))
        page.wait_for_selector("#status[data-state=waiting]", timeout=15000)
    finally:
        holder.wait(timeout=20)
    page.wait_for_selector("[data-role=staged][data-count='1']", timeout=20000)

    calls = {"n": 0}

    def drop_first(route: Any) -> None:
        calls["n"] += 1
        route.fetch()
        if calls["n"] == 1:
            route.abort()
        else:
            route.continue_()

    page.route("**/sessions/*/batches", drop_first)
    page.set_input_files(
        "[data-role=import]", write(tmp_path, "b.json", batch(block, [1], sequence=2, key="b-2"))
    )
    page.wait_for_selector("[data-role=staged][data-count='2']", timeout=20000)

    assert _count(onboarded, "SELECT count(*) FROM session_staged_events") == 2
    assert _count(onboarded, "SELECT count(*) FROM session_event_batches") == 2


# --- Recovery ------------------------------------------------------------------------


def test_a_reviewed_recovery_moves_the_selection_and_the_rest_can_follow(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    source = abandoned(onboarded, "plan-src", [0, 1, 2])
    launch(page, served)
    page.wait_for_selector("[data-role=recoverable-sessions]")
    page.click(f"[data-role=recoverable-sessions] li[data-session={source}] [data-role=review]")
    page.wait_for_selector("[data-role=recovery-review]")
    boxes = page.locator("[data-role=recover-event]")
    assert boxes.count() == 3
    left_behind = boxes.nth(2).get_attribute("data-event")
    boxes.nth(2).uncheck()
    page.check("[data-role=destination][data-session=new]")
    page.click("[data-role=recover]")
    page.wait_for_selector("[data-role=session][data-status=active]")
    target = page.get_attribute("[data-role=session]", "data-session")
    page.wait_for_selector("[data-role=staged][data-count='2']")

    page.click("[data-role=home]")
    page.click(f"[data-role=recoverable-sessions] li[data-session={source}] [data-role=review]")
    page.wait_for_selector("[data-role=recovery-review]")
    assert page.locator("[data-role=recover-event]").count() == 1
    assert page.get_attribute("[data-role=recover-event]", "data-event") == left_behind
    assert page.is_checked("[data-role=recover-event]")
    page.check(f"[data-role=destination][data-session={target}]")
    page.click("[data-role=recover]")
    page.wait_for_selector("[data-role=staged][data-count='3']")
    assert _count(onboarded, "SELECT count(*) FROM sessions") == 2


@pytest.mark.parametrize("boundary", ["before-start", "before-recover"])
def test_a_recovery_interrupted_between_steps_resumes_where_it_stopped(
    page: Any, served: Served, onboarded: PolishWorkspace, boundary: str
) -> None:
    source = abandoned(onboarded, "plan-src", [0, 1])
    launch(page, served)
    page.click(f"[data-role=recoverable-sessions] li[data-session={source}] [data-role=review]")
    page.wait_for_selector("[data-role=recovery-review]")
    page.check("[data-role=destination][data-session=new]")
    held = "**/sessions/*/start" if boundary == "before-start" else "**/sessions/*/recover"
    never_send(page, held)
    page.click("[data-role=recover]")
    page.wait_for_function(
        "(step) => { const r = JSON.parse(sessionStorage.getItem('linguawiki.recovery') || 'null');"
        " return r && r.steps[step]; }",
        arg="planned" if boundary == "before-start" else "started",
        timeout=20000,
    )
    record = json.loads(page.evaluate("sessionStorage.getItem('linguawiki.recovery')"))

    page.unroute(held)
    page.reload()
    settle(page)

    page.wait_for_selector("[data-role=staged][data-count='2']")
    assert (
        page.get_attribute("[data-role=session]", "data-session")
        == record["destination"]["session_id"]
    )
    assert record["source_session_id"] == source
    assert _count(onboarded, "SELECT count(*) FROM sessions") == 2
    assert (
        _count(
            onboarded,
            "SELECT count(*) FROM session_event_batches WHERE session_id = ?",
            [record["destination"]["session_id"]],
        )
        == 1
    )
    assert page.evaluate("sessionStorage.getItem('linguawiki.recovery')") is None


def test_a_recovery_whose_answer_was_lost_reports_what_it_moved(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    source = abandoned(onboarded, "plan-src", [0, 1])
    target = running(onboarded, "plan-target")
    launch(page, served)
    page.click("[data-role=home]")
    page.click(f"[data-role=recoverable-sessions] li[data-session={source}] [data-role=review]")
    page.wait_for_selector("[data-role=recovery-review]")
    page.check(f"[data-role=destination][data-session={target}]")
    lose_responses(page, "**/sessions/*/recover")
    page.click("[data-role=recover]")
    page.wait_for_selector("#status[data-state=offline]", timeout=20000)

    page.unroute("**/sessions/*/recover")
    page.reload()
    settle(page)

    page.wait_for_selector("[data-role=notice]")
    notice = page.inner_text("[data-role=notice]")
    assert "Recovered 2 event(s)" in notice and "already happened" in notice
    assert page.evaluate("sessionStorage.getItem('linguawiki.recovery')") is None


# --- Tracks --------------------------------------------------------------------------


def test_a_fresh_launch_on_two_active_tracks_asks_which_and_keeps_the_answer(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    user = learner_service.create_user(
        onboarded.paths,
        display_name="Druga Osoba",
        timezone="Europe/Warsaw",
        native_languages=["en"],
        clock=onboarded.clock,
    )
    other = learner_service.create_track(
        onboarded.paths,
        user=user.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        clock=onboarded.clock,
    ).track_id
    onboarding_service.start(
        onboarded.paths, track=other, declared_level="A2", clock=onboarded.clock
    )
    onboarding_service.finalize(onboarded.paths, track=other, clock=onboarded.clock)
    source = abandoned(onboarded, "plan-src", [0], track=other)
    discovered: list[str] = []
    page.on("request", lambda request: discovered.append(request.url))

    launch(page, served)

    page.wait_for_selector("[data-role=track-picker]")
    assert not [url for url in discovered if "/sessions?" in url or url.endswith("/sessions")]
    page.click(f"[data-role=track-option][data-track={other}] [data-role=pick-track]")
    page.wait_for_selector(f"[data-role=track][data-track={other}]")
    page.reload()
    settle(page)
    page.wait_for_selector(f"[data-role=track][data-track={other}]")

    page.click(f"[data-role=recoverable-sessions] li[data-session={source}] [data-role=review]")
    page.wait_for_selector("[data-role=recovery-review]")
    page.check("[data-role=destination][data-session=new]")
    never_send(page, "**/sessions/*/start")
    page.click("[data-role=recover]")
    page.wait_for_function(
        "() => { const r = JSON.parse(sessionStorage.getItem('linguawiki.recovery') || 'null');"
        " return r && r.steps.planned; }",
        timeout=20000,
    )
    page.evaluate(f"sessionStorage.setItem('linguawiki.track', '{onboarded.track_id}')")
    page.unroute("**/sessions/*/start")
    page.reload()
    settle(page)

    page.wait_for_selector(f"[data-role=track][data-track={other}]")
    page.wait_for_selector("[data-role=staged][data-count='1']")
    assert (
        _count(onboarded, "SELECT count(*) FROM sessions WHERE track_id = ?", [onboarded.track_id])
        == 0
    )

    learner_service.set_track_status(
        onboarded.paths, track=other, status="paused", clock=onboarded.clock
    )
    page.reload()
    settle(page)
    page.wait_for_selector("[data-role=track-picker]")
    assert other in page.inner_text("[data-role=notice]")


# --- The page does not teach ---------------------------------------------------------


def test_the_session_page_offers_no_exercise_and_says_where_teaching_happens(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    running(onboarded, "plan-1")

    launch(page, served)

    page.wait_for_selector("[data-role=teaching-elsewhere]")
    assert "Teaching happens elsewhere" in page.inner_text("[data-role=teaching-elsewhere]")
    assert page.locator("textarea").count() == 0
    assert page.locator(".choices, section.task, [data-role=submit]").count() == 0


def test_a_recovery_refused_because_events_moved_reviews_what_is_left(
    page: Any, served: Served, onboarded: PolishWorkspace
) -> None:
    source = abandoned(onboarded, "plan-src", [0, 1, 2])
    elsewhere = running(onboarded, "plan-elsewhere")
    launch(page, served)
    page.click("[data-role=home]")
    page.click(f"[data-role=recoverable-sessions] li[data-session={source}] [data-role=review]")
    page.wait_for_selector("[data-role=recovery-review]")
    moved = page.get_attribute("[data-role=recover-event] >> nth=0", "data-event")
    session_service.recover(
        onboarded.paths, source=source, target=elsewhere, events=[moved], clock=onboarded.clock
    )
    page.check(f"[data-role=destination][data-session={elsewhere}]")

    page.click("[data-role=recover]")

    page.wait_for_selector(".error[data-code=recovery_selection_needed]")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-role=recover-event]').length === 2"
    )
    assert page.is_checked(f"[data-role=destination][data-session={elsewhere}]")
    page.click("[data-role=recover]")
    page.wait_for_selector("[data-role=staged][data-count='3']")
    assert _count(onboarded, "SELECT count(*) FROM sessions") == 2
