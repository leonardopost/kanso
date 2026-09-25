"""A print read from a file that says nothing of its side, and the resting order it reaches.

The engine moves only the ask down for a seller's print, only the bid up for a buyer's, and
both sides to the print for one with no aggressor (`kanso.nautilus.facts`). So a side the
loader invented would decide which resting orders a print could fill: labelled a buyer's, a
print under a resting buy never reaches it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from kanso.data.loaders.csv_parquet import CsvParquetLoader
from kanso.nautilus.backtest import execute

from .conftest import RESEARCH, bars, hypothesis, instrument

RESTING_BUY = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Rests one buy at 9.60 on the first session, under every low the bars will print."""

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.placed = False

    def on_bar(self, bar) -> None:
        if not self.placed:
            self.placed = True
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100, price=9.6)
'''


def prints(tmp_path: Path, side: str | None) -> tuple[object, ...]:
    """One print at 9.55 on the fifth evening, read by `csv_parquet` from a file that maps a
    side column holding `side`, or maps none at all."""
    path = tmp_path / "prints.csv"
    header, row = ["t", "p", "q"], ["2024-01-05 17:00:00", "9.55", "500"]
    columns = {"ts_event": "t", "price": "p", "size": "q"}
    if side is not None:
        header, row = [*header, "side"], [*row, side]
        columns["aggressor_side"] = "side"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerow(row)
    loader = CsvParquetLoader()
    spec = {
        "loader": "csv_parquet",
        "timezone": "UTC",
        "files": [
            {
                "path": str(path),
                "instrument": "DEMO",
                "venue": "XNAS",
                "type": "trade",
                "columns": columns,
            }
        ],
    }
    (ref,) = loader.discover(spec)
    return tuple(loader.load(ref, ref.span))


def fills_under(tmp_path: Path, request_for, side: str | None) -> list[tuple[str, float, float]]:
    request = request_for(
        source=RESTING_BUY, hypothesis_=hypothesis(data_requirements=("bar", "trade"))
    )
    groups = [tuple(bars(RESEARCH)), prints(tmp_path, side)]
    card = execute(request, [instrument()], groups).run
    return [(fill.side, fill.qty, fill.px) for fill in card.fills]


def test_a_print_whose_side_no_one_recorded_reaches_a_resting_buy_beneath_it(
    tmp_path: Path, request_for
) -> None:
    assert fills_under(tmp_path, request_for, None) == [("BUY", 100.0, 9.6)]


def test_the_same_print_labelled_a_buyer_s_never_does(tmp_path: Path, request_for) -> None:
    """The control, and the defect: this is what a fabricated `buyer` did to every file."""
    assert fills_under(tmp_path, request_for, "buyer") == []
    assert fills_under(tmp_path, request_for, "none") == [("BUY", 100.0, 9.6)]
