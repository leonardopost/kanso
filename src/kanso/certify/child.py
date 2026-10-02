"""A stall's certification, run in a child of the lane that stalled.

A lane is a long-lived process, and a certification is the heaviest thing it is ever asked
to do: both windows run in the process certifying, the certification window's points stay
in hand for every run a perturbation gate makes, and `parity_replay` replays the subject on
a trading node. Run in the lane itself, none of that was given back. Measured on an
operator's workspace on 2026-10-01 (kanso 0.13.1.dev3, macOS arm64, 16 GB, six lanes at
`[env] mem_per_lane_gb` 2.5): once five lanes had certified, two idle ones held 2.9 GB and
4.1 GB (peaks 6.3 GB and 9.1 GB) and one certifying held 7.5 GB, beside cards capped at
2.5 GB; swap reached 13.3 GB and free disk fell from 20 GiB to 8.1 GiB, and only killing
the idle lanes gave either back.

So a lane certifies in a child it watches the way it watches a card (`backtest.watched`):
started in a session of its own, held to the resident memory a card of the subject's run
may hold (`kanso.research.loop.mem_cap`: the lane's share, floored at three times what that
run's baseline needed), killed at the watcher's next poll once the lane is told to stop,
and ending itself when the lane that started it is gone (`backtest.end_with`). What the
windows cost goes when the child exits, so a lane between cards holds only itself, and the
share the envelope plans bounds everything a lane runs.

What differs from a card is what the child may reach. A card has no path to any catalog and
starts under an allow-list of the environment; certification is the one place a strategy
meets the window kept from it, and it plans with a model when no plan is pinned, so the
child opens the workspace, its catalog and its store as the lane does, under the lane's
environment. It certifies exactly as `kanso cert run` does — `kanso.certify.run.certify`,
the one function — writes the certificate and every event itself, and reports the
certificate, or the refusal, to the lane, which raises that refusal as the same kind of
error with the same remedy. A fault that is not a refusal comes back as one naming what
raised, so a lane records it and goes on rather than dying of it. No wall time is imposed:
how many engine runs a certification makes is its plan's choice — the gates, and the
parameters a perturbation moves — so no multiple of a card's budget bounds it.

A child killed over the share is refused, naming what it reached and the two ways to give
it more: a larger `[env] mem_per_lane_gb`, which plans the lanes around what certification
actually costs, or `kanso cert run` by hand, which certifies in the operator's own process
with nothing but the host to bound it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from kanso.certify.run import certify, subject_run
from kanso.errors import (
    ApprovalError,
    Exit,
    KansoError,
    PreconditionError,
    ValidationError,
)
from kanso.nautilus import backtest
from kanso.research.lanes import DEFAULT_LANE
from kanso.research.loop import mem_cap
from kanso.schemas import Certificate
from kanso.state import StateStore
from kanso.workspace import find

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = ["CHILD", "Certified", "certify_in_child", "main"]

CHILD: Final = (
    "import sys; from kanso.certify.child import main; raise SystemExit(main(sys.argv[1:]))"
)
"""What the child runs: `main`, handed the workspace, the subject, the lane, where to report
and the pid of the lane it answers to."""

REPORT: Final = "report.json"
ERRORS: Final = "stderr.txt"
"""The two files a child leaves in its temporary directory: what it made, and what it said."""

_REFUSALS: Final[dict[Exit, Callable[[str, str | None], KansoError]]] = {
    Exit.PRECONDITION: PreconditionError,
    Exit.VALIDATION: ValidationError,
    Exit.APPROVAL: ApprovalError,
}


@dataclass(frozen=True)
class Certified:
    """The certificate a child made, and what making it cost: the child's peak resident
    memory in gibibytes and its wall time in seconds."""

    certificate: Certificate
    peak_mem_gb: float
    wall_s: float


def certify_in_child(
    ws: Workspace, store: StateStore, hyp_id: str, *, sha: str, lane: str = DEFAULT_LANE
) -> Certified:
    """Certify one carded strategy of a hypothesis in a child, and return what it made.

    Raises what the certification raised, as the same kind of error with the same remedy; a
    `PreconditionError` when the child was killed over its share, when this process was
    told to stop, or when it ended without a report.
    """
    cap = mem_cap(ws, subject_run(store, hyp_id, sha))
    with tempfile.TemporaryDirectory(prefix="kanso-cert-") as room:
        report = Path(room) / REPORT
        breach, peak_gb, wall_s = backtest.watched(
            [
                sys.executable,
                "-c",
                CHILD,
                str(ws.root),
                hyp_id,
                sha,
                lane,
                str(report),
                str(os.getpid()),
            ],
            cwd=ws.root,
            errors=Path(room) / ERRORS,
            env=None,
            budget_s=None,
            mem_cap_gb=cap,
        )
        said = _last_words(Path(room) / ERRORS)
        answer = _answer(report)
    if breach == backtest.INTERRUPTED:
        raise PreconditionError(
            f"the certification of {hyp_id} was interrupted: the lane running it was told to stop",
            remedy="start the daemon again; the hypothesis goes back in the queue and is "
            "certified at its next stall",
        )
    if breach is not None:  # no wall time is imposed, so only the memory share is breached
        raise PreconditionError(
            f"certifying {sha[:7]} of {hyp_id} needed more than the {cap:.2f} GB a card of its "
            f"run may hold, so it was killed at {peak_gb:.2f} GB",
            remedy="declare a larger `[env] mem_per_lane_gb` in kanso.toml and run `kanso env "
            f"detect`, or certify it by hand with `kanso cert run {hyp_id}`",
        )
    if answer is None:
        raise PreconditionError(
            f"certifying {sha[:7]} of {hyp_id} ended without a report: {said}",
            remedy=f"certify it by hand with `kanso cert run {hyp_id}` to see why",
        )
    if not answer["ok"]:
        raise _refusal(answer)
    return Certified(Certificate.model_validate(answer["certificate"]), peak_gb, wall_s)


def main(argv: Sequence[str]) -> int:
    """The child: certify, and write the certificate or the refusal where the lane reads it.

    The last argument is the pid of the lane that started it, which it outlives by at most
    `backtest.PARENT_POLL_S` (`backtest.end_with`).
    """
    root, hyp_id, sha, lane, report, parent = argv
    threading.Thread(
        target=backtest.end_with, args=(int(parent),), name="kanso-cert-parent", daemon=True
    ).start()
    answer: dict[str, object]
    try:
        ws = find(Path(root))
        with StateStore(ws.path("state.db")) as store:
            made = certify(ws, store, hyp_id, sha=sha, lane=lane)
        answer = {"ok": True, "certificate": made.model_dump(mode="json", by_alias=True)}
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
            "message": f"certifying {sha[:7]} of {hyp_id} raised {type(fault).__name__}: {fault}",
            "remedy": f"certify it by hand with `kanso cert run {hyp_id}` to see the traceback",
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


def _refusal(answer: dict[str, object]) -> KansoError:
    """The child's refusal, as the kind of error it raised and with the remedy it gave."""
    code = Exit(int(str(answer["code"])))
    message = str(answer["message"])
    remedy = None if answer.get("remedy") is None else str(answer["remedy"])
    kind = _REFUSALS.get(code)
    return KansoError(message, code, remedy) if kind is None else kind(message, remedy)
