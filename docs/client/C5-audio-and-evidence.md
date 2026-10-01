---
title: "C5 — Audio, and the claims that rest on it"
stage: C5
status: blocked
depends_on: [C4]
---

# C5 — Audio, and the claims that rest on it

Parent: [Learner Client Delivery Plan](../learner-client-plan.md)

**Goal.** Pronunciation and speaking become answerable in the browser, and no assessment claim
outlives the recording it rests on.

**Two hazards, both verified.** `assessment_results` has **no artifact column**, and
`artifacts.dependent_observations` finds dependents only through
`pronunciation_observations.audio_artifact_id` — so a purge today would delete the recording
and leave the score standing. And **nothing scans private roots for unregistered files**: a
recording written but not registered is accounted for by nothing. This repository already
names that failure — *"skipping it left the bytes sitting unregistered under an ignored
directory, accounted for by nothing"* (`services/artifacts.py`) — and Stage 5 closed it for
package ingestion. Do not reopen it through the browser.

## 0. Inherited from C2 — pack-shipped recordings

C2 built the pack side of audio and left two things here, deliberately.

- [ ] **Install pack assets into a table.** `served_task` resolves a served recording by
      loading the whole pack from `pack_installations.source_path` to read one digest.
      That is correct -- what comes back is checked against the snapshotted digest rather
      than trusted -- and it was cheap while no pack shipped audio. Once one does, every
      audio task read pays for a full `load_pack`. Store `(content_id, path, sha256,
      media_type, duration_ms)` at install time and resolve from there, keeping the
      digest comparison: the point was never where the file is, it is whether the bytes
      are the ones the learner heard.
- [ ] **Decide whether an asset becomes learner content.** C2 deliberately keeps a
      recording out of `content_records`: its `content_kind` is a closed CHECK on the
      most-referenced table in the schema, DuckDB cannot re-constrain it, and rebuilding
      it for zero rows was not worth doing while nothing in a learner database referenced
      a recording. `LoadedPack.installable_items` is where that split is stated. If C5
      wants `db check`, a rights audit, or a purge to reason about pack audio from the
      database, that is the migration to weigh.
- [ ] **The pilot's six listening recordings.** `pl-pilot` 0.2.0 ships none, so its six
      audio tasks still carry the spoken sentence in the prompt and declare no asset --
      what they measure today is reading a sentence somebody transcribed. The contract,
      the catalog, the validation and the refusals are all in place and tested;
      `tests/language_packs/test_pilot_presentation.py` pins the gap so it stays visible.
      Producing the recordings, with their rights and origin class, is what closes it.

## 1. Capture, with consent first

- [ ] Press to start, press to stop. **No countdown.**
- [ ] Check the track's recording consent **before any bytes are persisted**. A track that
      does not permit recording never produces a file to clean up.
- [ ] Surface the track's retention setting on the screen; never override it.

## 2. Two-phase capture with an owner

Registration can fail — `writer_locked`, unknown track, hash mismatch, retention conflict —
and these bytes were created by the client rather than supplied by the learner.

- [ ] Bytes land first in a **client-owned staging location whose ownership is recorded**, not
      directly at their final path.
- [ ] Registration through `artifacts.register` promotes them.
- [ ] A refusal **removes them and reports why**.
- [ ] Retry `writer_locked` and `database_busy` before abandoning: they are transient and the
      learner has already spoken.
- [ ] A disconnection mid-upload, or a server restart with staged bytes present, resolves in
      one direction or the other — completed or removed. Nothing is left for a later reader to
      guess about.

## 3. Result-to-artifact relationship

- [ ] Migration: an audio reference on `assessment_results`.
- [ ] Teach `artifacts.dependent_observations` to find assessment results as well as
      pronunciation observations.
- [ ] **Retained-audio validation at judging time**: a pronunciation or spoken-production
      score may not be recorded against audio that is absent or not retained.

## 4. Invalidation

- [ ] The result is **marked invalidated, not deleted** — a learner told their vowel was wrong
      deserves to see the evidence is gone.
- [ ] The dimension's posterior is recomputed from surviving results.
- [ ] A finalized estimate derived from it is marked as resting on withdrawn evidence.
- [ ] **Baseline selection must preserve assessment chronology.** `estimates.py` picks a
      baseline with `ORDER BY state.updated_at DESC, state.run_id DESC`. Recomputing an older
      run after a purge refreshes `updated_at` and would let it displace a newer calibration.
      Order by the run's own chronology instead.

## 5. Tests — `tests/integration/test_client_audio.py`

- [ ] Audio recorded through the client lands as an artifact `artifact verify` reports present.
- [ ] Purging it invalidates every assessment result resting on it, and the affected estimates
      say so.
- [ ] Recording a score against absent or non-retained audio is refused.
- [ ] **Two-run purge regression**: an older run recomputed after a purge does not become the
      baseline over a newer calibration.
- [ ] **Failure paths, beside the success path** — registration refused after the bytes were
      written; disconnection mid-capture; restart with staged bytes present. After each,
      assert **no file exists under a private root that no row accounts for**.
- [ ] A track forbidding recording never reaches the point of writing a file.

## Gate

- [ ] `./.tools/uv run python scripts/verify.py`
- [ ] `linguawiki privacy audit` on a scratch workspace after the failure-path tests

## Done when

A pronunciation task is answered in the browser, purged afterwards, and the resulting state is
honest about what it no longer knows — with nothing left on disk that nothing accounts for.
