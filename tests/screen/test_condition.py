"""`condition`: a state read off a declared signal, scored on a follower's periods.

Every follower here is stated session by session, its open at 09:31 and its close at 16:00 in
New York, and the signal point by point, so each test reads its answer off its own arguments.
The signal is handed to the measure through the as-of join the catalog read ends in
(`kanso.screen.signals.join`); the read itself is driven against a catalog in
`tests/screen/test_signals.py` and through `kanso screen run` in `tests/cli`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from kanso.data.closures import US_EQUITY
from kanso.errors import PreconditionError
from kanso.schemas.screen import Cell, Condition, Screen
from kanso.schemas.venue import CostsOverride, resolve_venue_model
from kanso.screen import condition, hurdle, signals
from kanso.screen.condition import Mark
from kanso.screen.hurdle import Hurdles
from kanso.screen.run import Context, _folds
from kanso.screen.sessions import Series, days
from tests.nautilus.backtest.conftest import instrument, perpetual

NEW_YORK = ZoneInfo("America/New_York")
WINDOW = (date(2026, 1, 5), date(2026, 3, 27))
COSTS = CostsOverride(commission_bps=1.0, slippage_bps=0.5, spread="fixed_bps", fixed_bps=2.0)
MODEL = resolve_venue_model("XNAS", hypothesis_costs=COSTS, quotes_available=False)
QUOTED = resolve_venue_model(
    "XNAS", hypothesis_costs=CostsOverride(commission_bps=1.0, slippage_bps=0.5)
)
ROUND_TRIP = 2 * (1.0 + 0.5 + 1.0)
"""Commission, slippage and half the fixed spread, on each of two fills of a bar follower."""

TEST: dict[str, Any] = {
    "signal": "lvl",
    "when": {"gt": 0},
    "against": {"lt": 0},
    "entry": "open",
    "exit": "close",
    "hold": "while",
    "side": "long",
}


def screen(tests: Mapping[str, Mapping[str, Any]] | None = None, **changes: Any) -> Screen:
    verdict = changes.pop("verdict", None)
    measure = {
        "id": "condition",
        "tests": tests or {"up": TEST},
        "followers": "a",
        "payoff": "contrast",
        "null_by": "circular",
        "demean": [],
        "controls": [],
        "latency_ms": 900_000,
        **changes,
    }
    return Screen.model_validate(
        {
            "schema": 1,
            "id": "probe",
            "title": "probe",
            "thesis": "probe",
            "window": {"start": str(WINDOW[0]), "end": str(WINDOW[1])},
            "legs": {
                "a": {"instrument": "AAA.XNAS", "type": "bar", "resolution": "1m"},
                "b": {"instrument": "BBB.XNAS", "type": "bar", "resolution": "1m"},
                "q": {"instrument": "QQQ.XNAS", "type": "quote"},
            },
            "groups": {"ab": ["a", "b"]},
            "signals": {
                name: {"type": "kanso_state_tape", "field": field, "instrument": "AAA.XNAS"}
                for name, field in (("lvl", "level"), ("flg", "flag"), ("evt", "flag"))
            },
            "clock": {"hours": {"tz": "America/New_York", "span": "09:31-16:01"}},
            "measures": [measure],
            **({"verdict": verdict} if verdict else {}),
        }
    )


def at(day: date, hour: int, minute: int = 0) -> int:
    """An instant in New York as UTC nanoseconds."""
    moment = datetime.combine(day, time(hour, minute), tzinfo=NEW_YORK)
    return int(moment.timestamp()) * 1_000_000_000


def trading(first: date = WINDOW[0], last: date = WINDOW[1]) -> list[date]:
    found, day = [], first
    while day <= last:
        if not US_EQUITY.closed(day):
            found.append(day)
        day += timedelta(days=1)
    return found


Prices = Mapping[date, tuple[float, float]]
Points = Sequence[tuple[int, float]]


def measured(
    spec: Screen,
    prices: Mapping[str, Prices],
    points: Mapping[str, Points],
    monkeypatch: pytest.MonkeyPatch,
    draws: int = 999,
    folds: int = 2,
    quoted: float | None = None,
    definitions: Mapping[str, Any] | None = None,
    events: Mapping[str, Sequence[int]] | None = None,
) -> list[Cell]:
    """The measure's cells over these opens and closes and these signal points."""

    def asof(catalog: Any, screen_: Screen, wanted: Mapping[str, np.ndarray], window: Any) -> Any:
        found = {}
        for name, instants in wanted.items():
            ts = np.asarray([point[0] for point in points[name]], dtype=np.int64)
            values = np.asarray([point[1] for point in points[name]], dtype=np.float64)
            stamped = np.asarray((events or {}).get(name, ts), dtype=np.int64)
            found[name] = signals.join([(0, 2**62, ts, values, stamped)], instants)
        return found

    monkeypatch.setattr("kanso.screen.condition.signals.asof", asof)
    measure = spec.measures[0]
    assert isinstance(measure, Condition)
    sessions = days(spec, WINDOW)
    followers = spec.members(measure.followers)
    marks = []
    for day in sessions:
        row: list[condition.Marks | None] = []
        for follower in followers:
            held = prices[follower].get(day)
            if held is None:
                row.append(None)
                continue
            if quoted is None:
                row.append((Mark(at(day, 9, 31), held[0]), Mark(at(day, 16), held[1])))
            else:
                row.append(
                    (
                        Mark(at(day, 9, 31), held[0], held[0] - quoted, held[0] + quoted),
                        Mark(at(day, 16), held[1], held[1] - quoted, held[1] + quoted),
                    )
                )
        marks.append(row)
    context = Context(
        screen=spec,
        sha="a" * 64,
        snapshot_id="b" * 64,
        draws=draws,
        folds=folds,
        stamps={},
        betas={},
        slopes={},
        window=WINDOW,
        definitions=dict(
            definitions or {f"{s}.XNAS": instrument(s) for s in ("AAA", "BBB", "QQQ")}
        ),
    )
    model = MODEL if quoted is None else QUOTED
    hurdles = Hurdles(
        spec,
        {"XNAS": model},
        {"a": "XNAS", "b": "XNAS", "q": "XNAS"},
        {"a": 1.0, "b": 1.0, "q": 1.0},
        {"a": 0.01, "b": 0.01, "q": 0.01},
    )
    fold_of = _folds(sessions, WINDOW, folds)
    return condition.measure(context, 0, measure, sessions, marks, fold_of, None, hurdles)


