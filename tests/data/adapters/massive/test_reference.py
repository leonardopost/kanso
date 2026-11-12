"""Reference resolution: keys become definitions, and a universe is walked to its end.

The fixture here is a *directory*, not a script: a table of reference rows, a listing that
answers in pages behind a cursor, an option chain that does the same, and a set of keys
whose aggregates the plan serves. Every measured behaviour of the real source is one
setting of those — a key the vendor does not carry, a lookup it refuses, an index whose
feed is entitled while the next one's is not — so a test says which source it is imitating
rather than which bytes it expects.

Three properties are asserted throughout and are the reason this module exists.

* Reference has no history floor. An option that expired years before the aggregate floor
  still resolves, and the requests made to resolve it are reference requests only.
* A universe is the whole cursor walk, and entitlement over it is established one key at a
  time. The two index keys in these fixtures are arranged so that a filter on the feed name
  would give exactly the wrong answer, which is what makes the probe load bearing.
* A definition is never completed by a guess. Where neither kanso's conventions nor the
  vendor's row states a number the engine requires, the key fails by name and the rest of
  the universe still resolves.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kanso.data import conventions as core
from kanso.data.adapters import massive
from kanso.data.adapters.massive import conventions
from kanso.data.adapters.massive.client import MassiveClient, Response
from kanso.data.adapters.massive.entitlement import FUTURES_CONTRACTS, FUTURES_PRODUCTS
from kanso.data.adapters.massive.errors import (
    MalformedRequestError,
    NotEntitledError,
    TransportError,
)
from kanso.data.adapters.massive.reference import (
    CONTRACTS,
    LISTING,
    MassiveReference,
    contracts,
    entitled,
    front_month,
    provider,
    universe,
)
from kanso.data.instruments import (
    DELISTED,
    NOT_YET_LISTED,
    UNKNOWN,
    ResolveError,
    build,
    definition_checksum,
)
from kanso.errors import Exit, ValidationError
from kanso.schemas import InstrumentEntry
from kanso.workspace import Workspace, init

from . import Replay, bar, nothing, refused, rejected, served, window_of

API = "https://api.massive.com"

TODAY = date(2026, 9, 4)
"""The day every resolution here is made as of. Frozen: a definition is a dated fact, and
a test that drifted with the calendar would prove a different thing every day."""

AGGREGATE_FLOOR = date(2024, 9, 3)
"""Where the plan's option aggregates begin. Nothing in this module may consult it: the
whole point is that a definition older than it still resolves."""


# --- the directory the fixtures answer from ------------------------------------


def stock(
    ticker: str = "AAPL",
    *,
    exchange: object = "XNAS",
    listed: str | None = "1980-12-12",
    delisted: str | None = None,
    market: str = "stocks",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": ticker,
        "name": f"{ticker} test",
        "market": market,
        "currency_name": "usd",
        "primary_exchange": exchange,
    }
    if listed is not None:
        row["list_date"] = listed
    if delisted is not None:
        row["delisted_utc"] = delisted
    return row


def index(ticker: str = "I:NDX", *, feed: str = "NasdaqGIDS") -> dict[str, Any]:
    return {"ticker": ticker, "name": f"{ticker} index", "market": "indices", "source_feed": feed}


def pair(ticker: str = "C:EURUSD") -> dict[str, Any]:
    return {"ticker": ticker, "name": f"{ticker} pair", "market": "currencies"}


def option(
    ticker: str = "O:AAPL261218C00150000",
    *,
    underlying: str | None = "AAPL",
    expiry: str | None = "2026-12-18",
    strike: object = 150,
    kind: str | None = "call",
    listed: str | None = "2024-01-05",
    size: object = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"ticker": ticker, "market": "options"}
    for name, value in (
        ("underlying_ticker", underlying),
        ("expiration_date", expiry),
        ("contract_type", kind),
        ("list_date", listed),
    ):
        if value is not None:
            row[name] = value
    if strike is not None:
        row["strike_price"] = strike
    if size is not None:
        row["shares_per_contract"] = size
    return row


FUTURES = Path(__file__).parent / "fixtures" / "futures"
"""Futures listings and aggregates recorded from the live source on 2026-10-29; each file's
request and instant are in `provenance.json` there."""


def recorded(name: str) -> Response:
    """One recorded futures answer, its body as served and its status as recorded."""
    files = json.loads((FUTURES / "provenance.json").read_text("utf-8"))["files"]
    return Response(status=int(files[name]["status"]), body=(FUTURES / name).read_bytes())


def future(
    ticker: str = "ESZ6",
    *,
    exchange: str | None = "XCME",
    expiry: str | None = "2026-12-18",
    listed: str | None = "2021-12-21",
    size: object = 50,
    tick: object = 0.25,
    unit: str | None = "IPNT",
    product: str | None = "ES",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A futures contract's row and its product's, in the fields the vendor's futures
    listings were measured stating (`fixtures/futures/`)."""
    contract: dict[str, Any] = {"active": True, "date": TODAY.isoformat(), "ticker": ticker}
    for name, value in (
        ("trading_venue", exchange),
        ("last_trade_date", expiry),
        ("first_trade_date", listed),
        ("trade_tick_size", tick),
        ("product_code", product),
    ):
        if value is not None:
            contract[name] = value
    stated: dict[str, Any] = {
        "product_code": product,
        "type": "single",
        "trade_currency_code": "USD",
    }
    for name, value in (("unit_of_measure", unit), ("unit_of_measure_qty", size)):
        if value is not None:
            stated[name] = value
    return contract, stated


