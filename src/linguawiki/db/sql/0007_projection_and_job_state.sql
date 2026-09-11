-- Projection watermarks and local job state for wiki, report, and import work.
CREATE TABLE projection_state (
    projection         VARCHAR   NOT NULL PRIMARY KEY CHECK (length(projection) > 0),
    projection_version INTEGER   NOT NULL CHECK (projection_version >= 1),
    last_event_id      VARCHAR,
    content_hash       VARCHAR   CHECK (content_hash IS NULL OR length(content_hash) = 64),
    stale              BOOLEAN   NOT NULL DEFAULT TRUE,
    generated_at       TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL
);

CREATE TABLE jobs (
    job_id             VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(job_id, 'evt_')),
    kind               VARCHAR   NOT NULL CHECK (length(kind) > 0),
    status             VARCHAR   NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    correlation_id     VARCHAR   NOT NULL CHECK (starts_with(correlation_id, 'evt_')),
    error_code         VARCHAR,
    error_message      VARCHAR,
    error_details_json VARCHAR   CHECK (error_details_json IS NULL OR json_valid(error_details_json)),
    started_at         TIMESTAMP,
    finished_at        TIMESTAMP,
    created_at         TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP NOT NULL
);