def daily(values: Mapping[date, float], hour: int = 8) -> list[tuple[int, float]]:
    """One point a day, published at `hour` New York time."""
    return [(at(day, hour), value) for day, value in sorted(values.items())]


def flat(sessions: Sequence[date], step: float = 0.0, start: float = 100.0) -> dict[date, Any]:
    """A follower that moves `step` bp open to close and nothing overnight."""
    return {day: (start, start * math.exp(step / 1e4)) for day in sessions}


def walk(sessions: Sequence[date], seed: int, drift: float = 0.0) -> dict[date, Any]:
    """A follower whose open-to-close and overnight moves are seeded noise and `drift` bp."""
    rng = np.random.default_rng(seed)
    price, found = 100.0, {}
    for day in sessions:
        opened = price * math.exp(rng.normal() * 20 / 1e4)
        price = opened * math.exp((rng.normal() * 50 + drift) / 1e4)
        found[day] = (opened, price)
    return found


def test_an_always_true_long_reads_no_excess_and_its_margin_shows_the_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    test = {**TEST, "entry": "close", "against": None}
    spec = screen({"up": test}, payoff="long_flat")
    prices = {"a": walk(sessions, 1, drift=10.0)}

    (cell,) = measured(spec, prices, {"lvl": daily(dict.fromkeys(sessions, 1.0))}, monkeypatch)

    assert cell.mean == pytest.approx(0.0, abs=1e-9) and cell.t == pytest.approx(0.0, abs=1e-6)
    stats, found = cell.response, cell.condition
    assert stats is not None and found is not None
    assert stats.events == 1, "a state held throughout, close to close, is one round trip"
    assert stats.hurdle_bp == pytest.approx(ROUND_TRIP)
    held = cell.sessions
    assert stats.ceiling_bp_day == pytest.approx(found.drift_bp_day - ROUND_TRIP / held)
    assert found.exposure == 1.0 and found.raw_bp == pytest.approx(found.drift_bp_day)
    assert found.long_flat_bp is None and found.sessions_against is None


