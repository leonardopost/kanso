"""What a stage node does with a book, with a construct attached, and with a strategy that fails."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kanso.nautilus.cross_section import is_marker
from kanso.portfolio import clients, deploy, files, records, set_state, show
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.portfolio.conftest import deployable, reconfigure
from tests.replay.conftest import (
    BLOCKING_FILTER,
    FLAT,
    RAISING,
    REVERTING,
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
    assert [name for name, _, _ in realised.positions] == ["DEMO.XNAS"]
    assert realised.positions[0][1] > 0, "the window closed holding a long"
    assert realised.gross == pytest.approx(abs(realised.net))
    assert len(realised.run.trades) == 1, "the flatten closed the position it was holding"


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

    deployable(ws, store, "warm", sleeve=REVERTING, doc=WARMED)

    made = deploy(ws, store, "paper")

    assert made.session is not None
    stream = record.stream_of(ws, made.session.session_id)
    assert made.session.released == len(stream) == 31, "March, and not a day of February"
    assert stream[0].ts_event == bar_close_ns(1) < stream[0].ts_init
    assert made.session.clock_ns == stream[-1].ts_init
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

    deployable(ws, store, "warm", sleeve=REVERTING, doc=WARMED)
    deploy(ws, store, "paper")
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(MARCH_15_CLOSE_NS),))

    made = deploy(ws, store, "paper")

    assert made.session is not None
    stream = record.stream_of(ws, made.session.session_id)
    assert made.session.released == len(stream) == 16, "March 16 through 31"
    assert stream[0].ts_init > MARCH_15_CLOSE_NS
    assert made.session.clock_ns == stream[-1].ts_init
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
