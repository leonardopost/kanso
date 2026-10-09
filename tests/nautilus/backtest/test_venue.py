"""The simulated venues a run is given, and the cost model they deliberately lack."""

from __future__ import annotations

from typing import get_args

import pytest
from nautilus_trader.backtest.node import get_fill_model

from kanso.errors import ValidationError
from kanso.nautilus.venue import (
    LIMIT_FILL,
    NETTING,
    PRINT_SIZE,
    fill_model,
    known_currency,
    latency_model,
    starting_balance,
    venue_config,
    venue_configs,
    venues_of,
)
from kanso.schemas import Hypothesis, LimitFill, VenueModel

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
    # The whole point: a fee or latency model here, or a fill model that slipped, would be a
    # second application of a cost the extraction already applies.
    (config,) = venue_configs(hyp, venue_model(hyp), CAPITAL)

    assert (config.fee_model, config.latency_model) == (None, None)
    assert config.fill_model is not None
    assert config.fill_model.config["prob_slippage"] == 0.0


def test_a_touched_limit_fills_unless_the_venue_model_says_through() -> None:
    touching = hypothesis()
    through = hypothesis(
        costs={"spread": "fixed_bps", "fixed_bps": 4.0, "limit_fill": "through"},
    )

    (default,) = venue_configs(touching, venue_model(touching), CAPITAL)
    (stated,) = venue_configs(through, venue_model(through), CAPITAL)

    assert default.fill_model == fill_model("touch")
    assert get_fill_model(default).prob_fill_on_limit == 1.0
    assert stated.fill_model == fill_model("through")
    assert get_fill_model(stated).prob_fill_on_limit == 0.0
    assert set(LIMIT_FILL) | set(PRINT_SIZE) == set(get_args(LimitFill))
    assert not set(LIMIT_FILL) & set(PRINT_SIZE)


@pytest.mark.parametrize(
    ("rule", "size"), [("print_through", "print"), ("print_through_whole", "whole")]
)
def test_a_print_rule_builds_kanso_s_own_fill_model(rule: str, size: str) -> None:
    """Under a print rule the venue's fill model is `kanso.nautilus.tape.PrintThrough`, sized
    by the print or whole, with a limit probability of one and no slippage; it draws nothing."""
    from kanso.nautilus.tape import PrintThrough

    ruled = hypothesis(
        resolution="tick",
        data_requirements=["quote", "trade"],
        costs={"spread": "fixed_bps", "fixed_bps": 4.0, "limit_fill": rule},
    )

    (config,) = venue_configs(ruled, venue_model(ruled), CAPITAL)
    built = get_fill_model(config)

    assert config.fill_model == fill_model(rule)  # type: ignore[arg-type]
    assert config.fill_model.config == {
        "prob_fill_on_limit": 1.0,
        "prob_slippage": 0.0,
        "size": size,
    }
    assert isinstance(built, PrintThrough)
    assert (built.prob_fill_on_limit, built.prob_slippage) == (1.0, 0.0)


def test_a_stated_latency_delays_every_command_by_that_much_and_none_is_no_model() -> None:
    """A latency model only when the venue model states one: zero configures none at all."""
    from nautilus_trader.backtest.node import get_latency_model

    quiet = hypothesis()
    slow = hypothesis(costs={"spread": "fixed_bps", "fixed_bps": 4.0, "latency_ms": 20})

    (default,) = venue_configs(quiet, venue_model(quiet), CAPITAL)
    (stated,) = venue_configs(slow, venue_model(slow), CAPITAL)

    assert default.latency_model is None
    assert stated.latency_model == latency_model(20)
    built = get_latency_model(stated)
    assert built.base_latency_nanos == 20_000_000
    # the engine reads the base for every command kind left at zero
    assert (
        built.insert_latency_nanos,
        built.update_latency_nanos,
        built.cancel_latency_nanos,
    ) == (20_000_000, 20_000_000, 20_000_000)
    assert latency_model(0) is None


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


def test_one_function_builds_the_venue_both_paths_are_configured_from() -> None:
    """`venue_config` sets every field a card's venue carries: the latency, the book, the
    queue position, the fill model and the fee model. That a stage's venue equals a card's
    is `tests/portfolio/test_node.py`'s to show."""
    hyp = hypothesis(
        max_leverage=3.0,
        costs={"spread": "fixed_bps", "fixed_bps": 4.0, "latency_ms": 50, "limit_fill": "through"},
    )
    model = VenueModel.model_validate(venue_model(hyp))

    built = venue_config("XNAS", model, CAPITAL, 3.0, book=False)

    assert built.name == "XNAS"
    assert (built.oms_type, built.account_type, built.base_currency) == (NETTING, "MARGIN", "USD")
    assert (built.starting_balances, built.default_leverage) == (["100000.00 USD"], 3.0)
    assert (built.bar_execution, built.trade_execution) == (True, True)
    assert (built.book_type, built.queue_position) == ("L1_MBP", False)
    assert built.fill_model == fill_model("through")
    assert built.fee_model is None, "the runner charges once"
    assert built.latency_model == latency_model(50)
    deep = venue_config("XNAS", model, CAPITAL, 3.0, book=True)
    assert (deep.book_type, deep.queue_position) == ("L2_MBP", True)
    assert {k: v for k, v in deep.dict().items() if k not in ("book_type", "queue_position")} == {
        k: v for k, v in built.dict().items() if k not in ("book_type", "queue_position")
    }, "the book is the only thing `book` changes"


def test_a_currency_the_engine_registers_funds_at_the_engine_s_own_precision() -> None:
    """Measured on nautilus_trader 1.231.0: `Money(100000, USDT)` renders eight decimals,
    where USD renders two; kanso adds no precision of its own."""
    from nautilus_trader.model.objects import Money

    known_currency("USD")
    known_currency("USDT")
    rendered = starting_balance(100_000, "USDT")

    assert rendered == "100000.00000000 USDT"
    assert Money.from_str(rendered).as_double() == pytest.approx(100_000)


@pytest.mark.parametrize("code", ["FOOBAR", "usdt", "USTD"])
def test_a_currency_the_engine_does_not_register_is_refused_before_it_is_minted(
    hyp: Hypothesis, code: str
) -> None:
    """Measured on nautilus_trader 1.231.0: `Currency.from_str("FOOBAR")` raises nothing and
    mints a crypto currency at precision 8, so the refusal has to come before it is asked."""
    with pytest.raises(ValidationError) as caught:
        starting_balance(100_000, code)
    assert repr(code) in caught.value.message
    assert caught.value.remedy == (
        "set [research] currency in kanso.toml or venues.<MIC>.currency in portfolio.yaml "
        "to a code the engine registers"
    )

    with pytest.raises(ValidationError, match="not a code the engine registers"):
        venue_configs(hyp, {**venue_model(hyp), "currency": "FOOBAR"}, CAPITAL)
