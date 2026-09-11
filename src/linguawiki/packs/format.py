"""Read a pack directory into resolved, hash-verified, gate-checked items.

Three properties are established here and nowhere else, because a pack that is
"validated" in two different places is a pack whose contents nobody can trust:

- **total checksum coverage.** The manifest's `files` map must name every file in the
  directory except the manifest itself, at its exact digest. Listing only the files the
  manifest happens to care about would let an undeclared file ride along, and omitting a
  declared file would restore as silently absent.
- **derived identity.** Every content ID is derived from `(pack_key, content kind,
  stable key)`, so reinstalling a pack version produces the same IDs and a learner's
  annotations survive an upgrade. Nothing in a pack file may assert an ID.
- **the promotion gate.** An item's declared lifecycle has to be one its per-axis review
  states actually support, judged by `provenance.gate_problems`.

Role assignment is by declared path: `seed/knowledge.jsonl` is knowledge because of
where it is. A file with no role is still checksum-covered but is never loaded, which is
how the fixture packs keep their Stage 0 `content.json` example.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ValidationError

from linguawiki.contracts import (
    ContentItem,
    ContentProvenance,
    PackActivityFile,
    PackActivityTemplate,
    PackAssessmentFile,
    PackAssessmentTask,
    PackBundleFile,
    PackCapabilities,
    PackDescriptor,
    PackExample,
    PackExpectations,
    PackItemOrigin,
    PackItemProvenance,
    PackItemReview,
    PackKnowledgeItem,
    PackManifest,
    PackProficiencyFile,
    PackReferencesFile,
    PackRelation,
    PackSourcePolicy,
    PackSourceRecommendation,
    canonical_content_hash,
)
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import ContentId, IdPrefix, derive_id
from linguawiki.provenance import (
    CONTENT_ORIGIN_BY_CLASS,
    content_lifecycle,
    content_reviews,
    gate_problems,
)
from linguawiki.versions import file_sha256, tree_sha256

MANIFEST_NAME = "manifest.json"
#: Namespace of the pack-item hash. It is *not* `lingua.content.v1`: that contract's
#: canonical hash covers a frozen field set, which is narrower than a pack item's content.
PACK_ITEM_HASH_SCHEMA = "lingua.pack.item-hash.v1"
CAPABILITIES_NAME = "capabilities.json"
SOURCE_POLICY_NAME = "source-policy.json"
KNOWLEDGE_SEED = "seed/knowledge.jsonl"
RELATIONS_SEED = "seed/relations.jsonl"
EXAMPLES_SEED = "seed/examples.jsonl"
EXPECTATIONS_NAME = "tests/expectations.json"
PROFICIENCY_PREFIX = "proficiency/"
ASSESSMENTS_PREFIX = "assessments/"
ACTIVITIES_PREFIX = "activities/"
REFERENCES_PREFIX = "references/"
BUNDLES_PREFIX = "resource-bundles/"

#: Files a pack must contain to be installable at all.
REQUIRED_FILES = (CAPABILITIES_NAME, SOURCE_POLICY_NAME, KNOWLEDGE_SEED)

#: The `content_records.content_kind` each pack item becomes.
KNOWLEDGE_KIND = "knowledge"
EXAMPLE_KIND = "example"
DESCRIPTOR_KIND = "descriptor"
TASK_KIND = "assessment_task"
ACTIVITY_KIND = "activity_template"
RECOMMENDATION_KIND = "source_recommendation"
BUNDLE_KIND = "resource_bundle"


class PackError(LinguaWikiError):
    """A pack that cannot be read, resolved, or trusted."""


def _fail(code: str, message: str, problems: Sequence[str]) -> PackError:
    return PackError(
        code,
        message,
        details=tuple(ErrorDetail(reason=problem) for problem in problems[:50]),
    )


@dataclass(frozen=True, slots=True)
class ResolvedItem:
    """One persistent pack item with its identity, hash, and resolved provenance."""

    content_kind: str
    stable_key: str
    content_id: ContentId
    content_hash: str
    declared_hash: str
    lifecycle: str
    risk_tier: int
    title: str
    body: str
    language: str
    origins: tuple[PackItemOrigin, ...]
    reviews: Mapping[str, PackItemReview]
    dependencies: tuple[ContentId, ...]
    provenance: PackItemProvenance
    payload: BaseModel

    @property
    def hash_matches(self) -> bool:
        return self.content_hash == self.declared_hash


@dataclass(frozen=True, slots=True)
class LoadedPack:
    """A pack directory read into memory and proven internally consistent."""

    root: Path
    manifest: PackManifest
    capabilities: PackCapabilities
    source_policy: PackSourcePolicy
    expectations: PackExpectations | None
    file_digests: Mapping[str, str]
    content_address: str
    knowledge: tuple[ResolvedItem, ...]
    relations: tuple[PackRelation, ...]
    examples: tuple[ResolvedItem, ...]
    descriptors: tuple[ResolvedItem, ...]
    tasks: tuple[ResolvedItem, ...]
    activities: tuple[ResolvedItem, ...]
    recommendations: tuple[ResolvedItem, ...]
    bundles: tuple[ResolvedItem, ...]
    bundle_files: Mapping[str, PackBundleFile]
    assessment_forms: tuple[PackAssessmentFile, ...]
    warnings: tuple[str, ...] = field(default=())

    @property
    def pack_key(self) -> str:
        return self.manifest.pack_key

    @property
    def items(self) -> tuple[ResolvedItem, ...]:
        return (
            *self.knowledge,
            *self.examples,
            *self.descriptors,
            *self.tasks,
            *self.activities,
            *self.recommendations,
            *self.bundles,
        )

    def item_index(self) -> dict[tuple[str, str], ResolvedItem]:
        return {(item.content_kind, item.stable_key): item for item in self.items}

    def framework_levels(self, framework_id: str) -> tuple[str, ...]:
        framework = self.manifest.framework_by_id.get(framework_id)
        return () if framework is None else framework.levels


def content_id_for(pack_key: str, content_kind: str, stable_key: str) -> ContentId:
    """The one derivation of a pack item's content ID."""

    return ContentId.derive(pack_key, content_kind, stable_key)


