"""The exchange's public history, driven against what the exchange was recorded answering.

Every answer a loader reads here was recorded by driving that loader against `us.okx.com`
and its file host on 2026-09-30, with no credential (`recorded.py`,
`fixtures/history/provenance.json`), and is served only for exactly the request it was
recorded for; the loaders run with `as_of` pinned to that day. Nothing reaches a network,
no variable is read and no test waits. Where a test needs an answer the exchange did not
give — a damaged zip, a row the engine refuses — it builds it in the test and says so.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.data import Bar, TradeTick
from nautilus_trader.model.enums import AggressorSide

from kanso.config import CONFIG_NAME, load_config
from kanso.data import registry
from kanso.data.instruments import resolve_universe
from kanso.data.loader import Loader, utc_day
from kanso.data.manifest import cache_path
from kanso.data.types import Funding
from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.nautilus.adapters.okx import history, reference, trades
from kanso.nautilus.adapters.okx.bars import BAR_SIZES, CANDLES, OkxBarsLoader, build_bar
from kanso.nautilus.adapters.okx.funding import FUNDING, OkxFundingLoader, build_funding
from kanso.nautilus.adapters.okx.history import HistoryLoader, Series, runs, units
from kanso.nautilus.adapters.okx.reference import ARCHIVES, Response
from kanso.nautilus.adapters.okx.trades import OkxTradesLoader
from kanso.workspace import Workspace, init

from .recorded import HISTORY, HISTORY_PROVENANCE, THROTTLED, History, Replay, answer

AS_OF = date(2026, 9, 30)
"""The day the recordings were made, which is the day every loader here runs on."""

US = "https://us.okx.com"
BTC = "BTC-USDT-SWAP.OKX"
USDC = "USDC-USDT-SWAP.OKX"
NS = 1_000_000


def at(millis: int) -> int:
    return millis * NS


def utc(text: str) -> int:
    """An ISO instant in UTC, as engine nanoseconds."""
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000) * NS


def body(name: str) -> Any:
    return json.loads((HISTORY / name).read_bytes())


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A workspace on the US host, BTC and USDC swaps resolved through the reference."""
    fresh = init(tmp_path / "ws")
    path = fresh.path(CONFIG_NAME)
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[adapters.okx]\nregion = "us"\n\n[data]\nreference = "okx"\n',
        encoding="utf-8",
    )
    ready = Workspace(root=fresh.root, config=load_config(path))
    monkeypatch.setattr(reference, "pyo3_transport", lambda rate, **_: Replay())
    resolve_universe(ready, [BTC, USDC], AS_OF)
    return ready


def opened(cls: type[HistoryLoader], ws: Workspace, transport: Any = None) -> Any:
    """A loader on the recordings, on the recording day, recording every pause it takes."""
    pauses: list[float] = []
    loader = cls(workspace=ws, transport=transport or History(), as_of=AS_OF, pause=pauses.append)
    loader.pauses = pauses  # type: ignore[attr-defined]
    return loader


def spec(**over: Any) -> dict[str, Any]:
    return {"instruments": ["USDC-USDT-SWAP"], "start": "2026-09-28", "end": "2026-09-28", **over}


# --- the adapter hands them out -------------------------------------------------


def test_the_three_loaders_are_handed_out_as_factories_that_send_nothing(ws: Workspace) -> None:
    factories = registry.adapter_loaders(ws)

    built = {name: factories[name]() for name in ("okx_bars", "okx_trades", "okx_funding")}
    assert {name: type(loader) for name, loader in built.items()} == {
        "okx_bars": OkxBarsLoader,
        "okx_trades": OkxTradesLoader,
        "okx_funding": OkxFundingLoader,
    }
    assert all(isinstance(loader, Loader) for loader in built.values())
    assert all(loader.workspace is ws for loader in built.values())


def test_a_loader_opens_the_client_on_the_table_s_host_once(ws: Workspace) -> None:
    loader = OkxBarsLoader(workspace=ws, transport=History())

    assert loader.client() is loader.client()
    assert loader.client().base_url == US
    before = datetime.now(tz=UTC).date()
    latest = loader.latest()
    after = datetime.now(tz=UTC).date()
    assert latest in {before - history.DAY, after - history.DAY}


# --- bars -----------------------------------------------------------------------


