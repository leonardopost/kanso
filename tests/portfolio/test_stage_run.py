"""What a stage node does with a book, with a construct attached, and with a strategy that fails."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from kanso.nautilus import sandbox, tape
from kanso.nautilus.cross_section import is_marker
from kanso.portfolio import clients, deploy, files, records, set_state, show
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.portfolio.conftest import deployable, reconfigure
from tests.replay.conftest import (
    BLOCKING_FILTER,
    CAPITAL,
    FLAT,
    RAISING,
    REVERTING,
    bars,
    composed,
    document,
    hypothesis,
)
from tests.replay.conftest import carded as a_card

BUYER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys once and holds, so the window closes with a position still open."""

    config_cls = Config

    def on_start(self) -> None:
        self.bought = False

    def on_bar(self, bar) -> None:
        if not self.bought:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.bought = True
'''


LIMITED = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys once limited at the bar's close, which the market takes it at, and holds."""

    config_cls = Config

    def on_start(self) -> None:
        self.bought = False

    def on_bar(self, bar) -> None:
        if not self.bought:
            self.submit_entry(
                bar.bar_type.instrument_id,
                "BUY",
                notional=self.kanso_config.notional,
                price=round(float(bar.close), 2),
            )
            self.bought = True
'''


def with_filter(ws: Workspace, store: StateStore, hyp_id: str) -> StrategyFile:
    """A composed strategy whose sleeve carries a filter that refuses every entry."""
    a_card(ws, store, doc=document(id=hyp_id), strategy=REVERTING)
    file = composed(
        ws,
        store,
        hyp_id,
        sleeve=REVERTING,
        attached=(("blocker", "filter", BLOCKING_FILTER, {}),),
    )
    from kanso.strategy import files as strategy_files

    strategy_files.record(store, hyp_id, file.latest())
    return file


def test_the_book_is_measured_before_the_flatten_and_realised_by_it(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "holder", sleeve=BUYER, doc=document(id="holder"))

    made = deploy(ws, store, "paper")

    realised = made.results[0]
    assert [name for name, _, _, _ in realised.positions] == ["DEMO.XNAS"]
    assert realised.positions[0][3] == 1.0, "a share's contract multiplier"
    assert realised.positions[0][1] > 0, "the window closed holding a long"
    assert realised.gross == pytest.approx(abs(realised.net))
    assert len(realised.run.trades) == 1, "the flatten closed the position it was holding"


def test_the_flatten_lands_under_a_latency_too(ws: Workspace, store: StateStore) -> None:
    """A node's flatten is sent after its last point; under a stated latency it waits in
    flight for a point that never comes, and the venue advances past the delay so it lands
    against the last point's book — the stage still ends flat."""
    doc = document(id="slow")
    doc["costs"] = {**doc.get("costs", {}), "latency_ms": 20}
    deployable(ws, store, "slow", sleeve=BUYER, doc=doc)

    made = deploy(ws, store, "paper")

    realised = made.results[0]
    assert realised.positions[0][1] > 0, "the window closed holding a long"
    assert len(realised.run.trades) == 1, "the flatten closed it, latency and all"
    assert deploy(ws, store, "paper").results[0].positions == ()


