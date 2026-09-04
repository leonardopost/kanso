"""What a replay leaves behind: one session directory, one row, and the stream it released.

A session is the record of a run over historical data on one of the two code paths. It says
what was replayed, over which range, at what speed and against which execution client, and
it keeps beside it the points that were released and the order intents that came back. That
is enough to inspect a session after the fact and enough to run the other code path over the
same range and compare, which is the whole of what parity is.

The stream is kanso's own, in plain JSON lines, rather than the engine's feather writer. The
engine's writer files a sandbox node's stream under an instance id that neither of the
catalog's two run listings can see, and a backtest's under a different one, so the two code
paths would be read back by two different mechanisms — one of them private. A line per point
and a line per intent is readable by anything, identical in shape on both paths, and is what
the comparison actually needs.

`clock_ts` is the availability instant of the last point released, in nanoseconds: the
replay position a stage resumes from. It is held as digits rather than as a timestamp
because a nanosecond does not survive a `datetime`, and losing the last nanosecond of a
session clock means resuming a nanosecond early or late.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field

from kanso.errors import PreconditionError
from kanso.schemas import Versioned, load_yaml, write_yaml
from kanso.schemas.base import NonEmpty

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "INTENTS_FILE",
    "SESSIONS_DIR",
    "SESSION_FILE",
    "STREAM_FILE",
    "Intent",
    "Mode",
    "Point",
    "Session",
    "insert",
    "intents_of",
    "list_sessions",
    "now",
    "read",
    "session_dir",
    "session_id",
    "sessions_path",
    "stream_of",
    "write",
]

SESSIONS_DIR: Final = "sessions"
SESSION_FILE: Final = "session.yaml"
STREAM_FILE: Final = "stream.jsonl"
INTENTS_FILE: Final = "intents.jsonl"

Mode = Literal["node", "engine", "paper", "live"]


@dataclass(frozen=True)
class Intent:
    """One order a session's strategy submitted, stamped with data time."""

    ts_event: int
    instrument: str
    side: str
    qty: float
    order_type: str
    price: float | None = None

    @classmethod
    def of(cls, row: Sequence[Any]) -> Intent:
        """One intent from the tuple the runner records."""
        ts, instrument, side, qty, order_type, price = row
        return cls(
            ts_event=int(ts),
            instrument=str(instrument),
            side=str(side),
            qty=float(qty),
            order_type=str(order_type),
            price=None if price is None else float(price),
        )

    @classmethod
    def loads(cls, payload: dict[str, Any]) -> Intent:
        """One intent from its stored line."""
        price = payload.get("price")
        return cls(
            ts_event=int(payload["ts_event"]),
            instrument=str(payload["instrument"]),
            side=str(payload["side"]),
            qty=float(payload["qty"]),
            order_type=str(payload["order_type"]),
            price=None if price is None else float(price),
        )

    def dumps(self) -> dict[str, Any]:
        """This intent as its stored line."""
        return {
            "ts_event": self.ts_event,
            "instrument": self.instrument,
            "side": self.side,
            "qty": self.qty,
            "order_type": self.order_type,
            "price": self.price,
        }


@dataclass(frozen=True)
class Point:
    """One data event a session released, as the stream records it."""

    ts_init: int
    ts_event: int
    type: str
    instrument: str | None = None

    @classmethod
    def of(cls, point: Any) -> Point:
        """One stream line from a data object, whatever type it is."""
        inner = getattr(point, "data", point)
        bar_type = getattr(inner, "bar_type", None)
        if bar_type is not None:
            instrument: str | None = str(bar_type.instrument_id)
        else:
            found = getattr(inner, "instrument_id", None)
            instrument = None if found is None else str(found)
        return cls(
            ts_init=int(point.ts_init),
            ts_event=int(point.ts_event),
            type=type(inner).__name__,
            instrument=instrument,
        )

    @classmethod
    def loads(cls, payload: dict[str, Any]) -> Point:
        """One stream line back into a point."""
        instrument = payload.get("instrument")
        return cls(
            ts_init=int(payload["ts_init"]),
            ts_event=int(payload["ts_event"]),
            type=str(payload["type"]),
            instrument=None if instrument is None else str(instrument),
        )

    def dumps(self) -> dict[str, Any]:
        """This point as its stored line."""
        return {
            "ts_init": self.ts_init,
            "ts_event": self.ts_event,
            "type": self.type,
            "instrument": self.instrument,
        }


class Session(Versioned):
    """`sessions/<id>/session.yaml`: one replay, on one code path, over one range."""

    session_id: NonEmpty
    mode: Mode
    target: NonEmpty
    instruments: list[NonEmpty] = Field(default_factory=list)
    from_: date = Field(alias="from")
    to: date
    speed: float = Field(ge=0)
    exec_: NonEmpty = Field(alias="exec")
    released: int = Field(default=0, ge=0)
    intents: int = Field(default=0, ge=0)
    clock_ns: int | None = None
    started_at: datetime
    ended_at: datetime | None = None

    @property
    def range(self) -> tuple[date, date]:
        """The range this session replayed."""
        return self.from_, self.to


