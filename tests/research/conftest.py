"""A whole workspace a run can begin in, built from synthetic bars and nothing else.

The series is a four-day saw-tooth — 10.00, 10.50, 11.00, 10.50 — over January and
February 2024, published a second after each session closes. It is deliberately trivial
to trade: a strategy that buys the day after a fall and sells the day after a rise makes
about ten percent a cycle, which is far above any cost model, so "improves" and "does not
improve" are facts about the loop rather than accidents of the data.

Every instrument is `manual`, so nothing here resolves through a vendor and the suite is
green with every credential unset.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.run import midnight_ns
from kanso.data import catalog, snapshot
from kanso.data.instruments import resolve_universe
from kanso.data.loader import DatasetRef
from kanso.env import write as write_envelope
from kanso.hyp import add as register
from kanso.hyp import set_status
from kanso.schemas import Detected, Envelope, Plan
from kanso.state import StateStore
from kanso.workspace import Workspace, find, init

SYMBOL = "DEMO"
VENUE = "XNAS"
INSTRUMENT = f"{SYMBOL}.{VENUE}"
HYP_ID = "demo_mr"

RESEARCH = (date(2024, 1, 1), date(2024, 1, 31))
CERTIFICATION = (date(2024, 2, 6), date(2024, 2, 29))
SPAN = (date(2024, 1, 1), date(2024, 2, 29))

SECOND_NS = 1_000_000_000
CLOSE_NS = 16 * 3_600 * SECOND_NS
"""Each session's bar is stamped at 16:00 UTC and published a second later."""

CYCLE = 4
"""The saw-tooth's period in days: two folds of the research window hold several."""

FLAT = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Trades nothing, so every fold reports a zero and no trade."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        pass
'''

REVERTING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys the trough and sells the peak: two falls in a row, then two rises."""

    config_cls = Config

    def on_start(self) -> None:
        self.closes = []
        self.long = False

    def on_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        if len(self.closes) < 3:
            return
        first, second, third = self.closes[-3:]
        if first > second > third and not self.long:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.long = True
        elif first < second < third and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''
"""The saw-tooth's trough is the only day preceded by two falls, so this one trades it."""

WEAK = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys any fall and sells any rise, which on a saw-tooth is a coin toss."""

    config_cls = Config

    def on_start(self) -> None:
        self.previous = None
        self.long = False

    def on_bar(self, bar) -> None:
        price = float(bar.close)
        if self.previous is not None:
            if price < self.previous and not self.long:
                self.submit_entry(
                    bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
                )
                self.long = True
            elif price > self.previous and self.long:
                self.submit_exit(bar.bar_type.instrument_id)
                self.long = False
        self.previous = price
'''
"""One step of memory cannot tell the trough from the day before it, so it earns nothing."""