def test_a_day_of_minute_bars_is_every_closed_candle_stamped_at_its_close(ws: Workspace) -> None:
    replay = History()
    loader = opened(OkxBarsLoader, ws, replay)

    [ref] = loader.discover(spec(resolution="1m"))
    points = list(loader.load(ref, ref.span))

    assert (ref.instrument, ref.type, ref.resolution, ref.span) == (
        USDC,
        "bar",
        "1m",
        (date(2026, 9, 28), date(2026, 9, 28)),
    )
    assert (ref.publication, ref.vendor, ref.vendor_dataset) == ("realtime", "okx_bars", CANDLES)
    assert ref.request_params == {
        "inst_id": "USDC-USDT-SWAP",
        "price_precision": "6",
        "size_precision": "0",
        "host": US,
    }
    assert len(points) == 1440 and all(isinstance(point, Bar) for point in points)
    assert points[0].ts_event == utc("2026-09-28T00:00:00")
    assert points[-1].ts_event == utc("2026-09-28T23:59:00")
    assert all(point.ts_init == point.ts_event for point in points)
    assert [point.ts_event for point in points] == sorted(point.ts_event for point in points)
    assert str(points[0].bar_type) == "USDC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL"
    # one probe of the window's first day, then five pages walked back and an empty sixth
    assert [params.get("limit") for _, params in replay.asked] == ["1"] + ["300"] * 6
    assert all(url == f"{US}{CANDLES}" for url, _ in replay.asked)


def test_a_bar_is_the_recorded_candle_it_came_from(ws: Workspace) -> None:
    """The candle that opened at 23:58 is the bar that closed at 23:59, field for field."""
    loader = opened(OkxBarsLoader, ws)
    [ref] = loader.discover(spec(resolution="1m"))
    last = list(loader.load(ref, ref.span))[-1]
    page = body(
        "history-candles__after-1790639940000_bar-1m_before-1790553539999"
        "_instId-USDC-USDT-SWAP_limit-300.json"
    )
    row = page["data"][0]

    assert int(row[0]) + 60_000 == last.ts_event // NS
    assert [str(last.open), str(last.high), str(last.low), str(last.close)] == [
        f"{float(value):.6f}" for value in row[1:5]
    ]
    assert str(last.volume) == row[5]


def test_the_newest_candle_is_still_forming_and_is_no_bar() -> None:
    """Recorded without `after`: the endpoint's newest candle carries `confirm` "0"."""
    rows = body("history-candles__bar-1m_instId-BTC-USDT-SWAP_limit-3.json")["data"]
    series = Series("BTC-USDT-SWAP", 1, 2, US, "1m")

    assert rows[0][8] == "0" and build_bar(series, rows[0]) is None
    closed = build_bar(series, rows[1])
    assert closed.ts_event == closed.ts_init == at(int(rows[1][0]) + 60_000)
    assert float(str(closed.volume)) == float(rows[1][5])


def test_a_price_the_definition_s_precision_cannot_hold_is_refused_not_rounded() -> None:
    row = body("history-candles__bar-1m_instId-BTC-USDT-SWAP_limit-3.json")["data"][1]

    with pytest.raises(ValidationError, match="cannot hold") as refused:
        build_bar(Series("BTC-USDT-SWAP", 0, 2, US, "1m"), row)
    assert refused.value.remedy is not None and "override" in refused.value.remedy


def test_a_candle_of_another_shape_is_refused() -> None:
    row = body("history-candles__bar-1m_instId-BTC-USDT-SWAP_limit-3.json")["data"][1]

    with pytest.raises(ValidationError, match="a candle of 8 fields"):
        build_bar(Series("BTC-USDT-SWAP", 1, 2, US, "1m"), row[:8])


def test_a_candle_the_engine_refuses_is_refused_by_name() -> None:
    """Built in the test: a recorded candle with its high and low swapped."""
    row = list(body("history-candles__bar-1m_instId-BTC-USDT-SWAP_limit-3.json")["data"][1])
    row[2], row[3] = row[3], row[2]

    with pytest.raises(ValidationError, match="not a bar the engine accepts"):
        build_bar(Series("BTC-USDT-SWAP", 1, 2, US, "1m"), row)


