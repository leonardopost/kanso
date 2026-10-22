"""After each chunk a run hands `Marks.price` the fills it may not have priced yet, and prices
exactly what it priced when it was handed every fill the run held.

`_fills_since` reads the positions the cache holds and only the position snapshots taken
since the last chunk, each unpickled once, where the full scan unpickled every snapshot after
every chunk. The run below opens, flips, closes and reopens a position over daily chunks, so
the engine snapshots it again and again, and its fills pay the half-spread its quotes state,
so a fill priced differently would move the card.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

from kanso.criteria.run import day_of
from kanso.nautilus import backtest
from kanso.nautilus.backtest import _fills_since, execute_chunked, window_data

from .conftest import RESEARCH, bars, catalog, hypothesis, instrument, quotes

CYCLER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    \"\"\"Buys, flips short, covers, and buys again, over and over: one open, flip, close and
    reopen every four bars.\"\"\"

    config_cls = Config

    def on_start(self):
        self.seen = 0

    def on_bar(self, bar):
        self.seen += 1
        name = bar.bar_type.instrument_id
        step = self.seen % 4
        if step == 1:
            self.submit_entry(name, "BUY", qty=100)
        elif step == 2:
            self.submit_entry(name, "SELL", qty=250)
        elif step == 3:
            self.submit_exit(name)
"""


def _daily(groups):
    days = sorted({day_of(int(p.ts_init)) for g in groups for p in g})
    return [
        tuple(
            chunk
            for chunk in (tuple(p for p in g if day_of(int(p.ts_init)) == day) for g in groups)
            if chunk
        )
        for day in days
    ]


def test_a_chunked_run_prices_every_fill_as_the_full_scan_priced_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request_for
) -> None:
    hyp = hypothesis(
        data_requirements=("bar", "quote"),
        costs={"commission_bps": 0.0, "slippage_bps": 0.0, "spread": "quotes"},
    )
    store = catalog(tmp_path / "catalog", [*bars(RESEARCH), *quotes(RESEARCH)], [instrument()])
    request = request_for(source=CYCLER, hypothesis_=hyp, quotes_available=True)
    instruments, groups = window_data(request, store)
    chunks = _daily(groups)

    read = execute_chunked(request, instruments, chunks)
    monkeypatch.setattr(
        backtest,
        "_fills_since",
        lambda cache, _read: list(backtest._fill_events(backtest._positions(cache))[0]),
    )
    scanned = execute_chunked(request, instruments, chunks)

    assert len(chunks) == 31
    assert len(read.run.trades) >= 12, "the position was opened, flipped and closed again"
    assert any(fill.cost > 0.0 for fill in read.run.fills), "each fill paid its quoted half"
    assert pickle.dumps(read.run) == pickle.dumps(scanned.run)
    assert read.intents == scanned.intents


def _frame(*ids: str) -> bytes:
    return pickle.dumps(SimpleNamespace(events=[SimpleNamespace(id=i) for i in ids]))


class _Cache:
    """What `_fills_since` asks of a cache: its positions and its pickled snapshots."""

    def __init__(self) -> None:
        self.live = [SimpleNamespace(events=[SimpleNamespace(id="live")])]
        self.frames: dict[str, list[bytes]] = {"P-1": [_frame("a", "b")]}

    def positions(self) -> list[SimpleNamespace]:
        return self.live

    def position_snapshot_ids(self) -> set[str]:
        return set(self.frames)

    def position_snapshot_bytes(self, position_id: str) -> list[bytes]:
        return self.frames[position_id]


def _ids(events) -> list[str]:
    return sorted(event.id for event in events)


def test_a_snapshot_is_read_once_and_a_list_not_the_one_read_is_read_whole() -> None:
    cache = _Cache()
    read: dict[object, tuple[list[bytes], int]] = {}

    assert _ids(_fills_since(cache, read)) == ["a", "b", "live"]
    assert _ids(_fills_since(cache, read)) == ["live"]

    cache.frames["P-1"].append(_frame("c"))
    cache.frames["P-2"] = [_frame("d")]
    assert _ids(_fills_since(cache, read)) == ["c", "d", "live"]

    cache.frames["P-1"] = [_frame("a", "b"), _frame("c")]
    assert _ids(_fills_since(cache, read)) == ["a", "b", "c", "live"]
