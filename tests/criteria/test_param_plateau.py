"""The perturbation gate: what it moves, what it refuses to move, and what a move costs.

Two kinds of test. The first score the gate against a stand-in for the certification
runner's re-run, which records what it was asked to run and answers with a metric chosen
by the test, so the arithmetic of the verdict is checked without an engine. The second
certify a real strategy over the real certification window, which is the only way to prove
that a perturbed parameter reaches the strategy: the subject there is written so that one
move of one parameter stops it trading and the opposite move leaves it untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.certify import run
from kanso.certify.run import certify
from kanso.criteria import CardRun, gates
from kanso.criteria.gates.param_plateau import (
    ALL_DROPPED,
    NO_PARAMETERS,
    NO_RERUN,
    NOT_POSITIVE,
    _moved,
    gate,
)
from kanso.criteria.objectives import wf_sharpe_vs_hold
from kanso.nautilus import backtest
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_run import CERT_GATES, a_card, write_plan
from tests.criteria.builders import build_run, context, make_hyp, trade
from tests.research.conftest import DOCUMENT, HYP_ID, classify, prepared, store, ws

__all__ = ["prepared", "store", "ws"]

DAYS = [date(2024, 1, 1 + i) for i in range(4)]
FLAT = (0.0, 0.0, 0.0, 0.0)
MOVES = {"perturb_pct": 10.0, "keep_fraction": 0.9}


def a_run(edge: float) -> CardRun:
    """A four-day run closing one trade a day, each netting `edge` bps of its notional."""
    return build_run(FLAT, trades=tuple(trade(day, pnl=edge) for day in DAYS))


class Rerun:
    """The certification runner's re-run, recording every setting it was asked to score."""

    def __init__(self, metric: float = 1.0, **named: float) -> None:
        self.metric = metric
        self.named = named
        self.calls: list[dict[str, float]] = []

    def __call__(self, overrides: Mapping[str, float]) -> CardRun:
        self.calls.append(dict(overrides))
        [(name, value)] = overrides.items()
        return a_run(self.named.get(f"{name}={value:g}", self.metric))


def judged(rerun: Rerun | None, tunable: Mapping[str, float], **overrides: Any) -> Any:
    """The gate's verdict on a run whose own metric is one bps per trade."""
    fields: dict[str, Any] = {"params": MOVES, "tunable": tunable, "rerun": rerun}
    return gate.evaluate(context(a_run(1.0), **{**fields, **overrides}))


# --- what it moves ------------------------------------------------------------


def test_it_moves_every_declared_parameter_once_each_way_and_one_at_a_time() -> None:
    rerun = Rerun()

    judged(rerun, {"slow": 40.0, "fast": 10.0})

    assert rerun.calls == [
        {"fast": 11.0},
        {"fast": 9.0},
        {"slow": 44.0},
        {"slow": 36.0},
    ], "each parameter alone, in name order, up before down"


def test_the_cost_of_the_gate_is_a_backtest_per_move_and_it_says_so() -> None:
    result = judged(Rerun(), {"slow": 40.0, "fast": 10.0})

    assert result.evidence["n_backtests"] == 4
    assert result.evidence["n_fields"] == 2
    assert result.evidence["perturb_pct"] == 10.0


def test_a_whole_number_parameter_stays_a_whole_number() -> None:
    rerun = Rerun()

    judged(rerun, {"lookback": 20}, params={"perturb_pct": 25.0, "keep_fraction": 0.9})

    assert rerun.calls == [{"lookback": 25}, {"lookback": 15}]
    assert all(isinstance(call["lookback"], int) for call in rerun.calls)


def test_the_same_context_scored_twice_asks_for_the_same_runs_in_the_same_order() -> None:
    first, second = Rerun(), Rerun()

    judged(first, {"slow": 40.0, "fast": 10.0, "offset": 2.5})
    judged(second, {"offset": 2.5, "fast": 10.0, "slow": 40.0})

    assert first.calls == second.calls


# --- what it refuses to count -------------------------------------------------


