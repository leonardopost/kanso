"""The `anthropic` wire: the request it builds, the answer it reads, and what it never says.

Every exchange here goes through a fake transport carrying a recorded shape. No test in
this file opens a socket or resolves a credential that exists outside its own workspace.
"""

from __future__ import annotations

import httpx
import pytest

from kanso.errors import PreconditionError
from kanso.models import Call, read_register
from kanso.models.anthropic import AnthropicClient
from kanso.schemas.models import ModelSpec
from kanso.workspace import Workspace

from .conftest import RAW_TEXT, credentialled, model, recorded, write_register

KEY = "sk-ant-secret-abcdefghijklmnop"
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"aligned": {"type": "boolean"}},
    "required": ["aligned"],
    "additionalProperties": False,
}

USAGE = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900}
REPLY = {"content": [{"type": "text", "text": '{"aligned": true}'}], "usage": USAGE}


def call(effort: str = "high") -> Call:
    return Call(
        task_class="align_check",
        tier="frontier",
        effort=effort,
        max_output=256,
        system="the stable half",
        user="the dynamic half",
        schema=SCHEMA,
    )


def hosted(ws: Workspace, **changes: object) -> ModelSpec:
    write_register(ws, [model("claude", "frontier", protocol="anthropic", local=False, **changes)])
    credentialled(ws, "KANSO_KANSO_API_KEY", KEY)
    return read_register(ws).models[0]


