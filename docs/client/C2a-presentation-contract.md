---
title: "C2a — Presentation contract and pack authoring"
stage: C2a
status: ready
depends_on: []
---

# C2a — Presentation contract and pack authoring

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** A task carries enough structure for a UI to render it without parsing prose, and
the pack files carry it. Nothing is installed or served in this stage — that is
[C2b](C2b-served-presentation-snapshot.md).

**Why it is shaped this way.** `PackAssessmentTask` has `prompt: str`, the `expected:
AnswerKey | None` C1 typed, and nothing that says how a task is *shown*. `pl-pilot` encodes
options inside the prompt — `"Which is correct? 'pięć bilety' / 'pięć biletów' / 'pięć
biletu'"` — and its six audio tasks carry the spoken text in `prompt` because the pack has no
way to reference a recording, and ships none. No read model can invent those facts. **Most of
this stage is authoring, and it is the schedule risk.** It depends on nothing and can start
immediately.

## 0. Confirm the starting point

- [ ] `PackAssessmentTask.expected` is `AnswerKey | None` (C1) and there is no presentation
      field. If either has changed, stop and re-read this stage.
- [ ] Three packs are shipped: `language-packs/pl-pilot` and the two fixtures
      `language-packs/fixtures/inflected` and `language-packs/fixtures/tonal`. Only `pl-pilot`
      is re-authored here. Both fixtures hold `objective` and `audio` tasks and stay
      untouched, which is what makes them the unchanged-legacy-pack case in §2 and §7.
- [ ] `language-packs/` holds no audio file of any kind, and `packs/format.py` has no asset
      concept. §3 creates one; it is not an extension of anything.

## 1. The contract

- [ ] Extend `PackAssessmentTask` in `src/linguawiki/contracts.py` with an optional, versioned
      presentation record carrying its own `presentation_version: int = 1`:
  - typed `choices` — each a `value` plus an optional `display` form — and whether order is
    fixed or may be shuffled
  - an **asset reference** for audio tasks: the asset's stable key, resolved against the
    catalog §3 defines. Identity is the key *and* the digest; see §3.
  - `replay_allowance` — unlimited, or a count
  - the expected response shape for `short-response`
- [ ] Distinguish **multiple-choice** from **deterministically scored free text**. Both are
      machine-scorable; they render differently. Conflating them was an earlier error.
- [ ] Absent presentation is legal and means *render as free text*. The client never parses
      prose to recover structure. An audio task with no asset reference also stays legal: its
      prompt carries the text, as all six do today, and the two fixture packs depend on that
      remaining true.
- [ ] Constrain the **type**, not the field. `json_schema_extra={"enum": [...]}` is invisible
      at runtime; every vocabulary here — presentation kind, order — is an `Annotated[str,
      ...]` marker so the runtime check and the published enum are the same object.

### What a choice submits, and how it is scored

The scorer is `placement.score_response`: it folds the response with `scoring_form` and
compares it against the `answers` in the served `AnswerKey`. It does not know what a choice
is, and this stage does not teach it.

- [ ] **The client submits the chosen `value`, verbatim.** Not an index, not an opaque option
      id. A mapping from option id back to an accepted answer would be a second place the
      right answer is recorded, and the two would drift; C2b would then have to snapshot the
      mapping as well.
- [ ] `display` is presentation only, defaults to `value`, and is never scored. This is what
      lets a choice read `pięć biletów (5 tickets)` while `value` stays `pięć biletów`.
- [ ] `score_response` is unchanged by this stage. If it needs a change, this rule is wrong
      and the stage is wrong.

## 2. Hash stability for packs that are not re-authored

`pack_item_hash` hashes `payload.model_dump(mode="json")` whole — deliberately, so a field
added later is covered without anyone remembering. That is also why adding
`presentation: … | None = None` writes `"presentation": null` into every existing task's
canonical payload and **moves every existing item hash**. Reproduced: the same model with and
without the new optional field dumps differently. A pack whose content nobody edited would
then fail `pack validate`, and its reviews would detach from content nobody reviewed — the
false positive the hash exists to avoid.

- [ ] Omit `presentation` from the canonical payload when it is absent. Scope the rule to the
      field: either a serializer on `PackAssessmentTask` or an explicit omission set in
      `pack_item_hash` beside `UNHASHED_PROVENANCE_FIELDS`.
- [ ] **Not `exclude_none=True` on the dump.** Rubric-scored tasks already serialize
      `"expected": null`, so a blanket rule would move *those* hashes instead and cause the
      defect it was added to prevent.
- [ ] Write the rule down where the next optional field will be added: a field introduced
      after the hash schema was frozen is omitted from the canonical payload when it holds its
      introduction-time default. `PACK_ITEM_HASH_SCHEMA` stays `lingua.pack.item-hash.v1`,
      because under this rule the hash of unchanged content is unchanged.
- [ ] `pl-pilot`'s hashes *do* move, because its content genuinely changes. That is §6, and it
      is a re-stamp of edited items rather than a re-stamp forced by the contract.

## 3. Pack audio assets

Lines that reference "a clip" assume a resolver that does not exist. A stable id alone is not
enough: it can resolve to different bytes after a pack update, which defeats C2b's *same
recording or refuse* guarantee before C2b is written.

- [ ] Add an asset catalog as a pack file, assigned by declared path like every other:
      `assets/` alongside `assessments/` and `resource-bundles/`, with `ASSET_KIND = "asset"`
      beside the existing kinds in `packs/format.py`.
- [ ] Each entry declares `asset_key`, pack-relative `path`, `media_type`, `sha256`,
      `duration_ms`, and a `PackItemProvenance` — a recording is content with an origin class
      and a rights class, governed by the pack's `source-policy.json` like any other.
