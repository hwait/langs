# The session contract

Two different things are called a "session package" in this system, and confusing them is
the fastest way to record something wrong:

- **`lingua.session.events.v1`** is what *you* flush with `session log`. It is one bounded
  batch of observations from a session this workspace is running, and it says directly
  what the learner did.
Text fields must carry something: a value of only whitespace is refused where it arrives
rather than at close, because every row it would be written into requires more than that.

- **`lingua.session.v1`** is a whole session produced *somewhere else* — a tutor call, a
  voice app — handed over as a file with its transcript layers and artifacts. It is
  ingested with `session ingest-package`, and speaking work belongs to
  `linguawiki-speak`.

This reference is about the first one. `schemas/lingua.session.events.v1.json` is the
generated schema and is authoritative; what follows is what each field means.

## The batch

| Field | Meaning |
|---|---|
| `sequence` | Your flush number for this session, from 1, no gaps. A gap makes `close` refuse. |
| `idempotency_key` | Same key + same events = stored once. Same key + different events = refused. |
| `content_hash` | Optional. Your own sha256 of the canonical events; a mismatch refuses the batch. |
| `block` | The block this flush covers. Individual events may name their own. |
| `events` | Ordered by `occurred_at`, unique `event_id`s, at least one. |

An `event_id` identifies the *observation*, and it is stored as the observation's source
identity. Two consequences: re-sending one under a new idempotency key is refused rather
than staged twice, and a genuinely new observation needs a genuinely new ID.

Every event carries its own `occurred_at`, and it is stored *beside* the flush time
rather than replacing it. That is the timestamp the learner model is built from, so an
event you are sending late must say when it actually happened.

Some fields on a payload are the workspace's rather than yours: `response_visibility` and
`response_hash` are written by the retention rule when the batch is stored, and `source`,
`package_id`, `utterance_id`, and `transcript_layer` say the observation came out of an
ingested recording. A flush that sets them has them stripped.

A batch is stored durably and **changes nothing about the learner**. That is the whole
design: a session can be interrupted at any point and the next invocation can see exactly
what it holds.

## `attempt.observed`

One thing the learner did, and how it went.

| Field | Notes |
|---|---|
| `task_type`, `modality` | Required. What was demanded, and in what channel. |
| `score` | `0.0`–`1.0`. What happened, not what you hoped. |
| `target` | A knowledge item, by stable key or content ID. |
| `dimension` | A skill dimension. One of `target`/`dimension` is required; both is fine. |
| `claims` | What the attempt proves. Omit it and the weakest claim the task supports is used. |
| `help_level` | `none`, `prompted`, `hinted`, `scaffolded`, `full-answer`. Be truthful: help discounts the evidence, which is correct. |
| `retrieval` | `immediate`, `same-session`, `delayed`. A `delayed` claim is refused unless the gap is real. |
| `response` | The learner's own words. The retention rule runs when the batch is stored: without consent the text is reduced to a hash before anything is written, and asking for `full` without consent is refused at flush time. |
| `assessor_kind`, `confidence` | `ai` when you scored it; `low` confidence on a productive judgement is honest. |

The claim caps what the observation can ever promote, and the task type caps the claim.
Naming `spontaneous-production` on an objective item is refused rather than accepted.

## `correction.given`

One correction, filed against the error pattern it belongs to.

| Field | Notes |
|---|---|
| `category`, `signature`, `description` | Required. The signature is the normalized pattern; identity is derived from it. |
| `learner_form`, `corrected_form` | What was said and what it should have been. |
| `classification` | `learner-error` by default. A mishearing or a transcription artifact is **not** a learner error and is counted against nobody. |
| `attach_to` / `distinct` | A signature close to an existing pattern without matching it is refused until you say which. |

Two corrections of the same pattern in one flush are two *occurrences* of one error, and
that is what the close records: the pattern's count goes to two, and each staged event
names its own occurrence.

## `pronunciation.assessment`

| Field | Notes |
|---|---|
| `status` | `observed`, `uncertain`, or `confirmed`. `confirmed` requires `audio_artifact_id`. |
| `note` | What you heard. |

In this release a pronunciation event is materialized as a **note**, never as
pronunciation evidence: confirming how something sounded needs the utterance-and-audio
model that `linguawiki-speak` owns. Pronunciation *evidence* comes from
`attempt.observed` in a pronunciation block.

## `observation.noted`

`fatigue`, `confidence`, `strategy`, `notable-success`, or `note`, with a note and a
salience. An observation is context for planning and can never promote an item: it names
no claim and carries no strength.

## `follow_up`

`kind` and `action`, optionally a target, an error, a priority, and a due window. This
becomes a row in the follow-up queue the next plan reads.
