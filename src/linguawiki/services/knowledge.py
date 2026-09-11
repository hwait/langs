"""The knowledge graph: read it, extend it, and merge duplicates out of it.

Three things here are load-bearing rather than incidental.

*Identity survives a pack upgrade.* A pack item's `content_id` is derived from
`(pack_key, kind, stable_key)`, so a reinstall re-derives the same identity and every
alias, tag, relation, error, and piece of evidence a learner accumulated against it stays
attached. Nothing in this module asserts an identity; it only derives or reads one.

*A learner's own item is a different kind of thing from a pack's.* Learner-authored items
are owned by the track, live at the `approved-personal` lifecycle at most, and can be
merged away. A pack item cannot be merged away at all: the next install would restore it,
so the refusal names the only remediation that actually works -- change the pack.

*Search is deterministic.* Every listing has a total order, so the same query returns the
same rows in the same sequence, which is what lets an agent page through one and a test
assert on it. Matching folds case and composes Unicode and does nothing else: no
tokenization, no transliteration, no script assumptions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Never

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.contracts import PackAlias, canonical_content_hash
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader, open_writer
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import ContentId, EventId, IdPrefix, derive_id
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.services import learners as learner_service
from linguawiki.text import normalize_alias

#: The content kind knowledge items are registered under in `content_records`.
KNOWLEDGE_KIND = "knowledge"
EXAMPLE_KIND = "example"
#: Learner-authored knowledge is the learner's own note. It can be trusted for their own
#: study and never claims to be reviewed reference material.
LEARNER_LIFECYCLE = "approved-personal"
#: The kinds `knowledge_items` admits, ordered as the schema lists them.
ITEM_KINDS: tuple[str, ...] = (
    "concept",
    "lexeme",
    "sense",
    "form",
    "construction",
    "grammar",
    "pronunciation",
    "character",
    "pragmatics",
    "culture",
    "skill_strategy",
)
RELATION_TYPES: tuple[str, ...] = (
    "prerequisite",
    "form-of",
    "sense-of",
    "contrast",
    "collocation",
    "government",
    "example-of",
    "related",
    "error-target",
    "curriculum-objective",
)
TAG_KINDS: tuple[str, ...] = ("theme", "feature", "level", "frequency", "user")
DEFAULT_LIMIT = 50
MAXIMUM_LIMIT = 500


class AliasRecord(ContractModel):
    alias: str
    normalized: str
    locale: str
    script: str | None = None


class TagRecord(ContractModel):
    tag_kind: str
    tag_value: str


class RelationRecord(ContractModel):
    relation_id: str
    relation_type: str
    direction: str
    other_content_id: str | None = None
    other_title: str | None = None
    target_ref: str | None = None


class ExampleRecord(ContractModel):
    content_id: str
    text: str
    translation: str | None = None
    gloss: str | None = None
    locale: str | None = None
    difficulty: str | None = None


class ItemStateRecord(ContractModel):
    """The learner's standing on one item, as the aggregation last computed it."""

    track_id: str
    stage: str
    stage_source: str
    confidence: float
    gated_stage: str | None = None
    evidence_ceiling: str | None = None
    aggregation_version: str | None = None
    positive_evidence: int = 0
    negative_evidence: int = 0
    first_encounter_at: str | None = None
    last_encounter_at: str | None = None
    computed_at: str | None = None
    explanation: tuple[str, ...] = ()


class KnowledgeItemReport(ContractModel):
    content_id: str
    stable_key: str
    owner: str
    pack_key: str | None = None
    track_id: str | None = None
    language: str
    kind: str
    title: str
    body: str
    summary: str | None = None
    level_min: str | None = None
    level_max: str | None = None
    lifecycle: str
    risk_tier: int
    content_hash: str
    aliases: tuple[AliasRecord, ...] = ()
    tags: tuple[TagRecord, ...] = ()
    relations: tuple[RelationRecord, ...] = ()
    examples: tuple[ExampleRecord, ...] = ()
    state: ItemStateRecord | None = None
    created_at: str
    updated_at: str
    warnings: tuple[str, ...] = ()


class SearchHit(ContractModel):
    content_id: str
    stable_key: str
    kind: str
    title: str
    level_min: str | None = None
    owner: str
    stage: str | None = None
    matched_on: str
    matched_value: str | None = None


class SearchReport(ContractModel):
    query: str | None = None
    kind: str | None = None
    tag: str | None = None
    level: str | None = None
    relation: str | None = None
    related_to: str | None = None
    track_id: str | None = None
    limit: int
    total_matched: int
    truncated: bool
    hits: tuple[SearchHit, ...] = ()
    warnings: tuple[str, ...] = ()


class UpsertReport(ContractModel):
    content_id: str
    stable_key: str
    track_id: str
    kind: str
    title: str
    created: bool
    aliases: tuple[str, ...] = ()
    tags: tuple[TagRecord, ...] = ()
    content_hash: str
    lifecycle: str
    warnings: tuple[str, ...] = ()


class LinkReport(ContractModel):
    relation_id: str
    source_content_id: str
    relation_type: str
    target_content_id: str | None = None
    target_ref: str | None = None
    created: bool
    warnings: tuple[str, ...] = ()


class MergeMove(ContractModel):
    """One category of row the merge moves, with how many and how many collided."""

    table: str
    moved: int
    already_present: int = 0


class MergeReport(ContractModel):
    source_content_id: str
    target_content_id: str
    dry_run: bool
    applied: bool
    source_stage: str | None = None
    target_stage: str | None = None
    moves: tuple[MergeMove, ...] = ()
    #: Evidence rows the merge repoints. The merged item's stage is recomputed from them
    #: rather than copied, so a merge cannot manufacture mastery.
    evidence_moved: int = 0
    #: Error patterns whose derived identity was re-computed because their target moved,
    #: and those that turned out to be the same error already recorded on the target.
    errors_remapped: int = 0
    errors_folded: int = 0
    stage_recomputation_required: bool = True
    warnings: tuple[str, ...] = ()


def _assert_known(value: str, *, vocabulary: Sequence[str], field: str, code: str) -> str:
    if value not in vocabulary:
        raise LinguaWikiError(
            code,
            f"{value} is not a valid {field}; expected one of {list(vocabulary)}",
            details=(ErrorDetail(field=field, reason=f"unknown {field}"),),
        )
    return value


