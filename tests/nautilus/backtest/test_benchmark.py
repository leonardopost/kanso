"""The benchmark run: a hold of the first leg, produced by the runner from the subject's request.

What is under test is that the hold is a run like any other — the same window, prefix, money
and cost model as the request it is derived from, through the same engine — and that it holds
what it says: the first leg, bought on the first print it may trade on, never sold.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kanso.criteria.integrity import scan
from kanso.criteria.run import midnight_ns
from kanso.nautilus.backtest import HOLD, benchmark, run
from kanso.schemas import Hypothesis, Warmup

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
TWO_MONTHS = (date(2024, 1, 1), date(2024, 2, 29))
"""A window with a month end inside it, so a monthly reset has somewhere to fire."""


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


def booked(book: dict[str, object] | None) -> Hypothesis:
    """The benchmark hypothesis over two months, with a book policy or with none."""
    fields = hypothesis().model_dump(by_alias=True, mode="json")
    fields["benchmark"] = {"hold": "first_leg"}
    fields["windows"] = {
        "research": {"start": TWO_MONTHS[0].isoformat(), "end": TWO_MONTHS[1].isoformat()},
        "certification": {"start": "2024-03-08", "end": "2024-03-31"},
        "forward": {"start": "2024-04-01"},
    }
    if book is not None:
        fields["book"] = book
    return Hypothesis.model_validate(fields)


def test_the_hold_keeps_the_book_policy_of_the_subject(tmp_path: Path, request_for) -> None:
    """The hold's request is the subject's but for the strategy, so the hypothesis — and
    with it the book policy — rides across: the hold is reset at the turn of the month and
    carries what it borrows on the same terms. Measured on the saw-tooth over January and
    February: both holds are worth 105,981.015 at January's last end, and at February's
    first the policy returns the book to its capital and sets 4,982.515 aside, where the
    hold declaring none rides on at 104,982.515. A hold of a fifth of the book borrows
    nothing, so its carry is a series of zeros rather than no series at all."""
    store = catalog(tmp_path / "booked", bars(TWO_MONTHS), [instrument()])
    policy = {"reset": "monthly", "financing_rate_bps": 250.0}

    held = run(benchmark(request_for(TWO_MONTHS, hypothesis_=booked(policy))), store).run
    plain = run(benchmark(request_for(TWO_MONTHS, hypothesis_=booked(None))), store).run

    assert (plain.cushion, plain.carry) == ((), ()), "no policy, no series"
    assert len(held.cushion) == len(held.carry) == len(held.returns)
    assert held.equity[30] == pytest.approx(plain.equity[30])
    assert held.equity[31] == pytest.approx(held.capital), "January's gain moved out"
    assert held.cushion[31] == pytest.approx(plain.equity[31] - held.capital)


def test_the_shipped_hold_passes_the_integrity_scan() -> None:
    assert scan(HOLD.read_text(encoding="utf-8"), HOLD.name) == []
