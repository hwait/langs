---
name: linguawiki
description: Inspect and operate a LinguaWiki language-learning workspace through its typed CLI, and route a request to the right specialist skill. Use for LinguaWiki status, learner-workspace setup, database migration, backup, restore, and privacy diagnostics, for the evidence-backed learner model - knowledge graph, attempts and evidence, recurring errors, skill estimates, and bounded context bundles - and to dispatch language setup, pack authoring, and assessment work.
---

# LinguaWiki

Use the Python CLI as the machine boundary. Never edit a learner database, a generated
`.agents/skills/` snapshot, or the generated wiki as a substitute for a command.

Every command accepts `--format json` and returns a stable envelope. Treat that envelope as
authoritative and preserve its error `code` and `retryable` flag in any explanation.

Exit codes:

- `0` — the command succeeded;
- `1` — the command ran and reported failures in its own payload (`data.ok` is `false`);
- `2` — the command itself failed and printed an error envelope on stderr.

## Runtime status

```bash
linguawiki status --format json
```

Report the application version, contract schema version, database schema version, and stage.

## Learner workspaces

A learner workspace is an independent private repository that pins a released core; it never
contains a copy of the core Python package. Create one only outside the core repository, and
always with an explicit backup root outside that workspace and outside any Git repository:

```bash
linguawiki workspace init <path> --backup-root <path-outside-the-workspace> \
  --name "<display name>" --timezone <IANA-zone> --history git-wiki [--git-init] [--uv-lock]
```

`workspace init` builds the workspace in a staging directory and publishes it with a single
rename, so an interrupted run leaves the target untouched and can simply be retried. It is
idempotent on an untouched workspace and refuses to overwrite one whose generated files were
edited. `--git-init` creates a local repository only: never a remote, a
commit, or a push. Only the `git-wiki` history policy exists in `lingua.workspace.v1`; the
other policies in the plan need a contract amendment first.

Diagnostics:

```bash
linguawiki workspace status  --workspace <path> --format json
linguawiki workspace doctor  --workspace <path> --format json
linguawiki workspace privacy-check --workspace <path> --format json
linguawiki workspace confirm-remote --workspace <path> --private
linguawiki workspace lock-dependencies --workspace <path> [--find-links <artifacts>]
```

`workspace lock-dependencies` (and `workspace init --uv-lock`) resolves the workspace `uv.lock`
with `uv`. The workspace pins an exact core version, so uv must be able to reach that artifact:
from an index, or via `--find-links` for a release that is built but not yet published. Never
hand-write that file — `workspace doctor` parses it and checks that it resolves the pinned core.
`workspace init --uv-lock` reports an unresolvable lock as a warning and still creates the
workspace; `workspace lock-dependencies` fails, because resolving the lock is its only job.

`workspace confirm-remote` records every fetch and push endpoint of every remote the learner
confirmed private, as sanitized fingerprints — a remote can push somewhere other than it fetches,
and can have several push destinations. Adding, removing, or repointing any endpoint makes the
confirmation stale and `workspace doctor` warns again; re-confirm only after checking them all.

`workspace doctor` checks pins, generated files, the generated skill snapshot, ignore rules,
database integrity, the dependency lock, and the backup root. Report failures verbatim; do not repair a workspace
by editing files. A modified skill snapshot is repaired with `linguawiki skills install`.

`workspace privacy-check` lists the files that could reach Git. It rejects both known private
artifacts and any entry outside the workspace's Git-safe top level, so a stray note or scratch
directory is reported rather than quietly committed. It inspects whichever repository would
actually track the workspace, including an ancestor one. If it reports `source: unavailable`, the
scan did **not** run — report that as a failure and never describe the workspace as safe. Sanitization is policy filtering, not proof
that a repository is safe to publish; never tell a learner their workspace is safe to make
public.

## Database lifecycle

```bash
linguawiki db status  --workspace <path> --format json
linguawiki db check   --workspace <path> --format json
linguawiki db migrate --workspace <path> [--dry-run] --format json
linguawiki db backup  --workspace <path> [--reason <slug>] --format json
linguawiki db export-portable --workspace <path> --to <directory> --format json
linguawiki db restore --from <backup> --to <new-path> [--kind native|portable] --format json
```

