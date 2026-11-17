"""`kanso hyp explore`: a draft hypothesis from a parent's research, from the command line.

The explorer itself is proved in its own slice; what is asserted here is the command around
it — that the object printed under `--json` names what was written, that the draft it
writes is one `hyp validate` admits and `hyp add` registers as a draft, that with no
model to call the step exits 2 rather than writing anything of its own, and that an attempt
by hand that fails starts a lane's count of stalls over just as a lane's own does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.research import explore
from kanso.state import StateStore

from . import mocked
from .conftest import DRAFT, HYP_ID, at, payload

CANDIDATE_ID = "demo_dip"

DIP = '''from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 1_000.0


class Strategy(KansoStrategy):
    """Buys a close under the lowest of the six before it, and sells back above their mean."""

    config_cls = Config

    def on_start(self) -> None:
        self.closes: list[float] = []
        self.long = False

    def on_bar(self, bar) -> None:
        close = float(bar.close)
        before = self.closes[-6:]
        self.closes.append(close)
        if len(before) < 6:
            return
        if close < min(before) and not self.long:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.long = True
        elif close > sum(before) / 6 and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''


def answer() -> dict[str, Any]:
    """The explorer's scripted candidate: the parent's file, mechanism included, a new idea.

    Its seed trades. The parent's baseline is the flat seed, which places no order, so the
    parent's books hold the empty one, and a seed that placed no order would be refused as
    that card when it is measured as one more card of the parent before it is written.
    """
    return {
        "id": CANDIDATE_ID,
        "hypothesis_yaml": yaml.safe_dump(
            {
                **DRAFT,
                "id": CANDIDATE_ID,
                "title": "Demo: hourly dip-buy on a synthetic OU series",
                "thesis": "A close under the last six hours' low reverts to their mean.",
                "mechanism": DRAFT["mechanism"],
                "derived_from": [HYP_ID],
            },
            sort_keys=False,
        ),
        "program_md": "# program\n\nEdit strategy.py; keep the dip-buy.\n",
        "strategy_py": DIP,
        "rationale": "the parent only faded deviations from a mean; this buys a new six-hour low",
        "tags": ["signal_mean_reversion"],
    }


def researched(runner: CliRunner, root: Path) -> None:
    """The parent after a baseline and one proposed card, and an explorer to call."""
    assert at(runner, root, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK
    mocked.write_script(root, "frontier", {"certify_plan": [mocked.PLAN], "explore": [answer()]})


def test_explore_writes_a_draft_that_validate_admits_and_add_registers(
    runner: CliRunner, mocked_ws: Path
) -> None:
    researched(runner, mocked_ws)

    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    written = payload(result)
    path = mocked_ws / "hypotheses" / CANDIDATE_ID / "hypothesis.yaml"
    assert (written["parent"], written["id"]) == (HYP_ID, CANDIDATE_ID)
    assert Path(written["dir"]) == path.parent
    assert written["files"] == ["hypothesis.yaml", "program.md", "strategy.py"]
    assert written["tags"] == ["signal_mean_reversion"]
    seed = written["seed"]
    assert {"metric", "se", "trades", "objective", "window", "books"} <= set(seed)
    assert seed["trades"] > 0 and seed["objective"] == "net_edge_bps"

    inbox = payload(at(runner, mocked_ws, "inbox", "--json"))
    assert [(entry["kind"], entry["subject"]) for entry in inbox["entries"]] == [
        ("explored", CANDIDATE_ID)
    ]
    assert "as its card the seed scored" in inbox["entries"][0]["summary"]
    assert at(runner, mocked_ws, "hyp", "validate", path).exit_code == Exit.OK
    added = at(runner, mocked_ws, "hyp", "add", path, "--json")
    assert added.exit_code == Exit.OK, added.stdout
    assert payload(added)["status"] == "draft"


def test_explore_reads_as_what_was_written_and_the_next_command(
    runner: CliRunner, mocked_ws: Path
) -> None:
    researched(runner, mocked_ws)

    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID)

    assert result.exit_code == Exit.OK, result.stdout
    assert f"{CANDIDATE_ID} · from {HYP_ID}" in result.stdout
    assert "the parent only faded deviations" in result.stdout
    [seed] = [line for line in result.stdout.splitlines() if line.lstrip().startswith("seed")]
    assert f"trades · net_edge_bps as a card of {HYP_ID} over " in seed
    assert "book(s) compared" in seed
    assert f"kanso hyp validate {mocked_ws / 'hypotheses' / CANDIDATE_ID}" in result.stdout


def test_the_inbox_shows_a_draft_s_whole_rationale_beneath_its_entry(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """The summary leads with the seed's score and is capped at a line, so a rationale of the
    240 characters the schema allows is cut there — and a lane's draft has no `hyp explore`
    output to read it in. The inbox prints it whole, off the event that names the entry."""
    whole = ("the parent only faded deviations from a mean and never bought a fresh low; " * 4)[
        :240
    ].rstrip()
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK
    long = {**answer(), "rationale": whole}
    mocked.write_script(mocked_ws, "frontier", {"certify_plan": [mocked.PLAN], "explore": [long]})
    assert at(runner, mocked_ws, "hyp", "explore", HYP_ID).exit_code == Exit.OK

    [entry] = payload(at(runner, mocked_ws, "inbox", "--json"))["entries"]
    listed = at(runner, mocked_ws, "inbox")

    assert entry["kind"] == "explored" and whole not in entry["summary"]
    assert entry["rationale"] == whole
    assert listed.exit_code == Exit.OK
    lines = listed.stdout.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith(entry["id"]))
    assert lines[head + 1].strip() == f"rationale: {whole}"
    assert lines[head + 2].strip() == entry["actions"]


def test_explore_with_no_model_exits_2_and_writes_nothing(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    (mocked_ws / "models.yaml").unlink()

    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "models.yaml" in payload(result)["error"]
    assert sorted(path.name for path in (mocked_ws / "hypotheses").iterdir()) == [HYP_ID]


def test_explore_of_a_hypothesis_never_researched_exits_2(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert payload(result)["remedy"] == f"run `kanso research run {HYP_ID}`"


def test_a_failed_attempt_by_hand_starts_the_count_of_stalls_over(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """The count a lane reads is the parent's events, and the operator's attempt is one."""
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK
    mocked.write_script(mocked_ws, "frontier", {"explore": [{}]})
    with StateStore(mocked_ws / "state.db") as store:
        for _ in range(2):
            store.event("stalled", HYP_ID, {"best_sha": "a" * 64, "verdict": None})
        assert explore.due(store, HYP_ID, 2) is True

    result = at(runner, mocked_ws, "hyp", "explore", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "explore: no model answered" in payload(result)["error"]
    with StateStore(mocked_ws / "state.db") as store:
        [failed] = [e for e in store.events(subject=HYP_ID) if e.kind == explore.EXPLORED_FAILED]
        assert failed.detail["lane"] == "op"
        assert failed.detail["error"] == payload(result)["error"]
        assert explore.due(store, HYP_ID, 2) is False
