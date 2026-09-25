"""A custom data requirement, read from the catalog, carried into a card and handed over.

A hypothesis may require a registered custom type — `corporate_action` is kanso's own. The
parent reads the type's points for the universe over the window, the child is handed them
by pickle, and the sleeve receives each at the instant it became public, as the type
itself, without a subscription of its own: a researched `strategy.py` may not import the
class a subscription would need.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from kanso import ext
from kanso.criteria.run import midnight_ns
from kanso.data.loaders.csv_parquet import CsvParquetLoader
from kanso.data.types import CorporateAction
from kanso.nautilus.backtest import run, run_subprocess

from .conftest import (
    CLOSE_NS,
    INSTRUMENT,
    RESEARCH,
    SECOND_NS,
    bars,
    catalog,
    hypothesis,
    instrument,
)

DAY_NS = 86_400 * SECOND_NS

DIVIDEND_TAKER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Buys a share for every cent of dividend declared, the moment it is declared."""

    config_cls = KansoConfig

    def on_bar(self, bar) -> None:
        return

    def on_data(self, data) -> None:
        if data.kind == "dividend":
            self.submit_entry(data.instrument_id, "BUY", qty=round(data.cash * 100))
'''


def declared(cents: int, day: int) -> CorporateAction:
    """A dividend of `cents` declared at the close of the research window's `day`-th day."""
    at = midnight_ns(RESEARCH[0]) + day * DAY_NS + CLOSE_NS
    return CorporateAction(
        ts_event=at,
        ts_init=at + SECOND_NS,
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        kind="dividend",
        ratio=1.0,
        cash=cents / 100,
        currency="USD",
        ex_date_ns=at + 14 * DAY_NS,
    )


def test_a_card_hands_the_sleeve_every_declaration_its_window_holds(
    tmp_path: Path, request_for
) -> None:
    held = catalog(
        tmp_path / "catalog", [*bars(RESEARCH), declared(24, 3), declared(26, 10)], [instrument()]
    )
    request = request_for(
        source=DIVIDEND_TAKER,
        hypothesis_=hypothesis(data_requirements=("bar", "corporate_action")),
    )

    carded = run_subprocess(request, held, tmp_path)
    in_process = run(request, held)

    assert not carded.crashed, carded.traceback_tail
    assert [(fill.side, fill.qty) for fill in carded.run.fills] == [("BUY", 24.0), ("BUY", 26.0)]
    assert carded.run.fills == in_process.run.fills


def test_a_hypothesis_that_does_not_require_the_type_is_not_handed_it(
    tmp_path: Path, request_for
) -> None:
    """The requirement is what is loaded and what is subscribed; the catalog is not."""
    held = catalog(tmp_path / "catalog", [*bars(RESEARCH), declared(24, 3)], [instrument()])

    carded = run_subprocess(request_for(source=DIVIDEND_TAKER), held, tmp_path)

    assert not carded.crashed, carded.traceback_tail
    assert carded.run.fills == ()


# --- a type a workspace extension registers ----------------------------------

TAPE = """
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.types import register_custom_type

PROVIDES = {"data_types": ["kanso_card_tape"]}


@customdataclass
class KansoCardTape(Data):
    instrument_id: InstrumentId
    shares: int


register_custom_type("kanso_card_tape", KansoCardTape)
"""
"""An extension's own type. No `from __future__ import annotations`: the decorator reads the
annotations as they are written."""

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


Taped = tuple[Path, tuple[tuple[str, str], ...]]
"""A catalog, and the extensions a card has to import to read it."""


@pytest.fixture(scope="module")
def taped(tmp_path_factory: pytest.TempPathFactory) -> Taped:
    """A catalog holding the window's bars and two prints of an extension's type, read
    from a CSV by the `csv_parquet` loader, and where the extension was imported from.

    Built once for the module: a type is registered once per process, so the extension
    that defines it is imported once and its classes are the same objects in every test.
    """
    root = tmp_path_factory.mktemp("taped")
    extensions = root / "ws" / "kanso_ext"
    extensions.mkdir(parents=True)
    (extensions / "kanso_card_tape.py").write_text(TAPE, encoding="utf-8")
    found = ext.discover(root / "ws", ["kanso_ext"])
    assert [one.ok for one in found] == [True], [one.error for one in found]
    rows = root / "tape.csv"
    with rows.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", "who", "n"])
        writer.writerow(["2024-01-04 16:30:00", INSTRUMENT, "3"])
        writer.writerow(["2024-01-11 16:30:00", INSTRUMENT, "5"])
    loader = CsvParquetLoader()
    spec = {
        "loader": "csv_parquet",
        "timezone": "UTC",
        "files": [
            {
                "path": str(rows),
                "instrument": "DEMO",
                "venue": "XNAS",
                "type": "kanso_card_tape",
                "columns": {"ts_event": "t", "instrument_id": "who", "shares": "n"},
            }
        ],
    }
    (ref,) = loader.discover(spec)
    points = list(loader.load(ref, ref.span))
    held = catalog(root / "catalog", [*bars(RESEARCH), *points], [instrument()])
    return held, ext.sources(found)


def test_a_card_imports_the_extension_whose_type_it_is_handed(
    tmp_path: Path, request_for, taped: Taped
) -> None:
    """The child has no workspace to discover from: it is handed where the extension lives
    and imports it before it unpickles a point of the type it defines."""
    held, extensions = taped
    request = request_for(
        source=TAPE_TAKER,
        hypothesis_=hypothesis(data_requirements=("bar", "kanso_card_tape")),
    )

    carded = run_subprocess(request, held, tmp_path, extensions)
    in_process = run(request, held)

    assert not carded.crashed, carded.traceback_tail
    assert [(fill.side, fill.qty) for fill in carded.run.fills] == [("BUY", 3.0), ("BUY", 5.0)]
    assert carded.run.fills == in_process.run.fills


def test_a_card_not_handed_the_extension_cannot_read_its_type(
    tmp_path: Path, request_for, taped: Taped
) -> None:
    """The control: the same card, told nothing, dies unpickling the first print."""
    held, _ = taped
    request = request_for(
        source=TAPE_TAKER,
        hypothesis_=hypothesis(data_requirements=("bar", "kanso_card_tape")),
    )

    carded = run_subprocess(request, held, tmp_path)

    assert carded.crashed and carded.reason == "died"
    assert "No module named 'kanso_card_tape'" in (carded.traceback_tail or "")
