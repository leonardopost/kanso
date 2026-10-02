"""Which window a run may read, and the refusal that is the embargo."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.model.enums import BarAggregation

from kanso.errors import PreconditionError
from kanso.nautilus.backtest import execute, grains_of, run, run_subprocess, stage_of, window_data
from kanso.schemas import Hypothesis

from .conftest import CERTIFICATION, RESEARCH, bars, catalog, hypothesis, instrument, second_bars


def test_the_two_windows_a_run_may_name(hyp: Hypothesis) -> None:
    assert stage_of(hyp, RESEARCH) == "research"
    assert stage_of(hyp, CERTIFICATION) == "certification"


def test_the_forward_window_is_never_backtested(hyp: Hypothesis) -> None:
    with pytest.raises(PreconditionError, match="is not a window demo_mr declares"):
        stage_of(hyp, (date(2024, 3, 1), date(2024, 3, 31)))


def test_a_window_the_hypothesis_does_not_declare_is_refused(hyp: Hypothesis) -> None:
    with pytest.raises(PreconditionError, match="is not a window"):
        stage_of(hyp, (date(2024, 1, 1), date(2024, 2, 29)))


def test_a_card_may_not_ask_for_the_certification_window(
    hyp: Hypothesis, store: Path, tmp_path: Path, request_for
) -> None:
    # The embargo, enforced in code: the card path refuses the window that judges it,
    # and refuses it before any data is read.
    lane = tmp_path / "lane"
    lane.mkdir()

    with pytest.raises(PreconditionError, match="certification window"):
        run_subprocess(request_for(CERTIFICATION), store, lane)


def test_a_card_may_not_ask_for_a_window_that_is_neither(
    store: Path, tmp_path: Path, request_for
) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()

    with pytest.raises(PreconditionError, match="is not a window"):
        run_subprocess(request_for((date(2024, 1, 1), date(2024, 1, 15))), store, lane)


def test_the_certification_window_runs_in_process(store: Path, request_for) -> None:
    result = run(request_for(CERTIFICATION), store)

    opens = min(result.run.period_ends_ns)
    assert result.run.window == CERTIFICATION
    assert opens >= result.run.bounds[0]


def test_a_research_run_reads_only_the_research_window(store: Path, request_for) -> None:
    # The catalog holds both windows; the run must see only one of them.
    result = run(request_for(RESEARCH), store)

    opens, closes = result.run.bounds
    assert len(result.run.period_ends_ns) == 31
    assert all(opens <= ts < closes for ts in result.run.period_ends_ns)
    assert all(opens <= fill.ts_ns < closes for fill in result.run.fills)


def test_only_the_window_and_only_the_universe_is_loaded(
    hyp: Hypothesis, store: Path, request_for
) -> None:
    instruments, groups = window_data(request_for(RESEARCH), store)

    assert [str(found.id) for found in instruments] == list(hyp.universe)
    assert sum(len(group) for group in groups) == 31


def test_an_overlay_card_loads_the_host_grain_and_its_own(tmp_path: Path, request_for) -> None:
    points = [*bars(RESEARCH), *second_bars(RESEARCH)]
    store = catalog(tmp_path / "both", points, [instrument()])
    overlay = hypothesis().model_copy(update={"resolution": "1s"})
    request = request_for(RESEARCH, hypothesis_=overlay, grains=("1d", "1s"))
    _instruments, groups = window_data(request, store)
    aggregations = {group[0].bar_type.spec.aggregation for group in groups}

    assert sum(len(group) for group in groups) == 31 + 62
    assert BarAggregation.SECOND in aggregations
    assert BarAggregation.DAY in aggregations


def test_an_overlay_card_refuses_a_missing_host_grain(tmp_path: Path, request_for) -> None:
    store = catalog(tmp_path / "seconds-only", second_bars(RESEARCH), [instrument()])
    overlay = hypothesis().model_copy(update={"resolution": "1s"})
    request = request_for(RESEARCH, hypothesis_=overlay, grains=("1d", "1s"))

    with pytest.raises(PreconditionError, match="holds no 1d bars"):
        window_data(request, store)


def test_data_from_another_window_is_refused_at_the_door(
    tmp_path: Path, store: Path, request_for
) -> None:
    # The child is handed its data rather than a catalog, so the window check has to
    # survive the crossing: points from the wrong window are refused, not traded on.
    request = request_for(RESEARCH)
    instruments, _ = window_data(request, store)

    with pytest.raises(PreconditionError, match="lies outside the requested window"):
        execute(request, instruments, [tuple(bars(CERTIFICATION))])


def test_a_window_the_catalog_cannot_answer_is_refused(
    tmp_path: Path, hyp: Hypothesis, request_for
) -> None:
    empty = catalog(tmp_path / "only-cert", bars(CERTIFICATION), [instrument()])

    with pytest.raises(PreconditionError, match="the catalog holds nothing"):
        run(request_for(RESEARCH), empty)


def test_an_unresolved_instrument_is_refused_before_anything_runs(
    tmp_path: Path, request_for
) -> None:
    from .conftest import hypothesis

    hyp = hypothesis(universe=["DEMO.XNAS", "OTHER.XNAS"])
    store = catalog(tmp_path / "catalog", bars(RESEARCH), [instrument()])

    with pytest.raises(PreconditionError, match="no definition for OTHER.XNAS"):
        run(request_for(RESEARCH, hypothesis_=hyp), store)


def test_two_grains_are_loaded_only_for_an_overlay_at_a_grain_of_its_own() -> None:
    sleeve = hypothesis()
    overlay = sleeve.model_copy(
        update={
            "resolution": "1s",
            "construct_": sleeve.construct.model_copy(update={"id": "overlay"}),
        }
    )
    filtering = overlay.model_copy(
        update={"construct_": sleeve.construct.model_copy(update={"id": "filter"})}
    )

    assert grains_of(sleeve, None) == ()
    assert grains_of(overlay, "1d") == ("1d", "1s")
    assert grains_of(overlay, "1s") == ()
    assert grains_of(filtering, "1d") == ()


TIED_PRINTS = 30_000
"""Enough prints that share instants unevenly for the catalog's sorted query to reorder some
of them: measured on 2026-10-02, a synthetic file of this many, its instants holding 1 to 40
prints each, came back in file order through that query at 12,000 prints and out of it at
30,000. The real file the defect was found on held 210,855 of its 250,013 OKX BTC-USDT-SWAP
prints (2026-06-22, from 00:00 UTC) at an instant another print shared."""


def _tied_prints() -> list[object]:
    """`TIED_PRINTS` prints from noon on 2024-01-02, ten seconds between instants and 1 to 40
    prints an instant, each print's trade id its place in the file."""
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
    from nautilus_trader.model.objects import Price, Quantity

    from kanso.criteria.run import midnight_ns

    from .conftest import SECOND_NS, SYMBOL, _venue

    ident = InstrumentId(Symbol(SYMBOL), _venue())
    instant = midnight_ns(date(2024, 1, 2)) + 12 * 3_600 * SECOND_NS
    made: list[object] = []
    cohort = 0
    while len(made) < TIED_PRINTS:
        for _ in range(1 + cohort * 7_919 % 40):
            made.append(
                TradeTick(
                    ident,
                    Price(10.0 + len(made) % 7 / 100, 2),
                    Quantity.from_int(1 + len(made) % 3),
                    AggressorSide.BUYER,
                    TradeId(str(len(made))),
                    ts_event=instant,
                    ts_init=instant,
                )
            )
        instant += 10 * SECOND_NS
        cohort += 1
    return made[:TIED_PRINTS]


