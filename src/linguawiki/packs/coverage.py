"""Pack coverage measurement and the maturity gates it feeds.

A maturity level is a promise about what onboarding may offer, so it is checked rather
than declared: `pack coverage` reports counts *and* the qualitative gaps, and
`maturity_gate` decides whether a pack may claim a level. Counts alone never pass a
pack, which is why every requirement carries its own gap text and the report keeps the
review-debt, provenance, connectivity, and theme distributions beside the totals.

Dimension classification comes from the manifest (`dimension_kinds`); the core must
never decide that a dimension called "reading" is receptive. Knowledge groupings use the
core `kind` vocabulary, which is core's own.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from linguawiki.contracts import (
    PackActivityTemplate,
    PackAssessmentTask,
    PackDescriptor,
    PackExample,
    PackKnowledgeItem,
    PackMaturity,
    PackSourceRecommendation,
)
from linguawiki.packs.format import LoadedPack, ResolvedItem
from linguawiki.provenance import PROMOTED_LIFECYCLES, REVIEW_AXES, required_strengths

#: Knowledge kinds grouped the way the coverage gates count them.
LEXICAL_KINDS = frozenset({"lexeme", "sense", "form", "concept"})
GRAMMAR_KINDS = frozenset({"grammar", "construction"})
SCRIPT_KINDS = frozenset({"pronunciation", "character"})
FUNCTIONAL_KINDS = frozenset({"pragmatics", "culture", "skill_strategy"})

OBJECTIVE_TASK_TYPES = frozenset({"objective", "short-response"})
PRODUCTIVE_TASK_TYPES = frozenset({"extended-productive"})
PRONUNCIATION_TASK_TYPES = frozenset({"pronunciation-target"})
CONNECTED_SPEECH_TYPES = frozenset({"connected-speech"})

#: Per-band thresholds from the pack-maturity gates in the implementation plan.
PILOT_THRESHOLDS = {
    "knowledge": 60,
    "themes_min": 4,
    "themes_max": 6,
    "grammar": 15,
    "script": 10,
    "objective_tasks": 24,
    "productive_prompts": 8,
    "activity_templates": 6,
    "activity_modes": 4,
    "recommendations_per_modality": 1,
}
ONBOARDING_THRESHOLDS = {
    "lexical": 300,
    "grammar": 60,
    "script": 25,
    "functional": 20,
    "examples_per_productive_target": 2,
    "templates_per_mode": 8,
    "recommendations_per_modality": 3,
}
PLACEMENT_THRESHOLDS = {
    "objective_per_dimension": 24,
    "families_per_dimension": 4,
    "productive_per_dimension": 12,
    "pronunciation_targets": 12,
    "connected_speech_prompts": 6,
    "alternate_forms": 3,
}
#: Ordered so a gate can require everything a weaker level required.
MATURITY_ORDER = (
    PackMaturity.FIXTURE,
    PackMaturity.PILOT,
    PackMaturity.ONBOARDING_READY,
    PackMaturity.PLACEMENT_READY,
)


@dataclass(frozen=True, slots=True)
class Requirement:
    """One measured coverage requirement and whether this pack meets it."""

    name: str
    band: str | None
    required: str
    observed: str
    met: bool
    gap: str | None = None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    pack_key: str
    version: str
    declared_maturity: str
    bands: tuple[str, ...]
    counts: Mapping[str, int]
    per_band: Mapping[str, Mapping[str, int]]
    themes: Mapping[str, int]
    provenance: Mapping[str, int]
    review_debt: Mapping[str, int]
    unresolved_items: tuple[str, ...]
    descriptor_coverage: Mapping[str, int]
    support_languages: Mapping[str, int]
    prerequisite_connectivity: Mapping[str, int]
    orphan_targets: tuple[str, ...]
    requirements: Mapping[str, tuple[Requirement, ...]]
    declared_maturity_supported: bool
    highest_supported_maturity: str
    expectation_failures: tuple[str, ...]

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(
            f"{requirement.name}"
            + (f" [{requirement.band}]" if requirement.band else "")
            + f": {requirement.gap}"
            for requirements in self.requirements.values()
            for requirement in requirements
            if not requirement.met and requirement.gap
        )


def _knowledge(pack: LoadedPack) -> list[tuple[ResolvedItem, PackKnowledgeItem]]:
    return [
        (item, payload)
        for item in pack.knowledge
        if isinstance(payload := item.payload, PackKnowledgeItem)
    ]


def _tasks(pack: LoadedPack) -> list[tuple[ResolvedItem, PackAssessmentTask]]:
    return [
        (item, payload)
        for item in pack.tasks
        if isinstance(payload := item.payload, PackAssessmentTask)
    ]


def _is_promoted(item: ResolvedItem) -> bool:
    return item.lifecycle in PROMOTED_LIFECYCLES


def _review_debt(pack: LoadedPack) -> tuple[dict[str, int], tuple[str, ...]]:
    """Axes that are still short of what an item's own claim needs."""

    debt = dict.fromkeys(REVIEW_AXES, 0)
    unresolved: list[str] = []
    for item in pack.items:
        required = required_strengths(risk_tier=item.risk_tier, lifecycle=item.lifecycle)
        short = [axis for axis in REVIEW_AXES if required[axis] > 0 and axis not in item.reviews]
        for axis in short:
            debt[axis] += 1
        if not _is_promoted(item):
            unresolved.append(f"{item.content_kind}/{item.stable_key}")
    return debt, tuple(sorted(unresolved))


