"""The milestone end to end, on the workspace kanso ships: no credential, no network.

This is the sequence a person types on a fresh machine — scaffold the demo, load its
synthetic bars, freeze a snapshot, register the idea, classify it, then hand it to the
driver — run here as one test because what it proves is that the pieces fit, which no
one of them can prove alone. One step differs from what a person runs: the research runs
under a card floor no operator can set (`CARD_FLOOR_S`), so whether the shipped budget fits
the demo's cards on a given host is row 138 of `docs/backlog.md`, not this test's to say.

Everything a model says comes from the demo's own scripted register, whose three answers
are a keep, a discard and a crash in that order; nothing here resolves a provider key or
opens a socket. What is asserted at the end is the operator's screen: the cards the loop
produced, the best it found, and the fact that every call it made along the way is in the
ledger `status` reports.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.research import loop, records
from kanso.schemas import Card
from kanso.state import StateStore

from .conftest import at, payload, run

DEMO_ID = "demo_mr"
CARDS = 3
"""What the demo's scripted register has to say before it wraps around."""

CARD_FLOOR_S = 600.0
"""The card budget's floor while the demo researches: ten minutes.

The budget is `max(MIN_CARD_BUDGET_S, HEADROOM × baseline wall)` (`research/loop.py`), and
which term is larger, and whether a card fits under it, depends on how fast the host runs.
This test asserts the demo's flow, not its budget: `tests/research/test_loop.py` pins the
formula and its two constants, and `tests/nautilus/backtest/test_subprocess.py` the kill. So
it lifts the floor to 600 s, about eight times the slowest demo card measured (74.9 s, the
discard, on an Apple M2's efficiency cores under pytest-cov beside another test run), and
the 3× term binds only past a 200 s baseline, over five times the 35.8 s measured there. A
slow runner cannot turn the scripted discard into a budget crash, and a card that hangs is
killed in ten minutes.
"""


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo workspace, scaffolded, loaded, resolved, snapshotted and registered.

    Built once because the load and the freeze are the slow half and every test here
    wants exactly the same starting point. Registration is part of it so that each test
    stands on its own: what the sequence proves is what comes after.
    """
    runner = CliRunner()
    root = tmp_path_factory.mktemp("demo") / "ws"
    assert run(runner, "init", root, "--demo").exit_code == Exit.OK
    loaded = at(runner, root, "data", "load", "--loader", "synthetic", "--spec", root / "demo.yaml")
    assert loaded.exit_code == Exit.OK, loaded.stdout
    resolve = ("data", "instruments", "resolve", "DEMO.SIM", "--as-of", "2024-01-02")
    assert at(runner, root, *resolve).exit_code == Exit.OK
    assert at(runner, root, "data", "snapshot").exit_code == Exit.OK
    registered = at(runner, root, "hyp", "add", root / "hypotheses" / DEMO_ID / "hypothesis.yaml")
    assert registered.exit_code == Exit.OK, registered.stdout
    return root


def calls(runner: CliRunner, root: Path) -> int:
    """How many model calls today's ledger holds, as `status` reports it."""
    return int(payload(at(runner, root, "status", "--json"))["spend_today"]["calls"])


def ledger(root: Path) -> tuple[list[Card], str]:
    """The hypothesis's cards, oldest first, and the log an operator reads.

    The log is `results.tsv` followed by the tail every crashed card recorded, so an
    assertion that carries it as its message says which card went wrong and why: a card
    killed for its time budget and one that raised read the same in the counts alone.
    """
    log = (root / "hypotheses" / DEMO_ID / "results.tsv").read_text(encoding="utf-8")
    with StateStore(root / "state.db") as store:
        carded = records.cards_of(store, DEMO_ID)
    tails = [f"{card.sha7} crash_tail: {card.crash_tail}" for card in carded if card.crash_tail]
    return carded, "\n".join([log, *tails])


def scripted(root: Path) -> list[str]:
    """The descriptions of the demo's scripted proposals, in the order the mock answers them."""
    script = yaml.safe_load((root / "mock" / "responses.yaml").read_text(encoding="utf-8"))
    return [str(answer["desc"]) for answer in script["propose"]]


