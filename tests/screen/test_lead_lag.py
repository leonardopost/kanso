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
        ([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]),
        ([np.nan, 1.0, 2.0, 3.0], [1.0, np.nan, 2.0, 3.0]),
    ],
)
def test_a_correlation_that_is_not_a_number_says_so(x: list[float], y: list[float]) -> None:
    assert math.isnan(lead_lag.correlation(np.asarray(x), np.asarray(y)))


def test_a_correlation_is_pearson_s() -> None:
    x = np.asarray([1.0, 2.0, 3.0, 4.0])
    assert lead_lag.correlation(x, 2 * x + 1) == pytest.approx(1.0)
    assert lead_lag.correlation(x, -x) == pytest.approx(-1.0)
