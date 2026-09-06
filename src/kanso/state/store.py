"""The workspace state store: one SQLite database, content-addressed blobs, migrations.

`state.db` is the only database kanso keeps and `StateStore` is its only write path. It
runs in WAL mode so readers never block the writer and the lanes, the scheduler and the
stage nodes can read while a card is being recorded. Every write is a short
`BEGIN IMMEDIATE` transaction and a five-second busy timeout absorbs the overlap, which
is what makes several processes writing at once safe rather than merely likely to work.
A connection belongs to the process that opened it — SQLite connections do not survive a
fork — so each process opens its own store and using one across a process boundary is
refused instead of corrupting the database.

Schema versioning is SQLite's own `PRAGMA user_version` rather than a table kanso
maintains, so the version is readable from a database kanso has never opened and cannot
disagree with the tables. Migrations are `migrations/NNNN_name.sql`, applied in numeric
order, each in one transaction that ends by stamping its own number; a partially applied
migration therefore cannot exist, and re-running `migrate` on a current database is a
no-op.

Blobs are content addressed: the sha256 of the bytes is the key, storing the same bytes
twice is one row, and every reader accepts any unique prefix of a key, which is what lets
an operator or an agent name a card by its first seven characters.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Final

from kanso.errors import KansoError, PreconditionError, ValidationError

BUSY_TIMEOUT_MS: Final = 5_000
"""How long a write waits for another process's transaction before giving up."""

_MIGRATION_NAME: Final = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_HEX: Final = re.compile(r"^[0-9a-f]+$")
_SHA_LEN: Final = 64
_MIGRATIONS_DIR: Final = "migrations"
_PACKAGE: Final = "kanso.state"

TABLES: Final = (
    "approvals",
    "blobs",
    "cards",
    "certificates",
    "escalations",
    "events",
    "hypotheses",
    "plans",
    "queue",
    "runs",
    "sessions",
    "snapshots",
    "spend",
    "strategies",
    "strategy_versions",
)
"""Every table the store owns, as of the newest migration."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file: its version, its file name and its statements."""

    version: int
    name: str
    sql: str


def usable(store: StateStore, path: Path) -> None:
    """Refuse a database this package cannot correctly write, in either direction.

    Behind the package is a migration away and says so. Ahead of it is not: a file written
    by a later kanso has a shape this one does not know, and applying this package's
    writes to it is how a downgrade corrupts a workspace rather than merely failing. Both
    the command layer and the daemon call this, because a guard only one entry point
    honours protects only the operators who use that one — and the shipped service recipe
    starts the daemon directly.
    """
    behind = store.pending()
    if behind:
        raise PreconditionError(
            f"{path} is {len(behind)} migration(s) behind this kanso",
            remedy="run `kanso migrate`",
        )
    ahead = store.ahead_by()
    if ahead:
        raise PreconditionError(
            f"{path} was written by a later kanso: its schema is {ahead} version(s) "
            f"past the newest this package ships",
            remedy="install the kanso that wrote it, or start a workspace this one owns",
        )


@dataclass(frozen=True, slots=True)
class Event:
    """One row of the append-only event log."""

    event_id: int
    ts: str
    kind: str
    subject: str
    detail: dict[str, object]


@lru_cache(maxsize=1)
def migrations() -> tuple[Migration, ...]:
    """The shipped migrations in application order.

    Raises `KansoError` when a file in `migrations/` is misnamed or two files claim the
    same version, because either makes the application order ambiguous.
    """
    found: dict[int, Migration] = {}
    root = resources.files(_PACKAGE) / _MIGRATIONS_DIR
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            raise KansoError(
                f"migration {entry.name!r} is not named NNNN_name.sql",
                remedy="rename the file to a four-digit version and a lower-case slug",
            )
        version = int(match.group(1))
        if version in found:
            raise KansoError(
                f"migrations {found[version].name!r} and {entry.name!r} share version {version}"
            )
        found[version] = Migration(version, entry.name, entry.read_text(encoding="utf-8"))
    return tuple(found[v] for v in sorted(found))


