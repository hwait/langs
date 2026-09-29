-- The session engine: a plan, the blocks it chose, and the durable staging that makes a
-- close atomic and a crash recoverable.
--
-- The shape of this schema follows one rule from the plan: while a session is running,
-- nothing about the learner changes. `session log` writes *provisional* rows, and only
-- `session close` turns them into attempts, evidence, error occurrences, and follow-ups
-- -- in one transaction, once. Everything here exists to make that boundary real:
--
-- - a **batch** is what one flush wrote: a sequence number, a content hash, and an
--   idempotency key. Retrying a flush that already landed is a no-op, and a retry that
--   carries different events at the same sequence is a conflict rather than an
--   overwrite, because the second one would silently discard the first;
-- - a **staged event** is one provisional observation. It keeps the payload it arrived
--   with and, after a close, names the durable row it became. Nothing is edited into
--   place: the staged row is the audit trail of what the close was given;
-- - a **finalization** is the record that a close happened, including the result it
--   returned. That stored result is what makes a close whose response was lost safe to
--   repeat: the second call returns the first call's answer instead of doing the work
--   again;
-- - a **package** is an externally produced session (a tutor call, a voice app). It is
--   deduplicated by content hash, so ingesting the same conversation twice stages
--   nothing the second time.
--
-- `sessions.status` holds `closing` durably, which looks redundant until a close crashes
-- mid-transaction: the transaction rolls back and leaves a session that says "I was
-- being finalized" with no finalization row. That is a state the next invocation can
-- name and retry. Without it the same crash would be indistinguishable from a session
-- nobody ever tried to close.
--
-- The `materialized_*` columns on staged events are deliberately *not* foreign keys.
-- They are written after the row exists, and DuckDB rewrites an UPDATE of a foreign-key
-- column as a delete and an insert, which a referenced row refuses. `db check` carries
-- each of them by name through `integrity.ORPHAN_RELATIONS`.

CREATE TABLE sessions (
    session_id         VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(session_id, 'ses_')),
    track_id           VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    status             VARCHAR   NOT NULL DEFAULT 'planned' CHECK (
        status IN ('planned', 'active', 'closing', 'completed', 'partial', 'abandoned')
    ),
    mode               VARCHAR   NOT NULL CHECK (length(mode) > 0),
    -- What the learner asked for and what the planner could fill. They differ when
    -- reported energy or the available material made a shorter session the honest one,
    -- and the difference is a fact about the plan rather than a rounding error.
    requested_minutes  INTEGER   NOT NULL CHECK (requested_minutes > 0),
    planned_minutes    INTEGER   NOT NULL CHECK (planned_minutes > 0),
    actual_minutes     INTEGER   CHECK (actual_minutes IS NULL OR actual_minutes >= 0),
    energy             VARCHAR   NOT NULL DEFAULT 'normal' CHECK (
        energy IN ('low', 'normal', 'high')
    ),
    correction_mode    VARCHAR   NOT NULL DEFAULT 'accuracy' CHECK (
        correction_mode IN ('fluency', 'accuracy', 'exam')
    ),
    intent             VARCHAR,
    novel_target_cap   INTEGER   NOT NULL CHECK (novel_target_cap >= 0),
    -- The learner's IANA zone at planning time. Timestamps are naive UTC like everywhere
    -- else; this is what makes "today's plan" mean today where the learner is.
    timezone           VARCHAR   NOT NULL CHECK (length(timezone) > 0),
    planner_version    VARCHAR   NOT NULL CHECK (length(planner_version) > 0),
    lifecycle_version  VARCHAR   NOT NULL CHECK (length(lifecycle_version) > 0),
    plan_warnings_json VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(plan_warnings_json)),
    fatigue            VARCHAR   CHECK (fatigue IS NULL OR fatigue IN ('low', 'medium', 'high')),
    summary            VARCHAR   CHECK (summary IS NULL OR length(summary) <= 4000),
    -- Set by `plan create`; a repeat with the same key returns the same session rather
    -- than planning a second one. The hash is of the *request* -- track, minutes, mode,
    -- energy, intent, correction mode -- because a key alone cannot tell a retry from a
    -- different plan asked for under a reused key, and silently answering the second
    -- with the first is worse than refusing.
    idempotency_key    VARCHAR,
    request_hash       VARCHAR   CHECK (request_hash IS NULL OR length(request_hash) = 64),
    planned_at         TIMESTAMP NOT NULL,
    started_at         TIMESTAMP,
    closing_at         TIMESTAMP,
    closed_at          TIMESTAMP,
    created_at         TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP NOT NULL,
    -- A planned session has not started; a session that ran has. `abandoned` is exempt
    -- because a plan the learner never showed up for is abandoned without ever having
    -- started, and recording a start time for it would be an invention. Checked here
    -- because a restored database that disagreed would let `resume` offer a session that
    -- never ran.
    CHECK (status IN ('planned', 'abandoned') OR started_at IS NOT NULL),
    CHECK (status <> 'planned' OR started_at IS NULL),
    CHECK (
        status IN ('completed', 'partial', 'abandoned') OR closed_at IS NULL
    ),
    CHECK (planned_minutes <= requested_minutes)
);

