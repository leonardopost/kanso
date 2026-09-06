"""What the broker will actually let a strategy do with an instrument.

An instrument definition says what a thing *is* — its venue, its currency, its tick and
its lot. It says nothing about whether this account may short it today. The broker does,
one row per symbol, and this module is that row carried into the engine's view of the
instrument: tradable, marginable, shortable, easy to borrow, fractionable, and the venue
and status the broker itself reports for it.

**Beside the definition, never inside it.** The engine's instrument classes will carry an
arbitrary `info` mapping, and it is tempting to put these flags there. They do not belong
there. A definition in kanso is a *dated fact* that is content-addressed and cached by
that address, so a borrow flag inside one would re-key the instrument every time the
broker's overnight list changed and turn a stable cache into a daily churn of definitions
that differ in nothing an order can price. A definition is what the instrument was; this
overlay is what the broker will permit right now. They have different lifetimes, so they
are kept apart, and a caller consults both.

**Two absences, and only one of them means anything.** A flag missing from a row the
broker sent is not a flag with a default. Read a missing `shortable` as true and a card
shorts what it could never have borrowed; read it as false and the same card quietly
stops trading a name the broker was happy to lend. Neither is a reading of the row, so
the row is refused whole: the instrument is recorded as undescribed with the field named,
and every permission that flag would have granted is withheld until the broker is asked
again. The other absence is the opposite: `min_order_size`, `min_trade_increment` and
`price_increment` were measured *absent* from a live equity row, because they are crypto
fields. They are never consulted here — the asset class is checked first and the only
class kanso trades through this broker states none of them — so their absence can refuse
nothing, and an overlay that required them would refuse every equity there is.

**Nothing is fetched implicitly.** `load` asks the broker; every other method reads what
is held. A lookup that reached the network from inside an order hook would block the
trading loop on a socket at the worst possible moment, and one that silently fetched on a
cache miss would hide how many requests a session actually makes from the one quota this
adapter is allowed.

**What is held has an age.** The broker publishes no refresh cadence for these flags and
none was measured, so the bound is kanso's own rather than a claim about the broker: a
flag older than `max_age_s` is no longer evidence, and an order sized against it is
refused with the age named instead of being permitted on a stale borrow list. The default
is an hour — short enough that a flag consulted when an order is sized was read in the
same session, long enough that a universe is not re-read per order.

**Shortable and easy to borrow are two different sentences.** A short of a name the
broker does not mark shortable is refused here, before anything is sent. A short of one
that is shortable but hard to borrow is permitted and carries a caveat, because the
broker does accept those and it is the locate rather than the permission that may fail —
and a refusal invented here would be exactly the kind of guess this module exists to
avoid. `marginable` is reported and enforced nowhere: it describes how a position is
financed, not whether an order is accepted, and leverage in kanso comes from the resolved
venue model and the hypothesis's risk limits.

**The venue must agree.** The broker names the listing venue of every symbol it describes.
An order for `AAPL` on one venue cannot be vouched for by a row the broker filed under
another, so a disagreement is a refusal naming both rather than a match on the symbol.

Credentials are resolved at the moment of use and reach the request headers and nothing
else. No value, no header and no response body reaches a message, a reason, a `repr` or a
payload from anywhere in this module: a refusal carries the HTTP status, the symbol and
the field, which is what an operator needs and all of it.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`InstrumentId` is a `Symbol` and a `Venue`, and `Symbol` is the broker's own ticker, so
the symbol a lookup is keyed by is `instrument_id.symbol.value` and nothing derived.
`OrderSide` is the engine's side enum, of which only `BUY` and `SELL` reach a broker.
`Instrument.info` accepts an arbitrary mapping at construction, which is the door this
module deliberately does not use, for the reason given above.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from kanso.errors import Exit, KansoError, PreconditionError, ValidationError
from kanso.nautilus.adapters.alpaca import BROKER
from kanso.nautilus.adapters.alpaca.config import (
    CLIENTS,
    PAPER_CLIENT,
    Response,
    Transport,
    account,
    credential_names,
    endpoint,
    resolve,
)
from kanso.nautilus.adapters.alpaca.parsing import (
    NS_PER_SECOND,
    Asset,
    asset,
    instrument_id_of,
    symbol_of,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = [
    "ACTIVE",
    "ASSETS_PATH",
    "DEFAULT_MAX_AGE_S",
    "SYMBOL",
    "Overlay",
    "Tradability",
    "Undescribed",
    "overlay",
]

ASSETS_PATH: Final = "/v2/assets"
"""Where one instrument's tradability is read, one symbol at a time.

