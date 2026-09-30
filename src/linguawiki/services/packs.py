"""Pack validation, installation, diff, update, listing, coverage, and publication.

Installation is a single transaction and is keyed by *derived* identity: a pack's row ID
comes from its `pack_key` and each item's content ID from `(pack_key, kind, stable_key)`.
Two consequences matter:

- reinstalling the same version is a no-op that still proves the bytes match, and
- an update reconciles the installed rows in place, so learner annotations, evidence, and
  track state that reference a pack item survive the upgrade.

A workspace pins one version of a pack at a time, which is why the registry holds one row
per pack key and version history lives in the event log rather than in duplicate rows.
An item that disappears from a newer pack is deprecated, never deleted: learner state may
still point at it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from pydantic import Field

from linguawiki.clock import Clock, SystemClock
from linguawiki.contracts import (
    OriginClass,
    PackActivityFile,
    PackActivityTemplate,
    PackAlias,
    PackAssessmentFile,
    PackAssessmentTask,
    PackBundleFile,
    PackCapabilities,
    PackDescriptor,
    PackExample,
    PackExpectations,
    PackFramework,
    PackItemOrigin,
    PackItemReview,
    PackKnowledgeItem,
    PackManifest,
    PackMaturity,
    PackProficiencyFile,
    PackReferencesFile,
    PackSourceClass,
    PackSourcePolicy,
    PackSourceRecommendation,
    ReviewerKind,
)
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import (
    Database,
    open_reader,
    open_writer,
    quote_identifier,
)
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import AssessmentId, EventId, PackId
from linguawiki.models import ContractModel
from linguawiki.packs import coverage as coverage_module
from linguawiki.packs.format import (
    KNOWLEDGE_KIND,
    LoadedPack,
    ResolvedItem,
    content_id_for,
    directory_digests,
    load_pack,
    pack_content_address,
    relation_id_for,
    resolve_pack_path,
)
from linguawiki.paths import WorkspacePaths
from linguawiki.provenance import PROMOTED_LIFECYCLES
from linguawiki.text import normalize_alias

MANIFEST_FILE = "manifest.json"
CAPABILITIES_FILE = "capabilities.json"
SOURCE_POLICY_FILE = "source-policy.json"
EXPECTATIONS_FILE = "tests/expectations.json"


class PackSummary(ContractModel):
    pack_id: str
    pack_key: str
    pack_name: str
    language_tag: str
    version: str
    checksum: str
    maturity: str
    framework_id: str | None
    status: str
    installed_at: str
    item_counts: dict[str, int] = Field(default_factory=dict)


class PackValidationReport(ContractModel):
    pack: str
    pack_key: str
    pack_name: str
    version: str
    language: str
    maturity: str
    content_address: str
    declared_content_address: str | None
    bands: tuple[str, ...]
    frameworks: tuple[str, ...]
    dimensions: tuple[str, ...]
    counts: dict[str, int]
    ok: bool
    expectation_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class CoverageRequirementReport(ContractModel):
    name: str
    band: str | None
    required: str
    observed: str
    met: bool
    gap: str | None = None


class PackCoverageReport(ContractModel):
    pack_key: str
    version: str
    declared_maturity: str
    declared_maturity_supported: bool
    highest_supported_maturity: str
    bands: tuple[str, ...]
    counts: dict[str, int]
    per_band: dict[str, dict[str, int]]
    themes: dict[str, int]
    provenance: dict[str, int]
    review_debt: dict[str, int]
    unresolved_items: tuple[str, ...]
    descriptor_coverage: dict[str, int]
    support_languages: dict[str, int]
    prerequisite_connectivity: dict[str, int]
    orphan_targets: tuple[str, ...]
    requirements: dict[str, tuple[CoverageRequirementReport, ...]]
    supported_onboarding_modes: tuple[str, ...]
    gaps: tuple[str, ...]
    expectation_failures: tuple[str, ...]
    ok: bool


class PackItemChange(ContractModel):
    content_kind: str
    stable_key: str
    change: str
    installed_hash: str | None = None
    incoming_hash: str | None = None
    dependents: tuple[str, ...] = ()


class TrackLevelConflict(ContractModel):
    """A level a bound track still uses that the incoming pack no longer teaches."""

    track_id: str
    framework_id: str
    field: str
    level: str


class TrackFrameworkConflict(ContractModel):
    """A framework a bound track is taught in that the incoming pack no longer declares."""

    track_id: str
    framework_id: str


class FrameworkRebinding(ContractModel):
    """A framework-scoped record the incoming pack places under a different framework."""

    record: str
    identity: str
    stable_key: str
    installed_framework: str
    incoming_framework: str


class TaskReformConflict(ContractModel):
    """A task moved to a different assessment form that learner state already used."""

    stable_key: str
    content_id: str
    installed_definition: str
    incoming_definition: str


class PackDiffReport(ContractModel):
    pack_key: str
    installed_version: str | None
    installed_checksum: str | None
    incoming_version: str
    incoming_checksum: str
    maturity_change: str | None = None
    added: tuple[PackItemChange, ...] = ()
    changed: tuple[PackItemChange, ...] = ()
    removed: tuple[PackItemChange, ...] = ()
    unchanged: int = 0
    invalidated_learner_content: tuple[str, ...] = ()
    #: Levels bound tracks still use that this update would withdraw. A non-empty tuple
    #: is a refusal: the tracks have to be resolved before the update can be applied.
    level_conflicts: tuple[TrackLevelConflict, ...] = ()
    #: Frameworks bound tracks are taught in that this update would withdraw entirely.
    #: Also a refusal, and independent of whether those tracks name any level at all.
    framework_conflicts: tuple[TrackFrameworkConflict, ...] = ()
    #: Framework-scoped records this update would silently re-point at a different
    #: framework while keeping their derived identity. A refusal.
    framework_rebindings: tuple[FrameworkRebinding, ...] = ()
    #: Tasks this update moves to a different form, but which a learner has already been
    #: served or scored on. A refusal: the move cannot be applied without rewriting a row
    #: their history references.
    task_reforms: tuple[TaskReformConflict, ...] = ()
    identical: bool = False


class PackInstallReport(ContractModel):
    pack_id: str
    pack_key: str
    pack_name: str
    version: str
    checksum: str
    maturity: str
    language_tag: str
    frameworks: tuple[str, ...]
    created: bool
    reinstalled: bool = False
    updated_from: str | None = None
    item_counts: dict[str, int] = Field(default_factory=dict)
    diff: PackDiffReport | None = None
    dry_run: bool = False
    warnings: tuple[str, ...] = ()


class PackPublishReport(ContractModel):
    pack: str
    pack_key: str
    version: str
    maturity: str
    content_address: str
    stamped: bool
    files: int
    warnings: tuple[str, ...] = ()


def _summary_counts(pack: LoadedPack) -> dict[str, int]:
    return {
        "knowledge": len(pack.knowledge),
        "examples": len(pack.examples),
        "relations": len(pack.relations),
        "descriptors": len(pack.descriptors),
        "assessment_tasks": len(pack.tasks),
        "activity_templates": len(pack.activities),
        "source_recommendations": len(pack.recommendations),
        "bundles": len(pack.bundles),
    }


def pack_row_id(pack_key: str) -> PackId:
    """A pack's registry ID, derived so an update keeps the same row."""

    return PackId.derive(pack_key)


def definition_id_for(pack_key: str, form_key: str, version: int) -> AssessmentId:
    return AssessmentId.derive(pack_key, "assessment-form", form_key, str(version))


class PackScaffoldReport(ContractModel):
    pack: str
    pack_key: str
    version: str
    maturity: str
    files: tuple[str, ...]
    warnings: tuple[str, ...] = ()


#: The dimensions and their kinds a new pack starts from. They are written into the pack
#: file for the author to edit, never assumed by core: `dimension_kinds` is what decides
#: which task types and budgets apply, and only the pack may set it.
SCAFFOLD_DIMENSIONS: Mapping[str, str] = {
    "reading": "receptive",
    "listening": "receptive",
    "vocabulary-control": "form",
    "grammar-control": "form",
    "writing": "productive",
    "spoken-production": "productive",
    "pronunciation": "pronunciation",
}
SCAFFOLD_MODALITIES = ("text", "audio", "speech", "writing")


