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
from kanso.screen import condition, hurdle, nulls, signals
from kanso.screen.condition import Mark
from kanso.screen.hurdle import Hurdles
from kanso.screen.run import Context, _folds
from kanso.screen.sessions import Series, days
from tests.nautilus.backtest.conftest import instrument, perpetual
from tests.screen.test_nulls import rejected

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
    window = changes.pop("window", WINDOW)
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
            "window": {"start": str(window[0]), "end": str(window[1])},
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


Prices = Mapping[date, tuple[float, float] | tuple[float]]
"""A follower's open and close each session, or the one price of a session holding one point."""
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
    """The measure's cells over these opens and closes and these signal points, over the
    screen's own window."""

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
    assert isinstance(measure, Condition) and spec.window is not None
    window = (spec.window.start, spec.window.end)
    sessions = days(spec, window)
    followers = spec.members(measure.followers)
    marks = []
    for day in sessions:
        row: list[condition.Marks | None] = []
        for follower in followers:
            held = prices[follower].get(day)
            if held is None:
                row.append(None)
                continue
            if len(held) == 1:
                row.append((Mark(at(day, 16), held[0]), Mark(at(day, 16), held[0])))
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
        window=window,
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
    fold_of = _folds(sessions, window, folds)
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
    # Centred on the mean position, the raw estimator carries none of the drift either: held
    # throughout, the drift is all in the ceiling.
    assert found.exposure == 1.0 and found.raw_bp == pytest.approx(0.0, abs=1e-9)
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


def utc(day: date, hour: int) -> int:
    """An instant in UTC as nanoseconds."""
    return int(datetime.combine(day, time(hour), tzinfo=ZoneInfo("UTC")).timestamp()) * 10**9


@pytest.mark.parametrize(
    ("entry", "standard", "daylight", "both"),
    [
        # 09:16 New York less 13:00 UTC: 14:16Z in EST, 13:16Z in EDT; ten sessions of each.
        ("open", 76 * 60, 16 * 60, (76 + 16) * 60 / 2),
        # 15:45 New York, a session's previous close less 15 minutes: 20:45Z in EST, 19:45Z in
        # EDT. Monday 2026-03-09 enters at Friday's close, in EST: ten of EST to nine of EDT.
        ("close", 7 * 3600 + 45 * 60, 6 * 3600 + 45 * 60, 7 * 3600 + 45 * 60),
    ],
)
def test_a_table_stamped_at_13_utc_is_read_at_the_anchors_either_side_of_the_clock_change(
    monkeypatch: pytest.MonkeyPatch, entry: str, standard: int, daylight: int, both: float
) -> None:
    # New York moves to daylight time on 2026-03-08; the table stays at 13:00 UTC.
    every = [date(2026, 2, 23) + timedelta(days=n) for n in range(26)]
    table = [(utc(day, 13), 1.0 if n % 2 else -1.0) for n, day in enumerate(every)]
    test = {"up": {**TEST, "entry": entry}}
    winter = trading(date(2026, 2, 23), date(2026, 3, 6))
    summer = trading(date(2026, 3, 9), date(2026, 3, 20))

    found = {}
    for name, sessions in (("winter", winter), ("summer", summer), ("both", winter + summer)):
        (cell,) = measured(screen(test), {"a": walk(sessions, 22)}, {"lvl": table}, monkeypatch)
        assert cell.condition is not None
        found[name] = cell.condition

    assert found["winter"].signal_age_median_s == found["winter"].signal_age_max_s == standard
    assert found["summer"].signal_age_median_s == found["summer"].signal_age_max_s == daylight
    assert found["both"].signal_age_max_s == standard
    assert found["both"].signal_age_median_s == both


