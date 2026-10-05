-- C5: audio a learner records, the judge who hears it, and the claims that rest on it.
--
-- Four things arrive together because each is only meaningful beside the others: a
-- recording the client captured has to be accounted for from the moment its bytes exist,
-- bound to the served task it answers, judged against that binding, and -- when the
-- recording goes -- the score that rested on it has to go with it, marked rather than
-- deleted.

-- The recordings a pack ships, recorded when the pack is installed.
--
-- Serving used to load the whole pack directory to read one digest. That was correct --
-- what comes back is still compared with the digest the run snapshotted -- and cheap
-- while no pack shipped audio. This is where the digest is read from instead. The point
-- was never where the file is: it is whether the bytes are the ones the learner heard,
-- and that comparison is unchanged.
--
-- Not `content_records`, deliberately. A pack recording is not learner content: nothing
-- in a learner's history is *about* it, its `content_kind` would need a widened CHECK on
-- the most-referenced table in the schema, and its rights are the pack's, not the
-- learner's -- so a purge or a retention sweep has nothing to decide about it. The row
-- describes an installation; `pack install` replaces a pack's rows wholesale.
CREATE TABLE pack_assets (
    content_id   VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(content_id, 'cnt_')),
    pack_id      VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    asset_key    VARCHAR   NOT NULL CHECK (length(asset_key) > 0),
    -- Pack-relative, under the installed source path. Resolved with containment on every
    -- read, because a symlink out of the pack reads through to bytes nobody installed.
    path         VARCHAR   NOT NULL CHECK (
        length(path) > 0 AND NOT starts_with(path, '/') AND position('..' IN path) = 0
    ),
    sha256       VARCHAR   NOT NULL CHECK (length(sha256) = 64),
    media_type   VARCHAR   NOT NULL CHECK (length(media_type) > 0),
    duration_ms  INTEGER   NOT NULL CHECK (duration_ms > 0),
    pack_version VARCHAR   NOT NULL CHECK (length(pack_version) > 0),
    installed_at TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX pack_assets_key ON pack_assets (pack_id, asset_key);

-- A browser capture, accounted for before its bytes exist.
--
-- These bytes are created by the client rather than offered by the learner, and
-- registration can fail -- the writer is locked, the run closed, the hash is wrong. A
-- recording written and then not registered is accounted for by nothing, which is the
-- failure Stage 5 closed for package ingestion. So the row is written first and the file
-- second, and from then until the artifact row owns the bytes this row does, wherever
-- they are: the staged path while `staged`, either path while `promoting`.
--
-- `capture_id` is the client's own identifier for the capture. It is the idempotency key
-- of the upload, and it is deliberately not `artifacts.external_id`: a non-null producer
-- identity there reads as "arrived with a package" and `delete-after-ingestion` sweeps
-- it, which is a policy about packages, not about a learner's own recordings.
CREATE TABLE capture_stagings (
    capture_id     VARCHAR   NOT NULL PRIMARY KEY CHECK (length(trim(capture_id)) > 0),
    track_id       VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    run_id         VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    content_id     VARCHAR   NOT NULL REFERENCES assessment_tasks (content_id),
    -- Under the private staging root, which is outside the roots `artifacts.register`
    -- accepts: a staged file cannot be registered by mistake.
    staged_path    VARCHAR   NOT NULL CHECK (
        starts_with(staged_path, 'staging/captures/') AND position('..' IN staged_path) = 0
    ),
    final_path     VARCHAR   NOT NULL CHECK (
        starts_with(final_path, 'artifacts/') AND position('..' IN final_path) = 0
    ),
    sha256         VARCHAR   NOT NULL CHECK (length(sha256) = 64),
    byte_size      BIGINT    NOT NULL CHECK (byte_size > 0),
    media_type     VARCHAR   NOT NULL CHECK (length(media_type) > 0),
    state          VARCHAR   NOT NULL CHECK (state IN ('staged', 'promoting', 'registered', 'refused')),
    -- Written by the registration transaction, after the row exists, so not a foreign
    -- key: `capture_stagings_resolved` asserts it names a live artifact.
    artifact_id    VARCHAR,
    refusal_code   VARCHAR,
    refusal_reason VARCHAR,
    created_at     TIMESTAMP NOT NULL,
    updated_at     TIMESTAMP NOT NULL,
    CHECK ((state = 'registered') = (artifact_id IS NOT NULL)),
    CHECK ((state = 'refused') = (refusal_code IS NOT NULL AND refusal_reason IS NOT NULL))
);

