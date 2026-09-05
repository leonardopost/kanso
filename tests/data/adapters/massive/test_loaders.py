"""The request-path loaders: bars, trades and quotes, over frozen wire.

Nothing here opens a socket or reads a credential; every answer is a body written out in
full and replayed through the injectable transport, and the clock is frozen so a probe's
recent window is the same two weeks on every host and in every year.

What these tests are for, beyond the obvious round trip:

* the availability invariant, which is the reason the adapter exists at all. A bar is
  stamped at the close of its own window and never at its open; a tick's reference time is
  the venue's instant and its availability is the tape's, so `ts_init >= ts_event` is a
  measured property of the data rather than a copy of one field into the other.
* the four outcomes, established by probing. The vendor's sentence is byte identical for
  all of them, so the same `WARNING` bytes are fed into scenarios that must come out
  differently: a test that passed by reading the message would have to pass four
  contradictory ways at once.
* the parameters that quietly return nothing. `limit` caps base aggregates before roll-up,
  so a multiplier above one must carry no limit at all.
* coverage as what was served. A range the vendor truncates at HTTP 200 must leave a
  manifest that records the shorter span, and the difference must be nameable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kanso.data.adapters.massive.client import MassiveClient, Response
from kanso.data.adapters.massive.entitlement import PROBE_SPAN, settled_end
from kanso.data.adapters.massive.errors import (
    BelowFloorError,
    MalformedRequestError,
    NotEntitledError,
    TransportError,
)
from kanso.data.adapters.massive.loaders.bars import (
    AGGREGATE_LIMIT,
    MassiveBarsLoader,
    MassiveSpec,
    Request,
    aggregate,
    bars_endpoint,
    futures_year,
    instant,
    numbers,
    shift,
    ticks,
    vendor_ticker,
    vendor_window,
)
from kanso.data.adapters.massive.loaders.quotes import MassiveQuotesLoader, quotes_endpoint
from kanso.data.adapters.massive.loaders.trades import MassiveTradesLoader, trades_endpoint
from kanso.data.loader import DatasetRef, Loader, utc_day
from kanso.data.manifest import shortfall
from kanso.data.publication import check_delayed
from kanso.errors import PreconditionError, ValidationError
from kanso.workspace import Workspace, init

from . import WARNING, Replay, bar, definition, nothing, refused, rejected, window_of
from . import served as served_rows

TODAY = date(2024, 3, 10)
"""The frozen probe day. Every window in this module is stated against it."""

SETTLED = settled_end(TODAY)
FLOOR = date(2024, 1, 5)
VENUE = "XNAS"
KEY = "frozen-key-not-a-credential"

NS = 1_000_000_000
MS = 1_000_000

Answer = Any


# --- the frozen vendor ---------------------------------------------------------


def days(start: date, end: date) -> list[date]:
    """Every calendar day in a span; a fixture has no holiday calendar to consult."""
    return [start + timedelta(days=step) for step in range((end - start).days + 1)]


def asked_window(url: str, params: Mapping[str, str]) -> tuple[date, date]:
    """The window a request names, wherever this endpoint family puts it."""
    if "timestamp.gte" in params:
        return date.fromisoformat(params["timestamp.gte"]), date.fromisoformat(
            params["timestamp.lte"]
        )
    return window_of(url)


def tick(day: date, *, hour: int = 14, lag_ns: int = 1_000, **fields: Any) -> dict[str, Any]:
    """One tape row: a venue instant, a tape instant a microsecond later, and a price."""
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    venue_ns = int(moment.timestamp()) * NS
    row: dict[str, Any] = {
        "participant_timestamp": venue_ns,
        "sip_timestamp": venue_ns + lag_ns,
        "price": 100.0,
        "size": 10,
        "bid_price": 99.5,
        "ask_price": 100.5,
        "bid_size": 4,
        "ask_size": 6,
        "sequence_number": 7,
    }
    row.update(fields)
    return row


def source(
    *,
    floor: date = FLOOR,
    last: date = SETTLED,
    cap: int | None = None,
    hole: tuple[date, date] | None = None,
) -> Answer:
    """A vendor serving one daily series from `floor` to `last`, and truncating silently.

    `cap` is how it truncates: an answer beginning at the start of what it holds and
    stopping after that many rows, with HTTP 200 and no warning, which is what a range
    straddling the floor was measured to come back as. `hole` is a stretch it serves
    nothing over, the shape a halted or not-yet-listed instrument has.
    """

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        start, end = asked_window(url, params)
        first, stop = max(start, floor), min(end, last)
        if first > stop:
            return nothing()
        held = [day for day in days(first, stop) if hole is None or not hole[0] <= day <= hole[1]]
        rows = [bar(day) for day in (held[:cap] if cap else held)]
        return served_rows(rows) if rows else nothing()

    return answer


def client_of(answer: Answer) -> tuple[MassiveClient, Replay]:
    """A client over a replayed transport, and the recorder that saw every request."""
    replay = Replay(answer)
    return MassiveClient(KEY, transport=replay), replay


def bars_loader(answer: Answer) -> tuple[MassiveBarsLoader, Replay]:
    client, replay = client_of(answer)
    return MassiveBarsLoader(client=client, as_of=TODAY), replay


def spec(**overrides: Any) -> dict[str, Any]:
    """A stocks bar spec whose whole range sits above the frozen floor."""
    base: dict[str, Any] = {
        "asset_class": "stocks",
        "instruments": ["AAPL"],
        "venue": VENUE,
        "resolution": "1m",
        "start": date(2024, 2, 1),
        "end": date(2024, 3, 1),
    }
    base.update(overrides)
    return base


# --- the vendor's spellings ----------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "asset_class", "expected"),
    [
        ("AAPL", "stocks", "AAPL"),
        ("AAPL240119C00190000", "options", "O:AAPL240119C00190000"),
        ("O:AAPL240119C00190000", "options", "O:AAPL240119C00190000"),
        ("EURUSD", "forex", "C:EURUSD"),
        ("C:EURUSD", "forex", "C:EURUSD"),
        ("NDX", "indices", "I:NDX"),
        ("I:NDX", "indices", "I:NDX"),
    ],
)
def test_vendor_ticker_prefixes(symbol: str, asset_class: str, expected: str) -> None:
    assert vendor_ticker(symbol, asset_class, TODAY) == expected


def test_vendor_ticker_refuses_a_class_the_adapter_does_not_serve() -> None:
    with pytest.raises(MalformedRequestError) as caught:
        vendor_ticker("BTCUSD", "crypto", TODAY)
    assert "crypto" in caught.value.message


@pytest.mark.parametrize(
    ("symbol", "on", "expected"),
    [
        ("ESZ4", date(2014, 6, 1), "ESZ14"),
        ("ESZ4", date(2026, 1, 1), "ESZ24"),
        ("ESZ4", date(2024, 3, 1), "ESZ24"),
        ("ESZ24", date(2014, 6, 1), "ESZ24"),
        ("ESZ2024", date(1999, 1, 1), "ESZ24"),
        ("ZNZ5", date(2025, 6, 1), "ZNZ25"),
        ("6EM5", date(2025, 1, 1), "6EM25"),
        ("esz4", date(2014, 6, 1), "ESZ14"),
    ],
)
def test_futures_year_is_resolved_against_the_window(symbol: str, on: date, expected: str) -> None:
    """A one-digit year names a decade, and the range asked for is what decides which."""
    assert vendor_ticker(symbol, "futures", on) == expected


def test_a_one_digit_futures_year_does_not_follow_the_calendar() -> None:
    """The same code read against two windows is two contracts, which is the whole point."""
    assert vendor_ticker("ESZ4", "futures", date(2014, 12, 1)) != vendor_ticker(
        "ESZ4", "futures", date(2024, 12, 1)
    )


@pytest.mark.parametrize(("digits", "expected"), [("69", 2069), ("70", 1970), ("2024", 2024)])
def test_futures_year_pivots_at_1970(digits: str, expected: int) -> None:
    assert futures_year(digits, TODAY) == expected


def test_a_three_digit_futures_year_is_refused() -> None:
    with pytest.raises(MalformedRequestError):
        futures_year("204", TODAY)


@pytest.mark.parametrize("symbol", ["ES", "ESA4", "ES4", "TOOLONGZ4"])
def test_a_symbol_that_is_not_a_contract_is_refused(symbol: str) -> None:
    with pytest.raises(MalformedRequestError):
        vendor_ticker(symbol, "futures", TODAY)


# --- aggregation, the multiplier and the limit ---------------------------------


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [("1d", (1, "day")), ("5m", (5, "minute")), ("1h", (1, "hour")), ("2w", (2, "week"))],
)
def test_aggregate_reads_multiplier_and_timespan(
    resolution: str, expected: tuple[int, str]
) -> None:
    assert aggregate(resolution) == expected


@pytest.mark.parametrize("resolution", ["0d", "1y"])
def test_aggregate_refuses_a_size_the_vendor_does_not_have(resolution: str) -> None:
    with pytest.raises(MalformedRequestError):
        aggregate(resolution)


def request_for(**overrides: Any) -> Request:
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "venue": VENUE,
        "ticker": "AAPL",
        "asset_class": "stocks",
        "dataset": "bars",
        "resolution": "1d",
        "adjusted": False,
        "publication": "realtime",
        "publication_rule": None,
        "price_precision": 2,
        "size_precision": 0,
    }
    base.update(overrides)
    return Request(**base)


def test_a_multiplier_of_one_carries_a_limit() -> None:
    """No roll-up happens, so the cap applies to exactly the rows that were asked for."""
    params = dict(bars_endpoint(request_for(resolution="1d")).params)
    assert params["limit"] == str(AGGREGATE_LIMIT)


@pytest.mark.parametrize("resolution", ["5m", "15m", "2h"])
def test_a_multiplier_above_one_carries_no_limit(resolution: str) -> None:
    """The vendor caps base aggregates before rolling them up: a limit here returns nothing."""
    assert "limit" not in dict(bars_endpoint(request_for(resolution=resolution)).params)


def test_the_endpoint_carries_the_multiplier_and_the_adjustment() -> None:
    endpoint = bars_endpoint(request_for(resolution="5m", adjusted=True))
    path, params = endpoint.request("AAPL", (date(2024, 1, 1), date(2024, 1, 2)))
    assert path == "/v2/aggs/ticker/AAPL/range/5/minute/2024-01-01/2024-01-02"
    assert params["adjusted"] == "true"
    assert params["sort"] == "asc"


def test_the_tick_endpoints_may_carry_a_limit() -> None:
    """They roll nothing up, so a page size caps the rows asked for rather than the inputs."""
    for endpoint in (trades_endpoint(request_for()), quotes_endpoint(request_for())):
        assert "limit" in dict(endpoint.params)


# --- windows, shifts and units -------------------------------------------------


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [(None, 0), ("1m", 0), ("1h", 0), ("1d", 1), ("1w", 7)],
)
def test_shift_is_the_days_a_close_lies_beyond_its_window(
    resolution: str | None, expected: int
) -> None:
    assert shift(resolution) == timedelta(days=expected)


def test_the_vendor_window_is_widened_at_both_ends() -> None:
    """A bar is stamped at its close, so the point of the first day opened before it."""
    assert vendor_window((date(2024, 2, 1), date(2024, 2, 10)), "1d") == (
        date(2024, 1, 31),
        date(2024, 2, 11),
    )
    assert vendor_window((date(2024, 2, 1), date(2024, 2, 10)), None) == (
        date(2024, 1, 31),
        date(2024, 2, 11),
    )


def test_the_two_epochs_are_read_by_the_endpoint_not_by_magnitude() -> None:
    """Aggregates are milliseconds and ticks are nanoseconds; the same integer is both."""
    row = {"t": 1_700_000_000_000}
    assert instant(row, "t", "ms") == 1_700_000_000_000 * MS
    assert instant(row, "t", "ns") == 1_700_000_000_000


@pytest.mark.parametrize("value", [None, True, "1700000000000"])
def test_a_field_that_is_not_a_number_carries_no_instant(value: Any) -> None:
    assert instant({"t": value}, "t", "ms") is None


def test_numbers_refuses_a_row_missing_a_field_it_needs() -> None:
    assert numbers({"o": 1.0, "h": 2.0}, ("o", "h", "l")) is None
    assert numbers({"o": 1.0, "h": True}, ("o", "h")) is None
    assert numbers({"o": 1, "h": 2.5}, ("o", "h")) == [1.0, 2.5]


def test_ticks_quantises_at_the_declared_precision() -> None:
    assert ticks(123.456, 2) == 12346
    assert ticks(1.0, 0) == 1


# --- the spec ------------------------------------------------------------------


def test_a_class_the_adapter_does_not_serve_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        MassiveSpec.model_validate(spec(asset_class="crypto"))
    assert "asset_class" in str(caught.value.message)


def test_an_end_before_the_start_is_refused() -> None:
    with pytest.raises(ValidationError):
        MassiveSpec.model_validate(spec(start=date(2024, 3, 1), end=date(2024, 2, 1)))


def test_an_unknown_publication_class_is_refused() -> None:
    with pytest.raises(ValidationError):
        MassiveSpec.model_validate(spec(publication="soon"))


def test_a_delayed_spec_must_name_its_rule() -> None:
    with pytest.raises(ValidationError) as caught:
        MassiveSpec.model_validate(spec(publication="delayed"))
    assert "publication_rule" in str(caught.value.message)


def test_a_delayed_spec_must_name_a_declared_rule() -> None:
    with pytest.raises(ValidationError):
        MassiveSpec.model_validate(spec(publication="delayed", publication_rule="whenever"))


def test_a_ticker_override_must_name_an_instrument_in_the_spec() -> None:
    with pytest.raises(ValidationError) as caught:
        MassiveSpec.model_validate(spec(tickers={"MSFT": "MSFT"}))
    assert "tickers" in str(caught.value.message)


# --- discover ------------------------------------------------------------------


def test_discover_probes_and_clamps_the_span_to_the_measured_floor() -> None:
    loader, replay = bars_loader(source())
    found = loader.discover(spec(start=date(2023, 6, 1), end=date(2024, 3, 1)))
    assert len(found) == 1
    ref = found[0]
    assert ref.span == (FLOOR, date(2024, 3, 1))
    assert ref.instrument == f"AAPL.{VENUE}"
    assert ref.type == "bar"
    assert ref.vendor == "massive_bars"
    assert ref.vendor_dataset == "bars"
    assert ref.publication == "realtime"
    assert replay.asked, "a probe was made"


def test_discover_clamps_the_end_to_the_last_settled_day() -> None:
    """A session in progress has no complete bar, so nothing asks about today."""
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(end=date(2024, 12, 31)))[0]
    assert ref.span[1] == SETTLED


def test_a_daily_bars_floor_moves_with_the_close_convention() -> None:
    """The floor is a session day and a bar is stamped at its close, one day later."""
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(resolution="1d", start=date(2023, 6, 1)))[0]
    assert ref.span[0] == FLOOR + timedelta(days=1)


def test_the_recorded_request_carries_the_floor_and_no_credential() -> None:
    loader, replay = bars_loader(source())
    ref = loader.discover(spec())[0]
    params = ref.request_params or {}
    assert params["floor"] == FLOOR.isoformat()
    assert params["probed_on"] == TODAY.isoformat()
    assert KEY not in str(params)
    assert all(KEY not in str(item.params) for item in replay.asked)
    assert any(KEY in str(item.headers) for item in replay.asked), "the key travels in a header"


def test_a_second_instrument_of_the_same_class_reuses_the_entitlement_answer() -> None:
    """The source gates equities per endpoint, so only the floor is asked again.

    Two requests per series and no repeats: establishing the plan and measuring a floor
    both want the recent window, and the second reads the first's answer rather than
    paying for it again.
    """
    loader, replay = bars_loader(source())
    found = loader.discover(spec(instruments=["AAPL", "MSFT"]))
    assert [ref.instrument for ref in found] == [f"AAPL.{VENUE}", f"MSFT.{VENUE}"]
    assert len(replay.asked) == 4, "a recent window and a straddle for each, asked once each"


def test_an_override_replaces_the_derived_vendor_ticker() -> None:
    loader, replay = bars_loader(source())
    loader.discover(spec(tickers={"AAPL": "AAPL.TEST"}))
    assert any("AAPL.TEST" in item.url for item in replay.asked)


def test_a_class_without_this_dataset_is_refused_before_any_request() -> None:
    """Futures carry no tick endpoint here, and nothing needs to be asked to know it."""
    client, replay = client_of(source())
    loader = MassiveQuotesLoader(client=client, as_of=TODAY)
    with pytest.raises(MalformedRequestError) as caught:
        loader.discover(spec(asset_class="futures", instruments=["ESZ4"], resolution=None))
    assert "quotes" in caught.value.message
    assert replay.asked == []


def test_bars_with_no_bar_size_are_refused_before_any_request() -> None:
    """An aggregate with no size has no timespan to ask the vendor for."""
    loader, replay = bars_loader(source())
    with pytest.raises(MalformedRequestError) as caught:
        loader.discover(spec(resolution=None))
    assert "resolution" in caught.value.message
    assert replay.asked == []


def test_ticks_that_name_a_bar_size_are_refused_before_any_request() -> None:
    """A tick spec with a resolution expects an aggregation this endpoint does not perform."""
    client, replay = client_of(source())
    loader = MassiveTradesLoader(client=client, as_of=TODAY)
    with pytest.raises(MalformedRequestError) as caught:
        loader.discover(spec(resolution="1d"))
    assert "resolution" in caught.value.message
    assert replay.asked == []


def test_a_window_after_the_last_settled_day_is_refused() -> None:
    loader, _ = bars_loader(source())
    with pytest.raises(MalformedRequestError) as caught:
        loader.discover(spec(start=date(2024, 6, 1), end=date(2024, 7, 1)))
    assert str(SETTLED) in caught.value.message


def test_a_loader_with_neither_workspace_nor_client_refuses_to_open() -> None:
    with pytest.raises(PreconditionError) as caught:
        MassiveBarsLoader().discover(spec())
    assert "massive_bars" in caught.value.message


# --- the four outcomes, separated by probing -----------------------------------


def blocked_source(*, control: Any, live: Any) -> Answer:
    """A vendor that answers the series one way and the control question another."""

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return control()
        return live()

    return answer


def test_a_refused_series_the_vendor_knows_is_not_entitled() -> None:
    loader, _ = bars_loader(
        blocked_source(control=lambda: served_rows([definition("AAPL")]), live=refused)
    )
    with pytest.raises(NotEntitledError) as caught:
        loader.discover(spec())
    assert "not included in this plan" in caught.value.message
    assert WARNING not in caught.value.message


def test_the_same_refusal_with_an_unknown_key_is_a_malformed_request() -> None:
    """Identical bytes on the wire, a different outcome: the message is never read."""
    loader, _ = bars_loader(blocked_source(control=nothing, live=refused))
    with pytest.raises(MalformedRequestError) as caught:
        loader.discover(spec())
    assert WARNING not in caught.value.message


def test_a_rejected_request_shape_needs_no_second_question() -> None:
    loader, replay = bars_loader(blocked_source(control=nothing, live=rejected))
    with pytest.raises(MalformedRequestError):
        loader.discover(spec())
    assert not any("/v3/reference/" in item.url for item in replay.asked)


def test_a_range_wholly_older_than_the_source_is_a_history_floor() -> None:
    """The most expensive wrong answer this adapter could give is calling this entitlement."""
    loader, _ = bars_loader(source())
    with pytest.raises(BelowFloorError) as caught:
        loader.discover(spec(start=date(2020, 1, 1), end=date(2020, 6, 1)))
    assert caught.value.floor == FLOOR
    assert str(FLOOR) in caught.value.message


def test_a_blocked_series_ends_that_dataset_and_not_the_run() -> None:
    """A universe rarely entitles every class it names, so one refusal is not fatal.

    The blocked index refuses, measured: `I:SPX` answers a recent window with a `403` and
    the vendor's sentence. This fixture used to answer it with an empty page, which is a
    fixture encoding a hope — an empty page is what a *quiet window* looks like, and a
    probe that read one as a plan would pass here and, live, tell an operator to buy a
    subscription they already hold.
    """

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("NDX")])
        if "I%3ASPX" in url or "I:SPX" in url:
            return refused()
        start, end = asked_window(url, params)
        first, stop = max(start, FLOOR), min(end, SETTLED)
        return nothing() if first > stop else served_rows([bar(day) for day in days(first, stop)])

    client, _ = client_of(answer)
    loader = MassiveBarsLoader(client=client, as_of=TODAY)
    found = loader.discover(spec(asset_class="indices", instruments=["SPX", "NDX"]))
    assert [ref.instrument for ref in found] == [f"NDX.{VENUE}"]


def test_a_silent_instrument_ends_its_own_dataset_and_not_the_walk() -> None:
    """A name the vendor carries nothing for is one dataset, not the end of the spec.

    Equities are gated per endpoint, so this is the case the index test above cannot
    reach: the answer is established for the silent name itself and the names beside it
    are discovered normally. A ticker that never traded, one listed yesterday, one long
    delisted — each is an ordinary member of a universe, and a walk that aborted on the
    first would throw away every name behind it, the ones the source serves included.
    """

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        if "HALTED" in url:
            return nothing()
        start, end = asked_window(url, params)
        first, stop = max(start, FLOOR), min(end, SETTLED)
        return nothing() if first > stop else served_rows([bar(day) for day in days(first, stop)])

    loader, _ = bars_loader(answer)

    found = loader.discover(spec(instruments=["HALTED", "AAPL", "MSFT"]))

    assert [ref.instrument for ref in found] == [f"AAPL.{VENUE}", f"MSFT.{VENUE}"]


def test_when_every_series_is_blocked_the_first_refusal_is_raised() -> None:
    loader, _ = bars_loader(
        blocked_source(control=lambda: served_rows([definition("AAPL")]), live=refused)
    )
    with pytest.raises(NotEntitledError):
        loader.discover(spec(asset_class="indices", instruments=["SPX", "VIX"]))


# --- load: the bar-close convention and the millisecond epoch ------------------


def load_one(loader: Loader, ref: DatasetRef) -> list[Any]:
    return list(loader.load(ref, ref.span))


def test_a_bar_is_stamped_at_the_close_of_its_own_window() -> None:
    """The row's `t` is the window's open; a bar known at its open is a look-ahead bug."""
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(resolution="1m", start=date(2024, 2, 1), end=date(2024, 2, 3)))[0]
    points = load_one(loader, ref)
    opened = datetime(2024, 2, 1, tzinfo=UTC)
    assert points[0].ts_event == int(opened.timestamp()) * NS + 60 * NS
    assert points[0].ts_init == points[0].ts_event


def test_a_daily_bar_closes_a_day_after_its_window_opened() -> None:
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(resolution="1d", start=date(2024, 2, 1), end=date(2024, 2, 3)))[0]
    points = load_one(loader, ref)
    assert [utc_day(point.ts_event) for point in points] == days(date(2024, 2, 1), date(2024, 2, 3))
    assert (
        points[0].ts_event == int(datetime(2024, 1, 31, tzinfo=UTC).timestamp()) * NS + 86_400 * NS
    )


def test_the_surplus_of_the_widened_window_is_dropped() -> None:
    """The vendor is asked wide so no bar is lost at a seam; the extra days are not written."""
    loader, replay = bars_loader(source())
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 3)))[0]
    points = load_one(loader, ref)
    assert {utc_day(point.ts_event) for point in points} == set(
        days(date(2024, 2, 1), date(2024, 2, 3))
    )
    assert any("2024-01-31" in item.url for item in replay.asked), "asked wider than it kept"


def test_prices_are_quantised_at_the_declared_precision() -> None:
    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        return served_rows([bar(date(2024, 2, 1), close=123.456)])

    loader, _ = bars_loader(answer)
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 1)))[0]
    assert str(load_one(loader, ref)[0].close) == "123.46"


def test_a_row_missing_a_field_the_point_needs_is_dropped() -> None:
    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        whole = bar(date(2024, 2, 1))
        broken = {key: value for key, value in bar(date(2024, 2, 2)).items() if key != "c"}
        return served_rows([whole, broken, {"o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}])

    loader, _ = bars_loader(answer)
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 3)))[0]
    assert len(load_one(loader, ref)) == 1


def test_a_row_the_engine_refuses_fails_as_a_kanso_validation() -> None:
    """`Bar` validates its own OHLC ordering, and a raw ValueError is not an answer."""

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        crooked = bar(date(2024, 2, 1))
        crooked["h"] = 1.0
        crooked["l"] = 500.0
        return served_rows([crooked])

    loader, _ = bars_loader(answer)
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 1)))[0]
    with pytest.raises(ValidationError) as caught:
        load_one(loader, ref)
    assert "bar" in caught.value.message


def test_a_cursor_is_walked_to_the_end() -> None:
    page_two = "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/x/y?cursor=2"

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        if "cursor=2" in url:
            return served_rows([bar(date(2024, 2, 2))])
        start, end = asked_window(url, params)
        if start < FLOOR:
            return served_rows([bar(day) for day in days(max(start, FLOOR), min(end, SETTLED))])
        return served_rows([bar(date(2024, 2, 1))], next_url=page_two)

    loader, _ = bars_loader(answer)
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 3)))[0]
    assert len(load_one(loader, ref)) == 2


def test_load_over_a_window_the_dataset_does_not_reach_serves_nothing() -> None:
    loader, _ = bars_loader(source())
    ref = loader.discover(spec())[0]
    assert list(loader.load(ref, (date(2019, 1, 1), date(2019, 2, 1)))) == []


def test_load_never_asks_below_the_floor_it_recorded() -> None:
    """The source truncates such a range silently, so asking is a request spent on nothing."""
    loader, replay = bars_loader(source())
    ref = loader.discover(spec(resolution="1d", start=date(2023, 1, 1)))[0]
    replay.asked.clear()
    load_one(loader, ref)
    assert asked_window(replay.asked[0].url, replay.asked[0].params)[0] == FLOOR


def test_a_ref_that_did_not_come_from_discover_is_refused() -> None:
    loader, _ = bars_loader(source())
    ref = DatasetRef(
        dataset_id="AAPL.XNAS-bar-1m-raw-20240301",
        instrument=f"AAPL.{VENUE}",
        type="bar",
        resolution="1m",
        span=(date(2024, 2, 1), date(2024, 3, 1)),
        adjusted=False,
        publication="realtime",
    )
    with pytest.raises(MalformedRequestError):
        load_one(loader, ref)


def test_an_empty_floor_parameter_reads_as_no_floor() -> None:
    loader, _ = bars_loader(source())
    ref = loader.discover(spec())[0]
    params = dict(ref.request_params or {})
    params["floor"] = ""
    params["probed_on"] = ""
    request = Request.of(
        DatasetRef(
            dataset_id=ref.dataset_id,
            instrument=ref.instrument,
            type=ref.type,
            resolution=ref.resolution,
            span=ref.span,
            adjusted=ref.adjusted,
            publication=ref.publication,
            request_params=params,
        )
    )
    assert request.floor is None
    assert request.probed_on is None
    assert request.instrument == f"AAPL.{VENUE}"


# --- load: a refusal mid-walk is probed, never assumed -------------------------


def test_a_refusal_during_a_load_is_established_by_probing() -> None:
    state = {"discovered": False}

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        if state["discovered"]:
            return refused()
        start, end = asked_window(url, params)
        first, stop = max(start, FLOOR), min(end, SETTLED)
        return nothing() if first > stop else served_rows([bar(day) for day in days(first, stop)])

    loader, _ = bars_loader(answer)
    ref = loader.discover(spec())[0]
    state["discovered"] = True
    with pytest.raises(NotEntitledError):
        load_one(loader, ref)


def test_a_refusal_the_probe_contradicts_is_no_answer_at_all() -> None:
    """A refusal a probe cannot reproduce establishes nothing, so it is not an outcome."""
    served = source()
    once: set[tuple[date, date]] = set()

    def answer(url: str, params: Mapping[str, str]) -> Response:
        window = None if "/v3/reference/" in url else asked_window(url, params)
        if window == (date(2024, 1, 31), date(2024, 3, 2)) and window not in once:
            once.add(window)
            return refused()
        return served(url, params)

    loader, _ = bars_loader(answer)
    ref = loader.discover(spec())[0]
    with pytest.raises(TransportError) as caught:
        load_one(loader, ref)
    assert "contradicted itself" in caught.value.message


# --- the manifest: coverage is what was served --------------------------------


def test_a_silently_truncated_range_is_recorded_as_the_span_it_served() -> None:
    """HTTP 200 with fewer days than were asked for is the vendor's normal behaviour."""
    loader, _ = bars_loader(source(cap=11))
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 3, 1)))[0]
    manifest = loader.manifest(ref)
    assert ref.span == (date(2024, 2, 1), date(2024, 3, 1))
    assert manifest.span == (date(2024, 2, 1), date(2024, 2, 10))
    difference = shortfall(ref.span, manifest.span)
    assert difference is not None
    assert "2024-02-11 to 2024-03-01 at the end" in difference


