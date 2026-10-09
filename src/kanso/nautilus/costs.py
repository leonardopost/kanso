"""What a trade and a book cost: the arithmetic the runner charges and the harness reads.

Commission — in basis points and, where the model states it, per share — slippage and half
the spread are charged on every fill, once, by the runner's extraction
(`kanso.nautilus.backtest`); the simulated venue charges nothing. A fill that
rested on the book — one the venue reports as a maker's — pays the venue model's maker
schedule instead of all three, and instead of the per-share commission, when the model states
one: `maker_bps` of its notional and `maker_per_share` on each share, either of which alone
states the schedule and the other is then nothing. It filled at its own price, so it slipped
nothing, and the spread is what it earns rather than pays. A negative rate or per-share charge
is a rebate. A sale pays the regulatory fee the model states on top, whoever the venue reports
the fill as: `sell_fee_bps` of its notional and `sell_fee_per_share` on each share, the
transaction fee and the trading activity fee an account passes through on sells alone.
A taker's fill — one the venue reports as a taker's, never a maker's — pays the model's
`slippage_ticks` on top as well: that many of the instrument's price increments on each share,
capped on an order with a limit at what the limit leaves past the fill's price
(`tick_slip`), so an account that pays a tick over the touch to take states it in the unit it
pays it in, exact for any increment, and a limit is never charged past its own price.
A sleeve's harness needs the same number while it runs, to know what its account
holds, so the arithmetic lives here and both call it: the balance a strategy sizes against
is the equity the runner strikes.

A perpetual's funding is the third charge, and the only one that is neither a fill cost nor
a period-end policy: at each settlement the holder pays the realised rate on the notional it
holds then (`funding_payment`), so a long pays a positive rate and a short receives it. The
runner books it once, in the extraction, at the settlement instant; the harness books the
same amount when the settlement point is delivered, before the sleeve is handed it.

The book policy a hypothesis declares is the same shape of promise at the period end. A
monthly reset moves a surplus into a cushion and restores a deficit from it; a financing
carry charges a yearly rate on what the book holds above its equity; both are applied
once, by the runner, at each period end of the extraction. The harness settles the same
period from the same functions when the first point of the next period is delivered, so a
balance read at a period's last point is the equity struck there before that end's carry
and transfer, and any later read includes them. The maintenance ratio is read from the run
alone — a gate's arithmetic, with no harness half — and lives here beside the rest so the
three rules are one module.

Nothing here is delegated to the venue, because the venue does none of it. Under
nautilus_trader 1.231.0 `RiskEngine._check_orders_risk_for_account` returns before any
balance or margin check when the account is a margin account (`risk/engine.pyx`,
"Determine risk controls for margin"), kanso's resolved instruments carry zero margin
rates (`kanso.nautilus.splits`), and the engine's `FUNDING` position adjustment is
published to nothing and applied by no venue (`kanso.nautilus.facts`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from math import fsum
from typing import Any, Final

from kanso.schemas import Book

__all__ = [
    "BPS",
    "MONTHLY",
    "NS_PER_YEAR",
    "BookPolicy",
    "carry",
    "fill_cost",
    "fill_rate",
    "fixed_half_spread",
    "funding_payment",
    "limit_at",
    "maintenance_ratio",
    "month_turned",
    "policy_of",
    "quote_half_spread",
    "reset",
    "side_rate",
    "tick_slip",
]

BPS: Final = 10_000.0
"""Basis points in one: the unit every cost in a venue model is stated in."""

NS_PER_YEAR: Final = 31_557_600_000_000_000
"""A Julian year of 365.25 days in nanoseconds: the year a financing rate is stated per."""

MONTHLY: Final = "monthly"
"""The one reset rhythm: the book returns to its capital at the first period end of a month."""

_EPOCH: Final = date(1970, 1, 1)
_NS_PER_DAY: Final = 86_400 * 1_000_000_000


def fixed_half_spread(fixed_bps: float | None) -> float:
    """Half a stated spread width, as a fraction of notional: what one side of a trip pays."""
    return (fixed_bps or 0.0) / 2.0 / BPS


def quote_half_spread(bid: float, ask: float) -> float:
    """Half a quoted spread as a fraction of its mid; nothing when the quote has no mid."""
    mid = (bid + ask) / 2.0
    return 0.0 if mid <= 0 else (ask - bid) / mid / 2.0


def side_rate(commission_bps: float, slippage_bps: float, half_spread: float) -> float:
    """What one fill costs per unit of notional: commission, slippage and half the spread."""
    return (commission_bps + slippage_bps) / BPS + half_spread


def _rests(maker: bool, maker_bps: float | None, maker_per_share: float | None) -> bool:
    """Whether a fill is charged the maker schedule: the venue reported it as a maker's and
    the model states a schedule for one — `maker_bps`, `maker_per_share` or both."""
    return maker and (maker_bps is not None or maker_per_share is not None)


def fill_rate(
    commission_bps: float,
    slippage_bps: float,
    half_spread: float,
    maker_bps: float | None,
    *,
    maker: bool,
    maker_per_share: float | None = None,
) -> float:
    """What one fill costs per unit of notional under a venue model.

    A maker's fill under a stated maker schedule pays `maker_bps` and nothing else per unit
    of notional — nothing at all when the schedule is stated per share alone — which may be
    negative; every other fill — and a maker's, under a model that states no maker schedule —
    pays commission, slippage and half the spread, exactly as `side_rate` strikes it.
    """
    if _rests(maker, maker_bps, maker_per_share):
        return 0.0 if maker_bps is None else maker_bps / BPS
    return side_rate(commission_bps, slippage_bps, half_spread)


def tick_slip(
    slippage_ticks: float, increment: float, px: float, limit: float | None, *, sell: bool
) -> float:
    """What a taker's fill is charged on each share for `slippage_ticks`, as a price: that many
    of the instrument's `increment`, and on an order with a `limit` no more than the limit
    leaves past the fill's price `px` — above it for a buy, below it for a sale — so a limit
    filled at its own price pays none of it and none pays past its limit. A market order
    carries no limit and pays it whole. Nothing for a model that states none.
    """
    slip = slippage_ticks * increment
    if limit is None or slip <= 0.0:
        return slip
    room = px - limit if sell else limit - px
    return min(slip, max(room, 0.0))


def limit_at(order: Any, fill_id: Any) -> float | None:
    """The limit price `order` carried when its fill with event id `fill_id` was applied: the
    price it was created with, as each update the venue accepted restated it, up to that fill,
    so a fill before a modify is capped by the limit it filled under; an id the order never
    took reads as its last limit. `None` for an order that carries no limit — a market order,
    a stop to market — and for one no cache holds.

    Read from the order's own events, because the order object holds only its last price:
    under nautilus_trader 1.231.0 `OrderInitialized.options` carries a limit's `price` as a
    string and a market order's options none, `OrderUpdated.price` is the price a modify set
    or `None` when it set none, and an order the emulator released keeps the events of the
    order it was made from — so one released at market carries no limit here.
    """
    from nautilus_trader.model.events import OrderInitialized, OrderUpdated

    if order is None or not order.has_price:
        return None
    price: float | None = None
    for event in order.events:
        if isinstance(event, OrderInitialized):
            stated = event.options.get("price")
            price = None if stated is None else float(stated)
        elif isinstance(event, OrderUpdated) and event.price is not None:
            price = float(event.price)
        elif event.id == fill_id:
            break
    return price


def fill_cost(
    notional: float,
    qty: float,
    commission_bps: float,
    slippage_bps: float,
    half_spread: float,
    maker_bps: float | None,
    commission_per_share: float,
    *,
    maker: bool,
    sell: bool = False,
    sell_fee_bps: float = 0.0,
    sell_fee_per_share: float = 0.0,
    maker_per_share: float | None = None,
    slip: float = 0.0,
    multiplier: float = 1.0,
) -> float:
    """What one fill costs in the account currency: `fill_rate` of its notional, plus the
    per-share charge its side pays on each share, plus the sell-side fees on a sale.

    `slip` is the price a taker's share is charged over its fill for the model's
    `slippage_ticks` (`tick_slip`), worth `multiplier` in the account currency per unit of
    price on a contract; a fill the venue reports as a maker's pays none of it, whatever the
    model states, since it filled at its own price.

    A maker's fill under a stated maker schedule pays that schedule alone: `maker_bps` of its
    notional and `maker_per_share` on each share, the per-share commission not at all — the
    schedule is the whole charge on that fill by contract. A per-share-priced account states
    its maker charge per share there, commission less any rebate, exactly as it is charged;
    one priced per notional states `maker_bps`. Every other fill pays the per-share
    commission on top of the three rates, so a cheap share pays more of its price than a
    dear one, exactly as the account would charge it. A sale pays `sell_fee_bps` of its
    notional and `sell_fee_per_share` on each share on top of all of that, maker or taker:
    a regulatory fee is passed through on every sell, and no venue's maker schedule covers it.

    A model that states neither maker key nor a tick charges every fill as it always was, bit
    for bit: the sums are taken in the order they were before `maker_per_share` and
    `slippage_ticks` existed.
    """
    charged = notional * fill_rate(
        commission_bps,
        slippage_bps,
        half_spread,
        maker_bps,
        maker=maker,
        maker_per_share=maker_per_share,
    )
    if sell:
        charged += notional * sell_fee_bps / BPS + qty * sell_fee_per_share
    if _rests(maker, maker_bps, maker_per_share):
        return charged if maker_per_share is None else charged + qty * maker_per_share
    if slip and not maker:
        charged += qty * slip * multiplier
    return charged + qty * commission_per_share


def funding_payment(signed_qty: float, mark: float, multiplier: float, rate: float) -> float:
    """What one funding settlement takes from the holder, in the account currency.

    The rate is the realised rate of the period that settled, a fraction of notional, and
    the notional is signed: `qty x mark x multiplier`, a short's negative. So a long pays a
    positive rate and is paid a negative one, and a short the reverse — a negative amount is
    one the holder received. Nothing is held, nothing is paid.
    """
    return signed_qty * mark * multiplier * rate


@dataclass(frozen=True)
class BookPolicy:
    """A hypothesis's `book`, as the runner and the harness carry it: three plain numbers."""

    reset: str = "none"
    financing_rate_bps: float = 0.0
    maintenance_pct: float | None = None

    @property
    def resets(self) -> bool:
        """Whether the book returns to its capital at the turn of each month."""
        return self.reset == MONTHLY

    @property
    def charges(self) -> bool:
        """Whether a borrowed notional costs anything."""
        return self.financing_rate_bps > 0


