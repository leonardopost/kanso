"""Parity: the two code paths over one range, and what a divergence between them looks like."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from kanso import replay
from kanso.nautilus import actions
from kanso.replay import record
from kanso.replay.parity import RELEASED, STREAM, Intent, Parity, compare, of_sessions
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import (
    BLOCKING_FILTER,
    FLAT,
    HOLDING,
    INSTRUMENT,
    RAISING,
    bars,
    carded,
    composed,
    document,
    hypothesis,
    instrument,
    request_for,
)
from tests.replay.test_session import (
    MS,
    OTHER_ID,
    STALE,
    STALE_PRINTS,
    T0,
    both,
    fills_of,
    posted,
    quote,
    trade,
)


def intent(**changes: object) -> Intent:
    """One order intent, with these fields replaced."""
    base = {
        "ts_event": 1_000,
        "instrument": "DEMO.XNAS",
        "side": "BUY",
        "qty": 10.0,
        "order_type": "MARKET",
        "price": None,
    }
    return Intent(**{**base, **changes})  # type: ignore[arg-type]


# --- the claim ----------------------------------------------------------------


def test_the_two_code_paths_produce_the_same_intents(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The live path and the research path submit the same orders over the same data."""
    result = replay.parity(ws, store, hyp=carded_hyp)

    assert result.identical, result.divergence and result.divergence.render()
    assert result.compared > 0
    assert result.max_ts_delta_ns == 0


