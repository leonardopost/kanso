"""What deployment writes down: the capital rule, the client registry and the record itself."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria.run import CardRun, Fill, Trade
from kanso.errors import Exit, KansoError
from kanso.portfolio import approvals, approve, approved, capital, clients, files, records
from kanso.schemas import SANDBOX, Deployment, Limits, Stage
from kanso.state import StateStore
from kanso.workspace import Workspace

LIMITS = Limits(max_gross_pct=100, max_net_pct=100, per_strategy_max_pct=40, daily_loss_pct=3)
JOINED = datetime(2024, 3, 1, tzinfo=UTC)


def a_stage(*entries: tuple[str, float], capital_: float = 100_000.0) -> Stage:
    """A stage holding these strategies at these sizes."""
    return Stage(
        exec="sandbox",
        capital=capital_,
        strategies=[
            Deployment(id=name, version=1, capital=amount, joined_at=JOINED)
            for name, amount in entries
        ],
    )


# --- the capital rule ---------------------------------------------------------


def test_a_new_strategy_takes_the_largest_share_the_limits_allow() -> None:
    assert capital.assign(a_stage(), LIMITS, "one") == pytest.approx(40_000.0)


def test_a_new_strategy_takes_only_what_is_unallocated() -> None:
    assert capital.assign(
        a_stage(("alpha", 40_000), ("bravo", 30_000)), LIMITS, "charlie"
    ) == pytest.approx(30_000.0)


def test_a_full_stage_gives_a_newcomer_nothing() -> None:
    stage = a_stage(("alpha", 40_000), ("bravo", 40_000), ("charlie", 20_000))

    assert capital.assign(stage, LIMITS, "delta") == 0.0


def test_a_new_version_inherits_its_predecessors_share() -> None:
    stage = a_stage(("alpha", 12_345.0), ("bravo", 40_000.0))

    assert capital.assign(stage, LIMITS, "alpha") == pytest.approx(12_345.0)


def test_an_inherited_share_is_still_capped_by_the_current_limit() -> None:
    stage = a_stage(("alpha", 90_000.0), capital_=100_000.0)

    assert capital.assign(stage, LIMITS, "alpha") == pytest.approx(40_000.0)


def test_the_ceiling_is_a_percentage_of_the_stage() -> None:
    assert capital.ceiling(a_stage(capital_=250_000.0), LIMITS) == pytest.approx(100_000.0)


# --- execution clients --------------------------------------------------------


def test_the_only_client_the_framework_ships_is_the_simulated_one() -> None:
    assert clients.builtin() == {"sandbox": SANDBOX}
    assert clients.registry() == {"sandbox": SANDBOX}


def test_an_unknown_client_is_refused_by_name(ws: Workspace) -> None:
    with pytest.raises(KansoError) as raised:
        clients.get("nowhere", ws)

    assert raised.value.code == Exit.PRECONDITION
    assert "sandbox" in raised.value.message


def test_a_declaration_that_is_not_a_list_of_specifications_is_ignored() -> None:
    assert clients._declared(None) == ()
    assert clients._declared("sandbox") == ()
    assert clients._declared([SANDBOX, "sandbox", 3]) == (SANDBOX,)


# --- approvals ----------------------------------------------------------------


def test_an_approval_is_recorded_against_one_version(store: StateStore) -> None:
    approve(store, "alpha", 2, "Leonardo")

    assert approved(store, "alpha", 2) is True
    assert approved(store, "alpha", 1) is False
    assert approved(store, "beta", 2) is False
    assert [one.operator for one in approvals(store, "alpha", 2)] == ["Leonardo"]


def test_approvals_accumulate_oldest_first(store: StateStore) -> None:
    approve(store, "alpha", 1, "Leonardo")
    approve(store, "alpha", 1, "Someone Else")

    assert [one.operator for one in approvals(store, "alpha", 1)] == ["Leonardo", "Someone Else"]


# --- the portfolio file -------------------------------------------------------


def test_placing_a_version_replaces_the_entry_rather_than_joining_it() -> None:
    stage = a_stage(("alpha", 10_000.0))

    placed = files.place(stage, "alpha", 2, 20_000.0, JOINED)

    assert [(e.id, e.version, e.capital) for e in placed.strategies] == [("alpha", 2, 20_000.0)]


def test_entries_stay_in_id_order() -> None:
    stage = files.place(a_stage(("bravo", 10_000.0)), "alpha", 1, 10_000.0, JOINED)

    assert [entry.id for entry in stage.strategies] == ["alpha", "bravo"]


def test_removing_a_strategy_leaves_the_rest() -> None:
    stage = files.remove(a_stage(("alpha", 1.0), ("bravo", 2.0)), "alpha")

    assert [entry.id for entry in stage.strategies] == ["bravo"]
    assert files.held(stage, "alpha") is None


def test_unallocated_never_goes_below_zero() -> None:
    assert files.unallocated(a_stage(("alpha", 100_000.0))) == 0.0
    assert files.allocated(a_stage(("alpha", 1.0), ("bravo", 2.0)), without="alpha") == 2.0


# --- a measured window as plain data ------------------------------------------


def a_fill(ts: int = 1) -> Fill:
    return Fill(ts_ns=ts, instrument_id="DEMO.XNAS", side="BUY", qty=10.0, px=10.5, cost=0.25)


def a_run(**changes: object) -> CardRun:
    base: dict[str, object] = {
        "window": (date(2024, 3, 1), date(2024, 3, 31)),
        "period": "1d",
        "period_ends_ns": (1, 2, 3),
        "returns": (0.1, -0.2, 0.3),
        "equity": (100.1, 99.9, 100.2),
        "trades": (
            Trade(
                opened_ns=1,
                closed_ns=2,
                instrument_id="DEMO.XNAS",
                qty=10.0,
                avg_open=10.0,
                avg_close=11.0,
                pnl_net=9.5,
                cost=0.5,
                fills=(a_fill(1), a_fill(2)),
            ),
        ),
        "fills": (a_fill(1), a_fill(2)),
        "capital": 100.0,
        "currency": "USD",
        "venue_model": {"venue": "XNAS"},
    }
    return CardRun(**{**base, **changes})  # type: ignore[arg-type]


def test_a_measured_window_survives_the_round_trip() -> None:
    run = a_run()

    assert records.decode_run(records.encode_run(run)) == run


@given(
    returns=st.lists(st.floats(-1e6, 1e6, allow_nan=False), min_size=0, max_size=8),
    capital_=st.floats(1.0, 1e9, allow_nan=False),
)
def test_any_series_survives_the_round_trip(returns: list[float], capital_: float) -> None:
    run = a_run(
        period_ends_ns=tuple(range(1, len(returns) + 1)),
        returns=tuple(returns),
        equity=tuple(float(i) for i in range(len(returns))),
        capital=capital_,
    )

    assert records.decode_run(records.encode_run(run)) == run


def test_a_stage_run_is_recorded_as_an_event_and_read_back(store: StateStore) -> None:
    from kanso.nautilus.node import Book, Realised

    realised = Realised(
        strategy_id="alpha",
        version=1,
        capital=1_000.0,
        run=a_run(),
        positions=(Book(instrument_id="DEMO.XNAS", qty=-5.0, price=10.0),),
    )

    written = records.record_stage_run(store, "paper", "sess-1", (realised,))

    read = records.stage_results(store, strategy_id="alpha", version=1)
    assert [one.session_id for one in read] == ["sess-1"]
    assert read[0].run == written[0].run
    assert read[0].pnl == pytest.approx(sum(a_run().returns))
    assert read[0].gross == pytest.approx(50.0)
    assert read[0].net == pytest.approx(-50.0)


def test_stage_runs_can_be_narrowed_to_a_stage_or_a_strategy(store: StateStore) -> None:
    from kanso.nautilus.node import Realised

    one = Realised(strategy_id="alpha", version=1, capital=1.0, run=a_run(), positions=())
    two = Realised(strategy_id="beta", version=1, capital=1.0, run=a_run(), positions=())
    records.record_stage_run(store, "paper", "s1", (one, two))
    records.record_stage_run(store, "live", "s2", (one,))

    assert len(records.stage_results(store)) == 3
    assert len(records.stage_results(store, stage="live")) == 1
    assert [r.strategy_id for r in records.stage_results(store, strategy_id="beta")] == ["beta"]


def test_clearing_a_stage_reports_what_held_it(store: StateStore) -> None:
    store.connection.execute(
        "INSERT INTO strategies (strategy_id, created_at) VALUES ('alpha', '2024-01-01')"
    )
    store.connection.execute(
        "INSERT INTO strategy_versions (strategy_id, version, state, stage, created_at)"
        " VALUES ('alpha', 1, 'paper', 'paper', '2024-01-01')"
    )

    assert records.clear_stage(store, "alpha", "paper") == [1]
    assert records.clear_stage(store, "alpha", "paper") == []


def test_a_version_is_named_the_same_way_everywhere() -> None:
    assert records.subject_of("alpha", 3) == "alpha@3"
