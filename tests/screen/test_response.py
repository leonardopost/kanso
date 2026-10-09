"""`response`: a planted reaction reads as itself, and nothing else does.

Every series here is stated point by point, one a second, so each test reads its answer off
its own arguments: where the trigger fires, when the follower moves, and by how much.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from kanso.nautilus.costs import fill_cost
from kanso.schemas.screen import Response, Screen
from kanso.schemas.venue import CostsOverride, resolve_venue_model
from kanso.screen import response
from kanso.screen.hurdle import Hurdles
from kanso.screen.sessions import Series

S = 1_000_000_000
COSTS = CostsOverride(commission_bps=1.0, slippage_bps=0.5, spread="fixed_bps", fixed_bps=2.0)
MODEL = resolve_venue_model("XNAS", hypothesis_costs=COSTS, quotes_available=False)
QUOTED = resolve_venue_model(
    "XNAS", hypothesis_costs=CostsOverride(commission_bps=1.0, slippage_bps=0.5)
)


def screen(trigger: dict[str, Any], **extra: Any) -> Screen:
    legs = {
        "a": {"instrument": "AAA.XNAS", "type": "trade"},
        "b": {"instrument": "BBB.XNAS", "type": "trade"},
        "q": {"instrument": "QQQ.XNAS", "type": "quote"},
    }
    measure = {
        "id": "response",
        "trigger": trigger,
        "followers": "b",
        "side": "with",
        "horizons": ["5s"],
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
            "legs": legs,
            "derived": {
                "pair": {"spread": {"long": "a", "short": "b", "hedge": "fixed", "beta": 2.0}},
                "both": {"basket": {"a": 0.5, "b": 0.5}},
                "gap": {"gap": {"a": "a", "b": "b"}},
                "mixed": {"basket": {"a": 0.5, "q": 0.5}},
            },
            "clock": {"hours": "overlap"},
            "measures": [measure],
        }
    )


MOVE = {"leg": "a", "move_bp": [40.0], "within": "1s"}


def path(points: dict[int, float], last: int = 60, start: float = 100.0) -> Series:
    """One print a second from 1 to `last`, the price set at the given seconds and held."""
    price = start
    prices = []
    for second in range(1, last + 1):
        price = points.get(second, price)
        prices.append(price)
    return Series(ts=np.arange(1, last + 1, dtype=np.int64) * S, price=np.asarray(prices))


def tally(spec: Screen, series: dict[str, Series], model: Any = MODEL) -> response.Tally:
    measure = spec.measures[0]
    assert isinstance(measure, Response)
    hurdles = Hurdles(
        spec,
        {"XNAS": model},
        {"a": "XNAS", "b": "XNAS", "q": "XNAS"},
        {"a": 1.0, "b": 1.0, "q": 1.0},
        {"a": 0.01, "b": 0.01, "q": 0.01},
    )
    (found,) = response.session(spec, measure, series, (0, 10**12), True, {}, hurdles)
    assert found is not None
    return found


def reaction(after: int, bp: float = 30.0) -> dict[str, Series]:
    """`a` jumps 50 bp at 10s; `b` follows by `bp` at `10 + after` seconds."""
    return {
        "a": path({10: 100.5}),
        "b": path({10 + after: 100.0 * math.exp(bp / 1e4)}),
        "q": path({}),
    }


def test_a_planted_reaction_reads_as_itself_against_the_venue_s_round_trip() -> None:
    found = tally(screen(MOVE), reaction(after=2))

    assert found.events == 1 and found.unfilled == 0 and found.hits == 1
    assert found.gross == pytest.approx(30.0)
    assert found.hurdle == pytest.approx(2 * (1.0 + 0.5 + 1.0))
    assert found.signal == pytest.approx(30.0 - 30.0 * 5 / 59, rel=1e-6)


def test_a_reaction_faster_than_the_latency_is_already_in_the_entry() -> None:
    found = tally(screen(MOVE, latency_ms=3_000), reaction(after=2))

    assert found.events == 1 and found.gross == pytest.approx(0.0)


def test_against_bets_the_other_way() -> None:
    found = tally(screen(MOVE, side="against"), reaction(after=2))

    assert found.gross == pytest.approx(-30.0) and found.hits == 0


def test_one_position_at_a_time() -> None:
    # A second jump at 12s fires while the first event is held to 15s and is skipped; a
    # third at 30s fires after it closed.
    series = {
        "a": path({10: 100.5, 12: 101.01, 30: 101.6}),
        "b": path({}),
        "q": path({}),
    }
    assert tally(screen(MOVE), series).events == 2


def test_an_event_with_no_point_after_its_horizon_is_unfilled() -> None:
    series = {"a": path({57: 100.5}), "b": path({}), "q": path({})}

    found = tally(screen(MOVE), series)

    assert (found.events, found.unfilled, found.gross) == (0, 1, 0.0)


def test_a_quoted_follower_crosses_its_own_spread_and_is_charged_none() -> None:
    quotes = path({12: 100.0 * math.exp(30 / 1e4)})
    half = 0.02
    quotes = Series(
        ts=quotes.ts, price=quotes.price, bid=quotes.price - half, ask=quotes.price + half
    )
    spec = screen(MOVE, followers="q")

    found = tally(spec, {"a": path({10: 100.5}), "b": path({}), "q": quotes}, model=QUOTED)

    entry_ask, exit_bid = 100.0 + half, 100.0 * math.exp(30 / 1e4) - half
    assert found.gross == pytest.approx(math.log(exit_bid / entry_ask) * 1e4)
    assert found.hurdle == pytest.approx(2 * (1.0 + 0.5))


def test_a_z_trigger_fires_on_an_excursion_from_its_trailing_mean() -> None:
    wiggle = {second: 100.0 + (0.01 if second % 2 else -0.01) for second in range(1, 61)}
    series = {"a": path({**wiggle, 30: 101.0}), "b": path({}), "q": path({})}
    spec = screen({"leg": "a", "z": [3.0], "lookback": "20s"})

    assert tally(spec, series).events == 1


def test_drift_is_taken_out_of_the_signal_and_left_in_the_gross() -> None:
    trending = Series(
        ts=np.arange(1, 61, dtype=np.int64) * S, price=100.0 * np.exp(np.arange(60) * 1e-4)
    )
    found = tally(screen(MOVE), {"a": path({10: 100.5}), "b": trending, "q": path({})})

    assert found.gross == pytest.approx(5.0, rel=1e-6)
    assert found.signal == pytest.approx(0.0, abs=1e-6)


def test_a_derived_follower_pays_each_leg_s_round_trip_in_its_share() -> None:
    series = {"a": path({10: 100.5}), "b": path({}), "q": path({})}
    one_leg = 2 * (1.0 + 0.5 + 1.0)

    pair = tally(screen(MOVE, followers="pair"), series)
    basket = tally(screen(MOVE, followers="both"), series)
    gap = tally(screen(MOVE, followers="gap"), series)

    assert pair.hurdle == pytest.approx((1 + 2) * one_leg)
    assert basket.hurdle == pytest.approx((0.5 + 0.5) * one_leg)
    assert gap.hurdle == pytest.approx(2 * one_leg)


def test_per_share_commission_and_sale_fees_are_the_runner_s_own_arithmetic() -> None:
    costs = CostsOverride(
        commission_bps=0.0,
        commission_per_share=0.005,
        slippage_bps=0.0,
        spread="fixed_bps",
        fixed_bps=0.0,
        sell_fee_bps=0.2,
        sell_fee_per_share=0.0002,
    )
    model = resolve_venue_model("XNAS", hypothesis_costs=costs, quotes_available=False)
    found = tally(screen(MOVE), reaction(after=2), model=model)

    def fraction(price: float, sell: bool) -> float:
        charged = fill_cost(
            price,
            1.0,
            0.0,
            0.0,
            0.0,
            None,
            0.005,
            maker=False,
            sell=sell,
            sell_fee_bps=0.2,
            sell_fee_per_share=0.0002,
        )
        return charged / price

    exit_price = 100.0 * math.exp(30 / 1e4)
    assert found.hurdle == pytest.approx(
        (fraction(100.0, False) + fraction(exit_price, True)) * 1e4
    )


def test_a_cell_is_named_by_its_trigger_follower_and_horizon() -> None:
    spec = screen({"leg": "a", "move_bp": [10.0, 20.0], "within": "1s"}, horizons=["5s", "1m"])
    measure = spec.measures[0]
    assert isinstance(measure, Response)

    keys = response.cells(spec, measure)

    assert [key.name for key in keys] == [
        "response/a/10bp/1s/b/5s",
        "response/a/10bp/1s/b/1m",
        "response/a/20bp/1s/b/5s",
        "response/a/20bp/1s/b/1m",
    ]
    assert keys[0].params["side"] == "with" and keys[0].params["unit"] == "bp"


def test_a_session_a_leg_never_printed_in_is_not_live() -> None:
    spec = screen(MOVE)
    measure = spec.measures[0]
    assert isinstance(measure, Response)
    empty = Series(ts=np.zeros(0, np.int64), price=np.zeros(0))
    hurdles = Hurdles(spec, {"XNAS": MODEL}, {"b": "XNAS"}, {"b": 1.0}, {"b": 0.01})
    (found,) = response.session(
        spec, measure, {"a": path({}), "b": empty, "q": path({})}, (0, 10**12), True, {}, hurdles
    )
    assert found is None
    quiet = tally(spec, {"a": path({}), "b": path({}), "q": path({})})
    assert (quiet.events, quiet.unfilled) == (0, 0)


def quoted(points: dict[int, float], half: float = 0.02) -> Series:
    mids = path(points)
    return Series(ts=mids.ts, price=mids.price, bid=mids.price - half, ask=mids.price + half)


def test_a_short_on_a_quoted_follower_sells_the_bid_and_buys_back_the_ask() -> None:
    spec = screen(MOVE, followers="q", side="against")
    series = {"a": path({10: 100.5}), "b": path({}), "q": quoted({12: 100.3})}

    found = tally(spec, series, model=QUOTED)

    assert found.gross == pytest.approx(math.log((100.0 - 0.02) / (100.3 + 0.02)) * 1e4)


def test_a_derived_follower_s_quoted_leg_pays_the_half_spread_it_shows() -> None:
    spec = screen(MOVE, followers="mixed")
    series = {"a": path({10: 100.5}), "b": path({}), "q": quoted({}, half=0.05)}

    found = tally(spec, series)

    one_leg = 2 * (1.0 + 0.5 + 1.0)
    measured = 2 * (1.0 + 0.5 + 0.05 / 100.0 * 1e4)
    assert found.hurdle == pytest.approx(0.5 * one_leg + 0.5 * measured)


def test_a_round_trip_pays_the_model_s_ticks_whole_on_both_fills() -> None:
    """One tick of a cent stated: a taker priced at market has no limit to cap it, so the entry
    at 100 pays a basis point more and the exit at the moved price a cent of that price."""
    spec = screen(MOVE)
    measure = spec.measures[0]
    assert isinstance(measure, Response)
    ticked = resolve_venue_model(
        "XNAS",
        hypothesis_costs=CostsOverride(**{**COSTS.model_dump(), "slippage_ticks": 1.0}),
        quotes_available=False,
    )
    hurdles = Hurdles(
        spec,
        {"XNAS": ticked},
        {"a": "XNAS", "b": "XNAS", "q": "XNAS"},
        {"a": 1.0, "b": 1.0, "q": 1.0},
        {"a": 0.01, "b": 0.01, "q": 0.01},
    )
    series = reaction(after=2)

    (found,) = response.session(spec, measure, series, (0, 10**12), True, {}, hurdles)

    assert found is not None
    moved = 100.0 * math.exp(30.0 / 1e4)
    assert found.hurdle == pytest.approx(2 * (1.0 + 0.5 + 1.0) + 1.0 + 1e4 * 0.01 / moved)


def test_a_multiplied_leg_pays_its_ticks_per_contract() -> None:
    """A 50-times contract priced at 100 with an increment of 0.25: one tick is $12.50 a
    contract on a notional of $5,000, 25 bp at the entry and 0.25 of the moved price at the
    exit, as the runner charges a taker on that contract."""
    spec = screen(MOVE)
    measure = spec.measures[0]
    assert isinstance(measure, Response)
    ticked = resolve_venue_model(
        "XNAS",
        hypothesis_costs=CostsOverride(**{**COSTS.model_dump(), "slippage_ticks": 1.0}),
        quotes_available=False,
    )
    hurdles = Hurdles(
        spec,
        {"XNAS": ticked},
        {"a": "XNAS", "b": "XNAS", "q": "XNAS"},
        {"a": 1.0, "b": 50.0, "q": 1.0},
        {"a": 0.01, "b": 0.25, "q": 0.01},
    )

    (found,) = response.session(spec, measure, reaction(after=2), (0, 10**12), True, {}, hurdles)

    assert found is not None
    moved = 100.0 * math.exp(30.0 / 1e4)
    assert found.hurdle == pytest.approx(2 * (1.0 + 0.5 + 1.0) + 25.0 + 1e4 * 0.25 / moved)


def test_the_hurdle_s_line_names_the_ticks_only_when_a_model_states_them() -> None:
    from kanso.screen.hurdle import _line

    ticked = resolve_venue_model(
        "XNAS", hypothesis_costs=CostsOverride(**{**COSTS.model_dump(), "slippage_ticks": 1.0})
    )

    assert "slippage 0.5 bp + 1 ticks, spread fixed 2 bp" in _line(ticked, "screen")
    assert "slippage 0.5 bp, spread fixed 2 bp" in _line(MODEL, "screen")
