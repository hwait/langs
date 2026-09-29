"""What `artifact list` answers when it is asked something narrower than "everything".

Nine test files call `listing`, and every one of them calls it bare -- so the `kind` filter
and the `limit`/`total` accounting had no coverage at all. The accounting is the part worth
pinning: `total` counts the track's rows and `artifacts` is capped at `limit`, so a change
that conflated them would tell a learner they hold three recordings when they hold forty,
and a retention decision made on that number is made on a false one.
"""

from __future__ import annotations

import hashlib

import pytest

from linguawiki.services import artifacts as artifact_service
from linguawiki.services import onboarding as onboarding_service
from tests.conftest import PolishWorkspace


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    return polish_workspace


def register(workspace: PolishWorkspace, name: str, *, kind: str = "audio") -> str:
    path = workspace.root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    registered = artifact_service.register(
        workspace.paths,
        relative_path=f"imports/{name}",
        kind=kind,
        retained=True,
        expected_sha256=hashlib.sha256(name.encode()).hexdigest(),
        track=workspace.track_id,
        clock=workspace.clock,
    )
    artifact_id: str = registered.artifact_id
    return artifact_id


def test_the_kind_filter_narrows_the_rows_and_the_total_with_them(
    running: PolishWorkspace,
) -> None:
    """A filtered listing's `total` counts the filtered rows, not the track's.

    Both the count and the rows go through the same `where`, so this pins that they stay
    together: a total describing a wider set than the rows it accompanies is a number that
    invites exactly the wrong conclusion.
    """

    recording = register(running, "spoken.opus", kind="audio")
    written = register(running, "notes.txt", kind="transcript")

    audio = artifact_service.listing(
        running.paths, kind="audio", track=running.track_id, clock=running.clock
    )
    assert [entry.artifact_id for entry in audio.artifacts] == [recording]
    assert audio.total == 1, "the total still counted the transcript"

    transcripts = artifact_service.listing(
        running.paths, kind="transcript", track=running.track_id, clock=running.clock
    )
    assert [entry.artifact_id for entry in transcripts.artifacts] == [written]

    everything = artifact_service.listing(
        running.paths, track=running.track_id, clock=running.clock
    )
    assert everything.total == 2


def test_a_kind_nothing_was_registered_under_lists_nothing(running: PolishWorkspace) -> None:
    """An empty answer, not an error: nothing of that kind is a fact about the track."""

    register(running, "only-audio.opus")
    empty = artifact_service.listing(
        running.paths, kind="transcript", track=running.track_id, clock=running.clock
    )
    assert empty.artifacts == ()
    assert empty.total == 0


def test_the_limit_caps_the_rows_while_the_total_still_says_how_many_there_are(
    running: PolishWorkspace,
) -> None:
    """`limit` truncates the page; `total` is how many exist.

    This is the distinction a learner's retention decision rests on, and the newest rows
    are the ones a truncated page must show -- an accidental `ASC` would page through the
    oldest recordings and silently hide everything recent.
    """

    identifiers = [register(running, f"recording-{index}.opus") for index in range(5)]

    page = artifact_service.listing(
        running.paths, limit=2, track=running.track_id, clock=running.clock
    )
    assert len(page.artifacts) == 2, "the limit did not cap the page"
    assert page.total == 5, "the total should count every row, not the page"
    assert [entry.artifact_id for entry in page.artifacts] == identifiers[:-3:-1], (
        "a truncated page must show the newest recordings"
    )
