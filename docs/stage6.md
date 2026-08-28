# Stage 6 — Review and Anki MVP

Parent: [LinguaWiki Implementation Plan](LinguaWiki%20Implementation%20Plan.md)
Previous: [Stage 5](stage5.md) · Next: [Stage 7](stage7.md)

## Outcome

Due evidence and recurring errors produce a bounded, explainable review queue. High-value items can be reviewed by the learner and exported to Anki with stable identities and no duplicate cards.

## Estimate

- Engineering: 5–8 days
- Review/rubric refinement: 2–4 days

## Entry criteria

- Stage 5 exit gate passes.
- Dogfooding has produced enough evidence, errors, and source context to exercise review selection.

## Work packages

### 6.1 Review scheduling

- [ ] Add review-task and review-event migrations.
- [ ] Prioritize by due state, forgetting risk, error severity/recurrence, learning goal, usefulness, production gap, and recent load.
- [ ] Apply daily time/card caps and novelty limits.
- [ ] Explain why each task is due and which evidence caused it.
- [ ] Feed review results back through normal attempt/evidence paths.

### 6.2 Error lifecycle

- [ ] Connect `observed -> active -> improving -> resolved -> reactivated` transitions to review outcomes.
- [ ] Escalate repeated failures from light review to a richer diagnostic/remediation task.
- [ ] Keep corrections, evidence, and selected remediation traceable.

### 6.3 Anki candidate lifecycle

- [ ] Add candidate, note, validation, decision, export-batch, and manifest records.
- [ ] Define language-pack card-template capabilities rather than hard-coded Polish note types.
- [ ] Generate only bounded candidates with usefulness and evidence links.
- [ ] Require explicit learner approval; support edit, reject, and suppress.
- [ ] Deduplicate on stable semantic identity, not surface text alone.

### 6.4 TSV export

- [ ] Export only approved notes as UTF-8 TSV with stable IDs, deterministic field order, escaped tabs/newlines, and pack-defined tags.
- [ ] Write a manifest containing hashes, schema/template versions, and source note IDs.
- [ ] Make re-export update existing notes rather than create duplicates.
- [ ] Detect template or content changes and preview their effect.

### 6.5 Skills

- [ ] Implement `linguawiki-review` to request a bounded queue, run reviews, and record outcomes.
- [ ] Implement `linguawiki-anki` to inspect, approve/edit/reject, validate, preview, and export candidates.
- [ ] Keep card generation deterministic where possible and label AI-authored fields with provenance.

## Required commands

```text
linguawiki review due|start|record|explain
linguawiki anki candidates|approve|edit|reject|validate|preview|export
```

## Verification

- [ ] Test ordering and caps with fixed clocks and deterministic fixtures.
- [ ] Verify delayed failure can reactivate a resolved error.
- [ ] Verify repeated failure creates a richer review task.
- [ ] Verify rejected/unapproved notes never export.
- [ ] Verify repeated export preserves stable IDs and produces no duplicates.
- [ ] Round-trip TSV fixtures containing Unicode, tabs, newlines, HTML, pinyin, and diacritics.
- [ ] Run the repository-wide quality gate.

## Exit gate

Stage 6 is complete when a normal dogfooding day can run `due review -> evidence/error updates -> bounded Anki approval -> deterministic export` without direct SQL or hand-edited manifests.

## Handoff to Stage 7

Stage 7 makes the accumulated system understandable, reproducible, upgradeable, and safe to operate for months.
