# The three layers, and why they never merge

- **raw** — what the transcription produced. Immutable. Correcting it in place would
  destroy the only evidence that it was ever wrong.
- **normalized** — punctuation, casing, filler removal. *The words are the same words.*
- **reviewed-hearing** — a person listened again and says this is what was said.

## Choosing between normalize and review

Ask one question: **did the words change?**

- No → `transcript normalize`. It is refused if the words did change, and that refusal is
  the point: a mishearing filed as a tidy-up ends up taught back to the learner as their
  own mistake.
- Yes, or you are not sure → `transcript review`. You do not have to decide: the revision
  derives its own kind from what it actually changed, recording a `hearing` when the words
  differ and a confirmation when they do not. Saying "I listened and the machine was
  right" is a useful thing to have on the record, and it is not a claim that it was wrong.

A second review of the same layer **supersedes** the first rather than replacing it. Both
readings stay, the earlier one naming the one that replaced it, because two people
listening and disagreeing is the measure of how far the transcription can be trusted.

The input is a payload, not a flag, because it is the learner's own words:

```json
{"text": "Dokąd pan jedzie?", "original": "dokad pan jedzie"}
```

`original` is only needed when the workspace keeps the words as an excerpt or a hash
rather than in full. The CLI verifies what you supply against the stored hash, so a
normalization is still checkable in a workspace that refused to keep transcripts.

## Disagreement is data

`transcript show` reports `disagreement` between layers and a `best_layer`. Do not hide
it. How far apart the layers are *is* the measure of how much the transcription can be
trusted, and a learner is entitled to that before believing a correction derived from it.

## Interpretation is a separate act

`transcript interpret` says what an utterance was:

- `learner-error` — their mistake. Requires a `corrected_form` *and* a `--category`: the
  occurrence is filed against a recurring error pattern, and a correction the next
  occurrence of the same mistake cannot find is a note on one line. Refused outright when
  the transcriber reported low confidence in the words.
- `transcription-artifact` — the machine misheard. Recorded, and counted against nobody.
- `uncertain` — nobody is sure. Also counted against nobody.

When the transcription's own confidence was low, or the layers disagree about the word in
question, prefer `uncertain` over `learner-error`. Losing a real mistake costs one
correction; inventing one costs the learner a form they had right.
