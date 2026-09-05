"""Both Massive transports through the commands an operator actually runs.

The adapter's own suite exercises each loader directly. What it cannot show is the
sequence the milestone is written around — `load`, then `backfill`, then `sync`, then
`snapshot` — because that sequence is the catalog's, not a loader's: it is where a chunk
becomes a manifest, a manifest becomes a checkpoint, a checkpoint decides whether the next
run fetches anything, and a successor supersedes what a snapshot pinned. Every defect that
only shows up across two commands lives in that gap, so the whole of it is run here, over
each transport, and the two are then compared to each other.

**Both transports are seeded from one vendor row.** The request path's aggregate and the
store's object carry the same window, the same prices and the same volume, spelled the two
ways the vendor spells them — a letter per field and milliseconds over the API, a column
per field and nanoseconds in the file. So a difference between the two catalogs below is a
difference between two code paths and never between two fixtures, which is the property
the whole pass turns on: the same session, fetched either way, is one bar.

Nothing here opens a socket. Both of the adapter's senders are frozen — the rate-limited
transport and the engine's bulk download — and the three credentials the adapter resolves
are strings this suite invented.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.data.manifest import manifests_path
from kanso.errors import Exit
from kanso.workspace import find

from ..data.adapters.massive import Replay
from . import massive_wire
from .conftest import at, payload

SYMBOL = "AAPL"
VENUE = "XNAS"
INSTRUMENT = f"{SYMBOL}.{VENUE}"

HELD = (date(2024, 3, 1), date(2024, 3, 15))
"""The tail a first `load` holds, so `backfill` has history in front of it to fill."""

WHOLE = (date(2024, 1, 3), date(2024, 3, 29))
"""The range both transports serve end to end.

It starts the day after the store's oldest object because a bar is stamped at its close,
so the oldest *session* the store holds belongs to the following day — and the request
path's floor for this class is two decades older, so this is the range on which the two
spans coincide and the two catalogs are comparable.
"""

SYNC_TO = date(2024, 4, 12)
"""Where `sync` is told to stop. Given rather than defaulted, so the test does not move
with the calendar."""

TRANSPORTS = ("massive_bars", "massive_bulk")

DAY = timedelta(days=1)


def write_massive_instruments(root: Path) -> Path:
    """One manual entry for the vendor's own key, so nothing resolves through a network."""
    document: dict[str, Any] = {
        INSTRUMENT: {
            "nautilus_id": INSTRUMENT,
            "asset_class": "EQUITY",
            "manual": True,
            "corporate_actions": "none",
            "override": {"currency": "USD", "price_increment": "0.01", "lot_size": "1"},
        }
    }
    path = root / "instruments.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def write_massive_spec(
    root: Path, loader: str, *, span: tuple[date, date], name: str | None = None
) -> Path:
    """A spec for one transport over one range, in the fields that transport reads.

    The two specs differ only in the loader they name and the fields peculiar to it, which
    is the point: one class, one venue, one instrument, one resolution and one range mean
    the same thing on either side.
    """
    document: dict[str, Any] = {
        "loader": loader,
        "asset_class": "stocks",
        "venue": VENUE,
        "instruments": [SYMBOL],
        "resolution": "1d",
        "start": str(span[0]),
        "end": str(span[1]),
    }
    path = root / (name or f"{loader}.yaml")
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def vendor(runner: CliRunner, wired_bulk: Replay, workspace: Path) -> Path:
    """A workspace whose instrument resolves and whose vendor answers on both transports."""
    write_massive_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    return workspace


