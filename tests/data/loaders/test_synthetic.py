"""The synthetic loader: the fixture everything else stands on.

The checksums below are golden values. They are not an implementation detail: they are
the claim that this generator produces the same bytes on macOS arm64 and on Linux
x86_64, which is what makes a card reproducible and a snapshot worth pinning. CI runs
this file on both hosts, so a change that makes the generator platform-dependent — a
`standard_normal`, an `exp`, a reassociated sum — fails here rather than months later in
a card nobody can reproduce.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria.run import NS_PER_SECOND, midnight_ns
from kanso.data.loader import get_loader, utc_day
from kanso.data.loaders.synthetic import (
    SyntheticLoader,
    SyntheticSpec,
    _decode,
    _request_params,
    _spec_of,
)
from kanso.errors import ValidationError

NS_PER_HOUR = 3_600 * NS_PER_SECOND

GOLDEN_OU = {
    ("DEMO.SIM", "bar"): "9779b7af8847202ff22320f5ecc7db5baf63eba9db3712eac9628f3365de769e",
    ("DEMO.SIM", "quote"): "752ee266336a17ebb928e4ac8f5aef6c27b5119749f680111ff3319184675dc5",
    ("DEMO.SIM", "trade"): "0747fbe06d88eafaec1f2e666d5b91a1e798c5c919cb819eae3e12056502f702",
    ("OTHER.SIM", "bar"): "3eea17b994c67d0a1dafb64c41b933a7228135dd4d0b03fd314f425ce6c86c61",
    ("OTHER.SIM", "quote"): "2a667fbbb2eb38e3e022726629733f9abec8a4fe439aa0d0b9ed5e34ff7d7adb",
    ("OTHER.SIM", "trade"): "30f445a09ee93168a53549a47adfd7ba5640ae8f69998607b5ea0566fe699a83",
}

GOLDEN_GBM = {
    ("DEMO.SIM", "bar"): "dfd211163056b009946a45606e43b9051ed21e23cb98b1c59df1b188c8d0fdbe",
    ("DEMO.SIM", "quote"): "51fa89cab73be9c2dbbcb5fb499d2678f751b7c18423d5e595a581e589cd8803",
    ("DEMO.SIM", "trade"): "506bc874241c3147f64b20281202d64d9e6762fb65c4e240b566c169d1e028af",
    ("OTHER.SIM", "bar"): "cd491717a48bff808aa9c3ee62a401358cf04637f5d0d9eb33e4bee084edb35d",
    ("OTHER.SIM", "quote"): "12eb174626d41d0546b211b7666e40e2096feb50f8ac8c3a91966a0a26f56e54",
    ("OTHER.SIM", "trade"): "a6dfbeaad4c83aba9de84ce92ada0a164201a40e98603d6f8e15d50581a07c45",
}

GOLDEN_CONTINUOUS = {
    ("DEMO.SIM", "bar"): "5b8af3a825bad5c676f76d6bbea7b6761074c84de41a80c3f20793f260c07d8f",
    ("DEMO.SIM", "quote"): "1dd0708adda99af7c2f78c41783dd678305594868237a9212b1995935cf2858e",
    ("DEMO.SIM", "trade"): "3e32ac643d13e12980d145ecd9429e4def6482099a292bb17ef5ecb724da4afc",
    ("OTHER.SIM", "bar"): "bc1ae401a72d54d08a1ecd3e50d3a1c0dcabde293bd8113ff5aaaeadca6f157d",
    ("OTHER.SIM", "quote"): "7fd55008a2760b0d4ef17e16569c9d605138421a3c4c88f4fd076c12dd8a53cf",
    ("OTHER.SIM", "trade"): "248571bfee438deafc1cbc369f9d09e10e91d0316dcf4cee83311988b2d28e60",
}

PINNED_PARAMS = {
    "end": "2024-03-05",
    "instruments": "DEMO,OTHER",
    "kappa": "0.02",
    "loader": "synthetic",
    "model": "ou",
    "mu_bps": "0.0",
    "price_precision": "2",
    "resolution": "5m",
    "seed": "7",
    "session_end": "16:00",
    "session_start": "09:30",
    "sigma_bps": "10.0",
    "size_precision": "0",
    "spread_bps": "2.0",
    "start": "2024-03-04",
    "start_price": "100.0",
    "theta": "",
    "timezone": "America/New_York",
    "types": "bar,quote,trade",
    "venue": "SIM",
    "volume": "5000",
}
"""The request parameters the fixture spec recorded before `calendar` existed: what every
manifest already written holds, and what a default spec must go on recording."""

MONDAY = date(2024, 3, 4)
SUNDAY = date(2024, 3, 10)

LOADER = SyntheticLoader()


def continuous(spec: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """The fixture spec on a continuous calendar over one Monday-to-Sunday week."""
    return {
        **spec,
        "calendar": "continuous",
        "start": MONDAY.isoformat(),
        "end": SUNDAY.isoformat(),
        **overrides,
    }


def checksums(spec: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {
        (ref.instrument, ref.type): LOADER.manifest(ref).checksum for ref in LOADER.discover(spec)
    }


def test_the_registry_serves_the_generator() -> None:
    assert get_loader("synthetic") is not None
    assert get_loader("synthetic").id == "synthetic"


@pytest.mark.parametrize(("model", "golden"), [("ou", GOLDEN_OU), ("gbm", GOLDEN_GBM)])
def test_a_seed_reproduces_byte_for_byte(
    synthetic_spec: dict[str, Any], model: str, golden: dict[tuple[str, str], str]
) -> None:
    """The same spec produces the same bytes, here and on the other host."""
    spec = {**synthetic_spec, "model": model}
    assert checksums(spec) == golden
    assert checksums(spec) == checksums(dict(spec))


def test_a_different_seed_is_a_different_series(synthetic_spec: dict[str, Any]) -> None:
    other = checksums({**synthetic_spec, "seed": 8})
    assert set(other) == set(GOLDEN_OU)
    assert all(other[key] != GOLDEN_OU[key] for key in other)


def test_each_instrument_has_its_own_path(synthetic_spec: dict[str, Any]) -> None:
    """Spawned seeds, so one instrument's path never leaks into another's."""
    assert GOLDEN_OU[("DEMO.SIM", "bar")] != GOLDEN_OU[("OTHER.SIM", "bar")]


def test_the_path_does_not_depend_on_what_else_is_loaded(
    synthetic_spec: dict[str, Any],
) -> None:
    """Loading only quotes gives the quotes that loading everything would give."""
    quotes_only = checksums({**synthetic_spec, "types": ["quote"]})
    assert quotes_only[("DEMO.SIM", "quote")] == GOLDEN_OU[("DEMO.SIM", "quote")]


def test_a_window_selects_points_and_never_changes_them(
    synthetic_spec: dict[str, Any],
) -> None:
    """The whole span is generated before a window is applied."""
    ref = LOADER.discover(synthetic_spec)[0]
    whole = list(LOADER.load(ref, ref.span))
    first_day = list(LOADER.load(ref, (date(2024, 3, 4), date(2024, 3, 4))))
    second_day = list(LOADER.load(ref, (date(2024, 3, 5), date(2024, 3, 5))))
    assert len(first_day) + len(second_day) == len(whole)
    assert [str(bar) for bar in first_day + second_day] == [str(bar) for bar in whole]


def test_a_window_outside_the_span_yields_nothing(synthetic_spec: dict[str, Any]) -> None:
    ref = LOADER.discover(synthetic_spec)[0]
    assert list(LOADER.load(ref, (date(2020, 1, 1), date(2020, 1, 2)))) == []
    assert ref.window((date(2020, 1, 1), date(2020, 1, 2))) is None
    assert ref.window((date(2024, 3, 1), date(2024, 3, 4))) == (
        date(2024, 3, 4),
        date(2024, 3, 4),
    )


def test_every_point_is_available_when_it_happened(synthetic_spec: dict[str, Any]) -> None:
    """No adapter, so nothing is delayed: a generated point is public at its instant."""
    for ref in LOADER.discover(synthetic_spec):
        assert ref.publication == "realtime"
        assert ref.publication_rule is None
        for point in LOADER.load(ref, ref.span):
            assert point.ts_init == point.ts_event


def test_only_weekday_sessions_are_generated(synthetic_spec: dict[str, Any]) -> None:
    """A span that opens on a Saturday serves from the Monday, and says so."""
    spec = {**synthetic_spec, "start": "2024-03-02", "end": "2024-03-05"}
    ref = LOADER.discover(spec)[0]
    assert ref.span == (date(2024, 3, 4), date(2024, 3, 5))
    days = {utc_day(point.ts_event) for point in LOADER.load(ref, ref.span)}
    assert days == {date(2024, 3, 4), date(2024, 3, 5)}


def test_bars_close_on_the_grid(synthetic_spec: dict[str, Any]) -> None:
    """390 minutes of session at five minutes a bar is 78 bars a day."""
    ref = LOADER.discover({**synthetic_spec, "types": ["bar"]})[0]
    bars = list(LOADER.load(ref, ref.span))
    assert len(bars) == 78 * 2
    assert str(bars[0].bar_type) == "DEMO.SIM-5-MINUTE-LAST-EXTERNAL"


def test_quotes_are_two_sided_around_the_mid(synthetic_spec: dict[str, Any]) -> None:
    ref = next(r for r in LOADER.discover(synthetic_spec) if r.type == "quote")
    for quote in LOADER.load(ref, ref.span):
        assert quote.ask_price > quote.bid_price
        assert quote.bid_size > 0 and quote.ask_size > 0


def test_trades_carry_a_side_and_a_unique_id(synthetic_spec: dict[str, Any]) -> None:
    ref = next(r for r in LOADER.discover(synthetic_spec) if r.type == "trade")
    trades = list(LOADER.load(ref, ref.span))
    assert len({str(trade.trade_id) for trade in trades}) == len(trades)
    from nautilus_trader.model.enums import AggressorSide

    assert {trade.aggressor_side for trade in trades} == {
        AggressorSide.BUYER,
        AggressorSide.SELLER,
    }


def test_the_manifest_records_what_was_served(synthetic_spec: dict[str, Any]) -> None:
    ref = LOADER.discover(synthetic_spec)[0]
    manifest = LOADER.manifest(ref)
    assert manifest.source == "synthetic"
    assert manifest.dataset_id == ref.dataset_id
    assert manifest.span == ref.span
    assert manifest.row_count == 78 * 2
    assert manifest.publication == "realtime"
    assert manifest.adjusted is False
    assert manifest.request_params is not None
    assert manifest.request_params["seed"] == "7"


def test_the_manifest_carries_the_spec_that_reproduces_it(
    synthetic_spec: dict[str, Any],
) -> None:
    """A synthetic dataset's provenance is its spec, so the ref carries the whole of it."""
    ref = LOADER.discover(synthetic_spec)[0]
    assert ref.request_params is not None
    rebuilt = SyntheticSpec.model_validate(synthetic_spec)
    assert ref.request_params["instruments"] == "DEMO,OTHER"
    assert ref.request_params["theta"] == ""
    assert rebuilt.long_run == rebuilt.start_price


