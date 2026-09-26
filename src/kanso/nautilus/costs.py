"""What a trade and a book cost: the arithmetic the runner charges and the harness reads.

Commission — in basis points and, where the model states it, per share — slippage and half
the spread are charged on every fill, once, by the runner's extraction
(`kanso.nautilus.backtest`); the simulated venue charges nothing. A fill that
rested on the book — one the venue reports as a maker's — pays the venue model's
`maker_bps` instead of all three when the model states one: it filled at its own price, so
it slipped nothing, and the spread is what it earns rather than pays. A negative rate is a
rebate. A sleeve's harness needs the same number while it runs, to know what its account
holds, so the arithmetic lives here and both call it: the balance a strategy sizes against
is the equity the runner strikes.

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
from typing import Final

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
    "maintenance_ratio",
    "month_turned",
    "policy_of",
    "quote_half_spread",
    "reset",
    "side_rate",
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


def fill_rate(
    commission_bps: float,
    slippage_bps: float,
    half_spread: float,
    maker_bps: float | None,
    *,
    maker: bool,
) -> float:
    """What one fill costs per unit of notional under a venue model.

    A maker's fill pays `maker_bps` and nothing else when the model states it, which may be
    negative; every other fill — and a maker's, under a model that states no `maker_bps` —
    pays commission, slippage and half the spread, exactly as `side_rate` strikes it.
    """
    if maker and maker_bps is not None:
        return maker_bps / BPS
    return side_rate(commission_bps, slippage_bps, half_spread)


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
) -> float:
    """What one fill costs in the account currency: `fill_rate` of its notional, plus the
    per-share commission on each share whenever the fill pays commission at all.

    A maker's fill under a stated maker rate pays that rate alone, per share included: the
    rate is the whole charge on that fill by contract, and a per-share-priced account states
    its maker net there — commission less the rebate. Every other fill pays the per-share
    commission on top of the three rates, so a cheap share pays more of its price than a
    dear one, exactly as the account would charge it.
    """
    charged = notional * fill_rate(
        commission_bps, slippage_bps, half_spread, maker_bps, maker=maker
    )
    if maker and maker_bps is not None:
        return charged
    return charged + qty * commission_per_share


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
