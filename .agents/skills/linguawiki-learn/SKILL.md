---
name: linguawiki-learn
description: Run a LinguaWiki lesson end to end - plan the blocks, teach them, batch what you observe, and close the session so it counts. Use for a structured lesson or a grammar, reading, listening, writing, or mixed-mode session, for continuing where the last session stopped, and for recovering a session that was interrupted. Never records evidence outside the session boundary.
---

# LinguaWiki lessons

Every command is a `linguawiki` CLI call with `--format json`. The division of labour is
strict and it is the reason this works: **you teach, the CLI decides.** Block selection,
what an observation proves, whether a stage moved, whether an error is fixed — all of
that is Python. Your job is to run the lesson honestly and report what came back.

```bash
linguawiki plan create    --workspace <path> --minutes <n> [--mode <mode>] \
  [--energy low|normal|high] [--intent "<what the learner asked for>"] \
  [--correction-mode fluency|accuracy|exam] [--idempotency-key <key>] --format json
linguawiki plan show      --workspace <path> [--session <id>] --format json
linguawiki session start  --workspace <path> [--session <id>] --format json
linguawiki session log    --workspace <path> --input batch.json --format json
linguawiki session staged --workspace <path> [--session <id>] --format json
linguawiki session status --workspace <path> [--session <id>] --format json
linguawiki session close  --workspace <path> --outcome completed \
  [--actual-minutes <n>] [--fatigue low|medium|high] [--summary "<text>"] \
  [--idempotency-key <key>] --format json
linguawiki session partial-close --workspace <path> [--discard-block <block-id>] --format json
linguawiki session abandon --workspace <path> [--reason "<why>"] --format json
linguawiki session resume  --workspace <path> --format json
linguawiki session recover --workspace <path> --from <session-id> [--into <session-id>] \
  [--event <staged-event-id>] --format json
linguawiki wiki build     --workspace <path> --view dashboard --format json
```

## The loop

1. **Context first.** `linguawiki context session --format json` gives the learner's
   profile, due work, active errors, and estimates, bounded and consent-aware. Read that,
   not the generated Markdown.
2. **Plan.** `plan create --minutes <n>`. Every block comes back with a `rationale` and
   every high-priority candidate that missed out comes back with a `reason`. Tell the
   learner what the session holds and why, in a sentence, before starting.
3. **Start.** `session start`. Until you do, `session log` is refused: a flush that
   belongs to no started session is an observation nobody can place.
4. **Teach one block at a time.** Follow the block's `objective`, `activities`, and
   `targets`. See `references/correction-modes.md` for how to correct, and the other
   references for each modality's procedure.
5. **Flush at the block boundary.** One `session log` per block, carrying that block's
   observations. Nothing about the learner changes yet — that is the point.
6. **Close explicitly.** `session close --outcome completed`, or `partial-close` for a
   session cut short. This is the only command that turns observations into evidence,
   errors, follow-ups, and stage changes, and it does so once.
7. **Show the result.** Report what the close returned: stages that moved, errors
   touched, dimensions recomputed. Then `wiki build --view dashboard` if the learner
   wants the page refreshed.

## Flushing a batch

`session log` takes a `lingua.session.events.v1` payload through `--input`, never through
flags: the learner's own words belong in a file rather than in shell history.

```json
{
  "sequence": 1,
  "idempotency_key": "<session>-block-1",
  "block": "blk_...",
  "events": [
    {
      "event_id": "evt_...",
      "kind": "attempt.observed",
      "occurred_at": "2026-09-10T09:00:00Z",
      "payload": {
        "task_type": "short-response",
        "modality": "writing",
        "dimension": "writing",
        "target": "<stable key or content id>",
        "score": 1.0,
        "help_level": "none",
        "claims": ["controlled-production"],
        "response": "<the learner's answer>",
        "assessor_kind": "ai",
        "confidence": "medium"
      }
    }
  ]
}
```

Rules that the CLI enforces and you should not fight:

- **`occurred_at` is when it happened**, not when you are sending it. A block worked at
  18:30 and flushed at 19:10 is one observation with two timestamps, and the learner
  model is built from the first: the delay class, whether a repeat is a repeat, when an
  error was last seen. Send the real time.