def test_a_deployed_stage_lands_what_was_in_flight_then_tells_its_venues_it_is_closing(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage's own drive flattens through `kanso.nautilus.node._closed`: what the sleeves
    still had in flight lands first, then each venue's fill model is told the node is closing
    — so under the print rules a close no quote can fill is filled from the engine's own book,
    which `tests/replay/test_print_through.py` measures on the node's venue — and then the
    flatten lands."""
    done: list[str] = []
    closing, advance = tape.closing, sandbox.SimulatedVenue.advance_past_latency

    def told(exchange: Any) -> None:
        done.append("closing")
        closing(exchange)

    def advanced(venue: Any) -> None:
        done.append("advance")
        advance(venue)

    monkeypatch.setattr(tape, "closing", told)
    monkeypatch.setattr(sandbox.SimulatedVenue, "advance_past_latency", advanced)
    deployable(ws, store, "holder", sleeve=BUYER, doc=document(id="holder"))

    deploy(ws, store, "paper")

    assert done == ["advance", "closing", "advance"]


def test_a_stage_charges_a_limit_taken_inside_it_the_tick_the_card_does(
    ws: Workspace, store: StateStore
) -> None:
    """One tick stated: a buy limited at the close and taken there pays none of it, read off
    the limit the order carried, on the stage's realised window as in a card — the realised
    paper objective reads the card's arithmetic — while the flatten at market pays it whole."""
    from kanso.nautilus.costs import fill_cost

    doc = document(id="limited")
    doc["costs"] = {**doc["costs"], "slippage_ticks": 1.0}
    deployable(ws, store, "limited", sleeve=LIMITED, doc=doc)

    realised = deploy(ws, store, "paper").results[0]

    bought, flattened = realised.run.fills
    assert (bought.side, bought.maker, bought.limit) == ("BUY", False, bought.px)
    assert (flattened.side, flattened.limit, flattened.tick) == ("SELL", None, 0.01)
    rates = (0.5, 1.0, 2.0 / 2.0 / 10_000)
    for fill, slip in ((bought, 0.0), (flattened, 0.01)):
        assert fill.tick == 0.01
        assert fill.cost == pytest.approx(
            fill_cost(
                fill.qty * fill.px,
                fill.qty,
                *rates[:2],
                rates[2],
                None,
                0.0,
                maker=False,
                sell=fill.side == "SELL",
                slip=slip,
            ),
            rel=1e-12,
        )


def test_a_second_restart_finds_the_stage_flat(ws: Workspace, store: StateStore) -> None:
    deployable(ws, store, "holder", sleeve=BUYER, doc=document(id="holder"))
    deploy(ws, store, "paper")

    assert deploy(ws, store, "paper").results[0].positions == ()


def test_an_attached_construct_answers_only_its_own_sleeve(
    ws: Workspace, store: StateStore
) -> None:
    with_filter(ws, store, "guarded")
    deployable(ws, store, "plain", sleeve=REVERTING, doc=document(id="plain"))

    made = deploy(ws, store, "paper")

    by_id = {one.strategy_id: one for one in made.results}
    assert not any(trade.qty > 0 for trade in by_id["guarded"].run.trades), (
        "its filter refuses every entry, so it never opens a long"
    )
    assert any(trade.qty > 0 for trade in by_id["plain"].run.trades), (
        "and answers nobody else's sleeve, which trades the saw-tooth as it always does"
    )


def test_a_strategy_that_raises_halts_the_node_rather_than_the_process(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "boomer", sleeve=RAISING, doc=document(id="boomer"))

    made = deploy(ws, store, "paper")

    assert made.halted is not None
    assert made.session is not None
    assert made.results[0].run.trades == ()


BURSTING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Rests three buys far under the market on its first bar: one more than two a minute."""

    config_cls = Config

    def on_start(self) -> None:
        self.rested = False

    def on_bar(self, bar) -> None:
        if not self.rested:
            for share in (0.5, 0.6, 0.7):
                price = round(float(bar.close) * share, 2)
                self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=1, price=price)
            self.rested = True
'''


@pytest.mark.parametrize(("per_minute", "sent"), [(None, 3), (2, 2)], ids=["unrated", "rated"])
def test_a_stage_node_drops_an_entry_past_the_rate_and_runs_on(
    ws: Workspace, store: StateStore, per_minute: int | None, sent: int
) -> None:
    """A stage runs the sleeve's own code, so it holds the version to the rate it was
    certified under as a card does: at two a minute the third entry of an instant is dropped
    unsent, and the node runs its window to the end rather than halting."""
    doc = document(id="burst")
    if per_minute is not None:
        doc["costs"] = {**doc["costs"], "orders_per_minute": per_minute}
    deployable(ws, store, "burst", sleeve=BURSTING, doc=doc)

    made = deploy(ws, store, "paper")

    assert made.halted is None
    assert made.session is not None and made.session.intents == sent


def test_a_clock_past_the_catalog_runs_no_node(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    ahead = int(datetime(2024, 5, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(ahead),))

    made = deploy(ws, store, "paper")

    assert made.session is None
    assert made.results == ()
    assert made.admitted, "the version stays deployed; there is simply nothing to replay"


def test_a_stage_that_has_fallen_behind_is_not_live(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    behind = int(datetime(2024, 3, 2, tzinfo=UTC).timestamp()) * 1_000_000_000
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(behind),))

    assert show(ws, store).stage("paper").live is False


def test_a_halted_stage_is_never_live(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    files.halt(ws, "paper")

    paper = show(ws, store).stage("paper")
    assert paper.kill_switch is True
    assert paper.live is False
    assert paper.allocated == pytest.approx(paper.strategies[0].capital)


def test_clearing_the_kill_switch_lets_the_stage_deploy_again(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    files.halt(ws, "paper")
    files.halt(ws, "paper", on=False)

    assert deploy(ws, store, "paper").admitted


def test_a_universe_the_catalog_holds_nothing_for_has_no_last_day(ws: Workspace) -> None:
    from kanso.portfolio.deploy import served_to

    assert served_to(ws, ("GHOST.XNAS",)) is None
    assert served_to(ws, ()) is None


def test_a_broken_extension_leaves_the_clients_that_load(ws: Workspace) -> None:
    from kanso.portfolio import exec_clients

    directory = ws.path("kanso_ext")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brokenext.py").write_text("this is not python(", encoding="utf-8")

    assert set(exec_clients(ws)) == set(clients.builtin())


def test_a_state_move_is_written_to_the_file_and_the_store(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    written = set_state(ws, store, composed_strategy.id, 1, "promotable")

    assert written.latest().state == "promotable"
    row = store.connection.execute(
        "SELECT state FROM strategy_versions WHERE strategy_id = ?", (composed_strategy.id,)
    ).fetchone()
    assert row[0] == "promotable"


def test_the_deployment_reports_the_capital_it_committed(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    reconfigure(ws, "paper", capital=200_000.0)

    made = deploy(ws, store, "paper")

    assert made.capital == pytest.approx(80_000.0)
    assert made.admitted[0].label == f"{composed_strategy.id}@1"
    assert records.subject_of(composed_strategy.id, 1) == made.admitted[0].label


# --- warming --------------------------------------------------------------------

WARMED = document(id="warm", warmup={"sessions": 3})
"""The reverting sleeve warmed on three sessions; cold it needs three closes before it acts."""

MARCH_15_CLOSE_NS = int(datetime(2024, 3, 15, 16, 0, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
"""The availability instant of the March 15 bar: a clock a stage stopped at mid-window."""


def first_intent_of(ws: Workspace, session_id: str) -> int:
    from kanso.replay import record

    return min(intent.ts_event for intent in record.intents_of(ws, session_id))


def bar_close_ns(day: int) -> int:
    return int(datetime(2024, 3, day, 16, 0, tzinfo=UTC).timestamp()) * 1_000_000_000


def test_a_warmed_stage_trades_from_its_first_session_and_claims_only_the_window(
    ws: Workspace, store: StateStore
) -> None:
    from kanso.replay import record
    from kanso.replay.record import Point

    deployable(ws, store, "warm", sleeve=REVERTING, doc=WARMED)

    made = deploy(ws, store, "paper")

    assert made.session is not None
    march = bars((date(2024, 3, 1), date(2024, 3, 31)))
    assert made.session.released == 31, "March, and not a day of February"
    assert made.session.stream_sha256 == record.digest(Point.of(bar) for bar in march)
    assert march[0].ts_event == bar_close_ns(1) < march[0].ts_init
    assert made.session.clock_ns == march[-1].ts_init
    assert made.results[0].run.window[0].isoformat() == "2024-03-01"
    assert first_intent_of(ws, made.session.session_id) == bar_close_ns(1), (
        "warmed on February's last three sessions it buys March's first bar, a trough; "
        "cold it would wait for its third close and the trough four sessions on"
    )


def test_a_restart_with_nothing_but_its_prefix_to_replay_is_idle(
    ws: Workspace, store: StateStore
) -> None:
    """The prefix is fed on every restart; a restart that would feed only the prefix feeds
    nothing, or the clock would walk backwards and the span replay forever."""
    deployable(ws, store, "warm", sleeve=REVERTING, doc=WARMED)
    first = deploy(ws, store, "paper")
    assert first.session is not None

    second = deploy(ws, store, "paper")

    assert second.session is not None
    assert second.session.released == 0
    assert second.session.clock_ns is None
    from kanso.portfolio import clock_of

    assert clock_of(store, "paper") == first.session.clock_ns


def test_a_restart_warms_on_the_sessions_at_or_before_its_clock(
    ws: Workspace, store: StateStore
) -> None:
    """Restarted flat mid-window, the node re-warms on what it replayed and trades on."""
    from kanso.replay import record
    from kanso.replay.record import Point

    deployable(ws, store, "warm", sleeve=REVERTING, doc=WARMED)
    deploy(ws, store, "paper")
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(MARCH_15_CLOSE_NS),))

    made = deploy(ws, store, "paper")

    assert made.session is not None
    rest = bars((date(2024, 3, 16), date(2024, 3, 31)))
    assert made.session.released == 16, "March 16 through 31"
    assert made.session.stream_sha256 == record.digest(Point.of(bar) for bar in rest)
    assert rest[0].ts_init > MARCH_15_CLOSE_NS
    assert made.session.clock_ns == rest[-1].ts_init
    assert first_intent_of(ws, made.session.session_id) == bar_close_ns(17), (
        "warmed on the 13th to the 15th it sees the fall through the 17th and buys; cold "
        "it would have its third close on the 18th and the next trough on the 21st"
    )


def test_the_feed_carries_the_prefix_and_each_version_s_view_does_not(
    ws: Workspace, store: StateStore
) -> None:
    from datetime import date

    from kanso.data.manifest import catalog_path
    from kanso.nautilus import node
    from tests.portfolio.test_node import a_placement

    deployable(ws, store, "warm", sleeve=REVERTING, doc=WARMED)
    deployable(ws, store, "cold", sleeve=REVERTING, doc=document(id="cold"))
    window = (date(2024, 3, 15), date(2024, 3, 31))
    warm = a_placement(ws, "warm", hyp=hypothesis(**WARMED))
    cold = a_placement(ws, "cold", hyp=hypothesis(id="cold"))
    requests = (
        cold.request(window),
        warm.request(window, (date(2024, 3, 13), date(2024, 3, 15))),
    )

    loaded = node._window_data(requests, catalog_path(ws), MARCH_15_CLOSE_NS)

    fed = [int(point.ts_init) for point in loaded.points if not is_marker(point)]
    assert len(fed) == 3 + 16, "the warmed version's three sessions, then the sixteen new"
    assert fed[0] == bar_close_ns(13) + 1_000_000_000
    assert [len(group) for groups in loaded.per_version for group in groups] == [16, 16]
    assert all(
        int(point.ts_init) > MARCH_15_CLOSE_NS
        for groups in loaded.per_version
        for group in groups
        for point in group
    )
    assert all(
        fed[-len(group) :] == [int(point.ts_init) for point in group]
        for groups in loaded.per_version
        for group in groups
    ), "every version's own view is a suffix of the shared feed of its series"


def fills_of(made: object, strategy_id: str) -> list[tuple[int, str, float]]:
    realised = next(one for one in made.results if one.strategy_id == strategy_id)  # type: ignore[attr-defined]
    return sorted(
        (fill.ts_ns, fill.side, fill.qty) for trade in realised.run.trades for fill in trade.fills
    )


@pytest.mark.parametrize(
    ("own", "mates"),
    [(None, 3), (2, 5)],
    ids=["a cold version beside a warmed one", "a shallower warmup beside a deeper one"],
)
def test_a_version_measures_on_a_stage_as_it_does_alone_whatever_its_stage_mate_warms_on(
    ws: Workspace, store: StateStore, own: int | None, mates: int
) -> None:
    """The feed is shared and cut at the deepest warmup on the stage; each version is handed
    only the span its own request delivers, so a stage-mate's prefix warms nobody else.
    The mate trades nothing, so what it adds to the stage is its prefix and nothing more."""
    subject = document(id="subject", **({} if own is None else {"warmup": {"sessions": own}}))
    deployable(ws, store, "subject", sleeve=REVERTING, doc=subject)
    alone = fills_of(deploy(ws, store, "paper"), "subject")
    store.connection.execute("DELETE FROM sessions")
    deployable(ws, store, "mate", sleeve=FLAT, doc=document(id="mate", warmup={"sessions": mates}))

    beside = fills_of(deploy(ws, store, "paper"), "subject")

    assert beside == alone and len(alone) >= 14
    if own is None:
        assert alone[0][0] == bar_close_ns(5) + 1_000_000_000, (
            "cold, the reverting sleeve needs three closes and buys the trough on the 5th; "
            "fed its mate's prefix it would have bought March's first bar"
        )


def test_two_versions_in_one_name_each_trade_as_they_do_alone_and_end_flat(
    ws: Workspace, store: StateStore
) -> None:
    """Two copies of the reverting sleeve on one paper stage share one account and one
    name. Each reads, sizes and exits its own position, never the account's, so each
    realises the fills it realises alone and ends the window flat — reading the account's
    net position, each exit sold the mate's shares too, and both ended the window short."""
    deployable(ws, store, "first", sleeve=REVERTING, doc=document(id="first"))
    made = deploy(ws, store, "paper")
    alone = fills_of(made, "first")
    assert made.results[0].positions == () and len(alone) >= 14
    store.connection.execute("DELETE FROM sessions")
    deployable(ws, store, "second", sleeve=REVERTING, doc=document(id="second"))

    beside = deploy(ws, store, "paper")

    assert fills_of(beside, "first") == alone
    assert fills_of(beside, "second") == alone
    assert [one.positions for one in beside.results] == [(), ()]


# --- a benchmark ------------------------------------------------------------------


HELD = document(
    id="held",
    warmup={"sessions": 3},
    benchmark={"hold": "first_leg"},
    objective={"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 0.5}},
)
"""The warmed reverting sleeve, measured against a hold of its one instrument."""


def test_a_stage_stores_the_hold_beside_the_window_the_version_realised(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "held", sleeve=REVERTING, doc=HELD)
    deployable(ws, store, "plain", sleeve=REVERTING, doc=document(id="plain"))

    made = deploy(ws, store, "paper")

    held, plain = sorted(made.results, key=lambda result: result.strategy_id)
    assert plain.benchmark is None
    hold = held.benchmark
    assert hold is not None
    assert hold.window == held.run.window
    assert hold.period_ends_ns == held.run.period_ends_ns, "one span, so the folds pair"
    (entry,) = hold.fills
    assert entry.ts_ns > bar_close_ns(1), "the prefix dropped the hold's order as well"
    assert hold.trades == ()
    recorded = {one.strategy_id: one for one in records.stage_results(store, stage="paper")}
    assert recorded["held"].benchmark == hold
    assert recorded["plain"].benchmark is None


def test_a_restarted_stage_holds_from_the_first_point_after_its_clock(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "held", sleeve=REVERTING, doc=HELD)
    deploy(ws, store, "paper")
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(MARCH_15_CLOSE_NS),))

    made = deploy(ws, store, "paper")

    hold = made.results[0].benchmark
    assert hold is not None
    (entry,) = hold.fills
    assert entry.ts_ns > MARCH_15_CLOSE_NS


