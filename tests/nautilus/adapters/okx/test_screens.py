"""What this adapter declares to a screen: the series it can fetch, and the spec it fetches with."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from nautilus_trader.model.identifiers import InstrumentId

from kanso.nautilus.adapters.okx.history import HistorySpec
from kanso.nautilus.adapters.okx.reference import ADAPTER
from tests.nautilus.backtest.conftest import instrument

SWAP = SimpleNamespace(id=InstrumentId.from_str("BTC-USDT-SWAP.OKX"))


def test_it_serves_a_listed_swap_s_bars_prints_and_book() -> None:
    assert ADAPTER.serves(None, SWAP, "1s") == ("bar", "trade", "book")
    assert ADAPTER.serves(None, SWAP, None) == ("bar", "trade", "book")
    assert ADAPTER.serves(None, SWAP, "7s") == ("trade", "book")
    assert ADAPTER.serves(None, instrument("MARA"), "1s") == ()
    assert ADAPTER.serves(None, object(), "1s") == ()
    assert ADAPTER.timestamps == "exchange"


def test_its_specs_are_ones_the_history_loaders_admit() -> None:
    first, last = date(2026, 9, 1), date(2026, 9, 30)
    bars = ADAPTER.spec_for(None, SWAP, "bar", "1s", first, last)
    trades = ADAPTER.spec_for(None, SWAP, "trade", None, first, last)
    book = ADAPTER.spec_for(None, SWAP, "book", None, first, last)

    assert [bars[0], trades[0], book[0]] == ["okx_bars", "okx_trades", "okx_book"]
    assert bars[1]["resolution"] == "1s" and "levels" not in bars[1]
    assert book[1]["levels"] == 1 and "resolution" not in book[1]
    for _, spec in (bars, trades, book):
        assert HistorySpec.model_validate(spec).instruments == ["BTC-USDT-SWAP.OKX"]
