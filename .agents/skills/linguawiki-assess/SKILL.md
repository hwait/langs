---
name: linguawiki-assess
description: Run a bounded LinguaWiki calibration or placement, finalize a baseline, and report per-dimension estimates with their uncertainty and provenance. Use for baseline calibration, level checks, placement runs including pausing and resuming across sittings, explaining why an estimate changed, and recomputing estimates from raw evidence. Refuses comprehensive placement where the pack's bank cannot support it.
---

# LinguaWiki assessment

Every command is a `linguawiki` CLI call with `--format json`. The CLI owns the algorithm:
task selection, the posterior update, the stop rule, exposure limits, and the confidence
label. Your job is to run the tasks honestly and report what came back — never to compute an
estimate yourself or to round a range into a level.

```bash
linguawiki assessment start    --workspace <path> [--run-type pilot-calibration|placement] \
  [--dimension <name> ...] [--modality <name> ...] [--idempotency-key <key>] --format json
linguawiki assessment next     --workspace <path> [--run <id>] --format json
linguawiki assessment record   --workspace <path> --content <id> --score <0..1> \
  [--input rubric.json] [--excerpt "<learner response>"] \
  [--assessor-kind deterministic|ai|learner|human] [--assessor <who>] \
  [--confidence low|medium|high] --format json
linguawiki assessment pause    --workspace <path> [--run <id>] --format json
linguawiki assessment resume   --workspace <path> [--run <id>] --format json
linguawiki assessment finalize --workspace <path> [--run <id>] [--reason <why>] --format json
linguawiki assessment report   --workspace <path> [--run <id>] --format json
```

## The loop

1. `start` opens a run and persists every dimension's grid, prior, budget, and status. A
   dimension the pack or the learner's equipment cannot serve is closed as `not-tested`
   immediately, with the reason.
2. `next` serves one task and says why it chose it: `informativeness`, `content-family
   diversity`, or `boundary probe`. When no dimension is still open it returns the run report
   instead of a task — that is the signal to finalize.
3. Present the task exactly as `prompt` gives it. Respect `permitted_help`. Do not rephrase an
   objective item, add examples, or hint.
4. `record` scores the served task. Then loop back to `next`.
5. `finalize` closes the run and writes one estimate per dimension.

`next` and `record` are the only way tasks enter a run: recording a task that was not served
is refused, and re-recording an answered task is an idempotent no-op, so a retry is safe.

## Scoring

`--score` is a fraction from `0.0` to `1.0`.

- Objective and short-response tasks: compare against the task's expected answers. Use `1.0`
  or `0.0`; use `0.5` only when the task's own rubric defines a partial credit.
- Extended productive tasks: score each rubric dimension, pass the whole rubric result through
  `--input`, and set `--score` to the weighted total. Store the rubric detail, not only the
  total.
- Pronunciation tasks: **audio is required**. A correct transcript proves nothing about
  pronunciation. If you have no linked audio, do not score the task — leave the dimension to
  stop as under-evidenced, or pause the run until audio is available.

Set `--assessor-kind` truthfully. `ai` means you scored it; `human` means a person did.
`--confidence low` on an AI-scored productive task is the honest default. Include
`--excerpt` only within the learner's retention consent.

## Reading a result

Report, per dimension: `status`, `tasks_used` against its budget, `confidence`,
`estimated_level`, and the `credible_low..credible_high` range. Then:

- **Never collapse the profile into one level.** A learner can be B1 in reading and A2 in
  speaking; that difference is the useful part.
- **Always give the range and the confidence together.** `estimated_level` alone overstates
  what a six-task probe established.
- **`not-tested` is not a failure.** Say which dimensions were not measured and why —
  the pack had no task, or the modality was unavailable.
- **Read `stop_reason`.** `precision-reached` is a real result; `maximum-budget`, `bank
  exhausted inside the reuse window`, fatigue, and a learner request are all lower-confidence
  stops and must be reported as such.
