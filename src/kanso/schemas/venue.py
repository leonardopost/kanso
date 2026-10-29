"""The venue model: account type, account currency and cost model, and where each came from.

A venue's trading model is inherited, never invented. The workspace's `[research]`
table states an account type and an account currency for every venue; the broker behind
the configured execution client declares them, and its costs, for the venues it serves; a
per-venue entry in the portfolio overrides any field; a hypothesis's own `costs` overrides
the cost model for that hypothesis alone. Where nothing is declared the shipped defaults
apply: a margin account whose leverage is the hypothesis's `max_leverage`, USD, zero
commission and one basis point of slippage, with the spread taken from quotes when quotes
are available, and a resting limit order filled when the market reaches its price — or, for a
hypothesis at tick resolution that asks every name for quotes and prints, only on a later
print through it (`TICK_LIMIT_FILL`), the caller saying which (`default_limit_fill`). The
spread, quoted or fixed, and the slippage in basis points are charged on a fill the venue
struck at a print or a bar's close inside the quote, or in a name with no quote before it; a
fill struck at the touch — a buy at or above the ask of its name's last quote, a sale at or
below its bid, or any fill on a venue matching a level-two book — carries the spread in its
price and is charged neither (`kanso.nautilus.costs.at_touch`). Under `spread: quotes` a
stated `fixed_bps` is the width a fill with no quote before it pays. A
currency code is any code the engine could register — a fiat code or a crypto code such as
USDT — and whether it does register it is checked where an account is funded, not here.

A fill that rested on the book may be charged apart. `maker_bps` and `maker_per_share`, when
the resolved model states either, are the whole charge on a fill the venue reports as a
maker's — `maker_bps` of its notional and `maker_per_share` on each share, the one the resolved
model leaves unstated counting as nothing — so it pays no slippage, since a resting limit fills
at its own price, no half-spread, since the spread is what it earns rather than what it pays,
and no per-share commission. Either may be negative, a rebate. With neither stated, a maker's
fill is charged like any other, which is how every fill was charged before the keys existed.
The two layer one by one, like every key of the block: `maker_per_share` stated over a layer
that states `maker_bps` charges both, and `maker_bps: 0.0` beside it charges the share alone.

A commission may be stated per share as well as in basis points. `commission_per_share` is
charged on every share of a fill that pays commission at all — a taker's, and a maker's
under a model that states no maker schedule — on top of the three rates, so a cheap share
pays more of its price than a dear one, the way a per-share-priced account does; a maker's
fill under a stated maker schedule pays that schedule alone, because it is the whole charge on
that fill by contract, and an account that charges a resting fill per share states that charge
as `maker_per_share`. Zero unless stated.

Slippage may be stated in ticks as well as in basis points. `slippage_ticks` is charged on
every share of a fill the venue reports as a taker's — a market order, or a limit that was
marketable when it landed — as that many of the instrument's own price increments, the way an
account states that it pays a tick over the touch to take; never on a maker's fill, which
filled at its own price. On an order that carries a limit it is capped, share by share, at
what the limit leaves above the fill's price for a buy or below it for a sale, so a limit
filled at its own price pays none of it and none pays past its limit. Zero unless stated.

The cost model carries four keys that are not charges. `latency_ms` is how long the venue
takes to see an order — the delay between a sleeve's insert, update or cancel and the
simulated book acting on it, zero unless stated, measured on the account and route the
strategy will trade through; a hypothesis at tick resolution may not resolve it to zero
(`kanso hyp validate`). `limit_fill` is the matching rule the
simulated venue is built with: `touch` fills a resting limit the market only reached,
`through` fills it only once the market trades beyond its price, and `print_through` fills it
only on a print beyond its price, by that print's own size (`print_through_whole`, for all
that is left of it), and fills a taker on the quote rather than on a print. It decides which fills a
run has rather than what they cost, so it cannot be re-applied to recorded fills the way the
charges can; it lives beside them because it is the same kind of statement — how
pessimistic the simulation is about execution — and is stated at the same three layers.
`orders_per_minute` is how many of the account's requests a minute the sleeve's orders may
spend: the sleeve holds itself to it on the data clock, on every path (`kanso.nautilus.rate`),
each submit and each modify spending one and each cancel of an order `requests_per_cancel`,
the requests the broker's client sends for one (one unless stated), and with it unset nothing
holds a sleeve to any rate. A broker states its figure and its client's cancel for its venues
and a workspace its plan's figure; it is the sleeve's own figure, so sleeves sharing one
account state shares of it. A rate below what one cancel spends is refused where the model is
resolved, naming the venue, and by the model itself: the sleeve's bucket holds no more than the
rate, so its first cancel would wait for ever and every entry and modify behind it be dropped.
Unset, each is left out of the resolved model's serialised form altogether, so a model in which
no layer states either, the broker's declaration included, dumps, digests and records exactly
as one did before the keys existed.

The resolved model records the origin of every field, so a number on a card, a certificate
or a strategy version is always traceable to the venue that produced it.

An execution client declares how its capital is funded and which clock it runs on. Those
two declarations are what forbid real money off the live stage and a historical replay
feeding a broker that matches against current prices.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from kanso.errors import ApprovalError, PreconditionError, ValidationError
from kanso.schemas.base import KansoModel, NonEmpty

Account = Literal["margin", "cash"]
Spread = Literal["quotes", "fixed_bps"]
LimitFill = Literal["touch", "through", "print_through", "print_through_whole"]
Origin = Literal["default", "config", "broker", "venue_override", "hypothesis"]
Funding = Literal["simulated", "broker_paper", "real"]
Clock = Literal["replay", "wall"]

VenueCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,16}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]{1,7}$")]

DEFAULT_ACCOUNT: Account = "margin"
DEFAULT_CURRENCY = "USD"
DEFAULT_COMMISSION_BPS = 0.0
DEFAULT_SLIPPAGE_BPS = 1.0
DEFAULT_LIMIT_FILL: LimitFill = "touch"
"""A resting limit fills when the market reaches its price: the engine's own default, what
every venue model resolved before the key existed was measured under, and the rule a
hypothesis resolves to when no layer states one and it does not ask every name for quotes and
prints at tick resolution — on bars there is no tape to credit a resting order from, and in a
name with one feed alone the print rule would fill a taker or a resting limit never."""
TICK_LIMIT_FILL: LimitFill = "print_through"
"""The rule a hypothesis at tick resolution that asks every name for quotes and prints resolves
to when no layer states one: a resting limit fills only on a later print through it, by that print's
size, and a taker on the quote in force. On a tape `touch` credits a resting limit with no
queue ahead of it, all that is left of it from any point beyond its price, and the size a
quote shows again at every quote that shows it — fills the market did not give."""
REPLAY_DATA_CLIENT = "replay"
"""The catalog replay data client; every other data client id belongs to an adapter."""


UNWRITTEN_UNSTATED: Final = ("orders_per_minute", "requests_per_cancel")
"""The cost keys a resolved model leaves out of what it dumps when no layer states them."""


class CostsOverride(KansoModel):
    """Any subset of a cost model, as a hypothesis or a venue entry may state it."""

    commission_bps: float | None = Field(default=None, ge=0)
    commission_per_share: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    slippage_ticks: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    spread: Spread | None = None
    fixed_bps: float | None = Field(default=None, ge=0)
    maker_bps: float | None = Field(default=None, allow_inf_nan=False)
    maker_per_share: float | None = Field(default=None, allow_inf_nan=False)
    sell_fee_bps: float | None = Field(default=None, ge=0)
    sell_fee_per_share: float | None = Field(default=None, ge=0)
    limit_fill: LimitFill | None = None
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    orders_per_minute: int | None = Field(default=None, ge=1)
    requests_per_cancel: int | None = Field(default=None, ge=1)


class Costs(KansoModel):
    """A complete cost model: what the runner charges, once, to every fill, whether the
    venue fills a resting limit the market only touched (`limit_fill`), how long the venue
    takes to see an order (`latency_ms`, zero unless stated), how many of the account's
    requests a minute the sleeve's orders may spend (`orders_per_minute`, no limit unless
    stated), and how many of them a cancel of one order spends (`requests_per_cancel`, one
    unless stated); either, unstated, is left out of what the model dumps.

    `maker_bps` and `maker_per_share` are the charge on a fill the venue reports as a maker's,
    of its notional and per share, in place of all three of the others and of the per-share
    commission; negative is a rebate, and with both `None` a maker's fill is charged like any
    other. `commission_per_share` is charged per share on every fill that pays commission, on
    top. `slippage_ticks` is charged per share on a taker's fill, in the instrument's price
    increments, and no further than the order's limit allows.
    `sell_fee_bps` and `sell_fee_per_share` are charged on every sale, maker or taker, on top
    of everything else: the regulatory fees an account passes through on sells alone.
    """

    commission_bps: float = Field(ge=0)
    commission_per_share: float = Field(default=0.0, ge=0)
    slippage_bps: float = Field(ge=0)
    slippage_ticks: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    spread: Spread
    fixed_bps: float | None = Field(default=None, ge=0)
    maker_bps: float | None = Field(default=None, allow_inf_nan=False)
    maker_per_share: float | None = Field(default=None, allow_inf_nan=False)
    sell_fee_bps: float = Field(default=0.0, ge=0)
    sell_fee_per_share: float = Field(default=0.0, ge=0)
    limit_fill: LimitFill = DEFAULT_LIMIT_FILL
    latency_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    orders_per_minute: int | None = Field(default=None, ge=1)
    requests_per_cancel: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _fixed_bps_present(self) -> Costs:
        if self.spread == "fixed_bps" and self.fixed_bps is None:
            raise ValueError("fixed_bps: required when spread is fixed_bps")
        return self

    @model_validator(mode="after")
    def _cancel_fits_the_rate(self) -> Costs:
        short = _cancel_past_rate(self.orders_per_minute, self.requests_per_cancel)
        if short is not None:
            raise ValueError(short)
        return self

    @model_serializer(mode="wrap")
    def _unstated_rate_unwritten(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """The model as it dumps, without `orders_per_minute` or `requests_per_cancel` when
        no layer states it.

        Every card, certificate and strategy version records the venue model it was
        measured under, and a research run digests it into what a stored number was measured
        under (`kanso.research.loop.Setup.measured_under`). A model that states no rate dumps
        exactly as one did before the keys existed, so neither a record nor that digest moves
        for a workspace that states none.
        """
        dumped: dict[str, Any] = handler(self)
        for key in UNWRITTEN_UNSTATED:
            if getattr(self, key) is None:
                dumped.pop(key, None)
        return dumped


class VenueDeclaration(KansoModel):
    """What a broker declares for a venue it serves; every field is optional."""

    account: Account | None = None
    currency: Currency | None = None
    costs: CostsOverride | None = None


class VenueOverride(VenueDeclaration):
    """A `venues.<MIC>` entry in the portfolio: the operator's last word on a venue."""


