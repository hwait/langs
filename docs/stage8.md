# Stage 8 — Four-Week Polish Hardening and Pack Maturity

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)
Previous: [Stage 7](stage7.md) · Next: [Stage 9](stage9.md)

## Outcome

The operational MVP survives four weeks of real Polish learning, and the Polish pack advances from a thin pilot toward an explicitly selected A1–B1 maturity gate based on measured coverage—not optimistic labels.

## Estimate

- Engineering: 5–10 days distributed across four calendar weeks
- Remaining Polish A1–B1 authoring/review: 30–60+ days, depending on the selected gate

## Entry criteria

- Stage 7 exit gate passes.
- `PolishLinguaWiki` is private, backed up externally, and already in regular use.
- The team has chosen `onboarding-ready` or `placement-ready` as the target pack gate for this stage.
- **The learner client (C1–C7) has landed.** See the [Learner Client Delivery Plan](learner-client-plan.md). Four weeks of learning conducted through chat would spend most of its budget on work a string comparison can do, and would measure the interface as much as the learner. The requirements for that client came out of an hour of real study, so the evidence for building it first already exists.

## Work packages

### 8.1 Dogfooding protocol

- [ ] Run at least five sessions per week for four weeks across multiple durations and modes.
- [ ] Include reading, listening, speaking, writing, review, and Anki workflows.
- [ ] Use normal skills/CLI only; record any need for direct DB edits as a release-blocking defect.
- [ ] Take reviewed learner-repo snapshots and external native/portable backups on schedule.

### 8.2 Evaluation and instrumentation

- [ ] Collect planner acceptance/override reasons, context size, command latency, recovery incidents, estimate volatility, review load, candidate approval rate, and skill/tool failures.
- [ ] Add golden conversational evaluations for all skills.
- [ ] Evaluate explanation quality: the learner must be able to trace priorities and estimates to evidence.
- [ ] Keep analytics local and privacy-preserving.

### 8.3 Polish pack expansion

- [ ] Expand knowledge, grammar, vocabulary, pronunciation, cultural/register, curriculum, and resource coverage level by level.
- [ ] Expand assessment banks per skill/dimension/form until the chosen readiness thresholds pass.
- [ ] Run AI-assisted drafting only through item-level provenance, review axes, risk tiers, template inspection, sampling, quarantine, and dependency invalidation.
- [ ] Use qualified human review for high-risk pronunciation, assessment, and normative claims.
- [ ] Publish coverage reports showing supported and unsupported levels/dimensions.

### 8.4 Weekly hardening loops

- [ ] Week 1: fix state-loss, idempotency, migration, privacy, and blocking usability defects.
- [ ] Week 2: tune planner balance, bounded context, source continuity, and missed-session behavior.
- [ ] Week 3: tune evidence weighting, uncertainty, review workload, and Anki candidate quality.
- [ ] Week 4: freeze release candidates, rehearse backup/restore and upgrade, and complete regression/evaluation passes.
- [ ] Triage pack defects separately from generic core defects.

### 8.5 Release readiness

- [ ] Pin the supported DuckDB/Python/core/pack versions and regenerate locks.
- [ ] Publish recovery and upgrade notes.
- [ ] Record known limitations and maturity labels in machine-readable manifests and human docs.
- [ ] Tag core and Polish pack releases only after their independent gates pass.

## Verification

- [ ] Demonstrate five sessions/week without corruption or manual data repair.
- [ ] Demonstrate recovery from a staged session, native backup, and portable export.
- [ ] Verify the planner adapts to changed time, fatigue, and missed sessions.
- [ ] Verify every priority/estimate has inspectable evidence and algorithm metadata.
- [ ] Verify maintenance finds seeded stale/contradictory items.
- [ ] Pass the selected Polish A1–B1 pack coverage/review gate; label any unsupported dimensions explicitly.
- [ ] Run the repository-wide quality gate and all skill behavioral evaluations.

## Exit gate

Stage 8 is complete only after four calendar weeks of successful ordinary use and an explicit Polish-pack maturity decision backed by coverage/review reports. Calendar completion alone is insufficient, and unsupported placement dimensions must remain unavailable rather than guessed.

## Handoff to Stage 9

Stage 9 uses Chinese to test whether the supposedly generic contracts are truly independent of Polish, whitespace tokenization, Latin script, and CEFR-only assumptions.
