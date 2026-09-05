"""How Massive spells an instrument, and what each class needs before it can be built.

Two kinds of fact live here, and they are kept apart because they age differently.

**Formats** are the vendor's own spelling. A prefix says which market a key belongs to
(`O:` options, `C:` a currency pair, `I:` an index, `X:` crypto, which this adapter does
not serve); a bare key carries no prefix at all, so a stock and a futures contract look
alike and the market is taken from the reference row rather than guessed from the string.
Two of the formats encode the contract itself. An option key is an OSI symbol — root,
`YYMMDD`, `C` or `P`, then the strike in thousandths — and is parsed from the right, so a
root of any length works and the expiry, the kind and the strike come out exactly. A
futures key is a product code, a month letter and a year, and the year is where the
trouble is: a single digit names one year in every decade, so it is resolved against the
date the question is asked on, and a resolution that disagrees with the source's own
expiry is a refusal rather than a guess.

**Conventions** are what the engine needs and the vendor does not publish: the minimum
price increment, the minimum lot and the contract size. Three rules govern them.

* Where kanso's own dated table covers the pair, that table answers. US equities do, and
  their increment is a regulator's rule, so nothing here restates it.
* Where a number is part of a **contract specification** — the size of a futures contract,
  its tick — the reference row relays the exchange's own figure and it is read from the
  row. This is not a vendor reporting its rounding; it is the exchange's rulebook arriving
  through a vendor, and inventing it instead would misprice a contract by its multiplier.
* Where neither holds, a schedule is stated here, in the same dated shape kanso's core
  table uses, with the authority written out — so a reader can see who set the number, and
  so the schedule can be lifted into the core table unchanged if it is ever wanted there.

Nothing in this module reads the vendor's error prose, asks a network or holds a
credential. It is the vendor's grammar and the market's arithmetic, and that is all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from kanso.data import conventions as market
from kanso.data.adapters.massive.errors import MalformedRequestError
from kanso.errors import ValidationError

__all__ = [
    "CLASSES",
    "CONVENTIONS",
    "FOREX",
    "FUTURES",
    "INDICES",
    "MARKETS",
    "MARKET_OF",
    "MONTHS",
    "OPTIONS",
    "PREFIXES",
    "STOCKS",
    "VENDOR",
    "Convention",
    "Ticker",
    "classify",
    "contract_month",
    "contract_year",
    "convention",
    "increments",
    "market_class",
    "pair_increment",
    "parse",
]

VENDOR: Final = "massive"
"""The adapter's id, as an instrument's `sources` records it. It equals the package's own
`ID`; the suite asserts that, because two spellings of one identity is a bug waiting."""

STOCKS: Final = "stocks"
OPTIONS: Final = "options"
FUTURES: Final = "futures"
FOREX: Final = "forex"
INDICES: Final = "indices"
CRYPTO: Final = "crypto"

CLASSES: Final[tuple[str, ...]] = (STOCKS, OPTIONS, FUTURES, FOREX, INDICES)
"""The classes this adapter resolves. Crypto is spelled here only so a crypto key is
refused by name rather than mistaken for a stock."""

PREFIXES: Final[dict[str, str]] = {
    "O:": OPTIONS,
    "C:": FOREX,
    "I:": INDICES,
    "X:": CRYPTO,
}
"""The prefix a key carries, by the market it names. A key without one is a bare key, and
a bare key is a stock or a futures contract — the string cannot tell which."""

PREFIX_LENGTH: Final = 2
"""Every prefix is a letter and a colon, which is what lets one be stripped by length."""

MARKETS: Final[dict[str, str]] = {
    "stocks": STOCKS,
    "otc": STOCKS,
    "options": OPTIONS,
    "futures": FUTURES,
    "fx": FOREX,
    "currencies": FOREX,
    "indices": INDICES,
    "crypto": CRYPTO,
}
"""The vendor's own `market` value, as one of this adapter's classes. It is what settles
the class of a bare key: the source knows which market it lists a key in, and no amount of
string inspection does."""

MARKET_OF: Final[dict[str, str]] = {
    STOCKS: "stocks",
    OPTIONS: "options",
    FUTURES: "futures",
    FOREX: "fx",
    INDICES: "indices",
}
"""The same table read backwards, for a listing request that filters by market."""

MONTHS: Final[dict[str, int]] = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}
"""The delivery-month letters, which every futures exchange shares: F is January and Z is
December, and I and O are omitted so neither can be read as a digit."""

CONTRACT: Final = re.compile(rf"([A-Z0-9]{{1,5}}?)([{''.join(MONTHS)}])([0-9]{{1,4}})")
"""A futures key: a product code, a delivery month, then one, two or four year digits. The
product is matched lazily so a code that itself ends in a month letter — `ZN`, `GC` — still
splits at the right place, the trailing digits being the anchor."""

DIGITS: Final = re.compile(r"[0-9]+")
LETTERS: Final = re.compile(r"[A-Za-z]+")
"""ASCII digits and letters. `str.isdigit` is not the test: it is true of digits from every
script, and a key written in them would pass a format check and fail the engine."""

YEAR_BACK: Final = 1
DECADE: Final = 10
"""How a single-digit year is resolved: over the ten years beginning one year before the
date the question is asked on. One year back, because a contract is still referred to for
a while after it expires; ten years, because a single digit distinguishes no more."""

OSI_STRIKE_DIGITS: Final = 8
OSI_DATE_DIGITS: Final = 6
STRIKE_SCALE: Final = Decimal(1000)
"""The OSI option symbol: `<root><YYMMDD><C|P><strike x 1000, 8 digits>`."""

CENTURY: Final = 2000
"""The century a two-digit year belongs to, for both formats that carry one. The OSI
symbology dates from 2010 and no series predates it, and a futures key has been written
with two digits only since the market stopped tolerating the ambiguity of one."""

PAIR_LENGTH: Final = 6
"""A currency pair is two ISO 4217 alphabetic codes, base then quote, and nothing else."""

FX_DECIMALS: Final = 5
QUOTE_DECIMALS: Final[dict[str, int]] = {"JPY": 3}
"""How many decimals a spot pair is quoted to. The interbank convention places the pip at
the fourth decimal and electronic venues quote one further decimal — the fractional pip —
so five, except for a yen-quoted pair, whose pip is the second decimal and which therefore
quotes to three. A pair quoted otherwise is corrected by the operator's `override`."""

