"""The hurdle a screened trade must clear: the runner's arithmetic, applied to no fill.

A screen books nothing. It sets beside each event's gross move the round trip a card on that
venue would be charged for it, struck by the function the runner charges every fill with
(`kanso.nautilus.costs.fill_cost`), on the venue model the workspace would trade under —
resolved in the order a hypothesis's is: the shipped defaults, `[research]`, the broker's
declaration, `venues.<MIC>` in `portfolio.yaml`, then the screen's own `costs` for the venue,
or, for a bound screen, its hypothesis's `costs`. So the hurdle is the number a card would
pay, and the result records where each part of it came from.

One round trip is two taker fills: commission and slippage on both — `slippage_ticks` too, as
that many of the leg's price increments, whole, since a taker priced at market carries no limit
to cap them — the sell-side fees on the sale (the exit of a long, the entry of a short), the
per-share commission at the fill's own price, and half the spread on each side. **No spread
is charged twice**: a follower read as quotes or a book enters at the ask or the bid it shows,
so its gross move has crossed the spread already and the hurdle charges none; a follower read
as bars or prints is priced at its last price and charged the venue model's spread. A model
whose spread comes from quotes, over a leg that has none, is refused (exit 3): nothing could
price it.

A derived follower is traded leg by leg: a basket's legs in its weights, a spread's long leg and
beta of its short one, both legs of a gap. Its hurdle is the sum of its legs' round trips, each
weighted by its share, each leg charged its own half spread — measured from its quotes where it
has them, the model's otherwise — because a derived level is a mid and crossed no spread.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from kanso.errors import ValidationError
from kanso.nautilus import adapters
from kanso.nautilus.costs import BPS, fill_cost, fixed_half_spread, quote_half_spread, tick_slip
from kanso.schemas.screen import Response, Screen
from kanso.schemas.venue import (
    DEFAULT_ACCOUNT,
    DEFAULT_CURRENCY,
    VenueDeclaration,
    VenueModel,
    resolve_venue_model,
)
from kanso.screen import grid
from kanso.screen.sessions import Series

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import Hypothesis
    from kanso.workspace import Workspace

TOUCHED: Final = frozenset({"quote", "book"})
"""Leg types that carry a bid and an ask."""


@dataclass(frozen=True)
class Hurdles:
    """The venue model of every leg a response trades, and what one round trip costs."""

    screen: Screen
    models: dict[str, VenueModel]
    venue_of: dict[str, str]
    multiplier_of: dict[str, float]
    increment_of: dict[str, float]

    def round_trip(
        self,
        name: str,
        direction: int,
        series: Mapping[str, Series],
        entry_ns: int,
        exit_ns: int,
        betas: Mapping[str, float],
    ) -> float:
        """What one round trip in `name` costs, in basis points of its notional."""
        if name in self.screen.legs:
            touched = self.screen.legs[name].type in TOUCHED
            return self._leg(name, direction, series, entry_ns, exit_ns, crossed=touched)
        return sum(
            abs(share)
            * self._leg(leg, direction * (1 if share > 0 else -1), series, entry_ns, exit_ns)
            for leg, share in shares(self.screen, name, betas).items()
        )

    def _leg(
        self,
        leg: str,
        direction: int,
        series: Mapping[str, Series],
        entry_ns: int,
        exit_ns: int,
        crossed: bool = False,
    ) -> float:
        model = self.models[self.venue_of[leg]]
        at = np.asarray([entry_ns, exit_ns], dtype=np.int64)
        prices = grid.asof(series[leg], at)
        halves = (0.0, 0.0)
        if not crossed:
            halves = self._halves(leg, model, series[leg], at)
        long = direction > 0
        total = 0.0
        ticks, increment = model.costs.slippage_ticks, self.increment_of.get(leg, 0.0)
        for price, half, sell in ((prices[0], halves[0], not long), (prices[1], halves[1], long)):
            notional = float(price) * self.multiplier_of[leg]
            charged = fill_cost(
                notional,
                1.0,
                model.costs.commission_bps,
                model.costs.slippage_bps,
                half,
                model.costs.maker_bps,
                model.costs.commission_per_share,
                maker=False,
                sell=sell,
                sell_fee_bps=model.costs.sell_fee_bps,
                sell_fee_per_share=model.costs.sell_fee_per_share,
                slip=tick_slip(ticks, increment, float(price), None, sell=sell),
                multiplier=self.multiplier_of[leg],
            )
            total += charged / notional
        return total * BPS

    def _halves(
        self, leg: str, model: VenueModel, series: Series, at: np.ndarray
    ) -> tuple[float, float]:
        """Half the spread on each side: measured from the leg's quotes, or the model's."""
        if series.bid is not None and series.ask is not None:
            bids = grid.asof(series, at, series.bid)
            asks = grid.asof(series, at, series.ask)
            return (
                quote_half_spread(float(bids[0]), float(asks[0])),
                quote_half_spread(float(bids[1]), float(asks[1])),
            )
        half = fixed_half_spread(model.costs.fixed_bps)
        return half, half


def shares(screen: Screen, name: str, betas: Mapping[str, float]) -> dict[str, float]:
    """How much of each leg one unit of a derived leg trades, signed."""
    derived = screen.derived[name]
    if derived.basket is not None:
        return dict(derived.basket)
    if derived.spread is not None:
        spread = derived.spread
        beta = spread.beta if spread.beta is not None else betas[name]
        return {spread.long: 1.0, spread.short: -beta}
    assert derived.gap is not None
    return {derived.gap.a: 1.0, derived.gap.b: -1.0}


def traded(screen: Screen) -> tuple[str, ...]:
    """Every catalog leg a response measure trades, its followers' legs expanded."""
    found: list[str] = []
    for measure in screen.measures:
        if isinstance(measure, Response):
            for leg in screen.legs_of(measure.followers):
                if leg not in found:
                    found.append(leg)
    return tuple(found)


