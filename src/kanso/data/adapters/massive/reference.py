"""Reference resolution against Massive: a key and a date become an engine instrument.

This is kanso's `InstrumentProvider` for the vendor. It answers the dated question the
core asks — what was this instrument on this day — for the five classes the adapter
serves, and it answers it from the vendor's *reference* endpoints alone.

**Reference is not aggregates, and the difference is a floor.** The vendor's aggregate
history is bounded: for several classes the plan grants a rolling window, so bars older
than it do not exist for the operator. Reference carries no such window. A contract that
expired years before the aggregate floor still has a definition, and asking for one is
never a history-floor question — so nothing here probes a floor, and a caller must not
read a resolution failure as one. Getting this backwards would refuse an option chain
because its *prices* are out of plan, which is a different sentence about a different
thing.

**The key's own spelling settles the market, or the source does.** A prefixed key names
its market (`O:` an option, `C:` a pair, `I:` an index) and its format carries the
contract: an OSI option key states the expiry, the kind and the strike exactly, and those
are used where the row omits them. A bare key is a stock or a futures contract and the
string cannot tell which, so the source decides: the generic reference's row names its
market, and a key it does not carry is a futures contract only if the futures reference
lists one under it. Nothing infers a market from the shape of a bare symbol alone.

**The market also settles which endpoint holds the key.** Option contracts are keyed
outside the generic ticker reference, which rejects an option key with a client error, so
a chain looked up there fails every contract in it as an unrecognised shape — a definite,
confident and entirely wrong reason. The lookup endpoint is chosen the way entitlement is,
by asking the source the question it can answer.

**A futures contract is defined by two listings of its own.** The vendor keeps contracts
under `/futures/v1/contracts` and their products under `/futures/v1/products`, both dated
point-in-time listings, and its generic ticker reference answers a futures key with a 404. A
bare key the generic reference does not carry, and that is spelled as a contract code, is
asked for there exactly as it is written, as of the day asked: the vendor spells each
contract its own way — `ESZ4`, `ESH0` and `CLZ9` with one year digit, `CLF30` and `CLZ36`
with two — and its listing as of that day is what names the contract, so no year is read out
of a key and no key is respelled. Its venue, tick and life come from the contract's row, and
its currency, product and contract size from its product's. Each field was read off rows
measured on 2026-10-29 before it was trusted (`_future`), and the asset class stays the
convention's: the product names one, but the engine's class is what picks a market calendar,
and none is on file for an exchange's futures.

**A universe is walked, not sampled, and never filtered on a feed.** The listing endpoint
answers a page at a time behind a cursor and the real index universe runs to several
thousand keys, so `universe` follows the cursor to the end and a short first page is never
mistaken for the whole market. Entitlement is then established one key at a time, by
probing: index entitlement is decided by the feed behind each key, feeds are entitled in
part, and a filter on a feed name silently drops keys the plan does serve. This module
therefore reads a feed name for nobody and passes every key to the probe.

**Four failures, each naming what the operator does next.** An unknown key, an ambiguous
one, one delisted before the date and one listed after it are the core's four reasons, and
they are reported per key so one report fixes a whole universe. A refused *reference*
lookup is none of the four and is not reported per key at all: the control endpoint is not
gated per key, so a refusal there is the credential or the plan, and it stops the call
rather than marking one instrument unknown. That distinction is the whole adapter in
miniature — the same refusal means different things in different places, and only where it
was asked tells them apart.

Ambiguity never arises here. The vendor's reference is keyed by its own ticker, which is
unique across its markets; the ambiguity kanso does report comes from the workspace file,
where two entries can claim one symbol, and the core settles that before a provider is
asked.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
The five instrument classes this resolves into — `Equity`, `OptionContract`,
`FuturesContract`, `CurrencyPair` and `IndexInstrument` — are constructed by
`kanso.data.instruments.build`, which owns the field sets, the coercions and the engine's
own refusals; nothing about a constructor is restated here. `price_precision` must equal
`price_increment.precision`, which is why an increment is supplied and a precision never
is. A definition's `ts_event` and `ts_init` are the day it was resolved as of: a definition
is a dated fact and it was public on the date it describes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Final

from kanso.data.adapters.massive import conventions
from kanso.data.adapters.massive.client import MassiveClient, Signal
from kanso.data.adapters.massive.entitlement import (
    BARS,
    FUTURES_CONTRACTS,
    FUTURES_PRODUCTS,
    OPTION_CONTRACTS,
    REFERENCE,
    Endpoint,
    Entitlements,
    control_for,
)
from kanso.data.adapters.massive.errors import MalformedRequestError, NotEntitledError
from kanso.data.instruments import (
    DELISTED,
    NOT_YET_LISTED,
    UNKNOWN,
    InstrumentProvider,
    ResolveError,
    build,
)
from kanso.errors import ValidationError
from kanso.schemas import InstrumentEntry

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.data.adapters.massive.client import Transport
    from kanso.workspace import Workspace

__all__ = [
    "CONTRACTS",
    "LISTING",
    "PAGE",
    "MassiveReference",
    "contracts",
    "entitled",
    "front_month",
    "provider",
    "universe",
]

LISTING: Final = "/v3/reference/tickers"
"""The universe endpoint. It answers a page at a time behind a cursor, so every caller
here walks it to the end; one page is a sample and a sample is not a universe."""

CONTRACTS: Final = OPTION_CONTRACTS
"""The option chain endpoint, also cursor-paged. It is reference, so it has no history
window: a chain that expired before the aggregate floor still lists.

