"""`catch_up`: a follower planted to catch up, to run away or to stand still exits as itself.

Every series here is stated point by point, one a second, so each test reads its answer off its
own arguments: when the leader moves, when the follower does, and which condition ends the
position. The leader jumps 50 bp at 10 s in all of them; the follower is what changes.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from kanso.schemas.screen import CatchUp, Screen
from kanso.schemas.venue import CostsOverride, resolve_venue_model
from kanso.screen import catch_up
from kanso.screen.fit import Moments
from kanso.screen.hurdle import Hurdles
from kanso.screen.sessions import Series

S = 1_000_000_000
COSTS = CostsOverride(commission_bps=1.0, slippage_bps=0.5, spread="fixed_bps", fixed_bps=2.0)
MODEL = resolve_venue_model("XNAS", hypothesis_costs=COSTS, quotes_available=False)
QUOTED = resolve_venue_model(
    "XNAS", hypothesis_costs=CostsOverride(commission_bps=1.0, slippage_bps=0.5)
)
ROUND_TRIP = 2 * (1.0 + 0.5 + 1.0)
"""Commission, slippage and half the fixed spread, on each of two taker fills."""
JUMP = math.log(100.5 / 100.0) * 1e4
"""The leader's move, in basis points of log price."""


def screen(followers: str = "b", **extra: Any) -> Screen:
    measure = {
        "id": "catch_up",
        "leader": "a",
        "followers": followers,
        "within": "5s",
        "fit": "window",
        "beta_over": "5s",
        "enter_bp": [20.0],
        "close": [0.25],
        "stop": [2.0],
        "max_hold": "30s",
        "latency_ms": 0,
        **extra,
    }
    return Screen.model_validate(
        {
            "schema": 1,
            "id": "probe",
            "title": "probe",
            "thesis": "probe",
            "window": {"start": "2026-03-02", "end": "2026-03-31"},
            "legs": {
                "a": {"instrument": "AAA.XNAS", "type": "trade"},
                "b": {"instrument": "BBB.XNAS", "type": "trade"},
                "q": {"instrument": "QQQ.XNAS", "type": "quote"},
            },
            "groups": {"both": ["a", "b"]},
            "clock": {"hours": "overlap"},
            "measures": [measure],
        }
    )


def path(points: dict[int, float], last: int = 60, start: float = 100.0) -> Series:
    """One print a second from 1 to `last`, the price set at the given seconds and held."""
    price = start
    prices = []
    for second in range(1, last + 1):
        price = points.get(second, price)
        prices.append(price)
    return Series(ts=np.arange(1, last + 1, dtype=np.int64) * S, price=np.asarray(prices))


def moved(bp: float) -> float:
    return 100.0 * math.exp(bp / 1e4)


def hurdles(spec: Screen, model: Any = MODEL) -> Hurdles:
    return Hurdles(
        spec,
        {"XNAS": model},
        {"a": "XNAS", "b": "XNAS", "q": "XNAS"},
        {"a": 1.0, "b": 1.0, "q": 1.0},
        {"a": 0.01, "b": 0.01, "q": 0.01},
    )


def tally(
    spec: Screen,
    series: dict[str, Series],
    beta: float | None = 1.0,
    model: Any = MODEL,
    follower: str = "b",
    overlap: bool = True,
) -> catch_up.Tally:
    measure = spec.measures[0]
    assert isinstance(measure, CatchUp)
    slopes = {("a", follower): beta}
    found = catch_up.session(
        spec, measure, series, (0, 10**12), overlap, {}, slopes, hurdles(spec, model)
    )
    assert found[0] is not None
    return found[0]


def follows(points: dict[int, float], last: int = 60) -> dict[str, Series]:
    return {"a": path({10: 100.5}, last), "b": path(points, last), "q": path({}, last)}


