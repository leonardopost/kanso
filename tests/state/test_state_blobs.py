"""Content addressing: sha256 keys, idempotent writes and unique-prefix resolution."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.errors import ValidationError
from kanso.state import StateStore


def _colliding_pair() -> tuple[bytes, bytes, str]:
    """Two payloads whose shas share a first hex character, and that character."""
    seen: dict[str, bytes] = {}
    for i in range(1_000):
        payload = f"payload-{i}".encode()
        head = hashlib.sha256(payload).hexdigest()[0]
        if head in seen:
            return seen[head], payload, head
        seen[head] = payload
    raise AssertionError("no first-character collision in 1000 payloads")  # pragma: no cover


def test_put_blob_keys_by_sha256_and_is_idempotent(store: StateStore) -> None:
    data = b"class Strategy: pass\n"
    sha = store.put_blob(data)
    assert sha == hashlib.sha256(data).hexdigest()
    assert store.put_blob(data) == sha
    rows = store.connection.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    assert rows == 1
    assert store.connection.execute("SELECT size FROM blobs").fetchone()[0] == len(data)


def test_blob_round_trip_by_full_sha_and_by_prefix(store: StateStore) -> None:
    sha = store.put_blob(b"program text")
    assert store.get_blob(sha) == b"program text"
    assert store.get_blob(sha[:7]) == b"program text"
    assert store.resolve_sha(sha[:7]) == sha
    assert store.resolve_sha(sha) == sha
    assert store.resolve_sha(sha[:7].upper()) == sha
    assert store.has_blob(sha)
    assert not store.has_blob("0" * 64)


def test_empty_bytes_round_trip(store: StateStore) -> None:
    sha = store.put_blob(b"")
    assert sha == hashlib.sha256(b"").hexdigest()
    assert store.get_blob(sha) == b""


def test_an_ambiguous_prefix_is_refused(store: StateStore) -> None:
    left, right, head = _colliding_pair()
    sha_left = store.put_blob(left)
    sha_right = store.put_blob(right)
    with pytest.raises(ValidationError, match="ambiguous") as caught:
        store.resolve_sha(head)
    assert caught.value.code == 3
    # Both candidates are named, so the operator can lengthen the prefix.
    assert sha_left[:12] in caught.value.message
    assert sha_right[:12] in caught.value.message
    # Lengthening resolves it.
    assert store.resolve_sha(sha_left[:10]) == sha_left


def test_an_unknown_prefix_is_refused(store: StateStore) -> None:
    store.put_blob(b"only one")
    with pytest.raises(ValidationError, match="no stored object") as caught:
        store.resolve_sha("deadbeef")
    assert caught.value.code == 3
    with pytest.raises(ValidationError, match="no stored object"):
        store.get_blob("deadbeef")


@pytest.mark.parametrize("bad", ["", "   ", "zz", "0x12", "abcdefg", "a" * 65])
def test_a_prefix_that_is_not_hex_is_refused(store: StateStore, bad: str) -> None:
    with pytest.raises(ValidationError):
        store.resolve_sha(bad)


def test_prefix_matching_never_bleeds_past_the_prefix(store: StateStore) -> None:
    # 'f' is the last hex character; the range scan must not run off the end of it.
    for i in range(200):
        store.put_blob(f"spread-{i}".encode())
    for row in store.connection.execute("SELECT sha FROM blobs").fetchall():
        sha = str(row[0])
        assert store.resolve_sha(sha) == sha
        assert store.resolve_sha(sha[:20]) == sha


@pytest.fixture(scope="session")
def property_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[StateStore]:
    path: Path = tmp_path_factory.mktemp("state-property") / "state.db"
    with StateStore(path) as opened:
        opened.migrate()
        yield opened


@given(data=st.binary(max_size=4096))
def test_any_bytes_round_trip(property_store: StateStore, data: bytes) -> None:
    sha = property_store.put_blob(data)
    assert len(sha) == 64
    assert sha == hashlib.sha256(data).hexdigest()
    assert property_store.get_blob(sha) == data
    assert property_store.put_blob(data) == sha