The path is the one an option key's *control* lookup uses, so it is defined once, in the
module every other one imports, and named again here rather than spelled again: two
spellings of one path drift, and the day they do the chain walk and the key check disagree
about where the vendor keeps a contract."""

PAGE: Final = 1_000
"""Rows asked for per page. It is a page size and never a cap on the answer, because the
cursor is followed; a `limit` that capped an answer would make a short walk look complete."""

WEEK: Final = timedelta(days=6)
"""How far before an as-of day with no futures listing row of its own a contract's latest row
is looked for: the six days before it and the day itself. Measured on 2026-10-29, `ESH5`'s
listing from 2025-01-02 to 2025-03-21 held no row on the ten Saturdays before 2025-03-12 nor
on Friday 2025-01-10, two days in a row at the longest."""

VENUE_FIELDS: Final = ("primary_exchange", "exchange")
CURRENCY_FIELDS: Final = ("currency_name", "currency")
LISTED_FIELDS: Final = ("list_date", "listing_date")
DELISTED_FIELDS: Final = ("delisted_utc", "delisting_date")
EXPIRY_FIELDS: Final = ("expiration_date",)
STRIKE_FIELDS: Final = ("strike_price",)
KIND_FIELDS: Final = ("contract_type", "option_kind")
UNDERLYING_FIELDS: Final = ("underlying_ticker", "underlying")
SIZE_FIELDS: Final = ("shares_per_contract", "contract_size", "trade_multiplier", "multiplier")
TICK_FIELDS: Final = ("min_tick", "tick_size", "minimum_tick")
"""Where each fact is read from a reference row.

Each is a list because the vendor spells one fact differently across its own reference
endpoints, and the first key a row carries wins. Where a row carries none of them the class
convention supplies the value, and where no convention states one either the resolution
fails naming the field — a definition is never completed by a guess.
"""

VENUE_CHARS: Final = 16
"""How long a venue may be in an instrument id. The engine's own limit, and a MIC is four."""