class Origins(KansoModel):
    """Where each field of a resolved venue model came from."""

    account: Origin
    currency: Origin
    costs: Origin


class VenueModel(KansoModel):
    """The resolved trading model of one venue, recorded on every result it produced."""

    venue: VenueCode
    broker: NonEmpty | None = None
    account: Account
    default_leverage: float | None = Field(default=None, gt=0)
    currency: Currency
    costs: Costs
    origins: Origins

    @model_validator(mode="after")
    def _leverage_is_a_margin_notion(self) -> VenueModel:
        if self.account == "cash" and self.default_leverage is not None:
            raise ValueError("default_leverage: a cash account cannot borrow")
        return self


class ExecutionClientSpec(KansoModel):
    """How an execution client is funded and which clock it runs on."""

    id: NonEmpty
    capital: Funding
    clock: Clock


SANDBOX = ExecutionClientSpec(id="sandbox", capital="simulated", clock="replay")
"""The only execution client the framework itself provides; brokers declare their own."""


def _merge_costs(
    layers: list[tuple[Origin, CostsOverride | None]],
    *,
    venue: str,
    quotes_available: bool,
    limit_fill: LimitFill = DEFAULT_LIMIT_FILL,
) -> tuple[Costs, Origin]:
    values: dict[str, float | str | None] = {
        "commission_bps": DEFAULT_COMMISSION_BPS,
        "commission_per_share": 0.0,
        "slippage_bps": DEFAULT_SLIPPAGE_BPS,
        "spread": "quotes" if quotes_available else "fixed_bps",
        "fixed_bps": None,
        "maker_bps": None,
        "maker_per_share": None,
        "limit_fill": limit_fill,
        "latency_ms": 0.0,
    }
    origin: Origin = "default"
    for layer_origin, override in layers:
        if override is None:
            continue
        stated = override.model_dump(exclude_none=True)
        if stated:
            values.update(stated)
            origin = layer_origin
    if values["spread"] == "fixed_bps" and values["fixed_bps"] is None:
        raise ValidationError(
            "costs.fixed_bps: no value declared and no quotes to take a spread from; "
            "set it on the hypothesis costs or on the venue"
        )
    rate, per_cancel = values.get("orders_per_minute"), values.get("requests_per_cancel")
    short = _cancel_past_rate(rate, per_cancel, venue)
    if short is not None:
        raise ValidationError(
            short,
            remedy=f"state costs.orders_per_minute of at least {per_cancel} — the share of the "
            f"account's rate this sleeve may spend — in the hypothesis costs or under "
            f"venues.{venue}.costs in portfolio.yaml",
        )
    return Costs.model_validate(values), origin