def _prerequisite_connectivity(pack: LoadedPack) -> tuple[dict[str, int], tuple[str, ...]]:
    """How connected the prerequisite graph is, and which targets stand alone."""

    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    for relation in pack.relations:
        if relation.relation_type != "prerequisite":
            continue
        outgoing[relation.source_key] += 1
        if relation.target_key is not None:
            incoming[relation.target_key] += 1
    keys = [item.stable_key for item in pack.knowledge]
    connected = [key for key in keys if incoming[key] or outgoing[key]]
    orphans = tuple(sorted(key for key in keys if not incoming[key] and not outgoing[key]))
    return (
        {
            "prerequisite_edges": sum(outgoing.values()),
            "connected_targets": len(connected),
            "isolated_targets": len(orphans),
        },
        orphans,
    )


def _support_language_coverage(pack: LoadedPack) -> dict[str, int]:
    """How much support-language material each declared support language has."""

    counts = dict.fromkeys(pack.manifest.support_languages, 0)
    for item in pack.examples:
        example = item.payload
        if isinstance(example, PackExample) and example.locale in counts:
            counts[example.locale] += 1
    for item in pack.descriptors:
        descriptor = item.payload
        if isinstance(descriptor, PackDescriptor) and descriptor.locale in counts:
            counts[descriptor.locale] += 1
    for item in pack.recommendations:
        recommendation = item.payload
        if (
            isinstance(recommendation, PackSourceRecommendation)
            and recommendation.support_language in counts
        ):
            counts[recommendation.support_language] += 1
    return counts


def _descriptor_coverage(pack: LoadedPack) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in pack.descriptors:
        descriptor = item.payload
        if isinstance(descriptor, PackDescriptor):
            counts[f"{descriptor.level}/{descriptor.dimension}"] += 1
    return dict(counts)


