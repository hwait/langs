---
title: "Speaking Ingestion and AI-Generated Content Provenance"
status: proposed
project: lingua-wiki
scope:
  - speaking-session-ingestion
  - transcript-management
  - audio-retention
  - generated-pack-provenance
  - content-review
last_updated: 2026-08-28
---

# Speaking Ingestion and AI-Generated Content Provenance

## Purpose

This document proposes answers to two architectural questions for the LinguaWiki project:

1. How should speaking audio and transcripts reach the AI agent?
2. What provenance and review policy should apply to AI-generated learning-pack content?

The proposed design should support:

- live AI-led speaking lessons;
- grammar and vocabulary analysis from transcripts;
- pronunciation analysis from audio;
- persistent learner-error tracking;
- generation of exercises and Anki cards;
- reproducible and auditable learning content;
- local-first privacy;
- replacement of individual AI, speech-to-text, or voice providers without redesigning the repository.

---

# 1. Speaking Audio and Transcript Ingestion

## 1.1 Recommendation

Use a provider-independent **session package** as the integration boundary between a speaking lesson and the LinguaWiki repository.

A speaking interface may be:

- ChatGPT voice mode;
- another commercial voice application;
- a local speech-to-text pipeline;
- a custom real-time tutor application;
- an uploaded voice recording.

Regardless of the interface, the repository agent should receive a normalized session package with:

- session metadata;
- timestamped learner transcript;
- tutor responses or event log;
- references to locally stored audio;
- selected audio clips requiring pronunciation review;
- identified corrections and learning events.

The repository must not depend on the internal conversation history or transcript format of one particular voice provider.

---

## 1.2 Separation of Responsibilities

The system should distinguish two components.

### Live tutor

The live tutor:

- conducts the conversation;
- asks questions;
- provides hints;
- performs role-play;
- adjusts language difficulty;
- gives immediate corrections when appropriate;
- maintains conversational flow.

### Repository agent

The repository agent:

- processes the completed session;
- updates learner state;
- records recurring errors;
- updates pronunciation targets;
- creates review tasks;
- generates Anki candidates;
- updates curriculum, book, or media progress;
- preserves relevant learning evidence.

These roles may be powered by the same model, but they should communicate through explicit files rather than relying on implicit chat memory.

---

## 1.3 Why Both Audio and Transcript Are Needed

### Transcript is appropriate for

- grammatical analysis;
- vocabulary analysis;
- sentence structure;
- learner intent;
- comprehension answers;
- identifying recurring constructions;
- searchable history;
- generating written corrections and exercises.

### Audio is required for

- pronunciation;
- intelligibility;
- hesitation;
- rhythm;
- stress;
- intonation;
- speaking speed;
- sound substitutions;
- distinguishing a learner error from a transcription error.

Speech recognition may infer the intended word from context. A correct transcript therefore does not prove correct pronunciation.

The system must follow this rule:

> Grammar and content may normally be analyzed from the transcript. Pronunciation and prosody must not be evaluated from the transcript alone.

---

## 1.4 Recommended Data Flow

```text
Learner microphone
        |
        v
Session bridge or voice interface
        |
        +--------------------> Live AI tutor
        |
        +--------------------> Local learner-audio recording
        |
        +--------------------> Timestamped transcription
        |
        +--------------------> Structured lesson event log
                                      |
                                      v
                              Final session package
                                      |
                                      v
                              Repository AI agent
                                      |
             +------------------------+----------------------+
             |                        |                      |
             v                        v                      v
        Session note             Error records        Anki candidates
```

The **session bridge** may initially be a manual process. It can later become a dedicated local application.

---

## 1.5 Session-Package Structure

Recommended repository structure:

```text
lingua-wiki/
├── private-media/
│   └── sessions/
│       └── pl-2026-08-28-001/
│           ├── learner-full.m4a
│           └── clips/
│               ├── utt-0017.wav
│               └── utt-0034.wav
│
└── sources/
    └── session-packs/
        └── pl-2026-08-28-001/
            ├── manifest.yaml
            ├── transcript.raw.jsonl
            ├── transcript.normalized.md
            ├── transcript.reviewed.md
            ├── events.jsonl
            └── summary.md
```

`private-media/` should normally be excluded from Git.

```gitignore
/private-media/
```