def test_a_move_that_rounds_back_to_where_it_started_is_dropped() -> None:
    rerun = Rerun()

    result = judged(rerun, {"lookback": 3, "notional": 5_000.0})

    assert result.evidence["dropped"] == ["lookback up", "lookback down"]
    assert result.evidence["n_backtests"] == 2
    assert result.evidence["n_fields"] == 2
    assert rerun.calls == [{"notional": 5_500.0}, {"notional": 4_500.0}]


def test_a_parameter_sitting_at_zero_has_no_proportional_move() -> None:
    rerun = Rerun()

    result = judged(rerun, {"offset": 0.0})

    assert result.passed and result.skipped == ALL_DROPPED
    assert rerun.calls == []


# --- the verdict --------------------------------------------------------------


def test_it_passes_when_every_move_keeps_enough_of_the_metric() -> None:
    result = judged(Rerun(metric=0.9), {"fast": 10.0})

    assert result.passed
    assert result.evidence["unperturbed"] == pytest.approx(1.0)
    assert result.evidence["floor"] == pytest.approx(0.9)


def test_it_fails_when_one_move_falls_below_the_floor() -> None:
    result = judged(Rerun(metric=0.95, **{"fast=9": 0.5}), {"fast": 10.0})

    assert not result.passed
    assert result.evidence["perturbations"] == [
        {"field": "fast", "direction": "up", "value": 11.0, "metric": pytest.approx(0.95)},
        {"field": "fast", "direction": "down", "value": 9.0, "metric": pytest.approx(0.5)},
    ]


def test_the_floor_is_the_chosen_fraction_of_the_unperturbed_metric() -> None:
    result = judged(
        Rerun(metric=0.6),
        {"fast": 10.0},
        run=a_run(2.0),
        params={"perturb_pct": 10.0, "keep_fraction": 0.25},
    )

    assert result.evidence["unperturbed"] == pytest.approx(2.0)
    assert result.evidence["floor"] == pytest.approx(0.5)
    assert result.passed, "0.6 keeps a quarter of 2.0"


