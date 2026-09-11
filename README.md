# LinguaWiki

LinguaWiki is a local-first, language-agnostic learning system. Its Python core exposes typed CLI and JSON contracts for learner workspaces, language packs, sessions, and progress data. Persistent learner state lives in a private DuckDB-backed workspace; this core repository contains only reusable code and synthetic fixtures.

Stage 0 established the architecture and frozen v1 interchange contracts. Stage 1 added learner-workspace generation, the transactional DuckDB storage foundation, migrations, backup/recovery, and privacy diagnostics. Stage 2 added the language-pack contract with its authoring and review tooling, learner and track setup, declared-level onboarding, bounded resource preparation, prior-course import and audit, and bounded calibration runs. Stage 3 added the evidence-backed learner model: the knowledge graph, attempts and atomic evidence, recurring errors, transparent mastery aggregation, multidimensional skill estimates with an immutable history, and bounded privacy-aware context bundles. Stage 4 added the session engine and the first usable Polish vertical: an explained lesson plan, durable batched staging that changes nothing mid-session, an atomic close that credits its work exactly once, recovery from every interruption state, ingestion of externally produced sessions, and a generated learner dashboard. Stage 5 adds the material the learner actually works with: a source catalogue bounded by its rights, reading and listening with unaided comprehension kept apart from aided, reading recorded through the same session close as everything else, spoken sessions stored as transcript layers that keep a correction apart from a mishearing, acoustic claims that require the audio they rest on, opt-in recordings with auditable purges, and a privacy audit that looks for private content in the files a learner commits. See [the implementation plan](docs/LinguaWiki%20Implementation%20Plan.md), [Stage 0](docs/stage0.md), [Stage 1](docs/stage1.md), [Stage 2](docs/stage2.md), [Stage 3](docs/stage3.md), [Stage 4](docs/stage4.md), and [Stage 5](docs/stage5.md) for the detailed design and verification gates.

Create an independent learner workspace outside this repository, with a backup root outside that workspace:

```bash
linguawiki workspace init ~/PolishLinguaWiki \
  --backup-root ~/linguawiki-backups --name "Polish LinguaWiki" \
  --timezone Europe/Warsaw --history git-wiki

linguawiki workspace doctor --workspace ~/PolishLinguaWiki --format json
linguawiki db check --workspace ~/PolishLinguaWiki --format json
```

Then install a language pack, create a learner and a track, and onboard at a declared
level. The pack decides which frameworks, levels, dimensions, and themes exist, and its
maturity decides which onboarding modes are available at all:

```bash
W=~/PolishLinguaWiki
linguawiki pack install --workspace $W pl-pilot --format json
linguawiki user create  --workspace $W --name "Anna" --timezone Europe/Warsaw \
  --native ru --support en --format json
linguawiki track create --workspace $W --target-language pl --framework cefr \
  --declared-level A2 --target-level B1 --format json

linguawiki onboard start    --workspace $W --declared-level A2 --format json
linguawiki onboard finalize --workspace $W --format json
linguawiki assessment next  --workspace $W --format json
```

A declared level is stored as a low-confidence *hypothesis*: it decides which material to
prepare and establishes nothing about what the learner can do. `pl-pilot` is a deliberately
narrow `pilot` pack, so it serves a labelled calibration and refuses comprehensive
placement.

Progress comes only from evidence, and an item's stage is capped by the *kind* of evidence
behind it:

```bash
linguawiki evidence record --workspace $W --task-type objective --modality text \
  --score 1.0 --target pl.lex.dworzec --dimension reading --format json
linguawiki knowledge get   --workspace $W pl.lex.dworzec --format json
linguawiki estimate show   --workspace $W --format json
linguawiki context session --workspace $W --format json
```

Recognition can never promote spontaneous production, however often it succeeds: the claim
an attempt is allowed to make is checked against what the learner actually had to do, and
each claim caps the stage it can justify. `knowledge get` shows that reasoning — the gates
that were met, the ceiling the evidence imposes, and the recency behind it — and
`estimate history` explains every change to an estimate with the observations and weighting
factors that produced it. A dimension nothing has tested reports `not-tested`, which is not
a weak result.

A lesson runs through the session engine, and the boundary is the point of it: staging is
durable and changes nothing, and the close credits the whole session exactly once.

```bash
linguawiki plan create    --workspace $W --minutes 60 --format json
linguawiki session start  --workspace $W --format json
linguawiki session log    --workspace $W --input block-1.json --format json
linguawiki session close  --workspace $W --outcome completed --format json
linguawiki wiki build     --workspace $W --view dashboard --format json
```

Every block in the plan comes with the reasons that selected it, and every high-priority
candidate that missed out comes with the constraint that stopped it. `session log` stores a
bounded batch of observations and moves no stage; `session close` is the only command that
turns them into evidence, and calling it twice returns the first result rather than
crediting the work again. An interrupted session is picked up with `session resume`, which
names the state it is in: staged work waiting, a close that did not finish, or a close that
finished and lost its answer. `wiki build` regenerates the dashboard from the database — a
hand edit to a generated page is overwritten, never adopted.

Every command supports `--format json` and returns a stable envelope. Exit code `0` means
success, `1` means the command ran and reported failures in its payload, and `2` means the
command itself failed and printed an error envelope on stderr.

Development verification uses:

```bash
uv run python scripts/verify.py
```
