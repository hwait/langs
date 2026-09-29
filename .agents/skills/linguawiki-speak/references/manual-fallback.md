# Speaking with no voice tooling

Speaking must not depend on any product this repository does not control. The manual path
is a first-class route, not a degraded one.

1. The learner records the conversation however they can — a phone, a laptop, a friend
   taking notes. Or nobody records anything, and they simply speak.
2. Build the skeleton:

   ```bash
   linguawiki speaking package --external-session-id rozmowa-2026-03-02 \
     --language pl --started-at 2026-03-02T18:00:00Z --minutes 20 \
     --utterances 8 --out package.json --format json
   ```

3. Fill in the utterances from memory or from the recording. Approximate times are fine
   as long as they are *honest* approximations and stay inside the session's window; an
   invented precision is worse than a rounded minute.
4. If there is audio, register it: `artifact register --path artifacts/audio/... --kind
   audio`. Without it, every pronunciation claim is `observed` or `uncertain` at best, and
   prosody cannot be judged at all. That is the correct outcome, not a limitation to work
   around.
5. `speaking validate`, then `speaking ingest`, then close the session.

## Adapters

`--adapter whisper-verbose-json` maps a segment list with times and text onto utterances.
It needs the context the export does not carry:

```bash
linguawiki speaking ingest --input transcript.json --adapter whisper-verbose-json \
  --external-session-id rozmowa-2026-03-02 --language pl \
  --started-at 2026-03-02T18:00:00Z --format json
```

Everything the format carries that the contract has no place for stops at the adapter. A
provider field that reached a domain table would have to be migrated out later by somebody
who no longer knows why it is there.

An adapter that maps the wrong field produces a *valid* package that is wrong, and nothing
downstream could tell. So check the first import of any new export shape against the
recording by hand, once, before trusting it.