def test_a_manifest_measures_the_rows_rather_than_the_request() -> None:
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 5)))[0]
    manifest = loader.manifest(ref)
    assert manifest.row_count == 5
    assert manifest.source == "massive_bars"
    assert manifest.vendor_dataset == "bars"
    assert len(manifest.checksum) == 64


def test_a_dataset_that_served_nothing_has_no_manifest() -> None:
    """An empty dataset covers no days, so there is nothing to record about it."""
    loader, _ = bars_loader(source(hole=(date(2024, 2, 10), date(2024, 2, 25))))
    ref = loader.discover(spec(start=date(2024, 2, 15), end=date(2024, 2, 20)))[0]
    assert list(loader.load(ref, ref.span)) == []
    with pytest.raises(ValidationError):
        loader.manifest(ref)


def test_an_adjusted_dataset_records_the_date_it_was_adjusted_as_of() -> None:
    """A vendor-adjusted series is adjusted as of its request date and is therefore mutable."""
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(adjusted=True, start=date(2024, 2, 1), end=date(2024, 2, 5)))[0]
    manifest = loader.manifest(ref)
    assert manifest.adjusted is True
    assert manifest.adjustment_basis == TODAY.isoformat()


def test_a_declared_adjustment_basis_is_kept() -> None:
    loader, _ = bars_loader(source())
    ref = loader.discover(
        spec(adjusted=True, adjustment_basis="2024-02-29", start=date(2024, 2, 1))
    )[0]
    assert (ref.request_params or {})["adjustment_basis"] == "2024-02-29"


