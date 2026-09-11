"""The one writer of `skill_estimates`, and the immutable trail behind every change.

Every estimate has to answer three questions: what is it, how sure is it, and why did it
move. That is why nothing writes `skill_estimates` directly any more -- a calibration
finalizing a run, a declared level being seeded, and evidence recorded outside any run
all come through `write_estimate`, which records a snapshot in `estimate_history` and
links the evidence rows the snapshot rests on.

Three distinctions are kept that a single number would lose:

- a dimension nothing could test is `not-tested`, never a low score. `estimate_status`
  carries it, so "we did not look" cannot be read as "they cannot do it";
- a declared level is `provisional` with zero evidence behind it. It is a hypothesis the
  learner offered, and it is labelled as one until something tests it;
- uncertainty is reported, not smoothed away, and it falls only when independent
  observations arrive. Repeating one observation cannot narrow an interval.

Evidence recorded outside a run folds into the *same* posterior a placement run left
behind, tempered by how much the observation is worth. That is what makes a placement
finalization and a month of ordinary evidence one continuous estimate rather than two
competing ones.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from linguawiki.clock import Clock, SystemClock, aware_utc
from linguawiki.db import migrations as migration_module
from linguawiki.db.connection import Database, open_reader
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EstimateId, EventId
from linguawiki.mastery import CLAIM_WEIGHTS, DEFAULT_POLICY, MasteryPolicy, decay
from linguawiki.models import ContractModel
from linguawiki.paths import WorkspacePaths
from linguawiki.placement import (
    ALGORITHM_VERSION,
    DimensionState,
    ability_grid,
    broad_prior,
    confidence_label,
    estimated_level,
    posterior_mean,
    posterior_sd,
    update_posterior,
)
from linguawiki.services import learners as learner_service

#: The estimate calculation itself, versioned separately from the placement algorithm so
#: a change to either can be told apart on a stored row.
CALCULATION_VERSION = "estimate.v2"

ESTIMATE_STATUSES: tuple[str, ...] = ("not-tested", "provisional", "estimated")
BASES: tuple[str, ...] = (
    "declared-hypothesis",
    "self-report",
    "calibration",
    "placement",
    "evidence",
)
#: The fields whose change makes a new history snapshot worth writing. A recomputation
#: that reproduces all of them is a no-op, which is what makes recompute idempotent.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "estimate_status",
    "level_code",
    "level_low",
    "level_high",
    "score",
    "uncertainty",
    "confidence_label",
    "basis",
    "evidence_count",
)
#: Differences below this are floating-point noise, not a change in the estimate.
NUMERIC_TOLERANCE = 1e-9


class EstimateFactor(ContractModel):
    """One named, numeric reason an estimate is where it is."""

    name: str
    weight: float
    detail: str


class EstimateRecord(ContractModel):
    track_id: str
    dimension: str
    framework_id: str
    estimate_status: str
    level_code: str | None = None
    level_low: str | None = None
    level_high: str | None = None
    score: float | None = None
    uncertainty: float | None = None
    confidence_label: str
    basis: str
    evidence_count: int = 0
    source_run_id: str | None = None
    calculation_version: str
    as_of: str
    updated_at: str


class EstimateChange(ContractModel):
    dimension: str
    changed: bool
    snapshot_id: str | None = None
    reason: str
    previous: EstimateRecord | None = None
    current: EstimateRecord
    factors: tuple[EstimateFactor, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    #: Evidence that could not be folded in, with the reason. Reported rather than
    #: silently dropped: an estimate that ignored half the evidence is not an estimate.
    excluded_evidence: tuple[str, ...] = ()
    #: Repetitions of a context already counted. They agree with the estimate without
    #: sharpening it, because one context is one observation however often it is met.
    corroborating_evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class EstimateReport(ContractModel):
    track_id: str
    framework_id: str
    framework_levels: tuple[str, ...] = ()
    calculation_version: str
    estimates: tuple[EstimateRecord, ...] = ()
    not_tested: tuple[str, ...] = ()
    #: Never one number unless it was asked for, and labelled a summary when it is.
    summary_level: str | None = None
    summary_label: str | None = None
    warnings: tuple[str, ...] = ()


class SnapshotRecord(ContractModel):
    snapshot_id: str
    dimension: str
    estimate_status: str
    level_code: str | None = None
    level_low: str | None = None
    level_high: str | None = None
    score: float | None = None
    uncertainty: float | None = None
    confidence_label: str
    basis: str
    evidence_count: int
    reason: str
    previous_snapshot_id: str | None = None
    factors: tuple[EstimateFactor, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    calculation_version: str
    as_of: str
    recorded_at: str


class HistoryReport(ContractModel):
    track_id: str
    dimension: str | None = None
    limit: int
    total: int
    snapshots: tuple[SnapshotRecord, ...] = ()
    warnings: tuple[str, ...] = ()


class RecomputeReport(ContractModel):
    track_id: str
    dry_run: bool
    calculation_version: str
    changes: tuple[EstimateChange, ...] = ()
    unchanged: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _row_to_record(row: Sequence[Any]) -> EstimateRecord:
    return EstimateRecord(
        track_id=str(row[0]),
        dimension=str(row[1]),
        framework_id=str(row[2]),
        level_code=None if row[3] is None else str(row[3]),
        level_low=None if row[4] is None else str(row[4]),
        level_high=None if row[5] is None else str(row[5]),
        score=None if row[6] is None else float(row[6]),
        uncertainty=None if row[7] is None else float(row[7]),
        confidence_label=str(row[8]),
        basis=str(row[9]),
        evidence_count=int(row[10]),
        source_run_id=None if row[11] is None else str(row[11]),
        calculation_version=str(row[12]),
        as_of=aware_utc(row[13]).isoformat(),
        updated_at=aware_utc(row[14]).isoformat(),
        estimate_status=_stored_status(row),
    )


def _stored_status(row: Sequence[Any]) -> str:
    """The estimate's status, tolerating a row written before schema 0021.

    Migration 0021 backfilled every existing row, so a null here means a database behind
    that migration; deriving the answer keeps a report readable rather than failing on it.
    """

    if row[15] is not None:
        return str(row[15])
    if str(row[8]) == "not-tested":
        return "not-tested"
    return "provisional" if int(row[10]) == 0 else "estimated"


ESTIMATE_COLUMNS = (
    "track_id, dimension, framework_id, level_code, level_low, level_high, score, "
    "uncertainty, confidence_label, basis, evidence_count, source_run_id, "
    "calculation_version, as_of, updated_at, estimate_status"
)


def read_estimate(database: Database, *, track_id: str, dimension: str) -> EstimateRecord | None:
    """One current estimate, inside a caller's connection."""

    row = database.one(
        f"SELECT {ESTIMATE_COLUMNS} FROM skill_estimates WHERE track_id = ? AND dimension = ?",
        [track_id, dimension],
    )
    return None if row is None else _row_to_record(row)


