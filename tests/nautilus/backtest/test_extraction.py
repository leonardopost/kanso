"""What one backtest produces: periods, equity, trades, fills and the costs on them."""

from __future__ import annotations

from pathlib import Path

import pytest

from kanso.criteria.run import BPS
from kanso.errors import ValidationError
from kanso.nautilus.backtest import _instrument_of, run
from kanso.schemas import Hypothesis

from .conftest import (
    BOTH_SLEEVE,
    CAPITAL,
    CLOSING_SLEEVE,
    FILTER_MODIFIER,
    FLAT_SLEEVE,
    INSTRUMENT,
    RESEARCH,
    bars,
    catalog,
    hypothesis,
    instrument,
    quotes,
)

FIXED_BPS_COST = (1.0 + 2.0 + 4.0 / 2.0) / BPS
"""Commission and slippage in full, and half the stated spread on each side."""


def test_a_period_exists_for_every_day_that_held_a_data_event(store: Path, request_for) -> None:
    card = run(request_for(), store).run

    assert card.period == "1d"
    assert len(card.period_ends_ns) == 31
    assert list(card.period_ends_ns) == sorted(card.period_ends_ns)


def test_the_three_series_are_parallel_and_the_returns_are_the_differences(
    store: Path, request_for
) -> None:
    card = run(request_for(), store).run

    assert len(card.returns) == len(card.equity) == len(card.period_ends_ns)
    previous = card.capital
    for value, made in zip(card.equity, card.returns, strict=True):
        assert made == pytest.approx(value - previous)
        previous = value


def test_a_sleeve_that_never_trades_leaves_the_capital_where_it_found_it(
    store: Path, request_for
) -> None:
    card = run(request_for(source=FLAT_SLEEVE), store).run

    assert card.fills == ()
    assert card.trades == ()
    assert set(card.equity) == {CAPITAL}
    assert set(card.returns) == {0.0}


def test_every_fill_carries_the_venue_model_cost_and_carries_it_once(
    store: Path, request_for
) -> None:
    card = run(request_for(), store).run

    assert card.fills
    for fill in card.fills:
        assert fill.cost == pytest.approx(fill.qty * fill.px * FIXED_BPS_COST)


def test_a_trade_is_a_closed_position_netted_of_its_own_fills(store: Path, request_for) -> None:
    card = run(request_for(), store).run

    assert card.trades
    for trade in card.trades:
        gross = (trade.avg_close - trade.avg_open) * trade.qty
        assert trade.cost == pytest.approx(sum(fill.cost for fill in trade.fills))
        assert trade.pnl_net == pytest.approx(gross - trade.cost)
        assert trade.opened_ns < trade.closed_ns


def test_a_position_still_open_when_the_window_closes_is_not_a_trade(
    store: Path, request_for
) -> None:
    card = run(request_for(), store).run
    closing = run(request_for(source=CLOSING_SLEEVE), store).run

    # The trading sleeve leaves one position open, so it has one fill more than its
    # trades account for; the closing sleeve leaves nothing open and they agree.
    assert len(card.fills) > sum(len(trade.fills) for trade in card.trades)
    assert len(closing.fills) == sum(len(trade.fills) for trade in closing.trades)


def test_a_window_that_closes_flat_ends_at_the_capital_plus_what_it_made(
    store: Path, request_for
) -> None:
    card = run(request_for(source=CLOSING_SLEEVE), store).run

    (trade,) = card.trades
    assert card.equity[-1] == pytest.approx(card.capital + trade.pnl_net)


def test_the_run_records_the_venue_model_it_was_costed_by(
    hyp: Hypothesis, store: Path, request_for
) -> None:
    request = request_for()

    card = run(request, store).run

    assert card.venue_model == dict(request.venue_model)
    assert card.currency == "USD"
    assert card.capital == CAPITAL


def test_the_extraction_folds_into_contiguous_calendar_spans(store: Path, request_for) -> None:
    card = run(request_for(), store).run

    folds = card.folds(4)

    assert len(folds) == 4
    assert sum(len(fold.returns) for fold in folds) == len(card.returns)
    assert sum(len(fold.fills) for fold in folds) == len(card.fills)


