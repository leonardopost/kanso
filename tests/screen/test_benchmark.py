"""`benchmark: hold`: a condition cell's positions set against buying its follower and keeping it.

Every number here is read against a book kept by hand in plain Python — each session's position
from the level the test's decision reads, its period's return, a fill of `FILL` wherever a
position opens or closes, and the hold's close-to-close return less a fill at each end — and
every p against a recount over every rotation of the positions, so the arithmetic under test
and the arithmetic it is read against share nothing but the stated prices.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np
import pytest

from kanso.criteria.quantities import (
    correlation,
    long_run_factor,
    moments,
    sharpe_difference_variance,
    sharpe_variance,
    years,
)
from kanso.schemas.screen import Cell, Condition, ConditionStats, HoldStats, ScreenVerdict
from kanso.screen import benchmark, condition, nulls
from kanso.screen.hurdle import Hurdles
from kanso.screen.run import _judged
from tests.screen.test_condition import (
    MODEL,
    QUOTED,
    ROUND_TRIP,
    TEST,
    WINDOW,
    daily,
    measured,
    screen,
    trading,
    walk,
)

FILL = ROUND_TRIP / 2
"""One fill of a bar follower under `MODEL`: commission, slippage and half the fixed spread."""

CLOSE: dict[str, Any] = {**TEST, "entry": "close"}
"""Close to close, long above zero and short below it: always in the market on ±1 levels."""


def levels(sessions: Sequence[date], seed: int, zero: float = 0.0) -> dict[date, float]:
    """A level of ±1 a session in runs averaging four, and 0 on a share `zero` of them."""
    rng = np.random.default_rng(seed)
    found, level = {}, 1.0
    for day in sessions:
        if rng.random() < 0.25:
            level = -level
        found[day] = 0.0 if rng.random() < zero else level
    return found


def hand_book(
    test: Mapping[str, Any],
    level: Mapping[date, float],
    prices: Mapping[date, tuple[float, float]],
    sessions: Sequence[date],
    fill: float = FILL,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Each held session's position, its period's return, the hold's net, and the period's
    `[entry price, exit price]` contiguity, by hand: a close-to-close session is decided on the
    level published the session before and holds no first session; open to close, on its own."""
    side = 1.0 if test["side"] == "long" else -1.0
    across = test["entry"] == "close"
    positions, returns, holds = [], [], []
    for at, day in enumerate(sessions):
        if across and at == 0:
            continue
        read = level[sessions[at - 1]] if across else level[day]
        state = 1.0 if read > 0 else -1.0 if read < 0 else 0.0
        positions.append(side * state)
        before = prices[sessions[at - 1]][1] if at else prices[day][0]
        entry = before if across else prices[day][0]
        returns.append(1e4 * (math.log(prices[day][1]) - math.log(entry)))
        holds.append(1e4 * (math.log(prices[day][1]) - math.log(before)))
    holds[0] -= fill
    holds[-1] -= fill
    return positions, returns, holds, [float(across)] * len(positions)


def hand_net(
    positions: Sequence[float], returns: Sequence[float], joins: Sequence[float], fill: float = FILL
) -> list[float]:
    """What the positions make each session less a fill wherever one opens or closes."""
    found = []
    for at, (position, move) in enumerate(zip(positions, returns, strict=True)):
        value = position * move
        if position != 0:
            runs_on = at > 0 and joins[at] and positions[at - 1] == position
            carried = at + 1 < len(positions) and joins[at + 1] and positions[at + 1] == position
            value -= fill * (int(not runs_on) + int(not carried))
        found.append(value)
    return found


def hand_sharpe(values: Sequence[float], annualisation: float) -> float:
    spread = statistics.stdev(values)
    return 0.0 if spread == 0 else statistics.fmean(values) / spread * math.sqrt(annualisation)


def rotated(values: Sequence[float], shift: int) -> list[float]:
    """Every value moved `shift` sessions later, wrapping at the end."""
    return [values[(at - shift) % len(values)] for at in range(len(values))]


def held(cell: Cell) -> HoldStats:
    found = cell.condition
    assert found is not None and found.hold is not None
    return found.hold


