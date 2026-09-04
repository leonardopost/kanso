"""The deterministic half of classification, and the workspace the whole slice is tested in.

Every fixture here is the shipped demo workspace: one synthetic instrument resolved
manually, one mock model listed on every tier, and a script of answers on disk. Nothing
in this directory resolves a credential or opens a socket, and the objective table is
asserted to be total rather than sampled, because classification has no fallback if it
is not.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from kanso.classify.features import (
    attachable,
    card_gates,
    certified,
    construct_catalogue,
    features,
    objectives,
)
from kanso.hyp import add, stub
from kanso.models import reset_mock
from kanso.schemas import (
    Expectation,
    Hypothesis,
    Mechanism,
    Pins,
    StrategyFile,
    embargo_days,
    resolve_venue_model,
    write_yaml,
)
from kanso.state import StateStore
from kanso.workspace import Workspace, init

HYP_ID = "demo_mr"
HOST_ID = "demo_sleeve"
INSTRUMENT = "DEMO.SIM"
COMMENT = "# the operator's own note, which classification must not eat"

DRAFT = f"""\
{COMMENT}
schema: 1
id: {HYP_ID}
title: "Demo: intraday mean reversion on a synthetic OU series"
thesis: "Prices of DEMO.SIM revert to a rolling mean within the hour."
mechanism: mean_reversion
universe: [DEMO.SIM]                 # one synthetic instrument, resolved manually
horizon: 30m
resolution: 1m
data_requirements: [bar]
costs: {{commission_bps: 0.5, slippage_bps: 1.0, spread: fixed_bps, fixed_bps: 2}}
risk_limits: {{max_position_pct: 20, max_drawdown_pct: 15, max_leverage: 1}}
windows:
  research:      {{start: 2024-01-02, end: 2024-12-31}}
  certification: {{start: 2025-01-06, end: 2025-05-30}}
  forward:       {{start: 2025-06-02}}

construct:                           # (set by kanso classify)
objective:
constraints:
"""
"""An unclassified hypothesis, comments and all, as an operator would leave it."""

HOST = DRAFT.replace(f"id: {HYP_ID}", f"id: {HOST_ID}").replace(
    "mechanism: mean_reversion", "mechanism: momentum"
)
"""The sleeve a certified strategy is built on: same universe, another mechanism."""

SECRET = "SECRET_SIGNAL_ONLY_THE_STRATEGY_KNOWS"
"""A marker in the strategy source, so a prompt carrying source is caught by name."""

MEASURED = "7.7777"
"""A number only a measurement of the host holds; no prompt may contain it."""

PINS = Pins.model_validate(
    {
        "kanso_version": "0.1.0",
        "nautilus_version": "1.231.0",
        "criteria_version": "0.1.0",
        "plan_version": 1,
        "snapshot_id": "snap1",
        "venue_model": resolve_venue_model("SIM", max_leverage=1.0),
    }
)

EXPECTATION = Expectation.model_validate(
    {
        "objective_id": "net_edge_bps",
        "value": float(MEASURED),
        "ci90": [6.6666, 8.8888],
        "mdd_p95": 5.5555,
        "window": {"start": "2025-01-06", "end": "2025-05-30"},
    }
)
"""What certification measured of the host. Classification is never shown any of it."""


@pytest.fixture(autouse=True)
def _fresh_cursors() -> Iterator[None]:
    """The mock's cursors live for the process, so each test starts its script over."""
    reset_mock()
    yield
    reset_mock()


def workspace(tmp_path: Path) -> Workspace:
    """The demo workspace: a mock register, a manual instrument, a draft hypothesis."""
    made = init(tmp_path / "ws", demo=True)
    write_hypothesis(made, HYP_ID, DRAFT)
    return made


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """That workspace, per test."""
    return workspace(tmp_path)


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store for that workspace."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


