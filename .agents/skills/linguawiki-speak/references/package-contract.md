# The `lingua.session.v1` package

One file describing one spoken session. It is somebody else's account of what happened,
which is why nothing in it is credited until a session close.

## Shape

```json
{
  "schema_name": "lingua.session.v1",
  "schema_version": 1,
  "package_id": "pkg_...",
  "external_session_id": "the producer's own id for this call",
  "session_id": null,
  "track_hint": "trk_... or null",
  "target_language": "pl",
  "mode": "checkpoint | completed",
  "started_at": "2026-03-02T18:00:00Z",
  "ended_at": "2026-03-02T18:15:00Z",
  "learning_targets": ["cnt_..."],
  "transcript_layers": [{"kind": "raw", "derived_from": null, "utterances": [...]}],
  "events": [],
  "artifacts": []
}
```

An utterance is `{utterance_id, speaker, started_at, ended_at, text}`. The
`utterance_id` is the **producer's** identifier, not ours: it is what makes one utterance
the same utterance across a checkpoint export and the completed export of the same call.
Keep it stable between exports or the same words will be stored twice.

## Audio a package declares is opened, not believed

A manifest entry is a claim about a file. Every `retained` artifact is checked before
anything is stored: it must sit under `artifacts/` or `imports/`, exist, hash to what the
package says, and not already name different bytes in this workspace. It is then
registered, so `artifact verify` checks it and `artifact purge` can reach it.

A package confirming pronunciation on a track that has not consented to keeping audio is
refused outright. The recording would be deleted at the door, and the claim would rest on
nothing.

## Rules the contract enforces

- exactly one `raw` layer, and unique layer kinds;
- every derived layer names the layer it came from, and its utterances resolve there;
- utterance times sit inside the session's own times, and end after they start;
- utterances are ordered by `started_at`;
- a `confirmed` pronunciation event names an audio artifact.

A validation failure names the field. Fix the export rather than deleting the field that
failed: a package that validates by having had its evidence removed is worse than one
that refuses.

## `checkpoint` versus `completed`

`checkpoint` is a partial export of a call still in progress. Ingesting a checkpoint and
then the completed export of the same call is the normal case, not a mistake — the
utterances they share are stored once, by the producer's identifier.

## Filling one in by hand

`linguawiki speaking package --out package.json` writes a skeleton. Replace:

- the stub `text` with what was actually said, in the target language, as heard —
  including the learner's errors. Do **not** correct it here; corrections are
  interpretations and belong in `transcript interpret`;
- the stub times with real ones. The scaffold spaces them evenly so the file is valid as
  written; leaving them would record a session whose chronology is invented.

Then `speaking validate` before ingesting, every time.