def test_a_point_at_the_decision_instant_is_read_and_one_a_nanosecond_later_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    points: list[tuple[int, float]] = []
    for n, day in enumerate(sessions):
        decision = at(day, 9, 31) - 900 * 1_000_000_000  # 09:16, fifteen minutes before the open
        value = 1.0 if n % 3 else -1.0
        points += [(decision, value), (decision + 1, -value)]
    test = {"up": {**TEST, "against": None}}

    (cell,) = measured(
        screen(test, payoff="long_flat"), {"a": walk(sessions, 31)}, {"lvl": points}, monkeypatch
    )

    found = cell.condition
    assert found is not None
    assert found.sessions_when == sum(1 for n in range(len(sessions)) if n % 3)
    assert found.signal_age_median_s == found.signal_age_max_s == 0.0


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


def test_a_fixed_hold_runs_on_the_follower_s_sessions_and_never_on_a_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 2, 2), date(2026, 2, 27))
    assert date(2026, 2, 16) not in sessions, "Washington's Birthday is a closure"
    level = dict.fromkeys(sessions, -1.0)
    level[date(2026, 2, 13)] = 1.0
    test = {**TEST, "against": None, "hold": {"sessions": 3}}

    (cell,) = measured(
        screen({"up": test}, payoff="long_flat"),
        {"a": walk(sessions, 23)},
        {"lvl": daily(level)},
        monkeypatch,
    )

    # Opened on Friday the 13th, held that day, Tuesday and Wednesday: the closure is no session.
    found = cell.condition
    assert found is not None and cell.response is not None
    assert found.sessions_when == 3 and cell.response.events == 3 and found.skipped == 0
    assert found.exposure == pytest.approx(3 / len(sessions))


def test_a_fixed_hold_counts_a_session_whose_period_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 2, 2), date(2026, 2, 27))
    missing = date(2026, 2, 18)  # the market opened, and the follower has no point
    level = dict.fromkeys(sessions, -1.0)
    level[date(2026, 2, 12)] = 1.0  # read at Thursday's close: the period into Friday
    test = {**TEST, "entry": "close", "against": None, "hold": {"sessions": 3}}

    (cell,) = measured(
        screen({"up": test}, payoff="long_flat"),
        {"a": {day: price for day, price in walk(sessions, 24).items() if day != missing}},
        {"lvl": daily(level)},
        monkeypatch,
    )

    # The hold is Friday, Tuesday and Thursday the 19th, whose period from Tuesday's close spans
    # the 18th and is invalid: it counts toward the three and holds no value.
    found = cell.condition
    assert found is not None and cell.response is not None
    assert found.sessions_when == 2 and cell.response.unfilled == 2
    assert cell.response.events == 1, "Friday's and Tuesday's periods are contiguous"


def test_a_fixed_hold_waits_flat_while_the_state_is_undefined_and_a_running_one_is_no_onset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 2, 2), date(2026, 2, 27))
    pattern = {5: 1.0, 6: 1.0, 10: 1.0}
    level = {day: pattern.get(n, -1.0) for n, day in enumerate(sessions) if n >= 5}
    test = {**TEST, "against": None, "hold": {"sessions": 2}}

    (cell,) = measured(
        screen({"up": test}, payoff="long_flat"),
        {"a": walk(sessions, 25)},
        {"lvl": daily(level)},
        monkeypatch,
    )

    # Before the first point the state is undefined, so the position is too and nothing is held;
    # the state the first point shows is already running, so the one onset is the eleventh's.
    found = cell.condition
    assert found is not None and cell.response is not None
    assert cell.sessions == len(sessions) - 5
    assert found.sessions_when == 2 and cell.response.events == 2
    assert found.episodes == 2 and found.skipped == 0


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


