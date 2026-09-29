# Working through material, and recording what it taught

```bash
linguawiki source comprehension --workspace <path> --source <id-or-title> [--unit <label>] \
  --aid unaided|glossed|subtitled|translated|explained --band none|little|gist|most|full \
  [--mode intensive|extensive] [--replays <n>] [--lookups <n>] [--minutes <n>] --format json
linguawiki source position      --workspace <path> --source <id> [--unit <label>] [--minutes <n>] --format json
linguawiki source complete-unit --workspace <path> --source <id> --unit <label> [--minutes <n>] --format json
linguawiki source link          --workspace <path> --source <id> --unit <label> --target <key> --format json
```

## Unaided first, always

Ask what the learner understood **before** giving any help, and record that first. An
unaided reading recorded *after* an aided one is refused — it is not a second observation,
it is the first one with the help left out, and without that rule the strongest kind of
comprehension evidence would be the easiest to manufacture.

If you gave help before asking, record the aided band honestly. The workspace will warn
that this unit has no unaided measurement and never can have one. That warning is the
cost; do not avoid it by backdating an unaided reading.

## Intensive and extensive are different work

- **intensive** — a short passage worked closely. Several attempts and corrections from a
  small amount of text; extracting items from it is the point.
- **extensive** — volume for gist. One comprehension observation and a progress note. The
  workspace refuses more than a few extractions from an extensive pass, because harvesting
  a pleasure read turns it into an intensive one the learner did not agree to.

Say which you are doing before starting.

## Coverage counts completions, not passes

`complete-unit` marks a unit worked through. Completing it twice does not advance coverage
twice: a reread is not more of the source. Record a reread as another comprehension
observation instead — that is what it is.

## Inside a session

A reading or listening block records its work through the session like everything else:

```json
{"event_id": "evt_...", "kind": "source.progress", "occurred_at": "...",
 "payload": {"source_ref": "Polski Daily", "unit": "Odcinek 2", "aid": "unaided",
             "band": "most", "mode": "intensive", "replays": 1, "minutes": 9,
             "completed": true}}
```

Nothing is credited until the session closes, and the unaided-before-aided rule is
enforced there too. The standalone `source comprehension` command is for work done outside
a session — a commute, a chapter before bed — not a way around the close.

## Listening is not reading

Never infer listening from a transcript the learner read, and never let them read along on
a first listening pass unless the block is explicitly about that. If the audio is
unavailable, the block is unavailable — say so rather than substituting text.