The repository may contain references to private media without committing the audio itself.

---

## 1.6 Session Manifest

Example:

```yaml
schema: lingua.session.v1

session_id: pl-2026-08-28-001
language: pl
session_type: speaking
mode: fluency

started_at: 2026-08-28T18:00:00+04:00
ended_at: 2026-08-28T18:42:17+04:00
duration_minutes: 42

targets:
  - past-tense
  - verb-aspect
  - sequential-events
  - sz-vs-soft-s

privacy: private
retention_policy: rolling-30-days

artifacts:
  transcript_raw: transcript.raw.jsonl
  transcript_normalized: transcript.normalized.md
  transcript_reviewed: transcript.reviewed.md
  events: events.jsonl
  summary: summary.md
  learner_audio: private-media://sessions/pl-2026-08-28-001/learner-full.m4a

transcription:
  provider: local-or-remote-provider
  model: model-name
  language_hint: pl
  confidence_available: true
  generated_at: 2026-08-28T18:43:02+04:00

selected_clips:
  - utterance_id: utt-0017
    path: private-media://sessions/pl-2026-08-28-001/clips/utt-0017.wav
    purpose: pronunciation-review

  - utterance_id: utt-0034
    path: private-media://sessions/pl-2026-08-28-001/clips/utt-0034.wav
    purpose: transcription-verification
```

---

## 1.7 Raw Transcript Format

The raw transcript should be machine-readable and immutable.

JSON Lines is recommended:

```json
{"id":"utt-0016","speaker":"assistant","start_ms":31120,"end_ms":34200,"text":"Co zrobiłeś najpierw?"}
{"id":"utt-0017","speaker":"learner","start_ms":34750,"end_ms":40510,"text":"Najpierw sprzątałem mieszkanie, a potem przyszli goście.","confidence":0.84,"audio_ref":"utt-0017.wav"}
```

Each learner utterance should have:

- stable utterance ID;
- speaker;
- start and end time;
- raw transcription;
- transcription confidence where available;
- audio reference where available.

---

## 1.8 Transcript Versions

The project should distinguish three transcript layers.

### Raw transcript

Direct speech-to-text output.

```text
transcript.raw.jsonl
```

Rules:

- immutable;
- never silently corrected;
- retains transcription confidence and timestamps.

### Normalized transcript

Human-readable formatting of the raw transcript.

```text
transcript.normalized.md
```

Allowed changes:

- punctuation;
- paragraphing;
- speaker formatting;
- obvious formatting artifacts.

Not allowed:

- silently correcting learner grammar;
- replacing learner vocabulary with better vocabulary;
- normalizing pronunciation-related forms without a note.

Example:

```md
**Tutor:** Co zrobiłeś najpierw?

**Learner:** Najpierw sprzątałem mieszkanie, a potem przyszli goście.
`utterance: utt-0017`
`audio: private-media://sessions/pl-2026-08-28-001/clips/utt-0017.wav`
```

### Reviewed transcript

Optional transcript created when speech recognition may be wrong.

```text
transcript.reviewed.md
```

Example:

```md
## utt-0017

### Raw speech-to-text

Najpierw sprzątałem mieszkanie, a potem przyszli goście.

### Reviewed hearing

Najpierw sprzątaliśmy mieszkanie, a potem przyszli goście.

### Pedagogical correction

Najpierw posprzątaliśmy mieszkanie, a potem przyszli goście.

### Notes