def test_load_arrow_produces_catalog_schema_tables() -> None:
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 5)))[0]
    batches = loader.load_arrow(ref, ref.span)
    assert batches is not None
    assert sum(table.num_rows for table in batches) == 5


# --- publication ---------------------------------------------------------------


def test_a_delayed_plan_stamps_availability_from_its_declared_rule() -> None:
    """A delayed dataset whose ts_init equals ts_event is a look-ahead bug wearing a manifest."""
    loader, _ = bars_loader(source())
    ref = loader.discover(
        spec(
            publication="delayed",
            publication_rule="delayed_trade",
            start=date(2024, 2, 1),
            end=date(2024, 2, 3),
        )
    )[0]
    points = load_one(loader, ref)
    assert points
    assert all(point.ts_init == point.ts_event + 15 * 60 * NS for point in points)
    check_delayed(points, "delayed_trade")


# --- trades --------------------------------------------------------------------


Rows = Callable[[date], Sequence[Mapping[str, Any]]]


def one_tick(day: date) -> Sequence[Mapping[str, Any]]:
    return [tick(day)]


def tick_source(make: Rows = one_tick, *, floor: date = FLOOR, last: date = SETTLED) -> Answer:
    """A tape that carries whatever `make` says for every day it holds."""

    def answer(url: str, params: Mapping[str, str]) -> Response:
        if "/v3/reference/" in url:
            return served_rows([definition("AAPL")])
        start, end = asked_window(url, params)
        first, stop = max(start, floor), min(end, last)
        if first > stop:
            return nothing()
        rows = [row for day in days(first, stop) for row in make(day)]
        return served_rows(rows) if rows else nothing()

    return answer


