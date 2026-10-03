"""Waiting for a judge in a real browser: written answers, polling, batches, finishing.

A judge marks a recorded or written answer later, from another process, and the server
cannot tell the page. What is under test is that the page finds out on its own -- by
polling `/screen` -- and that what it finds never disturbs the task the learner is on: typed
words and a live microphone survive a verdict landing in another dimension.

Time in the page is Playwright's installed clock, which runs naturally and is jumped forward
to fire a poll at once, so no test waits out the 2 s to 30 s backoff. The recordings are
Chromium's generated tone; nothing anybody said is recorded.

Part of the release gate, and it **fails** when the browser cannot be launched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from playwright import sync_api as playwright_api

from linguawiki.client import server as server_module
from linguawiki.db.connection import open_reader
from linguawiki.retrying import with_retry
from linguawiki.services import assessment as assessment_service
from linguawiki.services import judging
from tests.browser.test_client_assessment_browser import HOLD
from tests.browser.test_client_audio_browser import TRACK_STREAMS
from tests.conftest import PolishWorkspace
from tests.integration.test_client_audio import permit_recording

#: Further than the backoff's cap, so one jump always fires the next poll.
PAST_THE_CAP_MS = 31_000


def _rows(workspace: PolishWorkspace, sql: str, parameters: list[object] | None = None) -> Any:
    def read() -> Any:
        with open_reader(workspace.paths) as database:
            return database.query(sql, parameters or [])

    return with_retry(read, attempts=40, base_delay=0.05)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(
                args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
            )
        except playwright_api.Error as failure:
            pytest.fail(
                "Chromium could not be launched, and this acceptance test does not skip: "
                f"run `uv run playwright install chromium`. ({failure})"
            )
        yield launched
        launched.close()


class Served:
    """A server on a thread, opened on one run."""

    def __init__(self, workspace: PolishWorkspace, run: str | None) -> None:
        self.server = server_module.build_server(workspace.paths, clock=workspace.clock, run=run)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.close()
        self.thread.join(timeout=5)


def start(workspace: PolishWorkspace, dimensions: list[str], scoring: str) -> str:
    modalities = ["text", "audio", "writing"]
    if scoring == "machine+judged" and "pronunciation" in dimensions:
        modalities.append("speech")
    return assessment_service.start(
        workspace.paths,
        dimensions=dimensions,
        modalities=modalities,
        scoring=scoring,
        clock=workspace.clock,
    ).run_id


@pytest.fixture
def opened(browser: Any) -> Iterator[Any]:
    """A page whose clock the test can jump forward."""

    context = browser.new_context(viewport={"width": 420, "height": 720})
    context.grant_permissions(["microphone"])
    context.add_init_script(TRACK_STREAMS)
    page = context.new_page()
    page.clock.install()
    yield page
    context.close()


@pytest.fixture
def serving() -> Iterator[list[Served]]:
    running: list[Served] = []
    yield running
    for served in running:
        served.stop()


def open_page(page: Any, workspace: PolishWorkspace, run: str, serving: list[Served]) -> Served:
    served = Served(workspace, run)
    serving.append(served)
    page.goto(served.server.launch_url)
    return served


def task_on_screen(page: Any) -> str:
    page.wait_for_selector("section.task", timeout=20000)
    return str(page.locator("section.task").get_attribute("data-content"))


def hand_in(page: Any, text: str = "Mieszkam w Poznaniu od trzech lat i lubię to miasto.") -> str:
    content_id = task_on_screen(page)
    page.fill("textarea[name=written]", text)
    page.click("[data-role=hand-in]")
    page.wait_for_selector("[data-role=awaiting-judge] li[data-claim-state]", timeout=20000)
    return content_id


def judge(workspace: PolishWorkspace, run_id: str, *, score: float = 0.7) -> str:
    """Claim the oldest waiting answer and record a verdict for it, as a judge would:
    under the claim, keyed by it. Retried, because the page's server may be mid-read."""

    claimed = with_retry(
        lambda: judging.claim(
            workspace.paths, run=run_id, judge="synthetic-judge", limit=1, clock=workspace.clock
        ),
        attempts=40,
        base_delay=0.05,
    ).claimed[0]
    with_retry(
        lambda: assessment_service.record(
            workspace.paths,
            run=run_id,
            content_id=claimed.task.content_id,
            score=score,
            submission=claimed.submission.submission_id,
            claim=claimed.claim_id,
            assessor_kind="ai",
            assessor="synthetic-judge",
            confidence="medium",
            rubric={"accuracy": score},
            idempotency_key=claimed.claim_id,
            clock=workspace.clock,
        ),
        attempts=40,
        base_delay=0.05,
    )
    return str(claimed.task.content_id)