def test_a_benchmark_stays_fixed_while_the_subject_is_moved() -> None:
    """Every perturbed run is measured against the one hold the unperturbed run was.

    The subject beats its hold, since the gate refuses one that does not before it moves
    anything; the moved run beats it by less.
    """
    hold = build_run((1.0, 2.0, 1.0, 3.0, 2.0, 1.0, 2.0, 2.0))
    subject = build_run((3.0, 2.0, 4.0, 3.0, 5.0, 2.0, 6.0, 3.0))
    moved = build_run((2.0, 2.0, 4.0, 4.0, 5.0, 2.0, 6.0, 2.0))
    measured = judged(
        lambda overrides: moved,  # type: ignore[arg-type]
        {"fast": 10.0},
        run=subject,
        benchmark_run=hold,
        hyp=make_hyp(
            benchmark={"hold": "first_leg"},
            objective={"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 1.0}},
        ),
    )

    assert measured.evidence["unperturbed"] == pytest.approx(
        wf_sharpe_vs_hold.compute(subject, 4, benchmark=hold)[0]
    )
    assert [p["metric"] for p in measured.evidence["perturbations"]] == pytest.approx(
        [wf_sharpe_vs_hold.compute(moved, 4, benchmark=hold)[0]] * 2
    )


# --- what it refuses to judge -------------------------------------------------


@pytest.mark.parametrize("edge", [-69.63, -1.0, 0.0])
def test_an_objective_at_or_below_zero_fails_before_any_parameter_is_moved(edge: float) -> None:
    """A fraction of a loss lies above the loss, so the floor would reward leaving the plateau."""
    rerun = Rerun()

    result = judged(rerun, {"slow": 40.0, "fast": 10.0}, run=a_run(edge))

    assert not result.passed and result.skipped is None, "a judged fail, not a skip"
    assert result.evidence["reason"] == NOT_POSITIVE
    assert result.evidence["unperturbed"] == pytest.approx(edge)
    assert result.evidence["n_backtests"] == 0
    assert result.evidence["perturbations"] == []
    assert "floor" not in result.evidence, "a fraction of a loss is no floor"
    assert rerun.calls == [], "not one perturbation was paid for"


def test_a_loss_with_nothing_to_move_is_still_the_skip_it_always_was() -> None:
    """The skips for absent context come first: the fail is a verdict on a whole context."""
    result = judged(Rerun(), {}, run=a_run(-1.0))

    assert result.passed and result.skipped == NO_PARAMETERS


# --- what it will not judge ---------------------------------------------------


def test_a_configuration_declaring_no_numeric_parameter_of_its_own_is_skipped() -> None:
    result = judged(Rerun(), {})

    assert result.passed and result.skipped == NO_PARAMETERS


def test_without_a_re_run_of_the_subject_it_judges_nothing() -> None:
    result = judged(None, {"fast": 10.0})

    assert result.passed and result.skipped == NO_RERUN


def test_without_the_planner_values_it_judges_nothing() -> None:
    result = judged(Rerun(), {"fast": 10.0}, params={})

    assert result.passed and result.skipped is not None


def test_without_an_objective_it_judges_nothing() -> None:
    result = judged(Rerun(), {"fast": 10.0}, hyp=make_hyp(objective=None, constraints=None))

    assert result.passed and result.skipped is not None


# --- the move itself ----------------------------------------------------------


@given(
    value=st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    percent=st.floats(min_value=1.0, max_value=50.0),
)
def test_a_move_either_changes_the_value_or_is_no_move_at_all(value: float, percent: float) -> None:
    up, down = _moved(value, percent, 1.0), _moved(value, percent, -1.0)

    assert up is None or up > value
    assert down is None or down < value


# --- a real certification -----------------------------------------------------


def source(min_fall: float) -> bytes:
    """The saw-tooth reverter, entering only when the fall it just saw was big enough.

    The saw-tooth falls exactly 1.00 into its trough, so a `min_fall` of 1.00 trades every
    cycle while one of 1.10 — the same setting perturbed up by a tenth — trades nothing.
    That is a parameter whose perturbation is visible in the result rather than merely
    applied to a configuration nobody reads.
    """
    return f'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0
    min_fall: float = {min_fall!r}


class Strategy(KansoStrategy):
    """Buys the trough of a fall it considers deep enough, and sells the peak."""

    config_cls = Config

    def on_start(self) -> None:
        self.closes = []
        self.long = False

    def on_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        if len(self.closes) < 3:
            return
        first, second, third = self.closes[-3:]
        settings = self.kanso_config
        if first > second > third and not self.long:
            if first - third >= settings.min_fall:
                self.submit_entry(
                    bar.bar_type.instrument_id, "BUY", notional=settings.notional
                )
                self.long = True
        elif first < second < third and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''.encode()


PLATEAU_GATE: dict[str, Any] = {
    "id": "param_plateau",
    "stage": "cert",
    "params": {"perturb_pct": 10.0, "keep_fraction": 0.9},
    "rationale": "a result that survives only one setting was fitted to it",
}


def certified(ws: Workspace, store: StateStore, min_fall: float) -> Any:
    """Certify the reverter at this setting, with the perturbation gate planned."""
    strategy = source(min_fall)
    classify(ws, store, DOCUMENT, strategy)
    a_card(ws, store, strategy)
    write_plan(ws, gates=[*CERT_GATES, PLATEAU_GATE])
    made = certify(ws, store, HYP_ID)
    return next(found for found in made.gates if found.id == "param_plateau")


def test_a_certification_runs_one_backtest_per_perturbation(
    ws: Workspace, store: StateStore
) -> None:
    result = certified(ws, store, 0.5)

    assert result.skipped is None
    assert result.evidence["n_fields"] == 2, "notional and min_fall, and nothing injected"
    assert result.evidence["n_backtests"] == 4
    assert result.evidence["unperturbed"] > 0


def test_a_setting_off_a_plateau_is_what_the_gate_catches(ws: Workspace, store: StateStore) -> None:
    result = certified(ws, store, 1.0)

    assert not result.passed
    moved = {
        (point["field"], point["direction"]): point["metric"]
        for point in result.evidence["perturbations"]
    }
    assert moved[("min_fall", "up")] == 0.0, "1.10 is deeper than the saw-tooth ever falls"
    assert moved[("min_fall", "down")] == pytest.approx(result.evidence["unperturbed"])


def test_the_moved_parameters_are_the_authors_own_and_not_the_injected_ones(
    ws: Workspace, store: StateStore
) -> None:
    result = certified(ws, store, 0.5)

    assert {point["field"] for point in result.evidence["perturbations"]} == {
        "notional",
        "min_fall",
    }


CHASING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0
    min_rise: float = 0.5


class Strategy(KansoStrategy):
    """Buys the peak of a rise it considers big enough, and sells the trough: a loss a cycle."""

    config_cls = Config

    def on_start(self) -> None:
        self.closes = []
        self.long = False

    def on_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        if len(self.closes) < 3:
            return
        first, second, third = self.closes[-3:]
        settings = self.kanso_config
        if first < second < third and not self.long:
            if third - first >= settings.min_rise:
                self.submit_entry(
                    bar.bar_type.instrument_id, "BUY", notional=settings.notional
                )
                self.long = True
        elif first > second > third and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''
"""The reverter's mirror: it trades the saw-tooth the wrong way round, with two parameters."""

UNFLOORED: dict[str, Any] = {**CERT_GATES[0], "params": {}}
"""The window gate with no `min_fraction` chosen, which is a window gate that judges nothing."""


def perturbed_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, float]]:
    """The overrides of every engine run a certification makes from here on, as they happen.

    The subject's own runs, a hold's and a replay's carry none; only a perturbation does.
    """
    moved: list[dict[str, float]] = []
    original = backtest.execute

    def counted(request: backtest.RunRequest, instruments: Any, groups: Any) -> Any:
        if request.overrides:
            moved.append(dict(request.overrides))
        return original(request, instruments, groups)

    monkeypatch.setattr(run.backtest, "execute", counted)
    return moved


