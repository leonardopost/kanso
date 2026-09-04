"""The venue model: account type, account currency and cost model, and where each came from.

A venue's trading model is inherited, never invented. The broker behind the configured
execution client declares it for the venues it serves; a per-venue entry in the portfolio
overrides any field; a hypothesis's own `costs` overrides the cost model for that
hypothesis alone. Where nothing is declared the shipped defaults apply: a margin account
whose leverage is the hypothesis's `max_leverage`, USD, zero commission and one basis
point of slippage, with the spread taken from quotes when quotes are available.

The resolved model records the origin of every field, so a number on a card, a certificate
or a strategy version is always traceable to the venue that produced it.

An execution client declares how its capital is funded and which clock it runs on. Those
two declarations are what forbid real money off the live stage and a historical replay
feeding a broker that matches against current prices.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from kanso.errors import ApprovalError, PreconditionError, ValidationError
from kanso.schemas.base import KansoModel, NonEmpty

Account = Literal["margin", "cash"]
Spread = Literal["quotes", "fixed_bps"]
Origin = Literal["default", "broker", "venue_override", "hypothesis"]
Funding = Literal["simulated", "broker_paper", "real"]
Clock = Literal["replay", "wall"]

VenueCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,16}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

DEFAULT_ACCOUNT: Account = "margin"
DEFAULT_CURRENCY = "USD"
DEFAULT_COMMISSION_BPS = 0.0
DEFAULT_SLIPPAGE_BPS = 1.0
REPLAY_DATA_CLIENT = "replay"
"""The catalog replay data client; every other data client id belongs to an adapter."""


class CostsOverride(KansoModel):
    """Any subset of a cost model, as a hypothesis or a venue entry may state it."""

    commission_bps: float | None = Field(default=None, ge=0)
    slippage_bps: float | None = Field(default=None, ge=0)
    spread: Spread | None = None
    fixed_bps: float | None = Field(default=None, ge=0)


class Costs(KansoModel):
    """A complete cost model: what the runner applies, once, to every fill."""

    commission_bps: float = Field(ge=0)
    slippage_bps: float = Field(ge=0)
    spread: Spread
    fixed_bps: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _fixed_bps_present(self) -> Costs:
        if self.spread == "fixed_bps" and self.fixed_bps is None:
            raise ValueError("fixed_bps: required when spread is fixed_bps")
        return self


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
    quotes_available: bool,
) -> tuple[Costs, Origin]:
    values: dict[str, float | str | None] = {
        "commission_bps": DEFAULT_COMMISSION_BPS,
        "slippage_bps": DEFAULT_SLIPPAGE_BPS,
        "spread": "quotes" if quotes_available else "fixed_bps",
        "fixed_bps": None,
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
    return Costs.model_validate(values), origin


def resolve_venue_model(
    venue: str,
    *,
    broker: str | None = None,
    declaration: VenueDeclaration | None = None,
    override: VenueOverride | None = None,
    hypothesis_costs: CostsOverride | None = None,
    max_leverage: float | None = None,
    quotes_available: bool = True,
) -> VenueModel:
    """Inherit a venue's model from the broker, the operator's override and the hypothesis."""
    account: Account = DEFAULT_ACCOUNT
    account_origin: Origin = "default"
    currency = DEFAULT_CURRENCY
    currency_origin: Origin = "default"
    layers: tuple[tuple[Origin, VenueDeclaration | None], ...] = (
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
        quotes_available=quotes_available,
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