def read_estimates(database: Database, *, track_id: str) -> tuple[EstimateRecord, ...]:
    """Every current estimate for a track, in dimension order."""

    return tuple(
        _row_to_record(row)
        for row in database.query(
            f"SELECT {ESTIMATE_COLUMNS} FROM skill_estimates WHERE track_id = ? ORDER BY dimension",
            [track_id],
        )
    )


def _differs(previous: EstimateRecord | None, current: EstimateRecord) -> bool:
    """Whether a rewrite is a change worth a snapshot.

    Numeric fields compare within a tolerance: recomputing the same evidence must be a
    no-op, and floating-point arithmetic is not obliged to be bit-identical for that to
    be true.
    """

    if previous is None:
        return True
    for field_name in SNAPSHOT_FIELDS:
        before = getattr(previous, field_name)
        after = getattr(current, field_name)
        if isinstance(before, float) and isinstance(after, float):
            if abs(before - after) > NUMERIC_TOLERANCE:
                return True
        elif before != after:
            return True
    return False


def write_estimate(
    database: Database,
    *,
    track_id: str,
    framework_id: str,
    dimension: str,
    estimate_status: str,
    level_code: str | None,
    level_low: str | None,
    level_high: str | None,
    score: float | None,
    uncertainty: float | None,
    confidence: str,
    basis: str,
    evidence_count: int,
    source_run_id: str | None,
    reason: str,
    factors: Sequence[EstimateFactor] = (),
    evidence_weights: Mapping[str, float] | None = None,
    calculation_version: str = CALCULATION_VERSION,
    dry_run: bool = False,
) -> EstimateChange:
    """Write one estimate and, when it changed, the snapshot that explains the change.

    The only writer of `skill_estimates`. An unchanged estimate is rewritten with a fresh
    `as_of` -- the estimate is still current -- but records no snapshot, so the history is
    a record of changes rather than of recomputations.
    """

    if estimate_status not in ESTIMATE_STATUSES:
        raise LinguaWikiError(
            "unknown_estimate_status",
            f"{estimate_status} is not an estimate status; expected {list(ESTIMATE_STATUSES)}",
            details=(ErrorDetail(field="estimate_status", reason="unknown status"),),
        )
    if basis not in BASES:
        raise LinguaWikiError(
            "unknown_estimate_basis",
            f"{basis} is not an estimate basis; expected {list(BASES)}",
            details=(ErrorDetail(field="basis", reason="unknown basis"),),
        )
    if estimate_status == "not-tested" and evidence_count:
        raise LinguaWikiError(
            "not_tested_with_evidence",
            f"{dimension} cannot be 'not-tested' while {evidence_count} observation(s) "
            "support it; a tested dimension with a poor result is not an untested one",
            details=(ErrorDetail(field="estimate_status", reason="evidence contradicts status"),),
        )
    if estimate_status == "estimated" and not evidence_count:
        raise LinguaWikiError(
            "estimate_without_evidence",
            f"{dimension} cannot be 'estimated' with no observations behind it; a level "
            "nobody tested is provisional",
            details=(ErrorDetail(field="estimate_status", reason="no evidence"),),
        )
    previous = read_estimate(database, track_id=track_id, dimension=dimension)
    now = database.now()
    current = EstimateRecord(
        track_id=track_id,
        dimension=dimension,
        framework_id=framework_id,
        estimate_status=estimate_status,
        level_code=level_code,
        level_low=level_low,
        level_high=level_high,
        score=score,
        uncertainty=uncertainty,
        confidence_label=confidence,
        basis=basis,
        evidence_count=evidence_count,
        source_run_id=source_run_id,
        calculation_version=calculation_version,
        as_of=aware_utc(now).isoformat(),
        updated_at=aware_utc(now).isoformat(),
    )
    changed = _differs(previous, current)
    weights = dict(evidence_weights or {})
    if dry_run:
        return EstimateChange(
            dimension=dimension,
            changed=changed,
            reason=reason,
            previous=previous,
            current=current,
            factors=tuple(factors),
            evidence_ids=tuple(sorted(weights)),
            warnings=("dry run: nothing was written",),
        )
    parameters = [
        level_code,
        level_low,
        level_high,
        score,
        uncertainty,
        confidence,
        basis,
        evidence_count,
        source_run_id,
        calculation_version,
        estimate_status,
        now,
        now,
    ]
    if previous is None:
        database.execute(
            "INSERT INTO skill_estimates (track_id, dimension, framework_id, level_code, "
            "level_low, level_high, score, uncertainty, confidence_label, basis, "
            "evidence_count, source_run_id, calculation_version, estimate_status, as_of, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [track_id, dimension, framework_id, *parameters],
        )
    else:
        database.execute(
            "UPDATE skill_estimates SET level_code = ?, level_low = ?, level_high = ?, "
            "score = ?, uncertainty = ?, confidence_label = ?, basis = ?, evidence_count = ?, "
            "source_run_id = ?, calculation_version = ?, estimate_status = ?, as_of = ?, "
            "updated_at = ? WHERE track_id = ? AND dimension = ?",
            [*parameters, track_id, dimension],
        )
    if not changed:
        return EstimateChange(
            dimension=dimension,
            changed=False,
            reason=f"{reason}; the estimate did not change",
            previous=previous,
            current=current,
            factors=tuple(factors),
            evidence_ids=tuple(sorted(weights)),
        )
    snapshot_id = str(EstimateId.new())
    previous_snapshot = database.scalar(
        "SELECT snapshot_id FROM estimate_history WHERE track_id = ? AND dimension = ? "
        "ORDER BY recorded_at DESC, snapshot_id DESC LIMIT 1",
        [track_id, dimension],
    )
    database.execute(
        "INSERT INTO estimate_history (snapshot_id, track_id, dimension, framework_id, "
        "estimate_status, level_code, level_low, level_high, score, uncertainty, "
        "confidence_label, basis, evidence_count, source_run_id, calculation_version, reason, "
        "previous_snapshot_id, change_json, as_of, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            snapshot_id,
            track_id,
            dimension,
            framework_id,
            estimate_status,
            level_code,
            level_low,
            level_high,
            score,
            uncertainty,
            confidence,
            basis,
            evidence_count,
            source_run_id,
            calculation_version,
            reason,
            None if previous_snapshot is None else str(previous_snapshot),
            json.dumps(
                {
                    "previous": None if previous is None else previous.model_dump(mode="json"),
                    "factors": [factor.model_dump(mode="json") for factor in factors],
                },
                sort_keys=True,
            ),
            now,
            now,
        ],
    )
    for evidence_id, weight in sorted(weights.items()):
        database.execute(
            "INSERT INTO estimate_evidence (snapshot_id, evidence_id, weight, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            [snapshot_id, evidence_id, round(weight, 6), now],
        )
    migration_module.record_domain_event(
        database,
        event_type="estimate.changed",
        aggregate_type="skill_estimate",
        aggregate_id=snapshot_id,
        correlation_id=EventId.new(),
        payload_json=json.dumps(
            {
                "dimension": dimension,
                "level_code": level_code,
                "estimate_status": estimate_status,
                "basis": basis,
            },
            sort_keys=True,
        ),
    )
    return EstimateChange(
        dimension=dimension,
        changed=True,
        snapshot_id=snapshot_id,
        reason=reason,
        previous=previous,
        current=current,
        factors=tuple(factors),
        evidence_ids=tuple(sorted(weights)),
    )


