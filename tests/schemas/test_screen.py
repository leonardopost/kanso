"""`screen.yaml`: the rules that make a screen admissible, and its round trip."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.errors import ValidationError
from kanso.schemas import dump_yaml, parse_yaml
from kanso.schemas.screen import (
    Comparison,
    LeadLag,
    Screen,
    ScreenResult,
    TurnOfMonth,
    cells,
    disjoint,
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


SIGNALS: dict[str, Any] = {
    "regime": {"type": "house_state", "field": "level", "instrument": "IDX.XNAS"},
    "event_day": {"type": "house_calendar", "field": "flag", "instrument": "MARA.XNAS"},
}


def test_a_signal_is_a_custom_field_and_its_instrument_is_one_the_screen_reads() -> None:
    screen = build(signals=SIGNALS)

    assert screen.signals["regime"].field == "level"
    assert screen.instruments == ("BTC-USDT-SWAP.OKX", "IDX.XNAS", "MARA.XNAS", "RIOT.XNAS")
    assert build().instruments == ("BTC-USDT-SWAP.OKX", "MARA.XNAS", "RIOT.XNAS")
    assert parse_yaml(Screen, dump_yaml(screen)) == screen


@pytest.mark.parametrize(
    ("signal", "message"),
    [
        ({"type": "bar", "field": "close"}, "'bar' is a market type"),
        ({"type": "house_state", "field": "ts_init"}, "says when or whose a point is"),
        ({"type": "house_state", "field": "instrument_id"}, "says when or whose a point is"),
        ({"type": "house_state", "field": "9lives"}, "field"),
        ({"type": "House", "field": "level"}, "type"),
    ],
)
def test_a_signal_that_is_no_custom_value_is_refused(signal: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        build(signals={"regime": {**signal, "instrument": "IDX.XNAS"}})


def test_a_signal_shares_the_namespace_of_legs_and_groups() -> None:
    with pytest.raises(ValidationError, match="signals.btc: already declared as a leg"):
        build(signals={"btc": SIGNALS["regime"]})
    with pytest.raises(ValidationError, match="signals.lead: already declared as a group"):
        build(signals={"lead": SIGNALS["regime"]})


@given(st.tuples(st.integers(0, 10), st.integers(0, 10)).filter(lambda pair: pair != (0, 0)))
def test_a_turn_of_month_round_trips(window: tuple[int, int]) -> None:
    value = TurnOfMonth(turn_of_month=window)
    assert parse_yaml(TurnOfMonth, dump_yaml(value)) == value


def test_a_turn_of_month_holding_no_day_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"\[0, 0\] holds no day"):
        TurnOfMonth(turn_of_month=(0, 0))


def test_a_verdict_no_cell_could_pass_is_refused() -> None:
    from kanso.schemas.screen import p_floor

    assert p_floor(3) == 0.25 and p_floor(1) == 1.0
    verdict = {"alpha": 0.01, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 3}
    with pytest.raises(ValidationError, match="smallest p 3 session"):
        build(verdict=verdict)
    build(verdict={**verdict, "min_sessions": 8})


CATCH_UP: dict[str, Any] = {
    "id": "catch_up",
    "leader": "btc",
    "followers": "follow",
    "within": "5s",
    "fit": "first_fold",
    "beta_over": "1m",
    "enter_bp": [5, 10],
    "close": [0, 0.5],
    "stop": [2],
    "max_hold": "10m",
    "latency_ms": 20,
}


def test_a_catch_up_counts_a_cell_per_follower_entry_close_and_stop() -> None:
    screen = build(measures=[CATCH_UP])

    assert cells(screen) == (3 * 2 * 2 * 1,)


@pytest.mark.parametrize(
    ("measure", "message"),
    [
        ({"leader": "nobody"}, "leader: 'nobody' is not a declared leg"),
        ({"leader": "follow"}, "leader: 'follow' is not a declared leg"),
        ({"followers": "nobody"}, "'nobody' is not a declared leg or group"),
        ({"followers": "btc"}, "the leader cannot follow itself"),
        ({"within": "0s"}, "within: a span of zero measures nothing"),
        ({"max_hold": "0ms"}, "max_hold: a span of zero measures nothing"),
        ({"enter_bp": [5, 5]}, "enter_bp: a value appears twice"),
        ({"close": [0.5, 0.5]}, "close: a value appears twice"),
        ({"stop": [2, 2]}, "stop: a value appears twice"),
        ({"close": [1.0]}, "less than 1"),
        ({"stop": [1.0]}, "greater than 1"),
        ({"fit": "never"}, "window"),
    ],
)
def test_a_catch_up_names_its_pair_and_ends_on_either_side_of_its_entry_gap(
    measure: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        build(measures=[{**CATCH_UP, **measure}])


TESTED: dict[str, Any] = {
    "signal": "regime",
    "when": {"gt": 0},
    "against": {"lt": 0},
    "entry": "close",
    "exit": "close",
    "hold": "while",
    "side": "long",
}

CONDITION: dict[str, Any] = {
    "id": "condition",
    "tests": {"up": TESTED, "down": {**TESTED, "when": {"lt": 0}, "against": "rest"}},
    "followers": "names",
    "payoff": "contrast",
    "null_by": "circular",
    "demean": ["weekday", "month", {"turn_of_month": [1, 3]}, "third_friday_week"],
    "controls": ["event_day"],
    "latency_ms": 900000,
}


def conditioned(measure: dict[str, Any] | None = None, **changes: Any) -> Screen:
    return build(
        signals=SIGNALS,
        groups={**BASE["groups"], "names": ["mara", "riot"]},
        measures=[measure or CONDITION],
        **changes,
    )


def test_a_condition_counts_its_tests_by_its_followers() -> None:
    screen = conditioned()

    assert cells(screen) == (2 * 2,)
    assert parse_yaml(Screen, dump_yaml(screen)) == screen


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tests": {"up": {**TESTED, "when": {}}}}, "exactly one of gt, ge, lt, le, eq or ne"),
        (
            {"tests": {"up": {**TESTED, "when": {"gt": 0, "lt": 1}}}},
            "exactly one of gt, ge, lt, le, eq or ne",
        ),
        (
            {"tests": {"up": {**TESTED, "against": {"le": 0.5}}}},
            "against: le 0.5 meets when gt 0",
        ),
        ({"tests": {"up": {**TESTED, "against": None}}}, "a contrast states what"),
        ({"payoff": "long_flat"}, "a long-or-flat payoff sets the favoured state against nothing"),
        ({"block": 20}, "a circular null takes every shift and sums into no block"),
        ({"min_shift": 30}, "min_shift.*Extra inputs are not permitted"),
        ({"null_by": "sign_flip"}, "a sign-flip null states the block its sessions are summed"),
        (
            {"null_by": "sign_flip", "block": 20, "benchmark": "hold"},
            "benchmark: a hold is judged by circular shifts of the positions",
        ),
        ({"benchmark": "market"}, "benchmark.*Input should be 'hold'"),
        (
            {
                "null_by": "sign_flip",
                "block": 3,
                "tests": {"up": {**TESTED, "hold": {"sessions": 5}}},
            },
            "block: 3 session.s. is shorter than tests.up's hold of 5, so no position of it fits",
        ),
        ({"demean": ["weekday", "weekday"]}, "demean: a value appears twice"),
        (
            {"demean": [{"turn_of_month": [1, 3]}, {"turn_of_month": [2, 2]}]},
            "demean: a value appears twice",
        ),
        ({"controls": ["event_day", "event_day"]}, "controls: a value appears twice"),
        ({"tests": {"up": {**TESTED, "signal": "nobody"}}}, "'nobody' is not a declared signal"),
        ({"controls": ["nobody"]}, "controls: 'nobody' is not a declared signal"),
        ({"followers": "miners"}, "'miners' is a derived leg"),
        ({"followers": "follow"}, "'miners' is a derived leg"),
        ({"followers": "nobody"}, "'nobody' is not a declared leg or group"),
        ({"tests": {}}, "at least 1"),
        ({"tests": {"up": {**TESTED, "hold": {"sessions": 0}}}}, "greater than or equal to 1"),
    ],
)
def test_a_condition_that_breaks_a_rule_is_refused_by_name(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        conditioned({**CONDITION, **change})


def test_two_condition_measures_making_one_cell_are_refused_and_two_nulls_are_admitted() -> None:
    blocks = {**CONDITION, "null_by": "sign_flip", "block": 20}
    screen = build(
        signals=SIGNALS,
        groups={**BASE["groups"], "names": ["mara", "riot"]},
        measures=[CONDITION, blocks],
    )
    assert cells(screen) == (4, 4)

    with pytest.raises(ValidationError, match="measures.1: condition/up/mara/circular is"):
        build(
            signals=SIGNALS,
            groups={**BASE["groups"], "names": ["mara", "riot"]},
            measures=[CONDITION, {**CONDITION, "tests": {"up": TESTED}}],
        )


def test_a_verdict_no_condition_cell_could_pass_under_its_null_is_refused() -> None:
    verdict = {"alpha": 0.01, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 100}
    blocks = {**CONDITION, "null_by": "sign_flip", "block": 20}

    with pytest.raises(ValidationError, match="smallest p 5 block.s. of measures.0's 20"):
        conditioned(blocks, verdict=verdict)
    conditioned({**blocks, "block": 10}, verdict=verdict)
    with pytest.raises(ValidationError, match="below 0.01, the smallest p circular shifts") as e:
        conditioned(verdict={**verdict, "alpha": 0.009})
    assert "give a family of 100 session(s), one in its 100 rotations" in str(e.value)
    conditioned(verdict=verdict)


CRITICAL = st.sampled_from([-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0])
OPERATOR = st.sampled_from(["gt", "ge", "lt", "le", "eq", "ne"])


@given(first=OPERATOR, low=CRITICAL, second=OPERATOR, high=CRITICAL)
def test_two_comparisons_are_disjoint_exactly_when_no_point_satisfies_both(
    first: str, low: float, second: str, high: float
) -> None:
    a, b = Comparison.model_validate({first: low}), Comparison.model_validate({second: high})
    points = {low, high, (low + high) / 2, low - 1, low + 1, high - 1, high + 1, -1e9, 1e9}

    meets = any(a.holds(point) and b.holds(point) for point in points)

    assert disjoint(a, b) is (not meets)
    assert disjoint(b, a) is disjoint(a, b)


@given(operator=OPERATOR, bound=CRITICAL, value=CRITICAL)
def test_a_comparison_holds_where_its_operator_says(
    operator: str, bound: float, value: float
) -> None:
    import operator as ops

    compare = {"gt": ops.gt, "ge": ops.ge, "lt": ops.lt, "le": ops.le, "eq": ops.eq, "ne": ops.ne}
    comparison = Comparison.model_validate({operator: bound})

    assert comparison.holds(value) is compare[operator](value, bound)
    assert comparison.text == f"{operator} {bound:g}"


@given(operator=OPERATOR, bound=st.floats(allow_nan=False, allow_infinity=False))
def test_a_comparison_s_text_reads_back_as_the_number_it_compares_with(
    operator: str, bound: float
) -> None:
    name, number = Comparison.model_validate({operator: bound}).text.split()

    assert name == operator and float(number) == bound


def test_a_comparison_s_text_is_short_where_that_is_exact_and_whole_where_not() -> None:
    texts = {
        bound: Comparison.model_validate({"gt": bound}).text
        for bound in (2.0, 0.5, 2.0000001, 0.1234567, 1234567.0, 1e-7)
    }

    assert texts == {
        2.0: "gt 2",
        0.5: "gt 0.5",
        2.0000001: "gt 2.0000001",
        0.1234567: "gt 0.1234567",
        1234567.0: "gt 1234567.0",
        1e-7: "gt 1e-07",
    }


@pytest.mark.parametrize("bound", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("operator", ["gt", "ge", "lt", "le", "eq", "ne"])
def test_a_comparison_with_no_finite_number_is_refused(operator: str, bound: float) -> None:
    with pytest.raises(ValidationError, match=f"{operator}: Input should be a finite number"):
        Comparison.model_validate({operator: bound})