def test_an_idle_restart_stores_an_empty_hold(ws: Workspace, store: StateStore) -> None:
    deployable(ws, store, "held", sleeve=REVERTING, doc=HELD)
    deploy(ws, store, "paper")

    idle = deploy(ws, store, "paper").results[0]

    assert idle.benchmark is not None
    assert (idle.benchmark.returns, idle.benchmark.fills) == ((), ())


LATE_RAISING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Trades nothing, and asks the impossible of the tenth bar it sees."""

    config_cls = Config

    def on_start(self) -> None:
        self.seen = 0

    def on_bar(self, bar) -> None:
        self.seen += 1
        if self.seen == 10:
            raise RuntimeError("the replay asked for the impossible")
'''


def test_a_halted_stage_holds_over_the_periods_its_version_is_marked_on(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "held", sleeve=REVERTING, doc=HELD)
    deployable(ws, store, "late", sleeve=LATE_RAISING, doc=document(id="late"))

    made = deploy(ws, store, "paper")

    assert made.halted is not None
    assert made.session is not None and made.session.released == 10
    held = next(result for result in made.results if result.strategy_id == "held")
    hold = held.benchmark
    assert hold is not None
    assert len(held.run.period_ends_ns) == 31, "the version is marked over its whole view"
    assert hold.period_ends_ns == held.run.period_ends_ns, (
        "a hold cut at the halt would span ten sessions beside the version's thirty-one"
    )


# --- the book policy ----------------------------------------------------------------

BOOKED = document(id="booked", book={"reset": "monthly"})
"""A sleeve that buys once and holds, on a book reset at the turn of each month."""

FEBRUARY_29_CLOSE_NS = int(datetime(2024, 2, 29, 16, 0, 1, tzinfo=UTC).timestamp()) * 1_000_000_000


def test_a_restart_seeds_the_book_from_where_the_last_window_left_it(
    ws: Workspace, store: StateStore, tmp_path: Path
) -> None:
    """The node restarts flat at the version's capital, but the cushion and the last end it
    settled carry across: a window that closed in February with 500 set aside, restarted
    in March, restores the entry's costs from that 500 at its first period end — in the
    extraction, and in the balance the node's harness reads at the next bar."""
    from dataclasses import replace

    from kanso.nautilus.node import Realised

    record = tmp_path / "balance.txt"
    deployable(ws, store, "booked", sleeve=recording_buyer(record), doc=BOOKED)
    first = deploy(ws, store, "paper").results[0]
    record.unlink()
    assert set(first.run.cushion) == {0.0}, "March turns no month inside itself"
    february = replace(
        first.run,
        window=(date(2024, 2, 29), date(2024, 2, 29)),
        period_ends_ns=(FEBRUARY_29_CLOSE_NS,),
        returns=(0.0,),
        equity=(first.capital,),
        trades=(),
        fills=(),
        held=(),
        cushion=(500.0,),
        carry=(0.0,),
        worst_ratio=(None,),
    )
    records.record_stage_run(
        store,
        "paper",
        "an-earlier-window",
        [Realised("booked", 1, first.capital, february, ())],
    )
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(MARCH_15_CLOSE_NS),))

    second = deploy(ws, store, "paper").results[0].run

    assert second.returns[0] < 0, "the entry's costs on March 16"
    assert second.equity[0] == pytest.approx(first.capital), "restored at the month's first end"
    assert second.cushion[0] == pytest.approx(500.0 + second.returns[0])
    (_, entered), (at, restored), *_ = balances(record)
    assert entered == first.capital
    assert at == second.period_ends_ns[1]
    assert restored == pytest.approx(second.equity[1], rel=1e-12), "the seed, restored"


