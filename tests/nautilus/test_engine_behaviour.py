"""The engine behaviours kanso's correctness rests on, asserted directly.

`test_facts.py` checks that `verify()` reports these; this module states them
independently of that machinery, so the guarantees survive as executable prose
whatever happens to the fact list. Everything here runs offline against
nautilus_trader 1.231.0.
"""

from __future__ import annotations

import pytest
from nautilus_trader.backtest.engine import BacktestDataIterator
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, StrategyConfig
from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from nautilus_trader.trading.strategy import Strategy


def bar(ts_event: int, ts_init: int) -> Bar:
    bar_type = BarType(
        InstrumentId.from_str("AAPL.XNAS"),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
    return Bar(
        bar_type,
        Price.from_str("1.00"),
        Price.from_str("2.00"),
        Price.from_str("0.50"),
        Price.from_str("1.50"),
        Quantity.from_int(10),
        ts_event=ts_event,
        ts_init=ts_init,
    )


class _Strategy(StrategyConfig, frozen=True):
    pass


class _ActorC(ActorConfig, frozen=True):
    pass


def test_a_strategy_config_cannot_configure_an_actor() -> None:
    with pytest.raises(TypeError):
        Actor(config=_Strategy())


def test_an_actor_config_cannot_configure_a_strategy() -> None:
    with pytest.raises(TypeError):
        Strategy(config=_ActorC())


def test_matched_config_and_component_construct() -> None:
    assert Strategy(config=_Strategy()) is not None
    assert Actor(config=_ActorC()) is not None


def test_the_engine_delivers_data_in_ts_init_order_not_ts_event_order() -> None:
    # Availability, not the economic reference time, decides what a strategy sees.
    iterator = BacktestDataIterator()
    iterator.add_data("published_late", [bar(ts_event=10, ts_init=100)])
    iterator.add_data("published_early", [bar(ts_event=50, ts_init=20)])

    delivered = []
    while (item := iterator.next()) is not None:
        delivered.append((item.ts_event, item.ts_init))

    assert delivered == [(50, 20), (10, 100)]


def test_the_catalog_preserves_both_timestamps(tmp_path: object) -> None:
    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_data([bar(ts_event=1_000, ts_init=2_000)])

    (read,) = catalog.bars()

    assert (read.ts_event, read.ts_init) == (1_000, 2_000)


def test_the_catalog_is_the_instrument_store(tmp_path: object) -> None:
    from kanso.nautilus.facts import _sample_equity

    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_data([_sample_equity()])

    assert [i.id.value for i in catalog.instruments()] == ["AAPL.XNAS"]
