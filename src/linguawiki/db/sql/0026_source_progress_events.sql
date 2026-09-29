-- Reading and listening join the session lifecycle.
--
-- Stage 4 shipped a close that could credit an attempt, a correction, a pronunciation
-- observation, a note, and a follow-up -- and nothing at all about working through a
-- book. So reading happened *beside* a session rather than inside one: a learner recorded
-- forty minutes with a podcast and their session knew nothing about it. This adds the
-- staged kind that fixes that, and the materialized kind a close writes when it lands.
--
-- It is a table rebuild rather than an `ALTER`, because both of the constraints involved
-- are CHECKs and DuckDB cannot drop one. The copy is exact and the indexes are recreated
-- with the same names, so nothing outside this file can tell the difference -- which is
-- the point: 0023 shipped, and a released migration is not edited.

CREATE TABLE session_staged_events_rebuilt (
    staged_event_id  VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(staged_event_id, 'sev_')
    ),
    session_id       VARCHAR   NOT NULL REFERENCES sessions (session_id),
    batch_id         VARCHAR   NOT NULL REFERENCES session_event_batches (batch_id),
    sequence         INTEGER   NOT NULL CHECK (sequence > 0),
    -- `source.progress` is the addition. Everything else is 0023 unchanged.
    kind             VARCHAR   NOT NULL CHECK (
        kind IN ('attempt.observed', 'correction.given', 'pronunciation.assessment',
                 'source.progress', 'observation.noted', 'follow_up')
    ),
    occurred_at      TIMESTAMP NOT NULL,
    source_event_id  VARCHAR   NOT NULL CHECK (length(source_event_id) > 0),
    schema_version   INTEGER   NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    payload_json     VARCHAR   NOT NULL CHECK (json_valid(payload_json)),
    block_id         VARCHAR   REFERENCES session_blocks (block_id),
    activity_id      VARCHAR   REFERENCES activities (activity_id),
    evidence_basis   VARCHAR   NOT NULL DEFAULT 'direct' CHECK (
        evidence_basis IN ('direct', 'transcript', 'audio')
    ),
    status           VARCHAR   NOT NULL DEFAULT 'staged' CHECK (
        status IN ('staged', 'materialized', 'discarded', 'rejected')
    ),
    finalization_id  VARCHAR,
    -- `comprehension` is the addition: what a staged source-progress event becomes is a
    -- comprehension observation, which is a row about the learner's relationship with a
    -- work rather than about an item they know.
    materialized_kind VARCHAR  CHECK (
        materialized_kind IS NULL
        OR materialized_kind IN
            ('attempt', 'error-occurrence', 'followup', 'observation', 'comprehension')
    ),
    materialized_id  VARCHAR,
    discard_reason   VARCHAR,
    created_at       TIMESTAMP NOT NULL,
    CHECK (
        (status = 'materialized') =
        (finalization_id IS NOT NULL AND materialized_kind IS NOT NULL
         AND materialized_id IS NOT NULL)
    ),
    CHECK ((status IN ('discarded', 'rejected')) = (discard_reason IS NOT NULL))
);

INSERT INTO session_staged_events_rebuilt
SELECT staged_event_id, session_id, batch_id, sequence, kind, occurred_at, source_event_id,
       schema_version, payload_json, block_id, activity_id, evidence_basis, status,
       finalization_id, materialized_kind, materialized_id, discard_reason, created_at
FROM session_staged_events;

DROP TABLE session_staged_events;

ALTER TABLE session_staged_events_rebuilt RENAME TO session_staged_events;

CREATE UNIQUE INDEX session_staged_events_sequence
    ON session_staged_events (batch_id, sequence);

CREATE UNIQUE INDEX session_staged_events_source
    ON session_staged_events (session_id, source_event_id);
