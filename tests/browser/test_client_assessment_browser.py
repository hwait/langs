"""The assessment screen in a real browser: buttons, playback, retries, and resuming.

An HTTP exercise proves what the server owes the page and nothing about the page: it
presses no button, plays no recording, never authenticates the way a browser must, and
cannot see a retry state. These tests drive Chromium through the launch URL exactly as
`client serve` prints it.

They are part of the release gate and they **fail** when the browser cannot be launched.
A skipped acceptance test is a gate that passes by not looking.
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

# A plain import, not `importorskip`: Playwright is a pinned dev dependency, and an
# acceptance test that skips when it is missing passes by not looking.
from playwright import sync_api as playwright_api

from linguawiki.client import server as server_module
from linguawiki.db.connection import open_reader
from tests.conftest import PolishWorkspace
from tests.support.recordings import publish_pilot_with_recordings

FINISH = "Finish and save the estimates"


def _scalar(workspace: PolishWorkspace, sql: str, parameters: list[object] | None = None) -> Any:
    """A read beside a live page, retried: the server in this process may be mid-request."""

    from linguawiki.retrying import with_retry

    def read() -> Any:
        with open_reader(workspace.paths) as database:
            return database.scalar(sql, parameters or [])

    return with_retry(read, attempts=40, base_delay=0.05)


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


class Served:
    """A server on a thread, restartable on the same port with a new token."""

    def __init__(self, workspace: PolishWorkspace, port: int = 0) -> None:
        self.workspace = workspace
        self.server = server_module.build_server(workspace.paths, clock=workspace.clock, port=port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.close()
        self.thread.join(timeout=5)


@pytest.fixture
def recorded(polish_workspace: PolishWorkspace, tmp_path: Path) -> PolishWorkspace:
    publish_pilot_with_recordings(polish_workspace, tmp_path, replay_allowance=2)
    return polish_workspace


@pytest.fixture
def served(recorded: PolishWorkspace) -> Iterator[Served]:
    running = Served(recorded)
    yield running
    running.stop()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    context = browser.new_context(viewport={"width": 420, "height": 720})
    opened = context.new_page()
    yield opened
    context.close()


def _sent_results(page: Any) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    page.on(
        "request",
        lambda request: bodies.append(json.loads(request.post_data or "{}"))
        if request.method == "POST" and request.url.endswith("/results")
        else None,
    )
    return bodies


def _wait_for_work(page: Any) -> str:
    """The next thing on screen: a task, the finish button, or the paused panel."""

    page.wait_for_selector(f"section.task, button:has-text('{FINISH}')", timeout=20000)
    if page.locator(f"button:has-text('{FINISH}')").count():
        return "finish"
    return str(page.locator("section.task").get_attribute("data-content"))


def _answer_current(page: Any) -> list[str]:
    """Answer what is on screen the way a learner would. Returns the values offered."""

    current = page.locator("section.task").get_attribute("data-content")
    choices = page.locator(".choices button")
    offered: list[str] = []
    if choices.count():
        offered = [
            str(value) for value in choices.evaluate_all("(b) => b.map((x) => x.dataset.value)")
        ]
        choices.first.click()
    else:
        page.fill("input[name=response]", "nie wiem")
        page.click("[data-role=submit]")
    page.wait_for_function(
        """(previous) => {
            const task = document.querySelector('section.task');
            return !task || task.dataset.content !== previous;
        }""",
        arg=current,
        timeout=20000,
    )
    return offered


def _start(page: Any, served: Served) -> None:
    page.goto(served.server.launch_url)
    page.click("text=Start a calibration")


def test_the_launch_url_authenticates_and_then_leaves_the_address_bar(
    page: Any, served: Served
) -> None:
    page.goto(served.server.launch_url)

    page.wait_for_selector("text=Start a calibration")
    assert "#" not in page.url
    assert served.server.token not in page.url


def test_a_calibration_completes_by_button_and_matches_the_report(
    page: Any, served: Served, recorded: PolishWorkspace
) -> None:
    sent = _sent_results(page)
    _start(page, served)
    offered_per_answer: list[list[str]] = []
    for _ in range(120):
        state = _wait_for_work(page)
        if state == "finish":
            break
        if page.locator("[data-role=play]").count():
            page.click("[data-role=play]")
            page.wait_for_selector("[data-role=plays]:has-text('1 play left')")
        offered_per_answer.append(_answer_current(page))
    else:  # pragma: no cover
        raise AssertionError("the calibration did not finish in the browser")
    page.click(f"text={FINISH}")
    page.wait_for_selector("text=This calibration is finalized.")

    # Every submission is the learner's response and nothing that decides its score; a
    # button submits its choice `value`, never an index or the drawn text.
    assert sent
    for body, offered in zip(sent, offered_per_answer, strict=True):
        assert set(body) == {"content_id", "response", "idempotency_key"}
        if offered:
            assert body["response"] in offered
    # What the page drew is exactly what the CLI reports for the run.
    run_id = str(_scalar(recorded, "SELECT run_id FROM assessment_runs"))
    sources = _scalar(recorded, "SELECT list(DISTINCT score_source) FROM assessment_results")
    assert sources == ["computed"]
    from linguawiki.retrying import with_retry
    from linguawiki.services import assessment as assessment_service

    report = with_retry(lambda: assessment_service.report(recorded.paths, run=run_id), attempts=40)
    for dimension in report.dimensions:
        row = page.locator(f"li[data-dimension='{dimension.dimension}']")
        assert dimension.confidence in row.inner_text()
        if dimension.estimated_level is not None:
            assert dimension.estimated_level in row.inner_text()
        if dimension.status == "not-tested":
            assert "not-tested" in row.inner_text()


def test_replays_are_limited_and_reach_the_stored_result(
    page: Any, served: Served, recorded: PolishWorkspace
) -> None:
    _start(page, served)
    for _ in range(10):
        _wait_for_work(page)
        if page.locator("[data-role=play]").count():
            break
        _answer_current(page)
    content_id = page.locator("section.task").get_attribute("data-content")

    page.click("[data-role=play]")
    page.wait_for_selector("[data-role=plays]:has-text('1 play left')")
    assert page.evaluate("document.querySelector('audio').src.startsWith('blob:')")
    page.click("[data-role=play]")
    page.wait_for_selector("[data-role=plays]:has-text('0 plays left')")
    assert page.locator("[data-role=play]").is_disabled()
    _answer_current(page)

    count = _scalar(
        recorded, "SELECT play_count FROM assessment_results WHERE content_id = ?", [content_id]
    )
    assert count == 2


def test_two_clicks_are_one_answer(page: Any, served: Served, recorded: PolishWorkspace) -> None:
    _start(page, served)
    for _ in range(10):
        _wait_for_work(page)
        if page.locator(".choices button").count():
            break
        _answer_current(page)
    content_id = page.locator("section.task").get_attribute("data-content")

    page.locator(".choices button").first.dblclick()
    page.wait_for_function(
        "(previous) => document.querySelector('section.task')?.dataset.content !== previous",
        arg=content_id,
        timeout=20000,
    )

    count = _scalar(
        recorded, "SELECT count(*) FROM assessment_results WHERE content_id = ?", [content_id]
    )
    assert count == 1


HOLD = """
import sys, time
from linguawiki.paths import workspace_paths
from linguawiki.db.connection import open_writer
with open_writer(workspace_paths(sys.argv[1]), command="test.hold"):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_another_process_holding_the_database_is_a_visible_wait(
    page: Any, served: Served, recorded: PolishWorkspace
) -> None:
    _start(page, served)
    first = _wait_for_work(page)

    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(recorded.root), "4"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        if page.locator(".choices button").count():
            page.locator(".choices button").first.click()
        else:
            page.fill("input[name=response]", "nie wiem")
            page.click("[data-role=submit]")
        page.wait_for_selector("#status[data-state=waiting]", timeout=10000)
        assert "Another LinguaWiki command" in page.inner_text("#status")
    finally:
        holder.wait(timeout=20)
    page.wait_for_function(
        "(previous) => document.querySelector('section.task')?.dataset.content !== previous",
        arg=first,
        timeout=20000,
    )
    assert page.locator("#status").is_hidden()


