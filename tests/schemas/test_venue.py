"""The venue model, its inheritance chain and the execution-client declarations."""

from __future__ import annotations

import pytest

from kanso.errors import ApprovalError, Exit, PreconditionError, ValidationError
from kanso.schemas import (
    SANDBOX,
    Costs,
    CostsOverride,
    ExecutionClientSpec,
    VenueDeclaration,
    VenueModel,
    VenueOverride,
    check_execution_client,
    resolve_venue_model,
    single_currency,
)


def test_the_shipped_defaults() -> None:
    model = resolve_venue_model("XNAS", max_leverage=1.0)
    assert model.account == "margin"
    assert model.default_leverage == 1.0
    assert model.currency == "USD"
    assert model.costs == Costs(commission_bps=0, slippage_bps=1.0, spread="quotes")
    assert model.origins.account == "default"
    assert model.origins.currency == "default"
    assert model.origins.costs == "default"


def test_without_quotes_a_fixed_spread_needs_a_width() -> None:
    with pytest.raises(ValidationError) as caught:
        resolve_venue_model("XNAS", quotes_available=False)
    assert "costs.fixed_bps" in caught.value.message
    assert caught.value.code is Exit.VALIDATION
    model = resolve_venue_model(
        "XNAS",
        quotes_available=False,
        hypothesis_costs=CostsOverride(fixed_bps=2),
    )
    assert model.costs.spread == "fixed_bps"
    assert model.costs.fixed_bps == 2


def test_the_broker_declares_and_the_operator_overrides() -> None:
    model = resolve_venue_model(
        "XNAS",
        broker="house",
        declaration=VenueDeclaration(
            account="cash", currency="EUR", costs=CostsOverride(commission_bps=3)
        ),
        override=VenueOverride(currency="GBP"),
    )
    assert model.broker == "house"
    assert model.account == "cash"
    assert model.origins.account == "broker"
    assert model.currency == "GBP"
    assert model.origins.currency == "venue_override"
    assert model.costs.commission_bps == 3
    assert model.origins.costs == "broker"
    assert model.default_leverage is None


def test_a_hypothesis_overrides_only_the_costs() -> None:
    model = resolve_venue_model(
        "XNAS",
        declaration=VenueDeclaration(currency="EUR", costs=CostsOverride(slippage_bps=0.5)),
        hypothesis_costs=CostsOverride(slippage_bps=9.0),
        max_leverage=2.0,
    )
    assert model.costs.slippage_bps == 9.0
    assert model.origins.costs == "hypothesis"
    assert model.currency == "EUR"


def test_the_limit_fill_rule_is_inherited_and_overridden_like_any_cost() -> None:
    assert resolve_venue_model("XNAS").costs.limit_fill == "touch"
    declared = VenueDeclaration(costs=CostsOverride(limit_fill="through"))

    inherited = resolve_venue_model("XNAS", declaration=declared)
    restated = resolve_venue_model(
        "XNAS",
        declaration=declared,
        override=VenueOverride(costs=CostsOverride(slippage_bps=2.0)),
        hypothesis_costs=CostsOverride(limit_fill="touch"),
    )

    assert (inherited.costs.limit_fill, inherited.origins.costs) == ("through", "broker")
    assert (restated.costs.limit_fill, restated.origins.costs) == ("touch", "hypothesis")
    assert restated.costs.slippage_bps == 2.0
    with pytest.raises(ValidationError, match="limit_fill"):
        CostsOverride.model_validate({"limit_fill": "sometimes"})


def test_a_venue_model_recorded_before_the_limit_fill_rule_reads_as_touch() -> None:
    """Every card, certificate and version pinned before the key fills a touched limit."""
    recorded = resolve_venue_model("XNAS").model_dump()
    del recorded["costs"]["limit_fill"]

    assert VenueModel.model_validate(recorded).costs.limit_fill == "touch"


def test_a_cash_account_cannot_borrow() -> None:
    with pytest.raises(ValidationError, match="default_leverage"):
        VenueModel(
            venue="XNAS",
            account="cash",
            default_leverage=2.0,
            currency="USD",
            costs=Costs(commission_bps=0, slippage_bps=1, spread="quotes"),
            origins={"account": "default", "currency": "default", "costs": "default"},
        )


def test_a_single_currency_universe_resolves() -> None:
    models = {
        venue: resolve_venue_model(venue, override=VenueOverride(currency="USD"))
        for venue in ("XNAS", "XNYS")
    }
    assert single_currency(models) == "USD"


def test_a_mixed_currency_universe_is_refused() -> None:
    models = {
        "XNAS": resolve_venue_model("XNAS", override=VenueOverride(currency="USD")),
        "XLON": resolve_venue_model("XLON", override=VenueOverride(currency="GBP")),
    }
    with pytest.raises(ValidationError) as caught:
        single_currency(models)
    assert caught.value.code is Exit.VALIDATION
    assert "more than one account currency" in caught.value.message
    assert "USD (XNAS)" in caught.value.message
    assert "GBP (XLON)" in caught.value.message


def test_an_empty_universe_has_no_currency() -> None:
    with pytest.raises(ValidationError, match="no venue resolved"):
        single_currency({})


def test_the_sandbox_is_simulated_and_replayed() -> None:
    assert SANDBOX.capital == "simulated"
    assert SANDBOX.clock == "replay"
    check_execution_client("paper", SANDBOX, data_client="replay", speed=0)


def test_real_capital_belongs_on_the_live_stage() -> None:
    real = ExecutionClientSpec(id="house", capital="real", clock="wall")
    with pytest.raises(ApprovalError) as caught:
        check_execution_client("paper", real, data_client="feed", speed=1)
    assert caught.value.code is Exit.APPROVAL
    with pytest.raises(ApprovalError, match="approval"):
        check_execution_client("live", real, data_client="feed", speed=1)
    check_execution_client("live", real, data_client="feed", speed=1, approved=True)


def test_a_wall_clock_client_needs_a_live_feed_at_real_time() -> None:
    broker_paper = ExecutionClientSpec(id="house_paper", capital="broker_paper", clock="wall")
    with pytest.raises(PreconditionError) as caught:
        check_execution_client("paper", broker_paper, data_client="replay", speed=1)
    assert caught.value.code is Exit.PRECONDITION
    assert "live data client" in caught.value.message
    with pytest.raises(PreconditionError, match="speed 1"):
        check_execution_client("paper", broker_paper, data_client="feed", speed=0)
    check_execution_client("paper", broker_paper, data_client="feed", speed=1)


def test_costs_refuse_a_fixed_spread_without_a_width() -> None:
    with pytest.raises(ValidationError, match="fixed_bps"):
        Costs(commission_bps=0, slippage_bps=1, spread="fixed_bps")
