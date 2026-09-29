-- Attempts and atomic evidence: the only thing a progress change is ever derived from.
--
-- The split is the point. An `attempt` is what happened, in full: what was asked, what
-- help was given, how long it took, who judged it. An `evidence` row is one *claim* the
-- attempt justifies, and the same attempt justifies different claims depending on what
-- the learner actually had to do. Keeping both means an aggregation policy can change
-- and every stage can be recomputed, because the raw observations never moved.
--
-- An observation is never edited: correcting one means recording the correction, which is
-- also the only honest thing to do with evidence. The single exception is `merge`, which
-- repoints an observation from a duplicate item onto the item it was folded into --
-- the learner's history has to survive that or a merge would silently discard it.
--
-- That is why `target_content_id` is *not* a foreign key here. DuckDB rewrites an UPDATE
-- of a foreign-key column as a delete and an insert, which a row referenced by another
-- table refuses, and both tables are referenced. The relation is enforced by
-- `integrity.ORPHAN_RELATIONS` instead, which `db check` reports on by name.
CREATE TABLE attempts (
    attempt_id        VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(attempt_id, 'att_')),
    track_id          VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    -- `session` is listed now so the session engine needs no new constraint; DuckDB
    -- cannot drop a CHECK, and there is no session table for it to reference yet.
    origin            VARCHAR   NOT NULL CHECK (
        origin IN ('assessment', 'curriculum-audit', 'import', 'repair', 'session')
    ),
    assessment_run_id VARCHAR   REFERENCES assessment_runs (run_id),
    task_content_id   VARCHAR   REFERENCES assessment_tasks (content_id),
    -- Deliberately not a foreign key; see the note above. `db check` carries it.
    target_content_id VARCHAR,
    dimension         VARCHAR,
    task_type         VARCHAR   NOT NULL CHECK (
        task_type IN ('objective', 'short-response', 'extended-productive',
                      'pronunciation-target', 'connected-speech', 'meaning-focused-exchange',
                      'reading-comprehension', 'listening-comprehension', 'recall-prompt')
    ),
    modality          VARCHAR   NOT NULL CHECK (
        modality IN ('text', 'audio', 'speech', 'writing')
    ),
    outcome           VARCHAR   NOT NULL CHECK (outcome IN ('success', 'partial', 'failure')),
    normalized_score  DOUBLE    NOT NULL CHECK (normalized_score BETWEEN 0.0 AND 1.0),
    help_level        VARCHAR   NOT NULL CHECK (
        help_level IN ('none', 'prompted', 'hinted', 'scaffolded', 'full-answer')
    ),
    correction_mode   VARCHAR   NOT NULL DEFAULT 'none' CHECK (
        correction_mode IN ('none', 'delayed', 'immediate', 'recast', 'explicit')
    ),
    -- Immediate repetition and delayed retrieval are different facts about memory, so
    -- they are stored rather than inferred from timestamps a caller may not have.
    retrieval         VARCHAR   NOT NULL CHECK (
        retrieval IN ('immediate', 'same-session', 'delayed')
    ),
    delay_hours       DOUBLE    CHECK (delay_hours IS NULL OR delay_hours >= 0.0),
    latency_ms        INTEGER   CHECK (latency_ms IS NULL OR latency_ms >= 0),
    source_difficulty DOUBLE,
    context_key       VARCHAR   NOT NULL CHECK (length(context_key) > 0),
    -- What of the learner's own words is kept. `withheld` means the response existed and
    -- was judged, and only its hash remains: consent decides, not convenience.
    response_visibility VARCHAR NOT NULL DEFAULT 'withheld' CHECK (
        response_visibility IN ('withheld', 'excerpt', 'full')
    ),
    response_excerpt  VARCHAR   CHECK (
        response_excerpt IS NULL OR length(response_excerpt) <= 2000
    ),
    response_hash     VARCHAR   CHECK (response_hash IS NULL OR length(response_hash) = 64),
    assessor_kind     VARCHAR   NOT NULL CHECK (
        assessor_kind IN ('deterministic', 'ai', 'learner', 'human')
    ),
    assessor          VARCHAR,
    confidence        VARCHAR   NOT NULL DEFAULT 'medium' CHECK (
        confidence IN ('low', 'medium', 'high')
    ),
    strength_version  VARCHAR   NOT NULL CHECK (length(strength_version) > 0),
    idempotency_key   VARCHAR,
    occurred_at       TIMESTAMP NOT NULL,
    recorded_at       TIMESTAMP NOT NULL,
    -- An attempt with neither a target item nor a dimension measures nothing.
    CHECK (target_content_id IS NOT NULL OR dimension IS NOT NULL),
    -- A withheld response still records that there was one; an excerpt or a full text
    -- has to actually be there, or the visibility is a claim about nothing.
    CHECK (response_visibility = 'withheld' OR response_excerpt IS NOT NULL)
);

