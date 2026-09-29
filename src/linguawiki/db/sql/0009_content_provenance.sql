-- Item-level origin, independent review axes, dependency invalidation, and the
-- generation bookkeeping that makes an AI-assisted batch quarantinable.
--
-- `content_records` holds the shared identity and lifecycle of every durable
-- instructional item, so a knowledge target, an assessment task, and a later Anki note
-- all answer the provenance question in the same place.
CREATE TABLE prompt_templates (
    template_id              VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(template_id, 'cnt_')
    ),
    template_key             VARCHAR   NOT NULL CHECK (length(template_key) > 0),
    version                  INTEGER   NOT NULL CHECK (version >= 1),
    purpose                  VARCHAR   NOT NULL CHECK (purpose IN ('generation', 'review')),
    intended_kinds_json      VARCHAR   NOT NULL DEFAULT '[]' CHECK (
        json_valid(intended_kinds_json)
    ),
    body                     VARCHAR   NOT NULL CHECK (length(body) > 0),
    body_sha256              VARCHAR   NOT NULL CHECK (length(body_sha256) = 64),
    maturity                 VARCHAR   NOT NULL DEFAULT 'new' CHECK (
        maturity IN ('new', 'stable', 'quarantined', 'retired')
    ),
    known_failure_modes_json VARCHAR   NOT NULL DEFAULT '[]' CHECK (
        json_valid(known_failure_modes_json)
    ),
    -- Runs whose every persistent item was inspected. A new or materially changed
    -- template needs three before it may be stabilized.
    inspected_runs           INTEGER   NOT NULL DEFAULT 0 CHECK (inspected_runs >= 0),
    quarantine_reason        VARCHAR,
    created_at               TIMESTAMP NOT NULL,
    updated_at               TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX prompt_templates_identity ON prompt_templates (template_key, version);

CREATE TABLE generation_runs (
    generation_run_id VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(generation_run_id, 'evt_')
    ),
    template_id       VARCHAR   NOT NULL REFERENCES prompt_templates (template_id),
    provider          VARCHAR,
    model             VARCHAR,
    model_version     VARCHAR,
    parameters_hash   VARCHAR   NOT NULL CHECK (length(parameters_hash) = 64),
    privacy_class     VARCHAR   NOT NULL CHECK (
        privacy_class IN ('public', 'private', 'synthetic')
    ),
    run_ordinal       INTEGER   NOT NULL CHECK (run_ordinal >= 1),
    created_at        TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX generation_runs_ordinal ON generation_runs (template_id, run_ordinal);

CREATE TABLE generation_batches (
    batch_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(batch_id, 'evt_')),
    generation_run_id VARCHAR   NOT NULL REFERENCES generation_runs (generation_run_id),
    template_id       VARCHAR   NOT NULL REFERENCES prompt_templates (template_id),
    status            VARCHAR   NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'sampling', 'accepted', 'quarantined')
    ),
    -- The sampling policy the batch was judged under, recorded so a later policy change
    -- cannot silently reinterpret a historical decision.
    sampling_policy   VARCHAR   NOT NULL CHECK (
        sampling_policy IN ('full-inspection', 'sampled')
    ),
    -- How many items the batch was opened to hold. An import may not exceed it, so the
    -- inspection duty cannot be renegotiated once the output looks convincing.
    planned_items     INTEGER   NOT NULL CHECK (planned_items > 0),
    item_count        INTEGER   NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    required_sample   INTEGER   NOT NULL DEFAULT 0 CHECK (required_sample >= 0),
    inspected_count   INTEGER   NOT NULL DEFAULT 0 CHECK (inspected_count >= 0),
    defect_count      INTEGER   NOT NULL DEFAULT 0 CHECK (defect_count >= 0),
    quarantine_reason VARCHAR,
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL
);

