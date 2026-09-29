-- What was said, who says so, and what may be claimed about how it sounded.
--
-- A spoken session arrives as text produced from sound, and the text is not the
-- evidence. Three tables keep the layers apart because collapsing them loses the one
-- thing that matters:
--
-- - `utterances` is the **raw** hearing, immutable. Correcting it in place would destroy
--   the record of what the transcription actually did, which is the only evidence that
--   it was ever wrong;
-- - `transcript_revisions` holds every later reading of the same utterance -- a tidy-up
--   or a person listening again -- each as a new row naming what it changed. A
--   normalization may not change which words were heard; a different hearing is a
--   different kind of claim and says so;
-- - `utterance_interpretations` is the pedagogical reading: what the learner meant, what
--   the correct form is, and crucially whether this was a **learner error** or a
--   **transcription artifact**. Teaching a mishearing back to the learner as a mistake is
--   worse than losing it, so only a confirmed learner error counts against them.
--
-- `pronunciation_observations` carries the acoustic rule the whole stage turns on: a
-- correct transcript proves nothing about how something sounded. A `confirmed` claim
-- needs audio that is still present, and prosody and native-likeness need it at every
-- status, because the words of a question and the words of a flat statement are the same
-- words. When audio is purged those claims are invalidated -- and only those: what the
-- learner *said* was established by the transcript, which is still there.

CREATE TABLE utterances (
    utterance_id     VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(utterance_id, 'utt_')),
    track_id         VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    -- The producer's own identifier for this utterance, as it appeared in the package.
    -- It is what makes one utterance the same utterance across a checkpoint export and
    -- the completed export of the same call.
    external_id      VARCHAR   NOT NULL CHECK (length(external_id) > 0),
    ingestion_id     VARCHAR   REFERENCES session_packages (ingestion_id),
    session_id       VARCHAR   REFERENCES sessions (session_id),
    source_id        VARCHAR   REFERENCES sources (source_id),
    speaker          VARCHAR   NOT NULL CHECK (speaker IN ('learner', 'tutor', 'other')),
    sequence         INTEGER   NOT NULL CHECK (sequence > 0),
    started_at       TIMESTAMP NOT NULL,
    ended_at         TIMESTAMP NOT NULL,
    -- What the machine or the notetaker first produced. Immutable: every later reading
    -- is a revision row, never an edit here.
    raw_text         VARCHAR   NOT NULL,
    -- The transcriber's own confidence, when it reported one. Kept because a low
    -- confidence is the first reason to suspect a mishearing rather than a mistake.
    raw_confidence   DOUBLE    CHECK (raw_confidence IS NULL OR raw_confidence BETWEEN 0.0 AND 1.0),
    -- The sound this text came from, when it was kept. Not a foreign key: an artifact is
    -- purged by repointing its own row, and DuckDB refuses to update a referenced row's
    -- key. `db check` carries the relation by name.
    audio_artifact_id VARCHAR,
    -- What of the learner's words this workspace kept, under the same retention rule
    -- every other learner text obeys.
    visibility       VARCHAR   NOT NULL DEFAULT 'withheld' CHECK (
        visibility IN ('withheld', 'excerpt', 'full')
    ),
    text_hash        VARCHAR   CHECK (text_hash IS NULL OR length(text_hash) = 64),
    policy_version   VARCHAR   NOT NULL CHECK (length(policy_version) > 0),
    recorded_at      TIMESTAMP NOT NULL,
    CHECK (ended_at >= started_at)
);

CREATE UNIQUE INDEX utterances_external ON utterances (track_id, external_id);

