"""Fixtures for the CLI slice: a runner, the three repository situations, a clean environment.

Every command is exercised through typer's `CliRunner`, which is the closest thing to the
console script that a test can invoke without a subprocess: the same argument parsing, the
same exit code, and standard output and standard error kept apart, so a `--json` test can
assert that the object is alone on standard output.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import Result
from typer.testing import CliRunner

from kanso.cli import app
from kanso.models import reset_mock

from . import mocked


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `KANSO_` variable of the developer's shell reaches a test."""
    for name in [name for name in os.environ if name.startswith("KANSO_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def fresh_cursors() -> Iterator[None]:
    """The mock protocol's cursors live for the process, so every test reads from the start."""
    reset_mock()
    yield
    reset_mock()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fresh(tmp_path: Path) -> Path:
    """A directory with nothing in it and no repository anywhere above."""
    directory = tmp_path / "ws"
    directory.mkdir()
    return directory


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A directory that is itself the root of a git repository."""
    directory = tmp_path / "repo"
    (directory / ".git" / "objects").mkdir(parents=True)
    (directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (directory / "README.md").write_text("# repo\n", encoding="utf-8")
    return directory


@pytest.fixture
def monorepo(repo: Path) -> Path:
    """A subdirectory deep inside an existing repository."""
    directory = repo / "apps" / "research"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def workspace(runner: CliRunner, fresh: Path) -> Path:
    """A scaffolded workspace: `kanso init` in a fresh directory, asserted green."""
    assert run(runner, "init", fresh).exit_code == 0
    return fresh


def run(runner: CliRunner, *args: object) -> Result:
    """Invoke the application with the arguments stringified, as a shell would."""
    return runner.invoke(app, [str(arg) for arg in args])


def at(runner: CliRunner, root: Path, *args: object) -> Result:
    """Invoke a command against a workspace, as `kanso --workspace ROOT ...`."""
    return run(runner, "--workspace", root, *args)


def payload(result: Result) -> dict[str, Any]:
    """The single JSON object a `--json` invocation printed on standard output."""
    document = json.loads(result.stdout)
    assert isinstance(document, dict)
    return document


# -- a workspace with data in it ---------------------------------------------------

SYMBOL = "DEMO"
VENUE = "SIM"
INSTRUMENT = f"{SYMBOL}.{VENUE}"
HYP_ID = "demo_mr"

FIRST = date(2024, 1, 2)
LAST = date(2024, 6, 28)
RESEARCH = (FIRST, date(2024, 3, 29))
CERTIFICATION = (date(2024, 4, 1), date(2024, 5, 31))
"""Hourly bars over half a year: enough for four folds of research and a certification
window a day after it, and small enough that a card is a second rather than a minute."""

SPEC: dict[str, Any] = {
    "loader": "synthetic",
    "model": "ou",
    "seed": 7,
    "instruments": [SYMBOL],
    "venue": VENUE,
    "resolution": "1h",
    "types": ["bar"],
    "start": str(FIRST),
    "end": str(LAST),
    "sigma_bps": 40,
    "kappa": 0.5,
}
"""A briskly reverting path: the deviations are large against the cost model, so a card
that fades them earns an edge a test can tell from noise."""

INSTRUMENTS: dict[str, Any] = {
    INSTRUMENT: {
        "nautilus_id": INSTRUMENT,
        "asset_class": "EQUITY",
        "manual": True,
        "corporate_actions": "none",
        "override": {"currency": "USD", "price_increment": "0.01", "lot_size": "1"},
    }
}
"""One manual entry, so nothing here resolves through a vendor or reaches a network."""

HYPOTHESIS: dict[str, Any] = {
    "schema": 1,
    "id": HYP_ID,
    "title": "Demo: hourly mean reversion on a synthetic OU series",
    "thesis": "The series reverts within the session, so fading a deviation pays.",
    "mechanism": "mean_reversion",
    "universe": [INSTRUMENT],
    "horizon": "4h",
    "resolution": "1h",
    "data_requirements": ["bar"],
    "costs": {"commission_bps": 0.5, "slippage_bps": 1.0, "spread": "fixed_bps", "fixed_bps": 2},
    "risk_limits": {"max_position_pct": 20, "max_drawdown_pct": 40, "max_leverage": 1},
    "windows": {
        "research": {"start": str(RESEARCH[0]), "end": str(RESEARCH[1])},
        "certification": {"start": str(CERTIFICATION[0]), "end": str(CERTIFICATION[1])},
        "forward": {"start": "2024-06-03"},
    },
    "construct": {"id": "sleeve", "rationale": "a strategy of its own"},
    "objective": {"id": "net_edge_bps", "params": {"min_delta": 0.0, "k_se": 0.5}},
    "constraints": [{"id": "strategy_integrity"}, {"id": "min_trades", "params": {"min": 4}}],
}
"""A classified sleeve, as `classify` would leave it; the operator override path writes
exactly this by hand, which is how a hypothesis is classified before the driver exists."""

FLAT = '''from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Trades nothing, so the baseline keeps no metric and no constraint passes."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        pass
'''

FADE = '''from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    lookback: int = {lookback}
    notional: float = 20_000.0


class Strategy(KansoStrategy):
    """Buys a close below its rolling mean and sells it back above."""

    config_cls = Config

    def on_start(self) -> None:
        self.closes: list[float] = []
        self.long = False

    def on_bar(self, bar) -> None:
        window = self.kanso_config.lookback
        self.closes.append(float(bar.close))
        if len(self.closes) > window:
            self.closes.pop(0)
        if len(self.closes) < window:
            return
        mean = sum(self.closes) / window
        spread = max(self.closes) - min(self.closes)
        if spread <= 0.0:
            return
        deviation = (self.closes[-1] - mean) / spread
        if deviation < {low} and not self.long:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.long = True
        elif deviation > {high} and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''
"""A strategy that fades a deviation of the close from its rolling mean, as a template:
the three parameters are what a researcher would edit between cards."""


def fade(lookback: int = 12, low: float = -0.3, high: float = 0.1) -> str:
    """The fading strategy at these parameters, as `strategy.py` bytes."""
    return FADE.format(lookback=lookback, low=low, high=high)


REVERTING = fade()
"""The first card that trades: it keeps, because a run with no best keeps anything sound."""

WEAK = fade(lookback=6, low=-0.4, high=0.0)
"""A shorter memory earns less on the same path, so this card discards against that best."""

BETTER = fade(lookback=24, low=-0.35, high=0.0)
"""A longer memory earns more than either, so this card keeps and moves the best."""

RAISING = '''from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Asks the impossible of the first bar it sees."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        raise RuntimeError("the card asked for the impossible")
'''

READING = '''from nautilus_trader.persistence.catalog import ParquetDataCatalog

from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Reaches for the catalog, which is refused before anything runs."""

    config_cls = Config

    def on_bar(self, bar) -> None:
        ParquetDataCatalog(open("/etc/hosts").read())
'''


def write_spec(root: Path, name: str = "data.yaml", **changes: Any) -> Path:
    """A synthetic loader spec in the workspace, with these fields replaced."""
    path = root / name
    path.write_text(yaml.safe_dump({**SPEC, **changes}, sort_keys=False), encoding="utf-8")
    return path


def write_instruments(root: Path) -> Path:
    """The manual instrument entry the whole slice resolves against."""
    path = root / "instruments.yaml"
    path.write_text(yaml.safe_dump(INSTRUMENTS, sort_keys=False), encoding="utf-8")
    return path


def write_hypothesis(
    root: Path, strategy: str = FLAT, *, base: dict[str, Any] | None = None, **changes: Any
) -> Path:
    """The three scoped files of the demo hypothesis; returns its `hypothesis.yaml`.

    `base` replaces the document the changes are applied to, so a caller can write the
    same idea with fewer keys than the classified one — a draft is not the classified
    document with three keys overwritten, it is a document those keys are absent from.
    """
    document = {**(HYPOTHESIS if base is None else base), **changes}
    directory = root / "hypotheses" / str(document["id"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hypothesis.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    (directory / "program.md").write_text("# program\n\nEdit strategy.py, run a card.\n")
    (directory / "strategy.py").write_text(strategy, encoding="utf-8")
    return path


HOST_ID = "demo_host"
"""A composed strategy standing in the workspace, so a construct that needs a host has one."""


def certify(root: Path, strategy_id: str = HOST_ID) -> Path:
    """Write a composed `strategies/<id>/strategy.yaml`, as composition later will.

    Classification reads what a certified strategy trades, never how well it traded, so
    the fields that matter here are its id and the sleeve it names; the rest is the
    smallest document the schema accepts.
    """
    from kanso.schemas import Expectation, Pins, StrategyFile, resolve_venue_model, write_yaml

    document = StrategyFile.model_validate(
        {
            "schema": 1,
            "id": strategy_id,
            "versions": [
                {
                    "version": 1,
                    "sleeve": {"hyp_id": strategy_id, "strategy_sha": "b" * 64},
                    "attached": [],
                    "config": {},
                    "pins": Pins.model_validate(
                        {
                            "kanso_version": "0.1.0",
                            "nautilus_version": "1.231.0",
                            "criteria_version": "0.1.0",
                            "plan_version": 1,
                            "snapshot_id": "snap1",
                            "venue_model": resolve_venue_model(VENUE, max_leverage=1.0),
                        }
                    ),
                    "expectation": Expectation.model_validate(
                        {
                            "objective_id": "net_edge_bps",
                            "value": 1.0,
                            "ci90": [0.2, 1.8],
                            "mdd_p95": 5.0,
                            "window": {
                                "start": str(CERTIFICATION[0]),
                                "end": str(CERTIFICATION[1]),
                            },
                        }
                    ),
                    "state": "composed",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        }
    )
    directory = root / "strategies" / strategy_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "strategy.yaml"
    write_yaml(document, path)
    return path


def classify(root: Path, hyp_id: str = HYP_ID) -> None:
    """Move a registered hypothesis to `classified`, which `kanso classify` will do.

    The classifier is the next milestone's, and until it lands this is the operator
    override path of the research protocol: the classification is written into the file
    by hand, `hyp add` pins it, and the status moves here.
    """
    from kanso.hyp import set_status
    from kanso.state import StateStore
    from kanso.workspace import find

    with StateStore(find(root).path("state.db")) as store:
        set_status(store, hyp_id, "classified")


@pytest.fixture(scope="session")
def _prepared(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace with the instrument resolved, the bars loaded and a snapshot frozen.

    Built once: the load and the freeze are the slow part, and every test that needs data
    needs exactly this. Each test gets its own copy, so one test's writes never reach
    another's.
    """
    runner = CliRunner()
    root = tmp_path_factory.mktemp("prepared") / "ws"
    assert run(runner, "init", root).exit_code == 0
    write_instruments(root)
    spec = write_spec(root)
    assert at(runner, root, "data", "instruments", "resolve", "--as-of", str(FIRST)).exit_code == 0
    assert at(runner, root, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code == 0
    assert at(runner, root, "data", "snapshot").exit_code == 0
    return root


@pytest.fixture
def loaded(_prepared: Path, tmp_path: Path) -> Path:
    """A fresh copy of that workspace."""
    root = tmp_path / "ws"
    shutil.copytree(_prepared, root, symlinks=True)
    return root


@pytest.fixture
def registered(runner: CliRunner, loaded: Path) -> Path:
    """That workspace with the demo hypothesis written, registered and classified."""
    path = write_hypothesis(loaded)
    assert at(runner, loaded, "hyp", "add", path).exit_code == 0
    classify(loaded)
    return loaded


def lane(root: Path, hyp_id: str = HYP_ID) -> Path:
    """The interactive lane's directory for a hypothesis."""
    return root / "runs" / "op" / hyp_id


def edit(root: Path, source: str, hyp_id: str = HYP_ID) -> Path:
    """Write the lane's `strategy.py`, which is the whole of the agent's interface."""
    path = lane(root, hyp_id) / "strategy.py"
    path.write_text(source, encoding="utf-8")
    return path


DRAFT: dict[str, Any] = {
    key: value
    for key, value in HYPOTHESIS.items()
    if key not in ("construct", "objective", "constraints")
}
"""The same idea with nothing decided about it: what `kanso classify` is given."""


E2E_WINDOWS: dict[str, Any] = {
    "research": {"start": str(RESEARCH[0]), "end": str(RESEARCH[1])},
    "certification": {"start": "2024-05-01", "end": "2024-05-31"},
    "forward": {"start": "2024-06-03"},
}
"""The windows the deployment slice's fixtures use.

A month of certification and four weeks of paper, so that what the paper stage realises is
measured with roughly the precision the band it is judged against was measured with. The
hypothesis's own certification window is two months, which is the right shape for the
certification tests and the wrong one here: a band that tight is one a four-week paper
window falls outside of on ordinary sampling noise, and a fixture that promotes only on a
lucky draw proves nothing about promotion.
"""


def certify_sleeve(root: Path, cards: int = 6) -> None:
    """Take the demo sleeve from an unclassified draft to a certificate, through the CLI.

    Every step is the command an operator would type, because what the deployment slice is
    built on has to be what the earlier commands actually leave behind. The run is ended
    before the certification, so the workspace this leaves is one nothing is running in and
    a later `hyp add` is free to re-pin. A passing verdict composes the version and offers
    it to the paper stage on its own, so this returns with something already deployed.
    """
    runner = CliRunner()
    path = write_hypothesis(root, mocked.SEED, base=DRAFT, windows=E2E_WINDOWS)
    assert at(runner, root, "hyp", "add", path).exit_code == 0
    mocked.scripted(root, classify=[mocked.CLASSIFICATION])
    assert at(runner, root, "classify", HYP_ID).exit_code == 0
    assert at(runner, root, "research", "run", HYP_ID, "--cards", cards).exit_code == 0
    assert at(runner, root, "research", "end", HYP_ID).exit_code == 0
    verdict = payload(at(runner, root, "cert", "run", HYP_ID, "--json"))
    assert verdict["verdict"] == "pass", verdict


@pytest.fixture(scope="session")
def _certified(tmp_path_factory: pytest.TempPathFactory, _prepared: Path) -> Path:
    """A workspace whose sleeve is certified, composed and on the paper stage.

    Built once: research, certification and the deployment that follows are the slow part,
    and every test of what comes after them needs exactly this.
    """
    root = tmp_path_factory.mktemp("certified") / "ws"
    shutil.copytree(_prepared, root, symlinks=True)
    certify_sleeve(root)
    return root


@pytest.fixture
def deployed(_certified: Path, tmp_path: Path) -> Path:
    """A fresh copy of that workspace, so one test's deployment never reaches another's."""
    root = tmp_path / "ws"
    shutil.copytree(_certified, root, symlinks=True)
    return root


def portfolio_document(root: Path) -> dict[str, Any]:
    """`portfolio.yaml` as it stands."""
    parsed = yaml.safe_load((root / "portfolio.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def reconfigure(root: Path, stage: str, **changes: Any) -> dict[str, Any]:
    """Rewrite one stage of `portfolio.yaml`, as an operator editing it would."""
    document = portfolio_document(root)
    document["stages"][stage].update(changes)
    (root / "portfolio.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    return document


def a_client(root: Path, name: str, *, capital: str, clock: str) -> str:
    """A workspace extension declaring one execution client, as a broker package would."""
    directory = root / "kanso_ext"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(
        "from kanso.schemas import ExecutionClientSpec\n\n"
        f'EXEC_CLIENTS = [ExecutionClientSpec(id="{name}", capital="{capital}", '
        f'clock="{clock}")]\n',
        encoding="utf-8",
    )
    return name


@pytest.fixture
def _leave_the_interpreter_as_found() -> Iterator[None]:
    """An extension is imported by name, so a test's own must not outlive it."""
    modules = set(sys.modules)
    path = list(sys.path)
    yield
    for name in set(sys.modules) - modules:
        del sys.modules[name]
    sys.path[:] = path


@pytest.fixture
def mocked_ws(runner: CliRunner, loaded: Path) -> Path:
    """The loaded workspace with a mock-only register and a classified, steerable hypothesis.

    Nothing here can reach a network: the register lists the `mock` protocol on all three
    tiers, and the strategy the scripts steer is the one this slice calibrated its keep,
    discard and crash on.
    """
    path = write_hypothesis(loaded, mocked.SEED)
    assert at(runner, loaded, "hyp", "add", path).exit_code == 0
    classify(loaded)
    mocked.scripted(loaded)
    return loaded
