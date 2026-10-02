---
name: linguawiki-assess
description: Run a bounded LinguaWiki calibration or placement, finalize a baseline, and report per-dimension estimates with their uncertainty and provenance. Use for baseline calibration, level checks, placement runs including pausing and resuming across sittings, explaining why an estimate changed, and recomputing estimates from raw evidence. Refuses comprehensive placement where the pack's bank cannot support it.
---

# LinguaWiki assessment

Every command is a `linguawiki` CLI call with `--format json`. The CLI owns the algorithm:
task selection, the posterior update, the stop rule, exposure limits, and the confidence
label. Your job is to run the tasks honestly and report what came back — never to compute an
estimate yourself or to round a range into a level.

```bash
linguawiki assessment start    --workspace <path> [--run-type pilot-calibration|placement] \
  [--dimension <name> ...] [--modality <name> ...] [--idempotency-key <key>] --format json
linguawiki assessment next     --workspace <path> [--run <id>] --format json
linguawiki assessment record   --workspace <path> --content <id> \
  [--response "<what the learner answered>" | --response-file <path>] [--score <0..1>] \
  [--response-visibility withheld|excerpt|full] [--rubric rubric.json] \
  [--audio-artifact <artifact-id>] [--submission <id> --claim <id>] \
  [--assessor-kind deterministic|ai|learner|human] [--assessor <who>] \
  [--confidence low|medium|high] [--idempotency-key <key>] --format json
linguawiki assessment pending  --workspace <path> [--run <id>] --format json
linguawiki assessment claim    --workspace <path> [--run <id>] --judge <your name> \
  [--lease <seconds, default 600>] [--limit <n>] --format json
linguawiki assessment release  --workspace <path> --claim <id> --reason <why> \
  [--terminal --code <code>] --format json
linguawiki assessment pause    --workspace <path> [--run <id>] --format json
linguawiki assessment resume   --workspace <path> [--run <id>] --format json
linguawiki assessment finalize --workspace <path> [--run <id>] [--reason <why>] --format json
linguawiki assessment report   --workspace <path> [--run <id>] --format json
```

## The loop

1. `start` opens a run and persists every dimension's grid, prior, budget, and status. A
   dimension the pack or the learner's equipment cannot serve is closed as `not-tested`
   immediately, with the reason.
2. `next` serves one task and says why it chose it: `informativeness`, `content-family
   diversity`, or `boundary probe`. When no dimension is still open it returns the run report
   instead of a task — that is the signal to finalize.
3. Present the task exactly as `prompt` gives it. Respect `permitted_help`. Do not rephrase an
   objective item, add examples, or hint.
4. `record` scores the served task: the learner's answer for a machine-scorable one, your
   rubric verdict for a judged one. Then loop back to `next`.
5. `finalize` closes the run and writes one estimate per dimension.

`next` and `record` are the only way tasks enter a run: recording a task that was not served
is refused, and re-recording an answered task is an idempotent no-op, so a retry is safe.

## Recorded answers: claim, listen, record

A learner can answer a spoken task in the browser by recording it, where their track has
both said it can record and agreed to the recording being kept. The page submits the
recording; nothing scores it until a judge does. You may be that judge.

1. `assessment claim --run <id> --judge <your name>` hands you the recordings waiting for a
   judge, each under a lease (600 seconds unless you ask with `--lease`; `--limit` takes
   fewer). Each entry is what `pending` lists -- the served task as it was served (`prompt`,
   `rubric`, `dimension`), the submission, and `audio_path`, the recording's path inside the
   workspace, already checked to be the bytes the learner submitted -- plus its `claim_id`.
   **Read the audio only through that path.** Never guess one from a directory listing.
   The command returns before you judge: you hold nothing open while you listen.
2. Listen to the recording and score it against the snapshotted rubric.
3. `assessment record --content <id> --submission <submission_id> --claim <claim_id>
   --idempotency-key <claim_id> --score <0..1> --rubric <file> --assessor-kind ai
   --assessor <your name> --confidence low|medium`. Use the claim ID as the key, so a
   retry after a lost response replays rather than scoring twice. An `ai` verdict on
   pronunciation or spoken production **must name its assessor and may not claim more
   than `medium`**: `assessor_required` and `assessor_confidence_ceiling` refuse it, and
   the rule's version is stored on the result. On a paused run the verdict is *held*
   (`held: true`) and applied when the run resumes.
