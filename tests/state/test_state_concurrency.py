"""Two processes writing the same `state.db` at once, which is the daemon's normal case.

Each lane, each stage node and each replay session is its own process and each opens its
own connection, so the store's WAL mode, busy timeout and short write transactions have to
hold under real concurrent writers rather than under threads sharing one connection.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from kanso.state import StateStore

_WRITER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from kanso.state import StateStore

    db, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    store = StateStore(Path(db))
    store.migrate()
    for i in range(count):
        sha = store.put_blob(f"{tag}-{i}".encode())
        store.event("carded", tag, {"i": i, "sha": sha})
    store.close()
    """
)

_WRITES = 150


def _spawn(db: Path, tag: str, count: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(db), tag, str(count)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_processes_write_concurrently_without_corruption(db_path: Path) -> None:
    writers = ("alpha", "beta")
    # Nothing migrates the database first: the writers race on the migration too.
    processes = [_spawn(db_path, tag, _WRITES) for tag in writers]
    for process in processes:
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, f"writer failed:\n{out}\n{err}"

    with StateStore(db_path) as store:
        assert store.pending() == []
        assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert str(store.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"

        blobs = store.connection.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
        assert blobs == len(writers) * _WRITES
        assert len(store.events()) == len(writers) * _WRITES

        for tag in writers:
            events = store.events(kind="carded", subject=tag)
            assert [e.detail["i"] for e in events] == list(range(_WRITES))
            for event in events:
                expected = f"{tag}-{event.detail['i']}".encode()
                assert store.get_blob(str(event.detail["sha"])) == expected


def test_a_reader_sees_a_writer_commit_immediately(db_path: Path) -> None:
    with StateStore(db_path) as writer, StateStore(db_path) as reader:
        writer.migrate()
        sha = writer.put_blob(b"visible")
        assert reader.get_blob(sha) == b"visible"
        assert [e.kind for e in reader.events()] == []
        writer.event("carded", "demo_mr")
        assert [e.kind for e in reader.events()] == ["carded"]