@dataclass
class Directory:
    """A reference source: rows by key, paged listings, and a plan over the aggregates.

    **Corrected against the live source: an option key is not in the generic reference.**
    This fixture used to answer every key on `/v3/reference/tickers/{key}` and to route
    every request touching the contracts path to the chain listing, so a lookup sent to
    the wrong one of the two was indistinguishable from a lookup sent to the right one —
    which is precisely what let an option chain resolve here and fail live. The source
    rejects an option key on the generic reference with a client error and carries the
    contract on `/v3/reference/options/contracts/{key}`, and so does this.
    """

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    futures: dict[str, tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=dict)
    pages: tuple[tuple[dict[str, Any], ...], ...] = ()
    chain: tuple[tuple[dict[str, Any], ...], ...] = ()
    entitled: frozenset[str] = frozenset()
    lookup: str = "rows"
    asked: list[str] = field(default_factory=list)

    def __call__(self, url: str, params: Mapping[str, str]) -> Response:
        self.asked.append(url)
        path = url.split("?", 1)[0]
        if path.endswith(FUTURES_CONTRACTS):
            listed = self.futures.get(params.get("ticker", ""))
            return nothing() if listed is None else served([listed[0]])
        if path.endswith(FUTURES_PRODUCTS):
            named = [stated for _, stated in self.futures.values()]
            return served([row for row in named if row["product_code"] == params["product_code"]])
        if f"{CONTRACTS}/" in path:
            return self._detail(path.split(f"{CONTRACTS}/", 1)[1])
        if CONTRACTS in url:
            return self._page(self.chain, url, CONTRACTS)
        if f"{LISTING}/" in path:
            return self._generic(path.split(f"{LISTING}/", 1)[1])
        if LISTING in url:
            return self._page(self.pages, url, LISTING)
        return self._bars(url)

    @property
    def lookups(self) -> list[str]:
        """The keys either detail endpoint was asked about, in order."""
        return [
            url.split(f"{prefix}/", 1)[1]
            for url in self.asked
            for prefix in (LISTING, CONTRACTS)
            if f"{prefix}/" in url.split("?", 1)[0]
        ]

    def _generic(self, ticker: str) -> Response:
        """The generic ticker reference, which does not carry an option contract.

        Measured: an option key sent here comes back a client error, not an empty answer.
        A provider that read that as "the vendor does not know this key" would fail a whole
        chain by the one reason that is definitely wrong. A futures key comes back a 404, the
        body recorded on 2026-10-29.
        """
        if ticker.startswith("O:"):
            return rejected()
        if ticker in self.futures:
            return recorded("reference_tickers_ESZ5.json")
        return self._detail(ticker)

    def _detail(self, ticker: str) -> Response:
        if self.lookup == "refused":
            return refused()
        if self.lookup == "rejected":
            return rejected()
        if self.lookup == "broken":
            return Response(status=503)
        row = self.rows.get(ticker)
        return nothing() if row is None else served([row])

    def _page(self, pages: tuple[tuple[dict[str, Any], ...], ...], url: str, path: str) -> Response:
        index_of = int(url.partition("cursor=")[2] or 0) if "cursor=" in url else 0
        following = f"{API}{path}?cursor={index_of + 1}" if index_of + 1 < len(pages) else None
        return served(list(pages[index_of]), next_url=following)

    def _bars(self, url: str) -> Response:
        ticker = url.split("/v2/aggs/ticker/", 1)[1].split("/range/", 1)[0]
        if ticker not in self.entitled:
            return nothing()
        return served([bar(window_of(url)[1])])


def reading(directory: Directory) -> MassiveReference:
    """A provider over this directory, with no credential and no socket."""
    return MassiveReference(MassiveClient("test-key-not-a-secret", transport=Replay(directory)))


def one(directory: Directory, wanted: str, as_of: date = TODAY) -> object:
    """The single answer for one key."""
    return reading(directory).resolve([wanted], as_of)[wanted]


def epoch_ns(day: date) -> int:
    """A day as the engine holds an instant: whole nanoseconds since the UTC epoch."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


def fields_of(found: object) -> dict[str, Any]:
    dumped: dict[str, Any] = type(found).to_dict(found)
    return dumped


def rebuilt(found: object, asset_class: str) -> object:
    """What the core does with a provider's answer: rebuild it, the override applied over it.

    `asset_class` is what the workspace entry declares, which is what the core builds from;
    the two derivative classes also carry an `instrument_class`, since an asset class does
    not imply a derivative.
    """
    stored = fields_of(found)
    named = {"OptionContract": "option", "FuturesContract": "future"}.get(type(found).__name__)
    entry = InstrumentEntry(
        nautilus_id=str(stored["id"]),
        asset_class=asset_class,
        corporate_actions="adjust_all",
        override={} if named is None else {"instrument_class": named},
    )
    fields = {k: v for k, v in stored.items() if v is not None and k not in ("type", "id")}
    return build(entry, fields)


# --- the vendor's spelling ------------------------------------------------------


def test_the_adapter_and_its_conventions_agree_on_one_identity() -> None:
    """Two spellings of one vendor id is a bug that surfaces as a missing `sources` entry."""
    assert conventions.VENDOR == massive.ID
    assert MassiveReference.id == massive.ID


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("O:AAPL251219C00150000", conventions.OPTIONS),
        ("C:EURUSD", conventions.FOREX),
        ("I:NDX", conventions.INDICES),
        ("X:BTCUSD", conventions.CRYPTO),
        ("AAPL", None),
        ("ESZ5", None),
    ],
)
def test_a_prefix_names_a_market_and_a_bare_key_names_none(ticker: str, expected: str) -> None:
    """A bare key is a stock or a futures contract, and the string cannot tell which."""
    assert conventions.classify(ticker) == expected


def test_an_option_key_carries_its_whole_contract() -> None:
    found = conventions.parse("O:AAPL251219C00150000")

    assert found.asset_class == conventions.OPTIONS
    assert found.key == "AAPL251219C00150000"
    assert (found.underlying, found.expiry) == ("AAPL", date(2025, 12, 19))
    assert (found.option_kind, found.strike) == ("CALL", Decimal("150.000"))


def test_an_option_key_is_read_from_the_right_so_any_root_parses() -> None:
    """Roots run from one character to six and the vendor pads none of them."""
    assert conventions.parse("O:F260116P00012500").underlying == "F"
    assert conventions.parse("O:BRKB260116P00012500").option_kind == "PUT"


@pytest.mark.parametrize(
    "ticker",
    [
        "O:AAPL",
        "O:AAPL251219X00150000",
        "O:AAPL2512X9C00150000",
        "O:AAPL251219C0015000X",
        "O:251219C00150000",
        "O:AAPL259919C00150000",
    ],
)
def test_a_key_that_is_not_an_option_key_is_a_malformed_request(ticker: str) -> None:
    """Malformed rather than unknown: every later request of this shape fails identically."""
    with pytest.raises(MalformedRequestError):
        conventions.parse(ticker)


def test_a_currency_pair_becomes_the_slashed_symbol_a_kanso_id_carries() -> None:
    found = conventions.parse("C:eurusd")

    assert (found.base, found.quote) == ("EUR", "USD")
    assert found.key == "EUR/USD"


@pytest.mark.parametrize("ticker", ["C:EUR", "C:EURUSDX", "C:EUR1SD"])
def test_a_key_that_is_not_a_pair_is_refused(ticker: str) -> None:
    with pytest.raises(MalformedRequestError):
        conventions.parse(ticker)


def test_a_crypto_key_is_refused_by_name_rather_than_read_as_a_stock() -> None:
    with pytest.raises(MalformedRequestError, match="crypto"):
        conventions.parse("X:BTCUSD")


def test_an_unknown_prefix_names_the_prefixes_that_exist() -> None:
    with pytest.raises(MalformedRequestError, match="O:"):
        conventions.parse("Q:WHAT")


def test_a_prefix_with_no_key_after_it_is_refused() -> None:
    with pytest.raises(MalformedRequestError, match="no key after"):
        conventions.parse("O:")


def test_an_empty_string_is_not_a_ticker() -> None:
    with pytest.raises(MalformedRequestError, match="empty string"):
        conventions.parse("  ")


# --- the futures key ------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "product"),
    [("ESZ5", "ES"), ("CLZ30", "CL"), ("MESH6", "MES"), ("ZNZ5", "ZN"), ("6EM5", "6E")],
)
def test_a_contract_code_splits_at_the_digits_however_the_product_is_spelled(
    symbol: str, product: str
) -> None:
    """`ZN` ends in a month letter itself, so the trailing digits are what anchors the split."""
    assert conventions.contract_product(symbol) == product


@pytest.mark.parametrize("symbol", ["AAPL", "ES", "ES1", "esz5"])
def test_a_key_that_is_not_a_contract_code_names_no_product(symbol: str) -> None:
    assert conventions.contract_product(symbol) is None


# --- the conventions -----------------------------------------------------------


def test_a_stock_increment_is_kansos_own_dated_rule_and_not_restated_here() -> None:
    """The regulator's rule lives in one table for the whole package; this asks that table."""
    for price in (0.5, 1.0, 250.0):
        assert conventions.increments(conventions.STOCKS, "XNAS", TODAY, price=price) == (
            core.tick_size("EQUITY", "XNAS", price, TODAY),
            core.lot_size("EQUITY", "XNAS", TODAY),
        )


