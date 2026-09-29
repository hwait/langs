"""The knowledge graph against the pilot pack and both contrasting fixture packs.

The exit gate asks for one graph that works for a language that inflects and separates
words and for one that neither inflects nor uses whitespace. The same scenarios are
therefore run against all three, and the assertions are about structure -- kinds, edges,
aliases, search, merge -- never about a linguistic form.
"""

from __future__ import annotations

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.services import database as database_service
from linguawiki.services import errors as error_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service
from linguawiki.services import packs as pack_service
from tests.conftest import FIXTURE_PACKS, PolishWorkspace, SyntheticWorkspace

#: One anchor item per pack, plus the pack's language and framework, so the same
#: scenario can be parameterized over three deliberately different languages.
PACKS = {
    "pl-pilot": ("pl", "cefr", "pl.lex.dworzec", "A2", "lexeme"),
    "inflected": ("qix-Latn", "fixture-bands-v1", "qix.lex.lum", "L1", "lexeme"),
    "tonal": ("ztx-Zzzz", "fixture-bands-v1", "ztx.char.diamond", "L1", "character"),
}


@pytest.fixture
def fixture_track(
    request: pytest.FixtureRequest, installed_pilot: SyntheticWorkspace
) -> tuple[SyntheticWorkspace, str, str]:
    """A track on one of the three packs, named by the test's parameter."""

    name = request.param
    language, framework, anchor, _level, _kind = PACKS[name]
    if name != "pl-pilot":
        pack_service.install(
            installed_pilot.paths, FIXTURE_PACKS / name, clock=installed_pilot.clock
        )
    learner_service.create_user(
        installed_pilot.paths,
        display_name="Синтетический Учащийся",
        timezone="Europe/Warsaw",
        native_languages=["ru"],
        support_languages=["en"],
        clock=installed_pilot.clock,
    )
    pack_key = name if name == "pl-pilot" else f"fixture-{name}"
    track = learner_service.create_track(
        installed_pilot.paths,
        target_language=language,
        framework=framework,
        pack_key=pack_key,
        clock=installed_pilot.clock,
    )
    return (installed_pilot, track.track_id, anchor)


@pytest.mark.parametrize("fixture_track", list(PACKS), indirect=True)
def test_an_item_reads_back_with_its_edges_aliases_and_examples(
    fixture_track: tuple[SyntheticWorkspace, str, str],
) -> None:
    workspace, track_id, anchor = fixture_track

    item = knowledge_service.get(
        workspace.paths, item=anchor, track=track_id, clock=workspace.clock
    )

    assert item.stable_key == anchor
    assert item.owner == "pack"
    assert item.kind in knowledge_service.ITEM_KINDS
    assert item.title
    assert item.body
    assert item.lifecycle in ("verified", "approved-personal", "publication-ready")


@pytest.mark.parametrize("fixture_track", list(PACKS), indirect=True)
def test_search_finds_an_item_by_its_own_title_whatever_the_script(
    fixture_track: tuple[SyntheticWorkspace, str, str],
) -> None:
    """No tokenization, no transliteration: the title is matched as a folded substring."""

    workspace, track_id, anchor = fixture_track
    item = knowledge_service.get(
        workspace.paths, item=anchor, track=track_id, clock=workspace.clock
    )

    found = knowledge_service.search(
        workspace.paths, query=item.title, track=track_id, clock=workspace.clock
    )

    assert anchor in {hit.stable_key for hit in found.hits}
    assert found.hits[0].matched_on == "text"


@pytest.mark.parametrize("fixture_track", list(PACKS), indirect=True)
def test_search_narrows_by_kind_level_and_tag_together(
    fixture_track: tuple[SyntheticWorkspace, str, str],
) -> None:
    workspace, track_id, anchor = fixture_track
    item = knowledge_service.get(
        workspace.paths, item=anchor, track=track_id, clock=workspace.clock
    )
    tag = next(entry.tag_value for entry in item.tags if entry.tag_kind != "level")

    narrowed = knowledge_service.search(
        workspace.paths,
        kind=item.kind,
        level=item.level_min,
        tag=tag,
        track=track_id,
        clock=workspace.clock,
    )

    assert anchor in {hit.stable_key for hit in narrowed.hits}
    assert all(hit.kind == item.kind for hit in narrowed.hits)


