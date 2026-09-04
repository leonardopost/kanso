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
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import Result
from typer.testing import CliRunner

from kanso.cli import app


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `KANSO_` variable of the developer's shell reaches a test."""
    for name in [name for name in os.environ if name.startswith("KANSO_")]:
        monkeypatch.delenv(name, raising=False)


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


def write_hypothesis(root: Path, strategy: str = FLAT, **changes: Any) -> Path:
    """The three scoped files of the demo hypothesis; returns its `hypothesis.yaml`."""
    document = {**HYPOTHESIS, **changes}
    directory = root / "hypotheses" / str(document["id"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hypothesis.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    (directory / "program.md").write_text("# program\n\nEdit strategy.py, run a card.\n")
    (directory / "strategy.py").write_text(strategy, encoding="utf-8")
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
