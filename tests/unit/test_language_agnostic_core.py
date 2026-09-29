"""The core learner model may not know which language it is modelling.

The plan's rule is that core code must not branch on a language code except inside a
registered adapter. This file enforces it by reading the source of the modules the
learner model is made of: the aggregation, the claim vocabulary, the error model, the
identity normalization, and the services that write learner state.

A source-text check is blunt, but it is the only kind that catches the defect this rule
exists to prevent -- a rule that works for Polish and quietly does the wrong thing for a
language with no whitespace, no inflection, or no alphabet.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "linguawiki"

#: The modules that decide what a learner knows. Nothing here may consult a language.
AGGREGATION_MODULES = (
    "mastery.py",
    "evidence.py",
    "error_model.py",
    "text.py",
    "placement.py",
    "services/evidence.py",
    "services/errors.py",
    "services/estimates.py",
    "services/knowledge.py",
    "services/context.py",
)

#: Language codes that must never appear as a literal being compared against. `pl` and
#: `zh` are the two the plan names; the fixture packs use two more that no rule may know.
LANGUAGE_CODES = ("pl", "zh", "qix", "ztx", "pl-PL", "zh-Hans", "cmn")
#: Scripts and linguistic structures a generic rule must not assume either.
STRUCTURAL_ASSUMPTIONS = (
    "Latn",
    "Hans",
    "Hant",
    "Cyrl",
    "hanzi",
    "pinyin",
    "declension",
    "conjugation",
)


def module_source(relative: str) -> str:
    return (SOURCE / relative).read_text(encoding="utf-8")


def code_only(relative: str) -> str:
    """The module's source with every docstring and comment removed.

    Prose may name Polish and Chinese -- explaining *why* a rule is generic usually
    requires it. Only executable code is checked, so the docstring line ranges are
    blanked out rather than searched for as text.
    """

    text = module_source(relative)
    lines = text.splitlines()
    blanked = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            blanked.update(range(first.lineno, first.end_lineno + 1))
    return "\n".join(
        "" if number in blanked else re.sub(r"#.*$", "", line)
        for number, line in enumerate(lines, start=1)
    )


@pytest.mark.parametrize("relative", AGGREGATION_MODULES)
def test_no_aggregation_module_branches_on_a_language_code(relative: str) -> None:
    code = code_only(relative)

    for token in LANGUAGE_CODES:
        for pattern in (f'"{token}"', f"'{token}'"):
            assert pattern not in code, f"{relative} compares against the language {token}"


@pytest.mark.parametrize("relative", AGGREGATION_MODULES)
def test_no_aggregation_module_assumes_a_script_or_a_grammar(relative: str) -> None:
    code = code_only(relative)

    for token in STRUCTURAL_ASSUMPTIONS:
        assert token.lower() not in code.lower(), f"{relative} assumes the structure {token}"


@pytest.mark.parametrize("relative", AGGREGATION_MODULES)
def test_no_aggregation_module_tokenizes_on_whitespace(relative: str) -> None:
    """A language that does not separate words must aggregate identically to one that does."""

    code = code_only(relative)

    for pattern in (".split()", '.split(" ")', ".rsplit()", "shlex.split"):
        assert pattern not in code, f"{relative} splits text on whitespace"


def test_identity_normalization_is_the_only_place_that_folds_text() -> None:
    """One shared floor, so alias search and error dedup cannot drift apart."""

    from linguawiki import text as text_module

    assert text_module.normalize_alias("Dworzec") == text_module.fold("Dworzec")
    assert text_module.normalize_identity("Dworzec!") == "dworzec"

    for relative in AGGREGATION_MODULES:
        code = code_only(relative)
        if relative in ("text.py",):
            continue
        assert ".casefold()" not in code, (
            f"{relative} folds case itself instead of using linguawiki.text"
        )


def test_the_mastery_and_claim_vocabularies_name_no_linguistic_category() -> None:
    """A stage and a claim describe demand on the learner, not a part of speech."""

    from linguawiki.evidence import CLAIMS, TASK_TYPES
    from linguawiki.mastery import STAGES

    forbidden = ("noun", "verb", "case", "tone", "gender", "aspect", "character")
    vocabulary = " ".join((*STAGES, *CLAIMS, *TASK_TYPES)).lower()

    for token in forbidden:
        assert token not in vocabulary, f"the vocabulary names the category {token}"


@pytest.mark.parametrize("relative", AGGREGATION_MODULES)
def test_the_stripped_source_is_still_the_module_s_code(relative: str) -> None:
    """Guards the guard: a checker that blanked everything would pass every check."""

    code = code_only(relative)

    assert "def " in code
    assert "from linguawiki" in code or "import " in code
    assert len(code.strip()) > len(module_source(relative)) // 4