def test_a_window_before_the_one_second_horizon_is_refused_naming_it(ws: Workspace) -> None:
    replay = History()
    loader = opened(OkxBarsLoader, ws, replay)

    with pytest.raises(ValidationError) as refused:
        loader.discover(
            {"instruments": [BTC], "start": "2026-03-01", "end": "2026-03-01", "resolution": "1s"}
        )

    assert "1s candles begin on 2026-03-14 (as of 2026-09-30)" in refused.value.message
    assert "not an empty market" in refused.value.message
    assert refused.value.remedy == "set `start: 2026-03-14` or later"
    assert len(replay.asked) == 10  # the window's first day, today, then a bisection


def test_a_swap_with_no_candles_at_all_is_refused(ws: Workspace) -> None:
    """Every request answered with the recorded empty page."""
    empty = answer("history-candles__after-1772323199001_bar-1s_instId-BTC-USDT-SWAP_limit-1.json")
    loader = opened(OkxBarsLoader, ws, lambda url, params: empty)

    with pytest.raises(ValidationError, match="serves no 1s candles of it at all"):
        loader.discover(spec(instruments=[BTC], resolution="1s"))


def test_a_bar_size_the_endpoint_does_not_serve_is_refused_before_a_request(
    ws: Workspace,
) -> None:
    replay = History()

    with pytest.raises(ValidationError, match="'2s' is not a bar size") as refused:
        opened(OkxBarsLoader, ws, replay).discover(spec(resolution="2s"))

    assert replay.asked == []
    assert "1s, 5s" in refused.value.message
    recorded = body("history-candles__bar-2s_instId-BTC-USDT-SWAP_limit-1.json")
    assert (recorded["code"], recorded["msg"]) == ("51000", "Parameter bar error")
    assert "2s" not in BAR_SIZES and BAR_SIZES["1d"] == "1Dutc"


def test_a_page_the_endpoint_repeats_ends_the_walk(ws: Workspace) -> None:
    """A stand-in that ignores `after` and serves one recorded page for every request."""
    name = (
        "history-candles__after-1790567940000_bar-1m_before-1790553539999"
        "_instId-USDC-USDT-SWAP_limit-300.json"
    )
    loader = opened(OkxBarsLoader, ws, lambda url, params: answer(name))
    series = Series("USDC-USDT-SWAP", 6, 0, US, "1m")

    points = list(loader.points(loader.client(), series, (date(2026, 9, 28),) * 2))
    later = list(loader.points(loader.client(), series, (date(2026, 9, 29),) * 2))

    assert len(points) == len(body(name)["data"])
    assert later == []  # every candle of that page closes on the 28th, so none is the 29th's


# --- the shape of a spec ------------------------------------------------------------


def test_a_week_bar_belongs_to_the_monday_it_closes_on(ws: Workspace) -> None:
    """A `1w` spec over a Tuesday alone is served no bar, although the source holds the week
    that closed the day before, and its manifest is refused as an empty series."""
    replay = History()
    loader = opened(OkxBarsLoader, ws, replay)
    [ref] = loader.discover(spec(resolution="1w", start="2026-09-29", end="2026-09-29"))

    assert list(loader.load(ref, ref.span)) == []
    with pytest.raises(ValidationError, match="served no points"):
        loader.manifest(ref)
    assert {params["bar"] for _, params in replay.asked} == {"1Wutc"}
    week = body("history-candles__after-1790035200001_bar-1Wutc_instId-USDC-USDT-SWAP_limit-1.json")
    assert datetime.fromtimestamp(int(week["data"][0][0]) / 1000, tz=UTC).weekday() == 0


def test_a_spec_is_refused_for_what_it_cannot_ask(ws: Workspace) -> None:
    replay = History()
    bars = opened(OkxBarsLoader, ws, replay)
    funding = opened(OkxFundingLoader, ws, replay)

    with pytest.raises(ValidationError, match="names no bar size"):
        bars.discover(spec())
    with pytest.raises(ValidationError, match="which have no bar size"):
        funding.discover(spec(resolution="1m"))
    with pytest.raises(ValidationError, match="end: 2026-09-30 is not a UTC day that has ended"):
        funding.discover(spec(end="2026-09-30"))
    with pytest.raises(ValidationError, match="names the venue XNAS"):
        funding.discover(spec(instruments=["USDC-USDT-SWAP.XNAS"]))
    with pytest.raises(ValidationError, match="end: 2026-09-27 is before start"):
        funding.discover(spec(end="2026-09-27"))
    assert replay.asked == []


