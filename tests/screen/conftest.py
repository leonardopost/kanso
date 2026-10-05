"""Builders for the screen suite: small catalogs whose points are stated, not generated.

Every point here is placed by hand, at an instant a test names, so a test about which
session a print falls in, or what a grid samples, reads its answer off its own arguments.
Prices are whole cents.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from kanso.data.loaders.points import (
    bar_type,
    instrument_id,
    make_bar,
    make_delta,
    make_quote,
    make_trade,
)
from tests.nautilus.backtest.conftest import catalog, instrument

VENUE = "XNAS"


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
