"""Tick and lot conventions: dated facts about a market, keyed by asset class, venue and price.

A minimum price increment and a minimum tradable lot are set by a regulator or by the
venue's own rulebook. **No vendor publishes them**, which is why they are held here rather
than read from a reference feed: a vendor that returned one would be reporting its own
rounding convention, and a run that priced against it would be pricing against a guess.

They are also *dated*. A tick size is reassigned — by rule change, by pilot programme, by
a security moving across a price band — and a reassignment must never rewrite the past: a
card completed under the old increment stays valid under it. So the table holds, per
(asset class, venue), a tuple of dated `Schedule`s, and a lookup asks for the one in force
on a date. Adding a reassignment is appending a `Schedule` — a data change, not a code
change — and nothing that reads the table needs editing.

Within a schedule the increment may depend on the price: US equities quote in whole cents
at or above a dollar and in hundredths of a cent below it, so `Schedule.bands` is a ladder
of price floors, ascending, the first of which is zero. `tick_size` walks it and returns
the increment of the highest floor at or below the price.

`venue` may be the wildcard `ANY`, which is the right key when the rule is national rather
than per-venue: the US sub-penny prohibition binds every US equity venue identically, and
enumerating them would claim a per-venue fact that does not exist. A venue-specific entry
wins over the wildcard.

The core ships only what it can state exactly. An asset class and venue with no schedule on
file is a refusal, not a default: the instrument's entry must then declare its own
`price_increment` and `lot_size`, which is an operator asserting a fact rather than kanso
inventing one.

These values feed the `price_increment` and `lot_size` constructor arguments of the
NautilusTrader instrument classes (nautilus_trader 1.231.0), which have no engine defaults
of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from kanso.errors import ValidationError

ANY = "*"
"""The venue key for a rule that binds every venue of an asset class."""


@dataclass(frozen=True)
class Band:
    """The minimum price increment that applies at or above a price floor."""

    at_or_above: Decimal
    tick: Decimal


@dataclass(frozen=True)
class Schedule:
    """The increments and lot in force from a date, and the rule that set them.

    `bands` ascends by price floor and its first floor is zero, so every non-negative
    price falls in a band. `authority` is the rule in words, so a refusal or a `doctor`
    listing can say who set the number rather than only what it is.
    """

    effective: date
    authority: str
    bands: tuple[Band, ...]
    lot: Decimal


SCHEDULES: dict[tuple[str, str], tuple[Schedule, ...]] = {
    ("EQUITY", ANY): (
        Schedule(
            effective=date(2005, 8, 29),
            authority=(
                "SEC Rule 612 (Regulation NMS): no quotation in an NMS stock priced at or "
                "above $1.00 may be in an increment finer than $0.01, and none below "
                "$1.00 finer than $0.0001"
            ),
            bands=(
                Band(Decimal("0"), Decimal("0.0001")),
                Band(Decimal("1.00"), Decimal("0.01")),
            ),
            lot=Decimal("1"),
        ),
    ),
}
"""Every schedule on file, keyed by asset class and venue, in effective-date order.

One entry ships because one is what can be stated exactly. A round lot of 100 shares is a
display and routing convention, not a minimum size — odd lots trade freely — so the lot
here is one share, which is what the engine needs to size an order.
"""


def schedule(asset_class: str, venue: str, as_of: date) -> Schedule:
    """The schedule in force for `asset_class` on `venue` on `as_of`.

    Raises a validation failure when no schedule is on file for the pair, or when every
    schedule on file takes effect after the date.
    """
    known = SCHEDULES.get((asset_class, venue)) or SCHEDULES.get((asset_class, ANY))
    if not known:
        raise ValidationError(
            f"no tick and lot convention on file for {asset_class} on {venue}",
            remedy=(
                "declare `price_increment` and `lot_size` in this instrument's `override` "
                "in instruments.yaml"
            ),
        )
    in_force = [item for item in known if item.effective <= as_of]
    if not in_force:
        earliest = min(item.effective for item in known)
        raise ValidationError(
            f"no tick and lot convention for {asset_class} on {venue} as of {as_of}: "
            f"the earliest on file takes effect {earliest}"
        )
    return max(in_force, key=lambda item: item.effective)


def tick_size(asset_class: str, venue: str, price: float, as_of: date) -> Decimal:
    """The minimum price increment for an instrument quoted near `price` on `as_of`."""
    if price < 0:
        raise ValidationError(f"tick size asked at a negative price ({price})")
    level = Decimal(str(price))
    found = schedule(asset_class, venue, as_of)
    tick = found.bands[0].tick
    for band in found.bands:
        if level < band.at_or_above:
            break
        tick = band.tick
    return tick


def lot_size(asset_class: str, venue: str, as_of: date) -> Decimal:
    """The minimum tradable lot for an instrument of `asset_class` on `venue` on `as_of`."""
    return schedule(asset_class, venue, as_of).lot
