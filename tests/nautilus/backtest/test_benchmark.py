"""The benchmark run: a hold of the first leg, produced by the runner from the subject's request.

What is under test is that the hold is a run like any other — the same window, prefix, money
and cost model as the request it is derived from, through the same engine — and that it holds
what it says: the first leg, bought on the first print it may trade on, never sold.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kanso.criteria.integrity import scan
from kanso.criteria.run import midnight_ns
from kanso.nautilus.backtest import HOLD, benchmark, run
from kanso.schemas import Warmup

from .conftest import (
    CAPITAL,
    CERTIFICATION,
    FILTER_MODIFIER,
    INSTRUMENT,
    RESEARCH,
    bars,
    catalog,
    hypothesis,
    instrument,
)

OTHER = "OTHR.XNAS"
DECEMBER = (date(2023, 12, 1), date(2023, 12, 31))


def test_the_benchmark_keeps_everything_the_subject_pins_but_the_strategy(request_for) -> None:
    subject = request_for(
        modifiers=(("filter", FILTER_MODIFIER, {"allow": True}),),
        overrides={"every": 3.0},
        budget_s=30.0,
        mem_cap_gb=1.0,
        period="1d",
        grains=("1d",),
        sleeve_budget=5_000.0,
        prefix=(date(2023, 12, 27), date(2023, 12, 31)),
    )

    held = benchmark(subject)

    assert held.strategy_source == HOLD.read_bytes()
    assert (held.modifiers, dict(held.overrides), held.budget_s, held.mem_cap_gb) == (
        (),
        {},
        None,
        None,
    )
    assert (held.hyp, held.window, held.prefix, held.snapshot_id) == (
        subject.hyp,
        subject.window,
        subject.prefix,
        subject.snapshot_id,
    )
    assert (held.venue_model, held.capital, held.period, held.grains, held.sleeve_budget) == (
        subject.venue_model,
        subject.capital,
        subject.period,
        subject.grains,
        subject.sleeve_budget,
    )


def test_the_hold_buys_the_first_leg_once_and_never_sells(tmp_path: Path, request_for) -> None:
    """Two names print every day; only the first of the universe is ever bought."""
    hyp = hypothesis(universe=(INSTRUMENT, OTHER))
    points = [*bars(RESEARCH), *bars(RESEARCH, symbol="OTHR"), *bars(CERTIFICATION)]
    store = catalog(tmp_path / "two", points, [instrument(), instrument("OTHR")])

    result = run(benchmark(request_for(hypothesis_=hyp)), store)

    (entry,) = result.run.fills
    assert (entry.instrument_id, entry.side) == (INSTRUMENT, "BUY")
    assert result.run.trades == ()
    assert {item.instrument_id for item in result.run.held} == {INSTRUMENT}
    first_close = bars(RESEARCH)[0].ts_init
    assert entry.ts_ns <= bars(RESEARCH)[1].ts_init
    assert entry.ts_ns >= first_close
    # The whole room the risk limits leave: max_position_pct of the book, less the round trip.
    assert entry.qty * entry.px <= CAPITAL * 0.20
    assert entry.qty * entry.px > CAPITAL * 0.19


def test_a_warmed_hold_buys_on_the_window_s_first_print(tmp_path: Path, request_for) -> None:
    """The prefix drops the hold's order as it drops the strategy's, so both start at the open."""
    hyp = hypothesis().model_copy(update={"warmup": Warmup(sessions=5)})
    points = [*bars(DECEMBER), *bars(RESEARCH), *bars(CERTIFICATION)]
    store = catalog(tmp_path / "deep", points, [instrument()])

    result = run(
        benchmark(request_for(hypothesis_=hyp, prefix=(date(2023, 12, 27), date(2023, 12, 31)))),
        store,
    )

    (entry,) = result.run.fills
    assert entry.ts_ns >= midnight_ns(RESEARCH[0])
    assert result.run.period_ends_ns[0] >= midnight_ns(RESEARCH[0])


def test_the_shipped_hold_passes_the_integrity_scan() -> None:
    assert scan(HOLD.read_text(encoding="utf-8"), HOLD.name) == []
