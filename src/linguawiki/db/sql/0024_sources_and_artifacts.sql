-- Sources the learner works through, and the files that go with them.
--
-- A source is the learner's catalogue entry for material somebody else made. It is not
-- pack content and never becomes it: pack content is authored, reviewed, and licensed
-- for redistribution, while a source is a book the learner owns or a podcast they
-- listen to. The separation is why `sources` has a rights class and no pack identity,
-- and why a knowledge item extracted while reading is the learner's own note *about* the
-- source rather than a copy of it.
--
-- Two rules are enforced by the schema rather than left to callers:
--
-- - **only short quotations, and only where the rights allow.** `rights` decides how
--   much of the source's own words a row may hold, and the CHECK is per class: a
--   `metadata-only` source stores none of its text at all. "I'll keep just this chapter"
--   is how a learning workspace becomes an unlicensed copy of a book;
-- - **the original never lives in DuckDB or in Git.** An artifact is a relative path
--   plus a SHA-256, under the workspace's already-private `artifacts/` directory. The
--   table records that a file exists, what it is, and whether it is still there -- never
--   its bytes.
--
-- `artifacts` carries its own tombstone rather than being deleted, because evidence
-- points at it: a purged recording has to leave behind the fact that it existed, when it
-- went, and why, or the claims that rested on it become claims resting on nothing with
-- no way to tell.

CREATE TABLE sources (
    source_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(source_id, 'src_')),
    track_id         VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    kind             VARCHAR   NOT NULL CHECK (
        kind IN ('course', 'curriculum', 'book', 'article', 'podcast', 'video',
                 'conversation', 'learner-created')
    ),
    status           VARCHAR   NOT NULL DEFAULT 'cataloged' CHECK (
        status IN ('proposed', 'cataloged', 'reviewed', 'active', 'completed',
                   'archived', 'rejected')
    ),
    title            VARCHAR   NOT NULL CHECK (length(trim(title)) > 0),
    creator          VARCHAR,
    -- Where the original lives: a URL, an ISBN, a shelf. Not the material itself.
    canonical_uri    VARCHAR,
    language         VARCHAR   NOT NULL CHECK (length(language) > 0),
    -- The learner's estimate of the source's level, in their own framework's vocabulary.
    -- Advisory: it orders candidates and claims nothing about the learner.
    level_code       VARCHAR,
    -- What may be kept of the source's own words. `metadata-only` is the default because
    -- assuming less is the safe direction: a learner who has the rights can say so.
    rights           VARCHAR   NOT NULL DEFAULT 'metadata-only' CHECK (
        rights IN ('metadata-only', 'short-excerpt', 'full-local')
    ),
    rights_note      VARCHAR,
    -- What the source makes available, which decides which modalities it can serve.
    has_audio        BOOLEAN   NOT NULL DEFAULT FALSE,
    has_transcript   BOOLEAN   NOT NULL DEFAULT FALSE,
    -- How many units the source has, when that is knowable. NULL for a podcast feed,
    -- and coverage is then reported as unknown rather than as a fraction of a guess.
    total_units      INTEGER   CHECK (total_units IS NULL OR total_units > 0),
    notes            VARCHAR   CHECK (notes IS NULL OR length(notes) <= 4000),
    policy_version   VARCHAR   NOT NULL CHECK (length(policy_version) > 0),
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX sources_identity ON sources (track_id, kind, title, creator);