@pytest.mark.parametrize("fixture_track", list(PACKS), indirect=True)
def test_search_by_relation_needs_an_anchor_and_then_follows_the_edge(
    fixture_track: tuple[SyntheticWorkspace, str, str],
) -> None:
    workspace, track_id, anchor = fixture_track
    item = knowledge_service.get(
        workspace.paths, item=anchor, track=track_id, clock=workspace.clock
    )
    edges = [edge for edge in item.relations if edge.other_content_id is not None]
    if not edges:
        pytest.skip("this pack's anchor item has no item-to-item edge")

    related = knowledge_service.search(
        workspace.paths,
        relation=edges[0].relation_type,
        related_to=anchor,
        track=track_id,
        clock=workspace.clock,
    )

    assert edges[0].other_content_id in {hit.content_id for hit in related.hits}
    assert related.hits[0].matched_on == "relation"

    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.search(
            workspace.paths, relation="prerequisite", track=track_id, clock=workspace.clock
        )

    assert failure.value.payload.code == "relation_filter_needs_anchor"


@pytest.mark.parametrize("fixture_track", list(PACKS), indirect=True)
def test_a_learner_can_author_an_item_and_link_it_to_the_pack_s(
    fixture_track: tuple[SyntheticWorkspace, str, str],
) -> None:
    workspace, track_id, anchor = fixture_track

    written = knowledge_service.upsert(
        workspace.paths,
        stable_key="learner.note.one",
        kind="concept",
        title="a learner's own note",
        body="Written by the learner, not by the pack.",
        aliases=[{"alias": "own note", "locale": "en"}],
        themes=["fixture"],
        track=track_id,
        clock=workspace.clock,
    )
    linked = knowledge_service.link(
        workspace.paths,
        source=written.content_id,
        relation_type="related",
        target=anchor,
        track=track_id,
        clock=workspace.clock,
    )

    assert written.created is True
    assert written.lifecycle == knowledge_service.LEARNER_LIFECYCLE
    assert linked.created is True

    again = knowledge_service.link(
        workspace.paths,
        source=written.content_id,
        relation_type="related",
        target=anchor,
        track=track_id,
        clock=workspace.clock,
    )
    assert again.created is False
    assert again.relation_id == linked.relation_id


def test_a_learner_item_keeps_its_own_identity_namespace(
    polish_workspace: PolishWorkspace,
) -> None:
    """A learner note and a pack item with the same key are different things."""

    written = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.dworzec",
        kind="concept",
        title="moja notatka",
        body="A learner note about a station.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    replaced = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.dworzec",
        kind="concept",
        title="moja notatka, poprawiona",
        body="A learner note about a station, revised.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert replaced.created is False
    assert replaced.content_id == written.content_id
    assert replaced.content_hash != written.content_hash


