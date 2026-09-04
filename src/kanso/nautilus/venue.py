"""One simulated venue per venue a hypothesis trades, and a cost model on none of them.

A hypothesis names its universe as fully qualified instrument ids, so the venues it
trades are read off the universe rather than declared twice. Each of them becomes one
engine venue configuration carrying the resolved venue model's account type and account
currency, netting, bar execution, the hypothesis's leverage ceiling, and a starting
balance of the run's capital.

**The simulated venue is deliberately cost-neutral.** No fee model, no fill model and no
latency model is configured, because kanso deducts commission, slippage and the spread
exactly once, in the runner's extraction. One application means one number: the same
cost arithmetic produces the figure on a card, the figure a certification gate reads and
the figure a replay or a broker-paper session is compared against, and a cost model can
be re-applied to recorded fills without re-running anything. A venue that also charged
would double-count, and the two counts would disagree the moment either changed.

Every venue account is funded with the whole run capital, because the engine keeps one
account per venue and has no cross-venue book. What bounds exposure across venues is the
sleeve, which sizes against `max_position_pct` of capital per instrument and
`max_leverage` x capital gross over every position it holds; the venue balance only has
to be large enough not to reject an order the sleeve already allowed.

Engine facts this module relies on (nautilus_trader 1.231.0): `BacktestVenueConfig` is
the declarative form of `BacktestEngine.add_venue`, and `BacktestNode` translates one
into the other with `get_oms_type`, `get_account_type`, `get_base_currency` and
`get_starting_balances`; `starting_balances` entries are strings parsed by
`Money.from_str`, which requires the amount to carry the currency's own precision;
`fill_model`, `fee_model` and `latency_model` left unset mean the exchange charges
nothing beyond an instrument's own maker and taker rates, which kanso's resolved
instruments leave at zero; `default_leverage` is a `Decimal` and is meaningless on a cash
account, which cannot borrow.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from nautilus_trader.config import BacktestVenueConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency, Money

from kanso.errors import ValidationError
from kanso.schemas import Hypothesis, VenueModel

__all__ = ["NETTING", "starting_balance", "venue_configs", "venues_of"]

NETTING: Final = "NETTING"
"""The order management model: one net position per instrument, which is what a
hypothesis's risk limits and the sleeve's own exposure arithmetic are expressed in."""

CASH_LEVERAGE: Final = 1.0
"""A cash account cannot borrow, so its leverage is one whatever the hypothesis asks."""

_ACCOUNT_TYPES: Final[dict[str, str]] = {"margin": "MARGIN", "cash": "CASH"}


def venues_of(universe: Sequence[str]) -> tuple[str, ...]:
    """The distinct venues a universe trades, sorted, read off its instrument ids."""
    venues: set[str] = set()
    for identifier in universe:
        try:
            venues.add(InstrumentId.from_str(identifier).venue.value)
        except ValueError as exc:
            raise ValidationError(
                f"universe: {identifier!r} is not a qualified instrument id, so the venue it "
                f"trades cannot be read from it: {exc}",
                remedy="write universe ids as SYMBOL.VENUE, for example AAPL.XNAS",
            ) from None
    return tuple(sorted(venues))


def starting_balance(capital: float, currency: str) -> str:
    """The run's capital as the amount-and-currency string a venue is funded with."""
    try:
        return str(Money(capital, Currency.from_str(currency)))
    except (ValueError, OverflowError) as exc:
        raise ValidationError(
            f"capital: {capital} is not an amount of {currency} the engine can fund an "
            f"account with: {exc}"
        ) from None


def venue_configs(
    hyp: Hypothesis,
    venue_model: Mapping[str, object] | VenueModel,
    capital: float,
) -> list[BacktestVenueConfig]:
    """One cost-neutral engine venue per venue in the hypothesis's universe.

    The account type and currency come from the resolved venue model, the leverage
    ceiling from the hypothesis's risk limits, and the starting balance from the run's
    capital. The model's costs are deliberately not translated into a fee model: the
    runner applies them once, to the fills, after the backtest.
    """
    model = (
        venue_model
        if isinstance(venue_model, VenueModel)
        else VenueModel.model_validate(dict(venue_model))
    )
    if capital <= 0:
        raise ValidationError(f"capital: {capital} is not an amount to fund a venue with")
    balance = starting_balance(capital, model.currency)
    leverage = CASH_LEVERAGE if model.account == "cash" else hyp.risk_limits.max_leverage
    return [
        BacktestVenueConfig(
            name=venue,
            oms_type=NETTING,
            account_type=_ACCOUNT_TYPES[model.account],
            starting_balances=[balance],
            base_currency=model.currency,
            default_leverage=leverage,
            bar_execution=True,
            fill_model=None,
            fee_model=None,
            latency_model=None,
        )
        for venue in venues_of(hyp.universe)
    ]