def test_a_follower_that_catches_up_is_closed_when_its_gap_has() -> None:
    found = tally(screen(), follows({13: moved(50.0)}))

    assert (found.events, found.closed, found.stopped, found.capped) == (1, 1, 0, 0)
    assert found.holds == [3.0]
    assert found.gross == pytest.approx(50.0) and found.hits == 1
    assert found.hurdle == pytest.approx(ROUND_TRIP)
    assert found.signal == pytest.approx(50.0 - 50.0 * 3 / 59, rel=1e-6)


def test_a_follower_that_runs_the_other_way_is_stopped_and_a_gap_still_open_is_entered_again() -> (
    None
):
    """Stopped at 12 s, the gap from 8 s is still open at 13 s and wider than any entry: the
    rule enters it again, and that position, which neither closes nor widens, is capped."""
    found = tally(screen(), follows({12: moved(-60.0)}))

    assert (found.events, found.closed, found.stopped, found.capped) == (2, 0, 1, 1)
    assert found.holds == [2.0, 30.0] and found.hits == 0
    assert found.gross == pytest.approx(-60.0)


def test_a_follower_that_never_moves_is_capped_and_the_cap_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catch_up, "FIRST_LOOK", 2)

    found = tally(screen(), follows({}))

    assert (found.events, found.capped) == (1, 1)
    assert found.holds == [30.0] and found.gross == pytest.approx(0.0)


def test_an_exit_found_after_several_looks_is_the_first_instant_it_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catch_up, "FIRST_LOOK", 2)

    found = tally(screen(), follows({21: moved(50.0)}))

    assert (found.closed, found.holds) == (1, [11.0])


def test_a_follower_that_moves_against_its_leader_is_traded_that_way() -> None:
    found = tally(screen(), follows({13: moved(-50.0)}), beta=-1.0)

    assert (found.events, found.closed) == (1, 1)
    assert found.gross == pytest.approx(50.0)


def test_a_follower_moving_alone_is_not_a_lag() -> None:
    series = {"a": path({}), "b": path({10: moved(-60.0)}), "q": path({})}

    assert tally(screen(), series).events == 0


def test_a_gap_under_the_entry_does_not_fire() -> None:
    assert tally(screen(enter_bp=[60.0]), follows({13: moved(50.0)})).events == 0


def test_a_leader_that_keeps_moving_moves_the_target_with_it() -> None:
    """The leader jumps again at 12 s: a follower that matched the first jump at 13 s has not
    caught up with the second, so the position holds until it matches both."""
    series = {
        "a": path({10: 100.5, 12: 101.0}),
        "b": path({13: moved(50.0), 16: moved(math.log(1.01) * 1e4)}),
        "q": path({}),
    }

    found = tally(screen(), series)

    assert (found.closed, found.holds) == (1, [6.0])


def test_a_catch_up_faster_than_the_latency_is_in_the_entry_already() -> None:
    found = tally(screen(latency_ms=5_000), follows({12: moved(50.0)}))

    assert (found.events, found.closed) == (1, 1)
    assert found.holds == [5.0] and found.gross == pytest.approx(0.0)


def test_one_position_at_a_time() -> None:
    """The gap is open at every second from 10 s to 14 s; one position holds them all."""
    found = tally(screen(), follows({14: moved(50.0)}))

    assert found.events == 1


def test_a_quoted_follower_crosses_its_own_spread() -> None:
    mids = path({13: moved(50.0)})
    half = 0.02
    quotes = Series(ts=mids.ts, price=mids.price, bid=mids.price - half, ask=mids.price + half)
    spec = screen(followers="q")
    series = {"a": path({10: 100.5}), "b": path({}), "q": quotes}

    found = tally(spec, series, model=QUOTED, follower="q")

    expected = math.log((moved(50.0) - half) / (100.0 + half)) * 1e4
    assert found.gross == pytest.approx(expected)
    # the commission alone: struck at the touch, a fill pays neither spread nor slippage_bps
    assert found.hurdle == pytest.approx(2 * 1.0)


def test_a_position_the_session_cannot_close_is_unfilled_and_ends_the_session() -> None:
    found = tally(screen(), follows({}, last=20))

    assert (found.events, found.unfilled) == (0, 1)


