-- What the aggregation wrote, and the immutable trail of why an estimate moved.

-- `track_item_state` already held the aggregate stage. What it lacked was the two things
-- that make a stage answerable: which policy produced it, and what that policy saw.
ALTER TABLE track_item_state ADD COLUMN aggregation_version VARCHAR;
ALTER TABLE track_item_state ADD COLUMN computed_at TIMESTAMP;
-- The named, numeric factors behind the stage. Kept as JSON rather than rows because a
-- factor is a derived number belonging to one computation, not an entity with a life of
-- its own; it is replaced wholesale every time the stage is recomputed.
ALTER TABLE track_item_state ADD COLUMN explanation_json VARCHAR;
-- The stage the gates alone would have granted, and the cap the kind of evidence
-- imposed. Storing both is what makes "recognition did not promote production" a fact
-- on the row rather than a claim in a report.
ALTER TABLE track_item_state ADD COLUMN gated_stage VARCHAR;
ALTER TABLE track_item_state ADD COLUMN evidence_ceiling VARCHAR;

-- A dimension nothing could test is not a dimension the learner is bad at. That
-- distinction was carried only by `confidence_label = 'not-tested'`, which conflates it
-- with a tested dimension whose confidence happens to be low.
ALTER TABLE skill_estimates ADD COLUMN estimate_status VARCHAR;
UPDATE skill_estimates SET estimate_status = CASE
    WHEN confidence_label = 'not-tested' THEN 'not-tested'
    WHEN evidence_count = 0 THEN 'provisional'
    ELSE 'estimated'
END;

CREATE TABLE estimate_history (
    snapshot_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(snapshot_id, 'est_')),
    track_id             VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    dimension            VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    framework_id         VARCHAR   NOT NULL REFERENCES proficiency_frameworks (framework_id),
    estimate_status      VARCHAR   NOT NULL CHECK (
        estimate_status IN ('not-tested', 'provisional', 'estimated')
    ),
    level_code           VARCHAR,
    level_low            VARCHAR,
    level_high           VARCHAR,
    score                DOUBLE,
    uncertainty          DOUBLE    CHECK (uncertainty IS NULL OR uncertainty >= 0.0),
    confidence_label     VARCHAR   NOT NULL CHECK (
        confidence_label IN ('low', 'medium', 'high', 'not-tested')
    ),
    basis                VARCHAR   NOT NULL CHECK (
        basis IN ('declared-hypothesis', 'self-report', 'calibration', 'placement', 'evidence')
    ),
    evidence_count       INTEGER   NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    source_run_id        VARCHAR   REFERENCES assessment_runs (run_id),
    calculation_version  VARCHAR   NOT NULL CHECK (length(calculation_version) > 0),
    reason               VARCHAR   NOT NULL CHECK (length(reason) > 0),
    -- The previous snapshot and the weighting factors that produced the change. Not a
    -- foreign key: a self-referencing table cannot be bulk-restored in one insert, so
    -- the relation is checked by name in `db check` instead.
    previous_snapshot_id VARCHAR,
    change_json          VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(change_json)),
    as_of                TIMESTAMP NOT NULL,
    recorded_at          TIMESTAMP NOT NULL
);

-- Which evidence rows the snapshot rests on. Relational rather than a JSON list, because
-- "show me every estimate this observation moved" is a question a learner may fairly ask
-- and a defect may need answered in the other direction.
CREATE TABLE estimate_evidence (
    snapshot_id VARCHAR   NOT NULL REFERENCES estimate_history (snapshot_id),
    evidence_id VARCHAR   NOT NULL REFERENCES evidence (evidence_id),
    weight      DOUBLE    NOT NULL CHECK (weight >= 0.0),
    recorded_at TIMESTAMP NOT NULL,
    PRIMARY KEY (snapshot_id, evidence_id)
);
