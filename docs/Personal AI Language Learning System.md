# Personal AI Language Learning System

## Comprehensive Implementation Plan

## 1. Project Summary

Build a personal AI-assisted language learning system for studying Polish, with an architecture that can later support other languages.

The system must combine:

- a linked Markdown knowledge base;
- a structured curriculum;
- adaptive AI-led lessons;
- voice conversations;
- listening and pronunciation practice;
- reading of adapted books;
- films, series, YouTube, and podcasts;
- personal error tracking;
- spaced repetition through Anki;
- periodic assessment;
- progress reporting.

The project should follow the general knowledge-base pattern proposed by Andrej Karpathy:

1. preserve original source materials separately;
2. maintain a structured, agent-curated Markdown wiki;
3. describe the repository architecture and operating rules in `AGENTS.md`;
4. continuously integrate new knowledge into existing pages rather than accumulating disconnected notes;
5. periodically lint, consolidate, and reorganize the knowledge base.

The Markdown repository is the long-term memory and source of truth for the learning system. Anki is a derived spaced-repetition layer. Voice mode is the main channel for speaking and pronunciation practice.

---

# 2. Learner Context

The initial learner profile is:

- Target language: Polish.
- Native language: Russian.
- Secondary support language: English may be used when useful.
- Current learning materials: the `Hurra!!! Po polsku` course.
- Current progress: approximately 15 lessons completed with a tutor.
- Desired change: pause regular tutoring and continue primarily with AI until approximately B1.
- Possible future tutoring: resume at B2, possibly twice per week.
- Target workload: five structured sessions per week.
- Session duration: between 40 and 120 minutes.
- The learner states the available time at the beginning of every session.
- Preferred session structure: approximately 20-minute focused blocks.
- Existing spaced-repetition tool: Anki.
- Desired content:
  - grammar;
  - vocabulary;
  - reading;
  - writing;
  - listening;
  - speaking;
  - pronunciation;
  - films and series;
  - Polish podcasts and YouTube;
  - adapted Polish books;
  - real-life Polish usage.
- The learner lives in Poland and can apply the language in real situations.

Do not assume that completing 15 tutoring lessons proves a specific CEFR level. The system must create a multidimensional skill profile instead of assigning one global level prematurely.

---

# 3. Primary Goals

## 3.1 Short-term goal

Create a functioning minimum viable learning system that can:

1. audit the material covered in the first 15 Hurra lessons;
2. estimate the learner’s current ability by skill;
3. construct sessions dynamically from the available time;
4. conduct lessons through text and voice;
5. track new knowledge and recurring errors;
6. generate reviewed Anki cards;
7. use adapted books, podcasts, and short video fragments;
8. produce weekly progress reports.

## 3.2 Medium-term goal

Bring the learner to a stable B1 level without regular tutoring.

A stable B1 level should mean that the learner can:

- understand the main point of clear everyday Polish;
- manage most routine situations in Poland;
- describe experiences, plans, and events;
- participate in practical conversations;
- write simple connected texts;
- read adapted books and increasingly accessible native content;
- understand appropriately selected podcasts and video;
- use core Polish grammar with manageable error rates;
- recognize and correct recurring personal errors.

## 3.3 Long-term goal

Generalize the architecture so that a new language can be added without redesigning the system.

Language-specific rules should live in:

```text
languages/<language-code>/
```

Generic session orchestration, content tracking, Anki export, and assessment logic should remain language-independent.

---

# 4. Core Design Principles

## 4.1 The repository is not merely a notebook

The project must operate as a learning system with four connected layers:

1. **Knowledge base**
   What is known about the target language.

2. **Learner model**
   What this learner understands, produces, forgets, or repeatedly gets wrong.

3. **Session engine**
   How a lesson is assembled for the time available today.

4. **Assessment loop**
   How the system verifies that knowledge transfers into reading, listening, speech, and writing.

## 4.2 Separate language knowledge from learner state

The knowledge base answers:

> How does Polish work?

The learner model answers:

> What can this learner currently do, and what requires more practice?

Do not mix these responsibilities in a single large file.

## 4.3 Recognition is not mastery

Track at least these states:

```text
unseen
encountered
recognized
understood
controlled-production
spontaneous-production
stable
```

A learner may recognize a grammatical form in a workbook but still fail to use it in conversation. The system must distinguish these cases.

## 4.4 Use AI as the main instructor, not as an unquestioned authority

AI may:

- explain;
- generate exercises;
- conduct conversations;
- select review material;
- adapt texts;
- check answers;
- organize the repository.

Potentially uncertain questions about idiomaticity, government, pronunciation, or normative grammar should be verified against reliable Polish sources or multiple native examples.

## 4.5 Prefer contextual language units

Do not build the vocabulary system around isolated translations alone.

Prefer:

- phrases;
- collocations;
- sentence patterns;
- verb-aspect pairs;
- preposition-plus-case combinations;
- useful conversational chunks;
- examples from actual learning materials;
- the learner’s own corrected sentences.

## 4.6 Avoid uncontrolled content accumulation

The system must not automatically create a permanent note or Anki card for every unfamiliar word.

Every candidate should pass a usefulness filter:

- Is it frequent or relevant?
- Did it block comprehension?
- Did the learner make an error with it?
- Is it likely to be used actively?
- Has it appeared more than once?
- Does it represent an important pattern?
- Is an equivalent card already present?

---

# 5. Recommended Technology Stack

## 5.1 Required

- Git repository.
- Markdown files.
- Obsidian or another Markdown wiki interface.
- AI coding agent capable of reading and updating the repository.
- Chat and voice interface for interactive lessons.
- Anki Desktop.
- Python 3 for repository automation.

