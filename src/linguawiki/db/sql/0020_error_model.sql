-- Recurring errors, their occurrences, and the counter-evidence that can retire one.
--
-- `error_id` is *derived* from (track, category, normalized signature, target), so
-- deduplication rides on the primary key and no second unique index restates it. That
-- also settles what a NULL target means: a pattern with no target item has one identity,
-- not one per occurrence, which a unique index over nullable columns could not express.
CREATE TABLE error_patterns (
    error_id          VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(error_id, 'err_')),
    track_id          VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    category          VARCHAR   NOT NULL CHECK (length(category) > 0),
    -- The normalized form identity is computed from. The raw form the learner produced
    -- lives on the occurrence, where it belongs.
    signature         VARCHAR   NOT NULL CHECK (length(signature) > 0),
    description       VARCHAR   NOT NULL CHECK (length(description) > 0),
    -- Not a foreign key: `knowledge merge` repoints an error from a duplicate item onto
    -- the item it was folded into, and DuckDB rewrites an update of a foreign-key column
    -- as a delete and an insert, which this row -- referenced by occurrences,
    -- counter-evidence, and follow-ups -- refuses. `db check` carries the relation.
    target_content_id VARCHAR,
    severity          VARCHAR   NOT NULL DEFAULT 'medium' CHECK (
        severity IN ('low', 'medium', 'high')
    ),
    -- The five lifecycle states, plus two that are not stages of an error's life:
    --
    -- `unconfirmed` -- every occurrence so far was a transcription artifact or was
    -- classified uncertain. The pattern is recorded so the artifact has somewhere to
    -- live, and is deliberately not counted against the learner: a mishearing is not a
    -- mistake, and teaching one back would be worse than losing it.
    --
    -- `superseded` -- the identity was re-derived when a duplicate knowledge item was
    -- merged away. Retained rather than deleted: DuckDB cannot delete a referenced row
    -- in the same transaction that repointed its children, and the repository deprecates
    -- history rather than removing it. It names its successor.
    status            VARCHAR   NOT NULL DEFAULT 'observed' CHECK (
        status IN ('observed', 'active', 'monitoring', 'resolved', 'reactivated',
                   'unconfirmed', 'superseded')
    ),
    status_reason     VARCHAR,
    -- Written after insertion and self-referencing, so not a foreign key on either
    -- count; `db check` carries the relation.
    superseded_by     VARCHAR,
    policy_version    VARCHAR   NOT NULL CHECK (length(policy_version) > 0),
    -- Confirmed occurrences only: an artifact or an uncertain classification is kept on
    -- the occurrence row but never counts toward activation.
    occurrence_count  INTEGER   NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
    success_count     INTEGER   NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    first_seen_at     TIMESTAMP NOT NULL,
    last_seen_at      TIMESTAMP NOT NULL,
    last_success_at   TIMESTAMP,
    monitoring_since  TIMESTAMP,
    resolved_at       TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL
);

CREATE TABLE error_occurrences (
    occurrence_id  VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(occurrence_id, 'err_')),
    error_id       VARCHAR   NOT NULL REFERENCES error_patterns (error_id),
    attempt_id     VARCHAR   REFERENCES attempts (attempt_id),
    -- What the learner actually produced and what it should have been. Bounded like any
    -- other retained learner text.
    learner_form   VARCHAR   CHECK (learner_form IS NULL OR length(learner_form) <= 2000),
    corrected_form VARCHAR   CHECK (corrected_form IS NULL OR length(corrected_form) <= 2000),
    explanation    VARCHAR,
    meaning_impact VARCHAR   NOT NULL DEFAULT 'minor' CHECK (
        meaning_impact IN ('none', 'minor', 'major', 'breakdown')
    ),
    -- A microphone artifact is not a learner error. Recording the distinction is what
    -- keeps a mishearing from being taught back as a mistake.
    classification VARCHAR   NOT NULL DEFAULT 'learner-error' CHECK (
        classification IN ('learner-error', 'transcription-artifact', 'uncertain')
    ),
    confidence     VARCHAR   NOT NULL DEFAULT 'medium' CHECK (
        confidence IN ('low', 'medium', 'high')
    ),
    observed_at    TIMESTAMP NOT NULL,
    recorded_at    TIMESTAMP NOT NULL
);

-- Counter-evidence. One evidence row can qualify in several ways at once -- a delayed,
-- novel, spontaneous success is all three -- and the policy counts each way separately,
-- so the qualification is part of the key rather than a column.
CREATE TABLE error_evidence (
    error_id      VARCHAR   NOT NULL REFERENCES error_patterns (error_id),
    evidence_id   VARCHAR   NOT NULL REFERENCES evidence (evidence_id),
    qualification VARCHAR   NOT NULL CHECK (
        qualification IN ('controlled', 'novel', 'spontaneous', 'delayed')
    ),
    context_key   VARCHAR   NOT NULL CHECK (length(context_key) > 0),
    recorded_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (error_id, evidence_id, qualification)
);

CREATE TABLE followups (
    followup_id       VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(followup_id, 'fup_')),
    track_id          VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    kind              VARCHAR   NOT NULL CHECK (
        kind IN ('review', 'clarify', 'practice', 'resource', 'assessment')
    ),
    action            VARCHAR   NOT NULL CHECK (length(action) > 0),
    target_content_id VARCHAR   REFERENCES knowledge_items (content_id),
    error_id          VARCHAR   REFERENCES error_patterns (error_id),
    origin_attempt_id VARCHAR   REFERENCES attempts (attempt_id),
    priority          INTEGER   NOT NULL DEFAULT 0,
    status            VARCHAR   NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'scheduled', 'done', 'dropped')
    ),
    due_from          TIMESTAMP,
    due_by            TIMESTAMP,
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL
);
