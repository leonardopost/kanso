"""The latency stress gate: what it re-runs, what it refuses to re-run, and what a run costs.

Two kinds of test, as for the plateau. The first score the gate against a stand-in for the
certification runner's cost re-run, which records the venue model it was asked to run under
and answers with a metric chosen by the test. The second certify a real strategy over the real
certification window with a latency stated, and prove that the replaced latency reaches the
venue: a market order sent on a bar lands one bar later under a stated latency and the
multiple moves it further, so the stressed run's fills are stamped later than the unstressed
run's.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from kanso.certify.run import certify
from kanso.criteria import CardRun
from kanso.criteria.gates.latency_stress import (
    NO_CHOICE,
    NO_LATENCY,
    NO_RERUN,
    NOT_POSITIVE,
    gate,
)
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_run import CERT_GATES, a_card, write_plan
from tests.criteria.builders import build_run, context, make_hyp, trade
from tests.research.conftest import DOCUMENT, HYP_ID, classify, prepared, store, ws

__all__ = ["prepared", "store", "ws"]

DAYS = [date(2024, 1, 1 + i) for i in range(4)]
FLAT = (0.0, 0.0, 0.0, 0.0)
STRESS = {"mult_a": 2.0, "mult_b": 5.0, "keep_fraction": 0.5}
LATENT = {"costs": {"latency_ms": 20.0}}


def a_run(edge: float, latency_ms: float | None = 20.0) -> CardRun:
    """A four-day run closing one trade a day at `edge` bps, measured under a latency."""
    made = build_run(FLAT, trades=tuple(trade(day, pnl=edge) for day in DAYS))
    model = {} if latency_ms is None else {"costs": {"latency_ms": latency_ms}}
    return CardRun(**{**made.__dict__, "venue_model": model})


class Rerun:
    """The runner's cost re-run, recording every venue model it was asked to run under."""

    def __init__(self, metric: float = 1.0, **at: float) -> None:
        self.metric = metric
        self.at = at
        self.calls: list[dict[str, float]] = []

    def __call__(self, costs: Mapping[str, float]) -> CardRun:
        self.calls.append(dict(costs))
        return a_run(self.at.get(f"{costs['latency_ms']:g}", self.metric))


def judged(rerun: Rerun | None, **overrides: Any) -> Any:
    fields: dict[str, Any] = {"params": STRESS, "rerun_costs": rerun}
    return gate.evaluate(context(a_run(1.0), **{**fields, **overrides}))


# --- what it runs --------------------------------------------------------------


def test_it_runs_the_subject_once_per_multiple_of_the_stated_latency() -> None:
    rerun = Rerun()

    result = judged(rerun)

    assert rerun.calls == [{"latency_ms": 40.0}, {"latency_ms": 100.0}]
    assert result.evidence["n_backtests"] == 2
    assert result.evidence["latency_ms"] == 20.0
    assert result.evidence["runs"] == [
        {"multiple": 2.0, "latency_ms": 40.0, "metric": pytest.approx(1.0)},
        {"multiple": 5.0, "latency_ms": 100.0, "metric": pytest.approx(1.0)},
    ]


def test_two_equal_multiples_are_one_run() -> None:
    rerun = Rerun()

    result = judged(rerun, params={"mult_a": 3.0, "mult_b": 3.0, "keep_fraction": 0.5})

    assert rerun.calls == [{"latency_ms": 60.0}]
    assert result.evidence["n_backtests"] == 1


# --- the verdict ---------------------------------------------------------------


def test_it_passes_when_every_slower_route_keeps_enough_of_the_metric() -> None:
    result = judged(Rerun(metric=0.6))

    assert result.passed
    assert result.evidence["unstressed"] == pytest.approx(1.0)
    assert result.evidence["floor"] == pytest.approx(0.5)


def test_it_fails_when_the_slowest_route_falls_below_the_floor() -> None:
    result = judged(Rerun(metric=0.9, **{"100": 0.2}))

    assert not result.passed
    assert [run["metric"] for run in result.evidence["runs"]] == [
        pytest.approx(0.9),
        pytest.approx(0.2),
    ]


def test_a_result_that_is_not_positive_fails_before_any_run_is_made() -> None:
    rerun = Rerun()

    result = judged(rerun, run=a_run(-1.0))

    assert not result.passed and result.skipped is None
    assert result.evidence["reason"] == NOT_POSITIVE
    assert result.evidence["n_backtests"] == 0
    assert rerun.calls == []


# --- what it refuses to run ----------------------------------------------------


def test_a_venue_model_with_no_latency_has_no_route_to_slow() -> None:
    rerun = Rerun()

    none = judged(rerun, run=a_run(1.0, latency_ms=None))
    zero = judged(rerun, run=a_run(1.0, latency_ms=0.0))

    assert none.passed and none.skipped == NO_LATENCY
    assert zero.passed and zero.skipped == NO_LATENCY
    assert rerun.calls == []


def test_without_a_cost_re_run_it_judges_nothing() -> None:
    result = judged(None)
    assert result.passed and result.skipped == NO_RERUN


def test_without_the_planner_values_it_judges_nothing() -> None:
    result = judged(Rerun(), params={"mult_a": 2.0})
    assert result.passed and result.skipped == NO_CHOICE


def test_without_an_objective_it_judges_nothing() -> None:
    result = judged(Rerun(), hyp=make_hyp(objective=None, constraints=None))
    assert result.passed and result.skipped is not None


# --- a real certification ------------------------------------------------------


