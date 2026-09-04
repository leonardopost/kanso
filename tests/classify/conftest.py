"""Fixtures for the construct catalogue: the demo hypothesis, a host strategy, extensions.

The hypothesis and the instrument are the demo's — one synthetic instrument on a synthetic
venue — so every construct is exercised against the same data the shipped demo generates
and no test needs a vendor, a credential or a network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kanso.schemas import (
    Expectation,
    Hypothesis,
    Params,
    Pins,
    StrategyFile,
    resolve_venue_model,
)
from kanso.workspace import Workspace, init

SHA = "a" * 64
"""A card's `strategy_sha`; content addresses are 64 hex digits."""

HOST_SHA = "b" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEMO: dict[str, Any] = {
    "schema": 1,
    "id": "demo_mr",
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

PINS = Pins.model_validate(
    {
        "kanso_version": "0.1.0",
        "nautilus_version": "1.231.0",
        "criteria_version": "0.1.0",
        "plan_version": 1,
        "snapshot_id": "snap1",
        "venue_model": resolve_venue_model("SIM", max_leverage=1.0),
    }
)

EXPECTATION = Expectation.model_validate(
    {
        "objective_id": "wf_sharpe_net",
        "value": 1.1,
        "ci90": [0.4, 1.8],
        "mdd_p95": 9.0,
        "window": {"start": "2025-01-06", "end": "2025-05-30"},
    }
)


def hypothesis(
    construct: str | None = None,
    host: str | None = None,
    params: Params | None = None,
    **changes: Any,
) -> Hypothesis:
    """The demo hypothesis, optionally classified onto `construct`."""
    document = dict(DEMO, **changes)
    if construct is not None:
        document["construct"] = {"id": construct, "host": host, "params": params}
    return Hypothesis.model_validate(document)


def strategy(*attached: dict[str, Any], id: str = "demo_sleeve") -> StrategyFile:
    """A composed host strategy: a sleeve at version 1 plus one version per attachment."""
    versions = [
        {
            "version": 1,
            "sleeve": {"hyp_id": id, "strategy_sha": HOST_SHA},
            "attached": [],
            "config": {"lookback": 20},
            "pins": PINS,
            "expectation": EXPECTATION,
            "state": "composed",
            "created_at": NOW,
        }
    ]
    for number, ref in enumerate(attached, start=2):
        versions.append(
            dict(
                versions[-1],
                version=number,
                attached=[*versions[-1]["attached"], ref],  # type: ignore[misc]
                state="composed",
            )
        )
    return StrategyFile.model_validate({"schema": 1, "id": id, "versions": versions})


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A scaffolded workspace, so extension discovery has somewhere to look."""
    return init(tmp_path / "ws")


def extension(ws: Workspace, name: str, body: str) -> Path:
    """Write a single-module workspace extension and return its path."""
    directory = ws.root / "kanso_ext"
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def package(ws: Workspace, name: str, body: str, impl: str) -> Path:
    """Write a package extension: `__init__.py` declares, `constructs.py` implements.

    A catalogue item's `impl` is a dotted path, so an extension that ships one puts its
    implementation in a submodule of its own package.
    """
    directory = ws.root / "kanso_ext" / name
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(body, encoding="utf-8")
    (directory / "constructs.py").write_text(impl, encoding="utf-8")
    return directory


def item(construct_id: str, impl: str, **changes: Any) -> dict[str, Any]:
    """A catalogue item in the shape an extension declares."""
    return {
        "id": construct_id,
        "description": f"a {construct_id} provided by an extension",
        "needs_host": "sleeve",
        "objective_mode": "relative",
        "runnable": True,
        "impl": impl,
        **changes,
    }
