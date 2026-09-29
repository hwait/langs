"""Normalization used for *identity*, never for meaning.

Two learner-facing questions need to know when two strings are the same string: is this
alias the one being searched for, and is this mistake the one seen before. Both answers
have to hold for a language that inflects and separates words with spaces and for one
that does neither, so nothing here tokenizes, splits, transliterates, or strips a script.

`fold` is the shared floor: canonical composition, then case folding. `normalize_alias`
is exactly that, because an alias is a form the learner might type and its punctuation is
part of it. `normalize_identity` goes further and drops marks, punctuation, and
separators, which is what makes two spellings of the same mistake one mistake.

Tone marks and diacritics survive `normalize_identity`, because composition turns them
into letters before anything is stripped. That is deliberate: in a tonal language the
tone is the word, and folding it away would merge two different errors into one.
"""

from __future__ import annotations

import re
import unicodedata

#: Unicode general-category prefixes dropped by `normalize_identity`: marks that did not
#: compose into a letter, punctuation, separators, and control characters.
NON_IDENTITY_CATEGORIES: tuple[str, ...] = ("M", "P", "Z", "C")


def fold(value: str) -> str:
    """Compose and case-fold, the one normalization every comparison starts from."""

    return unicodedata.normalize("NFKC", value).casefold()


#: Runs of Unicode whitespace, for collapsing rather than for splitting.
WHITESPACE_RUN = re.compile(r"\s+")


def normalize_response(value: str) -> str:
    """The form two *answers* must agree on: `fold`, with whitespace runs collapsed.

    Looser than identity -- a learner who typed two spaces did not give a different
    answer -- and no looser: no marks dropped, no punctuation stripped. This *collapses*
    whitespace rather than tokenizing on it, which is why it belongs in the learner model
    at all: a language that writes without spaces has no run to collapse and gets its
    input back unchanged.
    """

    return WHITESPACE_RUN.sub(" ", fold(value)).strip()


def normalize_alias(value: str) -> str:
    """The stored form of a searchable alias.

    Deliberately no more than `fold`: an alias is something a learner types, and
    collapsing its punctuation would make two distinguishable forms one entry.
    """

    return fold(value)


def normalize_identity(value: str) -> str:
    """The form two occurrences of the same thing must agree on.

    Marks, punctuation, and separators go; letters, digits, and symbols stay in order.
    An empty result means the input carried no identity at all, which callers refuse
    rather than store.
    """

    composed = fold(value)
    return "".join(
        character
        for character in composed
        if unicodedata.category(character)[0] not in NON_IDENTITY_CATEGORIES
    )


__all__ = [
    "NON_IDENTITY_CATEGORIES",
    "WHITESPACE_RUN",
    "fold",
    "normalize_alias",
    "normalize_identity",
    "normalize_response",
]
