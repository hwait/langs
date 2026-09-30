"""Every presentation refusal reaches `pack validate` under its own name.

Codes are part of the contract. An author or a skill that can only see
`pack_contract_invalid` cannot tell "this bank has two right answers" from "this task
carries choices it should not" from "a recording is missing", and the way that breaks
again is somebody matching on the error *prose* to recover the distinction.

These assert the public refusal, through `load_pack`, rather than the pydantic error the
model raises on the way there.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from linguawiki.packs.format import PackError, load_pack
from tests.conftest import PILOT_PACK
from tests.language_packs.support import republish

CHOOSER = "pl.task.grammar-control.04"
Mutation = Callable[[dict[str, Any]], None]


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    target = tmp_path / "pl-pilot"
    shutil.copytree(PILOT_PACK, target)
    return target


def _edit(root: Path, stable_key: str, mutate: Mutation) -> None:
    path = root / "assessments" / "pl-a2-calibration.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for task in document["tasks"]:
        if task["stable_key"] == stable_key:
            mutate(task)
            break
    else:  # pragma: no cover - a typo in the test, not a behaviour
        raise AssertionError(f"{stable_key} is not in the pilot form")
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    republish(root)


@pytest.mark.parametrize(
    ("stable_key", "mutate", "code"),
    [
        (
            CHOOSER,
            lambda task: task.update(expected={"answers": ["nothing any button says"]}),
            "pack_choices_do_not_answer",
        ),
        (
            CHOOSER,
            lambda task: task.update(expected={"answers": ["pięć biletów", "pięć biletu"]}),
            "pack_choices_do_not_answer",
        ),
        (
            CHOOSER,
            lambda task: task["presentation"]["choices"].append({"value": "PIĘĆ  BILETÓW "}),
            "pack_choice_values_invalid",
        ),
        (
            CHOOSER,
            lambda task: task["presentation"]["choices"].append({"value": "   "}),
            "pack_choice_values_invalid",
        ),
        (
            CHOOSER,
            lambda task: task["presentation"].update(choices=task["presentation"]["choices"][:1]),
            "pack_presentation_mismatched",
        ),
        (
            "pl.task.writing.01",
            lambda task: task.update(
                presentation={
                    "kind": "multiple-choice",
                    "choices": [{"value": "a"}, {"value": "b"}],
                }
            ),
            "pack_presentation_mismatched",
        ),
        (
            CHOOSER,
            lambda task: task["presentation"].update(audio={"asset_key": "pl.audio.one"}),
            "pack_presentation_mismatched",
        ),
        (
            "pl.task.listening.01",
            lambda task: task.update(presentation={"kind": "free-text", "order": "shuffled"}),
            "pack_presentation_mismatched",
        ),
    ],
    ids=[
        "no-choice-answers-the-key",
        "two-choices-answer-the-key",
        "choice-values-collide-when-folded",
        "blank-choice-value",
        "one-button-is-not-a-choice",
        "choices-on-a-rubric-scored-task",
        "a-text-task-plays-nothing",
        "nothing-to-shuffle",
    ],
)
def test_each_presentation_defect_is_refused_by_its_own_name(
    pack: Path, stable_key: str, mutate: Mutation, code: str
) -> None:
    _edit(pack, stable_key, mutate)

    with pytest.raises(PackError) as failure:
        load_pack(pack)

    assert failure.value.payload.code == code
    assert failure.value.payload.details, "a refusal has to say which rule failed"


def test_a_defect_the_presentation_rules_do_not_name_stays_generic(pack: Path) -> None:
    """The distinct codes are for the rules C2a named, not a replacement for the rest."""

    _edit(pack, CHOOSER, lambda task: task.update(difficulty="not a number"))

    with pytest.raises(PackError) as failure:
        load_pack(pack)
    assert failure.value.payload.code == "pack_contract_invalid"