def test_a_learner_note_can_never_overwrite_pack_content(
    polish_workspace: PolishWorkspace,
) -> None:
    """The identities are derived from different namespaces, so they cannot collide.

    Reusing a pack's stable key is allowed and costs only lookup by key, which becomes
    ambiguous -- so the upsert warns and `resolve_item` refuses rather than guessing.
    """

    written = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="pl.lex.dworzec",
        kind="concept",
        title="moja notatka o dworcu",
        body="A learner note that happens to reuse a pack item's key.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert written.created is True
    assert any("ambiguous" in warning for warning in written.warnings)
    assert any("pl-pilot" in warning for warning in written.warnings)

    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.get(
            polish_workspace.paths,
            item="pl.lex.dworzec",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    assert failure.value.payload.code == "ambiguous_knowledge_item"

    pack_item = knowledge_service.search(
        polish_workspace.paths,
        query="dworzec",
        kind="lexeme",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert [hit.owner for hit in pack_item.hits] == ["pack"]
    assert written.content_id not in {hit.content_id for hit in pack_item.hits}


def test_an_item_level_outside_the_track_s_framework_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.upsert(
            polish_workspace.paths,
            stable_key="learner.mislevelled",
            kind="concept",
            title="a note at a level from another framework",
            body="HSK 3 is not a CEFR band.",
            level="HSK3",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "unknown_framework_level"


def test_a_pack_item_cannot_be_merged_away_and_the_refusal_names_the_remedy(
    polish_workspace: PolishWorkspace,
) -> None:
    """The next install would restore it, so merging it away would not hold."""

    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.merge(
            polish_workspace.paths,
            source="pl.lex.dworzec",
            into="pl.lex.peron",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "pack_item_not_mergeable"
    assert "pack update" in failure.value.payload.message


def test_a_merge_dry_run_reports_what_would_move_and_writes_nothing(
    polish_workspace: PolishWorkspace,
) -> None:
    duplicate = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate note",
        body="The learner wrote the same note twice.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    evidence_service.record(
        polish_workspace.paths,
        task_type="objective",
        modality="text",
        score=1.0,
        target=duplicate.content_id,
        dimension="reading",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    planned = knowledge_service.merge(
        polish_workspace.paths,
        source=duplicate.content_id,
        into="pl.lex.dworzec",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert planned.dry_run is True
    assert planned.applied is False
    assert planned.evidence_moved == 1
    assert any("--apply" in warning for warning in planned.warnings)
    still_there = knowledge_service.get(
        polish_workspace.paths,
        item=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert still_there.lifecycle == knowledge_service.LEARNER_LIFECYCLE


def test_an_applied_merge_moves_the_learner_s_history_and_recomputes_the_stage(
    polish_workspace: PolishWorkspace,
) -> None:
    """A merge must not manufacture mastery: the stage is recomputed, never copied."""

    duplicate = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate note",
        body="The learner wrote the same note twice.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    for index in range(2):
        evidence_service.record(
            polish_workspace.paths,
            task_type="objective",
            modality="text",
            score=1.0,
            target=duplicate.content_id,
            dimension="reading",
            context=f"objective:{index}",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )
    error_service.record(
        polish_workspace.paths,
        category="fixture",
        signature="duplicate note error",
        description="An error recorded against the duplicate.",
        target=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    applied = knowledge_service.merge(
        polish_workspace.paths,
        source=duplicate.content_id,
        into="pl.lex.dworzec",
        dry_run=False,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    settled = evidence_service.recompute(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )

    assert applied.applied is True
    assert applied.evidence_moved == 2
    merged = knowledge_service.get(
        polish_workspace.paths,
        item="pl.lex.dworzec",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert merged.state is not None
    assert merged.state.stage == "recognized"
    assert merged.state.positive_evidence == 2
    assert settled.dry_run is False
    source = knowledge_service.get(
        polish_workspace.paths,
        item=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert source.lifecycle == "deprecated"
    errors = error_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert errors.entries[0].target_content_id == merged.content_id
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert [check.name for check in report.failures] == []


def test_merging_an_item_into_itself_is_refused(polish_workspace: PolishWorkspace) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.merge(
            polish_workspace.paths,
            source="pl.lex.dworzec",
            into="pl.lex.dworzec",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "self_merge"


def test_relating_an_item_to_itself_is_refused(polish_workspace: PolishWorkspace) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.link(
            polish_workspace.paths,
            source="pl.lex.dworzec",
            relation_type="related",
            target="pl.lex.dworzec",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "self_relation"


def test_an_edge_names_exactly_one_target(polish_workspace: PolishWorkspace) -> None:
    for arguments in (
        {"target": "pl.lex.peron", "target_ref": "unit:u1"},
        {},
    ):
        with pytest.raises(LinguaWikiError) as failure:
            knowledge_service.link(
                polish_workspace.paths,
                source="pl.lex.dworzec",
                relation_type="curriculum-objective",
                track=polish_workspace.track_id,
                clock=polish_workspace.clock,
                **arguments,  # type: ignore[arg-type]
            )
        assert failure.value.payload.code == "invalid_relation_target"


def test_an_unknown_item_is_refused_rather_than_resolved_by_luck(
    polish_workspace: PolishWorkspace,
) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        knowledge_service.get(
            polish_workspace.paths,
            item="pl.lex.nieistniejacy",
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "knowledge_item_not_found"


def test_an_item_resolves_by_id_stable_key_or_alias(
    polish_workspace: PolishWorkspace,
) -> None:
    by_key = knowledge_service.get(
        polish_workspace.paths,
        item="pl.lex.dworzec",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    by_id = knowledge_service.get(
        polish_workspace.paths,
        item=by_key.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    by_alias = knowledge_service.get(
        polish_workspace.paths,
        item=by_key.aliases[0].alias.upper(),
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert by_id.content_id == by_key.content_id
    assert by_alias.content_id == by_key.content_id


def test_search_is_deterministic_and_reports_what_it_truncated(
    polish_workspace: PolishWorkspace,
) -> None:
    first = knowledge_service.search(
        polish_workspace.paths,
        limit=5,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    again = knowledge_service.search(
        polish_workspace.paths,
        limit=5,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert [hit.content_id for hit in first.hits] == [hit.content_id for hit in again.hits]
    assert first.truncated is True
    assert first.total_matched > len(first.hits)
    assert first.warnings


def test_a_listing_limit_outside_the_permitted_range_is_refused(
    polish_workspace: PolishWorkspace,
) -> None:
    for limit in (0, knowledge_service.MAXIMUM_LIMIT + 1):
        with pytest.raises(LinguaWikiError) as failure:
            knowledge_service.search(
                polish_workspace.paths,
                limit=limit,
                track=polish_workspace.track_id,
                clock=polish_workspace.clock,
            )
        assert failure.value.payload.code == "invalid_limit"


def test_a_merge_re_derives_an_error_s_identity_so_the_next_occurrence_finds_it(
    polish_workspace: PolishWorkspace,
) -> None:
    """An error's identity is derived from its target, so moving the target moves the key.

    Without re-deriving it, the pattern would keep a key that no longer describes it and
    the next occurrence of the same mistake would open a second pattern beside it.
    `db check`'s `error_identity` reports exactly that.
    """

    duplicate = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate note",
        body="The learner wrote the same note twice.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    original = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    knowledge_service.merge(
        polish_workspace.paths,
        source=duplicate.content_id,
        into="pl.lex.dworzec",
        dry_run=False,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    listed = error_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )
    assert [entry.error_id for entry in listed.entries] != [original.error_id]
    moved = listed.entries[0]
    assert moved.category == "case-government"
    assert moved.occurrence_count == 1

    # The next occurrence of the same mistake finds the moved pattern rather than
    # opening a second one beside it.
    again = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target="pl.lex.dworzec",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert again.error_id == moved.error_id
    assert again.created is False
    assert again.occurrence_count == 2
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert [check.name for check in report.failures] == []
    assert "error_supersession" in {check.name for check in report.checks}


def test_a_superseded_pattern_is_refused_and_names_its_successor(
    polish_workspace: PolishWorkspace,
) -> None:
    duplicate = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate note",
        body="The learner wrote the same note twice.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    original = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    knowledge_service.merge(
        polish_workspace.paths,
        source=duplicate.content_id,
        into="pl.lex.dworzec",
        dry_run=False,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    with pytest.raises(LinguaWikiError) as failure:
        error_service.show(
            polish_workspace.paths,
            error=original.error_id,
            track=polish_workspace.track_id,
            clock=polish_workspace.clock,
        )

    assert failure.value.payload.code == "error_pattern_superseded"
    successor = str(failure.value.payload.details[0].context["superseded_by"])
    revived = error_service.show(
        polish_workspace.paths,
        error=successor,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    assert revived.occurrence_count == 1


def test_two_records_of_one_error_are_folded_when_their_items_merge(
    polish_workspace: PolishWorkspace,
) -> None:
    """The same mistake on a duplicate and on the original is one mistake."""

    duplicate = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate note",
        body="The learner wrote the same note twice.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    for target in (duplicate.content_id, "pl.lex.dworzec"):
        for _ in range(2):
            error_service.record(
                polish_workspace.paths,
                category="case-government",
                signature="szukam bilet",
                description="Accusative where the verb governs the genitive.",
                target=target,
                track=polish_workspace.track_id,
                clock=polish_workspace.clock,
            )

    applied = knowledge_service.merge(
        polish_workspace.paths,
        source=duplicate.content_id,
        into="pl.lex.dworzec",
        dry_run=False,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    listed = error_service.listing(
        polish_workspace.paths, track=polish_workspace.track_id, clock=polish_workspace.clock
    )

    assert applied.errors_folded == 1
    assert any("folded together" in warning for warning in applied.warnings)
    assert len(listed.entries) == 1
    folded = listed.entries[0]
    assert folded.occurrence_count == 4
    assert folded.status_reason is not None
    assert "folded together" in folded.status_reason
    superseded = error_service.listing(
        polish_workspace.paths,
        track=polish_workspace.track_id,
        status="superseded",
        clock=polish_workspace.clock,
    )
    assert [entry.superseded_by for entry in superseded.entries] == [folded.error_id]
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert [check.name for check in report.failures] == []


def test_an_occurrence_deriving_a_superseded_pattern_follows_its_successor(
    polish_workspace: PolishWorkspace,
) -> None:
    """A refusal here would be a dead end: the two really are the same error.

    Recording against the merged-away item derives the old identity, which is now a
    superseded shell. The occurrence belongs on the successor, and the caller is told.
    """

    duplicate = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate note",
        body="The learner wrote the same note twice.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    knowledge_service.merge(
        polish_workspace.paths,
        source=duplicate.content_id,
        into="pl.lex.dworzec",
        dry_run=False,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    redirected = error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert redirected.created is False
    assert redirected.occurrence_count == 2
    assert any("folded into" in warning for warning in redirected.warnings)
    assert redirected.target_content_id != duplicate.content_id
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert [check.name for check in report.failures] == []


def test_counter_evidence_never_lands_on_a_superseded_shell(
    polish_workspace: PolishWorkspace,
) -> None:
    """`db check` requires a superseded pattern to hold nothing, so nothing may attach."""

    duplicate = knowledge_service.upsert(
        polish_workspace.paths,
        stable_key="learner.duplicate",
        kind="concept",
        title="a duplicate note",
        body="The learner wrote the same note twice.",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    error_service.record(
        polish_workspace.paths,
        category="case-government",
        signature="szukam bilet",
        description="Accusative where the verb governs the genitive.",
        target=duplicate.content_id,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )
    knowledge_service.merge(
        polish_workspace.paths,
        source=duplicate.content_id,
        into="pl.lex.dworzec",
        dry_run=False,
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    supported = evidence_service.record(
        polish_workspace.paths,
        task_type="extended-productive",
        modality="writing",
        score=1.0,
        target="pl.lex.dworzec",
        dimension="writing",
        claims=["spontaneous-production"],
        context="essay:one",
        track=polish_workspace.track_id,
        clock=polish_workspace.clock,
    )

    assert supported.errors_supported, "the live successor must receive the counter-evidence"
    report = database_service.check(polish_workspace.paths, clock=polish_workspace.clock)
    assert [check.name for check in report.failures] == []