- The raw transcript may have omitted the plural ending.
- The grammatical learning target is perfective aspect in a sequence of completed events.
```

The three layers must remain distinguishable:

1. what speech recognition produced;
2. what the learner probably said;
3. what the correct Polish form should be.

---

## 1.9 Structured Event Log

A transcript does not fully describe the lesson. The system should also record instructional events.

Recommended event types:

```text
utterance
hint
correction
self-correction
exercise-start
exercise-answer
exercise-result
pronunciation-observation
listening-observation
card-candidate
topic-change
session-note
```

Example:

```json
{
  "event": "correction",
  "id": "event-0042",
  "target_utterance": "utt-0017",
  "category": "verb-aspect",
  "learner_form": "sprzątaliśmy",
  "suggested_form": "posprzątaliśmy",
  "reason": "A completed action occurred before another completed event.",
  "confidence": "high",
  "source": "live-tutor"
}
```

Another example:

```json
{
  "event": "pronunciation-observation",
  "id": "event-0043",
  "target_utterance": "utt-0034",
  "category": "sz-vs-soft-s",
  "status": "suspected",
  "requires_audio_review": true,
  "source": "live-tutor"
}
```

The word `suspected` is important. A live model should not convert uncertain pronunciation impressions into confirmed error records automatically.

---

## 1.10 Ingestion Workflow

The repository agent should process a completed session through a command such as:

```bash
python scripts/close_session.py pl-2026-08-28-001
python scripts/ingest_session.py pl-2026-08-28-001
```

The ingestion process should:

1. validate the manifest;
2. verify referenced files;
3. import the normalized transcript;
4. inspect structured events;
5. identify low-confidence utterances;
6. inspect selected audio clips when required;
7. create or update the session note;
8. create error candidates;
9. create pronunciation-review candidates;
10. create Anki candidates;
11. update the review queue;
12. update learner skill evidence;
13. mark the session package as ingested.

The agent should not automatically convert every correction into a permanent error pattern.

---

## 1.11 Implementation Stages

### Stage 1: Manual batch workflow

Use immediately.

```text
voice recording
    ->
manual or automatic transcription
    ->
save transcript and audio
    ->
create minimal manifest
    ->
run repository ingestion
```

This is sufficient for:

- monologues;
- reading aloud;
- retelling;
- pronunciation samples;
- delayed speaking analysis.

### Stage 2: Existing voice application plus export

Use a consumer voice interface for live tutoring.

After the session:

1. export or copy the transcript;
2. save it in `sources/inbox/`;
3. attach selected learner recordings;
4. create the session manifest;
5. run ingestion.

This is practical but depends partly on the export capabilities of the selected voice application.

### Stage 3: Custom session bridge

Recommended long-term target.

The bridge should:

- capture learner audio locally;
- send learner turns to the tutor;
- store tutor text before speech synthesis;
- create timestamped transcripts;
- generate structured events;
- finalize the session package automatically.

### Stage 4: Specialized pronunciation analysis

Optional later extension.

Possible functions:

- speech-rate measurements;
- pause analysis;
- phoneme-level comparison;
- reference-recording comparison;
- automatic difficult-utterance extraction;
- multiple-transcriber intelligibility comparison.

This should not block the initial implementation.

---

## 1.12 Privacy and Retention

Speaking recordings may contain:

- personal information;
- health information;
- details about family or work;
- biometric voice data.

Recommended defaults:

```yaml
privacy: private
retention_policy: rolling-30-days
```

Suggested rules:

- full-session audio remains local;
- full audio is not committed to normal Git history;
- raw audio is deleted after the configured retention period;
- selected pronunciation clips may be retained longer;
- transcripts may be retained after redacting irrelevant personal data;
- canonical language pages should not include unnecessary personal details;
- the learner may explicitly mark sessions as `keep` or `delete-after-ingestion`.

Possible values:

```yaml
retention_policy: keep
```

```yaml
retention_policy: rolling-30-days
```

```yaml
retention_policy: delete-after-ingestion
```

---

## 1.13 Proposed Decision

Adopt the following policy:

> LinguaWiki will use a provider-independent session package as the boundary between speaking interfaces and repository maintenance. Each speaking session should produce a timestamped transcript, structured event log, and references to locally stored learner audio. Grammar and content may be analyzed from transcripts. Pronunciation and prosody require audio evidence. Full audio remains private and outside normal Git history, while only selected clips are exposed for persistent review.

---

# 2. Provenance and Review of AI-Generated Pack Content

## 2.1 Definition of a Pack

A learning pack may contain:

- lesson objectives;
- preview vocabulary;
- grammar explanations;
- example sentences;
- reading passages;
- listening scripts;
- synthetic audio;
- comprehension questions;
- exercises;
- answer keys;
- role-play prompts;
- Anki candidates;
- pronunciation drills;
- cultural or factual notes.

A single pack may combine authentic content, transformed content, learner content, and generated content.

Therefore provenance should be recorded at the **item level**, not only at the pack level.

---

## 2.2 Core Recommendation

Every persistent pack item should declare:

1. where it came from;
2. how it was transformed;
3. which model or human created it;
4. what evidence supports it;
5. what review it has passed;
6. whether it may be stored or redistributed;
7. whether it contains private learner information.

Provenance and correctness must be separate concepts.

An item can be authentically sourced but unsuitable or incorrect for the current purpose. An AI-generated item can be linguistically correct even though it has no authentic source.

---

## 2.3 Provenance Classes

Use a controlled set of provenance types.

### `authentic-source`

Verbatim or near-verbatim material from an identified source.

Examples:

- textbook sentence;
- podcast transcript;
- film dialogue;
- dictionary example;
- official examination item.

Required fields:

```yaml
origin:
  type: authentic-source
  source_id: source-id
  locator: "page 42, exercise 3"
  edition: "edition or version"