def recording_buyer(record: Path, notional: str = "5_000.0") -> bytes:
    """A sleeve that buys once on its first bar and writes down every balance it reads."""
    return f'''
from pathlib import Path

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys once, sized as it is told, and records the balance before every bar it acts on."""

    config_cls = Config

    def on_start(self) -> None:
        self.bought = False

    def on_bar(self, bar) -> None:
        with Path({str(record)!r}).open("a") as out:
            out.write(f"{{bar.ts_init}} {{self.balance!r}}\\n")
        if not self.bought:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional={notional})
            self.bought = True
'''.encode()


def balances(record: Path) -> list[tuple[int, float]]:
    return [
        (int(ts), float(value))
        for ts, value in (line.split() for line in record.read_text().splitlines())
    ]


def levered(ws: Workspace, store: StateStore, hyp_id: str, sleeve: bytes, **doc: Any) -> None:
    """A deployable version whose implementation carries the hypothesis's own risk limits.

    `composed` generates every implementation from the demo hypothesis; a sleeve that has to
    borrow needs the manifest to say it may."""
    from kanso.strategy.impl import generate

    written = document(id=hyp_id, **doc)
    file = deployable(ws, store, hyp_id, sleeve=sleeve, doc=written)
    generate(
        ws,
        store,
        hyp_id,
        file.latest(),
        hypothesis(id=hyp_id, **doc),
        CAPITAL,
        created_at=datetime(2024, 3, 1, tzinfo=UTC),
    )


