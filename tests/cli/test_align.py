"""`kanso align check`: asking on demand whether a run still tests its own idea.

Drift is not an error, so the command does not exit like one. What these tests assert is
that a drift is reported with exit 0 having already been repaired — the lane rewound, the
escalation written — and that an operator reading the object can tell the two verdicts
apart without interpreting an exit code.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

from . import mocked
from .conftest import HYP_ID, at, edit, lane, payload


def test_a_run_that_still_tests_its_thesis_is_aligned(runner: CliRunner, mocked_ws: Path) -> None:
    mocked.scripted(mocked_ws, align_check=[mocked.ALIGNED])
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "align", "check", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    checked = payload(result)
    assert checked["aligned"] is True
    assert checked["reason"] is None
    assert checked["id"] == HYP_ID
    assert checked["lane"] == "op"


def test_a_drift_is_reported_with_exit_zero_and_the_lane_rewound(
    runner: CliRunner, mocked_ws: Path
) -> None:
    mocked.scripted(mocked_ws, align_check=[mocked.DRIFTED])
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1).exit_code == Exit.OK
    wandered = (lane(mocked_ws) / "strategy.py").read_bytes()

    result = at(runner, mocked_ws, "align", "check", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    checked = payload(result)
    assert checked["aligned"] is False
    assert checked["reason"] == mocked.DRIFTED["reason"]
    # Rewound: the lane holds something other than what the check found there.
    assert (lane(mocked_ws) / "strategy.py").read_bytes() != wandered
    # And the escalation is where every other one lands.
    inbox = mocked_ws / "escalations" / "inbox.md"
    assert "misaligned" in inbox.read_text(encoding="utf-8")


def test_the_deterministic_checks_come_first_and_cost_no_call(
    runner: CliRunner, mocked_ws: Path
) -> None:
    # No `align_check` script at all: a model asked here would answer `{}` and refuse.
    mocked.scripted(mocked_ws)
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    edit(mocked_ws, mocked.SEED + '\nOTHER = "NOPE.XX"\n')

    result = at(runner, mocked_ws, "align", "check", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result)["aligned"] is False


def test_align_check_reads_as_the_verdict_and_the_count(runner: CliRunner, mocked_ws: Path) -> None:
    mocked.scripted(mocked_ws, align_check=[mocked.ALIGNED])
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "align", "check", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "yes" in result.stdout
    assert "checked" in result.stdout


def test_a_drift_reads_as_the_reason_and_where_to_look(runner: CliRunner, mocked_ws: Path) -> None:
    mocked.scripted(mocked_ws, align_check=[mocked.DRIFTED])
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "align", "check", HYP_ID)

    assert result.exit_code == Exit.OK
    assert str(mocked.DRIFTED["reason"]) in result.stdout
    assert "kanso inbox" in result.stdout


def test_checking_a_hypothesis_with_no_run_is_refused(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "align", "check", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert HYP_ID in payload(result)["error"]