-- Which recording answers which served task, and where its judgement stands.
--
-- `status` is mutable, so "at most one non-superseded submission per served task" is a
-- named check rather than a unique index: an index over a mutable column has the effect
-- of a foreign key, and DuckDB rewrites the update as a delete and an insert.
CREATE TABLE assessment_submissions (
    submission_id    VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(submission_id, 'asm_')),
    run_id           VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    content_id       VARCHAR   NOT NULL REFERENCES assessment_tasks (content_id),
    capture_id       VARCHAR   NOT NULL CHECK (length(trim(capture_id)) > 0),
    -- Not foreign keys, like every other column naming an artifact: a purge rewrites the
    -- artifact row. `submission_artifacts_agree` asserts both relations.
    artifact_id      VARCHAR   NOT NULL CHECK (starts_with(artifact_id, 'art_')),
    status           VARCHAR   NOT NULL CHECK (
        status IN ('pending', 'judged', 'superseded', 'withdrawn')
    ),
    -- Self-referencing and written after insertion, so not a foreign key on either count.
    superseded_by    VARCHAR,
    withdrawn_code   VARCHAR,
    withdrawn_reason VARCHAR,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
    CHECK ((status = 'withdrawn') = (withdrawn_code IS NOT NULL AND withdrawn_reason IS NOT NULL))
);

-- One capture is one submission, and a re-sent upload finds it rather than adding one.
CREATE UNIQUE INDEX assessment_submissions_capture ON assessment_submissions (capture_id);

-- What a result rests on, and whether it still stands.
--
-- `assessment_results` had no artifact column, and the purge looked for dependents only
-- among pronunciation observations, so a purge deleted the recording and left the score.
-- `invalidated_at` marks a result whose recording is gone -- marked, not deleted: a
-- learner told their vowel was wrong deserves to see that the evidence is gone.
--
-- Unconstrained, like every column added since 0016: DuckDB refuses
-- `ALTER TABLE ... ADD COLUMN` with a constraint. `results_rest_on_their_recording`,
-- `result_invalidation_complete`, and the orphan relation assert them over the data.
ALTER TABLE assessment_results ADD COLUMN audio_artifact_id VARCHAR;
ALTER TABLE assessment_results ADD COLUMN invalidated_at TIMESTAMP;
ALTER TABLE assessment_results ADD COLUMN invalidated_reason VARCHAR;
-- The version of the rule that decided what a judged score may claim (`evidence.py`),
-- stored on the rows it produced so a policy change is replayed rather than migrated.
-- NULL where the rule did not apply: a machine-scored task, or a result older than it.
ALTER TABLE assessment_results ADD COLUMN judgement_policy_version VARCHAR;

-- A note on an estimate snapshot that the evidence it rested on was withdrawn.
--
-- History is marked, never rewritten: `estimate_history` stays exactly as it was, and
-- this says which of its snapshots rested on a result that no longer stands.
CREATE TABLE estimate_annotations (
    annotation_id VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(annotation_id, 'est_')),
    snapshot_id   VARCHAR   NOT NULL REFERENCES estimate_history (snapshot_id),
    kind          VARCHAR   NOT NULL CHECK (kind IN ('evidence-withdrawn')),
    -- Not foreign keys: the result is marked invalidated in the transaction that writes
    -- this row, and the artifact is purged in it. The orphan relations assert both.
    result_id     VARCHAR   NOT NULL CHECK (starts_with(result_id, 'asm_')),
    artifact_id   VARCHAR,
    reason        VARCHAR   NOT NULL CHECK (length(trim(reason)) > 0),
    annotated_at  TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX estimate_annotations_once ON estimate_annotations (snapshot_id, result_id);
