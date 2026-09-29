# Placement algorithm v1

A transparent ordinal Bayesian staircase. It is written to be explainable to the learner, so
explain it rather than presenting a number.

## The grid and the prior

Framework levels sit on an ordered numeric grid, one unit per band, at half-band resolution
so a reviewed half-band item difficulty means something. A declared level centres the prior
with a spread of about one band; with no declared level the prior is broad. A declared level
therefore changes *which task is asked first*, and nothing about what the run may conclude.

## The response curve

`p(success | ability, difficulty) = 1 / (1 + exp(-1.7 * (ability - difficulty)))`

A score `s` in `[0, 1]` multiplies each grid point by `p ** s * (1 - p) ** (1 - s)`, and the
posterior is renormalised. The prior, item difficulty, score, and posterior are stored for
every task, so any run can be replayed. The parameters are fixed and expert-authored in v1.

## Task selection

Eligibility first — task type for the dimension, an available modality, and no item the
learner has seen inside the reuse window. Then the duties the stop rule will demand:
content-family diversity while coverage is short, and a boundary probe once the minimum
budget is met. Only then informativeness, measured as expected posterior entropy.

That order matters: choosing the most informative eligible task first keeps asking from one
content family and never satisfies the coverage condition.

## Budgets

| Dimension kind | Minimum | Maximum |
|---|---:|---:|
| receptive, form (objective or short response) | 6 | 12 |
| productive (extended rubric prompt) | 3 | 5 |
| pronunciation (targets plus connected speech) | 4, including 1 connected sample | 8, including 2 |

## Stopping

A dimension may stop only when **all** of these hold:

- its minimum budget is met;
- its evidence spans at least two content families or task types;
- at least 80% of the posterior lies inside an interval no wider than one adjacent band;
- a boundary probe above or below the estimate has been served, unless the estimate sits at
  the edge of the framework.

Otherwise it stops at the maximum budget, on the learner's request, at a fatigue threshold, or
because the bank ran out of unseen items. Every such stop is labelled and is *not* forced into
a precise band.

## Confidence labels

- `high` — stopped on precision with a range of half a band or less and diverse evidence.
- `medium` — stopped on precision with a one-band range, or met the minimum budget within one
  band.
- `low` — anything else: a maximum-budget stop, an exhausted bank, fatigue, or a request.
- `not-tested` — the dimension was never probed. This is a gap, not a score.

## Exposure

A placement item is unavailable to the same learner for six months unless it is an explicitly
designated longitudinal anchor. This exists so that improvement is not memorisation, which is
why a `bank exhausted inside the reuse window` stop must be reported rather than worked
around.
