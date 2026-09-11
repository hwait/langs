# Choosing material, and cataloguing it honestly

A source belongs to a **track**, not to a workspace: two learners reading the same book
have two catalogue entries, because progress and comprehension are facts about a learner's
relationship with the material rather than about the material.

```bash
linguawiki source add  --workspace <path> --kind podcast --title "..." [--creator ...] \
  [--uri ...] --rights metadata-only|short-excerpt|full-local [--rights-note ...] \
  [--has-audio] [--has-transcript] [--input units.json] --format json
linguawiki source list --workspace <path> [--status ...] [--kind ...] --format json
linguawiki source show --workspace <path> --source <id-or-title> --format json
linguawiki source status --workspace <path> --source <id> --status active|completed|... --format json
```

Units arrive as a payload because a unit may carry an excerpt, and an excerpt is text from
the work:

```json
{"units": [{"label": "Rozdział 1", "start_locator": "p.7", "excerpt": "..."},
           {"label": "Rozdział 2", "start_locator": "p.31"}]}
```

## Rights are chosen before anything is stored

- `metadata-only` — title, author, where it is. **No text from the work at all.** Use this
  by default for anything published.
- `short-excerpt` — a bounded quotation that justifies an observation. Not a page.
- `full-local` — the learner wrote it, or it is theirs to hold.

An excerpt longer than the class permits is refused at the command, and `db check` finds
one that got in another way. This is not pedantry: the excerpt lives in a database the
learner may sync, back up, or share.

## Choosing what to work

Prefer material the learner has already started: `source list` shows `in-progress` first
and the planner scores unfinished material as a reason to plan that kind of block. A half
-read book the learner returns to teaches more than a new one they open.

Reject material that is above the level for *unaided* work. If the first unaided pass
comes back below `gist`, say so and choose something else rather than glossing the whole
thing — the aided reading that follows would measure your scaffolding.
