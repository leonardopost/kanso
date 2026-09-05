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
string cannot tell which, so the row's own `market` field decides. Nothing infers a market
from the shape of a bare symbol.

**The market also settles which endpoint holds the key.** Option contracts are keyed
outside the generic ticker reference, which rejects an option key with a client error, so
a chain looked up there fails every contract in it as an unrecognised shape — a definite,
confident and entirely wrong reason. The lookup endpoint is chosen the way entitlement is,
by asking the source the question it can answer.

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
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar, Final

from kanso.data.adapters.massive import conventions
from kanso.data.adapters.massive.client import MassiveClient, Signal
from kanso.data.adapters.massive.entitlement import (
    BARS,
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
        extra = _contract_fields(rule, row, ticker, life, as_of, venue)
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
    as_of: date,
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
    return _derivative(rule, row, ticker, life, as_of, venue, fields)


def _derivative(
    rule: conventions.Convention,
    row: Mapping[str, Any],
    ticker: conventions.Ticker,
    life: tuple[date | None, date | None],
    as_of: date,
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
    if rule.asset_class == conventions.FUTURES:
        product, refusal = _delivery(ticker, ends, as_of)
        if refusal is not None:
            return refusal
        underlying = underlying or product
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


def _delivery(
    ticker: conventions.Ticker, ends: date | None, as_of: date
) -> tuple[str | None, str | None]:
    """The product a futures key names, and the refusal a disagreeing year earns.

    A futures key's year may be a single digit, which names one year in every decade. The
    digit is resolved against the date the question is asked on, and the result is checked
    against the expiry the source states: where they disagree, the key does not identify
    one contract, and resolving it would mint an instrument id whose meaning changes with
    the day it was resolved. That is refused, with the unambiguous spelling as the remedy.

    A key that is no contract code at all yields neither: it is an ordinary symbol the
    source happens to list in its futures market, and the row's own underlying names it.
    """
    parsed = conventions.contract_month(ticker.key, as_of)
    if parsed is None:
        return None, None
    product, year, month = parsed
    if ends is not None and (ends.year, ends.month) != (year, month):
        return None, (
            f"{UNKNOWN}: the key's delivery month resolves to {year}-{month:02d} and the "
            f"source expires it {ends}; a one-digit year names one year in every decade, so "
            f"this key names no single contract — write it with two digits"
        )
    return product, None


def _option(row: Mapping[str, Any], ticker: conventions.Ticker) -> dict[str, object] | str:
    """The strike and the kind, from the row where it states them and the key where not.

    The key states them exactly — an OSI symbol encodes a complete date, a side and a
    strike in thousandths — so falling back to it invents nothing. The futures key gets no
    such fallback, because its year is genuinely ambiguous and its expiry day is not in it.
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