def _level_label(index: float, levels: Sequence[str]) -> str | None:
    if not levels:
        return None
    bounded = max(0, min(len(levels) - 1, round(index)))
    return levels[bounded]


def upsert_from_state(
    database: Database,
    *,
    track_id: str,
    framework_id: str,
    state: DimensionState,
    level: str | None,
    low: str | None,
    high: str | None,
    run_id: str | None,
    basis: str,
    reason: str,
    dry_run: bool = False,
) -> EstimateChange:
    """Write the estimate a placement or calibration dimension state implies."""

    label = confidence_label(state)
    tested = bool(state.tasks_used)
    mean = posterior_mean(state.grid, state.posterior) if tested else None
    spread = posterior_sd(state.grid, state.posterior) if tested else None
    if state.status == "not-tested":
        status = "not-tested"
    elif tested:
        status = "estimated"
    else:
        status = "provisional"
    factors = [
        EstimateFactor(
            name="tasks-used",
            weight=float(state.tasks_used),
            detail=(
                f"{state.tasks_used} bank task(s) folded into the posterior across "
                f"{len(set(state.families))} content family/families"
            ),
        ),
        EstimateFactor(
            name="algorithm",
            weight=1.0,
            detail=f"{ALGORITHM_VERSION} staircase on a half-band ability grid",
        ),
    ]
    if state.stop_reason:
        factors.append(
            EstimateFactor(name="stop-reason", weight=0.0, detail=str(state.stop_reason))
        )
    return write_estimate(
        database,
        track_id=track_id,
        framework_id=framework_id,
        dimension=state.dimension,
        estimate_status=status,
        level_code=level,
        level_low=low,
        level_high=high,
        score=mean,
        uncertainty=spread,
        confidence="not-tested" if state.status == "not-tested" else label,
        basis=basis,
        evidence_count=state.tasks_used,
        source_run_id=run_id,
        reason=reason,
        factors=tuple(factors),
        calculation_version=f"{CALCULATION_VERSION}+{ALGORITHM_VERSION}",
        dry_run=dry_run,
    )


