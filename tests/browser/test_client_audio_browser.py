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