def test_a_paused_run_survives_a_server_restart_and_a_new_token(
    browser: Any, page: Any, served: Served, recorded: PolishWorkspace
) -> None:
    _start(page, served)
    for _ in range(10):
        _wait_for_work(page)
        if page.locator("[data-role=play]").count():
            break
        _answer_current(page)
    outstanding = page.locator("section.task").get_attribute("data-content")
    page.click("[data-role=play]")
    page.wait_for_selector("[data-role=plays]:has-text('1 play left')")
    page.click("text=Pause")
    page.wait_for_selector("text=This calibration is paused.")

    # A new start of the server on the same port: a new token, and the old page's is dead.
    port = served.server.port
    old_token = served.server.token
    served.stop()
    restarted = Served(recorded, port=port)
    try:
        assert restarted.server.token != old_token
        page.click("text=Resume")
        page.wait_for_selector("text=was restarted")

        fresh = browser.new_context(viewport={"width": 420, "height": 720}).new_page()
        fresh.goto(restarted.server.launch_url)
        fresh.click("button:has-text('Resume')")
        fresh.wait_for_selector("section.task")
        assert fresh.locator("section.task").get_attribute("data-content") == outstanding
        assert "1 play left" in fresh.inner_text("[data-role=plays]")
        fresh.context.close()
    finally:
        restarted.stop()
        served.server = restarted.server  # the fixture's teardown closes it again harmlessly
        served.thread = restarted.thread