REFERENCE_PRICE: Final = 1.0
"""The price a banded schedule is asked at when the caller names none: one unit of the
quote currency, the same standard band kanso's core resolution uses."""

LOT: Final = Decimal(1)
"""The minimum tradable lot wherever a lot is not itself a contract term. A round lot is a
display and routing convention rather than a minimum size, so the minimum is one unit."""

OPTION_SCHEDULE: Final[tuple[market.Schedule, ...]] = (
    market.Schedule(
        effective=date(2007, 1, 26),
        authority=(
            "the SEC-approved Penny Pilot Program, made permanent as the Penny Interval "
            "Program: a participating option series is quoted in $0.01 increments up to a "
            "premium of $3.00 and in $0.05 increments above it"
        ),
        bands=(
            market.Band(Decimal("0"), Decimal("0.01")),
            market.Band(Decimal("3.00"), Decimal("0.05")),
        ),
        lot=LOT,
    ),
)
"""US listed options. A class outside the programme quotes in $0.05 and $0.10 instead, and
an operator holding one says so in that instrument's `override`."""

INDEX_SCHEDULE: Final[tuple[market.Schedule, ...]] = (
    market.Schedule(
        effective=date(1970, 1, 1),
        authority=(
            "no rule sets the increment of a published index level, which is disseminated "
            "rather than quoted; two decimals is what index publishers disseminate at, and "
            "it is stated so an index resolves at all"
        ),
        bands=(market.Band(Decimal("0"), Decimal("0.01")),),
        lot=LOT,
    ),
)
"""An index is computed, not traded, so its increment is a publisher's precision. It is the
weakest fact in this module and it says so; an override is the correction."""