def _bounded_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAXIMUM_LIMIT:
        raise LinguaWikiError(
            "invalid_limit",
            f"a listing limit is between 1 and {MAXIMUM_LIMIT}",
            details=(ErrorDetail(field="limit", reason=str(limit)),),
        )
    return limit


#: What a track may refer to: its own pack's content, its own learner-authored content,
#: or content owned by neither. Everything else belongs to another learner's programme
#: or to a pack this track is not taught from.
IN_SCOPE = (
    "(record.pack_id = ? OR record.track_id = ? "
    "OR (record.pack_id IS NULL AND record.track_id IS NULL))"
)


def track_scope(database: Database, track_id: str | None) -> tuple[Any, ...]:
    """The parameters that bind a lookup to one track's own material.

    Returned as a pair so a caller cannot accidentally use the predicate without them.
    A track names the pack it is taught from, and every level label, dimension, and item
    identity is relative to that pack -- so a reference that reaches outside it is not a
    reference to something the learner is studying.
    """

    if track_id is None:
        return ()
    record = learner_service.track_context(database, track_id)
    return (record.pack_id, track_id)


def resolve_item(database: Database, reference: str, *, track_id: str | None = None) -> str:
    """Resolve an item reference: a content ID, a stable key, or an exact alias.

    A stable key is what a pack author and a learner both actually have; a content ID is
    what the database uses. Accepting either is what keeps the derived-identity rule from
    leaking into every command line. Ambiguity is refused rather than resolved by luck.

    Every form is scoped to the track when one is given -- including a content ID, which
    is the form that looks least like a guess and so was the one that let a track record
    evidence against another pack's item. A workspace-wide lookup happens only when no
    track is in play at all, which is a maintainer reading the graph rather than a
    learner working in it.
    """

    scope = track_scope(database, track_id)
    if reference.startswith(f"{IdPrefix.CONTENT}_"):
        found = database.scalar(
            "SELECT record.content_id FROM content_records record "
            "JOIN knowledge_items item ON item.content_id = record.content_id "
            f"WHERE record.content_id = ? AND {IN_SCOPE if scope else 'TRUE'}",
            [reference, *scope],
        )
        if found is None:
            _refuse_unreachable(database, reference, track_id=track_id, field="item")
        return str(found)
    by_key = [
        str(content_id)
        for (content_id,) in database.query(
            "SELECT record.content_id FROM content_records record "
            "JOIN knowledge_items item ON item.content_id = record.content_id "
            f"WHERE record.stable_key = ? AND {IN_SCOPE if scope else 'TRUE'} "
            "ORDER BY record.content_id",
            [reference, *scope],
        )
    ]
    if len(by_key) == 1:
        return by_key[0]
    if len(by_key) > 1:
        raise LinguaWikiError(
            "ambiguous_knowledge_item",
            f"{reference} is the stable key of {len(by_key)} items; name the content ID",
            details=(
                ErrorDetail(
                    field="item", reason="ambiguous stable key", context={"matches": by_key}
                ),
            ),
        )
    by_alias = [
        str(content_id)
        for (content_id,) in database.query(
            "SELECT DISTINCT alias.content_id FROM knowledge_aliases alias "
            "JOIN content_records record ON record.content_id = alias.content_id "
            f"WHERE alias.normalized = ? AND {IN_SCOPE if scope else 'TRUE'} "
            "ORDER BY alias.content_id",
            [normalize_alias(reference), *scope],
        )
    ]
    if len(by_alias) == 1:
        return by_alias[0]
    if len(by_alias) > 1:
        raise LinguaWikiError(
            "ambiguous_knowledge_item",
            f"{reference} is an alias of {len(by_alias)} items; name the content ID",
            details=(
                ErrorDetail(field="item", reason="ambiguous alias", context={"matches": by_alias}),
            ),
        )
    _refuse_unreachable(database, reference, track_id=track_id, field="item")


def _refuse_unreachable(
    database: Database, reference: str, *, track_id: str | None, field: str
) -> Never:
    """Refuse a reference, saying whether it is unknown or simply not this track's.

    The two are different problems with different remedies, and reporting the second as
    "not found" would send a caller looking for a typo in an identifier that is perfectly
    valid somewhere else.
    """

    owner = database.one(
        "SELECT pack.pack_key, record.track_id FROM content_records record "
        "LEFT JOIN language_packs pack ON pack.pack_id = record.pack_id "
        "WHERE record.content_id = ? OR record.stable_key = ?",
        [reference, reference],
    )
    if owner is not None and track_id is not None:
        held_by = f"pack {owner[0]}" if owner[0] is not None else f"another track ({owner[1]})"
        raise LinguaWikiError(
            "knowledge_item_out_of_scope",
            f"{reference} belongs to {held_by}, which this track is not taught from; a "
            "track's items come from its own pack or its own notes",
            details=(
                ErrorDetail(
                    field=field,
                    reason="outside the track's own material",
                    context={"held_by": held_by},
                ),
            ),
        )
    raise LinguaWikiError(
        "knowledge_item_not_found",
        f"no knowledge item matches {reference} by ID, stable key, or alias",
        details=(ErrorDetail(field=field, reason="no match"),),
    )


def _aliases(database: Database, content_id: str) -> tuple[AliasRecord, ...]:
    return tuple(
        AliasRecord(
            alias=str(alias),
            normalized=str(normalized),
            locale=str(locale),
            script=None if script is None else str(script),
        )
        for alias, normalized, locale, script in database.query(
            "SELECT alias, normalized, locale, script FROM knowledge_aliases "
            "WHERE content_id = ? ORDER BY locale, normalized",
            [content_id],
        )
    )


def _tags(database: Database, content_id: str) -> tuple[TagRecord, ...]:
    return tuple(
        TagRecord(tag_kind=str(kind), tag_value=str(value))
        for kind, value in database.query(
            "SELECT tag_kind, tag_value FROM item_tags WHERE content_id = ? "
            "ORDER BY tag_kind, tag_value",
            [content_id],
        )
    )


