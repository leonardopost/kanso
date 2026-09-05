"""The Massive adapter's suite, and the frozen wire it replays.

Nothing here opens a socket, reads a credential or carries a recorded secret. A test
answers with a response body written out in full, through a `Replay` transport that also
records what was asked, so a test can assert on the requests as well as on the answers —
which is the only way to prove that an outcome was established by probing rather than by
reading the vendor's sentence.

`WARNING` is that sentence. It is the same bytes for a dataset the plan excludes, a range
older than the plan's window, a wrong asset-class prefix and an unrecognised key, so every
fixture here uses the identical text for all four: a test that passed by parsing it would
have to pass four contradictory ways at once.

Three answers here are not envelopes at all, and each was measured because assuming it
cost a live failure that the suite was green through: a path the source does not serve
(`missing`), a page wider than one listing will hand over (`over_limit`), and a request
carrying a `Host` the sender sets for itself (`refuse_host`, which every request through
`Replay` is now held to). A fixture that shrugs at one of those is a fixture that encodes
a hope.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest

from kanso.data.adapters.massive.client import Response

WARNING = (
    "Warning [NOT_ENTITLED]: This data isn't included in your current plan. "
    "Please upgrade your plan to access it."
)
"""The vendor's one sentence for four different conditions. No code reads it."""

Answer = Callable[[str, Mapping[str, str]], Response]
"""How a test decides what a request gets back, from its URL and its parameters."""


@dataclass(frozen=True, slots=True)
class Asked:
    """One request the transport was handed, as the test sees it."""

    method: str
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    keys: tuple[str, ...]

    @property
    def path(self) -> str:
        """The URL with the host stripped, which is what a test asserts on."""
        return self.url.split("//", 1)[-1].split("/", 1)[-1]


def refuse_host(headers: Mapping[str, str], sender: str) -> None:
    """Fail when a sender that sets its own `Host` is handed one to send as well.

    Every sender kanso reaches — the engine's rate-limited transport and its bulk download
    alike — puts a `Host` on the wire from the URL. A request that arrives with two is
    answered by the object store's front end with a `400` and an HTML page, whatever the
    signature says, which is the whole bulk path down. A fixture that shrugged at the extra
    header is why that could be true live while every test here was green, so no fixture
    here shrugs: the guard sits on the shared transport, so a signing path added later is
    held to it without anyone remembering to ask.

    `pytest.fail` and not `assert`: a caller turns an `Exception` from a sender into a
    `TransportError`, and a pytest outcome is not an `Exception`.
    """
    carried = sorted(name for name in headers if name.lower() == "host")
    if carried:
        pytest.fail(
            f"the {sender} was handed {carried} alongside the Host it sets from the URL; "
            "the store answers a request bearing two Host headers with a 400 and an HTML "
            "page rather than an object"
        )


@dataclass
class Replay:
    """A transport that answers from frozen bodies, remembers every request, and refuses
    one that carries a `Host` of its own."""

    answer: Answer
    asked: list[Asked] = field(default_factory=list)

    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
    ) -> Response:
        refuse_host(headers, "transport")
        self.asked.append(Asked(method, url, dict(params), dict(headers), tuple(keys)))
        return self.answer(url, params)

    @property
    def paths(self) -> list[str]:
        """Every path asked for, in order."""
        return [item.url for item in self.asked]


def body(payload: Mapping[str, Any], status: int = 200) -> Response:
    """A response carrying `payload` as JSON."""
    return Response(status=status, body=json.dumps(payload).encode())


def served(
    rows: Sequence[Mapping[str, Any]], *, next_url: str | None = None, vendor: str = "OK"
) -> Response:
    """A successful answer carrying rows, and optionally a cursor to the next page."""
    payload: dict[str, Any] = {
        "status": vendor,
        "request_id": "frozen",
        "results": list(rows),
        "resultsCount": len(rows),
    }
    if next_url is not None:
        payload["next_url"] = next_url
    return body(payload)


def nothing() -> Response:
    """A successful answer with no rows: the shape a below-floor stock range comes back as."""
    return body({"status": "OK", "request_id": "frozen", "results": [], "resultsCount": 0})


def refused(status: int = 403) -> Response:
    """The refusal, with the sentence that means four things."""
    return body({"status": "NOT_AUTHORIZED", "request_id": "frozen", "message": WARNING}, status)


def rejected() -> Response:
    """A rejected request shape, which the vendor answers with a client error."""
    return body({"status": "ERROR", "request_id": "frozen", "message": WARNING}, 400)


def missing() -> Response:
    """A path the source does not serve: a plain 404 carrying no vendor envelope.

    Neither a refusal nor an empty window, which is the point — a listing asked for under
    the wrong version prefix comes back like this and must not be readable as either.
    """
    return Response(status=404, body=b"not found")


def over_limit(ceiling: int) -> Response:
    """A page wider than a listing serves, which the source rejects rather than trims.

    Measured per endpoint: a listing's ceiling is that listing's, so a fixture that ignored
    `limit` could not tell a page size that works from one that fails on the first live
    request.
    """
    return body(
        {
            "status": "ERROR",
            "request_id": "frozen",
            "message": f"limit must be no greater than {ceiling}",
        },
        400,
    )


def bar(day: date, *, close: float = 100.0) -> dict[str, Any]:
    """One daily aggregate, timed in the milliseconds the aggregate endpoints use."""
    moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return {
        "t": int(moment.timestamp()) * 1_000,
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": 1_000,
        "n": 10,
    }


def definition(ticker: str, *, source_feed: str | None = None) -> dict[str, Any]:
    """One reference definition, as the control endpoint answers with."""
    found: dict[str, Any] = {"ticker": ticker, "name": f"{ticker} test", "active": True}
    if source_feed is not None:
        found["source_feed"] = source_feed
    return found


def window_of(url: str) -> tuple[date, date]:
    """The window an aggregates path names, for a fixture that answers by date."""
    parts = url.split("?")[0].rstrip("/").split("/")
    return date.fromisoformat(parts[-2]), date.fromisoformat(parts[-1])
