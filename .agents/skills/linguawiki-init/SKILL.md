---
name: linguawiki-init
description: Set up a LinguaWiki learner, a target-language track, and its first two weeks of bounded resources, and hand over the resulting learner model. Use for new-language setup, learner profile and preferences, pack installation, declared-level or placement onboarding, importing and auditing a prior course, and reporting the initial per-dimension estimates with their status. Never claims a level from a self-report.
---

# LinguaWiki initialization

Every command is a `linguawiki` CLI call with `--format json`. The order matters: a track
cannot exist without an installed pack for its language, and onboarding cannot run without a
track.

```text
workspace init -> pack install -> user create -> track create -> onboard start
  -> onboard record -> onboard finalize -> assessment (linguawiki-assess)
```

## 1. The workspace

A learner workspace is an independent private repository. If `linguawiki status` reports no
database, or `workspace doctor` fails, route to `$linguawiki` for the workspace and storage
commands rather than improvising here.

## 2. Install the pack before the track

```bash
linguawiki pack list --workspace <path> --format json
linguawiki pack install --workspace <path> <pack-dir-or-key> --format json
```

The pack decides what is possible: the frameworks and their level order, the dimensions that
can be tested, the themes and modalities that exist, and — through its maturity — which
onboarding modes are available at all. Read `maturity` from the install result and say what
it permits. A `fixture` pack is never offered to a learner.

## 3. Collect only what changes behaviour

```bash
linguawiki user create --workspace <path> --name "<display name>" \
  --timezone <IANA-zone> --native <tag> [--support <tag> ...] --format json
```

Ask for the learner's own timezone, not the machine's. At least one native language is
required, because contrast and explanation depend on it.

```bash
linguawiki track create --workspace <path> \
  --target-language <bcp47> --framework <framework-id> \
  [--script <Scrp>] [--region <RE>] [--declared-level <level>] [--target-level <level>] \
  [--goal "<why they are learning>"] [--input preferences.json] --format json
```

`preferences.json` is a typed object; anything outside it is refused rather than stored as a
note nobody reads. The fields that change behaviour: `goals`, `intended_uses`,
`weekly_minutes`, `session_minutes`, `sessions_per_week`, `interests`, `avoided_topics`,
`preferred_source_types`, `correction_mode` (`fluency`, `accuracy`, `exam`),
`explanation_language`, `accessibility_needs`, `voice_available`,
`audio_recording_available`, `transcript_retention_consent`, `audio_retention_consent`,
`external_service_consent`, `prior_materials`, `anki_deck`.

Ask for retention consent explicitly before any speaking work, and record it. Do not assume
it from the presence of a microphone.

### A level belongs to one framework

`--framework` must be one the installed pack declares, and `--declared-level` must be a level
*of that framework*. A label from another framework is refused by name: `A2` and `HSK2` are
not interchangeable, and nothing in the system will translate one into the other. If the
learner offers a label from elsewhere, ask which installed framework they mean — never map it
yourself.

## 4. Onboarding is resumable

```bash
linguawiki onboard start  --workspace <path> --mode declared-level --declared-level <level>
linguawiki onboard record --workspace <path> --key <answer-key> --input value.json
linguawiki onboard status --workspace <path> --format json
linguawiki onboard finalize --workspace <path> [--weeks 2] [--item-budget 150] \
  [--calibration-sample 10] [--no-calibration] --format json
```

`start` stores the declared level as a **hypothesis** and seeds one low-confidence
`declared-hypothesis` estimate per dimension. It marks nothing mastered.

`record` accepts only these keys: `self_reported_level`, `can_do_summary`, `study_history`,
`recent_materials`, `weekly_availability`, `priority_dimensions`, `known_gaps`, `audio_setup`,
`notes`. A self-report is a prior, never evidence.

`finalize` resolves the declared band's bundles plus their prerequisites, prepares bounded
resources, builds the calibration queue, and opens the calibration run. Read `status` at any
point to see what is still expected; `next_step` names it.

Report the result honestly: the `plan_label` (a pilot pack yields a *pilot curriculum*),
`unsupported_dimensions`, and the plan's `missing_modalities`. Never present a pilot
curriculum as covering a level.

## 5. Resource preparation is bounded and reversible