-- One bounded piece of a source: a chapter, a page range, a timestamp span, an episode.
-- `parent_unit_id` is deliberately not a foreign key: a portable restore inserts a table
-- in one statement and a self-reference cannot resolve mid-insert, which is the same
-- reason `curriculum_units` carries its parent through `db check` instead.
CREATE TABLE source_units (
    unit_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(unit_id, 'src_')),
    source_id      VARCHAR   NOT NULL REFERENCES sources (source_id),
    parent_unit_id VARCHAR,
    sequence       INTEGER   NOT NULL CHECK (sequence > 0),
    label          VARCHAR   NOT NULL CHECK (length(trim(label)) > 0),
    -- Where the unit is. Text positions and time positions are different facts, so both
    -- exist and a unit uses whichever its source kind makes meaningful.
    starts_at_ms   INTEGER   CHECK (starts_at_ms IS NULL OR starts_at_ms >= 0),
    ends_at_ms     INTEGER   CHECK (ends_at_ms IS NULL OR ends_at_ms >= 0),
    start_locator  VARCHAR,
    end_locator    VARCHAR,
    -- A short quotation, where the rights allow one. The length rule is per rights class
    -- and lives in `db check`, because DuckDB cannot express "depends on the parent row".
    excerpt        VARCHAR   CHECK (excerpt IS NULL OR length(excerpt) <= 20000),
    notes          VARCHAR   CHECK (notes IS NULL OR length(notes) <= 4000),
    difficulty     DOUBLE    CHECK (difficulty IS NULL OR difficulty BETWEEN 0.0 AND 1.0),
    -- When the learner finished working this unit. On the unit rather than in a join
    -- table because a source belongs to one track, so a unit belongs to one learner --
    -- and completing it twice must not advance coverage twice, which a timestamp
    -- expresses and a counter does not.
    completed_at   TIMESTAMP,
    created_at     TIMESTAMP NOT NULL,
    updated_at     TIMESTAMP NOT NULL,
    CHECK (ends_at_ms IS NULL OR starts_at_ms IS NULL OR ends_at_ms >= starts_at_ms)
);

CREATE UNIQUE INDEX source_units_sequence ON source_units (source_id, sequence);

-- How far the learner has got, and what they understood. Unaided and aided comprehension
-- are separate columns because they are separate facts: the first measures the learner,
-- the second measures the help.
CREATE TABLE track_source_progress (
    track_id          VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    source_id         VARCHAR   NOT NULL REFERENCES sources (source_id),
    status            VARCHAR   NOT NULL DEFAULT 'not-started' CHECK (
        status IN ('not-started', 'in-progress', 'completed', 'abandoned')
    ),
    mode              VARCHAR   NOT NULL DEFAULT 'intensive' CHECK (
        mode IN ('intensive', 'extensive')
    ),
    -- The furthest unit reached. Not a foreign key: `knowledge merge` and unit
    -- re-sequencing both repoint it, and DuckDB rewrites an UPDATE of a foreign-key
    -- column as a delete and an insert, which a referenced row refuses.
    position_unit_id  VARCHAR,
    completed_units   INTEGER   NOT NULL DEFAULT 0 CHECK (completed_units >= 0),
    minutes_spent     INTEGER   NOT NULL DEFAULT 0 CHECK (minutes_spent >= 0),
    -- The best band reached without any help, and with it. Nullable because "nobody
    -- measured this" is a different answer from "they understood none of it".
    unaided_band      VARCHAR   CHECK (
        unaided_band IS NULL
        OR unaided_band IN ('none', 'little', 'gist', 'most', 'full')
    ),
    aided_band        VARCHAR   CHECK (
        aided_band IS NULL OR aided_band IN ('none', 'little', 'gist', 'most', 'full')
    ),
    coverage          DOUBLE    CHECK (coverage IS NULL OR coverage BETWEEN 0.0 AND 1.0),
    policy_version    VARCHAR   NOT NULL CHECK (length(policy_version) > 0),
    started_at        TIMESTAMP,
    last_worked_at    TIMESTAMP,
    completed_at      TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (track_id, source_id),
    CHECK (status <> 'not-started' OR started_at IS NULL),
    CHECK (status <> 'completed' OR completed_at IS NOT NULL)
);

