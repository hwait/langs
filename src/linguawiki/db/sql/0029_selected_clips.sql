-- A selected clip is not a whole conversation, and the retention rules turn on which.
--
-- The implementation plan is explicit: "selected audio clips are preferred over retaining
-- full recordings indefinitely", and extended retention is for clips that support an
-- active pronunciation target. Without a way to say "this is a thirty-second excerpt of
-- that call", the retention sweep held *entire recordings* for as long as any target they
-- touched was unfinished -- which is the indefinite full-recording retention the plan is
-- written to avoid.
--
-- `clip_of_artifact_id` is deliberately not a foreign key: the recording a clip came from
-- is purged by repointing its own row, and DuckDB refuses to update a referenced row's
-- key. `integrity.ORPHAN_RELATIONS` carries the relation by name, and `db check`
-- `clip_provenance` asserts the rest.

ALTER TABLE artifacts ADD COLUMN clip_of_artifact_id VARCHAR;

ALTER TABLE artifacts ADD COLUMN clip_starts_at_ms BIGINT;

ALTER TABLE artifacts ADD COLUMN clip_ends_at_ms BIGINT;
