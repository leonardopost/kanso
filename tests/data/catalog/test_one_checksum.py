"""One canonicalisation each for a dataset and for an instrument set.

Two independent checksums over the same thing mean two snapshot ids for the same data,
decided by whichever code path reached the snapshot first. Both hazards were real while
the store and the loaders were built side by side, so both are pinned here.
"""

from __future__ import annotations

import hashlib
import json

from kanso.data import catalog, instruments, loader, manifest, snapshot

from .conftest import AAPL, FakeWorkspace, Ref, bars, define, equity


def test_only_the_store_writes_the_manifest_a_snapshot_pins(ws: FakeWorkspace) -> None:
    """A loader's manifest answers what was served; the store's answers what is held."""
    ref = Ref()
    points = bars(ref.span[0], 5)

    from_loader = loader.manifest_for(ref, "synthetic", points)
    written = catalog.write(ws, points, ref=ref, source="synthetic")

    # Both are honest about the same dataset, and they are not the same number — so only
    # one of them may ever reach a snapshot.
    assert from_loader.row_count == written.manifest.row_count
    assert from_loader.checksum != written.manifest.checksum

    held = manifest.manifests(ws)
    assert [item.checksum for item in held.values()] == [written.manifest.checksum]

    define(ws)
    frozen = snapshot.freeze(ws)
    assert written.manifest.checksum in frozen.checksums
    assert from_loader.checksum not in frozen.checksums


def test_the_store_delegates_the_instrument_checksum(ws: FakeWorkspace) -> None:
    """`resolved_instruments_checksum` is the instruments module's function, not a rival."""
    catalog.open_catalog(ws).write_data([equity(AAPL)])

    held = list(catalog.open_catalog(ws).instruments())

    assert catalog.resolved_instruments_checksum(ws) == instruments.instruments_checksum(held)


def test_a_second_canonicalisation_would_disagree(ws: FakeWorkspace) -> None:
    """Which is the whole reason there is only one of them."""
    catalog.open_catalog(ws).write_data([equity(AAPL)])
    held = list(catalog.open_catalog(ws).instruments())

    naive = hashlib.sha256(
        "\n".join(
            sorted(
                json.dumps(type(item).to_dict(item), sort_keys=True, default=str) for item in held
            )
        ).encode()
    ).hexdigest()

    assert catalog.resolved_instruments_checksum(ws) != naive


def test_the_loader_manifest_needs_no_store(ws: FakeWorkspace) -> None:
    """Which is why it exists: a dry run can ask what a source would serve."""
    ref = Ref()

    answer = loader.manifest_for(ref, "synthetic", bars(ref.span[0], 5))

    assert answer.row_count == 5
    assert answer.span[0] == ref.span[0]