def hurdles(
    ws: Workspace,
    screen: Screen,
    hypothesis: Hypothesis | None,
    definitions: Mapping[str, Any],
) -> Hurdles:
    """The venue model of every leg the screen's responses trade, refused where unpriceable."""
    from kanso.portfolio.files import venue_overrides  # `kanso.portfolio` imports kanso.hyp

    legs = traded(screen)
    venue_of = {leg: str(definitions[screen.legs[leg].instrument].id.venue) for leg in legs}
    quoted = {
        venue: any(screen.legs[leg].type in TOUCHED for leg in legs if venue_of[leg] == venue)
        for venue in set(venue_of.values())
    }
    research = ws.config.research
    config = VenueDeclaration(
        account=None if research.account == DEFAULT_ACCOUNT else research.account,
        currency=None if research.currency == DEFAULT_CURRENCY else research.currency,
    )
    overrides = venue_overrides(ws)
    models: dict[str, VenueModel] = {}
    for venue in sorted(quoted):
        try:
            models[venue] = resolve_venue_model(
                venue,
                config=config,
                broker=research.broker,
                declaration=adapters.venue_declaration(research.broker, venue),
                override=overrides.get(venue),
                hypothesis_costs=hypothesis.costs
                if hypothesis
                else (screen.costs or {}).get(venue),
                quotes_available=quoted[venue],
            )
        except ValidationError as error:
            where = f"{hypothesis.id}'s costs" if hypothesis else f"costs.{venue} in the screen"
            raise ValidationError(
                f"{venue}: {error.message}",
                remedy=f"state `spread: fixed_bps` and `fixed_bps` under {where}, or under "
                f"venues.{venue} in portfolio.yaml",
            ) from None
    for leg in legs:
        model = models[venue_of[leg]]
        if screen.legs[leg].type not in TOUCHED and model.costs.spread == "quotes":
            raise ValidationError(
                f"legs.{leg}: {venue_of[leg]}'s model takes the spread from quotes and "
                f"{screen.legs[leg].instrument} is read as {screen.legs[leg].type}, which has none",
                remedy=f"state `fixed_bps` under costs.{venue_of[leg]}, or read the leg as quote",
            )
    multipliers = {leg: float(definitions[screen.legs[leg].instrument].multiplier) for leg in legs}
    assert all(math.isfinite(value) and value > 0 for value in multipliers.values())
    increments = {
        leg: float(definitions[screen.legs[leg].instrument].price_increment) for leg in legs
    }
    return Hurdles(screen, models, venue_of, multipliers, increments)


def described(
    ws: Workspace, screen: Screen, hypothesis: Hypothesis | None
) -> dict[str, dict[str, object]]:
    """Per venue a response trades on, the model its hurdle is struck under, and from where.

    Asked of the definitions the store holds; when a traded leg's instrument is not defined
    yet, its model is resolved when the screen runs, after the fetch defines it, and nothing
    is described here.
    """
    from kanso.data.instruments import current_definitions

    defined = current_definitions(ws)
    legs = traded(screen)
    if not legs or any(screen.legs[leg].instrument not in defined for leg in legs):
        return {}
    found = hurdles(ws, screen, hypothesis, defined)
    return {
        venue: {
            "line": _line(model, _origin(screen, model)),
            "costs": model.costs.model_dump(mode="json", exclude_none=True),
            "origin": _origin(screen, model),
        }
        for venue, model in sorted(found.models.items())
    }


def _origin(screen: Screen, model: VenueModel) -> str:
    """Where the costs came from: a free screen's own `costs` stand where a hypothesis's would,
    and are named for the file that stated them."""
    origin = model.origins.costs
    return "screen" if origin == "hypothesis" and screen.hyp is None else origin


def _line(model: VenueModel, origin: str) -> str:
    costs = model.costs
    spread = "from quotes" if costs.spread == "quotes" else f"fixed {costs.fixed_bps:g} bp"
    ticks = f" + {costs.slippage_ticks:g} ticks" if costs.slippage_ticks else ""
    return (
        f"commission {costs.commission_bps:g} bp + {costs.commission_per_share:g}/share, "
        f"slippage {costs.slippage_bps:g} bp{ticks}, spread {spread}, sale fees "
        f"{costs.sell_fee_bps:g} bp · from {origin}"
    )