def test_the_request_is_the_recorded_shape(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    AnthropicClient(ws.root, transport).complete(spec, call())
    assert seen.urls == ["https://api.anthropic.com/v1/messages"]
    assert seen.body == {
        "model": "claude",
        "max_tokens": 256,
        "system": [
            {
                "type": "text",
                "text": "the stable half",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": "the dynamic half"}],
        "output_config": {
            "format": {"type": "json_schema", "schema": SCHEMA},
            "effort": "high",
        },
        "thinking": {"type": "adaptive"},
    }


def test_the_credential_travels_in_the_header_and_the_version_is_pinned(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    AnthropicClient(ws.root, transport).complete(spec, call())
    assert seen.headers[0]["x-api-key"] == KEY
    assert seen.headers[0]["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_an_effort_maps_onto_the_protocols_own_scale(ws: Workspace, effort: str) -> None:
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    AnthropicClient(ws.root, transport).complete(spec, call(effort))
    assert seen.body["output_config"]["effort"] == effort
    assert seen.body["thinking"] == {"type": "adaptive"}


def test_an_effort_of_none_sends_no_thinking_budget_at_all(ws: Workspace) -> None:
    """Not a budget of zero: the field is absent, and so is the effort."""
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    AnthropicClient(ws.root, transport).complete(spec, call("none"))
    assert "thinking" not in seen.body
    assert "effort" not in seen.body["output_config"]
    assert "format" in seen.body["output_config"]


def test_a_base_url_override_is_used_without_a_double_slash(ws: Workspace) -> None:
    spec = hosted(ws, base_url="https://gateway.example/anthropic/")
    transport, seen = recorded((200, REPLY))
    AnthropicClient(ws.root, transport).complete(spec, call())
    assert seen.urls == ["https://gateway.example/anthropic/v1/messages"]


def test_the_answer_carries_the_parsed_object_the_tokens_and_the_cost(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, _ = recorded((200, REPLY))
    answer = AnthropicClient(ws.root, transport).complete(spec, call())
    assert answer.data == {"aligned": True}
    assert (answer.model, answer.tier) == ("claude", "frontier")
    assert answer.tokens_in == 1000
    assert answer.tokens_out == 20
    assert answer.cost == pytest.approx((1000 * 3.0 + 20 * 15.0) / 1_000_000)
    assert answer.cache_hit is True


def test_a_cache_miss_is_reported_as_a_miss_and_silence_as_unknown(ws: Workspace) -> None:
    spec = hosted(ws)
    miss = {"content": [], "usage": {"input_tokens": 3, "cache_read_input_tokens": 0}}
    silent = {"content": [], "usage": {"input_tokens": 3}}
    transport, _ = recorded((200, miss))
    assert AnthropicClient(ws.root, transport).complete(spec, call()).cache_hit is False
    transport, _ = recorded((200, silent))
    assert AnthropicClient(ws.root, transport).complete(spec, call()).cache_hit is None


def test_cache_creation_counts_towards_the_prompt_that_was_read(ws: Workspace) -> None:
    spec = hosted(ws)
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 90, "output_tokens": 5}
    transport, _ = recorded((200, {"content": [], "usage": usage}))
    assert AnthropicClient(ws.root, transport).complete(spec, call()).tokens_in == 100


def test_thinking_blocks_are_skipped_and_text_blocks_joined(ws: Workspace) -> None:
    spec = hosted(ws)
    reply = {
        "content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": '{"aligned":'},
            {"type": "text", "text": " true}"},
        ],
        "usage": {},
    }
    transport, _ = recorded((200, reply))
    assert AnthropicClient(ws.root, transport).complete(spec, call()).data == {"aligned": True}


def test_a_fenced_answer_is_unwrapped(ws: Workspace) -> None:
    spec = hosted(ws)
    fenced = '```json\n{"aligned": false}\n```'
    transport, _ = recorded((200, {"content": [{"type": "text", "text": fenced}], "usage": {}}))
    assert AnthropicClient(ws.root, transport).complete(spec, call()).data == {"aligned": False}


@pytest.mark.parametrize(
    "reply",
    [
        {"content": [{"type": "text", "text": "I cannot answer that."}], "usage": {}},
        {"content": [{"type": "text", "text": "[1, 2]"}], "usage": {}},
        {"content": "not a list", "usage": {}},
        {"content": [{"type": "text"}], "usage": {}},
        {"usage": "not a mapping"},
    ],
)
def test_a_reply_that_is_not_an_object_is_an_empty_answer(ws: Workspace, reply: object) -> None:
    """Which fails every schema, so a model ignoring its constraint takes the ladder."""
    spec = hosted(ws)
    transport, _ = recorded((200, reply))
    answer = AnthropicClient(ws.root, transport).complete(spec, call())
    assert answer.data == {}
    assert answer.tokens_in == 0


def test_a_rejected_call_never_repeats_the_credential(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, _ = recorded((401, {"error": {"message": f"invalid key {KEY}"}}))
    with pytest.raises(PreconditionError) as caught:
        AnthropicClient(ws.root, transport).complete(spec, call())
    rendered = f"{caught.value.message} {caught.value.remedy}"
    assert KEY not in rendered
    assert "KANSO_KANSO_API_KEY" in rendered
    assert "401" in rendered


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, "not permitted"), (404, "does not know a model"), (429, "rate limiting"), (500, "")],
)
def test_every_refusal_names_the_status_and_what_to_do(
    ws: Workspace, status: int, expected: str
) -> None:
    spec = hosted(ws)
    transport, _ = recorded((status, {"error": "no"}))
    with pytest.raises(PreconditionError) as caught:
        AnthropicClient(ws.root, transport).complete(spec, call())
    assert str(status) in caught.value.message
    assert expected in str(caught.value.remedy)


def test_a_reply_that_is_not_json_is_a_failure_rather_than_an_empty_answer(
    ws: Workspace,
) -> None:
    spec = hosted(ws)
    transport, _ = recorded((200, RAW_TEXT))
    with pytest.raises(PreconditionError, match="not JSON"):
        AnthropicClient(ws.root, transport).complete(spec, call())


def test_a_reply_that_is_a_json_array_is_a_failure(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, _ = recorded((200, [1, 2]))
    with pytest.raises(PreconditionError, match="not a JSON object"):
        AnthropicClient(ws.root, transport).complete(spec, call())


def test_a_transport_that_cannot_reach_the_provider_refuses(ws: Workspace) -> None:
    spec = hosted(ws)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(PreconditionError) as caught:
        AnthropicClient(ws.root, httpx.MockTransport(unreachable)).complete(spec, call())
    assert "ConnectError" in caught.value.message


def test_a_local_model_with_no_credential_sends_no_key(ws: Workspace) -> None:
    write_register(ws, [model("local", "cheap", protocol="anthropic", local=True)])
    spec = read_register(ws).models[0]
    transport, seen = recorded((200, REPLY))
    AnthropicClient(ws.root, transport).complete(spec, call())
    assert "x-api-key" not in seen.headers[0]


def test_a_hosted_model_with_no_credential_refuses_before_the_call(ws: Workspace) -> None:
    write_register(ws, [model("claude", "frontier", protocol="anthropic", local=False)])
    spec = read_register(ws).models[0]
    transport, seen = recorded((200, REPLY))
    with pytest.raises(PreconditionError, match="KANSO_KANSO_API_KEY is not set"):
        AnthropicClient(ws.root, transport).complete(spec, call())
    assert seen.bodies == []


def test_a_renamed_credential_variable_is_the_one_read(ws: Workspace) -> None:
    spec = hosted(ws, api_key_env="MY_OWN_KEY")
    credentialled(ws, "MY_OWN_KEY", KEY)
    transport, seen = recorded((200, REPLY))
    AnthropicClient(ws.root, transport).complete(spec, call())
    assert seen.headers[0]["x-api-key"] == KEY
