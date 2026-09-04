"""A construct attached to a certified host: the card runs both, the objective differences.

A `filter` is not a strategy. Its card runs the host sleeve with the modifier on it, and
its metric is what the modifier added to the host fold by fold — so the host has to run
too, once per run, and a modifier that changes nothing must score exactly zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest

from kanso.errors import PreconditionError
from kanso.research import loop, records
from kanso.schemas import StrategyFile, resolve_venue_model, write_yaml
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import RAISING, REVERTING, classify, document

HOST = "host_sleeve"
NOW = datetime(2024, 1, 1, tzinfo=UTC)

ALLOWING = b'''
from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    scope: str = "time"


class Modifier(KansoModifier):
    """Allows every entry, so the host runs exactly as it would alone."""

    construct = "filter"
    config_cls = Config

    def evaluate(self, ctx) -> Decision:
        return Decision(allow=True)
'''

BLOCKING = ALLOWING.replace(b"Decision(allow=True)", b"Decision(allow=False)").replace(
    b"Allows every entry, so the host runs exactly as it would alone.",
    b"Refuses every entry, so the host never trades.",
)

FILTER = document(
    id="demo_filter",
    construct={"id": "filter", "host": HOST, "params": {"scope": "time"}},
    objective={"id": "marginal_wf_sharpe", "params": {"min_delta": 0.0, "k_se": 0.5}},
)
"""A filter attached to the certified host, measured against what the host does alone."""


def compose_host(ws: Workspace, store: StateStore, source: bytes = REVERTING) -> str:
    """Write a one-version host strategy and store the bytes its sleeve names."""
    sha = store.put_blob(source)
    strategy = StrategyFile.model_validate(
        {
            "schema": 1,
            "id": HOST,
            "versions": [
                {
                    "version": 1,
                    "sleeve": {"hyp_id": HOST, "strategy_sha": sha},
                    "pins": {
                        "kanso_version": "0.1.0",
                        "nautilus_version": "1.231.0",
                        "criteria_version": "0.1.0",
                        "plan_version": 1,
                        "snapshot_id": "s",
                        "venue_model": resolve_venue_model("XNAS", max_leverage=1.0),
                    },
                    "expectation": {
                        "objective_id": "wf_sharpe_net",
                        "value": 15.0,
                        "ci90": [10.0, 20.0],
                        "mdd_p95": 5.0,
                        "window": {"start": "2024-01-01", "end": "2024-01-31"},
                    },
                    "state": "composed",
                    "created_at": NOW,
                }
            ],
        }
    )
    path = ws.path("strategies", HOST, "strategy.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(strategy, path)
    return sha


def attached(ws: Workspace, store: StateStore, **changes: Any) -> str:
    """The filter hypothesis, classified onto the composed host."""
    return classify(ws, store, {**FILTER, **changes}, ALLOWING)


def test_a_modifier_that_changes_nothing_scores_exactly_zero(
    ws: Workspace, store: StateStore
) -> None:
    compose_host(ws, store)
    hyp_id = attached(ws, store)

    run = loop.begin(ws, store, hyp_id)
    baseline = records.cards_of(store, hyp_id)[0]

    assert run.host_version == 1
    assert baseline.status == "keep"
    assert baseline.metric == 0.0, "the host with a neutral filter is the host"
    assert baseline.n_trades > 0, "the card is the host's run, not the modifier's"


def test_a_filter_that_refuses_every_entry_loses_what_the_host_earned(
    ws: Workspace, store: StateStore
) -> None:
    compose_host(ws, store)
    hyp_id = attached(ws, store)
    run = loop.begin(ws, store, hyp_id)

    (ws.root / run.dir / "strategy.py").write_bytes(BLOCKING)
    blocked = loop.card(ws, store, hyp_id, "refuse every entry")

    assert blocked.status == "discard"
    assert blocked.metric < 0.0
    assert blocked.n_trades == 0
    assert (ws.root / run.dir / "strategy.py").read_bytes() == ALLOWING


def test_the_host_run_is_computed_once_per_run_and_dropped_when_it_ends(
    ws: Workspace, store: StateStore
) -> None:
    compose_host(ws, store)
    hyp_id = attached(ws, store)
    run = loop.begin(ws, store, hyp_id)

    assert list(loop._HOST_RUNS[run.run_id]) == [f"{run.snapshot_id}:{HOST}@1"]
    loop.card(ws, store, hyp_id, "the same neutral filter")
    assert len(loop._HOST_RUNS[run.run_id]) == 1

    loop.end(ws, store, hyp_id)
    assert run.run_id not in loop._HOST_RUNS


def test_a_host_that_cannot_run_leaves_nothing_to_measure_against(
    ws: Workspace, store: StateStore
) -> None:
    compose_host(ws, store, RAISING)
    hyp_id = attached(ws, store)
    with pytest.raises(PreconditionError, match="did not run over the research window"):
        loop.begin(ws, store, hyp_id)


def test_a_host_whose_bytes_this_workspace_lost_is_refused(
    ws: Workspace, store: StateStore
) -> None:
    compose_host(ws, store)
    hyp_id = attached(ws, store)
    store.connection.execute("DELETE FROM blobs WHERE sha = ?", (sha256(REVERTING).hexdigest(),))
    with pytest.raises(PreconditionError, match="holds no such bytes"):
        loop.begin(ws, store, hyp_id)


def test_a_host_this_workspace_never_composed_is_refused(ws: Workspace, store: StateStore) -> None:
    compose_host(ws, store)
    hyp_id = attached(ws, store)
    ws.path("strategies", HOST, "strategy.yaml").unlink()
    with pytest.raises(PreconditionError, match="is not a composed strategy"):
        loop.begin(ws, store, hyp_id)


def test_an_unclassified_hypothesis_has_no_construct_to_research_it_as(
    ws: Workspace, store: StateStore
) -> None:
    from kanso.schemas import Hypothesis

    from .conftest import DOCUMENT

    plain = Hypothesis.model_validate(
        {
            key: value
            for key, value in DOCUMENT.items()
            if key not in ("construct", "objective", "constraints")
        }
    )
    with pytest.raises(PreconditionError, match="is not classified"):
        loop._setup(ws, store, plain)
