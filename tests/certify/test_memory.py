"""What a certification holds: one read and one chunk of a window, whatever its plan runs.

A certification makes many engine runs in the process certifying — both windows, a host or a
hold beside each, one per parameter `param_plateau` moves, and the composition a pass implies.
Measured on 2026-10-14 on a synthetic month of one name's quotes (kanso 0.15.1.dev5,
nautilus_trader 1.231.0), before the windows streamed: the traced Python heap held 216 bytes
for every point of the certification window from the first run to the last, and peaked at
about three and a half times a window's points during each run, so the peak grew with the
windows' length and not with the number of runs. On an operator's tick hypothesis that was
17 GB in-process, and a lane's certification child was killed at 2.78 GB beside cards of the
same run that peak at 0.4 GB.

The numbers here are the traced Python heap (`tracemalloc`), which counts every point the
engine and kanso allocate and nothing the host does with them — no resident set, no
compression, no clock — so the comparison is the same on any machine. The window is cut in
reads and chunks far smaller than the defaults, so the bound a chunk sets shows at a size the
suite can afford, and both windows hold more quotes than the engine's cache keeps of an
instrument (10,000 ticks on nautilus_trader 1.231.0), so the cache is full, and the same
size, in every run.
"""

from __future__ import annotations

import gc
import math
import shutil
import tracemalloc
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from kanso.certify import run
from kanso.certify.run import certify
from kanso.criteria.run import midnight_ns
from kanso.data import catalog, snapshot
from kanso.data.instruments import resolve_universe
from kanso.data.loader import DatasetRef
from kanso.data.manifest import catalog_path
from kanso.env import write as write_envelope
from kanso.nautilus import backtest
from kanso.schemas import Hypothesis
from kanso.state import StateStore
from kanso.workspace import Workspace, find, init
from tests.research.conftest import DOCUMENT, ENVELOPE, classify

from .test_run import CERT_GATES, a_card, venue_model, write_plan

HYP_ID = "dense_follow"
NAME = "DENSE.XNAS"
RESEARCH = (date(2024, 1, 1), date(2024, 1, 8))
CERTIFICATION = (date(2024, 1, 15), date(2024, 1, 22))
PER_DAY = {RESEARCH: 2_100, CERTIFICATION: 1_400}
"""Quotes a session: eight sessions of the certification window hold more than the engine's
cache keeps of an instrument, and eight of the research window half as many again. The
sleeve trades the sine's hours rather than its quotes, so both windows trade alike and differ
in their points alone: what a run's fills cost is the run's own and is not what is measured."""
SECOND_NS = 1_000_000_000
OPEN_NS = (14 * 3_600 + 30 * 60) * SECOND_NS
SESSION_NS = int(6.5 * 3_600) * SECOND_NS

READ_POINTS = 2_000
CHUNK_POINTS = 1_000
"""The read and chunk caps the certification here runs under, in place of 250,000 each."""

FIELDS = 2
"""The sleeve's numeric fields: `param_plateau` moves each both ways, four runs in all."""

SOURCE = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    entry: float = 0.05
    unread: float = 1.0


class Strategy(KansoStrategy):
    """Follows the mid: in on a rise of `entry` off its low, out on a fall of it off its high."""

    config_cls = Config

    def on_start(self) -> None:
        self.low = None
        self.high = None
        self.long = False

    def on_quote_tick(self, tick) -> None:
        mid = (tick.bid_price.as_double() + tick.ask_price.as_double()) / 2
        step = self.kanso_config.entry
        if self.long:
            self.high = max(self.high, mid)
            if mid < self.high - step:
                self.submit_exit(tick.instrument_id)
                self.long, self.low = False, mid
        else:
            self.low = mid if self.low is None else min(self.low, mid)
            if mid > self.low + step:
                self.submit_entry(tick.instrument_id, "BUY", notional=5_000.0)
                self.long, self.high = True, mid