4. If you cannot judge it now (you crashed, timed out, the tool failed), give it back:
   `assessment release --claim <id> --reason <why>`. If it can never be judged (the
   recording is silence, or not the task), `--terminal --code <code> --reason <why>`
   withdraws it and skips its task. Doing nothing is the same as a non-terminal release
   once the lease runs out. A verdict after the lease ran out still lands if nothing else
   happened to the submission.

`assessment pending` is the read-only listing of the same entries, with `attempts`,
`claimed_by`, and `lease_expires_at`; it hands nothing out. Never judge an entry whose
`judgeable` is false; its `problem` says why, and that reason is what to report -- `claim`
withdraws such a recording rather than hand it out, and lists it under `withdrawn`.

Each claim is an attempt. After three attempts end with no verdict, the submission is
withdrawn as `assessment_judging_exhausted`: the learner loses that observation, and the
dimension serves another task. Relay that; it is not a learner error.

The recording is checked again when your verdict arrives. If it was purged, altered, or is
no longer kept while you were listening, the verdict is refused by that reason
(`assessment_audio_purged`, `assessment_audio_altered`, ...), the submission is withdrawn,
and the task is skipped -- relay that; do not try again with another recording. Other
refusals name what happened instead: `assessment_submission_superseded` (the learner
answered again; judge the successor it names), `assessment_claim_released` (your claim was
released; claim again), `assessment_verdict_conflict` (another judge's different verdict
landed first). A verdict for a task whose result was invalidated is refused as
`assessment_result_invalidated`: a fresh measurement belongs to a fresh run.

## Scoring

- **Objective and short-response tasks: do not score these yourself.** Pass the learner's
  answer verbatim through `--response` and leave `--score` off. The CLI compares it against
  the answer key the run recorded when it served the task, which is the only account of what
  the learner was actually asked — a pack edited since cannot change the verdict, and neither
  can you. Pass the answer *as given*: do not correct, tidy, or excerpt it. Use
  `--response-file` when the answer is long enough that argv is awkward.
- Extended productive tasks: score each rubric dimension, pass the whole rubric result through
  `--rubric`, and set `--score` to the weighted total. Store the rubric detail, not only the
  total. (`--input` is the older spelling of the same payload; pass one, not both.)
- Pronunciation tasks: **audio is required**. A correct transcript proves nothing about
  pronunciation. Judge only from a recording `assessment claim` hands you, and name it
  with `--submission` and `--claim`. If there is none, do not score the task — leave the dimension to
  stop as under-evidenced, or pause the run until audio is available.

`--score` stays available for the judged types and for a verdict you reached yourself. A
supplied score is recorded as `supplied` and wins over any comparison; only a computed one
carries the scoring policy version, because `--assessor-kind` has only ever *labelled* a
score rather than saying who reached it.

Set `--assessor-kind` truthfully. `ai` means you scored it; `human` means a person did.
`--confidence low` on an AI-scored productive task is the honest default, and on a spoken
one `medium` is the ceiling.

**When a recording goes, so does what rested on it.** Purging a recording -- or a retention
sweep doing it -- marks every result judged from it invalidated, replays the run without it,
and rebuilds the estimate from what survives; the history it shaped is annotated, not
rewritten. Report an invalidated result as withdrawn evidence, never as a score.

**The response is an input, not something you decide to store.** It is compared in memory
and reaches the database only through the track's retention rule: no consent keeps a hash
and no text, and the score still stands. Ask for `--response-visibility full` only where the
track has granted transcript consent — it is refused otherwise, before anything is written,
and the task stays answerable. Refusals worth knowing by name:
`assessment_not_machine_scorable` (this type needs a judge),
`assessment_score_required` (nothing here can compute it — supply a score),
`assessment_snapshot_partial` and `assessment_answer_key_malformed` (the record is damaged;
report it rather than working around it).

## Reading a result

Report, per dimension: `status`, `tasks_used` against its budget, `confidence`,
`estimated_level`, and the `credible_low..credible_high` range. Then:

- **Never collapse the profile into one level.** A learner can be B1 in reading and A2 in
  speaking; that difference is the useful part.
