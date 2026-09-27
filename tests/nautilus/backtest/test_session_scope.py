"""A session scope: a name's market data is delivered only on the sessions its flag admits.

The runner reads the scope series before the market types and loads a name's bars only on
the sessions its point carries the flag above zero, with `always` names loaded whatever
their points say. A node delivers every subscription, so the strategy base drops what the
runner would not have loaded, and a run over the unfiltered points fills exactly as a run
over the filtered ones.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from kanso import ext
from kanso.criteria.run import day_of, midnight_ns
from kanso.data.loaders.csv_parquet import CsvParquetLoader
from kanso.nautilus.backtest import execute, run, run_subprocess, window_data
from kanso.schemas import Hypothesis

from .conftest import INSTRUMENT, RESEARCH, SECOND_NS, bars, catalog, hypothesis, instrument

OTHER = "OTHER.XNAS"
FLAGGED = (date(2024, 1, 4), date(2024, 1, 11))

TAPE = """
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.types import register_custom_type

PROVIDES = {"data_types": ["kanso_scope_tape"]}


@customdataclass
class KansoScopeTape(Data):
    instrument_id: InstrumentId
    in_play: int


register_custom_type("kanso_scope_tape", KansoScopeTape)
"""

OTHER_TAKER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Buys a share of the second name on every bar of it the harness hands over."""

    config_cls = KansoConfig

    def on_bar(self, bar) -> None:
        if str(bar.bar_type.instrument_id) == "OTHER.XNAS":
            self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=1)