def scaffold(
    target: str | Path,
    *,
    pack_key: str,
    name: str,
    language: str,
    framework_id: str,
    framework_name: str,
    framework_version: str,
    levels: Sequence[str],
    bands: Sequence[str],
    themes: Sequence[str],
    support_languages: Sequence[str] = (),
    maintainers: Sequence[str] = ("pack author",),
    license_name: str = "CC-BY-4.0",
) -> PackScaffoldReport:
    """Create the declared pack structure and nothing else.

    A scaffolded pack is a `fixture`: it validates, installs nowhere near a learner, and
    has to *earn* every stronger maturity through measured coverage. That is the honest
    starting point, and it is why the scaffold writes no content.
    """

    root = Path(str(target)).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise LinguaWikiError(
            "pack_target_not_empty",
            f"{root} already contains files; scaffold into a new directory",
            details=(ErrorDetail(field="path", reason="target is not empty"),),
        )
    if not bands:
        raise LinguaWikiError("invalid_arguments", "declare at least one band to cover")
    if not themes:
        raise LinguaWikiError("invalid_arguments", "declare at least one theme")
    manifest = PackManifest(
        pack_key=pack_key,
        name=name,
        version="0.0.1",
        language=language,
        license=license_name,
        maintainers=tuple(maintainers),
        maturity=PackMaturity.FIXTURE,
        frameworks=(
            PackFramework(
                framework_id=framework_id,
                name=framework_name,
                version=framework_version,
                levels=tuple(levels),
            ),
        ),
        bands=tuple(bands),
        dimensions=tuple(SCAFFOLD_DIMENSIONS),
        dimension_kinds=dict(SCAFFOLD_DIMENSIONS),  # type: ignore[arg-type]
        modalities=SCAFFOLD_MODALITIES,
        support_languages=tuple(support_languages),
        themes=tuple(themes),
        origin_profiles={
            "authored": (
                PackItemOrigin(
                    origin_class=OriginClass.HUMAN_AUTHORED,
                    transformation="written for this pack",
                    rights=f"{license_name} pack content",
                    privacy="public",
                ),
            )
        },
        review_profiles={
            "unreviewed": {
                "linguistic": PackItemReview(
                    state="unreviewed", reviewer_kind=ReviewerKind.NOT_APPLICABLE
                ),
                "pedagogical": PackItemReview(
                    state="unreviewed", reviewer_kind=ReviewerKind.NOT_APPLICABLE
                ),
                "source-alignment": PackItemReview(
                    state="unchecked", reviewer_kind=ReviewerKind.NOT_APPLICABLE
                ),
                "rights": PackItemReview(
                    state="unknown", reviewer_kind=ReviewerKind.NOT_APPLICABLE
                ),
                "privacy": PackItemReview(
                    state="private", reviewer_kind=ReviewerKind.NOT_APPLICABLE
                ),
            }
        },
    )
    documents: dict[str, ContractModel] = {
        CAPABILITIES_FILE: PackCapabilities(
            word_segmentation="whitespace", inflection=False, grammatical_case=False
        ),
        SOURCE_POLICY_FILE: PackSourcePolicy(
            source_classes=(
                PackSourceClass(
                    source_class="normative-reference",
                    rank=1,
                    description="Replace this with the authoritative references for this language.",
                ),
            ),
            escalation=("Describe when a claim needs a rank 1 source.",),
            prohibited=("Describe what may never be copied into this pack.",),
        ),
        f"proficiency/{framework_id}.json": PackProficiencyFile(
            framework=framework_id, descriptors=()
        ),
        "activities/activities.json": PackActivityFile(templates=()),
        "references/sources.json": PackReferencesFile(recommendations=()),
        EXPECTATIONS_FILE: PackExpectations(
            maturity=PackMaturity.FIXTURE,
            refuses_maturity=(
                PackMaturity.PILOT,
                PackMaturity.ONBOARDING_READY,
                PackMaturity.PLACEMENT_READY,
            ),
        ),
    }
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for relative, document in documents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                document.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(relative)
    for relative in ("seed/knowledge.jsonl", "seed/relations.jsonl", "seed/examples.jsonl"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        written.append(relative)
    digests = directory_digests(root)
    stamped = manifest.model_copy(update={"files": digests})
    stamped = stamped.model_copy(update={"content_address": pack_content_address(stamped, digests)})
    (root / MANIFEST_FILE).write_text(
        json.dumps(stamped.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    written.append(MANIFEST_FILE)
    return PackScaffoldReport(
        pack=str(root),
        pack_key=pack_key,
        version=stamped.version,
        maturity=str(stamped.maturity),
        files=tuple(sorted(written)),
        warnings=(
            "a scaffolded pack is a fixture: add content, stamp it, and let 'pack coverage' "
            "decide which maturity it may claim",
            "edit capabilities.json and source-policy.json before authoring: the scaffold "
            "guesses neither the language's structure nor its authoritative sources",
        ),
    )


def validate(reference: str | Path) -> PackValidationReport:
    """Load and fully validate a pack directory without touching a database."""

    root = resolve_pack_path(reference)
    pack = load_pack(root)
    report = coverage_module.coverage_report(pack)
    return PackValidationReport(
        pack=str(root),
        pack_key=pack.pack_key,
        pack_name=pack.manifest.name,
        version=pack.manifest.version,
        language=pack.manifest.language,
        maturity=str(pack.manifest.maturity),
        content_address=pack.content_address,
        declared_content_address=pack.manifest.content_address,
        bands=pack.manifest.bands,
        frameworks=tuple(pack.manifest.framework_by_id),
        dimensions=pack.manifest.dimensions,
        counts=_summary_counts(pack),
        ok=not report.expectation_failures and report.declared_maturity_supported,
        expectation_failures=report.expectation_failures,
        warnings=(
            *pack.warnings,
            *(
                ()
                if report.declared_maturity_supported
                else (
                    f"declared maturity {report.declared_maturity} is not supported by measured "
                    f"coverage; the highest supported level is "
                    f"{report.highest_supported_maturity}",
                )
            ),
        ),
    )


def coverage(reference: str | Path) -> PackCoverageReport:
    """Measure a pack's coverage and report both counts and qualitative gaps."""

    pack = load_pack(resolve_pack_path(reference))
    report = coverage_module.coverage_report(pack)
    return PackCoverageReport(
        pack_key=report.pack_key,
        version=report.version,
        declared_maturity=report.declared_maturity,
        declared_maturity_supported=report.declared_maturity_supported,
        highest_supported_maturity=report.highest_supported_maturity,
        bands=report.bands,
        counts=dict(report.counts),
        per_band={band: dict(values) for band, values in report.per_band.items()},
        themes=dict(report.themes),
        provenance=dict(report.provenance),
        review_debt=dict(report.review_debt),
        unresolved_items=report.unresolved_items,
        descriptor_coverage=dict(report.descriptor_coverage),
        support_languages=dict(report.support_languages),
        prerequisite_connectivity=dict(report.prerequisite_connectivity),
        orphan_targets=report.orphan_targets,
        requirements={
            level: tuple(
                CoverageRequirementReport(
                    name=requirement.name,
                    band=requirement.band,
                    required=requirement.required,
                    observed=requirement.observed,
                    met=requirement.met,
                    gap=requirement.gap,
                )
                for requirement in requirements
            )
            for level, requirements in report.requirements.items()
        },
        supported_onboarding_modes=coverage_module.supported_onboarding_modes(
            report.declared_maturity
        ),
        gaps=report.gaps,
        expectation_failures=report.expectation_failures,
        ok=report.declared_maturity_supported and not report.expectation_failures,
    )


def publish(reference: str | Path, *, maturity: str | None = None) -> PackPublishReport:
    """Stamp file checksums and the content address after the maturity gate passes.

    Publication is the only writer of `files` and `content_address`, so a pack cannot
    claim an address its own contents do not produce.
    """

    root = resolve_pack_path(reference)
    manifest_path = root / MANIFEST_FILE
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    draft = PackManifest.model_validate(payload)
    target = PackMaturity(maturity) if maturity is not None else PackMaturity(draft.maturity)
    digests = directory_digests(root)
    stamped = draft.model_copy(update={"files": digests, "content_address": None})
    stamped = stamped.model_copy(
        update={
            "maturity": target,
            "content_address": pack_content_address(
                stamped.model_copy(update={"maturity": target}), digests
            ),
        }
    )
    serialized = json.dumps(stamped.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(serialized, encoding="utf-8")
    try:
        pack = load_pack(root)
        unmet = coverage_module.unmet_requirements(pack, target)
        if unmet:
            raise LinguaWikiError(
                "pack_maturity_gate_failed",
                f"{pack.pack_key} cannot be published as {target}",
                details=tuple(
                    ErrorDetail(
                        field=requirement.name,
                        reason=f"required {requirement.required}, observed {requirement.observed}",
                        context={"band": requirement.band or "", "gap": requirement.gap or ""},
                    )
                    for requirement in unmet
                ),
            )
        report = coverage_module.coverage_report(pack)
        if report.expectation_failures:
            raise LinguaWikiError(
                "pack_expectations_failed",
                f"{pack.pack_key} fails its own declared expectations",
                details=tuple(
                    ErrorDetail(reason=failure) for failure in report.expectation_failures
                ),
            )
    except Exception:
        manifest_path.write_text(original, encoding="utf-8")
        raise
    return PackPublishReport(
        pack=str(root),
        pack_key=pack.pack_key,
        version=pack.manifest.version,
        maturity=str(target),
        content_address=pack.content_address,
        stamped=serialized != original,
        files=len(digests),
        warnings=pack.warnings,
    )


def _installed_pack(database: Database, pack_key: str) -> tuple[str, str, str, str] | None:
    row = database.one(
        "SELECT pack.pack_id, installation.version, installation.checksum, installation.maturity "
        "FROM language_packs pack "
        "JOIN pack_installations installation ON installation.pack_id = pack.pack_id "
        "WHERE pack.pack_key = ?",
        [pack_key],
    )
    return None if row is None else (str(row[0]), str(row[1]), str(row[2]), str(row[3]))


def _installed_items(database: Database, pack_id: str) -> dict[tuple[str, str], tuple[str, str]]:
    return {
        (str(kind), str(key)): (str(content_id), str(content_hash))
        for kind, key, content_id, content_hash in database.query(
            "SELECT content_kind, stable_key, content_id, content_hash FROM content_records "
            "WHERE pack_id = ?",
            [pack_id],
        )
    }


def _learner_dependents(database: Database, content_ids: Sequence[str]) -> dict[str, list[str]]:
    """Learner-owned content that depends on the given pack content."""

    if not content_ids:
        return {}
    placeholders = ", ".join("?" for _ in content_ids)
    dependents: dict[str, list[str]] = {}
    for reference, dependent in database.query(
        "SELECT dependency.dependency_ref, dependency.content_id "
        "FROM content_dependencies dependency "
        "JOIN content_records record ON record.content_id = dependency.content_id "
        f"WHERE dependency.dependency_kind = 'content' "
        f"AND dependency.dependency_ref IN ({placeholders}) "
        "AND record.track_id IS NOT NULL AND dependency.on_change <> 'ignore' "
        "ORDER BY 1, 2",
        list(content_ids),
    ):
        dependents.setdefault(str(reference), []).append(str(dependent))
    return dependents


TRACK_LEVEL_FIELDS = ("declared_level", "current_level", "target_level")


def _track_level_conflicts(
    database: Database, pack: LoadedPack, *, pack_id: str
) -> tuple[TrackLevelConflict, ...]:
    """Levels this update would withdraw while a bound track still names them.

    Withdrawing a level is not a content change that learner state can absorb: a track
    declaring C2 in a pack that stops teaching C2 keeps a label nothing can act on, and
    the next calibration quietly drops the declared prior on the floor. The update is
    refused so a person decides what those tracks should say instead.

    Only tracks the learner is still taught in are considered. An archived track keeps
    labels of the scale it was taught on, and the global framework record keeps those
    interpretable.
    """

    incoming = {
        framework.framework_id: set(framework.levels) for framework in pack.manifest.frameworks
    }
    withdrawn: dict[str, set[str]] = {}
    for framework_id, level_code in database.query(
        "SELECT framework_id, level_code FROM pack_framework_levels WHERE pack_id = ?",
        [pack_id],
    ):
        framework, level = str(framework_id), str(level_code)
        if level not in incoming.get(framework, set()):
            withdrawn.setdefault(framework, set()).add(level)
    if not withdrawn:
        return ()
    conflicts: list[TrackLevelConflict] = []
    for track_id, framework_id, declared, current, target in database.query(
        "SELECT track_id, proficiency_framework, declared_level, current_level, target_level "
        "FROM learning_tracks WHERE pack_id = ? AND status <> 'archived' ORDER BY track_id",
        [pack_id],
    ):
        lost = withdrawn.get(str(framework_id), set())
        for field, value in zip(TRACK_LEVEL_FIELDS, (declared, current, target), strict=True):
            if value is not None and str(value) in lost:
                conflicts.append(
                    TrackLevelConflict(
                        track_id=str(track_id),
                        framework_id=str(framework_id),
                        field=field,
                        level=str(value),
                    )
                )
    return tuple(conflicts)


def _track_framework_conflicts(
    database: Database, pack: LoadedPack, *, pack_id: str
) -> tuple[TrackFrameworkConflict, ...]:
    """Frameworks this update would withdraw while a bound track is taught in them.

    Checking the level fields was not enough. A track may legitimately name no level at
    all -- a learner who has declared nothing yet -- and such a track has no level to
    conflict, so dropping its entire framework passed the level guard untouched. The
    track then recorded a framework its own pack no longer declares, and the next run
    fell back to the framework's global level list: an order no installed pack vouches
    for. A framework is the more fundamental binding, so it is checked on its own terms.
    """

    declared = {framework.framework_id for framework in pack.manifest.frameworks}
    return tuple(
        TrackFrameworkConflict(track_id=str(track_id), framework_id=str(framework_id))
        for track_id, framework_id in database.query(
            # Archived tracks are history, not programmes. Holding an update hostage to
            # one made a framework transition impossible: archiving the old track was the
            # only remediation available, and it did not clear the refusal.
            "SELECT track_id, proficiency_framework FROM learning_tracks WHERE pack_id = ? "
            "AND status <> 'archived' ORDER BY track_id",
            [pack_id],
        )
        if str(framework_id) not in declared
    )


#: Framework-scoped records: the table, its key column, and its framework column.
FRAMEWORK_SCOPED_RECORDS = (
    ("proficiency_descriptors", "descriptor_id"),
    ("resource_bundles", "bundle_id"),
    ("assessment_definitions", "definition_id"),
)


def _incoming_framework_bindings(pack: LoadedPack) -> dict[tuple[str, str], tuple[str, str]]:
    """(table, identity) -> (framework the incoming pack places it under, stable key)."""

    bindings: dict[tuple[str, str], tuple[str, str]] = {}
    for item in pack.descriptors:
        descriptor = item.payload
        assert isinstance(descriptor, PackDescriptor)
        bindings[("proficiency_descriptors", str(item.content_id))] = (
            _descriptor_framework(pack, descriptor),
            item.stable_key,
        )
    for item in pack.bundles:
        document = item.payload
        assert isinstance(document, PackBundleFile)
        bindings[("resource_bundles", str(item.content_id))] = (
            document.framework,
            item.stable_key,
        )
    for form in pack.assessment_forms:
        identity = str(definition_id_for(pack.pack_key, form.form_key, form.version))
        bindings[("assessment_definitions", identity)] = (form.framework, form.form_key)
    return bindings


def _framework_rebindings(
    database: Database, pack: LoadedPack, *, pack_id: str
) -> tuple[FrameworkRebinding, ...]:
    """Framework-scoped records this update would re-point at a different framework.

    A descriptor, bundle, or assessment form belongs to one scale: its framework is part
    of what it *is*, which is why the installers deliberately never update the column --
    DuckDB would rewrite the row as a delete and an insert, and referencing rows refuse.
    The consequence was that renaming a pack's framework left these rows pointing at the
    old one while `pack_frameworks` named the new, and nothing noticed because serving
    does not read those columns.

    Since the stable keys already carry the framework by convention -- `cefr.a1.reading.en`,
    `cefr-a1-core` -- an item that changes framework should change key too, and then it is
    an ordinary added-and-removed pair with the old row deprecated and learner state kept.
    Keeping the key while changing the framework is the authoring error, so it is refused.
    """

    incoming = _incoming_framework_bindings(pack)
    rebindings: list[FrameworkRebinding] = []
    for table, key_column in FRAMEWORK_SCOPED_RECORDS:
        for identity, installed_framework in database.query(
            f"SELECT {quote_identifier(key_column)}, framework_id "
            f"FROM {quote_identifier(table)} WHERE pack_id = ? "
            f"ORDER BY {quote_identifier(key_column)}",
            [pack_id],
        ):
            found = incoming.get((table, str(identity)))
            if found is None or found[0] == str(installed_framework):
                continue
            rebindings.append(
                FrameworkRebinding(
                    record=table,
                    identity=str(identity),
                    stable_key=found[1],
                    installed_framework=str(installed_framework),
                    incoming_framework=found[0],
                )
            )
    return tuple(rebindings)


def _incoming_task_definitions(pack: LoadedPack) -> dict[str, tuple[str, str]]:
    """content_id -> (definition the incoming pack files it under, stable key)."""

    forms = {task.stable_key: form for form in pack.assessment_forms for task in form.tasks}
    filed: dict[str, tuple[str, str]] = {}
    for item in pack.tasks:
        form = forms.get(item.stable_key)
        if form is None:
            continue
        identity = str(definition_id_for(pack.pack_key, form.form_key, form.version))
        filed[str(item.content_id)] = (identity, item.stable_key)
    return filed


def _task_reform_conflicts(
    database: Database, pack: LoadedPack, *, pack_id: str
) -> tuple[TaskReformConflict, ...]:
    """Tasks refiled under a new form that a learner's history already references.

    `assessment_tasks.definition_id` is a foreign key, so DuckDB rewrites an update of it
    as a delete and an insert. That is why the upsert never touched it -- and why
    re-versioning a form used to leave the new form with no tasks at all while every task
    still pointed at the old one. The installer now moves an unreferenced task properly.
    A task a learner has already been served cannot be moved, because the row their run
    refers to would have to be rewritten, so that case is refused and wants a new key.
    """

    incoming = _incoming_task_definitions(pack)
    if not incoming:
        return ()
    conflicts: list[TaskReformConflict] = []
    for content_id, installed_definition in database.query(
        "SELECT task.content_id, task.definition_id FROM assessment_tasks task "
        "JOIN content_records item ON item.content_id = task.content_id "
        "WHERE item.pack_id = ? ORDER BY task.content_id",
        [pack_id],
    ):
        found = incoming.get(str(content_id))
        if found is None or found[0] == str(installed_definition):
            continue
        if not _task_is_referenced(database, str(content_id)):
            continue
        conflicts.append(
            TaskReformConflict(
                stable_key=found[1],
                content_id=str(content_id),
                installed_definition=str(installed_definition),
                incoming_definition=found[0],
            )
        )
    return tuple(conflicts)


def _task_is_referenced(database: Database, content_id: str) -> bool:
    """Whether a learner's own history points at this task row."""

    return bool(
        int(
            database.scalar(
                "SELECT (SELECT count(*) FROM assessment_run_tasks WHERE content_id = ?) "
                "+ (SELECT count(*) FROM assessment_item_exposures WHERE content_id = ?)",
                [content_id, content_id],
            )
        )
    )


def _diff(database: Database, pack: LoadedPack) -> PackDiffReport:
    installed = _installed_pack(database, pack.pack_key)
    incoming = {(item.content_kind, item.stable_key): item for item in pack.installable_items}
    if installed is None:
        return PackDiffReport(
            pack_key=pack.pack_key,
            installed_version=None,
            installed_checksum=None,
            incoming_version=pack.manifest.version,
            incoming_checksum=pack.content_address,
            added=tuple(
                PackItemChange(
                    content_kind=kind,
                    stable_key=key,
                    change="added",
                    incoming_hash=item.content_hash,
                )
                for (kind, key), item in sorted(incoming.items())
            ),
        )
    pack_id, version, checksum, maturity = installed
    current = _installed_items(database, pack_id)
    added: list[PackItemChange] = []
    changed: list[PackItemChange] = []
    removed: list[PackItemChange] = []
    unchanged = 0
    changed_ids: list[str] = []
    for key, item in sorted(incoming.items()):
        existing = current.get(key)
        if existing is None:
            added.append(
                PackItemChange(
                    content_kind=key[0],
                    stable_key=key[1],
                    change="added",
                    incoming_hash=item.content_hash,
                )
            )
        elif existing[1] != item.content_hash:
            changed_ids.append(existing[0])
            changed.append(
                PackItemChange(
                    content_kind=key[0],
                    stable_key=key[1],
                    change="changed",
                    installed_hash=existing[1],
                    incoming_hash=item.content_hash,
                )
            )
        else:
            unchanged += 1
    for key, existing in sorted(current.items()):
        if key not in incoming:
            # A removal is the change most likely to break something a learner built:
            # the item it points at is going away entirely, not merely being reworded.
            changed_ids.append(existing[0])
            removed.append(
                PackItemChange(
                    content_kind=key[0],
                    stable_key=key[1],
                    change="removed",
                    installed_hash=existing[1],
                )
            )
    dependents = _learner_dependents(database, changed_ids)

    def with_dependents(entry: PackItemChange) -> PackItemChange:
        content_id = str(current[(entry.content_kind, entry.stable_key)][0])
        return entry.model_copy(update={"dependents": tuple(dependents.get(content_id, ()))})

    changed = [with_dependents(entry) for entry in changed]
    removed = [with_dependents(entry) for entry in removed]
    return PackDiffReport(
        pack_key=pack.pack_key,
        installed_version=version,
        installed_checksum=checksum,
        incoming_version=pack.manifest.version,
        incoming_checksum=pack.content_address,
        maturity_change=(
            None
            if maturity == str(pack.manifest.maturity)
            else f"{maturity} -> {pack.manifest.maturity}"
        ),
        added=tuple(added),
        changed=tuple(changed),
        removed=tuple(removed),
        unchanged=unchanged,
        invalidated_learner_content=tuple(
            sorted({dependent for values in dependents.values() for dependent in values})
        ),
        level_conflicts=_track_level_conflicts(database, pack, pack_id=pack_id),
        framework_conflicts=_track_framework_conflicts(database, pack, pack_id=pack_id),
        framework_rebindings=_framework_rebindings(database, pack, pack_id=pack_id),
        task_reforms=_task_reform_conflicts(database, pack, pack_id=pack_id),
        identical=(
            checksum == pack.content_address
            and version == pack.manifest.version
            and not added
            and not changed
            and not removed
        ),
    )


FRAMEWORK_IDENTITY_FIELDS = ("name", "version", "source")


def _assert_framework_levels(database: Database, pack: LoadedPack) -> None:
    """Refuse a pack that describes an installed framework differently.

    A framework ID names one scale. Its level order, its name, its version, and its
    source are all part of that identity, and none of them may be renegotiated by a later
    pack: a track records only the ID, so a silent change to what the ID means would
    restate every level label already recorded against it. `cefr` at a new edition is a
    new scale and belongs under a new ID.

    Read-only, and separate from registration, because every path into `install` owes the
    caller this answer -- a preview, an ordinary install, and a reinstall that changes
    nothing all have to report the clash rather than skip the check.
    """

    for framework in pack.manifest.frameworks:
        recorded_identity = database.one(
            "SELECT name, version, source FROM proficiency_frameworks WHERE framework_id = ?",
            [framework.framework_id],
        )
        if recorded_identity is not None:
            declared = (framework.name, framework.version, framework.source)
            differing = [
                (field, "" if was is None else str(was), "" if now is None else str(now))
                for field, was, now in zip(
                    FRAMEWORK_IDENTITY_FIELDS, recorded_identity, declared, strict=True
                )
                if ("" if was is None else str(was)) != ("" if now is None else str(now))
            ]
            if differing:
                raise LinguaWikiError(
                    "framework_identity_conflict",
                    f"{framework.framework_id} is already installed as "
                    + ", ".join(f"{field} {was!r}" for field, was, _ in differing)
                    + "; a framework ID names one scale, so publish a changed scale under a "
                    "new framework ID rather than redefining this one",
                    details=tuple(
                        ErrorDetail(
                            field=framework.framework_id,
                            reason=f"{field} differs",
                            context={"installed": was, "incoming": now},
                        )
                        for field, was, now in differing
                    ),
                )
        for sequence, level in enumerate(framework.levels, start=1):
            recorded = database.one(
                "SELECT sequence FROM proficiency_framework_levels WHERE framework_id = ? "
                "AND level_code = ?",
                [framework.framework_id, level],
            )
            if recorded is not None and int(recorded[0]) != sequence:
                raise LinguaWikiError(
                    "framework_level_conflict",
                    f"{framework.framework_id} already orders {level} differently; a framework's "
                    "level sequence is part of its identity",
                    details=(
                        ErrorDetail(
                            field=framework.framework_id,
                            reason="level sequence differs",
                            context={
                                "level": level,
                                "installed": str(recorded[0]),
                                "incoming": str(sequence),
                            },
                        ),
                    ),
                )


def _register_pack_framework_levels(database: Database, pack: LoadedPack, *, pack_id: str) -> None:
    """Record which levels *this pack* teaches, replacing whatever it declared before.

    The global `proficiency_framework_levels` table carries shared framework identity --
    the level order every pack must agree on -- and can only grow, because an order once
    agreed cannot be renegotiated. Membership is a claim of the pack, so a pack that
    narrows its range from A1..C2 to A1..C1 has to stop offering C2, and that is what
    these rows say.
    """

    database.execute("DELETE FROM pack_framework_levels WHERE pack_id = ?", [pack_id])
    for framework in pack.manifest.frameworks:
        for sequence, level in enumerate(framework.levels, start=1):
            database.execute(
                "INSERT INTO pack_framework_levels (pack_id, framework_id, level_code, "
                "sequence) VALUES (?, ?, ?, ?)",
                [pack_id, framework.framework_id, level, sequence],
            )


def _register_frameworks(database: Database, pack: LoadedPack) -> None:
    _assert_framework_levels(database, pack)
    now = database.now()
    for framework in pack.manifest.frameworks:
        # Identity was already checked by `_assert_framework_levels`, so an existing row
        # is known to agree with the manifest and needs no update.
        existing = database.one(
            "SELECT framework_id FROM proficiency_frameworks WHERE framework_id = ?",
            [framework.framework_id],
        )
        if existing is None:
            database.execute(
                "INSERT INTO proficiency_frameworks (framework_id, name, version, source, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                [
                    framework.framework_id,
                    framework.name,
                    framework.version,
                    framework.source,
                    now,
                ],
            )
        for sequence, level in enumerate(framework.levels, start=1):
            recorded = database.one(
                "SELECT sequence FROM proficiency_framework_levels WHERE framework_id = ? "
                "AND level_code = ?",
                [framework.framework_id, level],
            )
            if recorded is None:
                database.execute(
                    "INSERT INTO proficiency_framework_levels (framework_id, level_code, "
                    "sequence) VALUES (?, ?, ?)",
                    [framework.framework_id, level, sequence],
                )


def _write_content_record(
    database: Database, item: ResolvedItem, *, pack_key: str, pack_id: str, replace: bool
) -> None:
    now = database.now()
    if replace:
        database.execute(
            "UPDATE content_records SET content_hash = ?, lifecycle = ?, risk_tier = ?, "
            "language_tag = ?, quarantined = FALSE, invalidation_reason = NULL, updated_at = ? "
            "WHERE content_id = ?",
            [
                item.content_hash,
                item.lifecycle,
                item.risk_tier,
                item.language,
                now,
                str(item.content_id),
            ],
        )
        database.execute("DELETE FROM content_origins WHERE content_id = ?", [str(item.content_id)])
        database.execute("DELETE FROM content_reviews WHERE content_id = ?", [str(item.content_id)])
        database.execute(
            "DELETE FROM content_dependencies WHERE content_id = ?", [str(item.content_id)]
        )
    else:
        database.execute(
            "INSERT INTO content_records (content_id, content_kind, pack_id, track_id, "
            "stable_key, language_tag, content_hash, lifecycle, risk_tier, batch_id, "
            "quarantined, invalidation_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, FALSE, NULL, ?, ?)",
            [
                str(item.content_id),
                item.content_kind,
                pack_id,
                item.stable_key,
                item.language,
                item.content_hash,
                item.lifecycle,
                item.risk_tier,
                now,
                now,
            ],
        )
    for sequence, origin in enumerate(item.origins, start=1):
        database.execute(
            "INSERT INTO content_origins (content_id, sequence, origin_class, reference, "
            "locator, transformation, origin_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                str(item.content_id),
                sequence,
                str(origin.origin_class),
                origin.reference,
                origin.locator,
                origin.transformation,
                origin.origin_hash,
            ],
        )
    for axis, review in sorted(item.reviews.items()):
        database.execute(
            "INSERT INTO content_reviews (content_id, axis, state, reviewer_kind, reviewer, "
            "method, evidence_reference, reviewed_content_hash, reviewed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(item.content_id),
                axis,
                review.state,
                str(review.reviewer_kind),
                review.reviewer,
                review.method,
                review.evidence_reference,
                item.content_hash,
                now,
                now,
            ],
        )
    for sequence, dependency in enumerate(item.provenance.dependencies, start=1):
        # A content dependency names a knowledge stable key in the pack; store the
        # derived content ID so invalidation can walk it without re-deriving.
        reference = (
            str(content_id_for(pack_key, KNOWLEDGE_KIND, dependency.reference))
            if dependency.kind == "content"
            else dependency.reference
        )
        database.execute(
            "INSERT INTO content_dependencies (content_id, sequence, dependency_kind, "
            "dependency_ref, expected_hash, on_change) VALUES (?, ?, ?, ?, ?, ?)",
            [
                str(item.content_id),
                sequence,
                dependency.kind,
                reference,
                dependency.expected_hash,
                dependency.on_change,
            ],
        )


def _unique_aliases(aliases: Sequence[PackAlias]) -> tuple[PackAlias, ...]:
    """Aliases keyed the way the table keys them, so a repeat is not an error."""

    seen: dict[tuple[str, str], PackAlias] = {}
    for alias in aliases:
        seen.setdefault((alias.locale, alias.alias.casefold()), alias)
    return tuple(seen.values())


def _install_knowledge(database: Database, pack: LoadedPack) -> None:
    now = database.now()
    for item in pack.knowledge:
        knowledge = item.payload
        assert isinstance(knowledge, PackKnowledgeItem)
        database.execute(
            "INSERT INTO knowledge_items (content_id, language, kind, title, body, summary, "
            "level_min, level_max, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (content_id) DO UPDATE SET language = excluded.language, "
            "kind = excluded.kind, title = excluded.title, body = excluded.body, "
            "summary = excluded.summary, level_min = excluded.level_min, "
            "level_max = excluded.level_max, updated_at = excluded.updated_at",
            [
                str(item.content_id),
                item.language,
                knowledge.kind,
                knowledge.title,
                knowledge.body,
                knowledge.summary,
                knowledge.level,
                knowledge.level_max,
                now,
                now,
            ],
        )
        database.execute(
            "DELETE FROM knowledge_aliases WHERE content_id = ?", [str(item.content_id)]
        )
        for alias in _unique_aliases(knowledge.aliases):
            database.execute(
                "INSERT INTO knowledge_aliases (content_id, normalized, alias, script, locale) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    str(item.content_id),
                    normalize_alias(alias.alias),
                    alias.alias,
                    alias.script,
                    alias.locale,
                ],
            )
        database.execute("DELETE FROM item_tags WHERE content_id = ?", [str(item.content_id)])
        tags = [("theme", theme) for theme in knowledge.themes] + [
            ("feature", feature) for feature in knowledge.features
        ]
        tags.append(("level", knowledge.level))
        if knowledge.frequency_band is not None:
            tags.append(("frequency", knowledge.frequency_band))
        for tag_kind, tag_value in dict.fromkeys(tags):
            database.execute(
                "INSERT INTO item_tags (content_id, tag_kind, tag_value) VALUES (?, ?, ?)",
                [str(item.content_id), tag_kind, tag_value],
            )