```

### `learner-produced`

Content created by the learner.

Examples:

- spoken utterance;
- written answer;
- translation;
- self-correction.

Required fields:

```yaml
origin:
  type: learner-produced
  session_id: pl-2026-08-28-001
  utterance_id: utt-0017
  audio_ref: private-media://sessions/pl-2026-08-28-001/clips/utt-0017.wav
```

### `source-derived`

Content transformed from an authentic source without substantial rewriting.

Examples:

- cloze exercise;
- sentence reordering;
- comprehension question;
- tense transformation;
- glossary extraction.

Required fields:

```yaml
origin:
  type: source-derived
  source_ref: podcast-episode-012@02:14-02:31
  transformation: cloze-generation
```

### `ai-adapted`

Content materially rewritten by AI from an identified source.

Examples:

- simplified text;
- CEFR-graded adaptation;
- shortened dialogue;
- rewritten story.

Required fields:

```yaml
origin:
  type: ai-adapted
  source_ref: source-id
  target_level: A2
  transformation: simplify-and-shorten
  prompt_template: adapted-reading@2
```

### `ai-generated`

New content generated without a direct text source.

Examples:

- original role-play;
- generated story;
- example sentence;
- grammar drill;
- artificial dialogue.

Required fields:

```yaml
origin:
  type: ai-generated
  based_on:
    - languages/pl/grammar/aspect-in-sequences.md
  prompt_template: aspect-drill@3
```

### `synthetic-media`

Generated audio, image, or other media.

Examples:

- text-to-speech audio;
- generated illustration;
- artificial listening dialogue.

Required fields:

```yaml
origin:
  type: synthetic-media
  generator: provider-and-model
  source_text_hash: sha256:example
  voice: voice-id
```

### `human-authored`

Content directly written by a human.

Examples:

- tutor explanation;
- manually reviewed correction;
- human-created exercise.

Required fields:

```yaml
origin:
  type: human-authored
  author_role: tutor
  created_at: 2026-08-28
```

---

## 2.4 Provenance and Review Must Be Independent

Recommended review structure:

```yaml
review:
  linguistic: unreviewed
  pedagogical: unreviewed
  source_alignment: not-applicable
  rights: unknown
  privacy: private