def test_a_planted_contrast_is_recovered_at_its_size(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    rng = np.random.default_rng(4)
    level = {day: float(rng.choice([-1.0, 1.0])) for day in sessions}
    prices = {
        "a": {
            day: (
                100.0,
                100.0 * math.exp((rng.normal() * 30 + (40.0 if level[day] > 0 else 0)) / 1e4),
            )
            for day in sessions
        }
    }
    points = {"lvl": daily(level)}

    (contrast,) = measured(screen(), prices, points, monkeypatch)
    (lone,) = measured(
        screen({"up": {**TEST, "against": None}}, payoff="long_flat"), prices, points, monkeypatch
    )

    assert abs(contrast.mean - 40.0) <= 2 * contrast.se
    share = contrast.condition.sessions_when / contrast.sessions  # type: ignore[union-attr]
    assert abs(lone.mean - share * (1 - share) * 40.0) <= 2 * lone.se
    assert contrast.p < 0.05 and contrast.t > 0


def test_a_long_or_flat_cell_s_se_is_the_spread_of_its_excess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = {day: (1.0 if n % 4 else -1.0) for n, day in enumerate(sessions)}
    prices = {"a": walk(sessions, 21, drift=15.0)}
    points = {"lvl": daily(level)}
    test = {"up": {**TEST, "against": None}}

    (shifted,) = measured(screen(test, payoff="long_flat"), prices, points, monkeypatch)
    (flipped,) = measured(
        screen(test, payoff="long_flat", null_by="sign_flip", block=1), prices, points, monkeypatch
    )

    moves = np.asarray([1e4 * math.log(prices["a"][d][1] / prices["a"][d][0]) for d in sessions])
    residuals = moves - float(np.mean(moves))
    positions = np.asarray([1.0 if level[day] > 0 else 0.0 for day in sessions])
    centred = (positions - float(np.mean(positions))) * residuals
    se = float(np.std(centred, ddof=1)) / math.sqrt(len(sessions))
    for cell in (shifted, flipped):
        assert cell.mean == pytest.approx(float(np.mean(positions * residuals)))
        assert cell.se == pytest.approx(se) and cell.t == pytest.approx(cell.mean / cell.se)
    # A position times its residual alone spreads as if the mean were not fitted: at an
    # exposure of three quarters, about twice the excess's own standard error.
    naive = float(np.std(positions * residuals, ddof=1)) / math.sqrt(len(sessions))
    assert 1.6 <= naive / se <= 2.4


def test_a_point_stamped_at_the_close_is_not_read_at_that_close_and_is_read_at_the_next_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    stamped = [(at(day, 16), 1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)]
    close = screen({"up": {**TEST, "entry": "close"}})
    opened = screen()

    (at_close,) = measured(close, {"a": flat(sessions, 5)}, {"lvl": stamped}, monkeypatch)
    (at_open,) = measured(opened, {"a": flat(sessions, 5)}, {"lvl": stamped}, monkeypatch)

    # At the close the decision is 15:45, so the point read is the previous session's 16:00.
    closing, opening = at_close.condition, at_open.condition
    assert closing is not None and opening is not None
    assert closing.signal_age_median_s == 24 * 3600 - 15 * 60
    assert closing.signal_age_max_s == 4 * 24 * 3600 - 15 * 60, "across a long weekend"
    # At the next open the decision is 09:16 and the last close's point is 17h16m old.
    assert opening.signal_age_median_s == 17 * 3600 + 16 * 60


def test_monday_s_open_reads_sunday_s_point(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    # A point on Sunday at +1 and on Tuesday to Friday at -1, and none on Monday or Saturday.
    every = [WINDOW[0] + timedelta(days=n) for n in range(26)]
    level = {
        day: 1.0 if day.weekday() == 6 else -1.0
        for day in every
        if day.weekday() in (1, 2, 3, 4, 6)
    }
    prices = {"a": {day: (100.0, 101.0 if day.weekday() == 0 else 99.0) for day in sessions}}

    (cell,) = measured(screen(), prices, {"lvl": daily(level)}, monkeypatch, draws=99)

    found = cell.condition
    assert found is not None
    # The window's first Monday has no Sunday inside the window to read.
    mondays = sum(1 for day in sessions if day.weekday() == 0) - 1
    assert found.sessions_when == mondays, "only a Monday reads a Sunday point, and it is +1"
    assert found.episodes == mondays and found.longest_run == 1


def test_an_onset_while_a_fixed_hold_runs_is_skipped_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    # +, -, +, -, -, ... : the onset at the third session falls inside a three-session hold.
    level = dict.fromkeys(sessions, -1.0)
    level[sessions[1]] = level[sessions[3]] = 1.0
    test = {**TEST, "hold": {"sessions": 3}}

    (cell,) = measured(
        screen({"up": test}), {"a": flat(sessions, 5)}, {"lvl": daily(level)}, monkeypatch
    )

    found = cell.condition
    assert found is not None
    assert found.skipped == 2, "the against and when onsets inside the first hold"
    assert found.sessions_when == 3, "one hold of three sessions, opened at the first onset"
    assert found.sessions_against == 3, "then one short hold, opened at the next onset"


def test_a_state_held_k_sessions_close_to_close_is_one_round_trip_and_open_to_close_is_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 2, 27))
    level = dict.fromkeys(sessions, -1.0)
    for day in sessions[10:14]:
        level[day] = 1.0
    prices = {"a": walk(sessions, 2)}
    long_flat = {**TEST, "against": None}
    across = screen({"up": {**long_flat, "entry": "close"}}, payoff="long_flat")
    within = screen({"up": long_flat}, payoff="long_flat")

    (one,) = measured(across, prices, {"lvl": daily(level)}, monkeypatch)
    (four,) = measured(within, prices, {"lvl": daily(level)}, monkeypatch)

    assert one.response is not None and four.response is not None
    assert one.response.events == 1 and four.response.events == 4
    # The close-to-close position is decided on the four days and held over the next four.
    entry, exit_ = prices["a"][sessions[10]][1], prices["a"][sessions[14]][1]
    series = Series(
        ts=np.asarray([at(sessions[10], 16), at(sessions[14], 16)], dtype=np.int64),
        price=np.asarray([entry, exit_]),
    )
    spec_hurdles = Hurdles(across, {"XNAS": MODEL}, {"a": "XNAS"}, {"a": 1.0}, {"a": 0.01})
    expected = spec_hurdles.round_trip(
        "a", 1, {"a": series}, int(series.ts[0]), int(series.ts[1]), {}
    )
    assert one.response.hurdle_bp == pytest.approx(expected)
    assert one.response.gross_bp == pytest.approx(1e4 * math.log(exit_ / entry))
    assert four.response.hurdle_bp == pytest.approx(ROUND_TRIP)


