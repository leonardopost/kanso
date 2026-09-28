"""Coverage across a backfill's chunk edges, through the commands an operator runs.

`data backfill` cuts history into chunks of thirty calendar days, and each chunk's manifest
records the span its source served: from the first day that carried data to the last. So
where a chunk's edge meets a weekend or a holiday, two neighbouring chunks' spans sit a day
or three apart with no session between them. Measured on an operator's workspace: 102
chunks of one-minute bars over four years merged into some twenty pieces per instrument,
every break a Sunday or a holiday, and `research begin` refused a window no trading day was
missing from. These tests rebuild that shape from the synthetic generator and hold the
commands to the rule: a day the market was closed joins the spans either side of it, and
a day it opened does not.

The generator serves every weekday, so the source here is the generator behind a market
that does not open on its holidays — the five the exchanges kept between the floor and the
end — and, in one test, a source that lost the nine sessions of the last chunk. It trades
04:00 to 20:00 in New York, as the measured source does, so a Friday's post-market is
stamped on the Saturday in UTC and a chunk that ends on a weekend serves to the Saturday.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner

from kanso.data.closures import US_EQUITY
from kanso.data.loader import DatasetRef
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.data.manifest import EMPTY_CHUNK, manifests, merge
from kanso.data.snapshot import Snapshot, covering
from kanso.errors import Exit
from kanso.schemas.hypothesis import Windows
from kanso.state import StateStore
from kanso.workspace import find

from .conftest import at, payload

SYMBOL = "SOXL"
VENUE = "ARCA"
INSTRUMENT = f"{SYMBOL}.{VENUE}"
"""The venue as the measured workspace spells it, which is not the exchange's MIC."""

FLOOR = date(2023, 10, 2)
TAIL = date(2024, 1, 16)
END = date(2024, 3, 28)
"""The source's first session, the first one a load already holds, and its last."""

HOLIDAYS = frozenset(
    {date(2023, 11, 23), date(2023, 12, 25), date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19)}
)
"""Thanksgiving, Christmas, New Year's Day, Martin Luther King Jr. Day, Washington's Birthday."""

DOWN = frozenset(date(2024, 1, 2) + timedelta(days=n) for n in range(11))
"""2024-01-02 to -12: every session of the backfill's last chunk, which a source may lose."""

WINDOWS = Windows.model_validate(
    {
        "research": {"start": FLOOR, "end": date(2024, 1, 31)},
        "certification": {"start": date(2024, 2, 5), "end": END},
        "forward": {"start": date(2024, 4, 1)},
    }
)
"""Research across both chunk edges that meet a closure, certification in the loaded tail."""

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Market:
    """The synthetic source, serving nothing for the New York sessions of `closed`."""

    closed: frozenset[date]
    id: ClassVar[str] = "synthetic"

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        return SyntheticLoader().discover(spec)

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        return [
            point
            for point in SyntheticLoader().load(ref, window)
            if session(point) not in self.closed
        ]