CREATE UNIQUE INDEX sessions_idempotency ON sessions (idempotency_key);

CREATE TABLE session_blocks (
    block_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(block_id, 'blk_')),
    session_id      VARCHAR   NOT NULL REFERENCES sessions (session_id),
    sequence        INTEGER   NOT NULL CHECK (sequence > 0),
    role            VARCHAR   NOT NULL CHECK (role IN ('warm-up', 'core', 'closure')),
    block_type      VARCHAR   NOT NULL CHECK (length(block_type) > 0),
    area            VARCHAR   NOT NULL CHECK (length(area) > 0),
    dimension       VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    modality        VARCHAR   NOT NULL CHECK (
        modality IN ('text', 'audio', 'speech', 'writing')
    ),
    planned_minutes INTEGER   NOT NULL CHECK (planned_minutes > 0),
    actual_minutes  INTEGER   CHECK (actual_minutes IS NULL OR actual_minutes >= 0),
    objective       VARCHAR   NOT NULL CHECK (length(objective) > 0),
    -- Why this block is in the plan, as the planner's own components phrased it. Stored
    -- rather than regenerated: the weights may change, and this is what the learner was
    -- actually told.
    rationale_json  VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(rationale_json)),
    score           DOUBLE    NOT NULL DEFAULT 0.0,
    difficulty      DOUBLE    CHECK (difficulty IS NULL OR difficulty BETWEEN 0.0 AND 1.0),
    novel_targets   INTEGER   NOT NULL DEFAULT 0 CHECK (novel_targets >= 0),
    repeated        BOOLEAN   NOT NULL DEFAULT FALSE,
    status          VARCHAR   NOT NULL DEFAULT 'planned' CHECK (
        status IN ('planned', 'active', 'completed', 'skipped')
    ),
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX session_blocks_sequence ON session_blocks (session_id, sequence);

-- Which items a block was planned around. A separate table because the novelty cap is a
-- promise about the session as a whole, and `db check` has to be able to count it.
CREATE TABLE session_block_targets (
    block_id   VARCHAR   NOT NULL REFERENCES session_blocks (block_id),
    content_id VARCHAR   NOT NULL REFERENCES knowledge_items (content_id),
    sequence   INTEGER   NOT NULL CHECK (sequence > 0),
    novel      BOOLEAN   NOT NULL DEFAULT FALSE,
    due        BOOLEAN   NOT NULL DEFAULT FALSE,
    stage      VARCHAR   NOT NULL CHECK (length(stage) > 0),
    priority   INTEGER   NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (block_id, content_id)
);

