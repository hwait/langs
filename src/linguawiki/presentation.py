"""How a task is shown, and the one rule that ties a choice to the answer key.

The client renders from this record and never from the prompt string. The scorer does
not read it at all: `placement.score_response` folds whatever the learner submitted and
compares it against the served `AnswerKey`, so the only way a button can be right is for
its **value** to be a string that key accepts. That is enforced here, at the last moment
a pack can still be corrected, rather than discovered as a learner's zero.

Deciding it anywhere else would mean a second place the right answer is recorded -- an
option id mapped back to an answer -- and the two would drift. The mapping does not
exist, so there is nothing to keep in step and nothing for the served snapshot to carry
beyond the choices themselves.
"""

from __future__ import annotations

from collections.abc import Sequence

from linguawiki.placement import scoring_form

#: How a task is answered. `multiple-choice` draws buttons; `free-text` draws a field.
#: Both may be machine-scored -- the distinction is rendering, not scoring, and
#: conflating them was an earlier error.
PRESENTATION_KINDS: tuple[str, ...] = ("multiple-choice", "free-text")

#: Whether a renderer may reorder the choices. `shuffled` is resolved once, at serve
#: time, and the realized order is snapshotted; a resumed task must not reshuffle.
CHOICE_ORDERS: tuple[str, ...] = ("fixed", "shuffled")

#: Media types a pack asset may declare. Audio only: nothing else in this repository
#: knows what to do with a pack-shipped file, and a vocabulary that accepts what no
#: caller handles is documentation pretending to be a constraint.
ASSET_MEDIA_TYPES: tuple[str, ...] = ("audio/wav", "audio/mpeg", "audio/ogg", "audio/flac")

#: Where a pack keeps the bytes an asset names. Role is assigned by declared path
#: everywhere else in a pack, and this keeps a recording out of `assets/`, where the
#: loader reads every file as a catalog.
MEDIA_PREFIX = "media/"


def colliding_choice_values(values: Sequence[str]) -> tuple[str, ...]:
    """Choice values that are the same answer under the comparison that scores them.

    Two buttons that fold to one string are one answer shown twice: whichever the
    learner presses, the same score comes back, so the task cannot distinguish them.
    """

    seen: dict[str, str] = {}
    collisions: list[str] = []
    for value in values:
        folded = scoring_form(value)
        if folded in seen:
            collisions.append(value)
        else:
            seen[folded] = value
    return tuple(collisions)


def choices_answering_key(values: Sequence[str], answers: Sequence[str]) -> tuple[str, ...]:
    """The choices the answer key accepts, compared exactly as the scorer will.

    A key may hold several spellings of one choice -- that is what `answers` is for --
    so the rule is about how many *choices* it accepts, not how many answers match.
    """

    accepted = {scoring_form(answer) for answer in answers}
    return tuple(value for value in values if scoring_form(value) in accepted)