def saw_tooth_rule(closes_read: int) -> bytes:
    """A daily saw-tooth rule that buys after `closes_read` falling closes and sells after as
    many rising ones.

    Measured on the demo's saw-tooth (half a point a day, turning every six): with no latency
    an order sent on a bar fills at that bar's close, and under 20 ms it lands a bar later.
    So the two-close reverter, which buys the trough exactly with no latency (+2,487 on the
    certification window), buys a bar up the next leg under 20 ms (−13), while the one-close
    rule, early by a bar with no latency (−13), is landed on the trough by the latency
    (+2,368) — and under 40 and 100 ms the route is still one bar, so it keeps the whole of
    it. The one-close rule is the subject whose window passes under a stated latency; the
    reverter is the one the window refuses there.
    """
    chain = " > ".join(f"c{i}" for i in range(closes_read + 1))
    reverse = " < ".join(f"c{i}" for i in range(closes_read + 1))
    names = ", ".join(f"c{i}" for i in range(closes_read + 1))
    return f'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys after {closes_read} falling close(s) and sells after as many rising ones."""

    config_cls = Config

    def on_start(self) -> None:
        self.closes = []
        self.long = False

    def on_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        if len(self.closes) < {closes_read + 1}:
            return
        {names} = self.closes[-{closes_read + 1}:]
        if {chain} and not self.long:
            size = self.kanso_config.notional
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=size)
            self.long = True
        elif {reverse} and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''.encode()


REVERTER = saw_tooth_rule(2)
ANTICIPATOR = saw_tooth_rule(1)

LATENCY_GATE: dict[str, Any] = {
    "id": "latency_stress",
    "stage": "cert",
    "params": {"mult_a": 2.0, "mult_b": 5.0, "keep_fraction": 0.0},
    "rationale": "an edge that lives in the first milliseconds after a point is not an edge",
}


def certified_under(ws: Workspace, store: StateStore, source: bytes, latency_ms: float) -> Any:
    """Certify `source` on a hypothesis stating `latency_ms`, with the latency gate planned,
    and hand back the certificate."""
    from kanso.schemas import Costs, resolve_venue_model

    document = {**DOCUMENT, "costs": {**DOCUMENT["costs"], "latency_ms": latency_ms}}
    model = resolve_venue_model(
        "XNAS",
        hypothesis_costs=Costs.model_validate(document["costs"]),
        max_leverage=1.0,
        quotes_available=False,
    )
    classify(ws, store, document, source)
    a_card(ws, store, source, document=document, model=model)
    write_plan(ws, gates=[*CERT_GATES, LATENCY_GATE])
    return certify(ws, store, HYP_ID)


def test_a_certification_runs_the_subject_again_under_the_slower_routes(
    ws: Workspace, store: StateStore
) -> None:
    """The latency stated on the hypothesis reaches the venue, and so does each multiple:
    the stressed runs are made under the replaced model and the evidence says so. On a daily
    saw-tooth the route is a bar long whatever the multiple, so the stressed runs keep the
    objective; what is proved here is that they were run."""
    made = certified_under(ws, store, ANTICIPATOR, 20.0)

    result = next(found for found in made.gates if found.id == "latency_stress")
    window = next(found for found in made.gates if found.id == "embargoed_window")
    assert window.passed, window.evidence
    assert result.skipped is None
    assert result.evidence["latency_ms"] == 20.0
    assert result.evidence["n_backtests"] == 2
    assert [run["latency_ms"] for run in result.evidence["runs"]] == [40.0, 100.0]
    assert result.evidence["unstressed"] > 0
    assert result.passed


def test_the_slower_routes_are_not_run_for_a_subject_the_window_refused(
    ws: Workspace, store: StateStore
) -> None:
    """A subject the embargoed window failed is spared the re-runs, as the plateau is: the
    reverter under 20 ms lands on the wrong side of every turn and the window refuses it."""
    made = certified_under(ws, store, REVERTER, 20.0)

    result = next(found for found in made.gates if found.id == "latency_stress")
    window = next(found for found in made.gates if found.id == "embargoed_window")
    assert not window.passed
    assert result.skipped is not None and "re-run backtests were not made" in result.skipped


def test_a_hypothesis_stating_no_latency_is_skipped_at_certification(
    ws: Workspace, store: StateStore
) -> None:
    """The reverter's window passes with no latency stated, and the gate has no route to
    slow, so it says so rather than running anything."""
    classify(ws, store, DOCUMENT, REVERTER)
    a_card(ws, store, REVERTER)
    write_plan(ws, gates=[*CERT_GATES, LATENCY_GATE])

    made = certify(ws, store, HYP_ID)

    result = next(found for found in made.gates if found.id == "latency_stress")
    assert result.skipped == NO_LATENCY


def test_a_cost_re_run_replaces_the_venue_model_s_keys_and_nothing_else(
    ws: Workspace, store: StateStore
) -> None:
    from kanso.certify import run as certify_run

    document = {**DOCUMENT, "costs": {**DOCUMENT["costs"], "latency_ms": 20.0}}
    classify(ws, store, document, ANTICIPATOR)
    a_card(ws, store, ANTICIPATOR, document=document)
    subject = certify_run._subject(ws, store, HYP_ID, None)

    plain = certify_run._request(subject, subject.certification)
    slowed = certify_run._request(subject, subject.certification, costs={"latency_ms": 100.0})

    assert slowed.venue_model["costs"]["latency_ms"] == 100.0
    assert {
        **slowed.venue_model["costs"],
        "latency_ms": plain.venue_model["costs"]["latency_ms"],
    } == (plain.venue_model["costs"])
    assert slowed.strategy_source == plain.strategy_source
    assert slowed.overrides == plain.overrides