class DimensionObservation(ContractModel):
    """One evidence row reduced to what a dimension estimate can use."""

    evidence_id: str
    claim: str
    polarity: str
    strength: float
    difficulty: float | None
    retrieval: str
    occurred_at: datetime
    context_key: str
    #: The run whose posterior already holds this observation, when it came from one.
    #: Folding it in again would count one answer twice.
    assessment_run_id: str | None = None


def dimension_observations(
    database: Database, *, track_id: str, dimension: str
) -> tuple[DimensionObservation, ...]:
    """Every evidence row bearing on one dimension, oldest first.

    Read from `evidence` joined to its attempt, because the attempt is where the
    difficulty the observation was made at lives. Evidence with no difficulty is still
    returned: the caller reports it as excluded rather than pretending it was folded in.
    """

    return tuple(
        DimensionObservation(
            evidence_id=str(row[0]),
            claim=str(row[1]),
            polarity=str(row[2]),
            strength=float(row[3]),
            difficulty=None if row[4] is None else float(row[4]),
            retrieval=str(row[5]),
            occurred_at=aware_utc(row[6]),
            context_key=str(row[7]),
            assessment_run_id=None if row[8] is None else str(row[8]),
        )
        for row in database.query(
            "SELECT item.evidence_id, item.claim, item.polarity, item.strength, "
            "attempt.source_difficulty, item.retrieval, item.occurred_at, item.context_key, "
            "attempt.assessment_run_id "
            "FROM evidence item JOIN attempts attempt ON attempt.attempt_id = item.attempt_id "
            "WHERE item.track_id = ? AND item.dimension = ? "
            "ORDER BY item.occurred_at, item.evidence_id",
            [track_id, dimension],
        )
    )


