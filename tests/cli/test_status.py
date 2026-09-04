"""`kanso status`: the one screen that says how the workbench is doing.

Six facts, and each is asserted here for the reason it is on the screen at all: the lanes
say whether research is happening, the card rate says how fast, the best metric says
whether the speed bought anything, the spend says what it cost, the inbox says what is
waiting for a person, and a failed baseline is the one failure that is silent everywhere
else. Nothing here writes, so the last test runs it against a workspace with a run in it
and asserts the state is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.inbox import escalate
from kanso.state import StateStore
from kanso.workspace import find

from . import mocked
from .conftest import HYP_ID, at, edit, payload


def test_a_fresh_workspace_reports_every_field_with_nothing_in_it(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "status", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    found = payload(result)
    assert found["daemon"] == {"running": False, "pid": None}
    assert found["cards_per_hour"] == 0
    assert found["hypotheses"] == []
    assert found["spend_today"]["calls"] == 0
    assert found["escalations"] == {"unread": 0, "entries": []}
    assert found["baseline_failed"] == []
    # `init` writes an envelope, so the lanes a daemon would use are already known.
    assert [row["lane"] for row in found["lanes"]]


def test_a_run_shows_up_on_its_lane_with_its_cards_and_its_best(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    found = payload(at(runner, mocked_ws, "status", "--json"))

    working = [row for row in found["lanes"] if row["id"] == HYP_ID]
    assert len(working) == 1
    lane_row = working[0]
    # The interactive lane is not in the envelope's plan, and an operator's run is real.
    assert lane_row["lane"] == "op"
    assert lane_row["planned"] is False
    assert lane_row["cards"] >= 2
    assert lane_row["best_metric"] is not None


def test_the_card_rate_counts_what_the_last_hour_produced(
    runner: CliRunner, mocked_ws: Path
) -> None:
    before = payload(at(runner, mocked_ws, "status", "--json"))["cards_per_hour"]

    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    after = payload(at(runner, mocked_ws, "status", "--json"))["cards_per_hour"]
    # The baseline and the proposed card, both recorded just now.
    assert after - before == 2


def test_the_best_metric_is_reported_per_hypothesis(runner: CliRunner, mocked_ws: Path) -> None:
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    found = payload(at(runner, mocked_ws, "status", "--json"))

    assert [row["id"] for row in found["hypotheses"]] == [HYP_ID]
    entry = found["hypotheses"][0]
    assert entry["status"] == "researching"
    assert entry["best_sha"] is not None
    assert entry["best_metric"] is not None
    assert entry["cards"] >= 2


def test_today_s_spend_is_what_the_ledger_holds_broken_out_by_lane(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK

    spend = payload(at(runner, mocked_ws, "status", "--json"))["spend_today"]

    assert spend["calls"] >= 1
    assert spend["tokens_in"] > 0
    assert "op" in spend["by_lane"]


def test_unread_escalations_are_listed_and_counted(runner: CliRunner, mocked_ws: Path) -> None:
    ws = find(mocked_ws)
    with StateStore(ws.path("state.db")) as store:
        escalate(ws, store, "misaligned", HYP_ID, "it wandered off the thesis")

    found = payload(at(runner, mocked_ws, "status", "--json"))

    assert found["escalations"]["unread"] == 1
    assert found["escalations"]["entries"][0]["kind"] == "misaligned"
    assert "1 unread" in at(runner, mocked_ws, "status").stdout


def test_a_hypothesis_whose_baseline_will_not_run_is_named(
    runner: CliRunner, mocked_ws: Path
) -> None:
    # The workspace copy is what a run is opened from, and this one raises on its first bar.
    (mocked_ws / "hypotheses" / HYP_ID / "strategy.py").write_text(
        mocked.SEED.replace('mode = "flat"', 'mode = "boom"'), encoding="utf-8"
    )
    assert (
        at(
            runner, mocked_ws, "hyp", "add", mocked_ws / "hypotheses" / HYP_ID / "hypothesis.yaml"
        ).exit_code
        == Exit.OK
    )
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code != Exit.OK

    found = payload(at(runner, mocked_ws, "status", "--json"))

    assert [row["id"] for row in found["baseline_failed"]] == [HYP_ID]
    assert HYP_ID in at(runner, mocked_ws, "status").stdout


def test_a_baseline_failure_a_later_run_cleared_is_not_reported(
    runner: CliRunner, mocked_ws: Path
) -> None:
    document = mocked_ws / "hypotheses" / HYP_ID / "hypothesis.yaml"
    (mocked_ws / "hypotheses" / HYP_ID / "strategy.py").write_text(
        mocked.SEED.replace('mode = "flat"', 'mode = "boom"'), encoding="utf-8"
    )
    assert at(runner, mocked_ws, "hyp", "add", document).exit_code == Exit.OK
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code != Exit.OK
    (mocked_ws / "hypotheses" / HYP_ID / "strategy.py").write_text(mocked.SEED, encoding="utf-8")
    assert at(runner, mocked_ws, "hyp", "add", document).exit_code == Exit.OK
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    assert payload(at(runner, mocked_ws, "status", "--json"))["baseline_failed"] == []


def test_status_reads_as_one_screen_and_writes_nothing(runner: CliRunner, mocked_ws: Path) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    edited = edit(mocked_ws, mocked.SEED)
    before = edited.read_bytes()

    result = at(runner, mocked_ws, "status")

    assert result.exit_code == Exit.OK
    for label in ("workspace", "daemon", "lanes", "cards/h", "best", "spend", "inbox"):
        assert label in result.stdout
    assert edited.read_bytes() == before


def test_a_workspace_with_no_envelope_says_where_lanes_come_from(
    runner: CliRunner, mocked_ws: Path
) -> None:
    (mocked_ws / "envelope.yaml").unlink()

    result = at(runner, mocked_ws, "status")

    assert result.exit_code == Exit.OK
    assert "kanso env detect" in result.stdout
    assert payload(at(runner, mocked_ws, "status", "--json"))["lanes"] == []
