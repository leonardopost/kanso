"""`lead_lag` on the grid: a planted lead reads at its lag and its sign, and nowhere else."""

from __future__ import annotations

import math

import numpy as np
import pytest

from kanso.schemas.screen import LeadLag, Screen
from kanso.screen import lead_lag
from kanso.screen.sessions import Series

S = 1_000_000_000


def planted(shift: int, count: int = 4000, seed: int = 5) -> dict[str, Series]:
    """`b` repeats `a`'s return `shift` seconds later, plus noise; one print a second each."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(scale=1e-3, size=count + shift)
    a = np.cumsum(shocks[shift:])
    b = np.cumsum(shocks[:count] + rng.normal(scale=5e-4, size=count))
    times = np.arange(1, count + 1, dtype=np.int64) * S
    return {
        "a": Series(ts=times, price=100.0 * np.exp(a)),
        "b": Series(ts=times, price=50.0 * np.exp(b)),
    }


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
        "clock": {"grid": "1s", "hours": "overlap"},
        "measures": [
            {
                "id": "lead_lag",
                "from": "a",
                "to": "b",
                "estimator": "grid",
                "lags": ["-3s", "-1s", "1s", "3s", "5s"],
            }
        ],
    }
)
MEASURE = SPEC.measures[0]
assert isinstance(MEASURE, LeadLag)


def test_a_planted_lead_reads_at_its_lag_and_nowhere_else() -> None:
    values, stale = lead_lag.session(SPEC, MEASURE, planted(3), (0, 10_000 * S), True, {})

    by_lag = dict(zip(MEASURE.lags, values.tolist(), strict=True))
    assert by_lag["3s"] > 0.8
    assert all(abs(by_lag[lag]) < 0.1 for lag in ("-3s", "-1s", "1s", "5s"))
    assert stale[0] == {"a": 0.0, "b": 0.0}


def test_a_lead_the_other_way_reads_at_the_negative_lag() -> None:
    swapped = planted(1)
    series = {"a": swapped["b"], "b": swapped["a"]}

    values, _ = lead_lag.session(SPEC, MEASURE, series, (0, 10_000 * S), True, {})

    by_lag = dict(zip(MEASURE.lags, values.tolist(), strict=True))
    assert by_lag["-1s"] > 0.8 and abs(by_lag["1s"]) < 0.1


def test_a_cell_is_named_by_its_pair_and_lag() -> None:
    keys = lead_lag.cells(SPEC, MEASURE)
    assert [key.name for key in keys][:2] == ["lead_lag/a>b/-3s", "lead_lag/a>b/-1s"]
    assert keys[0].params == {"from": "a", "to": "b", "lag": "-3s"}


def test_a_session_a_leg_never_printed_in_holds_no_value() -> None:
    series = {**planted(1), "b": Series(ts=np.zeros(0, np.int64), price=np.zeros(0))}

    values, stale = lead_lag.session(SPEC, MEASURE, series, (0, 10_000 * S), True, {})

    assert all(math.isnan(value) for value in values)
    assert stale == [{}] * len(MEASURE.lags)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        ([1.0, 2.0], [1.0, 2.0]),
        ([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]),
        ([np.nan, 1.0, 2.0, 3.0], [1.0, np.nan, 2.0, 3.0]),
    ],
)
def test_a_correlation_that_is_not_a_number_says_so(x: list[float], y: list[float]) -> None:
    assert math.isnan(lead_lag.correlation(np.asarray(x), np.asarray(y)))


def test_a_correlation_is_the_realised_one_of_returns_not_demeaned() -> None:
    x = np.asarray([1.0, -2.0, 3.0, -4.0])
    assert lead_lag.correlation(x, 2 * x) == pytest.approx(1.0)
    assert lead_lag.correlation(x, -x) == pytest.approx(-1.0)
    shifted = np.asarray([2.0, 2.0, 2.0, 2.0])
    assert lead_lag.correlation(np.ones(4), shifted) == pytest.approx(1.0)


MS = 1_000_000


def asynchronous(
    lag_ms: int, count: int = 200_000, seed: int = 9, every_ms: int = 20
) -> dict[str, Series]:
    """One efficient price, observed by `a` at its own random instants and by `b` at others,
    `lag_ms` later: b's print at u shows the price a showed at u - lag. `a` prints about
    once every `every_ms`, `b` a quarter less often."""
    rng = np.random.default_rng(seed)
    path = np.cumsum(rng.normal(scale=1e-4, size=count))
    a_ms = np.sort(rng.choice(np.arange(lag_ms, count), size=count // every_ms, replace=False))
    b_ms = np.sort(
        rng.choice(np.arange(lag_ms, count), size=count * 4 // (every_ms * 5), replace=False)
    )
    return {
        "a": Series(ts=a_ms.astype(np.int64) * MS, price=100.0 * np.exp(path[a_ms])),
        "b": Series(ts=b_ms.astype(np.int64) * MS, price=100.0 * np.exp(path[b_ms - lag_ms])),
    }


HY = Screen.model_validate(
    {
        **{
            key: value for key, value in SPEC.model_dump(by_alias=True).items() if key != "measures"
        },
        "clock": {"grid": "100ms", "hours": "overlap"},
        "measures": [
            {
                "id": "lead_lag",
                "from": "a",
                "to": "b",
                "estimator": "hy",
                "lags": ["-250ms", "-50ms", "50ms", "250ms", "1s"],
            },
            {"id": "lead_lag", "from": "a", "to": "b", "estimator": "grid", "lags": ["300ms"]},
            {"id": "lead_lag", "from": "a", "to": "b", "estimator": "hy", "lags": ["300ms"]},
        ],
    }
)


def test_between_asynchronous_prints_hy_finds_the_lead_at_its_delay() -> None:
    measure = HY.measures[0]
    assert isinstance(measure, LeadLag)

    values, stale = lead_lag.session(HY, measure, asynchronous(250), (0, 10**15), True, {})

    by_lag = dict(zip(measure.lags, values.tolist(), strict=True))
    assert by_lag["250ms"] > 0.8
    assert by_lag["250ms"] > max(by_lag[lag] for lag in ("-250ms", "-50ms", "50ms", "1s")) + 0.4
    assert stale == [{}] * len(measure.lags)


def test_a_fine_grid_shrinks_the_same_lead_where_hy_does_not() -> None:
    # Prints about once a second sampled every tenth of one: most grid steps hold no move,
    # which is the Epps effect, and the gridded correlation at the true lag collapses.
    sparse = asynchronous(300, count=2_000_000, every_ms=1_000)
    gridded, _ = lead_lag.session(HY, HY.measures[1], sparse, (0, 10**15), True, {})
    direct, _ = lead_lag.session(HY, HY.measures[2], sparse, (0, 10**15), True, {})

    assert direct[0] > 0.6
    assert gridded[0] < 0.5 * direct[0]


def test_points_that_share_an_instant_are_one_point_the_last() -> None:
    ts = np.asarray([1, 1, 2, 3, 4, 5, 6], dtype=np.int64) * S
    a = Series(ts=ts, price=np.asarray([1.0, 100.0, 101.0, 100.0, 102.0, 101.0, 103.0]))
    collapsed = Series(ts=ts[1:], price=a.price[1:])
    measure = HY.measures[0]
    assert isinstance(measure, LeadLag)
    one, _ = lead_lag.session(HY, measure, {"a": a, "b": a}, (0, 10 * S), True, {})
    two, _ = lead_lag.session(HY, measure, {"a": collapsed, "b": collapsed}, (0, 10 * S), True, {})
    assert one.tolist() == pytest.approx(two.tolist(), nan_ok=True)


def test_hy_reads_nothing_when_a_side_is_too_short_flat_or_absent() -> None:
    times = np.arange(1, 4, dtype=np.int64) * S
    flat = np.ones(10)
    long_times = np.arange(1, 11, dtype=np.int64) * S
    assert math.isnan(lead_lag.hayashi_yoshida(times, flat[:3], long_times, flat, 0))
    assert math.isnan(lead_lag.hayashi_yoshida(long_times, flat, long_times, flat, 0))
    measure = HY.measures[0]
    assert isinstance(measure, LeadLag)
    empty = {"a": Series(ts=np.zeros(0, np.int64), price=np.zeros(0)), "b": asynchronous(1)["b"]}
    values, _ = lead_lag.session(HY, measure, empty, (0, 10**15), True, {})
    assert all(math.isnan(value) for value in values)
