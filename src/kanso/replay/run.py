"""Replaying a target over the catalog, on either code path, into a session.

`node` is the live code path: a trading node, the replay data client and a simulated
execution client. `engine` is the research code path: the runner every card and every
certificate is measured by. They are given the same target, the same range and the same
points, and the reason both exist is that whether they agree is a fact about the system
rather than an assumption of it.

**Replay always executes against the simulated client**, whatever a stage is configured
with. A replay feeds historical data; a broker's paper account fills against current prices;
pairing the two would fill an order at a price that has nothing to do with the data that
triggered it, and the intents on the two paths could then never be compared.

**Replay is evaluation only.** It writes a session and an event and nothing else: no card,
no `best`, no certificate. The range it runs is the forward window onwards, which is the
window nothing is allowed to backtest, and that is exactly why nothing measured here may
become evidence for a decision the research loop makes.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Final

from kanso.data.manifest import manifests
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus import backtest, session
from kanso.nautilus.session import Replayed
from kanso.replay import record
from kanso.replay.record import Intent, Point, Session
from kanso.replay.target import Target, resolve
from kanso.schemas.venue import SANDBOX

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "ENGINE",
    "MODES",
    "NODE",
    "REPLAYED",
    "last_day",
    "run",
    "sessions",
    "show",
]

NODE: Final = "node"
ENGINE: Final = "engine"
MODES: Final = (NODE, ENGINE)
"""The two code paths a replay runs on; a stage session is a node session with a stage."""

REPLAYED: Final = "replayed"
"""The event a finished replay appends; nothing else about a replay is recorded."""


def run(
    ws: Workspace,
    store: StateStore,
    *,
    strategy: str | None = None,
    version: int | None = None,
    hyp: str | None = None,
    sha: str | None = None,
    start: date | None = None,
    end: date | None = None,
    speed: float = 0.0,
    mode: str = NODE,
) -> Session:
    """Replay one target over the catalog and record the session it produced.

    The range defaults to the target's forward window through the last day the catalog
    holds for its universe. Speed is wall-clock pacing on the node path and means nothing
    on the engine path, where there is no feed to pace; it is recorded either way, because
    a session says what it was asked for.
    """
    if mode not in MODES:
        raise ValidationError(
            f"mode: {mode!r} is not a code path; one of {', '.join(MODES)} was expected"
        )
    if speed < 0:
        raise ValidationError(f"speed: {speed} is not a rate to replay at")
    target = resolve(ws, store, strategy=strategy, version=version, hyp=hyp, sha=sha)
    window = _range(ws, target, start, end)
    request = target.request(window)
    instruments, groups = backtest.window_data(request, target.catalog)
    points = session.ordered(groups)
    started = record.now()
    replayed = _execute(request, instruments, groups, mode=mode, speed=speed)
    result = replayed.result
    made = Session.model_validate(
        {
            "session_id": record.session_id(mode, target.label, window, started),
            "mode": mode,
            "target": target.label,
            "instruments": list(target.universe),
            "from": window[0],
            "to": window[1],
            "speed": speed,
            "exec": SANDBOX.id,
            "released": replayed.released,
            "intents": len(result.intents),
            "clock_ns": replayed.clock_ns,
            "started_at": started,
            "ended_at": record.now(),
        }
    )
    written = record.write(
        ws,
        made,
        (Point.of(point) for point in points[: replayed.released]),
        (Intent.of(row) for row in result.intents),
    )
    record.insert(store, written)
    store.event(
        REPLAYED,
        written.session_id,
        {
            "mode": mode,
            "target": target.label,
            "from": window[0].isoformat(),
            "to": window[1].isoformat(),
            "speed": speed,
            "intents": len(result.intents),
            "crashed": result.crashed,
            "reason": result.reason,
        },
    )
    return written


def sessions(ws: Workspace) -> list[Session]:
    """Every session this workspace holds, oldest first."""
    return record.list_sessions(ws)


def show(ws: Workspace, session_id: str) -> Session:
    """One session's record."""
    return record.read(ws, session_id)


def last_day(ws: Workspace, universe: tuple[str, ...]) -> date:
    """The last day the catalog serves for any instrument of this universe.

    Read from the manifests, which record what a source actually served rather than what
    was asked of it, so a replay ends where the data ends and not a day later.
    """
    served = [
        manifest.span[1] for manifest in manifests(ws).values() if manifest.instrument in universe
    ]
    if not served:
        raise PreconditionError(
            f"data: the catalog holds nothing for {', '.join(sorted(universe))}",
            remedy="run `kanso data load` for the universe, or pass an explicit range",
        )
    return max(served)


def _range(
    ws: Workspace, target: Target, start: date | None, end: date | None
) -> tuple[date, date]:
    """The days this replay covers: the forward window through the end of the catalog."""
    opens = start or target.hyp.windows.forward.start
    closes = end or last_day(ws, target.universe)
    if closes < opens:
        raise ValidationError(
            f"range: {opens}..{closes} ends before it begins",
            remedy="pass --from and --to in order, or leave them out",
        )
    return opens, closes


def _execute(
    request: backtest.RunRequest,
    instruments: tuple[object, ...],
    groups: tuple[tuple[object, ...], ...],
    *,
    mode: str,
    speed: float,
) -> Replayed:
    """The chosen code path over this window's points.

    The research path has no feed to stop short, so it always reaches the end of the window
    and its session clock is the last point of it.
    """
    if mode == NODE:
        return session.run_node(request, instruments, groups, speed=speed)
    points = session.ordered(groups)
    result = backtest.execute(request, instruments, groups)
    return Replayed(result, len(points), int(points[-1].ts_init) if points else None)
