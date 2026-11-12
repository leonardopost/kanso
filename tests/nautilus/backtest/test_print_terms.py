"""A print's terms in the catalog, read beside it under the print rules.

Under `print_through` and `print_through_whole` the runner hands the venue each print its
`print_terms` mark not lit as a `kanso.nautilus.tape.Unlit`, which fills no resting order and
moves no queue; a print they mark lit, and one they say nothing of, is the print it always was.
Under `touch` and `through` the terms are not read at all, and neither are terms the run's
snapshot does not pin. A sleeve that rests no order reads a tape with terms exactly as one
without.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.data import manifest as m
from kanso.data import snapshot as snap
from kanso.data.types import PrintTerms
from kanso.nautilus.backtest import (
    RunRequest,
    _catalog,
    execute,
    termed,
    window_chunks,
    window_data,
)
from kanso.nautilus.tape import Unlit
from kanso.schemas import Hypothesis
from kanso.schemas.yamlio import write_yaml

from .conftest import INSTRUMENT, RESEARCH, catalog, hypothesis, instrument

SECOND_NS = 1_000_000_000
DAY_NS = 86_400 * SECOND_NS
DAY = date(2024, 1, 3)
OPEN_NS = midnight_ns(DAY) + 15 * 3_600 * SECOND_NS
NAME = InstrumentId.from_str(INSTRUMENT)

POSTING = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    \"\"\"Joins the bid of the first quote it sees with 100, and rests there.\"\"\"

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.posted = False

    def on_quote_tick(self, tick) -> None:
        if not self.posted:
            self.posted = True
            self.submit_entry(tick.instrument_id, "BUY", qty=100, price=float(tick.bid_price))
"""

TAKING = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    \"\"\"Buys 50 at market on the first quote and sells them on the third print.\"\"\"

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.quotes = 0
        self.prints = 0

    def on_quote_tick(self, tick) -> None:
        self.quotes += 1
        if self.quotes == 1:
            self.submit_entry(tick.instrument_id, "BUY", qty=50)

    def on_trade_tick(self, tick) -> None:
        self.prints += 1
        if self.prints == 3:
            self.submit_exit(tick.instrument_id)
"""


def tape() -> list[object]:
    """A quote of 10.00/10.02 showing 100 on each side, then four prints of 100 at the bid a
    second apart — P1 and P3 lit, P2 not, P4 with no terms — and a quote after them."""
    quote = QuoteTick(
        NAME, Price(10.00, 2), Price(10.02, 2), Quantity.from_int(100), Quantity.from_int(100),
        OPEN_NS, OPEN_NS,
    )  # fmt: skip
    prints = [
        TradeTick(
            NAME,
            Price(10.00, 2),
            Quantity.from_int(100),
            AggressorSide.NO_AGGRESSOR,
            TradeId(f"P{n}"),
            OPEN_NS + n * SECOND_NS,
            OPEN_NS + n * SECOND_NS,
        )
        for n in range(1, 5)
    ]
    last = QuoteTick(
        NAME, Price(10.00, 2), Price(10.02, 2), Quantity.from_int(100), Quantity.from_int(100),
        OPEN_NS + 9 * SECOND_NS, OPEN_NS + 9 * SECOND_NS,
    )  # fmt: skip
    return [quote, *prints, last]


def terms(readings: dict[str, bool]) -> list[PrintTerms]:
    """Terms for the prints `readings` names, lit or not, at each print's own instants."""
    return [
        PrintTerms(
            instrument_id=NAME,
            trade_id=name,
            exchange="4" if not lit else "11",
            conditions="" if lit else "37",
            lit=lit,
            ts_event=OPEN_NS + int(name[1:]) * SECOND_NS,
            ts_init=OPEN_NS + int(name[1:]) * SECOND_NS,
        )
        for name, lit in readings.items()
    ]


READINGS = {"P1": True, "P2": False, "P3": True}