```

### Linguistic review

Checks:

- grammar;
- vocabulary;
- idiomaticity;
- register;
- pronunciation representation;
- naturalness.

### Pedagogical review

Checks:

- target level;
- usefulness;
- clarity;
- cognitive load;
- answerability;
- relevance to learner goals;
- whether the item tests one identifiable skill.

### Source-alignment review

Used for source-derived or adapted content.

Checks:

- whether the answer follows from the source;
- whether the source locator is correct;
- whether wording changes introduced a contradiction;
- whether multiple answers should be accepted.

### Rights review

Checks:

- whether the item may be stored;
- whether it is personal-use only;
- whether it may be shared;
- whether redistribution is restricted.

### Privacy review

Checks:

- whether learner data is present;
- whether the item should remain private;
- whether it has been redacted sufficiently for sharing.

---

## 2.5 Review States

### Linguistic review states

```text
unreviewed
machine-checked
reference-verified
human-verified
```

### Pedagogical review states

```text
unreviewed
machine-checked
learner-approved
teacher-verified
```

### Source-alignment states

```text
not-applicable
unchecked
machine-checked
verified
```

### Rights states

```text
unknown
personal-use-only
cleared
restricted
```

### Privacy states

```text
private
redacted
shareable
```

A second AI model may count as an independent machine check, but not as human or authoritative verification.

---

## 2.6 Content Lifecycle

Recommended lifecycle:

```text
draft
candidate
approved-personal
verified
publication-ready
rejected
deprecated
needs-review
```

### `draft`

Generated but not checked.

### `candidate`

Passed basic automated validation and is ready for review.

### `approved-personal`

Suitable for use by the learner.

This does not imply that the content is appropriate for publication.

### `verified`

Passed the required linguistic and pedagogical checks.

### `publication-ready`

Passed:

- linguistic review;
- pedagogical review;
- source and rights review;
- privacy review.

### `needs-review`

A source, dependency, or policy changed after approval.

---

## 2.7 Risk-Based Review

The required review level should depend on:

```text
persistence
× instructional impact
× ambiguity
× distribution scope
```

### Tier 0: Ephemeral low-risk material

Examples:

- conversation question;
- temporary role-play;
- warm-up prompt;
- one-time generated drill.

Minimum review:

- schema validation;
- obvious linguistic sanity check.

May be used immediately.

Should not automatically become permanent repository or Anki content.

### Tier 1: Session-specific source-derived material

Examples:

- comprehension questions;
- cloze from a podcast;
- sentence reordering from a chapter.

Minimum review:

- exact source locator;
- source-alignment check;
- answerability check.

### Tier 2: Persistent personal content

Examples:

- Anki cards;
- reusable exercises;
- recurring-error drills;
- personal vocabulary entries.

Minimum review:

- linguistic machine check;
- duplicate check;
- learner approval;
- reference verification for uncertain or normative claims.

### Tier 3: Canonical knowledge content

Examples:

- grammar page;
- pronunciation rule;
- verb-government page;
- idiom or register explanation;
- standard aspect-pair explanation.

Minimum review:

- reliable reference or authentic evidence;
- independent linguistic review;
- verified examples;
- clear scope and exceptions.

### Tier 4: Shareable or published packs

Minimum review:

- full linguistic review;
- full pedagogical review;
- source and rights review;
- privacy review;
- documented reviewer or review method.

---

## 2.8 Policy by Content Type

### Grammar explanations

A canonical grammar explanation should not be based solely on AI generation.

Require:

- an authoritative grammar or trusted textbook;
- verified example sentences;
- indication of exceptions;
- review of scope and terminology.

AI-generated temporary explanations may be used in a lesson but should remain ephemeral until verified.

### Vocabulary and collocations

Simple common vocabulary may be checked against a reliable dictionary.

Require stronger verification for:

- idioms;
- collocations;
- register distinctions;
- false friends;
- case government;
- aspect pairs;
- preposition usage.

### Anki cards

Every persistent Anki card should pass:

- stable ID check;
- duplicate check;
- one clear learning target;
- unambiguous prompt;
- correct answer;
- appropriate Polish;
- known provenance;
- useful context;
- learner relevance review.

Cards based on personal mistakes should retain a link to:

- the learner utterance or answer;
- the correction;
- the relevant language page.

### Pronunciation content

Canonical pronunciation content should rely on:

- authentic native recordings;
- authoritative phonetic descriptions;
- verified pronunciation resources;
- human review for subtle distinctions where practical.

Synthetic audio may be used for drills but must be marked as synthetic.

A synthetic voice should not be treated as the sole authority for normative pronunciation.

### Minimal pairs

Before permanent use, verify:

- both words exist;
- the intended contrast is genuine;
- pronunciations are correct;
- the pair is pedagogically useful;
- the audio represents the intended distinction.

### Comprehension questions

For source-derived questions:

- the answer must be supported by the source;
- exact source location must be recorded;
- ambiguous answers must be accepted or the question rewritten;
- the same fact should not be contradicted elsewhere in the pack.

### Adapted reading material

Record:

- original source;
- target level;
- degree of rewriting;
- prompt-template version;
- model;
- copyright status;
- whether the material is restricted to personal use.

### Cultural and factual claims

Claims about:

- Polish history;
- law;
- institutions;
- examinations;
- customs;
- contemporary events;

should include an external source.

Clearly fictional generated stories do not require factual citations, provided they are labeled as fictional.

---

## 2.9 Source Hierarchy

Use the following hierarchy when validating Polish-language content:

1. official institutions and official examination materials;
2. authoritative dictionaries, grammars, and academic resources;
3. established textbooks and professional teaching materials;
4. authentic Polish corpora and original native media;
5. verified transcripts and native-speaker usage examples;
6. professional language-learning resources;
7. crowdsourced usage discussions;
8. AI-generated content.

Lower-ranked evidence may supplement but should not silently override stronger evidence.

---

## 2.10 AI Review Policy

AI review is useful for:

- grammar checking;
- answerability checking;
- duplicate detection;
- CEFR difficulty estimation;
- source-alignment comparison;
- contradiction detection;
- suspicious phrase detection.

However:

> AI review is quality control, not authoritative verification.

The same model reviewing its own output counts as:

```yaml
linguistic: machine-checked
```

A different model may count as an independent automated check, but not as:

```yaml
linguistic: human-verified
```

or:

```yaml
linguistic: reference-verified
```

---

## 2.11 Pack Sampling Policy

### New generation template

For the first three packs generated by a new prompt template:

- manually inspect all persistent items;
- record failure modes;
- revise the template where necessary;
- do not mark the template as stable until the sample is satisfactory.

### Stable generation template

After the template has demonstrated reliability:

- automatically validate every item;
- manually inspect at least 20% or five items, whichever is larger;
- manually inspect all high-risk items;
- inspect all Anki cards in small packs.

### Failure rule

If a sampled item contains a substantive error:

1. quarantine the whole pack;
2. review all persistent items;
3. mark the template as requiring revalidation;
4. correct dependent cards or knowledge pages.

---

## 2.12 Pack Manifest

Example:

```yaml
schema: lingua.pack.v1