-- One report of how much of a unit the learner understood, and with what help. Kept per
-- observation rather than folded into progress, because the *order* is the evidence: a
-- learner cannot un-see a translation, so an unaided reading recorded after an aided one
-- is not a second observation.
CREATE TABLE comprehension_observations (
    observation_id VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(observation_id, 'obs_')
    ),
    track_id       VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    source_id      VARCHAR   NOT NULL REFERENCES sources (source_id),
    unit_id        VARCHAR   REFERENCES source_units (unit_id),
    sequence       INTEGER   NOT NULL CHECK (sequence > 0),
    aid            VARCHAR   NOT NULL CHECK (
        aid IN ('unaided', 'glossed', 'subtitled', 'translated', 'explained')
    ),
    band           VARCHAR   NOT NULL CHECK (
        band IN ('none', 'little', 'gist', 'most', 'full')
    ),
    mode           VARCHAR   NOT NULL DEFAULT 'intensive' CHECK (
        mode IN ('intensive', 'extensive')
    ),
    -- How many times the learner went back over it. A reread is not a failure; it is the
    -- measure of how hard the material was.
    replays        INTEGER   NOT NULL DEFAULT 0 CHECK (replays >= 0),
    lookups        INTEGER   NOT NULL DEFAULT 0 CHECK (lookups >= 0),
    minutes        INTEGER   CHECK (minutes IS NULL OR minutes >= 0),
    note           VARCHAR   CHECK (note IS NULL OR length(note) <= 2000),
    -- The session this happened in, when it happened in one. Not a foreign key: it is
    -- written by a close, after the row exists.
    session_id     VARCHAR,
    observed_at    TIMESTAMP NOT NULL,
    recorded_at    TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX comprehension_observations_sequence
    ON comprehension_observations (track_id, source_id, unit_id, sequence);

-- What a source unit gave the learner: an item they noted, an error it surfaced, an
-- example worth keeping. The link is the provenance -- it is how "where did I meet this
-- word?" has an answer -- and it is a reference, never a copy of the source's text.
CREATE TABLE source_item_links (
    unit_id      VARCHAR   NOT NULL REFERENCES source_units (unit_id),
    target_kind  VARCHAR   NOT NULL CHECK (
        target_kind IN ('knowledge-item', 'example', 'error-pattern')
    ),
    -- One column pointing at three tables, so no foreign key can express it; `db check`
    -- resolves each kind against its own table by name.
    target_id    VARCHAR   NOT NULL CHECK (length(target_id) > 0),
    relation     VARCHAR   NOT NULL DEFAULT 'encountered-in' CHECK (
        relation IN ('encountered-in', 'extracted-from', 'illustrated-by', 'practised-in')
    ),
    created_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (unit_id, target_kind, target_id, relation)
);

-- A file outside the database: a recording, a downloaded transcript, a scan. The row
-- says a file exists and what it is; the bytes stay under the workspace's private
-- `artifacts/` directory, which Git already ignores.
CREATE TABLE artifacts (
    artifact_id     VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(artifact_id, 'art_')),
    track_id        VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    kind            VARCHAR   NOT NULL CHECK (kind IN ('audio', 'transcript', 'other')),
    -- Relative and inside the workspace. An absolute path would tie the row to one
    -- machine, and a path with `..` in it would reach outside the workspace entirely.
    relative_path   VARCHAR   NOT NULL CHECK (
        length(relative_path) > 0
        AND NOT starts_with(relative_path, '/')
        AND position('..' IN relative_path) = 0
    ),
    media_type      VARCHAR,
    byte_size       BIGINT    CHECK (byte_size IS NULL OR byte_size >= 0),
    sha256          VARCHAR   NOT NULL CHECK (length(sha256) = 64),
    origin          VARCHAR   NOT NULL DEFAULT 'learner-recording' CHECK (
        origin IN ('learner-recording', 'provider-export', 'source-download', 'other')
    ),
    rights          VARCHAR   NOT NULL DEFAULT 'metadata-only' CHECK (
        rights IN ('metadata-only', 'short-excerpt', 'full-local')
    ),
    source_id       VARCHAR   REFERENCES sources (source_id),
    -- Whether the learner consented to keeping it at all. A `FALSE` artifact is a record
    -- that the file existed and was not kept, which is what makes a later acoustic claim
    -- explicable rather than merely unsupported.
    retained        BOOLEAN   NOT NULL DEFAULT TRUE,
    -- The tombstone. Present exactly when the file is gone.
    purged_at       TIMESTAMP,
    purge_reason    VARCHAR   CHECK (
        purge_reason IS NULL
        OR purge_reason IN ('learner-request', 'retention-expiry', 'source-withdrawn',
                            'superseded')
    ),
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    -- A purge is a decision, so it comes with its reason and its moment together.
    CHECK ((purged_at IS NULL) = (purge_reason IS NULL))
);

CREATE UNIQUE INDEX artifacts_content ON artifacts (track_id, sha256);