def _pilot_requirements(pack: LoadedPack, band: str) -> list[Requirement]:
    knowledge = [(item, payload) for item, payload in _knowledge(pack) if payload.level == band]
    promoted = [(item, payload) for item, payload in knowledge if _is_promoted(item)]
    themes = {theme for _, payload in promoted for theme in payload.themes}
    grammar = [payload for _, payload in promoted if payload.kind in GRAMMAR_KINDS]
    script = [payload for _, payload in promoted if payload.kind in SCRIPT_KINDS]
    tasks = [(item, payload) for item, payload in _tasks(pack) if payload.level == band]
    objective = [
        payload
        for item, payload in tasks
        if _is_promoted(item) and payload.task_type in OBJECTIVE_TASK_TYPES
    ]
    productive = [
        payload
        for item, payload in tasks
        if _is_promoted(item) and payload.task_type in PRODUCTIVE_TASK_TYPES
    ]
    templates = [
        payload
        for item in pack.activities
        if _is_promoted(item) and isinstance(payload := item.payload, PackActivityTemplate)
    ]
    modes = {payload.mode for payload in templates}
    modality_counts: Counter[str] = Counter()
    for item in pack.recommendations:
        recommendation = item.payload
        if _is_promoted(item) and isinstance(recommendation, PackSourceRecommendation):
            modality_counts[recommendation.modality] += 1
    missing_modalities = sorted(
        modality
        for modality in pack.manifest.modalities
        if modality_counts[modality] < PILOT_THRESHOLDS["recommendations_per_modality"]
    )
    return [
        Requirement(
            name="reviewed_knowledge_targets",
            band=band,
            required=f">= {PILOT_THRESHOLDS['knowledge']}",
            observed=str(len(promoted)),
            met=len(promoted) >= PILOT_THRESHOLDS["knowledge"],
            gap=f"{PILOT_THRESHOLDS['knowledge'] - len(promoted)} more reviewed targets needed",
        ),
        Requirement(
            name="practical_themes",
            band=band,
            required=(f"{PILOT_THRESHOLDS['themes_min']}-{PILOT_THRESHOLDS['themes_max']} themes"),
            observed=str(len(themes)),
            met=PILOT_THRESHOLDS["themes_min"] <= len(themes) <= PILOT_THRESHOLDS["themes_max"],
            gap=f"themes present: {sorted(themes)}",
        ),
        Requirement(
            name="grammar_targets",
            band=band,
            required=f">= {PILOT_THRESHOLDS['grammar']}",
            observed=str(len(grammar)),
            met=len(grammar) >= PILOT_THRESHOLDS["grammar"],
            gap=f"{PILOT_THRESHOLDS['grammar'] - len(grammar)} more grammar targets needed",
        ),
        Requirement(
            name="script_and_pronunciation_targets",
            band=band,
            required=f">= {PILOT_THRESHOLDS['script']}",
            observed=str(len(script)),
            met=len(script) >= PILOT_THRESHOLDS["script"],
            gap=f"{PILOT_THRESHOLDS['script'] - len(script)} more targets needed",
        ),
        Requirement(
            name="objective_diagnostic_items",
            band=band,
            required=f">= {PILOT_THRESHOLDS['objective_tasks']}",
            observed=str(len(objective)),
            met=len(objective) >= PILOT_THRESHOLDS["objective_tasks"],
            gap=f"{PILOT_THRESHOLDS['objective_tasks'] - len(objective)} more items needed",
        ),
        Requirement(
            name="productive_prompts",
            band=band,
            required=f">= {PILOT_THRESHOLDS['productive_prompts']} with rubrics",
            observed=str(len(productive)),
            met=len(productive) >= PILOT_THRESHOLDS["productive_prompts"],
            gap=(
                f"{PILOT_THRESHOLDS['productive_prompts'] - len(productive)} more rubric "
                "prompts needed"
            ),
        ),
        Requirement(
            name="activity_templates",
            band=None,
            required=(
                f">= {PILOT_THRESHOLDS['activity_templates']} across "
                f">= {PILOT_THRESHOLDS['activity_modes']} modes"
            ),
            observed=f"{len(templates)} across {len(modes)} modes",
            met=len(templates) >= PILOT_THRESHOLDS["activity_templates"]
            and len(modes) >= PILOT_THRESHOLDS["activity_modes"],
            gap=f"modes present: {sorted(modes)}",
        ),
        Requirement(
            name="source_recommendations_per_modality",
            band=None,
            required=f">= {PILOT_THRESHOLDS['recommendations_per_modality']} per modality",
            observed=", ".join(
                f"{modality}={modality_counts[modality]}" for modality in pack.manifest.modalities
            ),
            met=not missing_modalities,
            gap=f"modalities without a reviewed recommendation: {missing_modalities}",
        ),
    ]


