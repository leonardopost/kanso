"""A mock-scripted register, and a strategy the scripted diffs can steer.

The driver's fixture has to answer a question the loop's own fixtures did not: what does a
proposer return that applies to whatever the lane holds *now*? A run's file is the run's
best after a discard and the new bytes after a keep, so a diff written against one exact
file would apply once and never again.

The answer is a strategy whose behaviour is one class attribute and a diff that inserts an
assignment to it above a marker line at the end of the file. The marker survives every
application, so the same three-answer script drives a run of any length: the newest
assignment is the last one executed and therefore the one that wins, and a script of
`revert`, `boom` and `weak` produces a keep, a crash and a discard in that order and then
goes round again.

Every model here is the `mock` protocol, so nothing in this directory opens a socket and
every test is green with every provider credential unset.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from kanso.models import reset_mock
from kanso.workspace import Workspace, find

TIER_MODELS: dict[str, str] = {
    "cheap": "cheap_mock",
    "mid": "mid_mock",
    "frontier": "frontier_mock",
}
"""One mock model per tier, each with its own script: `propose` is routed to `mid` and
`align_check` to `cheap`, so the two never share a cursor."""

MARKER = "# end"
"""The last line of the seed strategy, and the anchor every scripted diff hangs on."""

SEED = b'''from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """One saw-tooth trader whose behaviour is the `mode` the last assignment sets."""

    config_cls = Config
    mode = "flat"

    def on_start(self) -> None:
        self.closes = []
        self.long = False

    def on_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        if self.mode == "boom":
            raise RuntimeError("the card asked for the impossible")
        if self.mode == "flat" or len(self.closes) < 3:
            return
        first, second, third = self.closes[-3:]
        if self.mode == "revert":
            if first > second > third and not self.long:
                self.submit_entry(
                    bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
                )
                self.long = True
            elif first < second < third and self.long:
                self.submit_exit(bar.bar_type.instrument_id)
                self.long = False
        elif third < second and not self.long:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.long = True
        elif third > second and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False


# end
'''
"""`flat` trades nothing, `revert` buys the trough and sells the peak, `weak` reacts to
every step and earns nothing, `boom` raises on the first bar."""


def mode_diff(mode: str) -> str:
    """A diff that makes the strategy run in `mode`, and that applies however often."""
    return (
        "--- a/strategy.py\n"
        "+++ b/strategy.py\n"
        "@@ -40,1 +40,2 @@\n"
        f'+Strategy.mode = "{mode}"\n'
        f" {MARKER}\n"
    )


def proposal(mode: str, desc: str | None = None) -> dict[str, Any]:
    """One scripted `propose` answer."""
    return {"desc": desc or f"run in {mode} mode", "diff": mode_diff(mode)}


CYCLE: list[dict[str, Any]] = [proposal("revert"), proposal("boom"), proposal("weak")]
"""A keep, a crash and a discard, forever."""

ALIGNED: dict[str, Any] = {"aligned": True, "reason": "still the stated mean reversion"}
DRIFTED: dict[str, Any] = {"aligned": False, "reason": "it now trades momentum instead"}


def model(model_id: str, tier: str) -> dict[str, Any]:
    """One register entry for a mock model serving one tier."""
    return {
        "id": model_id,
        "provider": "kanso",
        "protocol": "mock",
        "tier": tier,
        "local": True,
        "ctx": 100_000,
        "cost_in": 3.0,
        "cost_out": 15.0,
        "tools": True,
        "script": f"mock/{model_id}.yaml",
    }


def write_register(ws: Workspace) -> None:
    """Replace the workspace's register with one mock model per tier."""
    document = {
        "schema": 1,
        "models": [model(name, tier) for tier, name in TIER_MODELS.items()],
    }
    ws.path("models.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def write_script(ws: Workspace, tier: str, answers: dict[str, list[Any]]) -> Path:
    """Write the script the model on `tier` answers from."""
    path = ws.path("mock", f"{TIER_MODELS[tier]}.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")
    return path


def scripted(
    ws: Workspace,
    propose: list[Any] | None = None,
    align_check: list[Any] | None = None,
) -> None:
    """Point the register at mock models and script the tiers the two classes route to."""
    write_register(ws)
    write_script(ws, "mid", {"propose": propose if propose is not None else CYCLE})
    write_script(ws, "cheap", {"align_check": align_check} if align_check is not None else {})
    write_script(ws, "frontier", {})


def tuned(ws: Workspace, **values: object) -> Workspace:
    """The same workspace with these `[research]` settings replaced in `kanso.toml`."""
    path = ws.path("kanso.toml")
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text, found = re.subn(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
        assert found == 1, f"{key} is not a [research] key of the template"
    path.write_text(text, encoding="utf-8")
    return find(ws.root)


@pytest.fixture(autouse=True)
def fresh_cursors() -> Iterator[None]:
    """The mock's cursors live for the process, so each test starts its script over."""
    reset_mock()
    yield
    reset_mock()