def test_every_order_intent_is_stamped_with_data_time(store: Path, request_for) -> None:
    result = run(request_for(), store)

    assert result.intents
    events = {bar.ts_event for bar in bars(RESEARCH)}
    for ts_event, instrument_id, side, qty, order_type, price in result.intents:
        assert ts_event in events
        assert instrument_id == INSTRUMENT
        assert side in ("BUY", "SELL")
        assert qty > 0
        assert order_type == "MARKET"
        assert price is None


def test_the_run_reports_what_it_cost_to_produce(store: Path, request_for) -> None:
    result = run(request_for(), store)

    assert result.crashed is False
    assert result.reason is None
    assert result.wall_s > 0
    assert result.peak_mem_gb > 0


# --- the cost model ----------------------------------------------------------


def _quoting_hypothesis() -> Hypothesis:
    return hypothesis(
        data_requirements=("bar", "quote"),
        costs={"commission_bps": 0.0, "slippage_bps": 0.0, "spread": "quotes"},
    )


def test_a_quoted_spread_is_read_from_the_quotes_that_were_published(
    tmp_path: Path, request_for
) -> None:
    hyp = _quoting_hypothesis()
    store = catalog(
        tmp_path / "catalog",
        [*bars(RESEARCH), *quotes(RESEARCH)],
        [instrument()],
    )

    card = run(request_for(hypothesis_=hyp, quotes_available=True), store).run

    assert card.fills
    for fill in card.fills:
        # Two cents wide around the mid, so one side of it is a cent a share.
        assert fill.cost == pytest.approx(fill.qty * 0.01, rel=0.01)


def test_a_fill_before_the_first_quote_bears_no_observed_spread(
    tmp_path: Path, request_for
) -> None:
    hyp = _quoting_hypothesis()
    store = catalog(
        tmp_path / "catalog",
        [*bars(RESEARCH), *quotes(RESEARCH, first=20)],
        [instrument()],
    )

    card = run(request_for(hypothesis_=hyp, quotes_available=True), store).run

    early = [
        fill
        for fill in card.fills
        if fill.ts_ns < min(q.ts_init for q in quotes(RESEARCH, first=20))
    ]
    assert early
    assert {fill.cost for fill in early} == {0.0}


def test_an_instrument_with_no_quotes_at_all_bears_no_observed_spread(
    tmp_path: Path, request_for
) -> None:
    hyp = hypothesis(
        universe=("OTHER.XNAS",),
        data_requirements=("bar", "quote"),
        costs={"commission_bps": 0.0, "slippage_bps": 0.0, "spread": "quotes"},
    )
    store = catalog(
        tmp_path / "catalog",
        [*bars(RESEARCH, "OTHER"), *quotes(RESEARCH)],
        [instrument(), instrument("OTHER")],
    )

    card = run(request_for(hypothesis_=hyp, quotes_available=True), store).run

    assert card.fills
    assert {fill.cost for fill in card.fills} == {0.0}


# --- what else the window may hold -------------------------------------------


def test_a_custom_type_is_loaded_for_the_universe_and_for_nobody_else(
    tmp_path: Path, request_for
) -> None:
    from nautilus_trader.model.identifiers import InstrumentId

    from kanso.criteria.run import midnight_ns
    from kanso.data.types import CorporateAction
    from kanso.nautilus.backtest import window_data

    from .conftest import SECOND_NS

    hyp = hypothesis(data_requirements=("bar", "corporate_action"))
    ts = midnight_ns(RESEARCH[0]) + 5 * 86_400 * SECOND_NS

    def action(instrument_id: str) -> CorporateAction:
        return CorporateAction(
            instrument_id=InstrumentId.from_str(instrument_id),
            kind="split",
            ratio=2.0,
            cash=0.0,
            currency="USD",
            ex_date_ns=ts,
            ts_event=ts,
            ts_init=ts,
        )

    store = catalog(
        tmp_path / "catalog",
        [*bars(RESEARCH), action(INSTRUMENT), action("OTHER.XNAS")],
        [instrument(), instrument("OTHER")],
    )
    request = request_for(hypothesis_=hyp)

    _instruments, groups = window_data(request, store)
    card = run(request, store).run

    custom = [group for group in groups if type(group[0]).__name__ == "CustomData"]
    assert [len(group) for group in custom] == [1]
    assert _instrument_of(custom[0][0]) == INSTRUMENT
    assert card.fills