## 5.2 Optional later

- AnkiConnect for automated Anki synchronization.
- Speech-to-text and text-to-speech APIs.
- Local transcription tools.
- Media clipping tools.
- Whisper-compatible transcription.
- A small local web interface.
- Automated subtitle alignment.
- External dictionaries and corpus APIs.

## 5.3 Do not introduce initially

Do not begin with:

- a vector database;
- a separate application server;
- a large custom UI;
- complex analytics;
- a custom spaced-repetition scheduler;
- automated mass-downloading of copyrighted content;
- automatic creation of hundreds of cards.

The first version should remain auditable and easy to modify manually.

---

# 6. Repository Structure

```text
language-learning/
├── README.md
├── AGENTS.md
├── index.md
├── CHANGELOG.md
│
├── config/
│   ├── project.yaml
│   ├── learner.yaml
│   ├── session-rules.yaml
│   └── anki.yaml
│
├── learner/
│   ├── profile.md
│   ├── goals.md
│   ├── preferences.md
│   ├── current-level.md
│   ├── skill-map.md
│   ├── pronunciation-profile.md
│   └── learning-history.md
│
├── state/
│   ├── current.md
│   ├── weekly-plan.md
│   ├── review-queue.md
│   ├── open-loops.md
│   ├── due-assessments.md
│   └── session-counter.md
│
├── languages/
│   └── pl/
│       ├── index.md
│       ├── grammar/
│       ├── vocabulary/
│       ├── pronunciation/
│       ├── pragmatics/
│       ├── writing/
│       ├── speaking/
│       ├── culture/
│       └── reference/
│
├── curriculum/
│   └── hurra/
│       ├── index.md
│       ├── course-map.md
│       ├── completed.md
│       ├── current-unit.md
│       ├── grammar-map.md
│       ├── vocabulary-map.md
│       ├── audit-first-15-lessons.md
│       └── lesson-notes/
│
├── assessments/
│   ├── baseline/
│   ├── weekly/
│   ├── monthly/
│   ├── milestone/
│   └── official-samples/
│
├── errors/
│   ├── index.md
│   ├── active/
│   ├── monitoring/
│   └── resolved/
│
├── sources/
│   ├── inbox/
│   ├── catalog.md
│   ├── books/
│   ├── podcasts/
│   ├── youtube/
│   ├── films/
│   ├── series/
│   ├── dictionaries/
│   └── official/
│
├── library/
│   ├── queue.md
│   ├── reading-log.md
│   ├── books/
│   └── chapters/
│
├── media/
│   ├── queue.md
│   ├── viewing-log.md
│   ├── listening-log.md
│   ├── podcasts/
│   ├── series/
│   ├── films/
│   ├── youtube/
│   └── clips/
│
├── sessions/
│   └── YYYY/
│       └── YYYY-MM/
│
├── exercises/
│   ├── generated/
│   ├── completed/
│   ├── reusable/
│   └── answer-keys/
│
├── anki/
│   ├── README.md
│   ├── schema.md
│   ├── templates/
│   ├── candidates/
│   ├── approved/
│   ├── exports/
│   ├── media/
│   └── rejected/
│
├── reports/
│   ├── weekly/
│   ├── monthly/
│   └── milestones/
│
└── scripts/
    ├── lint_repo.py
    ├── build_session.py
    ├── close_session.py
    ├── build_review_queue.py
    ├── export_anki.py
    ├── import_anki_stats.py
    ├── create_weekly_report.py
    ├── validate_frontmatter.py
    └── find_orphans.py
```

---

# 7. Repository Navigation

The root `index.md` should contain links to:

- current learner state;
- today’s lesson;
- current Hurra unit;
- active book;
- active podcast or series;
- active error patterns;
- review queue;
- latest weekly report;
- pending Anki candidates;
- current CEFR skill map.

Example:

```md
# Language Learning Dashboard

## Current state

- [[learner/current-level]]
- [[learner/skill-map]]
- [[state/current]]
- [[state/weekly-plan]]
- [[state/review-queue]]

## Curriculum

- [[curriculum/hurra/current-unit]]
- [[curriculum/hurra/completed]]

## Active content

- Book: [[library/books/current-book]]
- Podcast: [[media/podcasts/current-podcast]]
- Series: [[media/series/current-series]]

## Active problems

- [[errors/index]]

## Latest reports

- [[reports/weekly/latest]]
```

---

# 8. `AGENTS.md` Requirements

The root `AGENTS.md` must explain how an AI agent operates the system.

It should include the following rules.

## 8.1 Before every lesson

The agent must read:

1. `learner/profile.md`;
2. `learner/current-level.md`;
3. `learner/skill-map.md`;
4. `state/current.md`;
5. `state/weekly-plan.md`;
6. `state/review-queue.md`;
7. the last two or three session notes;
8. active errors;
9. current Hurra unit;
10. active book and media progress.

## 8.2 At the start of every lesson

Ask:

> How much time do you have today?

Accept values from approximately 40 to 120 minutes.

Then propose a session composed of 20-minute blocks.

The learner may request a specific mode, for example:

- grammar;
- reading;
- listening;
- speaking;
- pronunciation;
- review;
- writing;
- film;
- podcast;
- Hurra;
- free conversation.

If no mode is requested, select blocks adaptively.

## 8.3 During the lesson

The agent must:

- keep the lesson focused;
- avoid long lectures unless requested;
- elicit answers before explaining;
- use graduated hints;
- distinguish fluency mode from accuracy mode;
- record candidate errors;
- record possible Anki items;
- monitor fatigue and performance;
- adapt difficulty if the material is too easy or too difficult.

## 8.4 At the end of every lesson

The agent must:

