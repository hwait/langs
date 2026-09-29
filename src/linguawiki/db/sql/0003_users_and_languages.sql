-- Learner identity. In the MVP user_id scopes records; it is not an authorization boundary.
CREATE TABLE users (
    user_id      VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(user_id, 'usr_')),
    workspace_id VARCHAR   NOT NULL REFERENCES workspaces (workspace_id),
    display_name VARCHAR   NOT NULL CHECK (length(display_name) > 0),
    timezone     VARCHAR   NOT NULL CHECK (length(timezone) > 0),
    status       VARCHAR   NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at   TIMESTAMP NOT NULL,
    updated_at   TIMESTAMP NOT NULL
);

CREATE TABLE user_languages (
    user_id          VARCHAR   NOT NULL REFERENCES users (user_id),
    language_tag     VARCHAR   NOT NULL CHECK (length(language_tag) > 0),
    role             VARCHAR   NOT NULL CHECK (role IN ('native', 'support')),
    preference_order INTEGER   NOT NULL CHECK (preference_order >= 1),
    created_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (user_id, language_tag, role)
);

CREATE UNIQUE INDEX user_languages_role_order ON user_languages (user_id, role, preference_order);