def test_parity_holds_for_a_composed_version(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A version is what a stage runs, so it is the thing parity has to hold for."""
    composed(ws, store, carded_hyp)
    result = replay.parity(ws, store, strategy=carded_hyp)

    assert result.identical, result.divergence and result.divergence.render()
    assert result.compared > 0


def test_parity_holds_for_an_attached_construct_on_its_host(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A construct is replayed on the host it was researched against, on both paths."""
    composed(ws, store, carded_hyp)
    hyp_id = carded(
        ws,
        store,
        doc=document(
            id="demo_filter",
            construct={"id": "filter", "host": carded_hyp, "params": {"scope": "time"}},
            objective={"id": "marginal_wf_sharpe", "params": {"min_delta": 0.0, "k_se": 0.5}},
        ),
        strategy=BLOCKING_FILTER,
        host_version=1,
    )

    result = replay.parity(ws, store, hyp=hyp_id)

    assert result.identical, result.divergence and result.divergence.render()


def test_parity_holds_with_a_construct_attached(ws: Workspace, store: StateStore) -> None:
    """An attached construct changes what the host does on both paths or on neither."""
    host = carded(ws, store, strategy=FLAT)
    composed(
        ws,
        store,
        host,
        attached=(("demo_filter", "filter", BLOCKING_FILTER, {"allow": True}),),
    )
    result = replay.parity(ws, store, strategy=host)

    assert result.identical


def test_parity_names_both_sessions_it_compared(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The result points at the two sessions, so a divergence can be inspected after."""
    result = replay.parity(ws, store, hyp=carded_hyp)

    assert replay.show(ws, result.node).mode == "node"
    assert replay.show(ws, result.engine).mode == "engine"
    assert replay.show(ws, result.engine).range == replay.show(ws, result.node).range


def test_a_silent_target_is_identical_and_says_so(ws: Workspace, store: StateStore) -> None:
    """Two paths that submit nothing agree, and the count is how a reader tells."""
    hyp_id = carded(ws, store, strategy=FLAT)
    result = replay.parity(ws, store, hyp=hyp_id)

    assert result.identical
    assert result.compared == 0


def test_a_strategy_that_raises_fails_the_engine_path(ws: Workspace, store: StateStore) -> None:
    """The node path reports a failing strategy; the research path raises it, so parity does."""
    carded(ws, store, strategy=RAISING, doc=document(id="demo_raise"))
    with pytest.raises(RuntimeError, match="the replay asked for the impossible"):
        replay.parity(ws, store, hyp="demo_raise")


def test_the_payload_is_one_json_object(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A parity result renders as a flat object a command can print."""
    payload = replay.parity(ws, store, hyp=carded_hyp).payload()

    assert payload["identical"] is True
    assert payload["divergence"] is None
    assert payload["ts_ns"] == 0


def test_both_paths_were_released_the_same_stream_and_the_payload_says_which(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The count and the digest each session recorded travel into the evidence, so a
    certificate shows what both paths were fed even once the sessions are gone."""
    result = replay.parity(ws, store, hyp=carded_hyp)
    payload = result.payload()
    node = replay.show(ws, result.node)

    assert result.fed is None
    assert payload["node_released"] == payload["engine_released"] == node.released > 0
    assert payload["node_stream"] == payload["engine_stream"] == node.stream_sha256
    assert node.stream_sha256 is not None


def test_two_sessions_released_different_points_diverge_on_the_stream(ws: Workspace) -> None:
    """Read back from disk: the same orders over different data are not agreement."""
    from tests.replay.test_record import session

    days = [record.Point.of(bar) for bar in bars((date(2024, 3, 1), date(2024, 3, 2)))]
    orders = [intent()]
    node = record.write(ws, session(mode="node", released=2), days, orders)
    engine = record.write(ws, session(mode="engine", released=2), days[::-1], orders)

    result = of_sessions(ws, node, engine)

    assert not result.identical
    assert result.compared == 1
    assert result.divergence == result.fed
    assert result.divergence is not None
    assert result.divergence.field == STREAM
    assert result.divergence.render().startswith("stream: stream_sha256 is ")


# --- the comparison itself ----------------------------------------------------


def test_identical_sequences_have_no_divergence() -> None:
    """The same orders in the same order agree."""
    divergence, widest = compare(
        [intent(), intent(ts_event=2_000)], [intent(), intent(ts_event=2_000)]
    )

    assert divergence is None
    assert widest == 0


def test_a_quantity_that_differs_is_a_divergence() -> None:
    """A different size is a different decision, whatever the tolerance."""
    divergence, _ = compare([intent(qty=10.0)], [intent(qty=11.0)], ts_ns=10**9)

    assert divergence is not None
    assert divergence.field == "qty"
    assert "qty is 10.0 on the node path" in divergence.render()


def test_an_instant_inside_the_tolerance_agrees() -> None:
    """The instant is the one field a tolerance is meaningful for."""
    divergence, widest = compare([intent(ts_event=1_000)], [intent(ts_event=1_050)], ts_ns=100)

    assert divergence is None
    assert widest == 50


def test_an_instant_outside_the_tolerance_diverges() -> None:
    """Past the tolerance the two paths acted at different moments."""
    divergence, widest = compare([intent(ts_event=1_000)], [intent(ts_event=1_500)], ts_ns=100)

    assert divergence is not None
    assert divergence.field == "ts_event"
    assert widest == 500


def test_a_missing_intent_diverges_at_its_index() -> None:
    """A path that stopped submitting diverges where it stopped."""
    divergence, _ = compare([intent(), intent()], [intent()])

    assert divergence is not None
    assert divergence.index == 1
    assert "only the node path submitted one" in divergence.render()


def test_a_surplus_intent_names_the_other_path() -> None:
    """The report names whichever path had the extra order."""
    divergence, _ = compare([intent()], [intent(), intent()])

    assert divergence is not None
    assert "only the engine path submitted one" in divergence.render()


def test_a_price_that_differs_is_a_divergence() -> None:
    """A limit at another price is another order."""
    divergence, _ = compare(
        [intent(order_type="LIMIT", price=10.0)], [intent(order_type="LIMIT", price=10.5)]
    )

    assert divergence is not None
    assert divergence.field == "price"


def fed(node: tuple[int, str | None], engine: tuple[int, str | None]) -> Parity:
    """The same single order on both paths, released what each of these says."""
    return Parity(
        node="n",
        engine="e",
        ts_ns=0,
        node_orders=(intent(),),
        engine_orders=(intent(),),
        max_ts_delta_ns=0,
        node_released=node[0],
        engine_released=engine[0],
        node_stream=node[1],
        engine_stream=engine[1],
    ).at(0)


def test_a_path_released_fewer_points_diverges_whatever_it_submitted() -> None:
    """A count that differs is the first divergence, and no tolerance reaches it."""
    result = fed((9, "a" * 64), (10, "a" * 64))

    assert not result.identical
    assert not result.at(10**9).identical
    assert result.divergence is not None
    assert result.divergence.index is None
    assert result.divergence.field == RELEASED
    assert (
        result.divergence.render()
        == "stream: released is 9 on the node path and 10 on the engine path"
    )


def test_the_same_count_of_other_points_diverges_on_the_digest() -> None:
    result = fed((10, "a" * 64), (10, "b" * 64))

    assert result.divergence is not None
    assert result.divergence.field == STREAM
    assert result.payload()["divergence"] == result.divergence.render()


def test_a_session_without_a_digest_is_compared_on_its_count_alone() -> None:
    """A session written before the digest has none; its count is all there is to compare."""
    assert fed((10, None), (10, "a" * 64)).identical
    assert fed((10, None), (10, None)).identical
    assert not fed((10, None), (11, None)).identical


def test_a_verdict_can_be_asked_again_at_another_tolerance() -> None:
    """The sequences travel with the verdict, so a reader with its own tolerance re-asks."""
    result = Parity(
        node="n",
        engine="e",
        ts_ns=0,
        node_orders=(intent(ts_event=1_000),),
        engine_orders=(intent(ts_event=1_100),),
        max_ts_delta_ns=100,
        divergence=None,
    ).at(0)

    assert not result.at(0).identical
    assert result.at(200).identical
    assert result.at(200).max_ts_delta_ns == 100
    assert result.compared == 1


# --- what a divergence does and does not claim --------------------------------


def test_a_divergence_is_reported_as_an_index_a_field_and_two_values() -> None:
    """And nothing else: the one cause this module used to name no longer exists.

    While the node ran on the engine's own convenience venue, an order larger than one
    book state was under-filled there and not in a backtest, so a lower quantity on the
    node path carried that explanation. kanso builds its own exchange now and the two
    paths fill it identically, so an explanation here would send a reader to check the
    one thing that has been repaired.
    """
    divergence, _ = compare([intent(qty=739.0)], [intent(qty=976.0)])

    assert divergence is not None
    assert (
        divergence.render()
        == "intent 0: qty is 739.0 on the node path and 976.0 on the engine path"
    )
    assert not hasattr(divergence, "likely_cause")


def test_the_payload_carries_no_explanation_for_a_divergence() -> None:
    parted = Parity(
        node="n",
        engine="e",
        ts_ns=0,
        node_orders=(intent(qty=739.0),),
        engine_orders=(intent(qty=976.0),),
        max_ts_delta_ns=0,
    ).at(0)

    assert "likely_cause" not in parted.payload()
    assert parted.payload()["divergence"] == parted.divergence.render()  # type: ignore[union-attr]


def test_parity_holds_across_a_split(ws_split: Workspace, store_split: StateStore) -> None:
    """A corporate action is applied by the venue, and both code paths run the same venue.

    That is why it is a simulation module rather than anything in the strategy: the module
    is loaded by `BacktestEngine.add_venue` on the research path and by
    `kanso.nautilus.sandbox` on the node path, and it acts one call before the exchange
    matches the point that carried the market past the ex-date. A quantity that changed on
    one path and not the other would diverge here at a tolerance of zero.
    """
    hyp_id = carded(ws_split, store_split, strategy=HOLDING)

    result = replay.parity(ws_split, store_split, hyp=hyp_id)

    assert result.identical, result.divergence and result.divergence.render()
    assert result.max_ts_delta_ns == 0
    assert [(order.side, order.qty) for order in result.node_orders] == [
        ("BUY", 1_005.0),
        ("SELL", 100.0),
    ]


def test_parity_is_identical_on_points_published_after_a_later_one() -> None:
    """A quote stamped before the venue's last update and published after it is applied by
    both venues, so the two paths submit and fill alike and parity holds at zero."""
    node, engine = posted(100, 9.9, STALE["quote"])

    divergence, widest = compare(
        [Intent.of(row) for row in node.intents], [Intent.of(row) for row in engine.intents]
    )

    assert divergence is None
    assert widest == 0
    assert len(node.intents) == 1
    assert node.run.fills == engine.run.fills
    assert node.run.fills


# --- where the two paths part: `docs/backlog.md` row 154 ---------------------------

PARTING = """Pinned for `docs/backlog.md` row 154. A fix that has the two venues match alike
once a command lands turns the fill assertions of every test below. The `compare` assertions
hold parity's own comparison, of intents: a fix that has parity compare fills as well leaves
them as they are — a session records no fills, so `replay.parity` has none to compare — and
brings its own failing test through `replay.parity`."""


def two_paths(
    source: bytes, points: list[object], *, names: tuple[str, ...] = (INSTRUMENT,)
) -> tuple[Any, Any]:
    """`source` on both paths over these quotes and prints of `names`, under `touch` and with
    no latency."""
    hyp = hypothesis(
        resolution="tick", horizon="1d", data_requirements=["quote", "trade"], universe=list(names)
    )
    request = request_for(hyp=hyp, source=source)
    model = dict(request.venue_model)
    model["costs"] = {**dict(model["costs"]), "limit_fill": "touch"}  # type: ignore[arg-type]
    groups = [
        group
        for group in (
            tuple(point for point in points if isinstance(point, QuoteTick)),
            tuple(point for point in points if isinstance(point, TradeTick)),
        )
        if group
    ]
    instruments = [instrument(name.split(".")[0]) for name in names]
    return both(replace(request, venue_model=model), instruments, groups)


def compared(node: Any, engine: Any) -> tuple[object, int]:
    """What parity says of the two runs' intents, at a tolerance of zero."""
    return compare(
        [Intent.of(row) for row in node.intents], [Intent.of(row) for row in engine.intents]
    )


FROM_THE_PRINT = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Rests a buy of 100 at 9.98 from the first print it is handed."""

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.sent = False

    def on_trade_tick(self, tick) -> None:
        if not self.sent:
            self.sent = True
            self.submit_entry(tick.instrument_id, "BUY", qty=100, price=9.98)
'''


def test_parity_misses_an_order_sent_from_a_print_s_handler_filling_against_it() -> None:
    """With no latency, a buy of 100 at 9.98 sent from the handler of a seller's print of 100
    at 9.96, under a quote of 9.99/10.01: the research engine lands the buy and matches it at
    once against the print, which stands as the book, and fills it as a maker; the node's
    venue lands it too and waits for the next point, a quote of 9.99/10.01 that does not
    reach it. Older than v0.14.1: every point here is stamped as it is published."""
    seller = TradeTick(
        InstrumentId.from_str(INSTRUMENT),
        Price(9.96, 2),
        Quantity.from_int(100),
        AggressorSide.SELLER,
        TradeId("S1"),
        T0 + 20 * MS,
        T0 + 20 * MS,
    )
    points = [quote(9.99, 10.01, 10, 10), seller, quote(9.99, 10.01, 40, 40)]

    node, engine = two_paths(FROM_THE_PRINT, points)

    assert fills_of(node) == []
    assert fills_of(engine) == [(20, 100.0, 9.98, True)]
    assert compared(node, engine) == (None, 0)


AT_THE_QUOTE = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Rests a buy of 445 at 9.90 from the first quote, sends a buy of one far under the
    market from the third, and from the fourth sells whatever it holds."""

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.seen = 0

    def on_quote_tick(self, tick) -> None:
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(tick.instrument_id, "BUY", qty=445, price=9.9)
        elif self.seen == 3:
            self.submit_entry(tick.instrument_id, "BUY", qty=1, price=1.0)
        elif self.seen == 4 and self.held(tick.instrument_id):
            self.submit_exit(tick.instrument_id)
'''

AT_THE_QUOTE_POINTS = [
    quote(9.99, 10.01, 10, 10),
    quote(9.95, 10.0, 20, 20),
    quote(9.85, 9.9, 15, 30, ask_size=100),
]
"""The resting buy's market, then a quote whose ask of 100 sits at its price, stamped at 15 ms,
before the venue's last update at 20 ms, and published at 30 ms."""


def test_parity_misses_a_quote_at_a_resting_price_credited_again_when_a_command_lands() -> None:
    """No print at all, and no latency. The quote at 30 ms shows 100 at the resting buy's
    price and fills 100 on both paths; the buy of one sent from its handler lands while that
    quote is the book, and the research engine, matching every resting order again, credits
    the same 100 a second time, where the node waits for the next point."""
    node, engine = two_paths(AT_THE_QUOTE, AT_THE_QUOTE_POINTS)

    assert fills_of(node) == [(30, 100.0, 9.9, True)]
    assert fills_of(engine) == [(30, 100.0, 9.9, True), (30, 100.0, 9.9, True)]
    assert compared(node, engine) == (None, 0)


def test_the_venue_module_parts_the_paths_intents_where_a_sleeve_acts_on_its_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same quotes and a fourth after them, from whose handler the sleeve sells what it
    holds: 100 on the node and 200 on the research path, so the intents part and parity
    fails — as `parity_replay` would fail the certification. Without the module the venue
    skips the stale quote, nothing fills, nothing is sold, and parity holds: v0.14.1 brings
    the quote into the parting, and with it a certification that passed before."""
    points = [*AT_THE_QUOTE_POINTS, quote(9.95, 10.0, 40, 40)]

    node, engine = two_paths(AT_THE_QUOTE, points)
    divergence, _ = compared(node, engine)

    assert divergence is not None
    assert (divergence.index, divergence.field, divergence.node, divergence.engine) == (
        2,
        "qty",
        100.0,
        200.0,
    )

    loaded = actions.modules
    monkeypatch.setattr(actions, "modules", lambda venue: loaded(venue)[:1])
    node, engine = two_paths(AT_THE_QUOTE, points)

    assert fills_of(node) == fills_of(engine) == []
    assert compared(node, engine) == (None, 0)
    assert len(node.intents) == 2


ACROSS = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Rests a buy of 445 at 9.90 on DEMO from its first quote, and sends a buy of one far
    under OTHR's market from every OTHR quote."""

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.placed = False

    def on_quote_tick(self, tick) -> None:
        if str(tick.instrument_id) == "DEMO.XNAS":
            if not self.placed:
                self.placed = True
                self.submit_entry(tick.instrument_id, "BUY", qty=445, price=9.9)
        else:
            self.submit_entry(tick.instrument_id, "BUY", qty=1, price=1.0)
'''


def test_parity_misses_a_command_for_another_name_re_crediting_a_print() -> None:
    """No latency. DEMO's book is a print of 100 at the resting buy's price from 30 ms, stamped
    before the venue's last update; OTHR quotes every millisecond from 31 to 36 ms and the
    sleeve sends OTHR a buy of one from each. Every one of those commands lands, and the
    research engine matches every resting order of the venue again — DEMO's buy among them,
    against DEMO's print — so one print of 100 is credited 445; the node credits it once."""
    other = [
        QuoteTick(
            InstrumentId.from_str(OTHER_ID),
            Price(50.0, 2),
            Price(50.02, 2),
            Quantity.from_int(1_000),
            Quantity.from_int(1_000),
            T0 + ms * MS,
            T0 + ms * MS,
        )
        for ms in range(31, 37)
    ]
    points = [quote(9.99, 10.01, 10, 10), quote(9.95, 10.0, 20, 20), trade(9.9, 100, 15, 30, 1)]

    node, engine = two_paths(ACROSS, [*points, *other], names=(INSTRUMENT, OTHER_ID))

    assert fills_of(node) == [(30, 100.0, 9.9, True)]
    assert fills_of(engine) == [
        (30, 100.0, 9.9, True),
        (31, 100.0, 9.9, True),
        (32, 100.0, 9.9, True),
        (33, 100.0, 9.9, True),
        (34, 45.0, 9.9, True),
    ]
    assert compared(node, engine) == (None, 0)


def test_parity_misses_the_paths_parting_on_a_stale_print_an_order_lands_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under 20 ms the buy of 320 lands on the first print's instant, and the print stands as
    the book: both paths fill 100 there as a taker, and the research engine, matching every
    resting order again once the buy has landed, fills another 100 from the same print as a
    maker, where the node waits for the next print. The venue used to skip that print,
    stamped before its last update, and the two paths then agreed; it now applies it, so the
    parting reaches it. Parity compares intents, which agree, so it calls the two identical
    either way. Pinned for `docs/backlog.md` row 154 as `PARTING` says."""

    node, engine = posted(320, 9.9, STALE_PRINTS, latency_ms=20)

    assert fills_of(node) == [
        (30, 100.0, 9.9, False),
        (40, 100.0, 9.9, True),
        (50, 100.0, 9.9, True),
        (60, 20.0, 9.9, True),
    ]
    assert fills_of(engine) == [
        (30, 100.0, 9.9, False),
        (30, 100.0, 9.9, True),
        (40, 100.0, 9.9, True),
        (50, 20.0, 9.9, True),
    ]
    assert compared(node, engine) == (None, 0)

    loaded = actions.modules
    monkeypatch.setattr(actions, "modules", lambda venue: loaded(venue)[:1])
    node, engine = posted(320, 9.9, STALE_PRINTS, latency_ms=20)

    skipped = [(40, 100.0, 9.9, True), (50, 100.0, 9.9, True), (60, 100.0, 9.9, True)]
    assert fills_of(node) == fills_of(engine) == skipped
    assert compared(node, engine) == (None, 0)
