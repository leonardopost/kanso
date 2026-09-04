"""A workspace whose register is the shipped mock protocol, one model per tier.

Every test here is offline by construction: the mock clients read files, and the two wire
clients are exercised through a fake transport that never opens a socket. No test in this
directory resolves a real credential, and the suite is green with every provider variable
unset.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from kanso.models import reset_mock
from kanso.state import StateStore
from kanso.workspace import Workspace, init

TIER_MODELS: dict[str, str] = {
    "cheap": "cheap_mock",
    "mid": "mid_mock",
    "frontier": "frontier_mock",
}
"""One mock model per tier, each with its own script, so a test can decide which tier
answers what and read the ladder off the ledger."""


def model(
    model_id: str,
    tier: str | list[str],
    *,
    protocol: str = "mock",
    script: str | None = None,
    provider: str = "kanso",
    local: bool = True,
    **changes: Any,
) -> dict[str, Any]:
    """One register entry, with the fields a test does not care about filled in."""
    entry: dict[str, Any] = {
        "id": model_id,
        "provider": provider,
        "protocol": protocol,
        "tier": tier,
        "local": local,
        "ctx": 100_000,
        "cost_in": 3.0,
        "cost_out": 15.0,
        "tools": True,
    }
    if protocol == "mock":
        entry["script"] = script if script is not None else f"mock/{model_id}.yaml"
    entry.update(changes)
    return entry


def write_register(
    ws: Workspace,
    models: list[dict[str, Any]] | None = None,
    routing: dict[str, Any] | None = None,
) -> None:
    """Replace the workspace's register."""
    document: dict[str, Any] = {
        "schema": 1,
        "models": models if models is not None else [model(m, t) for t, m in TIER_MODELS.items()],
    }
    if routing is not None:
        document["routing"] = routing
    ws.path("models.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def write_script(ws: Workspace, model_id: str, answers: dict[str, list[Any]]) -> Path:
    """Write one mock model's script of answers, keyed by task class."""
    path = ws.path("mock", f"{model_id}.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")
    return path


def write_scripts(ws: Workspace, **per_tier: dict[str, list[Any]]) -> None:
    """Write a script for each named tier's model."""
    for tier, answers in per_tier.items():
        write_script(ws, TIER_MODELS[tier], answers)


ALIGNED: dict[str, Any] = {"aligned": True, "reason": "same mechanism and universe"}
CLASSIFIED: dict[str, Any] = {
    "construct": {"id": "sleeve"},
    "objective_params": {"min_delta": 0.0, "k_se": 1.0},
    "constraints": [{"id": "strategy_integrity", "params": {}}],
    "rationale": "a complete signal-to-trade thesis with nothing to attach to",
}
PROPOSED: dict[str, Any] = {"desc": "fade two-sigma deviations", "diff": "--- a\n+++ b\n"}
PLANNED: dict[str, Any] = {
    "gates": [
        {"id": "embargoed_window", "stage": "cert", "params": {}, "rationale": "required"},
        {"id": "paper_forward", "stage": "paper", "params": {}, "rationale": "required"},
        {"id": "live_drift", "stage": "live", "params": {}, "rationale": "required"},
    ],
    "excluded": [],
}
"""One valid answer per task class, for scripts that are meant to succeed."""


@pytest.fixture(autouse=True)
def _fresh_cursors() -> Iterator[None]:
    """The mock's cursors live for the process, so each test starts them over."""
    reset_mock()
    yield
    reset_mock()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A scaffolded workspace whose register is three mock models, one per tier."""
    workspace = init(tmp_path / "ws")
    write_register(workspace)
    for model_id in TIER_MODELS.values():
        write_script(workspace, model_id, {})
    return workspace


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store for that workspace."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


@dataclass
class Exchange:
    """What a fake transport saw go out, kept so a test can assert the request shape."""

    bodies: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def body(self) -> dict[str, Any]:
        """The one request that was made."""
        assert len(self.bodies) == 1, f"{len(self.bodies)} requests were made"
        return self.bodies[0]


def recorded(*replies: tuple[int, Any]) -> tuple[httpx.MockTransport, Exchange]:
    """A transport that records each request and answers with the next `(status, json)`.

    No socket is opened, so a wire client is exercised by exactly the code the network
    path runs without a network being present.
    """
    seen = Exchange()

    def handle(request: httpx.Request) -> httpx.Response:
        seen.urls.append(str(request.url))
        seen.headers.append({k.lower(): v for k, v in request.headers.items()})
        seen.bodies.append(json.loads(request.content))
        status, payload = replies[min(len(seen.bodies) - 1, len(replies) - 1)]
        if payload is RAW_TEXT:
            return httpx.Response(status, text="not json at all")
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handle), seen


RAW_TEXT = object()
"""A reply body that is not JSON at all."""


def credentialled(ws: Workspace, name: str, value: str) -> None:
    """Put a credential in the workspace `.env`, as an operator would."""
    ws.path(".env").write_text(f"{name}={value}\n", encoding="utf-8")
