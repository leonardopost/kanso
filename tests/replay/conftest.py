"""A workspace whose catalog reaches past the forward window, so there is something to replay.

Replay runs the window nothing else may touch: from the forward window's start to the last
day the catalog serves. The research suite's workspace stops at the end of certification, so
this one extends the same saw-tooth into March and adds what a replay needs beside it — a
card to name a hypothesis by, and a composed version to name a strategy by.

Everything is synthetic and every instrument is `manual`, so the suite is green with every
credential unset.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from kanso import __version__, strategy
from kanso.criteria import criteria_version
from kanso.criteria.run import midnight_ns
from kanso.data import catalog, snapshot
from kanso.data.instruments import resolve_universe
from kanso.data.loader import DatasetRef
from kanso.env import write as write_envelope
from kanso.hyp import add as register
from kanso.hyp import set_status
from kanso.hyp import show as registration_of
from kanso.nautilus.backtest import RunRequest
from kanso.research import records
from kanso.schemas import (
    Card,
    Detected,
    Envelope,
    Hypothesis,
    Plan,
    RunRecord,
    StrategyFile,
    resolve_venue_model,
    write_yaml,
)
from kanso.schemas.venue import CostsOverride
from kanso.state import StateStore
from kanso.strategy.impl import generate as generate_impl
from kanso.workspace import Workspace, find, init

SYMBOL = "DEMO"
VENUE = "XNAS"
INSTRUMENT = f"{SYMBOL}.{VENUE}"
HYP_ID = "demo_mr"
CAPITAL = 100_000.0

RESEARCH = (date(2024, 1, 1), date(2024, 1, 31))
CERTIFICATION = (date(2024, 2, 6), date(2024, 2, 29))
FORWARD_START = date(2024, 3, 1)
FORWARD = (FORWARD_START, date(2024, 3, 31))
SPAN = (date(2024, 1, 1), date(2024, 3, 31))

SECOND_NS = 1_000_000_000
CLOSE_NS = 16 * 3_600 * SECOND_NS
CYCLE = 4

REVERTING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys the trough and sells the peak of a four-day saw-tooth."""

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

FLAT = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Trades nothing at all."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        pass
'''

RAISING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Asks the impossible of the first bar it sees."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        raise RuntimeError("the replay asked for the impossible")
'''

