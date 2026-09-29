-- The answer key as served, and what the score that followed actually rests on.
--
-- Migration 0016 snapshotted what a task *demanded* -- its type, level, difficulty,
-- family, modality, anchor flag, rubric version -- and 0022 added the items it targeted,
-- because a pack is mutable and a run is not. What none of them kept is the material a
-- scorer needs to reach a verdict: the accepted answers, the prompt the learner was
-- actually shown, and the rubric body behind the version number. Scoring against the
-- live bank would let a pack edit made after the sitting decide whether somebody was
-- right, and raise or lower a claim they are credited with. So the three are snapshotted
-- at serve time and read back from the run.
--
-- The three are one group. All three null means the row predates this migration, and the
-- snapshotted `content_hash` is what says whether the bank may still answer for it. All
-- three present means the record is whole. Any other combination is damage: it cannot
-- establish what the learner faced and it is not a legacy row either, so it is refused at
-- read and reported by `served_answer_key_complete` rather than quietly falling back to
-- the mutable bank the snapshot exists to replace. 0016's group and this one are
-- independent -- a row may legitimately be whole in one and absent in the other -- which
-- is why this is a second named check and not a widening of the first.
ALTER TABLE assessment_run_tasks ADD COLUMN expected_json VARCHAR;
ALTER TABLE assessment_run_tasks ADD COLUMN prompt_snapshot VARCHAR;
ALTER TABLE assessment_run_tasks ADD COLUMN rubric_json VARCHAR;

-- The score's provenance belongs on the result, not on the served row. The policy that
-- matters is the one that *computed* the score, and that runs when the result is
-- recorded; a serve-time copy would name a version that never ran. `score_source` is its
-- inseparable other half: a caller-supplied score has always only been *labelled*
-- `deterministic` by `assessor_kind`, and without a source column a compatibility call
-- would read afterwards as the scoring policy having run.
--
-- `response_visibility` and `response_hash` are the retention decision as a fact about
-- the row. Scoring needs the learner's whole answer; what survives is whatever consent
-- allows, which is why the answer arrives as input and reaches this table only through
-- `retain_response`. The hash is kept even when the text is not, so a response offered
-- later can be checked against the one that was actually scored.
ALTER TABLE assessment_results ADD COLUMN scoring_policy_version VARCHAR;
ALTER TABLE assessment_results ADD COLUMN score_source VARCHAR;
ALTER TABLE assessment_results ADD COLUMN response_visibility VARCHAR;
ALTER TABLE assessment_results ADD COLUMN response_hash VARCHAR;

-- Backfill only what the row itself already answers, so neither column has a legacy hole
-- the checks in `db check` have to guard around -- and a predicate guarded by
-- `IS NOT NULL` skips exactly the rows a write path refuses hardest. No computed scoring
-- path existed before this migration, so every existing score was supplied; and what is
-- stored is an excerpt exactly when `response_excerpt` is present. Note that this states
-- what the column *holds*, not what was consented to: no consent decision was recorded
-- against these rows, and inventing one would be a claim nobody made. For the same
-- reason `scoring_policy_version` and `response_hash` stay null -- a version names work
-- no policy did, and a hash of text nobody kept cannot be recomputed.
UPDATE assessment_results SET score_source = 'supplied';
UPDATE assessment_results
   SET response_visibility = CASE WHEN response_excerpt IS NULL THEN 'withheld' ELSE 'excerpt' END;

-- All seven columns are nullable and unconstrained. DuckDB refuses
-- `ALTER TABLE ... ADD COLUMN` with any constraint ("Adding columns with constraints not
-- yet supported"), and neither table can be recreated: both are widely referenced. So
-- there is no `json_valid` on `expected_json` or `rubric_json` and no vocabulary check on
-- `score_source` or `response_visibility`. The service layer enforces them on the way in,
-- `served_answer_key_wellformed`, `result_score_provenance` and `result_response_retention`
-- assert them over the data, and every Python reader parses the two JSON columns
-- defensively -- a diagnostic that aborts on damaged input reports less than one that
-- names the rows that will not parse.
