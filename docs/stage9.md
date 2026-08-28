# Stage 9 — Chinese Portability Proof

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)
Previous: [Stage 8](stage8.md)

## Outcome

An independent Chinese learner workspace uses the same core, skills, storage, onboarding, session, evidence, review, Anki, wiki, speaking, backup, and upgrade machinery. Any changes to core are generic capability extensions rather than Chinese special cases.

## Estimate

- Engineering: 8–15 days
- Chinese pilot-pack authoring/review: 10–20 days

## Entry criteria

- Stage 8 exit gate passes.
- The Chinese pilot has an identified domain reviewer.
- Script, locale/region, pronunciation standard, and proficiency framework choices are documented before content creation.

## Work packages

### 9.1 Scope the Chinese track

- [ ] Select `zh-Hans` or `zh-Hant` initially; record any cross-script mapping as an optional capability.
- [ ] Select region and pronunciation standard.
- [ ] Select a declared-level framework supported by the pack (for example an explicit HSK version); do not map CEFR implicitly.
- [ ] Define support languages, learner goals, input-method assumptions, and segmentation policy.

### 9.2 Chinese pilot pack

- [ ] Build a `pilot` pack with enough reviewed items and assessment tasks for declared-level onboarding plus limited calibration.
- [ ] Represent characters, readings/pinyin, tones, segmentation, classifiers/measure words, variants, register, and pronunciation metadata through pack capabilities.
- [ ] Include source/resource manifests and curriculum mappings for the selected framework.
- [ ] Apply the same origin, review-axis, risk-tier, generation-batch, sampling, quarantine, and invalidation rules as Polish.

### 9.3 Portability audit

- [ ] Search schemas, Python, SQL, skills, templates, fixtures, and wiki rendering for Polish/CEFR/Latin/whitespace assumptions.
- [ ] Replace assumptions with capability queries or pack-owned adapters.
- [ ] Ensure identifiers and deduplication do not depend on lowercased whitespace-delimited text.
- [ ] Ensure romanization is an annotation, not a replacement for native script.
- [ ] Keep framework levels opaque to core except for pack-declared ordering/prerequisites.

### 9.4 Independent learner workspace

- [ ] Generate a private `ChineseLinguaWiki`; do not branch or reuse `PolishLinguaWiki` learner state.
- [ ] Pin the same core release and the Chinese pack separately.
- [ ] Complete declared-level onboarding and limited calibration.
- [ ] Run multiple sessions spanning characters, listening, tones, grammar, reading, speaking, and review.
- [ ] Snapshot sanitized wiki history and verify external backups.

### 9.5 Cross-pack contract suite

- [ ] Run core tests against Polish, Chinese, and both synthetic contrasting packs.
- [ ] Validate segmentation, Unicode normalization, script variants, pinyin/tones, TSV export, transcript layers, and search/deduplication.
- [ ] Assert the same CLI and skill behavioral contracts for both real packs.
- [ ] Add every discovered portability defect as a permanent regression fixture.

## Verification

- [ ] Complete `workspace init -> onboarding -> plan -> lesson -> speaking ingestion -> close -> review -> Anki -> wiki -> snapshot` in `ChineseLinguaWiki`.
- [ ] Verify script, romanization, tone, and segmentation data survive DB, JSON, Markdown, and TSV round trips.
- [ ] Verify the selected framework accepts only pack-declared levels and is never silently translated to CEFR.
- [ ] Verify no Polish schema/workflow assumption blocks Chinese.
- [ ] Verify core changes made during this stage are justified by a generic capability and covered by contrasting fixtures.
- [ ] Run the repository-wide quality gate and cross-pack skill evaluations.

## Exit gate

Stage 9 is complete when Polish and Chinese workspaces independently use one core release, language-specific behavior resides in packs/adapters, and the end-to-end learner workflow succeeds for Chinese without Polish-specific branches in core.

## Final deliverables

- Versioned LinguaWiki core release.
- Independently versioned Polish and Chinese pack releases with explicit maturity labels.
- Private, independently upgradeable `PolishLinguaWiki` and `ChineseLinguaWiki` learner repositories.
- Passing cross-pack contract, recovery, privacy, and skill-behavior suites.