def test_one_family_is_exact_when_its_blocks_admit_every_sign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 23))
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    spec = screen(null_by="sign_flip", block=5)

    (cell,) = measured(spec, {"a": walk(sessions, 3)}, {"lvl": daily(level)}, monkeypatch)

    found = cell.condition
    assert found is not None and found.exact is True
    assert found.blocks == math.ceil(len(sessions) / 5) and found.shifts is None
    assert cell.p >= 2.0 ** (1 - found.blocks)
    # Summed into blocks, the mean and t are the session values' own.
    assert cell.se > 0 and cell.t == pytest.approx(cell.mean / cell.se)


def test_one_family_is_exact_when_its_shifts_are_fewer_than_the_draws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = {day: (1.0 if (n // 4) % 2 else -1.0) for n, day in enumerate(sessions)}
    points = {"lvl": daily(level)}

    (every,) = measured(screen(), {"a": walk(sessions, 5)}, points, monkeypatch)
    (drawn,) = measured(screen(), {"a": walk(sessions, 5)}, points, monkeypatch, draws=9)

    assert every.condition is not None and drawn.condition is not None
    assert every.condition.exact is True and drawn.condition.exact is False
    assert every.condition.shifts == drawn.condition.shifts == len(sessions) - 1
    assert every.p >= 1 / len(sessions) and drawn.p >= 1 / (9 + 1)
    assert every.t == drawn.t and every.mean == drawn.mean


def test_a_family_whose_signal_holds_no_point_reads_nothing_under_either_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 16))
    nothing: dict[str, Points] = {"lvl": []}

    (shifted,) = measured(screen(), {"a": walk(sessions, 4)}, nothing, monkeypatch)
    (flipped,) = measured(
        screen(null_by="sign_flip", block=2), {"a": walk(sessions, 4)}, nothing, monkeypatch
    )

    for cell in (shifted, flipped):
        assert cell.sessions == 0 and cell.p == 1.0 and cell.t == 0.0
    assert shifted.condition is not None and shifted.condition.shifts == 0
    assert flipped.condition is not None and flipped.condition.blocks == 0


def test_sessions_before_the_signal_s_first_point_hold_no_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 2, 27))
    later = {day: (1.0 if n % 3 else -1.0) for n, day in enumerate(sessions) if n >= 10}

    (cell,) = measured(screen(), {"a": walk(sessions, 6)}, {"lvl": daily(later)}, monkeypatch)

    assert cell.sessions == len(sessions) - 10, "counted from the first point, never as zero"


