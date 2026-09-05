"""The frozen Massive wire the CLI slice replays, and the plan it encodes.

Nothing here opens a socket, reads a credential or carries a recorded secret: every answer
is a response body written out in full, and the key the fixture sets is a string this
module invented. The plan it encodes is the shape that makes a per-class answer wrong —
two classes have their aggregates included and their ticks excluded, and a third has
neither — and it serves a range straddling the floor with a success status and a short
series, which is the behaviour that makes a short answer indistinguishable from a complete
one until it is measured.

**Corrected against the live source.** This fixture used to route on the tail of a path,
which made every version prefix equally acceptable and every reference endpoint answer for
every key; it ignored `limit`; and an unmatched path fell through into the tick branch
rather than being answered. Each of those was a fixture encoding what the adapter assumed
rather than what the source does, and each hid a defect that only a live request found. It
now routes on whole paths, holds each listing to the page size it serves, rejects an option
key on the generic reference, and answers a 404 for a path the source does not serve.

It also used to answer every listing request with rows, a probe's fourteen-day window
included. That is a hope rather than a measurement — statements are quarterly and filings
episodic, so the live source answers a fortnight of either with an empty page and the same
request with the dates taken off with a full one — and it hid a survey that asked the wrong
question and printed `not_entitled` for two listings this key is entitled to.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from kanso.data.adapters.massive.client import Response

from ..data.adapters.massive import (
    bar,
    definition,
    missing,
    nothing,
    over_limit,
    refused,
    rejected,
    served,
)
from . import massive_store

KEY = "a-key-this-test-invented"
"""What the workspace's variable is set to. It is never asserted on: a value that reached
an output would be a leak, and the test that would catch it asserts its absence."""

OPTION = "O:AAPL261218C00250000"
FUTURE = "ESZ6"
"""The two keys that expire, which the survey discovers rather than assumes."""

PLAN: dict[tuple[str, str], date | None] = {
    ("stocks", "bars"): date(2003, 9, 10),
    ("stocks", "trades"): date(2003, 9, 10),
    ("stocks", "quotes"): date(2003, 9, 10),
    ("options", "bars"): date(2024, 9, 3),
    ("options", "trades"): None,
    ("options", "quotes"): None,
    ("futures", "bars"): None,
    ("forex", "bars"): date(2024, 9, 4),
    ("forex", "quotes"): None,
    ("indices", "bars"): date(2023, 3, 1),
}
"""One frozen plan: per class and dataset, the day the source serves from, or `None` for a
dataset the plan excludes, and four different floors among the ones it serves.

Options and forex have their aggregates included and their ticks excluded, which is the
shape that makes a per-class answer wrong and a per-dataset answer right; futures has
nothing, which is what a whole class outside a plan looks like."""

PREFIXES = {"O:": "options", "C:": "forex", "I:": "indices"}


def _class_of(ticker: str) -> str:
    """Which class a key belongs to, by the prefix it carries."""
    if ticker == FUTURE:
        return "futures"
    return PREFIXES.get(ticker[:2], "stocks")


def _ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


PAGE = 500
"""How many rows one answer carries at most. The source pages, so a fixture that served an
unbounded range would answer a floor search — which asks from the epoch — with twenty
thousand rows, and would be measuring this module rather than the adapter."""


def _row(dataset: str, day: date) -> dict[str, Any]:
    """One row of the right shape for this dataset, timed on `day`.

    Aggregates carry milliseconds under `t` and ticks nanoseconds under `sip_timestamp`,
    which is what the floor probe reads a day off; a fixture that got the unit wrong would
    report a floor in 1970 and pass every other assertion.
    """
    if dataset == "bars":
        return bar(day)
    return {"sip_timestamp": _ns(day), "price": 100.0, "size": 1}


def _sessions(start: date, end: date) -> list[date]:
    """The days this source has anything for: weekdays, which is when the market is open.

    The flat-file store below holds an object per weekday because that is what the vendor
    writes, so the request path serves the same set — otherwise the two transports would
    disagree about which days exist, and a test comparing them would be comparing two
    fixtures rather than two code paths.
    """
    span = min((end - start).days, PAGE - 1)
    days = [start + timedelta(days=step) for step in range(span + 1)]
    return [day for day in days if day.weekday() < 5]


def _range(dataset: str, ticker: str, window: tuple[date, date]) -> Response:
    """What the source answers for one window: refused, truncated to the floor, or empty.

    A range straddling the floor comes back with a success status and a series beginning
    at the floor, never with a warning — which is the behaviour that makes a short answer
    impossible to tell from a complete one without measuring it.
    """
    floor = PLAN[(_class_of(ticker), dataset)]
    if floor is None:
        return refused()
    start = max(window[0], floor)
    if start > window[1]:
        return nothing()
    rows = [_row(dataset, day) for day in _sessions(start, window[1])]
    return served(rows) if rows else nothing()


def _window(params: Mapping[str, str], path: str) -> tuple[date, date]:
    """The window a request names, whether in its path or in its parameters."""
    if "timestamp.gte" in params:
        return date.fromisoformat(params["timestamp.gte"]), date.fromisoformat(
            params["timestamp.lte"]
        )
    parts = path.split("?")[0].rstrip("/").split("/")
    return date.fromisoformat(parts[-2]), date.fromisoformat(parts[-1])


MARKETS = {
    "stocks": "stocks",
    "options": "options",
    "futures": "futures",
    "forex": "fx",
    "indices": "indices",
}
"""The market each class is listed under, which a reference row names and a key does not."""


def _definition(ticker: str) -> dict[str, Any]:
    """One reference row, complete enough to resolve into an instrument.

    Completeness is the point: nothing in the adapter fills a missing venue, currency or
    listing date in, so a row that omits one fails that key by name — which is a fixture
    that would pass a probe and fail a resolution, and the difference is worth having.
    """
    found = dict(definition(ticker))
    found |= {
        "market": MARKETS[_class_of(ticker)],
        "primary_exchange": "XNAS",
        "currency_name": "usd",
        "list_date": "1980-12-12",
    }
    return found


TICKERS = "/v3/reference/tickers"
"""The generic reference: the universe listing, and every key the vendor carries there."""

CONTRACTS = "/v3/reference/options/contracts"
"""Where an option contract lives. The generic reference does not carry one and rejects an
option key outright, so a lookup sent to the wrong one of the two is a rejected request
here as it is live — which is what makes the endpoint a probe chooses load bearing."""

LISTINGS = {
    "/v3/reference/splits": "splits",
    "/v3/reference/dividends": "dividends",
    "/vX/reference/financials": "financials",
    "/v1/reference/sec/filings": "filings",
}
"""Where each reference listing lives, version prefix and all.