def pinned(root: Path, request: RunRequest, types: tuple[str, ...]) -> RunRequest:
    """`request` pinned to a snapshot of `root` naming a dataset of each of `types` over the
    tape's day, its manifest beside it."""
    ids = []
    for directory in (m.MANIFESTS_DIR, m.SNAPSHOTS_DIR):
        (root / directory).mkdir(exist_ok=True)
    for type_ in types:
        dataset = m.dataset_id(INSTRUMENT, type_, None, False, DAY)
        manifest = m.Manifest(
            dataset_id=dataset,
            source="synthetic",
            instrument=INSTRUMENT,
            type=type_,
            span=(DAY, DAY),
            row_count=1,
            checksum=f"{len(ids)}" * 64,
        )
        write_yaml(manifest, root / m.MANIFESTS_DIR / f"{dataset}.yaml")
        ids.append(dataset)
    checksums = [f"{n}" * 64 for n in range(len(ids))]
    held = snap.Snapshot(
        snapshot_id=snap.snapshot_id(checksums, "f" * 64),
        datasets=ids,
        checksums=checksums,
        instruments_checksum="f" * 64,
        created_at="2024-01-03T00:00:00Z",
    )
    write_yaml(held, root / m.SNAPSHOTS_DIR / f"{held.snapshot_id}.yaml")
    return replace(request, snapshot_id=held.snapshot_id)


TAPED = ("trade", "print_terms")


def taped(rule: str) -> Hypothesis:
    document = hypothesis(data_requirements=("quote", "trade"), resolution="tick").model_dump(
        mode="json"
    )
    document["costs"] = {**document["costs"], "limit_fill": rule, "latency_ms": 0.0}
    return Hypothesis.model_validate(document)


def kinds(groups: tuple[tuple[object, ...], ...]) -> list[str]:
    return [
        type(point).__name__ for group in groups for point in group if isinstance(point, TradeTick)
    ]


@pytest.mark.parametrize("rule", ["print_through", "print_through_whole"])
def test_a_print_its_terms_mark_not_lit_is_handed_on_unlit_under_a_print_rule(
    tmp_path: Path, request_for, rule: str
) -> None:
    root = catalog(tmp_path / "catalog", [*tape(), *terms(READINGS)], [instrument()])
    request = pinned(root, request_for(RESEARCH, source=POSTING, hypothesis_=taped(rule)), TAPED)

    _, whole = window_data(request, root)
    _, chunks = window_chunks(request, root)

    assert kinds(whole) == ["TradeTick", "Unlit", "TradeTick", "TradeTick"]
    assert [kinds(chunk) for chunk in chunks] == [kinds(whole)]
    unlit = next(p for group in whole for p in group if type(p) is Unlit)
    assert (str(unlit.trade_id), float(unlit.price), float(unlit.size)) == ("P2", 10.0, 100.0)


@pytest.mark.parametrize("rule", ["touch", "through"])
def test_under_the_engine_s_rules_no_print_is_read_for_its_terms(
    tmp_path: Path, request_for, rule: str
) -> None:
    root = catalog(tmp_path / "catalog", [*tape(), *terms(READINGS)], [instrument()])
    request = pinned(root, request_for(RESEARCH, source=POSTING, hypothesis_=taped(rule)), TAPED)
    _, whole = window_data(request, root)
    assert kinds(whole) == ["TradeTick"] * 4


@pytest.mark.parametrize("types", [(), ("trade",)])
def test_terms_the_run_s_snapshot_does_not_pin_are_not_read(
    tmp_path: Path, request_for, types: tuple[str, ...]
) -> None:
    """Terms loaded after the snapshot a run is pinned to, or with no snapshot held at all,
    move none of its fills: every print is handed on as the print it was."""
    root = catalog(tmp_path / "catalog", [*tape(), *terms(READINGS)], [instrument()])
    request = request_for(RESEARCH, source=POSTING, hypothesis_=taped("print_through"))
    if types:
        request = pinned(root, request, types)
    _, whole = window_data(request, root)
    _, chunks = window_chunks(request, root)
    assert kinds(whole) == ["TradeTick"] * 4
    assert [kinds(chunk) for chunk in chunks] == [kinds(whole)]


def test_terms_that_read_one_print_two_ways_are_held_for_neither(
    tmp_path: Path, request_for
) -> None:
    twice = [*terms({"P1": True, "P2": False}), *terms({"P2": True})]
    root = catalog(tmp_path / "catalog", [*tape(), *twice], [instrument()])
    request = pinned(
        root, request_for(RESEARCH, source=POSTING, hypothesis_=taped("print_through")), TAPED
    )
    _, whole = window_data(request, root)
    assert kinds(whole) == ["TradeTick"] * 4