def test_prints_sharing_instants_are_read_in_file_order_however_the_window_is_read(
    tmp_path: Path, request_for
) -> None:
    """The points of one instant reach a run in the order the catalog's files hold them, read
    whole as a run in process reads the window or an hour at a time as a card streams it: the
    order of an instant's prints is part of what a card is handed, and a card must not
    depend on where its window was cut."""
    import pickle

    from kanso.nautilus import backtest as runner

    from .conftest import tick_hypothesis

    document = tick_hypothesis().model_dump(mode="json")
    document["data_requirements"] = ["trade"]
    request = request_for(hypothesis_=Hypothesis.model_validate(document))
    store = catalog(tmp_path / "tied", _tied_prints(), [instrument()])
    in_file = [str(index) for index in range(TIED_PRINTS)]

    _, groups = window_data(request, store)
    whole = [str(point.trade_id) for group in groups for point in group]  # type: ignore[attr-defined]
    records = [pickle.loads(record) for record in runner._payload(request, store, ())]
    chunks = [record["groups"] for record in records if "groups" in record]
    streamed = [str(p.trade_id) for chunk in chunks for group in chunk for p in group]

    assert len(chunks) == 5, "an hour a read: a little over four hours of prints"
    assert whole == in_file
    assert streamed == in_file