1. conduct a short final retrieval test;
2. summarize what was practiced;
3. identify no more than three main weaknesses;
4. update the session file;
5. update learner state;
6. update active errors;
7. update the review queue;
8. update curriculum or media progress;
9. generate Anki candidates;
10. set follow-up items.

## 8.5 Hint ladder

When the learner asks for help:

1. identify the problematic part;
2. give a small hint;
3. ask a guiding question;
4. provide a simpler Polish explanation;
5. give a partial answer;
6. provide the complete answer only when needed.

Do not force this ladder in emergency communication or when the learner explicitly asks for a direct explanation.

---

# 9. Session Engine

## 9.1 Time-to-block mapping

| Available time | Default structure |
|---:|---|
| 40 minutes | 2 blocks |
| 60 minutes | 3 blocks |
| 80 minutes | 4 blocks |
| 100 minutes | 5 blocks |
| 120 minutes | 6 blocks |

A block should last approximately 20 minutes. It may end slightly earlier or later if necessary.

## 9.2 Block types

Supported blocks:

- Anki review;
- active recall;
- grammar explanation;
- controlled grammar practice;
- vocabulary activation;
- reading;
- listening;
- podcast;
- film or series;
- speaking;
- pronunciation;
- shadowing;
- dictation;
- writing;
- translation;
- cloze;
- error correction;
- role-play;
- free conversation;
- teach-back;
- exam simulation;
- progress review.

## 9.3 Default five-day weekly distribution

At the minimum workload of five 40-minute sessions, provide approximately ten blocks per week.

Recommended minimum:

| Skill | Minimum blocks per week |
|---|---:|
| Review and retrieval | 2 |
| Listening | 2 |
| Speaking | 2 |
| Reading | 1 |
| Writing | 1 |
| Grammar | 1 |
| Pronunciation | 1 |

Vocabulary should be integrated across all blocks.

## 9.4 Example weekly structure

### Day 1: Hurra and speaking

- Block 1: Hurra grammar or vocabulary.
- Block 2: controlled speaking using the new material.

### Day 2: Adapted reading

- Block 1: book reading and comprehension.
- Block 2: retelling and language extraction.

### Day 3: Podcast and pronunciation

- Block 1: listening.
- Block 2: transcript analysis, shadowing, or phonetics.

### Day 4: Hurra and writing

- Block 1: new or reviewed course material.
- Block 2: short written production and revision.

### Day 5: Native media and weekly control

- Block 1: film, series, or YouTube fragment.
- Block 2: mixed assessment and weekly review.

For longer sessions, add:

- Anki;
- old error review;
- additional speaking;
- extensive reading;
- pronunciation;
- writing revision.

---

# 10. Learning Modes

The agent must support direct natural-language commands and optional named modes.

## 10.1 `learn`

Select the most useful next activity based on:

- current curriculum;
- skill imbalance;
- active errors;
- due reviews;
- recent performance.

## 10.2 `review`

Review:

- due Anki cards;
- recurring errors;
- recently learned vocabulary;
- weak grammar patterns;
- material from earlier sessions.

## 10.3 `grammar`

Use this progression:

1. observe examples;
2. infer or explain the rule;
3. recognize correct forms;
4. complete controlled exercises;
5. produce original sentences;
6. use the structure in conversation or writing.

## 10.4 `reading`

Support:

- pre-reading vocabulary;
- adapted reading;
- intensive reading;
- extensive reading;
- comprehension questions;
- retelling;
- grammar extraction;
- cloze generation.

## 10.5 `listening`

Support:

- AI-generated audio;
- native podcasts;
- YouTube;
- films and series;
- official examination audio;
- dictation;
- listening with and without transcript.

## 10.6 `speaking`

Support:

- role-play;
- guided conversation;
- free conversation;
- story retelling;
- description;
- opinion;
- problem solving;
- practical scenarios in Poland.

## 10.7 `pronunciation`

Support:

- sound discrimination;
- minimal pairs;
- articulation explanation;
- imitation;
- recording and comparison;
- shadowing;
- sentence rhythm;
- difficult consonant clusters.

## 10.8 `writing`

Support:

- messages;
- forms;
- practical email;
- descriptions;
- diary entries;
- narratives;
- opinions;
- short exam-style responses.

The learner should rewrite corrected texts rather than only reading the corrections.

## 10.9 `translation`

Use Polish-to-Russian, Russian-to-Polish, and optionally Polish-to-English.

Focus on:

- alternative formulations;
- aspect;
- case;
- word order;
- idiomatic differences;
- false friends.

## 10.10 `teach-back`

The learner explains a rule in their own words. The agent challenges incomplete explanations with examples and counterexamples.

## 10.11 `exam`

Provide tasks without help until the task is completed.

---

# 11. Correction Modes

## 11.1 Fluency mode

Use during free speaking.

Rules:

- do not interrupt;
- respond naturally first;
- after the learner finishes, identify at most three important errors;
- prioritize errors that affect meaning or are recurring;
- ask the learner to repeat the corrected form.

## 11.2 Accuracy mode

Use during focused grammar or pronunciation work.

Rules:

- correct the target pattern immediately;
- allow the learner to self-correct first;
- separate the target error from minor unrelated errors.

## 11.3 Exam mode

Rules:

- no hints during the task;
- record errors silently;
- give feedback after completion;
- provide scoring criteria where possible.

## 11.4 Writing correction

Use three layers:

1. communicative effectiveness;
2. grammar and vocabulary;
3. naturalness and style.

Show:

- original;
- minimal correction;
- more natural alternative;
- short explanation;
- required rewrite task.

---

# 12. Initial Assessment

## 12.1 Purpose

Do not merely produce one label such as “A1” or “A2.”