'''


def scoped(always: tuple[str, ...] = (INSTRUMENT,)) -> Hypothesis:
    """Both names, the tape required, the first name always in scope unless said otherwise."""
    base = hypothesis(universe=(INSTRUMENT, OTHER), data_requirements=("bar", "kanso_scope_tape"))
    return Hypothesis.model_validate(
        {
            **base.model_dump(by_alias=True),
            "session_scope": {
                "series": "kanso_scope_tape",
                "flag": "in_play",
                "always": list(always),
            },
        }
    )


Taped = tuple[Path, tuple[tuple[str, str], ...]]


@pytest.fixture(scope="module")
def taped(tmp_path_factory: pytest.TempPathFactory) -> Taped:
    """Both names' bars over the window, and a tape point per name per session stamped a
    second after midnight — before the session's bar — flagging the second name on two days."""
    root = tmp_path_factory.mktemp("scoped")
    extensions = root / "ws" / "kanso_ext"
    extensions.mkdir(parents=True)
    (extensions / "kanso_scope_tape.py").write_text(TAPE, encoding="utf-8")
    found = ext.discover(root / "ws", ["kanso_ext"])
    assert [one.ok for one in found] == [True], [one.error for one in found]
    rows = root / "tape.csv"
    with rows.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", "who", "flag"])
        days = (RESEARCH[1] - RESEARCH[0]).days + 1
        for index in range(days):
            day = date.fromordinal(RESEARCH[0].toordinal() + index)
            stamp = f"{day.isoformat()} 00:00:01"
            writer.writerow([stamp, INSTRUMENT, "0"])
            writer.writerow([stamp, OTHER, "1" if day in FLAGGED else "0"])
    loader = CsvParquetLoader()
    spec = {
        "loader": "csv_parquet",
        "timezone": "UTC",
        "files": [
            {
                "path": str(rows),
                "instrument": "DEMO",
                "venue": "XNAS",
                "type": "kanso_scope_tape",
                "columns": {"ts_event": "t", "instrument_id": "who", "in_play": "flag"},
            }
        ],
    }
    (ref,) = loader.discover(spec)
    points = list(loader.load(ref, ref.span))
    held = catalog(
        root / "catalog",
        [*bars(RESEARCH), *bars(RESEARCH, "OTHER"), *points],
        [instrument(), instrument("OTHER")],
    )
    return held, ext.sources(found)


def test_the_runner_loads_a_scoped_name_only_on_the_sessions_its_flag_admits(
    request_for, taped: Taped
) -> None:
    held, _ = taped
    request = request_for(source=OTHER_TAKER, hypothesis_=scoped())

    _, groups = window_data(request, held)

    by_name: dict[str, list[date]] = {}
    for group in groups:
        for point in group:
            bar_type = getattr(point, "bar_type", None)
            if bar_type is not None:
                by_name.setdefault(str(bar_type.instrument_id), []).append(
                    day_of(int(point.ts_init))
                )
    assert len(by_name[INSTRUMENT]) == (RESEARCH[1] - RESEARCH[0]).days + 1, "always: every session"
    assert by_name[OTHER] == [day for day in FLAGGED], "the flagged sessions, and no other"


def test_a_card_and_an_in_process_run_fill_only_on_the_admitted_sessions(
    tmp_path: Path, request_for, taped: Taped
) -> None:
    held, extensions = taped
    request = request_for(source=OTHER_TAKER, hypothesis_=scoped())

    carded = run_subprocess(request, held, tmp_path, extensions)
    in_process = run(request, held)

    assert not carded.crashed, carded.traceback_tail
    assert [(fill.side, fill.qty) for fill in carded.run.fills] == [("BUY", 1.0), ("BUY", 1.0)]
    assert carded.run.fills == in_process.run.fills


def test_the_base_drops_what_a_node_would_deliver_outside_the_scope(
    request_for, taped: Taped
) -> None:
    """The parity half: handed every bar, as a node hands them, the strategy fills the same."""
    held, _ = taped
    request = request_for(source=OTHER_TAKER, hypothesis_=scoped())
    unscoped = request_for(
        source=OTHER_TAKER,
        hypothesis_=hypothesis(
            universe=(INSTRUMENT, OTHER), data_requirements=("bar", "kanso_scope_tape")
        ),
    )
    instruments, everything = window_data(unscoped, held)
    assert sum(
        1 for group in everything for point in group if getattr(point, "bar_type", None) is not None
    ) == 2 * ((RESEARCH[1] - RESEARCH[0]).days + 1), (
        "the control: every bar of both names is on offer"
    )

    result = execute(request, instruments, everything)

    assert not result.crashed, result.traceback_tail
    assert [(fill.side, fill.qty) for fill in result.run.fills] == [("BUY", 1.0), ("BUY", 1.0)]
    assert [day_of(int(fill.ts_ns)) for fill in result.run.fills] == list(FLAGGED)


def test_a_scoped_name_with_no_point_is_never_delivered(request_for, taped: Taped) -> None:
    held, _ = taped
    hyp = scoped(always=())
    request = request_for(source=OTHER_TAKER, hypothesis_=hyp)

    _, groups = window_data(request, held)

    names = {
        str(point.bar_type.instrument_id)
        for group in groups
        for point in group
        if getattr(point, "bar_type", None) is not None
    }
    assert names == {OTHER}, "the first name's flag is never above zero, so none of its bars load"
    assert InstrumentId.from_str(INSTRUMENT) is not None
    assert midnight_ns(RESEARCH[0]) < midnight_ns(RESEARCH[1])
    assert SECOND_NS == 1_000_000_000


def test_a_point_of_another_type_leaves_the_scope_alone(
    tmp_path: Path, request_for, taped: Taped
) -> None:
    """The base reads the scope series and nothing else: a dividend arriving under another
    requirement passes through untouched, and the admitted sessions are what the tape said."""
    from kanso.data.types import CorporateAction

    held, extensions = taped
    at = midnight_ns(RESEARCH[0]) + 3 * 86_400 * SECOND_NS + 16 * 3600 * SECOND_NS
    dividend = CorporateAction(
        ts_event=at,
        ts_init=at + SECOND_NS,
        instrument_id=InstrumentId.from_str(OTHER),
        kind="dividend",
        ratio=1.0,
        cash=0.1,
        currency="USD",
        ex_date_ns=at + 14 * 86_400 * SECOND_NS,
    )
    catalog(held, [dividend], [])
    base = hypothesis(
        universe=(INSTRUMENT, OTHER),
        data_requirements=("bar", "kanso_scope_tape", "corporate_action"),
    )
    hyp = Hypothesis.model_validate(
        {
            **base.model_dump(by_alias=True),
            "session_scope": {
                "series": "kanso_scope_tape",
                "flag": "in_play",
                "always": [INSTRUMENT],
            },
        }
    )

    result = run(request_for(source=OTHER_TAKER, hypothesis_=hyp), held)

    assert not result.crashed, result.traceback_tail
    assert [day_of(int(fill.ts_ns)) for fill in result.run.fills] == list(FLAGGED)
