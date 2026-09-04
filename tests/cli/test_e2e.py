"""The whole life of a hypothesis, in the commands an operator types, on synthetic data.

One test, one workspace, one sequence, and nothing mocked but the model: a thesis is
classified as a sleeve, researched, certified — the parity gate included, which is a replay
of both code paths over the certification window — composed, deployed to paper by nothing
but the certificate passing, judged promotable by the monitor, refused the live stage
without a name, promoted with one, and then demoted when the market it was promoted into
stops being the market it was measured on. A filter is then attached to that same sleeve and
certified, which gives the host a second version on paper.

**The drift is injected as data, not as a stub.** A second synthetic dataset, differently
seeded and with a downward drift, is loaded for the days after the ones the first one
covered. The live stage catches up on it the next time it is deployed, the rolling live
objective falls through the bottom of the band composition measured, and the version is
demoted with an entry in the inbox. Nothing here patches a gate or writes a verdict by hand:
what is asserted is that the loaded data is enough to make the machinery act.

The filter carries a price ceiling as a parameter, and on this series the sleeve's weakest
entries are the ones it takes above the long-run level, so the filter's marginal edge over
its host is positive and its certification passes. That is a property of the fixture rather
than a discovery about markets: what the leg proves is the attachment, the composition onto
the host and the second version reaching the stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from kanso.errors import Exit

from . import mocked
from .conftest import (
    DRAFT,
    E2E_WINDOWS,
    HYP_ID,
    at,
    payload,
    reconfigure,
    write_hypothesis,
    write_spec,
)

FILTER_ID = "demo_filter"
OPERATOR = "Ada Lovelace"

CEILING = '''from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    scope: str = "time"
    ceiling: float = 100.0


class Modifier(KansoModifier):
    """Lets the host fade a deviation only while the price is under the long-run level."""

    construct = "filter"
    config_cls = Config

    def evaluate(self, ctx) -> Decision:
        bar = ctx.last_bar
        if bar is None:
            return Decision(allow=True)
        return Decision(allow=float(bar.close) < self.modifier_config.ceiling)
'''

FILTER_DRAFT: dict[str, Any] = {
    **DRAFT,
    "id": FILTER_ID,
    "title": "Demo: only fade from below the long-run level",
    "thesis": "Fading a deviation pays below the series' long-run level and not above it.",
    "windows": E2E_WINDOWS,
}

FILTER_CLASSIFICATION: dict[str, Any] = {
    "construct": {"id": "filter", "host": HYP_ID, "params": {"scope": "time"}},
    "objective_params": {"min_delta": 0.0, "k_se": 0.5},
    "constraints": [{"id": "strategy_integrity", "params": {}}],
    "rationale": "A conditioning rule on an existing sleeve's entries: a filter.",
}

DRIFT: dict[str, Any] = {
    "seed": 99,
    "model": "gbm",
    "mu_bps": -8,
    "sigma_bps": 40,
    "start": "2024-07-01",
    "end": "2024-07-31",
}
"""A month the sleeve was never measured on: a different seed, and a downward drift a
strategy that only ever buys dips cannot survive."""


def ok(runner: CliRunner, root: Path, *args: object) -> dict[str, Any]:
    """Run one command with `--json`, insisting it succeeded, and return its object."""
    result = at(runner, root, *args, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def state_of(runner: CliRunner, root: Path, strategy: str, version: int) -> str:
    """One version's state, read back through `strat show`."""
    return str(ok(runner, root, "strat", "show", f"{strategy}@{version}")["state"])


def stage_of(runner: CliRunner, root: Path, name: str) -> dict[str, Any]:
    """One stage of `portfolio show`."""
    stages = ok(runner, root, "portfolio", "show")["stages"]
    return next(one for one in stages if one["stage"] == name)