- **`sequence` counts your flushes**, from 1, with no gaps. A gap means a flush was lost:
  `close --outcome completed` refuses rather than crediting a session that is missing a
  block, and `partial-close` is the remedy -- it credits what arrived and says what did
  not.
- **`idempotency_key` makes a retry safe.** The same key with the same events is stored
  once; the same key with *different* events is refused, because the second call would
  otherwise discard the first call's observations. New observations need a new key.
- **`event_id` identifies the observation, not the delivery.** Re-sending an event under
  a new key is refused (`duplicate_session_event`): one observation is staged once. Retry
  a lost flush with the *same* key and the same events; give genuinely new observations
  new event IDs.
- **One batch per block** by default. Flush earlier before handing off to
  `linguawiki-speak` or if the buffer gets large; never flush once per answer.
- **`score` is what happened**, from `0.0` to `1.0`. Do not round a struggling answer up.
- **`assessor_kind: "ai"`** is the truth when you scored it. `confidence: "low"` on a
  productive task you judged is the honest default.
- **Include `response` only within retention consent.** The rule is applied when the
  batch is stored, not later: without consent the text is reduced to a hash before
  anything is written, and asking for `response_visibility: "full"` on a track that has
  refused is refused *at flush time* (`transcript_consent_required`), with the session
  still runnable. Fix the payload and flush under a new key.
- **Do not set `source`, `package_id`, `utterance_id`, or `transcript_layer`.** They say
  an observation came out of an ingested recording, they are set by
  `session ingest-package`, and a flush that carries them has them stripped.
- **An `activity` must belong to the `block`** the event names. Naming one from another
  block is refused (`activity_not_in_block`), because those statuses are what `resume`
  reads to say what comes next.

Event kinds: `attempt.observed`, `correction.given`, `pronunciation.assessment`,
`observation.noted`, `follow_up`. See `references/session-contract.md` for each payload.

## What you must not do

- **Never write evidence directly.** `evidence record` refuses `--origin session`, and
  that refusal is deliberate: an attempt from a live session belongs to the staged event
  that produced it and the close that credited it. There is no supported way to record a
  lesson observation outside a session.
- **Never claim more than you saw.** The claim you name caps what the observation can
  promote, and the CLI caps it again by what the task type can support. Recognition never
  promotes production, however often it succeeds.
- **Never confirm pronunciation from text.** A correct transcript proves nothing about how
  something sounded. A `pronunciation.assessment` without linked audio is recorded as a
  note and confirms nothing; audio-backed pronunciation *evidence* comes from attempts in
  a pronunciation block, and speaking sessions belong to `linguawiki-speak`.
- **Never edit `wiki/`.** It is generated. `wiki build` overwrites it.
- **Never leave a session open.** Close it, partial-close it, or abandon it explicitly.

## Reading a plan

- `planned_minutes` below `requested_minutes` is a real answer, not a bug: low reported
  energy, or too little available material, makes a shorter session the honest one. The
  `warnings` say which.
- `omissions` are the high-priority candidates that lost. "the track records no voice
  channel" and "nothing was left for it to work on" are both worth relaying.
- An explicit `--mode` is honoured unless it is impossible, and then the refusal names
  the reason (`session_mode_unavailable`). Relay it; do not silently run another mode.
- A block marked `repeated: true` runs a second time on targets its first block did not
  cover. Say so — the learner will notice the repetition either way.

## When a session was interrupted

`session resume` finds the session and says what state it is in. See
`references/session-recovery.md` for the full decision table. In short:

- **active with staged batches** — carry on from `resume_from`, which names the next
  unfinished block and the activity inside it.
- **closing with no result** — a close was interrupted. Retry `close`; nothing was
  credited.
- **already closed** — the close happened and the answer was lost. `close` again returns
  the original result marked `replayed: true`. It does not do the work twice.
- **abandoned with staged work** — the observations are still on record. `session recover
  --from <id>` moves them into a new session after you have reviewed them with the
  learner.

Read `references/session-contract.md` for the event payloads and the batch rules,
`references/correction-modes.md` for fluency, accuracy, and exam behaviour,
`references/grammar-and-vocabulary.md`, `references/reading-and-media.md`, and
`references/writing-and-translation.md` for the block procedures, and
`references/session-recovery.md` for interruptions.