def _relations(database: Database, content_id: str) -> tuple[RelationRecord, ...]:
    outgoing = [
        RelationRecord(
            relation_id=str(relation_id),
            relation_type=str(relation_type),
            direction="outgoing",
            other_content_id=None if target is None else str(target),
            other_title=None if title is None else str(title),
            target_ref=None if target_ref is None else str(target_ref),
        )
        for relation_id, relation_type, target, target_ref, title in database.query(
            "SELECT edge.relation_id, edge.relation_type, edge.target_content_id, "
            "edge.target_ref, other.title FROM knowledge_relations edge "
            "LEFT JOIN knowledge_items other ON other.content_id = edge.target_content_id "
            "WHERE edge.source_content_id = ? "
            "ORDER BY edge.relation_type, edge.target_content_id, edge.target_ref",
            [content_id],
        )
    ]
    incoming = [
        RelationRecord(
            relation_id=str(relation_id),
            relation_type=str(relation_type),
            direction="incoming",
            other_content_id=str(source),
            other_title=None if title is None else str(title),
        )
        for relation_id, relation_type, source, title in database.query(
            "SELECT edge.relation_id, edge.relation_type, edge.source_content_id, other.title "
            "FROM knowledge_relations edge "
            "LEFT JOIN knowledge_items other ON other.content_id = edge.source_content_id "
            "WHERE edge.target_content_id = ? ORDER BY edge.relation_type, edge.source_content_id",
            [content_id],
        )
    ]
    return tuple(outgoing + incoming)


def _examples(database: Database, content_id: str) -> tuple[ExampleRecord, ...]:
    return tuple(
        ExampleRecord(
            content_id=str(example_id),
            text=str(text),
            translation=None if translation is None else str(translation),
            gloss=None if gloss is None else str(gloss),
            locale=None if locale is None else str(locale),
            difficulty=None if difficulty is None else str(difficulty),
        )
        for example_id, text, translation, gloss, locale, difficulty in database.query(
            "SELECT content_id, text, translation, gloss, locale, difficulty FROM examples "
            "WHERE item_content_id = ? ORDER BY content_id",
            [content_id],
        )
    )


def item_state(database: Database, *, track_id: str, content_id: str) -> ItemStateRecord | None:
    """The learner's recorded standing on one item, inside a caller's connection."""

    row = database.one(
        "SELECT track_id, stage, stage_source, confidence, positive_evidence, "
        "negative_evidence, first_encounter_at, last_encounter_at, aggregation_version, "
        "computed_at, explanation_json, gated_stage, evidence_ceiling FROM track_item_state "
        "WHERE track_id = ? AND content_id = ?",
        [track_id, content_id],
    )
    if row is None:
        return None
    explanation = () if row[10] is None else tuple(str(entry) for entry in json.loads(str(row[10])))
    return ItemStateRecord(
        track_id=str(row[0]),
        stage=str(row[1]),
        stage_source=str(row[2]),
        confidence=float(row[3]),
        positive_evidence=int(row[4]),
        negative_evidence=int(row[5]),
        first_encounter_at=None if row[6] is None else aware_utc(row[6]).isoformat(),
        last_encounter_at=None if row[7] is None else aware_utc(row[7]).isoformat(),
        aggregation_version=None if row[8] is None else str(row[8]),
        computed_at=None if row[9] is None else aware_utc(row[9]).isoformat(),
        explanation=explanation,
        gated_stage=None if row[11] is None else str(row[11]),
        evidence_ceiling=None if row[12] is None else str(row[12]),
    )


def _read_item(database: Database, content_id: str, *, track_id: str | None) -> KnowledgeItemReport:
    row = database.one(
        "SELECT item.content_id, item.language, item.kind, item.title, item.body, "
        "item.summary, item.level_min, item.level_max, item.created_at, item.updated_at, "
        "record.stable_key, record.pack_id, record.track_id, record.lifecycle, "
        "record.risk_tier, record.content_hash, pack.pack_key "
        "FROM knowledge_items item "
        "JOIN content_records record ON record.content_id = item.content_id "
        "LEFT JOIN language_packs pack ON pack.pack_id = record.pack_id "
        "WHERE item.content_id = ?",
        [content_id],
    )
    if row is None:
        raise LinguaWikiError(
            "knowledge_item_not_found",
            f"no knowledge item with ID {content_id}",
            details=(ErrorDetail(field="item", reason="unknown content id"),),
        )
    owner = "pack" if row[11] is not None else ("track" if row[12] is not None else "unowned")
    return KnowledgeItemReport(
        content_id=str(row[0]),
        language=str(row[1]),
        kind=str(row[2]),
        title=str(row[3]),
        body=str(row[4]),
        summary=None if row[5] is None else str(row[5]),
        level_min=None if row[6] is None else str(row[6]),
        level_max=None if row[7] is None else str(row[7]),
        created_at=aware_utc(row[8]).isoformat(),
        updated_at=aware_utc(row[9]).isoformat(),
        stable_key=str(row[10]),
        owner=owner,
        track_id=None if row[12] is None else str(row[12]),
        lifecycle=str(row[13]),
        risk_tier=int(row[14]),
        content_hash=str(row[15]),
        pack_key=None if row[16] is None else str(row[16]),
        aliases=_aliases(database, content_id),
        tags=_tags(database, content_id),
        relations=_relations(database, content_id),
        examples=_examples(database, content_id),
        state=None
        if track_id is None
        else item_state(database, track_id=track_id, content_id=content_id),
    )


def read_item(
    database: Database, content_id: str, *, track_id: str | None = None
) -> KnowledgeItemReport:
    """One item read inside a caller's connection."""

    return _read_item(database, content_id, track_id=track_id)


