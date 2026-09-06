"""What the two network protocols share: one request, one credential, one parse.

Neither wire client retries. The router's ladder is the only retry policy the package
has, and a transport that retried underneath it would multiply the attempt count the
ledger reports and the spend the operator sees by an amount neither of them can observe.
One request goes out and one outcome comes back.

Nothing here puts a credential anywhere but the request headers. A failure names the
model, the status and the variable to check — never the key, never the URL, and never the
provider's response body, since a provider that echoes the request is a provider that
would echo the key into a message an operator pastes into an issue.

An answer that is not a JSON object is not an error: it is an empty answer, which fails
the task class's schema and takes the same ladder a wrong answer takes. That is what
keeps a model that ignores its output constraint on the same path as one that obeys it
badly, and it is why this module never raises for content.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx

from kanso import creds
from kanso.errors import PreconditionError
from kanso.schemas.models import ModelSpec

__all__ = ["api_key", "as_object", "post"]

CONNECT_TIMEOUT_S: Final = 15.0
REQUEST_TIMEOUT_S: Final = 300.0
"""Long enough for a frontier model thinking hard at a four-thousand-token cap."""

_FENCE: Final = "```"

_REMEDIES: Final[dict[int, str]] = {
    401: "the provider rejected the credential; check {name}",
    403: "the credential in {name} is not permitted to use {model}",
    404: "the provider does not know a model called {model}",
    429: "the provider is rate limiting; the call is not retried, so try again later",
}


def api_key(root: Path, spec: ModelSpec) -> str | None:
    """The credential for one model, or `None` when it needs none.

    The variable is the model's own `api_key_env` when it names one and the standard name
    derived from its provider otherwise. A model served by a local process usually needs
    no credential, so an absent value there is not a failure; a hosted one refuses.
    """
    name = spec.api_key_env or creds.standard_name(spec.provider)
    if spec.local:
        return creds.resolve(name, root)
    return creds.require(name, root)


def post(
    spec: ModelSpec,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, object],
    transport: httpx.BaseTransport | None = None,
) -> Mapping[str, object]:
    """One POST, and the decoded JSON object that came back.

    `transport` exists so the suite can put a recorded exchange behind the same code the
    network path uses; nothing else passes it.

    A `base_url` the client cannot even parse is caught here with the rest: `InvalidURL`
    is not an `HTTPError`, so without it the placeholder the register template ships
    (`http://localhost:<port>/v1`) reaches an operator as a traceback and exit 1 rather
    than as the model that did not answer, which is what it is.
    """
    timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(url, headers=dict(headers), json=dict(body))
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise PreconditionError(
            f"{spec.id}: the request did not complete ({type(exc).__name__})",
            remedy="check the network and the model's base_url",
        ) from None
    if response.status_code >= 400:
        template = _REMEDIES.get(response.status_code)
        name = spec.api_key_env or creds.standard_name(spec.provider)
        raise PreconditionError(
            f"{spec.id}: the provider answered {response.status_code}",
            remedy=(
                template.format(name=name, model=spec.id)
                if template
                else "the provider refused the call; the response is not repeated here"
            ),
        )
    try:
        payload = response.json()
    except ValueError:
        raise PreconditionError(
            f"{spec.id}: the provider answered {response.status_code} with something "
            "that is not JSON"
        ) from None
    if not isinstance(payload, dict):
        raise PreconditionError(f"{spec.id}: the provider's reply is not a JSON object")
    return payload


def as_object(text: str) -> Mapping[str, object]:
    """The JSON object `text` holds, or an empty one when it holds no object.

    A fenced block is unwrapped first, because a model told to answer with JSON and
    nothing else still sometimes fences it, and a fence is a formatting habit rather than
    a wrong answer.
    """
    for candidate in (text, _unfenced(text)):
        try:
            loaded = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


def _unfenced(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith(_FENCE):
        return stripped
    body = stripped.removeprefix(_FENCE)
    newline = body.find("\n")
    if newline == -1:
        return stripped
    return body[newline + 1 :].removesuffix(_FENCE).removesuffix("\n")