def _observation_weight(
    observation: DimensionObservation, *, now: datetime, policy: MasteryPolicy
) -> float:
    """How much one observation tempers the likelihood.

    The same ordering the item aggregation uses -- spontaneous over recognition, delayed
    over immediate, recent over old -- so an item's stage and a dimension's estimate
    cannot disagree about which evidence mattered.
    """

    from linguawiki.mastery import RETRIEVAL_FACTORS

    weight = (
        observation.strength
        * CLAIM_WEIGHTS[observation.claim]
        * RETRIEVAL_FACTORS[observation.retrieval]
        * decay(observation.occurred_at, now=now, half_life_days=policy.half_life_days)
    )
    return max(0.001, min(1.0, weight))


def _score_for(observation: DimensionObservation) -> float:
    """The fractional score one observation represents."""

    return {"positive": 1.0, "partial": 0.5, "negative": 0.0}[observation.polarity]


def _starting_posterior(
    database: Database, *, track_id: str, dimension: str, levels: Sequence[str]
) -> tuple[tuple[float, ...], tuple[float, ...], str | None]:
    """The distribution evidence folds into: a run's posterior, or a broad prior.

    Continuing from the run's own posterior is what makes finalization and later evidence
    one estimate. A dimension no run ever touched starts broad rather than at a declared
    level: the declared level is already recorded as a hypothesis, and letting it also
    anchor the evidence would count the learner's own claim twice.
    """

    grid = ability_grid(max(1, len(levels)))
    row = database.one(
        "SELECT state.posterior_json, state.grid_json, state.run_id "
        "FROM placement_dimension_state state "
        "JOIN assessment_runs run ON run.run_id = state.run_id "
        "WHERE run.track_id = ? AND state.dimension = ? AND state.tasks_used > 0 "
        "ORDER BY state.updated_at DESC, state.run_id DESC LIMIT 1",
        [track_id, dimension],
    )
    if row is None:
        return (grid, broad_prior(grid), None)
    stored_grid = tuple(float(value) for value in json.loads(str(row[1]))["points"])
    posterior = tuple(float(value) for value in json.loads(str(row[0])))
    if len(stored_grid) != len(posterior):
        return (grid, broad_prior(grid), None)
    return (stored_grid, posterior, str(row[2]))