id: pack-pl-2026-08-28-podcast-001
type: listening-pack
language: pl
target_level: A2
status: candidate
scope: personal

created_at: 2026-08-28T20:00:00+04:00

generation:
  provider: provider-name
  model: model-name
  run_id: run-84932
  prompt_template: listening-pack@3
  parameters:
    difficulty: A2
    max_new_items: 8

sources:
  - id: podcast-episode-012
    locator: "02:14-04:05"
    accessed_at: 2026-08-28
    source_hash: sha256:example

rights:
  classification: personal-use-only
  redistributable: false

review:
  linguistic: machine-checked
  pedagogical: learner-review-required
  source_alignment: machine-checked
  rights: personal-use-only
  privacy: private

items:
  - id: question-001
    kind: comprehension-question
    origin:
      type: source-derived
      source_ref: podcast-episode-012@02:14-02:48
      transformation: question-generation
    review:
      linguistic: machine-checked
      pedagogical: machine-checked
      source_alignment: verified

  - id: dialogue-001
    kind: speaking-prompt
    origin:
      type: ai-generated
      based_on:
        - languages/pl/grammar/aspect-in-sequences.md
    review:
      linguistic: machine-checked
      pedagogical: machine-checked
      source_alignment: not-applicable
```

---

## 2.13 Recommended Directory Structure

```text
packs/
├── drafts/
├── candidates/
├── approved-personal/
├── verified/
├── publication-ready/
├── rejected/
└── manifests/
```

Anki should retain its separate promotion flow:

```text
anki/
├── candidates/
├── approved/
├── exports/
└── rejected/
```

Only sufficiently verified material should be promoted into canonical language pages:

```text
languages/pl/
```

---

## 2.14 Dependency Tracking and Invalidation

Pack items should record dependencies where practical.

Example:

```yaml
depends_on:
  - ref: languages/pl/grammar/aspect-in-sequences.md
    hash: sha256:abc123

  - ref: session-pl-2026-08-28-001@utt-0017
    hash: sha256:def456
```

An item should be marked `needs-review` when:

- the source transcript changes;
- a grammar page is corrected;
- an answer key is revised;
- a generation template is found to be defective;
- a source edition changes;
- a referenced recording is replaced.

This prevents incorrect material from remaining indefinitely in Anki or reusable packs.

---

## 2.15 Learner-Error Provenance

Personal error content should preserve this chain:

```text
learner audio
    ->
raw transcript
    ->
reviewed hearing
    ->
pedagogical correction
    ->
error record
    ->
