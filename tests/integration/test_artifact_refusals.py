"""Every refusal `artifacts.register` and `artifacts.purge` can raise, exercised once.

These are the guards the destructive paths stand behind, and until this file existed ten
of them had no test at all: they were reachable only through the packaging and sweep
flows, which pick one route through the module and leave the rest of the vocabulary
unexercised. A guard with no test is a guard that can be deleted by a refactor without
anything going red, which matters more here than elsewhere -- this module is the only one
that unlinks a learner's recordings.

Each test names the state it creates and asserts on the error *code*, because the code is
the part of the contract the CLI and the skills read.
"""

from __future__ import annotations

import errno
import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from tests.conftest import PolishWorkspace


@pytest.fixture
def running(polish_workspace: PolishWorkspace) -> PolishWorkspace:
    """An onboarded track, which is the least a registration needs."""

    onboarding_service.start(
        polish_workspace.paths, declared_level="A2", clock=polish_workspace.clock
    )
    onboarding_service.finalize(polish_workspace.paths, clock=polish_workspace.clock)
    return polish_workspace


def recording(root: Path, name: str, body: bytes = b"opus-bytes") -> tuple[str, str]:
    """A file under a private root, with the digest a caller would name for it."""

    path = root / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return f"imports/{name}", hashlib.sha256(body).hexdigest()


def held(workspace: PolishWorkspace, name: str) -> artifact_service.ArtifactReport:
    relative, _ = recording(workspace.root, name)
    registered: artifact_service.ArtifactReport = artifact_service.register(
        workspace.paths,
        relative_path=relative,
        # Explicit: registering without this lets the track's retention policy delete the
        # file, which is correct but leaves nothing for these tests to assert about.
        retained=True,
        track=workspace.track_id,
        clock=workspace.clock,
    )
    return registered


def second_track(workspace: PolishWorkspace) -> learner_service.TrackRecord:
    """Another learner's `pl` track, for the cross-track scope cases.

    A second track in the same workspace, not a second language: the pilot pack serves
    only `pl`, and the thing under test is track scoping, not pack resolution.
    """

    other_user = learner_service.create_user(
        workspace.paths,
        display_name="Second Learner",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        clock=workspace.clock,
    )
    created: learner_service.TrackRecord = learner_service.create_track(
        workspace.paths,
        user=other_user.user_id,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        clock=workspace.clock,
    )
    return created


def refusal(workspace: PolishWorkspace, **kwargs: str | int | bool | None) -> str:
    """Register with `kwargs` and return the code it refused with."""

    with pytest.raises(LinguaWikiError) as raised:
        artifact_service.register(
            workspace.paths,
            track=workspace.track_id,
            clock=workspace.clock,
            **kwargs,
        )
    code: str = raised.value.payload.code
    return code


# --- Where a file may live -------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative_path", "code"),
    [
        ("/etc/passwd", "unsafe_artifact_path"),
        ("imports/../../escape.opus", "unsafe_artifact_path"),
        ("notes/recording.opus", "artifact_outside_private_roots"),
        ("recording.opus", "artifact_outside_private_roots"),
    ],
)
def test_a_path_outside_the_private_roots_is_refused_by_shape_alone(
    running: PolishWorkspace, relative_path: str, code: str
) -> None:
    """The shape of the path decides this, before anything touches the filesystem.

    `artifacts/` and `imports/` are the directories the workspace template keeps out of
    Git. A recording registered anywhere else is a recording Git is willing to commit, so
    the refusal has to happen for a path that does not exist yet -- which is why these
    cases create no file.
    """

    assert refusal(running, relative_path=relative_path) == code


# --- What is being registered ----------------------------------------------------------


def test_an_unknown_kind_or_origin_or_rights_class_is_refused_by_name(
    running: PolishWorkspace,
) -> None:
    """The three vocabularies, each refused with its own code.

    They share a helper, so one generic code for all three would read the same in the
    tests and leave the CLI unable to say which field was wrong.
    """

    relative, _ = recording(running.root, "vocabulary.opus")
    assert refusal(running, relative_path=relative, kind="hologram") == "unknown_artifact_kind"
    assert refusal(running, relative_path=relative, origin="dream") == "unknown_artifact_origin"
    assert refusal(running, relative_path=relative, rights="mine") == "unknown_rights_class"


