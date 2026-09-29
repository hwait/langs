"""Learner and track setup, and the level-label rules that guard it.

The rule worth testing hardest is the one that must never bend: a level label belongs to
one framework, and nothing in the system translates between frameworks.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from linguawiki.db.connection import open_reader
from linguawiki.errors import LinguaWikiError
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service
from tests.conftest import FIXTURE_PACKS, PILOT_PACK, PolishWorkspace, SyntheticWorkspace


def test_a_learner_records_only_what_changes_behaviour(
    installed_pilot: SyntheticWorkspace,
) -> None:
    user = learner_service.create_user(
        installed_pilot.paths,
        display_name="Синтетический Учащийся",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        support_languages=["en", "ru"],
        clock=installed_pilot.clock,
    )

    assert user.timezone == "Europe/Warsaw"
    assert user.native_languages == ("ru",)
    # A native language is not repeated as a support language.
    assert user.support_languages == ("en",)
    assert user.status == "active"


def test_a_learner_needs_a_native_language_and_a_real_timezone(
    installed_pilot: SyntheticWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as missing:
        learner_service.create_user(
            installed_pilot.paths,
            display_name="A",
            timezone="Europe/Warsaw",
            native_languages=[],
            clock=installed_pilot.clock,
        )
    with pytest.raises(ValueError):
        learner_service.create_user(
            installed_pilot.paths,
            display_name="B",
            timezone="Mars/Olympus_Mons",
            native_languages=["ru"],
            clock=installed_pilot.clock,
        )
    with pytest.raises(LinguaWikiError) as malformed:
        learner_service.create_user(
            installed_pilot.paths,
            display_name="C",
            timezone="UTC",
            native_languages=["russian!"],
            clock=installed_pilot.clock,
        )

    assert missing.value.payload.code == "invalid_arguments"
    assert malformed.value.payload.code == "invalid_language_tag"


def test_a_duplicate_display_name_is_refused(installed_pilot: SyntheticWorkspace) -> None:
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Anna",
        timezone="UTC",
        native_languages=["ru"],
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_user(
            installed_pilot.paths,
            display_name="Anna",
            timezone="UTC",
            native_languages=["en"],
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "user_exists"


def test_a_track_binds_one_learner_to_one_language_inside_one_framework(
    polish_workspace: PolishWorkspace,
) -> None:
    track = learner_service.show_track(polish_workspace.paths, clock=polish_workspace.clock)

    assert track.target_language == "pl"
    assert track.script == "Latn"
    assert track.proficiency_framework == "cefr"
    assert track.framework_levels == ("A1", "A2", "B1", "B2", "C1", "C2")
    assert track.declared_level == "A2"
    assert track.current_level is None, "a level the learner holds comes from evidence"
    assert track.is_primary is True
    assert track.pack_key == "pl-pilot"
    assert track.pack_maturity == "pilot"
    assert track.preferences["correction_mode"] == "accuracy"
    assert track.preferences["interests"] == ["podróże", "praca i biuro"]


def test_a_level_from_another_framework_is_refused_by_name_never_translated(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """`A2` and `HSK2` are not interchangeable, and no code may relate them."""

    pack_service.install(
        installed_pilot.paths, FIXTURE_PACKS / "inflected", clock=installed_pilot.clock
    )
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["ru"],
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            installed_pilot.paths,
            target_language="pl",
            framework="cefr",
            pack_key="pl-pilot",
            declared_level="L2",
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "level_not_in_framework"
    assert "fixture-bands-v1" in failure.value.payload.message
    assert "not of cefr" in failure.value.payload.message


def test_a_level_no_installed_framework_has_is_refused_plainly(
    installed_pilot: SyntheticWorkspace,
) -> None:
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["ru"],
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            installed_pilot.paths,
            target_language="pl",
            framework="cefr",
            declared_level="Z9",
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "level_not_in_framework"
    assert failure.value.payload.message == "cefr has no level Z9"


def test_a_framework_the_pack_does_not_declare_is_refused(
    installed_pilot: SyntheticWorkspace,
) -> None:
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["ru"],
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            installed_pilot.paths,
            target_language="pl",
            framework="hsk",
            declared_level="A2",
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "framework_not_supported"


def test_a_language_no_installed_pack_serves_is_refused(
    installed_pilot: SyntheticWorkspace,
) -> None:
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["ru"],
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            installed_pilot.paths,
            target_language="cs",
            framework="cefr",
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "pack_language_mismatch"


def test_a_script_the_pack_does_not_declare_is_refused(
    installed_pilot: SyntheticWorkspace,
) -> None:
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["ru"],
        clock=installed_pilot.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            installed_pilot.paths,
            target_language="pl",
            framework="cefr",
            script="Cyrl",
            clock=installed_pilot.clock,
        )

    assert failure.value.payload.code == "pack_script_unsupported"


def test_a_support_language_the_pack_lacks_is_a_warning_not_a_refusal(
    installed_pilot: SyntheticWorkspace,
) -> None:
    """The learner may still study; they are told which explanations will fall back."""

    learner_service.create_user(
        installed_pilot.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["de"],
        support_languages=["fr"],
        clock=installed_pilot.clock,
    )

    track = learner_service.create_track(
        installed_pilot.paths,
        target_language="pl",
        framework="cefr",
        declared_level="A2",
        clock=installed_pilot.clock,
    )

    assert any("support language" in warning for warning in track.warnings)
    assert track.declared_level == "A2"


def test_a_second_track_for_the_same_language_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            polish_workspace.paths,
            target_language="pl",
            framework="cefr",
            declared_level="A2",
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "track_exists"


def test_an_update_never_sets_a_level_the_learner_merely_claims(
    polish_workspace: PolishWorkspace,
) -> None:
    updated = learner_service.update_track(
        polish_workspace.paths,
        goal="Rozmawiać o kolei",
        target_level="B2",
        clock=polish_workspace.clock,
    )

    assert updated.goal == "Rozmawiać o kolei"
    assert updated.target_level == "B2"
    assert updated.current_level is None


def test_an_update_refuses_a_level_from_another_framework(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        learner_service.update_track(
            polish_workspace.paths, declared_level="HSK2", clock=polish_workspace.clock
        )

    assert failure.value.payload.code == "level_not_in_framework"


def test_preferences_are_typed_and_a_stray_key_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(ValidationError):
        learner_service.TrackPreferences.model_validate({"favourite_colour": "blue"})

    updated = learner_service.update_track(
        polish_workspace.paths,
        preferences=learner_service.TrackPreferences(
            session_minutes=90, avoided_topics=("polityka",)
        ),
        clock=polish_workspace.clock,
    )

    assert updated.preferences["session_minutes"] == 90
    assert updated.preferences["avoided_topics"] == ["polityka"]
    # An update leaves untouched preferences alone.
    assert updated.preferences["correction_mode"] == "accuracy"


def test_audio_retention_cannot_be_consented_to_while_transcripts_are_refused() -> None:
    with pytest.raises(ValidationError):
        learner_service.TrackPreferences(
            transcript_retention_consent=False, audio_retention_consent=True
        )


def test_an_unknown_correction_mode_is_refused() -> None:
    with pytest.raises(ValidationError):
        learner_service.TrackPreferences(correction_mode="telepathic")


def test_archiving_the_primary_track_moves_the_flag_to_a_successor(
    polish_workspace: PolishWorkspace,
) -> None:
    """`db check` requires at most one primary track, so the flag must move with it."""

    pack_service.install(
        polish_workspace.paths, FIXTURE_PACKS / "inflected", clock=polish_workspace.clock
    )
    second = learner_service.create_track(
        polish_workspace.paths,
        target_language="qix",
        framework="fixture-bands-v1",
        pack_key="fixture-inflected",
        declared_level="L2",
        clock=polish_workspace.clock,
    )
    assert second.is_primary is False

    archived = learner_service.set_track_status(
        polish_workspace.paths,
        status="archived",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    successor = learner_service.show_track(
        polish_workspace.paths, track=second.track_id, clock=polish_workspace.clock
    )

    assert archived.status == "archived"
    assert archived.is_primary is False
    assert successor.is_primary is True


def test_resolving_a_track_needs_naming_once_two_are_active(
    polish_workspace: PolishWorkspace,
) -> None:
    pack_service.install(
        polish_workspace.paths, FIXTURE_PACKS / "inflected", clock=polish_workspace.clock
    )
    learner_service.create_track(
        polish_workspace.paths,
        target_language="qix",
        framework="fixture-bands-v1",
        pack_key="fixture-inflected",
        declared_level="L2",
        clock=polish_workspace.clock,
    )

    with open_reader(polish_workspace.paths, clock=polish_workspace.clock) as database:
        with pytest.raises(LinguaWikiError) as failure:
            learner_service.resolve_track(database, None)
        assert (
            learner_service.resolve_track(database, polish_workspace.track_id)
            == polish_workspace.track_id
        )

    assert failure.value.payload.code == "track_selection_required"


def test_a_learner_and_track_can_be_listed_and_shown(
    polish_workspace: PolishWorkspace,
) -> None:
    users = learner_service.list_users(polish_workspace.paths, clock=polish_workspace.clock)
    tracks = learner_service.list_tracks(polish_workspace.paths, clock=polish_workspace.clock)
    shown = learner_service.show_user(polish_workspace.paths, clock=polish_workspace.clock)

    assert [user.user_id for user in users] == [polish_workspace.user_id]
    assert [track.track_id for track in tracks] == [polish_workspace.track_id]
    assert shown.tracks == (polish_workspace.track_id,)


def test_an_unknown_user_or_track_is_named(polish_workspace: PolishWorkspace) -> None:
    with pytest.raises(LinguaWikiError) as user:
        learner_service.show_user(
            polish_workspace.paths,
            user="usr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clock=polish_workspace.clock,
        )
    with pytest.raises(LinguaWikiError) as track:
        learner_service.show_track(
            polish_workspace.paths,
            track="trk_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            clock=polish_workspace.clock,
        )

    assert user.value.payload.code == "user_not_found"
    assert track.value.payload.code == "track_not_found"


def test_updating_a_learner_replaces_language_roles_as_a_whole_set(
    polish_workspace: PolishWorkspace,
) -> None:
    updated = learner_service.update_user(
        polish_workspace.paths,
        native_languages=["ru", "uk"],
        support_languages=["en"],
        clock=polish_workspace.clock,
    )

    assert updated.native_languages == ("ru", "uk")
    assert updated.support_languages == ("en",)

    with pytest.raises(LinguaWikiError):
        learner_service.update_user(
            polish_workspace.paths, native_languages=[], clock=polish_workspace.clock
        )


def test_creating_a_track_before_a_pack_is_installed_is_refused(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """A pack decides which frameworks and levels exist, so it comes first."""

    learner_service.create_user(
        synthetic_workspace.paths,
        display_name="Learner",
        timezone="UTC",
        native_languages=["ru"],
        clock=synthetic_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        learner_service.create_track(
            synthetic_workspace.paths,
            target_language="pl",
            framework="cefr",
            clock=synthetic_workspace.clock,
        )

    assert failure.value.payload.code == "pack_selection_required"
    assert PILOT_PACK.name not in failure.value.payload.message