def test_an_always_long_cell_held_close_to_close_is_the_hold_and_beats_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    spec = screen({"up": {**CLOSE, "against": None}}, payoff="long_flat", benchmark="hold")

    (cell,) = measured(
        spec,
        {"a": walk(sessions, 1, drift=10.0)},
        {"lvl": daily(dict.fromkeys(sessions, 1.0))},
        monkeypatch,
    )

    hold = held(cell)
    assert hold.difference == 0.0 and hold.excess_bp == 0.0
    assert hold.sharpe == hold.sharpe_hold and hold.sharpe > 0
    assert hold.p == 1.0, "every rotation of a constant position is the observed one"
    assert hold.null_mean == 0.0 and hold.critical is None
    assert cell.params["benchmark"] == "hold"


def test_the_positions_and_the_hold_book_what_a_hand_book_makes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    prices, level = walk(sessions, 2, drift=6.0), levels(sessions, 3)
    spec = screen({"up": CLOSE}, benchmark="hold")

    (cell,) = measured(spec, {"a": prices}, {"lvl": daily(level)}, monkeypatch)

    positions, returns, hold_net, joins = hand_book(CLOSE, level, prices, sessions)
    own = hand_net(positions, returns, joins)
    annualisation = len(own) / years(WINDOW)
    hold = held(cell)
    assert cell.sessions == len(own) == len(sessions) - 1
    assert hold.annualisation == pytest.approx(annualisation, rel=1e-15)
    assert hold.sharpe == pytest.approx(hand_sharpe(own, annualisation), rel=1e-9)
    assert hold.sharpe_hold == pytest.approx(hand_sharpe(hold_net, annualisation), rel=1e-9)
    assert hold.difference == pytest.approx(hold.sharpe - hold.sharpe_hold, abs=1e-12)
    excess = statistics.fmean(a - b for a, b in zip(own, hold_net, strict=True))
    assert hold.excess_bp == pytest.approx(excess, abs=1e-9)
    # The positions' net, summed, is the cell's own round trips: booked where each fill happens
    # rather than on the session a position left in, it is the same ceiling.
    assert cell.response is not None
    assert statistics.fmean(own) == pytest.approx(cell.response.ceiling_bp_day, abs=1e-9)
    rho = correlation(own, hold_net)
    assert rho is not None
    factor = max(long_run_factor(own), long_run_factor(hold_net))
    se = math.sqrt(sharpe_difference_variance(own, hold_net, WINDOW, factor, rho))
    assert hold.difference_se == pytest.approx(se, rel=1e-12)


def test_an_open_to_close_cell_is_set_against_the_close_to_close_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    prices, level = walk(sessions, 4, drift=8.0), levels(sessions, 5, zero=0.3)
    spec = screen({"up": TEST}, benchmark="hold")

    (cell,) = measured(spec, {"a": prices}, {"lvl": daily(level)}, monkeypatch)

    positions, returns, hold_net, joins = hand_book(TEST, level, prices, sessions)
    assert 0.0 in positions, "the level is zero on some sessions, where the contrast is flat"
    own = hand_net(positions, returns, joins)
    annualisation = len(own) / years(WINDOW)
    hold = held(cell)
    assert len(own) == len(sessions), "open to close holds the window's first session"
    assert hold.sharpe == pytest.approx(hand_sharpe(own, annualisation), rel=1e-9)
    # The hold is long overnight too: from the session's own open only on the first session.
    assert hold.sharpe_hold == pytest.approx(hand_sharpe(hold_net, annualisation), rel=1e-9)
    assert hold_net[0] == pytest.approx(
        1e4 * math.log(prices[sessions[0]][1] / prices[sessions[0]][0]) - FILL
    )