CREATE TABLE transcript_revisions (
    revision_id   VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(revision_id, 'trv_')),
    utterance_id  VARCHAR   NOT NULL REFERENCES utterances (utterance_id),
    layer         VARCHAR   NOT NULL CHECK (layer IN ('normalized', 'reviewed-hearing')),
    derived_from  VARCHAR   NOT NULL CHECK (derived_from IN ('raw', 'normalized')),
    -- What this revision claims to have done. A `normalization` that changes the words is
    -- a hearing claim wearing the wrong label, and the difference decides whether the
    -- learner is told they mispronounced something or the machine is told it misheard.
    kind          VARCHAR   NOT NULL CHECK (kind IN ('normalization', 'hearing')),
    text          VARCHAR   NOT NULL,
    reviewer_kind VARCHAR   NOT NULL DEFAULT 'ai' CHECK (
        reviewer_kind IN ('deterministic', 'ai', 'learner', 'human')
    ),
    reviewer      VARCHAR,
    reason        VARCHAR,
    confidence    VARCHAR   NOT NULL DEFAULT 'medium' CHECK (
        confidence IN ('low', 'medium', 'high')
    ),
    visibility    VARCHAR   NOT NULL DEFAULT 'withheld' CHECK (
        visibility IN ('withheld', 'excerpt', 'full')
    ),
    revision_hash VARCHAR   NOT NULL CHECK (length(revision_hash) = 64),
    created_at    TIMESTAMP NOT NULL
);

-- One reading per layer per utterance. A second review is a *new* hearing of the same
-- utterance, which supersedes by being later rather than by overwriting -- so the index
-- is over the layer, and a genuine re-review replaces the row it disagrees with.
CREATE UNIQUE INDEX transcript_revisions_layer
    ON transcript_revisions (utterance_id, layer);

CREATE TABLE utterance_interpretations (
    interpretation_id VARCHAR NOT NULL PRIMARY KEY CHECK (
        starts_with(interpretation_id, 'int_')
    ),
    utterance_id      VARCHAR NOT NULL REFERENCES utterances (utterance_id),
    -- The whole point of the table. `learner-error` is the only value that counts
    -- against the learner; the others are facts about the transcription or about
    -- nobody being sure.
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
    -- The error pattern this reading filed the occurrence against, when it filed one.
    -- Written after the fact by a close, so not a foreign key; `db check` carries it.
    error_id          VARCHAR,
    created_at        TIMESTAMP NOT NULL
);

CREATE TABLE pronunciation_observations (
    observation_id   VARCHAR   NOT NULL PRIMARY KEY CHECK (
        starts_with(observation_id, 'prn_')
    ),
    track_id         VARCHAR   NOT NULL REFERENCES learning_tracks (track_id),
    utterance_id     VARCHAR   REFERENCES utterances (utterance_id),
    -- What is being judged. Separate dimensions because a learner can be perfectly
    -- intelligible and nothing like a native speaker, and one number would hide that.
    dimension        VARCHAR   NOT NULL CHECK (
        dimension IN ('intelligibility', 'phonetic-accuracy', 'prosody', 'native-likeness')
    ),
    status           VARCHAR   NOT NULL CHECK (
        status IN ('observed', 'uncertain', 'confirmed')
    ),
    -- What the claim rests on. The pairing with `status` is the acoustic rule, and it is
    -- a CHECK rather than a convention: a confirmed claim from a transcript is a claim
    -- about sound made by reading.
    basis            VARCHAR   NOT NULL CHECK (basis IN ('direct', 'transcript', 'audio')),
    audio_artifact_id VARCHAR,
    target_content_id VARCHAR,
    note             VARCHAR   CHECK (note IS NULL OR length(note) <= 2000),
    reviewer_kind    VARCHAR   NOT NULL DEFAULT 'ai' CHECK (
        reviewer_kind IN ('deterministic', 'ai', 'learner', 'human')
    ),
    reviewer         VARCHAR,
    -- Set when the audio this rested on was purged. The claim stays on the record,
    -- marked as no longer supported, rather than disappearing: a learner who was told
    -- their vowel was wrong deserves to see that the evidence for it is gone.
    invalidated_at   TIMESTAMP,
    invalidation_reason VARCHAR,
    policy_version   VARCHAR   NOT NULL CHECK (length(policy_version) > 0),
    observed_at      TIMESTAMP NOT NULL,
    recorded_at      TIMESTAMP NOT NULL,
    -- Confirming is saying "I heard this". Without the sound it is saying "I read this".
    CHECK (status <> 'confirmed' OR basis = 'audio'),
    -- Text cannot carry prosody at any confidence: the words of a question and the words
    -- of a flat statement are the same words.
    CHECK (dimension NOT IN ('prosody', 'native-likeness') OR basis = 'audio'),
    -- An audio-based claim names the audio it is based on.
    CHECK (basis <> 'audio' OR audio_artifact_id IS NOT NULL),
    CHECK ((invalidated_at IS NULL) = (invalidation_reason IS NULL))
);