def test_a_stock_before_the_rule_took_effect_has_no_increment_on_file() -> None:
    assert conventions.increments(conventions.STOCKS, "XNAS", date(2001, 1, 2)) is None


@pytest.mark.parametrize(("price", "tick"), [(0.5, "0.01"), (2.99, "0.01"), (3.0, "0.05")])
def test_an_option_is_quoted_in_pennies_below_three_dollars_and_nickels_above(
    price: float, tick: str
) -> None:
    found = conventions.increments(conventions.OPTIONS, "OPRA", TODAY, price=price)

    assert found == (Decimal(tick), conventions.LOT)


def test_an_option_before_the_penny_programme_has_no_increment_on_file() -> None:
    assert conventions.increments(conventions.OPTIONS, "OPRA", date(2005, 1, 3)) is None


def test_an_index_level_is_published_to_two_decimals() -> None:
    assert conventions.increments(conventions.INDICES, "IDX", TODAY) == (
        Decimal("0.01"),
        conventions.LOT,
    )


@pytest.mark.parametrize(("quote", "tick"), [("USD", "0.00001"), ("JPY", "0.001")])
def test_a_pair_is_quoted_to_the_fractional_pip_of_its_quote_currency(
    quote: str, tick: str
) -> None:
    assert conventions.increments(conventions.FOREX, "FX", TODAY, quote=quote) == (
        Decimal(tick),
        conventions.LOT,
    )


def test_a_pair_with_no_quote_currency_has_no_increment_to_state() -> None:
    assert conventions.increments(conventions.FOREX, "FX", TODAY) is None


def test_a_futures_tick_is_a_contract_term_and_no_convention_states_it() -> None:
    """Guessing a tick misprices a fill; the exchange's own figure arrives in the row."""
    assert conventions.increments(conventions.FUTURES, "XCME", TODAY) is None


def test_a_class_this_adapter_does_not_resolve_is_refused_by_name() -> None:
    with pytest.raises(MalformedRequestError, match="stocks, options"):
        conventions.convention("bonds")


@pytest.mark.parametrize(
    ("named", "expected"),
    [("stocks", "stocks"), ("otc", "stocks"), ("fx", "forex"), ("nope", None), (7, None)],
)
def test_the_vendors_market_field_is_what_settles_a_bare_keys_class(
    named: object, expected: str | None
) -> None:
    assert conventions.market_class(named) == expected


# --- one instrument per class --------------------------------------------------


def test_a_stock_resolves_to_an_equity_at_its_listing_venue() -> None:
    directory = Directory(rows={"AAPL": stock()})
    reader = reading(directory)

    found = reader.resolve(["AAPL"], TODAY)["AAPL"]

    stored = fields_of(found)
    assert (stored["type"], stored["id"]) == ("Equity", "AAPL.XNAS")
    assert (stored["price_increment"], stored["lot_size"], stored["currency"]) == (
        "0.01",
        "1",
        "USD",
    )
    assert reader.sources("AAPL") == {"massive": "AAPL"}
    assert reader.sources("AAPL.XNAS") == {"massive": "AAPL"}


def test_a_stock_whose_row_names_no_exchange_is_quoted_over_the_counter() -> None:
    """The absence is the fact: a US equity with no primary listing trades over the counter."""
    found = one(Directory(rows={"XYZ": stock("XYZ", exchange=7)}), "XYZ")

    assert fields_of(found)["id"] == "XYZ.OTC"


def test_a_venue_keeps_only_what_an_instrument_id_may_carry() -> None:
    found = one(Directory(rows={"XYZ": stock("XYZ", exchange="NYSE-ARCA")}), "XYZ")

    assert fields_of(found)["id"] == "XYZ.NYSEARCA"


def test_an_index_resolves_without_its_market_prefix_in_the_id() -> None:
    """The prefix names a market and a kanso id names a venue; carrying both says it twice."""
    found = one(Directory(rows={"I:NDX": index()}), "I:NDX")

    stored = fields_of(found)
    assert (stored["type"], stored["id"]) == ("IndexInstrument", "NDX.IDX")
    assert (stored["price_increment"], stored["size_increment"]) == ("0.01", "1")


def test_a_pair_resolves_to_a_currency_pair_on_no_venue_at_all() -> None:
    found = one(Directory(rows={"C:EURUSD": pair()}), "C:EURUSD")

    stored = fields_of(found)
    assert (stored["type"], stored["id"]) == ("CurrencyPair", "EUR/USD.FX")
    assert (stored["base_currency"], stored["quote_currency"]) == ("EUR", "USD")
    assert (stored["price_increment"], stored["price_precision"]) == ("0.00001", 5)


def test_a_yen_pair_is_quoted_three_decimals_rather_than_five() -> None:
    found = one(Directory(rows={"C:USDJPY": pair("C:USDJPY")}), "C:USDJPY")

    assert fields_of(found)["price_increment"] == "0.001"


def test_an_option_resolves_with_the_standard_contract_size() -> None:
    found = one(Directory(rows={"O:AAPL261218C00150000": option()}), "O:AAPL261218C00150000")

    stored = fields_of(found)
    assert (stored["type"], stored["id"]) == (
        "OptionContract",
        "AAPL261218C00150000.OPRA",
    )
    assert (stored["option_kind"], stored["strike_price"], stored["multiplier"]) == (
        "CALL",
        "150",
        "100",
    )
    assert (stored["asset_class"], stored["underlying"]) == ("EQUITY", "AAPL")


def test_an_option_key_is_looked_up_where_the_vendor_keeps_it() -> None:
    """The generic ticker reference does not carry an option contract; it rejects the key.

    Sent there, a whole chain comes back "the vendor rejected the shape of this key" — a
    definite reason, an operator correcting a ticker that was already right, and a fixture
    that could not tell the two endpoints apart is why it was invisible.
    """
    directory = Directory(rows={"O:AAPL261218C00150000": option()})

    found = one(directory, "O:AAPL261218C00150000")

    assert fields_of(found)["id"] == "AAPL261218C00150000.OPRA"
    assert directory.asked == [f"{API}{CONTRACTS}/O:AAPL261218C00150000"]
    assert not [url for url in directory.asked if f"{LISTING}/" in url]


def test_an_option_key_the_contracts_endpoint_does_not_carry_is_unknown_and_not_malformed() -> None:
    """A well-formed key the vendor has no contract for is that key's own answer."""
    directory = Directory()

    found = one(directory, "O:AAPL261218C00150000")

    assert isinstance(found, ResolveError)
    assert "carries no such key" in found.reason