def test_an_unresolved_swap_is_refused_before_a_request(ws: Workspace) -> None:
    replay = History()

    with pytest.raises(PreconditionError) as refused:
        opened(OkxFundingLoader, ws, replay).discover(spec(instruments=["ETH-USDT-SWAP"]))

    assert "ETH-USDT-SWAP.OKX: the catalog holds no definition" in refused.value.message
    assert "kanso data instruments resolve ETH-USDT-SWAP.OKX" in str(refused.value.remedy)
    assert replay.asked == []


def test_a_ref_not_discovered_here_is_refused(ws: Workspace) -> None:
    loader = opened(OkxFundingLoader, ws)
    [ref] = loader.discover(spec(instruments=["BTC-USDT-SWAP"]))

    with pytest.raises(ValidationError, match="carries no okx_funding request"):
        list(loader.load(replace(ref, request_params=None), ref.span))


def test_a_load_past_the_last_ended_day_serves_only_what_has_ended(ws: Workspace) -> None:
    loader = opened(OkxFundingLoader, ws)
    [ref] = loader.discover(spec(instruments=["BTC-USDT-SWAP"], end="2026-09-29"))

    assert list(loader.load(ref, (date(2026, 9, 30), date(2026, 10, 3)))) == []
    assert list(loader.load(ref, (date(2026, 10, 1), date(2026, 10, 3)))) == []


def test_a_number_is_read_exactly_or_not_at_all() -> None:
    assert units("0.999924", 6) == 999924
    assert units("16.0", 2) == 1600
    assert [units(text, 2) for text in ("0.001", "abc", "NaN", "Infinity", "")] == [None] * 5


def test_runs_are_how_a_refusal_names_many_days() -> None:
    days = [date(2023, 1, 1), date(2023, 1, 2), date(2023, 1, 3), date(2023, 1, 7)]

    assert runs(days) == "2023-01-01..2023-01-03, 2023-01-07"


# --- throttles ------------------------------------------------------------------


def test_a_throttle_is_waited_out(ws: Workspace) -> None:
    """The recorded 429, answered twice, then the recorded answer to the same request."""
    throttled = next(n for n in HISTORY_PROVENANCE["files"] if n.startswith(THROTTLED))
    replay = History()
    served = iter([answer(throttled), answer(throttled)])

    def sometimes(url: str, params: dict[str, str]) -> Response:
        return next(served, None) or replay(url, params)

    loader = opened(OkxTradesLoader, ws, sometimes)
    [ref] = loader.discover(spec(start="2023-03-19", end="2023-03-19"))

    assert ref.span == (date(2023, 3, 19), date(2023, 3, 19))
    assert loader.pauses == [trades.LISTING_GAP_S, 2.0, 4.0]
    assert answer(throttled).status == 429


def test_a_throttle_that_does_not_lift_stops_the_call(ws: Workspace) -> None:
    throttled = next(n for n in HISTORY_PROVENANCE["files"] if n.startswith(THROTTLED))
    loader = opened(OkxTradesLoader, ws, lambda url, params: answer(throttled))

    with pytest.raises(KansoError) as stopped:
        loader.discover(spec())

    assert stopped.value.code is Exit.ERROR
    assert "HTTP 429, code 50011" in stopped.value.message
    assert loader.pauses == [trades.LISTING_GAP_S, 2.0, 4.0, 6.0, 8.0]


def test_any_other_refusal_stops_the_call_at_once(ws: Workspace) -> None:
    """Recorded: the listing asked for eleven days answers HTTP 400, code 50076."""
    eleven = (
        "market-data-history__begin-1789747200000_dateAggrType-daily_end-1790611200000"
        "_instFamilyList-USDC-USDT_instType-SWAP_module-1.json"
    )
    loader = opened(OkxTradesLoader, ws, lambda url, params: answer(eleven))

    with pytest.raises(KansoError, match="HTTP 400, code 50076"):
        loader.discover(spec())
    assert loader.pauses == [trades.LISTING_GAP_S]


# --- trades ---------------------------------------------------------------------