def test_p_is_each_difference_s_rank_among_the_family_s_largest_at_every_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    prices = walk(sessions, 6, drift=4.0)
    level, other = levels(sessions, 7), levels(sessions, 8)
    spec = screen(
        {"up": CLOSE, "down": {**CLOSE, "signal": "flg", "side": "short"}}, benchmark="hold"
    )
    points = {"lvl": daily(level), "flg": daily(other)}

    found = measured(spec, {"a": prices}, points, monkeypatch)

    books = [
        hand_book(CLOSE, level, prices, sessions),
        hand_book({**CLOSE, "side": "short"}, other, prices, sessions),
    ]
    annualisation = len(books[0][0]) / years(WINDOW)
    differences, by_shift = [], []
    for positions, returns, hold_net, joins in books:
        base = hand_sharpe(hold_net, annualisation)
        differences.append(hand_sharpe(hand_net(positions, returns, joins), annualisation) - base)
        by_shift.append(
            [
                hand_sharpe(hand_net(rotated(positions, k), returns, joins), annualisation) - base
                for k in range(1, len(positions))
            ]
        )
    maxima = [max(a, b) for a, b in zip(*by_shift, strict=True)]
    for cell, difference, shifted in zip(found, differences, by_shift, strict=True):
        hold = held(cell)
        assert hold.difference == pytest.approx(difference, abs=1e-9)
        reaching = sum(value >= difference - 1e-9 for value in maxima)
        assert hold.p == pytest.approx((1 + reaching) / (len(maxima) + 1))
        assert hold.null_mean == pytest.approx(statistics.fmean(shifted), abs=1e-9)
    assert found[0].condition is not None and found[0].condition.exact


def test_a_short_through_the_falls_beats_the_hold_and_passes_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = levels(sessions, 9)
    rng = np.random.default_rng(10)
    price, prices = 100.0, {}
    for at, day in enumerate(sessions):
        falls = at > 0 and level[sessions[at - 1]] > 0
        price *= math.exp((rng.normal() * 20 + (-40.0 if falls else 60.0)) / 1e4)
        prices[day] = (price, price)
    verdict = {"alpha": 0.05, "min_margin_bp": -1e4, "min_events_per_day": 0, "min_sessions": 20}
    spec = screen({"up": {**CLOSE, "side": "short"}}, benchmark="hold", verdict=verdict)

    (cell,) = measured(spec, {"a": prices}, {"lvl": daily(level)}, monkeypatch)
    judged = _judged(spec.verdict, cell)

    hold = held(cell)
    assert hold.difference > 0 and hold.p <= 0.05 and hold.excess_bp > 0
    assert hold.critical is not None and hold.difference > hold.critical
    assert hold.null_mean < hold.critical < hold.difference
    assert judged.judged == "pass", judged.reason


def stated(hold: HoldStats | None, p: float = 0.001, mean: float = 5.0) -> Cell:
    """A condition cell with this p and mean on its contrast and this hold."""
    condition = ConditionStats(
        payoff="contrast",
        null_by="circular",
        sessions_when=50,
        sessions_against=50,
        exposure=1.0,
        episodes=10,
        episodes_against=10,
        longest_run=9,
        skipped=0,
        shifts=99,
        exact=True,
        raw_bp=mean,
        drift_bp_day=3.0,
        folds_adjusted=[mean, mean],
        folds_adjusted_same_sign=2,
        signal_age_median_s=1.0,
        signal_age_max_s=1.0,
        hold=hold,
    )
    return Cell(
        measure=0,
        id="condition",
        key="condition/up/a/circular",
        params={},
        mean=mean,
        se=1.0,
        t=mean,
        p=p,
        sessions=100,
        folds=[1.0, 1.0],
        folds_same_sign=2,
        condition=condition,
    )


def against(p: float, difference: float) -> HoldStats:
    return HoldStats(
        sharpe=0.5 + difference,
        sharpe_hold=0.5,
        difference=difference,
        difference_se=0.3,
        excess_bp=difference,
        p=p,
        null_mean=-0.2,
        annualisation=252.0,
        folds=[difference, difference],
        folds_same_sign=2,
    )


@pytest.mark.parametrize(
    ("cell", "judged", "reason"),
    [
        (stated(against(0.01, 0.4), p=0.9, mean=-3.0), "pass", None),
        (
            stated(against(0.01, -0.1)),
            "fail",
            "Sharpe 0.4 does not beat the hold's 0.5",
        ),
        (stated(against(0.2, 0.4)), "fail", "p 0.2 against the hold above alpha 0.05"),
        (
            stated(None, p=0.01, mean=-3.0),
            "fail",
            "mean -3 bp is against the declared side",
        ),
    ],
)
def test_a_cell_set_against_the_hold_is_judged_by_the_hold_and_not_its_contrast(
    cell: Cell, judged: str, reason: str | None
) -> None:
    verdict = ScreenVerdict(alpha=0.05, min_margin_bp=-1e4, min_events_per_day=0, min_sessions=20)

    found = _judged(verdict, cell)

    assert found.judged == judged and found.reason == reason