def tick_loader(kind: type, make: Rows = one_tick) -> tuple[Any, Replay]:
    client, replay = client_of(tick_source(make))
    return kind(client=client, as_of=TODAY), replay


def on(wanted: date, rows: Sequence[Mapping[str, Any]]) -> Rows:
    """`rows` on one day and the ordinary row on every other, so a probe still answers."""

    def make(day: date) -> Sequence[Mapping[str, Any]]:
        return rows if day == wanted else [tick(day)]

    return make


def tick_spec(**overrides: Any) -> dict[str, Any]:
    base = spec(resolution=None, start=date(2024, 2, 1), end=date(2024, 2, 3))
    base.update(overrides)
    return base


def test_a_trade_is_timed_at_the_venue_and_available_at_the_tape() -> None:
    loader, _ = tick_loader(MassiveTradesLoader)
    ref = loader.discover(tick_spec())[0]
    points = load_one(loader, ref)
    first = tick(date(2024, 2, 1))
    assert len(points) == 3
    assert points[0].ts_event == first["participant_timestamp"]
    assert points[0].ts_init == first["sip_timestamp"]
    assert points[0].ts_init > points[0].ts_event


def test_a_trade_with_no_venue_instant_falls_back_to_the_tape() -> None:
    row = tick(date(2024, 2, 1))
    del row["participant_timestamp"]
    loader, _ = tick_loader(MassiveTradesLoader, on(date(2024, 2, 1), [row]))
    ref = loader.discover(tick_spec())[0]
    point = load_one(loader, ref)[0]
    assert point.ts_event == point.ts_init == row["sip_timestamp"]