CREATE TABLE content_records (
    content_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(content_id, 'cnt_')),
    content_kind        VARCHAR   NOT NULL CHECK (
        content_kind IN ('knowledge', 'example', 'descriptor', 'assessment_task',
                         'activity_template', 'source_recommendation', 'resource_bundle',
                         'exercise', 'anki_note', 'session_derived')
    ),
    -- A pack item belongs to a pack and learner-created content to a track; a row may
    -- have neither, but never both. That rule is *not* a CHECK constraint: DuckDB
    -- rewrites an update of any row carrying a multi-column CHECK as a delete and an
    -- insert, which a row referenced by a foreign key refuses -- and every content row
    -- is referenced. The invariant is enforced by the service layer and by the
    -- content_ownership check in 'db check'.
    pack_id             VARCHAR   REFERENCES language_packs (pack_id),
    track_id            VARCHAR   REFERENCES learning_tracks (track_id),
    stable_key          VARCHAR   NOT NULL CHECK (length(stable_key) > 0),
    language_tag        VARCHAR   NOT NULL CHECK (length(language_tag) > 0),
    content_hash        VARCHAR   NOT NULL CHECK (length(content_hash) = 64),
    lifecycle           VARCHAR   NOT NULL CHECK (
        lifecycle IN ('draft', 'candidate', 'approved-personal', 'verified',
                      'publication-ready', 'needs-review', 'rejected', 'deprecated')
    ),
    risk_tier           INTEGER   NOT NULL CHECK (risk_tier BETWEEN 0 AND 4),
    batch_id            VARCHAR   REFERENCES generation_batches (batch_id),
    quarantined         BOOLEAN   NOT NULL DEFAULT FALSE,
    invalidation_reason VARCHAR,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX content_records_identity ON content_records (
    content_kind, coalesce(pack_id, ''), coalesce(track_id, ''), stable_key
);

CREATE TABLE content_origins (
    content_id     VARCHAR NOT NULL REFERENCES content_records (content_id),
    sequence       INTEGER NOT NULL CHECK (sequence >= 1),
    origin_class   VARCHAR NOT NULL CHECK (
        origin_class IN ('authentic-source', 'learner-produced', 'source-derived',
                         'ai-adapted', 'ai-generated', 'synthetic-media', 'human-authored')
    ),
    reference      VARCHAR,
    locator        VARCHAR,
    transformation VARCHAR,
    origin_hash    VARCHAR CHECK (origin_hash IS NULL OR length(origin_hash) = 64),
    PRIMARY KEY (content_id, sequence)
);

-- Review axes are independent of origin and of each other. `state` is checked per axis
-- by the service layer, because the legal states differ between axes.
CREATE TABLE content_reviews (
    content_id            VARCHAR   NOT NULL REFERENCES content_records (content_id),
    axis                  VARCHAR   NOT NULL CHECK (
        axis IN ('linguistic', 'pedagogical', 'source-alignment', 'rights', 'privacy')
    ),
    state                 VARCHAR   NOT NULL CHECK (length(state) > 0),
    reviewer_kind         VARCHAR   NOT NULL CHECK (
        reviewer_kind IN ('machine', 'ai', 'learner', 'human', 'not-applicable')
    ),
    reviewer              VARCHAR,
    method                VARCHAR,
    evidence_reference    VARCHAR,
    reviewed_content_hash VARCHAR   CHECK (
        reviewed_content_hash IS NULL OR length(reviewed_content_hash) = 64
    ),
    reviewed_at           TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL,
    PRIMARY KEY (content_id, axis)
);

CREATE TABLE content_dependencies (
    content_id      VARCHAR NOT NULL REFERENCES content_records (content_id),
    sequence        INTEGER NOT NULL CHECK (sequence >= 1),
    dependency_kind VARCHAR NOT NULL CHECK (
        dependency_kind IN ('content', 'source', 'template', 'artifact', 'policy')
    ),
    dependency_ref  VARCHAR NOT NULL CHECK (length(dependency_ref) > 0),
    expected_hash   VARCHAR CHECK (expected_hash IS NULL OR length(expected_hash) = 64),
    on_change       VARCHAR NOT NULL DEFAULT 'needs-review' CHECK (
        on_change IN ('needs-review', 'quarantine', 'ignore')
    ),
    PRIMARY KEY (content_id, sequence)
);

-- One inspection of one generated item, recorded apart from the review axes so a
-- sampling decision stays auditable after the item is approved or rejected.
CREATE TABLE batch_inspections (
    batch_id    VARCHAR   NOT NULL REFERENCES generation_batches (batch_id),
    content_id  VARCHAR   NOT NULL REFERENCES content_records (content_id),
    outcome     VARCHAR   NOT NULL CHECK (outcome IN ('accepted', 'defective')),
    axis        VARCHAR,
    finding     VARCHAR,
    reviewer    VARCHAR   NOT NULL CHECK (length(reviewer) > 0),
    recorded_at TIMESTAMP NOT NULL,
    PRIMARY KEY (batch_id, content_id)
);