class MassiveReference(InstrumentProvider):
    """Resolves the vendor's keys into engine instruments, one authenticated lookup each.

    Stateless but for a memo of which key produced which instrument, which is what
    `sources` reports: the mapping from a kanso id back to the vendor's own spelling is
    the one thing a caller cannot recompute, since a kanso id deliberately drops the
    vendor's market prefix.
    """

    id: ClassVar[str] = conventions.VENDOR

    def __init__(self, client: MassiveClient, *, control: Endpoint = REFERENCE) -> None:
        self._client = client
        self._generic = control
        self._seen: dict[str, str] = {}

    def resolve(self, ids: Sequence[str], as_of: date) -> dict[str, object]:
        """A definition or a `ResolveError` for every key, keyed as it was asked.

        One lookup per key, in the order asked, with no probe of any aggregate endpoint:
        reference has no history window, so `as_of` is answered from the row's own listing
        dates rather than from a floor.
        """
        return {wanted: self._one(wanted, as_of) for wanted in dict.fromkeys(ids)}

    def sources(self, instrument_id: str) -> dict[str, str]:
        """The vendor's own key for an instrument this provider resolved, if it did."""
        found = self._seen.get(instrument_id)
        return {} if found is None else {self.id: found}

    # --- one key ---------------------------------------------------------------

    def _one(self, wanted: str, as_of: date) -> object:
        try:
            ticker = conventions.parse(wanted)
        except MalformedRequestError as exc:
            return ResolveError(wanted, f"{UNKNOWN}: {exc.message}")
        row = self._row(wanted, ticker.text, ticker.asset_class)
        contract = None if ticker.asset_class else conventions.contract_product(ticker.key)
        if isinstance(row, ResolveError) and contract is not None:
            row = self._future(wanted, ticker.key, as_of)
        if isinstance(row, ResolveError):
            return row
        asset_class = ticker.asset_class or conventions.market_class(row.get("market"))
        if asset_class is None or asset_class not in conventions.CLASSES:
            return ResolveError(
                wanted,
                f"{UNKNOWN}: the vendor lists it in the "
                f"{row.get('market', 'unnamed')!r} market, which this adapter does not "
                f"resolve; it resolves {', '.join(conventions.CLASSES)}",
            )
        listed, ends = _life(row, ticker)
        if ends is not None and ends < as_of:
            return ResolveError(wanted, f"{DELISTED} {ends}, before {as_of}")
        if listed is not None and listed > as_of:
            return ResolveError(wanted, f"{NOT_YET_LISTED} {as_of}: it was listed {listed}")
        return self._instrument(wanted, ticker, asset_class, row, (listed, ends), as_of)

    def _row(
        self, wanted: str, ticker: str, asset_class: str | None
    ) -> Mapping[str, Any] | ResolveError:
        """The reference row for one key, or the failure that is this key's alone.

        **The key is asked for where the vendor keeps it.** An option contract is not in
        the generic ticker reference, which rejects an option key outright with a client
        error, so a lookup sent there reports a contract the vendor defines perfectly well
        as a key whose shape it does not recognise — and a whole chain resolves to that one
        wrong reason. The endpoint is therefore chosen by the class the key's own prefix
        names; a bare key names none and is a stock or a futures contract, both of which
        the generic reference carries.

        A refusal is not this key's failure: the control endpoint is not gated per key, so
        a refusal there says the credential or the plan cannot read reference at all, and
        marking one instrument unknown would hide that behind a universe of them.
        """
        control = self._generic if asset_class is None else control_for(asset_class, self._generic)
        path, params = control.request(ticker)
        call = self._client.call(path, params)
        call.raise_for_transport()
        if call.signal is Signal.REFUSED:
            raise NotEntitledError(
                f"massive: the reference endpoint refused {ticker}; reference is not gated "
                "per key, so this is the credential or the plan rather than the key",
                remedy="run `kanso data adapters --check` to see whether the key authenticates",
            )
        if call.signal is Signal.BAD_REQUEST:
            return ResolveError(wanted, f"{UNKNOWN}: the vendor rejected the shape of this key")
        if not call.rows:
            return ResolveError(wanted, f"{UNKNOWN}: the vendor's reference carries no such key")
        return call.rows[0]

    def _future(self, wanted: str, ticker: str, as_of: date) -> Mapping[str, Any] | ResolveError:
        """A futures contract as of `as_of`, from its contract's row and its product's, in the
        field names every other row here is read by.

        Measured on 2026-10-29 against `/futures/v1`: asked for `ESZ5` as of 2025-06-02, the
        contract's row stated `trading_venue` XCME, `trade_tick_size` 0.25, `first_trade_date`
        2021-06-07 and `last_trade_date` 2025-12-19 (`settlement_date` the same day), and
        `product_code` ES; ES's single product as of 2025-01-02 stated `unit_of_measure` IPNT,
        `unit_of_measure_qty` 50 and `trade_currency_code` USD. The last trade date is the
        contract's expiry: `ESZ5` as of 2026-10-28 was answered with no row, nor was any day of
        the week to it, while `CLX6`, last traded 2026-10-20, was still listed that day,
        inactive; neither resolves past its expiry. A row before 2025-03-12 states no tick
        (`_stated_tick`), and a product's quantity is its contract size only in a unit measured
        to be one (`conventions.CONTRACT_UNITS`).

        The key is asked for exactly as written, and the vendor's own spellings differ by
        product — measured, ES's 2030 contracts are `ESH0` to `ESZ0`, and CL's are `CLF30` to
        `CLZ30` beside `CLZ9` for 2029 — so a key the listing names no contract by is refused
        with where to read the spelling it does.
        """
        contract = self._contract(ticker, as_of)
        if contract is None:
            named = conventions.contract_product(ticker)
            return ResolveError(
                wanted,
                f"{UNKNOWN}: the vendor's futures listing holds no row of {ticker} in the "
                f"week to {as_of}, and kanso asks for a contract exactly as its id spells it: "
                f"write the ticker the vendor's listing of {named} names the contract by, read "
                f"off /futures/v1/contracts?product_code={named}&type=single&date={as_of} — "
                "or, if the contract was not listed then, resolve it as of a day it was",
            )
        last = _text(contract, "last_trade_date")
        tick = _number(contract, "trade_tick_size")
        if tick is None and last is not None and last >= as_of.isoformat():
            tick = self._stated_tick(ticker, as_of, last)
            if tick is None:
                return ResolveError(
                    wanted,
                    f"{UNKNOWN}: no row of {ticker}'s futures listing from {as_of} to its last "
                    f"trade on {last} states a tick (the vendor's rows state one from "
                    "2025-03-12 on, measured on ES's); define it as a `manual: true` entry in "
                    "instruments.yaml whose `override` states `instrument_class: future` and "
                    "its `price_increment`, `multiplier`, `lot_size`, `currency`, `underlying`, "
                    "`activation_ns` and `expiration_ns`",
                )
        code = _text(contract, "product_code")
        day = _text(contract, "date") or as_of.isoformat()
        stated = (
            ()
            if code is None
            else self._listing(
                FUTURES_PRODUCTS,
                {"product_code": code, "type": "single", "date": day, "limit": "1"},
            )
        )
        if not stated:
            return ResolveError(
                wanted, f"{UNKNOWN}: the vendor's futures reference lists no product of {ticker}"
            )
        product = stated[0]
        unit = _text(product, "unit_of_measure")
        size = _whole(_number(product, "unit_of_measure_qty"))
        if unit not in conventions.CONTRACT_UNITS or size is None:
            return ResolveError(
                wanted,
                f"{UNKNOWN}: the vendor states {code} as {size} {unit}, and kanso reads a "
                f"product's quantity as its contract size only in "
                f"{', '.join(sorted(conventions.CONTRACT_UNITS))}; define it as a manual entry "
                "with its multiplier",
            )
        return {
            "ticker": ticker,
            "market": conventions.MARKET_OF[conventions.FUTURES],
            "exchange": _text(contract, "trading_venue"),
            "tick_size": tick,
            "list_date": _text(contract, "first_trade_date"),
            "expiration_date": last,
            "currency": _text(product, "trade_currency_code"),
            "underlying_ticker": code,
            "multiplier": size,
        }

    def _contract(self, ticker: str, as_of: date) -> Mapping[str, Any] | None:
        """The contract's row on `as_of`, or, where the listing holds none that day, its latest
        row of the week to it (`WEEK`); `None` where the week holds none either.

        Measured on 2026-10-29: `ESH5`, first traded 2022-12-16 and last 2025-03-21, was
        answered with no row as of Saturday 2025-01-04, and its week to that day held six, the
        latest 2025-01-03's. A day with no row is no sign that the contract was not listed, so
        the latest row before it describes the contract as the source last did.
        """
        day = as_of.isoformat()
        rows = self._listing(FUTURES_CONTRACTS, {"ticker": ticker, "date": day, "limit": "1"})
        if not rows:
            week = {
                "ticker": ticker,
                "date.gte": (as_of - WEEK).isoformat(),
                "date.lte": day,
                "limit": str(PAGE),
            }
            rows = self._listing(FUTURES_CONTRACTS, week)
        return max(rows, key=lambda row: _text(row, "date") or "", default=None)

    def _stated_tick(self, ticker: str, as_of: date, last: str) -> Decimal | None:
        """The contract's tick on the earliest day of its life the source states one.

        Measured on 2026-10-29: `ESH5`'s 58 rows from 2025-01-02 to 2025-03-11 named its life,
        its product and its venue and no tick, and its ten from 2025-03-12, the day the source
        began stating a contract's terms, stated 0.25; they came back the stated days first,
        not in date order, so every row from `as_of` to the last trade date is read. A tick is
        a term of the contract, fixed when it was listed, so the earliest day that states it
        answers for the whole of its life. A contract whose life ended before 2025-03-12 has
        no such day: `ESZ4`'s 46 rows from 2024-10-29 to its last trade on 2024-12-20 stated
        none.
        """
        params = {
            "ticker": ticker,
            "date.gte": as_of.isoformat(),
            "date.lte": last,
            "limit": str(PAGE),
        }
        stated = sorted(
            (day, tick)
            for row in self._client.rows(FUTURES_CONTRACTS, params)
            if (tick := _number(row, "trade_tick_size")) is not None
            and (day := _text(row, "date")) is not None
        )
        return stated[0][1] if stated else None

    def _listing(self, path: str, params: Mapping[str, str]) -> tuple[Mapping[str, Any], ...]:
        """The rows one page of a futures listing answers, none when it answers none.

        A refusal is the credential or the plan, as a refused reference lookup is, and stops
        the call; a rejected request is kanso's question gone wrong, never a key's absence.
        """
        call = self._client.call(path, dict(params))
        call.raise_for_transport()
        if call.signal is Signal.REFUSED:
            raise NotEntitledError(
                f"massive: {path} refused {params}; the futures reference is not gated per "
                "contract, so this is the credential or the plan",
                remedy="run `kanso data adapters --check` to see whether the plan reaches futures",
            )
        if call.signal is Signal.BAD_REQUEST:
            raise MalformedRequestError(
                f"massive: {path} rejected {params} (HTTP {call.status})",
                remedy="report it: kanso asked the futures reference a question it does not take",
            )
        return call.rows

    def _instrument(
        self,
        wanted: str,
        ticker: conventions.Ticker,
        asset_class: str,
        row: Mapping[str, Any],
        life: tuple[date | None, date | None],
        as_of: date,
    ) -> object:
        """The engine instrument this row describes, or why it does not describe one."""
        rule = conventions.convention(asset_class)
        venue = _venue(row, rule)
        if venue is None:
            return ResolveError(
                wanted,
                f"{UNKNOWN}: the vendor's row names no listing venue and a "
                f"{rule.asset_class} contract has no default one",
            )
        fields = _increments(rule, row, venue, ticker, as_of)
        if isinstance(fields, str):
            return ResolveError(wanted, fields)
        extra = _contract_fields(rule, row, ticker, life, venue)
        if isinstance(extra, str):
            return ResolveError(wanted, extra)
        named = extra.pop("engine_asset_class", rule.engine_asset_class)
        try:
            entry = _entry(ticker, rule, venue, named)
        except ValidationError:
            return ResolveError(
                wanted, f"{UNKNOWN}: {ticker.key!r} on {venue} is not an instrument id"
            )
        try:
            found = build(entry, {**fields, **extra, "ts_event": as_of, "ts_init": as_of})
        except ValidationError as exc:
            return ResolveError(wanted, f"{UNKNOWN}: {exc.message}")
        self._seen[wanted] = ticker.text
        self._seen[entry.nautilus_id] = ticker.text
        return found


