---
title: "C2a — Presentation contract and pack authoring"
stage: C2a
status: ready
depends_on: []
---

# C2a — Presentation contract and pack authoring

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** A task carries enough structure for a UI to render it without parsing prose.

**Why it is shaped this way.** `PackAssessmentTask` has `prompt: str` and `expected: dict` and
nothing else. `pl-pilot` encodes options inside the prompt — `"Which is correct? 'pięć bilety'
/ 'pięć biletów' / 'pięć biletu'"` — and audio tasks carry the spoken text in `prompt` rather
than referencing a clip. No read model can invent those facts. **Most of this stage is
authoring, and it is the schedule risk.** It depends on nothing and can start immediately.

## 1. The contract

- [ ] Extend `PackAssessmentTask` in `src/linguawiki/contracts.py` with an optional, versioned
      presentation record:
  - typed `choices` (value plus display form), and whether order is fixed or may be shuffled
  - an **asset identity** for audio tasks — a stable id, not only a path
  - `replay_allowance` (unlimited, or a count)
  - the expected response shape for `short-response`
- [ ] Distinguish **multiple-choice** from **deterministically scored free text**. Both are
      machine-scorable; they render differently. Conflating them was an earlier error.
- [ ] Absent presentation is legal and means *render as free text*. The client never parses
      prose to recover structure.

## 2. Schemas

- [ ] `./.tools/uv run python scripts/generate_schemas.py`
- [ ] Review the diff in `schemas/lingua.pack.assessment.v1.json` before committing it.

## 3. Pack validation

- [ ] Reject a presentation record that disagrees with its `task_type` — choices on an
      `extended-productive` task, a clip reference on a text task, a replay allowance where
      nothing is played.
- [ ] Reject an `objective` task whose choices do not contain its `expected` answer. A bank
      that cannot be answered correctly is a bank defect, and this is the cheapest place to
      catch it.
- [ ] Reject an audio task whose asset identity resolves to nothing.

## 4. Author `pl-pilot`

- [ ] Split every objective prompt into a real prompt plus typed choices.
- [ ] Give audio tasks a clip reference and move spoken text out of `prompt`.
- [ ] Mark genuinely free-response tasks explicitly, so "no presentation" is a decision rather
      than an omission.
- [ ] Re-stamp content hashes: `linguawiki pack stamp`, then `pack validate`.
- [ ] Note in the pack's own notes that presentation was added, so the version bump is
      explicable.

## 5. Tests — `tests/language_packs/` and `tests/contracts/`

- [ ] Every `pl-pilot` objective task exposes typed choices or is explicitly free-text.
- [ ] A task with no presentation record round-trips as free text rather than failing.
- [ ] Each validation refusal above is asserted by name.
- [ ] The generated schema snapshot is current.

## Gate

- [ ] `./.tools/uv run python scripts/generate_schemas.py --check`
- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

Every task the pilot pack can serve is renderable as buttons or as an honest free-text field,
decided by data rather than by parsing.
