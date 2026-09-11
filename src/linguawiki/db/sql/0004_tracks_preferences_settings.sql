-- learning_tracks is the primary scope key for learner-specific data.
CREATE TABLE learning_tracks (
    track_id              VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(track_id, 'trk_')),
    user_id               VARCHAR   NOT NULL REFERENCES users (user_id),
    target_language       VARCHAR   NOT NULL CHECK (length(target_language) > 0),
    region                VARCHAR,
    script                VARCHAR,
    proficiency_framework VARCHAR   NOT NULL CHECK (length(proficiency_framework) > 0),
    declared_level        VARCHAR,
    current_level         VARCHAR,
    target_level          VARCHAR,
    goal                  VARCHAR,
    status                VARCHAR   NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'paused', 'archived')
    ),
    -- One primary program per user is an application invariant checked by 'db check',
    -- because DuckDB has no partial unique index.
    is_primary            BOOLEAN   NOT NULL DEFAULT TRUE,
    weekly_minutes        INTEGER   CHECK (weekly_minutes IS NULL OR weekly_minutes > 0),
    timezone              VARCHAR   NOT NULL CHECK (length(timezone) > 0),
    created_at            TIMESTAMP NOT NULL,
    updated_at            TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX learning_tracks_target ON learning_tracks (
    user_id, target_language, coalesce(region, ''), coalesce(script, '')
);

CREATE TABLE track_preferences (
    track_id   VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    key        VARCHAR   NOT NULL CHECK (length(key) > 0),
    value_json VARCHAR   NOT NULL CHECK (json_valid(value_json)),
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (track_id, key)
);

CREATE TABLE settings (
    scope          VARCHAR   NOT NULL CHECK (scope IN ('workspace', 'user', 'track')),
    scope_id       VARCHAR   NOT NULL CHECK (length(scope_id) > 0),
    key            VARCHAR   NOT NULL CHECK (length(key) > 0),
    value_json     VARCHAR   NOT NULL CHECK (json_valid(value_json)),
    schema_version INTEGER   NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    updated_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (scope, scope_id, key)
);