def test_a_quoted_follower_crosses_the_touch_on_the_positions_and_on_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    prices = walk(sessions, 11, drift=5.0)
    spec = screen(
        {"up": {**CLOSE, "against": None}}, payoff="long_flat", followers="q", benchmark="hold"
    )
    half = 0.02

    (cell,) = measured(
        spec,
        {"q": prices},
        {"lvl": daily(dict.fromkeys(sessions, 1.0))},
        monkeypatch,
        quoted=half,
    )

    first, last = prices[sessions[0]][1], prices[sessions[-1]][1]
    commission = 1.0
    bought = commission + 1e4 * math.log((first + half) / first)
    sold = commission + 1e4 * math.log(last / (last - half))
    hold = held(cell)
    _, _, hold_net, _ = hand_book(CLOSE, dict.fromkeys(sessions, 1.0), prices, sessions)
    hold_net[0] += FILL - bought
    hold_net[-1] += FILL - sold
    annualisation = (len(sessions) - 1) / years(WINDOW)
    assert hold.sharpe_hold == pytest.approx(hand_sharpe(hold_net, annualisation), rel=1e-9)
    assert hold.difference == 0.0, "held throughout, the cell is the hold, touches and all"


def test_a_cell_that_holds_nothing_reads_a_p_of_one_beside_one_that_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    spec = screen({"up": CLOSE, "none": {**CLOSE, "signal": "evt"}}, benchmark="hold")
    points = {"lvl": daily(levels(sessions, 12)), "evt": []}

    found = measured(spec, {"a": walk(sessions, 13)}, points, monkeypatch)

    empty = held(found[1])
    assert found[1].sessions == 0
    assert (empty.p, empty.sharpe, empty.difference, empty.difference_se) == (1.0, 0.0, 0.0, 0.0)
    assert empty.folds == [None, None] and empty.folds_same_sign == 0
    assert held(found[0]).p < 1.0


