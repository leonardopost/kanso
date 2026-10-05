"""What this adapter declares to a screen: the series it can fetch, and the spec it fetches with."""

from __future__ import annotations

from datetime import date

from kanso.data.adapters.massive import ADAPTER
from tests.nautilus.backtest.conftest import instrument, perpetual


def test_it_serves_a_us_equity_s_bars_prints_and_quotes_and_nothing_else(tmp_path) -> None:
    assert ADAPTER.serves(None, instrument("MARA"), "1s") == ("bar", "trade", "quote")
    assert ADAPTER.serves(None, perpetual(), "1s") == ()
    assert ADAPTER.timestamps == "consolidated_tape"


def test_its_spec_is_one_the_request_path_loader_admits() -> None:
    loader, spec = ADAPTER.spec_for(
        None, instrument("MARA"), "bar", "1s", date(2026, 7, 1), date(2026, 8, 28)
    )

    assert loader == "massive_bars"
    assert spec == {
        "loader": "massive_bars",
        "asset_class": "stocks",
        "instruments": ["MARA"],
        "venue": "XNAS",
        "start": "2026-07-01",
        "end": "2026-08-28",
        "price_precision": 2,
        "resolution": "1s",
    }
    for kind in ("trade", "quote"):
        named, document = ADAPTER.spec_for(
            None, instrument("MARA"), kind, None, date(2026, 7, 1), date(2026, 7, 2)
        )
        assert named == f"massive_{kind}s" and "resolution" not in document