@dataclass(frozen=True, slots=True)
class Ticker:
    """One vendor key, split into the parts a definition is built from.

    `text` is the key as the vendor spells it, prefix and all, and is what a request asks
    for and what an instrument's `sources` records. `key` is the same key without the
    prefix, which is the symbol a kanso instrument id carries — the prefix names a market
    and a kanso id names a venue, so carrying it into the id would say the same thing twice
    in the vendor's words.

    `asset_class` is `None` for a bare key, which the reference row settles.
    """

    text: str
    key: str
    asset_class: str | None
    underlying: str | None = None
    expiry: date | None = None
    option_kind: str | None = None
    strike: Decimal | None = None
    base: str | None = None
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class Convention:
    """What one asset class needs beyond what its reference row states.

    `venue` is the venue used when the row names none, and is `None` for a class where
    there is no such default — a futures contract trades on a designated market and
    inventing one would put a false venue in an instrument id. `multiplier` is the contract
    size where the class has a standard one, and `schedule` the increments where kanso's
    own dated table has none.

    `venue_from_row` says whether the row's own exchange is read at all. It is read for the
    two classes that have a listing venue — a stock and a futures contract — and ignored
    for the three that do not, where the constant is the whole truth: an option consolidated
    on one tape, a currency traded on none, an index traded nowhere. Reading a field for
    those would let the vendor's spelling of a non-fact into an instrument id, and an id is
    what every card, order and manifest is keyed by.
    """

    asset_class: str
    engine_class: str
    engine_asset_class: str
    instrument_class: str | None
    venue: str | None
    currency: str
    venue_from_row: bool = False
    multiplier: Decimal | None = None
    schedule: tuple[market.Schedule, ...] = ()


CONVENTIONS: Final[dict[str, Convention]] = {
    STOCKS: Convention(
        asset_class=STOCKS,
        engine_class="Equity",
        engine_asset_class="EQUITY",
        instrument_class=None,
        venue="OTC",
        currency="USD",
        venue_from_row=True,
    ),
    OPTIONS: Convention(
        asset_class=OPTIONS,
        engine_class="OptionContract",
        engine_asset_class="EQUITY",
        instrument_class="option",
        venue="OPRA",
        currency="USD",
        multiplier=Decimal(100),
        schedule=OPTION_SCHEDULE,
    ),
    FUTURES: Convention(
        asset_class=FUTURES,
        engine_class="FuturesContract",
        engine_asset_class="COMMODITY",
        instrument_class="future",
        venue=None,
        currency="USD",
        venue_from_row=True,
    ),
    FOREX: Convention(
        asset_class=FOREX,
        engine_class="CurrencyPair",
        engine_asset_class="FX",
        instrument_class=None,
        venue="FX",
        currency="USD",
    ),
    INDICES: Convention(
        asset_class=INDICES,
        engine_class="IndexInstrument",
        engine_asset_class="INDEX",
        instrument_class=None,
        venue="IDX",
        currency="USD",
        schedule=INDEX_SCHEDULE,
    ),
}
"""One entry per class this adapter resolves.

The defaults are chosen to be true where they are used rather than convenient. A US equity
whose row names no primary listing exchange is quoted over the counter, so `OTC` is what
the absence means. US listed options are consolidated on OPRA. Spot currency has no venue
at all and `FX` records that rather than pretending to a market. An index is published
rather than traded, and `IDX` says so. A futures contract has no such default because it
always has a real designated market, which the row states.

A futures contract's asset class is recorded as `COMMODITY`, the engine's general class for
an exchange-traded contract, because the ticker does not say whether the underlying is a
commodity, a rate or an index; an operator trading index futures corrects it in `override`.
"""


def convention(asset_class: str) -> Convention:
    """The conventions for `asset_class`, or a refusal naming the classes there are."""
    found = CONVENTIONS.get(asset_class)
    if found is None:
        raise MalformedRequestError(
            f"massive: {asset_class!r} is not a class this adapter resolves; it resolves "
            f"{', '.join(CLASSES)}",
            remedy="ask for one of those classes, or drop the instrument from the universe",
        )
    return found


