# Review axes, risk tiers, and what each state may claim

Origin and review are independent questions. An authentic quotation can be pedagogically
useless; an original AI drill can be correct but unverified. Never upgrade a review because
content is authentic, and never treat AI checking as verification.

## The five axes and their own state vocabularies

Each axis has its own words because the words mean different things. States are ordered
weakest first; the CLI refuses a state that does not belong to the axis.

| Axis | States, weakest first |
|---|---|
| `linguistic` | `unreviewed`, `machine-checked`, `reference-verified`, `human-verified` |
| `pedagogical` | `unreviewed`, `machine-checked`, `learner-approved`, `teacher-verified` |
| `source-alignment` | `unchecked`, `machine-checked`, `verified`, plus `not-applicable` |
| `rights` | `unknown`, `personal-use-only`, `cleared`, plus `restricted` (a refusal) |
| `privacy` | `private`, `redacted`, `shareable` |

`not-applicable` removes an axis from consideration; it never satisfies a requirement.
`restricted` rights is a negative verdict, not an unfinished one.

## The machine ceiling

A reviewer of kind `machine` or `ai` may reach `machine-checked` on the linguistic,
pedagogical, and source-alignment axes, and nothing at all on rights and privacy. This is
not a policy you can satisfy by running a second model: a second machine opinion is still a
machine opinion.

## What a promotion requires

A promotion gate is the elementwise maximum of the risk tier's row and the target
lifecycle's row. Read the `gate_problems` the CLI returns rather than reasoning it out.

| Risk tier | Typical content | Needs at least |
|---|---|---|
| 0 | ephemeral warm-up, one-time drill | nothing; do not persist it |
| 1 | comprehension question or cloze from a known segment | machine linguistic and pedagogical checks, machine source alignment, `personal-use-only` rights |
| 2 | Anki note, reusable personal drill, recurring-error exercise | as tier 1, plus `learner-approved` pedagogical |
| 3 | grammar, pronunciation, government, idiom, core example | `reference-verified` linguistic, `teacher-verified` pedagogical, **`verified` source alignment**, and an identified external source |
| 4 | redistributable pack release | `human-verified` linguistic, `teacher-verified` pedagogical, `verified` alignment, `cleared` rights, `shareable` privacy |

Lifecycle adds its own floor: `approved-personal` means useful and checked enough for *this*
learner; `verified` needs reference-level linguistic and pedagogical review; and
`publication-ready` needs every axis plus two different reviewer identities for AI-origin
content.

## Sampling, quarantine, and invalidation

1. A new or materially changed template inspects **every** persistent item of its first
   three runs. Changing a registered template's text is refused — register a new version, and
   its sampling history starts again.
2. After `pack template stabilize`, inspect at least 20% of a batch or five items, whichever
   is larger. Read `required_sample` rather than deciding for yourself.
3. Inspect every tier 3 and tier 4 item, and every Anki note in a small pack, regardless of
   sampling.
4. A substantive linguistic, alignment, rights, or answerability error in a sample
   quarantines the whole batch, quarantines its template, and invalidates dependent content.
   Record it as `--inspection defective --finding <what>`; do not quietly fix the one item.

`pack author invalidate` walks declared dependencies and marks everything downstream
`needs-review`. Use it when a source edition, answer key, recording, or governing policy
changes underneath content that was already approved.

## Reviews are bound to a revision

A recorded review names the content hash it examined. Editing the item leaves that review
attached to the old revision, and `approve` refuses until the review is redone. This is why
stamping is a deliberate act: re-stamping does **not** re-review.