def test_contradictory_instants_take_the_conservative_reference_time() -> None:
    """Availability is never moved earlier than the source stated it."""
    row = tick(date(2024, 2, 1), lag_ns=-1_000)
    loader, _ = tick_loader(MassiveTradesLoader, on(date(2024, 2, 1), [row]))
    ref = loader.discover(tick_spec())[0]
    point = load_one(loader, ref)[0]
    assert point.ts_event == point.ts_init == row["sip_timestamp"]


def test_a_row_with_no_tape_instant_is_dropped() -> None:
    """Without an availability instant nothing can be said about when the print was public."""
    row = tick(date(2024, 2, 1), sip_timestamp="not a number")
    loader, _ = tick_loader(MassiveTradesLoader, on(date(2024, 2, 1), [row]))
    ref = loader.discover(tick_spec())[0]
    assert len(load_one(loader, ref)) == 2


@pytest.mark.parametrize("field", ["price", "size"])
def test_a_print_with_no_price_or_no_size_is_not_a_trade(field: str) -> None:
    rows = [tick(date(2024, 2, 1), **{field: 0})]
    loader, _ = tick_loader(MassiveTradesLoader, on(date(2024, 2, 1), rows))
    ref = loader.discover(tick_spec())[0]
    assert len(load_one(loader, ref)) == 2