Create a profile such as:

```text
Reading: A2
Listening: A1+
Speaking: A1/A2
Writing: A1+
Grammar recognition: A2
Grammar production: A1+
Passive vocabulary: A2
Active vocabulary: A1+
Pronunciation: baseline required
```

## 12.2 Audit the first 15 Hurra lessons

Create:

```text
curriculum/hurra/audit-first-15-lessons.md
```

For every covered topic, record:

- lesson or unit;
- textbook pages if known;
- grammar introduced;
- vocabulary theme;
- recognition result;
- controlled production result;
- spontaneous production result;
- common errors;
- confidence;
- recommended action.

Example table:

| Topic | Encountered | Recognizes | Controlled use | Spontaneous use | Status |
|---|---:|---:|---:|---:|---|
| Present tense | Yes | Strong | Medium | Weak | Review |
| Past tense | Yes | Medium | Medium | Weak | Active |
| Verb aspect | Yes | Medium | Weak | Weak | Priority |
| Accusative | Yes | Strong | Medium | Medium | Monitor |

## 12.3 Baseline assessment components

Conduct across one long session or several shorter sessions:

1. short spoken introduction;
2. description of a normal day;
3. description of a past event;
4. selected Hurra exercises;
5. short adapted reading;
6. reading comprehension;
7. short listening;
8. oral retelling;
9. short writing task;
10. pronunciation sample;
11. practical role-play.

## 12.4 Assessment frequency

- Weekly: short mixed retrieval test.
- Monthly: broader skill review.
- Every 8–12 weeks: milestone assessment.
- Before claiming B1: use multiple B1-style tasks across all skills.

---

# 13. Hurra Curriculum Integration

## 13.1 Role of Hurra

Hurra is the curriculum spine.

It provides:

- systematic sequence;
- grammar progression;
- thematic vocabulary;
- controlled exercises;
- continuity from A1 toward B1.

The AI system must not create an unrelated parallel grammar curriculum unless there is a clear gap.

## 13.2 Lesson note template

```md
---
type: hurra-lesson
lesson_number:
unit:
pages:
date:
status:
---

# Hurra Lesson

## Material covered

- Grammar:
- Vocabulary:
- Functions:
- Exercises:

## What I could do

-

## Difficulties

-

## Tutor or AI corrections

-

## Personal errors

- [[...]]

## Follow-up practice

-

## Anki candidates

-

## Related knowledge pages

- [[...]]
```

## 13.3 AI use with Hurra

For each unit, the AI should:

1. review prerequisite material;
2. explain new material;
3. ask the learner to infer patterns;
4. guide textbook exercises;
5. create additional personalized exercises;
6. use the new forms in speaking;
7. connect them to books or media;
8. create Anki candidates;
9. schedule delayed review.

---

# 14. Adapted Reading System

## 14.1 Reading objectives

Develop:

- reading fluency;
- grammar recognition;
- contextual vocabulary;
- narrative comprehension;
- retelling;
- confidence with longer texts.

## 14.2 Two reading modes

### Intensive reading

Use a short passage and analyze it carefully.

Activities:

- pre-reading vocabulary;
- contextual explanation;
- grammar analysis;
- comprehension questions;
- cloze;
- translation;
- oral retelling;
- Anki extraction.

### Extensive reading

Read primarily for the story.

Rules:

- do not stop for every unfamiliar word;
- look up only words that block comprehension or recur;
- avoid creating cards for rare descriptive vocabulary;
- provide a short summary after reading;
- track pages or chapters completed.

## 14.3 Pre-reading procedure

Before a new section, provide:

- a spoiler-free orientation;
- 8–12 essential words or phrases;
- two or three relevant structures;
- one guiding comprehension question.

## 14.4 Help hierarchy during reading

When asked about a sentence:

1. explain it in simpler Polish;
2. provide a Polish synonym or paraphrase;
3. explain the relevant context;
4. give a Russian translation only if needed.

The learner can explicitly request a direct Russian explanation.

## 14.5 Post-reading procedure

After each passage:

- ask 4–6 questions in Polish;
- check event order;
- ask for a summary;
- discuss a character or decision;
- select a small number of useful expressions;
- generate one or two transfer exercises.

## 14.6 Book card template

```md
---
type: adapted-reader
title:
author:
level:
status:
source:
has_audio:
current_position:
started:
last_read:
---

# Book Title

## Progress

- Current chapter:
- Current page:
- Completion:

## Comprehension

- Estimated unaided comprehension:
- With support:
- Retelling quality:

## Active vocabulary

- [[...]]

## Grammar encountered

- [[...]]

## Difficulties

- [[...]]

## Chapter notes

- [[...]]

## Session history

- [[...]]
```

## 14.7 Initial reading recommendation

Begin with an adapted reader near the learner’s current level. A series such as `Czytaj krok po kroku` may be suitable, especially where audio and exercises are available.

The baseline audit should determine the correct starting volume rather than automatically beginning from the first page.

---

# 15. Podcasts and Listening

## 15.1 Listening sources

Use a mix of:

- AI-generated dialogues;
- beginner podcasts;
- graded podcasts;
- native podcasts with transcripts;
- YouTube;
- official examination recordings;
- films and series;
- audio from adapted books.

## 15.2 Do not rely exclusively on AI-generated speech

AI audio is useful for:

- controlled vocabulary;
- predictable grammar;
- adjustable speed;
- targeted drills.

Native speech is necessary for:

- voice variation;
- natural rhythm;
- reductions;
- real conversational speed;
- different accents and speaking styles.

## 15.3 Podcast workflow

### First pass

Listen without transcript.

The learner should identify:

- topic;
- speakers;
- setting;
- key words;
- main point.