@pytest.mark.parametrize("entry", ["close", "open"])
def test_a_long_straight_into_a_short_is_two_round_trips_and_one_held_on_is_one(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    sessions = trading()
    # Runs of three sessions long, then three short, with no flat session between them.
    level = {day: (1.0 if (n // 3) % 2 else -1.0) for n, day in enumerate(sessions)}
    test = {"up": {**TEST, "entry": entry, "exit": entry}}

    (cell,) = measured(screen(test), {"a": walk(sessions, 29)}, {"lvl": daily(level)}, monkeypatch)

    # Each period enters at the session before the one it exits, decided on that one's point.
    decided = [level[day] for day in sessions[:-1]]
    runs = 1 + sum(1 for one, two in zip(decided, decided[1:], strict=False) if one != two)
    assert cell.response is not None and cell.response.events == runs
    assert cell.response.hurdle_bp == pytest.approx(ROUND_TRIP)


@pytest.mark.parametrize(("entry", "exit_"), [("open", "close"), ("close", "open")])
def test_a_period_whose_exit_is_not_the_next_one_s_entry_is_a_round_trip_of_its_own(
    monkeypatch: pytest.MonkeyPatch, entry: str, exit_: str
) -> None:
    sessions = trading()
    test = {"up": {**TEST, "entry": entry, "exit": exit_, "against": None}}

    (cell,) = measured(
        screen(test, payoff="long_flat"),
        {"a": walk(sessions, 30)},
        {"lvl": daily(dict.fromkeys(sessions, 1.0))},
        monkeypatch,
    )

    assert cell.response is not None and cell.response.events == cell.sessions > 0


def test_a_cell_s_folds_drift_adjusted_move_and_hit_rate_are_its_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    second = date(2026, 2, 15)  # the second of two equal calendar folds of the window
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}

    def move(day: date) -> float:
        return 3.0 if level[day] < 0 else 20.0 if day < second else -5.0

    prices = {"a": {day: (100.0, 100.0 * math.exp(move(day) / 1e4)) for day in sessions}}
    test = {"up": {**TEST, "against": None}}

    (cell,) = measured(screen(test, payoff="long_flat"), prices, {"lvl": daily(level)}, monkeypatch)

    # Open to close, each long session is a round trip of its own, its gross its move.
    stats = cell.response
    assert stats is not None
    longs = [day for day in sessions if level[day] > 0]
    centre = sum(move(day) for day in sessions) / len(sessions)
    assert stats.events == len(longs)
    assert stats.hit_rate == pytest.approx(sum(day < second for day in longs) / len(longs))
    assert stats.drift_adjusted_bp == pytest.approx(
        sum(move(day) - centre for day in longs) / len(longs)
    )
    net = {day: move(day) - ROUND_TRIP if level[day] > 0 else 0.0 for day in sessions}
    assert stats.ceiling_bp_day == pytest.approx(sum(net.values()) / len(sessions))
    for fold, mean in zip((True, False), cell.folds, strict=True):
        inside = [net[day] for day in sessions if (day < second) == fold]
        assert mean == pytest.approx(sum(inside) / len(inside))
    assert cell.folds_same_sign == 1


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


def ragged(sessions: Sequence[date], seed: int, held: float) -> dict[str, Prices]:
    """Two seeded followers, the second holding only the last `held` share of the sessions."""
    start = sessions[int(len(sessions) * (1 - held))]
    later = {day: price for day, price in walk(sessions, seed + 10_000).items() if day >= start}
    return {"a": walk(sessions, seed), "b": later}


def test_a_circular_family_is_judged_on_the_sessions_every_cell_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    prices = ragged(sessions, 27, 0.4)
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    points = {"lvl": daily(level), "flg": []}
    tests = {
        "up": TEST,
        "up_cc": {**TEST, "entry": "close"},
        "none": {**TEST, "signal": "flg"},
    }
    later = [day for day in sessions if day in prices["b"]]

    shifted = measured(screen(tests, followers="ab"), prices, points, monkeypatch)
    flipped = measured(
        screen(tests, followers="ab", null_by="sign_flip", block=2), prices, points, monkeypatch
    )

    # b's close-to-close test holds b's sessions but its first, and so is every cell judged: a
    # shift rotates one index, and a cell holding part of it would lose its values to the gaps.
    judged = {cell.key: cell.sessions for cell in shifted}
    assert judged == {
        **{
            f"condition/{test}/{name}/circular": len(later) - 1
            for test in ("up", "up_cc")
            for name in "ab"
        },
        "condition/none/a/circular": 0,
        "condition/none/b/circular": 0,
    }, "a cell holding nothing takes nothing from the rest"
    for cell in shifted[:4]:
        assert cell.condition is not None and cell.condition.shifts == len(later) - 2
    # Sign flips of blocks need no shared index, and each cell keeps its own sessions.
    assert [cell.sessions for cell in flipped[:4]] == [
        len(sessions),
        len(later),
        len(sessions) - 1,
        len(later) - 1,
    ]


@pytest.mark.parametrize("null", ["circular", "sign_flip"])
@pytest.mark.parametrize("held", [1.0, 0.4])
def test_with_nothing_planted_p_is_near_uniform_across_seeds(
    monkeypatch: pytest.MonkeyPatch, null: str, held: float
) -> None:
    sessions = trading()
    tests = {"up": TEST, "up_cc": {**TEST, "entry": "close"}}
    blocks = {} if null == "circular" else {"null_by": "sign_flip", "block": 5}
    spec = screen(tests, followers="ab", **blocks)
    smallest, shifts = [], set()
    for seed in range(200):
        rng = np.random.default_rng(3000 + seed)
        side, level = 1.0, {}
        for day in sessions:  # a state persisting for runs averaging six sessions
            side = -side if rng.random() < 1 / 6 else side
            level[day] = side
        cells = measured(spec, ragged(sessions, seed, held), {"lvl": daily(level)}, monkeypatch, 99)
        smallest.append(min(cell.p for cell in cells))
        shifts |= {cell.condition.shifts for cell in cells if cell.condition is not None}
    # Every shift taken over the N sessions the family shares puts the smallest p at or under
    # 0.1 exactly floor(0.1 N) / N of the time; 99 drawn sign vectors, (1 + 9) / 100.
    nominal = 10 / 100
    if null == "circular":
        (taken,) = shifts
        assert taken is not None
        nominal = math.floor(0.1 * (taken + 1)) / (taken + 1)
    rejected(np.asarray(smallest), 0.1, nominal)


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


def test_a_block_longer_than_the_window_is_one_block_and_judges_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 16))
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}

    (cell,) = measured(
        screen(null_by="sign_flip", block=40),
        {"a": walk(sessions, 26)},
        {"lvl": daily(level)},
        monkeypatch,
    )

    assert cell.condition is not None and cell.condition.blocks == 1
    assert cell.sessions == len(sessions) and cell.mean != 0.0
    assert cell.t == 0.0 and cell.se == 0.0 and cell.p == 1.0, "one block has no spread"


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