The measured endpoint. The broker also serves the whole list in one request, which was
not measured, so it is not used: a universe here is tens of names and the request path
that was seen answering is the one worth trusting."""

ACTIVE: Final = "active"
"""The one status measured on a live row, and the only one an order is accepted under.
Any other status is reported by name and refuses the order rather than being ranked."""

DEFAULT_MAX_AGE_S: Final = 3600.0
"""How long a flag stays evidence, in seconds. kanso's own bound, not the broker's: it
publishes no refresh cadence and none was measured, so this is the interval within which
a borrow flag consulted at order time was read in the same session."""

SYMBOL: Final = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,15}")
"""What may become a path segment. A symbol is part of the URL of the lookup, so it is
matched against this rather than interpolated: a string carrying a slash or a parent
segment would address a different endpoint entirely, and `/v2/assets/../orders` is a very
different request from the one this module means to make."""

ZERO: Final = Decimal(0)


@dataclass(frozen=True, slots=True)
class Undescribed:
    """A symbol the broker said nothing usable about, and why in words.

    One instrument's failure rather than the universe's: a name the broker does not list,
    or files in a shape this adapter will not read, leaves every other name resolved. It
    grants nothing — an undescribed instrument is refused every permission, because the
    absence of an answer is not an answer.
    """

    symbol: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        """The report payload, carrying no credential and no response body."""
        return {"symbol": self.symbol, "described": False, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Tradability:
    """One instrument as the broker describes it, and what that permits.

    Holds the parsed row rather than a copy of its flags, so what a caller reads is what
    the broker said, and stamps the instant it was read, so a flag can be too old to act
    on rather than merely present.
    """

    asset: Asset
    instrument_id: InstrumentId | None
    fetched_ns: int

    @property
    def symbol(self) -> str:
        """The broker's own ticker, which is what a lookup is keyed by."""
        return self.asset.symbol

    @property
    def venue(self) -> str | None:
        """The market identifier code of the venue the broker lists it on, or `None`.

        `None` when the broker's exchange spelling is one this adapter does not map,
        which is not a venue to invent: an instrument id built on a guessed venue would
        re-key every card, order and manifest the instrument appears in.
        """
        return None if self.instrument_id is None else str(self.instrument_id.venue)

    @property
    def caveats(self) -> tuple[str, ...]:
        """What the broker said that permits an order but changes what it costs.

        Not refusals. A hard-to-borrow name is one the broker will still short, with a
        locate that may fail at submission; a name that is not marginable is one financed
        in cash whatever leverage the venue model carries. Both are worth a line in a
        report and neither is grounds for this module to refuse an order the broker
        accepts.
        """
        found: list[str] = []
        if self.asset.shortable and not self.asset.easy_to_borrow:
            found.append(f"{self.symbol} is shortable but not easy to borrow, so a locate may fail")
        if not self.asset.marginable:
            found.append(
                f"{self.symbol} is not marginable, so a position in it is financed in cash"
            )
        return tuple(found)

    def age_s(self, now_ns: int) -> float:
        """How long ago this row was read, in seconds, never negative."""
        return max(now_ns - self.fetched_ns, 0) / NS_PER_SECOND

    def stale(self, now_ns: int, max_age_s: float) -> bool:
        """Whether this row is too old to size an order against."""
        return self.age_s(now_ns) > max_age_s

    def forbids(
        self,
        *,
        side: OrderSide,
        quantity: Decimal,
        position_qty: Decimal = ZERO,
    ) -> str | None:
        """Why the broker would not accept this order, or `None` when it would.

        `position_qty` is the position the order acts on, positive long and negative
        short, and it is what decides whether a sale opens a short at all: selling into a
        long position is not a short sale and needs no borrow. Its default is flat, which
        is the conservative reading — with no position stated a sale is treated as
        opening a short, so the borrow flag is consulted rather than passed over.
        """
        if quantity <= ZERO:
            return f"a quantity of {quantity} is not an order"
        if not self.asset.equity:
            return (
                f"the broker files {self.symbol} as {self.asset.asset_class!r}, which is not "
                "an asset class kanso trades here"
            )
        if self.asset.status != ACTIVE:
            return (
                f"the broker reports {self.symbol} as {self.asset.status!r} rather than {ACTIVE!r}"
            )
        if not self.asset.tradable:
            return f"the broker does not accept orders for {self.symbol}"
        if self._opens_short(side, quantity, position_qty) and not self.asset.shortable:
            return f"the broker does not permit a short sale of {self.symbol}"
        if quantity != quantity.to_integral_value() and not self.asset.fractionable:
            return f"the broker does not accept a fractional quantity of {self.symbol}"
        return None

    def as_dict(self) -> dict[str, object]:
        """The whole row as plain scalars, for a report, a card or `doctor`.

        The increments are reported as the broker left them — `None` for an equity, which
        is the measured shape — so a reader can see that the broker stated none rather
        than that kanso dropped them.
        """
        return {
            "symbol": self.symbol,
            "described": True,
            "instrument_id": None if self.instrument_id is None else str(self.instrument_id),
            "venue": self.venue,
            "exchange": self.asset.exchange,
            "asset_class": self.asset.asset_class,
            "status": self.asset.status,
            "tradable": self.asset.tradable,
            "marginable": self.asset.marginable,
            "shortable": self.asset.shortable,
            "easy_to_borrow": self.asset.easy_to_borrow,
            "fractionable": self.asset.fractionable,
            "min_order_size": _text(self.asset.min_order_size),
            "min_trade_increment": _text(self.asset.min_trade_increment),
            "price_increment": _text(self.asset.price_increment),
            "caveats": list(self.caveats),
            "fetched_ns": self.fetched_ns,
        }

    @staticmethod
    def _opens_short(side: OrderSide, quantity: Decimal, position_qty: Decimal) -> bool:
        """Whether this order ends with a short position that did not exist before."""
        return side == OrderSide.SELL and quantity > max(position_qty, ZERO)