'''
"""A sine of an hour's period rewards following it, so the subject passes the window gate and
the plateau runs every move rather than refusing a loss."""

DOC: dict[str, Any] = {
    **DOCUMENT,
    "id": HYP_ID,
    "universe": [NAME],
    "resolution": "tick",
    "horizon": "1d",
    "data_requirements": ["quote"],
    "windows": {
        "research": {"start": RESEARCH[0], "end": RESEARCH[1]},
        "certification": {"start": CERTIFICATION[0], "end": CERTIFICATION[1]},
        "forward": {"start": date(2024, 2, 1)},
    },
}

PLATEAU = {
    "id": "param_plateau",
    "stage": "cert",
    "params": {"perturb_pct": 10.0, "keep_fraction": 0.0},
    "rationale": "a backtest per parameter moved",
}


def quotes(day: date, index: int, count: int) -> list[QuoteTick]:
    """`count` quotes of one session, the mid a sine of an hour's period whose swing differs
    from session to session, so a session's return does too."""
    swing = 0.15 + 0.03 * ((index * 7) % 5)
    opens = midnight_ns(day) + OPEN_NS
    made: list[QuoteTick] = []
    for at in range(count):
        ts = opens + at * (SESSION_NS // count)
        mid = 10.0 + swing * math.sin(2 * math.pi * (ts - opens) / (3_600 * SECOND_NS))
        made.append(
            QuoteTick(
                InstrumentId.from_str(NAME),
                Price(round(mid - 0.01, 2), 2),
                Price(round(mid + 0.01, 2), 2),
                Quantity.from_int(100),
                Quantity.from_int(100),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return made


def sessions() -> list[tuple[date, int]]:
    """Every day of both windows, with the quotes it holds."""
    return [
        (date.fromordinal(ordinal), count)
        for window, count in PER_DAY.items()
        for ordinal in range(window[0].toordinal(), window[1].toordinal() + 1)
    ]


@pytest.fixture(scope="module")
def quoted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The name's quotes over both windows, frozen into a snapshot once for the module."""
    root = tmp_path_factory.mktemp("quoted") / "ws"
    ws = init(root)
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
    entry = {
        "nautilus_id": NAME,
        "asset_class": "EQUITY",
        "manual": True,
        "corporate_actions": "none",
        "override": {"currency": "USD", "price_increment": "0.01", "lot_size": "1"},
    }
    ws.path("instruments.yaml").write_text(yaml.safe_dump({NAME: entry}), encoding="utf-8")
    resolve_universe(ws, [NAME], RESEARCH[0])
    points = [
        point
        for index, (day, count) in enumerate(sessions())
        for point in quotes(day, index, count)
    ]
    ref = DatasetRef(
        dataset_id="",
        instrument=NAME,
        type="quote",
        resolution=None,
        span=(RESEARCH[0], CERTIFICATION[1]),
        adjusted=False,
        publication="realtime",
    )
    catalog.write(ws, points, ref=ref, source="synthetic")
    snapshot.freeze(ws)
    write_envelope(ws, ENVELOPE)
    return root


@pytest.fixture
def ws(quoted: Path, tmp_path: Path) -> Workspace:
    """A fresh copy of the quoted workspace."""
    root = tmp_path / "ws"
    shutil.copytree(quoted, root)
    return find(root)


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


@dataclass(frozen=True)
class Ran:
    """One engine run of the certification: its window, what it moved, and the traced
    heap's peak while it ran."""

    window: tuple[date, date]
    overrides: dict[str, float]
    peak: int


def runs_of(monkeypatch: pytest.MonkeyPatch) -> list[Ran]:
    """Every engine run the certification makes, as it makes it: each goes through
    `execute_chunked`, the subject's own and every one `backtest.run` makes."""
    seen: list[Ran] = []
    real = backtest.execute_chunked

    def watched(request: Any, instruments: Any, chunks: Any, **kwargs: Any) -> Any:
        tracemalloc.reset_peak()
        result = real(request, instruments, chunks, **kwargs)
        seen.append(
            Ran(request.window, dict(request.overrides), tracemalloc.get_traced_memory()[1])
        )
        return result

    monkeypatch.setattr(backtest, "execute_chunked", watched)
    return seen


def window_held(ws: Workspace) -> int:
    """What the certification window costs held whole: its points, read at once."""
    request = backtest.RunRequest(
        hyp=Hypothesis.model_validate(DOC),
        strategy_source=SOURCE,
        window=CERTIFICATION,
        snapshot_id="a" * 64,
        venue_model=venue_model().model_dump(),
        capital=100_000.0,
    )
    gc.collect()
    tracemalloc.start()
    try:
        floor = tracemalloc.get_traced_memory()[0]
        held = backtest.window_data(request, catalog_path(ws))
        size = tracemalloc.get_traced_memory()[0] - floor
        del held
    finally:
        tracemalloc.stop()
    return size


def test_a_certification_s_peak_grows_with_neither_its_runs_nor_its_window(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The research window's run, half as many points again, peaks within a fifth of the
    certification window's points of the plateau's runs; and each of those peaks within a
    tenth of them of the others, however many came before it. A certification holds one read
    and one chunk of whichever window it is running, and nothing of a run once it has ended:
    not its window, and not what its engine left to the cyclic collector."""
    held = window_held(ws)
    classify(ws, store, DOC, SOURCE)
    a_card(ws, store, SOURCE, hyp_id=HYP_ID, document=DOC)
    write_plan(ws, HYP_ID, gates=[*CERT_GATES, PLATEAU])
    monkeypatch.setattr(backtest, "READ_POINTS", READ_POINTS)
    monkeypatch.setattr(backtest, "CHUNK_POINTS", CHUNK_POINTS)
    # The replay streams its window in the same chunks and is held to them by its own
    # suite; a node replay of these windows would cost this one more than it tells.
    monkeypatch.setattr(run, "_parity", lambda *_: None)
    ran = runs_of(monkeypatch)
    gc.collect()
    tracemalloc.start()
    try:
        made = certify(ws, store, HYP_ID)
    finally:
        tracemalloc.stop()

    (plateau,) = [gate for gate in made.gates if gate.id == "param_plateau"]
    assert plateau.skipped is None and plateau.evidence["n_backtests"] == 2 * FIELDS
    assert made.verdict == "pass", "a pass composes, which is one more run of the window"
    subject, research, *moved, composed = ran
    assert subject.window == composed.window == CERTIFICATION and not subject.overrides
    assert research.window == RESEARCH
    assert len(moved) == 2 * FIELDS and all(each.overrides for each in moved)
    peaks = [each.peak for each in moved]
    assert research.peak - max(peaks) < held / 5, (
        f"the research window's run peaked {research.peak - max(peaks):,} bytes over the "
        f"certification window's; the certification window held whole is {held:,}"
    )
    assert max(peaks) - min(peaks) < held / 10, (
        f"the plateau's runs peaked {peaks}, apart by {max(peaks) - min(peaks):,} bytes; the "
        f"certification window held whole is {held:,}"
    )
