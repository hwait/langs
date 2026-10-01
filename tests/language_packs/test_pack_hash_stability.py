"""A pack nobody edited keeps the hashes it shipped with.

`pack_item_hash` dumps the payload *whole*, deliberately, so a field added later is
covered without anyone remembering to cover it. The cost is that adding an optional
field with a `None` default writes `"<field>": null` into the canonical payload of every
item that does not use it -- moving the hash of content nobody touched, failing
`pack validate` for packs this repository does not own, and detaching reviews from text
that was reviewed. That is the false positive the hash exists to avoid.

These tests are the guard. They are characterization tests: they pass today, and they are
here to fail the moment a contract change moves a hash it had no business moving.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from linguawiki.packs.format import load_pack
from tests.conftest import FIXTURE_PACKS, PILOT_PACK

#: One task hash per shipped pack, pinned as a literal. A derived value compared against
#: another derived value proves only that two computations agree; a literal is what
#: notices that both of them moved.
PINNED_TASK_HASHES: tuple[tuple[Path, str, str], ...] = (
    (
        FIXTURE_PACKS / "inflected",
        "qix.task.read-1",
        "b6f186e5fd96015f2b25c39a6eb39f524f7083476380c3d28368652c908e9e5b",
    ),
    (
        FIXTURE_PACKS / "tonal",
        "ztx.task.read-1",
        "3b4da24d9acb76df40894dfdfc7d77637ef33d99a49542be62f4c9db79e9f000",
    ),
)


def _task_hashes(root: Path) -> dict[str, str]:
    return {
        item.stable_key: item.content_hash
        for item in load_pack(root).items
        if item.content_kind == "assessment_task"
    }


@pytest.mark.parametrize(
    "root", [PILOT_PACK, FIXTURE_PACKS / "inflected", FIXTURE_PACKS / "tonal"], ids=lambda p: p.name
)
def test_shipped_pack_validates_with_the_hashes_it_declares(root: Path) -> None:
    """`load_pack` verifies each item's declared hash against the derived one."""

    loaded = load_pack(root)
    drifted = [
        item.stable_key
        for item in loaded.items
        if item.declared_hash is not None and item.declared_hash != item.content_hash
    ]
    assert drifted == [], f"{root.name} needs a re-stamp it should not need: {drifted}"


@pytest.mark.parametrize(
    ("root", "stable_key", "expected"), PINNED_TASK_HASHES, ids=lambda v: getattr(v, "name", v)
)
def test_unedited_fixture_task_keeps_its_pinned_hash(
    root: Path, stable_key: str, expected: str
) -> None:
    """Neither fixture pack is re-authored, so neither task's hash may move.

    When a deliberate hash-schema change makes this fail, the fix is to re-stamp the
    fixture packs *and* update the literal here in the same commit -- not to relax the
    assertion.
    """

    assert _task_hashes(root)[stable_key] == expected