def recompute_dimension(
    database: Database,
    *,
    track_id: str,
    framework_id: str,
    dimension: str,
    levels: Sequence[str],
    now: datetime,
    policy: MasteryPolicy = DEFAULT_POLICY,
    dry_run: bool = False,
) -> EstimateChange:
    """Recompute one dimension's estimate from raw evidence plus any run posterior."""

    observations = dimension_observations(database, track_id=track_id, dimension=dimension)
    grid, posterior, run_id = _starting_posterior(
        database, track_id=track_id, dimension=dimension, levels=levels
    )
    # An observation recorded against the run whose posterior this starts from is already
    # in that posterior. Folding it in again would count one answer twice and narrow the
    # interval on evidence that never doubled.
    already_folded = tuple(
        entry.evidence_id
        for entry in observations
        if run_id is not None and entry.assessment_run_id == run_id
    )
    candidates = [entry for entry in observations if entry.evidence_id not in set(already_folded)]
    usable = [entry for entry in candidates if entry.difficulty is not None]
    excluded = (
        *(entry.evidence_id for entry in candidates if entry.difficulty is None),
        *already_folded,
    )
    existing = read_estimate(database, track_id=track_id, dimension=dimension)
    if not usable:
        # Nothing testable arrived. The dimension keeps whatever a run or a declared
        # level already established; recomputation is not an occasion to downgrade it.
        if existing is None:
            return write_estimate(
                database,
                track_id=track_id,
                framework_id=framework_id,
                dimension=dimension,
                estimate_status="not-tested",
                level_code=None,
                level_low=None,
                level_high=None,
                score=None,
                uncertainty=None,
                confidence="not-tested",
                basis="declared-hypothesis",
                evidence_count=0,
                source_run_id=None,
                reason="no evidence bears on this dimension yet",
                factors=(
                    EstimateFactor(
                        name="no-evidence",
                        weight=0.0,
                        detail=(
                            f"{len(excluded)} observation(s) exist but carry no difficulty"
                            if excluded
                            else "no observation names this dimension"
                        ),
                    ),
                ),
                dry_run=dry_run,
            )
        return EstimateChange(
            dimension=dimension,
            changed=False,
            reason="no usable evidence arrived; the existing estimate stands",
            previous=existing,
            current=existing,
            excluded_evidence=excluded,
        )
    # One context contributes one observation. Folding every repetition narrowed the
    # interval on evidence that never became independent -- three checks on the same
    # prompt are one check repeated, and an interval is a claim about how much is known.
    # The newest observation in each context is the one that counts, because it can be a
    # failure and because the latest check in a setting is what we know about it.
    newest_by_context: dict[str, DimensionObservation] = {}
    for observation in usable:
        held = newest_by_context.get(observation.context_key)
        if held is None or (observation.occurred_at, observation.evidence_id) > (
            held.occurred_at,
            held.evidence_id,
        ):
            newest_by_context[observation.context_key] = held = observation
    folded = sorted(
        newest_by_context.values(), key=lambda entry: (entry.occurred_at, entry.evidence_id)
    )
    corroborating = tuple(
        observation.evidence_id
        for observation in usable
        if observation.evidence_id not in {entry.evidence_id for entry in folded}
    )
    weights: dict[str, float] = {}
    contexts: set[str] = set()
    for observation in folded:
        weight = _observation_weight(observation, now=now, policy=policy)
        assert observation.difficulty is not None
        posterior = update_posterior(
            grid,
            posterior,
            difficulty=observation.difficulty,
            score=_score_for(observation),
            weight=weight,
        )
        weights[observation.evidence_id] = weight
        contexts.add(observation.context_key)
    mean = posterior_mean(grid, posterior)
    spread = posterior_sd(grid, posterior)
    state = DimensionState(
        dimension=dimension,
        dimension_kind="receptive",
        grid=tuple(grid),
        prior=tuple(posterior),
        posterior=tuple(posterior),
        minimum_tasks=0,
        maximum_tasks=len(usable),
        tasks_used=len(usable),
        families=tuple(sorted(contexts)),
        task_types=tuple(sorted(contexts)),
    )
    # The same band mapping the staircase uses: the median band, with the credible
    # bounds rounded outwards. A calibration estimate and an evidence estimate must not
    # disagree about what a posterior means.
    level, level_low, level_high = estimated_level(state, levels)
    label = confidence_label(state)
    # Diversity gates confidence here exactly as it gates an item's: independent
    # observations narrow an interval, and one observation repeated does not.
    if len(contexts) < 2 and label == "high":
        label = "medium"
    if len(contexts) < 2 and label == "medium":
        label = "low"
    factors = (
        EstimateFactor(
            name="evidence",
            weight=round(sum(weights.values()), 6),
            detail=(
                f"{len(folded)} observation(s) across {len(contexts)} context(s), tempered by "
                f"claim, help, assessor, and a {policy.half_life_days:.0f}-day half-life"
                + (
                    f"; {len(corroborating)} repetition(s) corroborate without sharpening it"
                    if corroborating
                    else ""
                )
            ),
        ),
        EstimateFactor(
            name="prior",
            weight=1.0,
            detail=(
                f"folded into the posterior left by run {run_id}"
                + (
                    f", excluding {len(already_folded)} observation(s) that run already holds"
                    if already_folded
                    else ""
                )
                if run_id
                else "folded into a broad prior; no run has tested this dimension"
            ),
        ),
        EstimateFactor(
            name="diversity",
            weight=float(len(contexts)),
            detail=(
                "confidence is capped until independent contexts agree"
                if len(contexts) < 2
                else f"{len(contexts)} independent contexts support the estimate"
            ),
        ),
    )
    # A single observation, or several in one context, is a reading rather than a
    # measurement. It is reported with its band and its interval, and labelled
    # `provisional` until independent observations agree -- the same diversity rule the
    # item aggregation applies before it raises confidence.
    measured = len(folded) >= 2 and len(contexts) >= 2
    change = write_estimate(
        database,
        track_id=track_id,
        framework_id=framework_id,
        dimension=dimension,
        estimate_status="estimated" if measured else "provisional",
        level_code=level,
        level_low=level_low,
        level_high=level_high,
        score=mean,
        uncertainty=spread,
        confidence=label,
        basis="evidence",
        evidence_count=len(folded),
        source_run_id=run_id,
        reason=(
            f"recomputed from {len(folded)} independent observation(s) across "
            f"{len(contexts)} context(s)"
            + (f", with {len(corroborating)} repetition(s) corroborating" if corroborating else "")
            + ("" if measured else "; provisional until independent contexts agree")
        ),
        factors=factors,
        evidence_weights=weights,
        dry_run=dry_run,
    )
    return change.model_copy(update={"corroborating_evidence": corroborating})