def _onboarding_requirements(pack: LoadedPack, band: str) -> list[Requirement]:
    knowledge = [
        (item, payload)
        for item, payload in _knowledge(pack)
        if payload.level == band and _is_promoted(item)
    ]
    groups = {
        "lexical": [p for _, p in knowledge if p.kind in LEXICAL_KINDS],
        "grammar": [p for _, p in knowledge if p.kind in GRAMMAR_KINDS],
        "script": [p for _, p in knowledge if p.kind in SCRIPT_KINDS],
        "functional": [p for _, p in knowledge if p.kind in FUNCTIONAL_KINDS],
    }
    examples_by_item: Counter[str] = Counter()
    for item in pack.examples:
        example = item.payload
        if _is_promoted(item) and isinstance(example, PackExample):
            examples_by_item[example.item_key] += 1
    productive_targets = [
        payload.stable_key
        for _, payload in knowledge
        if payload.kind in (LEXICAL_KINDS | GRAMMAR_KINDS)
    ]
    minimum = ONBOARDING_THRESHOLDS["examples_per_productive_target"]
    under_exampled = sorted(key for key in productive_targets if examples_by_item[key] < minimum)
    templates_by_mode: Counter[str] = Counter()
    for item in pack.activities:
        template = item.payload
        if _is_promoted(item) and isinstance(template, PackActivityTemplate):
            templates_by_mode[template.mode] += 1
    thin_modes = sorted(
        mode
        for mode, count in templates_by_mode.items()
        if count < ONBOARDING_THRESHOLDS["templates_per_mode"]
    )
    modality_counts: Counter[str] = Counter()
    for item in pack.recommendations:
        recommendation = item.payload
        if _is_promoted(item) and isinstance(recommendation, PackSourceRecommendation):
            modality_counts[recommendation.modality] += 1
    thin_modalities = sorted(
        modality
        for modality in pack.manifest.modalities
        if modality_counts[modality] < ONBOARDING_THRESHOLDS["recommendations_per_modality"]
    )
    requirements = [
        Requirement(
            name=f"{group}_targets",
            band=band,
            required=f">= {ONBOARDING_THRESHOLDS[group]}",
            observed=str(len(items)),
            met=len(items) >= int(ONBOARDING_THRESHOLDS[group]),
            gap=f"{int(ONBOARDING_THRESHOLDS[group]) - len(items)} more {group} targets needed",
        )
        for group, items in groups.items()
    ]
    requirements.extend(
        [
            Requirement(
                name="examples_per_productive_target",
                band=band,
                required=f">= {minimum} reviewed examples each",
                observed=f"{len(productive_targets) - len(under_exampled)} of "
                f"{len(productive_targets)} covered",
                met=not under_exampled,
                gap=f"{len(under_exampled)} targets are under-exampled",
            ),
            Requirement(
                name="templates_per_mode",
                band=None,
                required=f">= {ONBOARDING_THRESHOLDS['templates_per_mode']} per mode",
                observed=", ".join(
                    f"{mode}={count}" for mode, count in sorted(templates_by_mode.items())
                )
                or "none",
                met=bool(templates_by_mode) and not thin_modes,
                gap=f"modes below the threshold: {thin_modes}",
            ),
            Requirement(
                name="source_recommendations_per_modality",
                band=None,
                required=(
                    f">= {ONBOARDING_THRESHOLDS['recommendations_per_modality']} per modality"
                ),
                observed=", ".join(
                    f"{modality}={modality_counts[modality]}"
                    for modality in pack.manifest.modalities
                ),
                met=not thin_modalities,
                gap=f"modalities below the threshold: {thin_modalities}",
            ),
        ]
    )
    return requirements