def test_a_utc_day_of_prints_is_read_from_its_two_archives(ws: Workspace) -> None:
    replay = History()
    loader = opened(OkxTradesLoader, ws, replay)

    [ref] = loader.discover(spec(start="2026-09-26", end="2026-09-26"))
    points = list(loader.load(ref, ref.span))

    assert (ref.instrument, ref.type, ref.resolution) == (USDC, "trade", None)
    assert ref.vendor_dataset == f"{ARCHIVES}?module=1"
    assert len(points) == 214 and all(isinstance(point, TradeTick) for point in points)
    assert {utc_day(point.ts_event) for point in points} == {date(2026, 9, 26)}
    assert all(point.ts_init == point.ts_event for point in points)
    assert [point.ts_event for point in points] == sorted(point.ts_event for point in points)
    assert (str(points[0].trade_id), str(points[-1].trade_id)) == ("3031604", "3031817")
    fetched = [url.rsplit("/", 1)[1] for url, _ in replay.asked if "static.okx.com" in url]
    assert fetched == [
        "USDC-USDT-SWAP-trades-2026-09-26.zip?v=999",
        "USDC-USDT-SWAP-trades-2026-09-27.zip?v=999",
    ]
    assert sorted(path.name for path in (cache_path(ws) / "okx" / "trades").iterdir()) == [
        "USDC-USDT-SWAP-trades-2026-09-26.zip",
        "USDC-USDT-SWAP-trades-2026-09-27.zip",
    ]
    listings = [url for url, _ in replay.asked if url.endswith(ARCHIVES)]
    assert len(listings) == 2  # discover's and load's, each after its own pause
    assert loader.pauses == [trades.LISTING_GAP_S] * len(listings)


@pytest.mark.parametrize(
    ("field", "value", "refusal"),
    [
        ("filename", "../USDC-USDT-SWAP-trades-2026-09-26.zip", "not a bare zip name"),
        ("filename", "cache/USDC-USDT-SWAP-trades-2026-09-26.zip", "not a bare zip name"),
        ("filename", ".zip", "not a bare zip name"),
        ("filename", "USDC-USDT-SWAP-trades-2026-09-26.csv", "not a bare zip name"),
        ("url", "http://static.okx.com/x.zip", "from a URL that is not https"),
    ],
)
def test_a_listing_entry_that_would_leave_the_cache_or_https_is_refused(
    field: str, value: str, refusal: str
) -> None:
    """The recorded listing with one entry's field changed in the test: the exchange was
    never measured naming such a file or URL, and a loader must not follow one if it does."""
    listing = body(
        "market-data-history__begin-1790265600000_dateAggrType-daily_end-1790438400000"
        "_instFamilyList-USDC-USDT_instType-SWAP_module-1.json"
    )["data"]
    listing[0]["details"][0]["groupDetails"][0][field] = value

    with pytest.raises(ValidationError, match=refusal):
        list(trades._archives(tuple(listing)))


def test_a_print_is_the_rest_endpoint_s_print_in_contracts_and_taker_side(
    ws: Workspace,
) -> None:
    """Recorded both ways: trades 3031604-3031606 from the archive and from REST, whose
    `sz` is in contracts and whose `side` is the taker's."""
    loader = opened(OkxTradesLoader, ws)
    [ref] = loader.discover(spec(start="2026-09-26", end="2026-09-26"))
    by_id = {str(point.trade_id): point for point in loader.load(ref, ref.span)}
    rest = body("history-trades__after-3031607_instId-USDC-USDT-SWAP_limit-3_type-1.json")

    for row in rest["data"]:
        point = by_id[row["tradeId"]]
        assert str(point.size) == row["sz"]
        assert float(str(point.price)) == float(row["px"])
        assert point.ts_event == at(int(row["ts"]))
        assert (
            point.aggressor_side
            == {
                "buy": AggressorSide.BUYER,
                "sell": AggressorSide.SELLER,
            }[row["side"]]
        )


def test_an_archive_of_six_columns_is_read_by_name(ws: Workspace) -> None:
    """2023's archives carry no `source` column."""
    loader = opened(OkxTradesLoader, ws)
    [ref] = loader.discover(spec(start="2023-03-19", end="2023-03-19"))
    points = list(loader.load(ref, ref.span))
    with zipfile.ZipFile(HISTORY / "USDC-USDT-SWAP-trades-2023-03-19.zip") as archive:
        header = archive.read(archive.namelist()[0]).split(b"\r\n", 1)[0]

    assert header == b"instrument_name,trade_id,side,price,size,created_time"
    assert len(points) == 1761
    assert {utc_day(point.ts_event) for point in points} == {date(2023, 3, 19)}


