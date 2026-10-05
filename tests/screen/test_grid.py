"""Sampling a session: as of and never ahead, on one grid, levels that difference alike."""

from __future__ import annotations

import math

import numpy as np
import pytest

from kanso.schemas.screen import Screen
from kanso.screen import grid
from kanso.screen.sessions import Series

S = 1_000_000_000


def series(*rows: tuple[int, float]) -> Series:
    return Series(
        ts=np.asarray([ts for ts, _ in rows], dtype=np.int64),
        price=np.asarray([price for _, price in rows], dtype=np.float64),
    )


SPEC = Screen.model_validate(
    {
        "schema": 1,
        "id": "probe",
        "title": "probe",
        "thesis": "probe",
        "window": {"start": "2026-03-02", "end": "2026-03-31"},
        "legs": {
            "a": {"instrument": "AAA.XNAS", "type": "trade"},
            "b": {"instrument": "BBB.XNAS", "type": "trade"},
        },
        "derived": {
            "basket": {"basket": {"a": 0.5, "b": 0.5}},
            "fixed": {"spread": {"long": "a", "short": "b", "hedge": "fixed", "beta": 2.0}},
            "fitted": {"spread": {"long": "a", "short": "b", "hedge": "ols", "fit": "window"}},
            "gap": {"gap": {"a": "a", "b": "b"}},
        },
        "clock": {"grid": "1s", "hours": "overlap"},
        "measures": [
            {"id": "lead_lag", "from": "a", "to": "b", "estimator": "grid", "lags": ["1s"]}
        ],
    }
)


def test_a_value_is_the_newest_at_or_before_the_instant_and_none_before_the_first() -> None:
    # Two points at 2s: the later one in the order handed over is the value.
    leg = series((2 * S, 10.0), (2 * S, 11.0), (5 * S, 12.0))
    sampled = grid.asof(leg, np.asarray([1 * S, 2 * S, 4 * S, 5 * S, 9 * S]))
    assert math.isnan(sampled[0])
    assert sampled[1:].tolist() == [11.0, 11.0, 12.0, 12.0]


def test_the_grid_is_multiples_of_the_step_from_the_epoch() -> None:
    assert grid.grid((1_500_000_000, 4 * S), S).tolist() == [2 * S, 3 * S, 4 * S]
    assert grid.grid((2 * S, 2 * S), S).tolist() == [2 * S]


def test_a_cell_is_live_once_every_leg_has_printed() -> None:
    legs = {"a": series((3 * S, 1.0), (9 * S, 1.0)), "b": series((1 * S, 1.0), (7 * S, 1.0))}
    assert grid.live(["a", "b"], legs, (0, 20 * S), overlap=True) == (3 * S, 7 * S)
    assert grid.live(["a", "b"], legs, (5 * S, 20 * S), overlap=False) == (5 * S, 20 * S)
    assert grid.live(["a", "b"], {**legs, "b": series()}, (0, 20 * S), overlap=True) is None
    assert grid.live(["a"], {"a": series((3 * S, 1.0))}, (0, 20 * S), overlap=True) is None


def test_a_derived_leg_moves_at_its_legs_instants_once_all_have_printed() -> None:
    legs = {"a": series((3 * S, 1.0), (6 * S, 1.0)), "b": series((1 * S, 1.0), (4 * S, 1.0))}
    assert grid.instants(SPEC, "gap", legs).tolist() == [3 * S, 4 * S, 6 * S]
    assert grid.instants(SPEC, "a", legs).tolist() == [3 * S, 6 * S]
    assert grid.instants(SPEC, "gap", {**legs, "b": series()}).tolist() == []


def test_every_kind_of_leg_has_a_level_that_differences_alike() -> None:
    legs = {"a": series((1 * S, 100.0), (2 * S, 110.0)), "b": series((1 * S, 50.0))}
    at = np.asarray([1 * S, 2 * S])
    betas = {"fitted": 0.5}

    a = grid.level(SPEC, "a", legs, at, betas)
    assert a.tolist() == pytest.approx([math.log(100.0), math.log(110.0)])
    basket = grid.level(SPEC, "basket", legs, at, betas)
    assert basket.tolist() == pytest.approx(
        [0.5 * math.log(100.0) + 0.5 * math.log(50.0), 0.5 * math.log(110.0) + 0.5 * math.log(50.0)]
    )
    fixed = grid.level(SPEC, "fixed", legs, at, betas)
    assert fixed.tolist() == pytest.approx(
        [math.log(100.0) - 2 * math.log(50.0), math.log(110.0) - 2 * math.log(50.0)]
    )
    fitted = grid.level(SPEC, "fitted", legs, at, betas)
    assert fitted[1] - fitted[0] == pytest.approx(math.log(1.1))
    gap = grid.level(SPEC, "gap", legs, at, betas)
    assert gap.tolist() == pytest.approx([1.0, 1.2])


def test_a_leg_is_stale_over_every_interval_it_did_not_print_in() -> None:
    leg = series((1 * S, 1.0), (1 * S, 1.0), (3 * S, 1.0))
    times = np.asarray([0, 1 * S, 2 * S, 3 * S, 4 * S])
    assert grid.staleness(leg, times) == pytest.approx(2 / 4)
    assert grid.staleness(leg, times[:1]) == 1.0
