"""What the survey asks, which is the difference between an answer and a wrong answer.

`kanso data adapters --check` is the one screen an operator reads before deciding whether
to buy a subscription, so the question it asks has to be one the source can answer.

Two datasets make that concrete and both are here. A price series is continuous: a
fortnight of it holds bars, so a fortnight is a fair question and an empty answer to it
means something. A reference listing is an event series: statements are quarterly and
filings episodic, so a fortnight of either holds nothing in the ordinary case, and asking
one about a fortnight guarantees an empty page whatever the plan says. The listings are
therefore asked with no date window at all — measured: the identical request with the
dates taken off comes back with a full page — and the tests here assert on the *requests*,
because an outcome that came out right after the wrong question was asked is a coincidence
rather than a property.

Nothing here opens a socket or reads a credential: the key is a string this module
invented and every answer is a frozen body.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from kanso.data.adapters.massive import ADAPTER, API_KEY
from kanso.data.adapters.massive.client import Response
from kanso.data.adapters.massive.entitlement import BARS
from kanso.data.adapters.massive.survey import MARKET_NOTE, TARGETS, Target, survey
from kanso.data.registry import Reach
from kanso.workspace import Workspace, init

from . import Replay, bar, definition, missing, nothing, refused, served

TODAY = date(2026, 9, 4)
"""The day the survey runs on. Frozen: a probe's recent window is measured from today, and
a test that drifted with the calendar would prove nothing."""

KEY = "a-key-this-test-invented"

OPTION = "O:AAPL261218C00250000"
FUTURE = "ESZ6"
"""The two keys that expire, which the survey discovers rather than assumes."""

CONTRACTS = "/v3/reference/options/contracts"
TICKERS = "/v3/reference/tickers"

LISTINGS = {
    "/v3/reference/splits": "splits",
    "/v3/reference/dividends": "dividends",
    "/vX/reference/financials": "financials",
    "/v1/reference/sec/filings": "filings",
}
"""Where each reference listing lives, version prefix and all. Spelled out here rather
than imported from the adapter: a fixture that took its paths from the code under test
would follow a wrong one instead of catching it."""


def path_of(url: str) -> str:
    """The path a request names, with the host and the query stripped."""
    return "/" + url.split("//", 1)[-1].partition("/")[2].split("?", 1)[0]


def windowed(params: Mapping[str, str]) -> bool:
    """Whether a request carries a date window in its parameters."""
    return any(name.endswith((".gte", ".lte")) for name in params)


@dataclass
class Vendor:
    """A frozen vendor complete enough for a whole survey, under one plan.

    `refuses` is the set of listings this plan excludes, refused with the vendor's
    sentence. Every other listing behaves as the live source was measured behaving: a
    fourteen-day window comes back `200` with no rows, and the identical request with the
    dates taken off comes back `200` with a page. A fixture that answered the fortnight
    with rows would let a survey ask about a fortnight and still look right, which is
    exactly how two entitled listings came to be printed as `not_entitled`.
    """

    refuses: frozenset[str] = frozenset()
    asked: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url: str, params: Mapping[str, str]) -> Response:
        path = path_of(url)
        self.asked.append((path, dict(params)))
        if path.startswith(f"{CONTRACTS}/"):
            return served([definition(path.rsplit("/", 1)[1])])
        if path == CONTRACTS:
            return served([{"ticker": OPTION}])
        if path.startswith(f"{TICKERS}/"):
            return served([definition(path.rsplit("/", 1)[1])])
        if path == TICKERS:
            return served([{"ticker": FUTURE}])
        if path in LISTINGS:
            return self._listing(LISTINGS[path], params)
        if path.startswith("/v2/aggs/ticker/"):
            return served([bar(date.fromisoformat(path.rstrip("/").rsplit("/", 2)[1]))])
        if path.startswith(("/v3/trades/", "/v3/quotes/")):
            return served([{"sip_timestamp": _ns(TODAY), "price": 1.0, "size": 1}])
        return missing()

    def _listing(self, name: str, params: Mapping[str, str]) -> Response:
        if name in self.refuses:
            return refused()
        return nothing() if windowed(params) else served([{"ticker": "AAPL"}])


def _ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A workspace whose vendor key resolves, so the survey can be run at all."""
    found: Workspace = init(tmp_path / "ws")
    (found.root / ".env").write_text(f"{API_KEY}={KEY}\n", encoding="utf-8")
    return found


def run(ws: Workspace, vendor: Vendor) -> dict[tuple[str, str], Reach]:
    """The whole survey against one frozen vendor, keyed by class and dataset."""
    found = survey(ADAPTER, ws, transport=Replay(vendor), as_of=TODAY)
    assert found.reachable is True
    return {(item.asset_class, item.dataset): item for item in found.reach}