def _install_relations(database: Database, pack: LoadedPack) -> None:
    now = database.now()
    database.execute(
        "DELETE FROM knowledge_relations WHERE source_content_id IN "
        "(SELECT content_id FROM content_records WHERE pack_id = ?)",
        [str(pack_row_id(pack.pack_key))],
    )
    for relation in pack.relations:
        source = content_id_for(pack.pack_key, KNOWLEDGE_KIND, relation.source_key)
        target = (
            str(content_id_for(pack.pack_key, KNOWLEDGE_KIND, relation.target_key))
            if relation.target_key is not None
            else None
        )
        database.execute(
            # A plain insert: the pack's own relations were deleted above, and the loader
            # already rejects a duplicate edge. `ON CONFLICT` here reported a false
            # duplicate, because DuckDB still sees the deleted row's entry in the
            # secondary unique index inside the same transaction.
            "INSERT INTO knowledge_relations (relation_id, source_content_id, relation_type, "
            "target_content_id, target_ref, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                relation_id_for(pack.pack_key, relation),
                str(source),
                relation.relation_type,
                target,
                relation.target_ref,
                now,
            ],
        )


def _install_examples(database: Database, pack: LoadedPack) -> None:
    now = database.now()
    for item in pack.examples:
        example = item.payload
        assert isinstance(example, PackExample)
        database.execute(
            "INSERT INTO examples (content_id, item_content_id, text, translation, gloss, "
            "locale, difficulty, audio_artifact, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (content_id) DO UPDATE SET item_content_id = excluded.item_content_id, "
            "text = excluded.text, translation = excluded.translation, gloss = excluded.gloss, "
            "locale = excluded.locale, difficulty = excluded.difficulty",
            [
                str(item.content_id),
                str(content_id_for(pack.pack_key, KNOWLEDGE_KIND, example.item_key)),
                example.text,
                example.translation,
                example.gloss,
                example.locale,
                example.difficulty,
                None,
                now,
            ],
        )