def tick_until(page: Any, condition: str, *, rounds: int = 8) -> None:
    """Jump the page's clock past the backoff until `condition` (a function) holds."""

    for _ in range(rounds):
        page.clock.fast_forward(PAST_THE_CAP_MS)
        try:
            page.wait_for_function(condition, timeout=2500)
            return
        except playwright_api.TimeoutError:
            continue
    raise AssertionError(f"the page never reached: {condition}")


def screen_reads(page: Any) -> list[int]:
    seen: list[int] = []
    page.on(
        "response",
        lambda response: seen.append(response.status)
        if response.url.endswith("/screen") and response.request.method == "GET"
        else None,
    )
    return seen


def other_task(previous: str) -> str:
    # A function, not an expression: the page's CSP refuses `eval`, which is how Playwright
    # evaluates a bare expression.
    return (
        "() => { const t = document.querySelector('section.task');"
        f" return t !== null && t.dataset.content !== '{previous}'; }}"
    )


#: Any judgement line gone: the note is removed once nothing waits.
NOTHING_WAITS = "() => document.querySelector('[data-role=awaiting-judge]') === null"
#: Fires once a poll has had its chance.
ANY_TIME = "() => true"


# --- written answers ---------------------------------------------------------------------


def test_a_page_started_run_is_judged_where_the_track_keeps_writing_whole(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    served = Served(polish_workspace, None)
    serving.append(served)
    opened.goto(served.server.launch_url)
    opened.click("text=Start a calibration")
    task_on_screen(opened)

    (conditions,) = _rows(polish_workspace, "SELECT conditions_json FROM assessment_runs")[0]
    parsed = json.loads(conditions)
    assert parsed["scoring"] == "machine+judged"
    assert "writing" in parsed["available_modalities"]
    # The fixture's track keeps writing whole and records nothing: no speech is asked for.
    assert "speech" not in parsed["available_modalities"]


def test_a_written_answer_waits_for_its_judge_and_the_page_moves_on_when_judged(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    run_id = start(polish_workspace, ["writing"], "machine+judged")
    sent: list[dict[str, Any]] = []
    opened.on(
        "request",
        lambda request: sent.append(json.loads(request.post_data or "{}"))
        if request.method == "POST" and request.url.endswith("/submission")
        else None,
    )
    open_page(opened, polish_workspace, run_id, serving)

    first = hand_in(opened)

    # Handed in, and said to be waiting on somebody: unclaimed, not finished, not an error.
    assert opened.locator("[data-claim-state=unclaimed]").count() == 1
    assert opened.locator("[data-role=finish-now]").count() == 1
    assert opened.locator("li[data-dimension=writing][data-progress=waiting]").count() == 1
    assert len(sent) == 1 and set(sent[0]) == {"submission_key", "response"}
    stored = _rows(
        polish_workspace,
        "SELECT kind, status, capture_id FROM assessment_submissions WHERE run_id = ?",
        [run_id],
    )
    assert stored == [("text", "pending", sent[0]["submission_key"])]

    assert judge(polish_workspace, run_id) == first
    tick_until(opened, other_task(first))

    assert opened.locator("[data-role=awaiting-judge]").count() == 0
    assert _rows(
        polish_workspace,
        "SELECT count(*) FROM assessment_results WHERE run_id = ? AND content_id = ?",
        [run_id, first],
    ) == [(1,)]


def test_a_late_verdict_serves_the_dimension_it_opens_without_a_reload(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    run_id = start(polish_workspace, ["writing"], "machine+judged")
    open_page(opened, polish_workspace, run_id, serving)
    first = hand_in(opened)
    opened.evaluate("window.__same_document = true")

    # A while passes with nothing to find; the page keeps asking, on its backoff.
    reads = screen_reads(opened)
    tick_until(opened, ANY_TIME)
    opened.clock.fast_forward(PAST_THE_CAP_MS)
    opened.wait_for_timeout(300)
    assert reads and all(status == 200 for status in reads)
    assert opened.locator("section.task").count() == 0

    judge(polish_workspace, run_id)
    tick_until(opened, other_task(first))

    assert opened.evaluate("window.__same_document === true"), "the page was reloaded"


# --- the task in hand ----------------------------------------------------------------------


def record_once(page: Any) -> str:
    """Record the spoken task on screen, press to start and press to stop."""

    content_id = task_on_screen(page)
    page.click("[data-role=record]")
    page.wait_for_selector("[data-role=stop]")
    page.wait_for_timeout(500)
    page.click("[data-role=stop]")
    page.wait_for_function(other_task(content_id), timeout=20000)
    return content_id


def test_a_verdict_opening_another_dimension_leaves_what_the_learner_typed(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    permit_recording(polish_workspace)
    run_id = start(polish_workspace, ["pronunciation", "writing"], "machine+judged")
    open_page(opened, polish_workspace, run_id, serving)

    # The round is pronunciation, then writing, in the run's dimension order. The recording
    # goes to the judge, and the learner starts writing.
    spoken = record_once(opened)
    opened.fill("textarea[name=written]", "Mieszkam w Pozna")
    opened.focus("textarea[name=written]")
    opened.evaluate("window.__task = document.querySelector('section.task')")
    assert opened.locator("li[data-dimension=pronunciation][data-progress=waiting]").count() == 1

    assert judge(polish_workspace, run_id) == spoken
    tick_until(opened, NOTHING_WAITS)

    # Only what surrounds the task changed.
    assert opened.locator("li[data-dimension=pronunciation][data-progress=open]").count() == 1
    assert opened.evaluate("window.__task === document.querySelector('section.task')")
    assert opened.input_value("textarea[name=written]") == "Mieszkam w Pozna"
    assert opened.evaluate("document.activeElement && document.activeElement.name") == "written"

    # The dimension the verdict opened is served when the learner finishes this task.
    opened.type("textarea[name=written]", "niu.")
    opened.click("[data-role=hand-in]")
    opened.wait_for_selector("[data-role=record]", timeout=20000)
    assert task_on_screen(opened) != spoken


def test_a_verdict_opening_another_dimension_leaves_a_live_recording(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    permit_recording(polish_workspace)
    run_id = start(polish_workspace, ["pronunciation", "writing"], "machine+judged")
    open_page(opened, polish_workspace, run_id, serving)

    # The round is pronunciation, then writing: record one, hand the other in.
    spoken = record_once(opened)
    hand_in(opened)
    # Both wait on the judge, so the page waits -- and asks.
    opened.wait_for_selector("[data-role=finish-now]")

    # The recording is marked first: pronunciation opens, and the page serves it.
    assert judge(polish_workspace, run_id) == spoken
    tick_until(opened, other_task(spoken))
    opened.click("[data-role=record]")
    opened.wait_for_selector("[data-role=stop]")
    opened.evaluate("window.__task = document.querySelector('section.task')")

    # The written answer is marked while the learner is speaking.
    judge(polish_workspace, run_id)
    tick_until(opened, NOTHING_WAITS)

    assert opened.evaluate("window.__task === document.querySelector('section.task')")
    assert opened.locator("[data-role=stop]").count() == 1
    assert opened.evaluate(
        "window.__streams.flatMap((s) => s.getTracks()).some((t) => t.readyState === 'live')"
    )
    opened.click("[data-role=stop]")
    opened.wait_for_selector("textarea[name=written]", timeout=20000)


# --- how the page polls --------------------------------------------------------------------


def test_a_poll_meeting_the_writer_lock_keeps_the_screen_and_recovers(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    run_id = start(polish_workspace, ["writing"], "machine+judged")
    open_page(opened, polish_workspace, run_id, serving)
    first = hand_in(opened)
    opened.wait_for_selector("[data-role=finish-now]")
    # Anything the status banner ever says is kept, so a flash between checks is seen too.
    opened.evaluate(
        """() => {
          window.__said = [];
          new MutationObserver(() => {
            const banner = document.getElementById('status');
            if (banner && !banner.hidden) window.__said.push(banner.dataset.state);
          }).observe(
            document.getElementById('app'), {subtree: true, childList: true, attributes: true},
          );
        }"""
    )
    reads = screen_reads(opened)

    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(polish_workspace.root), "4"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        tick_until(opened, ANY_TIME)
        for _ in range(3):
            opened.clock.fast_forward(PAST_THE_CAP_MS)
            opened.wait_for_timeout(300)
        assert 503 in reads, reads
        assert opened.locator("[data-role=finish-now]").count() == 1
        assert opened.locator("#status").is_hidden()
    finally:
        holder.wait(timeout=20)

    judge(polish_workspace, run_id)
    tick_until(opened, other_task(first))
    assert opened.evaluate("window.__said") == []


def test_a_hidden_tab_stops_polling_and_a_visible_one_resumes_at_once(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    run_id = start(polish_workspace, ["writing"], "machine+judged")
    open_page(opened, polish_workspace, run_id, serving)
    hand_in(opened)
    opened.wait_for_selector("[data-role=finish-now]")
    reads = screen_reads(opened)
    tick_until(opened, ANY_TIME)
    opened.wait_for_timeout(300)
    assert reads

    visibility = """(value) => {
      Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => value});
      document.dispatchEvent(new Event('visibilitychange'));
    }"""
    opened.evaluate(visibility, "hidden")
    opened.wait_for_timeout(300)
    before = len(reads)
    for _ in range(4):
        opened.clock.fast_forward(PAST_THE_CAP_MS)
        opened.wait_for_timeout(200)
    assert len(reads) == before

    with opened.expect_response(lambda response: response.url.endswith("/screen")):
        opened.evaluate(visibility, "visible")


# --- batches and finishing -----------------------------------------------------------------


def test_a_reload_mid_batch_replays_the_stored_batch_and_serves_nothing_new(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    run_id = start(polish_workspace, ["grammar-control", "vocabulary-control"], "machine")
    keys: list[str] = []
    opened.on(
        "request",
        lambda request: keys.append(json.loads(request.post_data)["idempotency_key"])
        if request.method == "POST" and request.url.endswith("/batch")
        else None,
    )
    open_page(opened, polish_workspace, run_id, serving)
    first = task_on_screen(opened)
    served = _rows(
        polish_workspace,
        "SELECT content_id FROM assessment_run_tasks WHERE run_id = ? ORDER BY content_id",
        [run_id],
    )
    assert len(keys) == 1 and len(served) == 2
    if opened.locator(".choices button").count():
        opened.locator(".choices button").first.click()
    else:
        opened.fill("input[name=response]", "nie wiem")
        opened.click("[data-role=submit]")
    opened.wait_for_function(other_task(first), timeout=20000)
    second = task_on_screen(opened)
    assert len(keys) == 1, "the batch's second task was served again rather than worked through"

    opened.reload()

    assert task_on_screen(opened) == second
    assert keys[1:] == [keys[0]], keys
    assert (
        _rows(
            polish_workspace,
            "SELECT content_id FROM assessment_run_tasks WHERE run_id = ? ORDER BY content_id",
            [run_id],
        )
        == served
    )


def test_a_batch_whose_answer_was_lost_is_replayed_by_its_key_after_a_reload(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    """The round lands and its response is lost. The key was written down before it was
    sent, so the reloaded page replays it rather than serving a second round."""

    run_id = start(polish_workspace, ["grammar-control", "vocabulary-control"], "machine")
    phase = {"now": "lose"}
    keys: list[str] = []

    def handle(route: Any) -> None:
        keys.append(json.loads(route.request.post_data)["idempotency_key"])
        if phase["now"] == "lose":
            phase["now"] = "block"
            route.fetch()  # it reaches the server and serves...
            route.abort()  # ...and the page never hears back
        elif phase["now"] == "block":
            route.abort()  # the old document's retries go nowhere
        else:
            route.continue_()

    opened.route("**/batch", handle)
    with opened.expect_event("requestfailed"):
        open_page(opened, polish_workspace, run_id, serving)
    opened.reload()
    phase["now"] = "open"
    task_on_screen(opened)

    assert set(keys) == {keys[0]}, keys
    assert _rows(
        polish_workspace, "SELECT count(*) FROM assessment_run_tasks WHERE run_id = ?", [run_id]
    ) == [(2,)]
    assert _rows(
        polish_workspace, "SELECT count(*) FROM assessment_batches WHERE run_id = ?", [run_id]
    ) == [(1,)]


def test_finishing_with_answers_outstanding_offers_waiting_or_finishing_without_them(
    opened: Any, polish_workspace: PolishWorkspace, serving: list[Served]
) -> None:
    run_id = start(polish_workspace, ["writing"], "machine+judged")
    bodies: list[dict[str, Any]] = []
    opened.on(
        "request",
        lambda request: bodies.append(json.loads(request.post_data or "{}"))
        if request.method == "POST" and request.url.endswith("/finalization")
        else None,
    )
    open_page(opened, polish_workspace, run_id, serving)
    hand_in(opened)

    opened.click("[data-role=finish-now]")
    opened.wait_for_selector("[data-role=finish-without]")
    assert opened.locator(".error").count() == 0
    # Waiting is a way back to the waiting screen, not a request.
    opened.click("[data-role=keep-waiting]")
    opened.wait_for_selector("[data-role=finish-now]")
    assert len(bodies) == 1

    opened.click("[data-role=finish-now]")
    opened.click("[data-role=finish-without]")
    opened.wait_for_selector("text=This calibration is finalized.")

    # The second press's refusal and its remedy are one key: the flag is the difference.
    assert bodies[2]["idempotency_key"] == bodies[1]["idempotency_key"]
    assert "exclude_outstanding" not in bodies[1] and bodies[2]["exclude_outstanding"] is True
    assert _rows(
        polish_workspace,
        "SELECT status, withdrawn_code FROM assessment_submissions WHERE run_id = ?",
        [run_id],
    ) == [("withdrawn", "assessment_run_finalized")]