def policy_of(book: Book | None) -> BookPolicy | None:
    """The policy a hypothesis declares, or `None` for a book left as the fills leave it."""
    if book is None:
        return None
    return BookPolicy(
        reset=book.reset,
        financing_rate_bps=book.financing_rate_bps,
        maintenance_pct=book.maintenance_pct,
    )


def reset(equity: float, capital: float, cushion: float) -> tuple[float, float]:
    """The cash a reset moves into the book, and the cushion after it.

    A surplus over the capital leaves the book for the cushion, so the transfer is
    negative; a deficit is restored from the cushion while it lasts, and no further —
    nothing is borrowed to restore a book the cushion cannot. Positions are untouched
    either way: the transfer is cash, and the strategy sizes against the book it leaves.
    """
    surplus = equity - capital
    if surplus >= 0.0:
        return -surplus, cushion + surplus
    restore = min(cushion, -surplus)
    return restore, cushion - restore


def carry(gross: float, equity: float, rate_bps: float, span_ns: int) -> float:
    """What a period of borrowing costs: the rate per year on the notional above the equity.

    `gross` is the absolute exposure, shorts included, so a short's proceeds-backed notional
    is charged like a borrowed long — the stock-loan analogue — and a long-only book at
    leverage one is charged exactly nothing. The span is the period's own length, so a
    weekend inside a daily period costs the days it holds.
    """
    borrowed = max(0.0, gross - equity)
    return borrowed * rate_bps / BPS * span_ns / NS_PER_YEAR


def month_turned(previous_ns: int | None, ts_ns: int) -> bool:
    """Whether the calendar month has moved between two instants; never from no instant."""
    if previous_ns is None:
        return False
    return _month_of(previous_ns) != _month_of(ts_ns)


def _month_of(ts_ns: int) -> tuple[int, int]:
    day = _EPOCH + timedelta(days=ts_ns // _NS_PER_DAY)
    return day.year, day.month


def maintenance_ratio(cash: float, valued: Iterable[float]) -> float | None:
    """The book's equity over its gross with every holding at its adverse price.

    `valued` is each holding's signed worth at the period's adverse extreme — a long at
    the lowest low, a short at the highest high. The ratio is what a margin desk reads
    before a call, as a fraction; `None` when nothing is held, since a flat book has no
    margin to maintain.
    """
    worths = list(valued)
    gross = fsum(abs(worth) for worth in worths)
    if gross <= 0.0:
        return None
    return (cash + fsum(worths)) / gross