def _descriptor_framework(pack: LoadedPack, descriptor: PackDescriptor) -> str:
    """Which declared framework a descriptor's level belongs to.

    Shared by installation and by rebinding detection, so the two can never disagree
    about what framework an incoming descriptor is being placed under. It also replaces
    a bare `next`, which would have raised StopIteration inside the install transaction.
    """

    for framework in pack.manifest.frameworks:
        if descriptor.level in framework.levels:
            return framework.framework_id
    raise LinguaWikiError(
        "pack_framework_unknown",
        f"no declared framework has level {descriptor.level}, which descriptor "
        f"{descriptor.stable_key} claims",
        details=(ErrorDetail(field="level", reason="level is not in any declared framework"),),
    )


def _install_descriptors(database: Database, pack: LoadedPack, *, pack_id: str) -> None:
    now = database.now()
    for item in pack.descriptors:
        descriptor = item.payload
        assert isinstance(descriptor, PackDescriptor)
        framework_id = _descriptor_framework(pack, descriptor)
        database.execute(
            "INSERT INTO proficiency_descriptors (descriptor_id, pack_id, framework_id, "
            "level_code, dimension, locale, descriptor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (descriptor_id) DO UPDATE SET level_code = excluded.level_code, "
            "dimension = excluded.dimension, locale = excluded.locale, "
            "descriptor = excluded.descriptor",
            [
                str(item.content_id),
                pack_id,
                framework_id,
                descriptor.level,
                descriptor.dimension,
                descriptor.locale,
                descriptor.descriptor,
                now,
            ],
        )