def get(
    paths: WorkspacePaths,
    *,
    item: str,
    track: str | None = None,
    clock: Clock | None = None,
) -> KnowledgeItemReport:
    """One item with its aliases, tags, edges, examples, and the learner's standing."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = _optional_track(database, track)
        return _read_item(
            database, resolve_item(database, item, track_id=track_id), track_id=track_id
        )


def _optional_track(database: Database, track: str | None) -> str | None:
    """Resolve a track only when one is asked for, or when the workspace has just one."""

    if track is not None:
        return learner_service.resolve_track(database, track)
    tracks = database.query("SELECT track_id FROM learning_tracks")
    return str(tracks[0][0]) if len(tracks) == 1 else None


def search(
    paths: WorkspacePaths,
    *,
    query: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    level: str | None = None,
    relation: str | None = None,
    related_to: str | None = None,
    track: str | None = None,
    limit: int | None = None,
    clock: Clock | None = None,
) -> SearchReport:
    """Find items by ID, alias, tag, level, or relation, in a total order.

    Every named filter narrows the same set, so the answer to "prerequisites of this item
    at A2 tagged travel" is one query rather than three and a client-side intersection.
    """

    bounded = _bounded_limit(limit)
    if kind is not None:
        _assert_known(kind, vocabulary=ITEM_KINDS, field="kind", code="unknown_item_kind")
    if relation is not None:
        _assert_known(
            relation, vocabulary=RELATION_TYPES, field="relation", code="unknown_relation_type"
        )
    if relation is not None and related_to is None:
        raise LinguaWikiError(
            "relation_filter_needs_anchor",
            "a relation filter needs the item it is a relation to; pass --related-to",
            details=(ErrorDetail(field="related_to", reason="missing anchor item"),),
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = _optional_track(database, track)
        anchor = (
            None if related_to is None else resolve_item(database, related_to, track_id=track_id)
        )
        folded = None if query is None else normalize_alias(query)
        # Scoped for the same reason resolution is: a search that lists another pack's
        # items is how a caller finds an identifier it should never have been able to use.
        scope = track_scope(database, track_id)
        conditions: list[str] = [IN_SCOPE] if scope else []
        parameters: list[Any] = list(scope)
        if folded is not None:
            # Title and alias are both places a learner looks. Prefix matching on the
            # folded form is the whole of it: no stemming, no tokenizing, no script rules.
            conditions.append(
                "(strpos(lower(item.title), ?) > 0 OR EXISTS ("
                "  SELECT 1 FROM knowledge_aliases alias WHERE alias.content_id = item.content_id"
                "  AND strpos(alias.normalized, ?) > 0) OR record.stable_key = ?)"
            )
            parameters.extend([folded, folded, query])
        if kind is not None:
            conditions.append("item.kind = ?")
            parameters.append(kind)
        if level is not None:
            conditions.append(
                "(item.level_min = ? OR EXISTS ("
                "  SELECT 1 FROM item_tags tag WHERE tag.content_id = item.content_id "
                "  AND tag.tag_kind = 'level' AND tag.tag_value = ?))"
            )
            parameters.extend([level, level])
        if tag is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM item_tags tag WHERE tag.content_id = item.content_id "
                "AND tag.tag_value = ?)"
            )
            parameters.append(tag)
        if anchor is not None:
            edge = "edge.relation_type = ? AND " if relation is not None else ""
            conditions.append(
                "EXISTS (SELECT 1 FROM knowledge_relations edge WHERE "
                f"{edge}((edge.source_content_id = ? AND edge.target_content_id = item.content_id)"
                " OR (edge.target_content_id = ? AND edge.source_content_id = item.content_id)))"
            )
            if relation is not None:
                parameters.append(relation)
            parameters.extend([anchor, anchor])
        where = " AND ".join(conditions) if conditions else "TRUE"
        total = int(
            database.scalar(
                "SELECT count(*) FROM knowledge_items item "
                "JOIN content_records record ON record.content_id = item.content_id "
                f"WHERE {where}",
                parameters,
            )
        )
        rows = database.query(
            "SELECT item.content_id, record.stable_key, item.kind, item.title, item.level_min, "
            "record.pack_id, record.track_id, state.stage "
            "FROM knowledge_items item "
            "JOIN content_records record ON record.content_id = item.content_id "
            "LEFT JOIN track_item_state state ON state.content_id = item.content_id "
            "  AND state.track_id = ? "
            f"WHERE {where} "
            # A total order, so the same query always returns the same page.
            "ORDER BY item.kind, record.stable_key, item.content_id LIMIT ?",
            [track_id, *parameters, bounded],
        )
        hits = tuple(
            SearchHit(
                content_id=str(row[0]),
                stable_key=str(row[1]),
                kind=str(row[2]),
                title=str(row[3]),
                level_min=None if row[4] is None else str(row[4]),
                owner="pack"
                if row[5] is not None
                else ("track" if row[6] is not None else "unowned"),
                stage=None if row[7] is None else str(row[7]),
                matched_on=_matched_on(
                    query=query, tag=tag, level=level, relation=relation, anchor=anchor
                ),
                matched_value=query or tag or level or relation,
            )
            for row in rows
        )
    return SearchReport(
        query=query,
        kind=kind,
        tag=tag,
        level=level,
        relation=relation,
        related_to=anchor,
        track_id=track_id,
        limit=bounded,
        total_matched=total,
        truncated=total > len(hits),
        hits=hits,
        warnings=((f"{total} item(s) matched; {len(hits)} returned",) if total > len(hits) else ()),
    )


def _matched_on(
    *,
    query: str | None,
    tag: str | None,
    level: str | None,
    relation: str | None,
    anchor: str | None,
) -> str:
    """Which filter the row is here because of, named for the caller's benefit."""

    if query is not None:
        return "text"
    if relation is not None or anchor is not None:
        return "relation"
    if tag is not None:
        return "tag"
    return "level" if level is not None else "listing"


def _learner_content_hash(
    *,
    content_id: str,
    language: str,
    kind: str,
    title: str,
    body: str,
) -> str:
    """The frozen `lingua.content.v1` hash over a learner-authored item.

    The same canonical field set as a pack item, so a learner note and a pack item are
    hashed by one rule and neither can be mistaken for the other's provenance.
    """

    return canonical_content_hash(
        {
            "schema_name": "lingua.content.v1",
            "schema_version": 1,
            "content_id": content_id,
            "language": language,
            "kind": kind,
            "title": title,
            "body": body,
            "risk_tier": 1,
            "provenance": {
                "origin": "learner-produced",
                "lifecycle": LEARNER_LIFECYCLE,
                "reviews": [],
            },
            "dependencies": [],
        }
    )