BLOCKING_FILTER = b'''
from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    allow: bool = False
    scope: str = "time"


class Modifier(KansoModifier):
    """Refuses every entry it is asked about, so the host never opens a position."""

    construct = "filter"
    config_cls = Config

    def evaluate(self, ctx):
        return Decision(allow=self.modifier_config.allow)
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
        "forward": {"start": FORWARD_START},
    },
    "construct": {"id": "sleeve", "rationale": "a strategy of its own"},
    "objective": {"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 0.5}},
    "constraints": [{"id": "strategy_integrity"}],
}

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


def document(**changes: Any) -> dict[str, Any]:
    """The classified hypothesis with these fields replaced."""
    return {**DOCUMENT, **changes}


def instrument(symbol: str = SYMBOL) -> Equity:
    """One US equity, priced in cents and traded in whole shares."""
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue(VENUE)),
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def bar_type(symbol: str = SYMBOL) -> BarType:
    """The daily external bar type a `1d` hypothesis subscribes to."""
    return BarType(
        InstrumentId(Symbol(symbol), Venue(VENUE)),
        BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def price(index: int) -> float:
    """A four-day saw-tooth between 10.00 and 11.00."""
    return 10.0 + 0.5 * min(index % CYCLE, CYCLE - index % CYCLE)


def bars(window: tuple[date, date], symbol: str = SYMBOL) -> list[Bar]:
    """One bar per calendar day of the window, published a second after each close."""
    made: list[Bar] = []
    for index in range((window[1] - window[0]).days + 1):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        close = price(index)
        made.append(
            Bar(
                bar_type(symbol),
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


def hypothesis(**changes: Any) -> Hypothesis:
    """The classified hypothesis as a model, with these fields replaced."""
    return Hypothesis.model_validate(document(**changes))


def quotes(window: tuple[date, date], symbol: str = SYMBOL) -> list[QuoteTick]:
    """One quote per day, two cents wide around the day's close."""
    made: list[QuoteTick] = []
    for index in range((window[1] - window[0]).days + 1):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        mid = price(index)
        made.append(
            QuoteTick(
                InstrumentId(Symbol(symbol), Venue(VENUE)),
                Price(mid - 0.01, 2),
                Price(mid + 0.01, 2),
                Quantity.from_int(100),
                Quantity.from_int(100),
                ts_event=ts_event,
                ts_init=ts_event + SECOND_NS,
            )
        )
    return made


def trades(window: tuple[date, date], symbol: str = SYMBOL) -> list[TradeTick]:
    """One print per day at the day's close, published a second later."""
    made: list[TradeTick] = []
    for index in range((window[1] - window[0]).days + 1):
        ts_event = midnight_ns(window[0]) + index * 86_400 * SECOND_NS + CLOSE_NS
        made.append(
            TradeTick(
                InstrumentId(Symbol(symbol), Venue(VENUE)),
                Price(price(index), 2),
                Quantity.from_int(500),
                AggressorSide.BUYER,
                TradeId(f"T-{symbol}-{index}"),
                ts_event=ts_event,
                ts_init=ts_event + SECOND_NS,
            )
        )
    return made


def request_for(
    *,
    source: bytes = REVERTING,
    window: tuple[date, date] = FORWARD,
    hyp: Hypothesis | None = None,
    modifiers: tuple[tuple[str, bytes, dict[str, Any]], ...] = (),
) -> RunRequest:
    """The run both code paths are handed in the session tests."""
    subject = hyp or hypothesis()
    return RunRequest(
        hyp=subject,
        strategy_source=source,
        window=window,
        snapshot_id="a" * 64,
        venue_model=venue_model().model_dump(),
        capital=CAPITAL,
        modifiers=modifiers,
    )


def dataset(instrument_id: str = INSTRUMENT, span: tuple[date, date] = SPAN) -> DatasetRef:
    """The dataset reference the catalog files a write under."""
    return DatasetRef(
        dataset_id="",
        instrument=instrument_id,
        type="bar",
        resolution="1d",
        span=span,
        adjusted=False,
        publication="realtime",
    )


def venue_model() -> Any:
    """The resolved model every card here is costed with."""
    return resolve_venue_model(
        VENUE,
        broker="synthetic",
        hypothesis_costs=CostsOverride.model_validate(DOCUMENT["costs"]),
        max_leverage=1.0,
        quotes_available=False,
    )


def write_instruments(ws: Workspace, ids: tuple[str, ...] = (INSTRUMENT,)) -> None:
    """Manual entries, so resolution never reaches a reference adapter."""
    entries = {
        identifier: {
            "nautilus_id": identifier,
            "asset_class": "EQUITY",
            "manual": True,
            "corporate_actions": "none",
            "override": {"currency": "USD", "price_increment": "0.01", "lot_size": "1"},
        }
        for identifier in ids
    }
    ws.path("instruments.yaml").write_text(
        yaml.safe_dump(entries, sort_keys=False), encoding="utf-8"
    )


def write_hypothesis(
    ws: Workspace, doc: dict[str, Any], strategy: bytes = REVERTING, hyp_id: str | None = None
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
    root = tmp_path_factory.mktemp("prepared-replay") / "ws"
    ws = init(root)
    write_instruments(ws)
    resolve_universe(ws, [INSTRUMENT], RESEARCH[0])
    catalog.write(ws, bars(SPAN), ref=dataset(), source="synthetic")
    snapshot.freeze(ws)
    write_envelope(ws, ENVELOPE)
    return root


@pytest.fixture
def ws(prepared: Path, tmp_path: Path) -> Workspace:
    """A fresh copy of that workspace, so one test's sessions never reach another's."""
    root = tmp_path / "ws"
    shutil.copytree(prepared, root)
    return find(root)


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store for that workspace."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


def carded(
    ws: Workspace,
    store: StateStore,
    *,
    doc: dict[str, Any] | None = None,
    strategy: bytes = REVERTING,
    host_version: int | None = None,
) -> str:
    """A registered hypothesis with one kept card, which is what `--hyp` replays.

    The run and the card are written straight into the store rather than researched: what
    replay reads of them is the snapshot, the venue model and the bytes, and a real research
    loop would take minutes to produce the same three things.
    """
    document_ = doc or DOCUMENT
    hyp = register(ws, store, write_hypothesis(ws, document_, strategy))
    set_status(store, hyp.id, "certified")
    pinned = registration_of(ws, store, hyp.id)
    sha = store.put_blob(strategy)
    program = store.put_blob(PROGRAM)
    frozen = snapshot.snapshots(ws)[-1]
    run = records.insert(
        store,
        RunRecord(
            run_id=f"run-{hyp.id}",
            hyp_id=hyp.id,
            tag="20240301-1",
            lane="op",
            dir=str(ws.path("runs", "op", hyp.id)),
            base_sha=sha,
            hypothesis_sha=str(pinned.hypothesis_sha),
            program_sha=program,
            snapshot_id=frozen.snapshot_id,
            criteria_version=criteria_version(),
            host_version=host_version,
            card_budget_s=60.0,
            baseline_wall_s=1.0,
            baseline_peak_mem_gb=1.0,
            started_at=datetime(2024, 3, 1, tzinfo=UTC),
        ),
    )
    records.record_card(
        store,
        run,
        Card(
            run_id=run.run_id,
            lane="op",
            strategy_sha=sha,
            metric=1.0,
            metric_se=0.1,
            n_trials=1,
            n_trades=4,
            wall_s=1.0,
            peak_mem_gb=1.0,
            status="keep",
            desc="the card replay replays",
            venue_model=venue_model(),
            created_at=datetime(2024, 3, 1, tzinfo=UTC),
        ),
    )
    records.set_best(store, run, sha, 1.0)
    records.close(store, run)
    return hyp.id


def composed(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    sleeve: bytes = REVERTING,
    attached: tuple[tuple[str, str, bytes, dict[str, Any]], ...] = (),
) -> StrategyFile:
    """A composed strategy file, as `strat compose` would leave one.

    Composition belongs to another module; what replay needs of a version is the sleeve, the
    constructs attached to it and the pins, so this writes exactly those.
    """
    frozen = snapshot.snapshots(ws)[-1]
    pins = {
        "kanso_version": __version__,
        "nautilus_version": "1.231.0",
        "criteria_version": criteria_version(),
        "plan_version": 1,
        "snapshot_id": frozen.snapshot_id,
        "venue_model": venue_model().model_dump(),
    }
    expectation = {
        "objective_id": "wf_sharpe_net",
        "value": 1.0,
        "ci90": [0.5, 1.5],
        "mdd_p95": 0.1,
        "window": {"start": CERTIFICATION[0], "end": CERTIFICATION[1]},
    }
    version: dict[str, Any] = {
        "version": 1,
        "sleeve": {"hyp_id": hyp_id, "strategy_sha": store.put_blob(sleeve)},
        "attached": [
            {
                "hyp_id": attached_id,
                "strategy_sha": store.put_blob(source),
                "construct": construct,
                "params": params,
            }
            for attached_id, construct, source, params in attached
        ],
        "config": {},
        "pins": pins,
        "expectation": expectation,
        "state": "composed",
        "created_at": datetime(2024, 3, 1, tzinfo=UTC),
    }
    file = StrategyFile.model_validate({"schema": 1, "id": hyp_id, "versions": [version]})
    path = strategy.strategy_file(ws, hyp_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(file, path)
    generate_impl(
        ws,
        store,
        hyp_id,
        file.latest(),
        hypothesis(id=hyp_id),
        CAPITAL,
        created_at=datetime(2024, 3, 1, tzinfo=UTC),
    )
    return file


@pytest.fixture
def carded_hyp(ws: Workspace, store: StateStore) -> str:
    """The demo hypothesis with a kept card of the reverting sleeve."""
    return carded(ws, store)