def test_a_file_that_is_not_the_named_one_is_refused_rather_than_recorded(
    running: PolishWorkspace,
) -> None:
    """`--expected-sha256` is a claim about which recording this is.

    Registering the file anyway would attach every later claim about one recording to
    another, which is the failure this argument exists to prevent.
    """

    relative, _ = recording(running.root, "expected.opus", b"the-actual-bytes")
    wrong = hashlib.sha256(b"some-other-recording").hexdigest()
    assert (
        refusal(running, relative_path=relative, expected_sha256=wrong) == "artifact_hash_mismatch"
    )
    assert (running.root / relative).is_file(), "the file was not the caller's to delete"


def test_one_recording_cannot_answer_to_two_producer_identifiers(
    running: PolishWorkspace,
) -> None:
    """Identical bytes arriving under a second `external_id` is refused.

    Accepting it would leave two producer identifiers resolving to one row, so a later
    lookup by identifier would land claims on whichever row it happened to find.
    """

    relative, _ = recording(running.root, "identified.opus")
    artifact_service.register(
        running.paths,
        relative_path=relative,
        external_id="call-a/utt-1",
        retained=True,
        track=running.track_id,
        clock=running.clock,
    )
    again = running.root / "imports" / "identified-again.opus"
    again.write_bytes((running.root / relative).read_bytes())
    assert (
        refusal(
            running,
            relative_path="imports/identified-again.opus",
            external_id="call-b/utt-9",
            # Agreeing about retention, so the identity is the only disagreement left --
            # otherwise the retention conflict fires first and this asserts nothing.
            retained=True,
        )
        == "artifact_identity_conflict"
    )
    assert again.is_file(), "a refused registration deleted the learner's copy"


# --- Clips -----------------------------------------------------------------------------


def test_a_clip_must_be_audio_of_audio_that_exists(running: PolishWorkspace) -> None:
    """The three clip refusals that had no test.

    A clip earns longer retention than the recording it came from, so every part of the
    claim -- that it is audio, that its source is this track's, that the source is a
    recording -- is checked before the row exists.
    """

    relative, _ = recording(running.root, "clip.opus")
    source = held(running, "source.opus")
    assert (
        refusal(
            running,
            relative_path=relative,
            kind="transcript",
            clip_of=source.artifact_id,
            clip_starts_at_ms=0,
            clip_ends_at_ms=1000,
        )
        == "clip_is_not_audio"
    )
    assert (
        refusal(
            running,
            relative_path=relative,
            clip_of="art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clip_starts_at_ms=0,
            clip_ends_at_ms=1000,
        )
        == "clip_source_not_found"
    )


def test_a_clip_of_a_recording_in_another_track_is_not_found(
    running: PolishWorkspace,
) -> None:
    """Resolution is scoped to the track, so another track's recording is simply absent.

    This is the same code as a nonexistent source on purpose: from inside this track there
    is no difference, and saying "that belongs to your other track" would leak the fact
    that it exists.
    """

    source = held(running, "elsewhere.opus")
    other = second_track(running)
    relative, _ = recording(running.root, "cross-track-clip.opus")
    with pytest.raises(LinguaWikiError) as raised:
        artifact_service.register(
            running.paths,
            relative_path=relative,
            clip_of=source.artifact_id,
            clip_starts_at_ms=0,
            clip_ends_at_ms=1000,
            track=other.track_id,
            clock=running.clock,
        )
    assert raised.value.payload.code == "clip_source_not_found"


# --- Purge -----------------------------------------------------------------------------


def test_purging_something_this_workspace_does_not_hold_is_refused(
    running: PolishWorkspace,
) -> None:
    assert_code(
        lambda: artifact_service.purge(
            running.paths,
            artifact="art_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            track=running.track_id,
            clock=running.clock,
        ),
        "artifact_not_found",
    )


def test_purging_another_tracks_recording_is_refused_as_out_of_scope(
    running: PolishWorkspace,
) -> None:
    """A purge names one row, and the row names its track.

    Purge deletes a file, so resolving across tracks would let a command issued about one
    track destroy evidence belonging to another.
    """

    source = held(running, "other-tracks.opus")
    other = second_track(running)
    assert_code(
        lambda: artifact_service.purge(
            running.paths,
            artifact=source.artifact_id,
            track=other.track_id,
            clock=running.clock,
        ),
        "artifact_out_of_scope",
    )
    assert (running.root / "imports" / "other-tracks.opus").is_file()