@pytest.mark.parametrize("drawn", [{}, {"null_by": "sign_flip", "block": 1}])
def test_a_drawn_null_gives_the_same_bytes_from_the_same_pins_and_others_from_another_seed(
    monkeypatch: pytest.MonkeyPatch, drawn: dict[str, Any]
) -> None:
    sessions = trading()
    level = {day: (1.0 if (n // 3) % 2 else -1.0) for n, day in enumerate(sessions)}
    verdict = {"alpha": 0.5, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 20}
    tests = {"up": TEST, "up_cc": {**TEST, "entry": "close"}}
    spec = screen(tests, followers="ab", verdict=verdict, mde_power=0.8, **drawn)
    prices = {"a": walk(sessions, 7), "b": walk(sessions, 8)}
    points = {"lvl": daily(level)}

    one, two = (measured(spec, prices, points, monkeypatch, draws=9) for _ in range(2))
    monkeypatch.setattr(nulls, "seed", lambda *pins: 1)
    other = measured(spec, prices, points, monkeypatch, draws=9)

    # Nine of 56 shifts, or of 2^57 sign vectors: a sample, drawn from the measure's own seed.
    assert all(cell.condition is not None and not cell.condition.exact for cell in one)
    assert [cell.model_dump_json() for cell in one] == [cell.model_dump_json() for cell in two]

    def sampled(cells: list[Cell]) -> list[tuple[float, float | None, float | None]]:
        return [
            (cell.p, cell.condition.critical_t, cell.condition.mde_bp)
            for cell in cells
            if cell.condition is not None
        ]

    assert sampled(one) != sampled(other), "another seed draws another sample"


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


def test_a_session_holding_one_point_has_no_period_inside_it_and_enters_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading(date(2026, 1, 5), date(2026, 1, 30))
    prices = {"a": {**flat(sessions, 5), sessions[3]: (100.0,)}}
    every = {"lvl": daily(dict.fromkeys(sessions, 1.0))}
    within = {"up": {**TEST, "against": None}}
    across = {"up": {**TEST, "entry": "close", "against": None}}

    (inside,) = measured(screen(within, payoff="long_flat"), prices, every, monkeypatch)
    (onward,) = measured(screen(across, payoff="long_flat"), prices, every, monkeypatch)

    # Its open is its close, so the session's own period is empty; its close still enters.
    assert inside.response is not None and inside.response.unfilled == 1
    assert inside.sessions == len(sessions) - 1
    assert onward.response is not None and onward.response.unfilled == 1, "only the first"


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
    # The held position stops at the 13th and opens again on the 16th: two round trips, never
    # one joined across the session that held no value.
    assert cell.response.events == 2


def test_a_period_across_days_before_the_calendar_s_span_is_not_held_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = (date(2003, 9, 2), date(2003, 9, 19))
    # The calendar on file opens on 2003-09-10, and answers that no day before it was closed.
    sessions = [day for day in trading(*window) if day.weekday() < 5]
    test = {"up": {**TEST, "entry": "close", "against": None}}

    (cell,) = measured(
        screen(test, payoff="long_flat", window=window),
        {"a": flat(sessions, 5)},
        {"lvl": daily(dict.fromkeys(sessions, 1.0))},
        monkeypatch,
    )

    # Friday the 5th to Monday the 8th crosses a weekend the calendar does not hold: scored.
    assert cell.response is not None and cell.response.unfilled == 1, "only the first session"
    assert cell.sessions == len(sessions) - 1


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
    ) as refused:
        measured(screen(controls=["evt"]), {"a": flat(sessions)}, points, monkeypatch)

    remedy = refused.value.remedy or ""
    assert "reload kanso_state_tape under AAA.XNAS one dataset at a time" in remedy
    assert "kanso data load --loader csv_parquet --spec <spec> --supersedes <dataset>" in remedy


def test_a_control_describing_another_day_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    ones = daily({sessions[0]: 1.0})
    points = {"lvl": daily(dict.fromkeys(sessions, 1.0)), "evt": ones}

    with pytest.raises(
        PreconditionError, match="is dated 2026-01-05 in America/New_York"
    ) as refused:
        measured(screen(controls=["evt"]), {"a": flat(sessions)}, points, monkeypatch)

    assert "session of 2026-01-06" in refused.value.message
    remedy = refused.value.remedy or ""
    assert "ts_event on the session's date in America/New_York" in remedy
    assert "ts_init at or before the period's exit" in remedy
    assert "kanso data load --loader csv_parquet --spec <spec> --supersedes <dataset>" in remedy


def test_a_control_is_dated_in_the_clock_s_zone_either_side_of_the_clock_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # New York moves to daylight time on 2026-03-08: 04:00 UTC is 23:00 the day before in EST
    # and the midnight that opens the day in EDT, and 00:00 UTC is the evening before in both.
    winter = trading(date(2026, 2, 23), date(2026, 3, 6))
    summer = trading(date(2026, 3, 9), date(2026, 3, 20))
    every = [date(2026, 2, 23) + timedelta(days=n) for n in range(26)]
    level = daily({day: (1.0 if n % 2 else -1.0) for n, day in enumerate(winter + summer)})
    spec = screen(controls=["evt"])

    def table(hour: int) -> dict[str, Points]:
        events = [(utc(day, hour), float(n % 3 == 0)) for n, day in enumerate(every)]
        return {"lvl": level, "evt": events}

    (noon,) = measured(spec, {"a": walk(winter + summer, 28)}, table(12), monkeypatch)
    (midnight,) = measured(spec, {"a": walk(summer, 28)}, table(4), monkeypatch)

    assert noon.sessions == len(winter + summer) and midnight.sessions == len(summer)
    with pytest.raises(PreconditionError, match="session of 2026-02-23 is dated 2026-02-22 in Am"):
        measured(spec, {"a": walk(winter + summer, 28)}, table(4), monkeypatch)
    with pytest.raises(PreconditionError, match="session of 2026-03-09 is dated 2026-03-08 in Am"):
        measured(spec, {"a": walk(summer, 28)}, table(0), monkeypatch)


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
    condition.refuse_uncalendared(spec, defined, (date(2027, 8, 2), date(2027, 8, 31)))
    with pytest.raises(PreconditionError, match=r"window touches, 2027-08-01\.\.2027-09-30, "):
        condition.refuse_uncalendared(spec, defined, (date(2027, 8, 2), date(2027, 9, 3)))
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

    # Every column holds something over the window: the turn of each month, the three
    # third-Friday weeks, and the Fridays before Martin Luther King Jr. and Washington's Birthday.
    assert cell.condition is not None and cell.condition.dropped == []


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


def test_a_cell_just_above_a_circular_critical_t_reads_a_p_at_alpha_and_one_at_it_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level = {day: (1.0 if (n // 3) % 2 else -1.0) for n, day in enumerate(sessions)}
    verdict = {"alpha": 0.1, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 20}
    found: list[nulls.Evidence] = []
    real = nulls.circular

    def kept(*args: Any) -> nulls.Evidence:
        found.append(real(*args))
        return found[-1]

    monkeypatch.setattr(nulls, "circular", kept)

    (cell,) = measured(
        screen(verdict=verdict, mde_power=0.8),
        {"a": walk(sessions, 13)},
        {"lvl": daily(level)},
        monkeypatch,
    )

    # Every one of the 57 shifts is taken, and p is (1 + reaching) / 58, never reaching / 57.
    (evidence,) = found
    assert evidence.exact and len(evidence.maxima) == len(sessions) - 1
    assert cell.condition is not None and cell.condition.critical_t is not None
    critical, total = cell.condition.critical_t, len(evidence.maxima) + 1
    assert (1 + int(np.sum(evidence.maxima > critical))) / total <= 0.1
    assert (1 + int(np.sum(evidence.maxima >= critical))) / total > 0.1


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


def test_a_long_or_flat_fold_reads_the_condition_and_not_the_fold_s_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    second = date(2026, 2, 15)  # the second of two equal calendar folds of the window
    level = {day: (1.0 if n % 2 else -1.0) for n, day in enumerate(sessions)}
    # 10 bp more while the state holds, in both folds, on a drift of +20 bp and then -20 bp.
    moves = {day: (20.0 if day < second else -20.0) + 10.0 * (level[day] > 0) for day in sessions}
    prices = {"a": {day: (100.0, 100.0 * math.exp(moves[day] / 1e4)) for day in sessions}}
    test = {"up": {**TEST, "against": None}}

    (cell,) = measured(screen(test, payoff="long_flat"), prices, {"lvl": daily(level)}, monkeypatch)

    # Inside a fold, the position less its mean there times the return is the exposure times
    # one less it times the 10 bp, whatever the fold drifted; the residuals' window mean is not
    # the fold's, so a position times its residual alone would read +12.1 and -7.8 here.
    found = cell.condition
    assert found is not None
    for fold, adjusted in zip((True, False), found.folds_adjusted, strict=True):
        held = [level[day] > 0 for day in sessions if (day < second) == fold]
        share = sum(held) / len(held)
        assert adjusted == pytest.approx(10.0 * share * (1 - share), rel=1e-9)
    assert found.folds_adjusted_same_sign == 2


def test_control_r2_is_zero_with_the_intercept_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = trading()
    level = {day: (1.0 if n % 3 else -1.0) for n, day in enumerate(sessions)}

    (cell,) = measured(screen(), {"a": walk(sessions, 16)}, {"lvl": daily(level)}, monkeypatch)

    assert cell.condition is not None and cell.condition.control_r2 == pytest.approx(0, abs=1e-12)
    assert cell.condition.dropped == []


def calendar(sessions: Sequence[date], event: Mapping[date, float]) -> np.ndarray:
    """The design `demean: [weekday, month]` and one control make over a window from a Monday
    in January to a Friday in March: the intercept, Tuesday to Friday, February, March, and the
    control."""
    return np.asarray(
        [
            [1.0, *(float(day.weekday() == k) for k in range(1, 5))]
            + [float(day.month == 2), float(day.month == 3), event[day]]
            for day in sessions
        ]
    )


def annihilated(matrix: np.ndarray, target: np.ndarray) -> np.ndarray:
    """`target` less its least-squares projection on `matrix`'s columns, by `numpy.linalg`."""
    return np.asarray(target - matrix @ np.linalg.lstsq(matrix, target, rcond=None)[0])


def leaning(sessions: Sequence[date], seed: int, neither: float = 0.0) -> tuple[Any, ...]:
    """A state leaning on Mondays, Tuesdays and the event days, a control marking every fifth
    session, and a follower 8 bp up while the state holds and 5 bp a weekday later in the week;
    a share `neither` of sessions is in neither state."""
    rng = np.random.default_rng(seed)
    event = {day: float(n % 5 == 2) for n, day in enumerate(sessions)}
    level: dict[date, float] = {}
    for day in sessions:
        lean = day.weekday() < 2 or bool(event[day])
        level[day] = 1.0 if rng.random() < (0.85 if lean else 0.15) else -1.0
        if rng.random() < neither:
            level[day] = 0.0
    moves = {
        day: rng.normal() * 30 + 8.0 * (level[day] > 0) + 5.0 * day.weekday() for day in sessions
    }
    prices = {"a": {day: (100.0, 100.0 * math.exp(moves[day] / 1e4)) for day in sessions}}
    points = {"lvl": daily(level), "evt": daily(event, hour=9)}
    returns = np.asarray([1e4 * math.log(prices["a"][d][1] / prices["a"][d][0]) for d in sessions])
    return level, event, prices, points, returns


def test_against_rest_and_long_or_flat_the_estimate_is_the_state_s_coefficient_attenuated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level, event, prices, points, returns = leaning(sessions, 31)
    design = {"demean": ["weekday", "month"], "controls": ["evt"]}
    rest = {"up": {**TEST, "against": "rest"}}
    lone = {"up": {**TEST, "against": None}}
    cells = {
        "rest": [
            measured(screen(rest, **design, **null), prices, points, monkeypatch)[0]
            for null in ({}, {"null_by": "sign_flip", "block": 1})
        ],
        "lone": [
            measured(
                screen(lone, payoff="long_flat", **design, **null), prices, points, monkeypatch
            )[0]
            for null in ({}, {"null_by": "sign_flip", "block": 1})
        ],
    }

    # The reference: the state's coefficient beside the design, and the state's R² on it.
    matrix = calendar(sessions, event)
    state = np.asarray([float(level[day] > 0) for day in sessions])
    beta = float(np.linalg.lstsq(np.column_stack([state, matrix]), returns, rcond=None)[0][0])
    left = annihilated(matrix, state)
    f = float(np.mean(state))
    r2 = 1.0 - float(left @ left) / float(np.sum((state - f) ** 2))
    assert 0.3 <= r2 <= 0.7, "the design shares much of the state"
    residuals = annihilated(matrix, returns)
    n, longs = len(sessions), float(np.sum(state))
    weights = {"rest": np.where(state > 0, n / longs, -n / (n - longs)), "lone": state - f}
    expected = {"rest": beta * (1 - r2), "lone": beta * f * (1 - f) * (1 - r2)}
    for name, (shifted, flipped) in cells.items():
        # Circular shifts read each weight residualised on the design, as the residuals were;
        # sign flips read the weight alone, which overstates se by about 1 / sqrt(1 - R²).
        values = annihilated(matrix, weights[name]) * residuals
        se = float(np.std(values, ddof=1)) / math.sqrt(n)
        alone = float(np.std(weights[name] * residuals, ddof=1)) / math.sqrt(n)
        assert alone / se >= 1.2
        for cell, spread in ((shifted, se), (flipped, alone)):
            assert cell.condition is not None and cell.condition.dropped == []
            assert cell.mean == pytest.approx(expected[name], rel=1e-9)
            assert cell.condition.control_r2 == pytest.approx(r2, rel=1e-9)
            assert cell.se == pytest.approx(spread, rel=1e-9) and cell.sessions == n
            assert cell.t == pytest.approx(cell.mean / cell.se, rel=1e-9)


def test_against_a_comparison_r2_is_the_state_s_over_the_sessions_in_either_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = trading()
    level, event, prices, points, returns = leaning(sessions, 32, neither=0.3)
    design = {"demean": ["weekday", "month"], "controls": ["evt"]}

    shifted, flipped = (
        measured(screen(**design, **null), prices, points, monkeypatch)[0]
        for null in ({}, {"null_by": "sign_flip", "block": 1})
    )

    matrix = calendar(sessions, event)
    positions = np.asarray([level[day] for day in sessions])
    moving = positions != 0
    long = (positions[moving] > 0).astype(np.float64)
    left = annihilated(matrix[moving], long)
    r2 = 1.0 - float(left @ left) / float(np.sum((long - long.mean()) ** 2))
    everywhere = (positions > 0).astype(np.float64)
    spread = annihilated(matrix, everywhere)
    whole = 1.0 - float(spread @ spread) / float(np.sum((everywhere - everywhere.mean()) ** 2))
    assert abs(r2 - whole) > 0.02, "the two R² differ, so the test tells them apart"
    # The residuals are fitted on every held session, a session in neither state too. Under
    # circular shifts each session's value is its weight residualised on the design over them
    # times its residual, so a session in neither state holds one; sign flips read the weight
    # alone, which a session in neither state does not hold.
    residuals = annihilated(matrix, returns)
    n, longs, shorts = len(sessions), int(np.sum(positions > 0)), int(np.sum(positions < 0))
    weights = np.where(positions > 0, n / longs, np.where(positions < 0, -n / shorts, 0.0))
    se = float(np.std(annihilated(matrix, weights) * residuals, ddof=1)) / math.sqrt(n)
    side = longs + shorts
    alone = np.where(positions > 0, side / longs, -side / shorts)[moving] * residuals[moving]
    mean = float(np.mean(residuals[positions > 0]) - np.mean(residuals[positions < 0]))
    for cell, spread in ((shifted, se), (flipped, float(np.std(alone, ddof=1)) / math.sqrt(side))):
        assert cell.condition is not None
        assert cell.condition.control_r2 == pytest.approx(r2, rel=1e-9)
        assert cell.mean == pytest.approx(mean, rel=1e-9)
        assert cell.se == pytest.approx(spread, rel=1e-9)
        assert cell.sessions == side < n, "the sessions in either state"


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