@pytest.mark.parametrize(
    ("wanted", "row"),
    [
        ("AAPL", stock()),
        ("I:NDX", index()),
        ("C:EURUSD", pair()),
    ],
)
def test_every_other_class_is_looked_up_in_the_generic_reference(
    wanted: str, row: dict[str, Any]
) -> None:
    """Options are the one exception the source keeps elsewhere, so it is the only one."""
    directory = Directory(rows={wanted: row})

    one(directory, wanted)

    assert directory.asked == [f"{API}{LISTING}/{wanted}"]


def test_an_option_row_that_omits_its_terms_falls_back_to_the_key_which_states_them() -> None:
    """An OSI key encodes a complete date, a side and a strike, so this invents nothing."""
    wanted = "O:AAPL261218P00007500"
    row = option(wanted, strike=None, kind=None, expiry=None)
    found = one(Directory(rows={wanted: row}), wanted)

    stored = fields_of(found)
    assert (stored["option_kind"], stored["strike_price"]) == ("PUT", "7.5")
    assert stored["expiration_ns"] == epoch_ns(date(2026, 12, 18))


def test_an_adjusted_contract_takes_the_size_its_row_states() -> None:
    """A split adjusts the deliverable, and reading it from the row is why it is read at all."""
    row = option(size=10)
    found = one(Directory(rows={"O:AAPL261218C00150000": row}), "O:AAPL261218C00150000")

    assert fields_of(found)["multiplier"] == "10"


def test_a_contract_size_that_is_not_a_number_falls_back_to_the_convention() -> None:
    row = option(size=True)
    found = one(Directory(rows={"O:AAPL261218C00150000": row}), "O:AAPL261218C00150000")

    assert fields_of(found)["multiplier"] == "100"


def test_an_option_on_an_index_records_the_underlyings_own_class() -> None:
    row = option("O:SPX261218C05000000", underlying="I:SPX", strike=5000)
    found = one(Directory(rows={"O:SPX261218C05000000": row}), "O:SPX261218C05000000")

    stored = fields_of(found)
    assert (stored["asset_class"], stored["underlying"]) == ("INDEX", "SPX")


def test_a_future_takes_its_tick_and_its_size_from_the_contract_specification() -> None:
    found = one(Directory(futures={"ESZ6": future()}), "ESZ6")

    stored = fields_of(found)
    assert (stored["type"], stored["id"]) == ("FuturesContract", "ESZ6.XCME")
    assert (stored["price_increment"], stored["multiplier"]) == ("0.25", "50")
    assert (stored["underlying"], stored["asset_class"]) == ("ES", "COMMODITY")


def test_a_futures_key_the_generic_reference_does_not_carry_is_asked_of_its_own_listings() -> None:
    """The generic reference answers a futures key with a 404, measured; the contract and its
    product are asked of `/futures/v1` as of the day asked, and the vendor's spelling of the
    key is what the instrument's sources record."""
    directory = Directory(futures={"ESZ6": future()})
    reader = reading(directory)

    reader.resolve(["ESZ6"], TODAY)

    asked = [url.split("?", 1)[0].removeprefix(API) for url in directory.asked]
    assert asked == [f"{LISTING}/ESZ6", FUTURES_CONTRACTS, FUTURES_PRODUCTS]
    assert reader.sources("ESZ6.XCME") == {"massive": "ESZ6"}


def generic_future(ticker: str = "ES") -> dict[str, Any]:
    """A futures contract as a row of the generic reference would list it, its terms the
    futures listings' (`future`) under the names a generic row is read by. Measured, the
    generic reference lists no futures key — it answered `ESZ5` with a 404, and nothing under
    `market=futures` — so this is the route a key takes only if it ever does."""
    contract, stated = future(ticker)
    return {
        "ticker": ticker,
        "market": "futures",
        "exchange": contract["trading_venue"],
        "min_tick": contract["trade_tick_size"],
        "list_date": contract["first_trade_date"],
        "expiration_date": contract["last_trade_date"],
        "multiplier": stated["unit_of_measure_qty"],
        "underlying_ticker": ticker,
    }


def test_a_bare_key_the_generic_reference_lists_as_a_future_is_resolved_as_one() -> None:
    """The market field decides; nothing infers a class from the shape of a bare symbol."""
    found = one(Directory(rows={"ES": generic_future()}), "ES")

    assert fields_of(found)["type"] == "FuturesContract"


@pytest.mark.parametrize(
    ("dropped", "reason"),
    [
        ("multiplier", "the vendor's row states no contract size"),
        ("underlying_ticker", "the vendor's row names no underlying for this contract"),
    ],
    ids=["no contract size", "no underlying"],
)
def test_a_future_the_generic_reference_lists_without_a_term_fails_by_name(
    dropped: str, reason: str
) -> None:
    """The futures convention states no contract size, and a bare key names no underlying, so
    a generic row that states neither leaves nothing to take one from."""
    row = {name: value for name, value in generic_future().items() if name != dropped}
    found = one(Directory(rows={"ES": row}), "ES")

    assert isinstance(found, ResolveError)
    assert reason in found.reason


@pytest.mark.parametrize(
    ("listed", "reason"),
    [
        (future(tick=None), "states a tick"),
        (future(tick="n/a"), "states a tick"),
        (future(size=None), "contract size"),
        (future(listed=None), "listing date"),
        (future(expiry=None), "expiry"),
        (future(listed="the fifth"), "listing date"),
        (future(exchange=None), "no listing venue"),
        (future(product=None), "lists no product"),
    ],
)
def test_a_row_that_states_no_contract_term_fails_by_name_rather_than_by_guess(
    listed: tuple[dict[str, Any], dict[str, Any]], reason: str
) -> None:
    """Each of these is a number the engine requires and nothing here may invent."""
    ticker = listed[0]["ticker"]
    found = one(Directory(futures={ticker: listed}), ticker)

    assert isinstance(found, ResolveError)
    assert reason in found.reason


def test_a_definition_the_engine_itself_refuses_is_reported_as_the_engine_put_it() -> None:
    """A zero tick passes every check kanso makes and none the engine makes; its refusal is
    the answer, translated by nobody."""
    found = one(Directory(futures={"ESZ6": future(tick=0)}), "ESZ6")

    assert isinstance(found, ResolveError)
    assert "rejected its fields" in found.reason


# --- futures, against the answers the source gave -----------------------------