CREATE UNIQUE INDEX attempts_idempotency ON attempts (idempotency_key);

CREATE TABLE evidence (
    evidence_id       VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(evidence_id, 'evd_')),
    track_id          VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    attempt_id        VARCHAR   NOT NULL REFERENCES attempts (attempt_id),
    -- The five categories the plan keeps distinct, plus intelligibility, which is about
    -- how the learner sounds and so is about a dimension rather than an item.
    claim             VARCHAR   NOT NULL CHECK (
        claim IN ('recognition', 'comprehension', 'controlled-production',
                  'spontaneous-production', 'delayed-transfer', 'intelligibility')
    ),
    polarity          VARCHAR   NOT NULL CHECK (
        polarity IN ('positive', 'partial', 'negative')
    ),
    -- Deliberately not a foreign key; see the note above. `db check` carries it.
    target_content_id VARCHAR,
    dimension         VARCHAR,
    strength          DOUBLE    NOT NULL CHECK (strength BETWEEN 0.0 AND 1.0),
    -- The unit of diversity. Two successes in the same context are one observation
    -- repeated, and the aggregation has to be able to tell.
    context_key       VARCHAR   NOT NULL CHECK (length(context_key) > 0),
    novelty           VARCHAR   NOT NULL DEFAULT 'repeat' CHECK (novelty IN ('novel', 'repeat')),
    retrieval         VARCHAR   NOT NULL CHECK (
        retrieval IN ('immediate', 'same-session', 'delayed')
    ),
    delay_hours       DOUBLE    CHECK (delay_hours IS NULL OR delay_hours >= 0.0),
    help_level        VARCHAR   NOT NULL CHECK (
        help_level IN ('none', 'prompted', 'hinted', 'scaffolded', 'full-answer')
    ),
    modality          VARCHAR   NOT NULL CHECK (
        modality IN ('text', 'audio', 'speech', 'writing')
    ),
    task_type         VARCHAR   NOT NULL CHECK (length(task_type) > 0),
    assessor_kind     VARCHAR   NOT NULL CHECK (
        assessor_kind IN ('deterministic', 'ai', 'learner', 'human')
    ),
    confidence        VARCHAR   NOT NULL DEFAULT 'medium' CHECK (
        confidence IN ('low', 'medium', 'high')
    ),
    strength_version  VARCHAR   NOT NULL CHECK (length(strength_version) > 0),
    occurred_at       TIMESTAMP NOT NULL,
    recorded_at       TIMESTAMP NOT NULL,
    CHECK (target_content_id IS NOT NULL OR dimension IS NOT NULL)
);

-- One attempt can justify several distinct claims, but not the same claim twice about
-- the same target: that would double-count one observation into a promotion. The rule is
-- *not* a unique index, for the same reason `target_content_id` is not a foreign key: an
-- index covering that column would make `merge` unable to repoint the observation. It is
-- enforced when the row is written and checked by name in `db check`.

CREATE TABLE session_observations (
    observation_id VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(observation_id, 'obs_')),
    track_id       VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    attempt_id     VARCHAR   REFERENCES attempts (attempt_id),
    category       VARCHAR   NOT NULL CHECK (
        category IN ('fatigue', 'confidence', 'strategy', 'notable-success', 'note')
    ),
    salience       VARCHAR   NOT NULL DEFAULT 'medium' CHECK (
        salience IN ('low', 'medium', 'high')
    ),
    note           VARCHAR   NOT NULL CHECK (length(note) > 0 AND length(note) <= 2000),
    -- An observation is written *about* the learner, not by them, so it is never a
    -- transcript; the visibility still travels with it so a bundle can honour consent.
    visibility     VARCHAR   NOT NULL DEFAULT 'excerpt' CHECK (
        visibility IN ('excerpt', 'full')
    ),
    observed_at    TIMESTAMP NOT NULL,
    recorded_at    TIMESTAMP NOT NULL
);