def test_arrow_batches_carry_the_catalog_schema(synthetic_spec: dict[str, Any]) -> None:
    ref = LOADER.discover({**synthetic_spec, "types": ["bar"]})[0]
    tables = LOADER.load_arrow(ref, ref.span)
    assert tables is not None
    rows = [table.num_rows for table in tables]
    assert sum(rows) == 78 * 2


def test_a_ref_nobody_discovered_is_refused() -> None:
    from kanso.data.loader import DatasetRef

    ref = DatasetRef(
        dataset_id="DEMO.SIM-bar-1m-raw-20240304",
        instrument="DEMO.SIM",
        type="bar",
        resolution="1m",
        span=(date(2024, 3, 4), date(2024, 3, 4)),
        adjusted=False,
        publication="realtime",
    )
    with pytest.raises(ValidationError, match="carries no synthetic spec"):
        list(LOADER.load(ref, ref.span))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"end": "2024-01-01"}, "is before start"),
        ({"instruments": ["DEMO", "DEMO"]}, "repeats an id"),
        ({"types": ["bar", "bar"]}, "repeats a type"),
        ({"timezone": "Mars/Olympus"}, "not an IANA time zone"),
        ({"session_end": "09:00"}, "is not after session_start"),
        ({"session_start": "nine"}, "not a time of day"),
        ({"resolution": "1d"}, "is longer than the"),
        ({"seed": -1}, "seed"),
        ({"model": "brownian"}, "model"),
        ({"loader": "csv_parquet"}, "loader"),
        ({"nonsense": 1}, "nonsense"),
    ],
)
def test_a_bad_spec_is_refused_with_the_reason(
    synthetic_spec: dict[str, Any], override: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        LOADER.discover({**synthetic_spec, **override})


def test_a_range_holding_no_session_is_refused(synthetic_spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="no weekday session"):
        LOADER.discover({**synthetic_spec, "start": "2024-03-02", "end": "2024-03-03"})


def test_a_flat_path_never_falls_through_the_tick_floor() -> None:
    """A violent downward drift is clamped at one tick rather than going negative."""
    spec = {
        "loader": "synthetic",
        "model": "gbm",
        "seed": 3,
        "instruments": ["CRASH"],
        "resolution": "5m",
        "start": "2024-03-04",
        "end": "2024-03-05",
        "mu_bps": -9_000.0,
        "sigma_bps": 100.0,
        "start_price": 5.0,
    }
    ref = SyntheticLoader().discover(spec)[0]
    lows = [bar.low.as_double() for bar in SyntheticLoader().load(ref, ref.span)]
    assert min(lows) > 0


@given(
    seed=st.integers(min_value=0, max_value=2**31),
    steps=st.sampled_from(["1m", "5m", "15m", "1h"]),
    model=st.sampled_from(["ou", "gbm"]),
    calendar=st.sampled_from(["weekdays", "continuous"]),
)
def test_any_valid_spec_produces_ordered_available_points(
    seed: int, steps: str, model: str, calendar: str
) -> None:
    """The property every loader owes the engine, over the spec space."""
    spec = {
        "loader": "synthetic",
        "model": model,
        "seed": seed,
        "instruments": ["P"],
        "resolution": steps,
        "start": "2024-03-04",
        "end": "2024-03-04",
        "calendar": calendar,
    }
    ref = SyntheticLoader().discover(spec)[0]
    points = list(SyntheticLoader().load(ref, ref.span))
    assert points
    assert all(p.ts_init >= p.ts_event for p in points)
    assert [p.ts_init for p in points] == sorted(p.ts_init for p in points)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"types": []}, "name at least one"),
        ({"resolution": "0m"}, "must be longer than zero"),
    ],
)
def test_a_spec_that_generates_nothing_is_refused(
    synthetic_spec: dict[str, Any], override: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        LOADER.discover({**synthetic_spec, **override})


@given(
    seed=st.integers(min_value=0, max_value=2**40),
    model=st.sampled_from(["ou", "gbm"]),
    instruments=st.lists(
        st.text(alphabet="ABCDEFG", min_size=1, max_size=4), min_size=1, max_size=3, unique=True
    ),
    types=st.lists(st.sampled_from(["bar", "quote", "trade"]), min_size=1, unique=True),
    theta=st.one_of(st.none(), st.floats(min_value=1.0, max_value=500.0)),
    calendar=st.sampled_from(["weekdays", "continuous"]),
)
def test_a_spec_survives_the_round_trip_a_ref_makes_it_take(
    seed: int,
    model: str,
    instruments: list[str],
    types: list[str],
    theta: float | None,
    calendar: str,
) -> None:
    """`load` is given a ref and nothing else, so the ref must carry the whole spec."""
    payload: dict[str, Any] = {
        "loader": "synthetic",
        "model": model,
        "seed": seed,
        "instruments": instruments,
        "types": types,
        "resolution": "30m",
        "start": "2024-03-04",
        "end": "2024-03-04",
        "calendar": calendar,
    }
    if theta is not None:
        payload["theta"] = theta
    original = SyntheticSpec.model_validate(payload)
    for ref in LOADER.discover(payload):
        assert _spec_of(ref) == original
        assert ref.request_params is not None
        assert _request_params(_spec_of(ref)) == ref.request_params


def test_a_ref_whose_instrument_the_spec_does_not_generate_is_refused(
    synthetic_spec: dict[str, Any],
) -> None:
    """The instrument's position in the spec is what seeds its path, so it must be found."""
    import dataclasses

    ref = dataclasses.replace(LOADER.discover(synthetic_spec)[0], instrument="GHOST.SIM")
    with pytest.raises(ValidationError, match="which its own spec does not generate"):
        list(LOADER.load(ref, ref.span))


# -- the continuous calendar ------------------------------------------------------------


def test_a_default_spec_records_the_parameters_it_always_did(
    synthetic_spec: dict[str, Any],
) -> None:
    """A synthetic dataset's identity is its spec, so the default calendar is recorded
    nowhere: every manifest written before the field existed is byte-identical."""
    params = _request_params(SyntheticSpec.model_validate(synthetic_spec))
    assert params == PINNED_PARAMS
    assert "calendar" not in params
    assert SyntheticSpec.model_validate(_decode(params)).calendar == "weekdays"


def test_a_continuous_calendar_is_accepted_and_fixes_its_session(
    synthetic_spec: dict[str, Any],
) -> None:
    spec = SyntheticSpec.model_validate(continuous(synthetic_spec))
    assert spec.calendar == "continuous"
    assert (spec.timezone, spec.session_start, spec.session_end) == ("UTC", "00:00", "24:00")
    assert spec.steps_per_session == 24 * 12


def test_a_continuous_calendar_has_a_session_every_day(synthetic_spec: dict[str, Any]) -> None:
    """Monday to Sunday is seven sessions, where the weekday calendar holds five."""
    spec = SyntheticSpec.model_validate(continuous(synthetic_spec))
    assert spec.sessions() == [MONDAY + timedelta(days=i) for i in range(7)]
    assert (
        len(
            SyntheticSpec.model_validate(
                {**continuous(synthetic_spec), "calendar": "weekdays"}
            ).sessions()
        )
        == 5
    )


def test_continuous_bars_close_on_the_utc_grid_through_the_weekend(
    synthetic_spec: dict[str, Any],
) -> None:
    """The first bar of a day closes one resolution after 00:00Z, the last at 00:00Z of
    the next day, and Saturday and Sunday are stamped like any other day."""
    ref = LOADER.discover(continuous(synthetic_spec, types=["bar"], resolution="1h"))[0]
    bars = list(LOADER.load(ref, ref.span))
    assert len(bars) == 24 * 7
    assert bars[0].ts_init == midnight_ns(MONDAY) + NS_PER_HOUR
    assert bars[23].ts_init == midnight_ns(MONDAY + timedelta(days=1))
    assert bars[-1].ts_init == midnight_ns(SUNDAY + timedelta(days=1))
    assert {utc_day(bar.ts_init).weekday() for bar in bars} == set(range(7))
    assert ref.span == (MONDAY, SUNDAY + timedelta(days=1))


def test_a_daily_bar_on_a_continuous_calendar_closes_at_the_next_midnight(
    synthetic_spec: dict[str, Any],
) -> None:
    """A whole-day resolution fits a whole-day session, one bar a day, stamped at 00:00Z
    of the day after the one it summarises."""
    ref = LOADER.discover(continuous(synthetic_spec, types=["bar"], resolution="1d"))[0]
    bars = list(LOADER.load(ref, ref.span))
    assert [bar.ts_init for bar in bars] == [
        midnight_ns(MONDAY + timedelta(days=i + 1)) for i in range(7)
    ]


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"timezone": "America/New_York"}, "timezone"),
        ({"session_start": "09:30"}, "session_start"),
        ({"session_end": "16:00"}, "session_end"),
    ],
)
def test_a_continuous_spec_stating_a_session_is_refused_naming_the_field(
    synthetic_spec: dict[str, Any], override: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValidationError, match=f"{field}: .* conflicts with calendar 'continuous'"):
        LOADER.discover(continuous(synthetic_spec, **override))


