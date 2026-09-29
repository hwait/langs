-- Three corrections to how a transcript is identified, trusted, and revised.
--
-- **Identity.** 0025 made an utterance unique by `(track_id, external_id)`, and the
-- producer's ID is only unique inside *its own session*: a scaffold and the segment
-- adapter both mint `utt_001`. Two unrelated conversations therefore collapsed into one,
-- and the second was silently skipped as "already imported" -- the learner lost a whole
-- call. Identity now includes the external session the utterance came from.
--
-- **Confidence and provenance.** The plan requires the transcriber's own confidence to
-- survive ingestion, because low-confidence speech has to become a possible
-- transcription error rather than a confirmed learner error. `raw_confidence` existed and
-- nothing ever wrote it; these columns record who produced the text as well, so a
-- systematic mishearing can be traced to the tool that made it.
--
-- **Supersession.** 0025 allowed one revision per layer and a re-review *deleted* its
-- predecessor, which contradicts the rule the whole layer design exists for: the raw text
-- is immutable and every later reading is a new row. A second hearing now supersedes the
-- first by naming it, and both stay on the record.

ALTER TABLE utterances ADD COLUMN external_session_id VARCHAR;

-- Backfill from the package that carried the utterance, and fall back to the utterance's
-- own session. A row that can name neither keeps the value it has always effectively
-- had -- its own ID -- which is unique by construction and so cannot collide.
UPDATE utterances SET external_session_id = coalesce(
    (SELECT package.external_session_id FROM session_packages package
     WHERE package.ingestion_id = utterances.ingestion_id),
    utterances.session_id,
    utterances.utterance_id
);

-- A new name rather than the old one: DuckDB will not recreate a dropped index under the
-- same name inside the transaction that dropped it, and a migration is one transaction.
DROP INDEX utterances_external;

CREATE UNIQUE INDEX utterances_external_session
    ON utterances (track_id, external_session_id, external_id);

-- What the transcriber reported about its own output, when it reported anything. Kept
-- because a low confidence is the first reason to suspect a mishearing rather than a
-- mistake, and because "which tool heard this" is the question a systematic error leads to.
ALTER TABLE utterances ADD COLUMN transcriber VARCHAR;
ALTER TABLE utterances ADD COLUMN transcriber_version VARCHAR;

-- The reading this one replaced, and when. Not a foreign key: the superseded row is
-- written before its successor exists, and DuckDB will not update a referenced row's key.
-- `integrity.ORPHAN_RELATIONS` carries the relation by name.
ALTER TABLE transcript_revisions ADD COLUMN superseded_at TIMESTAMP;
ALTER TABLE transcript_revisions ADD COLUMN superseded_by VARCHAR;

-- One *current* reading per layer, not one reading ever -- so the unique index over
-- `(utterance_id, layer)` has to go: it is the thing that forced a re-review to delete
-- its predecessor. DuckDB has no partial indexes, so "at most one row per layer with
-- `superseded_at IS NULL`" cannot be a constraint here. It is `db check`
-- `revision_supersession` instead, which is the repository's rule for a uniqueness
-- requirement a column's mutability puts out of the schema's reach.
DROP INDEX transcript_revisions_layer;

-- The producer's own identifier for a recording, when the artifact came from a package.
-- A manifest entry is a claim about a file; the row is this workspace's record of it, and
-- this column is what makes the two the same recording on a re-ingest. Uniqueness is
-- `db check` `artifact_external_identity` rather than an index: the column is NULL for
-- every learner-made recording, and DuckDB has no partial index to exempt those.
ALTER TABLE artifacts ADD COLUMN external_id VARCHAR;

-- A package's pronunciation event becomes an acoustic claim, not a loose note.
--
-- Stage 4 materialized `pronunciation.assessment` as a generic session observation
-- because the table for acoustic claims did not exist yet. It does now, and the
-- difference is not cosmetic: a claim in `pronunciation_observations` names the audio it
-- rests on, so purging that audio invalidates it. A generic observation named nothing, so
-- a `confirmed` claim from a recording the learner later deleted went on standing.
CREATE TABLE session_staged_events_rebuilt (
    staged_event_id  VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(staged_event_id, 'sev_')
    ),
    session_id       VARCHAR   NOT NULL REFERENCES sessions (session_id),
    batch_id         VARCHAR   NOT NULL REFERENCES session_event_batches (batch_id),
    sequence         INTEGER   NOT NULL CHECK (sequence > 0),
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
    -- `pronunciation` is the addition.
    materialized_kind VARCHAR  CHECK (
        materialized_kind IS NULL
        OR materialized_kind IN
            ('attempt', 'error-occurrence', 'followup', 'observation', 'comprehension',
             'pronunciation')
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
