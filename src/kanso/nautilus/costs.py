"""What one side of a trade costs: the arithmetic the runner charges and the harness reads.

Commission, slippage and half the spread are charged on every fill, once, by the runner's
extraction (`kanso.nautilus.backtest`); the simulated venue charges nothing. A sleeve's harness
needs the same number while it runs, to know what its account holds, so the arithmetic lives
here and both call it: the balance a strategy sizes against is the equity the runner strikes.
"""

from __future__ import annotations

from typing import Final

__all__ = ["BPS", "fixed_half_spread", "quote_half_spread", "side_rate"]

BPS: Final = 10_000.0
"""Basis points in one: the unit every cost in a venue model is stated in."""


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