### Second pass

Listen again with targeted questions.

### Transcript pass

Open the transcript only after an honest listening attempt.

Identify:

- words that were known but not recognized;
- connected speech;
- missed endings;
- new useful expressions;
- difficult pronunciation.

### Production

The learner should:

- retell the segment;
- answer questions;
- give an opinion;
- reuse three to five expressions.

### Delayed retrieval

On the following day, ask short questions without replaying the audio first.

## 15.4 Podcast note template

```md
---
type: podcast
title:
episode:
level:
url:
status:
duration:
current_position:
transcript_available:
---

# Podcast Episode

## First-listen comprehension

- Main idea:
- Key words:
- Estimated comprehension:

## Difficult sections

- Timestamp:
- Reason:

## Useful language

- [[...]]

## Pronunciation targets

- [[...]]

## Questions

1.
2.
3.

## Retelling

-

## Anki candidates

-
```

---

# 16. Films, Series, and YouTube

## 16.1 Two viewing modes

### Intensive viewing

Use fragments of approximately 2–8 minutes.

### Extensive viewing

Watch 20–45 minutes primarily for enjoyment and broad comprehension.

Do not analyze every line in extensive mode.

## 16.2 Intensive viewing protocol

1. Watch without subtitles.
2. Summarize the scene.
3. Identify participants, setting, and event.
4. Watch with Polish subtitles.
5. Compare what was heard with the text.
6. Select 5–8 useful expressions at most.
7. Select one grammatical pattern.
8. Select one or two lines for shadowing.
9. Retell the scene.
10. Generate a limited set of Anki candidates.

## 16.3 Subtitle policy

Preferred order:

1. Polish audio without subtitles;
2. Polish audio with Polish subtitles;
3. Russian subtitles only when required to recover meaning.

Be aware that dubbed audio and subtitles may differ because they are separate adaptations.

## 16.4 Media note template

```md
---
type: series
title:
season:
episode:
platform:
url:
level:
status:
current_timestamp:
---

# Title

## Current position

- Season:
- Episode:
- Timestamp:

## Useful clips

- `00:00–00:00` — description

## First-viewing comprehension

- Main idea:
- Estimated comprehension:

## With Polish subtitles

- Estimated comprehension:
- Previously missed forms:

## Useful language

- [[...]]

## Pronunciation targets

- [[...]]

## Retelling

-

## Anki candidates

-
```

## 16.5 Difficulty progression

Possible progression:

1. learner-oriented video;
2. familiar dubbed animation;
3. short everyday scenes;
4. family sitcom;
5. native entertainment with clear context;
6. more culturally dense or stylistically difficult series.

Do not begin with highly idiomatic historical comedy or fast crime drama.

---

# 17. Speaking System

## 17.1 Core principle

Voice conversation must be a normal part of the course, not an optional add-on.

The AI should serve as:

- conversation partner;
- role-play partner;
- interviewer;
- examiner;
- storytelling coach;
- pronunciation practice partner.

## 17.2 Practical scenarios

Frequently simulate:

- shopping;
- restaurant;
- doctor;
- pharmacy;
- public office;
- transport;
- landlord;
- neighbor;
- workplace;
- telephone call;
- appointment;
- complaint;
- explaining a problem;
- asking for clarification.

## 17.3 Conversation contract

Example:

```text
Speak to me only in Polish at approximately A2 level.

This is fluency mode.

Do not interrupt while I am answering.

After each substantial answer:

1. respond naturally to the content;
2. identify no more than three important errors;
3. give corrected versions;
4. ask me to repeat the corrected sentence.

Track recurring problems involving:

- case;
- verb aspect;
- past-tense endings;
- word order;
- unclear pronunciation.
```

## 17.4 Speaking evidence

Do not mark a form as mastered because it was answered correctly in a grammar exercise.

Require evidence from:

- controlled sentence;
- role-play;
- retelling;
- spontaneous conversation;
- delayed reuse in a later session.

---

# 18. Pronunciation System

## 18.1 Objective

The primary goal before B1 is:

- intelligibility;
- reliable sound contrasts;
- acceptable rhythm;
- confidence;
- reduced interference from Russian.

Native-like pronunciation is not required.

## 18.2 Polish-specific targets

Track at least:

- `sz` versus `ś`;
- `cz` versus `ć`;
- `ż/rz` versus `ź`;
- `dż` versus `dź`;
- `ł` versus `l`;
- `s` versus `sz`;
- `c` versus `cz`;
- nasal vowels `ą` and `ę`;
- final devoicing;
- consonant clusters;
- word stress;
- sentence rhythm;
- intonation.

## 18.3 Pronunciation block sequence

1. perception;
2. articulation explanation;
3. native model;
4. repetition;
5. recording;
6. comparison;
7. use in a sentence;
8. use in free speech;
9. delayed review.

## 18.4 Minimal-pair policy

Use verified real words wherever possible.

Do not allow the AI to generate questionable or nonexistent minimal pairs without validation.

## 18.5 Shadowing

Use fragments of approximately 3–8 seconds.

Sequence:

1. listen;
2. read transcript;
3. repeat after speaker;
4. repeat with speaker;
5. record independently;
6. compare rhythm and difficult sounds;
7. use the phrase in a new context.

## 18.6 Limitation

Speech recognition is optimized for understanding intended words. It may transcribe a word correctly even when pronunciation is imperfect.

Therefore distinguish:

```text
intelligibility
phonetic accuracy
naturalness
native-likeness
```

For progress to B1, prioritize the first two.

## 18.7 Pronunciation profile

```md
# Pronunciation Profile

## Strong contrasts

-

## Active problems

- `sz / ś`
-

## Difficult clusters

-

## Frequent misunderstood words

-

## Intelligibility observations

-

## Current exercises

-

## Review schedule

-
```

