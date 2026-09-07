"""A mock-only register and a strategy the scripted diffs can steer, for the CLI slice.

The commands this milestone adds all end in a model call, and none of them may reach a
network. So the register these tests write lists one `mock` model per tier, each reading
its own script: `propose` routes to `mid` and `align_check` to `cheap`, so the two never
share a cursor and a test can script one without disturbing the other. `classify` and
`certify_plan` share the frontier model and so share one script, each reading its own
list from it.

The strategy the scripts drive is one file whose behaviour is the `mode` its last
assignment sets, and every scripted diff appends such an assignment above a marker line at
the end of the file. The marker survives every application, so one three-answer script
drives a run of any length: the newest assignment is the one that wins, and the file the
diff must fit is whatever the lane holds — the new bytes after a keep, the run's best
after a discard or a crash — which a marker-anchored diff always does.

The three trading modes are the three parameter sets the slice's own fixtures are
calibrated on, so `revert` keeps against a baseline that trades nothing, `weak` discards
against `revert`, and `better` keeps against it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tests.params import pairs

TIER_MODELS: dict[str, str] = {
    "cheap": "cheap_mock",
    "mid": "mid_mock",
    "frontier": "frontier_mock",
}
"""One mock model per tier, each with a script of its own."""

MARKER = "# end"
"""The last line of the seed strategy, and the anchor every scripted diff hangs on."""

SEED = '''from kanso.nautilus.strategy import KansoConfig, KansoStrategy

MODES = {
    "revert": (12, -0.3, 0.1),
    "weak": (6, -0.4, 0.0),
    "better": (24, -0.35, 0.0),
}


class Config(KansoConfig):
    notional: float = 20_000.0


class Strategy(KansoStrategy):
    """Fades a deviation of the close from its rolling mean, at the `mode`'s parameters."""

    config_cls = Config
    mode = "flat"

    def on_start(self) -> None:
        self.closes: list[float] = []
        self.long = False

    def on_bar(self, bar) -> None:
        if self.mode == "boom":
            raise RuntimeError("the card asked for the impossible")
        if self.mode not in MODES:
            return
        window, low, high = MODES[self.mode]
        self.closes.append(float(bar.close))
        if len(self.closes) > window:
            self.closes.pop(0)
        if len(self.closes) < window:
            return
        mean = sum(self.closes) / window
        spread = max(self.closes) - min(self.closes)
        if spread <= 0.0:
            return
        deviation = (self.closes[-1] - mean) / spread
        if deviation < low and not self.long:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.long = True
        elif deviation > high and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False


# end
'''
"""`flat` trades nothing, `boom` raises on the first bar, and the three in `MODES` fade."""


def mode_diff(mode: str) -> str:
    """A diff that makes the strategy run in `mode`, and that applies however often."""
    return (
        "--- a/strategy.py\n"
        "+++ b/strategy.py\n"
        "@@ -46,1 +46,2 @@\n"
        f'+Strategy.mode = "{mode}"\n'
        f" {MARKER}\n"
    )


def proposal(mode: str, desc: str | None = None) -> dict[str, Any]:
    """One scripted `propose` answer."""
    return {"desc": desc or f"run in {mode} mode", "diff": mode_diff(mode)}


CYCLE: list[dict[str, Any]] = [proposal("revert"), proposal("weak"), proposal("boom")]
"""A keep, a discard and a crash, forever."""

ALIGNED: dict[str, Any] = {"aligned": True, "reason": "still the stated mean reversion"}
DRIFTED: dict[str, Any] = {"aligned": False, "reason": "it now trades momentum instead"}


PLAN: dict[str, Any] = {
    "gates": [
        {
            "id": "embargoed_window",
            "stage": "cert",
            "params": pairs({"min_fraction": 0.0}),
            "rationale": "required; the only out-of-sample evidence there is",
        },
        {
            "id": "publication_lag",
            "stage": "cert",
            "params": pairs({"tolerance_s": 0.0}),
            "rationale": "required; the synthetic series is realtime, so any lag is a surprise",
        },
        {
            "id": "parity_replay",
            "stage": "cert",
            "params": pairs({"ts_ns": 0}),
            "rationale": "required; the deployed code path must be the researched one",
        },
        {
            "id": "book_correlation",
            "stage": "cert",
            "params": pairs({"max_corr": 0.8}),
            "rationale": "a candidate that repeats a deployed book adds risk, not return",
        },
        {
            "id": "paper_forward",
            "stage": "paper",
            "params": pairs({"min_duration": "5d", "horizon_mult": 20.0}),
            "rationale": "a plan reaches the paper stage",
        },
        {
            "id": "live_drift",
            "stage": "live",
            "params": pairs(),
            "rationale": "a plan reaches the live stage",
        },
    ],
    "excluded": [
        {"id": "bootstrap", "reason": "the two required gates are proof enough for a fixture"}
    ],
}
"""All three required cert gates, one gate that can only skip, then one gate per remaining
stage. Fewer gates is fewer engine runs, and what the CLI slice tests is the command
around the plan rather than the gates inside it; `book_correlation` is here because a
workspace with nothing deployed is how a skipped gate reaches the rendering."""


CLASSIFICATION: dict[str, Any] = {
    "construct": {"id": "sleeve"},
    "objective_params": {"min_delta": 0.0, "k_se": 0.5},
    "constraints": [
        {"id": "strategy_integrity", "params": pairs()},
        {"id": "min_trades", "params": pairs({"min": 4})},
    ],
    "rationale": "A complete signal-to-trade thesis with nothing to attach to: a sleeve.",
}
"""The `classify` answer this slice's hypothesis expects, matching its own fixtures."""


def model(model_id: str, tier: str, script: str | None = None) -> dict[str, Any]:
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
        "script": f"mock/{model_id}.yaml" if script is None else script,
    }


def write_register(root: Path, **scripts: str) -> Path:
    """Replace the workspace's register with one mock model per tier."""
    document = {
        "schema": 1,
        "models": [model(name, tier, scripts.get(tier)) for tier, name in TIER_MODELS.items()],
    }
    path = root / "models.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def write_script(root: Path, tier: str, answers: dict[str, list[Any]]) -> Path:
    """Write the script the model on `tier` answers from."""
    path = root / "mock" / f"{TIER_MODELS[tier]}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")
    return path


def scripted(
    root: Path,
    *,
    propose: list[Any] | None = None,
    align_check: list[Any] | None = None,
    classify: list[Any] | None = None,
    certify_plan: list[Any] | None = None,
) -> None:
    """Point the register at mock models and script the tiers the classes route to.

    `classify` and `certify_plan` share the frontier model, so they share one script and
    are written together; each task class reads its own list from it.
    """
    write_register(root)
    write_script(root, "mid", {"propose": CYCLE if propose is None else propose})
    write_script(root, "cheap", {} if align_check is None else {"align_check": align_check})
    frontier: dict[str, list[Any]] = {
        "certify_plan": [PLAN] if certify_plan is None else certify_plan
    }
    if classify is not None:
        frontier["classify"] = classify
    write_script(root, "frontier", frontier)


def tuned(root: Path, **values: object) -> None:
    """Replace these `[research]` settings in the workspace's `kanso.toml`."""
    path = root / "kanso.toml"
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text, found = re.subn(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
        assert found == 1, f"{key} is not a [research] key of the template"
    path.write_text(text, encoding="utf-8")
