"""Joining the windows a stage closed: one run, one equity curve, one book."""

from __future__ import annotations

from datetime import date

from kanso.criteria.run import NS_PER_DAY, midnight_ns
from kanso.monitor import combined, tenure
from kanso.portfolio.records import StageResult

from .builders import CAPITAL, START, fill_at, run_over

SECOND_DAY = date(2024, 3, 2)


def result(
    run_returns: tuple[float, ...],
    *,
    start: date = START,
    positions: tuple[tuple[str, float, float], ...] = (),
    **run_kwargs: object,
) -> StageResult:
    """One closed window, as the event that recorded it hands it back."""
    return StageResult(
        stage="paper",
        session_id="s",
        strategy_id="demo_mr",
        version=1,
        capital=CAPITAL,
        run=run_over(run_returns, start=start, **run_kwargs),  # type: ignore[arg-type]
        positions=positions,
    )


def test_no_window_is_no_run() -> None:
    assert combined([]) is None
    assert tenure("paper", [], None) is None


def test_two_windows_become_one_continuous_run() -> None:
    """The second window's returns follow the first's, from the first's capital."""
    first = result((100.0, 200.0))
    second = result((300.0,), start=date(2024, 3, 3))

    joined = combined([first, second])

    assert joined is not None
    assert joined.window == (START, date(2024, 3, 3))
    assert joined.returns == (100.0, 200.0, 300.0)
    assert joined.equity == (CAPITAL + 100.0, CAPITAL + 300.0, CAPITAL + 600.0)


def test_the_equity_curve_does_not_step_back_at_the_seam() -> None:
    """Concatenating two curves would restart the second at its own capital."""
    first = result((1_000.0,))
    second = result((1_000.0,), start=SECOND_DAY)

    joined = combined([first, second])

    assert joined is not None
    assert joined.equity[-1] == CAPITAL + 2_000.0
    assert second.run.equity[-1] == CAPITAL + 1_000.0


def test_two_windows_ending_on_the_same_day_are_added() -> None:
    """A stage stopped and restarted the same session leaves two partial periods."""
    first = result((40.0,))
    second = result((60.0,))

    joined = combined([first, second])

    assert joined is not None
    assert joined.period_ends_ns == (midnight_ns(START) + NS_PER_DAY - 1,)
    assert joined.returns == (100.0,)


def test_trades_and_fills_are_joined_in_time_order() -> None:
    late = result((10.0,), start=SECOND_DAY, fills=(fill_at(SECOND_DAY),))
    early = result((10.0,), fills=(fill_at(START),))

    joined = combined([early, late])

    assert joined is not None
    assert [fill.ts_ns for fill in joined.fills] == sorted(fill.ts_ns for fill in joined.fills)
    assert len(joined.fills) == 2


def test_a_tenure_reads_its_clock_from_the_stage_when_there_is_one() -> None:
    reached = midnight_ns(date(2024, 4, 1))

    held = tenure("paper", [result((10.0, 20.0))], reached)

    assert held is not None
    assert held.clock_ns == reached
    assert held.joined_ns == midnight_ns(START)
    assert held.windows == 1


def test_a_tenure_without_a_session_clock_ends_at_its_last_window() -> None:
    held = tenure("paper", [result((10.0, 20.0))], None)

    assert held is not None
    assert held.clock_ns == midnight_ns(SECOND_DAY) + NS_PER_DAY - 1


def test_the_book_is_the_last_windows_and_not_every_windows() -> None:
    """Each window's positions were recorded before its flatten; only the last still stands."""
    first = result((10.0,), positions=(("AAA.XNAS", 100.0, 10.0),))
    second = result((10.0,), start=SECOND_DAY, positions=(("BBB.XNAS", -50.0, 10.0),))

    held = tenure("paper", [first, second], None)

    assert held is not None
    assert held.positions == (("BBB.XNAS", -50.0, 10.0),)
    assert held.gross == 500.0
    assert held.net == -500.0


def test_a_days_profit_is_the_periods_that_ended_on_it() -> None:
    held = tenure("paper", [result((10.0, 20.0))], None)

    assert held is not None
    assert held.day_pnl(START) == 10.0
    assert held.day_pnl(SECOND_DAY) == 20.0
    assert held.day_pnl(date(2024, 4, 1)) == 0.0