def classify(ticker: str) -> str | None:
    """The class a key's prefix names, or `None` when it carries none.

    `None` is not a failure: a bare key is a stock or a futures contract and only the
    source knows which, so the answer waits for the reference row.
    """
    for prefix, asset_class in PREFIXES.items():
        if ticker.startswith(prefix):
            return asset_class
    return None


def market_class(named: object) -> str | None:
    """The class the vendor's own `market` field names, or `None` when it names none."""
    if not isinstance(named, str):
        return None
    return MARKETS.get(named.strip().lower())


def parse(ticker: str) -> Ticker:
    """A vendor key, split into its parts, or a refusal saying which format it broke.

    A malformed key is a malformed *request* rather than an unknown instrument: every
    later request built from it would fail identically, which is exactly what the outcome
    means. A caller resolving a universe turns it back into one id's failure so the rest of
    the universe still resolves.
    """
    text = ticker.strip()
    if not text:
        raise MalformedRequestError(
            "massive: an empty string is not a ticker",
            remedy=(
                "name the vendor's own key, such as AAPL, I:NDX, C:EURUSD or O:AAPL251219C00150000"
            ),
        )
    named = classify(text)
    if named is not None:
        return _prefixed(text, text[PREFIX_LENGTH:], named)
    if ":" in text:
        prefix = text.partition(":")[0]
        raise MalformedRequestError(
            f"massive: {text!r} carries the prefix {prefix + ':'!r}, which names no market "
            f"this adapter serves; the prefixes it serves are "
            f"{', '.join(name for name, cls in PREFIXES.items() if cls in CLASSES)}",
            remedy="check the prefix, or drop the instrument from the universe",
        )
    return Ticker(text=text, key=text, asset_class=None)


def contract_year(digits: str, as_of: date) -> int:
    """The calendar year a futures key's year digits name, resolved against `as_of`.

    Four digits are a year. Two are a year of this century, which the market has spelled
    that way since it stopped being ambiguous. One is ambiguous by construction — it names
    a year in every decade — and is resolved to the one in the ten years beginning a year
    before `as_of`, which is the range a listed contract falls in and the reason resolution
    is dated at all.
    """
    if len(digits) == 4:
        return int(digits)
    if len(digits) == 2:
        return CENTURY + int(digits)
    if len(digits) != 1:
        raise MalformedRequestError(
            f"massive: {digits!r} is not a contract year; a year is written with one, two "
            "or four digits",
            remedy="name the contract with a two-digit year, such as ESZ25",
        )
    first = as_of.year - YEAR_BACK
    return first + (int(digits) - first) % DECADE


def contract_month(symbol: str, as_of: date) -> tuple[str, int, int] | None:
    """A futures key as product, year and delivery month, or `None` when it is not one.

    `None` rather than a refusal, because a bare key that is not a contract code is an
    ordinary ticker until the source says otherwise, and the source is asked either way.
    """
    found = CONTRACT.fullmatch(symbol.upper())
    if found is None:
        return None
    product, month, digits = found.groups()
    if len(digits) == 3:
        return None
    return product, contract_year(digits, as_of), MONTHS[month]


def increments(
    asset_class: str,
    venue: str,
    as_of: date,
    *,
    price: float = REFERENCE_PRICE,
    quote: str | None = None,
) -> tuple[Decimal, Decimal] | None:
    """The minimum price increment and lot for this class, or `None` when only the source
    knows them.

    kanso's own dated table answers for stocks, whose increment is a regulator's rule and
    is therefore stated once for the whole package rather than per vendor. A currency pair
    is quoted by convention in the quote currency's own decimals. Options and indices take
    the schedules above. A futures contract takes neither: its tick is a contract term, so
    it is read from the reference row and a guess would be a mispriced fill.
    """
    if asset_class == STOCKS:
        return _core(venue, price, as_of)
    if asset_class == FOREX:
        return None if quote is None else (pair_increment(quote), LOT)
    found = _in_force(convention(asset_class).schedule, as_of)
    return None if found is None else (_tick(found, price), found.lot)


