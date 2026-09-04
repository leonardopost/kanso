"""The evaluation subject and its fold arithmetic."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kanso.criteria import CardRun
from kanso.criteria.run import NS_PER_DAY, day_of, midnight_ns
from kanso.errors import ValidationError
from tests.criteria.builders import at, build_run, fill, trade


def test_a_day_and_its_midnight_are_inverses() -> None:
    for day in (date(1970, 1, 1), date(2024, 2, 29), date(2025, 12, 31)):
        assert day_of(midnight_ns(day)) == day
        assert day_of(midnight_ns(day) + NS_PER_DAY - 1) == day


def test_a_run_reports_its_window_as_a_half_open_span() -> None:
    run = build_run((1.0, 2.0))
    assert run.bounds == (midnight_ns(date(2024, 1, 1)), midnight_ns(date(2024, 1, 3)))


def test_a_fill_and_a_trade_report_their_notional() -> None:
    assert fill(date(2024, 1, 1), qty=-100.0, px=12.5).notional == 1250.0
    assert trade(date(2024, 1, 1), pnl=10.0, notional=5_000.0).notional == 5_000.0


def test_a_run_refuses_series_of_different_lengths() -> None:
    with pytest.raises(ValidationError, match="parallel series"):
        CardRun(
            window=(date(2024, 1, 1), date(2024, 1, 2)),
            period="1d",
            period_ends_ns=(1, 2),
            returns=(1.0,),
            equity=(1.0, 2.0),
            trades=(),
            fills=(),
            capital=1.0,
            currency="USD",
            venue_model={},
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("window", (date(2024, 1, 2), date(2024, 1, 1)), "is before"),
        ("period", "hourly", "not a duration"),
        ("capital", 0.0, "not above zero"),
        ("period_ends_ns", (2, 1), "strictly increasing"),
    ],
)
def test_a_run_refuses_an_impossible_field(field: str, value: object, message: str) -> None:
    fields: dict[str, object] = {
        "window": (date(2024, 1, 1), date(2024, 1, 2)),
        "period": "1d",
        "period_ends_ns": (1, 2),
        "returns": (1.0, 2.0),
        "equity": (1.0, 2.0),
        "trades": (),
        "fills": (),
        "capital": 100.0,
        "currency": "USD",
        "venue_model": {},
    }
    with pytest.raises(ValidationError, match=message):
        CardRun(**{**fields, field: value})  # type: ignore[arg-type]


def test_folds_cut_the_window_into_equal_calendar_spans() -> None:
    run = build_run(tuple(float(i) for i in range(8)))
    folds = run.folds(4)
    assert [f.window for f in folds] == [
        (date(2024, 1, 1), date(2024, 1, 2)),
        (date(2024, 1, 3), date(2024, 1, 4)),
        (date(2024, 1, 5), date(2024, 1, 6)),
        (date(2024, 1, 7), date(2024, 1, 8)),
    ]
    assert [f.returns for f in folds] == [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0), (6.0, 7.0)]
    assert [f.equity for f in folds] == [
        (100_000.0, 100_001.0),
        (100_003.0, 100_006.0),
        (100_010.0, 100_015.0),
        (100_021.0, 100_028.0),
    ]


def test_a_fold_keeps_the_run_it_came_from() -> None:
    run = build_run((1.0, 2.0))
    fold = run.folds(2)[0]
    assert (fold.capital, fold.currency, fold.period) == (run.capital, run.currency, run.period)


def test_folds_of_a_window_that_does_not_divide_stay_contiguous() -> None:
    run = build_run(tuple(float(i) for i in range(7)))
    folds = run.folds(3)
    assert folds[0].window[0] == date(2024, 1, 1)
    assert folds[-1].window[1] == date(2024, 1, 7)
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert later.window[0] - earlier.window[1] <= timedelta(days=1)
    assert sum(len(f.returns) for f in folds) == 7


def test_a_trade_belongs_to_the_fold_it_closed_in() -> None:
    run = build_run(
        (0.0, 0.0, 0.0, 0.0),
        trades=(trade(date(2024, 1, 1), 10.0), trade(date(2024, 1, 4), 20.0)),
        fills=(fill(date(2024, 1, 2)), fill(date(2024, 1, 4))),
    )
    first, second = run.folds(2)
    assert [t.pnl_net for t in first.trades] == [10.0]
    assert [t.pnl_net for t in second.trades] == [20.0]
    assert [f.ts_ns for f in first.fills] == [at(date(2024, 1, 2))]
    assert [f.ts_ns for f in second.fills] == [at(date(2024, 1, 4))]


def test_a_fold_count_below_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="positive number of folds"):
        build_run((1.0,)).folds(0)


def test_one_fold_is_the_whole_run() -> None:
    run = build_run((1.0, 2.0, 3.0))
    (whole,) = run.folds(1)
    assert whole.window == run.window
    assert whole.returns == run.returns
