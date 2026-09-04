"""The `anthropic` protocol: how this package asks that wire for a schema-shaped answer.

Anthropic messages API facts this client relies on, as of API version `2023-06-01`:

* `POST <base_url>/v1/messages`, authenticated with the `x-api-key` header and versioned
  with `anthropic-version`.
* A schema-constrained answer is requested with `output_config.format`, a
  `{"type": "json_schema", "schema": ...}` document. The reply's first text block is then
  JSON matching it. The older top-level `output_format` field is superseded by this one.
* Thinking depth is `output_config.effort` — `low`, `medium`, `high` and higher — paired
  with `thinking: {"type": "adaptive"}`, under which the model decides how much to think.
  The fixed `budget_tokens` form belongs to earlier models and is refused by current ones,
  which is why kanso's effort maps onto `effort` rather than onto a token count.
* Caching is a prefix match over `tools`, then `system`, then `messages`, so the system
  turn carries the `cache_control` breakpoint and everything dynamic sits after it.
* `usage` reports `input_tokens` (uncached), `cache_read_input_tokens` and
  `cache_creation_input_tokens` separately; their sum is the prompt the model actually
  read, which is what the ledger records.

An effort of `none` sends no thinking control at all — no `thinking`, no `effort` — rather
than a zero budget. The two are different requests: one leaves the decision to the model's
default, the other asks for a thinking budget of nothing, and a class routed to `none` was
routed there because a rule had already decided the answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Final

import httpx

from kanso.models.call import Answer, Call
from kanso.models.ledger import cost_of
from kanso.models.wire import api_key, as_object, post
from kanso.schemas.models import ModelSpec

__all__ = ["AnthropicClient"]

DEFAULT_BASE_URL: Final = "https://api.anthropic.com"
API_VERSION: Final = "2023-06-01"
MESSAGES_PATH: Final = "/v1/messages"
NO_THINKING: Final = "none"


class AnthropicClient:
    """One request per call, on the Anthropic messages wire."""

    protocol: ClassVar[str] = "anthropic"

    def __init__(self, root: Path, transport: httpx.BaseTransport | None = None) -> None:
        self.root = root
        self.transport = transport

    def complete(self, spec: ModelSpec, call: Call) -> Answer:
        """Send `call` to `spec` and return what came back."""
        key = api_key(self.root, spec)
        headers = {"anthropic-version": API_VERSION, "content-type": "application/json"}
        if key is not None:
            headers["x-api-key"] = key
        base = (spec.base_url or DEFAULT_BASE_URL).rstrip("/")
        payload = post(spec, base + MESSAGES_PATH, headers, body(spec, call), self.transport)
        return _answer(spec, call, payload)


def body(spec: ModelSpec, call: Call) -> dict[str, object]:
    """The request body for one call.

    The system turn is a single cached text block, so a run's repeated calls of one task
    class re-read the same prefix; the dynamic half is the whole of the user turn.
    """
    output_config: dict[str, object] = {
        "format": {"type": "json_schema", "schema": dict(call.schema)}
    }
    request: dict[str, object] = {
        "model": spec.id,
        "max_tokens": call.max_output,
        "system": [
            {
                "type": "text",
                "text": call.system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": call.user}],
        "output_config": output_config,
    }
    if call.effort != NO_THINKING:
        output_config["effort"] = call.effort
        request["thinking"] = {"type": "adaptive"}
    return request


def _answer(spec: ModelSpec, call: Call, payload: Mapping[str, object]) -> Answer:
    usage = payload.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    cached = _count(usage, "cache_read_input_tokens")
    tokens_in = (
        _count(usage, "input_tokens") + cached + _count(usage, "cache_creation_input_tokens")
    )
    tokens_out = _count(usage, "output_tokens")
    return Answer(
        data=as_object(_text(payload)),
        model=spec.id,
        tier=call.tier,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost_of(tokens_in, tokens_out, spec.cost_in, spec.cost_out),
        cache_hit=cached > 0 if "cache_read_input_tokens" in usage else None,
    )


def _text(payload: Mapping[str, object]) -> str:
    """Every text block of the reply, joined.

    Thinking blocks carry no text under the default display setting and tool blocks carry
    none at all, so joining the text blocks is the answer whether the model thought or not.
    """
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block["text"])
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text" and "text" in block
    ]
    return "".join(parts)


def _count(usage: Mapping[str, object], key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