def test_a_node_sleeve_reads_the_book_its_carry_left(
    ws: Workspace, store: StateStore, tmp_path: Path
) -> None:
    """Levered to one and a half times its balance on the first bar and held, the version's
    node harness settles the carry at every period end: each balance it reads afterwards is
    the equity the node's extraction struck at that end before the end's own carry, where a
    harness never handed the policy would read the fills alone."""
    record = tmp_path / "balance.txt"
    levered(
        ws,
        store,
        "carried",
        recording_buyer(record, "self.balance * 1.5"),
        risk_limits={"max_position_pct": 200, "max_drawdown_pct": 40, "max_leverage": 2},
        book={"financing_rate_bps": 500.0},
    )

    run = deploy(ws, store, "paper").results[0].run

    assert run.fills and all(charged > 0 for charged in run.carry[:-1])
    ends = list(run.period_ends_ns)
    filled = {fill.ts_ns for fill in run.fills}
    assert filled == {ends[0], ends[-1]}, "the entry, and the flatten at the window's close"
    read = [(ends.index(ts), balance) for ts, balance in balances(record) if ts not in filled]
    assert len(read) == len(ends) - 2
    for index, balance in read:
        assert balance == pytest.approx(run.equity[index] + run.carry[index], rel=1e-12), index


def test_a_node_restart_charges_its_first_carry_from_the_instant_it_resumed(
    ws: Workspace, store: StateStore, tmp_path: Path
) -> None:
    """The newest window on the stage settled on February 29 and the clock stands at March
    15's close: the first carry of the restart covers the day since the clock, not the
    sixteen since the settled end, when nothing was held."""
    from dataclasses import replace

    from kanso.nautilus.costs import NS_PER_YEAR
    from kanso.nautilus.node import Realised

    levered(
        ws,
        store,
        "carried",
        recording_buyer(tmp_path / "balance.txt", "self.balance * 1.5"),
        risk_limits={"max_position_pct": 200, "max_drawdown_pct": 40, "max_leverage": 2},
        book={"financing_rate_bps": 500.0},
    )
    first = deploy(ws, store, "paper").results[0]
    february = replace(
        first.run,
        window=(date(2024, 2, 29), date(2024, 2, 29)),
        period_ends_ns=(FEBRUARY_29_CLOSE_NS,),
        returns=(0.0,),
        equity=(first.capital,),
        trades=(),
        fills=(),
        held=(),
        carry=(0.0,),
        worst_ratio=(None,),
    )
    records.record_stage_run(
        store, "paper", "an-earlier-window", [Realised("carried", 1, first.capital, february, ())]
    )
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(MARCH_15_CLOSE_NS),))

    second = deploy(ws, store, "paper").results[0].run

    end = second.period_ends_ns[0]
    borrowed = second.held[0].notional - (second.equity[0] + second.carry[0])
    assert borrowed > 0
    assert second.carry[0] == pytest.approx(
        borrowed * 0.05 * (end - MARCH_15_CLOSE_NS - 1) / NS_PER_YEAR
    )


# --- the period target ------------------------------------------------------------

WEEKLY = document(
    id="weekly",
    risk_limits={"max_position_pct": 100, "max_drawdown_pct": 40, "max_leverage": 1},
    period_target={
        "period": "week",
        "opens": "Mon 04:00",
        "tz": "America/New_York",
        "target_bp": 1000,
        "of": "capital",
    },
)
"""A sleeve held to a tenth of its capital a New York week, each week opening on Monday at
04:00 there: 08:00 UTC from March 11, 2024, the day after the clocks change."""

