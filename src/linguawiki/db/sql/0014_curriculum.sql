-- Generic prior-course import and audit. The database stores outlines, mappings,
-- learner self-reports, and short permissible references -- never copied course text.
CREATE TABLE curricula (
    curriculum_id    VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(curriculum_id, 'cnt_')),
    track_id         VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    pack_id          VARCHAR   REFERENCES language_packs (pack_id),
    title            VARCHAR   NOT NULL CHECK (length(title) > 0),
    kind             VARCHAR   NOT NULL CHECK (
        kind IN ('course', 'book', 'exam', 'syllabus', 'user-authored')
    ),
    version          VARCHAR   NOT NULL CHECK (length(version) > 0),
    source_reference VARCHAR,
    -- Only a legally permissible outline or the learner's own objectives may be stored.
    rights_status    VARCHAR   NOT NULL CHECK (
        rights_status IN ('user-authored', 'personal-use-only', 'cleared')
    ),
    provenance       VARCHAR   NOT NULL CHECK (length(provenance) > 0),
    framework_id     VARCHAR   REFERENCES proficiency_frameworks (framework_id),
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX curricula_identity ON curricula (track_id, title, version);

CREATE TABLE curriculum_units (
    unit_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(unit_id, 'cnt_')),
    curriculum_id  VARCHAR   NOT NULL REFERENCES curricula (curriculum_id),
    -- Deliberately not a foreign key: a self-referencing FK makes a bulk portable
    -- restore fail whenever a child row precedes its parent in the export. The edge is
    -- validated by the service layer and by the orphan check in 'db check'.
    parent_unit_id VARCHAR,
    sequence       INTEGER   NOT NULL CHECK (sequence >= 1),
    code           VARCHAR   NOT NULL CHECK (length(code) > 0),
    title          VARCHAR   NOT NULL CHECK (length(title) > 0),
    level_code     VARCHAR,
    created_at     TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX curriculum_units_identity ON curriculum_units (curriculum_id, code);

-- Objectives keep their own row whether or not they map onto pack content, so an
-- unmapped objective stays an explicit gap instead of disappearing.
CREATE TABLE curriculum_unit_objectives (
    unit_id     VARCHAR   NOT NULL REFERENCES curriculum_units (unit_id),
    sequence    INTEGER   NOT NULL CHECK (sequence >= 1),
    objective   VARCHAR   NOT NULL CHECK (length(objective) > 0),
    dimension   VARCHAR,
    mapped_kind VARCHAR   CHECK (
        mapped_kind IS NULL OR mapped_kind IN ('knowledge', 'descriptor')
    ),
    mapped_ref  VARCHAR,
    mapped_by   VARCHAR,
    map_confidence VARCHAR CHECK (
        map_confidence IS NULL OR map_confidence IN ('low', 'medium', 'high')
    ),
    PRIMARY KEY (unit_id, sequence),
    CHECK ((mapped_kind IS NULL) = (mapped_ref IS NULL))
);

CREATE TABLE track_curriculum_progress (
    track_id             VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    unit_id              VARCHAR   NOT NULL REFERENCES curriculum_units (unit_id),
    state                VARCHAR   NOT NULL DEFAULT 'not-started' CHECK (
        state IN ('not-started', 'current', 'claimed-complete', 'audited')
    ),
    provenance           VARCHAR   NOT NULL DEFAULT 'self-report' CHECK (length(provenance) > 0),
    evidence_summary_json VARCHAR  NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_summary_json)),
    started_at           TIMESTAMP,
    completed_at         TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL,
    PRIMARY KEY (track_id, unit_id)
);

CREATE TABLE curriculum_audits (
    audit_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(audit_id, 'asm_')),
    track_id        VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    curriculum_id   VARCHAR   NOT NULL REFERENCES curricula (curriculum_id),
    status          VARCHAR   NOT NULL DEFAULT 'in-progress' CHECK (
        status IN ('in-progress', 'finalized', 'abandoned')
    ),
    sample_size     INTEGER   NOT NULL CHECK (sample_size >= 0),
    idempotency_key VARCHAR,
    stop_reason     VARCHAR,
    started_at      TIMESTAMP NOT NULL,
    finalized_at    TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX curriculum_audits_idempotency ON curriculum_audits (idempotency_key);

CREATE TABLE curriculum_audit_items (
    audit_id    VARCHAR   NOT NULL REFERENCES curriculum_audits (audit_id),
    sequence    INTEGER   NOT NULL CHECK (sequence >= 1),
    unit_id     VARCHAR   NOT NULL REFERENCES curriculum_units (unit_id),
    target_kind VARCHAR   NOT NULL CHECK (target_kind IN ('knowledge', 'descriptor')),
    target_ref  VARCHAR   NOT NULL CHECK (length(target_ref) > 0),
    -- Why the sample selected this target: central prerequisite, recent unit, likely
    -- production target, or older supposedly known material.
    risk_reason VARCHAR   NOT NULL CHECK (length(risk_reason) > 0),
    risk_weight DOUBLE    NOT NULL CHECK (risk_weight > 0.0),
    status      VARCHAR   NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'recorded', 'skipped')
    ),
    outcome     VARCHAR   CHECK (outcome IS NULL OR outcome IN ('correct', 'partial', 'incorrect')),
    score       DOUBLE    CHECK (score IS NULL OR score BETWEEN 0.0 AND 1.0),
    recorded_at TIMESTAMP,
    PRIMARY KEY (audit_id, sequence)
);

CREATE UNIQUE INDEX curriculum_audit_items_unique ON curriculum_audit_items (
    audit_id, target_kind, target_ref
);