-- A candidate the planner ranked, including the ones it did not schedule. The plan owes
-- the reader a reason for every high-priority omission, and this is where the reason
-- lives so that `plan show` can give the same answer a week later.
CREATE TABLE session_plan_candidates (
    session_id      VARCHAR   NOT NULL REFERENCES sessions (session_id),
    block_type      VARCHAR   NOT NULL CHECK (length(block_type) > 0),
    sequence        INTEGER   NOT NULL CHECK (sequence > 0),
    score           DOUBLE    NOT NULL,
    selected        BOOLEAN   NOT NULL,
    omission_reason VARCHAR,
    components_json VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(components_json)),
    created_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (session_id, sequence),
    -- A selected candidate needs no reason; an omitted one is exactly what this table is
    -- for, so it must carry one.
    CHECK (selected OR omission_reason IS NOT NULL)
);

CREATE TABLE activities (
    activity_id   VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(activity_id, 'act_')),
    block_id      VARCHAR   NOT NULL REFERENCES session_blocks (block_id),
    sequence      INTEGER   NOT NULL CHECK (sequence > 0),
    kind          VARCHAR   NOT NULL CHECK (length(kind) > 0),
    prompt        VARCHAR   NOT NULL CHECK (length(prompt) > 0),
    settings_json VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(settings_json)),
    status        VARCHAR   NOT NULL DEFAULT 'planned' CHECK (
        status IN ('planned', 'active', 'completed', 'skipped')
    ),
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX activities_sequence ON activities (block_id, sequence);

CREATE TABLE session_packages (
    ingestion_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(ingestion_id, 'ing_')),
    -- The producer's own identifier. Not a LinguaWiki ID: it comes from a tool this
    -- repository does not control, and rewriting it would lose the only handle the
    -- learner has on their own recording.
    package_id          VARCHAR   NOT NULL CHECK (length(package_id) > 0),
    schema_name         VARCHAR   NOT NULL CHECK (length(schema_name) > 0),
    schema_version      INTEGER   NOT NULL CHECK (schema_version > 0),
    -- The canonical hash of the validated package. Deduplication is by *content*: the
    -- same conversation exported twice is one session however the bytes were arranged.
    package_hash        VARCHAR   NOT NULL CHECK (length(package_hash) = 64),
    -- The bytes as they arrived, so provenance survives a re-serialization.
    file_sha256         VARCHAR   CHECK (file_sha256 IS NULL OR length(file_sha256) = 64),
    track_id            VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    session_id          VARCHAR   REFERENCES sessions (session_id),
    external_session_id VARCHAR   NOT NULL CHECK (length(external_session_id) > 0),
    producer            VARCHAR,
    mode                VARCHAR   NOT NULL CHECK (mode IN ('checkpoint', 'completed')),
    ingest_status       VARCHAR   NOT NULL DEFAULT 'staged' CHECK (
        ingest_status IN ('staged', 'materialized', 'rejected')
    ),
    -- What arrived, without the transcript itself: counts, layers, artifacts, retention.
    -- Staged payloads follow the learner's retention policy, so the manifest never
    -- becomes a place a transcript survives a refusal to keep one.
    manifest_json       VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(manifest_json)),
    retention_policy    VARCHAR   NOT NULL DEFAULT 'withheld' CHECK (
        retention_policy IN ('withheld', 'excerpt', 'full')
    ),
    event_count         INTEGER   NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    artifact_count      INTEGER   NOT NULL DEFAULT 0 CHECK (artifact_count >= 0),
    audio_available     BOOLEAN   NOT NULL DEFAULT FALSE,
    started_at          TIMESTAMP NOT NULL,
    ended_at            TIMESTAMP NOT NULL,
    ingested_at         TIMESTAMP NOT NULL,
    created_at          TIMESTAMP NOT NULL,
    CHECK (ended_at >= started_at)
);

CREATE UNIQUE INDEX session_packages_hash ON session_packages (package_hash);

