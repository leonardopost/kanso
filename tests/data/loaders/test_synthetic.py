"""The synthetic loader: the fixture everything else stands on.

The checksums below are golden values. They are not an implementation detail: they are
the claim that this generator produces the same bytes on macOS arm64 and on Linux
x86_64, which is what makes a card reproducible and a snapshot worth pinning. CI runs
this file on both hosts, so a change that makes the generator platform-dependent — a
`standard_normal`, an `exp`, a reassociated sum — fails here rather than months later in
a card nobody can reproduce.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.data.loader import get_loader, utc_day
from kanso.data.loaders.synthetic import SyntheticLoader, SyntheticSpec
from kanso.errors import ValidationError

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

LOADER = SyntheticLoader()


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
)
def test_any_valid_spec_produces_ordered_available_points(
    seed: int, steps: str, model: str
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
)
def test_a_spec_survives_the_round_trip_a_ref_makes_it_take(
    seed: int,
    model: str,
    instruments: list[str],
    types: list[str],
    theta: float | None,
) -> None:
    """`load` is given a ref and nothing else, so the ref must carry the whole spec."""
    from kanso.data.loaders.synthetic import _spec_of

    payload: dict[str, Any] = {
        "loader": "synthetic",
        "model": model,
        "seed": seed,
        "instruments": instruments,
        "types": types,
        "resolution": "30m",
        "start": "2024-03-04",
        "end": "2024-03-04",
    }
    if theta is not None:
        payload["theta"] = theta
    original = SyntheticSpec.model_validate(payload)
    for ref in LOADER.discover(payload):
        assert _spec_of(ref) == original


def test_a_ref_whose_instrument_the_spec_does_not_generate_is_refused(
    synthetic_spec: dict[str, Any],
) -> None:
    """The instrument's position in the spec is what seeds its path, so it must be found."""
    import dataclasses

    ref = dataclasses.replace(LOADER.discover(synthetic_spec)[0], instrument="GHOST.SIM")
    with pytest.raises(ValidationError, match="which its own spec does not generate"):
        list(LOADER.load(ref, ref.span))
