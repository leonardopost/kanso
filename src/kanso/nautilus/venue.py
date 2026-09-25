"""One simulated venue per venue a hypothesis trades, and a cost model on none of them.

A hypothesis names its universe as fully qualified instrument ids, so the venues it
trades are read off the universe rather than declared twice. Each of them becomes one
engine venue configuration carrying the resolved venue model's account type and account
currency, netting, bar execution, the hypothesis's leverage ceiling, the fill model its
`limit_fill` names, and a starting balance of the run's capital.

**The simulated venue is deliberately cost-neutral.** No fee model and no latency model is
configured, and the fill model slips nothing, because kanso deducts commission, slippage
and the spread exactly once, in the runner's extraction. One application means one number:
the same cost arithmetic produces the figure on a card, the figure a certification gate
reads and the figure a replay or a broker-paper session is compared against, and a cost
model's charges can be re-applied to recorded fills without re-running anything. A venue
that also charged would double-count, and the two counts would disagree the moment either
changed.

**The one thing the venue is told is when a resting limit fills.** The fill model decides
nothing else here: its slippage probability is zero, and its limit probability is exactly
one or exactly zero, so it never draws a random number and the same request fills the same
way every time. `touch` is the engine's own default, probability one: a resting limit fills
the moment the market reaches its price. `through` is probability zero: a limit whose price
the market only reached stays on the book, and fills once a print or a quote goes beyond
it. Both code paths build their fill model from this one configuration, so a card and a
stage cannot disagree about which orders filled.

Every venue account is funded with the whole run capital, because the engine keeps one
account per venue and has no cross-venue book. What bounds exposure across venues is the
sleeve, which sizes against `max_position_pct` of what its book can fund per instrument and
`max_leverage` x capital gross over every position it holds; the venue balance only has
to be large enough not to reject an order the sleeve already allowed.

Engine facts this module relies on (nautilus_trader 1.231.0): `BacktestVenueConfig` is
the declarative form of `BacktestEngine.add_venue`, and `BacktestNode` translates one
into the other with `get_oms_type`, `get_account_type`, `get_base_currency`,
`get_starting_balances` and `get_fill_model`, the last building a `FillModel` from an
`ImportableFillModelConfig` through `FillModelFactory`; `starting_balances` entries are
strings parsed by `Money.from_str`, which requires the amount to carry the currency's own
precision; `fee_model` and `latency_model` left unset mean the exchange charges nothing
beyond an instrument's own maker and taker rates, which kanso's resolved instruments leave
at zero; `FillModel.is_limit_filled` and `is_slipped` answer a probability of exactly zero
or one without drawing, and the matching engine asks the first of them only for a MAKER
order whose price the market reached exactly (`kanso.nautilus.facts` measures which
market state counts as reaching it); `default_leverage` is a `Decimal` and is meaningless
on a cash account, which cannot borrow.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from nautilus_trader.config import BacktestVenueConfig, ImportableFillModelConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Currency, Money

from kanso.errors import ValidationError
from kanso.schemas import Hypothesis, LimitFill, VenueModel

__all__ = [
    "LIMIT_FILL",
    "NETTING",
    "fill_model",
    "starting_balance",
    "venue_configs",
    "venues_of",
]

NETTING: Final = "NETTING"
"""The order management model: one net position per instrument, which is what a
hypothesis's risk limits and the sleeve's own exposure arithmetic are expressed in."""

CASH_LEVERAGE: Final = 1.0
"""A cash account cannot borrow, so its leverage is one whatever the hypothesis asks."""

LIMIT_FILL: Final[dict[str, float]] = {"touch": 1.0, "through": 0.0}
"""The engine's `prob_fill_on_limit` for each rule a venue model can state: whether a
resting limit the market reached, and went no further than, fills."""

FILL_MODEL: Final = "nautilus_trader.backtest.models:FillModel"
FILL_MODEL_CONFIG: Final = "nautilus_trader.backtest.config:FillModelConfig"
"""The engine's own fill model and its configuration, by the paths its factory resolves."""

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


def fill_model(limit_fill: LimitFill) -> ImportableFillModelConfig:
    """The fill model a venue is configured with: when a resting limit fills, and nothing
    else — it slips no order, because slippage is the runner's to charge."""
    return ImportableFillModelConfig(
        fill_model_path=FILL_MODEL,
        config_path=FILL_MODEL_CONFIG,
        config={"prob_fill_on_limit": LIMIT_FILL[limit_fill], "prob_slippage": 0.0},
    )


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

    The account type, the currency and the limit-fill rule come from the resolved venue
    model, the leverage ceiling from the hypothesis's risk limits, and the starting
    balance from the run's capital. The model's charges are deliberately not translated
    into a fee model: the runner applies them once, to the fills, after the backtest.
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
            fill_model=fill_model(model.costs.limit_fill),
            fee_model=None,
            latency_model=None,
        )
        for venue in venues_of(hyp.universe)
    ]