def relation_id_for(pack_key: str, relation: PackRelation) -> str:
    return derive_id(
        IdPrefix.CONTENT,
        pack_key,
        "relation",
        relation.source_key,
        relation.relation_type,
        relation.target_key or relation.target_ref or "",
    )


def pack_content_address(manifest: PackManifest, file_digests: Mapping[str, str]) -> str:
    """Content-address a pack version from its manifest and every other file.

    The manifest is hashed with `content_address` removed, so stamping the address into
    the file cannot change the address it records.
    """

    payload = manifest.model_dump(mode="json")
    payload.pop("content_address", None)
    manifest_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return tree_sha256(
        ((MANIFEST_NAME, hashlib.sha256(manifest_bytes).hexdigest()), *file_digests.items())
    )


def directory_digests(root: Path) -> dict[str, str]:
    """Hash every file in a pack directory except the manifest."""

    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_NAME or "__pycache__" in path.parts:
            continue
        digests[relative] = file_sha256(path)
    return digests


def file_context(document: BaseModel, collection: str) -> dict[str, Any]:
    """A file's header, without the items it holds.

    An item's hash depends on the file that frames it: a task belongs to a form with a
    purpose and a level range, a descriptor to a framework. Removing only the item
    collection keeps every header field, including ones added later.
    """

    payload = document.model_dump(mode="json")
    payload.pop(collection, None)
    return payload


