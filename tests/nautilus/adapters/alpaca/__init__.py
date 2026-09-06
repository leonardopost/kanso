"""The broker adapter's suite, and the frozen wire it replays.

Nothing here opens a socket, resolves a credential or carries a recorded secret. Every row
below is the shape the live account answered with on the read-only pass, written out in
full so a test asserts against what was measured rather than against what was expected —
which is the failure this build has already paid for once, when a green suite hid eight
defects because its fixtures encoded a hope.

Where a row carries a field the measurement did not cover, it is said so here and no test
asserts on it. Two of them are worth naming:

* the daily bar's high and low, which were not part of the two-tape comparison; the open,
  close, volume and trade count are the measured figures, and the high and low are set to
  the wider of the two measured prices only so the row is a bar the engine will build;
* the equity asset row's three increment fields, whose *absence* was measured — they are
  crypto fields, and an overlay that required them would refuse every equity there is. The
  row below therefore omits them, exactly as the live one did.

The keys with a credential in them appear nowhere. A test that needed one would be a test
that reached the network, and that fails this milestone rather than passing it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kanso.nautilus.adapters.alpaca.config import Response

PAPER_KEY = "PKTESTNOTASECRET0000"
LIVE_KEY = "AKTESTNOTASECRET0000"
SECRET = "test-secret-not-a-secret"
"""Values that are not credentials, spelled so a leak scan can look for them. The prefixes
are the only part that matters: one is a paper key and the other is not."""

ACCOUNT_NUMBER = "PA3XYZTEST"

CLOCK: Mapping[str, Any] = {
    "timestamp": "2026-09-05T16:04:05.123456789-04:00",
    "is_open": True,
    "next_open": "2026-09-08T09:30:00-04:00",
    "next_close": "2026-09-08T16:00:00-04:00",
}
"""The clock endpoint's four measured keys, with the measured offset."""

ASSET: Mapping[str, Any] = {
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "class": "us_equity",
    "exchange": "NASDAQ",
    "symbol": "AAPL",
    "name": "Apple Inc. Common Stock",
    "status": "active",
    "tradable": True,
    "marginable": True,
    "shortable": True,
    "easy_to_borrow": True,
    "fractionable": True,
}
"""The measured asset row for AAPL. `min_order_size`, `min_trade_increment` and
`price_increment` are absent because they were absent live: they are crypto fields."""

ORDER_KEYS: tuple[str, ...] = (
    "asset_class",
    "asset_id",
    "canceled_at",
    "client_order_id",
    "created_at",
    "expired_at",
    "expires_at",
    "extended_hours",
    "failed_at",
    "filled_at",
    "filled_avg_price",
    "filled_qty",
    "hwm",
    "id",
    "legs",
    "limit_price",
    "notional",
    "order_class",
    "order_type",
    "position_intent",
    "qty",
    "replaced_at",
    "replaced_by",
    "replaces",
    "side",
    "source",
    "status",
    "stop_price",
    "submitted_at",
    "subtag",
    "symbol",
    "time_in_force",
    "trail_percent",
    "trail_price",
    "type",
    "updated_at",
)
"""The 35 keys an order row carries, measured in full. `order_type` and `type` are both
present and hold the same value, which is why the parser reads one and falls back to the
other rather than assuming either."""

POSITION_KEYS: tuple[str, ...] = (
    "asset_class",
    "asset_id",
    "asset_marginable",
    "avg_entry_price",
    "change_today",
    "cost_basis",
    "current_price",
    "exchange",
    "lastday_price",
    "market_value",
    "qty",
    "qty_available",
    "side",
    "symbol",
    "unrealized_intraday_pl",
    "unrealized_intraday_plpc",
    "unrealized_pl",
    "unrealized_plpc",
)
"""The keys a position row carries, measured in full. `qty` and `qty_available` differ
whenever shares are held against a resting order."""


def order(**changes: Any) -> dict[str, Any]:
    """One order row carrying every measured key, with `changes` applied.

    A market buy of ten shares, accepted and unfilled. Built key by key from the measured
    set so a test that adds a field it never saw is visible as such.
    """
    row: dict[str, Any] = {
        "asset_class": "us_equity",
        "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "canceled_at": None,
        "client_order_id": "O-20260905-160405-001-000-1",
        "created_at": "2026-09-05T16:04:05.123456789Z",
        "expired_at": None,
        "expires_at": "2026-09-05T20:00:00Z",
        "extended_hours": False,
        "failed_at": None,
        "filled_at": None,
        "filled_avg_price": None,
        "filled_qty": "0",
        "hwm": None,
        "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
        "legs": None,
        "limit_price": None,
        "notional": None,
        "order_class": "",
        "order_type": "market",
        "position_intent": "buy_to_open",
        "qty": "10",
        "replaced_at": None,
        "replaced_by": None,
        "replaces": None,
        "side": "buy",
        "source": "access_key",
        "status": "new",
        "stop_price": None,
        "submitted_at": "2026-09-05T16:04:05.200000000Z",
        "subtag": None,
        "symbol": "AAPL",
        "time_in_force": "day",
        "trail_percent": None,
        "trail_price": None,
        "type": "market",
        "updated_at": "2026-09-05T16:04:05.300000000Z",
    }
    row.update(changes)
    return row


def position(**changes: Any) -> dict[str, Any]:
    """One position row carrying every measured key, with `changes` applied."""
    row: dict[str, Any] = {
        "asset_class": "us_equity",
        "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "asset_marginable": True,
        "avg_entry_price": "303.42",
        "change_today": "0.0031",
        "cost_basis": "3034.2",
        "current_price": "304.36",
        "exchange": "NASDAQ",
        "lastday_price": "303.42",
        "market_value": "3043.6",
        "qty": "10",
        "qty_available": "4",
        "side": "long",
        "symbol": "AAPL",
        "unrealized_intraday_pl": "9.4",
        "unrealized_intraday_plpc": "0.0031",
        "unrealized_pl": "9.4",
        "unrealized_plpc": "0.0031",
    }
    row.update(changes)
    return row


SIP_BAR: Mapping[str, Any] = {
    "t": "2026-08-03T04:00:00Z",
    "o": 309.58,
    "h": 309.58,
    "l": 303.42,
    "c": 303.42,
    "v": 75_314_280,
    "n": 1_388_384,
    "vw": 305.11,
}
"""AAPL's 2026-08-03 daily bar on the consolidated tape. The open, close, volume and trade
count are measured; the high, low and volume-weighted price are not asserted on anywhere."""

IEX_BAR: Mapping[str, Any] = {
    "t": "2026-08-03T04:00:00Z",
    "o": 309.765,
    "h": 309.765,
    "l": 303.41,
    "c": 303.41,
    "v": 3_242_233,
    "n": 54_071,
    "vw": 305.09,
}
"""The same session on one venue's slice of the tape: a different open, a different close
and a twentieth of the volume. Two series, not two readings of one."""


@dataclass(frozen=True, slots=True)
class Asked:
    """One request the transport was handed, as a test sees it."""

    method: str
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    keys: tuple[str, ...]


Answer = Callable[[str, Mapping[str, str]], Response]


@dataclass
class Replay:
    """A transport that answers from frozen bodies and remembers every request."""

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
        self.asked.append(Asked(method, url, dict(params), dict(headers), tuple(keys)))
        return self.answer(url, params)


def body(payload: Any, status: int = 200) -> Response:
    """A response carrying `payload` as JSON."""
    return Response(status=status, body=json.dumps(payload).encode())
