"""Fixtures for the store: a stand-in workspace and the points written into it.

Everything here is the engine's own data, written into a real `ParquetDataCatalog` under
`tmp_path`. No loader and no vendor is involved: a dataset reference is the small frozen
record the loader package hands the writer, and the tests build it themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from kanso.data.catalog import NANOS_PER_SECOND, day_start_ns

settings.register_profile(
    "catalog",
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.load_profile("catalog")

AAPL = "AAPL.XNAS"
MSFT = "MSFT.XNAS"
DAILY = "1-DAY-LAST-EXTERNAL"
CLOSE_NS = 16 * 3600 * NANOS_PER_SECOND
"""Sixteen hours into the UTC day: a session close, so a lag stays inside the same day."""


@dataclass(frozen=True)
class FakeWorkspace:
    """The one attribute the store reads off a workspace."""

    root: Path


@dataclass(frozen=True)
class Ref:
    """A dataset reference shaped like the loader package's, built by hand."""

    instrument: str = AAPL
    type: str = "bar"
    resolution: str | None = "1d"
    span: tuple[date, date] = (date(2024, 1, 1), date(2024, 1, 5))
    adjusted: bool = False
    publication: str = "realtime"
    publication_rule: str | None = None
    vendor: str | None = None
    vendor_dataset: str | None = None
    request_params: dict[str, str] | None = None
    dataset_id: str = field(default="")


@pytest.fixture
def ws(tmp_path: Path) -> FakeWorkspace:
    return FakeWorkspace(root=tmp_path)


@pytest.fixture
def other_ws(tmp_path: Path) -> FakeWorkspace:
    """A second, independent workspace, for asking whether two stores agree."""
    root = tmp_path / "second"
    root.mkdir()
    return FakeWorkspace(root=root)


def days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=n) for n in range(count)]


def bar(instrument: str, day: date, lag_ns: int = 0, spec: str = DAILY) -> Bar:
    ts_event = day_start_ns(day) + CLOSE_NS
    return Bar(
        BarType.from_str(f"{instrument}-{spec}"),
        Price.from_str("100.00"),
        Price.from_str("101.00"),
        Price.from_str("99.00"),
        Price.from_str("100.50"),
        Quantity.from_str("1000"),
        ts_event,
        ts_event + lag_ns,
    )


def bars(
    start: date, count: int, instrument: str = AAPL, lag_ns: int = 0, spec: str = DAILY
) -> list[Bar]:
    return [bar(instrument, day, lag_ns, spec) for day in days(start, count)]


def quote(instrument: str, day: date, lag_ns: int = 0) -> QuoteTick:
    ts_event = day_start_ns(day) + CLOSE_NS
    return QuoteTick(
        InstrumentId.from_str(instrument),
        Price.from_str("100.00"),
        Price.from_str("100.02"),
        Quantity.from_str("100"),
        Quantity.from_str("100"),
        ts_event,
        ts_event + lag_ns,
    )


def quotes(start: date, count: int, instrument: str = AAPL, lag_ns: int = 0) -> list[QuoteTick]:
    return [quote(instrument, day, lag_ns) for day in days(start, count)]


def trade(instrument: str, day: date, lag_ns: int = 0, seq: int = 0) -> TradeTick:
    ts_event = day_start_ns(day) + CLOSE_NS
    return TradeTick(
        InstrumentId.from_str(instrument),
        Price.from_str("100.00"),
        Quantity.from_str("10"),
        AggressorSide.BUYER,
        TradeId(f"T{day:%Y%m%d}{seq}"),
        ts_event,
        ts_event + lag_ns,
    )


def equity(instrument: str = AAPL, increment: str = "0.01") -> Equity:
    identifier = InstrumentId.from_str(instrument)
    return Equity(
        instrument_id=identifier,
        raw_symbol=identifier.symbol,
        currency=USD,
        price_precision=len(increment.partition(".")[2]),
        price_increment=Price.from_str(increment),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def define(ws: FakeWorkspace, *instruments: str) -> None:
    """Put a definition of each instrument in the store, once.

    A snapshot over instrument data is refused while the store holds no definition, so a
    workspace that is to be frozen defines what its datasets name. Written only when the
    store lacks the id: the engine skips a same-dated rewrite and says so on stdout.
    """
    from kanso.data.catalog import open_catalog

    catalog = open_catalog(ws)
    held = {str(item.id) for item in catalog.instruments()}
    fresh = [equity(name) for name in (instruments or (AAPL,)) if name not in held]
    if fresh:
        catalog.write_data(fresh)