def test_two_prints_of_one_instant_are_each_read_by_the_terms_of_their_own_id(
    tmp_path: Path, request_for
) -> None:
    """Prints share an instant often enough on a consolidated tape: each is read by the terms
    that name its trade id, so only the one they mark not lit is handed on unlit."""
    at = OPEN_NS + 2 * SECOND_NS
    prints = [
        TradeTick(
            NAME, Price(10.00, 2), Quantity.from_int(100), AggressorSide.NO_AGGRESSOR,
            TradeId(name), at, at,
        )
        for name in ("A", "B")
    ]  # fmt: skip
    readings = [
        PrintTerms(
            instrument_id=NAME, trade_id=name, exchange="11" if lit else "4",
            conditions="" if lit else "37", lit=lit, ts_event=at, ts_init=at,
        )
        for name, lit in (("A", True), ("B", False))
    ]  # fmt: skip
    root = catalog(tmp_path / "catalog", [tape()[0], *prints, *readings], [instrument()])
    request = pinned(
        root, request_for(RESEARCH, source=POSTING, hypothesis_=taped("print_through")), TAPED
    )
    _, whole = window_data(request, root)
    handed = [p for group in whole for p in group if isinstance(p, TradeTick)]
    assert [(type(p).__name__, str(p.trade_id)) for p in handed] == [
        ("TradeTick", "A"),
        ("Unlit", "B"),
    ]


def test_prints_with_no_terms_over_their_span_are_handed_back_as_they_came(
    tmp_path: Path,
) -> None:
    """No prints, and prints over a span the catalog holds no terms of — here the terms are
    the next day's — are handed back untouched."""
    later = [
        PrintTerms(
            instrument_id=NAME, trade_id=held.trade_id, exchange="4", conditions="37",
            lit=False, ts_event=held.ts_event + DAY_NS, ts_init=held.ts_init + DAY_NS,
        )
        for held in terms(READINGS)
    ]  # fmt: skip
    root = catalog(tmp_path / "catalog", [*tape(), *later], [instrument()])
    prints = tuple(p for p in tape() if isinstance(p, TradeTick))
    held = _catalog(root)
    span = (OPEN_NS, OPEN_NS + 9 * SECOND_NS)

    assert termed(held, instrument(), *span, ()) == ()
    assert termed(held, instrument(), *span, prints) is prints


@pytest.mark.parametrize("filed", [False, True])
def test_prints_the_catalog_s_print_files_do_not_give_are_read_by_their_own_ids(
    tmp_path: Path, filed: bool
) -> None:
    """A print's id is read off the catalog's print files only when those give exactly the
    prints handed; where they hold none, or others, it is read off each print itself."""
    root = catalog(
        tmp_path / "catalog", [*(tape() if filed else []), *terms(READINGS)], [instrument()]
    )
    p2, p3 = (p for p in tape() if isinstance(p, TradeTick) and str(p.trade_id) in {"P2", "P3"})
    span = (OPEN_NS, OPEN_NS + 9 * SECOND_NS)

    handed = termed(_catalog(root), instrument(), *span, (p2, p3))

    assert [(type(p).__name__, str(p.trade_id)) for p in handed] == [  # type: ignore[attr-defined]
        ("Unlit", "P2"),
        ("TradeTick", "P3"),
    ]


def fills_of(tmp_path: Path, request_for, source: bytes, held: bool) -> object:
    root = catalog(
        tmp_path / ("held" if held else "bare"),
        [*tape(), *(terms(READINGS) if held else [])],
        [instrument()],
    )
    request = request_for(RESEARCH, source=source, hypothesis_=taped("print_through"))
    if held:
        request = pinned(root, request, TAPED)
    instruments, groups = window_data(request, root)
    return execute(request, instruments, groups)


def test_a_resting_order_fills_only_on_lit_prints_and_one_with_no_terms(
    tmp_path: Path, request_for
) -> None:
    """The buy joins a bid showing 100: P1 trades what stood ahead of it, and P2 fills it on a
    tape with no terms; with P2 marked not lit it waits for P3."""
    bare = fills_of(tmp_path, request_for, POSTING, held=False)
    held = fills_of(tmp_path, request_for, POSTING, held=True)

    def seconds(result: object) -> list[tuple[int, float]]:
        return [((f.ts_ns - OPEN_NS) // SECOND_NS, f.qty) for f in result.run.fills]  # type: ignore[attr-defined]

    assert seconds(bare) == [(2, 100.0)]
    assert seconds(held) == [(3, 100.0)]


def test_a_card_that_rests_no_order_reads_a_tape_with_terms_as_one_without(
    tmp_path: Path, request_for
) -> None:
    bare = fills_of(tmp_path, request_for, TAKING, held=False)
    held = fills_of(tmp_path, request_for, TAKING, held=True)

    assert held.run.fills  # type: ignore[attr-defined]
    assert held.run == bare.run  # type: ignore[attr-defined]
    assert held.intents == bare.intents  # type: ignore[attr-defined]