---

# 19. Error Model

## 19.1 Error categories

Use categories such as:

- grammar;
- case;
- verb aspect;
- conjugation;
- tense;
- agreement;
- preposition;
- vocabulary;
- collocation;
- word order;
- pronunciation;
- listening recognition;
- spelling;
- pragmatics;
- style.

## 19.2 Error lifecycle

```text
observed
active
monitoring
resolved
reactivated
```

## 19.3 Error note template

```md
---
type: error-pattern
id:
status: active
category:
first_seen:
last_seen:
occurrences:
successful_uses:
next_review:
related_level:
---

# Error Pattern

## Description

Explain the recurring problem.

## Learner examples

- Incorrect:
- Incorrect:

## Correct forms

-

## Rule

-

## Contrast

-

## Controlled evidence

-

## Spontaneous evidence

-

## Related pages

- [[...]]

## Anki cards

-

## Resolution criteria

The error is resolved when:
- used correctly in controlled practice;
- used correctly in a new sentence;
- used correctly in spontaneous speech or writing;
- used correctly after a delay.
```

## 19.4 Resolution policy

Do not resolve an error after one correct answer.

Require repeated success in different contexts.

---

# 20. Knowledge Page Design

## 20.1 Do not create one giant `vocab.md`

Use thematic pages for ordinary vocabulary and individual pages for important patterns.

Examples:

```text
languages/pl/vocabulary/shopping.md
languages/pl/vocabulary/health.md
languages/pl/vocabulary/travel.md
languages/pl/vocabulary/sprzatac-posprzatac.md
languages/pl/grammar/aspect-in-sequences.md
languages/pl/grammar/najpierw-a-potem.md
```

## 20.2 Create individual pages for

- important verb-aspect pairs;
- verbs with multiple government patterns;
- prepositions;
- particles;
- false friends;
- recurring learner errors;
- idiomatic expressions;
- high-value conversational frames;
- difficult pronunciation items.

## 20.3 Knowledge page template

```md
---
type:
id:
level:
status:
first_seen:
last_used:
sources:
skills:
---

# Title

## Meaning or function

-

## Form

-

## Usage

-

## Contrast

-

## Examples

-

## Common learner errors

-

## Personal examples

-

## Related pages

- [[...]]

## Sources

-

## Anki

- Card IDs:
```

---

# 21. Anki Integration

## 21.1 System role

Markdown is the source of truth.

Anki is a training projection optimized for spaced retrieval.

Do not duplicate Anki’s scheduling algorithm in Markdown.

## 21.2 Card timing

Use both preview and post-session cards.

### Before a lesson

Create at most 5–10 preview cards for:

- essential vocabulary;
- critical expressions;
- names or concepts required for comprehension.

Mark them:

```text
status::preview
```

### After a lesson

Create the primary card package based on:

- actual difficulty;
- personal errors;
- missed listening;
- useful expressions;
- repeated vocabulary;
- grammar that failed in production.

## 21.3 Card limits

Recommended:

| Session type | Maximum new notes |
|---|---:|
| Short session | 5–8 |
| Normal session | 8–15 |
| Rich book or media session | 15–20 |

These are ceilings, not targets.

## 21.4 Card selection filter

Create a card only if at least one applies:

- personally useful;
- frequent;
- recurring;
- caused an error;
- blocked understanding;
- important grammar pattern;
- likely active use;
- useful listening contrast;
- meaningful pronunciation target.

## 21.5 Recommended note types

### `Lingua`

Fields:

```text
ID
Prompt
Answer
Polish
Context
GlossRU
Explanation
Audio
Image
Source
SourceRef
WikiLink
Lesson
FirstSeen
CardKind
Tags
```

### `Lingua Cloze`

Use the normal Anki cloze mechanism with additional metadata fields.

## 21.6 Card types

Support:

- contextual recognition;
- active production;
- cloze;
- grammatical contrast;
- error correction;
- audio-to-text;
- audio-to-meaning;
- pronunciation;
- scene or image prompt;
- sentence transformation;
- personal error card.

## 21.7 Card quality principles

Prefer:

```text
Nie mogę dziś przyjść. Muszę ______ wizytę.
```

over:

```text
odwołać = cancel
```

Prefer one answerable unit per card.

Avoid:

- multiple unrelated blanks;
- ambiguous prompts;
- long explanations on the front;
- cards requiring recall of an entire paragraph;
- isolated rare words;
- unverified AI-generated Polish.

## 21.8 Deck and tag strategy

Use one primary deck:

```text
Polish
```

Use tags for classification:

```text
level::A1
level::A2
source::hurra::lesson_15
source::book::<book>::chapter_3
source::podcast::<podcast>::episode_12
source::series::<series>::s01e01
skill::listening
skill::speaking
grammar::aspect
topic::shopping
status::preview
status::active
error::personal
session::2026-07-20
```

## 21.9 Candidate approval workflow

```text
Lesson
  ↓
Anki candidates
  ↓
Deduplication and quality check
  ↓
Approved card data
  ↓
TSV export or AnkiConnect
  ↓
Anki
```

Do not import raw AI output without review.

## 21.10 Candidate file

```md
# Anki Candidates — YYYY-MM-DD

- [ ] `pl-...` — cloze — reason
- [ ] `pl-...` — personal error — reason
- [ ] `pl-...` — listening — reason
```

## 21.11 Stable IDs

Every Anki note must have a stable ID, for example:

```text
pl-aspect-posprzatac-001
```

Stable IDs allow later updates without duplicate notes.

## 21.12 Initial import method

Start with UTF-8 TSV export.