RETRYING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys 36,000 of its name at every bar it holds none of it, and holds."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        if self.held(bar.bar_type.instrument_id) == 0:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=36_000.0)
'''

NS = 1_000_000_000
WEEK_NS = int(datetime(2024, 3, 11, 8, tzinfo=UTC).timestamp()) * NS
"""Monday March 11, 04:00 New York: the week the version's last window ended inside."""
NEXT_WEEK_NS = int(datetime(2024, 3, 18, 8, tzinfo=UTC).timestamp()) * NS
WEEK_BEFORE_NS = int(datetime(2024, 3, 4, 9, tzinfo=UTC).timestamp()) * NS
"""Monday March 4, 04:00 New York, still five hours behind UTC."""
TUESDAY_CLOSE_NS = int(datetime(2024, 3, 12, 16, 0, 1, tzinfo=UTC).timestamp()) * NS
"""The availability instant of the March 12 bar, a Tuesday: the clock the stage stopped at."""
SUNDAY_CLOSE_NS = int(datetime(2024, 3, 17, 16, 0, 1, tzinfo=UTC).timestamp()) * NS
"""The availability instant of the March 17 bar, a Sunday: the week's last point."""


def tuesday_close(opening_ns: int) -> int:
    """The availability instant of the bar of the Tuesday of the week opening at `opening_ns`."""
    monday = datetime.fromtimestamp(opening_ns // NS, tz=UTC).date()
    return int(datetime.combine(monday + timedelta(days=1), time(16, 0, 1), UTC).timestamp()) * NS


def ended_tuesday(
    store: StateStore,
    first: records.StageResult,
    *,
    pnl: float,
    stopped: bool = False,
    opening_ns: int = WEEK_NS,
    capital: float | None = None,
    anchor: float | None = None,
    threshold: float | None = None,
    ends_ns: int | None = None,
    clock_ns: int = TUESDAY_CLOSE_NS,
) -> None:
    """Record a window of version 1 that ended at the Tuesday close of the week opening at
    `opening_ns`, or at `ends_ns`, having made `pnl` in that week and stopped at that Tuesday's
    close or not, at `capital` — the first window's own unless stated — the week anchored at
    `anchor`, the capital unless stated, and held to `threshold`, a tenth of the capital unless
    stated; and stand the stage's clock at `clock_ns`, March 12's close unless stated, which a
    stage-mate moves past a window's own end."""
    from dataclasses import replace

    from kanso.criteria.run import day_of
    from kanso.nautilus.node import Realised
    from kanso.schemas import PeriodStops

    capital = first.capital if capital is None else capital
    anchor = capital if anchor is None else anchor
    threshold = 0.1 * capital if threshold is None else threshold
    hit_ns = tuesday_close(opening_ns)
    ends_ns = hit_ns if ends_ns is None else ends_ns
    hit = {
        "opening_ns": opening_ns,
        "anchor": anchor,
        "threshold": threshold,
        "hit_ns": hit_ns,
        "segment": "rth",
        "equity": anchor + threshold,
    }
    kept = PeriodStops.model_validate(
        {
            "openings": [opening_ns],
            "stops": [hit] if stopped else [],
            "last": {
                "opening_ns": opening_ns,
                "anchor": anchor,
                "threshold": threshold,
                "pnl": pnl,
                "stopped": stopped,
            },
        }
    )
    ended = replace(
        first.run.between(opening_ns, ends_ns + 1),
        window=(day_of(opening_ns), day_of(ends_ns)),
        capital=capital,
        period_stops=kept,
    )
    records.record_stage_run(
        store, "paper", "a-window-to-tuesday", [Realised("weekly", 1, capital, ended, ())]
    )
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(clock_ns),))


def bought_on(run: Any) -> list[date]:
    """The UTC days of a window's buys, in order: every bar of the fixture is published at
    16:00:01 UTC, which is the same day in New York."""
    from kanso.criteria.run import day_of

    return [day_of(fill.ts_ns) for fill in run.fills if fill.side == "BUY"]


WEEKLY_WARMED = {**WEEKLY, "warmup": {"sessions": 2}}
"""`WEEKLY`, warmed on the two sessions before its window, and on a restart on the two at or
before the stage's clock."""


@pytest.mark.parametrize(
    ("doc", "opening_ns", "first_buy"),
    [
        (WEEKLY, WEEK_NS, date(2024, 3, 18)),
        (WEEKLY_WARMED, WEEK_NS, date(2024, 3, 18)),
        (WEEKLY, WEEK_BEFORE_NS, date(2024, 3, 13)),
    ],
    ids=["that-week", "that-week-warmed", "a-week-before"],
)
def test_a_week_stopped_on_tuesday_and_restarted_on_wednesday_enters_nothing_until_monday(
    doc: dict[str, Any], opening_ns: int, first_buy: date, ws: Workspace, store: StateStore
) -> None:
    """Every window restarts flat at the version's capital; the week its target had stopped
    carries across. The version's newest window ended at Tuesday March 12's close with its week
    stopped there: restarted after that close, the sleeve's entry is dropped at every bar from
    Wednesday through Sunday and goes at Monday March 18's, the next week's first, and the
    window lists the stop it resumed under as the window before struck it, with the five
    entries it dropped. Warmed on Monday's and Tuesday's sessions again, it drops the same five:
    the week is open over the warmup, which is read for nothing and counts nothing, as any
    warmup's orders are dropped unsent and uncounted. A stopped week the restart is already past
    is let go — the version's newest window stopped the week before at its Tuesday's close, and
    a stage-mate has since moved the clock to March 12's: it buys at Wednesday's bar."""
    deployable(ws, store, "weekly", sleeve=RETRYING, doc=doc)
    first = deploy(ws, store, "paper").results[0]
    ended_tuesday(store, first, pnl=0.1 * first.capital, stopped=True, opening_ns=opening_ns)

    run = deploy(ws, store, "paper").results[0].run

    assert bought_on(run)[0] == first_buy
    record = run.period_stops
    assert record is not None and record.openings[0] == WEEK_NS
    if opening_ns == WEEK_NS:
        (resumed, *_) = record.stops
        assert (resumed.opening_ns, resumed.hit_ns, resumed.anchor) == (
            WEEK_NS,
            TUESDAY_CLOSE_NS,
            first.capital,
        )
        assert (resumed.dropped, resumed.fills) == (5, [])
        assert all(stop.opening_ns >= NEXT_WEEK_NS for stop in record.stops[1:])
    else:
        assert all(stop.opening_ns != WEEK_NS for stop in record.stops)


LIMIT_ON_START = b'''
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Price, Quantity

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Rests a buy of 1,000 at 12.00, above every price of the saw-tooth, from on_start."""

    config_cls = Config

    def on_start(self) -> None:
        (name,) = self.universe
        order = self.order_factory.limit(
            name, OrderSide.BUY, Quantity.from_int(1_000), Price.from_str("12.00")
        )
        self.submit_order(order)

    def on_bar(self, bar) -> None:
        pass
'''

ENTRY_ON_START = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Asks for an entry of 1,000 limited at 12.00 from on_start."""

    config_cls = Config

    def on_start(self) -> None:
        (name,) = self.universe
        self.submit_entry(name, "BUY", qty=1_000, price=12.0)

    def on_bar(self, bar) -> None:
        pass
'''

KEEPS_ASKING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Asks for an entry of 1,000 limited at 12.00 from on_start, and for 100 more at every
    bar."""

    config_cls = Config

    def on_start(self) -> None:
        (name,) = self.universe
        self.submit_entry(name, "BUY", qty=1_000, price=12.0)

    def on_bar(self, bar) -> None:
        self.submit_entry(bar.bar_type.instrument_id, "BUY", qty=100, price=12.0)
'''


