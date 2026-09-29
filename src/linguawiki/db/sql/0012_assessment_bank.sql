-- The assessment bank a pack ships, the runs performed against it, and the persisted
-- placement state that lets a calibration pause and resume.
CREATE TABLE assessment_definitions (
    definition_id   VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(definition_id, 'asm_')),
    pack_id         VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    form_key        VARCHAR   NOT NULL CHECK (length(form_key) > 0),
    version         INTEGER   NOT NULL CHECK (version >= 1),
    purpose         VARCHAR   NOT NULL CHECK (
        purpose IN ('pilot-calibration', 'placement', 'weekly', 'monthly', 'milestone')
    ),
    framework_id    VARCHAR   NOT NULL REFERENCES proficiency_frameworks (framework_id),
    level_min       VARCHAR   NOT NULL CHECK (length(level_min) > 0),
    level_max       VARCHAR   NOT NULL CHECK (length(level_max) > 0),
    dimensions_json VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(dimensions_json)),
    title           VARCHAR   NOT NULL CHECK (length(title) > 0),
    created_at      TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX assessment_definitions_identity ON assessment_definitions (
    pack_id, form_key, version
);

CREATE TABLE assessment_tasks (
    content_id      VARCHAR   NOT NULL PRIMARY KEY REFERENCES content_records (content_id),
    definition_id   VARCHAR   NOT NULL REFERENCES assessment_definitions (definition_id),
    dimension       VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    task_type       VARCHAR   NOT NULL CHECK (
        task_type IN ('objective', 'short-response', 'extended-productive',
                      'pronunciation-target', 'connected-speech')
    ),
    -- The ordinal difficulty grid the placement staircase works on: adjacent framework
    -- bands are one unit apart and reviewed half-band difficulties are permitted.
    level_code      VARCHAR   NOT NULL CHECK (length(level_code) > 0),
    difficulty      DOUBLE    NOT NULL,
    content_family  VARCHAR   NOT NULL CHECK (length(content_family) > 0),
    modality        VARCHAR   NOT NULL CHECK (
        modality IN ('text', 'audio', 'speech', 'writing')
    ),
    prompt          VARCHAR   NOT NULL CHECK (length(prompt) > 0),
    rubric_version  INTEGER   NOT NULL DEFAULT 1 CHECK (rubric_version >= 1),
    rubric_json     VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(rubric_json)),
    expected_json   VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(expected_json)),
    permitted_help  VARCHAR   NOT NULL DEFAULT 'none' CHECK (length(permitted_help) > 0),
    is_anchor       BOOLEAN   NOT NULL DEFAULT FALSE,
    target_refs_json VARCHAR  NOT NULL DEFAULT '[]' CHECK (json_valid(target_refs_json)),
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE assessment_runs (
    run_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(run_id, 'asm_')),
    track_id        VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    definition_id   VARCHAR   REFERENCES assessment_definitions (definition_id),
    run_type        VARCHAR   NOT NULL CHECK (
        run_type IN ('pilot-calibration', 'placement', 'curriculum-audit', 'weekly',
                     'monthly', 'milestone')
    ),
    status          VARCHAR   NOT NULL DEFAULT 'in-progress' CHECK (
        status IN ('in-progress', 'paused', 'finalized', 'abandoned')
    ),
    algorithm_version VARCHAR NOT NULL CHECK (length(algorithm_version) > 0),
    conditions_json VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(conditions_json)),
    stop_reason     VARCHAR,
    idempotency_key VARCHAR,
    started_at      TIMESTAMP NOT NULL,
    finalized_at    TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX assessment_runs_idempotency ON assessment_runs (idempotency_key);

CREATE TABLE assessment_run_tasks (
    run_id     VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    sequence   INTEGER   NOT NULL CHECK (sequence >= 1),
    content_id VARCHAR   NOT NULL REFERENCES assessment_tasks (content_id),
    dimension  VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    status     VARCHAR   NOT NULL DEFAULT 'served' CHECK (
        status IN ('served', 'answered', 'skipped')
    ),
    served_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, sequence)
);

CREATE UNIQUE INDEX assessment_run_tasks_unique ON assessment_run_tasks (run_id, content_id);