`db init` only creates an absent or empty database. A database that already holds LinguaWiki data
must go through `db migrate`, which backs it up and verifies the backup before the schema changes;
a file holding data LinguaWiki did not create is refused by both. `db restore`
always writes to a new path and never overwrites the active database. Only one writer runs at
a time: a `writer_locked` or `database_busy` error is retryable, so wait and retry rather than
deleting a lock file. Deleting it does not help — DuckDB holds its own exclusive lock.

`--reason` must be a short lowercase slug; it names the backup directory.

## Routing

### Resolve the scope before routing anything

Three facts decide what is even possible, and all three come from the CLI:

```bash
linguawiki status           --format json                      # release, contract, schema
linguawiki workspace status --workspace <path> --format json   # database, pins, identity
linguawiki user list        --workspace <path> --format json   # is there a learner
linguawiki track list       --workspace <path> --format json   # is there a track, on which pack
```

`track list` is the important one: a track names the pack it is taught from, its framework,
and its level order. Every level label, dimension, and item identity below is relative to
that pack. If there is no track, the request is a setup request whatever it sounded like.

Where a command takes `--track`, omit it only when exactly one track exists. With several,
pass the identifier explicitly rather than relying on a default.

### Route by what the request is *for*, not by the words in it

| The request is about | Route to |
|---|---|
| No database, no learner, no track, or a new language | `$linguawiki-init` |
| Learner profile, preferences, consent, or a track's goal and level | `$linguawiki-init` |
| A prior course: importing an outline, positioning, auditing what transferred | `$linguawiki-init` |
| Creating, validating, measuring, reviewing, or publishing a language pack | `$linguawiki-pack` |
| A calibration, a level check, or a placement run | `$linguawiki-assess` |
| A lesson: "teach me", "let's practise", "continue", a grammar or reading session | `$linguawiki-learn` |
| A session that was interrupted, or staged work that was never credited | `$linguawiki-learn` |
| "What do I know / what am I forgetting / why does it say I know this" | this skill, `knowledge` and `evidence` |
| Recording what happened in practice done outside LinguaWiki | this skill, `evidence record` |
| A recurring mistake, whether it is fixed, what to come back to | this skill, `errors` |
| Assembling context for another agent to plan or assess with | this skill, `context` |
| Workspaces, migrations, backups, restore, privacy, or the skill snapshot | this skill |

An ambiguous request is one question, not a guess. "Test me on this" is an assessment run if
the learner wants a level, and evidence recording if they want practice to count — the two
write different things and the difference matters.

Rules that hold whichever way you route:

- Resolve the user and track through the CLI, never from the generated wiki.
- A maintenance request never starts a learning or assessment run.
- "Continue" means the learner's own next step: an open session is resumed, a closed one is
  followed by a new plan. Check `session status` before planning a second session, because
  two open sessions on one track make a flush ambiguous.
- Install a pack before creating a track for its language; create a track before onboarding.
- Never translate a level label between frameworks, and never claim a level from a
  self-report.
- One primary program per workspace is the default. A second target language is normally a
  second workspace, not a second track.

## Sessions

A lesson belongs to `$linguawiki-learn`, which owns the whole loop. What this skill needs
to know is the shape of it, so that "continue" routes correctly and a maintenance request
never walks into a half-open session:

```bash
linguawiki plan    create|show --workspace <path>
linguawiki session start|status|log|staged|resume|close|partial-close|abandon --workspace <path>
linguawiki session recover|ingest-package --workspace <path>
linguawiki wiki    build --view dashboard --workspace <path>
```

`session status` is the safe question to ask at any time. It says whether the track has an
open session, how many observations it is holding, and what a reader can do next. Three
answers matter here:

- **active, with staged events** — a lesson is in progress. Route to `$linguawiki-learn`
  to resume it rather than planning a new one.
- **closing, with no result** — a close was interrupted. Nothing was credited; the
  learning skill retries it.
- **abandoned, with staged events** — observations are on record and credited to nothing.
  They can be recovered into a new session after review.

`wiki build --view dashboard` regenerates the learner's dashboard from the database. It is
the only way that page changes: a hand edit is overwritten, never adopted.

## The learner model

