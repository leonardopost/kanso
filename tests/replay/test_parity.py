"""Parity: the two code paths over one range, and what a divergence between them looks like."""

from __future__ import annotations

import pytest

from kanso import replay
from kanso.replay.parity import Intent, Parity, compare
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import BLOCKING_FILTER, FLAT, RAISING, carded, composed, document


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