def test_a_reload_after_a_lost_answer_resends_it_rather_than_answering_twice(
    page: Any, served: Served, recorded: PolishWorkspace
) -> None:
    """The answer lands, its response is lost, and the learner reloads.

    The pending operation was written to sessionStorage before it was sent, so the
    reloaded page resends it -- same key, same body -- and the server answers with the
    first result instead of recording a second.
    """

    _start(page, served)
    content_id = _wait_for_work(page)
    keys: list[str] = []

    def lose_the_response(route: Any) -> None:
        keys.append(json.loads(route.request.post_data)["idempotency_key"])
        route.fetch()  # it reaches the server and lands...
        route.abort()  # ...and the page never hears back

    page.route("**/results", lose_the_response)
    with page.expect_event("requestfailed"):
        if page.locator(".choices button").count():
            page.locator(".choices button").first.click()
        else:
            page.fill("input[name=response]", "nie wiem")
            page.click("[data-role=submit]")
    page.unroute("**/results")
    # What survives the reload: the operation, unchanged, recorded before it was sent.
    pending = json.loads(page.evaluate("sessionStorage.getItem('linguawiki.pending')"))
    assert pending["body"]["idempotency_key"] == keys[0]
    resent: list[str] = []
    page.on(
        "request",
        lambda request: resent.append(json.loads(request.post_data)["idempotency_key"])
        if request.method == "POST" and request.url.endswith("/results")
        else None,
    )
    page.goto(served.server.launch_url)
    page.wait_for_function(
        "(previous) => document.querySelector('section.task')?.dataset.content !== previous",
        arg=content_id,
        timeout=20000,
    )

    assert keys and resent and resent[0] == keys[0]
    count = _scalar(
        recorded, "SELECT count(*) FROM assessment_results WHERE content_id = ?", [content_id]
    )
    assert count == 1


def test_a_run_opened_elsewhere_is_never_served_by_the_page(
    page: Any, served: Served, recorded: PolishWorkspace
) -> None:
    """Under `any` the next task can need a judge or a recording; the page serves none."""

    from linguawiki.services import assessment as assessment_service

    run = assessment_service.start(recorded.paths, clock=recorded.clock)

    page.goto(served.server.launch_url)
    page.wait_for_selector(f"[data-run='{run.run_id}']")
    assert "assess skill" in page.inner_text("ul.runs")
    page.click(f"[data-run='{run.run_id}']")
    page.wait_for_selector("text=opened outside this page")

    served_count = _scalar(
        recorded, "SELECT count(*) FROM assessment_run_tasks WHERE run_id = ?", [run.run_id]
    )
    assert served_count == 0