def test_a_continuous_spec_may_restate_the_session_it_fixes(
    synthetic_spec: dict[str, Any],
) -> None:
    """What a manifest records decodes to the spec that wrote it."""
    spec = SyntheticSpec.model_validate(
        continuous(synthetic_spec, timezone="UTC", session_start="00:00", session_end="24:00")
    )
    assert spec == SyntheticSpec.model_validate(continuous(synthetic_spec))


def test_a_continuous_spec_round_trips_through_its_manifest_byte_for_byte(
    synthetic_spec: dict[str, Any],
) -> None:
    ref = LOADER.discover(continuous(synthetic_spec))[0]
    assert ref.request_params is not None
    assert ref.request_params["calendar"] == "continuous"
    assert ref.request_params["timezone"] == "UTC"
    assert ref.request_params["session_end"] == "24:00"
    decoded = SyntheticSpec.model_validate(_decode(ref.request_params))
    assert decoded == SyntheticSpec.model_validate(continuous(synthetic_spec))
    assert _request_params(decoded) == ref.request_params
    assert _spec_of(ref) == decoded


def test_a_continuous_seed_reproduces_byte_for_byte(synthetic_spec: dict[str, Any]) -> None:
    """The reproducibility contract holds on the continuous calendar too, on both hosts."""
    spec = continuous(synthetic_spec, start="2024-03-04", end="2024-03-05")
    assert checksums(spec) == GOLDEN_CONTINUOUS
    assert all(GOLDEN_CONTINUOUS[key] != GOLDEN_OU[key] for key in GOLDEN_OU)