- [ ] **Identity is `(asset id, sha256)`.** The id is `content_id_for(pack_key, ASSET_KIND,
      asset_key)` and the digest is the version. Both are what C2b snapshots and compares; an
      id alone answers "which recording was meant", not "is this the recording they heard".
- [ ] **Open the file; do not believe the entry.** `pack validate` and `pack stamp` hash the
      bytes and refuse a declared digest that does not match, a `path` that resolves to
      nothing, and a `path` that resolves outside the pack root — resolved containment, not a
      lexical `..` check, since a symlink passes the lexical one. `services/artifacts.py`
      already answers this shape of question and its distinctions — absent, escaping,
      unreadable, unresolvable — are the ones to reuse rather than re-derive.
- [ ] Assets are part of the pack's content hash surface: an item referencing an asset
      declares it as a dependency, so replacing the recording re-stamps the task that plays it.
- [ ] The pack ships in the wheel, so binary assets must be included in the distribution.
      Confirm with the release gate's distribution check, not by inspection.

### The recordings themselves

Six `pl-pilot` listening tasks need audio that does not exist. This is a content deliverable,
not a checkbox, and it is the second schedule risk in this stage.

- [ ] Produce six recordings of the sentences currently embedded in those prompts, with their
      rights and origin class recorded in `source-policy.json` terms.
- [ ] **If they cannot be produced in this stage**, say so explicitly and leave those tasks
      without an asset reference and with their prompts unchanged: legal under §1, and the
      pack is no worse than today. Do **not** change their `modality` to `text` to make the
      gap disappear — `placement.REQUIRED_MODALITIES` requires `audio` for the receptive
      dimension, so that edit silently makes a dimension untestable rather than untested.

## 4. Schemas

- [ ] `./.tools/uv run python scripts/generate_schemas.py`
- [ ] Review the diff in `schemas/lingua.pack.assessment.v1.json` before committing it, and
      the new asset-catalog schema beside it.
- [ ] Validate a model-produced task against the checked-in schema twice — once with only
      required fields, so the defaults are what is under test, and once with every optional
      field set. A nullable field whose `json_schema_extra` sits at the field's top level is
      read conjunctively with its own `anyOf`, which is how a valid model dump stopped
      validating against its own contract before.

## 5. Pack validation

Each refusal carries its own code; collapsing them into one generic code takes away a caller's
ability to tell a bank defect from a missing file.

- [ ] Reject a presentation record that disagrees with its `task_type` or `modality`: choices
      on anything but `objective`, an asset reference on a task whose modality is not `audio`,
      a replay allowance where nothing is played.
- [ ] Reject an `objective` task whose choices do not resolve its answer key: **exactly one**
      choice `value` must fold-match — `placement.scoring_form`, the comparison that will
      actually score it — an entry in `expected.answers`. Zero matches is a task nobody can
      answer correctly; two is a task with two correct buttons. Both are bank defects, and
      this is the cheapest place to catch them.
- [ ] Reject choice values that are blank after stripping, or that collide under
      `scoring_form`. `min_length=1` accepts `"   "`, and two buttons that fold to one string
      are one answer shown twice.
- [ ] Reject an asset reference that names no catalog entry, and a catalog entry whose file is
      missing, altered, escaping the pack root, or unreadable — each by name.

## 6. Author `pl-pilot`

- [ ] Split every objective prompt into a real prompt plus typed choices, with the correct
      choice's `value` matching the answer key under `scoring_form`. Where the key currently
      holds free-text alternatives — `pl.task.listening.02` accepts `10`, `dziesięć`,
      `dziesięć minut`, `ten minutes` — the choice values are what the learner sees and
      submits, and the key must accept exactly one of them.
- [ ] Give audio tasks an asset reference and move spoken text out of `prompt`, for each task
      whose recording §3 produced. A task without one keeps its prompt, and the omission is
      recorded rather than left to be read as an oversight.
- [ ] Mark genuinely free-response tasks explicitly, so "no presentation" is a decision rather
      than an omission.
- [ ] Re-stamp content hashes: `linguawiki pack stamp`, then `pack validate`.
- [ ] Note in the pack's own notes that presentation was added, so the version bump is
      explicable.
- [ ] The fixture packs are not re-authored and not re-stamped.

## 7. Tests — `tests/language_packs/` and `tests/contracts/`

- [ ] **Unchanged packs keep their hashes.** Both fixture packs validate against their
      existing declared hashes after the contract change, with no re-stamp. Pin one fixture
      task's derived hash as a literal, so the next optional field that moves it fails here
      rather than in somebody's workspace.
- [ ] Every `pl-pilot` objective task exposes typed choices or is explicitly free-text.
- [ ] A task with no presentation record round-trips as free text rather than failing.
- [ ] **The correct button scores.** Take a re-authored `pl-pilot` objective task, submit the
      correct choice's `value` through `placement.score_response` against that task's
      `expected`, and assert 1.0; submit another choice's value and assert 0.0.
- [ ] Each validation refusal in §5 is asserted by name, including zero and two matching
      choices, colliding values, and a blank one.
- [ ] Asset resolution: a declared asset whose file is missing, whose bytes differ from the
      declared digest, and whose path is a symlink out of the pack root are each refused by
      name; a valid one resolves to its `(id, digest)`.
- [ ] The generated schema snapshots are current.

## Gate

- [ ] `./.tools/uv run python scripts/generate_schemas.py --check`
- [ ] `./.tools/uv run python scripts/verify.py`

## Done when

Every task the pilot pack can serve carries, *in its pack file*, the structure needed to
render it as buttons or as an honest free-text field, decided by data rather than by parsing;
every audio task either resolves to a verified recording or honestly declares it has none; and
the two packs nobody edited still validate with the hashes they shipped with. Getting that
structure into the database and onto a served task is C2b.