def run(runner: CliRunner, root: Path, *args: object) -> dict[str, Any]:
    """One command, asserted green, as its `--json` object."""
    result = at(runner, root, *args, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def series_of(runner: CliRunner, root: Path) -> dict[str, Any]:
    """The one series the workspace holds, as `data show` reports it."""
    shown = run(runner, root, "data", "show")
    [found] = shown["series"]
    assert isinstance(found, dict)
    return found


def newest(runner: CliRunner, root: Path) -> str:
    """The id of the dataset holding the most recent day, which is what `sync` extends.

    Named rather than left to the default, because a backfill leaves one manifest per
    chunk and `sync` with no dataset extends every one of them — a run of the command
    that belongs to a workspace holding one dataset per series, not to this one.
    """
    held = series_of(runner, root)["datasets"]
    return str(max(held, key=lambda item: item["span"][1])["dataset_id"])


def sequence(runner: CliRunner, root: Path, loader: str) -> dict[str, Any]:
    """`load` the tail, `backfill` the history before it, `sync` past it, then `snapshot`.

    The whole of what the milestone asks of a transport, in the order an operator meets
    it, returning what each step reported so a test may assert on any of them.
    """
    tail = write_massive_spec(root, loader, span=HELD, name=f"{loader}-tail.yaml")
    whole = write_massive_spec(root, loader, span=WHOLE, name=f"{loader}-whole.yaml")
    loaded = run(runner, root, "data", "load", "--loader", loader, "--spec", tail)
    filled = run(runner, root, "data", "backfill", "--loader", loader, "--spec", whole)
    synced = run(runner, root, "data", "sync", "--dataset", newest(runner, root), "--to", SYNC_TO)
    frozen = run(runner, root, "data", "snapshot")
    return {
        "load": loaded,
        "backfill": filled,
        "sync": synced,
        "snapshot": frozen,
        "spec": whole,
        "series": series_of(runner, root),
    }


@pytest.mark.parametrize("loader", TRANSPORTS)
def test_a_transport_loads_backfills_syncs_and_freezes_into_one_unbroken_series(
    runner: CliRunner, vendor: Path, loader: str
) -> None:
    """The sequence the milestone is written around, over each transport in turn.

    A first load holds a fortnight in the middle of the range; the backfill fills the
    history in front of it and closes the gap the load left; the sync extends the end into
    a successor; the snapshot freezes the lot. What is asserted is the shape of the result
    rather than a row count: one series with no holes in it, running from the first day of
    the range to somewhere past the backfill's end and no further than the sync was told to
    go — which is not the same day on both transports, because coverage is what was served
    and the store holds no object for a day it has not written yet.
    """
    done = sequence(runner, vendor, loader)

    assert done["load"]["rows"] > 0
    assert done["backfill"]["rows"] > 0
    assert done["sync"]["rows"] > 0
    assert done["series"]["instrument"] == INSTRUMENT
    assert done["series"]["gaps"] == []
    [span] = done["series"]["spans"]
    assert span[0] == str(WHOLE[0])
    assert str(WHOLE[1]) < span[1] <= str(SYNC_TO)
    assert len(done["snapshot"]["datasets"]) > 0


@pytest.mark.parametrize("loader", TRANSPORTS)
def test_a_repeated_backfill_over_either_transport_fetches_nothing(
    runner: CliRunner, vendor: Path, loader: str
) -> None:
    """Every chunk that writes leaves a manifest, and a manifest is a checkpoint.

    So the second run of the same command finds nothing missing and asks the vendor for
    nothing — which is what makes an interrupted run resumable rather than a run that
    starts over.
    """
    done = sequence(runner, vendor, loader)
    before = series_of(runner, vendor)["spans"]

    again = run(runner, vendor, "data", "backfill", "--loader", loader, "--spec", done["spec"])

    assert again["rows"] == 0
    assert again["chunks"] == []
    assert any("nothing missing" in note for note in again["notes"])
    assert series_of(runner, vendor)["spans"] == before


@pytest.mark.parametrize("loader", TRANSPORTS)
def test_an_interrupted_backfill_resumes_where_its_manifests_stop(
    runner: CliRunner, vendor: Path, loader: str
) -> None:
    """Half the history, then the rest, is the same series as all of it in one run.

    An interruption is not simulated by killing a process — a test cannot assert on what a
    signal left behind — but by doing what the resumption relies on: stopping part way, and
    letting the next run work the remainder out from the manifests alone. The spec it is
    handed is the whole range both times, so the second run has to narrow it itself, and
    what it has to notice is the hole between where the first run stopped and where the
    first load's fortnight begins.
    """
    tail = write_massive_spec(vendor, loader, span=HELD, name=f"{loader}-tail.yaml")
    whole = write_massive_spec(vendor, loader, span=WHOLE, name=f"{loader}-whole.yaml")
    middle = date(2024, 2, 15)
    run(runner, vendor, "data", "load", "--loader", loader, "--spec", tail)

    first = run(
        runner, vendor, "data", "backfill", "--loader", loader, "--spec", whole, "--to", middle
    )
    partial = series_of(runner, vendor)
    second = run(runner, vendor, "data", "backfill", "--loader", loader, "--spec", whole)
    whole_again = series_of(runner, vendor)

    assert first["rows"] > 0
    assert second["rows"] > 0
    assert partial["gaps"] == [[str(middle + DAY), str(HELD[0] - DAY)]]
    assert whole_again["gaps"] == []
    assert whole_again["spans"] == [[str(WHOLE[0]), str(HELD[1])]]
    assert whole_again["rows"] == partial["rows"] + second["rows"]


def test_the_two_transports_write_the_same_series_into_two_workspaces(
    runner: CliRunner, wired_bulk: Replay, tmp_path: Path
) -> None:
    """One vendor, one session, one bar — whichever transport a workspace was filled over.

    Two workspaces are filled from the same frozen vendor rows, one over the object store
    and one over the API, and their catalogs are compared to each other rather than to a
    constant. That is what a `backfill` over the bulk path followed by a `sync` over the
    request path relies on, and it is what a bulk loader stamping a day at the session
    close and a request loader stamping the same day at its window's close broke: both
    sides stayed self-consistent, and the two conventions only met in one series.
    """
    catalogs: dict[str, dict[str, Any]] = {}
    for loader in TRANSPORTS:
        root = tmp_path / loader
        root.mkdir()
        assert at(runner, root, "init", root).exit_code == Exit.OK
        write_massive_instruments(root)
        assert at(runner, root, "data", "instruments", "resolve").exit_code == Exit.OK
        spec = write_massive_spec(root, loader, span=WHOLE)
        run(runner, root, "data", "load", "--loader", loader, "--spec", spec)
        found = series_of(runner, root)
        catalogs[loader] = {
            "instrument": found["instrument"],
            "type": found["type"],
            "resolution": found["resolution"],
            "rows": found["rows"],
            "spans": found["spans"],
            "checksums": [item["checksum"] for item in found["datasets"]],
            "publication": [item["publication"] for item in found["datasets"]],
            "adjusted": [item["adjusted"] for item in found["datasets"]],
        }

    assert catalogs["massive_bars"] == catalogs["massive_bulk"]
    assert catalogs["massive_bulk"]["rows"] > 0
    assert catalogs["massive_bulk"]["spans"] == [[str(WHOLE[0]), str(WHOLE[1])]]


@pytest.mark.parametrize("loader", TRANSPORTS)
def test_neither_transport_puts_a_credential_in_what_it_writes(
    runner: CliRunner, vendor: Path, loader: str
) -> None:
    """A manifest records the request that fetched a dataset, and a key is not one of them.

    Both transports carry their parameters into the manifest so a later `sync` can rebuild
    the request without the spec — which is exactly the record a credential must never
    reach, since it is written to the workspace and read by everything downstream. All
    three of the adapter's names are set to one string here, so a leak of any of them is
    one assertion.
    """
    spec = write_massive_spec(vendor, loader, span=HELD)
    run(runner, vendor, "data", "load", "--loader", loader, "--spec", spec)

    written = sorted(manifests_path(find(vendor)).glob("*.yaml"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in written)

    assert written
    assert "request_params" in text
    assert massive_wire.KEY not in text
