-- The knowledge graph. Deliberately not a vocabulary table: `kind` admits the
-- structures different languages need, and nothing here assumes whitespace tokenization,
-- an alphabet, inflection, or tone.
CREATE TABLE knowledge_items (
    content_id VARCHAR   NOT NULL PRIMARY KEY REFERENCES content_records (content_id),
    language   VARCHAR   NOT NULL CHECK (length(language) > 0),
    kind       VARCHAR   NOT NULL CHECK (
        kind IN ('concept', 'lexeme', 'sense', 'form', 'construction', 'grammar',
                 'pronunciation', 'character', 'pragmatics', 'culture', 'skill_strategy')
    ),
    title      VARCHAR   NOT NULL CHECK (length(title) > 0),
    body       VARCHAR   NOT NULL CHECK (length(body) > 0),
    summary    VARCHAR,
    level_min  VARCHAR,
    level_max  VARCHAR,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE knowledge_aliases (
    content_id VARCHAR NOT NULL REFERENCES knowledge_items (content_id),
    normalized VARCHAR NOT NULL CHECK (length(normalized) > 0),
    alias      VARCHAR NOT NULL CHECK (length(alias) > 0),
    script     VARCHAR,
    locale     VARCHAR NOT NULL CHECK (length(locale) > 0),
    PRIMARY KEY (content_id, locale, normalized)
);

CREATE TABLE knowledge_relations (
    relation_id       VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(relation_id, 'cnt_')),
    source_content_id VARCHAR   NOT NULL REFERENCES knowledge_items (content_id),
    relation_type     VARCHAR   NOT NULL CHECK (
        relation_type IN ('prerequisite', 'form-of', 'sense-of', 'contrast', 'collocation',
                          'government', 'example-of', 'related', 'error-target',
                          'curriculum-objective')
    ),
    -- A typed edge either points at another knowledge item or names an external target
    -- such as a curriculum objective; exactly one of the two is set.
    target_content_id VARCHAR   REFERENCES knowledge_items (content_id),
    target_ref        VARCHAR,
    created_at        TIMESTAMP NOT NULL,
    CHECK ((target_content_id IS NULL) <> (target_ref IS NULL))
);

-- Edge uniqueness rides on the primary key rather than a second unique index over the
-- edge's parts: `relation_id` is *derived* from (owner, source, type, target), so the two
-- would state the same rule twice -- and a second, expression-based unique index made a
-- delete-then-reinsert inside one transaction report a duplicate that no longer existed.

CREATE TABLE examples (
    content_id      VARCHAR   NOT NULL PRIMARY KEY REFERENCES content_records (content_id),
    item_content_id VARCHAR   NOT NULL REFERENCES knowledge_items (content_id),
    text            VARCHAR   NOT NULL CHECK (length(text) > 0),
    translation     VARCHAR,
    gloss           VARCHAR,
    locale          VARCHAR,
    difficulty      VARCHAR,
    audio_artifact  VARCHAR,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE item_tags (
    content_id VARCHAR NOT NULL REFERENCES knowledge_items (content_id),
    tag_kind   VARCHAR NOT NULL CHECK (
        tag_kind IN ('theme', 'feature', 'level', 'frequency', 'user')
    ),
    tag_value  VARCHAR NOT NULL CHECK (length(tag_value) > 0),
    PRIMARY KEY (content_id, tag_kind, tag_value)
);

-- Per-track learner state. Reference items imported by pack installation stay `unseen`
-- until real evidence is recorded; the aggregate stage is recomputable from evidence,
-- which Stage 3 adds.
CREATE TABLE track_item_state (
    track_id            VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    content_id          VARCHAR   NOT NULL REFERENCES knowledge_items (content_id),
    stage               VARCHAR   NOT NULL DEFAULT 'unseen' CHECK (
        stage IN ('unseen', 'encountered', 'recognized', 'understood',
                  'controlled-production', 'spontaneous-production', 'stable')
    ),
    stage_source        VARCHAR   NOT NULL DEFAULT 'reference-import' CHECK (
        stage_source IN ('reference-import', 'self-report', 'evidence')
    ),
    confidence          DOUBLE    NOT NULL DEFAULT 0.0 CHECK (confidence BETWEEN 0.0 AND 1.0),
    priority            INTEGER   NOT NULL DEFAULT 0,
    first_encounter_at  TIMESTAMP,
    last_encounter_at   TIMESTAMP,
    next_review_at      TIMESTAMP,
    positive_evidence   INTEGER   NOT NULL DEFAULT 0 CHECK (positive_evidence >= 0),
    negative_evidence   INTEGER   NOT NULL DEFAULT 0 CHECK (negative_evidence >= 0),
    updated_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (track_id, content_id)
);
