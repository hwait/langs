---
name: linguawiki-speak
description: Bring a spoken conversation into LinguaWiki - build or adapt a lingua.session.v1 package, review it with the learner, ingest it idempotently, keep the transcript layers apart, and record only the pronunciation claims the audio can support. Use after a live or recorded conversation, when importing a voice application's export, when reviewing a transcript, and when deciding what audio to keep or purge.
---

# LinguaWiki speaking

Every command is a `linguawiki` CLI call with `--format json`. The division of labour is
the same as everywhere else: **you review, the CLI decides.** Whether a claim about
pronunciation can stand, whether a revision is a tidy-up or a mishearing, whether this
package was already ingested — all of that is Python.

```bash
linguawiki speaking package  --workspace <path> --external-session-id <id> \
  --language <tag> --started-at <iso8601> [--minutes <n>] [--utterances <n>] \
  [--out package.json] --format json
linguawiki speaking validate --workspace <path> --input package.json \
  [--adapter lingua|whisper-verbose-json] [--external-session-id <id>] \
  [--language <tag>] [--started-at <iso8601>] --format json
linguawiki speaking ingest   --workspace <path> --input package.json \
  [--adapter ...] [--session <id>] [--producer <name>] --format json
linguawiki transcript show        --workspace <path> [--session <id>] [--ingestion <id>] \
  [--utterance <id>] --format json
linguawiki transcript normalize   --workspace <path> --utterance <id> --input text.json --format json
linguawiki transcript review      --workspace <path> --utterance <id> --input text.json \
  [--confidence low|medium|high] --format json
linguawiki transcript interpret   --workspace <path> --utterance <id> \
  --classification learner-error|transcription-artifact|uncertain [--input detail.json] --format json
linguawiki transcript pronunciation --workspace <path> --dimension <d> --status <s> \
  --basis direct|transcript|audio [--utterance <id>] [--audio <artifact-id>] --format json
linguawiki artifact register --workspace <path> --path <relative> --kind audio --format json
linguawiki artifact verify   --workspace <path> --format json
linguawiki artifact purge    --workspace <path> --artifact <id> --reason <why> [--dry-run] --format json
linguawiki privacy audit     --workspace <path> --format json
```

## The one rule that shapes everything

**A correct transcript proves nothing about how something sounded.** The words of a
question and the words of a flat statement are the same words. So:

- a `confirmed` pronunciation claim needs audio that is still in the workspace;
- prosody and native-likeness need it at *every* confidence, because text cannot carry
  them at all;
- everything else — what the learner said, which forms they produced, what they
  understood — is established by the transcript and survives the audio being deleted.

The CLI enforces this. If it refuses a claim, do not retry it at a lower status hoping it
passes: record what the evidence actually supports and say so to the learner.

## The loop

1. **Get a package.** Either a voice application exported one, or you build one:
   `speaking package --out package.json` writes a valid skeleton with timed stubs.
   Replace the stub text and times with what was actually said. Nothing is guessed for
   you, because a guessed timestamp produces a *valid* package that is wrong.
2. **Review it with the learner before it is ingested.** `speaking validate` reports the
   layers, the counts, whether this content was already ingested, and what would be kept
   of their words. Read the `problems` array out loud if it is non-empty — every entry is
   something that will refuse at ingest.
3. **Ingest.** `speaking ingest` stages the events *and* stores the transcript. It is
   idempotent on content: a checkpoint export and the completed export of the same call
   share their utterances and the second one stages nothing. Say that plainly rather than
   reporting "0 events" as a failure.
4. **Work the transcript.** `transcript show` gives the layers and where they disagree.
   Normalize what needs tidying, review what you or the learner heard differently, and
   classify what was a mistake versus what the machine misheard.
5. **Close the session.** Nothing an ingest staged is credited until `session close`.
   That is the boundary for speaking exactly as it is for everything else — see
   `linguawiki-learn`.

## What never happens here

- Never upload a recording to any external service without the learner saying yes to that
  specific upload, in that moment. Consent to keeping audio locally is not consent to
  sending it somewhere.
- Never paste transcript text into a wiki page or a commit message. `privacy audit` will
  find it, but by then it is in the learner's history.
- Never correct a learner for a word the transcript may have misheard. Classify it as a
  `transcription-artifact` or `uncertain` and move on; a correction for something they
  said right teaches them away from a form they had.

## References

- `references/package-contract.md` — what a `lingua.session.v1` package holds and how to
  fill one in by hand.
- `references/transcript-layers.md` — raw, normalized, reviewed-hearing: which claim each
  layer makes and when to add one.
- `references/pronunciation-claims.md` — which claims the audio supports, and what to
  record when it does not.
- `references/audio-and-privacy.md` — registering, verifying, and purging recordings, and
  what a purge costs.
- `references/manual-fallback.md` — running speaking with no voice tooling at all.
