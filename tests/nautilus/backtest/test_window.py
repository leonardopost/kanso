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
