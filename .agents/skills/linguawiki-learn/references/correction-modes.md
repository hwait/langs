# Correction modes

The mode is a property of the session, chosen at plan time (`--correction-mode`) or taken
from the track's declared preference. It changes what you do in the moment; it never
changes what gets recorded, and it never softens a score.

## Fluency

Respond to **meaning first**. Let the turn finish. Then correct at most three high-value
errors — the ones that impeded understanding, not the ones that were merely wrong.

- Recast rather than interrupt.
- Record every error you *noticed*, even the ones you chose not to raise: the record is
  about the learner's language, not about your teaching decisions.
- `help_level: none` if you gave nothing during the turn, whatever you said afterwards.

## Accuracy

Give the learner a chance to self-correct first — a pause, a raised eyebrow, "again?" —
then correct the target pattern promptly and explicitly.

- A self-correction after a prompt is `help_level: prompted`, not `none`.
- Correct the pattern the block is about. A session that corrects everything teaches
  nothing in particular.

## Exam

Record silently. Give feedback only after the whole task is finished.

- No hints, no elicitation, no teach-back. The planner enforces this: those activities
  are refused in an exam-mode block (`activity_forbidden_by_mode`).
- `help_level: none` throughout, because none was given.
- Exam conditions are the strongest evidence this system can collect. Do not weaken them
  by helping and then recording as though you had not.

## Graduated hints (outside exam mode)

When the learner is stuck, help in this order, and record the level you reached:

| What you did | `help_level` |
|---|---|
| Waited, then asked again | `prompted` |
| Narrowed the field ("it is a case ending") | `hinted` |
| Gave a partial form or a model | `scaffolded` |
| Gave the answer | `full-answer` |

Each level discounts the evidence, which is the correct outcome rather than a penalty:
an answer that needed the form supplied is not evidence that the learner can produce it.

## The rule under all of them

Record **observed performance**, never inferred success. Explaining something well is not
evidence that the learner learned it. If you did not see them do it, there is no attempt
to record.