def now() -> datetime:
    """The instant a session is stamped with."""
    return datetime.now(tz=UTC)


def session_id(mode: str, target: str, window: tuple[date, date], started: datetime) -> str:
    """An id naming when a session ran, what it ran and on which path.

    The digest covers the target and the range, so two sessions of the same thing in the
    same second differ only by the counter `write` appends, and a session of something else
    never collides with them at all.

    The instant is formatted digit by digit rather than by `strftime`, which does not
    zero-pad a year below 1000 on glibc while it does on BSD, and normalised to UTC first,
    because the trailing `Z` is a claim about the stamp rather than a decoration. An instant
    that arrives without a zone is read as UTC rather than as local time, so the same call
    cannot name two different sessions on two machines.
    """
    digest = hashlib.sha256(f"{target}|{window[0]}|{window[1]}".encode()).hexdigest()[:7]
    aware = started if started.tzinfo is not None else started.replace(tzinfo=UTC)
    at = aware.astimezone(UTC)
    stamp = f"{at.year:04d}{at.month:02d}{at.day:02d}T{at.hour:02d}{at.minute:02d}{at.second:02d}Z"
    return f"{stamp}-{mode}-{digest}"


def sessions_path(ws: Workspace) -> Path:
    """`<workspace>/sessions`: where session directories live."""
    return Path(ws.root) / SESSIONS_DIR


def session_dir(ws: Workspace, identifier: str) -> Path:
    """The directory holding one session."""
    return sessions_path(ws) / identifier


def write(
    ws: Workspace,
    session: Session,
    points: Iterable[Point],
    intents: Iterable[Intent],
) -> Session:
    """Write the session and its stream, taking the first id no other session holds.

    A session is never overwritten: the id carries a counter until it names a directory
    that does not exist, so a repeated replay of one target in one second is two records
    rather than one record twice.
    """
    resolved = _free(ws, session)
    directory = session_dir(ws, resolved.session_id)
    directory.mkdir(parents=True)
    write_yaml(resolved, directory / SESSION_FILE)
    _write_lines(directory / STREAM_FILE, (point.dumps() for point in points))
    _write_lines(directory / INTENTS_FILE, (intent.dumps() for intent in intents))
    return resolved


def read(ws: Workspace, identifier: str) -> Session:
    """One session's record, or a precondition failure naming the id."""
    path = session_dir(ws, identifier) / SESSION_FILE
    if not path.is_file():
        raise PreconditionError(
            f"no session {identifier!r} in {sessions_path(ws)}",
            remedy="run `kanso replay show` to list the sessions this workspace holds",
        )
    return load_yaml(Session, path)


def list_sessions(ws: Workspace) -> list[Session]:
    """Every session the workspace holds, oldest first."""
    directory = sessions_path(ws)
    if not directory.is_dir():
        return []
    found = [
        load_yaml(Session, path / SESSION_FILE)
        for path in sorted(directory.iterdir())
        if (path / SESSION_FILE).is_file()
    ]
    found.sort(key=lambda session: (session.started_at, session.session_id))
    return found


def intents_of(ws: Workspace, identifier: str) -> tuple[Intent, ...]:
    """The order intents one session produced, in the order they were submitted."""
    return tuple(
        Intent.loads(payload) for payload in _read_lines(session_dir(ws, identifier) / INTENTS_FILE)
    )


def stream_of(ws: Workspace, identifier: str) -> tuple[Point, ...]:
    """The data events one session released, in the order they were released."""
    return tuple(
        Point.loads(payload) for payload in _read_lines(session_dir(ws, identifier) / STREAM_FILE)
    )


def insert(store: StateStore, session: Session) -> Session:
    """Record the session in the state store, so a stage can find its own clock."""
    values = (
        session.session_id,
        session.mode,
        session.target,
        json.dumps(list(session.instruments)),
        session.from_.isoformat(),
        session.to.isoformat(),
        session.speed,
        session.exec_,
        None if session.clock_ns is None else str(session.clock_ns),
        session.started_at.isoformat(),
        None if session.ended_at is None else session.ended_at.isoformat(),
    )
    try:
        store.connection.execute(
            "INSERT INTO sessions (session_id, mode, target, instruments, from_ts, to_ts,"
            " speed, exec_id, clock_ts, started_at, ended_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    except sqlite3.IntegrityError as exc:  # pragma: no cover - `write` takes a free id first
        raise PreconditionError(
            f"session {session.session_id} is already recorded: {exc}"
        ) from None
    return session


def _free(ws: Workspace, session: Session) -> Session:
    """The session with the first id whose directory does not exist yet."""
    base = session.session_id
    identifier, counter = base, 1
    while session_dir(ws, identifier).exists():
        counter += 1
        identifier = f"{base}-{counter}"
    if identifier == base:
        return session
    return session.model_copy(update={"session_id": identifier})


def _write_lines(path: Path, payloads: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _read_lines(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        raise PreconditionError(
            f"{path} is missing, so this session's stream cannot be read",
            remedy="replay the session again",
        )
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                loaded: Any = json.loads(line)
                yield dict(loaded)
