"""The optional escalation webhook: one JSON POST per entry, best effort, never in the way.

An escalation is already three durable writes — a row, a line in the inbox file and an event
— before anything is sent anywhere. The webhook is a fourth, unreliable copy for an operator
who wants a notification rather than a file to watch, and it is treated as exactly that.

**It cannot fail a command.** No exception a network raises leaves this module, and neither
does a non-2xx status: an escalation that reached the state store has happened whether or not
a chat server acknowledged it, and a `demote` that raised because a webhook endpoint had moved
would be a monitor stopped by a notification. What a failed post does is record an event, so
an operator whose alerts went quiet can find out why.

**There is one attempt and a short timeout.** A retry loop would hold the monitor pass for as
long as an unreachable host takes to time out several times over, and the entry it is retrying
is already durable. A second copy of an escalation is worth less than a pass that finishes.

**The URL is a secret and never appears in an output.** It is resolved at the moment of use,
from `[webhook] url` and then from the standard variable name, and nothing here puts it in a
return value, an event detail or an error message — a webhook URL is usually a bearer token
wearing a hostname, which is why the recorded reason for a failure is the exception's type and
the status code rather than the message the client raised, since that message quotes the URL.
Only `http` and `https` are posted to: every other scheme `urlopen` understands reads
something local, and a mistyped configuration must fail rather than open a file.

The request goes out through the standard library rather than the HTTP client the model layer
uses, because one JSON POST with a timeout and no retry needs nothing that client provides,
and the dependency is on the dependency list for the provider wire alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib import error, request
from urllib.parse import urlsplit

from kanso.creds import resolve, standard_name

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "FAILED",
    "SCHEMES",
    "TIMEOUT_S",
    "VARIABLE",
    "Delivery",
    "configured",
    "post",
    "url",
]

TIMEOUT_S: Final = 5.0
"""How long one attempt is given, connect and read together."""

VARIABLE: Final = standard_name("webhook", "URL")
"""`KANSO_WEBHOOK_URL`: the standard name, and the fallback when the config names none."""

FAILED: Final = "webhook_failed"
"""The event kind a post that did not land appends, under the escalation's subject."""

SCHEMES: Final = ("http", "https")
"""The only schemes posted to; anything else `urlopen` accepts reaches a local resource."""

BAD_SCHEME: Final = "not an http endpoint"

_JSON: Final = {"Content-Type": "application/json"}
_OK: Final = 400
"""The first status code that is not a delivery."""


@dataclass(frozen=True)
class Delivery:
    """What one post did, with nothing in it that could carry the URL."""

    attempted: bool
    delivered: bool
    status: int | None = None
    reason: str | None = None


def url(ws: Workspace) -> str | None:
    """The configured endpoint, or the one the standard variable names, or `None`.

    Never logged, never returned in a payload, never put in an error message.
    """
    configured_url = ws.config.webhook.url
    if configured_url:
        return configured_url
    return resolve(VARIABLE, ws.root)


def configured(ws: Workspace) -> bool:
    """Whether this workspace has a webhook at all, without disclosing where."""
    return url(ws) is not None


def post(ws: Workspace, store: StateStore, subject: str, payload: Mapping[str, object]) -> Delivery:
    """POST one escalation as JSON. Records a failure, raises nothing, retries nothing."""
    target = url(ws)
    if target is None:
        return Delivery(attempted=False, delivered=False)
    if urlsplit(target).scheme not in SCHEMES:
        return _failed(store, subject, None, BAD_SCHEME)
    try:
        # The scheme is checked above, so this reaches an http endpoint or nothing.
        posted = request.Request(
            target,
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers=dict(_JSON),
            method="POST",
        )
        with request.urlopen(posted, timeout=TIMEOUT_S) as response:
            status = int(response.status)
    except error.HTTPError as exc:
        return _failed(store, subject, int(exc.code), f"status {exc.code}")
    except Exception as exc:  # every failure is the same failure: the entry is already durable
        return _failed(store, subject, None, type(exc).__name__)
    if status >= _OK:
        return _failed(store, subject, status, f"status {status}")
    return Delivery(attempted=True, delivered=True, status=status)


def _failed(store: StateStore, subject: str, status: int | None, reason: str) -> Delivery:
    """Record why the post did not land, in words that cannot hold the URL."""
    store.event(FAILED, subject, {"status": status, "reason": reason})
    return Delivery(attempted=True, delivered=False, status=status, reason=reason)