def test_folds_are_the_difference_inside_each_calendar_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    prices, level = walk(sessions, 14, drift=3.0), levels(sessions, 15)
    spec = screen({"up": CLOSE}, benchmark="hold")

    (cell,) = measured(spec, {"a": prices}, {"lvl": daily(level)}, monkeypatch, folds=3)

    positions, returns, hold_net, joins = hand_book(CLOSE, level, prices, sessions)
    own = hand_net(positions, returns, joins)
    annualisation = len(own) / years(WINDOW)
    length = (WINDOW[1] - WINDOW[0]).days + 1
    fold = [(day - WINDOW[0]).days * 3 // length for day in sessions[1:]]
    hold = held(cell)
    for index, value in enumerate(hold.folds):
        inside = [at for at, f in enumerate(fold) if f == index]
        expected = hand_sharpe([own[at] for at in inside], annualisation) - hand_sharpe(
            [hold_net[at] for at in inside], annualisation
        )
        assert value == pytest.approx(expected, abs=1e-9)
    assert hold.folds_same_sign == nulls.same_sign(hold.folds, hold.difference)


def test_a_round_trip_is_its_entry_s_fill_and_its_exit_s() -> None:
    spec = screen()
    hurdles = Hurdles(spec, {"XNAS": QUOTED}, {"q": "XNAS"}, {"q": 1.0}, {"q": 0.01})
    bars = Hurdles(spec, {"XNAS": MODEL}, {"a": "XNAS"}, {"a": 1.0}, {"a": 0.01})
    price = np.asarray([100.0, 101.0])

    buys, sells = bars.fills("a", price, None, None)
    touched = hurdles.fills("q", price, price - 0.03, price + 0.01)

    assert buys.tolist() == pytest.approx([FILL, FILL]) and sells.tolist() == pytest.approx(
        [FILL, FILL]
    )
    assert touched[0][0] == pytest.approx(1.0 + 1e4 * math.log(100.01 / 100.0))
    assert touched[1][1] == pytest.approx(1.0 + 1e4 * math.log(101.0 / 100.97))


def test_a_family_of_fewer_than_two_sessions_has_no_shift_and_reads_a_p_of_one() -> None:
    book = benchmark.Book(
        returns=np.asarray([5.0]),
        joins=np.asarray([False]),
        buy_in=np.asarray([1.0]),
        sell_in=np.asarray([1.0]),
        buy_out=np.asarray([1.0]),
        sell_out=np.asarray([1.0]),
        hold=np.asarray([3.0]),
        folds=np.asarray([0]),
    )

    judged = benchmark.judge(np.asarray([[1.0]]), [book], 252.0, 999, 1)

    assert judged.p.tolist() == [1.0] and judged.maxima.size == 0
    assert judged.sharpe.tolist() == [0.0] and judged.null_mean.tolist() == [0.0]


def test_a_difference_whose_series_does_not_vary_sums_the_two_errors() -> None:
    own = np.zeros(40)
    hold = np.asarray([float(at % 7) - 3.0 for at in range(40)])

    found = benchmark.difference_se(own, hold, WINDOW)

    values = hold.tolist()
    expected = math.sqrt(sharpe_variance(values, WINDOW, long_run_factor(values), moments(values)))
    assert found == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("zero", [0.0, 0.3])
def test_with_nothing_planted_p_against_the_hold_is_near_uniform(
    monkeypatch: pytest.MonkeyPatch, zero: float
) -> None:
    sessions = trading()
    spec = screen({"up": CLOSE, "down": {**CLOSE, "signal": "flg"}}, benchmark="hold")
    found = []
    for seed in range(120):
        points = {
            "lvl": daily(levels(sessions, 1000 + seed, zero)),
            "flg": daily(levels(sessions, 5000 + seed, zero)),
        }
        cells = measured(spec, {"a": walk(sessions, seed, drift=15.0)}, points, monkeypatch)
        found.append(min(held(cell).p for cell in cells))
    # The family's smallest p is uniform under the null, so about one seed in ten is at or under
    # 0.10: binomial 99.9% bounds on 120 seeds.
    assert 2 <= sum(p <= 0.10 for p in found) <= 26


def test_a_hold_is_named_in_every_cell_s_params_and_in_none_without_one() -> None:
    plain, against_hold = screen(), screen(benchmark="hold")
    measures = [spec.measures[0] for spec in (plain, against_hold)]
    assert all(isinstance(measure, Condition) for measure in measures)

    keys = [
        condition.cells(spec, measure)
        for spec, measure in zip((plain, against_hold), measures, strict=True)
    ]  # type: ignore[arg-type]

    assert all("benchmark" not in key.params for key in keys[0])
    assert all(key.params["benchmark"] == "hold" for key in keys[1])


def test_a_random_alignment_of_a_follower_that_rose_gives_its_drift_up_where_not_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    spec = screen({"up": CLOSE}, benchmark="hold")

    (cell,) = measured(
        spec,
        {"a": walk(sessions, 16, drift=30.0)},
        {"lvl": daily(levels(sessions, 17))},
        monkeypatch,
    )

    hold = held(cell)
    assert hold.sharpe_hold > 0
    assert hold.null_mean < -0.5 * hold.sharpe_hold, "short about half the time at random"


def test_a_fold_holding_fewer_than_two_sessions_has_no_difference() -> None:
    returns = np.asarray([10.0, -4.0, 6.0, 3.0, -2.0])
    zero = np.zeros(5)
    book = benchmark.Book(
        returns=returns,
        joins=np.asarray([False, True, True, True, True]),
        buy_in=zero,
        sell_in=zero,
        buy_out=zero,
        sell_out=zero,
        hold=returns,
        folds=np.asarray([0, 0, 0, 0, 1]),
    )
    own = benchmark.net(np.asarray([1.0, 1.0, -1.0, -1.0, 1.0]), book)

    found = benchmark.folds(own, book, 252.0, 3)

    assert found[1] is None and found[2] is None
    expected = hand_sharpe(own[:4].tolist(), 252.0) - hand_sharpe(returns[:4].tolist(), 252.0)
    assert found[0] == pytest.approx(expected, abs=1e-12)
