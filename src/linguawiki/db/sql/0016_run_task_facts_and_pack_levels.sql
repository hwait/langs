-- Three corrections, each additive: no released migration is touched.

-- 1. What a score must not re-read from a mutable pack.
--
-- A run recorded the pack version it started under, but selection and scoring both
-- queried the currently installed pack. Updating a task's difficulty mid-run therefore
-- folded the *new* difficulty into a posterior built from the old one, and the learner's
-- estimate silently described a task they were never shown. The facts scoring needs are
-- now captured when the task is served, and scoring reads them from here.
ALTER TABLE assessment_run_tasks ADD COLUMN task_type VARCHAR;
ALTER TABLE assessment_run_tasks ADD COLUMN level_code VARCHAR;
ALTER TABLE assessment_run_tasks ADD COLUMN difficulty DOUBLE;
ALTER TABLE assessment_run_tasks ADD COLUMN content_family VARCHAR;
ALTER TABLE assessment_run_tasks ADD COLUMN modality VARCHAR;
ALTER TABLE assessment_run_tasks ADD COLUMN is_anchor BOOLEAN;
ALTER TABLE assessment_run_tasks ADD COLUMN rubric_version INTEGER;
-- The served item's content hash, so drift is detectable and not merely likely.
ALTER TABLE assessment_run_tasks ADD COLUMN content_hash VARCHAR;

-- 2. Which levels a framework has is a claim of the pack that declared them.
--
-- `proficiency_framework_levels` is keyed by framework alone, so a pack that narrows its
-- own range from A1..C2 to A1..C1 left C2 standing, and a track could still be created at
-- a level its pack no longer teaches. The global table keeps shared framework *identity*
-- -- the level order every pack must agree on -- and membership moves here.
CREATE TABLE pack_framework_levels (
    pack_id      VARCHAR NOT NULL REFERENCES language_packs (pack_id),
    framework_id VARCHAR NOT NULL REFERENCES proficiency_frameworks (framework_id),
    level_code   VARCHAR NOT NULL CHECK (length(level_code) > 0),
    sequence     INTEGER NOT NULL CHECK (sequence >= 1),
    PRIMARY KEY (pack_id, framework_id, level_code)
);

-- 3. Bind tracks that predate 0015 to their pack where the answer is unambiguous.
--
-- 0015 added a nullable column, which left any track created before it with no pack at
-- all -- a state no service can resolve. A track whose target language has exactly one
-- installed pack has only one possible answer, so it is filled in here. An ambiguous
-- track is left null deliberately and reported by the named `track_pack_binding` check
-- in `db check`, because guessing which of two packs taught a learner is not a repair.
UPDATE learning_tracks SET pack_id = (
    SELECT min(pack.pack_id)
    FROM language_packs pack
    JOIN pack_installations installation ON installation.pack_id = pack.pack_id
    WHERE pack.language_tag = learning_tracks.target_language
      AND installation.status = 'installed'
)
WHERE pack_id IS NULL
  AND (
    SELECT count(*)
    FROM language_packs pack
    JOIN pack_installations installation ON installation.pack_id = pack.pack_id
    WHERE pack.language_tag = learning_tracks.target_language
      AND installation.status = 'installed'
  ) = 1;
