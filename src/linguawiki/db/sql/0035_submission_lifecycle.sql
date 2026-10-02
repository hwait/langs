-- C6: a submission's lifecycle while a judgement is in flight, and a round served at once.
--
-- C5 judged a recording in the request that delivered the verdict. C6 lets the judging
-- happen elsewhere and later: a judge claims a submission, the claim lapses or is
-- released, a verdict arrives -- possibly while the run is paused -- and is applied or
-- voided. Each of those is a row, because nothing is held in memory between invocations,
-- and each names `submission_id`, never `(run_id, content_id)`: the pair outlives the
-- recording a supersession purged.
--
-- Relations. A row written in place must not be referenced by a foreign key: DuckDB
-- rewrites such an update as a delete and an insert, which a referenced row refuses.
-- Submissions (status, supersession, withdrawal, `response_text`), results
-- (`invalidated_*`), and runs (status) are written in place, so every new column naming
-- one of them is unenforced and carried by `integrity.ORPHAN_RELATIONS`. Claims, verdicts,
-- and outcomes are insert-only today; they are checked relations all the same, so the
-- first later stage that writes one in place does not have to rebuild a table. A batch and
-- its tasks are written together and never touched again, which is the one case a
-- foreign key is safe for.

-- The submission, rebuilt to carry its kind.
--
-- A judged written answer is the second kind of thing a judge hears. Its identity is the
-- producer's key, exactly as a recording's is the capture's, so `capture_id` stays
-- required for both kinds and the existing unique index deduplicates a resent answer with
-- no new mechanism. Where the recording's bytes live in `artifacts`, a written answer's
-- retained form lives on the row: `response_text` as `retain_response` kept it, its
-- visibility, and the digest of the full text.
--
-- `response_text` may be NULL on a withdrawn text submission only: withdrawing transcript
-- consent withdraws the submission and clears its text in the same statement, and the
-- digest stays so the record still says *which* answer it was.
--
-- DuckDB cannot add a column with a constraint, so the table is rebuilt; nothing has a
-- foreign key to it (0034 created it, and every relation naming it since is unenforced).
--
-- Rebuilt in place rather than renamed. A table created under another name and renamed
-- keeps that name in the catalog of every table it references: deleting an
-- `assessment_tasks` row afterwards failed with "Table with name
-- assessment_submissions_rebuilt does not exist". So the rows wait in an unconstrained
-- copy -- every C5 submission is a recording -- and return to a table created under its
-- own name.
CREATE TABLE assessment_submissions__0035 AS
SELECT submission_id, run_id, content_id, 'recording' AS kind, capture_id, artifact_id,
       CAST(NULL AS VARCHAR) AS response_visibility, CAST(NULL AS VARCHAR) AS response_text,
       CAST(NULL AS VARCHAR) AS response_digest, status, superseded_by, withdrawn_code,
       withdrawn_reason, created_at, updated_at
FROM assessment_submissions;

DROP TABLE assessment_submissions;

CREATE TABLE assessment_submissions (
    submission_id       VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(submission_id, 'asm_')),
    run_id              VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    content_id          VARCHAR   NOT NULL REFERENCES assessment_tasks (content_id),
    kind                VARCHAR   NOT NULL CHECK (kind IN ('recording', 'text')),
    -- The capture's identifier for a recording, the submission key for a written answer.
    capture_id          VARCHAR   NOT NULL CHECK (length(trim(capture_id)) > 0),
    -- Not a foreign key, like every column naming an artifact: a purge rewrites the
    -- artifact row. `submission_artifacts_agree` asserts it.
    artifact_id         VARCHAR   CHECK (artifact_id IS NULL OR starts_with(artifact_id, 'art_')),
    response_visibility VARCHAR   CHECK (
        response_visibility IS NULL OR response_visibility IN ('withheld', 'excerpt', 'full')
    ),
    response_text       VARCHAR   CHECK (response_text IS NULL OR length(trim(response_text)) > 0),
    response_digest     VARCHAR   CHECK (response_digest IS NULL OR length(response_digest) = 64),
    status              VARCHAR   NOT NULL CHECK (
        status IN ('pending', 'judged', 'superseded', 'withdrawn')
    ),
    -- Self-referencing and written after insertion, so not a foreign key on either count.
    superseded_by       VARCHAR,
    withdrawn_code      VARCHAR,
    withdrawn_reason    VARCHAR,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
    CHECK ((status = 'withdrawn') = (withdrawn_code IS NOT NULL AND withdrawn_reason IS NOT NULL)),
    -- A recording is its artifact; a written answer has none.
    CHECK ((kind = 'recording') = (artifact_id IS NOT NULL)),
    -- And a recording carries no text: its words, if anybody keeps them, are a transcript.
    CHECK (
        kind = 'text'
        OR (response_visibility IS NULL AND response_text IS NULL AND response_digest IS NULL)
    ),
    CHECK (
        kind = 'recording'
        OR (
            response_visibility IS NOT NULL
            AND response_digest IS NOT NULL
            AND (response_text IS NOT NULL OR status = 'withdrawn')
        )
    ),
    -- `withheld` keeps a digest and nothing else, wherever the text came from.
    CHECK (response_visibility IS DISTINCT FROM 'withheld' OR response_text IS NULL)
);

