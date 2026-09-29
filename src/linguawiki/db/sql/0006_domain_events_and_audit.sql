-- Append-only domain events plus the command audit log.
CREATE TABLE domain_events (
    event_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(event_id, 'evt_')),
    event_type      VARCHAR   NOT NULL CHECK (length(event_type) > 0),
    aggregate_type  VARCHAR   NOT NULL CHECK (length(aggregate_type) > 0),
    aggregate_id    VARCHAR   NOT NULL CHECK (length(aggregate_id) > 0),
    schema_version  INTEGER   NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    payload_json    VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    correlation_id  VARCHAR   NOT NULL CHECK (starts_with(correlation_id, 'evt_')),
    idempotency_key VARCHAR,
    occurred_at     TIMESTAMP NOT NULL,
    recorded_at     TIMESTAMP NOT NULL
);

-- NULL keys stay distinct, so only explicitly keyed commands become idempotent.
CREATE UNIQUE INDEX domain_events_idempotency ON domain_events (idempotency_key);

CREATE TABLE audit_log (
    audit_id               VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(audit_id, 'evt_')),
    actor                  VARCHAR   NOT NULL CHECK (length(actor) > 0),
    command                VARCHAR   NOT NULL CHECK (length(command) > 0),
    correlation_id         VARCHAR   NOT NULL CHECK (starts_with(correlation_id, 'evt_')),
    outcome                VARCHAR   NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    affected_records_json  VARCHAR   NOT NULL DEFAULT '[]' CHECK (
        json_valid(affected_records_json)
    ),
    before_summary         VARCHAR,
    after_summary          VARCHAR,
    recorded_at            TIMESTAMP NOT NULL
);