def _install_activities(database: Database, pack: LoadedPack, *, pack_id: str) -> None:
    now = database.now()
    for item in pack.activities:
        template = item.payload
        assert isinstance(template, PackActivityTemplate)
        database.execute(
            "INSERT INTO activity_templates (template_id, pack_id, mode, title, level_code, "
            "minutes, structure_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (template_id) DO UPDATE SET mode = excluded.mode, "
            "title = excluded.title, level_code = excluded.level_code, "
            "minutes = excluded.minutes, structure_json = excluded.structure_json",
            [
                str(item.content_id),
                pack_id,
                template.mode,
                template.title,
                template.level,
                template.minutes,
                json.dumps(template.structure, ensure_ascii=False, sort_keys=True),
                now,
            ],
        )


def _install_recommendations(database: Database, pack: LoadedPack, *, pack_id: str) -> None:
    now = database.now()
    for item in pack.recommendations:
        recommendation = item.payload
        assert isinstance(recommendation, PackSourceRecommendation)
        database.execute(
            "INSERT INTO source_recommendations (recommendation_id, pack_id, modality, title, "
            "creator, locator, level_code, license, rights_status, support_language, notes, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (recommendation_id) DO UPDATE SET modality = excluded.modality, "
            "title = excluded.title, creator = excluded.creator, locator = excluded.locator, "
            "level_code = excluded.level_code, license = excluded.license, "
            "rights_status = excluded.rights_status, support_language = excluded.support_language, "
            "notes = excluded.notes",
            [
                str(item.content_id),
                pack_id,
                recommendation.modality,
                recommendation.title,
                recommendation.creator,
                recommendation.locator,
                recommendation.level,
                recommendation.license,
                recommendation.rights_status,
                recommendation.support_language,
                recommendation.notes,
                now,
            ],
        )