@pytest.mark.parametrize(
    ("sleeve", "ends_ns", "first_buy", "dropped"),
    [
        (LIMIT_ON_START, TUESDAY_CLOSE_NS, None, 1),
        (ENTRY_ON_START, TUESDAY_CLOSE_NS, None, 1),
        (KEEPS_ASKING, TUESDAY_CLOSE_NS, (date(2024, 3, 18), 100.0), 6),
        (KEEPS_ASKING, SUNDAY_CLOSE_NS, (date(2024, 3, 18), 100.0), 1),
    ],
    ids=["built-by-hand", "submit-entry", "and-at-every-bar", "resumed-on-sunday"],
)
def test_what_a_restart_sends_before_its_first_point_is_held_to_the_week_it_resumes_in(
    sleeve: bytes,
    ends_ns: int,
    first_buy: tuple[date, float] | None,
    dropped: int,
    ws: Workspace,
    store: StateStore,
) -> None:
    """A stopped week is in force from the restarted window's first instant, not from its first
    point. An entry the sleeve sends from on_start — built by hand or asked of submit_entry,
    with no warmup to drop it — is dropped under the stop it resumed, and never rests at the
    venue to fill at Wednesday's bar inside the stopped week; the stop lifts at Monday March
    18's bar, where the sleeve is flat, and its entries go. Restarted at Sunday's close, the
    last point of the stopped week, the window resumes in it though its first point is
    Monday's: the on_start entry is dropped there, the week is the window's first, and
    Monday's entry goes."""
    deployable(ws, store, "weekly", sleeve=sleeve, doc=WEEKLY)
    first = deploy(ws, store, "paper").results[0]
    assert buys_of(first.run)[0] == (date(2024, 3, 1), 1_000.0), "a first deploy fills it"
    ended_tuesday(
        store, first, pnl=0.1 * first.capital, stopped=True, ends_ns=ends_ns, clock_ns=ends_ns
    )

    run = deploy(ws, store, "paper").results[0].run

    buys = buys_of(run)
    assert (buys[0] if buys else None) == first_buy
    assert all(qty == 100.0 for _, qty in buys), "the 1,000 sent from on_start never filled"
    assert all(fill.ts_ns >= NEXT_WEEK_NS for fill in run.fills)
    record = run.period_stops
    assert record is not None and record.openings[:2] == [WEEK_NS, NEXT_WEEK_NS]
    (resumed, *later) = record.stops
    assert (resumed.hit_ns, resumed.dropped, resumed.fills) == (TUESDAY_CLOSE_NS, dropped, [])
    assert all(stop.opening_ns >= NEXT_WEEK_NS for stop in later)


def buys_of(run: Any) -> list[tuple[date, float]]:
    """The UTC day and the quantity of each of a window's buys, in order."""
    from kanso.criteria.run import day_of

    return [(day_of(fill.ts_ns), fill.qty) for fill in run.fills if fill.side == "BUY"]


@pytest.mark.parametrize(
    ("made_bp", "stops", "share"),
    [(600.0, True, 1.0), (600.0, True, 2.0), (0.0, False, 1.0)],
    ids=["600-made", "600-made-at-twice-the-share", "nothing-made"],
)
def test_a_week_that_had_made_600_bp_stops_at_400_more(
    made_bp: float, stops: bool, share: float, ws: Workspace, store: StateStore
) -> None:
    """Restarted after Tuesday's close in a week that had made 600 bp of a 1,000 bp target,
    the window is read against the capital less those 600: it buys 3,600 at Wednesday's 10.00
    and stops at Thursday's 10.50 close, 450 bp up, with 400 left. The basis points carry
    across a share that moved: 600 bp made at twice the capital is 600 bp here. Having made
    nothing, the window's 450 on Thursday and 900 on Friday stop nothing that week."""
    deployable(ws, store, "weekly", sleeve=RETRYING, doc=WEEKLY)
    first = deploy(ws, store, "paper").results[0]
    capital = first.capital
    ended_tuesday(store, first, pnl=made_bp / 10_000 * share * capital, capital=share * capital)

    run = deploy(ws, store, "paper").results[0].run

    assert bought_on(run)[0] == date(2024, 3, 13)
    record = run.period_stops
    assert record is not None
    week = [stop for stop in record.stops if stop.opening_ns == WEEK_NS]
    if not stops:
        assert week == []
        return
    (stop,) = week
    thursday = int(datetime(2024, 3, 14, 16, 0, 1, tzinfo=UTC).timestamp()) * NS
    assert stop.hit_ns == thursday
    assert stop.anchor == pytest.approx(capital - 0.06 * capital)
    assert stop.threshold == pytest.approx(0.1 * capital)
    assert 0.04 * capital <= stop.equity - capital < 0.1 * capital, "400 bp more, not 1,000"


RAISES_THURSDAY = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Trades nothing, and raises at the bar of Thursday March 14, 2024."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        if 1710374400000000000 <= bar.ts_event < 1710460800000000000:
            raise RuntimeError("the stage-mate asked the impossible")
