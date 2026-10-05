"""`screen.yaml`: the rules that make a screen admissible, and its round trip."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from hypothesis import given

from kanso.errors import ValidationError
from kanso.schemas import dump_yaml, parse_yaml
from kanso.schemas.screen import (
    LeadLag,
    Screen,
    ScreenResult,
    cells,
    hours_minutes,
    pairs,
    span_ns,
)
from tests.schemas import strategies as gen

BASE: dict[str, Any] = {
    "schema": 1,
    "id": "btc_miners",
    "title": "BTC leads the miners",
    "thesis": "BTC prints lead MARA by a second.",
    "window": {"start": "2026-07-01", "end": "2026-08-28"},
    "legs": {
        "btc": {"instrument": "BTC-USDT-SWAP.OKX", "type": "trade"},
        "mara": {"instrument": "MARA.XNAS", "type": "bar", "resolution": "1s"},
        "riot": {"instrument": "RIOT.XNAS", "type": "quote"},
    },
    "derived": {"miners": {"basket": {"mara": 0.5, "riot": 0.5}}},
    "groups": {"lead": ["btc"], "follow": ["mara", "riot", "miners"]},
    "clock": {"grid": "1s", "hours": {"tz": "America/New_York", "span": "09:30-16:00"}},
    "measures": [
        {
            "id": "lead_lag",
            "from": "lead",
            "to": "follow",
            "estimator": "grid",
            "lags": ["-5s", "1s", "2s"],
        },
        {
            "id": "response",
            "trigger": {"leg": "btc", "move_bp": [10, 20], "within": "2s"},
            "followers": "follow",
            "side": "with",
            "horizons": ["5s", "30s"],
            "latency_ms": 5,
        },
    ],
}


def build(**changes: Any) -> Screen:
    document = deepcopy(BASE)
    for key, value in changes.items():
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
    return Screen.model_validate(document)


@given(gen.screens())
def test_a_screen_round_trips(value: Screen) -> None:
    assert parse_yaml(Screen, dump_yaml(value)) == value


@given(gen.screen_results())
def test_a_result_round_trips(value: ScreenResult) -> None:
    assert parse_yaml(ScreenResult, dump_yaml(value)) == value


def test_the_example_is_admissible_and_counts_its_cells() -> None:
    screen = build()
    lead_lag = screen.measures[0]
    assert isinstance(lead_lag, LeadLag)
    assert pairs(screen, lead_lag) == (("btc", "mara"), ("btc", "riot"), ("btc", "miners"))
    assert cells(screen) == (3 * 3, 2 * 3 * 2)
    assert screen.legs_of("follow") == ("mara", "riot")


def test_a_series_against_itself_is_its_autocorrelation() -> None:
    measure = {"id": "lead_lag", "from": "miners", "to": "miners", "estimator": "hy"}
    screen = build(measures=[{**measure, "lags": ["1s", "5s"]}])
    lead_lag = screen.measures[0]
    assert isinstance(lead_lag, LeadLag)
    assert pairs(screen, lead_lag) == (("miners", "miners"),)


def test_a_cross_section_pairs_every_name_with_every_other() -> None:
    measure = {"id": "lead_lag", "from": "follow", "to": "follow", "estimator": "hy"}
    screen = build(measures=[{**measure, "lags": ["1s"]}])
    lead_lag = screen.measures[0]
    assert isinstance(lead_lag, LeadLag)
    assert len(pairs(screen, lead_lag)) == 3 * 2


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"window": None}, "a free screen states its window"),
        ({"hyp": "demo_mr"}, "a bound screen reads demo_mr's research window"),
        (
            {"window": None, "hyp": "demo_mr", "costs": {"OKX": {"commission_bps": 5}}},
            "a bound screen is charged demo_mr's costs",
        ),
        ({"groups": {"btc": ["mara"]}}, "already declared as a leg"),
        ({"derived": {"miners": {"basket": {"mara": 1.0, "nobody": 1.0}}}}, "'nobody'"),
        ({"derived": {"miners": {"basket": {"mara": 1.0}}}}, "at least two legs"),
        ({"derived": {"miners": {"basket": {"mara": 1.0, "riot": 0.0}}}}, "weight of zero"),
        (
            {
                "derived": {
                    "miners": {
                        "basket": {"mara": 1.0, "riot": 1.0},
                        "gap": {"a": "btc", "b": "riot"},
                    }
                }
            },
            "exactly one of basket, spread or gap",
        ),
        (
            {"derived": {"miners": {"spread": {"long": "mara", "short": "riot", "hedge": "ols"}}}},
            "an ols hedge states where it is fitted",
        ),
        (
            {
                "derived": {
                    "miners": {"spread": {"long": "mara", "short": "riot", "hedge": "fixed"}}
                }
            },
            "a fixed hedge states beta",
        ),
        ({"derived": {"miners": {"gap": {"a": "btc", "b": "btc"}}}}, "the same leg as a"),
        (
            {"derived": {"miners": {"spread": {"long": "mara", "short": "mara", "hedge": "ols"}}}},
            "is the long leg too",
        ),
        ({"groups": {"lead": ["btc"], "follow": ["nobody"]}}, "'nobody' is not a declared leg"),
        ({"groups": {"lead": ["btc"], "follow": []}}, "at least one series"),
        ({"groups": {"lead": ["btc"], "follow": ["mara", "mara"]}}, "appears twice"),
        ({"clock": {"grid": "500ms", "hours": "overlap"}}, "finer than leg mara's 1s bars"),
        ({"clock": {"hours": "overlap"}}, "samples on a grid and none is set"),
        (
            {"clock": {"grid": "1s", "hours": {"tz": "Mars/Olympus", "span": "09:30-16:00"}}},
            "time zone",
        ),
        ({"clock": {"grid": "1s", "hours": {"tz": "UTC", "span": "16:00-09:30"}}}, "close after"),
    ],
)
def test_a_file_that_breaks_a_rule_is_refused_by_name(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        build(**changes)


@pytest.mark.parametrize(
    ("measure", "message"),
    [
        ({"lags": ["0s"]}, "zero is refused"),
        ({"lags": ["1s", "1000ms"]}, "appears twice"),
        ({"lags": ["1500ms"]}, "is not a whole number of 1s grid steps"),
        ({"from": "nobody"}, "'nobody' is not a declared leg or group"),
    ],
)
def test_a_lattice_is_finite_distinct_and_never_at_lag_zero(
    measure: dict[str, Any], message: str
) -> None:
    lead_lag = {**BASE["measures"][0], **measure}
    with pytest.raises(ValidationError, match=message):
        build(measures=[lead_lag])


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        ({"leg": "btc", "move_bp": [10]}, "states its thresholds and its window"),
        ({"leg": "btc", "z": [2]}, "states its thresholds and its lookback"),
        ({"leg": "btc", "move_bp": [10], "within": "1s", "z": [2], "lookback": "1m"}, "a move"),
        ({"leg": "follow", "move_bp": [10], "within": "1s"}, "'follow' is not a declared leg"),
    ],
)
def test_a_trigger_is_a_move_or_a_score_on_a_leg(trigger: dict[str, Any], message: str) -> None:
    response = {**BASE["measures"][1], "trigger": trigger}
    with pytest.raises(ValidationError, match=message):
        build(measures=[response])


def test_a_horizon_holds_something() -> None:
    response = {**BASE["measures"][1], "horizons": ["0s"]}
    with pytest.raises(ValidationError, match="a horizon of zero holds nothing"):
        build(measures=[response])


def test_a_gap_is_a_function_of_its_two_venues_legs() -> None:
    screen = build(derived={"miners": {"gap": {"a": "riot", "b": "mara"}}})
    assert screen.legs_of("miners") == ("riot", "mara")


def test_a_bar_leg_names_its_size_and_nothing_else_does() -> None:
    with pytest.raises(ValidationError, match="a bar leg names its bar size"):
        build(legs={**BASE["legs"], "mara": {"instrument": "MARA.XNAS", "type": "bar"}})
    with pytest.raises(ValidationError, match="unaggregated and has none"):
        build(legs={**BASE["legs"], "btc": {**BASE["legs"]["btc"], "resolution": "1s"}})


def test_followers_are_not_spelled_on_because_yaml_reads_it_as_true() -> None:
    text = dump_yaml(build())
    assert "followers: follow" in text
    assert parse_yaml(Screen, text) == build()


def test_a_span_is_read_in_nanoseconds_and_a_lag_keeps_its_sign() -> None:
    assert span_ns("250ms") == 250_000_000
    assert span_ns("-2m") == -120_000_000_000
    assert span_ns("1d") == 86_400_000_000_000
    with pytest.raises(ValidationError, match="is not a span"):
        span_ns("2 weeks")


def test_hours_are_minutes_after_midnight() -> None:
    assert hours_minutes("09:30-16:00") == (570, 960)
    assert hours_minutes("00:00-24:00") == (0, 1440)
    with pytest.raises(ValidationError, match="is not HH:MM-HH:MM"):
        hours_minutes("9:30-16:00")