def test_a_leader_moving_after_the_follower_s_last_print_cannot_be_entered() -> None:
    series = {"a": path({10: 100.5}, last=20), "b": path({}, last=8), "q": path({})}
    measure = screen().measures[0]
    assert isinstance(measure, CatchUp)

    (found,) = catch_up.session(
        screen(), measure, series, (0, 10**12), False, {}, {("a", "b"): 1.0}, hurdles(screen())
    )

    assert found is not None and (found.events, found.unfilled) == (0, 1)


def test_a_pair_with_no_response_or_a_leg_that_never_printed_holds_no_value() -> None:
    spec = screen()
    measure = spec.measures[0]
    assert isinstance(measure, CatchUp)
    series = follows({13: moved(50.0)})
    priced = hurdles(spec)

    assert catch_up.session(spec, measure, series, (0, 10**12), True, {}, {}, priced) == [None]
    silent = {**series, "b": Series(ts=np.zeros(0, np.int64), price=np.zeros(0))}
    assert catch_up.session(
        spec, measure, silent, (0, 10**12), True, {}, {("a", "b"): 1.0}, priced
    ) == [None]


def test_a_follower_that_printed_once_has_no_drift_and_nothing_to_leave_at() -> None:
    series = {
        "a": path({10: 100.5}),
        "b": Series(ts=np.asarray([1 * S]), price=np.asarray([100.0])),
    }
    series["q"] = path({})

    found = tally(screen(), series, overlap=False)

    assert (found.events, found.unfilled) == (0, 1)


def test_the_response_is_the_slope_of_the_follower_s_returns_on_the_leader_s() -> None:
    rng = np.random.default_rng(7)
    steps = rng.normal(0.0, 1e-3, 60)
    lead = 100.0 * np.exp(np.cumsum(steps))
    series = {
        "a": Series(ts=np.arange(1, 61, dtype=np.int64) * S, price=lead),
        "b": Series(
            ts=np.arange(1, 61, dtype=np.int64) * S, price=100.0 * np.exp(-2 * np.cumsum(steps))
        ),
        "q": path({}),
    }
    spec = screen(beta_over="1s")
    measure = spec.measures[0]
    assert isinstance(measure, CatchUp)

    found = catch_up.moments(spec, measure, series, (0, 10**12), True, {})

    assert catch_up.slope(found[("a", "b")]) == pytest.approx(-2.0)
    flat = {**series, "a": path({})}
    still = catch_up.moments(spec, measure, flat, (0, 10**12), True, {})[("a", "b")]
    assert still.n == 59 and catch_up.slope(still) is None
    silent = {**series, "b": Series(ts=np.zeros(0, np.int64), price=np.zeros(0))}
    assert catch_up.moments(spec, measure, silent, (0, 10**12), True, {})[("a", "b")] == Moments()


def test_a_cell_is_a_follower_an_entry_a_close_and_a_stop_and_never_the_leader() -> None:
    spec = screen(followers="both", enter_bp=[10.0, 20.0], close=[0.0, 0.5])
    measure = spec.measures[0]
    assert isinstance(measure, CatchUp)

    keys = catch_up.cells(spec, measure)

    assert len(keys) == 4 and {key.follower for key in keys} == {"b"}
    assert keys[0].name == "catch_up/a>b/10bp/5s/close0/stop2"
    assert keys[0].params["fit"] == "window" and keys[0].params["max_hold"] == "30s"
    assert catch_up.pairs(spec, measure) == (("a", "b"),)


def test_exits_describe_the_holds_the_conditions_produced() -> None:
    days = [catch_up.Tally(closed=2, holds=[1.0, 3.0]), catch_up.Tally(stopped=1, holds=[10.0])]

    found = catch_up.exits(days, 0.5)

    assert (found.closed, found.stopped, found.capped, found.beta) == (2, 1, 0, 0.5)
    assert found.hold_median_s == 3.0 and found.hold_p90_s == pytest.approx(8.6)
    empty = catch_up.exits([], None)
    assert (empty.hold_median_s, empty.hold_p90_s, empty.beta) == (0.0, 0.0, None)
