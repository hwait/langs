# When a session was interrupted

Run `session resume --format json` first. It reports the session's status and what it is
holding, and it starts a session that was planned but never begun.

| Status | What happened | What to do |
|---|---|---|
| `planned` | Nothing ran. | `resume` starts it; teach from block 1. |
| `active`, staged batches present | Flushes landed; the conversation ended. | Continue from `resume_from`, which names the next unfinished block and activity. Your next flush is `last_batch_sequence + 1`. |
| `active`, nothing staged | The conversation ended before any flush. | Those observations are gone. Say so, and re-run the block rather than reconstructing answers from memory. |
| `closing`, no result | A close was interrupted mid-transaction. | Nothing was credited. Retry `close` with the same outcome. |
| `completed` / `partial` | The close committed; you lost the answer. | `close` again returns the original result with `replayed: true`. `session show --session <id>` also reports it. |
| `abandoned`, staged events | Credited to nothing, kept for audit. | Review them with the learner, then `session recover --from <id>` into a new session. |

## What is safe to retry

- **A flush**: yes, with the *same* `idempotency_key` and the same events. A different
  payload under the same key is refused.
- **A close**: yes. The same outcome returns the stored result; a *different* outcome is
  refused, naming what was recorded.
- **A plan**: yes, with the same `--idempotency-key` *and the same request*. A different
  duration or mode under a key that already planned something is an
  `idempotency_conflict`, not a silent return of the older plan.

## What is not recoverable

A flush that never arrived. `close --outcome completed` refuses while a sequence is
missing; `partial-close` credits the batches that did arrive and warns that the others are
lost rather than pending.

Observations you never flushed. That is the cost of the batching boundary, and it is why
the default is one flush per block rather than one per session. If a block went well and
the conversation is getting long, flush it.

## Never do this

- Do not reconstruct unflushed answers from memory and record them as observed. An
  invented attempt is worse than a missing one: it enters the learner's model as evidence.
- Do not start a second session to "finish" the first. Resume it, or close it partially
  and plan the next one — two open sessions on one track make `session log` ambiguous, and
  the CLI refuses rather than guessing.
- Do not delete staged events. `abandon` keeps them on purpose.
