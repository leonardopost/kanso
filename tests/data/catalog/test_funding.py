"""A funding dataset in the store: written without a rule, refused when impossible, pinned.

Funding is public the instant it settles, so a dataset of it is `realtime` and names no
publication rule; `check_delayed` is consulted only for a dataset declared `delayed`. The
availability invariant is the one every point obeys, and a snapshot pins a funding series
like any other.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data import catalog as cat
from kanso.data import snapshot as snap
from kanso.data.catalog import NANOS_PER_SECOND, day_start_ns
from kanso.data.instruments import build, conventions_for
from kanso.data.publication import PUBLICATION_RULES
from kanso.data.types import Funding
from kanso.errors import ValidationError
from kanso.schemas import InstrumentEntry
from kanso.schemas.hypothesis import Windows
from tests.data.catalog.conftest import FakeWorkspace, Ref, bars, quotes

PERP = "BTCUSDT-PERP.SIM"
SPOT = "BTCUSDT.SIM"
JAN1 = date(2024, 1, 1)
DAYS = 25
EIGHT_HOURS_NS = 8 * 3_600 * NANOS_PER_SECOND


def perpetual() -> Any:
    """The manual BTCUSDT-PERP entry `docs/workspace.md` shows, built as a workspace would."""
    entry = InstrumentEntry.model_validate(
        {
            "nautilus_id": PERP,
            "asset_class": "CRYPTOCURRENCY",
            "manual": True,
            "corporate_actions": "none",
            "override": {
                "instrument_class": "swap",
                "base_currency": "BTC",
                "quote_currency": "USDT",
                "settlement_currency": "USDT",
                "multiplier": "0.01",
                "price_increment": "0.1",
                "size_increment": "1",
                "lot_size": "1",
            },
        }
    )
    return build(entry, conventions_for(entry, JAN1))


def spot() -> Any:
    """A manual BTCUSDT spot pair on the same venue: the other leg of a basis trade."""
    entry = InstrumentEntry.model_validate(
        {
            "nautilus_id": SPOT,
            "asset_class": "CRYPTOCURRENCY",
            "manual": True,
            "corporate_actions": "none",
            "override": {
                "base_currency": "BTC",
                "quote_currency": "USDT",
                "price_increment": "0.1",
                "size_increment": "0.001",
                "lot_size": "0.001",
            },
        }
    )
    return build(entry, conventions_for(entry, JAN1))


def settlements(count: int = DAYS * 3, lag_ns: int = 0) -> list[Funding]:
    """Three settlements a day from midnight UTC, each public `lag_ns` after it settled."""
    start = day_start_ns(JAN1)
    return [
        Funding(
            instrument_id=InstrumentId.from_str(PERP),
            rate=1e-4 if index % 2 else -5e-5,
            ts_event=start + index * EIGHT_HOURS_NS,
            ts_init=start + index * EIGHT_HOURS_NS + lag_ns,
        )
        for index in range(count)
    ]


def span(days: int = DAYS) -> tuple[date, date]:
    return (JAN1, date.fromordinal(JAN1.toordinal() + days - 1))


def funding_ref(**over: Any) -> Ref:
    return Ref(instrument=PERP, type="funding", resolution=None, span=span(), **over)


def test_no_publication_rule_governs_funding() -> None:
    """Nothing to declare: the settlement is the availability, so no delay is derived."""
    assert "funding" not in PUBLICATION_RULES


def test_a_realtime_funding_dataset_is_written_without_a_rule(ws: FakeWorkspace) -> None:
    written = cat.write(ws, settlements(), ref=funding_ref(), source="csv_parquet")

    assert written.manifest.type == "funding"
    assert written.manifest.publication == "realtime"
    assert written.manifest.publication_rule is None
    assert written.manifest.row_count == DAYS * 3
    assert written.manifest.span == span()


def test_funding_public_before_it_settled_is_refused_at_write(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="availability cannot precede the reference time"):
        cat.write(ws, settlements(lag_ns=-1), ref=funding_ref(), source="csv_parquet")
    assert cat.manifests(ws) == {}


WINDOWS = Windows.model_validate(
    {
        "research": {"start": JAN1, "end": date(2024, 1, 10)},
        "certification": {"start": date(2024, 1, 15), "end": date(2024, 1, 20)},
        "forward": {"start": date(2024, 2, 1)},
    }
)


def load_bars(ws: FakeWorkspace, *instruments: str) -> None:
    for instrument in instruments:
        cat.write(
            ws,
            bars(JAN1, DAYS, instrument=instrument),
            ref=Ref(instrument=instrument, span=span()),
            source="synthetic",
        )


def test_a_snapshot_pins_a_funding_dataset_and_covers_with_it(ws: FakeWorkspace) -> None:
    cat.open_catalog(ws).write_data([perpetual()])
    load_bars(ws, PERP)
    funding = cat.write(ws, settlements(), ref=funding_ref(), source="csv_parquet")

    taken = snap.freeze(ws)

    assert funding.manifest.dataset_id in taken.datasets
    assert snap.pinned_datasets(ws) >= {funding.manifest.dataset_id}
    found = snap.covering(ws, [PERP], ["bar", "funding"], "1d", WINDOWS)
    assert found is not None and found.snapshot_id == taken.snapshot_id


def test_a_perpetual_without_its_funding_is_not_covered(ws: FakeWorkspace) -> None:
    cat.open_catalog(ws).write_data([perpetual()])
    load_bars(ws, PERP)
    snap.freeze(ws)

    assert snap.covering(ws, [PERP], ["bar", "funding"], "1d", WINDOWS) is None


def test_funding_is_asked_of_the_perpetuals_alone(ws: FakeWorkspace) -> None:
    """A spot leg has no funding to load, so a basis universe is covered without inventing it.

    Which instrument is a perpetual is read from its stored definition, the class
    `hyp validate` reads, and never from the spelling of its id.
    """
    cat.open_catalog(ws).write_data([spot(), perpetual()])
    load_bars(ws, SPOT, PERP)
    cat.write(ws, settlements(), ref=funding_ref(), source="csv_parquet")

    taken = snap.freeze(ws)

    found = snap.covering(ws, [SPOT, PERP], ["bar", "funding"], "1d", WINDOWS)
    assert found is not None and found.snapshot_id == taken.snapshot_id


def test_a_spot_leg_is_still_asked_for_every_other_type(ws: FakeWorkspace) -> None:
    cat.open_catalog(ws).write_data([spot(), perpetual()])
    load_bars(ws, PERP)
    cat.write(ws, settlements(), ref=funding_ref(), source="csv_parquet")
    snap.freeze(ws)

    assert snap.covering(ws, [SPOT, PERP], ["bar", "funding"], "1d", WINDOWS) is None


def test_an_instrument_listed_by_type_is_asked_for_its_own_list_alone(ws: FakeWorkspace) -> None:
    """A universe whose second leg has quotes and whose first has none: covered once the first
    is listed for bars alone, and not before."""
    cat.open_catalog(ws).write_data([spot(), perpetual()])
    load_bars(ws, SPOT, PERP)
    cat.write(
        ws,
        quotes(JAN1, DAYS, instrument=PERP),
        ref=Ref(instrument=PERP, type="quote", resolution=None, span=span()),
        source="synthetic",
    )
    taken = snap.freeze(ws)

    assert snap.covering(ws, [SPOT, PERP], ["bar", "quote"], "1d", WINDOWS) is None
    found = snap.covering(
        ws, [SPOT, PERP], ["bar", "quote"], "1d", WINDOWS, by_instrument={SPOT: ["bar"]}
    )
    assert found is not None and found.snapshot_id == taken.snapshot_id
