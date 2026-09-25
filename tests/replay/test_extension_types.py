"""A type a workspace extension registers, replayed by a process that starts knowing nothing.

A composed version is resolved from its files, and nothing on that route asks the construct
catalogue — which is what used to import a workspace's extensions, incidentally, on every
other route. So the replay is run the way an operator runs it, as a command in a fresh
interpreter: whatever makes the extension's type readable there is the replay's own doing.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from kanso import ext
from kanso.data import catalog
from kanso.data.loaders.csv_parquet import CsvParquetLoader
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import INSTRUMENT, carded, composed, document

TAPE = """
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.types import register_custom_type

PROVIDES = {"data_types": ["kanso_replay_tape"]}


@customdataclass
class KansoReplayTape(Data):
    instrument_id: InstrumentId
    shares: int


register_custom_type("kanso_replay_tape", KansoReplayTape)
"""
"""An extension's own type, spelled without postponed annotations as the decorator needs."""

TAPE_TAKER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Buys the shares each tape print names, the moment the print is public."""

    config_cls = KansoConfig

    def on_bar(self, bar) -> None:
        return

    def on_data(self, data) -> None:
        self.submit_entry(data.instrument_id, "BUY", qty=data.shares)
'''

TAPED = ["bar", "kanso_replay_tape"]


def tape(ws: Workspace, tmp_path: Path) -> None:
    """The extension, and two prints of its type in the forward window, loaded by the
    `csv_parquet` loader into the workspace's catalog as `kanso data load` loads a file."""
    extensions = ws.path("kanso_ext")
    extensions.mkdir()
    (extensions / "kanso_replay_tape.py").write_text(TAPE, encoding="utf-8")
    found = ext.discover(ws.root, ws.config.extensions_paths)
    assert [one.ok for one in found] == [True], [one.error for one in found]
    rows = tmp_path / "tape.csv"
    with rows.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", "who", "n"])
        writer.writerow(["2024-03-05 16:30:00", INSTRUMENT, "7"])
        writer.writerow(["2024-03-12 16:30:00", INSTRUMENT, "9"])
    loader = CsvParquetLoader()
    (ref,) = loader.discover(
        {
            "loader": "csv_parquet",
            "timezone": "UTC",
            "files": [
                {
                    "path": str(rows),
                    "instrument": "DEMO",
                    "venue": "XNAS",
                    "type": "kanso_replay_tape",
                    "columns": {"ts_event": "t", "instrument_id": "who", "shares": "n"},
                }
            ],
        }
    )
    catalog.write(ws, loader.load(ref, ref.span), ref=ref, source="csv_parquet")


def test_both_replay_paths_read_an_extension_s_type_and_agree(
    ws: Workspace, store: StateStore, tmp_path: Path
) -> None:
    tape(ws, tmp_path)
    doc = document(data_requirements=TAPED)
    hyp_id = carded(ws, store, doc=doc, strategy=TAPE_TAKER)
    composed(ws, store, hyp_id, sleeve=TAPE_TAKER, doc=doc)

    replayed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from kanso.cli import app; app()",
            "--workspace",
            str(ws.root),
            "replay",
            "parity",
            "--strategy",
            hyp_id,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    parity = json.loads(replayed.stdout)
    assert parity["identical"] is True, parity["divergence"]
    assert parity["node_intents"] == parity["engine_intents"] == 2, "one order per print"
    assert parity["compared"] == 2