def _entry(
    ticker: conventions.Ticker,
    rule: conventions.Convention,
    venue: str,
    engine_asset_class: object,
) -> InstrumentEntry:
    """The entry `build` constructs from: an identity and the class to build.

    A construction vehicle rather than a cache entry — it is never written and never read
    back — so it carries no corporate-action policy of its own and no operator override.
    The operator's own override is applied by the core, over what this returns.
    """
    override: dict[str, object] = {}
    if rule.instrument_class is not None:
        override["instrument_class"] = rule.instrument_class
    return InstrumentEntry(
        nautilus_id=f"{ticker.key}.{venue}",
        asset_class=str(engine_asset_class),
        corporate_actions="none",
        override=override,
    )


def _venue(row: Mapping[str, Any], rule: conventions.Convention) -> str | None:
    """The venue an instrument id carries: the row's, where the class has one, else the
    convention's, and `None` where neither states one."""
    if not rule.venue_from_row:
        return rule.venue
    stated = _text(row, *VENUE_FIELDS)
    cleaned = "".join(character for character in (stated or "").upper() if character.isalnum())
    return cleaned[:VENUE_CHARS] or rule.venue


def _increments(
    rule: conventions.Convention,
    row: Mapping[str, Any],
    venue: str,
    ticker: conventions.Ticker,
    as_of: date,
) -> dict[str, object] | str:
    """The price increment and lot, from the convention or, failing one, from the row.

    The convention wins wherever it states anything, because a minimum increment is a rule
    of the market rather than a vendor's rounding. Only where no convention states one —
    a futures contract, whose tick is a term of the contract itself — is the row read, and
    where the row is silent too the resolution fails rather than inventing a tick.
    """
    found = conventions.increments(rule.asset_class, venue, as_of, quote=ticker.quote)
    if found is None:
        stated = _number(row, *TICK_FIELDS)
        if stated is None:
            return (
                f"{UNKNOWN}: neither kanso's conventions nor the vendor's row states a "
                f"minimum price increment for a {rule.asset_class} contract"
            )
        found = (stated, conventions.LOT)
    tick, lot = found
    return {"price_increment": tick, "lot_size": lot, "size_increment": lot}