def upsert(
    paths: WorkspacePaths,
    *,
    stable_key: str,
    kind: str,
    title: str,
    body: str,
    summary: str | None = None,
    level: str | None = None,
    level_max: str | None = None,
    aliases: Sequence[Mapping[str, str]] | None = None,
    themes: Sequence[str] = (),
    features: Sequence[str] = (),
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "knowledge.upsert",
) -> UpsertReport:
    """Create or replace one learner-authored knowledge item on a track.

    A learner's own note is track-owned content: it never claims a pack's provenance, it
    reaches `approved-personal` and no further, and a pack upgrade cannot overwrite it
    because the two live in different identity namespaces.
    """

    _assert_known(kind, vocabulary=ITEM_KINDS, field="kind", code="unknown_item_kind")
    parsed_aliases = tuple(PackAlias.model_validate(dict(entry)) for entry in (aliases or ()))
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        if level is not None:
            _assert_track_level(record, level, field="level")
        if level_max is not None:
            _assert_track_level(record, level_max, field="level_max")
        content_id = str(ContentId.derive("track", track_id, KNOWLEDGE_KIND, stable_key))
        content_hash = _learner_content_hash(
            content_id=content_id,
            language=record.target_language,
            kind=kind,
            title=title,
            body=body,
        )
        existing = database.one(
            "SELECT track_id, pack_id FROM content_records WHERE content_id = ?", [content_id]
        )
        warnings: list[str] = []
        # A learner's identity is derived from the track, a pack's from the pack, so the
        # two can never collide and a note can never overwrite pack content. What a
        # shared stable key *does* cost is lookup by key: it becomes ambiguous, and
        # `resolve_item` refuses it rather than picking one. Saying so here is more use
        # than refusing, because the learner's own key may well be the natural one.
        shadowed = database.one(
            "SELECT pack.pack_key FROM content_records record "
            "JOIN language_packs pack ON pack.pack_id = record.pack_id "
            "WHERE record.stable_key = ? AND record.pack_id IS NOT NULL",
            [stable_key],
        )
        if shadowed is not None:
            warnings.append(
                f"{stable_key} is also a stable key in pack {shadowed[0]}; refer to either "
                "item by its content ID, because the key alone is now ambiguous"
            )
        with database.transaction() as transaction:
            now = transaction.now()
            if existing is None:
                transaction.execute(
                    "INSERT INTO content_records (content_id, content_kind, pack_id, track_id, "
                    "stable_key, language_tag, content_hash, lifecycle, risk_tier, batch_id, "
                    "quarantined, invalidation_reason, created_at, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 1, NULL, FALSE, NULL, ?, ?)",
                    [
                        content_id,
                        KNOWLEDGE_KIND,
                        track_id,
                        stable_key,
                        record.target_language,
                        content_hash,
                        LEARNER_LIFECYCLE,
                        now,
                        now,
                    ],
                )
                transaction.execute(
                    "INSERT INTO content_origins (content_id, sequence, origin_class, reference, "
                    "locator, transformation, origin_hash) "
                    "VALUES (?, 1, 'learner-produced', ?, NULL, NULL, NULL)",
                    [content_id, f"track:{track_id}"],
                )
            else:
                transaction.execute(
                    "UPDATE content_records SET content_hash = ?, updated_at = ? "
                    "WHERE content_id = ?",
                    [content_hash, now, content_id],
                )
            transaction.execute(
                "INSERT INTO knowledge_items (content_id, language, kind, title, body, summary, "
                "level_min, level_max, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (content_id) DO UPDATE SET kind = excluded.kind, "
                "title = excluded.title, body = excluded.body, summary = excluded.summary, "
                "level_min = excluded.level_min, level_max = excluded.level_max, "
                "updated_at = excluded.updated_at",
                [
                    content_id,
                    record.target_language,
                    kind,
                    title,
                    body,
                    summary,
                    level,
                    level_max,
                    now,
                    now,
                ],
            )
            transaction.execute("DELETE FROM knowledge_aliases WHERE content_id = ?", [content_id])
            for alias in dict.fromkeys(parsed_aliases):
                transaction.execute(
                    "INSERT INTO knowledge_aliases (content_id, normalized, alias, script, locale) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    [
                        content_id,
                        normalize_alias(alias.alias),
                        alias.alias,
                        alias.script,
                        alias.locale,
                    ],
                )
            transaction.execute("DELETE FROM item_tags WHERE content_id = ?", [content_id])
            tags = [("theme", theme) for theme in themes]
            tags.extend(("feature", feature) for feature in features)
            if level is not None:
                tags.append(("level", level))
            tags.append(("user", "learner-authored"))
            for tag_kind, tag_value in dict.fromkeys(tags):
                transaction.execute(
                    "INSERT INTO item_tags (content_id, tag_kind, tag_value) VALUES (?, ?, ?)",
                    [content_id, tag_kind, tag_value],
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([content_id]),
                after_summary=f"{'created' if existing is None else 'replaced'} {stable_key}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="knowledge.upserted",
                aggregate_type="knowledge_item",
                aggregate_id=content_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps(
                    {"stable_key": stable_key, "kind": kind, "track_id": track_id}, sort_keys=True
                ),
            )
        return UpsertReport(
            content_id=content_id,
            stable_key=stable_key,
            track_id=track_id,
            kind=kind,
            title=title,
            created=existing is None,
            aliases=tuple(alias.alias for alias in parsed_aliases),
            tags=_tags(database, content_id),
            content_hash=content_hash,
            lifecycle=LEARNER_LIFECYCLE,
            warnings=tuple(warnings),
        )


def _assert_track_level(record: learner_service.TrackRecord, level: str, *, field: str) -> str:
    """Require a level label the track's own framework declares.

    A level label belongs to one framework. Accepting an unknown one here is how a
    learner's item ends up tagged with a band no installed pack can interpret.
    """

    if record.framework_levels and level not in record.framework_levels:
        raise LinguaWikiError(
            "unknown_framework_level",
            f"{level} is not a level of {record.proficiency_framework}; that framework has "
            f"{list(record.framework_levels)}",
            details=(ErrorDetail(field=field, reason="level not in the track's framework"),),
        )
    return level