SCHEMA_VERSION: Final = migrations()[-1].version if migrations() else 0
"""The schema version this package writes: the newest shipped migration."""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _dumps(value: object, what: str) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{what} is not JSON-serialisable: {exc}") from exc


def _loads(text: str) -> dict[str, object]:
    loaded: object = json.loads(text)
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def _enable_wal(conn: sqlite3.Connection, timeout_s: float) -> str:
    """Put the database in WAL mode and return the journal mode reached.

    Switching journal mode takes a brief exclusive lock and, unlike an ordinary write,
    SQLite does not run the busy handler for it: two processes opening a fresh `state.db`
    at the same moment would otherwise have one of them fail outright. The retry is that
    busy handler, bounded by the same timeout, and it costs nothing on the common path
    because a database already in WAL is never asked to change.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode == "wal":
            return mode
        try:
            mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError:
            mode = "locked"
        if mode == "wal" or time.monotonic() >= deadline:
            return mode
        time.sleep(0.01)


def _normalise_prefix(prefix: str) -> str:
    """Lower-case and check a sha or sha prefix; raise `ValidationError` if it cannot be one."""
    candidate = prefix.strip().lower()
    if not candidate:
        raise ValidationError("empty sha prefix", remedy="pass at least one hex character")
    if len(candidate) > _SHA_LEN or not _HEX.match(candidate):
        raise ValidationError(
            f"{prefix!r} is not a sha256 prefix",
            remedy=f"pass 1 to {_SHA_LEN} characters from 0-9a-f",
        )
    return candidate


class StateStore:
    """The workspace state store, opened on one `state.db` for the life of one process.

    Opening creates the file and its parent directory but applies no schema; call
    `migrate` before writing. Every method raises `kanso.errors` types, never
    `sqlite3` ones.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pid = os.getpid()
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        mode = _enable_wal(conn, BUSY_TIMEOUT_MS / 1000)
        if mode != "wal":
            conn.close()
            raise PreconditionError(
                f"{self.path} cannot run in WAL mode (journal_mode={mode})",
                remedy="place the workspace on a filesystem that supports WAL, not a network share",
            )
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        self._conn: sqlite3.Connection | None = conn

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        """Close the connection. Idempotent."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The live connection, refused once closed or once the process changed."""
        if self._conn is None:
            raise PreconditionError(
                f"the state store on {self.path} is closed",
                remedy="open a new StateStore",
            )
        if os.getpid() != self._pid:
            raise PreconditionError(
                f"the state store on {self.path} was opened by process {self._pid}",
                remedy="open a StateStore in each process; a SQLite connection does not fork",
            )
        return self._conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One short write transaction, rolled back on any exception."""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # -- migrations --------------------------------------------------------------

    def schema_version(self) -> int:
        """The version stamped on the database: `PRAGMA user_version`, 0 when fresh."""
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def pending(self) -> list[str]:
        """The file names of the migrations this database has not been stamped with."""
        current = self.schema_version()
        return [m.name for m in migrations() if m.version > current]

    def ahead_by(self) -> int:
        """How far the database's schema is *past* the newest migration this package ships.

        Zero for every database this kanso knows, including one behind it — that direction
        is `pending`. A positive number means the file was written by a later kanso and
        then opened by an earlier one, which `pending` cannot see because it only looks
        upward: an operator who downgrades would otherwise be told the schema is up to
        date while every command runs against a shape this package does not know.
        """
        newest = max((m.version for m in migrations()), default=0)
        return max(0, self.schema_version() - newest)

    def migrate(self) -> list[str]:
        """Apply every pending migration in order and return the names applied.

        Each migration runs in its own transaction that ends by stamping its version, so
        an interrupted run leaves the database on the last complete migration. A second
        process migrating the same database concurrently is serialised by the write lock;
        the loser sees the work already done and reports nothing applied.
        """
        conn = self.connection
        applied: list[str] = []
        for migration in migrations():
            if migration.version <= self.schema_version():
                continue
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{migration.sql}\n"
                f"PRAGMA user_version = {migration.version};\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                if self.schema_version() >= migration.version:
                    continue
                raise KansoError(f"migration {migration.name} failed: {exc}") from exc
            applied.append(migration.name)
        return applied

    def tables(self) -> list[str]:
        """The table names present in the database, sorted."""
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ).fetchall()
        return [str(row[0]) for row in rows]

    # -- content addressing ------------------------------------------------------

    def put_blob(self, data: bytes) -> str:
        """Store bytes under their sha256 and return it. Storing the same bytes twice is one row."""
        sha = hashlib.sha256(data).hexdigest()
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO blobs (sha, data, size, created_at) VALUES (?, ?, ?, ?)",
                (sha, data, len(data), _now()),
            )
        return sha

    def resolve_sha(self, prefix: str) -> str:
        """The one stored sha starting with `prefix`.

        Raises `ValidationError` when the prefix is not hex, matches nothing, or matches
        more than one blob; an operator naming a card by an ambiguous prefix must be told
        so rather than served an arbitrary one of the matches.
        """
        candidate = _normalise_prefix(prefix)
        # Every sha character is in 0-9a-f, all of which sort before 'g', so the half-open
        # range is exactly the set of shas carrying the prefix — and it uses the primary key.
        rows = self.connection.execute(
            "SELECT sha FROM blobs WHERE sha >= ? AND sha < ? ORDER BY sha LIMIT 2",
            (candidate, candidate + "g"),
        ).fetchall()
        if not rows:
            raise ValidationError(
                f"no stored object starts with {candidate!r}",
                remedy="list the hypothesis's cards to find the sha",
            )
        if len(rows) > 1:
            matches = ", ".join(str(row[0])[:12] for row in rows)
            raise ValidationError(
                f"{candidate!r} is ambiguous: {matches}",
                remedy="pass more characters of the sha",
            )
        return str(rows[0][0])

    def get_blob(self, sha: str) -> bytes:
        """The stored bytes named by a full sha or by any unique prefix of one."""
        resolved = self.resolve_sha(sha)
        row = self.connection.execute(
            "SELECT data FROM blobs WHERE sha = ?", (resolved,)
        ).fetchone()
        if row is None:  # pragma: no cover - resolve_sha already proved the row exists
            raise ValidationError(f"no stored object {resolved!r}")
        return bytes(row[0])

    def has_blob(self, sha: str) -> bool:
        """Whether a blob is stored under exactly this sha."""
        row = self.connection.execute(
            "SELECT 1 FROM blobs WHERE sha = ?", (_normalise_prefix(sha),)
        ).fetchone()
        return row is not None

    # -- events ------------------------------------------------------------------

    def event(self, kind: str, subject: str, detail: dict[str, object] | None = None) -> int:
        """Append one event and return its id."""
        payload = _dumps(detail or {}, f"detail of event {kind!r}")
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO events (ts, kind, subject, detail) VALUES (?, ?, ?, ?)",
                (_now(), kind, subject, payload),
            )
        return int(cursor.lastrowid or 0)

    def events(
        self,
        *,
        kind: str | None = None,
        subject: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Events in the order they were appended, optionally filtered."""
        sql = "SELECT event_id, ts, kind, subject, detail FROM events"
        where: list[str] = []
        params: list[object] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if subject is not None:
            where.append("subject = ?")
            params.append(subject)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY event_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [
            Event(
                event_id=int(row["event_id"]),
                ts=str(row["ts"]),
                kind=str(row["kind"]),
                subject=str(row["subject"]),
                detail=_loads(str(row["detail"])),
            )
            for row in rows
        ]
