# pl-pilot changelog

The pack directory assigns a role by path, and this file has none: it is checksum-covered
like every other file and never loaded. It exists so a version bump is explicable to a
reader who only has the pack.

## 0.2.0 — presentation

Every task now carries a `presentation` record, so a client can render it without
parsing the prompt.

- Twelve `objective` tasks became real multiple choice. Three of them had their options
  smuggled inside the prompt (`"Which is correct? 'pięć bilety' / …"`); the other nine
  were open questions answered by typing, which is what an `objective` task is not. The
  prompt now asks the question and the choices carry the options.
- Answer keys were **not** widened to fit a button. Exactly one choice value is a string
  the existing key already accepts, checked with the comparison that scores it; widening
  a key to fit an option would be inventing an answer nobody reviewed.
- Choice order is `shuffled`: where the right answer sits is not information about it.
  The shuffle is resolved once, when the task is served, and a resumed task does not
  reshuffle.
- Every other task is explicitly `free-text`, with a response shape where one helps, so
  "no presentation" is a decision somebody made rather than a task nobody got to.

**The six listening tasks still have no recording.** The pack ships no audio, so they
keep the spoken sentence inside the prompt and declare no asset. This is the honest
state, not the intended one: what they measure is reading a sentence somebody transcribed
for the learner. Their `modality` stays `audio` deliberately — the receptive dimension
requires an audio modality to be testable at all, and relabelling them `text` would make
a dimension silently untestable rather than visibly incomplete.

Item content hashes moved for every edited task, and only for those: the presentation
field is omitted from the canonical payload while it is absent, so a pack that does not
use it keeps the hashes it shipped with.