def test_a_range_the_archives_do_not_serve_in_full_is_refused_by_date(ws: Workspace) -> None:
    """Recorded at 16:21 UTC on 2026-09-30: the newest archive listed was 2026-09-29's."""
    loader = opened(OkxTradesLoader, ws)

    with pytest.raises(ValidationError) as refused:
        loader.discover(spec(start="2026-09-28", end="2026-09-29"))

    assert refused.value.message.startswith(
        "okx trades USDC-USDT-SWAP: UTC days 2026-09-29 are not served"
    )
    assert "archives of 2026-09-30" in refused.value.message
    assert "archives serve the UTC days 2026-09-28" in refused.value.message
    assert "at most 2026-09-28" in str(refused.value.remedy)


def test_a_range_no_archive_serves_says_so(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trades, "listed", lambda *args: {})

    with pytest.raises(ValidationError, match="no UTC day of that range is served"):
        opened(OkxTradesLoader, ws).discover(spec())


def test_a_sync_past_the_newest_archive_stops_there(ws: Workspace) -> None:
    """A ref asked over 2026-09-28..29, as `data sync` asks one: the 29th needs the archive
    of the 30th, which was not listed yet, so the prints stop at the end of the 28th."""
    loader = opened(OkxTradesLoader, ws)
    [ref] = loader.discover(spec(start="2026-09-26", end="2026-09-26"))
    later = replace(ref, span=(date(2026, 9, 28), date(2026, 9, 29)))

    points = list(loader.load(later, later.span))

    assert len(points) == 1263
    assert {utc_day(point.ts_event) for point in points} == {date(2026, 9, 28)}


