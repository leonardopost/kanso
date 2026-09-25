"""`kanso research start`, `stop`, `status` and `queue add`: the daemon from the outside.

The daemon's own slice proves the lock, the workers and what a stop keeps. What is
asserted here is the command surface an operator and an agent actually use: that starting
returns a pid and a lane list, that stopping leaves the active run and its lane directory
exactly where they were, that `status` reports the three shas of every active run, and
that the queue is served by priority and then by arrival.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit, KansoError
from kanso.research import daemon

from .conftest import HYP_ID, at, lane, payload


@pytest.fixture
def stopped(mocked_ws: Path) -> Iterator[Path]:
    """A workspace whose daemon is stopped however the test leaves it."""
    yield mocked_ws
    from kanso.workspace import find

    ws = find(mocked_ws)
    if daemon.pid_of(ws) is not None:  # pragma: no cover - only when a test failed early
        with contextlib.suppress(KansoError, OSError):
            daemon.stop(ws)


def test_start_reports_the_pid_and_the_lanes_and_stop_keeps_the_run(
    runner: CliRunner, stopped: Path
) -> None:
    assert at(runner, stopped, "research", "begin", HYP_ID).exit_code == Exit.OK
    edited = lane(stopped) / "strategy.py"
    edited.write_text("# the operator is mid-edit\n", encoding="utf-8")

    started = at(runner, stopped, "research", "start", "--json")

    assert started.exit_code == Exit.OK, started.stdout
    running = payload(started)
    assert running["running"] is True
    assert isinstance(running["pid"], int)
    assert running["lanes"]

    ended = at(runner, stopped, "research", "stop", "--json")

    assert ended.exit_code == Exit.OK, ended.stdout
    assert payload(ended) == {"running": False, "pid": running["pid"]}
    # Nothing is ended and nothing is cleaned up, so the next start resumes.
    assert edited.read_text(encoding="utf-8") == "# the operator is mid-edit\n"


def test_start_reads_as_the_pid_the_lanes_and_the_log(runner: CliRunner, stopped: Path) -> None:
    result = at(runner, stopped, "research", "start")

    assert result.exit_code == Exit.OK
    assert "started" in result.stdout
    assert "kanso research status" in result.stdout
    assert at(runner, stopped, "research", "stop").exit_code == Exit.OK


def test_stopping_nothing_is_a_precondition_failure(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "research", "stop", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no daemon" in payload(result)["error"]


def test_status_with_nothing_running_still_reports_the_lanes(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "research", "status", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    found = payload(result)
    assert found["running"] is False
    assert found["pid"] is None
    assert found["runs"] == []
    assert found["queue"] == []
    assert found["restarts"] == []
    assert "restarts   none" in at(runner, mocked_ws, "research", "status").stdout


def test_status_lists_every_child_the_running_daemon_started_again(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """What an operator reads after the kernel took a lane: which child, how often, how the
    newest death went and how long it waited to come back."""
    from kanso.state import StateStore
    from kanso.workspace import find

    ws = find(mocked_ws)
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    daemon.pid_path(ws).parent.mkdir(parents=True, exist_ok=True)
    daemon.pid_path(ws).write_text(f"{running.pid}\n", encoding="utf-8")
    killed = {"pid": 7, "exit": None, "signal": "SIGKILL", "lived_s": 412.3, "restart_in_s": 0.0}
    exited = {"pid": 8, "exit": 1, "signal": None, "lived_s": 3.0, "restart_in_s": 2.0}
    try:
        with StateStore(ws.path("state.db")) as store:
            lane = {"lane": "l1", **killed, "daemon": running.pid, "run": HYP_ID, "put_back": []}
            store.event(daemon.LANE_DIED, "l1", lane)
            store.event(daemon.MONITOR_DIED, daemon.MONITOR, {**exited, "daemon": running.pid})
        found = payload(at(runner, mocked_ws, "research", "status", "--json"))
        human = at(runner, mocked_ws, "research", "status").stdout
    finally:
        daemon.pid_path(ws).unlink()
        running.kill()
        running.wait()

    assert [
        (entry["child"], entry["deaths"], entry["exit"], entry["signal"])
        for entry in found["restarts"]
    ] == [("l1", 1, None, "SIGKILL"), ("monitor", 1, 1, None)]
    assert "restarts   2 since the daemon started" in human
    assert "killed by SIGKILL after 412s · started again at once" in human
    assert "exit 1 after 3s · started again after 2s" in human


def test_status_reports_a_run_with_its_three_shas(runner: CliRunner, mocked_ws: Path) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    found = payload(at(runner, mocked_ws, "research", "status", "--json"))

    assert [run["id"] for run in found["runs"]] == [HYP_ID]
    entry = found["runs"][0]
    assert entry["lane"] == "op"
    assert entry["lane_sha"] is not None
    assert entry["base_sha"] is not None


def test_status_reads_as_the_daemon_the_runs_and_the_queue(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    assert at(runner, mocked_ws, "research", "queue", "add", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "research", "status")

    assert result.exit_code == Exit.OK
    assert "stopped" in result.stdout
    assert "1 active" in result.stdout
    assert "1 waiting" in result.stdout


def test_a_lane_that_lost_its_strategy_is_reported_as_gone(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    (lane(mocked_ws) / "strategy.py").unlink()

    found = payload(at(runner, mocked_ws, "research", "status", "--json"))

    assert found["runs"][0]["lane_sha"] is None
    assert "gone" in at(runner, mocked_ws, "research", "status").stdout


# --- the queue ---------------------------------------------------------------


def test_queue_add_reports_the_place_it_took(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "research", "queue", "add", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    item = payload(result)
    assert item["id"] == HYP_ID
    assert item["priority"] == 0
    assert (item["place"], item["waiting"]) == (1, 1)


def test_a_higher_priority_is_served_first(runner: CliRunner, mocked_ws: Path) -> None:
    from .conftest import HYPOTHESIS, classify, write_hypothesis

    other = "demo_mr2"
    path = write_hypothesis(mocked_ws, base={**HYPOTHESIS, "id": other})
    assert at(runner, mocked_ws, "hyp", "add", path).exit_code == Exit.OK
    classify(mocked_ws, other)
    assert at(runner, mocked_ws, "research", "queue", "add", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "research", "queue", "add", other, "--priority", 5, "--json")

    assert payload(result)["place"] == 1
    assert payload(result)["waiting"] == 2


def test_queue_add_reads_as_the_place_and_the_priority(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "research", "queue", "add", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "priority 0" in result.stdout
    assert "1 of 1" in result.stdout


def test_queueing_an_unknown_hypothesis_is_refused(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "research", "queue", "add", "nonesuch", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "nonesuch" in payload(result)["error"]


def test_queue_remove_takes_the_hypothesis_out_and_says_where_from(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "queue", "add", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "research", "queue", "remove", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result) == {"id": HYP_ID, "removed_from": "queue", "waiting": 0}
    again = at(runner, mocked_ws, "research", "queue", "remove", HYP_ID, "--json")
    assert again.exit_code == Exit.PRECONDITION
    assert "not in the queue" in payload(again)["error"]


def test_queue_remove_reads_as_where_it_came_from(runner: CliRunner, mocked_ws: Path) -> None:
    assert at(runner, mocked_ws, "research", "queue", "add", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "research", "queue", "remove", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "from the queue" in result.stdout