CREATE TABLE session_event_batches (
    batch_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(batch_id, 'bat_')),
    session_id        VARCHAR   NOT NULL REFERENCES sessions (session_id),
    -- Monotonic within one session, starting at 1. The gap-free rule is asserted by
    -- `db check`: a missing sequence means a flush was lost, and a close that silently
    -- materialized the rest would credit an incomplete session as a whole one.
    sequence          INTEGER   NOT NULL CHECK (sequence > 0),
    idempotency_key   VARCHAR   NOT NULL CHECK (length(idempotency_key) > 0),
    content_hash      VARCHAR   NOT NULL CHECK (length(content_hash) = 64),
    event_count       INTEGER   NOT NULL CHECK (event_count >= 0),
    source            VARCHAR   NOT NULL DEFAULT 'skill' CHECK (
        source IN ('skill', 'package')
    ),
    ingestion_id      VARCHAR   REFERENCES session_packages (ingestion_id),
    block_id          VARCHAR   REFERENCES session_blocks (block_id),
    validation_status VARCHAR   NOT NULL DEFAULT 'valid' CHECK (
        validation_status IN ('valid', 'rejected')
    ),
    created_at        TIMESTAMP NOT NULL,
    -- A batch that came from a package must say which one, and one that did not must not
    -- claim to have.
    CHECK ((source = 'package') = (ingestion_id IS NOT NULL))
);

CREATE UNIQUE INDEX session_event_batches_sequence ON session_event_batches (session_id, sequence);
CREATE UNIQUE INDEX session_event_batches_idempotency ON session_event_batches (idempotency_key);

CREATE TABLE session_staged_events (
    staged_event_id  VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(staged_event_id, 'sev_')
    ),
    session_id       VARCHAR   NOT NULL REFERENCES sessions (session_id),
    batch_id         VARCHAR   NOT NULL REFERENCES session_event_batches (batch_id),
    sequence         INTEGER   NOT NULL CHECK (sequence > 0),
    kind             VARCHAR   NOT NULL CHECK (
        kind IN ('attempt.observed', 'correction.given', 'pronunciation.assessment',
                 'observation.noted', 'follow_up')
    ),
    -- When the observation *happened*, which is not when it was flushed. A block worked
    -- at 18:30 and flushed at 19:10 is one observation with two timestamps, and the
    -- learner model depends on the first: delay derivation, chronology, error timing,
    -- and every estimate snapshot are computed from when the learner did the thing.
    occurred_at      TIMESTAMP NOT NULL,
    -- The identifier the producer gave this observation: the skill's own event ID, or an
    -- external session's. It is what makes an observation *the same observation* across
    -- a retried flush, an overlapping checkpoint export, and a re-ingested package --
    -- none of which the row's own primary key can express, because each of those arrives
    -- as a new row. Without it, one event delivered twice was two observations, and the
    -- learner was credited for work they did once.
    source_event_id  VARCHAR   NOT NULL CHECK (length(source_event_id) > 0),
    schema_version   INTEGER   NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    payload_json     VARCHAR   NOT NULL CHECK (json_valid(payload_json)),
    block_id         VARCHAR   REFERENCES session_blocks (block_id),
    activity_id      VARCHAR   REFERENCES activities (activity_id),
    -- Whether the evidence behind this event is the learner's audible performance or
    -- only a transcript of it. A pronunciation claim is confirmable from `audio` and
    -- never from `transcript`: a correct transcript proves nothing about how it sounded.
    evidence_basis   VARCHAR   NOT NULL DEFAULT 'direct' CHECK (
        evidence_basis IN ('direct', 'transcript', 'audio')
    ),
    status           VARCHAR   NOT NULL DEFAULT 'staged' CHECK (
        status IN ('staged', 'materialized', 'discarded', 'rejected')
    ),
    -- Written by the close that consumed this row. Not foreign keys: see the note at the
    -- top of this file. `integrity.ORPHAN_RELATIONS` carries all four.
    finalization_id  VARCHAR,
    -- What the event became. `error-occurrence` rather than `error` on purpose: two
    -- corrections of the same pattern are two *occurrences* of one error, and naming the
    -- pattern here would make them look like one durable row claimed twice.
    materialized_kind VARCHAR  CHECK (
        materialized_kind IS NULL
        OR materialized_kind IN
            ('attempt', 'error-occurrence', 'followup', 'observation')
    ),
    materialized_id  VARCHAR,
    discard_reason   VARCHAR,
    created_at       TIMESTAMP NOT NULL,
    -- A materialized row names what it became; anything else names nothing. Written as
    -- one CHECK over three columns so a half-recorded materialization cannot exist.
    CHECK (
        (status = 'materialized') =
        (finalization_id IS NOT NULL AND materialized_kind IS NOT NULL
         AND materialized_id IS NOT NULL)
    ),
    -- Discarding is a decision, so it comes with its reason.
    CHECK ((status IN ('discarded', 'rejected')) = (discard_reason IS NOT NULL))
);

