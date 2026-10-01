-- How often the learner played a listening task's recording.
--
-- Replays are evidence, not a convenience: a learner who needed six hearings is telling
-- the estimate something a learner who needed one is not. Until now nothing recorded
-- them, and the only place a count could have come from was the caller -- which is one
-- more place a caller could talk its way into a different claim.
--
-- One row per play, appended before the recording is heard, rather than a counter on
-- `assessment_run_tasks`. Each play is a separately keyed request, and a counter cannot
-- tell a retried increment from a second play, while a unique key per row can. When and
-- in what order the learner listened stays recorded too.
CREATE TABLE assessment_task_plays (
    play_id         VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(play_id, 'asm_')),
    run_id          VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    content_id      VARCHAR   NOT NULL REFERENCES assessment_tasks (content_id),
    idempotency_key VARCHAR   NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    played_at       TIMESTAMP NOT NULL
);

-- A play is made once. A retry carrying the same key finds this row rather than adding
-- a second one, and two plays can never share a key by accident.
CREATE UNIQUE INDEX assessment_task_plays_key ON assessment_task_plays (idempotency_key);

-- `(run_id, content_id)` must name a task this run served, and that cannot be a foreign
-- key: `assessment_run_tasks` is keyed by `(run_id, sequence)`, and its rows are updated
-- as they are answered. `task_plays_name_served_audio` asserts the relation over the data.

-- The count `record` derived when it scored the task, from the rows above.
--
-- NULL is truthful in two cases and is not damage in either: a result recorded before
-- this migration, and a result with no play rows recorded through a surface that does not
-- track plays (the CLI, a skill). A count of zero is a different fact -- the learner
-- answered without listening -- and only a surface that records plays can establish it.
-- Plays that *were* recorded are counted whoever records the result.
--
-- Unconstrained, like every column added since 0016: DuckDB refuses
-- `ALTER TABLE ... ADD COLUMN` with a constraint. `result_play_counts_agree` asserts that
-- a recorded count equals the play rows it was derived from.
ALTER TABLE assessment_results ADD COLUMN play_count INTEGER;
