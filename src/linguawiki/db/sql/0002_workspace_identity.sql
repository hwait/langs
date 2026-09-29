-- Workspace identity and the mirror of linguawiki.lock used for integrity checks.
CREATE TABLE workspaces (
    workspace_id    VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(workspace_id, 'wsp_')),
    -- Enforces at most one workspace row per learner database.
    is_singleton    BOOLEAN   NOT NULL DEFAULT TRUE UNIQUE CHECK (is_singleton),
    name            VARCHAR   NOT NULL CHECK (length(name) > 0),
    normalized_name VARCHAR   NOT NULL CHECK (length(normalized_name) > 0),
    history_policy  VARCHAR   NOT NULL CHECK (
        history_policy IN ('git-wiki', 'portable-snapshot', 'git-portable-snapshot', 'local-only')
    ),
    track_policy    VARCHAR   NOT NULL DEFAULT 'single' CHECK (track_policy IN ('single', 'multi')),
    timezone        VARCHAR   NOT NULL CHECK (length(timezone) > 0),
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL
);

CREATE TABLE workspace_versions (
    workspace_id   VARCHAR   NOT NULL REFERENCES workspaces (workspace_id),
    component      VARCHAR   NOT NULL CHECK (
        component IN ('core', 'database_schema', 'skill_bundle', 'pack')
    ),
    component_key  VARCHAR   NOT NULL CHECK (length(component_key) > 0),
    version        VARCHAR   NOT NULL CHECK (length(version) > 0),
    sha256         VARCHAR   NOT NULL CHECK (length(sha256) = 64),
    applied_at     TIMESTAMP NOT NULL,
    audit_event_id VARCHAR,
    PRIMARY KEY (workspace_id, component, component_key)
);
