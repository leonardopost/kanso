"""The four task classes: their prompts, their schemas and the stability of the prefix."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from kanso.models import ANSWER_SCHEMAS, INSTRUCTIONS, CallInputs, build, canonical, validate
from kanso.models.tasks import PARAM_PAIRS, collapse
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


def test_the_classify_instruction_states_the_keep_rule_bounds_the_wire_no_longer_can() -> None:
    """The two bounds left the schema when a provider refused the keyword that carried them.

    `minimum` is not a keyword a provider accepts, so `min_delta` and `k_se` go out as bare
    numbers and the only place a model is told their range is the instruction. Prose is not
    machine-checked, so this reads the range off `ObjectiveParams` itself: a bound loosened
    or tightened in the data model and not mirrored here fails, and so does deleting the
    sentence, which would otherwise leave the model told neither bound by anything.
    """
    from kanso.schemas.hypothesis import ObjectiveParams

    bounds = {
        name: {
            key: getattr(meta, key)
            for meta in field.metadata
            for key in ("ge", "gt")
            if hasattr(meta, key)
        }
        for name, field in ObjectiveParams.model_fields.items()
    }
    assert bounds["min_delta"] == {"ge": 0} and bounds["k_se"] == {"gt": 0}, (
        "the keep rule's bounds moved; the instruction below must move with them"
    )
    said = INSTRUCTIONS["classify"]
    assert "`min_delta` is zero or more" in said
    assert "`k_se` is above zero" in said
    assert "the schema states no numeric range" in said, (
        "the instruction must say why it carries the bounds, or a reader will put them back "
        "on the wire and earn a 400"
    )


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


# --- the parameter map, on the wire and back -----------------------------------

CLASSIFY: dict[str, Any] = ANSWER_SCHEMAS["classify"]


def classification(construct_params: Any, constraint_params: Any) -> dict[str, Any]:
    """A classify answer carrying those two parameter values, whatever shape they are."""
    return {
        "construct": {"id": "sleeve", "params": construct_params},
        "objective_params": {"min_delta": 0.0, "k_se": 1.0},
        "constraints": [{"id": "min_trades", "params": constraint_params}],
        "rationale": "a sleeve",
    }


def test_a_parameter_list_is_read_back_as_the_mapping_every_step_expects() -> None:
    """The pairs are transport: nothing past the router has ever seen one."""
    answer = classification(
        [{"name": "scope", "value": "time"}],
        [{"name": "min", "value": 30}, {"name": "strict", "value": True}],
    )
    assert validate(answer, CLASSIFY) == []

    collapsed, complaints = collapse(answer, CLASSIFY)

    assert complaints == []
    assert collapsed["construct"] == {"id": "sleeve", "params": {"scope": "time"}}
    assert collapsed["constraints"] == [{"id": "min_trades", "params": {"min": 30, "strict": True}}]
    assert collapsed["rationale"] == "a sleeve"


def test_an_empty_parameter_list_reads_back_as_an_empty_mapping() -> None:
    collapsed, complaints = collapse(classification([], []), CLASSIFY)
    assert complaints == []
    assert collapsed["construct"] == {"id": "sleeve", "params": {}}


def test_a_name_given_twice_is_a_complaint_rather_than_a_quiet_winner() -> None:
    """kanso has no rule saying the first wins, so the model is asked again instead."""
    answer = classification([], [{"name": "min", "value": 30}, {"name": "min", "value": 40}])
    assert validate(answer, CLASSIFY) == []

    _, complaints = collapse(answer, CLASSIFY)

    assert complaints == [
        "the answer.constraints[0].params: 'min' is named twice; give each parameter once"
    ]


def test_a_value_the_schema_says_nothing_about_is_carried_through() -> None:
    """Collapsing reads the parameter lists and rewrites nothing else."""
    collapsed, complaints = collapse({"kept": [1, 2], "also": {"a": "b"}}, {})
    assert (collapsed, complaints) == ({"kept": [1, 2], "also": {"a": "b"}}, [])


def test_a_parameter_value_that_is_not_a_list_of_pairs_is_left_where_it_is() -> None:
    """Only a caller that skipped the schema can produce one, and the schema reports it."""
    schema: dict[str, Any] = {"type": "object", "properties": {"params": PARAM_PAIRS}}
    assert collapse({"params": "not a list"}, schema) == ({"params": "not a list"}, [])
    assert collapse({"params": [7]}, schema) == ({"params": [7]}, [])


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.one_of(st.integers(), st.text(max_size=8), st.booleans()),
        max_size=6,
    )
)
def test_a_parameter_mapping_survives_the_wire_shape_whole(params: dict[str, Any]) -> None:
    """What the model decided is what the step receives: every name, every type."""
    pairs = [{"name": name, "value": value} for name, value in params.items()]
    answer = classification(pairs, [])
    assert validate(answer, CLASSIFY) == []
    collapsed, complaints = collapse(answer, CLASSIFY)
    construct = collapsed["construct"]
    assert isinstance(construct, dict)
    assert construct["params"] == params
    assert complaints == []


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