def test_the_same_pins_give_the_same_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    level = {day: (1.0 if (n // 3) % 2 else -1.0) for n, day in enumerate(sessions)}
    spec = screen(
        {"up": TEST, "up_cc": {**TEST, "entry": "close"}},
        followers="ab",
        demean=["weekday", "month"],
    )
    prices = {"a": walk(sessions, 7), "b": walk(sessions, 8)}

    one = measured(spec, prices, {"lvl": daily(level)}, monkeypatch, draws=99)
    two = measured(spec, prices, {"lvl": daily(level)}, monkeypatch, draws=99)

    assert [cell.model_dump_json() for cell in one] == [cell.model_dump_json() for cell in two]
    assert [cell.key for cell in one] == [
        "condition/up/a/circular",
        "condition/up/b/circular",
        "condition/up_cc/a/circular",
        "condition/up_cc/b/circular",
    ]


def test_a_close_to_close_period_runs_from_the_previous_session_s_last_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    prices = {"a": {day: (100.0 + n, 100.5 + n) for n, day in enumerate(sessions)}}
    every = {day: 1.0 for day in sessions}
    spec = screen({"up": {**TEST, "entry": "close", "against": None}}, payoff="long_flat")

    (cell,) = measured(spec, prices, {"lvl": daily(every)}, monkeypatch)

    found = cell.condition
    assert cell.response is not None and found is not None
    assert cell.response.unfilled == 1, "the window's first session has no previous close"
    closes = [prices["a"][day][1] for day in sessions]
    moves = [1e4 * math.log(b / a) for a, b in zip(closes, closes[1:], strict=False)]
    assert found.drift_bp_day == pytest.approx(sum(moves) / len(moves))
    assert cell.sessions == len(sessions) - 1, "a weekend between two sessions is one period"


def test_a_close_to_close_period_across_a_day_the_market_opened_holds_no_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    missing = date(2026, 1, 14)
    prices = {"a": {day: (100.0, 101.0) for day in sessions if day != missing}}
    spec = screen({"up": {**TEST, "entry": "close", "against": None}}, payoff="long_flat")

    (cell,) = measured(spec, prices, {"lvl": daily(dict.fromkeys(sessions, 1.0))}, monkeypatch)

    assert cell.response is not None and cell.response.unfilled == 2
    assert cell.sessions == len(sessions) - 1 - 2
    # Martin Luther King Jr. Day is closed, so the period across it is one.
    assert date(2026, 1, 19) not in sessions


def test_the_four_entry_and_exit_pairs_score_the_periods_the_table_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    prices = {"a": {day: (100.0 + n, 100.4 + n) for n, day in enumerate(sessions)}}
    every = {"lvl": daily(dict.fromkeys(sessions, 1.0))}
    opens = [prices["a"][day][0] for day in sessions]
    closes = [prices["a"][day][1] for day in sessions]
    expected = {
        ("open", "close"): [math.log(c / o) for o, c in zip(opens, closes, strict=True)],
        ("close", "close"): [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False)],
        ("open", "open"): [math.log(b / a) for a, b in zip(opens, opens[1:], strict=False)],
        ("close", "open"): [math.log(b / a) for a, b in zip(closes, opens[1:], strict=False)],
    }
    for (entry, exit_), moves in expected.items():
        test = {**TEST, "entry": entry, "exit": exit_, "against": None}
        (cell,) = measured(screen({"up": test}, payoff="long_flat"), prices, every, monkeypatch)
        assert cell.condition is not None
        assert cell.condition.drift_bp_day == pytest.approx(1e4 * sum(moves) / len(moves))
        assert cell.sessions == len(moves)


def test_contrast_against_rest_and_against_a_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    cycle = [2.0, 0.5, -1.0, 0.0]
    level = {day: cycle[n % 4] for n, day in enumerate(sessions)}
    moves = {2.0: 30.0, 0.5: 10.0, -1.0: -20.0, 0.0: 0.0}
    prices = {"a": {day: (100.0, 100.0 * math.exp(moves[level[day]] / 1e4)) for day in sessions}}
    tests = {
        "rest": {**TEST, "when": {"gt": 1}, "against": "rest"},
        "versus": {**TEST, "when": {"gt": 1}, "against": {"lt": 0}},
        "short": {**TEST, "when": {"lt": 0}, "against": {"ge": 1}, "side": "short"},
    }

    rest, versus, short = measured(screen(tests), prices, {"lvl": daily(level)}, monkeypatch)

    others = [moves[level[day]] for day in sessions if level[day] < 1]
    assert rest.mean == pytest.approx(30.0 - sum(others) / len(others), rel=1e-6)
    assert versus.mean == pytest.approx(30.0 + 20.0, rel=1e-6)
    assert versus.sessions < rest.sessions, "a session in neither state holds no contrast value"
    assert short.mean == pytest.approx(30.0 + 20.0, rel=1e-6), "signed by the side, so positive"
    assert rest.params["against"] == "rest" and versus.params["against"] == "lt 0"


def test_a_contrast_with_one_state_empty_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()

    (cell,) = measured(
        screen(),
        {"a": walk(sessions, 9)},
        {"lvl": daily(dict.fromkeys(sessions, 1.0))},
        monkeypatch,
    )

    assert cell.sessions == 0 and cell.mean == 0.0 and cell.p == 1.0


def test_a_control_demeans_and_one_constant_over_the_window_is_dropped_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    rng = np.random.default_rng(11)
    level = {day: float(rng.choice([-1.0, 1.0])) for day in sessions}
    event = {day: float(n % 5 == 0) for n, day in enumerate(sessions)}
    for day in sessions:  # an event day is a +25 bp session, and leans to the favoured state
        if event[day]:
            level[day] = 1.0
    prices = {
        "a": {
            day: (100.0, 100.0 * math.exp((rng.normal() * 10 + 25 * event[day]) / 1e4))
            for day in sessions
        }
    }
    points = {
        "lvl": daily(level),
        "evt": daily(event, hour=9),
        "flg": daily(dict.fromkeys(sessions, 0.0), hour=9),
    }
    plain = screen()
    controlled = screen(controls=["evt", "flg"])

    (raw,) = measured(plain, prices, points, monkeypatch)
    (cell,) = measured(controlled, prices, points, monkeypatch)

    found = cell.condition
    assert found is not None
    assert found.dropped == ["control:flg"]
    assert abs(cell.mean) < abs(raw.mean), "the event days' move is no longer the state's"
    assert found.raw_bp == pytest.approx(raw.mean)
    assert found.control_r2 is not None and found.control_r2 > 0


def test_a_control_not_zero_or_one_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    points = {
        "lvl": daily(dict.fromkeys(sessions, 1.0)),
        "evt": daily(dict.fromkeys(sessions, 2.0)),
    }

    with pytest.raises(
        PreconditionError, match="controls.evt: 2 on 2026-01-05; a control is 0 or 1"
    ):
        measured(screen(controls=["evt"]), {"a": flat(sessions)}, points, monkeypatch)


def test_a_control_describing_another_day_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    ones = daily({sessions[0]: 1.0})
    points = {"lvl": daily(dict.fromkeys(sessions, 1.0)), "evt": ones}

    with pytest.raises(PreconditionError, match="is dated 2026-01-05") as refused:
        measured(screen(controls=["evt"]), {"a": flat(sessions)}, points, monkeypatch)

    assert "session of 2026-01-06" in refused.value.message
    assert "ts_event on that date" in (refused.value.remedy or "")


def test_a_calendar_control_without_a_calendar_over_the_window_is_refused() -> None:
    lead = {"id": "lead_lag", "from": "a", "to": "b", "estimator": "hy", "lags": ["1m"]}
    spec = screen(demean=[{"turn_of_month": [1, 3]}, "pre_holiday"])
    spec = Screen.model_validate(
        {**spec.model_dump(by_alias=True), "measures": [lead, *spec.model_dump()["measures"]]}
    )
    defined = {f"{s}.XNAS": instrument(s) for s in ("AAA", "BBB", "QQQ")}

    condition.refuse_uncalendared(spec, defined, WINDOW)
    with pytest.raises(
        PreconditionError, match=r"measures\.1\.demean: turn_of_month and pre_"
    ) as one:
        condition.refuse_uncalendared(spec, defined, (date(2003, 9, 1), date(2003, 12, 31)))
    assert "holds 2003-09-10..2027-09-06" in one.value.message
    assert "supply them as 0/1 controls" in (one.value.remedy or "")
    crypto = {**defined, "AAA.XNAS": perpetual()}
    with pytest.raises(PreconditionError, match="none is on file"):
        condition.refuse_uncalendared(spec, crypto, WINDOW)
    condition.refuse_uncalendared(screen(demean=["weekday"]), crypto, WINDOW)


def test_turn_of_month_and_pre_holiday_demean_on_the_follower_s_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    spec = screen(demean=[{"turn_of_month": [1, 3]}, "third_friday_week", "pre_holiday"])

    (cell,) = measured(spec, {"a": walk(sessions, 12)}, {"lvl": daily(level)}, monkeypatch)

    assert cell.condition is not None and "turn_of_month" not in cell.condition.dropped


def test_the_mde_is_the_critical_t_and_the_power_quantile_times_se(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from statistics import NormalDist

    sessions = trading()
    level = {day: (1.0 if (n // 3) % 2 else -1.0) for n, day in enumerate(sessions)}
    verdict = {"alpha": 0.1, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 20}
    spec = screen(verdict=verdict, mde_power=0.8)

    (cell,) = measured(spec, {"a": walk(sessions, 13)}, {"lvl": daily(level)}, monkeypatch)

    found = cell.condition
    assert found is not None and found.critical_t is not None and found.mde_bp is not None
    assert found.mde_bp == pytest.approx((found.critical_t + NormalDist().inv_cdf(0.8)) * cell.se)


def test_no_mde_without_a_verdict_or_a_power(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    points = {"lvl": daily(level)}
    verdict = {"alpha": 0.1, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 20}

    (unjudged,) = measured(screen(mde_power=0.8), {"a": flat(sessions)}, points, monkeypatch)
    (unasked,) = measured(screen(verdict=verdict), {"a": flat(sessions)}, points, monkeypatch)

    for cell in (unjudged, unasked):
        assert cell.condition is not None
        assert cell.condition.mde_bp is None and cell.condition.critical_t is None


def test_no_mde_when_alpha_is_below_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 16))
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    verdict = {"alpha": 0.05, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 24}
    spec = screen(verdict=verdict, mde_power=0.8, null_by="sign_flip", block=4)

    (cell,) = measured(spec, {"a": walk(sessions, 14)}, {"lvl": daily(level)}, monkeypatch)

    found = cell.condition
    assert found is not None and found.blocks == 3, "ten sessions are three blocks of four"
    assert 2.0 ** (1 - found.blocks) > 0.05
    assert found.critical_t is None and found.mde_bp is None


def test_a_quoted_follower_crosses_the_touch_at_entry_and_exit_sessions_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 2, 27))
    level = dict.fromkeys(sessions, -1.0)
    for day in sessions[5:8]:
        level[day] = 1.0
    prices = {"q": {day: (100.0 + n * 0.1, 100.05 + n * 0.1) for n, day in enumerate(sessions)}}
    test = {**TEST, "entry": "close", "against": None}
    spec = screen({"up": test}, payoff="long_flat", followers="q")

    (cell,) = measured(spec, prices, {"lvl": daily(level)}, monkeypatch, quoted=0.02)

    stats = cell.response
    assert stats is not None and stats.events == 1
    entry_ask = prices["q"][sessions[5]][1] + 0.02
    exit_bid = prices["q"][sessions[8]][1] - 0.02
    assert stats.gross_bp == pytest.approx(1e4 * math.log(exit_bid / entry_ask))
    assert stats.hurdle_bp == pytest.approx(2 * 1.0), "the commission alone at the touch"


def test_episodes_exposure_longest_run_and_signal_age(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 2, 27))
    pattern = [1, 1, 1, 0, -1, -1, 0, 1, 0, -1]
    level = {day: float(pattern[n % 10]) for n, day in enumerate(sessions)}

    (cell,) = measured(screen(), {"a": walk(sessions, 15)}, {"lvl": daily(level)}, monkeypatch)

    found = cell.condition
    assert found is not None
    states = [pattern[n % 10] for n in range(len(sessions))]
    runs = sum(1 for n, s in enumerate(states) if s == 1 and (n == 0 or states[n - 1] != 1))
    assert found.episodes == runs and found.longest_run == 3
    assert found.episodes_against == sum(
        1 for n, s in enumerate(states) if s == -1 and (n == 0 or states[n - 1] != -1)
    )
    assert found.exposure == pytest.approx(sum(1 for s in states if s) / len(states))
    assert found.signal_age_median_s == found.signal_age_max_s == pytest.approx(76 * 60)


def test_folds_adjusted_reestimate_inside_each_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    second = date(2026, 2, 15)  # the second of two equal calendar folds of the window
    moves = {day: 20.0 if level[day] > 0 and day < second else 0.0 for day in sessions}
    prices = {"a": {day: (100.0, 100.0 * math.exp(moves[day] / 1e4)) for day in sessions}}

    (cell,) = measured(screen(), prices, {"lvl": daily(level)}, monkeypatch, folds=2)

    found = cell.condition
    assert found is not None
    first, second = found.folds_adjusted
    assert first == pytest.approx(20.0, abs=1e-6) and second == pytest.approx(0.0, abs=1e-6)
    assert found.folds_adjusted_same_sign == 1


def test_control_r2_is_zero_with_the_intercept_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    level = {day: (1.0 if n % 3 else -1.0) for n, day in enumerate(sessions)}

    (cell,) = measured(screen(), {"a": walk(sessions, 16)}, {"lvl": daily(level)}, monkeypatch)

    assert cell.condition is not None and cell.condition.control_r2 == pytest.approx(0, abs=1e-12)
    assert cell.condition.dropped == []


def test_the_long_flat_realisation_is_reported_beside_a_contrast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = {day: (1.0 if (n // 2) % 2 else -1.0) for n, day in enumerate(sessions)}
    prices = {"a": walk(sessions, 17)}
    points = {"lvl": daily(level)}

    (contrast,) = measured(screen(), prices, points, monkeypatch)
    (lone,) = measured(
        screen({"up": {**TEST, "against": None}}, payoff="long_flat"), prices, points, monkeypatch
    )

    found = contrast.condition
    assert found is not None and lone.response is not None
    assert found.long_flat_bp == pytest.approx(lone.mean)
    assert found.long_flat_ceiling_bp_day == pytest.approx(lone.response.ceiling_bp_day)
    assert found.sessions_against is not None and found.episodes_against is not None


def test_traded_includes_a_condition_s_followers() -> None:
    assert hurdle.traded(screen(followers="ab")) == ("a", "b")


def test_an_assumption_is_stated_for_each_null() -> None:
    circular = screen().measures[0]
    blocks = screen(null_by="sign_flip", block=20).measures[0]
    assert isinstance(circular, Condition) and isinstance(blocks, Condition)

    assert "circular shift" in condition.assumption(circular)
    assert condition.assumption(blocks) == (
        "blocks of 20 sessions are roughly independent of each other"
    )


def test_a_session_s_marks_are_its_first_instant_and_its_last_point() -> None:
    spec = screen(followers="ab")
    measure = spec.measures[0]
    assert isinstance(measure, Condition)
    bars = Series(ts=np.asarray([5, 5, 9]), price=np.asarray([1.0, 2.0, 3.0]))
    empty = Series(ts=np.zeros(0, np.int64), price=np.zeros(0))

    found = condition.marks(spec, measure, {"a": bars, "b": empty})

    assert found == [(Mark(5, 2.0), Mark(9, 3.0)), None]
    quoted = screen(followers="q").measures[0]
    assert isinstance(quoted, Condition)
    touch = Series(
        ts=np.asarray([1, 2]),
        price=np.asarray([10.0, 11.0]),
        bid=np.asarray([9.5, 10.5]),
        ask=np.asarray([10.5, 11.5]),
    )
    assert condition.marks(screen(followers="q"), quoted, {"q": touch}) == [
        (Mark(1, 10.0, 9.5, 10.5), Mark(2, 11.0, 10.5, 11.5))
    ]


def test_a_follower_with_no_calendar_on_file_is_never_held_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    missing = date(2026, 1, 14)
    prices = {"a": {day: (100.0, 101.0) for day in sessions if day != missing}}
    spec = screen({"up": {**TEST, "entry": "close", "against": None}}, payoff="long_flat")
    defined = {
        "AAA.XNAS": perpetual(),
        "BBB.XNAS": instrument("BBB"),
        "QQQ.XNAS": instrument("QQQ"),
    }

    (cell,) = measured(
        spec,
        prices,
        {"lvl": daily(dict.fromkeys(sessions, 1.0))},
        monkeypatch,
        definitions=defined,
    )

    assert cell.response is not None and cell.response.unfilled == 1, "only the first session"


def test_a_session_whose_control_has_no_point_yet_is_not_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    event = {day: 0.0 for day in sessions[10:]}
    test = {**TEST, "entry": "close"}

    (cell,) = measured(
        screen({"up": test}, controls=["evt"]),
        {"a": walk(sessions, 18)},
        {"lvl": daily(level), "evt": daily(event, hour=9)},
        monkeypatch,
    )

    assert cell.sessions == len(sessions) - 10, "the first, invalid, and nine with no control"


def test_a_sign_flip_family_reads_its_critical_t_off_its_own_maxima(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = {day: (1.0 if (n // 3) % 2 else -1.0) for n, day in enumerate(sessions)}
    verdict = {"alpha": 0.1, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 20}
    spec = screen(verdict=verdict, mde_power=0.9, null_by="sign_flip", block=3)

    (cell,) = measured(spec, {"a": walk(sessions, 19)}, {"lvl": daily(level)}, monkeypatch)

    found = cell.condition
    assert found is not None and found.exact is False and found.critical_t is not None
    assert found.mde_bp is not None and found.mde_bp > found.critical_t * cell.se
