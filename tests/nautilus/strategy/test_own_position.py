"""Two sleeves on one account in one name: each reads, classifies and exits its own position.

Under one NETTING account the portfolio's net position in a name is every strategy's added
up, so a sleeve that read it would exit its stage-mate's shares with its own — two copies
of one reverting sleeve on one paper stage each ended the window short 500 that way, where
alone each ends flat. The harness reads the position the cache keeps for the sleeve's own
strategy id instead (`kanso.nautilus.facts` checks the engine keeps one per instrument and
strategy). Both sleeves run in one real engine here, as versions on one stage do.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.objects import Money, Quantity

from kanso.nautilus import actions
from kanso.nautilus.hooks import HookContext
from kanso.nautilus.node import _flatten
from kanso.nautilus.strategy import EXIT_ORDER, KansoStrategy

from .conftest import DEEP, DEMO, HEDGE, VENUE, equity, flat
from .test_sizing import DEMO_SHARES, sized, two_names
from .test_sleeve import FREE, config


@dataclass(frozen=True)
class Plan:
    """One sleeve's trade: `qty` of DEMO on `side` at bar `enter`, exited at bar `leave` with
    `asked` (the whole position when `None`); `again` enters `qty` more at that bar, and
    `rest` rests an exit of the whole position at a price no bar reaches at that bar."""

    side: str
    qty: int
    enter: int
    leave: int
    asked: int | None = None
    again: int | None = None
    rest: int | None = None


class Twin(KansoStrategy):
    """Trades its plan, and records what it read and how its exit was classified just
    before the exit."""

    plan: Plan
    flatten_at: int | None = None
    flatten_with: Sequence[KansoStrategy] = ()

    def on_start(self) -> None:
        self.bars = 0
        self.held_before_exit: float | None = None
        self.exit_kind: str | None = None
        self.exit: object = "unset"
        self.books: list[dict[str, float]] = []

    def before_entry(self, ctx: HookContext) -> bool:
        """Records the book handed to every attached filter with this entry."""
        self.books.append(dict(ctx.book))
        return super().before_entry(ctx)

    def on_bar(self, bar_: object) -> None:
        if str(bar_.bar_type.instrument_id) != "DEMO.XNAS":  # type: ignore[attr-defined]
            return
        self.bars += 1
        plan = self.plan
        if self.bars == plan.enter:
            if self.sized:
                self.submit_entry(DEMO, plan.side)
            else:
                self.submit_entry(DEMO, plan.side, qty=plan.qty)
        if self.bars == plan.again:
            self.submit_entry(DEMO, plan.side, qty=plan.qty)
        if self.bars == plan.rest:
            self.submit_exit(DEMO, price=20.0 if plan.side == "BUY" else 1.0)
        if self.bars == self.flatten_at:
            _flatten(self.flatten_with)
        if self.bars == plan.leave:
            self.held_before_exit = self.held(DEMO)
            closing = OrderSide.SELL if self.held_before_exit > 0 else OrderSide.BUY
            probe = self.order_factory.market(
                DEMO, closing, Quantity.from_int(int(abs(self.held_before_exit)))
            )
            self.exit_kind = self.classify(probe)
            self.exit = (
                self.submit_exit(DEMO)
                if plan.asked is None
                else self.submit_exit(DEMO, qty=plan.asked)
            )


@dataclass(frozen=True)
class Ran:
    first: Twin
    second: Twin
    open_at_end: dict[str, float]
    """Every position still open at the end, by id, with its signed quantity."""


def run_two(
    first: Plan,
    second: Plan,
    *,
    sizing: bool = False,
    latency_ms: float = 0.0,
    flatten: tuple[int, tuple[int, int]] | None = None,
) -> Ran:
    """Both sleeves on one NETTING venue — kanso's, corporate-action module and all —
    trading DEMO over a flat, deep tape, in one engine, under `latency_ms` when it is stated.

    With `flatten` = (bar, order), the second sleeve hands both to the node's flatten at that
    bar, in that order of the two (0 the first, 1 the second), after its own plan's step."""
    costs = {**FREE["costs"], "latency_ms": latency_ms} if latency_ms else FREE["costs"]
    latency = LatencyModel(base_latency_nanos=int(latency_ms * 1_000_000)) if latency_ms else None
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        engine.add_venue(
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(200_000, USD)],
            modules=actions.modules(VENUE.value),
            latency_model=latency,
        )
        engine.add_instrument(equity(DEMO))
        engine.add_instrument(equity(HEDGE))
        engine.add_data(two_names() if sizing else flat(DEMO, volume=DEEP))
        twins = []
        for tag, plan in (("001", first), ("002", second)):
            venue_model = {"costs": costs}
            twin = Twin(
                sized(order_id_tag=tag, venue_model=venue_model)
                if sizing
                else config(order_id_tag=tag, venue_model=venue_model)
            )
            twin.plan = plan
            engine.add_strategy(twin)
            twins.append(twin)
        if flatten is not None:
            twins[1].flatten_at = flatten[0]
            twins[1].flatten_with = [twins[i] for i in flatten[1]]
        engine.run()
        open_at_end = {
            str(position.id): float(position.signed_qty)
            for position in engine.cache.positions_open()
        }
        return Ran(first=twins[0], second=twins[1], open_at_end=open_at_end)
    finally:
        engine.dispose()