INSERT INTO assessment_submissions SELECT * FROM assessment_submissions__0035;

DROP TABLE assessment_submissions__0035;

-- One capture -- or one submission key -- is one submission.
CREATE UNIQUE INDEX assessment_submissions_capture ON assessment_submissions (capture_id);

-- A judge's lease on one submission.
--
-- The lease schedules; it does not decide correctness. Whether a claim is live, expired,
-- released, or ended by a verdict is derived from this row, its release, and the verdicts
-- naming it -- never stored -- and the attempt count is the number of claims made.
CREATE TABLE judging_claims (
    claim_id         VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(claim_id, 'asm_')),
    -- Not a foreign key: the submission is written in place. `ORPHAN_RELATIONS` carries it.
    submission_id    VARCHAR   NOT NULL CHECK (starts_with(submission_id, 'asm_')),
    judge            VARCHAR   NOT NULL CHECK (length(trim(judge)) > 0),
    claimed_at       TIMESTAMP NOT NULL,
    lease_expires_at TIMESTAMP NOT NULL,
    CHECK (lease_expires_at > claimed_at)
);

-- The end of a claim that no verdict ended: the judge gave it back, or gave up.
--
-- One release per claim, so the claim is the key. A terminal release withdraws the
-- submission, and the code it withdrew with is part of the contract, so it is required.
CREATE TABLE judging_releases (
    claim_id    VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(claim_id, 'asm_')),
    released_at TIMESTAMP NOT NULL,
    terminal    BOOLEAN   NOT NULL,
    code        VARCHAR   CHECK (code IS NULL OR length(trim(code)) > 0),
    reason      VARCHAR   NOT NULL CHECK (length(trim(reason)) > 0),
    CHECK (NOT terminal OR code IS NOT NULL)
);

-- What a judge said about one submission, as it arrived.
--
-- Receipt and application are one commit when the run is open; while it is paused the
-- verdict waits here with no outcome -- *held* -- and is revalidated when it is applied.
-- The idempotency key lives where every key lives, on the domain event, whose payload
-- names this row; a second home for it would be a second answer to "was this delivered?".
--
-- The response and rubric fields hold the *retained* form only: the same retention rule
-- the result applies, applied before this row is written.
CREATE TABLE assessment_verdicts (
    verdict_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(verdict_id, 'asm_')),
    submission_id       VARCHAR   NOT NULL CHECK (starts_with(submission_id, 'asm_')),
    -- NULL for a verdict delivered without a claim: a keyless `record`, or a C5 verdict.
    claim_id            VARCHAR   CHECK (claim_id IS NULL OR starts_with(claim_id, 'asm_')),
    raw_score           DOUBLE    NOT NULL CHECK (raw_score BETWEEN 0.0 AND 1.0),
    rubric_json         VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(rubric_json)),
    assessor_kind       VARCHAR   NOT NULL CHECK (
        assessor_kind IN ('deterministic', 'ai', 'learner', 'human')
    ),
    assessor            VARCHAR,
    confidence          VARCHAR   NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    response_visibility VARCHAR   CHECK (
        response_visibility IS NULL OR response_visibility IN ('withheld', 'excerpt', 'full')
    ),
    response_excerpt    VARCHAR,
    response_hash       VARCHAR   CHECK (response_hash IS NULL OR length(response_hash) = 64),
    received_at         TIMESTAMP NOT NULL
);

