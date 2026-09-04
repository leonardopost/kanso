"""The mock protocol: the script, the cursor, the wrap-around and the unlisted class."""

from __future__ import annotations

import pytest

from kanso.errors import PreconditionError, ValidationError
from kanso.models import Call, MockClient, read_register, reset_mock
from kanso.workspace import Workspace

from .conftest import ALIGNED, PROPOSED, write_script

SCHEMA: dict[str, object] = {"type": "object"}


def call(task: str = "align_check") -> Call:
    return Call(
        task_class=task,
        tier="cheap",
        effort="none",
        max_output=256,
        system="stable",
        user="dynamic",
        schema=SCHEMA,
    )


def spec_for(ws: Workspace, model_id: str = "cheap_mock") -> object:
    return next(m for m in read_register(ws).models if m.id == model_id)


def answers(ws: Workspace, task: str, n: int, model_id: str = "cheap_mock") -> list[object]:
    client = MockClient(ws.root)
    spec = spec_for(ws, model_id)
    return [client.complete(spec, call(task)).data for _ in range(n)]  # type: ignore[arg-type]


def test_answers_come_out_in_script_order(ws: Workspace) -> None:
    write_script(ws, "cheap_mock", {"align_check": [{"n": 1}, {"n": 2}, {"n": 3}]})
    assert answers(ws, "align_check", 3) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_the_cursor_wraps_around(ws: Workspace) -> None:
    write_script(ws, "cheap_mock", {"align_check": [{"n": 1}, {"n": 2}]})
    assert answers(ws, "align_check", 5) == [{"n": 1}, {"n": 2}, {"n": 1}, {"n": 2}, {"n": 1}]


def test_each_task_class_has_its_own_cursor(ws: Workspace) -> None:
    write_script(
        ws,
        "cheap_mock",
        {"align_check": [{"a": 1}, {"a": 2}], "propose": [{"p": 1}, {"p": 2}]},
    )
    client = MockClient(ws.root)
    spec = spec_for(ws)
    seen = [
        client.complete(spec, call("align_check")).data,  # type: ignore[arg-type]
        client.complete(spec, call("propose")).data,  # type: ignore[arg-type]
        client.complete(spec, call("align_check")).data,  # type: ignore[arg-type]
        client.complete(spec, call("propose")).data,  # type: ignore[arg-type]
    ]
    assert seen == [{"a": 1}, {"p": 1}, {"a": 2}, {"p": 2}]


def test_a_new_client_shares_the_cursor_within_one_process(ws: Workspace) -> None:
    """The cursor belongs to the process, not to the object; a lane is a process."""
    write_script(ws, "cheap_mock", {"align_check": [{"n": 1}, {"n": 2}]})
    spec = spec_for(ws)
    first = MockClient(ws.root).complete(spec, call()).data  # type: ignore[arg-type]
    second = MockClient(ws.root).complete(spec, call()).data  # type: ignore[arg-type]
    assert (first, second) == ({"n": 1}, {"n": 2})


def test_resetting_the_cursors_starts_the_script_over(ws: Workspace) -> None:
    write_script(ws, "cheap_mock", {"align_check": [{"n": 1}, {"n": 2}]})
    assert answers(ws, "align_check", 1) == [{"n": 1}]
    reset_mock()
    assert answers(ws, "align_check", 1) == [{"n": 1}]


def test_two_models_walk_their_own_scripts(ws: Workspace) -> None:
    write_script(ws, "cheap_mock", {"align_check": [{"from": "cheap"}]})
    write_script(ws, "mid_mock", {"align_check": [{"from": "mid"}]})
    assert answers(ws, "align_check", 1) == [{"from": "cheap"}]
    assert answers(ws, "align_check", 1, "mid_mock") == [{"from": "mid"}]


def test_an_unlisted_class_answers_an_empty_object(ws: Workspace) -> None:
    """Which fails every task schema, so an unlisted class takes the normal ladder."""
    write_script(ws, "cheap_mock", {"propose": [PROPOSED]})
    assert answers(ws, "align_check", 2) == [{}, {}]


def test_an_empty_script_answers_an_empty_object(ws: Workspace) -> None:
    ws.path("mock", "cheap_mock.yaml").write_text("", encoding="utf-8")
    assert answers(ws, "align_check", 1) == [{}]


def test_the_answer_carries_the_model_the_tier_and_a_deterministic_cost(ws: Workspace) -> None:
    write_script(ws, "cheap_mock", {"align_check": [ALIGNED]})
    client = MockClient(ws.root)
    answer = client.complete(spec_for(ws), call())  # type: ignore[arg-type]
    assert (answer.model, answer.tier, answer.cache_hit) == ("cheap_mock", "cheap", None)
    assert answer.tokens_in == (len("stable") + len("dynamic")) // 4
    assert answer.tokens_out > 0
    assert answer.cost == pytest.approx(
        (answer.tokens_in * 3.0 + answer.tokens_out * 15.0) / 1_000_000
    )


def test_a_missing_script_refuses_and_names_the_path(ws: Workspace) -> None:
    ws.path("mock", "cheap_mock.yaml").unlink()
    with pytest.raises(PreconditionError, match="does not exist"):
        answers(ws, "align_check", 1)


def test_a_script_that_is_not_a_mapping_is_refused(ws: Workspace) -> None:
    ws.path("mock", "cheap_mock.yaml").write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="mapping of task class"):
        answers(ws, "align_check", 1)


def test_a_class_whose_answers_are_not_a_list_is_refused(ws: Workspace) -> None:
    ws.path("mock", "cheap_mock.yaml").write_text("align_check: {a: 1}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="expected a list of answers"):
        answers(ws, "align_check", 1)


def test_an_answer_that_is_not_a_mapping_is_refused(ws: Workspace) -> None:
    ws.path("mock", "cheap_mock.yaml").write_text("align_check: [3]\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="every answer must be a mapping"):
        answers(ws, "align_check", 1)


def test_broken_yaml_is_refused_rather_than_half_read(ws: Workspace) -> None:
    ws.path("mock", "cheap_mock.yaml").write_text("align_check: [ {a: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="cannot be read as YAML"):
        answers(ws, "align_check", 1)


def test_a_rewritten_script_is_seen_without_disturbing_the_cursor(ws: Workspace) -> None:
    write_script(ws, "cheap_mock", {"align_check": [{"n": 1}, {"n": 2}]})
    assert answers(ws, "align_check", 1) == [{"n": 1}]
    write_script(ws, "cheap_mock", {"align_check": [{"n": 10}, {"n": 20}]})
    assert answers(ws, "align_check", 1) == [{"n": 20}]
