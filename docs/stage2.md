# Stage 2 — Language Packs, Onboarding, and Prior-Course Audit

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)

Previous: [Stage 1 — Learner Workspace and Storage Foundation](stage1.md)
Next: [Stage 3 — Knowledge, Evidence, and Learner Model](stage3.md)

## Outcome

Make a new learner workspace useful at a declared level and capable of a bounded calibration. This stage delivers pack authoring/validation, a thin Polish A2 pilot slice, user/track setup, framework-safe onboarding, and generic prior-course audit without claiming that pilot content supports comprehensive placement.

## Estimate

- Engineering: 8–12 days.
- Polish pilot authoring/review: 5–10 days in parallel.

## Entry criteria

- Stage 1 exit gate passes.
- An independent empty `PolishLinguaWiki` fixture exists.
- Pack provenance/review and maturity policies from the parent plan are accepted.

## Work packages

### 2.1 Implement the pack contract

- [ ] Validate `manifest.yaml`, capabilities, supported frameworks/levels, source policy, licenses, checksums, and bundle dependencies.
- [ ] Validate knowledge seed, relation, example, assessment, prompt, and resource-bundle formats.
- [ ] Implement pack maturity values: `fixture`, `pilot`, `onboarding-ready`, and `placement-ready`.
- [ ] Implement content-addressed immutable pack versions.
- [ ] Implement `pack validate|install|diff|update|list|coverage|publish`.
- [ ] Make reinstall idempotent and updates dry-run first.
- [ ] Never overwrite learner-created content during a pack update.

### 2.2 Implement provenance and review tooling

- [ ] Support origin classes: authentic, learner-produced, source-derived, AI-adapted, AI-generated, synthetic-media, and human-authored.
- [ ] Track linguistic, pedagogical, source-alignment, rights, and privacy review independently.
- [ ] Implement content lifecycle through `approved-personal`, `verified`, `publication-ready`, `needs-review`, and terminal states.
- [ ] Implement risk tiers 0–4 and promotion gates per content kind.
- [ ] Implement prompt-template versioning, first-three-run full review, stable-template sampling, and batch quarantine.
- [ ] Implement dependency-hash invalidation.
- [ ] Add `pack author generate-draft|import|review-queue|review|approve|reject|invalidate`.
- [ ] Add `pack template validate|stabilize|quarantine`.

### 2.3 Build the thin Polish A2 pilot slice

Create a deliberately bounded `pilot` pack with at least:

- [ ] 60–100 reviewed knowledge targets across four to six practical themes;
- [ ] 15–25 grammar/construction targets;
- [ ] 10–20 pronunciation/orthography targets;
- [ ] 24 objective diagnostic items across supported dimensions;
- [ ] eight productive prompts with versioned rubrics;
- [ ] six activity templates across at least four modes;
- [ ] one reviewed source recommendation per supported media modality.

Additional requirements:

- [ ] Every persistent item has origin, review axes, rights/privacy, content hash, and stable ID.
- [ ] AI drafts remain visibly unverified until their required reviews pass.
- [ ] The pack advertises only its covered themes/dimensions.
- [ ] It refuses to label itself onboarding-ready or placement-ready.
- [ ] Keep it in core only as a temporary dogfood pack until the contract stabilizes.

### 2.4 Implement user and track setup

- [ ] Implement `user create|show|update|list`.
- [ ] Implement `track create|show|update|activate|archive`.
- [ ] Capture timezone, target/support languages, goals, schedule, interests, correction preferences, voice capability, retention consent, and prior materials.
- [ ] Validate BCP-47 target language/script/region through the installed pack.
- [ ] Require one pack-declared proficiency framework.
- [ ] Reject or explicitly map level labels from another framework; never infer CEFR-to-HSK mappings.

### 2.5 Implement declared-level onboarding

- [ ] Implement resumable `onboard start|record|status|finalize` state.
- [ ] Store declared level as a low-confidence hypothesis.
- [ ] Resolve the selected band plus prerequisites recursively.
- [ ] Import reference items as `unseen`; never infer mastery.
- [ ] Build a bounded calibration queue sampling prerequisites and declared-band targets.
- [ ] Prepare a two-week pilot plan within the pack's actual theme coverage.
- [ ] Clearly label unsupported skills and missing resources.

### 2.6 Implement placement state and bounded calibration

- [ ] Persist per-dimension priors/posteriors, task exposure, content-family coverage, budgets, and stop reason.
- [ ] Implement the ordinal Bayesian staircase and fixed v1 response curve defined in the parent plan.
- [ ] Enforce minimum/maximum task budgets and the 80%/one-band stop criterion.
- [ ] Prevent item reuse within six months except longitudinal anchors.
- [ ] Allow pause/resume and fatigue/requested stop.
- [ ] Refuse comprehensive placement where the pack is not placement-ready; run only labeled pilot calibration.

### 2.7 Implement generic prior-course audit

- [ ] Implement `curriculum import|show|position|audit-start|audit-record|audit-finalize`.
- [ ] Import only legally permissible outlines or user-authored objectives.
- [ ] Map course objectives to pack items/descriptors and preserve unmapped gaps.
- [ ] Convert claimed completed units to at most `encountered` state with self-report provenance.
- [ ] Select a risk-weighted audit sample of prerequisites, recent units, and production targets.
- [ ] Produce an evidence gap/calibration queue rather than granting mastery.

### 2.8 Add Codex skills

- [ ] Implement and validate `linguawiki-pack`.
- [ ] Implement the workspace/track/onboarding slice of `linguawiki-init`.
- [ ] Add the placement-state shell of `linguawiki-assess`; full evidence aggregation lands in Stage 3.
- [ ] Ensure skills call CLI JSON contracts and never edit pack state or DuckDB directly.

## Verification

```bash
uv run linguawiki pack validate language-packs/pl-pilot
uv run linguawiki pack coverage language-packs/pl-pilot --format json
uv run pytest tests/language_packs tests/integration/test_onboarding.py tests/integration/test_curriculum_audit.py
uv run python scripts/validate_skills.py
```

## Exit gate

- [ ] Polish CEFR A2 track initializes inside `PolishLinguaWiki` with Russian/English support.
- [ ] Declared A2 loads bounded prerequisites/resources without marking knowledge mastered.
- [ ] Pilot calibration pauses/resumes and reports uncertainty and unsupported dimensions.
- [ ] Comprehensive placement refuses insufficient bank coverage.
- [ ] Prior-course completion creates only encountered items plus an audit queue.
- [ ] Every pilot item passes its risk-tier metadata gate.
- [ ] A defective sampled AI batch quarantines its template and dependents.
- [ ] Pack reinstall is idempotent and update preview is deterministic.

## Handoff to Stage 3

Stage 3 receives an initialized learner/track, installed pilot pack, curriculum position, calibration tasks, provenance-aware durable content, and persisted assessment state. It adds the evidence and mastery machinery that turns performance into explainable progress.