'''
"""A stage-mate whose id sorts ahead of `weekly`, so it is handed each bar first and the node
halts at Thursday's before `weekly` is handed it."""


def test_a_window_a_halt_ended_leaves_its_week_where_the_halt_left_the_sleeve(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halted node flattens nothing. Restarted after Tuesday's close in a week that had made
    600 bp, the sleeve buys 3,600 at Wednesday's bar, 2,500 at 10.00 and 1,100 at 10.01, and the
    stage-mate raises at Thursday's, before the sleeve is handed it: the window ends holding the
    3,600, and the week open there is read where the halt left the sleeve, every share marked at
    Wednesday's 10.00, the last price it was handed — the 1,100 too, struck after it — about
    595 bp made after the entry's costs. The window's own marks reach Thursday's 10.50, which
    takes the week past its 1,000 bp; the seed never reads it, and the next restart, after
    Thursday's bar, is handed the week at what the sleeve read, resumes it and buys at Friday's
    (`docs/backlog.md` row 283)."""
    from kanso.criteria.run import day_of
    from kanso.nautilus import cross_section

    deployable(ws, store, "weekly", sleeve=RETRYING, doc=WEEKLY)
    first = deploy(ws, store, "paper").results[0]
    capital = first.capital
    ended_tuesday(store, first, pnl=0.06 * capital)
    deployable(ws, store, "aboomer", sleeve=RAISES_THURSDAY, doc=document(id="aboomer"))

    made = deploy(ws, store, "paper")

    assert made.halted is not None
    (halted,) = [one for one in made.results if one.strategy_id == "weekly"]
    assert {(fill.side, fill.px) for fill in halted.run.fills} == {("BUY", 10.0), ("BUY", 10.01)}
    assert halted.positions == (("DEMO.XNAS", 3_600.0, 11.0, 1.0),), "nothing was flattened"
    record = halted.run.period_stops
    assert record is not None and record.last is not None
    assert (record.last.opening_ns, record.last.stopped) == (WEEK_NS, False)
    assert record.last.pnl / capital * 10_000 == pytest.approx(595, abs=1)
    costs = sum(fill.cost for fill in halted.run.fills)
    marked = 0.06 * capital - costs + sum(f.qty * (10.0 - f.px) for f in halted.run.fills)
    assert record.last.pnl == pytest.approx(marked, abs=1e-6), "the 10.01 fill marked at 10.00"
    ends = [day_of(end) for end in halted.run.period_ends_ns]
    thursday = halted.run.equity[ends.index(date(2024, 3, 14))] - record.last.anchor
    assert thursday / capital * 10_000 == pytest.approx(1_045, abs=1), "past the target"
    seed = records.target_seed(
        records.stage_results(store, strategy_id="weekly", stage="paper"), capital
    )
    assert seed is not None and seed.last == record.last
    handed: list[object] = []
    harness = cross_section.target

    def handing(
        strategy: object, target: object, struck: object = None, week: object = None
    ) -> None:
        handed.append(week)
        harness(strategy, target, struck, week)

    monkeypatch.setattr(cross_section, "target", handing)

    (resumed,) = [one for one in deploy(ws, store, "paper").results if one.strategy_id == "weekly"]

    assert handed == [seed], "the week resumed at what the sleeve read, not let go"
    assert bought_on(resumed.run)[0] == date(2024, 3, 15)


WEEKLY_OPEN = document(
    id="weekly",
    risk_limits={"max_position_pct": 100, "max_drawdown_pct": 40, "max_leverage": 1},
    period_target={
        "period": "week",
        "opens": "Mon 04:00",
        "tz": "America/New_York",
        "target_bp": 1000,
        "of": "period_open",
    },
)
"""`WEEKLY`, its tenth read of the equity each week opens at rather than of the capital."""


def test_a_resumed_week_is_held_to_the_threshold_it_opened_with(
    ws: Workspace, store: StateStore
) -> None:
    """Under `of: period_open` a week is held to a tenth of the equity it opened at, struck at
    its opening. Restarted in a week that opened at 1.2 times the capital, so held to 1,200 bp
    of it, and had made 600, the window is held to that week's own threshold and not to a tenth
    of the capital it restarts at: Thursday's 450 leave the week 1,050 up, short of it, and
    Friday's 900 take it to 1,500 and stop it."""
    deployable(ws, store, "weekly", sleeve=RETRYING, doc=WEEKLY_OPEN)
    first = deploy(ws, store, "paper").results[0]
    capital = first.capital
    ended_tuesday(store, first, pnl=0.06 * capital, anchor=1.2 * capital, threshold=0.12 * capital)

    run = deploy(ws, store, "paper").results[0].run

    assert bought_on(run)[0] == date(2024, 3, 13)
    assert run.period_stops is not None
    (stop,) = [stop for stop in run.period_stops.stops if stop.opening_ns == WEEK_NS]
    friday = int(datetime(2024, 3, 15, 16, 0, 1, tzinfo=UTC).timestamp()) * NS
    assert stop.hit_ns == friday
    assert (stop.anchor, stop.threshold) == pytest.approx((0.94 * capital, 0.12 * capital))


NEXT_BEST = RETRYING + b"\n# the next best snapshot the loop kept\n"
"""The same sleeve with other bytes: what a research stall certifies, which composes the next
version."""


@pytest.mark.parametrize(
    ("sleeve", "version"), [(RETRYING, 1), (NEXT_BEST, 2)], ids=["same-bytes", "new-bytes"]
)
def test_a_version_deployed_by_its_certificate_resumes_a_stopped_week(
    sleeve: bytes, version: int, ws: Workspace, store: StateStore
) -> None:
    """A passing certificate composes its version and deploys the paper stage with no one
    asked. The same bytes certified again return that version and deploy the stage again,
    restarting it; other bytes — what a research stall certifies — compose the next version,
    which replaces the one on paper. The week is the strategy's, so either resumes the week
    its newest window stopped at Tuesday's close: it enters nothing until Monday."""
    from kanso.certify import certificate
    from kanso.portfolio.lifecycle import on_certified
    from tests.portfolio.test_lifecycle import a_certificate

    file = deployable(ws, store, "weekly", sleeve=RETRYING, doc=WEEKLY)
    made = a_certificate(ws, sha=file.versions[0].sleeve.strategy_sha).model_copy(
        update={"hyp_id": "weekly"}
    )
    certificate.write(ws, store, made, RETRYING)
    adopted = on_certified(ws, store, made)
    assert adopted.deployment is not None and adopted.state == "paper"
    first = adopted.deployment.results[0]
    ended_tuesday(store, first, pnl=0.1 * first.capital, stopped=True)
    best = made
    if sleeve != RETRYING:
        # A second later than the first, so composition reads it as the newest by its stamp
        # and never by how far the wall clock moved between the two.
        best = a_certificate(ws, sha=store.put_blob(sleeve)).model_copy(
            update={"hyp_id": "weekly", "created_at": made.created_at + timedelta(seconds=1)}
        )
        certificate.write(ws, store, best, sleeve)

    again = on_certified(ws, store, best)

    assert again.version is not None and again.version.version == version
    assert again.deployment is not None
    (window,) = again.deployment.results
    assert (window.version, window.capital) == (version, first.capital)
    assert bought_on(window.run)[0] == date(2024, 3, 18)
    assert window.run.period_stops is not None
    assert window.run.period_stops.stops[0].hit_ns == TUESDAY_CLOSE_NS
    assert window.run.period_stops.stops[0].dropped == 5