def test_a_size_that_rounds_away_at_the_declared_precision_is_dropped() -> None:
    """The engine refuses a non-positive size, so a sub-increment print is not carried."""
    rows = [tick(date(2024, 2, 1), size=0.4)]
    loader, _ = tick_loader(MassiveTradesLoader, on(date(2024, 2, 1), rows))
    ref = loader.discover(tick_spec())[0]
    assert len(load_one(loader, ref)) == 2


def test_the_trade_id_descends_from_the_vendors_own_to_the_instant() -> None:
    def make(day: date) -> Sequence[Mapping[str, Any]]:
        if day == date(2024, 2, 1):
            return [tick(day, id="abc")]
        if day == date(2024, 2, 3):
            return [tick(day, sequence_number=None)]
        return [tick(day)]

    loader, _ = tick_loader(MassiveTradesLoader, make)
    ref = loader.discover(tick_spec())[0]
    points = load_one(loader, ref)
    assert str(points[0].trade_id) == "abc"
    assert str(points[1].trade_id) == "7"
    assert str(points[2].trade_id).startswith("AAPL-")


# --- quotes --------------------------------------------------------------------


def test_a_two_sided_quote_is_carried_with_both_sides() -> None:
    loader, _ = tick_loader(MassiveQuotesLoader)
    ref = loader.discover(tick_spec())[0]
    point = load_one(loader, ref)[0]
    first = tick(date(2024, 2, 1))
    assert str(point.bid_price) == "99.50"
    assert str(point.ask_price) == "100.50"
    assert point.ts_event == first["participant_timestamp"]
    assert point.ts_init == first["sip_timestamp"]


