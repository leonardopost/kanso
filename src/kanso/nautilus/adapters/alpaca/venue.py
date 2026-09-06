"""The venues this broker serves, and the trading model it declares for them.

A venue's model is inherited rather than invented: the broker behind a stage's execution
client — and, for research, the broker named in the configuration — declares the account
type, the account currency and the cost model of every venue it serves, an operator's
`venues.<MIC>` entry overrides any field, and a hypothesis's own `costs` overrides the
cost model for that hypothesis alone. This module is this broker's half of that.

**Which venues.** The broker trades US listed equities, and the venue of an instrument is
the venue it is listed on, never the broker: a broker is not a market, and a forked
instrument id would make the same share two different things in a backtest and in a
stage. So the venues served are exactly the listing venues the broker's own `exchange`
field names, and the table below is the mapping between its spelling of one and the
market identifier code kanso uses. Only `NASDAQ` was measured; the rest are the broker's
documented exchange values, and an exchange outside the table resolves to nothing at all
rather than to a guess, because a wrong venue in an instrument id silently re-keys every
card, order and manifest that instrument appears in.

**What is declared, and what is still a default.** The broker states two things about the
account that trades these venues — it is denominated in US dollars, and it is a margin
account, which is the account type kanso's own default assumes and the one that permits a
short sale at all — and one thing about the cost of a fill: its published US equity
commission is zero. Slippage and the spread are *not* declared, because the broker
publishes neither, and they continue to come from kanso's shipped defaults.

That leaves one honest caveat, recorded here because it cannot be recorded on a card: the
origin of a resolved cost model is kept per block rather than per field, so a card whose
costs came through this declaration reports the whole block as the broker's, including
the slippage the broker never stated. What the broker's zero commission also does not
cover is the regulatory pass-through on a sale — the exchange and industry fees the broker
forwards rather than charges. An operator who wants those modelled states them as
`venues.<MIC>.costs` or as the hypothesis's own `costs`, which is the same path used to
stress an idea against costs worse than the broker's.
"""

from __future__ import annotations

from typing import Final

from kanso.schemas import CostsOverride, VenueDeclaration

__all__ = [
    "ASSET_CLASS",
    "COMMISSION_BPS",
    "CURRENCY",
    "EXCHANGES",
    "US_EQUITY",
    "VENUES",
    "declaration",
    "serves",
    "venue_of",
]

ASSET_CLASS: Final = "us_equity"
"""The one asset class this adapter trades, as the broker spells it on every row it
returns. The broker also serves options and crypto; kanso 0.1.0 trades neither, so a row
of either class is refused by name rather than mistaken for an equity."""

CURRENCY: Final = "USD"
"""The account currency. A US equity account is denominated in dollars, and a universe
spanning two currencies is refused long before it reaches a broker."""

COMMISSION_BPS: Final = 0.0
"""The broker's published US equity commission. Regulatory pass-through fees on a sale are
not part of it and are the operator's to state where they matter."""

EXCHANGES: Final[dict[str, str]] = {
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "ARCA": "ARCX",
    "NYSEARCA": "ARCX",
    "AMEX": "XASE",
    "BATS": "BATS",
    "IEX": "IEXG",
    "OTC": "OTC",
}
"""The broker's spelling of a listing venue, as the market identifier code kanso uses.

`NASDAQ` was measured on a live asset row; the rest are the broker's documented exchange
values. Two of its spellings name the same market, which is why this is a table and not a
transformation."""

US_EQUITY: Final = VenueDeclaration(
    account="margin",
    currency=CURRENCY,
    costs=CostsOverride(commission_bps=COMMISSION_BPS),
)
"""What this broker declares about every venue it serves. One declaration for all of them,
because the account is one account: its type, its currency and its commission do not
change with the venue an order happens to route to."""

VENUES: Final[dict[str, VenueDeclaration]] = {
    venue: US_EQUITY for venue in sorted(set(EXCHANGES.values()))
}
"""The venues served, derived from the exchanges the broker names rather than listed
twice, so a venue this adapter can resolve an instrument onto is a venue it declares a
model for."""


def venue_of(exchange: str) -> str | None:
    """The market identifier code of the broker's own exchange name, or `None`.

    `None` rather than a refusal or a guess: an exchange this table does not hold is one
    instrument's failure, and a caller resolving a universe turns it into exactly that
    while the rest of the universe resolves.
    """
    return EXCHANGES.get(exchange.strip().upper())


def declaration(venue: str) -> VenueDeclaration | None:
    """What this broker declares about `venue`, or `None` for one it does not serve.

    `None` leaves the shipped defaults in force with their origin recorded as such, which
    is the truthful outcome: a venue this broker does not trade is a venue it has said
    nothing about.
    """
    return VENUES.get(venue)


def serves(venue: str) -> bool:
    """Whether this broker trades `venue` at all."""
    return venue in VENUES