def write_hypothesis(ws: Workspace, hyp_id: str, text: str) -> Path:
    """The three scoped files of one hypothesis, with the sleeve stub as its strategy."""
    directory = ws.path("hypotheses", hyp_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hypothesis.yaml"
    path.write_text(text, encoding="utf-8")
    (directory / "program.md").write_text("# program.md\n", encoding="utf-8")
    (directory / "strategy.py").write_text(stub(hyp_id), encoding="utf-8")
    return path


def register(ws: Workspace, store: StateStore, hyp_id: str, text: str) -> Hypothesis:
    """Write and register a hypothesis, as `kanso hyp add` does."""
    return add(ws, store, write_hypothesis(ws, hyp_id, text))


def strategy_file(strategy_id: str, sleeve_id: str, *attached: dict[str, Any]) -> StrategyFile:
    """A composed strategy: the sleeve at version 1, plus one version per attachment."""
    versions: list[dict[str, Any]] = [
        {
            "version": 1,
            "sleeve": {"hyp_id": sleeve_id, "strategy_sha": "b" * 64},
            "attached": [],
            "config": {"lookback": 20},
            "pins": PINS,
            "expectation": EXPECTATION,
            "state": "composed",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    for number, ref in enumerate(attached, start=2):
        versions.append(
            dict(
                versions[-1],
                version=number,
                attached=[*versions[-1]["attached"], ref],  # type: ignore[misc]
            )
        )
    return StrategyFile.model_validate({"schema": 1, "id": strategy_id, "versions": versions})


def certify(
    ws: Workspace,
    store: StateStore,
    *,
    strategy_id: str = HOST_ID,
    sleeve: str | None = HOST,
    attached: Sequence[dict[str, Any]] = (),
) -> None:
    """Put a composed strategy in the workspace, with or without its sleeve registered."""
    if sleeve is not None:
        register(ws, store, strategy_id, sleeve)
        source = ws.path("hypotheses", strategy_id, "strategy.py")
        source.write_text(f'"""{SECRET}"""\n', encoding="utf-8")
    directory = ws.path("strategies", strategy_id)
    directory.mkdir(parents=True, exist_ok=True)
    write_yaml(strategy_file(strategy_id, strategy_id, *attached), directory / "strategy.yaml")


def script(ws: Workspace, **answers: list[Any]) -> None:
    """Replace the mock model's script of answers, keyed by task class."""
    path = ws.path("mock", "responses.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")


def hypothesis(**changes: Any) -> Hypothesis:
    """The draft hypothesis with these fields replaced, parsed without a workspace."""
    document: dict[str, Any] = yaml.safe_load(DRAFT)
    document.update(changes)
    return Hypothesis.model_validate({k: v for k, v in document.items() if v is not None})


def windows_for(horizon: str) -> dict[str, Any]:
    """Windows the embargo of this horizon admits, so any horizon can be tested."""
    end = date(2024, 12, 31)
    opens = end + timedelta(days=embargo_days(horizon))
    closes = opens + timedelta(days=30)
    return {
        "research": {"start": date(2024, 1, 2), "end": end},
        "certification": {"start": opens, "end": closes},
        "forward": {"start": closes + timedelta(days=1)},
    }


# --- what the workspace holds ------------------------------------------------


def test_an_empty_book_offers_only_the_constructs_that_stand_alone(
    ws: Workspace, store: StateStore
) -> None:
    computed = features(ws, store, hypothesis())
    assert computed["certified"] == []
    assert computed["attachable"] == {"sleeve": []}


def test_a_certified_strategy_opens_every_construct_that_attaches(
    ws: Workspace, store: StateStore
) -> None:
    certify(ws, store)
    open_to = attachable(ws, certified(ws, store, hypothesis()))
    assert open_to["sleeve"] == []
    assert open_to["filter"] == [HOST_ID]
    assert open_to["overlay"] == [HOST_ID]
    assert open_to["exit"] == [HOST_ID]
    assert open_to["alpha"] == [HOST_ID]
    assert open_to["allocation"] == ["portfolio"]


def test_a_certified_strategy_is_described_by_what_it_trades(
    ws: Workspace, store: StateStore
) -> None:
    certify(
        ws,
        store,
        attached=[{"hyp_id": "some_filter", "strategy_sha": "c" * 64, "construct": "filter"}],
    )
    spec = certified(ws, store, hypothesis())[0]
    assert spec["id"] == HOST_ID
    assert spec["version"] == 2
    assert spec["sleeve"] == HOST_ID
    assert spec["attached"] == [{"construct": "filter", "hypothesis": "some_filter"}]
    assert spec["universe"] == [INSTRUMENT]
    assert spec["universe_overlap"] == [INSTRUMENT]
    assert spec["horizon_match"] is True
    assert spec["resolution_match"] is True
    assert spec["mechanism_match"] is False


def test_a_host_whose_sleeve_is_not_registered_keeps_its_facts_empty(
    ws: Workspace, store: StateStore
) -> None:
    certify(ws, store, sleeve=None)
    spec = certified(ws, store, hypothesis())[0]
    assert spec["id"] == HOST_ID
    assert spec["mechanism"] is None
    assert spec["universe_overlap"] is None
    assert spec["horizon_match"] is None


def test_a_host_registered_without_a_pin_keeps_its_facts_empty(
    ws: Workspace, store: StateStore
) -> None:
    certify(ws, store)
    store.connection.execute(
        "UPDATE hypotheses SET hypothesis_sha = NULL WHERE hyp_id = ?", (HOST_ID,)
    )
    assert certified(ws, store, hypothesis())[0]["horizon"] is None


def test_a_directory_without_a_strategy_file_is_not_a_host(
    ws: Workspace, store: StateStore
) -> None:
    ws.path("strategies", "half_built").mkdir(parents=True)
    assert certified(ws, store, hypothesis()) == []


def test_the_same_duration_spelled_differently_still_matches(
    ws: Workspace, store: StateStore
) -> None:
    certify(ws, store, sleeve=HOST.replace("horizon: 30m", "horizon: 1800s"))
    assert certified(ws, store, hypothesis())[0]["horizon_match"] is True


def test_an_unaggregated_resolution_matches_only_itself(ws: Workspace, store: StateStore) -> None:
    certify(ws, store)
    candidate = hypothesis(resolution="tick", data_requirements=["bar", "trade"])
    assert certified(ws, store, candidate)[0]["resolution_match"] is False


# --- the catalogues as classification shows them -----------------------------


def test_the_construct_catalogue_states_what_each_one_is_and_needs(ws: Workspace) -> None:
    entries = {entry["id"]: entry for entry in construct_catalogue(ws)}
    assert entries["sleeve"]["needs_host"] == "none"
    assert entries["sleeve"]["runnable"] is True
    assert entries["alpha"]["runnable"] is False
    assert entries["filter"]["params"] == {"scope": ["time", "instrument"]}
    assert entries["sleeve"]["params"] == {}
    assert set(entries["overlay"]) == {"id", "description", "needs_host", "params", "runnable"}


def test_the_card_gates_are_the_card_stage_ones_with_their_ranges(ws: Workspace) -> None:
    gates = {gate["id"]: gate for gate in card_gates(hypothesis(), ws.config.research.folds)}
    assert set(gates) == {"strategy_integrity", "min_trades", "max_drawdown"}
    assert gates["strategy_integrity"]["required"] is True
    assert gates["min_trades"]["required"] is False
    assert gates["min_trades"]["params"] == {"min": "int"}
    assert gates["min_trades"]["ranges"] == {"min": {"min": 1.0, "max": 10000.0}}
    assert gates["max_drawdown"]["ranges"] == {}
    assert gates["min_trades"]["meaningful_when"]


def test_an_unbounded_range_arrives_as_an_open_end(ws: Workspace) -> None:
    table = objectives(hypothesis(), ws.config.research.folds)
    ranges = table["absolute"]["applicable"][0]["ranges"]
    assert ranges["min_delta"] == {"min": 0.0, "max": None}
    assert ranges["k_se"] == {"min": 0.5, "max": 3.0}


def test_the_objective_table_names_what_wins_in_each_mode(ws: Workspace) -> None:
    table = objectives(hypothesis(), ws.config.research.folds)
    assert table["absolute"]["selected"] == "net_edge_bps"
    assert table["relative"]["selected"] == "marginal_net_edge_bps"
    assert [one["id"] for one in table["absolute"]["applicable"]] == ["net_edge_bps"]
    assert table["absolute"]["applicable"][0]["priority"] == 10


# --- the objective set is total ----------------------------------------------

MECHANISMS: tuple[Mechanism, ...] = (
    "mean_reversion",
    "momentum",
    "microstructure",
    "stat_arb",
    "event",
    "carry",
    "vol",
    "other",
)

SUB_DAILY = ("1s", "1m", "30m", "23h")
DAILY = ("1d", "2d", "1w", "4w")

EXPECTED: Mapping[tuple[str, bool], str] = {
    ("absolute", False): "net_edge_bps",
    ("absolute", True): "wf_sharpe_net",
    ("relative", False): "marginal_net_edge_bps",
    ("relative", True): "marginal_wf_sharpe",
}
"""What each mode selects either side of a day. The keep rule has nothing else to use."""


@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("horizon", SUB_DAILY + DAILY)
@pytest.mark.parametrize("mode", ["absolute", "relative"])
def test_every_mechanism_mode_and_horizon_selects_an_objective(
    mechanism: str, horizon: str, mode: str
) -> None:
    hyp = hypothesis(mechanism=mechanism, horizon=horizon, windows=windows_for(horizon))
    table = objectives(hyp, 4)
    assert table[mode]["selected"] == EXPECTED[(mode, horizon in DAILY)]
    assert table[mode]["applicable"][0]["id"] == table[mode]["selected"]


def test_the_whole_feature_set_is_computed_in_one_call(ws: Workspace, store: StateStore) -> None:
    certify(ws, store)
    computed = features(ws, store, hypothesis())
    assert set(computed) == {
        "mechanism",
        "horizon",
        "resolution",
        "universe",
        "certified",
        "attachable",
        "constructs",
        "objectives",
        "card_gates",
    }
    assert computed["mechanism"] == "mean_reversion"
    assert computed["horizon"] == "30m"
    assert computed["resolution"] == "1m"
    assert computed["universe"] == [INSTRUMENT]
