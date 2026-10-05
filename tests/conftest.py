"""Suite-wide guards.

Every escalation posts the optional webhook, so a developer who happens to have
`KANSO_WEBHOOK_URL` exported would have the suite post test escalations to their own
endpoint. The suite makes no network call, so the variable is removed for every test and a
test that wants one sets it itself.

A Massive cursor walk pauses before it asks again for a page that did not answer. No test
waits on that pause; a test that asserts on it records it itself.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kanso.data.adapters.massive import client as massive_client
from kanso.inbox.webhook import VARIABLE


@pytest.fixture(autouse=True)
def _no_ambient_webhook(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test posts anywhere unless it says so."""
    monkeypatch.delenv(VARIABLE, raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_backoff_pause(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test sleeps through a page's backoff unless it says so."""
    monkeypatch.setattr(massive_client, "sleep", lambda seconds: None)
    yield
