"""The milestone end to end, on the workspace kanso ships: no credential, no network.

This is the sequence a person types on a fresh machine — scaffold the demo, load its
synthetic bars, freeze a snapshot, register the idea, classify it, then hand it to the
driver — run here as one test because what it proves is that the pieces fit, which no
one of them can prove alone.

Everything a model says comes from the demo's own scripted register, whose three answers
are a keep, a discard and a crash in that order; nothing here resolves a provider key or
opens a socket. What is asserted at the end is the operator's screen: the cards the loop
produced, the best it found, and the fact that every call it made along the way is in the
ledger `status` reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import at, payload, run

DEMO_ID = "demo_mr"
CARDS = 3
"""What the demo's scripted register has to say before it wraps around."""


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo workspace, scaffolded, loaded, snapshotted and registered for this module.

    Built once because the load and the freeze are the slow half and every test here
    wants exactly the same starting point. Registration is part of it so that each test
    stands on its own: what the sequence proves is what comes after.
    """
    runner = CliRunner()
    root = tmp_path_factory.mktemp("demo") / "ws"
    assert run(runner, "init", root, "--demo").exit_code == Exit.OK
    loaded = at(runner, root, "data", "load", "--loader", "synthetic", "--spec", root / "demo.yaml")
    assert loaded.exit_code == Exit.OK, loaded.stdout
    assert at(runner, root, "data", "snapshot").exit_code == Exit.OK
    registered = at(runner, root, "hyp", "add", root / "hypotheses" / DEMO_ID / "hypothesis.yaml")
    assert registered.exit_code == Exit.OK, registered.stdout
    return root


def calls(runner: CliRunner, root: Path) -> int:
    """How many model calls today's ledger holds, as `status` reports it."""
    return int(payload(at(runner, root, "status", "--json"))["spend_today"]["calls"])


def document(root: Path) -> dict[str, Any]:
    text = (root / "hypotheses" / DEMO_ID / "hypothesis.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return parsed


def test_the_demo_classifies_and_researches_itself_with_no_human_in_the_loop(
    runner: CliRunner, demo: Path
) -> None:
    assert at(runner, demo, "doctor", "--json").exit_code == Exit.OK
    before = calls(runner, demo)

    # -- classify -----------------------------------------------------------------
    classified = at(runner, demo, "classify", DEMO_ID, "--json")

    assert classified.exit_code == Exit.OK, classified.stdout
    answer = payload(classified)
    assert answer["construct"]["id"] == "sleeve"
    assert answer["runnable"] is True
    assert answer["objective"]["id"] == "net_edge_bps"
    assert [item["id"] for item in answer["constraints"]] == [
        "strategy_integrity",
        "min_trades",
        "max_drawdown",
    ]
    # The classification is in the file, and the registration is the bytes it left there.
    on_disk = document(demo)
    assert on_disk["construct"]["id"] == "sleeve"
    assert on_disk["objective"]["params"] == answer["objective"]["params"]
    shown = payload(at(runner, demo, "hyp", "show", DEMO_ID, "--json"))
    assert (shown["status"], shown["pinned"]) == ("classified", True)

    # -- research it, unattended ---------------------------------------------------
    driven = at(runner, demo, "research", "run", DEMO_ID, "--cards", CARDS, "--json")

    assert driven.exit_code == Exit.OK, driven.stdout
    outcome = payload(driven)
    assert outcome["proposed"] == CARDS
    # The demo's three scripted answers, in the order its own file documents.
    assert (outcome["keeps"], outcome["discards"], outcome["crashes"]) == (1, 1, 1)
    assert outcome["best_sha"] is not None
    assert outcome["best_metric"] > 0

    # -- and the screen an operator reads ------------------------------------------
    found = payload(at(runner, demo, "status", "--json"))
    assert found["cards_per_hour"] == CARDS + 1  # the baseline, then the three proposals
    entry = next(row for row in found["hypotheses"] if row["id"] == DEMO_ID)
    assert entry["status"] == "researching"
    assert entry["best_metric"] == outcome["best_metric"]
    assert entry["cards"] == CARDS + 1
    # One `classify` call and one `propose` per card, every one of them ledgered.
    assert found["spend_today"]["calls"] - before == CARDS + 1
    assert found["spend_today"]["by_lane"] == {"op": 0.0}
    working = next(row for row in found["lanes"] if row["id"] == DEMO_ID)
    assert working["best_sha"] == outcome["best_sha"]
    assert found["escalations"]["unread"] == 0
    assert found["baseline_failed"] == []

    # The run is still open, and its lane directory is still the interface.
    assert (demo / "runs" / "op" / DEMO_ID / "strategy.py").is_file()
    assert at(runner, demo, "research", "end", DEMO_ID).exit_code == Exit.OK


def test_the_demo_reaches_no_provider_and_needs_no_credential(demo: Path) -> None:
    """Every model on the demo's register is the shipped mock protocol."""
    register = yaml.safe_load((demo / "models.yaml").read_text(encoding="utf-8"))

    assert {entry["protocol"] for entry in register["models"]} == {"mock"}
    assert all("api_key_env" not in entry for entry in register["models"])
    tiers = {tier for entry in register["models"] for tier in entry["tier"]}
    assert tiers == {"cheap", "mid", "frontier"}


def test_every_command_of_the_demo_sequence_prints_one_object_under_json(
    runner: CliRunner, demo: Path
) -> None:
    """`--json` is the agent's contract: one document on standard output, always."""
    for args in (
        ("hyp", "show", DEMO_ID),
        ("data", "show"),
        ("models", "check"),
        ("research", "status"),
        ("status",),
    ):
        result = at(runner, demo, *args, "--json")
        assert result.exit_code == Exit.OK, (args, result.stdout)
        assert isinstance(json.loads(result.stdout), dict), args