def test_a_resolution_longer_than_a_day_closes_no_continuous_bar(
    synthetic_spec: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="longer than the 00:00-24:00 session"):
        LOADER.discover(continuous(synthetic_spec, resolution="2d"))


# -- a perpetual's funding --------------------------------------------------------------

GOLDEN_FUNDING = {
    ("DEMO.SIM", "funding"): "847f7676d3fa2ae82527cbd1c5be059892652ef9834597d1f843166599cf8518",
    ("OTHER.SIM", "funding"): "38fd6549c49c98ba53bbffde7c73a89c478e8a8f863d6ab12720976d81c2b705",
}


def test_funding_settles_at_00_08_and_16_utc_every_day(synthetic_spec: dict[str, Any]) -> None:
    """Each session's three eight-hour periods close at 08:00, 16:00 and the next 00:00Z,
    as its last daily bar does, and each settlement is public the instant it settles."""
    ref = LOADER.discover(continuous(synthetic_spec, types=["funding"]))[0]
    settled = list(LOADER.load(ref, ref.span))

    assert ref.type == "funding"
    assert ref.resolution is None
    assert ref.span == (MONDAY, SUNDAY + timedelta(days=1))
    assert [point.ts_init for point in settled] == [
        midnight_ns(MONDAY) + hours * NS_PER_HOUR for hours in range(8, 7 * 24 + 1, 8)
    ]
    assert all(point.ts_event == point.ts_init for point in settled)
    assert {str(point.instrument_id) for point in settled} == {"DEMO.SIM"}


