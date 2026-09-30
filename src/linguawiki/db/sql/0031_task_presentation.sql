-- How a task is shown: in the bank, and as the learner was actually shown it.
--
-- One file covers both tables on purpose. Splitting them permits a released state in
-- which the bank holds a presentation no serve can snapshot -- the half-state the
-- snapshot exists to prevent -- and a state in which a snapshot column exists with no
-- source to fill it.
--
-- `assessment_tasks` is the bank. The installer writes an enumerated column list, so
-- before this a pack carrying a presentation installed into a bank that discarded it and
-- there was nothing for a serve to snapshot. NULL means *this task renders as free
-- text*, which is exactly what every task in every pack did before this migration -- so
-- there is no legacy hole here and nothing to backfill from.
ALTER TABLE assessment_tasks ADD COLUMN presentation_json VARCHAR;

-- `assessment_run_tasks` is the record of what was served. A pack is mutable and a run
-- is not: without this, a pack update between the serve and the resume would show new
-- choices against the answer key 0030 snapshotted, and a replaced recording would ask a
-- different question under the same identity.
--
-- The asset identity is stored *beside* the presentation rather than inside it because
-- it is a different fact. The presentation says which recording was meant, by key; the
-- identity says which bytes the learner actually heard, by digest. A key alone resolves
-- to whatever currently answers to it, which is the substitution this refuses.
--
-- These two are one group, on 0030's pattern: an asset identity with no presentation, or
-- an audio presentation with no asset identity, is damage rather than a legacy row, and
-- `served_presentation_complete` reports it. It is a third independent group -- 0016's,
-- 0030's, and this one -- because a row may legitimately be whole in one and absent in
-- another, and widening an existing check would make each of them lie about the others.
ALTER TABLE assessment_run_tasks ADD COLUMN presentation_json VARCHAR;
ALTER TABLE assessment_run_tasks ADD COLUMN asset_identity_json VARCHAR;

-- All three are nullable and unconstrained. DuckDB refuses `ALTER TABLE ... ADD COLUMN`
-- with any constraint, and neither table can be recreated: both are widely referenced.
-- So there is no `json_valid` on any of them and no vocabulary check on the presentation
-- kind. `contracts.parse_task_presentation` enforces the shape on the way in and again
-- on the way out, `bank_presentation_wellformed`, `served_presentation_complete` and
-- `served_presentation_wellformed` assert it over the data, and every Python reader
-- parses defensively -- a diagnostic that aborts on damaged input reports less than one
-- that names the rows that will not parse.