def _install_bundles(database: Database, pack: LoadedPack, *, pack_id: str) -> None:
    now = database.now()
    for item in pack.bundles:
        document = item.payload
        assert isinstance(document, PackBundleFile)
        # The loader has already refused a bundle naming an undeclared framework.
        framework_id = pack.manifest.framework_by_id[document.framework].framework_id
        # The bundle's own row carries an outgoing foreign key, so DuckDB rewrites an
        # update of it as a delete and an insert; its item rows have to be gone first.
        database.execute(
            "DELETE FROM resource_bundle_items WHERE bundle_id = ?", [str(item.content_id)]
        )
        database.execute(
            "INSERT INTO resource_bundles (bundle_id, pack_id, bundle_key, bundle_type, "
            "framework_id, level_code, title, license, dependencies_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            # framework_id is deliberately not updated: a bundle's framework is part of
            # its identity, and DuckDB rewrites an update that touches a foreign-key
            # column as a delete and an insert, which the bundle's item rows refuse.
            "ON CONFLICT (bundle_id) DO UPDATE SET bundle_type = excluded.bundle_type, "
            "level_code = excluded.level_code, title = excluded.title, "
            "license = excluded.license, dependencies_json = excluded.dependencies_json",
            [
                str(item.content_id),
                pack_id,
                document.bundle_key,
                document.bundle_type,
                framework_id,
                document.level,
                document.title,
                document.license,
                json.dumps(list(document.depends_on), sort_keys=True),
                now,
            ],
        )
        for sequence, entry in enumerate(document.items, start=1):
            database.execute(
                "INSERT INTO resource_bundle_items (bundle_id, sequence, item_kind, item_ref) "
                "VALUES (?, ?, ?, ?)",
                [
                    str(item.content_id),
                    sequence,
                    entry.item_kind,
                    str(content_id_for(pack.pack_key, entry.item_kind, entry.item_ref)),
                ],
            )