def _cancel_past_rate(rate: object, per_cancel: object, venue: str | None = None) -> str | None:
    """Why a rate admits no cancel, or `None` when it admits one or either is unstated: a
    bucket of `rate` tokens holds no more than `rate`, so a cancel that spends more never finds
    them, and while it waits no entry or modify takes a token
    (`kanso.nautilus.strategy.KansoStrategy`). `venue` names where it was resolved."""
    if not isinstance(rate, int) or not isinstance(per_cancel, int) or rate >= per_cancel:
        return None
    where = "" if venue is None else f" on {venue}"
    return (
        f"costs.orders_per_minute: {rate}{where} is below costs.requests_per_cancel: "
        f"{per_cancel}; a bucket of {rate} token(s) never holds the {per_cancel} one cancel "
        "spends, so the sleeve's first cancel would wait for ever and every entry and modify "
        "behind it be dropped"
    )


def resolve_venue_model(
    venue: str,
    *,
    config: VenueDeclaration | None = None,
    broker: str | None = None,
    declaration: VenueDeclaration | None = None,
    override: VenueOverride | None = None,
    hypothesis_costs: CostsOverride | None = None,
    max_leverage: float | None = None,
    quotes_available: bool = True,
    default_limit_fill: LimitFill = DEFAULT_LIMIT_FILL,
) -> VenueModel:
    """Inherit a venue's model from the workspace configuration, the broker, the operator's
    override and the hypothesis, in that order of precedence.

    `default_limit_fill` is the rule no layer stating one leaves the model under, which the
    hypothesis decides (`kanso.hyp.validate.default_limit_fill`); it is a default, so the
    costs' origin stays where the layers leave it.

    `config` is what `[research]` states beyond the shipped defaults; it carries an account
    type and a currency and never a cost, so a broker's declared currency still wins over it
    and the operator's `venues.<MIC>` entry is the place to override the broker.
    """
    account: Account = DEFAULT_ACCOUNT
    account_origin: Origin = "default"
    currency = DEFAULT_CURRENCY
    currency_origin: Origin = "default"
    layers: tuple[tuple[Origin, VenueDeclaration | None], ...] = (
        ("config", config),
        ("broker", declaration),
        ("venue_override", override),
    )
    for layer_origin, layer in layers:
        if layer is None:
            continue
        if layer.account is not None:
            account, account_origin = layer.account, layer_origin
        if layer.currency is not None:
            currency, currency_origin = layer.currency, layer_origin
    cost_layers: list[tuple[Origin, CostsOverride | None]] = [
        ("broker", declaration.costs if declaration else None),
        ("venue_override", override.costs if override else None),
        ("hypothesis", hypothesis_costs),
    ]
    costs, costs_origin = _merge_costs(
        cost_layers,
        venue=venue,
        quotes_available=quotes_available,
        limit_fill=default_limit_fill,
    )
    return VenueModel(
        venue=venue,
        broker=broker,
        account=account,
        default_leverage=max_leverage if account == "margin" else None,
        currency=currency,
        costs=costs,
        origins=Origins(account=account_origin, currency=currency_origin, costs=costs_origin),
    )


