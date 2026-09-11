# Conditions, rubrics, and what evidence supports what

## Conditions

State the conditions before the first task and keep them: no dictionary unless the task's
`permitted_help` allows one, no retries, no hints, and one attempt per task. If the learner
breaks a condition, record the task honestly with a note in the rubric payload rather than
discarding it silently — a discarded task still counts as exposure.

Present `prompt` verbatim. Rephrasing an objective item changes its difficulty, and the
difficulty is what the posterior update uses.

## Objective and short-response tasks

Compare the learner's answer against the task's expected answers. Accept spelling and
diacritic variants only when the task's `expected` payload lists them; for a language whose
diacritics change the word, a missing diacritic is a wrong answer, not a typo.

## Extended productive tasks

Score every rubric dimension, then set `--score` to the weighted total and pass the detail
through `--input`:

```json
{"version": 1,
 "dimensions": [{"name": "task_completion", "score": 1.0, "note": "<why>"},
                {"name": "range", "score": 0.5, "note": "<why>"},
                {"name": "accuracy", "score": 0.5, "note": "<why>"},
                {"name": "coherence", "score": 1.0, "note": "<why>"}],
 "total": 0.75}
```

Store the per-dimension scores and a short rationale, not just the total: a total alone cannot
be reviewed later. Set `--assessor-kind ai` when you scored it, and `--confidence low` or
`medium` accordingly. A high-stakes level claim needs human or independent review, so say so
rather than raising the confidence.

Include `--excerpt` only if the learner consented to transcript retention. Without consent,
score the response and keep no excerpt.

## Pronunciation tasks

Audio is required for a pronunciation, intelligibility, rhythm, stress, tone, or prosody
judgement. A correct speech-to-text result does not prove correct pronunciation — the
transcriber may have normalised what it heard.

If audio is unavailable, do not score the task. Pause the run, or let the dimension stop
under-evidenced and report it as low confidence. Never score a pronunciation task from text.

## What each observation supports

| Evidence | Supports | Never supports |
|---|---|---|
| Objective reading item | reading comprehension, receptive vocabulary | production of the same item |
| Objective listening item | listening comprehension | pronunciation, speaking |
| Short-response form item | controlled form production | spontaneous production |
| Extended written prompt | writing, productive range and accuracy | speaking, pronunciation |
| Extended spoken prompt with audio | spoken production, interaction | pronunciation detail without a targeted task |
| Pronunciation task with audio | the specific contrast tested | general intelligibility across contexts |

## Pausing across sittings

`pause` keeps every posterior, budget, family record, and boundary-probe flag; `resume`
continues from exactly there. Prefer pausing to pushing through fatigue: a tired learner's
answers move the posterior just as much as a rested one's, and the run has no way to know
which it got.