class Overlay:
    """The broker's view of a universe, held for one workspace and one account.

    Opened for the client id whose credentials it reads, because the trading host and the
    key that may address it are the same decision, and reference data read through the
    paper account is read through a key that cannot move money.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        client_id: str | None = None,
        transport: Transport | None = None,
        factory: Any = None,
        clock: Callable[[], int] = time.time_ns,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.workspace = workspace
        named = _default_client(workspace) if client_id is None else client_id
        self.client_id = account(named).client_id
        self.max_age_s = max_age_s
        self._transport = transport
        self._factory = factory
        self._clock = clock
        self._held: dict[str, Tradability | Undescribed] = {}

    def __repr__(self) -> str:
        """The account and how much is held; never a credential, because a repr is a log."""
        return f"Overlay(client_id={self.client_id!r}, held={len(self._held)})"

    @property
    def host(self) -> str:
        """The trading host this client's key may address, and no other."""
        return BROKER.config(self.workspace).host(self.client_id)

    def transport(self) -> Transport:
        """The one rate-limited connection, built on first use unless one was handed in.

        A caller that already holds this account's connection passes it, because the
        quota belongs to the account: an execution client, a data client and an overlay
        with a connection each would be three times the published limit.
        """
        if self._transport is None:
            self._transport = BROKER.transport(self.workspace, factory=self._factory)
        return self._transport

    def load(self, symbols: Iterable[str | InstrumentId]) -> dict[str, Tradability | Undescribed]:
        """Ask the broker about each symbol, and hold what it says.

        Always asks: this is the refresh, and a caller that wants what is already held
        reads `of`. The credentials are resolved once for the whole call, at the moment
        of use, and reach the headers of each request and nothing else.
        """
        wanted = tuple(dict.fromkeys(_symbol(value) for value in symbols))
        if not wanted:
            return {}
        credentials = resolve(self.workspace.root, self.client_id)
        headers = {**credentials.headers(), "Accept": "application/json"}
        host = self.host
        found: dict[str, Tradability | Undescribed] = {}
        for symbol in wanted:
            found[symbol] = self._read(host, headers, symbol)
            self._held[symbol] = found[symbol]
        return found

    def of(self, instrument: str | InstrumentId) -> Tradability | Undescribed | None:
        """What is held for an instrument, or `None` when it was never asked about."""
        return self._held.get(_symbol(instrument))

    def forbids(
        self,
        instrument: str | InstrumentId,
        *,
        side: OrderSide,
        quantity: Decimal,
        position_qty: Decimal = ZERO,
    ) -> str | None:
        """Why this order must not be sent, or `None` when the broker would accept it.

        Every refusal names what is missing rather than asserting the opposite of it. An
        instrument nothing was loaded for is refused because nothing is known about it,
        which is a different sentence from the broker having forbidden it, and the
        message says so.
        """
        symbol = _symbol(instrument)
        held = self._held.get(symbol)
        if held is None:
            return (
                f"nothing has been read from the broker about {symbol}, and this adapter "
                "does not assume what it would say"
            )
        if isinstance(held, Undescribed):
            return f"the broker did not describe {symbol}: {held.reason}"
        now = self._clock()
        if held.stale(now, self.max_age_s):
            return (
                f"what the broker says about {symbol} was read {held.age_s(now):.0f} seconds "
                f"ago, and a flag older than {self.max_age_s:.0f} seconds is not evidence"
            )
        if isinstance(instrument, InstrumentId) and held.instrument_id != instrument:
            return (
                f"the broker lists {symbol} on {held.venue or held.asset.exchange} and the "
                f"order is for {instrument.venue}"
            )
        return held.forbids(side=side, quantity=quantity, position_qty=position_qty)

    def describe(self) -> dict[str, object]:
        """Everything held, as one JSON object carrying no credential and no value."""
        return {
            "adapter": BROKER.id,
            "client": self.client_id,
            "max_age_s": self.max_age_s,
            "instruments": [self._held[symbol].as_dict() for symbol in sorted(self._held)],
        }

    def _read(
        self, host: str, headers: Mapping[str, str], symbol: str
    ) -> Tradability | Undescribed:
        """One symbol's row, or why there is not one.

        Only the 200 was measured. That a symbol the broker does not list answers `404`
        is its published behaviour and was not seen, so it is the one status treated as a
        statement about the instrument; every other refusal stops the call rather than
        being read as an absence. If the broker turns out to say "no such asset" some
        other way, this reports a failed lookup loudly instead of quietly recording that
        an instrument does not exist — which is the direction to be wrong in.
        """
        response = self._send(host, headers, symbol)
        if response.status == 404:
            return Undescribed(symbol, "the broker does not list it")
        self._raise_for_status(symbol, response.status)
        row = _document(response.body)
        if row is None:
            return Undescribed(symbol, "the broker answered with a body that is not one row")
        try:
            parsed = asset(row)
        except ValidationError as failure:
            return Undescribed(symbol, failure.message)
        if parsed.symbol.strip().upper() != symbol:
            return Undescribed(
                symbol, f"the broker answered about {parsed.symbol!r} rather than {symbol!r}"
            )
        return Tradability(
            asset=parsed,
            instrument_id=instrument_id_of(parsed.symbol, parsed.exchange),
            fetched_ns=self._clock(),
        )

    def _send(self, host: str, headers: Mapping[str, str], symbol: str) -> Response:
        """The request itself; a fault below the answer is one outcome, named as such."""
        url = endpoint(host, f"{ASSETS_PATH}/{symbol}")
        try:
            return self.transport()("GET", url, {}, headers, [])
        except KansoError:
            raise
        except Exception as failure:  # every fault below the answer is the same outcome
            raise KansoError(
                f"alpaca: {host} could not be reached for {symbol} ({type(failure).__name__})",
                Exit.ERROR,
                remedy="check the network and the broker's status page, then re-run",
            ) from failure

    def _raise_for_status(self, symbol: str, status: int) -> None:
        """A refusal that is about the account rather than about the symbol stops the call.

        A rejected credential says nothing about the instrument that happened to be asked
        for, and marking that instrument undescribed would hide a broken key behind a
        universe of names the broker never refused. So it is raised, naming the variables
        and never their values.
        """
        if status in (401, 403):
            key_name, secret_name = credential_names(self.client_id)
            found = account(self.client_id)
            raise PreconditionError(
                f"alpaca: the broker refused the tradability lookup with HTTP {status}, which "
                f"is about the credentials of {self.client_id!r} rather than about {symbol}",
                remedy=(
                    f"check that {key_name} and {secret_name} hold the "
                    f"{found.environment.value} account's own key and secret"
                ),
            )
        if status == 429:
            raise PreconditionError(
                f"alpaca: the broker rate-limited the tradability lookup for {symbol} "
                f"(HTTP {status})",
                remedy=(
                    "lower `requests_per_minute` in the [adapters.alpaca] table, or share one "
                    "connection between the execution client, the data client and the overlay"
                ),
            )
        if not 200 <= status < 300:
            raise KansoError(
                f"alpaca: the tradability lookup for {symbol} answered HTTP {status}",
                Exit.ERROR,
                remedy="re-run once the broker's status page reports the API healthy",
            )