- **Always give the range and the confidence together.** `estimated_level` alone overstates
  what a six-task probe established.
- **`not-tested` is not a failure.** Say which dimensions were not measured and why —
  the pack had no task, or the modality was unavailable.
- **Read `stop_reason`.** `precision-reached` is a real result; `maximum-budget`, `bank
  exhausted inside the reuse window`, fatigue, and a learner request are all lower-confidence
  stops and must be reported as such.
- **`calibration_label` matters.** `pilot-calibration` is a labelled probe against a narrow
  pack, not a level claim. Never describe it as a placement.

## Finalizing a baseline

`finalize` writes one estimate per dimension and records an immutable snapshot explaining
each one. From Stage 3, a run's posterior is not the end of the estimate — evidence recorded
later folds into the *same* distribution, so a baseline and the weeks after it are one
continuous estimate rather than two competing ones.

```bash
linguawiki assessment finalize --workspace <path> [--run <id>] --format json
linguawiki estimate  show      --workspace <path> --format json
linguawiki estimate  history   --workspace <path> --dimension <name> --format json
linguawiki evidence  recompute --workspace <path> [--dry-run] --format json
```

`estimate show` is what to report, and `estimate_status` comes first:

- `estimated` — independent observations agree. A band, a range, and a confidence.
- `provisional` — one observation, or observations from a single context. A reading.
- `not-tested` — nothing measured it. Say which dimensions, and why.

`estimate history` answers "why did this change": each snapshot names its `reason`, its
weighting `factors`, and the `evidence_ids` behind it. Use it instead of reconstructing a
story; if a learner disputes an estimate, the snapshot is the answer.

`evidence recompute --dry-run` shows what a recomputation *would* change without writing
anything. Run it before recomputing after a long gap, and report what moved and why — decay
alone can lower a stage, and that is a real result rather than a bug.

### Scoring a task also records evidence

A scored bank task is an observation of the learner, so where it should also count toward an
item's stage, record it as evidence with the task named:

```bash
linguawiki evidence record --workspace <path> --origin assessment \
  --assessment-run <run-id> --task <task-content-id> --score <0..1> \
  [--target <item>] [--claim <claim>] --format json
```

Do **not** pass `--task-type`, `--modality`, or `--dimension` here. What the learner faced
is read from **the run's own record of what it served**, not from the pack — a pack is
mutable and a run is not, so a later pack edit cannot rewrite what somebody answered. A
value you pass that contradicts it is refused with the recorded value named. That is the
guard: describing a text short-response as a spoken exchange would otherwise buy a
spontaneous-production claim the learner never earned, and so would editing the pack
afterwards.

If the bank item has changed since the run served it, the observation still records what
the learner faced and the result **warns** that the two disagree. Relay that warning: it is
the difference between the learner's history and the pack's current opinion of it.

`--assessment-run` requires that the run served the task *and* that the run belongs to this
track. A run is one learner's sitting; attaching it elsewhere would put one learner's work
in another learner's model.

The served record also supplies the difficulty and content family, so the observation lands
on the same ability grid the run used — and it is not counted twice, because the run's
posterior already holds what the run itself scored.

See `references/placement-algorithm.md` for how the estimate is produced and
`references/scoring-and-conditions.md` for exam conditions and rubric handling.

## Boundaries

- `--run-type placement` is refused unless the pack is `placement-ready`; the error lists the
  unmet bank requirements. Relay it and offer a labelled calibration instead. Do not retry as
  a calibration and then describe the result as a placement.
- Never re-serve an item the learner has seen inside the six-month reuse window; the CLI
  enforces this, so do not work around a `bank exhausted` stop by starting a fresh run.
- Never infer listening from reading a transcript, or speaking from a written answer.
- Never adjust an estimate by hand. There is no command that sets one, and that is
  deliberate: an estimate is derived from evidence plus an algorithm version, and both are
  recorded on it.
- Never claim a framework level for the learner as a whole without an explicit request.
  `estimate show --summary` produces one and labels it a summary; the label travels with the
  number and must reach the learner with it.
- Never report an `estimate_status` of `not-tested` or `provisional` as a level. The first
  means nobody looked; the second means one observation, which is a reading.
- Pause rather than push through fatigue: `pause` keeps every posterior, and `resume`
  continues where it stopped.
