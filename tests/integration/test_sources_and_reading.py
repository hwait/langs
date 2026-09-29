"""Cataloguing material, working through it, and what the workspace refuses.

The three refusals here are the stage's reason for existing. Rights bound what may be
stored from a work; unaided comprehension cannot be recorded after help was given; and
extensive work is not a harvest. Each is tested at the command *and* asserted over the
data by `db check`, because a restored file or a hand-repaired database can present a
state no command would produce.
"""

from __future__ import annotations

from typing import Any

import pytest

from linguawiki.db.connection import open_writer
from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import sources as source_service
from tests.conftest import PolishWorkspace


@pytest.fixture
def podcast(polish_workspace: PolishWorkspace) -> Any:
    return source_service.add(
        polish_workspace.paths,
        kind="podcast",
        title="Polski Daily",
        creator="Paulina",
        rights="metadata-only",
        has_audio=True,
        has_transcript=True,
        units=[{"label": "Odcinek 1"}, {"label": "Odcinek 2"}],
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )


def test_a_source_belongs_to_a_track_rather_than_to_the_workspace(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    assert podcast.track_id == polish_workspace.track_id
    listing = source_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert [entry.source_id for entry in listing.sources] == [podcast.source_id]


def test_a_metadata_only_source_cannot_carry_text_from_the_work(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        source_service.add(
            polish_workspace.paths,
            kind="book",
            title="Lalka",
            rights="metadata-only",
            units=[{"label": "Rozdział 1", "excerpt": "Ależ to był rok!"}],
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    assert "metadata-only" in str(failure.value)


def test_an_excerpt_longer_than_the_rights_permit_is_refused_before_it_is_stored(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError):
        source_service.add(
            polish_workspace.paths,
            kind="article",
            title="Długi artykuł",
            rights="short-excerpt",
            units=[{"label": "całość", "excerpt": "x" * 400}],
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )


def test_db_check_finds_an_excerpt_the_rights_do_not_permit(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    """The command refuses it; this is the same fact found in a database that has it."""

    with (
        open_writer(polish_workspace.paths, command="test", clock=polish_workspace.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE source_units SET excerpt = ? WHERE unit_id = ?",
            ["Ależ to był rok, powiadam państwu!", podcast.units[0].unit_id],
        )
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert not report.ok
    assert "source_metadata_only" in {check.name for check in report.failures}


def test_unaided_comprehension_cannot_be_recorded_after_help_was_given(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    source_service.record_comprehension(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 1",
        aid="unaided",
        band="gist",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    aided = source_service.record_comprehension(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 1",
        aid="subtitled",
        band="full",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert aided.progress.unaided_band == "gist"
    assert aided.progress.aided_band == "full"

    with pytest.raises(LinguaWikiError) as failure:
        source_service.record_comprehension(
            polish_workspace.paths,
            source=podcast.source_id,
            unit="Odcinek 1",
            aid="unaided",
            band="full",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    assert failure.value.payload.code == "comprehension_aid_regressed"


def test_db_check_finds_help_that_was_withdrawn_after_the_fact(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    for aid, band in (("unaided", "gist"), ("subtitled", "full")):
        source_service.record_comprehension(
            polish_workspace.paths,
            source=podcast.source_id,
            unit="Odcinek 1",
            aid=aid,
            band=band,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    with (
        open_writer(polish_workspace.paths, command="test", clock=polish_workspace.clock) as db,
        db.transaction() as transaction,
    ):
        # The order reversed: the aided reading now claims to have come first.
        transaction.execute(
            "UPDATE comprehension_observations SET aid = "
            "CASE WHEN aid = 'unaided' THEN 'subtitled' ELSE 'unaided' END"
        )
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert not report.ok
    assert "comprehension_order" in {check.name for check in report.failures}


def test_an_aided_first_encounter_says_the_unaided_measurement_is_gone(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    report = source_service.record_comprehension(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 2",
        aid="translated",
        band="most",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert report.progress.unaided_band is None
    assert any("no unaided measurement" in warning for warning in report.warnings)


def test_a_reread_is_not_more_of_the_source(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    first = source_service.complete_unit(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 1",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert first.progress is not None
    assert first.progress.coverage == pytest.approx(0.5)

    again = source_service.complete_unit(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 1",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert again.progress is not None
    assert again.progress.coverage == pytest.approx(0.5)
    assert any("already complete" in warning for warning in again.warnings)


def test_db_check_finds_progress_that_disagrees_with_the_units_completed(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    source_service.complete_unit(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 1",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    with (
        open_writer(polish_workspace.paths, command="test", clock=polish_workspace.clock) as db,
        db.transaction() as transaction,
    ):
        transaction.execute(
            "UPDATE track_source_progress SET completed_units = 2 WHERE source_id = ?",
            [podcast.source_id],
        )
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert not report.ok
    assert "source_progress_count" in {check.name for check in report.failures}


def test_extensive_work_records_breadth_rather_than_a_harvest(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    source_service.position(
        polish_workspace.paths,
        source=podcast.source_id,
        mode="extensive",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    for index in range(8):
        knowledge_service.upsert(
            polish_workspace.paths,
            stable_key=f"note.podcast.{index}",
            kind="concept",
            title=f"Notatka {index}",
            body="Learner note.",
            clock=polish_workspace.clock,
        )
    with pytest.raises(LinguaWikiError) as failure:
        for index in range(8):
            source_service.link_item(
                polish_workspace.paths,
                source=podcast.source_id,
                unit="Odcinek 1",
                target=f"note.podcast.{index}",
                relation="extracted-from",
                track=polish_workspace.track_id,
                clock=polish_workspace.clock,
            )
    assert failure.value.payload.code == "extensive_extraction_excessive"


def test_the_catalogue_stays_consistent_under_db_check(
    polish_workspace: PolishWorkspace, podcast: Any
) -> None:
    source_service.record_comprehension(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 1",
        aid="unaided",
        band="most",
        minutes=12,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    source_service.complete_unit(
        polish_workspace.paths,
        source=podcast.source_id,
        unit="Odcinek 1",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert report.ok, [check.name for check in report.failures]