RECORDED: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {
    (FUTURES_CONTRACTS, (("date", "2025-01-02"), ("ticker", "ESH5"))): (
        "contracts_ESH5_2025-01-02.json"
    ),
    (
        FUTURES_CONTRACTS,
        (("date.gte", "2025-01-02"), ("date.lte", "2025-03-21"), ("ticker", "ESH5")),
    ): "contracts_ESH5_life.json",
    (FUTURES_PRODUCTS, (("date", "2025-01-02"), ("product_code", "ES"), ("type", "single"))): (
        "products_ES_2025-01-02.json"
    ),
    (FUTURES_CONTRACTS, (("date", "2025-01-04"), ("ticker", "ESH5"))): (
        "contracts_ESH5_2025-01-04.json"
    ),
    (
        FUTURES_CONTRACTS,
        (("date.gte", "2024-12-29"), ("date.lte", "2025-01-04"), ("ticker", "ESH5")),
    ): "contracts_ESH5_week_to_2025-01-04.json",
    (FUTURES_PRODUCTS, (("date", "2025-01-03"), ("product_code", "ES"), ("type", "single"))): (
        "products_ES_2025-01-03.json"
    ),
    (
        FUTURES_CONTRACTS,
        (("date.gte", "2025-01-04"), ("date.lte", "2025-03-21"), ("ticker", "ESH5")),
    ): "contracts_ESH5_life_from_2025-01-04.json",
    (FUTURES_CONTRACTS, (("date", "2026-10-28"), ("ticker", "ESZ5"))): (
        "contracts_ESZ5_2026-10-28.json"
    ),
    (
        FUTURES_CONTRACTS,
        (("date.gte", "2026-10-22"), ("date.lte", "2026-10-28"), ("ticker", "ESZ5")),
    ): "contracts_ESZ5_week_to_2026-10-28.json",
    (FUTURES_CONTRACTS, (("date", "2024-10-29"), ("ticker", "ESZ4"))): (
        "contracts_ESZ4_2024-10-29.json"
    ),
    (
        FUTURES_CONTRACTS,
        (("date.gte", "2024-10-29"), ("date.lte", "2024-12-20"), ("ticker", "ESZ4")),
    ): "contracts_ESZ4_life.json",
    (FUTURES_CONTRACTS, (("date", "2026-10-28"), ("ticker", "CLZ6"))): (
        "contracts_CLZ6_2026-10-28.json"
    ),
    (FUTURES_CONTRACTS, (("date", "2026-10-28"), ("ticker", "CLZ30"))): (
        "contracts_CLZ30_2026-10-28.json"
    ),
    (FUTURES_CONTRACTS, (("date", "2026-10-28"), ("ticker", "CLZ36"))): (
        "contracts_CLZ36_2026-10-28.json"
    ),
    (FUTURES_PRODUCTS, (("date", "2026-10-28"), ("product_code", "CL"), ("type", "single"))): (
        "products_CL_2026-10-28.json"
    ),
    (FUTURES_CONTRACTS, (("date", "2026-10-28"), ("ticker", "CLF0"))): (
        "contracts_CLF0_2026-10-28.json"
    ),
    (
        FUTURES_CONTRACTS,
        (("date.gte", "2026-10-22"), ("date.lte", "2026-10-28"), ("ticker", "CLF0")),
    ): "contracts_CLF0_week_to_2026-10-28.json",
}
"""Every futures listing answer recorded from the live source on 2026-10-29, by the request
that drew it, its page size aside: the file and the instant are in `provenance.json`."""


def as_recorded(url: str, params: Mapping[str, str]) -> Response:
    """The futures listings as the source answered them, by request. The generic reference
    answers every key with the 404 it gave ESZ5, as it gave ESH5, CLZ6 and CLF30 theirs."""
    path = url.split("?", 1)[0].removeprefix(API)
    asked = dict(params)
    if path.startswith(f"{LISTING}/"):
        return recorded("reference_tickers_ESZ5.json")  # a futures key there: the 404
    key = tuple(sorted((name, value) for name, value in asked.items() if name != "limit"))
    name = RECORDED.get((path, key))
    if name is None:
        pytest.fail(f"no recorded answer for {path} {asked}")
    return recorded(name)


def recorded_reader() -> tuple[MassiveReference, Replay]:
    replay = Replay(as_recorded)
    return MassiveReference(MassiveClient("test-key-not-a-secret", transport=replay)), replay


def futures_asked(replay: Replay) -> list[tuple[str, dict[str, str]]]:
    """Every futures request a resolution made, its path and its parameters."""
    return [
        (item.url.removeprefix(API), item.params)
        for item in replay.asked
        if "/futures/" in item.url
    ]


def test_a_contract_resolves_from_the_rows_the_source_served() -> None:
    """ESH5 as of 2025-01-02, every answer recorded: its row that day states its life and its
    venue and no tick, the tick comes from the first day of its life that states one, and the
    contract size from ES's product as of that day — 50 index points."""
    reader, _ = recorded_reader()
    found = reader.resolve(["ESH5"], date(2025, 1, 2))["ESH5"]

    stored = fields_of(found)
    assert (stored["type"], stored["id"]) == ("FuturesContract", "ESH5.XCME")
    assert (stored["price_increment"], stored["multiplier"]) == ("0.25", "50")
    assert (stored["underlying"], stored["exchange"], stored["currency"]) == ("ES", "XCME", "USD")
    assert stored["activation_ns"] == epoch_ns(date(2022, 12, 16))
    assert stored["expiration_ns"] == epoch_ns(date(2025, 3, 21))


def test_crude_resolves_though_its_last_trade_falls_in_the_month_before_delivery() -> None:
    """CLZ6 as of 2026-10-28: December's crude last trades on 2026-11-20, as every CL single
    listed that day last trades in the month before its delivery month. The listing names the
    contract; nothing compares its expiry with the key's month."""
    reader, _ = recorded_reader()
    found = reader.resolve(["CLZ6"], date(2026, 10, 28))["CLZ6"]

    stored = fields_of(found)
    assert (stored["type"], stored["id"]) == ("FuturesContract", "CLZ6.XNYM")
    assert (stored["price_increment"], stored["multiplier"]) == ("0.01", "1000")
    assert (stored["underlying"], stored["currency"]) == ("CL", "USD")
    assert stored["expiration_ns"] == epoch_ns(date(2026, 11, 20))
    assert reader.sources("CLZ6.XNYM") == {"massive": "CLZ6"}


def test_a_two_digit_contract_is_asked_for_as_written_and_names_its_own_decade() -> None:
    """CL lists its contracts delivering from 2030 under two year digits, CLZ36 beside CLZ6:
    each key is asked for exactly as written, so CLZ30 resolves, and CLZ36 is December 2036's
    contract and never December 2026's."""
    reader, replay = recorded_reader()

    found = reader.resolve(["CLZ30", "CLZ36"], date(2026, 10, 28))

    assert fields_of(found["CLZ30"])["expiration_ns"] == epoch_ns(date(2030, 11, 20))
    assert fields_of(found["CLZ36"])["expiration_ns"] == epoch_ns(date(2036, 11, 20))
    assert [params.get("ticker") for path, params in futures_asked(replay)] == [
        "CLZ30",
        None,
        "CLZ36",
        None,
    ]


def test_a_key_the_listing_names_no_contract_by_is_refused_with_where_to_read_its_spelling() -> (
    None
):
    """January 2030's crude is CLF30 to the vendor; written CLF0 it names nothing that day nor
    in the week to it, and is refused rather than respelled, pointing at the product's
    listing, which names each contract by the ticker to write."""
    reader, replay = recorded_reader()

    found = reader.resolve(["CLF0"], date(2026, 10, 28))["CLF0"]

    assert isinstance(found, ResolveError)
    assert found.reason == (
        f"{UNKNOWN}: the vendor's futures listing holds no row of CLF0 in the week to "
        "2026-10-28, and kanso asks for a contract exactly as its id spells it: write the "
        "ticker the vendor's listing of CL names the contract by, read off "
        "/futures/v1/contracts?product_code=CL&type=single&date=2026-10-28 — or, if the "
        "contract was not listed then, resolve it as of a day it was"
    )
    assert [params.get("ticker") for _, params in futures_asked(replay)] == ["CLF0", "CLF0"]


