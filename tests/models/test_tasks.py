"""The four task classes: their prompts, their schemas and the stability of the prefix."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from kanso.models import ANSWER_SCHEMAS, INSTRUCTIONS, CallInputs, build, canonical, validate
from kanso.schemas.models import ROUTING_DEFAULTS, TASK_CLASSES, Route, TaskClass
from kanso.workspace import TEMPLATES

ROUTE = Route(tier="mid", effort="medium", max_output=4096)


def inputs(**changes: Any) -> CallInputs:
    base: dict[str, Any] = {
        "subject": "demo_mr",
        "stable": {"thesis": "reverts within four sessions", "horizon": "1d"},
        "dynamic": {"cards": []},
    }
    base.update(changes)
    return CallInputs(**base)


def test_every_task_class_has_an_instruction_and_a_schema() -> None:
    assert set(INSTRUCTIONS) == set(TASK_CLASSES)
    assert set(ANSWER_SCHEMAS) == set(TASK_CLASSES)


def test_no_prompt_or_schema_mentions_the_spec() -> None:
    """Nothing that outlives the build may point at the document that drove it."""
    for text in INSTRUCTIONS.values():
        assert "SPEC" not in text


def test_the_system_turn_is_byte_stable_across_a_subject() -> None:
    first = build("propose", ROUTE, inputs(dynamic={"cards": [1]}))
    second = build("propose", ROUTE, inputs(dynamic={"cards": [2, 3], "crash": "boom"}))
    assert first.system == second.system
    assert first.user != second.user


def test_the_stable_half_is_order_independent() -> None:
    """A mapping built in a different order must render the same bytes, or no cache hits."""
    one = build("classify", ROUTE, inputs(stable={"a": 1, "b": 2}))
    two = build("classify", ROUTE, inputs(stable={"b": 2, "a": 1}))
    assert one.system == two.system


def test_the_call_carries_the_route_it_was_built_from() -> None:
    call = build("align_check", ROUTING_DEFAULTS["align_check"], inputs())
    assert (call.tier, call.effort, call.max_output) == ("cheap", "none", 256)
    assert call.task_class == "align_check"


def test_the_system_turn_holds_the_instruction_the_schema_and_the_subject() -> None:
    call = build("classify", ROUTE, inputs())
    assert INSTRUCTIONS["classify"] in call.system
    assert '"objective_params"' in call.system
    assert "demo_mr" in call.system
    assert "reverts within four sessions" in call.system


def test_an_empty_dynamic_half_still_asks_for_an_answer() -> None:
    call = build("classify", ROUTE, inputs(dynamic={}))
    assert call.user == "Answer for the facts already given."


def test_retrying_appends_the_complaints_and_leaves_the_prefix_alone() -> None:
    call = build("propose", ROUTE, inputs())
    retry = call.retrying(["the answer: 'diff' is required and missing"])
    assert retry.system == call.system
    assert retry.user.startswith(call.user)
    assert "'diff' is required and missing" in retry.user
    assert (retry.tier, retry.effort, retry.max_output) == (
        call.tier,
        call.effort,
        call.max_output,
    )


def test_escalating_changes_the_tier_and_nothing_else() -> None:
    call = build("propose", ROUTE, inputs())
    higher = call.at("frontier")
    assert higher.tier == "frontier"
    assert (higher.system, higher.user, higher.effort, higher.max_output) == (
        call.system,
        call.user,
        call.effort,
        call.max_output,
    )


def test_canonical_renders_a_date_rather_than_failing() -> None:
    assert canonical({"start": date(2024, 1, 2)}) == '{\n  "start": "2024-01-02"\n}'


@pytest.mark.parametrize("task", TASK_CLASSES)
def test_a_dated_value_anywhere_in_the_facts_still_builds(task: TaskClass) -> None:
    call = build(task, ROUTE, inputs(stable={"window": {"start": date(2024, 1, 1)}}))
    assert "2024-01-01" in call.system


def test_the_shipped_demo_script_answers_every_class_it_lists() -> None:
    """The demo must classify, plan, align and propose with no provider and no network."""
    script = yaml.safe_load((TEMPLATES / "demo" / "responses.yaml").read_text(encoding="utf-8"))
    assert set(script) <= set(TASK_CLASSES)
    for task, answers in script.items():
        for index, answer in enumerate(answers):
            assert validate(answer, ANSWER_SCHEMAS[task]) == [], f"{task}[{index}]"


@given(st.dictionaries(st.text(max_size=8), st.integers(), max_size=6))
def test_canonical_is_the_same_bytes_for_the_same_content(mapping: dict[str, int]) -> None:
    shuffled = dict(reversed(list(mapping.items())))
    assert canonical(mapping) == canonical(shuffled)