Anki candidate
```

The agent must distinguish:

- a confirmed learner error;
- a probable learner error;
- a possible transcription error;
- a pronunciation concern;
- a stylistic improvement.

For low-confidence speech recognition, use wording such as:

```text
The transcript suggests this form, but audio review is required.
```

Do not state the learner definitely made an error until the evidence is sufficient.

---

## 2.16 Proposed Decision

Adopt the following policy:

> Every persistent learning-pack item must declare whether it is authentic, learner-produced, source-derived, AI-adapted, AI-generated, synthetic, or human-authored. Provenance must be recorded separately from linguistic, pedagogical, source-alignment, rights, and privacy review. Low-risk ephemeral exercises may be used after automated checking. Persistent personal material requires learner approval and appropriate linguistic validation. Canonical or shareable content requires authoritative evidence or independent human review.

---

# 3. Proposed Architecture Decision Records

## ADR-001: Speaking Session Ingestion

### Decision

- Use a provider-independent session package.
- Capture learner audio locally where possible.
- Preserve raw, normalized, and optionally reviewed transcripts.
- Store corrections and instructional events separately from the transcript.
- Give the repository agent transcript access by default.
- Provide selected audio clips for pronunciation or transcription review.
- Keep full-session audio outside normal Git history.
- Apply explicit privacy and retention rules.

### Consequences

Positive:

- voice providers can be replaced;
- learner evidence remains auditable;
- pronunciation analysis is not confused with transcription;
- repository automation becomes predictable;
- privacy can be controlled locally.

Costs:

- additional session-pack generation;
- audio-storage management;
- transcript-version handling;
- ingestion tooling.

---

## ADR-002: Generated-Content Provenance and Review

### Decision

- Track provenance at item level.
- Separate provenance from verification.
- Use risk-based review requirements.
- Permit ephemeral low-risk generated content after automated checks.
- Require review before promotion to Anki or canonical knowledge.
- Require stronger evidence for pronunciation, idiomaticity, grammar, aspect, and government.
- Label synthetic media explicitly.
- Treat AI self-review only as machine checking.
- Track dependencies and invalidate stale content.

### Consequences

Positive:

- incorrect content is less likely to become permanent;
- cards and explanations remain auditable;
- source-derived and generated material remain distinguishable;
- personal content can be separated from shareable content;
- defective templates can be traced and corrected.

Costs:

- more metadata;
- review workflow overhead;
- need for validation and dependency scripts;
- slower promotion of content into canonical status.

---

# 4. Immediate Implementation Tasks

## Speaking ingestion

- [ ] Define `lingua.session.v1`.
- [ ] Create a JSON Schema or equivalent validator.
- [ ] Create `sources/session-packs/`.
- [ ] Create `private-media/sessions/`.
- [ ] Add `private-media/` to `.gitignore`.
- [ ] Implement `scripts/close_session.py`.
- [ ] Implement `scripts/ingest_session.py`.
- [ ] Support raw JSONL transcripts.
- [ ] Support normalized Markdown transcripts.
- [ ] Support structured event logs.
- [ ] Add selected audio-clip references.
- [ ] Add configurable retention policies.
- [ ] Create one example session package.

## Pack provenance

- [ ] Define `lingua.pack.v1`.
- [ ] Create controlled provenance values.
- [ ] Create review-state enums.
- [ ] Add pack lifecycle states.
- [ ] Implement pack-manifest validation.
- [ ] Add item-level provenance.
- [ ] Add source locators and hashes.
- [ ] Add rights and privacy classifications.
- [ ] Add dependency tracking.
- [ ] Implement `needs-review` invalidation.
- [ ] Create candidate and approved-pack directories.
- [ ] Integrate provenance fields into Anki candidate generation.
- [ ] Create one example listening pack.
- [ ] Create one example personal-error Anki candidate.

---

# 5. Acceptance Criteria

The design is implemented successfully when:

1. A recorded speaking session can be converted into a validated session package.
2. The repository agent can process the transcript without requiring access to a specific chat provider.
3. Pronunciation observations retain links to audio evidence.
4. Raw transcription is never overwritten by pedagogical correction.
5. Personal learner errors can be traced back to a session utterance.
6. Every persistent pack item has an explicit provenance type.
7. Provenance and review status are stored separately.
8. Anki candidates cannot be approved without provenance metadata.
9. Canonical grammar or pronunciation content cannot be promoted using only unreviewed AI output.
10. Synthetic audio is visibly labeled as synthetic.
11. Changed dependencies can mark existing pack items as `needs-review`.
12. Private learner audio is not committed to the normal Git repository.