def gate_of(made: Any, name: str) -> Any:
    return next(found for found in made.gates if found.id == name)


def test_a_failed_window_spares_the_perturbation_backtests_and_the_certificate_says_so(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    classify(ws, store, DOCUMENT, CHASING)
    a_card(ws, store, CHASING)
    write_plan(ws, gates=[PLATEAU_GATE, *CERT_GATES])
    moved = perturbed_runs(monkeypatch)

    made = certify(ws, store, HYP_ID)

    window, plateau = gate_of(made, "embargoed_window"), gate_of(made, "param_plateau")
    assert made.verdict == "fail"
    assert window.skipped is None and not window.passed
    assert window.evidence["certification"] <= 0, "the mirror loses out of sample"
    assert plateau.skipped == run.WINDOW_FAILED and plateau.passed
    assert plateau.evidence == {}
    assert moved == [], "not one perturbation was run"
    assert [found.id for found in made.gates] == [
        planned["id"] for planned in (PLATEAU_GATE, *CERT_GATES)
    ], "judged last, listed where the plan put it"


def test_a_window_that_judged_nothing_spares_nothing_and_the_gate_refuses_the_loss_itself(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    classify(ws, store, DOCUMENT, CHASING)
    a_card(ws, store, CHASING)
    write_plan(ws, gates=[UNFLOORED, *CERT_GATES[1:], PLATEAU_GATE])
    moved = perturbed_runs(monkeypatch)

    made = certify(ws, store, HYP_ID)

    window, plateau = gate_of(made, "embargoed_window"), gate_of(made, "param_plateau")
    assert window.skipped is not None, "a skip is an absence of evidence, not a refusal"
    assert plateau.skipped is None and not plateau.passed
    assert plateau.evidence["reason"] == NOT_POSITIVE
    assert plateau.evidence["unperturbed"] <= 0
    assert plateau.evidence["n_backtests"] == 0
    assert plateau.evidence["n_fields"] == 2, "notional and min_rise were there to move"
    assert moved == [], "the gate refused the loss before paying for a move"
    assert made.verdict == "fail"


def test_the_toolbox_no_longer_declares_the_gate_without_an_implementation() -> None:
    assert "param_plateau" in gates()