def _contract_fields(
    rule: conventions.Convention,
    row: Mapping[str, Any],
    ticker: conventions.Ticker,
    life: tuple[date | None, date | None],
    venue: str,
) -> dict[str, object] | str:
    """Everything else the class needs: currencies, contract size and contract dates.

    A refusal comes back as the sentence saying why, so one bad row fails one key rather
    than the universe. One key of the answer is not a constructor argument at all:
    `engine_asset_class` names the entry's asset class, which an option takes from its
    underlying, and the caller pops it out before building.
    """
    if rule.asset_class == conventions.FOREX:
        return {"base_currency": ticker.base, "quote_currency": ticker.quote}
    fields: dict[str, object] = {
        "currency": (_text(row, *CURRENCY_FIELDS) or rule.currency).upper()
    }
    if rule.instrument_class is None:
        return fields
    return _derivative(rule, row, ticker, life, venue, fields)


def _derivative(
    rule: conventions.Convention,
    row: Mapping[str, Any],
    ticker: conventions.Ticker,
    life: tuple[date | None, date | None],
    venue: str,
    fields: dict[str, object],
) -> dict[str, object] | str:
    """A dated contract: its size, its underlying, when it started and when it ends.

    Both dates are required by the engine and neither is invented. A contract whose row
    states no listing date does not resolve, because an activation guessed from an expiry
    would let a card trade a contract before it existed, and that error is invisible in a
    result. The operator's `override` is where a date the vendor omits belongs.
    """
    size = _number(row, *SIZE_FIELDS)
    multiplier = rule.multiplier if size is None else size
    if multiplier is None:
        return (
            f"{UNKNOWN}: the vendor's row states no contract size, and a "
            f"{rule.asset_class} contract cannot be priced without one"
        )
    underlying = _text(row, *UNDERLYING_FIELDS) or ticker.underlying
    listed, ends = life
    if underlying is None:
        return f"{UNKNOWN}: the vendor's row names no underlying for this contract"
    if listed is None or ends is None:
        missing = "listing date" if listed is None else "expiry"
        return (
            f"{UNKNOWN}: the vendor's row states no {missing}, and a dated contract with an "
            "unknown one cannot be resolved as of a date"
        )
    fields.update(
        {
            "multiplier": multiplier,
            "underlying": _bare(underlying),
            "activation_ns": listed,
            "expiration_ns": ends,
            "exchange": venue,
        }
    )
    if rule.asset_class == conventions.OPTIONS:
        terms = _option(row, ticker)
        if isinstance(terms, str):
            return terms
        fields.update(terms)
        fields["engine_asset_class"] = _underlying_class(rule, underlying)
    return {key: value for key, value in fields.items() if value is not None}


