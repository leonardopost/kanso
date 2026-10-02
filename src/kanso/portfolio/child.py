"""A monitor's demotion, run in a child of the monitor.

The monitor is a long-lived process, and a demotion is the heaviest thing it is ever asked
to do: the version moves off the live stage, and every stage whose switch is off is
redeployed — a trading node built and run over everything the catalog holds that the stage
has not replayed, beside the hold a benchmark objective differences against. Run in the
monitor itself, none of that was given back. Measured in a fresh monitor process on the
replay suite's saw-tooth, one pass that demoted a live version and redeployed the paper
stage over a month of new daily bars: the monitor's own peak rose by 35.2 MB, and by 40.4 MB
over nine months, where a pass that only judged moved it by about 1 MB.

So the monitor demotes in a child it watches (`backtest.watched`): started in a session of
its own, ending itself when the monitor that started it is gone (`backtest.end_with`), and
what the redeploys cost goes when it exits. The child opens the workspace, its catalog and
its store as the monitor does, under the monitor's environment, and demotes exactly as
`kanso demote` does — `kanso.portfolio.promote.demote`, the one function — writing the
strategy file, the portfolio file, the sessions, the stage records and every event itself.
It reports what it did, or the refusal, which the monitor raises as an error of the same
code with the same message and remedy; a fault that is not a refusal comes back as one
naming what raised, so a pass records it against the version and goes on.

Nothing bounds it, and a stop does not kill it. A demotion takes a failing version off real
capital, so neither a memory share nor a wall time may refuse one. A stop that lands while it
runs leaves it to finish, as a demotion made in the monitor's own process finished — nothing
in a node run asks whether the process was told to stop — because one killed between the
move and the redeploys would leave the version off the live stage with no escalation saying
so. A monitor still busy when the supervisor's grace runs out is killed, and the child then
ends itself, which is what a demotion in the killed monitor did with it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from kanso.errors import Exit, KansoError, PreconditionError
from kanso.nautilus import backtest
from kanso.portfolio import records
from kanso.portfolio.promote import demote
from kanso.state import StateStore
from kanso.workspace import find

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = ["CHILD", "Demoted", "demote_in_child", "main"]

CHILD: Final = (
    "import sys; from kanso.portfolio.child import main; raise SystemExit(main(sys.argv[1:]))"
)
"""What the child runs: `main`, handed the workspace, the version, where to report and the
pid of the monitor it answers to."""

REPORT: Final = "report.json"
ERRORS: Final = "stderr.txt"
"""The two files a child leaves in its temporary directory: what it did, and what it said."""


@dataclass(frozen=True)
class Demoted:
    """What a child's demotion did — the state the version went to, the stages it
    redeployed and the stages it left halted — and what it cost: the child's peak resident
    memory in gibibytes and its wall time in seconds."""

    strategy_id: str
    version: int
    state: str
    redeployed: tuple[str, ...]
    halted: tuple[str, ...]
    peak_mem_gb: float
    wall_s: float


def demote_in_child(ws: Workspace, store: StateStore, strategy_id: str, version: int) -> Demoted:
    """Demote one live version in a child, and return what it did.

    The signature is the one a monitor pass moves a version by; `store` is the monitor's
    own and stays open beside the child, which writes through a connection of its own.
    Raises what the demotion raised, as an error of the same code with the same message and
    remedy, and a `PreconditionError` when the child ended without a report.
    """
    with tempfile.TemporaryDirectory(prefix="kanso-demote-") as room:
        report = Path(room) / REPORT
        _breach, peak_gb, wall_s = backtest.watched(
            [
                sys.executable,
                "-c",
                CHILD,
                str(ws.root),
                strategy_id,
                str(version),
                str(report),
                str(os.getpid()),
            ],
            cwd=ws.root,
            errors=Path(room) / ERRORS,
            env=None,
            budget_s=None,
            mem_cap_gb=None,
            stoppable=False,
        )
        said = _last_words(Path(room) / ERRORS)
        answer = _answer(report)
    subject = records.subject_of(strategy_id, version)
    if answer is None:
        raise PreconditionError(
            f"demoting {subject} ended without a report: {said}",
            remedy=f"run `kanso demote {subject}` by hand to see why",
        )
    if not answer["ok"]:
        remedy = answer.get("remedy")
        raise KansoError(
            str(answer["message"]),
            Exit(int(str(answer["code"]))),
            None if remedy is None else str(remedy),
        )
    return Demoted(
        strategy_id=strategy_id,
        version=version,
        state=str(answer["state"]),
        redeployed=tuple(str(one) for one in cast("list[object]", answer["redeployed"])),
        halted=tuple(str(one) for one in cast("list[object]", answer["halted"])),
        peak_mem_gb=peak_gb,
        wall_s=wall_s,
    )


def main(argv: Sequence[str]) -> int:
    """The child: demote, and write what it did or the refusal where the monitor reads it.

    The last argument is the pid of the monitor that started it, which it outlives by at
    most `backtest.PARENT_POLL_S` (`backtest.end_with`).
    """
    root, strategy_id, version, report, parent = argv
    threading.Thread(
        target=backtest.end_with, args=(int(parent),), name="kanso-demote-parent", daemon=True
    ).start()
    subject = records.subject_of(strategy_id, int(version))
    answer: dict[str, object]
    try:
        ws = find(Path(root))
        with StateStore(ws.path("state.db")) as store:
            made = demote(ws, store, strategy_id, int(version))
        answer = {
            "ok": True,
            "state": made.state,
            "redeployed": [one.stage for one in made.deployments],
            "halted": list(made.halted),
        }
    except KansoError as refused:
        answer = {
            "ok": False,
            "code": int(refused.code),
            "message": refused.message,
            "remedy": refused.remedy,
        }
    except Exception as fault:
        answer = {
            "ok": False,
            "code": int(Exit.ERROR),
            "message": f"demoting {subject} raised {type(fault).__name__}: {fault}",
            "remedy": f"run `kanso demote {subject}` by hand to see the traceback",
        }
    Path(report).write_text(json.dumps(answer), encoding="utf-8")
    return 0 if answer["ok"] else 1


def _last_words(errors: Path) -> str:
    """The last line a child wrote to a stream — a traceback's is the exception — or that it
    wrote none."""
    lines = errors.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    return lines[-1] if lines else "it said nothing"


def _answer(report: Path) -> dict[str, object] | None:
    """What the child reported, or `None` when it wrote nothing that reads."""
    try:
        return cast("dict[str, object]", json.loads(report.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