def dimensions_for(database: Database, *, track_id: str) -> tuple[str, ...]:
    """Every dimension the track can have an estimate for.

    The pack's declared dimensions, plus any a stored estimate or a piece of evidence
    already names. A dimension the pack dropped keeps its estimate rather than vanishing.
    """

    from linguawiki.services import packs as pack_service

    record = learner_service.track_context(database, track_id)
    declared: tuple[str, ...] = ()
    if record.pack_key is not None:
        from linguawiki.contracts import PackManifest

        row = pack_service.installed_pack(database, record.pack_key)
        manifest = PackManifest.model_validate(json.loads(row["manifest_json"]))
        declared = tuple(manifest.dimensions)
    stored = tuple(
        str(dimension)
        for (dimension,) in database.query(
            "SELECT DISTINCT dimension FROM skill_estimates WHERE track_id = ?", [track_id]
        )
    )
    observed = tuple(
        str(dimension)
        for (dimension,) in database.query(
            "SELECT DISTINCT dimension FROM evidence WHERE track_id = ? AND dimension IS NOT NULL",
            [track_id],
        )
    )
    return tuple(sorted(set(declared) | set(stored) | set(observed)))


def recompute(
    database: Database,
    *,
    track_id: str,
    dimensions: Sequence[str] | None = None,
    now: datetime | None = None,
    policy: MasteryPolicy = DEFAULT_POLICY,
    dry_run: bool = False,
) -> RecomputeReport:
    """Recompute every dimension's estimate from raw evidence, inside a connection."""

    record = learner_service.track_context(database, track_id)
    moment = now or aware_utc(database.now())
    selected = tuple(dimensions) if dimensions else dimensions_for(database, track_id=track_id)
    changes: list[EstimateChange] = []
    unchanged: list[str] = []
    for dimension in selected:
        change = recompute_dimension(
            database,
            track_id=track_id,
            framework_id=record.proficiency_framework,
            dimension=dimension,
            levels=record.framework_levels,
            now=moment,
            policy=policy,
            dry_run=dry_run,
        )
        if change.changed:
            changes.append(change)
        else:
            unchanged.append(dimension)
    return RecomputeReport(
        track_id=track_id,
        dry_run=dry_run,
        calculation_version=CALCULATION_VERSION,
        changes=tuple(changes),
        unchanged=tuple(unchanged),
    )


def report(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    summary: bool = False,
    clock: Clock | None = None,
) -> EstimateReport:
    """The learner's profile, one estimate per dimension and never one global number.

    `summary` is the explicit request the plan requires before a single level is offered,
    and the result is labelled a summary rather than presented as the learner's level.
    """

    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        record = learner_service.track_context(database, track_id)
        stored = read_estimates(database, track_id=track_id)
        # A dimension with no row at all is untested, not absent. Synthesizing it here
        # keeps "we did not look" visible in the profile before anything has run, and a
        # recomputation persists the same answer.
        known = {estimate.dimension for estimate in stored}
        moment = aware_utc(database.now()).isoformat()
        synthetic = tuple(
            EstimateRecord(
                track_id=track_id,
                dimension=dimension,
                framework_id=record.proficiency_framework,
                estimate_status="not-tested",
                confidence_label="not-tested",
                basis="declared-hypothesis",
                evidence_count=0,
                calculation_version=CALCULATION_VERSION,
                as_of=moment,
                updated_at=moment,
            )
            for dimension in dimensions_for(database, track_id=track_id)
            if dimension not in known
        )
        estimates = tuple(sorted((*stored, *synthetic), key=lambda estimate: estimate.dimension))
        not_tested = tuple(
            estimate.dimension for estimate in estimates if estimate.estimate_status == "not-tested"
        )
        tested = [
            estimate
            for estimate in estimates
            if estimate.estimate_status == "estimated" and estimate.score is not None
        ]
        summary_level = None
        summary_label = None
        if summary and tested:
            average = sum(float(entry.score or 0.0) for entry in tested) / len(tested)
            summary_level = _level_label(average, record.framework_levels)
            summary_label = (
                f"summary of {len(tested)} tested dimension(s); not a level the learner holds"
            )
        warnings: list[str] = []
        if not_tested:
            warnings.append(f"not tested: {', '.join(not_tested)} -- these are untested, not weak")
        # A point band inside a four-band interval is not a level the learner holds, and
        # a report that offers one without saying so is the overclaim this module exists
        # to avoid.
        levels = record.framework_levels
        wide = tuple(
            estimate.dimension
            for estimate in estimates
            if estimate.level_low in levels
            and estimate.level_high in levels
            and levels.index(str(estimate.level_high)) - levels.index(str(estimate.level_low)) > 2
        )
        if wide:
            warnings.append(
                f"wide interval: {', '.join(wide)} -- the band is the middle of a range "
                "spanning more than two levels, not a level the learner holds"
            )
        if summary and not tested:
            warnings.append("no dimension has evidence, so no summary can be offered")
        return EstimateReport(
            track_id=track_id,
            framework_id=record.proficiency_framework,
            framework_levels=record.framework_levels,
            calculation_version=CALCULATION_VERSION,
            estimates=estimates,
            not_tested=not_tested,
            summary_level=summary_level,
            summary_label=summary_label,
            warnings=tuple(warnings),
        )