def _option(row: Mapping[str, Any], ticker: conventions.Ticker) -> dict[str, object] | str:
    """The strike and the kind, from the row where it states them and the key where not.

    The key states them exactly — an OSI symbol encodes a complete date, a side and a
    strike in thousandths — so falling back to it invents nothing. The futures key gets no
    such fallback: the vendor spells its year its own way, and no expiry day is in it.
    """
    kind = _text(row, *KIND_FIELDS) or ticker.option_kind
    if kind is None or kind.upper() not in ("CALL", "PUT"):
        return f"{UNKNOWN}: neither the key nor the row says whether it is a call or a put"
    strike = _number(row, *STRIKE_FIELDS)
    return {
        "option_kind": kind.upper(),
        "strike_price": ticker.strike if strike is None else strike,
    }


def _underlying_class(rule: conventions.Convention, underlying: str) -> str:
    """The engine asset class of an option: its underlying's, which its key names."""
    named = conventions.classify(underlying)
    if named == conventions.INDICES:
        return "INDEX"
    return rule.engine_asset_class


def _life(row: Mapping[str, Any], ticker: conventions.Ticker) -> tuple[date | None, date | None]:
    """When the instrument started and stopped existing, as far as the source says.

    A derivative stops existing at expiry, so an expiry is a delisting for the purpose of
    the dated question, and the key's own expiry answers where the row omits one.
    """
    listed = _date(row, *LISTED_FIELDS)
    ends = _date(row, *DELISTED_FIELDS)
    if ends is None:
        ends = _date(row, *EXPIRY_FIELDS) or ticker.expiry
    return listed, ends


