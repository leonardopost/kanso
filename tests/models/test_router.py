"""The router: the ladder, the ledger of every attempt, and what it refuses to send."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kanso.errors import Exit, PreconditionError, ValidationError
from kanso.models import Call, CallInputs, Client, client_for, route
from kanso.models import router as router_module
from kanso.models.anthropic import AnthropicClient
from kanso.models.mock import MockClient
from kanso.models.openai_compat import OpenAICompatClient
from kanso.schemas.models import ModelSpec
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import (
    ALIGNED,
    CLASSIFIED,
    PLANNED,
    PROPOSED,
    credentialled,
    model,
    write_register,
    write_script,
    write_scripts,
)

BAD: dict[str, Any] = {"nonsense": True}
"""An answer no task class's schema accepts."""

INPUTS = CallInputs(subject="demo_mr", stable={"thesis": "reverts"}, dynamic={"cards": 3})


@dataclass
class Watched:
    """Every call the router actually made, in order."""

    calls: list[tuple[str, Call]] = field(default_factory=list)

    @property
    def models(self) -> list[str]:
        return [model_id for model_id, _ in self.calls]

    @property
    def tiers(self) -> list[str]:
        return [call.tier for _, call in self.calls]


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> Iterator[Watched]:
    """Wrap the router's own client factory so a test can read the calls it made."""
    seen = Watched()

    def spy(root: Path, spec: ModelSpec) -> Client:
        inner = client_for(root, spec)

        class Recording:
            protocol = inner.protocol

            def complete(self, spec: ModelSpec, call: Call) -> Any:
                seen.calls.append((spec.id, call))
                return inner.complete(spec, call)

        return Recording()

    monkeypatch.setattr(router_module, "client_for", spy)
    yield seen


def rows(store: StateStore) -> list[dict[str, Any]]:
    """The spend ledger, oldest first."""
    found = store.connection.execute("SELECT * FROM spend ORDER BY spend_id").fetchall()
    return [dict(row) for row in found]


# -- the happy path ------------------------------------------------------------------