def test_a_contract_still_listed_past_its_last_trade_with_no_tick_is_delisted_unread() -> None:
    """The listing named CLX6 eight days after its last trade, inactive (recorded); a row of
    that shape that states no tick, as no row did before 2025-03-12, is past its expiry, so it
    is refused as delisted without its life being read for a tick, and is not sent to a manual
    entry it has no use for."""
    contract, stated = future(tick=None, expiry="2026-12-18")
    listed = {**contract, "active": False, "date": "2026-12-28"}
    replay = Replay(Directory(futures={"ESZ6": (listed, stated)}))
    reader = MassiveReference(MassiveClient("test-key-not-a-secret", transport=replay))

    found = reader.resolve(["ESZ6"], date(2026, 12, 28))["ESZ6"]

    assert isinstance(found, ResolveError)
    assert found.reason == f"{DELISTED} 2026-12-18, before 2026-12-28"
    assert [params for path, params in futures_asked(replay) if path == FUTURES_CONTRACTS] == [
        {"date": "2026-12-28", "limit": "1", "ticker": "ESZ6"}
    ]


def test_a_contract_past_its_last_trade_is_listed_nowhere_and_says_so() -> None:
    reader, _ = recorded_reader()
    found = reader.resolve(["ESZ5"], date(2026, 10, 28))["ESZ5"]

    assert isinstance(found, ResolveError)
    assert "holds no row of ESZ5 in the week to 2026-10-28" in found.reason
    assert "if the contract was not listed then, resolve it as of a day it was" in found.reason


def test_a_day_the_listing_holds_no_row_on_is_answered_by_the_latest_row_of_its_week() -> None:
    """Saturday 2025-01-04, inside ESH5's life: the listing held no row that day, and its week
    held six, the latest the Friday's. The contract resolves from that row, its product as of
    that Friday, and the tick its life first states — the definition it has on the Thursday."""
    reader, replay = recorded_reader()

    saturday = reader.resolve(["ESH5"], date(2025, 1, 4))["ESH5"]
    thursday = reader.resolve(["ESH5"], date(2025, 1, 2))["ESH5"]

    stored = fields_of(saturday)
    assert (stored["id"], stored["price_increment"], stored["multiplier"]) == (
        "ESH5.XCME",
        "0.25",
        "50",
    )
    keep = ("price_increment", "multiplier", "activation_ns", "expiration_ns", "underlying")
    assert {name: stored[name] for name in keep} == {
        name: fields_of(thursday)[name] for name in keep
    }
    assert futures_asked(replay)[:3] == [
        (FUTURES_CONTRACTS, {"date": "2025-01-04", "limit": "1", "ticker": "ESH5"}),
        (
            FUTURES_CONTRACTS,
            {"date.gte": "2024-12-29", "date.lte": "2025-01-04", "limit": "1000", "ticker": "ESH5"},
        ),
        (
            FUTURES_CONTRACTS,
            {"date.gte": "2025-01-04", "date.lte": "2025-03-21", "limit": "1000", "ticker": "ESH5"},
        ),
    ]


def test_a_contract_that_ended_before_the_source_stated_ticks_is_refused_to_a_manual_entry() -> (
    None
):
    """ESZ4, the December 2024 E-mini: the plan serves its minutes from 2024-10-29, and no row of
    its life from then to its last trade states a tick, since the source states a contract's
    terms from 2025-03-12 on. Refused, naming the manual entry, before its product is asked."""
    reader, replay = recorded_reader()

    found = reader.resolve(["ESZ4"], date(2024, 10, 29))["ESZ4"]

    assert isinstance(found, ResolveError)
    assert found.reason == (
        f"{UNKNOWN}: no row of ESZ4's futures listing from 2024-10-29 to its last trade on "
        "2024-12-20 states a tick (the vendor's rows state one from 2025-03-12 on, measured on "
        "ES's); define it as a `manual: true` entry in instruments.yaml whose `override` states "
        "`instrument_class: future` and its `price_increment`, `multiplier`, `lot_size`, "
        "`currency`, `underlying`, `activation_ns` and `expiration_ns`"
    )
    assert all(path == FUTURES_CONTRACTS for path, _ in futures_asked(replay))


def test_the_manual_entry_that_refusal_names_defines_the_contract_and_needs_every_field() -> None:
    """Followed as written, with ESZ4's terms — its row's dates, ES's tick, size and currency —
    the entry builds the future; without any one field it names, it does not."""
    reader, _ = recorded_reader()
    found = reader.resolve(["ESZ4"], date(2024, 10, 29))["ESZ4"]
    assert isinstance(found, ResolveError)
    named = re.findall(r"`([a-z_]+)(?:: future)?`", found.reason.partition("`override`")[2])
    terms = {
        "instrument_class": "future",
        "price_increment": "0.25",
        "multiplier": "50",
        "lot_size": "1",
        "currency": "USD",
        "underlying": "ES",
        "activation_ns": "2021-06-04",
        "expiration_ns": "2024-12-20",
    }
    dated = {"ts_event": date(2024, 10, 29), "ts_init": date(2024, 10, 29)}

    def entry(*fields: str) -> InstrumentEntry:
        return InstrumentEntry(
            nautilus_id="ESZ4.XCME",
            asset_class="COMMODITY",
            manual=True,
            corporate_actions="none",
            override={name: terms[name] for name in fields},
        )

    assert sorted(named) == sorted(terms)
    built = fields_of(build(entry(*named), dated))
    assert (built["type"], built["price_increment"], built["multiplier"]) == (
        "FuturesContract",
        "0.25",
        "50",
    )
    for missing in named:
        refused_for = "implies no instrument class" if missing == "instrument_class" else missing
        with pytest.raises(ValidationError, match=refused_for):
            build(entry(*(name for name in named if name != missing)), dated)


def test_a_product_quoted_in_points_of_par_has_no_contract_size_kanso_reads() -> None:
    """ZN states 100,000 USD and is quoted in points of par, so a point is worth 1,000: the
    quantity is no multiplier, and the contract is refused rather than mispriced a hundredfold."""
    (stated,) = json.loads((FUTURES / "products_ZN_2026-10-27.json").read_text())["results"]
    contract, _ = future("ZNZ6", product="ZN", tick=0.015625)
    found = one(Directory(futures={"ZNZ6": (contract, stated)}), "ZNZ6")

    assert isinstance(found, ResolveError)
    assert "states ZN as 100000 USD" in found.reason
    assert "manual entry" in found.reason


def test_every_contract_unit_was_measured_quoting_its_price_per_one_of_it() -> None:
    """The four units kanso reads a product's quantity in, as the source stated each single
    product on 2026-10-27, and the size each exchange publishes for it."""
    rows = json.loads((FUTURES / "products_singles_2026-10-27.json").read_text())["results"]
    sizes = {
        row["product_code"]: (row["unit_of_measure"], row["unit_of_measure_qty"]) for row in rows
    }

    assert sizes == {
        "6E": ("EUR", 125_000.0),
        "CL": ("BBL", 1_000.0),
        "ES": ("IPNT", 50.0),
        "GC": ("TRYOZ", 100.0),
    }
    assert {unit for unit, _ in sizes.values()} == conventions.CONTRACT_UNITS
    assert all(row["price_quotation"].startswith("U.S. dollars and cents per") for row in rows)


