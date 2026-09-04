"""`models check`: one minimal call per configured model, and what it reports."""

from __future__ import annotations

import httpx
import pytest

from kanso.models import check
from kanso.models import router as router_module
from kanso.models.anthropic import AnthropicClient
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import credentialled, model, recorded, write_register, write_script

REPLY = {
    "content": [{"type": "text", "text": '{"ok": true}'}],
    "usage": {"input_tokens": 12, "output_tokens": 5},
}


def rows(store: StateStore) -> list[dict[str, object]]:
    found = store.connection.execute("SELECT * FROM spend ORDER BY spend_id").fetchall()
    return [dict(row) for row in found]


def test_every_configured_model_is_called_once_in_file_order(
    ws: Workspace, store: StateStore
) -> None:
    results = check(ws, store)
    assert [r.id for r in results] == ["cheap_mock", "mid_mock", "frontier_mock"]
    assert all(r.ok for r in results)
    assert all(r.latency_ms >= 0 for r in results)


def test_a_check_reports_the_model_its_provider_protocol_and_tiers(
    ws: Workspace, store: StateStore
) -> None:
    write_register(ws, [model("everywhere", ["cheap", "mid", "frontier"])])
    write_script(ws, "everywhere", {})
    result = check(ws, store)[0]
    assert (result.provider, result.protocol) == ("kanso", "mock")
    assert result.tiers == ("cheap", "mid", "frontier")


def test_a_check_call_is_ledgered_like_any_other_call(ws: Workspace, store: StateStore) -> None:
    """It costs what a call costs, so it belongs in the ledger."""
    check(ws, store)
    ledgered = rows(store)
    assert len(ledgered) == 3
    assert {row["task_class"] for row in ledgered} == {"check"}
    assert {row["lane"] for row in ledgered} == {None}


def test_a_model_that_cannot_answer_is_reported_rather_than_raised(
    ws: Workspace, store: StateStore
) -> None:
    ws.path("mock", "mid_mock.yaml").unlink()
    results = check(ws, store)
    assert [r.ok for r in results] == [True, False, True]
    assert "does not exist" in results[1].detail
    assert len(rows(store)) == 2, "a call that never happened is not ledgered"


def test_the_wire_check_asks_for_the_smallest_object_at_no_thinking_effort(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_register(ws, [model("claude", "frontier", protocol="anthropic", local=False)])
    credentialled(ws, "KANSO_KANSO_API_KEY", "sk-ant-abcdefghijklmnop")
    transport, seen = recorded((200, REPLY))
    monkeypatch.setattr(
        router_module,
        "client_for",
        lambda root, spec: AnthropicClient(root, transport),
    )
    results = check(ws, store)
    assert results[0].ok
    assert "thinking" not in seen.body
    assert seen.body["max_tokens"] == 64
    assert seen.body["output_config"]["format"]["schema"]["required"] == ["ok"]
    assert rows(store)[0]["tokens_in"] == 12


def test_a_provider_that_refuses_is_reported_with_its_reason(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_register(ws, [model("claude", "frontier", protocol="anthropic", local=False)])
    credentialled(ws, "KANSO_KANSO_API_KEY", "sk-ant-abcdefghijklmnop")
    transport, _ = recorded((401, {"error": "no"}))
    monkeypatch.setattr(
        router_module,
        "client_for",
        lambda root, spec: AnthropicClient(root, transport),
    )
    result = check(ws, store)[0]
    assert not result.ok
    assert "401" in result.detail
    assert rows(store) == []


def test_a_check_judges_reachability_rather_than_the_answer(
    ws: Workspace, store: StateStore
) -> None:
    """A mock that lists no `check` class answers `{}`, which still proves it is there."""
    write_script(ws, "cheap_mock", {"align_check": [{"aligned": True, "reason": "x"}]})
    assert check(ws, store)[0].ok


def test_an_unreachable_provider_is_reported_rather_than_raised(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_register(ws, [model("claude", "frontier", protocol="anthropic", local=False)])
    credentialled(ws, "KANSO_KANSO_API_KEY", "sk-ant-abcdefghijklmnop")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(
        router_module,
        "client_for",
        lambda root, spec: AnthropicClient(root, httpx.MockTransport(unreachable)),
    )
    result = check(ws, store)[0]
    assert not result.ok
    assert "ConnectError" in result.detail


def test_a_register_with_no_models_checks_nothing(ws: Workspace, store: StateStore) -> None:
    write_register(ws, [])
    assert check(ws, store) == []
