"""The `openai_compat` wire: the chat-completions request, and how effort reaches it."""

from __future__ import annotations

import httpx
import pytest

from kanso.errors import PreconditionError
from kanso.models import Call, read_register
from kanso.models.openai_compat import OpenAICompatClient
from kanso.schemas.models import ModelSpec
from kanso.workspace import Workspace

from .conftest import RAW_TEXT, credentialled, model, recorded, write_register

KEY = "sk-compat-secret-abcdefghij"
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"desc": {"type": "string"}},
    "required": ["desc"],
    "additionalProperties": False,
}
USAGE = {
    "prompt_tokens": 500,
    "completion_tokens": 30,
    "prompt_tokens_details": {"cached_tokens": 400},
}
REPLY = {"choices": [{"message": {"content": '{"desc": "widen the band"}'}}], "usage": USAGE}


def call(effort: str = "medium") -> Call:
    return Call(
        task_class="propose",
        tier="mid",
        effort=effort,
        max_output=4096,
        system="the stable half",
        user="the dynamic half",
        schema=SCHEMA,
    )


def hosted(ws: Workspace, **changes: object) -> ModelSpec:
    write_register(ws, [model("gpt", "mid", protocol="openai_compat", local=False, **changes)])
    credentialled(ws, "KANSO_KANSO_API_KEY", KEY)
    return read_register(ws).models[0]


def test_the_request_is_the_recorded_shape(ws: Workspace) -> None:
    spec = hosted(ws, base_url="https://serve.example/v1")
    transport, seen = recorded((200, REPLY))
    OpenAICompatClient(ws.root, transport).complete(spec, call())
    assert seen.urls == ["https://serve.example/v1/chat/completions"]
    assert seen.body == {
        "model": "gpt",
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "the stable half"},
            {"role": "user", "content": "the dynamic half"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "propose", "strict": True, "schema": SCHEMA},
        },
        "reasoning_effort": "medium",
    }


def test_the_credential_travels_as_a_bearer_token(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    OpenAICompatClient(ws.root, transport).complete(spec, call())
    assert seen.headers[0]["authorization"] == f"Bearer {KEY}"


def test_the_default_base_url_is_the_protocols_own(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    OpenAICompatClient(ws.root, transport).complete(spec, call())
    assert seen.urls == ["https://api.openai.com/v1/chat/completions"]


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_an_effort_maps_onto_the_protocols_own_scale(ws: Workspace, effort: str) -> None:
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    OpenAICompatClient(ws.root, transport).complete(spec, call(effort))
    assert seen.body["reasoning_effort"] == effort


def test_an_effort_of_none_sends_no_thinking_budget_at_all(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, seen = recorded((200, REPLY))
    OpenAICompatClient(ws.root, transport).complete(spec, call("none"))
    assert "reasoning_effort" not in seen.body


def test_the_answer_carries_the_parsed_object_the_tokens_and_the_cost(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, _ = recorded((200, REPLY))
    answer = OpenAICompatClient(ws.root, transport).complete(spec, call())
    assert answer.data == {"desc": "widen the band"}
    assert (answer.tokens_in, answer.tokens_out) == (500, 30)
    assert answer.cost == pytest.approx((500 * 3.0 + 30 * 15.0) / 1_000_000)
    assert answer.cache_hit is True


def test_a_server_that_says_nothing_about_caching_reports_unknown(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, _ = recorded((200, {"choices": [], "usage": {"prompt_tokens": 5}}))
    assert OpenAICompatClient(ws.root, transport).complete(spec, call()).cache_hit is None


def test_a_reported_cache_miss_is_a_miss(ws: Workspace) -> None:
    spec = hosted(ws)
    usage = {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}}
    transport, _ = recorded((200, {"choices": [], "usage": usage}))
    assert OpenAICompatClient(ws.root, transport).complete(spec, call()).cache_hit is False


@pytest.mark.parametrize(
    "reply",
    [
        {"choices": [], "usage": {}},
        {"choices": "not a list", "usage": {}},
        {"choices": ["not a mapping"], "usage": {}},
        {"choices": [{"message": "not a mapping"}], "usage": {}},
        {"choices": [{"message": {"content": None}}], "usage": {}},
        {"choices": [{"message": {"content": "sorry, no"}}], "usage": {}},
        {"usage": {"prompt_tokens": "not a number"}},
    ],
)
def test_a_reply_with_no_object_in_it_is_an_empty_answer(ws: Workspace, reply: object) -> None:
    spec = hosted(ws)
    transport, _ = recorded((200, reply))
    assert OpenAICompatClient(ws.root, transport).complete(spec, call()).data == {}


def test_a_rejected_call_never_repeats_the_credential(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, _ = recorded((401, {"error": {"message": f"bad key {KEY}"}}))
    with pytest.raises(PreconditionError) as caught:
        OpenAICompatClient(ws.root, transport).complete(spec, call())
    assert KEY not in f"{caught.value.message} {caught.value.remedy}"


def test_a_local_server_needs_no_credential(ws: Workspace) -> None:
    write_register(
        ws,
        [
            model(
                "local",
                "cheap",
                protocol="openai_compat",
                local=True,
                base_url="http://localhost:8080/v1",
            )
        ],
    )
    spec = read_register(ws).models[0]
    transport, seen = recorded((200, REPLY))
    OpenAICompatClient(ws.root, transport).complete(spec, call())
    assert "authorization" not in seen.headers[0]
    assert seen.urls == ["http://localhost:8080/v1/chat/completions"]


def test_a_reply_that_is_not_json_is_a_failure(ws: Workspace) -> None:
    spec = hosted(ws)
    transport, _ = recorded((200, RAW_TEXT))
    with pytest.raises(PreconditionError, match="not JSON"):
        OpenAICompatClient(ws.root, transport).complete(spec, call())


def test_a_transport_failure_refuses_without_retrying(ws: Workspace) -> None:
    spec = hosted(ws)
    tries = []

    def unreachable(request: httpx.Request) -> httpx.Response:
        tries.append(request)
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(PreconditionError, match="ReadTimeout"):
        OpenAICompatClient(ws.root, httpx.MockTransport(unreachable)).complete(spec, call())
    assert len(tries) == 1
