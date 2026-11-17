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

**Replay is evaluation only.** It writes a session, an event and, when its range reaches its
target's forward window, the read that counts it as a look there (`kanso.certify.looks`). It
writes no card, no `best` and no certificate. The range it runs is the forward window onwards,
which is the window nothing is allowed to backtest, and that is exactly why nothing measured
here may become evidence for a decision the research loop makes — and why a replay that read
any day of it is counted: whoever ran it read what it showed.

**The read is recorded as soon as the session's files are on disk.** A session is readable
from the moment `record.write` returns — `kanso replay show` reads its files — so the read is
in the store before the session's row or its `replayed` event, and a store that fails between
them leaves no readable session uncounted. A replay refused, or failed before it wrote its
session, showed nothing and records nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Final

from kanso.certify.looks import REPLAY, ForwardRead, record_read
from kanso.data.manifest import manifests
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import hypothesis_of
from kanso.nautilus import backtest, session
from kanso.nautilus.session import Replayed
from kanso.replay import record
from kanso.replay.record import Intent, Point, Session, Stream
from kanso.replay.target import Target, resolve
from kanso.schemas.venue import SANDBOX

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import Hypothesis
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "ENGINE",
    "MODES",
    "NODE",
    "REPLAYED",
    "check",
    "count",
    "last_day",
    "range_of",
    "replay_session",
    "run",
    "sessions",
    "show",
]

NODE: Final = "node"
ENGINE: Final = "engine"
MODES: Final = (NODE, ENGINE)
"""The two code paths a replay runs on; a stage session is a node session with a stage."""

REPLAYED: Final = "replayed"
"""The event a finished replay appends; beside it a replay records its session and, when its
range reaches its target's forward window, the read of it (`count`)."""


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
    a session says what it was asked for. A range that reaches the target's forward window
    is a read of it, recorded with the session (`count`).
    """
    check(mode, speed)
    target = resolve(ws, store, strategy=strategy, version=version, hyp=hyp, sha=sha)
    window = range_of(ws, target, start, end)
    return replay_session(ws, store, target, window, speed=speed, mode=mode, where=mode)


def check(mode: str, speed: float) -> None:
    """Refuse a mode that is not a code path and a speed that is not a rate, before anything
    is resolved."""
    if mode not in MODES:
        raise ValidationError(
            f"mode: {mode!r} is not a code path; one of {', '.join(MODES)} was expected"
        )
    if speed < 0:
        raise ValidationError(f"speed: {speed} is not a rate to replay at")


def replay_session(
    ws: Workspace,
    store: StateStore,
    target: Target,
    window: tuple[date, date],
    *,
    speed: float,
    mode: str,
    where: str | None,
    exempt: str | None = None,
) -> Session:
    """Run one resolved target over `window` on one code path and record what it produced:
    its session's files, then — where `where` names the replay that read it — its read of the
    forward window, then the session's row and its `replayed` event.

    The read comes between the files and the row because the session is readable from the
    moment its files are written, so nothing after them may fail with the read not yet in the
    store. `where` is `None` for a path whose read another path of the same replay records —
    the engine half of a parity — and `exempt` names why the read counts nothing.
    """
    request = target.request(window)
    instruments, chunks = backtest.window_chunks(request, target.catalog)
    started = record.now()
    stream = Stream()
    replayed = _execute(request, instruments, chunks, mode=mode, speed=speed, sink=stream)
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
            "released": stream.count,
            "intents": len(result.intents),
            "commands": result.commands if _rated(request) else None,
            "period_stops": result.run.period_stops,
            "clock_ns": replayed.clock_ns,
            "started_at": started,
            "ended_at": record.now(),
        }
    )
    written = record.write(ws, made, stream, (Intent.of(row) for row in result.intents))
    if where is not None:
        count(ws, store, target, written, where=where, exempt=exempt)
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


def count(
    ws: Workspace,
    store: StateStore,
    target: Target,
    session: Session,
    *,
    where: str,
    exempt: str | None = None,
) -> ForwardRead | None:
    """Record the session as a read of its target's forward window, where its range reaches it.

    The read is charged to the target's hypothesis — for a card, the file its run pinned; for a
    version, its sleeve — judged against that file's forward start, over the session's days
    from it on, and is of the session. An attached construct's card runs on its host version,
    so it shows that version's sleeve's forward days too: a second read, of the same session,
    is charged to the host sleeve's hypothesis, judged against the forward start it is
    registered under, where the range reaches that one. A card is never exempt, so neither
    read of it is. The target's own read is returned, or the host's where only the host's was
    reached, or `None` where neither was.
    """
    own = _read(target.hyp, target, session, where)
    if own is not None:
        own = replace(own, exempt=exempt)
        record_read(store, own)
    host = None
    if target.version_hyp is not None:
        host = _read(hypothesis_of(ws, store, target.version_hyp), target, session, where)
        if host is not None:
            record_read(store, host)
    return own or host


def _rated(request: backtest.RunRequest) -> bool:
    """Whether the venue model this replay ran under states `costs.orders_per_minute`.

    Only under one do the commands a strategy sent decide what it may send next, so only then
    does a session record them for parity to compare (`kanso.replay.parity.Parity.counted`).
    With no layer stating the rate a session records none, as one written before kanso
    counted, and two paths whose orders agree are judged identical as they were then, though
    what each counted may differ where the two paths already fill apart.
    """
    costs = request.venue_model.get("costs")
    return isinstance(costs, Mapping) and costs.get("orders_per_minute") is not None


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


def _read(hyp: Hypothesis, target: Target, session: Session, where: str) -> ForwardRead | None:
    """The read `session` made of `hyp`'s forward window, or `None` where its range ends before
    the window opens."""
    start = hyp.windows.forward.start
    if session.to < start:
        return None
    return ForwardRead(
        hyp_id=hyp.id,
        via=REPLAY,
        of=session.session_id,
        target=target.label,
        where=where,
        session=session.session_id,
        forward_start=start,
        first=max(session.from_, start),
        last=session.to,
    )


def range_of(
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
    chunks: Iterator[tuple[tuple[object, ...], ...]],
    *,
    mode: str,
    speed: float,
    sink: Stream,
) -> Replayed:
    """The chosen code path over this window's points, a chunk at a time.

    Both paths are handed the window in the chunks a card's child is streamed
    (`backtest.window_chunks`), and each folds what it released into `sink` as a chunk of
    it runs, so neither holds the window whole and the digest the session records is of
    the feed itself. The research path has no feed to stop short, so it always reaches the
    end of the window and its session clock is the last point of it. Both paths are fed a
    warmed target's prefix and neither counts it: what was released is the range's own
    points.
    """
    opens = request.bounds[0]
    clock: int | None = None

    def released(points: Sequence[object]) -> None:
        nonlocal clock
        for point in session.measured(points, opens):
            sink.add(Point.of(point))
            clock = int(point.ts_init)

    if mode == NODE:
        return session.run_node_chunked(request, instruments, chunks, speed=speed, sink=released)
    result = backtest.execute_chunked(request, instruments, chunks, sink=released)
    return Replayed(result, sink.count, clock)
