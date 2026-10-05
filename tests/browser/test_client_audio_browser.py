"""A spoken answer recorded in a real browser, through a fake microphone.

Chromium's fake capture device produces a generated tone, so nothing anybody said is ever
recorded. What is under test is the page: press to start and press to stop, with no
countdown; the retention policy said on screen; the recording uploaded under the
identifier the page minted; and the task shown as waiting for a judge afterwards rather
than hidden.

Part of the release gate, and it **fails** when the browser cannot be launched.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from playwright import sync_api as playwright_api

from linguawiki.client import server as server_module
from linguawiki.db.connection import open_reader
from linguawiki.retrying import with_retry
from linguawiki.services import assessment as assessment_service
from tests.conftest import PolishWorkspace
from tests.integration.test_client_audio import permit_recording


def _rows(workspace: PolishWorkspace, sql: str) -> list[Any]:
    def read() -> list[Any]:
        with open_reader(workspace.paths) as database:
            return database.query(sql)

    return with_retry(read, attempts=40, base_delay=0.05)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(
                args=[
                    # A generated tone instead of a microphone, and no permission prompt.
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                ]
            )
        except playwright_api.Error as failure:
            pytest.fail(
                "Chromium could not be launched, and this acceptance test does not skip: "
                f"run `uv run playwright install chromium`. ({failure})"
            )
        yield launched
        launched.close()


@pytest.fixture
def speaking(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    permit_recording(
        polish_workspace, audio_retention_policy="rolling-days", audio_retention_days=30
    )
    return polish_workspace


@pytest.fixture
def served(speaking: PolishWorkspace) -> Iterator[Any]:
    run = assessment_service.start(
        speaking.paths,
        dimensions=["pronunciation"],
        modalities=["text", "audio", "speech"],
        scoring="machine+recorded",
        clock=speaking.clock,
    )
    server = server_module.build_server(speaking.paths, clock=speaking.clock, run=run.run_id)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.close()
    thread.join(timeout=5)


def test_a_spoken_task_is_recorded_by_press_and_shown_as_waiting_for_a_judge(
    browser: Any, served: Any, speaking: PolishWorkspace
) -> None:
    context = browser.new_context(viewport={"width": 420, "height": 720})
    context.grant_permissions(["microphone"])
    page = context.new_page()
    uploads: list[str] = []
    page.on(
        "request",
        lambda request: uploads.append(request.url) if "/captures/" in request.url else None,
    )
    try:
        page.goto(served.launch_url)
        page.wait_for_selector("[data-role=record]", timeout=20000)
        # The policy is said, not chosen.
        assert "30 day(s)" in page.locator("[data-role=retention]").inner_text()

        page.click("[data-role=record]")
        page.wait_for_selector("[data-role=stop]")
        page.wait_for_timeout(700)
        page.click("[data-role=stop]")
        page.wait_for_selector("[data-role=awaiting-judge]", timeout=20000)

        assert len(uploads) == 1
        (captured,) = _rows(speaking, "SELECT capture_id, state FROM capture_stagings")
        assert captured[1] == "registered" and uploads[0].endswith(str(captured[0]))
        assert _rows(speaking, "SELECT status FROM assessment_submissions") == [("pending",)]
        stored = _rows(speaking, "SELECT relative_path, byte_size FROM artifacts")
        assert len(stored) == 1 and int(stored[0][1]) > 0
        assert (speaking.root / str(stored[0][0])).is_file()
    finally:
        context.close()


#: Keeps every microphone stream the page opens, so a test can ask whether it was stopped.
TRACK_STREAMS = """
window.__streams = [];
const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async (constraints) => {
  const stream = await original(constraints);
  window.__streams.push(stream);
  return stream;
};
"""


def test_pausing_mid_recording_turns_the_microphone_off(
    browser: Any, served: Any, speaking: PolishWorkspace
) -> None:
    context = browser.new_context(viewport={"width": 420, "height": 720})
    context.grant_permissions(["microphone"])
    context.add_init_script(TRACK_STREAMS)
    page = context.new_page()
    try:
        page.goto(served.launch_url)
        page.click("[data-role=record]", timeout=20000)
        page.wait_for_selector("[data-role=stop]")
        assert page.evaluate(
            "window.__streams.flatMap((s) => s.getTracks()).some((t) => t.readyState === 'live')"
        )

        page.click("text=Pause")
        page.wait_for_selector("text=This calibration is paused.")

        assert page.evaluate(
            "window.__streams.flatMap((s) => s.getTracks()).every((t) => t.readyState === 'ended')"
        )
        assert _rows(speaking, "SELECT count(*) FROM capture_stagings") == [(0,)]
    finally:
        context.close()


def test_the_page_does_not_promise_a_deletion_the_sweep_never_makes(
    browser: Any, polish_workspace: PolishWorkspace
) -> None:
    """Under `delete-after-ingestion` a recording made here is not swept, so it is not
    promised to be."""

    permit_recording(polish_workspace, audio_retention_policy="delete-after-ingestion")
    run = assessment_service.start(
        polish_workspace.paths,
        dimensions=["pronunciation"],
        modalities=["speech"],
        scoring="machine+recorded",
        clock=polish_workspace.clock,
    )
    server = server_module.build_server(
        polish_workspace.paths, clock=polish_workspace.clock, run=run.run_id
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(server.launch_url)
        note = page.locator("[data-role=retention]")
        note.wait_for(timeout=20000)
        assert "kept until you remove it" in note.inner_text()
    finally:
        context.close()
        server.close()
        thread.join(timeout=5)


def test_an_unreadable_answer_to_an_upload_resends_the_same_take(
    browser: Any, served: Any, speaking: PolishWorkspace
) -> None:
    """An envelope with neither `ok` nor `error` is not an answer: the page resends the same
    bytes under the same identifier, and the server answers the resend."""

    context = browser.new_context(viewport={"width": 420, "height": 720})
    context.grant_permissions(["microphone"])
    page = context.new_page()
    seen: list[str] = []

    def first_unreadable(route: Any) -> None:
        seen.append(route.request.url)
        if len(seen) == 1:
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.continue_()

    page.route("**/captures/**", first_unreadable)
    try:
        page.goto(served.launch_url)
        page.click("[data-role=record]", timeout=20000)
        page.wait_for_selector("[data-role=stop]")
        page.wait_for_timeout(500)
        page.click("[data-role=stop]")
        page.wait_for_selector("[data-role=awaiting-judge]", timeout=20000)

        assert len(seen) == 2 and seen[0] == seen[1]
        assert _rows(speaking, "SELECT count(*) FROM assessment_submissions") == [(1,)]
    finally:
        context.close()
