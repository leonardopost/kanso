"""The simulated venues a run is given, and the cost model they deliberately lack."""

from __future__ import annotations

import pytest

from kanso.errors import ValidationError
from kanso.nautilus.venue import NETTING, starting_balance, venue_configs, venues_of
from kanso.schemas import Hypothesis

from .conftest import CAPITAL, INSTRUMENT, hypothesis, venue_model


def test_one_venue_per_venue_of_the_universe_in_a_stable_order() -> None:
    hyp = hypothesis(universe=["ZZZ.XNYS", INSTRUMENT, "AAA.XNYS"])

    configs = venue_configs(hyp, venue_model(hyp), CAPITAL)

    assert [config.name for config in configs] == ["XNAS", "XNYS"]


def test_the_venue_is_netting_with_bar_execution(hyp: Hypothesis) -> None:
    (config,) = venue_configs(hyp, venue_model(hyp), CAPITAL)

    assert config.oms_type == NETTING
    assert config.bar_execution is True


def test_the_venue_charges_nothing_because_the_runner_charges_once(hyp: Hypothesis) -> None:
    # The whole point: a fee, fill or latency model here would be a second application of
    # a cost the extraction already applies.
    (config,) = venue_configs(hyp, venue_model(hyp), CAPITAL)

    assert (config.fee_model, config.fill_model, config.latency_model) == (None, None, None)


def test_a_margin_account_carries_the_hypothesis_leverage() -> None:
    hyp = hypothesis(max_leverage=3.0)

    (config,) = venue_configs(hyp, venue_model(hyp), CAPITAL)

    assert config.account_type == "MARGIN"
    assert config.default_leverage == 3.0


def test_a_cash_account_cannot_borrow_whatever_the_hypothesis_asks() -> None:
    hyp = hypothesis(max_leverage=4.0)
    model = venue_model(hyp)
    model["account"] = "cash"
    model["default_leverage"] = None

    (config,) = venue_configs(hyp, model, CAPITAL)

    assert config.account_type == "CASH"
    assert config.default_leverage == 1.0


def test_the_account_is_funded_in_its_own_currency_at_its_own_precision(
    hyp: Hypothesis,
) -> None:
    (config,) = venue_configs(hyp, venue_model(hyp), CAPITAL)

    assert config.starting_balances == ["100000.00 USD"]
    assert config.base_currency == "USD"


def test_the_balance_string_is_what_the_engine_parses() -> None:
    from nautilus_trader.model.objects import Money

    rendered = starting_balance(12_345.5, "USD")

    assert rendered == "12345.50 USD"
    assert Money.from_str(rendered).as_double() == pytest.approx(12_345.5)


def test_a_universe_id_without_a_venue_is_refused() -> None:
    with pytest.raises(ValidationError, match="qualified instrument id"):
        venues_of(["DEMO"])


def test_capital_that_is_not_money_is_refused(hyp: Hypothesis) -> None:
    with pytest.raises(ValidationError, match="not an amount to fund"):
        venue_configs(hyp, venue_model(hyp), 0.0)


def test_capital_the_engine_cannot_represent_is_refused(hyp: Hypothesis) -> None:
    with pytest.raises(ValidationError, match="cannot fund an account|is not an amount of USD"):
        venue_configs(hyp, venue_model(hyp), 1e30)


def test_a_resolved_model_object_is_accepted_as_readily_as_its_mapping(hyp: Hypothesis) -> None:
    from kanso.schemas import VenueModel

    mapping = venue_model(hyp)
    model = VenueModel.model_validate(mapping)

    assert venue_configs(hyp, model, CAPITAL) == venue_configs(hyp, mapping, CAPITAL)