These commands cover work that happened *outside* a live session: an import, or a repair.
A live lesson's observations belong to the session engine — `evidence record` refuses
`--origin session` — so route a lesson to `$linguawiki-learn` rather than recording its
attempts here.

```bash
linguawiki knowledge get|search|upsert|link|merge --workspace <path>
linguawiki evidence  record|observe|list|recompute --workspace <path>
linguawiki errors    record|show|list|followup|queue --workspace <path>
linguawiki estimate  show|history --workspace <path>
linguawiki context   session|assessment|source|concept --workspace <path>
```

### Recording evidence

```bash
linguawiki evidence record --workspace <path> \
  --task-type <shape-of-demand> --modality text|audio|speech|writing --score <0..1> \
  [--target <item>] [--dimension <name>] [--claim <claim> ...] \
  [--help-level none|prompted|hinted|scaffolded|full-answer] \
  [--retrieval immediate|same-session|delayed] [--delay-hours <n>] \
  [--context "<setting>"] [--input response.json] [--origin import|repair] --format json
```

An attempt is what happened; a **claim** is what it proves. The two are separate because the
same attempt proves different things depending on what the learner had to do, and the CLI
refuses a claim the attempt cannot support — by name, saying which claims that task type
*can* produce. Do not retry with a different flag to get past it; record the weaker claim
that is actually true.

`--target` must be an item the track's own pack ships, or one the learner authored on this
track. Another pack's item is refused and says whose it is, because a track's level labels,
dimensions, and identities are all relative to the pack it is taught from.

When `--task` names a bank task, **omit `--task-type` and `--modality`**: what the task
demanded is read from the run that served it — or from the pack when there is no run — and
passing values it contradicts is refused rather than obeyed. A pack is mutable and a run is
not, so the run's own record is what a learner's history rests on; if the pack has changed
since, the result says so.

`--assessment-run` additionally requires that the run served the task and that the run
belongs to this track.

- Naming no `--claim` records the **weakest** claim the task type supports. That is
  deliberate: an unstated claim is never the strongest one.
- `--retrieval delayed` needs a real gap — at least 12 hours, either declared with
  `--delay-hours` or visible in the item's own history. It is refused otherwise, because
  delayed evidence is the strongest kind there is.
- `--help-level full-answer` leaves nothing positive to observe. A given answer is not an
  observation of the learner.
- `--context` is the unit diversity is counted in. Two successes on the same prompt are one
  observation repeated; the aggregation needs to be able to tell.
- The learner's own words go through `--input` as `{"response": "..."}`, never argv, and what
  is retained follows the track's consent. Without it the attempt keeps a bounded excerpt;
  asking for the full text without consent is refused rather than silently truncated.
- `evidence observe` records fatigue, a strategy, or a note. An observation is context for
  planning and **cannot** promote anything — it names no claim and carries no strength.

Read what came back: `stage_before`, `stage_after`, and `stage_explanation`, which lists the
gates that were met, the ceiling the kind of evidence imposes, and the decay behind it. If
the stage did not move, say so and say why rather than implying progress.

### Errors

```bash
linguawiki errors record --workspace <path> --category <kind> --signature "<the form>" \
  --description "<what is wrong>" [--target <item>] [--attach-to <error-id>] [--distinct]
```

An error's identity is `(track, category, normalized signature, target)`. A signature that
is neither clearly the same as an existing one nor clearly different is **refused** with the
candidates listed: `--attach-to` files it against one, `--distinct` insists it is new. Both
remedies work; choose with the learner rather than guessing, because merging two different
errors loses the distinction permanently.

One correct answer never resolves an error. `outstanding` says exactly what the policy is
still waiting for — controlled, novel, spontaneous, and delayed success across more than one
context — and counter-evidence is derived from the evidence rows, never asserted. Relay
`outstanding` verbatim rather than saying an error looks fixed.

`controlled` and `spontaneous` are terms about **production**: they need the learner to have
produced language, not chosen between offered options, whatever claim the evidence carries.
Delayed reading checks are `delayed` and `novel`, and they cannot retire an error the learner
still makes in speech or writing.