def pair_increment(quote: str) -> Decimal:
    """The increment a spot pair quoted in `quote` trades in."""
    decimals = QUOTE_DECIMALS.get(quote.upper(), FX_DECIMALS)
    return Decimal(1).scaleb(-decimals)


def _core(venue: str, price: float, as_of: date) -> tuple[Decimal, Decimal] | None:
    """kanso's own table, asked for the one class it covers; `None` when it declines."""
    try:
        return (
            market.tick_size("EQUITY", venue, price, as_of),
            market.lot_size("EQUITY", venue, as_of),
        )
    except ValidationError:  # the table refuses by raising; a refusal here is an absence
        return None


def _in_force(schedules: tuple[market.Schedule, ...], as_of: date) -> market.Schedule | None:
    """The latest schedule that had taken effect by `as_of`, or `None` if none had."""
    reached = [item for item in schedules if item.effective <= as_of]
    if not reached:
        return None
    return max(reached, key=lambda item: item.effective)


def _tick(found: market.Schedule, price: float) -> Decimal:
    """The increment of the highest band at or below `price`."""
    level = Decimal(str(price))
    tick = found.bands[0].tick
    for band in found.bands:
        if level < band.at_or_above:
            break
        tick = band.tick
    return tick


def _prefixed(text: str, body: str, asset_class: str) -> Ticker:
    if asset_class not in CLASSES:
        raise MalformedRequestError(
            f"massive: {text!r} is a {asset_class} key and this adapter resolves "
            f"{', '.join(CLASSES)}",
            remedy="drop the instrument from the universe",
        )
    if not body:
        raise MalformedRequestError(
            f"massive: {text!r} is a prefix with no key after it",
            remedy="name the key the prefix belongs to",
        )
    if asset_class == OPTIONS:
        return _option(text, body)
    if asset_class == FOREX:
        return _pair(text, body)
    return Ticker(text=text, key=body, asset_class=asset_class)


def _option(text: str, body: str) -> Ticker:
    """An OSI symbol, read from the right so a root of any length parses."""
    tail = OSI_DATE_DIGITS + 1 + OSI_STRIKE_DIGITS
    root, day, kind, strike = body[:-tail], body[-tail:-9], body[-9:-8], body[-8:]
    shaped = root and DIGITS.fullmatch(day) and kind in ("C", "P") and DIGITS.fullmatch(strike)
    if not shaped:
        raise MalformedRequestError(
            f"massive: {text!r} is not an option key; an option key is a root, then YYMMDD, "
            "then C or P, then the strike in thousandths over eight digits",
            remedy="check the key, such as O:AAPL251219C00150000 for the $150 call",
        )
    try:
        expiry = date(CENTURY + int(day[:2]), int(day[2:4]), int(day[4:]))
    except ValueError:
        raise MalformedRequestError(
            f"massive: {text!r} names no calendar date: {day!r} is not YYMMDD",
            remedy="check the expiry in the key",
        ) from None
    return Ticker(
        text=text,
        key=body,
        asset_class=OPTIONS,
        underlying=root,
        expiry=expiry,
        option_kind="CALL" if kind == "C" else "PUT",
        strike=Decimal(strike) / STRIKE_SCALE,
    )


def _pair(text: str, body: str) -> Ticker:
    if len(body) != PAIR_LENGTH or not LETTERS.fullmatch(body):
        raise MalformedRequestError(
            f"massive: {text!r} is not a currency pair; a pair is two three-letter currency "
            "codes, base then quote",
            remedy="check the key, such as C:EURUSD",
        )
    upper = body.upper()
    return Ticker(
        text=text,
        key=f"{upper[:3]}/{upper[3:]}",
        asset_class=FOREX,
        base=upper[:3],
        quote=upper[3:],
    )