@pytest.mark.parametrize(
    ("answer", "error"),
    [(refused(), NotEntitledError), (rejected(), MalformedRequestError)],
    ids=["refused", "rejected"],
)
def test_a_futures_listing_that_refuses_or_rejects_stops_the_call(
    answer: Response, error: type[Exception]
) -> None:
    """A refusal is the plan, not one contract; a rejection is kanso's own question gone
    wrong. Neither is reported as a key the vendor does not carry."""

    def vendor(url: str, params: Mapping[str, str]) -> Response:
        return recorded("reference_tickers_ESZ5.json") if LISTING in url else answer

    reader = MassiveReference(MassiveClient("test-key-not-a-secret", transport=Replay(vendor)))

    with pytest.raises(error):
        reader.resolve(["ESZ6"], TODAY)


def test_the_front_month_is_the_soonest_single_still_trading() -> None:
    """The listing will not sort by last trade date (HTTP 400, recorded), so it is read whole
    and the soonest chosen here: ESZ6 on 2026-10-28, past ESH0, listed first, and ESH2, listed
    but not yet trading."""
    asked: list[dict[str, str]] = []

    def vendor(url: str, params: Mapping[str, str]) -> Response:
        asked.append(dict(params))
        return recorded("contracts_ES_2026-10-28.json")

    client = MassiveClient("test-key-not-a-secret", transport=Replay(vendor))

    assert front_month(client, "ES", date(2026, 10, 28)) == "ESZ6"
    assert front_month(client, "ES", date(2028, 1, 3)) == "ESH0", "the same rows, a later day"
    assert front_month(client, "ES", date(2031, 1, 2)) is None, "ESH2 is listed, not trading"
    assert "sort" not in asked[0] and asked[0]["type"] == "single"
    assert json.loads(recorded("contracts_sort_by_last_trade_date.json").body)["status"] == "ERROR"


def test_a_front_month_listing_that_answers_nothing_names_no_key() -> None:
    client = MassiveClient("test-key-not-a-secret", transport=Replay(lambda url, params: refused()))

    assert front_month(client, "ES", date(2026, 10, 28)) is None


def test_an_option_whose_side_nobody_states_does_not_resolve() -> None:
    row = option(kind="straddle")
    found = one(Directory(rows={"O:AAPL261218C00150000": row}), "O:AAPL261218C00150000")

    assert isinstance(found, ResolveError)
    assert "call or a put" in found.reason


def test_an_option_older_than_the_penny_programme_has_no_increment_anywhere() -> None:
    """Asked as of a date inside the contract's own life, so this is the increment and not
    the calendar: before the programme, no schedule was in force and the row states none."""
    row = option("O:AAPL060616C00150000", expiry="2006-06-16", listed="2005-06-17")
    found = one(
        Directory(rows={"O:AAPL060616C00150000": row}), "O:AAPL060616C00150000", date(2006, 1, 3)
    )

    assert isinstance(found, ResolveError)
    assert "minimum price increment" in found.reason


# --- the dated question --------------------------------------------------------


def test_an_instrument_delisted_before_the_date_is_a_delisting_and_says_when() -> None:
    row = stock("OLD", delisted="2019-04-01T00:00:00Z")
    found = one(Directory(rows={"OLD": row}), "OLD")

    assert isinstance(found, ResolveError)
    assert found.reason.startswith(f"{DELISTED} 2019-04-01")


def test_an_instrument_listed_after_the_date_is_not_yet_listed() -> None:
    found = one(Directory(rows={"NEW": stock("NEW", listed="2026-10-01")}), "NEW")

    assert isinstance(found, ResolveError)
    assert found.reason.startswith(NOT_YET_LISTED)


def test_a_contract_that_expired_before_the_date_is_delisted_by_its_own_expiry() -> None:
    """A derivative stops existing at expiry, which is a delisting for a dated question."""
    row = option("O:AAPL220617C00150000", expiry="2022-06-17", listed="2021-06-18")
    found = one(Directory(rows={"O:AAPL220617C00150000": row}), "O:AAPL220617C00150000")

    assert isinstance(found, ResolveError)
    assert DELISTED in found.reason


def test_reference_has_no_history_floor_even_where_the_aggregates_do() -> None:
    """The measured asymmetry: option aggregates begin at a floor and option reference does not."""
    row = option("O:AAPL220617C00150000", expiry="2022-06-17", listed="2021-06-18")
    directory = Directory(rows={"O:AAPL220617C00150000": row})
    as_of = date(2022, 6, 1)
    assert as_of < AGGREGATE_FLOOR

    found = one(directory, "O:AAPL220617C00150000", as_of)

    assert fields_of(found)["id"] == "AAPL220617C00150000.OPRA"
    assert all("/v2/aggs" not in url for url in directory.asked)


# --- the failures that are not the four ----------------------------------------


def test_a_key_the_vendor_does_not_carry_is_unknown() -> None:
    found = one(Directory(), "NOPE")

    assert isinstance(found, ResolveError)
    assert found.reason.startswith(UNKNOWN)


def test_a_rejected_key_shape_is_this_keys_failure_alone() -> None:
    found = one(Directory(rows={"AAPL": stock()}, lookup="rejected"), "AAPL")

    assert isinstance(found, ResolveError)
    assert "rejected the shape" in found.reason


def test_a_refused_reference_lookup_stops_the_call_rather_than_marking_one_key_unknown() -> None:
    """Reference is not gated per key, so a refusal there is the credential or the plan."""
    directory = Directory(rows={"AAPL": stock()}, lookup="refused")

    with pytest.raises(NotEntitledError) as raised:
        reading(directory).resolve(["AAPL", "MSFT"], TODAY)

    assert raised.value.code is Exit.PRECONDITION
    assert raised.value.fatal is False
    assert len(directory.asked) == 1


def test_no_answer_at_all_is_a_transport_failure_and_not_an_outcome() -> None:
    with pytest.raises(TransportError):
        one(Directory(rows={"AAPL": stock()}, lookup="broken"), "AAPL")


def test_a_market_this_adapter_does_not_resolve_names_the_ones_it_does() -> None:
    found = one(Directory(rows={"BTC": stock("BTC", market="crypto")}), "BTC")

    assert isinstance(found, ResolveError)
    assert "stocks, options" in found.reason


def test_a_row_that_names_no_market_leaves_a_bare_key_unresolved() -> None:
    found = one(Directory(rows={"HUH": {"ticker": "HUH"}}), "HUH")

    assert isinstance(found, ResolveError)
    assert found.reason.startswith(UNKNOWN)


def test_a_key_that_cannot_be_an_instrument_id_fails_as_that_key_rather_than_the_run() -> None:
    found = one(Directory(rows={"BRK B": stock("BRK B", exchange="XNYS")}), "BRK B")

    assert isinstance(found, ResolveError)
    assert "is not an instrument id" in found.reason