Measured per listing: the vendor versions them one at a time, so the prefix beside a
neighbouring endpoint says nothing about this one, and a listing asked for under the wrong
one answers a plain 404 — no envelope, no sentence, neither a refusal nor an empty window.
Spelled out here rather than imported from the adapter: a fixture that took the path from
the code under test would follow a wrong prefix instead of catching it."""

CEILING = {"splits": 1000, "dividends": 1000, "financials": 100, "filings": 1000}
"""The widest page each listing serves, measured, and every one of them written down.

A ceiling belongs to one endpoint. The financials listing rejects a page of a thousand
outright — a client error, not a trimmed answer, so a limit borrowed from the listing next
door yields no rows at all and reads as a dataset the plan excludes."""

EXCLUDED = frozenset({"filings"})
"""The listings this plan does not include, refused with the sentence that means four
things — so the survey has to establish which of the four by asking, here too."""


def answer(url: str, params: Mapping[str, str]) -> Response:
    """The whole frozen vendor: reference, aggregates, ticks, the listings and the store.

    A path this source does not serve is a 404 and not a fall-through, because a
    fall-through turns a wrong path into whatever the last branch happens to do — which
    here was an `IndexError` reported as a transport failure, and an unreachable dataset
    reported as an unavailable one.

    The object store answers on the same transport because that is how the adapter reaches
    it: the signed requests whose *body* matters share the REST client's connection and its
    quota. Only the bulk download of a whole object goes another way, through `download`.
    """
    path = "/" + url.split("//", 1)[-1].split("/", 1)[-1].split("?")[0]
    if path == f"/{massive_store.BUCKET}" or path.startswith(f"/{massive_store.BUCKET}/"):
        return massive_store.answer(path, params)
    if path.startswith(f"{CONTRACTS}/"):
        return served([_definition(path.split(f"{CONTRACTS}/", 1)[1])])
    if path == CONTRACTS:
        return served([{"ticker": OPTION}])
    if path.startswith(f"{TICKERS}/"):
        return _generic(path.split(f"{TICKERS}/", 1)[1])
    if path == TICKERS:
        return served([{"ticker": FUTURE}])
    if path in LISTINGS:
        return _listing(LISTINGS[path], params)
    if path.startswith("/v2/aggs/ticker/"):
        ticker = path.split("/v2/aggs/ticker/")[1].split("/")[0]
        return _range("bars", ticker, _window(params, path))
    if path.startswith("/v3/trades/") or path.startswith("/v3/quotes/"):
        dataset = "trades" if path.startswith("/v3/trades/") else "quotes"
        return _range(dataset, path.split(f"/v3/{dataset}/")[1], _window(params, path))
    return missing()


def _generic(ticker: str) -> Response:
    """The generic reference, which rejects an option key rather than answering for it."""
    return rejected() if ticker.startswith("O:") else served([_definition(ticker)])


DATED = ("execution_date", "ex_dividend_date", "period_of_report_date", "filing_date")
"""The date fields the four listings are windowed on, one per listing.

Named here so a windowed request can be recognised, because a listing answers one very
differently from an unwindowed one and this fixture used not to notice the difference.
"""


def _listing(name: str, params: Mapping[str, str]) -> Response:
    """One reference listing, at the page size it serves and under the plan it is in.

    **Corrected against the live source.** This used to answer every request with rows,
    including a probe's fourteen-day window, which is a fixture encoding a hope: these are
    event series, and the live source answers a fortnight of one with an empty page —
    measured, on the day of the correction, for statements, filings and splits alike — and
    the identical request with the dates taken off with a full one. Answering the fortnight
    with rows made a probe that asked the wrong question look right, and hid the defect that
    reported two entitled listings as `not_entitled` on the one screen an operator reads
    before buying a subscription they already hold.
    """
    ceiling = CEILING.get(name)
    if ceiling is not None and int(params.get("limit", ceiling)) > ceiling:
        return over_limit(ceiling)
    if name in EXCLUDED:
        return refused()
    if any(f"{field}.gte" in params for field in DATED):
        return nothing()
    return served([{"ticker": "AAPL"}])