-- What became of a verdict: applied, producing a result, or void, with the reason.
--
-- A verdict with no outcome is held. One outcome per verdict, so the verdict is the key.
CREATE TABLE assessment_verdict_outcomes (
    verdict_id VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(verdict_id, 'asm_')),
    outcome    VARCHAR   NOT NULL CHECK (outcome IN ('applied', 'void')),
    -- Not a foreign key: a result is invalidated in place when its recording is purged.
    result_id  VARCHAR   CHECK (result_id IS NULL OR starts_with(result_id, 'asm_')),
    code       VARCHAR   CHECK (code IS NULL OR length(trim(code)) > 0),
    reason     VARCHAR   CHECK (reason IS NULL OR length(trim(reason)) > 0),
    decided_at TIMESTAMP NOT NULL,
    CHECK ((outcome = 'applied') = (result_id IS NOT NULL)),
    CHECK ((outcome = 'void') = (code IS NOT NULL AND reason IS NOT NULL))
);

-- A round: one task per open dimension, served in one transaction.
--
-- Membership is persisted so a retry with the key returns the same tasks, each in its
-- current state, and serves nothing new.
CREATE TABLE assessment_batches (
    batch_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(batch_id, 'asm_')),
    run_id          VARCHAR   NOT NULL REFERENCES assessment_runs (run_id),
    idempotency_key VARCHAR   NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    request_hash    VARCHAR   NOT NULL CHECK (length(request_hash) = 64),
    created_at      TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX assessment_batches_idempotency ON assessment_batches (idempotency_key);

CREATE TABLE assessment_batch_tasks (
    batch_id   VARCHAR NOT NULL REFERENCES assessment_batches (batch_id),
    position   INTEGER NOT NULL CHECK (position >= 1),
    content_id VARCHAR NOT NULL REFERENCES assessment_tasks (content_id),
    dimension  VARCHAR NOT NULL CHECK (length(dimension) > 0),
    PRIMARY KEY (batch_id, position)
);

-- Never two within a dimension: the next task in a dimension depends on the last answer.
CREATE UNIQUE INDEX assessment_batch_tasks_dimension ON assessment_batch_tasks (batch_id, dimension);
CREATE UNIQUE INDEX assessment_batch_tasks_content ON assessment_batch_tasks (batch_id, content_id);

-- When the learner answered, as against when the answer was scored.
--
-- A verdict delivered a week after the recording was made is evidence about the learner
-- of a week ago, and `recorded_at` -- the write time -- would date it to the verdict. For
-- a submission-bound result this is the submission's `created_at`; for any other result
-- it equals `recorded_at`. NULL only on results older than this migration, where the
-- distinction was never recorded. Unconstrained, because DuckDB refuses `ADD COLUMN` with
-- a constraint; `result_observation_times` asserts the rule over the data.
ALTER TABLE assessment_results ADD COLUMN observed_at TIMESTAMP;

-- Every C5 verdict, recorded as the verdict it was and the result it produced.
--
-- C5 judged in the request that delivered the verdict, so each `judged` submission has
-- exactly one result resting on its recording: same run, same task, same artifact. That
-- result is the verdict's only surviving account, so the verdict is rebuilt from it --
-- received when it was recorded, under no claim, applied to that result. Without this
-- the rule "a judged submission has an applied outcome" would hold only for data written
-- after it, and a validation that runs only when the data is present is not one.
--
-- Everything but the rubric. A result's rubric never went through retention (the
-- deferred C1 gap), and copying it here would make an insert-only second copy that no
-- later scrub of the result could reach; the verdict's outcome names the result that
-- still holds it.
--
-- The identifiers are derived rather than generated: DuckDB has no ULID, and a hex digest
-- of the submission is a valid opaque identifier (hex digits are Crockford digits) that a
-- rerun of this backfill on the same data reproduces.
INSERT INTO assessment_verdicts
SELECT 'asm_' || upper(substr(sha256('lingua.0035.verdict' || submission.submission_id), 1, 26)),
       submission.submission_id, NULL, result.raw_score, '{}',
       result.assessor_kind, result.assessor, result.confidence,
       result.response_visibility, result.response_excerpt, result.response_hash,
       result.recorded_at
FROM assessment_submissions submission
JOIN assessment_results result
  ON result.run_id = submission.run_id
 AND result.content_id = submission.content_id
 AND result.audio_artifact_id = submission.artifact_id
WHERE submission.status = 'judged';

INSERT INTO assessment_verdict_outcomes
SELECT 'asm_' || upper(substr(sha256('lingua.0035.verdict' || submission.submission_id), 1, 26)),
       'applied', result.result_id, NULL, NULL, result.recorded_at
FROM assessment_submissions submission
JOIN assessment_results result
  ON result.run_id = submission.run_id
 AND result.content_id = submission.content_id
 AND result.audio_artifact_id = submission.artifact_id
WHERE submission.status = 'judged';
