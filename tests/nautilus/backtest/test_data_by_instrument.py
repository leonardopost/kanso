"""A hypothesis may ask each instrument for its own types, and the runner loads those alone.

A universe that mixes sources holds a type for one instrument and not another: a crypto
exchange's prints beside the quotes of the equities that follow it. `data_by_instrument` says
so, and what an instrument is not asked for is neither required of a snapshot nor read.
"""

from __future__ import annotations

from pathlib import Path

from kanso.nautilus.backtest import window_data
from kanso.schemas import Hypothesis

from .conftest import INSTRUMENT, RESEARCH, catalog, hypothesis, instrument, quotes, trades

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