def _placement_requirements(pack: LoadedPack, band: str) -> list[Requirement]:
    tasks = [(item, payload) for item, payload in _tasks(pack) if payload.level == band]
    promoted = [(item, payload) for item, payload in tasks if _is_promoted(item)]
    kinds = pack.manifest.dimension_kinds
    requirements: list[Requirement] = []
    for dimension in pack.manifest.dimensions:
        dimension_kind = kinds[dimension]
        of_dimension = [payload for _, payload in promoted if payload.dimension == dimension]
        if dimension_kind in {"receptive", "form"}:
            objective = [
                payload for payload in of_dimension if payload.task_type in OBJECTIVE_TASK_TYPES
            ]
            families = {payload.content_family for payload in objective}
            threshold = int(PLACEMENT_THRESHOLDS["objective_per_dimension"])
            requirements.append(
                Requirement(
                    name=f"objective_bank/{dimension}",
                    band=band,
                    required=(
                        f">= {threshold} across "
                        f">= {PLACEMENT_THRESHOLDS['families_per_dimension']} families"
                    ),
                    observed=f"{len(objective)} across {len(families)} families",
                    met=len(objective) >= threshold
                    and len(families) >= int(PLACEMENT_THRESHOLDS["families_per_dimension"]),
                    gap=f"families present: {sorted(families)}",
                )
            )
        elif dimension_kind == "productive":
            extended = [
                payload for payload in of_dimension if payload.task_type in PRODUCTIVE_TASK_TYPES
            ]
            threshold = int(PLACEMENT_THRESHOLDS["productive_per_dimension"])
            requirements.append(
                Requirement(
                    name=f"productive_bank/{dimension}",
                    band=band,
                    required=f">= {threshold} rubric-scored prompts",
                    observed=str(len(extended)),
                    met=len(extended) >= threshold,
                    gap=f"{threshold - len(extended)} more prompts needed",
                )
            )
        else:
            targets = [
                payload for payload in of_dimension if payload.task_type in PRONUNCIATION_TASK_TYPES
            ]
            connected = [
                payload for payload in of_dimension if payload.task_type in CONNECTED_SPEECH_TYPES
            ]
            target_threshold = int(PLACEMENT_THRESHOLDS["pronunciation_targets"])
            speech_threshold = int(PLACEMENT_THRESHOLDS["connected_speech_prompts"])
            requirements.append(
                Requirement(
                    name=f"pronunciation_bank/{dimension}",
                    band=band,
                    required=(
                        f">= {target_threshold} targets and "
                        f">= {speech_threshold} connected-speech prompts"
                    ),
                    observed=f"{len(targets)} targets, {len(connected)} prompts",
                    met=len(targets) >= target_threshold and len(connected) >= speech_threshold,
                    gap="pronunciation bank is short of the placement threshold",
                )
            )
    forms = {form.form_key for form in pack.assessment_forms if form.purpose == "placement"}
    requirements.append(
        Requirement(
            name="alternate_placement_forms",
            band=None,
            required=f">= {PLACEMENT_THRESHOLDS['alternate_forms']}",
            observed=str(len(forms)),
            met=len(forms) >= int(PLACEMENT_THRESHOLDS["alternate_forms"]),
            gap=f"placement forms present: {sorted(forms)}",
        )
    )
    anchors = [payload for _, payload in promoted if payload.is_anchor]
    requirements.append(
        Requirement(
            name="longitudinal_anchors",
            band=band,
            required=">= 1 designated anchor",
            observed=str(len(anchors)),
            met=bool(anchors),
            gap="no task is designated as a longitudinal anchor",
        )
    )
    return requirements


