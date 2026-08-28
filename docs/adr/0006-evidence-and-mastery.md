# ADR 0006: Atomic evidence and multidimensional mastery

Status: accepted for Stage 0

## Decision

LinguaWiki records atomic evidence tied to tasks, modalities, assistance, delay, evaluator, and confidence. Recognition, aided comprehension, controlled production, spontaneous production, pronunciation, and delayed transfer remain distinct. Mastery and proficiency are versioned, explainable aggregates that can be recomputed and regress after later failure.

## Alternatives rejected

- One global level or percentage: hides untested dimensions and uneven abilities.
- Marking pack content mastered from a declared course/level: confuses exposure with evidence.
- Permanent monotonic progress: cannot represent forgetting or false positives.

## Consequences

Declared levels create low-confidence priors and calibration queues. Estimates must retain uncertainty, algorithm version, evidence links, and `not-tested` states.

## Enforced invariants

- Typed attempt/evidence IDs are established in Stage 0.
- Stage 3 property tests prevent recognition from promoting spontaneous production and verify regression/reactivation.
- Stage 4 atomically materializes staged session observations rather than mutating aggregates mid-lesson.
