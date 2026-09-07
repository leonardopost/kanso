"""The `openai_compat` protocol: the chat-completions wire, hosted or local.

This is the protocol most servers other than Anthropic's speak, and it is the one a model
running on the operator's own machine speaks too. The facts this client relies on:

* `POST <base_url>/chat/completions`, authenticated with `Authorization: Bearer`. The
  base URL already carries the version segment, which is why the register's own example
  ends in `/v1`.
* A schema-constrained answer is requested with `response_format`, a
  `{"type": "json_schema", "json_schema": {"name", "strict", "schema"}}` document. The
  reply's message content is then JSON matching it.
* `strict: true` requires every object in that document to set
  `additionalProperties: false`, and requires every key of `properties` to appear in
  `required`. The document sent is `call.schema` exactly — nothing here rewrites it — so
  whatever `models/tasks.py` declares is what goes out on both wires, and the free-form
  parameter object that earned a 400 on the Anthropic wire would have been refused here
  for the same reason. **Only that first requirement has been measured, and only there**
  (`models/tasks.py`, `PARAM_PAIRS`): no request from this client has ever been sent to a
  server speaking this protocol. The second is not met today — `classify.construct` leaves
  `host` and `params` out of `required`, and `classify.constraints[]` leaves out `params` —
  so a strict server may refuse `classify` for a reason the Anthropic wire does not have.
  `docs/backlog.md` row 57 holds that half open; what closes it is a measurement, since
  making the two keys required changes what a model must answer.
* Thinking depth is `reasoning_effort`, a `low` / `medium` / `high` scale that kanso's own
  effort maps onto one for one. A server with no reasoning control ignores the field.
* The output cap is `max_tokens`. The newer `max_completion_tokens` is not universally
  implemented by the servers that call themselves compatible, and the older name is.
* `usage` reports `prompt_tokens` and `completion_tokens`, with cached input counted
  inside `prompt_tokens` and broken out under `prompt_tokens_details.cached_tokens` by the
  servers that cache at all.

An effort of `none` omits `reasoning_effort` entirely rather than sending a zero, so a
class a rule has already decided is not asked to think about it.
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

__all__ = ["OpenAICompatClient"]

DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"
COMPLETIONS_PATH: Final = "/chat/completions"
NO_THINKING: Final = "none"


class OpenAICompatClient:
    """One request per call, on the chat-completions wire."""

    protocol: ClassVar[str] = "openai_compat"

    def __init__(self, root: Path, transport: httpx.BaseTransport | None = None) -> None:
        self.root = root
        self.transport = transport

    def complete(self, spec: ModelSpec, call: Call) -> Answer:
        """Send `call` to `spec` and return what came back."""
        key = api_key(self.root, spec)
        headers = {"content-type": "application/json"}
        if key is not None:
            headers["authorization"] = f"Bearer {key}"
        base = (spec.base_url or DEFAULT_BASE_URL).rstrip("/")
        payload = post(spec, base + COMPLETIONS_PATH, headers, body(spec, call), self.transport)
        return _answer(spec, call, payload)


def body(spec: ModelSpec, call: Call) -> dict[str, object]:
    """The request body for one call.

    The stable half is the system message and the dynamic half the user message, in that
    order, which is the prefix a server that caches at all caches.
    """
    request: dict[str, object] = {
        "model": spec.id,
        "max_tokens": call.max_output,
        "messages": [
            {"role": "system", "content": call.system},
            {"role": "user", "content": call.user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": call.task_class,
                "strict": True,
                "schema": dict(call.schema),
            },
        },
    }
    if call.effort != NO_THINKING:
        request["reasoning_effort"] = call.effort
    return request


def _answer(spec: ModelSpec, call: Call, payload: Mapping[str, object]) -> Answer:
    usage = payload.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    tokens_in = _count(usage, "prompt_tokens")
    tokens_out = _count(usage, "completion_tokens")
    return Answer(
        data=as_object(_content(payload)),
        model=spec.id,
        tier=call.tier,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost_of(tokens_in, tokens_out, spec.cost_in, spec.cost_out),
        cache_hit=_cache_hit(usage),
    )


def _content(payload: Mapping[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _cache_hit(usage: Mapping[str, object]) -> bool | None:
    """Whether the prompt cache served any of the input, or `None` when unreported."""
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, Mapping) or "cached_tokens" not in details:
        return None
    return _count(details, "cached_tokens") > 0


def _count(usage: Mapping[str, object], key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