def session(point: Any) -> date:
    """The New York day a bar closed on, which is the session it belongs to."""
    return datetime.fromtimestamp(int(point.ts_event) // 1_000_000_000, tz=NEW_YORK).date()


def spec(root: Path, name: str, start: date, end: date) -> Path:
    path = root / name
    document = {
        "loader": "synthetic",
        "model": "ou",
        "seed": 11,
        "instruments": [SYMBOL],
        "venue": VENUE,
        "resolution": "1h",
        "types": ["bar"],
        "start": str(start),
        "end": str(end),
        "session_start": "04:00",
        "session_end": "20:00",
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def serving(
    runner: CliRunner, root: Path, monkeypatch: pytest.MonkeyPatch, closed: frozenset[date]
) -> None:
    """The workspace's one equity defined, and every data command reading from `Market`."""
    entry = {
        "nautilus_id": INSTRUMENT,
        "asset_class": "EQUITY",
        "manual": True,
        "corporate_actions": "none",
        "override": {"currency": "USD", "price_increment": "0.01", "lot_size": "1"},
    }
    (root / "instruments.yaml").write_text(yaml.safe_dump({INSTRUMENT: entry}), encoding="utf-8")
    resolved = at(runner, root, "data", "instruments", "resolve", "--as-of", str(FLOOR))
    assert resolved.exit_code == Exit.OK, resolved.stdout
    monkeypatch.setattr("kanso.data.commands.loader_for", lambda ws, loader_id: Market(closed))


def command(runner: CliRunner, root: Path, *args: object) -> dict[str, Any]:
    result = at(runner, root, "data", *args, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def backfilled(
    runner: CliRunner, root: Path, monkeypatch: pytest.MonkeyPatch, closed: frozenset[date]
) -> dict[str, Any]:
    """The tail loaded, the history before it backfilled in chunks, and a snapshot taken."""
    serving(runner, root, monkeypatch, closed)
    command(
        runner, root, "load", "--loader", "synthetic", "--spec", spec(root, "t.yaml", TAIL, END)
    )
    full = spec(root, "full.yaml", FLOOR, END)
    done = command(runner, root, "backfill", "--loader", "synthetic", "--spec", full)
    command(runner, root, "snapshot")
    return done


def served(root: Path) -> list[tuple[date, date]]:
    return sorted(manifest.span for manifest in manifests(find(root)).values())


def pinned(root: Path) -> Snapshot | None:
    return covering(find(root), [INSTRUMENT], ["bar"], "1h", WINDOWS)


def test_chunks_breaking_over_a_weekend_and_a_holiday_cover_a_window_across_them(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured shape: chunk spans a long weekend and New Year's Day apart, one series."""
    done = backfilled(runner, workspace, monkeypatch, HOLIDAYS)

    assert [(chunk["start"], chunk["end"]) for chunk in done["chunks"]] == [
        ("2023-10-02", "2023-10-31"),
        ("2023-11-01", "2023-11-30"),
        ("2023-12-01", "2023-12-30"),
        ("2023-12-31", "2024-01-15"),
    ]
    assert {chunk["outcome"] for chunk in done["chunks"]} == {"written"}
    # What each manifest records is still what was served: Friday's post-market is
    # Saturday's in UTC, and New Year's Day and the Sunday before it served nothing.
    assert served(workspace) == [
        (date(2023, 10, 2), date(2023, 10, 31)),
        (date(2023, 11, 1), date(2023, 11, 30)),
        (date(2023, 12, 1), date(2023, 12, 30)),
        (date(2024, 1, 2), date(2024, 1, 13)),
        (date(2024, 1, 16), date(2024, 3, 29)),
    ]
    assert len(merge(served(workspace))) == 3
    [series] = command(runner, workspace, "show")["series"]
    assert series["spans"] == [["2023-10-02", "2024-03-29"]]
    assert series["gaps"] == []
    assert pinned(workspace) is not None

    again = command(
        runner, workspace, "backfill", "--loader", "synthetic", "--spec", workspace / "full.yaml"
    )

    assert again["requests"] == 0
    assert any("nothing missing" in note for note in again["notes"])


def test_a_chunk_served_nothing_on_days_the_market_opened_still_refuses(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last chunk comes back empty, and its sessions are a hole however few they are.

    The answer is recorded and listed under `empty`, which is why the gap persists, and it
    is asked once: a second backfill reads the record rather than the source. It closes
    nothing, so no snapshot covers a window across it.
    """
    done = backfilled(runner, workspace, monkeypatch, HOLIDAYS | DOWN)

    assert [chunk["outcome"] for chunk in done["chunks"]] == ["written"] * 3 + ["empty"]
    [series] = command(runner, workspace, "show")["series"]
    assert series["spans"] == [["2023-10-02", "2023-12-30"], ["2024-01-16", "2024-03-29"]]
    assert series["gaps"] == [["2023-12-31", "2024-01-15"]]
    assert series["empty"] == [["2023-12-31", "2024-01-15"]]
    assert pinned(workspace) is None

    again = command(
        runner, workspace, "backfill", "--loader", "synthetic", "--spec", workspace / "full.yaml"
    )

    assert again["chunks"] == []
    with StateStore(workspace / "state.db") as store:
        recorded = store.events(kind=EMPTY_CHUNK)
    assert [event.detail for event in recorded] == [{"start": "2023-12-31", "end": "2024-01-15"}]
    assert pinned(workspace) is None


def test_a_single_load_is_read_as_it_always_was(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One dataset per instrument, recorded over what it served, holding its holidays inside.

    It covers what it held and refuses what it did not: a window past its last session
    reaches a day the market opened, and nothing about the calendar changes that.
    """
    serving(runner, workspace, monkeypatch, HOLIDAYS)
    loaded = command(
        runner,
        workspace,
        "load",
        "--loader",
        "synthetic",
        "--spec",
        spec(workspace, "all.yaml", FLOOR, END),
    )
    command(runner, workspace, "snapshot")

    [dataset] = loaded["datasets"]
    assert dataset["dataset_id"] == f"{INSTRUMENT}-bar-1h-raw-20240329"
    assert dataset["span"] == ["2023-10-02", "2024-03-29"]
    assert served(workspace) == [(FLOOR, date(2024, 3, 29))]
    [series] = command(runner, workspace, "show")["series"]
    assert series["spans"] == [["2023-10-02", "2024-03-29"]]
    assert series["gaps"] == []
    assert pinned(workspace) is not None
    beyond = Windows.model_validate(
        {
            "research": {"start": FLOOR, "end": date(2024, 1, 31)},
            "certification": {"start": date(2024, 2, 5), "end": date(2024, 4, 1)},
            "forward": {"start": date(2024, 4, 2)},
        }
    )
    assert covering(find(workspace), [INSTRUMENT], ["bar"], "1h", beyond) is None
    assert US_EQUITY.closed(date(2024, 3, 29)) and not US_EQUITY.closed(date(2024, 4, 1))