CREATE UNIQUE INDEX session_staged_events_sequence
    ON session_staged_events (batch_id, sequence);

-- One observation, once, per session. Scoped to the session rather than globally because
-- recovering staged work into a *new* session is a legitimate second row for the same
-- event, and that is the one case where the same identity should appear twice.
CREATE UNIQUE INDEX session_staged_events_source
    ON session_staged_events (session_id, source_event_id);

CREATE TABLE session_finalizations (
    finalization_id      VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(finalization_id, 'fin_')
    ),
    session_id           VARCHAR   NOT NULL REFERENCES sessions (session_id),
    outcome              VARCHAR   NOT NULL CHECK (outcome IN ('completed', 'partial')),
    idempotency_key      VARCHAR   NOT NULL CHECK (length(idempotency_key) > 0),
    -- The batch range this close consumed. A later flush cannot be folded into an
    -- earlier close, and the range is what makes that checkable.
    first_batch_sequence INTEGER   CHECK (first_batch_sequence IS NULL OR first_batch_sequence > 0),
    last_batch_sequence  INTEGER   CHECK (last_batch_sequence IS NULL OR last_batch_sequence > 0),
    staged_consumed      INTEGER   NOT NULL DEFAULT 0 CHECK (staged_consumed >= 0),
    staged_discarded     INTEGER   NOT NULL DEFAULT 0 CHECK (staged_discarded >= 0),
    attempts_written     INTEGER   NOT NULL DEFAULT 0 CHECK (attempts_written >= 0),
    evidence_written     INTEGER   NOT NULL DEFAULT 0 CHECK (evidence_written >= 0),
    errors_written       INTEGER   NOT NULL DEFAULT 0 CHECK (errors_written >= 0),
    followups_written    INTEGER   NOT NULL DEFAULT 0 CHECK (followups_written >= 0),
    observations_written INTEGER   NOT NULL DEFAULT 0 CHECK (observations_written >= 0),
    -- Which policy versions produced the derived state, so a later recomputation can
    -- tell what this close's numbers were made of.
    calculation_versions_json VARCHAR NOT NULL DEFAULT '{}' CHECK (
        json_valid(calculation_versions_json)
    ),
    -- The report this close returned. A close whose response never reached the caller is
    -- retried, and the retry has to answer with the first one's result rather than
    -- redoing the work: this column is what makes that possible.
    result_json          VARCHAR   NOT NULL CHECK (json_valid(result_json)),
    created_at           TIMESTAMP NOT NULL,
    CHECK (
        first_batch_sequence IS NULL
        OR last_batch_sequence IS NULL
        OR last_batch_sequence >= first_batch_sequence
    )
);

-- One close per session. The uniqueness is over an immutable column, so it cannot get in
-- the way of a later UPDATE the way an index over mutable state would.
CREATE UNIQUE INDEX session_finalizations_session ON session_finalizations (session_id);
CREATE UNIQUE INDEX session_finalizations_idempotency
    ON session_finalizations (idempotency_key);