def link(
    paths: WorkspacePaths,
    *,
    source: str,
    relation_type: str,
    target: str | None = None,
    target_ref: str | None = None,
    track: str | None = None,
    clock: Clock | None = None,
    command: str = "knowledge.link",
) -> LinkReport:
    """Add one typed edge, deriving its identity from the edge itself.

    The identity is `(owner, source, type, target)`, so adding the same edge twice is one
    edge and a learner's own edge never collides with a pack's.
    """

    _assert_known(
        relation_type, vocabulary=RELATION_TYPES, field="relation", code="unknown_relation_type"
    )
    if (target is None) == (target_ref is None):
        raise LinguaWikiError(
            "invalid_relation_target",
            "an edge names either a target item or an external target reference",
            details=(ErrorDetail(field="target", reason="exactly one target is required"),),
        )
    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = resolve_item(database, source, track_id=track_id)
        target_id = None if target is None else resolve_item(database, target, track_id=track_id)
        if target_id == source_id:
            raise LinguaWikiError(
                "self_relation",
                "an item cannot be related to itself",
                details=(ErrorDetail(field="target", reason="source and target are the same"),),
            )
        relation_id = learner_relation_id(
            track_id=track_id,
            source=source_id,
            relation_type=relation_type,
            target=target_id,
            target_ref=target_ref,
        )
        existing = database.scalar(
            "SELECT relation_id FROM knowledge_relations WHERE relation_id = ?", [relation_id]
        )
        with database.transaction() as transaction:
            now = transaction.now()
            if existing is None:
                transaction.execute(
                    "INSERT INTO knowledge_relations (relation_id, source_content_id, "
                    "relation_type, target_content_id, target_ref, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [relation_id, source_id, relation_type, target_id, target_ref, now],
                )
                migration_module.record_domain_event(
                    transaction,
                    event_type="knowledge.linked",
                    aggregate_type="knowledge_relation",
                    aggregate_id=relation_id,
                    correlation_id=EventId.new(),
                    payload_json=json.dumps(
                        {"source": source_id, "relation_type": relation_type}, sort_keys=True
                    ),
                )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([relation_id]),
                after_summary=f"{relation_type} edge from {source_id}",
            )
    return LinkReport(
        relation_id=relation_id,
        source_content_id=source_id,
        relation_type=relation_type,
        target_content_id=target_id,
        target_ref=target_ref,
        created=existing is None,
    )


#: What a merge moves, as (table, key column, the column naming the item).
MERGE_MOVES: tuple[tuple[str, str], ...] = (
    ("knowledge_aliases", "content_id"),
    ("item_tags", "content_id"),
    ("examples", "item_content_id"),
    ("track_item_state", "content_id"),
    ("attempts", "target_content_id"),
    ("evidence", "target_content_id"),
    ("error_patterns", "target_content_id"),
    ("followups", "target_content_id"),
)


def merge(
    paths: WorkspacePaths,
    *,
    source: str,
    into: str,
    track: str | None = None,
    dry_run: bool = True,
    clock: Clock | None = None,
    command: str = "knowledge.merge",
) -> MergeReport:
    """Fold a duplicate learner item into another item, moving everything attached.

    Defaults to a dry run: a merge is not reversible, and the learner's evidence, errors,
    and follow-ups all move with it. The dry run reports exactly what would move.

    The merged item's stage is *not* copied. Evidence is repointed and the stage is
    recomputed from it, because a stage is a conclusion about evidence and moving one
    across identities would let a merge manufacture mastery nobody observed.
    """

    active_clock = clock or SystemClock()
    with open_writer(paths, command=command, clock=active_clock) as database:
        track_id = learner_service.resolve_track(database, track)
        source_id = resolve_item(database, source, track_id=track_id)
        target_id = resolve_item(database, into, track_id=track_id)
        if source_id == target_id:
            raise LinguaWikiError(
                "self_merge",
                "an item cannot be merged into itself",
                details=(ErrorDetail(field="into", reason="source and target are the same"),),
            )
        ownership = database.one(
            "SELECT pack_id, track_id, stable_key FROM content_records WHERE content_id = ?",
            [source_id],
        )
        assert ownership is not None
        if ownership[0] is not None:
            raise LinguaWikiError(
                "pack_item_not_mergeable",
                f"{ownership[2]} belongs to a pack, and the next install would restore it. "
                "Merge the learner's own duplicate into it instead, or change the pack and "
                "run 'linguawiki pack update'.",
                details=(ErrorDetail(field="source", reason="pack-owned item"),),
            )
        moves: list[MergeMove] = []
        for table, column in MERGE_MOVES:
            moves.append(
                _plan_move(database, table=table, column=column, source=source_id, target=target_id)
            )
        relation_moves = _plan_relation_moves(database, source=source_id, target=target_id)
        moves.extend(relation_moves)
        evidence_moved = next((move.moved for move in moves if move.table == "evidence"), 0)
        source_stage = item_state(database, track_id=track_id, content_id=source_id)
        target_stage = item_state(database, track_id=track_id, content_id=target_id)
        if dry_run:
            return MergeReport(
                source_content_id=source_id,
                target_content_id=target_id,
                dry_run=True,
                applied=False,
                source_stage=None if source_stage is None else source_stage.stage,
                target_stage=None if target_stage is None else target_stage.stage,
                moves=tuple(moves),
                evidence_moved=evidence_moved,
                warnings=(
                    "this is a dry run; re-run with --apply to move these rows",
                    "the merged item's stage will be recomputed from the moved evidence",
                ),
            )
        with database.transaction() as transaction:
            now = transaction.now()
            remapped, folded = _apply_moves(
                transaction, track_id=track_id, source=source_id, target=target_id
            )
            transaction.execute(
                "UPDATE content_records SET lifecycle = 'deprecated', "
                "invalidation_reason = ?, updated_at = ? WHERE content_id = ?",
                [f"merged into {target_id}", now, source_id],
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=EventId.new(),
                outcome="succeeded",
                affected_records_json=json.dumps([source_id, target_id]),
                before_summary=f"{source_id} held {evidence_moved} evidence row(s)",
                after_summary=f"merged {source_id} into {target_id}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="knowledge.merged",
                aggregate_type="knowledge_item",
                aggregate_id=target_id,
                correlation_id=EventId.new(),
                payload_json=json.dumps({"source": source_id, "target": target_id}, sort_keys=True),
            )
    return MergeReport(
        source_content_id=source_id,
        target_content_id=target_id,
        dry_run=False,
        applied=True,
        source_stage=None if source_stage is None else source_stage.stage,
        target_stage=None if target_stage is None else target_stage.stage,
        moves=tuple(moves),
        evidence_moved=evidence_moved,
        errors_remapped=remapped,
        errors_folded=folded,
        warnings=(
            (
                "run 'linguawiki evidence recompute' to settle the merged item's stage from "
                "its evidence",
            )
            + (
                (
                    f"{folded} error pattern(s) turned out to be the same error already "
                    "recorded on the target and were folded together",
                )
                if folded
                else ()
            )
        ),
    )


def _plan_move(
    database: Database, *, table: str, column: str, source: str, target: str
) -> MergeMove:
    """How many rows would move, and how many the target already has."""

    from linguawiki.db.connection import quote_identifier

    scoped = quote_identifier(table)
    key = quote_identifier(column)
    moved = int(database.scalar(f"SELECT count(*) FROM {scoped} WHERE {key} = ?", [source]))
    present = int(database.scalar(f"SELECT count(*) FROM {scoped} WHERE {key} = ?", [target]))
    return MergeMove(table=table, moved=moved, already_present=present)


