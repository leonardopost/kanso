"""The two config bases, and what a perturbation gate may touch."""

from __future__ import annotations

import pytest
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, StrategyConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from kanso.errors import Exit, ValidationError
from kanso.nautilus.strategy import (
    KansoConfig,
    KansoModifier,
    KansoModifierConfig,
    KansoStrategy,
    _bar_type,
    tunable_fields,
)


class Params(KansoConfig):
    lookback: int = 20
    threshold: float = 1.5
    invert: bool = False
    label: str = "x"


class Sleeve(KansoStrategy):
    config_cls = Params


class Attached(KansoModifierConfig):
    pass


class Gate(KansoModifier):
    construct = "filter"
    config_cls = Attached


def test_the_two_bases_are_the_engine_s_two_bases() -> None:
    assert issubclass(KansoConfig, StrategyConfig)
    assert issubclass(KansoModifierConfig, ActorConfig)
    assert not issubclass(KansoModifierConfig, StrategyConfig)


def test_a_sleeve_is_a_strategy_and_a_modifier_is_an_actor() -> None:
    assert issubclass(KansoStrategy, Strategy)
    assert issubclass(KansoModifier, Actor)


def test_an_author_s_config_inherits_the_freeze() -> None:
    config = Params(lookback=5)
    with pytest.raises(AttributeError):
        config.lookback = 6  # type: ignore[misc]


def test_the_hypothesis_fields_are_what_a_sleeve_is_given() -> None:
    config = Params(
        hyp_id="demo_mr",
        universe=("DEMO.XNAS",),
        resolution="1m",
        data_requirements=("bar",),
        capital=50_000.0,
        max_position_pct=20.0,
        max_drawdown_pct=15.0,
        max_leverage=1.0,
        venue_model={"venue": "XNAS"},
    )
    sleeve = Sleeve(config)
    assert sleeve.kanso_config is config
    assert sleeve.universe == (InstrumentId.from_str("DEMO.XNAS"),)
    assert sleeve.capital == 50_000.0
    assert sleeve.venue_model == {"venue": "XNAS"}
    assert sleeve.max_notional == 10_000.0
    assert sleeve.gross_limit == 50_000.0
    assert sleeve.data_time == 0
    assert sleeve.intents == ()


def test_a_sleeve_without_a_config_takes_its_class_s_default() -> None:
    assert Sleeve().kanso_config.lookback == 20


def test_only_the_author_s_numeric_fields_are_perturbable() -> None:
    assert tunable_fields(Params(capital=1.0)) == ("lookback", "threshold")


def test_a_bare_kanso_config_has_nothing_to_perturb() -> None:
    assert tunable_fields(KansoConfig()) == ()


def test_a_sleeve_refuses_a_config_that_is_not_a_kanso_config() -> None:
    class Bare(StrategyConfig, frozen=True):
        pass

    with pytest.raises(ValidationError) as raised:
        KansoStrategy(Bare())  # type: ignore[arg-type]
    assert raised.value.code == Exit.VALIDATION
    assert "KansoConfig" in raised.value.message


def test_a_modifier_refuses_a_strategy_config_loudly() -> None:
    # The engine would raise a bare TypeError; the modifier says which base is wrong.
    with pytest.raises(ValidationError) as raised:
        Gate(Params())  # type: ignore[arg-type]
    assert raised.value.code == Exit.VALIDATION
    assert "KansoModifierConfig" in raised.value.message


def test_a_modifier_refuses_a_construct_that_does_not_attach() -> None:
    class Sleeved(KansoModifier):
        construct = "sleeve"
        config_cls = Attached

    with pytest.raises(ValidationError, match="not an attachable construct"):
        Sleeved(Attached(host_strategy_id="S-000"))


def test_an_unrendered_template_construct_is_refused() -> None:
    class Unrendered(KansoModifier):
        construct = "{{construct}}"
        config_cls = Attached

    with pytest.raises(ValidationError, match="not an attachable construct"):
        Unrendered(Attached(host_strategy_id="S-000"))


def test_a_modifier_must_name_the_sleeve_it_attaches_to() -> None:
    with pytest.raises(ValidationError, match="host_strategy_id"):
        Gate(Attached())


def test_a_modifier_carries_its_host_and_its_config() -> None:
    config = Attached(host_strategy_id="Sleeve", hyp_id="demo_filter")
    gate = Gate(config)
    assert gate.host_strategy_id == "Sleeve"
    assert gate.modifier_config is config


def test_a_default_modifier_config_names_no_host() -> None:
    class Defaulted(KansoModifier):
        construct = "filter"
        config_cls = Attached

    with pytest.raises(ValidationError, match="host_strategy_id"):
        Defaulted()


def test_a_bar_resolution_becomes_an_external_bar_type() -> None:
    instrument_id = InstrumentId.from_str("DEMO.XNAS")
    assert str(_bar_type(instrument_id, "1m")) == "DEMO.XNAS-1-MINUTE-LAST-EXTERNAL"
    assert str(_bar_type(instrument_id, "2h")) == "DEMO.XNAS-2-HOUR-LAST-EXTERNAL"
    assert str(_bar_type(instrument_id, "30s")) == "DEMO.XNAS-30-SECOND-LAST-EXTERNAL"
    assert str(_bar_type(instrument_id, "1d")) == "DEMO.XNAS-1-DAY-LAST-EXTERNAL"
    assert str(_bar_type(instrument_id, "1w")) == "DEMO.XNAS-1-WEEK-LAST-EXTERNAL"


def test_an_unaggregated_resolution_names_no_bar_type() -> None:
    with pytest.raises(ValidationError, match="not a bar size"):
        _bar_type(InstrumentId.from_str("DEMO.XNAS"), "quote")