def test_a_hypothesis_travels_from_a_draft_to_live_and_back(
    runner: CliRunner, loaded: Path
) -> None:
    root = loaded

    # -- classified as a sleeve ----------------------------------------------------
    path = write_hypothesis(root, mocked.SEED, base=DRAFT, windows=E2E_WINDOWS)
    assert at(runner, root, "hyp", "add", path).exit_code == Exit.OK
    mocked.scripted(root, classify=[mocked.CLASSIFICATION], align_check=[mocked.ALIGNED])
    classified = ok(runner, root, "classify", HYP_ID)
    assert classified["construct"]["id"] == "sleeve"
    assert classified["runnable"] is True

    # -- researched ----------------------------------------------------------------
    researched = ok(runner, root, "research", "run", HYP_ID, "--cards", 30)
    assert researched["proposed"] == 30
    assert researched["keeps"] and researched["discards"] and researched["crashes"], (
        "thirty scripted cards exercise all three outcomes"
    )
    assert researched["best_sha"]
    assert at(runner, root, "research", "end", HYP_ID).exit_code == Exit.OK

    # -- certified, the parity gate included ---------------------------------------
    certificate = ok(runner, root, "cert", "run", HYP_ID)
    assert certificate["verdict"] == "pass", certificate
    assert certificate["strategy_sha"] == researched["best_sha"]
    parity = next(gate for gate in certificate["gates"] if gate["id"] == "parity_replay")
    assert parity["skipped"] is None, "the required gate judged rather than skipping"
    assert parity["pass"] is True
    assert parity["evidence"]["identical"] is True
    assert parity["evidence"]["compared"] > 0
    assert ok(runner, root, "hyp", "show", HYP_ID)["status"] == "certified"

    # -- composed and on the paper stage, without being asked ----------------------
    assert state_of(runner, root, HYP_ID, 1) == "paper"
    paper = stage_of(runner, root, "paper")
    assert [one["strategy"] for one in paper["strategies"]] == [HYP_ID]
    assert paper["strategies"][0]["capital"] == 40_000, "the per-strategy limit, of the stage"
    assert paper["live"] is True

    # -- the paper gates pass, so the version becomes promotable -------------------
    watched = ok(runner, root, "monitor", "run")
    assert watched["actions"] == ["promotable"]
    assert state_of(runner, root, HYP_ID, 1) == "promotable"
    unread = ok(runner, root, "inbox")["entries"]
    assert [entry["kind"] for entry in unread] == ["promotable"]

    # -- the live stage refuses a version nobody named -----------------------------
    reconfigure(root, "live", capital=100_000)
    refused = at(runner, root, "promote", HYP_ID, "--live", "--json")
    assert refused.exit_code == Exit.APPROVAL
    assert state_of(runner, root, HYP_ID, 1) == "promotable", "nothing moved"

    # -- and takes it under a named operator's approval ----------------------------
    promoted = ok(runner, root, "promote", HYP_ID, "--live", "--as", OPERATOR)
    assert promoted["operator"] == OPERATOR
    assert state_of(runner, root, HYP_ID, 1) == "live"
    assert [one["strategy"] for one in stage_of(runner, root, "live")["strategies"]] == [HYP_ID]
    assert stage_of(runner, root, "paper")["strategies"] == []

    # -- a month of drift arrives as data, and the live gate fails on it -----------
    spec = write_spec(root, name="drift.yaml", **DRIFT)
    loaded_drift = ok(runner, root, "data", "load", "--loader", "synthetic", "--spec", spec)
    assert loaded_drift["datasets"][0]["span"] == ["2024-07-01", "2024-07-31"]
    caught_up = ok(runner, root, "portfolio", "deploy", "--stage", "live")
    assert caught_up["session"], "the live node replayed the days it had not seen"

    demoted = ok(runner, root, "monitor", "run")
    assert demoted["actions"] == ["demoted"]
    failing = next(one for one in demoted["outcomes"] if one["stage"] == "live" and one["gates"])
    drift = next(gate for gate in failing["gates"] if gate["id"] == "live_drift")
    assert drift["pass"] is False
    assert drift["evidence"]["realised"] < drift["evidence"]["floor"]
    assert state_of(runner, root, HYP_ID, 1) == "paper", "a demoted version returns to paper"
    assert stage_of(runner, root, "live")["strategies"] == []
    kinds = [entry["kind"] for entry in ok(runner, root, "inbox")["entries"]]
    assert kinds == ["promotable", "demoted"]

    # -- a filter attached to that sleeve gives the host a second version ----------
    filter_path = write_hypothesis(root, CEILING, base=FILTER_DRAFT)
    assert at(runner, root, "hyp", "add", filter_path).exit_code == Exit.OK
    mocked.scripted(root, classify=[FILTER_CLASSIFICATION])
    attached = ok(runner, root, "classify", FILTER_ID)
    assert attached["construct"]["id"] == "filter"
    assert attached["construct"]["host"] == HYP_ID
    assert attached["objective"]["id"] == "marginal_net_edge_bps"

    begun = ok(runner, root, "research", "begin", FILTER_ID)
    assert begun["baseline"]["status"] == "keep"
    assert at(runner, root, "research", "end", FILTER_ID).exit_code == Exit.OK

    second = ok(runner, root, "cert", "run", FILTER_ID)
    assert second["verdict"] == "pass", second
    assert second["objective"]["value"] > 0, "a filter earns its place or it does not compose"

    versions = ok(runner, root, "strat", "show", HYP_ID)["versions"]
    assert [one["version"] for one in versions] == [1, 2]
    assert [ref["hyp_id"] for ref in versions[1]["attached"]] == [FILTER_ID]
    assert versions[1]["sleeve"]["hyp_id"] == HYP_ID
    assert state_of(runner, root, HYP_ID, 2) == "paper"
    assert state_of(runner, root, HYP_ID, 1) == "retired", "a stage holds one version"
    assert [one["strategy"] for one in stage_of(runner, root, "paper")["strategies"]] == [HYP_ID]
    assert stage_of(runner, root, "paper")["strategies"][0]["version"] == 2