def test_an_unknown_purge_reason_is_refused_before_the_file_is_touched(
    running: PolishWorkspace,
) -> None:
    """The reason is recorded on the tombstone, so an unrecognized one is not accepted.

    The ordering matters: the vocabulary is checked before the deletion, or the workspace
    would end up with a deleted file and no honest account of why.
    """

    source = held(running, "reasoned.opus")
    assert_code(
        lambda: artifact_service.purge(
            running.paths,
            artifact=source.artifact_id,
            reason="because",
            track=running.track_id,
            clock=running.clock,
        ),
        "unknown_purge_reason",
    )
    assert (running.root / "imports" / "reasoned.opus").is_file(), "refused, but deleted anyway"


def assert_code(action: Callable[[], object], code: str) -> None:
    """Run `action` and assert it refused with `code`."""

    with pytest.raises(LinguaWikiError) as raised:
        action()
    assert raised.value.payload.code == code


# --- The last reachable gaps -----------------------------------------------------------


def test_a_recording_can_be_attached_to_a_catalogued_source(running: PolishWorkspace) -> None:
    """`register(source=...)` resolves the source and stores it on the row.

    This argument had no test at all, which mattered because the resolution runs inside
    the writer: a source name the preflight would accept and the writer would not is the
    shape that produced several of this stage's defects.
    """

    from linguawiki.services import sources as source_service

    catalogued = source_service.add(
        running.paths,
        kind="podcast",
        title="Polski Daily",
        rights="metadata-only",
        has_audio=True,
        clock=running.clock,
    )
    relative, digest = recording(running.root, "from-a-source.opus")
    registered = artifact_service.register(
        running.paths,
        relative_path=relative,
        kind="audio",
        source=catalogued.source_id,
        retained=True,
        expected_sha256=digest,
        track=running.track_id,
        clock=running.clock,
    )
    assert registered.source_id == catalogued.source_id

    listed = artifact_service.listing(running.paths, track=running.track_id, clock=running.clock)
    stored = {entry.artifact_id: entry.source_id for entry in listed.artifacts}
    assert stored[registered.artifact_id] == catalogued.source_id, "the link was not stored"


def test_a_source_this_track_does_not_hold_is_refused(running: PolishWorkspace) -> None:
    """Resolution happens against the track, so an unknown source stops the registration."""

    relative, _ = recording(running.root, "unknown-source.opus")
    with pytest.raises(LinguaWikiError):
        artifact_service.register(
            running.paths,
            relative_path=relative,
            source="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            retained=True,
            track=running.track_id,
            clock=running.clock,
        )


def test_purging_an_already_purged_recording_changes_nothing_and_says_so(
    running: PolishWorkspace,
) -> None:
    """Running the same purge twice is a thing a worried learner does.

    The second call has to be a no-op that reports the original tombstone rather than an
    error or a second deletion: the recording is already gone, and the honest answer is
    when it went.
    """

    source = held(running, "twice.opus")
    first = artifact_service.purge(
        running.paths,
        artifact=source.artifact_id,
        track=running.track_id,
        clock=running.clock,
    )
    assert first.file_removed
    assert not (running.root / "imports" / "twice.opus").exists()

    again = artifact_service.purge(
        running.paths,
        artifact=source.artifact_id,
        track=running.track_id,
        clock=running.clock,
    )
    assert not again.file_removed, "a second purge claimed to delete something again"
    assert again.purged_at == first.purged_at, "the tombstone's time was rewritten"
    assert any("already purged" in warning for warning in again.warnings)


def test_a_file_that_becomes_unreadable_between_hashing_and_sizing_is_refused(
    running: PolishWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow race in `register`: the digest succeeded, then `stat` did not.

    A real window, because hashing a long recording is not instant. The answer has to be
    the same as for any unreadable file -- refuse -- rather than an `OSError` escaping as
    an unexpected failure, because a recording no command can read is one no claim can
    rest on.
    """

    relative, _ = recording(running.root, "vanishing.opus")
    real_stat = Path.stat

    def vanish(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "vanishing.opus":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", vanish)
    with pytest.raises(LinguaWikiError) as raised:
        artifact_service.register(
            running.paths,
            relative_path=relative,
            retained=True,
            track=running.track_id,
            clock=running.clock,
        )
    assert raised.value.payload.code == "artifact_unreadable"