def test_a_malformed_key_costs_no_request_and_leaves_the_universe_resolving() -> None:
    directory = Directory(rows={"AAPL": stock()})

    answered = reading(directory).resolve(["O:NOPE", "AAPL"], TODAY)

    assert isinstance(answered["O:NOPE"], ResolveError)
    assert fields_of(answered["AAPL"])["id"] == "AAPL.XNAS"
    assert directory.lookups == ["AAPL"]


def test_one_key_asked_twice_is_looked_up_once() -> None:
    directory = Directory(rows={"AAPL": stock()})

    answered = reading(directory).resolve(["AAPL", "AAPL"], TODAY)

    assert list(answered) == ["AAPL"]
    assert directory.lookups == ["AAPL"]


def test_an_instrument_this_provider_never_resolved_has_no_vendor_symbol() -> None:
    assert reading(Directory()).sources("AAPL.XNAS") == {}


# --- what the core does with an answer -----------------------------------------


@pytest.mark.parametrize(
    ("wanted", "row", "asset_class"),
    [
        ("AAPL", stock(), "EQUITY"),
        ("I:NDX", index(), "INDEX"),
        ("C:EURUSD", pair(), "FX"),
        ("O:AAPL261218C00150000", option(), "EQUITY"),
        ("ESZ6", future(), "COMMODITY"),
    ],
)
def test_every_answer_survives_the_rebuild_the_core_puts_it_through(
    wanted: str, row: Any, asset_class: str
) -> None:
    """The core reconstructs a resolved definition so an override applies over it; a class
    whose fields did not survive that would be resolved once and lost on the next run."""
    directory = (
        Directory(futures={wanted: row})
        if isinstance(row, tuple)
        else Directory(rows={wanted: row})
    )
    found = one(directory, wanted)

    assert definition_checksum(rebuilt(found, asset_class)) == definition_checksum(found)


# --- universes, chains and the entitlement over them ---------------------------


def test_a_universe_is_the_whole_cursor_walk_and_not_its_first_page() -> None:
    """The real index universe runs to several thousand keys behind a cursor."""
    directory = Directory(
        pages=(
            (index("I:NDX"), index("I:SPX")),
            (index("I:ACQQIV"),),
        )
    )
    client = MassiveClient("test-key-not-a-secret", transport=Replay(directory))

    assert universe(client, conventions.INDICES) == ("I:NDX", "I:SPX", "I:ACQQIV")
    assert len(directory.asked) == 2


def test_a_universe_applies_no_feed_allowlist_of_any_kind() -> None:
    """Feeds are entitled in part, so a name filter drops keys the plan does serve."""
    directory = Directory(
        pages=(
            (
                index("I:SPX", feed="CboeGlobalIndicesMain"),
                index("I:ACQQIV", feed="CboeGlobalIndicesINAV"),
            ),
        )
    )
    client = MassiveClient("test-key-not-a-secret", transport=Replay(directory))

    assert universe(client, conventions.INDICES) == ("I:SPX", "I:ACQQIV")


def test_a_universe_asks_for_the_market_and_the_state_it_was_asked_about() -> None:
    directory = Directory(pages=((stock(),),))
    replay = Replay(directory)
    client = MassiveClient("test-key-not-a-secret", transport=replay)

    universe(client, conventions.STOCKS, active=False, page=250)

    assert replay.asked[0].params == {"market": "stocks", "active": "false", "limit": "250"}


def test_a_universe_of_a_class_this_adapter_does_not_resolve_is_refused() -> None:
    client = MassiveClient("test-key-not-a-secret", transport=Replay(Directory()))

    with pytest.raises(MalformedRequestError):
        universe(client, "bonds")


def test_entitlement_over_an_index_universe_is_probed_one_key_at_a_time() -> None:
    """The measured case: one index returns bars and the next does not, on one endpoint."""
    directory = Directory(
        rows={"I:NDX": index("I:NDX"), "I:SPX": index("I:SPX")},
        entitled=frozenset({"I:NDX"}),
    )
    client = MassiveClient("test-key-not-a-secret", transport=Replay(directory))

    assert entitled(client, ["I:NDX", "I:SPX", "I:NDX"], conventions.INDICES, as_of=TODAY) == (
        "I:NDX",
    )


def test_the_probe_and_not_the_feed_name_is_what_decides_an_index() -> None:
    """Arranged so a feed-name filter answers the opposite way round from the truth."""
    directory = Directory(
        rows={
            "I:ACQQIV": index("I:ACQQIV", feed="CboeGlobalIndicesINAV"),
            "I:SPX": index("I:SPX", feed="CboeGlobalIndicesMain"),
        },
        entitled=frozenset({"I:ACQQIV"}),
    )
    client = MassiveClient("test-key-not-a-secret", transport=Replay(directory))

    assert entitled(client, ["I:ACQQIV", "I:SPX"], conventions.INDICES, as_of=TODAY) == (
        "I:ACQQIV",
    )


def test_a_class_the_source_gates_as_a_whole_is_probed_once_for_the_whole_class() -> None:
    """Only the caching is coarsened; every key is still asked about at its own grain."""
    directory = Directory(entitled=frozenset({"AAPL"}), rows={"AAPL": stock()})
    client = MassiveClient("test-key-not-a-secret", transport=Replay(directory))

    assert entitled(client, ["AAPL", "MSFT"], conventions.STOCKS, as_of=TODAY) == ("AAPL", "MSFT")
    assert len([url for url in directory.asked if "/v2/aggs" in url]) == 1


def test_a_chain_is_walked_to_its_end_and_asks_for_the_expiries_it_was_given() -> None:
    directory = Directory(
        chain=(
            ({"ticker": "O:AAPL251219C00150000"},),
            ({"ticker": "O:AAPL251219P00150000"},),
        )
    )
    replay = Replay(directory)
    client = MassiveClient("test-key-not-a-secret", transport=replay)

    found = contracts(client, "AAPL", expiring=(date(2025, 12, 1), date(2025, 12, 31)), page=100)

    assert found == ("O:AAPL251219C00150000", "O:AAPL251219P00150000")
    assert replay.asked[0].params == {
        "underlying_ticker": "AAPL",
        "limit": "100",
        "expiration_date.gte": "2025-12-01",
        "expiration_date.lte": "2025-12-31",
    }


def test_a_chain_asks_only_for_its_underlying_when_no_window_is_named() -> None:
    directory = Directory(chain=(({"ticker": "O:AAPL251219C00150000"}, {"nope": 1}),))
    replay = Replay(directory)
    client = MassiveClient("test-key-not-a-secret", transport=replay)

    assert contracts(client, "AAPL") == ("O:AAPL251219C00150000",)
    assert "expiration_date.gte" not in replay.asked[0].params


# --- the workspace factory ------------------------------------------------------


def test_the_workspace_factory_opens_a_provider_from_the_configured_credential(
    tmp_path: Path,
) -> None:
    """The credential is resolved here rather than at import, so an unset workspace imports."""
    ws: Workspace = init(tmp_path / "ws")
    (ws.root / ".env").write_text(f"{massive.API_KEY}=test-key-not-a-secret\n")
    directory = Directory(rows={"AAPL": stock()})

    reader = provider(ws, transport=Replay(directory))

    assert reader.id == massive.ID
    assert fields_of(reader.resolve(["AAPL"], TODAY)["AAPL"])["id"] == "AAPL.XNAS"
