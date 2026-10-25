"""A hypothesis may ask each instrument for its own types, and the runner loads those alone.

A universe that mixes sources holds a type for one instrument and not another: a crypto
exchange's prints beside the quotes of the equities that follow it. `data_by_instrument` says
so, and what an instrument is not asked for is neither required of a snapshot nor read.
"""

from __future__ import annotations

from pathlib import Path

from kanso.hyp.validate import default_limit_fill
from kanso.nautilus.backtest import RunRequest, execute, window_data
from kanso.schemas import Hypothesis, resolve_venue_model
from kanso.schemas.venue import CostsOverride

from .conftest import (
    CAPITAL,
    INSTRUMENT,
    RESEARCH,
    SNAPSHOT,
    VENUE,
    bars,
    catalog,
    hypothesis,
    instrument,
    quotes,
    trades,
)

OTHER = "OTHR.XNAS"


def mixed() -> Hypothesis:
    base = hypothesis(
        universe=(INSTRUMENT, OTHER), data_requirements=("quote", "trade"), resolution="trade"
    )
    return base.model_copy(update={"data_by_instrument": {OTHER: ["trade"]}})


def test_the_runner_reads_each_instrument_only_what_it_is_asked_for(
    request_for, tmp_path: Path
) -> None:
    held = catalog(
        tmp_path / "catalog",
        [
            *quotes(RESEARCH),
            *trades(RESEARCH),
            *quotes(RESEARCH, "OTHR"),
            *trades(RESEARCH, "OTHR"),
        ],
        [instrument(), instrument("OTHR")],
    )

    _, groups = window_data(request_for(hypothesis_=mixed()), held)

    loaded = {
        (type(point).__name__, str(point.instrument_id)) for group in groups for point in group
    }
    assert ("QuoteTick", OTHER) not in loaded
    assert {("TradeTick", OTHER), ("QuoteTick", INSTRUMENT), ("TradeTick", INSTRUMENT)} <= loaded


def test_an_instrument_not_asked_for_bars_has_none_read(request_for, tmp_path: Path) -> None:
    held = catalog(
        tmp_path / "catalog",
        [*bars(RESEARCH), *trades(RESEARCH), *bars(RESEARCH, "OTHR"), *trades(RESEARCH, "OTHR")],
        [instrument(), instrument("OTHR")],
    )
    base = hypothesis(universe=(INSTRUMENT, OTHER), data_requirements=("bar", "trade"))
    asked = base.model_copy(update={"data_by_instrument": {OTHER: ["trade"]}})

    _, groups = window_data(request_for(hypothesis_=asked), held)

    loaded = {
        (type(point).__name__, str(getattr(point, "bar_type", point).instrument_id))
        for group in groups
        for point in group
    }
    assert ("Bar", OTHER) not in loaded
    assert {("TradeTick", OTHER), ("Bar", INSTRUMENT), ("TradeTick", INSTRUMENT)} <= loaded


TAKES_ON_PRINTS = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_quote_tick(self, q):
        pass

    def on_trade_tick(self, t):
        self.seen += 1
        if self.seen == 2:
            self.submit_entry(t.instrument_id, "BUY", qty=100)
        elif self.seen == 6:
            self.submit_exit(t.instrument_id)
"""


def test_a_name_asked_for_prints_alone_is_taken_under_the_rule_no_layer_states() -> None:
    """A tick hypothesis reading one name's quotes and another's prints, trading the second at
    market: under the print rules a taker fills only on a quote in force, so in a name with no
    quotes it never fills. The rule no layer states is `touch` here, as it always was, and the
    entry and the exit both fill; resolved to `print_through` the run had no fill at all."""
    traded = hypothesis(
        universe=(INSTRUMENT, OTHER),
        data_requirements=("quote", "trade"),
        resolution="tick",
        costs={
            "commission_bps": 0.0,
            "slippage_bps": 0.0,
            "spread": "fixed_bps",
            "fixed_bps": 0.0,
            "latency_ms": 20.0,
        },
    )
    hyp = traded.model_copy(
        update={"data_by_instrument": {INSTRUMENT: ["quote"], OTHER: ["trade"]}}
    )
    assert hyp.costs is not None
    model = resolve_venue_model(
        VENUE,
        broker="synthetic",
        hypothesis_costs=CostsOverride.model_validate(hyp.costs.model_dump(exclude_none=True)),
        max_leverage=hyp.risk_limits.max_leverage,
        default_limit_fill=default_limit_fill(hyp),
    )
    request = RunRequest(
        hyp=hyp,
        strategy_source=TAKES_ON_PRINTS,
        window=RESEARCH,
        snapshot_id=SNAPSHOT,
        venue_model=model.model_dump(),
        capital=CAPITAL,
    )

    run = execute(
        request,
        [instrument(), instrument("OTHR")],
        [tuple(quotes(RESEARCH)), tuple(trades(RESEARCH, "OTHR"))],
    ).run

    assert [(f.side, f.instrument_id) for f in run.fills] == [("BUY", OTHER), ("SELL", OTHER)]
    assert model.costs.limit_fill == "touch"