def test_a_day_missing_between_listed_archives_stops_the_load(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing with one archive taken out in the test, as a gap would read."""
    loader = opened(OkxTradesLoader, ws)
    [ref] = loader.discover(spec(start="2026-09-26", end="2026-09-26"))
    real = trades.listed

    def gapped(*args: Any) -> dict[date, trades.Archive]:
        found = real(*args)
        found.pop(date(2026, 9, 26), None)
        found[date(2026, 9, 28)] = found[date(2026, 9, 27)]
        return found

    monkeypatch.setattr(trades, "listed", gapped)
    with pytest.raises(ValidationError, match="between archives it does list") as refused:
        list(loader.load(ref, ref.span))
    assert "UTC days 2026-09-26" in refused.value.message


def test_an_archive_is_read_from_the_cache_once_it_is_there(ws: Workspace) -> None:
    replay = History()
    loader = opened(OkxTradesLoader, ws, replay)
    [ref] = loader.discover(spec(start="2026-09-26", end="2026-09-26"))
    list(loader.load(ref, ref.span))
    again = History()

    points = list(opened(OkxTradesLoader, ws, again).load(ref, ref.span))

    assert len(points) == 214
    assert not [url for url, _ in again.asked if "static.okx.com" in url]


def archive_of(tmp_path: Path, members: dict[str, bytes]) -> Path:
    path = tmp_path / "USDC-USDT-SWAP-trades-2026-09-26.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def recorded_csv() -> bytes:
    with zipfile.ZipFile(HISTORY / "USDC-USDT-SWAP-trades-2026-09-26.zip") as archive:
        return archive.read(archive.namelist()[0])


SERIES = Series("USDC-USDT-SWAP", 6, 0, US)


@pytest.mark.parametrize(
    ("members", "refusal"),
    [
        ({"a.csv": b"x", "b.csv": b"y"}, "2 files where every archive measured holds one CSV"),
        ({"a.csv": b"instrument_name,trade_id\r\n"}, "lacks side, price, size, created_time"),
        ({"a.csv": b""}, "its header is (empty)"),
    ],
)
def test_an_archive_of_another_layout_is_refused(
    ws: Workspace, tmp_path: Path, members: dict[str, bytes], refusal: str
) -> None:
    """Built in the test: layouts the exchange was not seen to serve."""
    with pytest.raises(ValidationError) as refused:
        list(opened(OkxTradesLoader, ws)._read(archive_of(tmp_path, members), SERIES))
    assert refusal in refused.value.message


@pytest.mark.parametrize(
    ("edit", "refusal"),
    [
        (lambda line: line.rsplit(b",", 1)[0], "line 2 has 6 fields and the header 7"),
        (lambda line: line.replace(b"USDC-USDT-SWAP", b"USDT-USDC-SWAP", 1), "not a print of"),
        (lambda line: line.replace(b",sell,", b",both,", 1), "with a taker side"),
        (lambda line: line.replace(b",1.0,", b",0.0,", 1), "not a print the engine accepts"),
    ],
)
def test_a_row_that_is_not_a_print_is_refused_by_trade(
    ws: Workspace, tmp_path: Path, edit: Any, refusal: str
) -> None:
    """Built in the test: the recorded archive with its first row altered as named."""
    header, first, rest = recorded_csv().split(b"\r\n", 2)
    data = b"\r\n".join([header, edit(first), rest])

    with pytest.raises(ValidationError) as refused:
        list(opened(OkxTradesLoader, ws)._read(archive_of(tmp_path, {"a.csv": data}), SERIES))
    assert refusal in refused.value.message


def test_prints_that_go_back_in_time_are_refused_not_sorted(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = opened(OkxTradesLoader, ws)
    [ref] = loader.discover(spec(start="2026-09-26", end="2026-09-26"))
    real = OkxTradesLoader._read
    monkeypatch.setattr(
        OkxTradesLoader,
        "_read",
        lambda self, path, series: reversed(list(real(self, path, series))),
    )

    with pytest.raises(ValidationError, match="goes back in time"):
        list(loader.load(ref, ref.span))


def test_a_cached_archive_that_is_not_a_zip_names_itself(ws: Workspace, tmp_path: Path) -> None:
    junk = tmp_path / "USDC-USDT-SWAP-trades-2026-09-26.zip"
    junk.write_bytes(b"not a zip")

    with pytest.raises(ValidationError) as refused:
        list(opened(OkxTradesLoader, ws)._read(junk, SERIES))
    assert f"delete {junk}" in str(refused.value.remedy)


def test_a_download_that_is_not_a_whole_zip_or_not_served_is_never_cached(
    ws: Workspace, tmp_path: Path
) -> None:
    """Stand-ins built from recorded bytes: the listing's own body served as the archive,
    and the recorded 429 served for it."""
    listing = (
        "market-data-history__begin-1790265600000_dateAggrType-daily_end-1790438400000"
        "_instFamilyList-USDC-USDT_instType-SWAP_module-1.json"
    )
    throttled = next(n for n in HISTORY_PROVENANCE["files"] if n.startswith(THROTTLED))
    archive = trades.Archive(date(2026, 9, 26), "x.zip", "https://static.okx.com/x.zip")
    cache = tmp_path / "cache"

    for served, refusal in ((answer(listing), "not a whole zip"), (answer(throttled), "HTTP 429")):
        loader = opened(OkxTradesLoader, ws, lambda url, params, served=served: served)
        loader.cache = cache
        with pytest.raises(KansoError, match=refusal):
            loader._fetched(loader.client(), archive)
        assert not cache.exists() or list(cache.iterdir()) == []


# --- funding ----------------------------------------------------------------------


def test_funding_is_the_realised_rate_at_each_settlement(ws: Workspace) -> None:
    replay = History()
    loader = opened(OkxFundingLoader, ws, replay)

    [ref] = loader.discover(spec(instruments=["BTC-USDT-SWAP"], end="2026-09-29"))
    points = list(loader.load(ref, ref.span))
    rows = body(
        "funding-rate-history__after-1790726400000_before-1790553599999"
        "_instId-BTC-USDT-SWAP_limit-400.json"
    )["data"]

    assert (ref.type, ref.vendor_dataset, ref.resolution) == ("funding", FUNDING, None)
    assert all(isinstance(point, Funding) for point in points)
    assert [point.ts_event for point in points] == [
        utc(f"2026-09-{day}T{hour}:00:00") for day in (28, 29) for hour in ("00", "08", "16")
    ]
    assert all(point.ts_init == point.ts_event for point in points)
    assert [point.rate for point in points] == [
        float(row["realizedRate"]) for row in sorted(rows, key=lambda row: row["fundingTime"])
    ]
    assert {str(point.instrument_id) for point in points} == {BTC}
    # the horizon walked back until empty, then the window walked the same way
    assert [params.get("after") for _, params in replay.asked] == [
        None,
        "1782720000000",
        "1790726400000",
        "1790553600000",
    ]


def test_a_window_before_the_funding_horizon_is_refused_naming_it(ws: Workspace) -> None:
    with pytest.raises(ValidationError) as refused:
        opened(OkxFundingLoader, ws).discover(
            spec(instruments=["BTC-USDT-SWAP"], start="2026-06-01", end="2026-06-02")
        )

    assert "settlements begin at 2026-06-29 08:00 UTC" in refused.value.message
    assert "whole UTC days from 2026-06-30" in refused.value.message
    assert refused.value.remedy == "set `start: 2026-06-30` or later"


def test_a_horizon_at_midnight_serves_its_own_day(ws: Workspace) -> None:
    """A stand-in serving one recorded midnight settlement as the newest page, and the
    recorded empty page for every page before it."""
    page = body("funding-rate-history__instId-BTC-USDT-SWAP_limit-400.json")
    midnight = next(row for row in page["data"] if int(row["fundingTime"]) % 86_400_000 == 0)
    empty = answer("funding-rate-history__after-1782720000000_instId-BTC-USDT-SWAP_limit-400.json")
    one = Response(200, json.dumps({"code": "0", "data": [midnight], "msg": ""}).encode())
    loader = opened(OkxFundingLoader, ws, lambda url, params: empty if "after" in params else one)
    day = utc_day(at(int(midnight["fundingTime"])))
    series = Series("BTC-USDT-SWAP", 1, 2, US)

    loader.measure(loader.client(), series, (day, day))
    with pytest.raises(ValidationError, match=f"whole UTC days from {day}"):
        loader.measure(loader.client(), series, (day - history.DAY, day))


def test_a_swap_that_never_settled_is_refused(ws: Workspace) -> None:
    empty = answer("funding-rate-history__after-1782720000000_instId-BTC-USDT-SWAP_limit-400.json")

    with pytest.raises(ValidationError, match="lists no settlement of it at all"):
        opened(OkxFundingLoader, ws, lambda url, params: empty).discover(spec())


def test_a_settlement_with_no_realised_rate_is_refused_not_read_from_the_published_one() -> None:
    """A recorded row with its realised rate taken out in the test."""
    row = dict(body("funding-rate-history__instId-BTC-USDT-SWAP_limit-400.json")["data"][0])
    series = Series("BTC-USDT-SWAP", 1, 2, US)

    for broken in ({}, {"realizedRate": ""}, {"realizedRate": "NaN"}):
        stripped = {key: value for key, value in row.items() if key != "realizedRate"}
        with pytest.raises(ValidationError, match="states no realised rate"):
            build_funding(series, {**stripped, **broken})
    assert build_funding(series, row).rate == float(row["realizedRate"])


def test_a_funding_page_the_endpoint_repeats_ends_the_walk(ws: Workspace) -> None:
    name = "funding-rate-history__instId-BTC-USDT-SWAP_limit-400.json"
    loader = opened(OkxFundingLoader, ws, lambda url, params: answer(name))
    series = Series("BTC-USDT-SWAP", 1, 2, US)

    with pytest.raises(ValidationError, match="settlements begin at 2026-06-29 08:00 UTC"):
        loader.measure(loader.client(), series, (date(2026, 6, 1),) * 2)
    points = list(loader.points(loader.client(), series, (date(2026, 9, 28), date(2026, 9, 29))))
    assert len(body(name)["data"]) == 281  # the page holds the whole three months ...
    assert len(points) == 6  # ... and only the six settlements of the two days are the span's
    assert {utc_day(point.ts_event) for point in points} == {date(2026, 9, 28), date(2026, 9, 29)}


# --- the catalog's side of the interface ------------------------------------------


def test_arrow_and_the_manifest_are_the_same_points(ws: Workspace) -> None:
    loader = opened(OkxFundingLoader, ws)
    [ref] = loader.discover(spec(instruments=["BTC-USDT-SWAP"], end="2026-09-29"))

    tables = list(loader.load_arrow(ref, ref.span) or ())
    manifest = loader.manifest(ref)

    assert sum(table.num_rows for table in tables) == 6  # type: ignore[attr-defined]
    assert (manifest.row_count, manifest.span, manifest.source) == (6, ref.span, "okx_funding")
    assert manifest.request_params == ref.request_params
    assert manifest.publication == "realtime" and manifest.publication_rule is None


def test_the_recordings_say_where_they_came_from() -> None:
    names = {path.name for path in HISTORY.iterdir()} - {"provenance.json"}

    assert names == set(HISTORY_PROVENANCE["files"])
    assert HISTORY_PROVENANCE["authentication"].startswith("none")
    for entry in HISTORY_PROVENANCE["files"].values():
        assert entry["recorded_at"].startswith("2026-09-30T1")
        assert entry["url"].startswith(f"https://{entry['host']}/")
        assert entry["host"] in {"us.okx.com", "static.okx.com"}