- **`calibration_label` matters.** `pilot-calibration` is a labelled probe against a narrow
  pack, not a level claim. Never describe it as a placement.

## Finalizing a baseline

`finalize` writes one estimate per dimension and records an immutable snapshot explaining
each one. From Stage 3, a run's posterior is not the end of the estimate — evidence recorded
later folds into the *same* distribution, so a baseline and the weeks after it are one
continuous estimate rather than two competing ones.

```bash
linguawiki assessment finalize --workspace <path> [--run <id>] --format json
linguawiki estimate  show      --workspace <path> --format json
linguawiki estimate  history   --workspace <path> --dimension <name> --format json
linguawiki evidence  recompute --workspace <path> [--dry-run] --format json
```

`estimate show` is what to report, and `estimate_status` comes first:

- `estimated` — independent observations agree. A band, a range, and a confidence.
- `provisional` — one observation, or observations from a single context. A reading.
- `not-tested` — nothing measured it. Say which dimensions, and why.

`estimate history` answers "why did this change": each snapshot names its `reason`, its
weighting `factors`, and the `evidence_ids` behind it. Use it instead of reconstructing a
story; if a learner disputes an estimate, the snapshot is the answer.

`evidence recompute --dry-run` shows what a recomputation *would* change without writing
anything. Run it before recomputing after a long gap, and report what moved and why — decay
alone can lower a stage, and that is a real result rather than a bug.

### Scoring a task also records evidence

A scored bank task is an observation of the learner, so where it should also count toward an
item's stage, record it as evidence with the task named:

```bash
linguawiki evidence record --workspace <path> --origin assessment \
  --assessment-run <run-id> --task <task-content-id> --score <0..1> \
  [--target <item>] [--claim <claim>] --format json
```

Do **not** pass `--task-type`, `--modality`, or `--dimension` here. What the learner faced
is read from **the run's own record of what it served**, not from the pack — a pack is
mutable and a run is not, so a later pack edit cannot rewrite what somebody answered. A
value you pass that contradicts it is refused with the recorded value named. That is the
guard: describing a text short-response as a spoken exchange would otherwise buy a
spontaneous-production claim the learner never earned, and so would editing the pack
afterwards.

If the bank item has changed since the run served it, the observation still records what
the learner faced and the result **warns** that the two disagree. Relay that warning: it is
the difference between the learner's history and the pack's current opinion of it.

`--assessment-run` requires that the run served the task *and* that the run belongs to this
track. A run is one learner's sitting; attaching it elsewhere would put one learner's work
in another learner's model.

The served record also supplies the difficulty and content family, so the observation lands
on the same ability grid the run used — and it is not counted twice, because the run's
posterior already holds what the run itself scored.

See `references/placement-algorithm.md` for how the estimate is produced and
`references/scoring-and-conditions.md` for exam conditions and rubric handling.

## Boundaries

- `--run-type placement` is refused unless the pack is `placement-ready`; the error lists the
  unmet bank requirements. Relay it and offer a labelled calibration instead. Do not retry as
  a calibration and then describe the result as a placement.
- Never re-serve an item the learner has seen inside the six-month reuse window; the CLI
  enforces this, so do not work around a `bank exhausted` stop by starting a fresh run.
- Never infer listening from reading a transcript, or speaking from a written answer.
- Never adjust an estimate by hand. There is no command that sets one, and that is
  deliberate: an estimate is derived from evidence plus an algorithm version, and both are
  recorded on it.
- Never claim a framework level for the learner as a whole without an explicit request.
  `estimate show --summary` produces one and labels it a summary; the label travels with the
  number and must reach the learner with it.
- Never report an `estimate_status` of `not-tested` or `provisional` as a level. The first
  means nobody looked; the second means one observation, which is a reading.
- Pause rather than push through fatigue: `pause` keeps every posterior, and `resume`
  continues where it stopped.
