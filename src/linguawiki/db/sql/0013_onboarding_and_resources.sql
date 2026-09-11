-- Resumable onboarding state, the bounded calibration queue it builds, and the
-- resource plan that turns pack bundles into a bounded starter curriculum.
CREATE TABLE onboarding_runs (
    onboarding_id      VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(onboarding_id, 'asm_')),
    track_id           VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    mode               VARCHAR   NOT NULL CHECK (mode IN ('declared-level', 'placement')),
    status             VARCHAR   NOT NULL DEFAULT 'started' CHECK (
        status IN ('started', 'awaiting-input', 'calibrating', 'finalized', 'abandoned')
    ),
    declared_level     VARCHAR,
    pack_id            VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    pack_maturity      VARCHAR   NOT NULL CHECK (length(pack_maturity) > 0),
    -- A pilot pack may only run a labelled calibration; comprehensive placement needs
    -- placement-ready bank coverage.
    calibration_label  VARCHAR   NOT NULL CHECK (
        calibration_label IN ('pilot-calibration', 'comprehensive-placement')
    ),
    -- Neither reference is a foreign key: both are written *after* the row exists, and
    -- DuckDB rewrites an update that touches a foreign-key column as a delete and an
    -- insert, which the calibration-queue rows that reference this run refuse. Both are
    -- validated by the orphan checks in 'db check'.
    assessment_run_id  VARCHAR,
    resource_plan_id   VARCHAR,
    idempotency_key    VARCHAR,
    started_at         TIMESTAMP NOT NULL,
    finalized_at       TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX onboarding_runs_idempotency ON onboarding_runs (idempotency_key);

CREATE TABLE onboarding_answers (
    onboarding_id VARCHAR   NOT NULL REFERENCES onboarding_runs (onboarding_id),
    key           VARCHAR   NOT NULL CHECK (length(key) > 0),
    value_json    VARCHAR   NOT NULL CHECK (json_valid(value_json)),
    provenance    VARCHAR   NOT NULL DEFAULT 'self-report' CHECK (length(provenance) > 0),
    recorded_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (onboarding_id, key)
);

CREATE TABLE calibration_queue_items (
    queue_item_id VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(queue_item_id, 'rev_')),
    track_id      VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    onboarding_id VARCHAR   REFERENCES onboarding_runs (onboarding_id),
    audit_id      VARCHAR,
    purpose       VARCHAR   NOT NULL CHECK (
        purpose IN ('prerequisite-sample', 'declared-band', 'evidence-gap', 'audit-gap')
    ),
    target_kind   VARCHAR   NOT NULL CHECK (
        target_kind IN ('knowledge', 'assessment_task', 'descriptor')
    ),
    target_ref    VARCHAR   NOT NULL CHECK (length(target_ref) > 0),
    dimension     VARCHAR,
    priority      INTEGER   NOT NULL DEFAULT 0,
    rationale     VARCHAR   NOT NULL CHECK (length(rationale) > 0),
    status        VARCHAR   NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'served', 'done', 'skipped')
    ),
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX calibration_queue_identity ON calibration_queue_items (
    track_id, purpose, target_kind, target_ref
);

CREATE TABLE resource_plans (
    plan_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(plan_id, 'rev_')),
    track_id         VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    pack_id          VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    onboarding_mode  VARCHAR   NOT NULL CHECK (
        onboarding_mode IN ('declared-level', 'placement')
    ),
    status           VARCHAR   NOT NULL DEFAULT 'planned' CHECK (
        status IN ('planned', 'applied', 'superseded')
    ),
    -- A pilot pack's plan is labelled as such, so nothing downstream can present it as
    -- a level-complete curriculum.
    plan_label       VARCHAR   NOT NULL CHECK (length(plan_label) > 0),
    level_codes_json VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(level_codes_json)),
    bundles_json     VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(bundles_json)),
    item_budget      INTEGER   NOT NULL CHECK (item_budget > 0),
    weeks            INTEGER   NOT NULL DEFAULT 2 CHECK (weeks > 0),
    unsupported_json VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(unsupported_json)),
    created_at       TIMESTAMP NOT NULL,
    applied_at       TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL
);

CREATE TABLE resource_plan_items (
    plan_id     VARCHAR NOT NULL REFERENCES resource_plans (plan_id),
    sequence    INTEGER NOT NULL CHECK (sequence >= 1),
    item_kind   VARCHAR NOT NULL CHECK (
        item_kind IN ('knowledge', 'descriptor', 'assessment_task', 'activity_template',
                      'source_recommendation', 'example')
    ),
    item_ref    VARCHAR NOT NULL CHECK (length(item_ref) > 0),
    bundle_key  VARCHAR NOT NULL CHECK (length(bundle_key) > 0),
    action      VARCHAR NOT NULL CHECK (action IN ('import', 'propose', 'skip')),
    reason      VARCHAR NOT NULL CHECK (length(reason) > 0),
    week        INTEGER CHECK (week IS NULL OR week >= 1),
    PRIMARY KEY (plan_id, sequence)
);
