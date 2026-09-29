# Choosing an onboarding mode

There are two modes, and the pack decides which are available.

## Declared-level mode

Use it when the learner names a level they have studied to, and when the pack is `pilot` or
`onboarding-ready`. It is the only mode a pilot pack supports.

1. `onboard start --mode declared-level --declared-level <level>` stores the level as a
   hypothesis and seeds one low-confidence estimate per dimension.
2. Collect the self-report answers that actually change the plan: `can_do_summary`,
   `study_history`, `recent_materials`, `weekly_availability`, `priority_dimensions`,
   `known_gaps`, `audio_setup`.
3. `onboard finalize` resolves the band plus prerequisites, prepares bounded resources,
   builds a calibration queue that samples **prerequisites as well as the declared band**,
   and opens a labelled pilot calibration.
4. Hand the calibration to `$linguawiki-assess`.

Say plainly what a declared level did and did not do: it chose which material to prepare, and
it established nothing about what the learner can do.

## Placement mode

Use it when prior study is uncertain or mixed, and only when the pack is `placement-ready`.

`onboard start --mode placement` is refused on a weaker pack, and
`assessment start --run-type placement` is refused with the unmet bank requirements listed.
That refusal is the correct outcome — relay it and fall back to declared-level onboarding
with a labelled calibration, saying which dimensions were therefore not measured.

## What the pack's maturity permits

| Maturity | Onboarding | Calibration label |
|---|---|---|
| `fixture` | none; never offered to a learner | none |
| `pilot` | declared-level only | `pilot-calibration` |
| `onboarding-ready` | declared-level | `pilot-calibration` |
| `placement-ready` | declared-level and placement | `comprehensive-placement` |

## Reading the finalize result

- `plan_label` — a pilot pack yields a *pilot curriculum*. Use that phrase with the learner.
- `calibration_queue` — prerequisite samples and declared-band targets, with the reason each
  was chosen.
- `unsupported_dimensions` — the pack can test nothing here. These are **not tested**, never
  failed or assumed.
- `resource_plan.missing_modalities` — no reviewed source recommendation exists for that
  modality; say so instead of inventing one.
- `resource_plan.skipped` — what the item budget or the learner's avoided topics excluded.
- `warnings` — relay these verbatim, especially the pilot-pack warning.

## Two weeks, not a syllabus

The default plan covers two weeks within the pack's *actual* theme coverage. If the learner
wants more, raise `--weeks` and `--item-budget` deliberately and tell them what that costs:
more active reference items to review, not more coverage.