```bash
linguawiki resources plan    --workspace <path> --format json
linguawiki resources prepare --workspace <path> [--level <code> ...] [--weeks 2] \
  [--item-budget 150] [--dry-run] --format json
linguawiki resources status  --workspace <path> --format json
```

Plan before preparing. Imported reference items become `unseen` learner state and nothing
more. External sources are **proposed**, never downloaded — pass the proposals to the learner
with their rights status. Every dropped item appears in `skipped` with a reason; a truncated
plan is never a silent one, so read `skipped` before saying a band is covered.

## 6. Prior courses: import, position, audit

```bash
linguawiki curriculum import   --workspace <path> --input outline.json --format json
linguawiki curriculum position --workspace <path> --completed <code> ... --current <code>
linguawiki curriculum audit-start    --workspace <path> [--sample-size 12] --format json
linguawiki curriculum audit-record   --workspace <path> --input results.json --format json
linguawiki curriculum audit-finalize --workspace <path> --format json
```

Store outlines and objectives only. `rights_status` must be `user-authored`,
`personal-use-only`, or `cleared`; never copy course text, exercises, or answer keys into the
outline. Mappings from an objective to a pack item are **declared in the input**, with a
confidence — the system never guesses one, and an unmapped objective stays an explicit gap
that `curriculum show` reports.

Declaring a unit complete moves its mapped items to at most `encountered` with `self-report`
provenance. It creates no recognition or production evidence. The audit then probes a
risk-weighted sample — central prerequisites, recent units, production targets, and a thinner
slice of older material — and `audit-finalize` turns misses into an evidence-gap queue.
Report `untested_targets`: an unprobed target is unverified, not confirmed.

## 7. Hand over a learner model, not a claim

Onboarding produces a *profile*, and the profile is where a self-report stops. Read it back
before saying anything about the learner's level:

```bash
linguawiki estimate show --workspace <path> --format json
linguawiki knowledge search --workspace <path> --level <declared> --limit 20 --format json
```

Every dimension has an `estimate_status`, and it is the first thing to report:

- `provisional` — what a declared level produces. `basis` is `declared-hypothesis` and
  `evidence_count` is `0`. Say "you told me B1, and nothing has tested it yet".
- `not-tested` — no run and no evidence touched this dimension. Not a weak result.
- `estimated` — only after independent observations agree.

Imported reference items are `unseen`. Self-reporting a completed unit moves its mapped items
to `encountered` and no further, with `self-report` provenance. Neither is recognition and
neither is mastery; `knowledge get <item>` shows the stage and, once evidence exists, the
gates and the ceiling behind it.

The audit's misses are work, not a verdict. Turn each confirmed gap into something the learner
will actually meet again:

```bash
linguawiki errors  followup --workspace <path> --kind practice \
  --action "<what to do>" [--target <item>] --format json
linguawiki context session --workspace <path> --format json
```

`context session` is the handoff: it carries the profile, the estimates with their statuses,
the live errors, the due work, and the pack provenance, bounded and with its `omissions`
listed. Hand that to whatever plans the first sessions rather than re-deriving it.

If the learner did work during setup that should count — a placement task, an exercise from
their old course — record it as evidence with an honest origin rather than describing it:

```bash
linguawiki evidence record --workspace <path> --origin import \
  --task-type <shape> --modality <modality> --score <0..1> \
  [--target <item>] [--dimension <name>] [--claim <claim>] --format json
```

The CLI refuses a claim the attempt cannot support and names the claims that task type
*can* produce. Record the weaker one rather than working around the refusal.

See `references/onboarding-modes.md` for choosing a mode and
`references/prior-course-audit.md` for the outline and result payloads.

## Boundaries

- Never record a level the learner claims as a current level. `current_level` comes from
  evidence, and `estimate show` reports `basis` and `estimate_status` so a hypothesis can
  never be read as a measurement.
- Never mark knowledge encountered, recognized, or mastered because a level was declared or a
  unit was claimed. A stage is computed from evidence and capped by the kind of evidence
  behind it; there is no command that sets one directly, and that is deliberate.
- Never describe a `not-tested` dimension as a weakness, and never offer a single global level
  during setup. Nothing has been measured yet.
- Never translate a level label between frameworks, and never infer a framework from a label.
- Never download a proposed source, and never store more than a short permitted reference.
- If a command reports an unsupported mode or an insufficient bank, relay the reason and offer
  the mode that *is* supported. Do not retry with a different flag to get past it.