def _read_json(path: Path, *, relative: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail("pack_file_unreadable", f"{relative} is not readable JSON", [str(exc)]) from exc


def _read_jsonl(path: Path, *, relative: str) -> Iterator[tuple[int, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail("pack_file_unreadable", f"{relative} is not readable", [str(exc)]) from exc
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            yield number, json.loads(line)
        except json.JSONDecodeError as exc:
            raise _fail(
                "pack_file_unreadable", f"{relative}:{number} is not a JSON object", [str(exc)]
            ) from exc


def _validate_model[T: BaseModel](model: type[T], payload: Any, *, where: str) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise _fail(
            "pack_contract_invalid",
            f"{where} does not satisfy {model.__name__}",
            [
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors()
            ],
        ) from exc


def _assert_checksum_coverage(manifest: PackManifest, observed: Mapping[str, str]) -> None:
    declared = set(manifest.files)
    present = set(observed)
    problems: list[str] = []
    problems.extend(f"declared but absent: {name}" for name in sorted(declared - present))
    problems.extend(f"present but undeclared: {name}" for name in sorted(present - declared))
    problems.extend(
        f"checksum differs: {name}"
        for name in sorted(declared & present)
        if manifest.files[name] != observed[name]
    )
    problems.extend(
        f"required file missing: {name}" for name in REQUIRED_FILES if name not in present
    )
    if problems:
        raise _fail(
            "pack_checksum_mismatch",
            f"{manifest.pack_key} does not match the file set its manifest declares",
            problems,
        )


def _resolve_origins(
    manifest: PackManifest, provenance: PackItemProvenance, *, where: str
) -> tuple[PackItemOrigin, ...]:
    if provenance.origins:
        origins = provenance.origins
    else:
        profile = manifest.origin_profiles.get(provenance.origin_profile or "")
        if not profile:
            raise _fail(
                "pack_profile_unknown",
                f"{where} names an origin profile the manifest does not declare",
                [f"origin_profile: {provenance.origin_profile}"],
            )
        origins = profile
    rights = {origin.rights for origin in origins}
    privacy = {origin.privacy for origin in origins}
    if len(rights) > 1 or len(privacy) > 1:
        raise _fail(
            "pack_provenance_inconsistent",
            f"{where} declares origins that disagree about rights or privacy",
            [f"rights: {sorted(rights)}", f"privacy: {sorted(privacy)}"],
        )
    return origins


def _resolve_reviews(
    manifest: PackManifest, provenance: PackItemProvenance, *, where: str
) -> dict[str, PackItemReview]:
    if provenance.reviews:
        return {str(axis): review for axis, review in provenance.reviews.items()}
    profile = manifest.review_profiles.get(provenance.review_profile or "")
    if not profile:
        raise _fail(
            "pack_profile_unknown",
            f"{where} names a review profile the manifest does not declare",
            [f"review_profile: {provenance.review_profile}"],
        )
    return {str(axis): review for axis, review in profile.items()}


def _content_provenance(
    origins: Sequence[PackItemOrigin], generation_run: str | None
) -> ContentProvenance:
    """Project resolved origins onto the frozen `lingua.content.v1` provenance."""

    primary = origins[0]
    references = sorted({origin.reference for origin in origins if origin.reference})
    return ContentProvenance(
        origin=CONTENT_ORIGIN_BY_CLASS[primary.origin_class],
        source_references=tuple(references),
        generation_run_id=generation_run,
        rights=primary.rights,
        privacy=primary.privacy,
    )


#: The provenance fields an item's hash deliberately excludes. They are review
#: *workflow*, not content: recording a review must not change the revision the review
#: examined, which is the same exclusion ADR 0005 froze for `lingua.content.v1`.
#: `origin_profile` and `review_profile` are absent too, because the hash covers the
#: *resolved* origins rather than the name of the profile they came from -- editing a
#: profile's rights in the manifest therefore changes every item that uses it.
UNHASHED_PROVENANCE_FIELDS = frozenset(
    {"content_hash", "lifecycle", "review_profile", "reviews", "origin_profile", "origins"}
)


def pack_item_hash(
    *,
    content_id: ContentId,
    content_kind: str,
    language: str,
    context: Mapping[str, Any],
    payload: BaseModel,
    origins: Sequence[PackItemOrigin],
    dependencies: Sequence[ContentId],
) -> str:
    """Hash everything about an item except its review workflow.

    The payload is hashed *whole* rather than field by field. An enumerated field list is
    the wrong shape for this: the first version hashed only title, body, and provenance,
    so changing an assessment task's expected answers, rubric, difficulty, or modality --
    or a knowledge item's level or themes -- left the hash untouched, `pack stamp --check`
    accepted the edit, and the item's existing reviews stayed bound to text nobody had
    reviewed. Hashing the whole payload makes a field added later covered by default
    instead of covered if somebody remembers.

    `context` carries the containing file's header, so a task also depends on the form it
    belongs to and a descriptor on its framework.
    """

    document = payload.model_dump(mode="json")
    declared = document.pop("provenance", {})
    hashed_provenance = {
        key: value for key, value in declared.items() if key not in UNHASHED_PROVENANCE_FIELDS
    }
    hashed_provenance["origins"] = [origin.model_dump(mode="json") for origin in origins]
    hashed_provenance["dependencies"] = [str(dependency) for dependency in dependencies]
    canonical = {
        "schema_name": PACK_ITEM_HASH_SCHEMA,
        "schema_version": 1,
        "content_id": str(content_id),
        "content_kind": content_kind,
        "language": language,
        "context": dict(context),
        "payload": document,
        "provenance": hashed_provenance,
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _resolve_item(
    manifest: PackManifest,
    *,
    content_kind: str,
    stable_key: str,
    title: str,
    body: str,
    provenance: PackItemProvenance,
    payload: BaseModel,
    where: str,
    context: Mapping[str, Any] | None = None,
) -> ResolvedItem:
    origins = _resolve_origins(manifest, provenance, where=where)
    reviews = _resolve_reviews(manifest, provenance, where=where)
    content_id = content_id_for(manifest.pack_key, content_kind, stable_key)
    dependencies = tuple(
        content_id_for(manifest.pack_key, KNOWLEDGE_KIND, dependency.reference)
        for dependency in provenance.dependencies
        if dependency.kind == "content"
    )
    return ResolvedItem(
        content_kind=content_kind,
        stable_key=stable_key,
        content_id=content_id,
        content_hash=pack_item_hash(
            content_id=content_id,
            content_kind=content_kind,
            language=manifest.language,
            context=context or {},
            payload=payload,
            origins=origins,
            dependencies=dependencies,
        ),
        declared_hash=provenance.content_hash,
        lifecycle=provenance.lifecycle,
        risk_tier=provenance.risk_tier,
        title=title,
        body=body,
        language=manifest.language,
        origins=origins,
        reviews=reviews,
        dependencies=dependencies,
        provenance=provenance,
        payload=payload,
    )


def descriptor_title(framework: str, level: str, dimension: str) -> str:
    return f"{framework} {level} {dimension}"


def task_title(form_key: str, dimension: str, level: str) -> str:
    return f"{form_key} {dimension} {level}"


def activity_body(structure: Mapping[str, Any]) -> str:
    return json.dumps(structure, ensure_ascii=False, sort_keys=True)


def bundle_body(bundle_type: str, framework: str, level: str) -> str:
    return f"{bundle_type} {framework} {level}"


def item_content_hash(
    manifest: PackManifest,
    *,
    content_kind: str,
    stable_key: str,
    provenance: PackItemProvenance,
    payload: BaseModel,
    where: str,
    context: Mapping[str, Any] | None = None,
) -> str:
    """The hash the loader would compute for this item, for the stamping tool.

    Stamping cannot use `load_pack`, which refuses a pack whose hashes are stale, so it
    resolves one item through the same code path instead of reimplementing it. Titles and
    bodies are irrelevant here: the hash covers the whole payload.
    """

    return _resolve_item(
        manifest,
        content_kind=content_kind,
        stable_key=stable_key,
        title="",
        body="",
        provenance=provenance,
        payload=payload,
        where=where,
        context=context,
    ).content_hash


def resolved_content_item(item: ResolvedItem) -> ContentItem:
    """Project a knowledge item onto the frozen content contract.

    Knowledge items are the kinds `lingua.content.v1` can express, so validating them
    through it proves the Stage 2 gate never falls below the frozen v1 floor.

    The projection carries its *own* hash. `lingua.content.v1`'s canonical hash covers a
    field set ADR 0005 froze, which is narrower than a pack item's content; the pack's
    `content_hash` is the broader one, and it is the one the stored reviews bind to.
    """

    payload = item.payload
    assert isinstance(payload, PackKnowledgeItem)
    provenance = _content_provenance(item.origins, item.provenance.generation_run)
    canonical = {
        "schema_name": "lingua.content.v1",
        "schema_version": 1,
        "content_id": str(item.content_id),
        "language": item.language,
        "kind": payload.kind,
        "title": item.title,
        "body": item.body,
        "risk_tier": item.risk_tier,
        "provenance": provenance.model_dump(mode="json"),
        "dependencies": [str(dependency) for dependency in item.dependencies],
    }
    content_hash = canonical_content_hash(canonical)
    return ContentItem(
        content_id=item.content_id,
        language=item.language,
        kind=payload.kind,
        title=item.title,
        body=item.body,
        content_hash=content_hash,
        lifecycle=content_lifecycle(item.lifecycle),  # type: ignore[arg-type]
        risk_tier=item.risk_tier,
        provenance=provenance,
        reviews=content_reviews(
            risk_tier=item.risk_tier,
            lifecycle=item.lifecycle,
            reviews=item.reviews,
            content_hash=content_hash,
        ),
        dependencies=item.dependencies,
    )


def _declared(manifest: PackManifest, prefix: str) -> tuple[str, ...]:
    return tuple(sorted(name for name in manifest.files if name.startswith(prefix)))


def _load_knowledge(root: Path, manifest: PackManifest) -> tuple[ResolvedItem, ...]:
    items: list[ResolvedItem] = []
    for number, payload in _read_jsonl(root / KNOWLEDGE_SEED, relative=KNOWLEDGE_SEED):
        where = f"{KNOWLEDGE_SEED}:{number}"
        record = _validate_model(PackKnowledgeItem, payload, where=where)
        items.append(
            _resolve_item(
                manifest,
                content_kind=KNOWLEDGE_KIND,
                stable_key=record.stable_key,
                title=record.title,
                body=record.body,
                provenance=record.provenance,
                payload=record,
                where=where,
            )
        )
    return tuple(items)


def _load_examples(root: Path, manifest: PackManifest) -> tuple[ResolvedItem, ...]:
    if EXAMPLES_SEED not in manifest.files:
        return ()
    items: list[ResolvedItem] = []
    for number, payload in _read_jsonl(root / EXAMPLES_SEED, relative=EXAMPLES_SEED):
        where = f"{EXAMPLES_SEED}:{number}"
        record = _validate_model(PackExample, payload, where=where)
        items.append(
            _resolve_item(
                manifest,
                content_kind=EXAMPLE_KIND,
                stable_key=record.stable_key,
                title=record.item_key,
                body=record.text,
                provenance=record.provenance,
                payload=record,
                where=where,
            )
        )
    return tuple(items)


def _load_relations(root: Path, manifest: PackManifest) -> tuple[PackRelation, ...]:
    if RELATIONS_SEED not in manifest.files:
        return ()
    return tuple(
        _validate_model(PackRelation, payload, where=f"{RELATIONS_SEED}:{number}")
        for number, payload in _read_jsonl(root / RELATIONS_SEED, relative=RELATIONS_SEED)
    )


def _load_descriptors(root: Path, manifest: PackManifest) -> tuple[ResolvedItem, ...]:
    items: list[ResolvedItem] = []
    for relative in _declared(manifest, PROFICIENCY_PREFIX):
        document = _validate_model(
            PackProficiencyFile, _read_json(root / relative, relative=relative), where=relative
        )
        if document.framework not in manifest.framework_by_id:
            raise _fail(
                "pack_framework_unknown",
                f"{relative} declares a framework the manifest does not support",
                [f"framework: {document.framework}"],
            )
        for descriptor in document.descriptors:
            items.append(
                _resolve_item(
                    manifest,
                    content_kind=DESCRIPTOR_KIND,
                    stable_key=descriptor.stable_key,
                    title=descriptor_title(
                        document.framework, descriptor.level, descriptor.dimension
                    ),
                    body=descriptor.descriptor,
                    provenance=descriptor.provenance,
                    payload=descriptor,
                    where=f"{relative}#{descriptor.stable_key}",
                    context=file_context(document, "descriptors"),
                )
            )
    return tuple(items)


def _load_assessments(
    root: Path, manifest: PackManifest
) -> tuple[tuple[ResolvedItem, ...], tuple[PackAssessmentFile, ...]]:
    items: list[ResolvedItem] = []
    forms: list[PackAssessmentFile] = []
    for relative in _declared(manifest, ASSESSMENTS_PREFIX):
        document = _validate_model(
            PackAssessmentFile, _read_json(root / relative, relative=relative), where=relative
        )
        forms.append(document)
        for task in document.tasks:
            items.append(
                _resolve_item(
                    manifest,
                    content_kind=TASK_KIND,
                    stable_key=task.stable_key,
                    title=task_title(document.form_key, task.dimension, task.level),
                    body=task.prompt,
                    provenance=task.provenance,
                    payload=task,
                    where=f"{relative}#{task.stable_key}",
                    context=file_context(document, "tasks"),
                )
            )
    return tuple(items), tuple(forms)


def _load_activities(root: Path, manifest: PackManifest) -> tuple[ResolvedItem, ...]:
    items: list[ResolvedItem] = []
    for relative in _declared(manifest, ACTIVITIES_PREFIX):
        document = _validate_model(
            PackActivityFile, _read_json(root / relative, relative=relative), where=relative
        )
        for template in document.templates:
            items.append(
                _resolve_item(
                    manifest,
                    content_kind=ACTIVITY_KIND,
                    stable_key=template.stable_key,
                    title=template.title,
                    body=activity_body(template.structure),
                    provenance=template.provenance,
                    payload=template,
                    where=f"{relative}#{template.stable_key}",
                    context=file_context(document, "templates"),
                )
            )
    return tuple(items)


def _load_recommendations(root: Path, manifest: PackManifest) -> tuple[ResolvedItem, ...]:
    items: list[ResolvedItem] = []
    for relative in _declared(manifest, REFERENCES_PREFIX):
        document = _validate_model(
            PackReferencesFile, _read_json(root / relative, relative=relative), where=relative
        )
        for recommendation in document.recommendations:
            items.append(
                _resolve_item(
                    manifest,
                    content_kind=RECOMMENDATION_KIND,
                    stable_key=recommendation.stable_key,
                    title=recommendation.title,
                    body=recommendation.locator or recommendation.title,
                    provenance=recommendation.provenance,
                    payload=recommendation,
                    where=f"{relative}#{recommendation.stable_key}",
                    context=file_context(document, "recommendations"),
                )
            )
    return tuple(items)


def _load_bundles(
    root: Path, manifest: PackManifest
) -> tuple[tuple[ResolvedItem, ...], dict[str, PackBundleFile]]:
    items: list[ResolvedItem] = []
    documents: dict[str, PackBundleFile] = {}
    for declaration in manifest.bundles:
        relative = declaration.file
        if relative not in manifest.files:
            raise _fail(
                "pack_bundle_missing",
                f"{declaration.bundle_key} names a file the manifest does not checksum",
                [f"file: {relative}"],
            )
        document = _validate_model(
            PackBundleFile, _read_json(root / relative, relative=relative), where=relative
        )
        if document.bundle_key != declaration.bundle_key:
            raise _fail(
                "pack_bundle_mismatch",
                f"{relative} declares bundle {document.bundle_key}, not {declaration.bundle_key}",
                [],
            )
        if document.framework not in manifest.framework_by_id:
            # Checked here, as it already is for a proficiency file. Without it, install
            # searched the manifest for the bundle's framework and raised a bare
            # StopIteration from inside the transaction -- a crash rather than a refusal.
            raise _fail(
                "pack_framework_unknown",
                f"{relative} declares a framework the manifest does not support",
                [f"framework: {document.framework}"],
            )
        if tuple(document.depends_on) != tuple(declaration.depends_on):
            raise _fail(
                "pack_bundle_mismatch",
                f"{relative} and the manifest disagree about {document.bundle_key}'s dependencies",
                [
                    f"manifest: {list(declaration.depends_on)}",
                    f"bundle: {list(document.depends_on)}",
                ],
            )
        documents[document.bundle_key] = document
        items.append(
            _resolve_item(
                manifest,
                content_kind=BUNDLE_KIND,
                stable_key=document.bundle_key,
                title=document.title,
                body=bundle_body(document.bundle_type, document.framework, document.level),
                provenance=document.provenance,
                payload=document,
                where=relative,
            )
        )
    return tuple(items), documents


def _reference_problems(pack: LoadedPack) -> list[str]:
    """Every internal reference a pack makes that does not resolve."""

    problems: list[str] = []
    index = pack.item_index()
    knowledge_keys = {item.stable_key for item in pack.knowledge}
    levels = {level for framework in pack.manifest.frameworks for level in framework.levels}
    dimensions = set(pack.manifest.dimensions)
    themes = set(pack.manifest.themes)
    for relation in pack.relations:
        if relation.source_key not in knowledge_keys:
            problems.append(f"relation source does not resolve: {relation.source_key}")
        if relation.target_key is not None and relation.target_key not in knowledge_keys:
            problems.append(f"relation target does not resolve: {relation.target_key}")
    for item in pack.examples:
        example = item.payload
        assert isinstance(example, PackExample)
        if example.item_key not in knowledge_keys:
            problems.append(f"example {item.stable_key} targets unknown {example.item_key}")
    for item in pack.knowledge:
        knowledge = item.payload
        assert isinstance(knowledge, PackKnowledgeItem)
        for level in (knowledge.level, knowledge.level_max):
            if level is not None and level not in levels:
                problems.append(f"{item.stable_key} uses undeclared level {level}")
        for theme in knowledge.themes:
            if theme not in themes:
                problems.append(f"{item.stable_key} uses undeclared theme {theme}")
    for item in pack.tasks:
        task = item.payload
        assert isinstance(task, PackAssessmentTask)
        if task.dimension not in dimensions:
            problems.append(f"{item.stable_key} tests undeclared dimension {task.dimension}")
        if task.level not in levels:
            problems.append(f"{item.stable_key} uses undeclared level {task.level}")
        if task.modality not in pack.manifest.modalities:
            problems.append(f"{item.stable_key} needs undeclared modality {task.modality}")
        for target in task.target_keys:
            if target not in knowledge_keys:
                problems.append(f"{item.stable_key} targets unknown {target}")
    for item in pack.descriptors:
        descriptor = item.payload
        assert isinstance(descriptor, PackDescriptor)
        if descriptor.dimension not in dimensions:
            problems.append(
                f"{item.stable_key} describes undeclared dimension {descriptor.dimension}"
            )
        if descriptor.level not in levels:
            problems.append(f"{item.stable_key} uses undeclared level {descriptor.level}")
    for item in pack.activities:
        activity = item.payload
        assert isinstance(activity, PackActivityTemplate)
        if activity.level not in levels:
            problems.append(f"{item.stable_key} uses undeclared level {activity.level}")
    for item in pack.recommendations:
        recommendation = item.payload
        assert isinstance(recommendation, PackSourceRecommendation)
        if recommendation.modality not in pack.manifest.modalities:
            problems.append(
                f"{item.stable_key} recommends undeclared modality {recommendation.modality}"
            )
        if recommendation.level not in levels:
            problems.append(f"{item.stable_key} uses undeclared level {recommendation.level}")
    for bundle_key, document in pack.bundle_files.items():
        for entry in document.items:
            if (entry.item_kind, entry.item_ref) not in index:
                problems.append(
                    f"bundle {bundle_key} references missing {entry.item_kind} {entry.item_ref}"
                )
    for item in pack.items:
        for dependency in item.provenance.dependencies:
            if (
                dependency.kind == "content"
                and (
                    KNOWLEDGE_KIND,
                    dependency.reference,
                )
                not in index
            ):
                problems.append(
                    f"{item.stable_key} depends on missing knowledge {dependency.reference}"
                )
    return problems


def _identity_problems(items: Iterable[ResolvedItem]) -> list[str]:
    seen: dict[tuple[str, str], int] = {}
    ids: dict[str, str] = {}
    problems: list[str] = []
    for item in items:
        key = (item.content_kind, item.stable_key)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            problems.append(f"duplicate stable key: {item.content_kind}/{item.stable_key}")
        previous = ids.setdefault(str(item.content_id), item.stable_key)
        if previous != item.stable_key:
            problems.append(f"derived ID collision: {previous} and {item.stable_key}")
    return problems


def _integrity_problems(pack: LoadedPack) -> list[str]:
    problems: list[str] = []
    for item in pack.items:
        if not item.hash_matches:
            problems.append(
                f"{item.content_kind}/{item.stable_key}: content_hash is stale "
                f"(declared {item.declared_hash[:12]}, computed {item.content_hash[:12]})"
            )
        problems.extend(
            f"{item.content_kind}/{item.stable_key}: {problem}"
            for problem in gate_problems(
                risk_tier=item.risk_tier,
                lifecycle=item.lifecycle,
                reviews=item.reviews,
                origin_classes=[origin.origin_class for origin in item.origins],
                source_references=[origin.reference for origin in item.origins if origin.reference],
            )
        )
    return problems


def load_pack(root: str | Path) -> LoadedPack:
    """Read and fully validate a pack directory."""

    directory = Path(root).expanduser().resolve()
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise _fail(
            "pack_manifest_missing",
            f"{directory} does not contain {MANIFEST_NAME}",
            [str(directory)],
        )
    manifest = _validate_model(
        PackManifest, _read_json(manifest_path, relative=MANIFEST_NAME), where=MANIFEST_NAME
    )
    digests = directory_digests(directory)
    _assert_checksum_coverage(manifest, digests)
    address = pack_content_address(manifest, digests)
    warnings: list[str] = []
    if manifest.content_address is None:
        warnings.append(
            f"{manifest.pack_key} has no recorded content address; run 'pack publish' to stamp it"
        )
    elif manifest.content_address != address:
        raise _fail(
            "pack_content_address_mismatch",
            f"{manifest.pack_key} records a content address its files do not produce",
            [f"recorded: {manifest.content_address}", f"computed: {address}"],
        )
    capabilities = _validate_model(
        PackCapabilities,
        _read_json(directory / CAPABILITIES_NAME, relative=CAPABILITIES_NAME),
        where=CAPABILITIES_NAME,
    )
    source_policy = _validate_model(
        PackSourcePolicy,
        _read_json(directory / SOURCE_POLICY_NAME, relative=SOURCE_POLICY_NAME),
        where=SOURCE_POLICY_NAME,
    )
    expectations = (
        _validate_model(
            PackExpectations,
            _read_json(directory / EXPECTATIONS_NAME, relative=EXPECTATIONS_NAME),
            where=EXPECTATIONS_NAME,
        )
        if EXPECTATIONS_NAME in manifest.files
        else None
    )
    tasks, forms = _load_assessments(directory, manifest)
    bundles, bundle_files = _load_bundles(directory, manifest)
    pack = LoadedPack(
        root=directory,
        manifest=manifest,
        capabilities=capabilities,
        source_policy=source_policy,
        expectations=expectations,
        file_digests=digests,
        content_address=address,
        knowledge=_load_knowledge(directory, manifest),
        relations=_load_relations(directory, manifest),
        examples=_load_examples(directory, manifest),
        descriptors=_load_descriptors(directory, manifest),
        tasks=tasks,
        activities=_load_activities(directory, manifest),
        recommendations=_load_recommendations(directory, manifest),
        bundles=bundles,
        bundle_files=bundle_files,
        assessment_forms=forms,
        warnings=tuple(warnings),
    )
    problems = [
        *_identity_problems(pack.items),
        *_reference_problems(pack),
        *_integrity_problems(pack),
    ]
    if problems:
        raise _fail(
            "pack_invalid",
            f"{manifest.pack_key} {manifest.version} is not internally consistent",
            problems,
        )
    return pack


def resolve_pack_path(reference: str | Path) -> Path:
    """Resolve a pack reference: a directory, or a pack key shipped with the core."""

    candidate = Path(str(reference)).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    text = str(reference)
    if "/" in text or "\\" in text or text in {".", ".."}:
        raise _fail("pack_not_found", f"no pack directory at {reference}", [text])
    from linguawiki import resources

    bundled = resources.language_packs_directory() / PurePosixPath(text)
    if bundled.is_dir():
        return bundled.resolve()
    raise _fail(
        "pack_not_found",
        f"no pack directory or bundled pack named {reference}",
        [f"searched: {resources.language_packs_directory()}"],
    )


__all__ = [
    "ACTIVITY_KIND",
    "BUNDLE_KIND",
    "DESCRIPTOR_KIND",
    "EXAMPLE_KIND",
    "KNOWLEDGE_KIND",
    "MANIFEST_NAME",
    "RECOMMENDATION_KIND",
    "TASK_KIND",
    "LoadedPack",
    "PackError",
    "ResolvedItem",
    "activity_body",
    "bundle_body",
    "content_id_for",
    "descriptor_title",
    "directory_digests",
    "file_context",
    "item_content_hash",
    "load_pack",
    "pack_content_address",
    "pack_item_hash",
    "relation_id_for",
    "resolve_pack_path",
    "resolved_content_item",
    "task_title",
]