def _expectation_failures(pack: LoadedPack, counts: Mapping[str, int]) -> tuple[str, ...]:
    """Check the pack's own declared expectations, which are its self-test."""

    expectations = pack.expectations
    if expectations is None:
        return ()
    failures: list[str] = []
    if expectations.maturity != pack.manifest.maturity:
        failures.append(
            f"expectations claim maturity {expectations.maturity}, manifest says "
            f"{pack.manifest.maturity}"
        )
    for name, minimum in sorted(expectations.minimum_counts.items()):
        observed = counts.get(name)
        if observed is None:
            failures.append(f"expectations name an unknown count: {name}")
        elif observed < minimum:
            failures.append(f"{name}: {observed} < expected {minimum}")
    themes = {
        theme
        for item, payload in _knowledge(pack)
        if _is_promoted(item)
        for theme in payload.themes
    }
    missing_themes = sorted(set(expectations.required_themes) - themes)
    if missing_themes:
        failures.append(f"required themes absent: {missing_themes}")
    missing_dimensions = sorted(
        set(expectations.required_dimensions) - set(pack.manifest.dimensions)
    )
    if missing_dimensions:
        failures.append(f"required dimensions undeclared: {missing_dimensions}")
    return tuple(failures)


def _requirements_for(
    pack: LoadedPack, maturity: PackMaturity
) -> dict[str, tuple[Requirement, ...]]:
    """Every requirement a maturity level implies, including weaker levels'."""

    collected: dict[str, list[Requirement]] = {}
    index = MATURITY_ORDER.index(maturity)
    for level in MATURITY_ORDER[1 : index + 1]:
        entries: list[Requirement] = []
        for band in pack.manifest.bands:
            if level is PackMaturity.PILOT:
                entries.extend(_pilot_requirements(pack, band))
            elif level is PackMaturity.ONBOARDING_READY:
                entries.extend(_onboarding_requirements(pack, band))
            else:
                entries.extend(_placement_requirements(pack, band))
        collected[str(level)] = entries
    return {level: tuple(entries) for level, entries in collected.items()}


def maturity_supported(pack: LoadedPack, maturity: PackMaturity) -> bool:
    """Whether a pack's measured coverage supports a maturity level."""

    if maturity is PackMaturity.FIXTURE:
        return True
    return all(
        requirement.met
        for requirements in _requirements_for(pack, maturity).values()
        for requirement in requirements
    )


def highest_supported_maturity(pack: LoadedPack) -> PackMaturity:
    supported = PackMaturity.FIXTURE
    for maturity in MATURITY_ORDER:
        if maturity_supported(pack, maturity):
            supported = maturity
        else:
            break
    return supported


def counts(pack: LoadedPack) -> dict[str, int]:
    """Flat counts a pack author and the coverage gates both read."""

    knowledge = _knowledge(pack)
    tasks = _tasks(pack)
    promoted_knowledge = [payload for item, payload in knowledge if _is_promoted(item)]
    promoted_tasks = [payload for item, payload in tasks if _is_promoted(item)]
    return {
        "knowledge": len(knowledge),
        "reviewed_knowledge": len(promoted_knowledge),
        "lexical": sum(1 for payload in promoted_knowledge if payload.kind in LEXICAL_KINDS),
        "grammar": sum(1 for payload in promoted_knowledge if payload.kind in GRAMMAR_KINDS),
        "script": sum(1 for payload in promoted_knowledge if payload.kind in SCRIPT_KINDS),
        "functional": sum(1 for payload in promoted_knowledge if payload.kind in FUNCTIONAL_KINDS),
        "examples": len(pack.examples),
        "relations": len(pack.relations),
        "descriptors": len(pack.descriptors),
        "assessment_tasks": len(tasks),
        "objective_tasks": sum(
            1 for payload in promoted_tasks if payload.task_type in OBJECTIVE_TASK_TYPES
        ),
        "productive_prompts": sum(
            1 for payload in promoted_tasks if payload.task_type in PRODUCTIVE_TASK_TYPES
        ),
        "pronunciation_tasks": sum(
            1 for payload in promoted_tasks if payload.task_type in PRONUNCIATION_TASK_TYPES
        ),
        "connected_speech_tasks": sum(
            1 for payload in promoted_tasks if payload.task_type in CONNECTED_SPEECH_TYPES
        ),
        "activity_templates": len(pack.activities),
        "source_recommendations": len(pack.recommendations),
        "bundles": len(pack.bundles),
        "assessment_forms": len(pack.assessment_forms),
    }


