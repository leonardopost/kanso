"""A monitor's demotion in a child of the monitor: what the child records, and what comes back.

The workspace is the replay suite's synthetic saw-tooth, with a version promoted onto the live
stage and a month of data loaded after both stages last ran, so a demotion's paper redeploy is
a real node replaying real points — in a real child process. What is under test is where the
demotion is made and what crosses the boundary, not what a demotion does; `test_promote.py`
has that.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from kanso import strategy as strategies
from kanso.data import catalog
from kanso.errors import Exit, KansoError, PreconditionError
from kanso.nautilus import backtest
from kanso.portfolio import child, deploy, files, promote, records
from kanso.portfolio import demote as demote_here
from kanso.replay import record
from kanso.replay.parity import compare
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace, find
from tests.monitor.builders import plan, write_plan
from tests.portfolio.conftest import deployable, reconfigure
from tests.portfolio.test_promote import make_promotable
from tests.replay.conftest import bars, dataset, document

APRIL: Final = (date(2024, 4, 1), date(2024, 4, 30))
"""A month after the forward window both stages last replayed: what a demotion's redeploy runs."""

HELD: Final = document(
    benchmark={"hold": "first_leg"},
    objective={"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 0.5}},
)
"""The demo sleeve measured against a hold of its one instrument, so a redeploy runs the hold."""


def on_live(ws: Workspace, store: StateStore, doc: dict[str, Any] | None = None) -> str:
    """A version promoted onto the live stage, and a month of data neither stage has seen."""
    composed = deployable(ws, store, doc=doc)
    deploy(ws, store, "paper")
    reconfigure(ws, "live", capital=50_000.0)
    make_promotable(ws, store, composed.id)
    promote(ws, store, composed.id, operator="Ada Lovelace")
    catalog.write(ws, bars(APRIL), ref=dataset(span=APRIL), source="synthetic")
    return composed.id


def twin(ws: Workspace, tmp_path: Path) -> Workspace:
    """A copy of the workspace as it stands, to demote the same version a second way."""
    root = tmp_path / "twin"
    shutil.copytree(ws.root, root)
    return find(root)


def newest_session(store: StateStore, stage: str) -> str:
    row = store.connection.execute(
        "SELECT session_id FROM sessions WHERE mode = ? ORDER BY started_at DESC, session_id DESC",
        (stage,),
    ).fetchone()
    return str(row[0])


def recorded(store: StateStore, strategy_id: str) -> list[tuple[object, ...]]:
    """Every window the stages recorded for the strategy, without the session it is filed by."""
    return [
        (one.stage, one.version, one.capital, one.run, one.positions, one.benchmark)
        for one in records.stage_results(store, strategy_id=strategy_id)
    ]


def stages(ws: Workspace) -> list[tuple[str, bool, list[tuple[str, int, float]]]]:
    """What every stage holds, without the instant each version joined it."""
    portfolio = files.read(ws)
    return [
        (
            name,
            files.stage_of(portfolio, name).kill_switch,
            [
                (one.id, one.version, one.capital)
                for one in files.stage_of(portfolio, name).strategies
            ],
        )
        for name in ("paper", "live")
    ]


@pytest.mark.parametrize("doc", [None, HELD], ids=["sleeve", "benchmarked"])
def test_a_demotion_in_a_child_records_what_one_in_this_process_records(
    ws: Workspace, store: StateStore, tmp_path: Path, doc: dict[str, Any] | None
) -> None:
    """The same version demoted twice from the same workspace — once here, once in a child —
    leaves the same record: the strategy file, the stages, every window the nodes realised,
    the hold beside each, and the paper node's intents, compared as `kanso replay parity`
    compares them, at an instant tolerance of zero."""
    strategy_id = on_live(ws, store, doc)
    other = twin(ws, tmp_path)

    with StateStore(other.path("state.db")) as there:
        here_made = demote_here(other, there, strategy_id)
        here_records = recorded(there, strategy_id)
        here_session = newest_session(there, "paper")
        here_events = [event.kind for event in there.events()]
    made = child.demote_in_child(ws, store, strategy_id, 1)

    assert (made.state, made.redeployed, made.halted) == (
        here_made.state,
        tuple(one.stage for one in here_made.deployments),
        here_made.halted,
    )
    assert made.state == "paper" and made.redeployed == ("paper", "live")
    assert strategies.require(ws, strategy_id) == strategies.require(other, strategy_id)
    assert stages(ws) == stages(other)
    assert recorded(store, strategy_id) == here_records
    if doc is not None:
        assert all(row[5] is not None for row in here_records), "every window carries its hold"
    session = record.read(ws, newest_session(store, "paper"))
    assert session.to == APRIL[1] and session.released > 0, "the paper node replayed April"
    intents = record.intents_of(ws, session.session_id)
    assert intents, "the paper node traded April"
    assert compare(intents, record.intents_of(other, here_session), ts_ns=0) == (None, 0)
    assert [event.kind for event in store.events()] == here_events
    assert made.peak_mem_gb > 0 and made.wall_s > 0


def test_a_refusal_in_the_child_is_the_same_refusal_in_the_monitor(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    with pytest.raises(KansoError) as here:
        demote_here(ws, store, composed_strategy.id, 1)
    with pytest.raises(KansoError) as there:
        child.demote_in_child(ws, store, composed_strategy.id, 1)

    assert there.value.code == here.value.code == Exit.PRECONDITION
    assert (there.value.message, there.value.remedy) == (here.value.message, here.value.remedy)


def test_a_fault_in_the_child_comes_back_as_an_error_naming_it(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fault that is not a refusal is recorded against the version rather than ending the
    pass."""
    watching: list[int] = []
    monkeypatch.setattr(backtest, "end_with", watching.append)

    def tripped(*_: object, **__: object) -> Any:
        raise RuntimeError("the redeploy tripped")

    monkeypatch.setattr(child, "demote", tripped)
    report = tmp_path / child.REPORT

    assert child.main([str(ws.root), "demo_mr", "1", str(report), "4242"]) == 1

    answer = json.loads(report.read_text(encoding="utf-8"))
    assert answer == {
        "ok": False,
        "code": int(Exit.ERROR),
        "message": "demoting demo_mr@1 raised RuntimeError: the redeploy tripped",
        "remedy": "run `kanso demote demo_mr@1` by hand to see the traceback",
    }
    deadline = time.monotonic() + 5.0
    while not watching and time.monotonic() < deadline:
        time.sleep(0.01)
    assert watching == [4242], "the child watches the monitor that started it"


def test_a_child_that_ends_without_a_report_is_refused_with_its_last_words(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(child, "CHILD", "raise RuntimeError('no node today')")

    with pytest.raises(PreconditionError, match="ended without a report") as refused:
        child.demote_in_child(ws, store, "demo_mr", 1)

    assert refused.value.message.endswith("RuntimeError: no node today")
    assert refused.value.remedy == "run `kanso demote demo_mr@1` by hand to see why"


def test_a_child_that_said_nothing_is_said_to_have_said_nothing(tmp_path: Path) -> None:
    said = tmp_path / child.ERRORS
    said.write_text("\n")
    assert child._last_words(said) == "it said nothing"


def test_a_stop_leaves_a_demotion_to_finish(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card or a certification is killed at its watcher's first poll after a stop. A
    demotion is not: killed between the move and the redeploys it would leave the version off
    the live stage with no escalation saying so, and in the monitor's own process nothing
    stopped one either."""
    strategy_id = on_live(ws, store)
    watch = backtest._watch

    def stopped_once_running(process: Any, *args: Any) -> tuple[str | None, float]:
        backtest.interrupt()
        return watch(process, *args)

    monkeypatch.setattr(backtest, "_watch", stopped_once_running)
    try:
        made = child.demote_in_child(ws, store, strategy_id, 1)
    finally:
        backtest.resume()

    assert made.state == "paper" and made.redeployed == ("paper", "live")
    assert strategies.require(ws, strategy_id).latest().state == "paper"


MONITOR_PASS: Final = r"""
import sys
from pathlib import Path

from kanso.monitor import run_once
from kanso.nautilus import backtest
from kanso.state import StateStore
from kanso.workspace import find

ws = find(Path(sys.argv[1]))
with StateStore(ws.path("state.db")) as store:
    before = backtest._own_peak_gb()
    outcomes = run_once(ws, store)
    after = backtest._own_peak_gb()
actions = [action for outcome in outcomes for action in outcome.actions]
print("passed", ",".join(actions), before, after, flush=True)
"""
"""A monitor's pass, made in a fresh process the way the daemon's monitor makes one."""

DEMOTION_FOOTPRINT_GB: Final = 0.017
"""How far a pass that demotes may move the monitor's own peak resident memory: half of what it
cost the monitor to demote in its own process. Measured on this workspace, a month of new daily
bars, in fresh processes: 35.2 MB when the monitor demoted itself, 1.0 to 1.1 MB now that a child
demotes — what a pass that only judges moves it by, 0 to 1.1 MB."""


def test_a_monitor_that_demoted_holds_none_of_what_the_redeploys_cost(
    ws: Workspace, store: StateStore
) -> None:
    """A live version whose live gate fails, demoted by a monitor pass made in a fresh process:
    the redeploys run a trading node over the month the paper stage has not replayed, and the
    monitor that asked for them keeps none of it."""
    strategy_id = on_live(ws, store)
    write_plan(ws, plan())
    held = strategy_files.require(ws, strategy_id)
    latest = held.latest()
    unreachable = latest.expectation.model_copy(update={"ci90": (1e9, 2e9)})
    strategy_files.write(
        ws,
        held.model_copy(
            update={
                "versions": [
                    *held.versions[:-1],
                    latest.model_copy(update={"expectation": unreachable}),
                ]
            }
        ),
    )

    monitor = subprocess.run(
        [sys.executable, "-c", MONITOR_PASS, str(ws.root)],
        cwd=str(ws.root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300.0,
        check=False,
    )

    said = monitor.stdout.splitlines()
    assert monitor.returncode == 0 and said and said[-1].startswith("passed "), (
        monitor.stdout + monitor.stderr
    )
    _, actions, before, after = said[-1].split()
    assert actions == "demoted"
    assert strategies.require(ws, strategy_id).latest().state == "paper"
    assert record.read(ws, newest_session(store, "paper")).released > 0, "a node ran April"
    assert float(after) - float(before) < DEMOTION_FOOTPRINT_GB, (
        f"the monitor's own peak went from {before} GB to {after} GB"
    )
