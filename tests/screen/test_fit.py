"""Fitting a spread's hedge: the slope a planted hedge was built with, merged exactly."""

from __future__ import annotations

import numpy as np
import pytest

from kanso.errors import PreconditionError
from kanso.schemas.screen import Spread
from kanso.screen import fit
from kanso.screen.sessions import Series

S = 1_000_000_000


def legs(beta: float, seed: int, count: int = 2000) -> dict[str, Series]:
    rng = np.random.default_rng(seed)
    short = 4.6 + np.cumsum(rng.normal(scale=1e-3, size=count))
    long = 1.0 + beta * short + rng.normal(scale=1e-4, size=count)
    times = np.arange(1, count + 1, dtype=np.int64) * S
    return {
        "a": Series(ts=times, price=np.exp(long)),
        "b": Series(ts=times[::2], price=np.exp(short[::2])),
    }


SPREAD = Spread(long="a", short="b", hedge="ols", fit="window")


def test_a_planted_hedge_is_the_slope_the_fit_recovers() -> None:
    moments = fit.session(SPREAD, legs(1.5, 1), (0, 10**13), True)

    assert moments.n == 1000
    assert fit.beta("pair", moments) == pytest.approx(1.5, rel=1e-2)


def test_sessions_merge_to_the_moments_of_their_samples_together() -> None:
    first = fit.session(SPREAD, legs(0.8, 2), (0, 10**13), True)
    second = fit.session(SPREAD, legs(0.8, 3), (0, 10**13), True)
    merged = first.merged(second)
    whole_x = np.concatenate(
        [np.log(item["a"].price[::2]) for item in (legs(0.8, 2), legs(0.8, 3))]
    )
    whole_y = np.concatenate([np.log(item["b"].price) for item in (legs(0.8, 2), legs(0.8, 3))])

    assert merged.n == len(whole_x)
    assert merged.mean_y == pytest.approx(whole_y.mean())
    assert merged.m2_y == pytest.approx(((whole_y - whole_y.mean()) ** 2).sum())
    assert merged.c_xy == pytest.approx(
        ((whole_x - whole_x.mean()) * (whole_y - whole_y.mean())).sum()
    )
    assert fit.Moments().merged(first) == first and first.merged(fit.Moments()) == first


def test_a_short_leg_that_never_moved_cannot_be_fitted() -> None:
    flat = {"a": legs(1.0, 4)["a"], "b": Series(ts=np.arange(1, 5) * S, price=np.ones(4))}

    with pytest.raises(PreconditionError, match="did not move over the fit span") as refused:
        fit.beta("pair", fit.session(SPREAD, flat, (0, 10**13), True))
    assert "fixed beta" in (refused.value.remedy or "")


def test_a_session_with_nothing_to_fit_gives_nothing() -> None:
    empty = Series(ts=np.zeros(0, np.int64), price=np.zeros(0))
    assert fit.session(SPREAD, {"a": empty, "b": empty}, (0, 10**13), True) == fit.Moments()
    nan = {
        "a": Series(ts=np.asarray([S, 2 * S]), price=np.asarray([np.nan, np.nan])),
        "b": legs(1.0, 5)["b"],
    }
    assert fit.session(SPREAD, nan, (0, 10**13), True).n == 0