def test_a_funding_rate_is_a_whole_hundredth_of_a_basis_point_from_minus_one_to_two(
    synthetic_spec: dict[str, Any],
) -> None:
    ref = LOADER.discover(continuous(synthetic_spec, types=["funding"]))[0]
    rates = [point.rate for point in LOADER.load(ref, ref.span)]

    assert all(-0.0001 <= rate < 0.0002 for rate in rates)
    assert all(round(rate * 1_000_000) / 1_000_000 == rate for rate in rates)
    assert any(rate > 0 for rate in rates) and any(rate < 0 for rate in rates)


def test_funding_reproduces_byte_for_byte_and_moves_no_other_series(
    synthetic_spec: dict[str, Any],
) -> None:
    """Funding draws from a fifth seed of its own, so a spec that adds it generates every
    other series exactly as it did without it."""
    spec = continuous(
        synthetic_spec,
        start="2024-03-04",
        end="2024-03-05",
        types=["bar", "quote", "trade", "funding"],
    )
    assert checksums(spec) == {**GOLDEN_CONTINUOUS, **GOLDEN_FUNDING}


def test_funding_on_a_weekday_calendar_is_refused(synthetic_spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="funding is settled round the clock"):
        LOADER.discover({**synthetic_spec, "types": ["bar", "funding"]})


