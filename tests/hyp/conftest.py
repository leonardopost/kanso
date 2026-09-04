"""Fixtures for the registry: a vendor-free workspace and one hypothesis to break.

Every instrument here is `manual`, so resolution never reaches a reference adapter and
the suite is green with every vendor credential unset. `document` is the admissible
hypothesis; each validation test changes exactly the field it is about.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from kanso.schemas import Portfolio, StrategyFile, load_yaml, resolve_venue_model, write_yaml
from kanso.state import StateStore
from kanso.workspace import Workspace, init

HYP_ID = "demo_mr"
"""The hypothesis every test starts from: the demo's, on one synthetic instrument."""

NOW = datetime(2026, 1, 1, tzinfo=UTC)
HOST_SHA = "b" * 64

DEMO_ENTRY: dict[str, Any] = {
    "nautilus_id": "DEMO.SIM",
    "asset_class": "EQUITY",
    "manual": True,
    "corporate_actions": "none",
    "override": {"currency": "USD", "price_increment": "0.01", "lot_size": "1"},
}
"""The synthetic instrument the demo loader generates, on the simulated venue."""

EURO_ENTRY: dict[str, Any] = dict(
    DEMO_ENTRY, nautilus_id="EURO.XETR", override={"currency": "EUR", "price_increment": "0.01"}
)
"""A second instrument, on a venue the portfolio can give another account currency."""

GONE_ENTRY: dict[str, Any] = dict(
    DEMO_ENTRY, nautilus_id="GONE.SIM", attributes={"delisted": "2023-06-30"}
)
LATE_ENTRY: dict[str, Any] = dict(
    DEMO_ENTRY, nautilus_id="LATE.SIM", attributes={"listed": "2024-06-01"}
)

INSTRUMENTS: dict[str, dict[str, Any]] = {
    "DEMO": DEMO_ENTRY,
    "EURO": EURO_ENTRY,
    "GONE": GONE_ENTRY,
    "LATE": LATE_ENTRY,
}

DOCUMENT: dict[str, Any] = {
    "schema": 1,
    "id": HYP_ID,
    "title": "Demo: intraday mean reversion on a synthetic OU series",
    "thesis": "Prices of DEMO revert to a rolling mean within the hour.",
    "mechanism": "mean_reversion",
    "universe": ["DEMO"],
    "horizon": "30m",
    "resolution": "1m",
    "data_requirements": ["bar"],
    "costs": {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "fixed_bps", "fixed_bps": 2},
    "risk_limits": {"max_position_pct": 20, "max_drawdown_pct": 15, "max_leverage": 1},
    "windows": {
        "research": {"start": "2024-01-02", "end": "2024-12-31"},
        "certification": {"start": "2025-01-06", "end": "2025-05-30"},
        "forward": {"start": "2025-06-02"},
    },
}
"""An admissible hypothesis; every test below changes exactly one thing about it."""

SLEEVE_CLASSIFICATION: dict[str, Any] = {
    "construct": {"id": "sleeve", "rationale": "a strategy of its own"},
    "objective": {"id": "net_edge_bps", "params": {"min_delta": 0.5, "k_se": 1.0}},
    "constraints": [{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 30}}],
}
"""What `classify` writes for a sub-daily sleeve: an absolute, per-trade objective."""

HOST_ID = "host_sleeve"

FILTER_CLASSIFICATION: dict[str, Any] = {
    "construct": {"id": "filter", "host": HOST_ID, "params": {"scope": "time"}},
    "objective": {"id": "marginal_net_edge_bps", "params": {"min_delta": 0.0, "k_se": 1.0}},
    "constraints": [{"id": "strategy_integrity"}],
}
"""What `classify` writes for a construct attached to a certified host."""


def document(**changes: Any) -> dict[str, Any]:
    """The admissible hypothesis with these fields replaced."""
    return {**DOCUMENT, **changes}


def instruments(*ids: str) -> dict[str, dict[str, Any]]:
    """The entries for these ids, as an `instruments.yaml` document."""
    return {wanted: INSTRUMENTS[wanted] for wanted in ids}


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A scaffolded workspace holding the manual instruments these tests resolve."""
    workspace = init(tmp_path / "ws")
    write_instruments(workspace, "DEMO", "EURO", "GONE", "LATE")
    return workspace


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store for that workspace."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


def write_instruments(ws: Workspace, *ids: str) -> None:
    """Replace the workspace's instrument file with these entries."""
    write(ws.path("instruments.yaml"), _yaml(instruments(*ids)))


def write_hypothesis(ws: Workspace, doc: dict[str, Any], hyp_id: str | None = None) -> Path:
    """Write a hypothesis document into `hypotheses/<id>/` and return its path."""
    directory = ws.path("hypotheses", hyp_id or str(doc["id"]))
    return write(directory / "hypothesis.yaml", _yaml(doc))


def write_portfolio(ws: Workspace, venues: dict[str, dict[str, Any]]) -> None:
    """Give the workspace portfolio a `venues` table."""
    path = ws.path("portfolio.yaml")
    held = load_yaml(Portfolio, path).model_dump(mode="json", by_alias=True, exclude_none=True)
    write_yaml(Portfolio.model_validate({**held, "venues": venues}), path)


def write_strategy(ws: Workspace, strategy_id: str, declared_id: str | None = None) -> Path:
    """Compose a one-version host strategy, as certification would."""
    named = declared_id or strategy_id
    strategy = StrategyFile.model_validate(
        {
            "schema": 1,
            "id": named,
            "versions": [
                {
                    "version": 1,
                    "sleeve": {"hyp_id": named, "strategy_sha": HOST_SHA},
                    "pins": {
                        "kanso_version": "0.1.0",
                        "nautilus_version": "1.231.0",
                        "criteria_version": "0.1.0",
                        "plan_version": 1,
                        "snapshot_id": "snap1",
                        "venue_model": resolve_venue_model("SIM", max_leverage=1.0),
                    },
                    "expectation": {
                        "objective_id": "net_edge_bps",
                        "value": 1.1,
                        "ci90": [0.4, 1.8],
                        "mdd_p95": 9.0,
                        "window": {"start": "2025-01-06", "end": "2025-05-30"},
                    },
                    "state": "composed",
                    "created_at": NOW,
                }
            ],
        }
    )
    path = ws.path("strategies", strategy_id, "strategy.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_yaml(strategy, path)


def begin_run(store: StateStore, hyp_id: str, run_id: str = "r1") -> str:
    """Insert an active run for a hypothesis, as `research begin` would."""
    store.connection.execute(
        "INSERT INTO runs (run_id, hyp_id, tag, lane, dir, hypothesis_sha, program_sha,"
        " snapshot_id, criteria_version, card_budget_s, baseline_wall_s,"
        " baseline_peak_mem_gb, started_at)"
        " VALUES (?, ?, '20260101-1', 'op', 'runs/op/x', 'a', 'b', 's', 'c', 60, 1, 1, ?)",
        (run_id, hyp_id, NOW.isoformat()),
    )
    return run_id


def write(path: Path, text: str) -> Path:
    """Write UTF-8 text, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