def listing_requests(vendor: Vendor) -> list[tuple[str, dict[str, str]]]:
    """Every request the survey sent to one of the four reference listings."""
    return [(path, params) for path, params in vendor.asked if path in LISTINGS]


# --- the question the listings are asked --------------------------------------------


def test_a_listing_whose_fortnight_holds_nothing_is_reported_entitled(ws: Workspace) -> None:
    """The defect this file exists for. Statements are quarterly and filings episodic, so
    an empty fortnight is the ordinary case; printing `not_entitled` for one sends an
    operator to buy a subscription they already hold."""
    vendor = Vendor()

    found = run(ws, vendor)

    for dataset in ("splits", "dividends", "financials", "filings"):
        assert found[("stocks", dataset)].outcome == "ok", dataset


def test_no_listing_is_asked_over_a_date_window_at_all(ws: Workspace) -> None:
    """The outcome above must come from the right question, not from a lucky fixture: an
    empty page can only be told from a refusal by asking something the source can answer."""
    vendor = Vendor()

    run(ws, vendor)

    sent = listing_requests(vendor)
    assert len(sent) == 4
    assert not any(windowed(params) for _, params in sent), sent


def test_a_listing_is_asked_of_the_whole_market_and_reported_without_a_key(
    ws: Workspace,
) -> None:
    """Unfiltered as well as undated: an empty answer for one issuer is that issuer."""
    vendor = Vendor()

    found = run(ws, vendor)

    assert not any("ticker" in params for _, params in listing_requests(vendor))
    assert found[("stocks", "splits")].ticker is None
    assert found[("stocks", "bars")].ticker == "AAPL"


def test_the_page_size_survives_the_window_being_taken_off(ws: Workspace) -> None:
    """Only the dates are dropped. The financials listing rejects a page of a thousand
    outright, so a request that lost its own `limit` on the way would fail as a client
    error and be read as a dataset the plan excludes."""
    vendor = Vendor()

    run(ws, vendor)

    assert all("limit" in params for _, params in listing_requests(vendor))


def test_a_listing_the_plan_refuses_is_still_an_entitlement_failure(ws: Workspace) -> None:
    """Taking an empty page seriously must not cost the answer that a refusal is a plan.
    A `403` is evidence about the subscription and stays evidence about it."""
    vendor = Vendor(refuses=frozenset({"filings"}))

    found = run(ws, vendor)

    assert found[("stocks", "filings")].outcome == "not_entitled"
    assert found[("stocks", "financials")].outcome == "ok"


def test_a_listing_is_never_floored_and_a_price_series_always_is(ws: Workspace) -> None:
    """A floor is the oldest row of a continuous series. In a sparse one the same search
    returns an arbitrary year, so the column is left empty rather than filled with it."""
    found = run(ws, Vendor())

    assert found[("stocks", "financials")].floor is None
    assert found[("stocks", "bars")].floor is not None


def test_the_notes_say_which_question_the_listings_were_asked(ws: Workspace) -> None:
    """An operator reading `ok` beside a listing has to know it was asked market-wide and
    undated, because that is what makes the line mean what it appears to mean."""
    found = survey(ADAPTER, ws, transport=Replay(Vendor()), as_of=TODAY)

    assert MARKET_NOTE in found.notes
    assert "no date window" in MARKET_NOTE


# --- the table that decides it -------------------------------------------------------


def test_exactly_the_reference_listings_are_marked_sparse() -> None:
    """The four event series and nothing else. A price series marked sparse would lose its
    floor; a listing left dense would be asked the fortnight that started all this."""
    sparse = {target.dataset for target in TARGETS if target.sparse}

    assert sparse == {"splits", "dividends", "financials", "filings"}
    assert all(not target.sparse for target in TARGETS if target.dataset == "bars")


def test_a_sparse_target_is_asked_with_its_window_off_and_a_dense_one_as_it_stands() -> None:
    for target in TARGETS:
        expected = target.endpoint.unwindowed() if target.sparse else target.endpoint
        assert target.question == expected


def test_a_target_is_dense_unless_it_says_otherwise() -> None:
    """The default is the safe one: a dataset nobody thought about is asked the ordinary
    way and floored, rather than silently having its window dropped."""
    assert Target("stocks", BARS).sparse is False
    assert Target("stocks", BARS).question is BARS


def test_every_answer_the_survey_gives_is_one_of_the_named_outcomes(ws: Workspace) -> None:
    """`unavailable` and `unprobed` are the two that are not an answer about a plan, and
    neither may appear where the vendor answered every question it was asked."""
    found = run(ws, Vendor(refuses=frozenset({"filings"})))

    assert {item.outcome for item in found.values()} <= {"ok", "not_entitled"}