def _plan_relation_moves(database: Database, *, source: str, target: str) -> list[MergeMove]:
    outgoing = int(
        database.scalar(
            "SELECT count(*) FROM knowledge_relations WHERE source_content_id = ?", [source]
        )
    )
    incoming = int(
        database.scalar(
            "SELECT count(*) FROM knowledge_relations WHERE target_content_id = ?", [source]
        )
    )
    return [
        MergeMove(table="knowledge_relations.outgoing", moved=outgoing),
        MergeMove(table="knowledge_relations.incoming", moved=incoming),
    ]


def _apply_moves(database: Database, *, track_id: str, source: str, target: str) -> tuple[int, int]:
    """Repoint every row attached to the source, keeping the target's own rows.

    Rows are deleted rather than repointed where the target already holds the same key:
    a merged alias, tag, or per-track state would otherwise collide on a primary key, and
    the target's own row is the one to keep.
    """

    for table, column, conflict in (
        ("knowledge_aliases", "content_id", ("locale", "normalized")),
        ("item_tags", "content_id", ("tag_kind", "tag_value")),
        ("track_item_state", "content_id", ("track_id",)),
    ):
        keys = ", ".join(conflict)
        database.execute(
            f"DELETE FROM {table} WHERE {column} = ? AND ({keys}) IN "
            f"(SELECT {keys} FROM {table} WHERE {column} = ?)",
            [source, target],
        )
        database.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", [target, source])
    database.execute(
        "UPDATE examples SET item_content_id = ? WHERE item_content_id = ?", [target, source]
    )
    # Edges are re-derived rather than repointed: an identity is computed from the parts
    # it describes, and `_remap_relations` collapses the edges that would have become an
    # item related to itself.
    _remap_relations(database, track_id=track_id, source=source, target=target)
    for table, column in (
        ("attempts", "target_content_id"),
        ("evidence", "target_content_id"),
        ("followups", "target_content_id"),
    ):
        database.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", [target, source])
    return _remap_errors(database, source=source, target=target)


def learner_relation_id(
    *, track_id: str, source: str, relation_type: str, target: str | None, target_ref: str | None
) -> str:
    """The identity `knowledge link` derives for a learner-created edge.

    One function, used by `link`, by the merge remap, and by `db check`, because three
    derivations of the same identity are three chances for them to disagree -- which is
    exactly how a merge came to leave an edge whose identity described an item it no
    longer touched.
    """

    return derive_id(
        IdPrefix.CONTENT,
        "track",
        track_id,
        source,
        relation_type,
        target or f"ref:{target_ref}",
    )


def _remap_relations(database: Database, *, track_id: str, source: str, target: str) -> int:
    """Repoint each edge touching the merged item, re-deriving its identity.

    An edge's identity is derived from `(owner, source, type, target)`, so repointing an
    endpoint without re-deriving leaves a `relation_id` that describes an item the edge
    no longer touches. Adding the same edge again then derives the *new* identity, finds
    nothing, and creates a second identical edge -- which is what happened.

    An edge is recognised as this track's by re-deriving its *old* identity: if that
    matches, the track created it and the new identity is derived the same way. An edge
    that does not match belongs to a pack, whose own installation owns its identity, and
    it is repointed without being re-keyed.

    Nothing references `knowledge_relations`, so unlike an error pattern an edge can be
    replaced outright inside the transaction.
    """

    moved = 0
    edges = database.query(
        "SELECT relation_id, source_content_id, relation_type, target_content_id, target_ref, "
        "created_at FROM knowledge_relations "
        "WHERE source_content_id = ? OR target_content_id = ? ORDER BY relation_id",
        [source, source],
    )
    for relation_id, edge_source, relation_type, edge_target, target_ref, created_at in edges:
        new_source = target if str(edge_source) == source else str(edge_source)
        new_target = (
            target
            if edge_target is not None and str(edge_target) == source
            else (None if edge_target is None else str(edge_target))
        )
        if new_source == new_target:
            # The merge collapsed both ends onto one item; an item is not related to
            # itself, so the edge simply ceases to exist.
            database.execute(
                "DELETE FROM knowledge_relations WHERE relation_id = ?", [str(relation_id)]
            )
            continue
        learner_created = str(relation_id) == learner_relation_id(
            track_id=track_id,
            source=str(edge_source),
            relation_type=str(relation_type),
            target=None if edge_target is None else str(edge_target),
            target_ref=None if target_ref is None else str(target_ref),
        )
        remapped = (
            learner_relation_id(
                track_id=track_id,
                source=new_source,
                relation_type=str(relation_type),
                target=new_target,
                target_ref=None if target_ref is None else str(target_ref),
            )
            if learner_created
            else str(relation_id)
        )
        database.execute(
            "DELETE FROM knowledge_relations WHERE relation_id = ?", [str(relation_id)]
        )
        # The target may already carry the same edge; its own row is the one to keep.
        if not int(
            database.scalar(
                "SELECT count(*) FROM knowledge_relations WHERE relation_id = ? "
                "OR (source_content_id = ? AND relation_type = ? "
                "AND coalesce(target_content_id, '-') = ? "
                "AND coalesce(target_ref, '-') = ?)",
                [
                    remapped,
                    new_source,
                    str(relation_type),
                    new_target or "-",
                    target_ref or "-",
                ],
            )
        ):
            database.execute(
                "INSERT INTO knowledge_relations (relation_id, source_content_id, "
                "relation_type, target_content_id, target_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [remapped, new_source, str(relation_type), new_target, target_ref, created_at],
            )
            moved += 1
    return moved


