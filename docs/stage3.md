# Stage 3 — Knowledge, Evidence, and Learner Model

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)

Previous: [Stage 2 — Language Packs, Onboarding, and Prior-Course Audit](stage2.md)
Next: [Stage 4 — Session Engine and First Polish Vertical](stage4.md)

## Outcome

Implement the evidence-backed learner model. At the end of this stage, LinguaWiki stores a language-agnostic knowledge graph, attempts and atomic evidence, recurring errors, reviewable proficiency estimates, and bounded context bundles. Recognition can never masquerade as spontaneous production.

## Estimate

- Engineering: 8–12 days.
- Rubric/seed refinement: 2–4 days.

## Entry criteria

- Stage 2 exit gate passes.
- A Polish pilot track and at least one synthetic track are initialized.
- Pack content has stable IDs, descriptors, tasks, provenance, and review metadata.

## Work packages

### 3.1 Implement the knowledge graph

- [ ] Add migrations/repositories for knowledge items, aliases, relations, examples, tags, and track item state.
- [ ] Support kinds without assuming inflection, case, alphabet, or whitespace tokenization.
- [ ] Support prerequisite, form/sense, contrast, collocation, example, curriculum, and error-target relations.
- [ ] Preserve pack-stable IDs across pack upgrades.
- [ ] Add deterministic search by ID, alias, tag, level, and relation.
- [ ] Implement `knowledge get|search|upsert|link|merge` with dry-run for merges.

### 3.2 Implement attempts and atomic evidence

- [ ] Add attempt, evidence, observation, and follow-up migrations/repositories.
- [ ] Capture modality, task type, target, normalized score, help level, correction mode, delay, evaluator, and confidence.
- [ ] Keep recognition, comprehension, controlled production, spontaneous production, and delayed transfer distinct.
- [ ] Preserve the learner response or privacy-compatible excerpt needed to justify evidence.
- [ ] Validate that evidence cannot reference an incompatible task/modality.
- [ ] Add immediate standalone evidence commands only for imports/repairs outside live sessions.

### 3.3 Implement the error model

- [ ] Add error patterns, occurrences, successful counter-evidence, and lifecycle state.
- [ ] Deduplicate through category + normalized signature + target item, with uncertain matches requiring review.
- [ ] Implement observed, active, monitoring, resolved, and reactivated transitions.
- [ ] Require controlled, novel, spontaneous, and delayed success according to configurable policy.
- [ ] Ensure one correct response cannot resolve an error.

### 3.4 Implement mastery aggregation

- [ ] Implement the configured stage progression from unseen through stable.
- [ ] Weight spontaneous and delayed evidence above recognition/immediate repetition.
- [ ] Discount hints and low-confidence transcription.
- [ ] Apply recency decay without deleting evidence.
- [ ] Require evidence diversity before increasing confidence.
- [ ] Allow failure to regress item stage and reactivate errors.
- [ ] Version the aggregation algorithm and support recomputation from raw evidence.

### 3.5 Implement multidimensional skill estimates

- [ ] Add current estimates and immutable estimate history.
- [ ] Estimate every supported dimension independently.
- [ ] Preserve `not-tested` separately from low performance.
- [ ] Store numeric estimate, framework band/range, uncertainty, recency, evidence count, and algorithm version.
- [ ] Explain every change using linked evidence and weighting factors.
- [ ] Complete placement finalization using Stage 2 posterior state and Stage 3 evidence.

### 3.6 Implement bounded context bundles

- [ ] Implement `context session|assessment|source|concept`.
- [ ] Include only relevant profile/preferences, estimates, due work, active errors, recent evidence, curriculum/source position, and provenance.
- [ ] Enforce configurable record/token-size limits.
- [ ] Return omissions/counts so an agent knows context was bounded.
- [ ] Never expose raw private transcripts/audio unless the command and consent explicitly require them.

### 3.7 Complete Codex skill slices

- [ ] Make the umbrella skill resolve workspace/user/track and route by explicit intent.
- [ ] Complete `linguawiki-init` against actual learner-model commands.
- [ ] Complete `linguawiki-assess` for calibration/baseline finalization.
- [ ] Add behavioral tests proving no mastery without evidence and no global-level overclaim.

## Verification

```bash
uv run pytest tests/unit/test_mastery.py tests/unit/test_errors.py tests/unit/test_estimates.py
uv run pytest tests/integration/test_evidence_pipeline.py tests/integration/test_context_bundles.py
uv run linguawiki evidence recompute --workspace <fixture> --dry-run --format json
uv run python scripts/validate_skills.py
```

Use property tests for:

- [ ] stage promotion/regression invariants;
- [ ] idempotent recomputation;
- [ ] error resolution/reactivation;
- [ ] estimate uncertainty decreasing only with qualifying independent evidence;
- [ ] no Polish/Chinese language-code branches in core aggregation.

## Exit gate

- [ ] Recognition evidence cannot promote spontaneous production.
- [ ] Delayed failure can regress stage and reactivate an error.
- [ ] Every estimate is reproducible and traceable to evidence plus an algorithm version.
- [ ] Unsupported modalities remain not-tested.
- [ ] Knowledge graph works for both contrasting synthetic packs and Polish pilot content.
- [ ] Context output is bounded, privacy-aware, and sufficient for an agent.
- [ ] Init and assessment skills pass realistic behavioral tests.

## Handoff to Stage 4

Stage 4 receives a complete learner-state model and bounded planning context. It can now build and close real sessions without inventing progress rules inside the learning skill.