Example:

```tsv
ID	Prompt	Answer	Context	Source	Tags
pl-aspect-posprzatac-001	Najpierw {{c1::posprzątali}} mieszkanie...	posprzątać	Completed action before another event	Hurra 15	grammar::aspect session::2026-07-20
```

## 21.13 Later automation

Add AnkiConnect only after the note schema is stable.

Potential automation:

- check duplicates;
- create notes;
- update fields;
- attach tags;
- upload audio;
- retrieve review statistics;
- identify repeated failures and leeches.

## 21.14 Anki feedback loop

Periodically record:

- cards repeatedly marked `Again`;
- leeches;
- ambiguous cards;
- cards that are too easy;
- pronunciation cards requiring review;
- topics producing repeated failures.

Feed these findings back into:

- `errors/`;
- `state/review-queue.md`;
- session planning.

---

# 22. Session File Template

```md
---
type: session
date:
duration_minutes:
language: pl
primary_mode:
blocks:
hurra_unit:
book:
media:
status: completed
---

# Session — YYYY-MM-DD

## Session plan

1.
2.
3.

## Block 1

### Objective

-

### Activities

-

### Performance

-

## Block 2

### Objective

-

### Activities

-

### Performance

-

## New language

-

## Personal errors

- [[...]]

## Pronunciation observations

-

## Listening observations

-

## Successful spontaneous usage

-

## Anki candidates

-

## Final retrieval test

1.
2.
3.

## Results

-

## Follow-up

-

## State updates

- Current curriculum position:
- Book position:
- Media position:
- Next review:
```

---

# 23. Review Queue

`state/review-queue.md` should contain learning items that require review outside Anki or require richer practice than a flashcard can provide.

Example:

```md
# Review Queue

## Due now

- [[aspect-in-sequential-actions]]
  - Task: retell yesterday using `najpierw`, `potem`, and perfective verbs.
  - Reason: failed in spontaneous speech.
  - Last reviewed: 2026-07-20.

## Due this week

- [[sz-vs-soft-s]]
  - Task: discrimination and shadowing.

## Monitoring

- [[genitive-after-negation]]
```

Do not reproduce Anki’s exact card intervals here.

---

# 24. Weekly Reports

## 24.1 Report contents

Generate a weekly report containing:

- total structured study time;
- number of sessions;
- blocks by skill;
- Hurra progress;
- reading progress;
- listening and viewing time;
- speaking time;
- writing tasks;
- Anki notes added;
- Anki review problems;
- newly observed errors;
- resolved errors;
- pronunciation targets;
- strongest improvement;
- main bottleneck;
- plan for next week.

## 24.2 Example

```md
# Weekly Report — YYYY-Www

## Activity

- Sessions:
- Total minutes:
- Anki reviews:
- Book pages:
- Podcast minutes:
- Native video minutes:

## Skill balance

| Skill | Blocks | Assessment |
|---|---:|---|
| Listening | 2 | improving |
| Speaking | 2 | unstable |
| Reading | 1 | strong |
| Writing | 1 | needs review |

## Progress

-

## Recurring errors

- [[...]]

## Resolved or improving

- [[...]]

## Next-week priorities

1.
2.
3.
```

---

# 25. Quality Assurance and Linting

Run a repository lint weekly.

Check for:

- orphaned pages;
- broken links;
- duplicate vocabulary entries;
- contradictory explanations;
- duplicate Anki IDs;
- invalid frontmatter;
- missing source references;
- unresolved candidate cards;
- stale active errors;
- old `current` files;
- session files without closure;
- pages too large and needing splitting;
- fragmented pages that should be merged.

## 25.1 Consolidation policy

If several notes describe the same concept, merge them into one canonical page.

Preserve:

- personal examples;
- source links;
- error history;
- backlinks;
- Anki IDs.

---

# 26. Copyright and Source Handling

Do not store complete copyrighted books, subtitle files, transcripts, films, or textbook pages unless legally permitted.

Store:

- title;
- source link;
- page number;
- timestamp;
- short quotation where appropriate;
- personal notes;
- derived exercises;
- vocabulary references;
- learner answers.

Maintain original materials outside the wiki when needed, under a clear source directory or external link.

---

# 27. Implementation Phases

## Phase 1: Repository bootstrap

Create:

- directory structure;
- `README.md`;
- `AGENTS.md`;
- root `index.md`;
- templates;
- learner profile;
- configuration files;
- script placeholders.

### Acceptance criteria

- all root navigation links work;
- all templates exist;
- agent operating rules are explicit;
- no learning content is generated yet beyond initial placeholders.

## Phase 2: Learner baseline

Create and execute:

- audit of the first 15 Hurra lessons;
- short spoken baseline;
- reading baseline;
- listening baseline;
- writing baseline;
- pronunciation baseline;
- initial skill map.

### Acceptance criteria

- `learner/current-level.md` includes separate estimates by skill;
- at least three priority weaknesses are identified;
- no unsupported global CEFR claim is made.

## Phase 3: Hurra integration

Create:

- current unit file;
- completed lessons map;
- lesson note template;
- grammar and vocabulary maps;
- next four weeks of curriculum guidance.

### Acceptance criteria

- every future Hurra session can update progress;
- new grammar is linked to canonical knowledge pages;
- learned material is connected to practice tasks.

## Phase 4: Session engine

Implement:

- duration-based lesson building;
- block selection;
- weekly skill balancing;
- session note creation;
- session closure.

### Acceptance criteria

Input:

```text
Today I have 60 minutes.
```

Output:

- three proposed blocks;
- clear objectives;
- connection to current state;
- end-of-session updates.

## Phase 5: Reading and media

Add:

- one adapted reader;
- one beginner podcast;
- one native or semi-native video source;
- reading log;
- media log;
- source templates.

### Acceptance criteria

The system can conduct:

- one intensive reading session;
- one podcast session;
- one film or series fragment session;
- one extensive activity without over-annotation.

## Phase 6: Voice and pronunciation

Create:

- speaking contracts;
- fluency and accuracy modes;
- pronunciation baseline;
- shadowing workflow;
- Polish sound target pages.

### Acceptance criteria

The system can conduct a 20-minute voice block and record:

- errors;
- successful usage;
- pronunciation observations;
- follow-up tasks.

## Phase 7: Anki MVP

Create:

- note schema;
- candidate format;
- approval process;
- TSV exporter;
- stable ID rules;
- sample cards.

### Acceptance criteria

- approved cards export to valid UTF-8 TSV;
- import does not create duplicates when stable IDs match;
- cards include source and session tags;
- audio field format is supported.

## Phase 8: Reporting and maintenance

Implement:

- weekly report generation;
- repository linting;
- orphan detection;
- duplicate detection;
- stale-state checks.

### Acceptance criteria

A weekly maintenance command produces:

- report;
- lint result;
- next-week priorities;
- unresolved issues.

## Phase 9: Anki automation

Optional after stable manual use.

Implement:

- AnkiConnect synchronization;
- media upload;
- note updates;
- review statistics import;
- repeated-failure detection.

---

# 28. First Four Weeks of Operation

## Week 1: Baseline and reconstruction

- Audit Hurra lessons 1–15.
- Conduct speaking, listening, reading, writing, and pronunciation samples.
- Build the initial error list.
- Select an adapted reader.
- Select a beginner podcast.
- Configure Anki note types.

## Week 2: Stable routine

- Begin the five-session weekly structure.
- Add limited Anki cards after each session.
- Start one adapted reading track.
- Start one podcast track.
- Conduct two voice blocks.

## Week 3: Native input

- Add a short series, film, or YouTube fragment.
- Begin shadowing.
- Add audio cards.
- Compare recognition with and without Polish subtitles.
- Review whether the selected materials are at the correct difficulty.

## Week 4: First monthly review

- Run a mixed assessment.
- Compare baseline and current performance.
- Audit Anki burden.
- Remove or rewrite bad cards.
- Adjust weekly skill allocation.
- Decide the next Hurra unit and media progression.

---

# 29. Definition of Done for the MVP

The MVP is complete when all of the following are true:

1. The repository structure exists.
2. `AGENTS.md` fully describes session behavior.
3. The first 15 Hurra lessons have been audited.
4. The learner has a multidimensional baseline.
5. A 40–120 minute session can be assembled automatically.
6. Sessions are divided into approximately 20-minute blocks.
7. Reading, listening, speaking, pronunciation, grammar, and writing are supported.
8. One adapted book is active.
9. One podcast is active.
10. One video or series source is active.
11. Personal errors are stored as structured notes.
12. Anki candidates are generated after sessions.
13. Approved cards export as TSV with stable IDs.
14. A weekly report can be generated.
15. Repository linting works.
16. The project can be operated by an agent with no prior conversation context.

---

# 30. Immediate Agent Task List

The implementing agent should perform these tasks in order:

1. Create the full repository structure.
2. Write `README.md` using this specification.
3. Write `AGENTS.md`.
4. Create all Markdown templates.
5. Create the initial learner profile from the information in this plan.
6. Create the initial Hurra audit framework.
7. Create the baseline assessment protocol.
8. Create the session-building rules.
9. Create the first weekly-plan template.
10. Create the error note schema.
11. Create the media and book schemas.
12. Create the Anki schema.
13. Implement TSV Anki export.
14. Implement frontmatter validation.
15. Implement broken-link and orphan detection.
16. Implement session creation and closure scripts.
17. Implement weekly report generation.
18. Populate `state/current.md` with the initial project status.
19. Create a checklist for information still needed from the learner:
    - exact Hurra book and level;
    - current unit and pages;
    - lesson duration and frequency used previously;
    - available textbook materials;
    - current Anki deck structure;
    - preferred podcast and film genres;
    - preferred reading genres;
    - available voice and transcription tools.
20. Prepare the first assessment session.

---

# 31. First Session Protocol

When the repository is ready, the first live session should begin as follows:

```text
1. Ask how much time is available.
2. Explain that the first meeting is a baseline and Hurra audit.
3. Conduct a short conversation in Polish.
4. Ask for a short description of a recent day.
5. Test selected grammar from the first 15 Hurra lessons.
6. Use a short reading passage.
7. Use a short listening passage.
8. Ask for an oral retelling.
9. Collect a short written sample.
10. Record a pronunciation sample.
11. Summarize strengths and weaknesses.
12. Generate only a small initial Anki candidate set.
13. Update the learner skill map.
14. Schedule the next session.
```

The first session should not attempt to finish the complete audit if the available time is short. Continue it across subsequent sessions while still including useful language practice.

---

# 32. Final Operating Model

The complete learning loop is:

```text
Hurra / book / podcast / video / real-life experience
                         ↓
                 AI-led learning session
                         ↓
       knowledge, performance, and personal errors
                         ↓
              Markdown knowledge repository
                         ↓
          reviewed candidates for spaced repetition
                         ↓
                         Anki
                         ↓
               delayed retrieval and review
                         ↓
      transfer into speech, writing, reading, and listening
                         ↓
                  updated learner model
                         ↓
                 next adaptive session
```

The system should remain:

- transparent;
- editable;
- version-controlled;
- evidence-based;
- learner-specific;
- multimodal;
- resistant to content overload;
- usable without a tutor through B1;
- extensible to other languages later.