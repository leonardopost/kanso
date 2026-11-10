"""Builders for the screen suite: small catalogs whose points are stated, not generated.

Every point here is placed by hand, at an instant a test names, so a test about which
session a print falls in, or what a grid samples, reads its answer off its own arguments.
Prices are whole cents.

`TAPE` is a synthetic custom type, written as a workspace extension would write one: a level,
a flag, a switch and a label filed under an instrument — a float, an int, a bool and a str, so
every annotation a signal may or may not read is one of its fields. It is kanso's own fixture,
not an outside party's, and stands for any series a workspace holds that a screen conditions on.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from kanso import ext
from kanso.data.loaders.points import (
    bar_type,
    instrument_id,
    make_bar,
    make_delta,
    make_quote,
    make_trade,
)
from kanso.data.types import custom_types
from tests.nautilus.backtest.conftest import catalog, instrument

VENUE = "XNAS"

TAPE_ID = "kanso_state_tape"
TAPE = """
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.types import register_custom_type

PROVIDES = {"data_types": ["kanso_state_tape"]}


@customdataclass
class KansoStateTape(Data):
    instrument_id: InstrumentId
    level: float
    flag: int
    switch: bool
    label: str


register_custom_type("kanso_state_tape", KansoStateTape)
"""
"""The extension text; no postponed annotations, which the engine's decorator cannot read."""


def tape_type(directory: Path) -> type:
    """The tape's class, registered in this process once, imported from its extension text."""
    found = custom_types().get(TAPE_ID)
    if found is None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{TAPE_ID}.py").write_text(TAPE, encoding="utf-8")
        ext.discover(directory.parent, [directory.name])
        found = custom_types()[TAPE_ID]
    return found


@pytest.fixture(scope="session")
def tape_cls(tmp_path_factory: pytest.TempPathFactory) -> type:
    return tape_type(tmp_path_factory.mktemp("tape") / "kanso_ext")


def tape(
    cls: type,
    instrument: str,
    rows: Iterable[tuple[int, float, int]],
    events: Sequence[int] | None = None,
) -> list[Any]:
    """Points of the tape filed under `instrument` at (ts_init, level, flag), each published
    the instant it refers to unless `events` states its `ts_event`; its switch is on where its
    flag is odd."""
    ident = InstrumentId.from_str(instrument)
    found = []
    for n, (ts, level, flag) in enumerate(rows):
        event = ts if events is None else events[n]
        found.append(
            cls(
                ts_event=event,
                ts_init=ts,
                instrument_id=ident,
                level=level,
                flag=flag,
                switch=bool(flag % 2),
                label="",
            )
        )
    return found


def ns(text: str) -> int:
    """An ISO instant with its offset as UTC nanoseconds."""
    moment = datetime.fromisoformat(text).astimezone(UTC)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def trades(symbol: str, rows: Iterable[tuple[int, int]]) -> list[Any]:
    """Prints of `symbol` at (instant, cents), numbered in the order given."""
    ident = instrument_id(symbol, VENUE)
    return [
        make_trade(ident, cents, 1, AggressorSide.BUYER, f"{symbol}-{n}", 2, 0, ts, ts)
        for n, (ts, cents) in enumerate(rows)
    ]


def bars(symbol: str, rows: Iterable[tuple[int, int]], resolution: str = "1m") -> list[Any]:
    """Bars of `symbol` closing at (instant, cents), flat inside."""
    kind = bar_type(instrument_id(symbol, VENUE), resolution)
    return [make_bar(kind, (c, c, c, c), 100, 2, 0, ts, ts) for ts, c in rows]


def quotes(symbol: str, rows: Iterable[tuple[int, int, int]]) -> list[Any]:
    """Quotes of `symbol` at (instant, bid cents, ask cents)."""
    ident = instrument_id(symbol, VENUE)
    return [make_quote(ident, bid, ask, 5, 5, 2, 0, ts, ts) for ts, bid, ask in rows]


def deltas(symbol: str, rows: Iterable[tuple[int, str, str, int, int]]) -> list[Any]:
    """Level-two changes of `symbol` at (instant, action, side, cents, size)."""
    ident = instrument_id(symbol, VENUE)
    actions = {"add": BookAction.ADD, "update": BookAction.UPDATE, "delete": BookAction.DELETE}
    sides = {"bid": OrderSide.BUY, "ask": OrderSide.SELL}
    return [
        make_delta(ident, actions[action], sides[side], cents, size, 0, 2, 0, ts, ts)
        for ts, action, side, cents, size in rows
    ]


def store(root: Path, points: Sequence[Any], symbols: Sequence[str]) -> ParquetDataCatalog:
    """A catalog holding these points and an equity definition of each symbol."""
    return ParquetDataCatalog(str(catalog(root, points, [instrument(s) for s in symbols])))