def _bare(underlying: str) -> str:
    """An underlying without its market prefix, which the instrument id does not carry."""
    if conventions.classify(underlying) is None:
        return underlying
    return underlying[conventions.PREFIX_LENGTH :]


# --- reading a row ------------------------------------------------------------


def _text(row: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _number(row: Mapping[str, Any], *names: str) -> Decimal | None:
    for name in names:
        value = row.get(name)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return Decimal(str(value))
        except InvalidOperation:
            continue
    return None


def _whole(value: Decimal | None) -> Decimal | None:
    """A count the source states as a float, `50.0`, as the whole number it is, `50`."""
    if value is None or value != value.to_integral_value():
        return value
    return value.quantize(Decimal(1))


def _date(row: Mapping[str, Any], *names: str) -> date | None:
    """A row's date field as a calendar day, whether it is a date or a full timestamp."""
    text = _text(row, *names)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


# --- universes and chains -----------------------------------------------------


def universe(
    client: MassiveClient, asset_class: str, *, active: bool = True, page: int = PAGE
) -> tuple[str, ...]:
    """Every key the vendor lists in this class, the whole cursor walked.

    No feed allowlist is applied and none exists: index entitlement is decided by the feed
    behind each key, feeds are entitled in part, and filtering on a feed name drops keys
    the plan does serve. Entitlement is `entitled`'s question, one key at a time.
    """
    market = conventions.MARKET_OF[conventions.convention(asset_class).asset_class]
    params = {"market": market, "active": "true" if active else "false", "limit": str(page)}
    return _tickers(client.rows(LISTING, params))


def contracts(
    client: MassiveClient,
    underlying: str,
    *,
    expiring: tuple[date, date] | None = None,
    page: int = PAGE,
) -> tuple[str, ...]:
    """Every option contract on `underlying`, the whole cursor walked.

    Reference has no history window, so a chain whose contracts expired before the
    aggregate floor still lists; whether their *prices* can be read is a separate question
    with a separate answer.
    """
    params = {"underlying_ticker": underlying, "limit": str(page)}
    if expiring is not None:
        params["expiration_date.gte"] = expiring[0].isoformat()
        params["expiration_date.lte"] = expiring[1].isoformat()
    return _tickers(client.rows(CONTRACTS, params))


def front_month(client: MassiveClient, product: str, on: date) -> str | None:
    """The single contract of `product` trading on `on` that expires first, or `None`.

    One page of the product's point-in-time listing, the contracts filtered to singles and
    the soonest last trade date chosen here: measured on 2026-10-29, the listing refuses to be
    sorted by `last_trade_date` (HTTP 400, it sorts by `date`, `product_code` or `ticker`
    alone), and answered ES as of 2026-10-28 with 22 singles from ESH0 — 2030's March — to
    ESZ9, with ESZ6, last traded 2026-12-18, the soonest. A listing that answers nothing names
    no contract, which leaves the caller with no key rather than a wrong one.
    """
    day = on.isoformat()
    call = client.call(
        FUTURES_CONTRACTS,
        {"product_code": product, "date": day, "type": "single", "limit": str(PAGE)},
    )
    if call.signal is not Signal.ROWS:
        return None
    trading = sorted(
        (last, ticker)
        for row in call.rows
        if row.get("active") is True
        and isinstance(ticker := row.get("ticker"), str)
        and isinstance(last := row.get("last_trade_date"), str)
        and last >= day
    )
    return trading[0][1] if trading else None


def entitled(
    client: MassiveClient,
    tickers: Sequence[str],
    asset_class: str,
    *,
    dataset: Endpoint = BARS,
    as_of: date | None = None,
) -> tuple[str, ...]:
    """The keys of `tickers` the plan actually serves, established by probing each one.

    Every key is probed. The memo behind this caches an answer only at the grain the source
    gates on, so a class the source gates as a whole costs one probe and a class it gates
    per key costs one each — which for indices is the point: one index answering says
    nothing about the next.
    """
    memo = Entitlements(client, as_of=as_of)
    return tuple(
        ticker
        for ticker in dict.fromkeys(tickers)
        if memo.check(ticker, asset_class, dataset=dataset).ok
    )


def provider(ws: Workspace, *, transport: Transport | None = None) -> MassiveReference:
    """This workspace's reference provider, for the instrument registry to be given.

    Resolving the credential is deferred to here rather than to import, so a workspace
    with no vendor variable set imports the adapter and never opens a client.
    """
    from kanso.data.adapters.massive import ADAPTER

    return MassiveReference(ADAPTER.client(ws, transport=transport))


def _tickers(rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """The `ticker` of every row, once each, in the order the walk produced them."""
    found = [ticker for row in rows if isinstance(ticker := row.get("ticker"), str) and ticker]
    return tuple(dict.fromkeys(found))