def single_currency(models: dict[str, VenueModel]) -> str:
    """The one account currency of a universe; a universe spanning two is refused.

    Funding a multi-currency universe in one currency would silently price a leg at a rate
    nothing in the workspace records, so the hypothesis is refused instead.
    """
    if not models:
        raise ValidationError("universe: no venue resolved, so no account currency exists")
    by_currency: dict[str, list[str]] = {}
    for venue, model in sorted(models.items()):
        by_currency.setdefault(model.currency, []).append(venue)
    if len(by_currency) > 1:
        spans = ", ".join(f"{cur} ({', '.join(v)})" for cur, v in sorted(by_currency.items()))
        raise ValidationError(
            f"universe: spans more than one account currency: {spans}; "
            "a hypothesis trades one currency"
        )
    return next(iter(by_currency))


def check_execution_client(
    stage: str,
    spec: ExecutionClientSpec,
    *,
    data_client: str,
    speed: float,
    approved: bool = False,
) -> None:
    """Refuse the two pairings that would trade the wrong money or at the wrong price.

    Real capital off the live stage, or without a recorded approval, is an approval
    failure. A wall-clock execution client fed by historical replay, or run at anything
    but real time, is a precondition failure: the broker matches against current prices,
    so the fills would bear no relation to the data that triggered them.
    """
    if spec.capital == "real":
        if stage != "live":
            raise ApprovalError(
                f"stages.{stage}.exec: {spec.id!r} trades real capital and may be configured "
                "only on the live stage",
                remedy="move it to stages.live",
            )
        if not approved:
            raise ApprovalError(
                f"stages.{stage}.exec: {spec.id!r} trades real capital and needs a recorded "
                "operator approval",
                remedy="kanso promote <strategy> --live --as NAME",
            )
    if spec.clock == "wall":
        if data_client == REPLAY_DATA_CLIENT:
            raise PreconditionError(
                f"stages.{stage}.data: {spec.id!r} runs on the wall clock and needs a live data "
                f"client, not {REPLAY_DATA_CLIENT!r}",
            )
        if speed != 1:
            raise PreconditionError(
                f"stages.{stage}.speed: {spec.id!r} runs on the wall clock and needs speed 1, "
                f"not {speed}",
            )