@pytest.mark.parametrize("side", ["bid_price", "ask_price"])
def test_a_zero_sided_quote_is_dropped(side: str) -> None:
    """The engine would build it happily, and every mid over the series would be halved."""
    rows = [tick(date(2024, 2, 1), **{side: 0})]
    loader, _ = tick_loader(MassiveQuotesLoader, on(date(2024, 2, 1), rows))
    ref = loader.discover(tick_spec())[0]
    points = load_one(loader, ref)
    assert len(points) == 2
    assert utc_day(points[0].ts_event) == date(2024, 2, 2)


def test_a_quote_missing_a_side_entirely_is_dropped() -> None:
    row = tick(date(2024, 2, 1))
    del row["bid_price"]
    loader, _ = tick_loader(MassiveQuotesLoader, on(date(2024, 2, 1), [row]))
    ref = loader.discover(tick_spec())[0]
    assert len(load_one(loader, ref)) == 2


def test_a_quote_with_no_tape_instant_is_dropped() -> None:
    """Without an availability instant nothing can be said about when the book was public."""
    row = tick(date(2024, 2, 1), sip_timestamp=None)
    loader, _ = tick_loader(MassiveQuotesLoader, on(date(2024, 2, 1), [row]))
    ref = loader.discover(tick_spec())[0]
    assert len(load_one(loader, ref)) == 2