def overlay(
    ws: Workspace,
    *,
    client_id: str | None = None,
    transport: Transport | None = None,
    factory: Any = None,
    clock: Callable[[], int] = time.time_ns,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> Overlay:
    """The overlay for one workspace, opened without a credential and without a socket.

    The name the broker's registry entry reaches this module by. Nothing is resolved or
    connected here, so listing or describing a workspace's brokers costs nothing and is
    green with every variable unset.
    """
    return Overlay(
        ws,
        client_id=client_id,
        transport=transport,
        factory=factory,
        clock=clock,
        max_age_s=max_age_s,
    )


def _default_client(ws: Workspace) -> str:
    """Which account reads reference data when a caller names none.

    The first configured client, paper before live, because the two accounts see the same
    reference data and the paper one holds a key that cannot move money. When neither is
    configured it is the paper client, so the failure an operator meets names the
    variables of the account they should be setting first. A caller that knows which
    account it trades — an execution client on a stage — passes its own id and this is
    never consulted.
    """
    for client in CLIENTS:
        if BROKER.configured(ws, client):
            return client
    return PAPER_CLIENT


def _symbol(value: str | InstrumentId) -> str:
    """The broker's ticker for a lookup, refusing anything that is not one.

    Refused rather than escaped: a symbol becomes a path segment, and a string this does
    not recognise is a request to somewhere other than one instrument's asset row.
    """
    text = (symbol_of(value) if isinstance(value, InstrumentId) else str(value)).strip().upper()
    if SYMBOL.fullmatch(text) is None or ".." in text:
        raise ValidationError(
            f"symbol: {text!r} is not a symbol this broker names, and a lookup is never "
            "built out of an arbitrary string"
        )
    return text


def _document(body: bytes) -> Mapping[str, Any] | None:
    """The response body as one JSON object, or `None` when it is not one."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _text(value: Decimal | None) -> str | None:
    """A decimal the broker stated, as text, or `None` for one it did not state."""
    return None if value is None else format(value, "f")
