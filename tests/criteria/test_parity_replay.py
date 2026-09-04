"""The parity gate: what it compares, what the tolerance is for, and what silence proves.

Two kinds of test. The first judge hand-built intent sequences, where every field of every
order is chosen here, so the verdict and its evidence are checked without replaying
anything. The last replays one hypothesis over its certification window on both code paths
and hands the gate exactly what the runner produced, because a gate that reads a shape
nobody builds is a gate that has never run.
"""

from __future__ import annotations

from typing import Any

from kanso import replay
from kanso.criteria import catalogue, gates, plannable
from kanso.criteria.gates.parity_replay import NO_INTENTS, NO_PARITY, NO_TOLERANCE, gate
from kanso.replay import Intent, Parity
from kanso.replay.parity import compare
from kanso.schemas import GateResult
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.criteria.builders import build_run, context
from tests.replay.conftest import CERTIFICATION, carded_hyp, prepared, store, ws

__all__ = ["carded_hyp", "prepared", "store", "ws"]

SECOND_NS = 1_000_000_000
FLAT = (0.0, 0.0)


def intent(**changes: Any) -> Intent:
    """One order intent, with these fields replaced."""
    fields: dict[str, Any] = {
        "ts_event": 1_000,
        "instrument": "DEMO.XNAS",
        "side": "BUY",
        "qty": 10.0,
        "order_type": "MARKET",
        "price": None,
    }
    return Intent(**{**fields, **changes})


def compared(node: tuple[Intent, ...], engine: tuple[Intent, ...], *, ts_ns: int = 0) -> Parity:
    """Two sequences as the replay runner would hand them over, already compared."""
    divergence, widest = compare(node, engine, ts_ns=ts_ns)
    return Parity(
        node="20240301T000000Z-node-abcdef0",
        engine="20240301T000001Z-engine-abcdef0",
        ts_ns=ts_ns,
        node_orders=node,
        engine_orders=engine,
        max_ts_delta_ns=widest,
        divergence=divergence,
    )


def judged(supplied: object, ts_ns: int | None = 0) -> GateResult:
    """The gate's verdict on a supplied comparison, at the tolerance a plan chose."""
    params = {} if ts_ns is None else {"ts_ns": ts_ns}
    return gate.evaluate(context(build_run(FLAT), stage="cert", params=params, session=supplied))


# --- what it compares ---------------------------------------------------------


def test_the_same_orders_on_both_paths_pass() -> None:
    """The claim the whole design rests on, when it holds."""
    orders = (intent(), intent(ts_event=2_000, side="SELL"))

    result = judged(compared(orders, orders))

    assert result.passed
    assert result.skipped is None
    assert result.evidence["identical"] is True
    assert result.evidence["compared"] == 2
    assert result.evidence["max_ts_delta_ns"] == 0
    assert result.evidence["divergence"] is None


def test_a_different_quantity_is_a_different_decision() -> None:
    """No tolerance makes two sizes the same size, so the size is compared exactly."""
    node = (intent(), intent(ts_event=2_000, qty=10.0))
    engine = (intent(), intent(ts_event=2_000, qty=11.0))

    result = judged(compared(node, engine))

    assert not result.passed
    assert "qty" in str(result.evidence["divergence"])
    assert "intent 1" in str(result.evidence["divergence"])


def test_a_path_that_stopped_submitting_diverges_where_it_stopped() -> None:
    """One path sending fewer orders is not agreement about the ones it did send."""
    result = judged(compared((intent(), intent(ts_event=2_000)), (intent(),)))

    assert not result.passed
    assert result.evidence["node_intents"] == 2
    assert result.evidence["engine_intents"] == 1
    assert "only the node path" in str(result.evidence["divergence"])


def test_evidence_names_both_sessions_so_a_divergence_can_be_looked_at() -> None:
    """A failure is a bug with a location, and the sessions are where it is."""
    result = judged(compared((intent(),), (intent(qty=1.0),)))

    assert result.evidence["node"] == "20240301T000000Z-node-abcdef0"
    assert result.evidence["engine"] == "20240301T000001Z-engine-abcdef0"


# --- the tolerance ------------------------------------------------------------


def test_an_instant_inside_the_tolerance_is_the_same_decision() -> None:
    node = (intent(ts_event=1_000),)
    engine = (intent(ts_event=1_000 + SECOND_NS // 2),)

    result = judged(compared(node, engine, ts_ns=SECOND_NS), ts_ns=SECOND_NS)

    assert result.passed
    assert result.evidence["max_ts_delta_ns"] == SECOND_NS // 2


def test_an_instant_outside_the_tolerance_fails() -> None:
    node = (intent(ts_event=1_000),)
    engine = (intent(ts_event=1_001),)

    result = judged(compared(node, engine))

    assert not result.passed
    assert "ts_event" in str(result.evidence["divergence"])


def test_the_plans_tolerance_judges_and_not_the_one_the_replay_ran_at() -> None:
    """The comparison travels with both sequences, so the gate asks its own question."""
    node = (intent(ts_event=1_000),)
    engine = (intent(ts_event=1_000 + SECOND_NS),)
    lenient = compared(node, engine, ts_ns=SECOND_NS)

    assert lenient.identical

    result = judged(lenient, ts_ns=0)

    assert not result.passed
    assert result.evidence["ts_ns"] == 0
    assert result.evidence["max_ts_delta_ns"] == SECOND_NS


# --- what it will not judge ---------------------------------------------------


def test_without_a_tolerance_nothing_is_required() -> None:
    result = judged(compared((intent(),), (intent(qty=1.0),)), ts_ns=None)

    assert result.passed
    assert result.skipped == NO_TOLERANCE


def test_without_a_replay_nothing_is_compared() -> None:
    result = judged(None)

    assert result.passed
    assert result.skipped == NO_PARITY


def test_something_that_is_not_a_comparison_is_not_one() -> None:
    """A session id is not the comparison of two of them, and is not read as one."""
    result = judged("20240301T000000Z-node-abcdef0")

    assert result.passed
    assert result.skipped == NO_PARITY


def test_two_empty_sequences_are_silence_rather_than_agreement() -> None:
    """A window neither path traded says nothing about whether they would agree."""
    result = judged(compared((), ()))

    assert result.passed
    assert result.skipped == NO_INTENTS


def test_one_empty_sequence_is_a_divergence_rather_than_silence() -> None:
    """Only one path deciding to trade is the loudest divergence there is."""
    result = judged(compared((), (intent(),)))

    assert not result.passed
    assert result.skipped is None
    assert "only the engine path" in str(result.evidence["divergence"])


# --- the toolbox --------------------------------------------------------------


def test_the_toolbox_resolves_this_gate_and_offers_it_to_a_planner() -> None:
    """A required gate with an implementation is one a certificate can actually claim."""
    assert gates()["parity_replay"] is gate
    assert catalogue()["parity_replay"].required
    assert "parity_replay" in plannable()


# --- the two code paths -------------------------------------------------------


def test_it_passes_on_a_real_replay_of_the_certification_window(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The gate over what a certification hands it: one target, two paths, one window."""
    result = judged(
        replay.parity(ws, store, hyp=carded_hyp, start=CERTIFICATION[0], end=CERTIFICATION[1])
    )

    assert result.passed, result.evidence["divergence"]
    assert result.skipped is None
    assert result.evidence["compared"] > 0
    assert result.evidence["max_ts_delta_ns"] == 0