CREATE TABLE assessment_results (
    result_id     VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(result_id, 'asm_')),
    run_id        VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    content_id    VARCHAR   NOT NULL REFERENCES assessment_tasks (content_id),
    dimension     VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    raw_score     DOUBLE    NOT NULL CHECK (raw_score BETWEEN 0.0 AND 1.0),
    rubric_json   VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(rubric_json)),
    response_excerpt VARCHAR,
    assessor_kind VARCHAR   NOT NULL CHECK (
        assessor_kind IN ('deterministic', 'ai', 'learner', 'human')
    ),
    assessor      VARCHAR,
    confidence    VARCHAR   NOT NULL DEFAULT 'medium' CHECK (
        confidence IN ('low', 'medium', 'high')
    ),
    -- The prior and posterior the score was folded into, kept so a run can be replayed.
    prior_json    VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(prior_json)),
    posterior_json VARCHAR  NOT NULL DEFAULT '{}' CHECK (json_valid(posterior_json)),
    difficulty    DOUBLE    NOT NULL,
    recorded_at   TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX assessment_results_unique ON assessment_results (run_id, content_id);

-- Exposure metadata, so a learner does not receive the same placement item again
-- inside the reuse window unless it is an explicitly designated longitudinal anchor.
CREATE TABLE assessment_item_exposures (
    track_id        VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    content_id      VARCHAR   NOT NULL REFERENCES assessment_tasks (content_id),
    purpose         VARCHAR   NOT NULL CHECK (length(purpose) > 0),
    -- Serving an item exposes it; answering it is a separate fact. Counting only scored
    -- answers left a served-but-unanswered item eligible for reuse, so a learner could
    -- meet the same task again and the score would measure recall of it.
    exposure_count  INTEGER   NOT NULL DEFAULT 1 CHECK (exposure_count >= 1),
    answered_count  INTEGER   NOT NULL DEFAULT 0 CHECK (answered_count >= 0),
    is_anchor       BOOLEAN   NOT NULL DEFAULT FALSE,
    first_exposed_at TIMESTAMP NOT NULL,
    last_exposed_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (track_id, content_id)
);

CREATE TABLE placement_dimension_state (
    run_id            VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    dimension         VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    algorithm_version VARCHAR   NOT NULL CHECK (length(algorithm_version) > 0),
    status            VARCHAR   NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'stopped', 'not-tested')
    ),
    grid_json         VARCHAR   NOT NULL CHECK (json_valid(grid_json)),
    prior_json        VARCHAR   NOT NULL CHECK (json_valid(prior_json)),
    posterior_json    VARCHAR   NOT NULL CHECK (json_valid(posterior_json)),
    minimum_tasks     INTEGER   NOT NULL CHECK (minimum_tasks >= 0),
    maximum_tasks     INTEGER   NOT NULL CHECK (maximum_tasks >= 0),
    tasks_used        INTEGER   NOT NULL DEFAULT 0 CHECK (tasks_used >= 0),
    families_json     VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(families_json)),
    boundary_probed   BOOLEAN   NOT NULL DEFAULT FALSE,
    stop_reason       VARCHAR,
    confidence_label  VARCHAR   NOT NULL DEFAULT 'low' CHECK (
        confidence_label IN ('low', 'medium', 'high', 'not-tested')
    ),
    estimated_level   VARCHAR,
    credible_low      VARCHAR,
    credible_high     VARCHAR,
    updated_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, dimension)
);

CREATE TABLE skill_estimates (
    track_id            VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    dimension           VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    framework_id        VARCHAR   NOT NULL REFERENCES proficiency_frameworks (framework_id),
    level_code          VARCHAR,
    level_low           VARCHAR,
    level_high          VARCHAR,
    score               DOUBLE,
    uncertainty         DOUBLE    CHECK (uncertainty IS NULL OR uncertainty >= 0.0),
    confidence_label    VARCHAR   NOT NULL CHECK (
        confidence_label IN ('low', 'medium', 'high', 'not-tested')
    ),
    basis               VARCHAR   NOT NULL CHECK (
        basis IN ('declared-hypothesis', 'self-report', 'calibration', 'placement', 'evidence')
    ),
    evidence_count      INTEGER   NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    source_run_id       VARCHAR   REFERENCES assessment_runs (run_id),
    calculation_version VARCHAR   NOT NULL CHECK (length(calculation_version) > 0),
    as_of               TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (track_id, dimension)
);
