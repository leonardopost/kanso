"""The exchange's recorded answers, served back through a transport that opens no socket.

Every body under `fixtures/` was recorded from the public API on 2026-09-30 with no
credential; `fixtures/provenance.json` gives each one's url, host, status and UTC instant,
and says which page was trimmed to the rows the tests read. A test asks for a recording by
its file name and is served its bytes and its recorded status unchanged.

`fixtures/history/` holds what the public-history loaders read — candles, the trade-archive
listing, the archives themselves and funding — recorded by driving the loaders against the
exchange; its own `provenance.json` gives each answer's url, parameters, host, status and
instant, and `History` serves an answer only for exactly the request it was recorded for.
A request for a file that names a `Range` is answered as the file host was measured
answering one on 2026-10-02 — 206 and the bytes of the range, fewer when it runs past the
end, and 416 when it starts at or past the end — from the recorded file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kanso.nautilus.adapters.okx.reference import Response

FIXTURES = Path(__file__).parent / "fixtures"

PROVENANCE: dict[str, Any] = json.loads((FIXTURES / "provenance.json").read_text("utf-8"))


def recorded(name: str) -> Response:
    """One recorded answer: its body as served and its status as recorded."""
    return Response(
        status=int(PROVENANCE["files"][name]["status"]),
        body=(FIXTURES / name).read_bytes(),
    )


def row(inst_id: str) -> dict[str, Any]:
    """One recorded listing row, as the exchange served it."""
    rows: list[dict[str, Any]] = json.loads(recorded(f"swap_{inst_id}.json").body)["data"]
    return rows[0]


BY_ID: dict[str | None, str] = {
    None: "swap_page.json",
    "BTC-USDT-SWAP": "swap_BTC-USDT-SWAP.json",
    "ETH-USDT-SWAP": "swap_ETH-USDT-SWAP.json",
    "BTC-USD-SWAP": "swap_BTC-USD-SWAP.json",
    "USDC-USDT-SWAP": "swap_USDC-USDT-SWAP.json",
    "NOPE-USDT-SWAP": "swap_unknown.json",
    "btc-usdt-swap": "swap_malformed.json",
}
"""Which recording answers which `instId`, exactly as each was asked of the exchange."""


@dataclass
class Replay:
    """A transport serving the recording for each request, and remembering what it sent."""

    answers: Mapping[str | None, str] = field(default_factory=lambda: dict(BY_ID))
    asked: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url: str, params: Mapping[str, str]) -> Response:
        self.asked.append((url, dict(params)))
        return recorded(self.answers[params.get("instId")])


HISTORY = FIXTURES / "history"

HISTORY_PROVENANCE: dict[str, Any] = json.loads((HISTORY / "provenance.json").read_text("utf-8"))

THROTTLED = "throttled__"
"""The prefix of an answer recorded as a throttle: served only when a test asks for it."""


def answer(name: str) -> Response:
    """One recorded history answer, by its file name, with its recorded status."""
    return Response(
        status=int(HISTORY_PROVENANCE["files"][name]["status"]),
        body=(HISTORY / name).read_bytes(),
    )


def recorded_for(url: str, params: Mapping[str, str]) -> str | None:
    """The name of the answer recorded for exactly this request, if there is one."""
    for name, entry in HISTORY_PROVENANCE["files"].items():
        if name.startswith(THROTTLED):
            continue
        if entry["url"] == url and entry["params"] == dict(params):
            return str(name)
    return None


@dataclass
class History:
    """A transport serving the recorded answer to each request the loaders send, and
    remembering what they sent. A request nothing was recorded for fails the test: a loader
    asking for something the exchange was never asked is a loader the recordings do not
    cover."""

    asked: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(
        self, url: str, params: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> Response:
        self.asked.append((url, dict(params)))
        name = recorded_for(url, params)
        if name is None:
            raise AssertionError(f"nothing was recorded for {url} {dict(params)}")
        served = answer(name)
        wanted = (headers or {}).get("Range")
        if wanted is None:
            return served
        first, _, last = wanted.removeprefix("bytes=").partition("-")
        if int(first) >= len(served.body):
            return Response(416, b"<Error><Code>InvalidRange</Code></Error>")
        return Response(206, served.body[int(first) : int(last) + 1])
