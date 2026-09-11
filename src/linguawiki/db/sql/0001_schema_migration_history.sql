-- Applied-migration history. Every timestamp column in the schema stores UTC in a
-- naive TIMESTAMP; the repository layer attaches the UTC zone on read.
CREATE TABLE schema_migrations (
    version             INTEGER     NOT NULL PRIMARY KEY CHECK (version >= 1),
    migration_id        VARCHAR     NOT NULL UNIQUE CHECK (length(migration_id) > 0),
    checksum            VARCHAR     NOT NULL CHECK (length(checksum) = 64),
    application_version VARCHAR     NOT NULL CHECK (length(application_version) > 0),
    applied_at          TIMESTAMP   NOT NULL
);
