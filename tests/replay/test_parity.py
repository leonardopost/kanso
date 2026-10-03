"""Parity: the two code paths over one range, and what a divergence between them looks like."""

from __future__ import annotations

from datetime import date

import pytest

from kanso import replay
from kanso.replay import record
from kanso.replay.parity import RELEASED, STREAM, Intent, Parity, compare, of_sessions
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import (
    BLOCKING_FILTER,
    FLAT,
    HOLDING,
    RAISING,
    bars,
    carded,
    composed,
    document,
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