def per_band_counts(pack: LoadedPack) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for band in pack.manifest.bands:
        knowledge = [
            payload
            for item, payload in _knowledge(pack)
            if payload.level == band and _is_promoted(item)
        ]
        tasks = [
            payload
            for item, payload in _tasks(pack)
            if payload.level == band and _is_promoted(item)
        ]
        result[band] = {
            "reviewed_knowledge": len(knowledge),
            "lexical": sum(1 for payload in knowledge if payload.kind in LEXICAL_KINDS),
            "grammar": sum(1 for payload in knowledge if payload.kind in GRAMMAR_KINDS),
            "script": sum(1 for payload in knowledge if payload.kind in SCRIPT_KINDS),
            "functional": sum(1 for payload in knowledge if payload.kind in FUNCTIONAL_KINDS),
            "objective_tasks": sum(
                1 for payload in tasks if payload.task_type in OBJECTIVE_TASK_TYPES
            ),
            "productive_prompts": sum(
                1 for payload in tasks if payload.task_type in PRODUCTIVE_TASK_TYPES
            ),
        }
    return result


def coverage_report(pack: LoadedPack) -> CoverageReport:
    """Measure a pack and judge the maturity it claims."""

    total_counts = counts(pack)
    debt, unresolved = _review_debt(pack)
    connectivity, orphans = _prerequisite_connectivity(pack)
    theme_counts: Counter[str] = Counter()
    provenance_counts: Counter[str] = Counter()
    for item, payload in _knowledge(pack):
        for theme in payload.themes:
            theme_counts[theme] += 1
        for origin in item.origins:
            provenance_counts[str(origin.origin_class)] += 1
    for item in pack.items:
        if item.content_kind == "knowledge":
            continue
        for origin in item.origins:
            provenance_counts[str(origin.origin_class)] += 1
    declared = PackMaturity(pack.manifest.maturity)
    return CoverageReport(
        pack_key=pack.pack_key,
        version=pack.manifest.version,
        declared_maturity=str(declared),
        bands=pack.manifest.bands,
        counts=total_counts,
        per_band=per_band_counts(pack),
        themes=dict(sorted(theme_counts.items())),
        provenance=dict(sorted(provenance_counts.items())),
        review_debt=debt,
        unresolved_items=unresolved,
        descriptor_coverage=_descriptor_coverage(pack),
        support_languages=_support_language_coverage(pack),
        prerequisite_connectivity=connectivity,
        orphan_targets=orphans,
        requirements=_requirements_for(pack, PackMaturity.PLACEMENT_READY),
        declared_maturity_supported=maturity_supported(pack, declared),
        highest_supported_maturity=str(highest_supported_maturity(pack)),
        expectation_failures=_expectation_failures(pack, total_counts),
    )


def unmet_requirements(pack: LoadedPack, maturity: PackMaturity) -> tuple[Requirement, ...]:
    """The requirements a pack would have to meet to claim this maturity."""

    return tuple(
        requirement
        for requirements in _requirements_for(pack, maturity).values()
        for requirement in requirements
        if not requirement.met
    )


def supported_onboarding_modes(maturity: str) -> tuple[str, ...]:
    """Which onboarding modes a maturity level may serve.

    A pilot pack may run a labelled calibration only; comprehensive placement needs
    `placement-ready` coverage, and a fixture pack is never offered to a learner.
    """

    if maturity == PackMaturity.FIXTURE:
        return ()
    if maturity == PackMaturity.PILOT:
        return ("declared-level",)
    if maturity == PackMaturity.ONBOARDING_READY:
        return ("declared-level",)
    return ("declared-level", "placement")


__all__ = [
    "MATURITY_ORDER",
    "CoverageReport",
    "Requirement",
    "counts",
    "coverage_report",
    "highest_supported_maturity",
    "maturity_supported",
    "per_band_counts",
    "supported_onboarding_modes",
    "unmet_requirements",
]