def test_a_usable_answer_on_the_routed_tier_is_one_call(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    answer = route(ws, store, "align_check", INPUTS)
    assert answer.data == ALIGNED
    assert answer.tier == "cheap"
    assert watched.models == ["cheap_mock"]
    assert len(rows(store)) == 1


@pytest.mark.parametrize(
    ("task", "tier", "answer", "effort", "cap"),
    [
        ("classify", "frontier", CLASSIFIED, "high", 1024),
        ("certify_plan", "frontier", PLANNED, "high", 4096),
        ("propose", "mid", PROPOSED, "medium", 4096),
        ("align_check", "cheap", ALIGNED, "none", 256),
    ],
)
def test_each_task_class_is_routed_to_its_own_tier_effort_and_cap(
    ws: Workspace,
    store: StateStore,
    watched: Watched,
    task: str,
    tier: str,
    answer: dict[str, Any],
    effort: str,
    cap: int,
) -> None:
    write_scripts(ws, **{tier: {task: [answer]}})
    route(ws, store, task, INPUTS)
    _, call = watched.calls[0]
    assert (call.tier, call.effort, call.max_output) == (tier, effort, cap)


def test_the_routing_table_overrides_a_default(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    write_register(ws, routing={"align_check": {"tier": "frontier", "max_output": 99}})
    write_scripts(ws, frontier={"align_check": [ALIGNED]})
    route(ws, store, "align_check", INPUTS)
    _, call = watched.calls[0]
    assert (call.tier, call.effort, call.max_output) == ("frontier", "none", 99)


# -- the ladder ----------------------------------------------------------------------


def test_a_rejected_answer_is_retried_once_on_the_same_model_with_the_complaints(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    write_scripts(ws, cheap={"align_check": [BAD, ALIGNED]})
    answer = route(ws, store, "align_check", INPUTS)
    assert answer.data == ALIGNED
    assert watched.models == ["cheap_mock", "cheap_mock"]
    first, retry = (call for _, call in watched.calls)
    assert retry.system == first.system, "the cached prefix must survive the retry"
    assert retry.user.startswith(first.user)
    assert "'aligned' is required and missing" in retry.user
    assert "'nonsense' is not a field of this object" in retry.user


def test_a_second_rejection_escalates_one_tier_at_the_same_effort(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    write_scripts(ws, cheap={"align_check": [BAD]}, mid={"align_check": [ALIGNED]})
    answer = route(ws, store, "align_check", INPUTS)
    assert answer.data == ALIGNED
    assert answer.tier == "mid"
    assert watched.models == ["cheap_mock", "cheap_mock", "mid_mock"]
    assert watched.tiers == ["cheap", "cheap", "mid"]
    assert {call.effort for _, call in watched.calls} == {"none"}


def test_the_escalated_call_is_not_primed_with_the_earlier_models_mistakes(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    write_scripts(ws, cheap={"align_check": [BAD]}, mid={"align_check": [ALIGNED]})
    route(ws, store, "align_check", INPUTS)
    first, _, escalated = (call for _, call in watched.calls)
    assert escalated.user == first.user
    assert escalated.system == first.system


def test_escalation_climbs_one_tier_and_never_skips(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    write_scripts(
        ws,
        cheap={"align_check": [BAD]},
        mid={"align_check": [BAD]},
        frontier={"align_check": [ALIGNED]},
    )
    with pytest.raises(PreconditionError):
        route(ws, store, "align_check", INPUTS)
    assert watched.models == ["cheap_mock", "cheap_mock", "mid_mock"]
    assert "frontier_mock" not in watched.models


def test_a_class_routed_to_the_top_tier_makes_exactly_two_attempts(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    """There is no tier above `frontier`, so the escalation step is skipped."""
    write_scripts(ws, frontier={"classify": [BAD]})
    with pytest.raises(PreconditionError) as caught:
        route(ws, store, "classify", INPUTS)
    assert watched.models == ["frontier_mock", "frontier_mock"]
    assert len(rows(store)) == 2
    assert caught.value.code == Exit.PRECONDITION
    assert "2 attempts" in caught.value.message


def test_a_ladder_that_runs_out_names_what_was_tried_and_why_it_failed(
    ws: Workspace, store: StateStore
) -> None:
    write_scripts(ws, mid={"propose": [BAD]}, frontier={"propose": [BAD]})
    with pytest.raises(PreconditionError) as caught:
        route(ws, store, "propose", INPUTS)
    assert "mid_mock on mid" in caught.value.message
    assert "frontier_mock on frontier" in caught.value.message
    assert "'desc' is required and missing" in str(caught.value.remedy)


def test_an_unlisted_task_class_in_the_script_takes_the_whole_ladder(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    """An empty answer is not a special case; it fails the schema like any other."""
    write_scripts(ws, cheap={"propose": [PROPOSED]}, mid={"align_check": [ALIGNED]})
    answer = route(ws, store, "align_check", INPUTS)
    assert answer.tier == "mid"
    assert len(watched.calls) == 3


# -- the ledger ----------------------------------------------------------------------


def test_every_attempt_is_ledgered_including_the_failed_ones(
    ws: Workspace, store: StateStore
) -> None:
    write_scripts(ws, cheap={"align_check": [BAD]}, mid={"align_check": [ALIGNED]})
    route(ws, store, "align_check", INPUTS, lane="a")
    ledgered = rows(store)
    assert [row["model"] for row in ledgered] == ["cheap_mock", "cheap_mock", "mid_mock"]
    assert {row["task_class"] for row in ledgered} == {"align_check"}
    assert {row["lane"] for row in ledgered} == {"a"}
    assert all(row["tokens_in"] > 0 for row in ledgered)
    assert all(row["cost"] > 0 for row in ledgered)


def test_the_lane_defaults_to_the_interactive_one(ws: Workspace, store: StateStore) -> None:
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    route(ws, store, "align_check", INPUTS)
    assert rows(store)[0]["lane"] == "op"


# -- refusals ------------------------------------------------------------------------


def test_a_tier_with_no_model_refuses_before_any_call_is_made(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    write_register(ws, [model("cheap_mock", "cheap"), model("mid_mock", "mid")])
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    with pytest.raises(PreconditionError) as caught:
        route(ws, store, "align_check", INPUTS)
    assert "frontier" in caught.value.message
    assert caught.value.code == Exit.PRECONDITION
    assert watched.calls == []
    assert rows(store) == []


def test_a_workspace_with_no_register_refuses(ws: Workspace, store: StateStore) -> None:
    ws.path("models.yaml").unlink()
    with pytest.raises(PreconditionError, match="models.yaml"):
        route(ws, store, "align_check", INPUTS)


def test_a_name_that_is_not_a_task_class_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(ValidationError) as caught:
        route(ws, store, "summarise", INPUTS)
    assert caught.value.code == Exit.VALIDATION
    assert "align_check" in str(caught.value.remedy)


# -- the caller's own check ----------------------------------------------------------


def test_the_callers_check_puts_a_wrong_answer_on_the_same_ladder(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    """A construct id that is not in the catalogue is as correctable as a missing field."""
    write_scripts(
        ws,
        frontier={
            "classify": [
                {**CLASSIFIED, "construct": {"id": "no_such_construct"}},
                CLASSIFIED,
            ]
        },
    )

    def catalogue(data: Mapping[str, object]) -> Sequence[str]:
        construct = data["construct"]
        assert isinstance(construct, Mapping)
        if construct["id"] != "sleeve":
            return [f"construct.id: {construct['id']!r} is not in the catalogue"]
        return []

    answer = route(ws, store, "classify", CallInputs(subject="demo_mr", check=catalogue))
    assert answer.data == {
        **CLASSIFIED,
        "constraints": [{"id": "strategy_integrity", "params": {}}],
    }
    assert len(watched.calls) == 2
    assert "not in the catalogue" in watched.calls[1][1].user


def test_the_callers_check_runs_only_on_an_answer_the_schema_accepted(
    ws: Workspace, store: StateStore
) -> None:
    write_scripts(ws, frontier={"classify": [BAD]})
    seen: list[Mapping[str, object]] = []

    def never(data: Mapping[str, object]) -> Sequence[str]:
        seen.append(data)
        return []

    with pytest.raises(PreconditionError):
        route(ws, store, "classify", CallInputs(subject="demo_mr", check=never))
    assert seen == []


# -- the wire shape ------------------------------------------------------------------


def scoped(*params: dict[str, Any]) -> dict[str, Any]:
    """The usable classification, with those parameters on its one constraint."""
    return {**CLASSIFIED, "constraints": [{"id": "min_trades", "params": list(params)}]}


def test_a_parameter_list_reaches_the_calling_step_as_a_mapping(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    """The router is where the pairs stop: no step inland has ever seen one."""
    write_scripts(
        ws,
        frontier={
            "classify": [scoped({"name": "min", "value": 30}, {"name": "strict", "value": True})]
        },
    )
    seen: list[Mapping[str, object]] = []

    def watchful(data: Mapping[str, object]) -> Sequence[str]:
        seen.append(data)
        return []

    answer = route(ws, store, "classify", CallInputs(subject="demo_mr", check=watchful))

    assert answer.data["constraints"] == [
        {"id": "min_trades", "params": {"min": 30, "strict": True}}
    ]
    assert seen == [answer.data]
    assert len(watched.calls) == 1


def test_a_parameter_named_twice_takes_the_ladder_rather_than_a_quiet_repair(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    """Two values for one parameter is a wrong answer; picking one would be kanso deciding."""
    twice = scoped({"name": "min", "value": 30}, {"name": "min", "value": 40})
    write_scripts(ws, frontier={"classify": [twice, scoped({"name": "min", "value": 30})]})
    seen: list[Mapping[str, object]] = []

    def watchful(data: Mapping[str, object]) -> Sequence[str]:
        seen.append(data)
        return []

    answer = route(ws, store, "classify", CallInputs(subject="demo_mr", check=watchful))

    assert answer.data["constraints"] == [{"id": "min_trades", "params": {"min": 30}}]
    assert len(watched.calls) == 2
    assert "'min' is named twice" in watched.calls[1][1].user
    assert len(seen) == 1


# -- credentials ---------------------------------------------------------------------


def test_a_prompt_holding_a_credential_is_refused_by_name_and_never_sent(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    secret = "sk-live-abcdefghijklmnopqrst"
    credentialled(ws, "KANSO_KANSO_API_KEY", secret)
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    leaking = CallInputs(subject="demo_mr", stable={"note": f"the key is {secret}"})
    with pytest.raises(ValidationError) as caught:
        route(ws, store, "align_check", leaking)
    rendered = f"{caught.value.message} {caught.value.remedy}"
    assert "KANSO_KANSO_API_KEY" in rendered
    assert secret not in rendered
    assert watched.calls == []
    assert rows(store) == []


def test_a_credential_in_the_dynamic_half_is_refused_too(ws: Workspace, store: StateStore) -> None:
    secret = "sk-live-abcdefghijklmnopqrst"
    credentialled(ws, "KANSO_KANSO_API_KEY", secret)
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    with pytest.raises(ValidationError, match="KANSO_KANSO_API_KEY"):
        route(ws, store, "align_check", CallInputs(subject="x", dynamic={"tail": secret}))


def test_a_renamed_variable_is_guarded_as_well(ws: Workspace, store: StateStore) -> None:
    secret = "opaque-value-1234567890"
    write_register(ws, [model(m, t, api_key_env="MY_OWN_KEY") for t, m in _tiers()])
    credentialled(ws, "MY_OWN_KEY", secret)
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    with pytest.raises(ValidationError, match="MY_OWN_KEY"):
        route(ws, store, "align_check", CallInputs(subject="x", stable={"n": secret}))


def test_a_credential_from_the_process_environment_is_guarded(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "environment-secret-0987654321"
    monkeypatch.setenv("KANSO_KANSO_API_KEY", secret)
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    with pytest.raises(ValidationError, match="KANSO_KANSO_API_KEY"):
        route(ws, store, "align_check", CallInputs(subject="x", stable={"n": secret}))


def test_a_short_variable_value_is_not_treated_as_a_credential(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-character value would match half the prompts ever written."""
    monkeypatch.setenv("KANSO_KANSO_API_KEY", "no")
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    assert route(ws, store, "align_check", CallInputs(subject="no")).data == ALIGNED


def test_no_prompt_carries_a_credential_when_nothing_put_one_there(
    ws: Workspace, store: StateStore, watched: Watched
) -> None:
    secret = "sk-live-abcdefghijklmnopqrst"
    credentialled(ws, "KANSO_KANSO_API_KEY", secret)
    write_scripts(ws, cheap={"align_check": [ALIGNED]})
    route(ws, store, "align_check", INPUTS)
    for _, call in watched.calls:
        assert secret not in call.system
        assert secret not in call.user


def _tiers() -> list[tuple[str, str]]:
    return [("cheap", "cheap_mock"), ("mid", "mid_mock"), ("frontier", "frontier_mock")]


# -- the client factory --------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [
        ("anthropic", AnthropicClient),
        ("openai_compat", OpenAICompatClient),
        ("mock", MockClient),
    ],
)
def test_each_protocol_reaches_its_own_client(ws: Workspace, protocol: str, expected: type) -> None:
    write_register(ws, [model("m", "cheap", protocol=protocol)])
    from kanso.models import read_register

    built = client_for(ws.root, read_register(ws).models[0])
    assert isinstance(built, expected)
    assert built.protocol == protocol


def test_the_shipped_demo_register_and_script_classify_together(tmp_path: Path) -> None:
    """The demo must reach an answer with no provider key and no network."""
    from kanso.workspace import init

    demo = init(tmp_path / "demo", demo=True)
    with StateStore(demo.path("state.db")) as demo_store:
        demo_store.migrate()
        answer = route(demo, demo_store, "classify", INPUTS)
    assert answer.data["construct"] == {"id": "sleeve"}
    assert answer.model == "mock"


def test_a_missing_script_names_the_file_rather_than_the_schema(
    ws: Workspace, store: StateStore
) -> None:
    write_script(ws, "cheap_mock", {"align_check": [ALIGNED]})
    ws.path("mock", "cheap_mock.yaml").unlink()
    with pytest.raises(PreconditionError, match="does not exist"):
        route(ws, store, "align_check", INPUTS)
