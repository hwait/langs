# Recordings: registering, verifying, purging

Audio never enters Git and never enters the database. It lives under `artifacts/` or
`imports/` in the workspace, both of which are ignored, and the database holds a row
naming it, its checksum, and whether the learner consented to keeping it.

```bash
linguawiki artifact register --path artifacts/audio/rozmowa-1.m4a --kind audio \
  --origin learner-recording --format json
linguawiki artifact verify --format json
linguawiki artifact purge --artifact art_... --reason learner-request --dry-run --format json
```

## Register even what you do not keep

`--not-retained` records that a file existed and was **not** kept. That row is what makes
a later claim explicable rather than merely unsupported: "there was a recording, the
learner chose not to keep it" is a different fact from "there was never any audio".

## Verify tells three different things apart

- *present* — the file is there with the bytes it had;
- *missing* — gone without a tombstone. Somebody moved or deleted it outside the
  workspace, and evidence resting on it is now unsupported with nothing recording why;
- *altered* — still there, different bytes. Worse than missing: the claims still point at
  it as though nothing happened.

## Always dry-run a purge first

`--dry-run` reports exactly what would be invalidated and what would survive, without
doing it. Show the learner both numbers before they decide. Deleting a recording is a
privacy choice they are entitled to make; making it without knowing the cost is not.

The cost is deliberately narrow: acoustic claims that needed the sound stop standing, and
nothing else does. What the learner *said* was established by the transcript.

## Uploading

Never send a recording to any external service without the learner agreeing to that
specific upload, in that moment. Consent to keeping audio locally is not consent to
sending it anywhere, and `external_service_consent` on the track is a standing preference
rather than permission for a particular file.