def test_a_point_belonging_to_no_instrument_belongs_to_no_instrument() -> None:
    assert _instrument_of(object()) is None


def test_a_universe_of_several_instruments_is_all_loaded(tmp_path: Path, request_for) -> None:
    hyp = hypothesis(universe=(INSTRUMENT, "OTHER.XNAS"))
    store = catalog(
        tmp_path / "catalog",
        [*bars(RESEARCH), *bars(RESEARCH, "OTHER")],
        [instrument(), instrument("OTHER")],
    )

    card = run(request_for(hypothesis_=hyp, source=BOTH_SLEEVE), store).run

    assert {fill.instrument_id for fill in card.fills} == {INSTRUMENT, "OTHER.XNAS"}


# --- what the runner refuses to load -----------------------------------------


def test_a_file_that_defines_no_strategy_is_refused(store: Path, request_for) -> None:
    with pytest.raises(
        ValidationError,
        match=r"^strategy\.py: defines no class Strategy subclassing KansoStrategy; "
        r"a sleeve is run by loading Strategy from the file$",
    ):
        run(request_for(source=b"answer = 42\n"), store)


def test_an_attached_modifier_is_consulted_by_the_sleeve(store: Path, request_for) -> None:
    denied = run(
        request_for(modifiers=(("filter", FILTER_MODIFIER, {"allow": False}),)),
        store,
    ).run
    allowed = run(
        request_for(modifiers=(("filter", FILTER_MODIFIER, {"allow": True}),)),
        store,
    ).run

    assert denied.fills == ()
    assert allowed.fills


def test_a_modifier_attached_as_the_wrong_construct_is_refused(store: Path, request_for) -> None:
    with pytest.raises(ValidationError, match="attached as an? 'overlay'"):
        run(request_for(modifiers=(("overlay", FILTER_MODIFIER, {}),)), store)


def test_a_parameter_the_modifier_does_not_take_is_refused(store: Path, request_for) -> None:
    with pytest.raises(ValidationError, match="does not take these parameters"):
        run(request_for(modifiers=(("filter", FILTER_MODIFIER, {"nonesuch": 1}),)), store)


def test_a_file_that_defines_no_modifier_is_refused(store: Path, request_for) -> None:
    with pytest.raises(
        ValidationError,
        match=r"^strategy\.py: defines no class Modifier subclassing KansoModifier; "
        r"a modifier is run by loading Modifier from the file$",
    ):
        run(request_for(modifiers=(("filter", b"answer = 42\n", {}),)), store)


def test_a_trade_print_marks_the_instrument_it_prints(tmp_path: Path, request_for) -> None:
    from .conftest import trades

    hyp = hypothesis(data_requirements=("bar", "trade"))
    store = catalog(
        tmp_path / "catalog",
        [*bars(RESEARCH), *trades(RESEARCH)],
        [instrument()],
    )

    card = run(request_for(hypothesis_=hyp), store).run

    assert card.fills
    assert len(card.period_ends_ns) == 31


def test_one_fill_is_counted_once_however_many_positions_hold_it() -> None:
    # A flip splits a fill across two positions; identity is what keeps the split from
    # becoming two executions in the record.
    from kanso.nautilus.backtest import _fill_events

    class _Event:
        def __init__(self, key: str, ts: int) -> None:
            self.id = key
            self.ts_event = ts
            self.instrument_id = INSTRUMENT
            self.client_order_id = "O-1"
            self.trade_id = key

    class _Position:
        def __init__(self, *events: _Event) -> None:
            self.events = list(events)

    shared = _Event("E-1", 10)
    later = _Event("E-2", 20)

    events, owners = _fill_events([_Position(shared), _Position(shared, later)])

    assert [event.id for event in events] == ["E-1", "E-2"]
    assert owners == (0, 1)


def test_a_return_period_of_no_time_is_refused(store: Path, request_for) -> None:
    with pytest.raises(ValidationError, match="is no time at all"):
        run(request_for(period="0d"), store)
