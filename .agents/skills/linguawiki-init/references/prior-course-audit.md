# Importing and auditing a prior course

The workflow is course-agnostic: a textbook, a class syllabus, or a list the learner wrote.
The question is always "I finished N units — what actually transferred?"

## What may be stored

Unit structure, unit titles, objective statements the learner or the outline's licence
permits, declared mappings, and a short source reference. **Not** course text, exercises,
answer keys, audio, or page images. `rights_status` records which case applies:

- `user-authored` — the learner wrote the objectives themselves;
- `personal-use-only` — a permissible outline for their own use;
- `cleared` — explicitly licensed for storage.

If the outline cannot legally be stored, ask the learner to enter unit titles and their own
objective statements instead. That is a normal outcome, not a failure.

## The import payload

```json
{
  "schema_name": "lingua.curriculum.v1",
  "schema_version": 1,
  "title": "<course or plan name>",
  "kind": "course",
  "version": "<edition or revision>",
  "source_reference": "<short citation, optional>",
  "rights_status": "personal-use-only",
  "provenance": "<who produced this outline and how it was obtained>",
  "units": [
    {
      "code": "u1",
      "title": "<unit title>",
      "level": "<a level of the track's framework, optional>",
      "parent_code": "<enclosing unit code, optional>",
      "objectives": [
        {
          "objective": "<what the unit claims the learner can do>",
          "dimension": "<a pack dimension, optional>",
          "mapped_kind": "knowledge",
          "mapped_key": "<pack stable key>",
          "map_confidence": "high"
        },
        {"objective": "<an objective the pack does not cover>"}
      ]
    }
  ]
}
```

A mapping needs `mapped_kind`, `mapped_key`, and `map_confidence` together; a mapped key that
is not an installed pack item is refused. Leave an objective unmapped when nothing in the
pack corresponds to it — that gap is the useful part of the import, and `curriculum show`
lists every one of them.

## Positioning

```bash
linguawiki curriculum position --workspace <path> \
  --completed u1 --completed u2 --current u3 --format json
```

A unit cannot be both completed and current. Completion moves the unit's *mapped* items to at
most `encountered`, with `self-report` provenance, and never lowers a stage the learner has
actually earned. Nothing here is evidence.

## Auditing

```bash
linguawiki curriculum audit-start  --workspace <path> --sample-size 12 --format json
linguawiki curriculum audit-record --workspace <path> --input results.json --format json
linguawiki curriculum audit-finalize --workspace <path> --format json
```

The sample is risk-weighted, and each item says why it was chosen:

| `risk_reason` | Why it is in the sample |
|---|---|
| `central-prerequisite` | Two or more later targets depend on it |
| `recent-unit` | From the last units claimed complete |
| `production-target` | A word, form, or construction meant to be produced, not just recognised |
| `older-material` | A thinner slice of supposedly known earlier material |

Results are a batch:

```json
{"results": [{"target_ref": "<content id from audit-report>", "outcome": "correct"},
             {"target_ref": "<content id>", "outcome": "partial", "score": 0.5}]}
```

`outcome` is `correct`, `partial`, or `incorrect`; `score` defaults to 1.0, 0.5, and 0.0.
Recording the same target twice is a no-op, so a retry is safe.

`audit-finalize` marks probed units `audited`, queues every miss as an `audit-gap` calibration
item weighted by its risk reason, and reports `untested_targets`. Read that list aloud: a
target the audit never probed is unverified, and calling it confirmed is the exact error this
workflow exists to prevent.