def history(
    paths: WorkspacePaths,
    *,
    track: str | None = None,
    dimension: str | None = None,
    limit: int = 50,
    clock: Clock | None = None,
) -> HistoryReport:
    """The immutable snapshots behind a dimension, newest first."""

    if limit < 1 or limit > 500:
        raise LinguaWikiError(
            "invalid_limit",
            "a history limit is between 1 and 500",
            details=(ErrorDetail(field="limit", reason=str(limit)),),
        )
    with open_reader(paths, clock=clock or SystemClock()) as database:
        track_id = learner_service.resolve_track(database, track)
        condition = "" if dimension is None else "AND dimension = ? "
        parameters: list[Any] = [track_id] if dimension is None else [track_id, dimension]
        total = int(
            database.scalar(
                f"SELECT count(*) FROM estimate_history WHERE track_id = ? {condition}",
                parameters,
            )
        )
        rows = database.query(
            "SELECT snapshot_id, dimension, estimate_status, level_code, level_low, level_high, "
            "score, uncertainty, confidence_label, basis, evidence_count, reason, "
            "previous_snapshot_id, change_json, calculation_version, as_of, recorded_at "
            f"FROM estimate_history WHERE track_id = ? {condition}"
            "ORDER BY recorded_at DESC, snapshot_id DESC LIMIT ?",
            [*parameters, limit],
        )
        snapshots = tuple(
            SnapshotRecord(
                snapshot_id=str(row[0]),
                dimension=str(row[1]),
                estimate_status=str(row[2]),
                level_code=None if row[3] is None else str(row[3]),
                level_low=None if row[4] is None else str(row[4]),
                level_high=None if row[5] is None else str(row[5]),
                score=None if row[6] is None else float(row[6]),
                uncertainty=None if row[7] is None else float(row[7]),
                confidence_label=str(row[8]),
                basis=str(row[9]),
                evidence_count=int(row[10]),
                reason=str(row[11]),
                previous_snapshot_id=None if row[12] is None else str(row[12]),
                factors=tuple(
                    EstimateFactor.model_validate(entry)
                    for entry in json.loads(str(row[13])).get("factors", ())
                ),
                evidence_ids=tuple(
                    str(evidence_id)
                    for (evidence_id,) in database.query(
                        "SELECT evidence_id FROM estimate_evidence WHERE snapshot_id = ? "
                        "ORDER BY evidence_id",
                        [str(row[0])],
                    )
                ),
                calculation_version=str(row[14]),
                as_of=aware_utc(row[15]).isoformat(),
                recorded_at=aware_utc(row[16]).isoformat(),
            )
            for row in rows
        )
    return HistoryReport(
        track_id=track_id,
        dimension=dimension,
        limit=limit,
        total=total,
        snapshots=snapshots,
        warnings=(
            (f"{total} snapshot(s) recorded; {len(snapshots)} returned",)
            if total > len(snapshots)
            else ()
        ),
    )


__all__ = [
    "BASES",
    "CALCULATION_VERSION",
    "ESTIMATE_STATUSES",
    "SNAPSHOT_FIELDS",
    "DimensionObservation",
    "EstimateChange",
    "EstimateFactor",
    "EstimateRecord",
    "EstimateReport",
    "HistoryReport",
    "RecomputeReport",
    "SnapshotRecord",
    "dimension_observations",
    "dimensions_for",
    "history",
    "read_estimate",
    "read_estimates",
    "recompute",
    "recompute_dimension",
    "report",
    "upsert_from_state",
    "write_estimate",
]