RAISING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Asks the impossible of the first bar it sees."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        raise RuntimeError("the card asked for the impossible")
'''

READING = b'''
import os

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Reaches outside the lane directory, which is refused before anything runs."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        os.listdir(".")
'''

PROGRAM = b"# program.md\n\nEdit strategy.py, run a card, repeat.\n"

DOCUMENT: dict[str, Any] = {
    "schema": 1,
    "id": HYP_ID,
    "title": "Demo: daily mean reversion on a synthetic saw-tooth",
    "thesis": "The series reverts within four sessions, so buying falls pays.",
    "mechanism": "mean_reversion",
    "universe": [INSTRUMENT],
    "horizon": "1d",
    "resolution": "1d",
    "data_requirements": ["bar"],
    "costs": {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "fixed_bps", "fixed_bps": 2},
    "risk_limits": {"max_position_pct": 20, "max_drawdown_pct": 40, "max_leverage": 1},
    "windows": {
        "research": {"start": RESEARCH[0], "end": RESEARCH[1]},
        "certification": {"start": CERTIFICATION[0], "end": CERTIFICATION[1]},
        "forward": {"start": date(2024, 3, 1)},
    },
    "construct": {"id": "sleeve", "rationale": "a strategy of its own"},
    "objective": {"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 0.5}},
    "constraints": [{"id": "strategy_integrity"}],
}
"""A classified sleeve whose only card gate is the integrity one."""

ENVELOPE = Envelope(
    detected=Detected(
        os="test",
        os_version="1",
        arch="arm64",
        chip="test",
        cores_perf=4,
        cores_eff=4,
        cores_total=8,
        mem_gb=32.0,
        disk_free_gb=100.0,
        on_ac_power=True,
        python="3.12.0",
        nautilus_version="1.231.0",
        nautilus_wheel_ok=True,
    ),
    plan=Plan(
        live_colocated=False,
        reserved_cores=2,
        reserved_mem_gb=8.0,
        cores_per_lane=2,
        mem_per_lane_gb=8.0,
        lanes=2,
    ),
    detected_at="2024-01-01T00:00:00+00:00",
)
"""A fixed envelope: the lane memory share is a setting, not a property of the host."""


def document(**changes: Any) -> dict[str, Any]:
    """The classified hypothesis with these fields replaced."""
    return {**DOCUMENT, **changes}


def instrument() -> Equity:
    """One US equity, priced in cents and traded in whole shares."""
    return Equity(
        instrument_id=InstrumentId(Symbol(SYMBOL), Venue(VENUE)),
        raw_symbol=Symbol(SYMBOL),
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def bar_type() -> BarType:
    """The daily external bar type a `1d` hypothesis subscribes to."""
    return BarType(
        InstrumentId(Symbol(SYMBOL), Venue(VENUE)),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def price(index: int) -> float:
    """A four-day saw-tooth between 10.00 and 11.00."""
    return 10.0 + 0.5 * min(index % CYCLE, CYCLE - index % CYCLE)


def bars(window: tuple[date, date]) -> list[Bar]:
    """One bar per calendar day of the window, published a second after each close."""
    made: list[Bar] = []
    for index in range((window[1] - window[0]).days + 1):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        close = price(index)
        made.append(
            Bar(
                bar_type(),
                Price(close, 2),
                Price(close + 0.25, 2),
                Price(close - 0.25, 2),
                Price(close, 2),
                Quantity.from_int(10_000),
                ts_event=ts_event,
                ts_init=ts_event + SECOND_NS,
            )
        )
    return made


def dataset() -> DatasetRef:
    """The one dataset this workspace holds: daily bars over both windows."""
    return DatasetRef(
        dataset_id="",
        instrument=INSTRUMENT,
        type="bar",
        resolution="1d",
        span=SPAN,
        adjusted=False,
        publication="realtime",
    )


def write_instruments(ws: Workspace) -> None:
    """One manual entry, so resolution never reaches a reference adapter."""
    entry = {
        INSTRUMENT: {
            "nautilus_id": INSTRUMENT,
            "asset_class": "EQUITY",
            "manual": True,
            "corporate_actions": "none",
            "override": {"currency": "USD", "price_increment": "0.01", "lot_size": "1"},
        }
    }
    ws.path("instruments.yaml").write_text(yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")


def write_hypothesis(
    ws: Workspace, doc: dict[str, Any], strategy: bytes = FLAT, hyp_id: str | None = None
) -> Path:
    """Write the three scoped files of a hypothesis and return its `hypothesis.yaml`."""
    directory = ws.path("hypotheses", hyp_id or str(doc["id"]))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hypothesis.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (directory / "program.md").write_bytes(PROGRAM)
    (directory / "strategy.py").write_bytes(strategy)
    return path


@pytest.fixture(scope="session")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace built once: the catalog write and the freeze are the slow part."""
    root = tmp_path_factory.mktemp("prepared") / "ws"
    ws = init(root)
    write_instruments(ws)
    resolve_universe(ws, [INSTRUMENT], RESEARCH[0])
    catalog.write(ws, bars(SPAN), ref=dataset(), source="synthetic")
    snapshot.freeze(ws)
    write_envelope(ws, ENVELOPE)
    return root


@pytest.fixture
def ws(prepared: Path, tmp_path: Path) -> Workspace:
    """A fresh copy of that workspace, so one test's runs never reach another's."""
    root = tmp_path / "ws"
    shutil.copytree(prepared, root)
    return find(root)


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store for that workspace."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


def classify(ws: Workspace, store: StateStore, doc: dict[str, Any], strategy: bytes = FLAT) -> str:
    """Register a hypothesis and move it to `classified`, as `kanso classify` does."""
    hyp = register(ws, store, write_hypothesis(ws, doc, strategy))
    set_status(store, hyp.id, "classified")
    return hyp.id


@pytest.fixture
def registered(ws: Workspace, store: StateStore) -> str:
    """The demo hypothesis, classified, with a flat baseline strategy."""
    return classify(ws, store, DOCUMENT)