def test_a_two_sided_quote_with_no_sizes_is_still_a_quote() -> None:
    row = tick(date(2024, 2, 1))
    del row["bid_size"]
    del row["ask_size"]
    loader, _ = tick_loader(MassiveQuotesLoader, on(date(2024, 2, 1), [row]))
    ref = loader.discover(tick_spec())[0]
    point = load_one(loader, ref)[0]
    assert str(point.bid_size) == "0"
    assert str(point.ask_size) == "0"


def test_a_window_of_only_one_sided_rows_serves_nothing() -> None:
    """A window whose every row is one-sided serves nothing, and that is the truth about it."""
    inside = set(days(date(2024, 1, 31), date(2024, 2, 4)))

    def make(day: date) -> Sequence[Mapping[str, Any]]:
        return [tick(day, bid_price=0)] if day in inside else [tick(day)]

    loader, _ = tick_loader(MassiveQuotesLoader, make)
    ref = loader.discover(tick_spec())[0]
    assert load_one(loader, ref) == []
    with pytest.raises(ValidationError):
        loader.manifest(ref)


# --- the interface and the adapter's own way of opening one -------------------


@pytest.mark.parametrize("kind", [MassiveBarsLoader, MassiveTradesLoader, MassiveQuotesLoader])
def test_every_request_loader_satisfies_the_loader_interface(kind: type) -> None:
    assert isinstance(kind(), Loader)


def test_the_three_loaders_have_distinct_ids_and_types() -> None:
    kinds = [MassiveBarsLoader, MassiveTradesLoader, MassiveQuotesLoader]
    assert len({kind.id for kind in kinds}) == 3
    assert [kind.kind.type for kind in kinds] == ["bar", "trade", "quote"]


def test_a_loader_opened_for_a_workspace_uses_that_workspaces_credential(tmp_path: Path) -> None:
    """Opened once and kept, because the quota lives in the connection rather than the call."""
    ws: Workspace = init(tmp_path / "ws")
    (ws.root / ".env").write_text("KANSO_MASSIVE_API_KEY=not-a-real-key\n", encoding="utf-8")
    loader = MassiveBarsLoader(workspace=ws, as_of=TODAY)
    opened = loader._open()
    assert opened.quota == "90/s"
    assert loader._open() is opened


def test_a_replayed_client_is_opened_once_and_reused() -> None:
    loader, _ = bars_loader(source())
    assert loader._open() is loader._open()


def test_the_probe_window_is_the_adapters_own_two_weeks() -> None:
    """A fixture that drifted from the foundation's window would prove nothing about it."""
    loader, replay = bars_loader(source())
    loader.discover(spec())
    first = asked_window(replay.asked[0].url, replay.asked[0].params)
    assert first == (SETTLED - PROBE_SPAN, SETTLED)


def test_the_transport_never_sees_the_key_in_a_query_string() -> None:
    loader, replay = bars_loader(source())
    loader.discover(spec())
    for item in replay.asked:
        assert "apiKey" not in item.params
        assert "apiKey" not in item.url


def test_a_response_is_only_ever_the_frozen_one() -> None:
    """A guard on the fixtures themselves: the replay answers, so nothing reaches a socket."""
    seen: list[str] = []

    def answer(url: str, params: Mapping[str, str]) -> Response:
        seen.append(url)
        return served_rows([definition("AAPL")])

    client, _ = client_of(answer)
    assert client.call("/v3/reference/tickers/AAPL").rows
    assert seen == ["https://api.massive.com/v3/reference/tickers/AAPL"]


def test_iterating_a_load_twice_gives_the_same_points() -> None:
    """Statelessness is what makes a snapshot reproducible, so it is asserted rather than hoped."""
    loader, _ = bars_loader(source())
    ref = loader.discover(spec(start=date(2024, 2, 1), end=date(2024, 2, 5)))[0]
    first = [point.ts_event for point in load_one(loader, ref)]
    second = [point.ts_event for point in load_one(loader, ref)]
    assert first == second