def _remap_errors(database: Database, *, source: str, target: str) -> tuple[int, int]:
    """Re-derive each error's identity after its target item moved, folding collisions.

    An error's identity is derived from its target, so repointing one without
    re-deriving it would leave a pattern whose key no longer describes it -- and the next
    occurrence of that same mistake would derive the new key, find nothing, and open a
    second pattern beside the first. `db check` reports exactly that, which is how this
    was found.

    Where the merged-into item already carries the same error, the two *are* the same
    error: their occurrences and counter-evidence are pooled, their counts add, and the
    status that survives is the one that says the error is still happening.
    """

    from linguawiki.error_model import stronger_status
    from linguawiki.services.errors import error_identity

    moved = 0
    folded = 0
    for error_id, track_id, category, signature in database.query(
        "SELECT error_id, track_id, category, signature FROM error_patterns "
        "WHERE target_content_id = ? ORDER BY error_id",
        [source],
    ):
        identity = str(error_id)
        remapped = error_identity(
            track_id=str(track_id),
            category=str(category),
            signature=str(signature),
            target_content_id=target,
        )
        if remapped == identity:
            continue
        surviving = database.one(
            "SELECT status, occurrence_count, success_count, first_seen_at, last_seen_at "
            "FROM error_patterns WHERE error_id = ?",
            [remapped],
        )
        if surviving is None:
            _reinsert_error(database, old_id=identity, new_id=remapped, target=target)
            moved += 1
            continue
        _fold_error(
            database,
            old_id=identity,
            new_id=remapped,
            status=stronger_status(str(surviving[0]), _status_of(database, identity)),
        )
        folded += 1
    return (moved, folded)


def _status_of(database: Database, error_id: str) -> str:
    return str(database.scalar("SELECT status FROM error_patterns WHERE error_id = ?", [error_id]))


#: The tables whose rows point at an error pattern. A remap has to bring all of them.
ERROR_DEPENDENTS: tuple[tuple[str, str], ...] = (
    ("error_occurrences", "error_id"),
    ("error_evidence", "error_id"),
    ("followups", "error_id"),
)


def _reinsert_error(database: Database, *, old_id: str, new_id: str, target: str) -> None:
    """Write the pattern under its re-derived identity and bring its history across.

    The old row is *superseded*, not deleted. DuckDB will not delete a referenced row in
    the same transaction that repointed its children -- the foreign-key index still holds
    the old key -- and a merge has to be one transaction. Retaining it also matches how
    the rest of the repository treats history: content is deprecated, an assessment form
    is superseded, and nothing a learner's record pointed at is destroyed.
    """

    database.execute(
        "INSERT INTO error_patterns (error_id, track_id, category, signature, description, "
        "target_content_id, severity, status, status_reason, policy_version, "
        "occurrence_count, success_count, first_seen_at, last_seen_at, last_success_at, "
        "monitoring_since, resolved_at, updated_at) "
        "SELECT ?, track_id, category, signature, description, ?, severity, status, "
        "status_reason, policy_version, occurrence_count, success_count, first_seen_at, "
        "last_seen_at, last_success_at, monitoring_since, resolved_at, updated_at "
        "FROM error_patterns WHERE error_id = ?",
        [new_id, target, old_id],
    )
    for table, column in ERROR_DEPENDENTS:
        database.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", [new_id, old_id])
    _supersede_error(
        database,
        old_id=old_id,
        new_id=new_id,
        reason=(
            "the target item was merged away, so this pattern's identity was re-derived; "
            "its history continues under the successor named here"
        ),
    )


def _supersede_error(database: Database, *, old_id: str, new_id: str, reason: str) -> None:
    """Retire a pattern in favour of the one its history moved to.

    The counts are zeroed with it: the occurrences and counter-evidence they described
    now belong to the successor, and a shell claiming occurrences it no longer holds is
    a shell whose numbers say something untrue -- which `db check` reports.
    """

    from linguawiki.error_model import SUPERSEDED_STATUS

    database.execute(
        "UPDATE error_patterns SET status = ?, status_reason = ?, superseded_by = ?, "
        "occurrence_count = 0, success_count = 0, updated_at = ? WHERE error_id = ?",
        [SUPERSEDED_STATUS, reason, new_id, database.now(), old_id],
    )


def _fold_error(database: Database, *, old_id: str, new_id: str, status: str) -> None:
    """Pool two records of one error onto the surviving pattern."""

    now = database.now()
    for table, column in ERROR_DEPENDENTS:
        if table == "error_evidence":
            # The key is (error, evidence, qualification), so the same counter-evidence
            # already on the survivor would collide. Its own row is the one to keep.
            database.execute(
                "DELETE FROM error_evidence WHERE error_id = ? AND (evidence_id, qualification) "
                "IN (SELECT evidence_id, qualification FROM error_evidence WHERE error_id = ?)",
                [old_id, new_id],
            )
        database.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", [new_id, old_id])
    from linguawiki.error_model import CONFIRMED_CLASSIFICATION

    database.execute(
        "UPDATE error_patterns SET status = ?, status_reason = ?, "
        # Confirmed occurrences only, which is what `occurrence_count` means: an
        # artifact pooled from either side is on the record and counts for nothing.
        "occurrence_count = (SELECT count(*) FROM error_occurrences WHERE error_id = ? "
        "  AND classification = ?), "
        "success_count = (SELECT count(DISTINCT evidence_id) FROM error_evidence "
        "  WHERE error_id = ?), "
        "first_seen_at = least(first_seen_at, (SELECT first_seen_at FROM error_patterns "
        "  WHERE error_id = ?)), "
        "last_seen_at = greatest(last_seen_at, (SELECT last_seen_at FROM error_patterns "
        "  WHERE error_id = ?)), "
        "resolved_at = CASE WHEN ? = 'resolved' THEN resolved_at ELSE NULL END, "
        "updated_at = ? WHERE error_id = ?",
        [
            status,
            "folded together with the same error on a merged item; the counts are pooled "
            "and the status that says it is still happening survives",
            new_id,
            CONFIRMED_CLASSIFICATION,
            new_id,
            old_id,
            old_id,
            status,
            now,
            new_id,
        ],
    )
    _supersede_error(
        database,
        old_id=old_id,
        new_id=new_id,
        reason=(
            "the same error was already recorded on the item this one was merged into; "
            "the two were folded together under the successor named here"
        ),
    )


__all__ = [
    "DEFAULT_LIMIT",
    "ITEM_KINDS",
    "KNOWLEDGE_KIND",
    "LEARNER_LIFECYCLE",
    "MAXIMUM_LIMIT",
    "RELATION_TYPES",
    "TAG_KINDS",
    "AliasRecord",
    "ExampleRecord",
    "ItemStateRecord",
    "KnowledgeItemReport",
    "LinkReport",
    "MergeMove",
    "MergeReport",
    "RelationRecord",
    "SearchHit",
    "SearchReport",
    "TagRecord",
    "UpsertReport",
    "get",
    "item_state",
    "learner_relation_id",
    "link",
    "merge",
    "read_item",
    "resolve_item",
    "search",
    "upsert",
]
