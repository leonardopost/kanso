"""The escalation webhook: best effort, one attempt, and a URL that never leaves.

Nothing here reaches a network. `urlopen` is replaced by a recorder or by something that
raises, which is the whole surface: what is sent, what is done with a failure, and what is
never written down.
"""

from __future__ import annotations

import json
from typing import Any
from urllib import error, request

import pytest

from kanso import inbox
from kanso.inbox import webhook
from kanso.state import StateStore
from kanso.workspace import Workspace

ENDPOINT = "https://hooks.example.invalid/T0000/B0000/verysecrettoken"
PAYLOAD: dict[str, object] = {"id": "abcdef12", "kind": "promotable", "subject": "demo_mr@1"}


class Answer:
    """What `urlopen` returns: a context manager carrying a status."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class Sent:
    """A stand-in for `urlopen` that records the request and answers with a status."""

    def __init__(self, status: int = 200, raises: Exception | None = None) -> None:
        self.status = status
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any], float, str]] = []

    def __call__(self, posted: request.Request, timeout: float = 0.0) -> Answer:
        self.calls.append(
            (
                posted.full_url,
                json.loads((posted.data or b"{}").decode("utf-8")),
                timeout,
                posted.get_method(),
            )
        )
        if self.raises is not None:
            raise self.raises
        return Answer(self.status)


def rejected(status: int) -> error.HTTPError:
    """What `urlopen` raises for a status it will not return."""
    return error.HTTPError(ENDPOINT, status, "no", {}, None)  # type: ignore[arg-type]


def configure(ws: Workspace, url: str) -> None:
    """Name the endpoint in `kanso.toml`, as an operator would."""
    path = ws.path("kanso.toml")
    path.write_text(
        path.read_text(encoding="utf-8").replace("[webhook]", f'[webhook]\nurl = "{url}"'),
        encoding="utf-8",
    )


def reopened(ws: Workspace) -> Workspace:
    """The same workspace with its configuration read again."""
    from kanso.workspace import find

    return find(ws.root)


def sender(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Sent:
    """Replace `urlopen` for the length of one test."""
    made = Sent(**kwargs)
    monkeypatch.setattr(request, "urlopen", made)
    return made


def test_a_workspace_with_no_webhook_attempts_nothing(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = sender(monkeypatch)

    delivery = webhook.post(ws, store, "demo_mr@1", PAYLOAD)

    assert delivery == webhook.Delivery(attempted=False, delivered=False)
    assert sent.calls == []
    assert not webhook.configured(ws)


def test_the_configured_url_is_used_and_the_entry_is_the_body(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = sender(monkeypatch)
    configure(ws, ENDPOINT)

    delivery = webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD)

    assert delivery.delivered and delivery.status == 200
    url, body, timeout, method = sent.calls[0]
    assert url == ENDPOINT
    assert body == PAYLOAD
    assert timeout == webhook.TIMEOUT_S
    assert method == "POST"


def test_the_standard_variable_is_the_fallback(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`KANSO_WEBHOOK_URL`, which is an instance of the credential naming scheme."""
    sent = sender(monkeypatch)
    monkeypatch.setenv(webhook.VARIABLE, ENDPOINT)

    assert webhook.VARIABLE == "KANSO_WEBHOOK_URL"
    assert webhook.post(ws, store, "demo_mr@1", PAYLOAD).delivered
    assert sent.calls[0][0] == ENDPOINT


def test_the_configured_url_wins_over_the_variable(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = sender(monkeypatch)
    monkeypatch.setenv(webhook.VARIABLE, "https://elsewhere.invalid/hook")
    configure(ws, ENDPOINT)

    webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD)

    assert sent.calls[0][0] == ENDPOINT


def test_anything_but_an_http_endpoint_is_refused_unsent(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`urlopen` would read a local file for a `file:` URL and report a delivery."""
    sent = sender(monkeypatch)
    configure(ws, "file:///etc/passwd")

    delivery = webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD)

    assert sent.calls == []
    assert delivery.reason == webhook.BAD_SCHEME


def test_a_network_failure_is_recorded_and_does_not_propagate(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monitor pass is not stopped by a notification."""
    sender(monkeypatch, raises=TimeoutError("connecting to " + ENDPOINT))
    configure(ws, ENDPOINT)

    delivery = webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD)

    assert delivery.attempted and not delivery.delivered
    assert delivery.reason == "TimeoutError"
    events = store.events(kind=webhook.FAILED)
    assert [event.subject for event in events] == ["demo_mr@1"]
    assert events[0].detail == {"status": None, "reason": "TimeoutError"}


def test_a_url_the_client_itself_rejects_is_a_failure_like_any_other(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `ValueError` from the client is still not allowed to fail the escalation."""
    sender(monkeypatch, raises=ValueError("bad url " + ENDPOINT))
    configure(ws, ENDPOINT)

    assert webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD).reason == "ValueError"


def test_a_rejected_post_is_a_failure_with_its_status(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender(monkeypatch, raises=rejected(404))
    configure(ws, ENDPOINT)

    delivery = webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD)

    assert not delivery.delivered and delivery.status == 404
    assert store.events(kind=webhook.FAILED)[0].detail["reason"] == "status 404"


def test_a_bad_status_that_is_returned_rather_than_raised_is_a_failure_too(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opener with its own error handling can hand a 500 back instead of raising."""
    sender(monkeypatch, status=500)
    configure(ws, ENDPOINT)

    delivery = webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD)

    assert not delivery.delivered and delivery.status == 500


def test_an_accepted_post_is_a_delivery(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender(monkeypatch, status=202)
    configure(ws, ENDPOINT)

    assert webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD).delivered


def test_no_recorded_failure_can_hold_the_url(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A webhook URL is usually a bearer token wearing a hostname."""
    sender(monkeypatch, raises=ConnectionRefusedError("failed to connect to " + ENDPOINT))
    configure(ws, ENDPOINT)

    delivery = webhook.post(reopened(ws), store, "demo_mr@1", PAYLOAD)

    written = " ".join(
        str(part) for event in store.events() for part in (event.subject, event.detail)
    )
    assert "verysecrettoken" not in written
    assert "verysecrettoken" not in str(delivery)


def test_every_escalation_posts_the_entry_it_wrote(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The webhook is the fourth copy of an entry that is already durable three times."""
    sent = sender(monkeypatch)
    configure(ws, ENDPOINT)
    opened = reopened(ws)

    entry = inbox.escalate(opened, store, "promotable", "demo_mr@1", "ready for real capital")

    assert sent.calls[0][1] == entry.payload()


def test_an_escalation_survives_a_webhook_that_fails(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender(monkeypatch, raises=TimeoutError("slow"))
    configure(ws, ENDPOINT)
    opened = reopened(ws)

    entry = inbox.escalate(opened, store, "demoted", "demo_mr@1", "drifted below its band")

    assert inbox.unread(store)[0].escalation_id == entry.escalation_id
    assert entry.line() in inbox.inbox_file(opened).read_text(encoding="utf-8")
    assert store.events(kind=webhook.FAILED)[0].subject == "demo_mr@1"