def closes(spec: dict[str, Any]) -> list[float]:
    (ref,) = [ref for ref in LOADER.discover(spec) if ref.type == "bar"]
    return [float(bar.close) for bar in LOADER.load(ref, ref.span)]


def test_a_follower_repeats_its_leader_s_moves_a_step_later(
    synthetic_spec: dict[str, Any],
) -> None:
    import numpy as np

    base = {**synthetic_spec, "instruments": ["DEMO"], "types": ["bar"], "sigma_bps": 40}
    leader = np.diff(np.log(closes(base)))
    follower_spec = {
        **base,
        "seed": 8,
        "instruments": ["LAGD"],
        "leader_seed": base["seed"],
        "coupling": 0.9,
        "lag_steps": 1,
    }
    follower = np.diff(np.log(closes(follower_spec)))

    led = np.corrcoef(leader[:-1], follower[1:])[0, 1]
    same = np.corrcoef(leader, follower)[0, 1]
    assert led > 0.7 and abs(same) < 0.2
    ref = LOADER.discover(follower_spec)[0]
    assert ref.request_params is not None
    assert ref.request_params["leader_seed"] == "7" and ref.request_params["coupling"] == "0.9"
    assert _spec_of(ref).leader_seed == 7


@pytest.mark.parametrize(
    "changes", [{"leader_seed": 7}, {"coupling": 0.5}, {"leader_seed": 7, "coupling": 0.0}]
)
def test_a_leader_and_a_coupling_come_together(
    synthetic_spec: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="stated together, or neither is"):
        SyntheticSpec.model_validate({**synthetic_spec, **changes})