`--classification` matters. A `transcription-artifact` or an `uncertain` occurrence is
recorded and counts for nothing: the pattern sits at `unconfirmed`, stays out of the live
error set, and activates only when an occurrence is classified `learner-error`. A mishearing
taught back as a mistake is worse than a mishearing lost, so classify honestly rather than
defaulting.

### Estimates

```bash
linguawiki estimate show    --workspace <path> [--summary] --format json
linguawiki estimate history --workspace <path> [--dimension <name>] --format json
```

One estimate per dimension, each with `level_code`, the `level_low..level_high` range,
`confidence_label`, `basis`, and `estimate_status`. Read `estimate_status` before anything
else:

- `not-tested` — nothing measured this. It is **not** a weak result, and must never be
  reported as one.
- `provisional` — a declared level, or a single observation. A reading, not a measurement.
- `estimated` — independent observations agree.

Never present one global level unless the learner asks; `--summary` produces one and labels
it a summary, and the label travels with it. `estimate history` explains every change with
the evidence and the weighting factors behind it, so "why did this move" is answerable.

### Context bundles

```bash
linguawiki context session|assessment|source|concept --workspace <path> \
  [--item <item>] [--max-records <n>] [--max-tokens <n>] [--include-responses] --format json
```

Each scope carries only the sections it declares. An `assessment` bundle deliberately holds
no errors and no per-item evidence: an assessor that knows the learner's usual mistakes is
scoring its own expectations.

**Always read `omissions` and `bounded`.** A bundle that hit its budget drops whole sections
and records what was in them, so "no active errors" and "eleven, three shown" look different.
Acting on a bounded bundle as though it were complete is the failure this reporting exists to
prevent. `--include-responses` needs transcript consent and is refused without it.

`records` never exceeds `--max-records`, and both count every section except `provenance` —
one row of version and pack metadata that is what makes the rest traceable.

## Learners, tracks, and packs

These commands exist as of Stage 2; the specialist skills own the workflows.

```bash
linguawiki pack   scaffold|validate|coverage|stamp|publish <pack>
linguawiki pack   install|diff|update|list --workspace <path>
linguawiki pack   author ... | template ...     --workspace <path>
linguawiki user   create|show|update|list       --workspace <path>
linguawiki track  create|show|update|activate|pause|archive --workspace <path>
linguawiki onboard start|record|status|finalize|abandon --workspace <path>
linguawiki resources plan|prepare|status        --workspace <path>
linguawiki curriculum import|show|position|audit-start|audit-record|audit-finalize|audit-report
linguawiki assessment start|next|record|pause|resume|abandon|finalize|report
linguawiki knowledge get|search|upsert|link|merge  --workspace <path>
linguawiki evidence  record|observe|list|recompute --workspace <path>
linguawiki errors    record|show|list|followup|queue --workspace <path>
linguawiki estimate  show|history                  --workspace <path>
linguawiki context   session|assessment|source|concept --workspace <path>
```

Structured payloads go through `--input <file>` or `--input -` for stdin, never as a shell
argument: learner text and pack drafts are arbitrary Unicode, and argv leaves them in shell
history.

## Boundaries

Stage 4 covers workspaces, storage, packs, learners, tracks, onboarding, bounded resource
preparation, prior-course audit, calibration and placement runs, the learner model
(knowledge graph, attempts and evidence, recurring errors, mastery aggregation, skill
estimates, context bundles), and the session engine: planning a lesson, staging its
observations, closing it atomically, recovering an interrupted one, ingesting an externally
produced session, and rendering the learner dashboard. There are still no source, review-
queue, Anki, report, or draft-import commands, and `wiki build` renders only `--view
dashboard`. Do not invent them, and do not write learner content into a workspace by hand.

Three refusals are the point of this stage rather than obstacles in it:

- **No mastery without evidence.** A stage is computed from evidence and capped by the *kind*
  of evidence behind it. Recognition can never promote production, however often it
  succeeds, and no flag changes that.
- **No level the learner does not hold.** An estimate is a band plus a range plus a
  confidence. Report all three; a band alone overstates a six-observation probe.
- **No untested dimension reported as weak.** `not-tested` means nobody looked.

A failure is also a result. If a stage did not move, an error is not resolved, or a
dimension stayed untested, say so plainly — the learner's model is only worth having if it
can disagree with them.