def document(root: Path) -> dict[str, Any]:
    text = (root / "hypotheses" / DEMO_ID / "hypothesis.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return parsed


def test_the_demo_classifies_and_researches_itself_with_no_human_in_the_loop(
    runner: CliRunner, demo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `research run` begins the run in this process and strikes its card budget there, so this
    # floor is the one every card of the run is watched against (`CARD_FLOOR_S`).
    monkeypatch.setattr(loop, "MIN_CARD_BUDGET_S", CARD_FLOOR_S)
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
    carded, log = ledger(demo)
    with StateStore(demo / "state.db") as store:
        opened = records.active(store, DEMO_ID)
    # The lifted floor reached the run, so no host's speed decides which card is a crash.
    assert opened is not None and opened.card_budget_s >= CARD_FLOOR_S, log
    assert outcome["proposed"] == CARDS, log
    # The demo's three scripted answers, in the order its own file documents.
    assert (outcome["keeps"], outcome["discards"], outcome["crashes"]) == (1, 1, 1), log
    keep, discard, crash = scripted(demo)
    assert [(card.status, card.desc) for card in carded] == [
        ("discard", "baseline"),
        ("keep", keep),
        ("discard", discard),
        ("crash", crash),
    ], log
    # The crash is the one the script plants, not a card the host was too slow to finish.
    assert "rolling_sigma" in (carded[-1].crash_tail or ""), log
    # The floor is lifted here, but an operator runs the demo under the shipped one: three times
    # the baseline and never under 60 s. The demo's baseline is the stub, which trades nothing,
    # and past it a card's wall grows with its fills, so a discard trading far more than the keep
    # fits only under the floor and is killed on a host slow enough for the floor to stop binding
    # (`docs/backlog.md` row 138: 2,445 trades to the keep's 1,003 took four times the baseline).
    # Holding the script's discard to a fifth more than the keep's fills keeps its cost near the
    # keep's on the operator's host; it narrows that margin, it guarantees none. The trade count
    # is the proxy because it is deterministic and a wall time is not.
    _, kept, discarded, _ = carded
    assert discarded.n_trades <= 1.2 * kept.n_trades, log
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

    # -- and again, as the README says: the script wraps with nothing new to propose ------
    again = at(runner, demo, "research", "run", DEMO_ID, "--cards", CARDS, "--json")

    assert again.exit_code == Exit.OK, again.stdout
    repeated = payload(again)
    assert (repeated["proposed"], repeated["missed"], repeated["redundant"]) == (CARDS, CARDS, 0)
    assert (repeated["keeps"], repeated["discards"], repeated["crashes"]) == (0, 0, 0)
    assert repeated["best_sha"] == outcome["best_sha"]
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


def test_the_demo_screen_finds_the_lead_it_plants(
    runner: CliRunner, demo: Path, tmp_path: Path
) -> None:
    """LAGD takes 0.6 of DEMO's shock a minute late; the shipped screen reads it back.

    A known answer, run on every commit: the lead at one minute and nowhere else, and a
    response that clears the round trip, with the data step the screen does itself.
    """
    root = tmp_path / "ws"
    shutil.copytree(demo, root, symlinks=True)
    spec = root / "demo_lag.yaml"
    assert at(runner, root, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code == 0
    resolve = ("data", "instruments", "resolve", "LAGD.SIM", "--as-of", "2024-01-02")
    assert at(runner, root, *resolve).exit_code == Exit.OK

    result = at(runner, root, "screen", "run", root / "screens/demo_lag/screen.yaml", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert document["verdict"]["worth_a_lane"] is True
    shown = at(runner, root, "screen", "show", "demo_lag", "--json")
    assert shown.exit_code == Exit.OK
    lead = payload(
        at(runner, root, "screen", "show", "demo_lag", "--cell", "lead_lag/demo>lagd/1m", "--json")
    )
    assert lead["judged"] == "pass" and abs(lead["mean"] - 0.6) < 0.05
    for lag in ("-1m", "2m", "5m"):
        other = payload(
            at(
                runner,
                root,
                "screen",
                "show",
                "demo_lag",
                "--cell",
                f"lead_lag/demo>lagd/{lag}",
                "--json",
            )
        )
        assert abs(other["mean"]) < 0.05, lag
