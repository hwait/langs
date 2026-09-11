# What may be claimed about how something sounded

Four dimensions, because a learner can be perfectly intelligible and nothing like a
native speaker, and one number would hide that:

- `intelligibility` — could a listener understand it?
- `phonetic-accuracy` — were the sounds the right sounds?
- `prosody` — stress, rhythm, intonation. **Audio only, at every status.**
- `native-likeness` — how close to a native speaker. **Audio only, at every status.**

Three statuses, ordered by how much they assert: `observed`, `uncertain`, `confirmed`.
**`confirmed` requires audio** in every dimension, because confirming is saying "I heard
this", and without the sound it is saying "I read this".

Three bases: `direct` (observed live, in the session), `transcript` (text only), `audio`
(the recording is in the workspace and still there).

## When the audio is not there

Record what the evidence supports:

```bash
linguawiki transcript pronunciation --dimension intelligibility --status observed \
  --basis transcript --utterance utt_001 --note "Understandable in context." --format json
```

and tell the learner that this is a reading, not a hearing. Do not retry a refused
`confirmed` claim as `observed` *on a dimension that needs audio anyway* — prosody is
refused at every status, and the right move is to judge a dimension the transcript can
support, or to keep the recording next time.

## When the audio is purged

Claims that needed the sound are marked invalid, with the reason, and stay on the record
rather than disappearing: a learner who was told their vowel was wrong deserves to see
that the evidence for it is gone. Everything the *transcript* established is untouched.
