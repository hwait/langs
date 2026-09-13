-- An override of the low-confidence rule is a decision, so the schema records it as one.
--
-- Calling a badly-heard line the learner's mistake is refused, and a person who has
-- listened to the audio can say so anyway. That override was accepted from any caller and
-- stored nowhere: an AI could turn a mishearing into a learner error, and the durable
-- record looked exactly like a confident, unremarkable correction.
--
-- The columns are not enough on their own. A service guard can be bypassed by a restore,
-- a hand-repaired database, or a build under a looser rule, and the whole point of the
-- override is that somebody *could have heard* the audio -- so the pairing is a CHECK.
-- DuckDB cannot add a column with a constraint, so the table is rebuilt; it is small, and
-- 0025 is unreleased alongside this file.

CREATE TABLE utterance_interpretations_rebuilt (
    interpretation_id VARCHAR NOT NULL PRIMARY KEY CHECK (
        starts_with(interpretation_id, 'int_')
    ),
    utterance_id      VARCHAR NOT NULL REFERENCES utterances (utterance_id),
    classification    VARCHAR NOT NULL CHECK (
        classification IN ('learner-error', 'transcription-artifact', 'uncertain')
    ),
    meaning           VARCHAR CHECK (meaning IS NULL OR length(meaning) <= 2000),
    corrected_form    VARCHAR CHECK (corrected_form IS NULL OR length(corrected_form) <= 2000),
    explanation       VARCHAR CHECK (explanation IS NULL OR length(explanation) <= 2000),
    confidence        VARCHAR NOT NULL DEFAULT 'medium' CHECK (
        confidence IN ('low', 'medium', 'high')
    ),
    reviewer_kind     VARCHAR NOT NULL DEFAULT 'ai' CHECK (
        reviewer_kind IN ('deterministic', 'ai', 'learner', 'human')
    ),
    reviewer          VARCHAR,
    error_id          VARCHAR,
    -- Whether somebody overruled the transcriber's own uncertainty to call this the
    -- learner's mistake, and why. On the row rather than in a log, because it is the
    -- reason a learner may want to argue with the correction.
    overrode_low_confidence BOOLEAN,
    override_reason   VARCHAR,
    created_at        TIMESTAMP NOT NULL,
    -- An override asserts that somebody heard the sound. Only a reviewer who could have
    -- done so may claim it, and a reason that is absent or blank is not a reason.
    CHECK (
        overrode_low_confidence IS NOT TRUE
        OR (
            reviewer_kind IN ('human', 'learner')
            AND override_reason IS NOT NULL
            AND length(trim(override_reason)) > 0
        )
    ),
    -- And a reason without an override is a reason for nothing.
    CHECK (override_reason IS NULL OR overrode_low_confidence IS TRUE)
);

INSERT INTO utterance_interpretations_rebuilt
SELECT interpretation_id, utterance_id, classification, meaning, corrected_form, explanation,
       confidence, reviewer_kind, reviewer, error_id, NULL, NULL, created_at
FROM utterance_interpretations;

DROP TABLE utterance_interpretations;

ALTER TABLE utterance_interpretations_rebuilt RENAME TO utterance_interpretations;