def _signed(plan: Plan) -> float:
    return float(plan.qty if plan.side == "BUY" else -plan.qty)


def _exit_qty(twin: Twin) -> float:
    (_, exit_intent) = twin.intents
    return exit_intent.qty


plans = st.builds(
    Plan,
    side=st.sampled_from(("BUY", "SELL")),
    qty=st.integers(min_value=1, max_value=1_500),
    enter=st.integers(min_value=2, max_value=6),
    leave=st.integers(min_value=8, max_value=16),
)


@settings(max_examples=25, deadline=None)
@given(first=plans, second=plans)
def test_each_sleeve_exits_its_own_position_and_nothing_of_its_stage_mate_s(
    first: Plan, second: Plan
) -> None:
    """Whatever the sides, sizes and order of the two trades, each sleeve reads its own
    position, classifies its own close as an exit, closes exactly what it holds, and the
    account ends flat."""
    ran = run_two(first, second)

    for twin, plan in ((ran.first, first), (ran.second, second)):
        assert twin.held_before_exit == _signed(plan), "its own position, not the account's"
        assert twin.exit_kind == EXIT_ORDER
        assert [intent.side for intent in twin.intents] == [
            plan.side,
            "SELL" if plan.side == "BUY" else "BUY",
        ]
        assert _exit_qty(twin) == plan.qty
    assert ran.open_at_end == {}, "both ended flat"


def test_an_exit_never_goes_past_this_sleeve_s_flat_however_much_the_mate_holds() -> None:
    """Asked for far more than it holds beside a mate holding more on the same side, a
    sleeve's exit is cut to its own position: past it, it would be selling the mate's."""
    ran = run_two(
        Plan("BUY", 10, enter=3, leave=6, asked=10_000),
        Plan("BUY", 500, enter=2, leave=12),
    )

    assert _exit_qty(ran.first) == 10.0
    assert _exit_qty(ran.second) == 500.0
    assert ran.open_at_end == {}


def test_the_mate_s_position_is_left_open_while_this_sleeve_closes_its_own() -> None:
    """The first sleeve exits while the second still holds; the second never exits, and its
    position is the only one left at the end, whole."""
    ran = run_two(
        Plan("BUY", 100, enter=2, leave=8),
        Plan("BUY", 300, enter=3, leave=99),
    )

    assert _exit_qty(ran.first) == 100.0
    assert ran.open_at_end == {"DEMO.XNAS-Twin-002": 300.0}


def test_against_a_larger_opposite_mate_a_close_is_still_an_exit() -> None:
    """The account is net short when the long sleeve sells: by the account's position the
    sale would grow a short — an entry, sized and capped as one — and by its own it closes
    a long."""
    ran = run_two(
        Plan("BUY", 100, enter=2, leave=8),
        Plan("SELL", 400, enter=3, leave=12),
    )

    assert ran.first.exit_kind == EXIT_ORDER
    assert _exit_qty(ran.first) == 100.0
    assert ran.open_at_end == {}


def test_under_a_sizing_rule_each_sleeve_exits_its_whole_budget_and_no_more() -> None:
    """`full_book` exits are the sleeve's whole position: with two full books in one name
    each closes its own budget's shares, where the account holds twice that."""
    ran = run_two(
        Plan("BUY", 0, enter=3, leave=6),
        Plan("BUY", 0, enter=3, leave=9),
        sizing=True,
    )

    for twin in (ran.first, ran.second):
        assert twin.held_before_exit == DEMO_SHARES
        assert [(i.side, i.qty) for i in twin.intents] == [
            ("BUY", DEMO_SHARES),
            ("SELL", DEMO_SHARES),
        ]
    assert ran.open_at_end == {}


@pytest.mark.parametrize("order", [(0, 1), (1, 0)], ids=["first-first", "second-first"])
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (Plan("BUY", 10, enter=2, leave=99), Plan("BUY", 500, enter=3, leave=99)),
        (Plan("SELL", 5, enter=2, leave=99), Plan("BUY", 100, enter=3, leave=99)),
        (Plan("BUY", 500, enter=2, leave=99), Plan("BUY", 10, enter=3, leave=99)),
    ],
    ids=["mate-smaller", "mate-opposite", "mate-larger"],
)
@pytest.mark.parametrize("latency_ms", [0.0, 20.0])
def test_the_node_s_flatten_closes_every_sleeve_s_position_whatever_order_it_is_handed(
    first: Plan, second: Plan, order: tuple[int, int], latency_ms: float
) -> None:
    """The flatten's close is reduce-only, and the venue holds one to the first open
    position in the name by id, whoever's (`kanso.nautilus.facts`): a flatten that closed
    in the order the strategies came — the second first — sold only the first's 10 of the
    second's 500, and only 5 of its 100 beside a first short 5. Closed in the cache's order,
    both end flat whichever comes first."""
    ran = run_two(first, second, latency_ms=latency_ms, flatten=(10, order))

    assert ran.open_at_end == {}