def _install_assessments(database: Database, pack: LoadedPack, *, pack_id: str) -> None:
    now = database.now()
    task_forms: dict[str, PackAssessmentFile] = {}
    live_definitions: list[str] = []
    for form in pack.assessment_forms:
        definition_id = definition_id_for(pack.pack_key, form.form_key, form.version)
        live_definitions.append(str(definition_id))
        database.execute(
            "INSERT INTO assessment_definitions (definition_id, pack_id, form_key, version, "
            "purpose, framework_id, level_min, level_max, dimensions_json, title, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?) "
            # framework_id is deliberately not updated: it is part of the form's identity,
            # and DuckDB cannot rewrite a foreign-key column on a row that tasks reference.
            "ON CONFLICT (definition_id) DO UPDATE SET purpose = excluded.purpose, "
            "level_min = excluded.level_min, level_max = excluded.level_max, "
            "dimensions_json = excluded.dimensions_json, title = excluded.title, "
            "status = 'active'",
            [
                str(definition_id),
                pack_id,
                form.form_key,
                form.version,
                form.purpose,
                form.framework,
                form.level_min,
                form.level_max,
                json.dumps(sorted({task.dimension for task in form.tasks})),
                form.title,
                now,
            ],
        )
        for task in form.tasks:
            task_forms[task.stable_key] = form
    # A form the pack has stopped shipping is superseded, not deleted: its tasks and any
    # run that used it still refer to it, and the record of what was once asked has to
    # survive. Marking it is what lets `db check` tell an abandoned form from a live one.
    if live_definitions:
        placeholders = ", ".join("?" for _ in live_definitions)
        database.execute(
            "UPDATE assessment_definitions SET status = 'superseded' WHERE pack_id = ? "
            f"AND definition_id NOT IN ({placeholders})",
            [pack_id, *live_definitions],
        )
    else:
        database.execute(
            "UPDATE assessment_definitions SET status = 'superseded' WHERE pack_id = ?",
            [pack_id],
        )
    for item in pack.tasks:
        payload = item.payload
        assert isinstance(payload, PackAssessmentTask)
        task = payload
        form = task_forms[task.stable_key]
        # A task's form is a foreign key, so an update of it would be rewritten as a
        # delete and an insert. Deleting the row first is what actually moves the task, so
        # a re-versioned form does not end up with no tasks at all. `_diff` has already
        # refused the case where a learner's history references the row.
        recorded_definition = database.one(
            "SELECT definition_id FROM assessment_tasks WHERE content_id = ?",
            [str(item.content_id)],
        )
        if recorded_definition is not None and str(recorded_definition[0]) != str(
            definition_id_for(pack.pack_key, form.form_key, form.version)
        ):
            database.execute(
                "DELETE FROM assessment_tasks WHERE content_id = ?", [str(item.content_id)]
            )
        database.execute(
            "INSERT INTO assessment_tasks (content_id, definition_id, dimension, task_type, "
            "level_code, difficulty, content_family, modality, prompt, rubric_version, "
            "rubric_json, expected_json, presentation_json, permitted_help, is_anchor, "
            "target_refs_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (content_id) DO UPDATE SET dimension = excluded.dimension, "
            "task_type = excluded.task_type, level_code = excluded.level_code, "
            "difficulty = excluded.difficulty, content_family = excluded.content_family, "
            "modality = excluded.modality, prompt = excluded.prompt, "
            "rubric_version = excluded.rubric_version, rubric_json = excluded.rubric_json, "
            "expected_json = excluded.expected_json, "
            # Written on conflict as well as on insert. An upgrade that removes a task's
            # presentation has to clear the column: leaving the previous pack's choices
            # standing behind a task that no longer has them makes the bank disagree
            # with the pack it says it was installed from.
            "presentation_json = excluded.presentation_json, "
            "permitted_help = excluded.permitted_help, "
            "is_anchor = excluded.is_anchor, target_refs_json = excluded.target_refs_json",
            [
                str(item.content_id),
                str(definition_id_for(pack.pack_key, form.form_key, form.version)),
                task.dimension,
                task.task_type,
                task.level,
                task.difficulty,
                task.content_family,
                task.modality,
                task.prompt,
                task.rubric_version,
                json.dumps(task.rubric, ensure_ascii=False, sort_keys=True),
                # A task with no key stores `{}`, the column's own default: the bank
                # column is NOT NULL, and `expected_json` there has always meant
                # "nothing to compare against" for a rubric-scored task.
                json.dumps(
                    {} if task.expected is None else task.expected.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                # NULL rather than `{}`: this column is nullable and `{}` would be a
                # presentation record with no kind. `expected_json` uses `{}` only
                # because its column is NOT NULL and `{}` has always meant "nothing to
                # compare against" there.
                None
                if task.presentation is None
                else json.dumps(
                    task.presentation.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                task.permitted_help,
                task.is_anchor,
                json.dumps(
                    [
                        str(content_id_for(pack.pack_key, KNOWLEDGE_KIND, key))
                        for key in task.target_keys
                    ]
                ),
                now,
            ],
        )


def _deprecate_removed(
    database: Database, *, pack_id: str, removed: Iterable[PackItemChange], version: str
) -> None:
    """Never delete a pack item a learner may already reference."""

    now = database.now()
    for entry in removed:
        database.execute(
            "UPDATE content_records SET lifecycle = 'deprecated', invalidation_reason = ?, "
            "updated_at = ? WHERE pack_id = ? AND content_kind = ? AND stable_key = ?",
            [
                f"absent from pack version {version}",
                now,
                pack_id,
                entry.content_kind,
                entry.stable_key,
            ],
        )


def _invalidate_dependents(database: Database, dependents: Sequence[str], *, reason: str) -> None:
    now = database.now()
    for content_id in dependents:
        database.execute(
            "UPDATE content_records SET lifecycle = 'needs-review', invalidation_reason = ?, "
            "updated_at = ? WHERE content_id = ?",
            [reason, now, content_id],
        )


def _write_pack_rows(
    database: Database, pack: LoadedPack, *, pack_id: str, exists: bool, source: Path
) -> None:
    now = database.now()
    framework_id = pack.manifest.frameworks[0].framework_id
    capabilities = json.dumps(
        pack.capabilities.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    manifest_json = json.dumps(
        pack.manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    source_policy = json.dumps(
        pack.source_policy.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    if exists:
        installed_language = database.scalar(
            "SELECT language_tag FROM language_packs WHERE pack_id = ?", [pack_id]
        )
        if str(installed_language) != pack.manifest.language:
            raise LinguaWikiError(
                "pack_language_conflict",
                f"{pack.pack_key} is installed for {installed_language}; a pack's language tag "
                "is part of its identity and cannot change between versions",
                details=(
                    ErrorDetail(
                        field="language",
                        reason="language tag differs",
                        context={
                            "installed": str(installed_language),
                            "incoming": pack.manifest.language,
                        },
                    ),
                ),
            )
        database.execute(
            "UPDATE pack_installations SET pack_name = ?, version = ?, checksum = ?, "
            "maturity = ?, framework_id = ?, capabilities_json = ?, manifest_json = ?, "
            "source_policy_json = ?, status = 'installed', source_path = ?, updated_at = ? "
            "WHERE pack_id = ?",
            [
                pack.manifest.name,
                pack.manifest.version,
                pack.content_address,
                str(pack.manifest.maturity),
                framework_id,
                capabilities,
                manifest_json,
                source_policy,
                str(source),
                now,
                pack_id,
            ],
        )
    else:
        database.execute(
            "INSERT INTO language_packs (pack_id, pack_key, language_tag, created_at) "
            "VALUES (?, ?, ?, ?)",
            [pack_id, pack.pack_key, pack.manifest.language, now],
        )
        database.execute(
            "INSERT INTO pack_installations (pack_id, pack_name, version, checksum, maturity, "
            "framework_id, capabilities_json, manifest_json, source_policy_json, status, "
            "source_path, installed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'installed', ?, ?, ?)",
            [
                pack_id,
                pack.manifest.name,
                pack.manifest.version,
                pack.content_address,
                str(pack.manifest.maturity),
                framework_id,
                capabilities,
                manifest_json,
                source_policy,
                str(source),
                now,
                now,
            ],
        )
    database.execute("DELETE FROM pack_files WHERE pack_id = ?", [pack_id])
    for relative, digest in sorted(pack.file_digests.items()):
        database.execute(
            "INSERT INTO pack_files (pack_id, relative_path, sha256, byte_count) "
            "VALUES (?, ?, ?, ?)",
            [pack_id, relative, digest, (pack.root / relative).stat().st_size],
        )
    database.execute("DELETE FROM pack_frameworks WHERE pack_id = ?", [pack_id])
    for framework in pack.manifest.frameworks:
        database.execute(
            "INSERT INTO pack_frameworks (pack_id, framework_id) VALUES (?, ?)",
            [pack_id, framework.framework_id],
        )


def _already_installed_unchanged(
    database: Database, pack: LoadedPack, *, pack_id: str, difference: PackDiffReport, source: Path
) -> bool:
    """Whether this exact pack is already installed, byte for byte.

    A reinstall of the same version at the same content address used to rewrite every
    content record, re-deprecate, and re-invalidate on the way to reporting "identical".
    Rewriting a row is not free of consequence -- it is the operation that re-binds
    reviews and touches `updated_at` on rows learner state points at -- so the honest
    answer is to verify and then do nothing. The file digests are checked as well as the
    content address, because the address covers the items and not every shipped file.
    """

    if not difference.identical:
        return False
    recorded_source = database.scalar(
        "SELECT coalesce(source_path, '') FROM pack_installations WHERE pack_id = ?", [pack_id]
    )
    if str(recorded_source) != str(source):
        # Same contents, different directory. The recorded source is what later commands
        # re-resolve the pack from, so this reinstall does have work to do.
        return False
    stored = {
        str(path): str(digest)
        for path, digest in database.query(
            "SELECT relative_path, sha256 FROM pack_files WHERE pack_id = ? ORDER BY 1",
            [pack_id],
        )
    }
    return stored == dict(pack.file_digests)


def install(
    paths: WorkspacePaths,
    reference: str | Path,
    *,
    clock: Clock | None = None,
    allow_update: bool = False,
    dry_run: bool = False,
    command: str = "pack.install",
) -> PackInstallReport:
    """Install or update a pack in one transaction, keyed by derived identity."""

    active_clock = clock or SystemClock()
    root = resolve_pack_path(reference)
    pack = load_pack(root)
    pack_id = str(pack_row_id(pack.pack_key))
    with open_writer(paths, command=command, clock=active_clock) as database:
        installed = _installed_pack(database, pack.pack_key)
        difference = _diff(database, pack)
        if installed is not None:
            _, version, checksum, _ = installed
            if version == pack.manifest.version and checksum != pack.content_address:
                raise LinguaWikiError(
                    "pack_version_conflict",
                    f"{pack.pack_key} {version} is already installed with different contents; "
                    "a published pack version is immutable, so publish a new version",
                    details=(
                        ErrorDetail(
                            field="checksum",
                            reason="content address differs",
                            context={"installed": checksum, "incoming": pack.content_address},
                        ),
                    ),
                )
            if version != pack.manifest.version and not allow_update:
                raise LinguaWikiError(
                    "pack_update_required",
                    f"{pack.pack_key} {version} is installed; use 'pack update' so the change "
                    "is previewed before it is applied",
                    details=(
                        ErrorDetail(
                            field="version",
                            reason="a different version is installed",
                            context={"installed": version, "incoming": pack.manifest.version},
                        ),
                    ),
                )
        _assert_framework_levels(database, pack)
        if difference.task_reforms and not dry_run:
            raise LinguaWikiError(
                "pack_task_reform_blocked",
                f"{pack.pack_key} {pack.manifest.version} files "
                f"{len(difference.task_reforms)} task(s) under a different assessment form, "
                "but a learner has already been served or scored on them; give the refiled "
                "tasks new stable keys so their history keeps pointing at what was asked",
                details=tuple(
                    ErrorDetail(
                        field="assessment_tasks",
                        reason=f"{conflict.stable_key} is filed under "
                        f"{conflict.installed_definition} and a learner's history uses it",
                        context={
                            "stable_key": conflict.stable_key,
                            "installed": conflict.installed_definition,
                            "incoming": conflict.incoming_definition,
                        },
                    )
                    for conflict in difference.task_reforms[:20]
                ),
            )
        if difference.framework_rebindings and not dry_run:
            raise LinguaWikiError(
                "pack_framework_rebinding",
                f"{pack.pack_key} {pack.manifest.version} keeps the identity of "
                f"{len(difference.framework_rebindings)} framework-scoped record(s) while "
                "placing them under a different framework; a descriptor, bundle, or form "
                "belongs to one scale, so give the moved records a new identity -- a new "
                "stable key, or for an assessment form a new version",
                details=tuple(
                    ErrorDetail(
                        field=rebinding.record,
                        reason=f"{rebinding.stable_key} is installed under "
                        f"{rebinding.installed_framework} and this version places it under "
                        f"{rebinding.incoming_framework}",
                        context={
                            "record": rebinding.record,
                            "stable_key": rebinding.stable_key,
                            "installed": rebinding.installed_framework,
                            "incoming": rebinding.incoming_framework,
                        },
                    )
                    for rebinding in difference.framework_rebindings[:20]
                ),
            )
        if difference.framework_conflicts and not dry_run:
            affected = {conflict.track_id for conflict in difference.framework_conflicts}
            raise LinguaWikiError(
                "pack_frameworks_in_use",
                f"{pack.pack_key} {pack.manifest.version} no longer declares framework(s) "
                f"{sorted({c.framework_id for c in difference.framework_conflicts})}, which "
                f"{len(affected)} active track(s) are taught in; a track cannot be moved "
                "between frameworks, so archive those tracks, apply this update, then create "
                "their replacements against a framework this version declares",
                details=tuple(
                    ErrorDetail(
                        field="proficiency_framework",
                        reason=f"{conflict.track_id} is taught in {conflict.framework_id}, "
                        "which this version no longer declares",
                        context={
                            "track": conflict.track_id,
                            "framework": conflict.framework_id,
                        },
                    )
                    for conflict in difference.framework_conflicts[:20]
                ),
            )
        if difference.level_conflicts and not dry_run:
            raise LinguaWikiError(
                "pack_levels_in_use",
                f"{pack.pack_key} {pack.manifest.version} withdraws framework level(s) that "
                f"{len({conflict.track_id for conflict in difference.level_conflicts})} "
                "active track(s) still use; change those tracks to levels the new pack "
                "teaches -- or archive them -- then apply the update",
                details=tuple(
                    ErrorDetail(
                        field=conflict.field,
                        reason=f"{conflict.track_id} uses {conflict.framework_id} "
                        f"{conflict.level}, which this version no longer teaches",
                        context={
                            "track": conflict.track_id,
                            "framework": conflict.framework_id,
                            "level": conflict.level,
                        },
                    )
                    for conflict in difference.level_conflicts[:20]
                ),
            )
        unchanged_install = installed is not None and _already_installed_unchanged(
            database, pack, pack_id=pack_id, difference=difference, source=root
        )
        if dry_run or unchanged_install:
            return PackInstallReport(
                pack_id=pack_id,
                pack_key=pack.pack_key,
                pack_name=pack.manifest.name,
                version=pack.manifest.version,
                checksum=pack.content_address,
                maturity=str(pack.manifest.maturity),
                language_tag=pack.manifest.language,
                frameworks=tuple(pack.manifest.framework_by_id),
                created=installed is None,
                reinstalled=difference.identical,
                updated_from=None if installed is None else installed[1],
                item_counts=_summary_counts(pack),
                diff=difference,
                dry_run=dry_run,
                warnings=(
                    pack.warnings
                    if dry_run
                    else (
                        *pack.warnings,
                        f"{pack.pack_key} {pack.manifest.version} is already installed at this "
                        "content address; nothing was changed",
                    )
                ),
            )
        correlation_id = EventId.new()
        with database.transaction() as transaction:
            _register_frameworks(transaction, pack)
            _write_pack_rows(
                transaction, pack, pack_id=pack_id, exists=installed is not None, source=root
            )
            # After the registry row exists: these rows reference it.
            _register_pack_framework_levels(transaction, pack, pack_id=pack_id)
            existing_items = _installed_items(transaction, pack_id)
            for item in pack.installable_items:
                _write_content_record(
                    transaction,
                    item,
                    pack_key=pack.pack_key,
                    pack_id=pack_id,
                    replace=(item.content_kind, item.stable_key) in existing_items,
                )
            _install_knowledge(transaction, pack)
            _install_relations(transaction, pack)
            _install_examples(transaction, pack)
            _install_descriptors(transaction, pack, pack_id=pack_id)
            _install_activities(transaction, pack, pack_id=pack_id)
            _install_recommendations(transaction, pack, pack_id=pack_id)
            _install_bundles(transaction, pack, pack_id=pack_id)
            _install_assessments(transaction, pack, pack_id=pack_id)
            _deprecate_removed(
                transaction,
                pack_id=pack_id,
                removed=difference.removed,
                version=pack.manifest.version,
            )
            _invalidate_dependents(
                transaction,
                difference.invalidated_learner_content,
                reason=(f"a dependency changed in {pack.pack_key} {pack.manifest.version}"),
            )
            migration_module.record_audit_entry(
                transaction,
                command=command,
                correlation_id=correlation_id,
                outcome="succeeded",
                affected_records_json=json.dumps([pack_id]),
                before_summary=None if installed is None else f"version {installed[1]}",
                after_summary=f"version {pack.manifest.version}",
            )
            migration_module.record_domain_event(
                transaction,
                event_type="pack.installed" if installed is None else "pack.updated",
                aggregate_type="pack",
                aggregate_id=pack_id,
                correlation_id=correlation_id,
                payload_json=json.dumps(
                    {
                        "pack_key": pack.pack_key,
                        "version": pack.manifest.version,
                        "checksum": pack.content_address,
                        "maturity": str(pack.manifest.maturity),
                    },
                    sort_keys=True,
                ),
                idempotency_key=(
                    f"pack.installed:{pack.pack_key}:{pack.manifest.version}:{pack.content_address}"
                    if difference.identical is False
                    else None
                ),
            )
    return PackInstallReport(
        pack_id=pack_id,
        pack_key=pack.pack_key,
        pack_name=pack.manifest.name,
        version=pack.manifest.version,
        checksum=pack.content_address,
        maturity=str(pack.manifest.maturity),
        language_tag=pack.manifest.language,
        frameworks=tuple(pack.manifest.framework_by_id),
        created=installed is None,
        reinstalled=difference.identical,
        updated_from=None if installed is None else installed[1],
        item_counts=_summary_counts(pack),
        diff=difference,
        warnings=pack.warnings,
    )


def diff(
    paths: WorkspacePaths, reference: str | Path, *, clock: Clock | None = None
) -> PackDiffReport:
    """Preview what installing this pack would change, without writing anything."""

    pack = load_pack(resolve_pack_path(reference))
    with open_reader(paths, clock=clock or SystemClock()) as database:
        return _diff(database, pack)


def listing(paths: WorkspacePaths, *, clock: Clock | None = None) -> tuple[PackSummary, ...]:
    """Every installed pack with its identity, maturity, and item counts."""

    with open_reader(paths, clock=clock or SystemClock()) as database:
        rows = database.query(
            "SELECT pack.pack_id, pack.pack_key, installation.pack_name, pack.language_tag, "
            "installation.version, installation.checksum, installation.maturity, "
            "installation.framework_id, installation.status, installation.installed_at "
            "FROM language_packs pack "
            "JOIN pack_installations installation ON installation.pack_id = pack.pack_id "
            "ORDER BY pack.pack_key"
        )
        summaries: list[PackSummary] = []
        for row in rows:
            pack_id = str(row[0])
            counts = {
                str(kind): int(count)
                for kind, count in database.query(
                    "SELECT content_kind, count(*) FROM content_records WHERE pack_id = ? "
                    "GROUP BY content_kind ORDER BY 1",
                    [pack_id],
                )
            }
            summaries.append(
                PackSummary(
                    pack_id=pack_id,
                    pack_key=str(row[1]),
                    pack_name=str(row[2]),
                    language_tag=str(row[3]),
                    version=str(row[4]),
                    checksum=str(row[5]),
                    maturity=str(row[6]),
                    framework_id=None if row[7] is None else str(row[7]),
                    status=str(row[8]),
                    installed_at=str(row[9]),
                    item_counts=counts,
                )
            )
        return tuple(summaries)


def installed_pack(database: Database, pack_key: str | None = None) -> Mapping[str, str]:
    """One installed pack's registry row, defaulting to the only installed pack."""

    selection = (
        "SELECT pack.pack_id, pack.pack_key, installation.version, installation.maturity, "
        "pack.language_tag, installation.framework_id, installation.manifest_json, "
        "installation.capabilities_json, coalesce(installation.source_path, ''), "
        "installation.checksum "
        "FROM language_packs pack "
        "JOIN pack_installations installation ON installation.pack_id = pack.pack_id "
    )
    if pack_key is None:
        rows = database.query(
            f"{selection} WHERE installation.status = 'installed' ORDER BY pack.pack_key"
        )
        if len(rows) != 1:
            raise LinguaWikiError(
                "pack_selection_required",
                f"name a pack explicitly: the workspace has {len(rows)} installed packs",
                details=(
                    ErrorDetail(
                        field="pack",
                        reason="ambiguous pack selection",
                        context={"installed": ", ".join(str(row[1]) for row in rows)},
                    ),
                ),
            )
        row = rows[0]
    else:
        found = database.one(f"{selection} WHERE pack.pack_key = ?", [pack_key])
        if found is None:
            raise LinguaWikiError(
                "pack_not_installed",
                f"no pack named {pack_key} is installed; run 'linguawiki pack install' first",
                details=(ErrorDetail(field="pack", reason="not installed"),),
            )
        row = found
    return {
        "pack_id": str(row[0]),
        "pack_key": str(row[1]),
        "version": str(row[2]),
        "maturity": str(row[3]),
        "language_tag": str(row[4]),
        "framework_id": "" if row[5] is None else str(row[5]),
        "manifest_json": str(row[6]),
        "capabilities_json": str(row[7]),
        "source_path": str(row[8]),
        "checksum": str(row[9]),
    }


def pack_manifest(database: Database, pack_key: str | None = None) -> PackManifest:
    """The manifest of an installed pack, read back from the registry."""

    row = installed_pack(database, pack_key)
    return PackManifest.model_validate(json.loads(row["manifest_json"]))


def promoted_content_ids(database: Database, pack_id: str, content_kind: str) -> tuple[str, ...]:
    placeholders = ", ".join("?" for _ in PROMOTED_LIFECYCLES)
    return tuple(
        str(content_id)
        for (content_id,) in database.query(
            "SELECT content_id FROM content_records WHERE pack_id = ? AND content_kind = ? "
            f"AND lifecycle IN ({placeholders}) ORDER BY stable_key",
            [pack_id, content_kind, *PROMOTED_LIFECYCLES],
        )
    )


__all__ = [
    "PackCoverageReport",
    "PackDiffReport",
    "PackInstallReport",
    "PackPublishReport",
    "PackScaffoldReport",
    "PackSummary",
    "PackValidationReport",
    "coverage",
    "diff",
    "install",
    "installed_pack",
    "listing",
    "pack_manifest",
    "pack_row_id",
    "promoted_content_ids",
    "publish",
    "scaffold",
    "validate",
]
