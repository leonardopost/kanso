"""A workspace with something certified in it, for both shapes composition produces.

The workspace is the research suite's synthetic saw-tooth, so every backtest here is a
real engine run over real catalog data and nothing reaches a network. The sleeve is
certified by running the whole real path — a card, a pinned plan, `cert run` — because
what composition consumes is a certificate and a made-up one would prove less.

A construct attached to that sleeve is certified by writing the certificate instead. The
demo sleeve buys the saw-tooth's trough and sells its peak, which is the best any strategy
can do on this series, so every filter of it is neutral or worse and none of them can earn
a passing verdict from `embargoed_window` — that gate wants a marginal above zero.
Certification of an attached construct has its own tests; what these need is a passing
certificate to compose, and it is written with the same function `cert run` writes with.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from kanso.certify import certificate
from kanso.certify.plan import certificates_dir
from kanso.certify.run import certify
from kanso.criteria import criteria_version
from kanso.env.envelope import engine_version
from kanso.hyp import HYPOTHESIS_FILE, Registration
from kanso.hyp import show as registration_of
from kanso.schemas import (
    Certificate,
    CertificationPlan,
    Hypothesis,
    parse_yaml,
    write_yaml,
)
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_run import a_card, snapshot_id, venue_model
from tests.research.conftest import (
    DOCUMENT,
    HYP_ID,
    classify,
    document,
    prepared,
    store,
    ws,
)

__all__ = ["prepared", "store", "ws"]

NOW = datetime(2024, 3, 1, tzinfo=UTC)

FILTER_ID = "demo_filter"

VARYING = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 2_000.0


class Strategy(KansoStrategy):
    """Buys the trough and sells the peak, at a size that grows with every round trip.

    The demo sleeve trades the saw-tooth at a fixed size, so every one of its trades earns
    the same amount and a resampling of them cannot vary. This one varies the size, which
    is what gives a bootstrap something to resample and an expectation a band with width.
    """

    config_cls = Config

    def on_start(self) -> None:
        self.closes = []
        self.long = False
        self.round_trips = 0

    def on_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        if len(self.closes) < 3:
            return
        first, second, third = self.closes[-3:]
        if first > second > third and not self.long:
            self.round_trips += 1
            self.submit_entry(
                bar.bar_type.instrument_id,
                "BUY",
                notional=self.kanso_config.notional * self.round_trips,
            )
            self.long = True
        elif first < second < third and self.long:
            self.submit_exit(bar.bar_type.instrument_id)
            self.long = False
'''

ALLOWING = b'''
from kanso.nautilus.strategy import Decision, KansoModifier, KansoModifierConfig


class Config(KansoModifierConfig):
    scope: str = "time"


class Modifier(KansoModifier):
    """Allows every entry, so the host runs exactly as it would alone."""

    construct = "filter"
    config_cls = Config

    def evaluate(self, ctx) -> Decision:
        return Decision(allow=True)
'''

BLOCKING = ALLOWING.replace(b"Decision(allow=True)", b"Decision(allow=False)").replace(
    b"Allows every entry, so the host runs exactly as it would alone.",
    b"Refuses every entry, so the host never trades.",
)

CERT_GATES: list[dict[str, Any]] = [
    {
        "id": "embargoed_window",
        "stage": "cert",
        "params": {"min_fraction": 0.0},
        "rationale": "the one out-of-sample test",
    },
    {
        "id": "publication_lag",
        "stage": "cert",
        "params": {"tolerance_s": 0.0},
        "rationale": "availability is the load-bearing invariant",
    },
    {
        "id": "parity_replay",
        "stage": "cert",
        "params": {"ts_ns": 0},
        "rationale": "required; the deployed code path must be the researched one",
    },
]
"""The three cert gates every plan must carry, since all three are structural invariants."""

BOOTSTRAP_GATE: dict[str, Any] = {
    "id": "bootstrap",
    "stage": "cert",
    "params": {"n": 400},
    "rationale": "how deep the drawdown could have been in another order",
}

TAIL_GATES: list[dict[str, Any]] = [
    {
        "id": "paper_forward",
        "stage": "paper",
        "params": {"min_duration": "30d", "horizon_mult": 10.0},
        "rationale": "forward evidence",
    },
    {"id": "live_drift", "stage": "live", "params": {}, "rationale": "the expectation holds"},
]
"""A plan reaches every stage, so every plan here ends with these two."""

FILTER_CONSTRUCT: dict[str, Any] = {
    "id": "filter",
    "host": HYP_ID,
    "params": {"scope": "time"},
}

FILTER_DOCUMENT = document(
    id=FILTER_ID,
    construct=FILTER_CONSTRUCT,
    objective={"id": "marginal_wf_sharpe", "params": {"min_delta": 0.0, "k_se": 0.5}},
)
"""A filter attached to the composed sleeve, measured against what the sleeve does alone."""


def write_plan(
    ws: Workspace,
    hyp_id: str,
    construct: dict[str, Any],
    *,
    gates: list[dict[str, Any]] | None = None,
) -> CertificationPlan:
    """Pin a plan for a hypothesis, as a first `cert plan` would."""
    plan = CertificationPlan.model_validate(
        {
            "schema": 1,
            "hyp_id": hyp_id,
            "plan_version": 1,
            "planned_at": NOW,
            "planned_by": "mock",
            "inputs": {
                "hypothesis_sha": "0" * 64,
                "construct": construct,
                "data_availability": {"types": ["bar"], "spans": {}},
                "n_trials": 1,
            },
            "gates": [*(CERT_GATES if gates is None else gates), *TAIL_GATES],
            "excluded": [],
        }
    )
    path = certificates_dir(ws, hyp_id) / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(plan, path)
    return plan


def certified_sleeve(
    ws: Workspace,
    store: StateStore,
    *,
    source: bytes = VARYING,
    gates: list[dict[str, Any]] | None = None,
) -> Certificate:
    """Classify, card, plan and certify the demo sleeve, the whole real path."""
    classify(ws, store, DOCUMENT, source)
    a_card(ws, store, source)
    write_plan(ws, HYP_ID, {"id": "sleeve"}, gates=gates)
    made = certify(ws, store, HYP_ID)
    assert made.verdict == "pass", "the saw-tooth pays out of sample too"
    return made


def a_certificate(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    source: bytes,
    *,
    construct: dict[str, Any],
    objective_id: str,
    gates: list[dict[str, Any]] | None = None,
    doc: dict[str, Any] | None = None,
    verdict: str = "pass",
) -> Certificate:
    """A certificate written straight in, for a subject that cannot earn one.

    `doc` registers the hypothesis first, for a subject this workspace does not hold yet;
    the certificate's own `construct` is separate from it, so a certificate can be made
    that disagrees with the file and the refusals that reads can be reached. It is stamped
    now, so it is newer than anything the real path has already written.
    """
    if doc is not None:
        classify(ws, store, {**doc, "id": hyp_id}, source)
    made = Certificate.model_validate(
        {
            "schema": 1,
            "hyp_id": hyp_id,
            "strategy_sha": store.put_blob(source),
            "nautilus_version": engine_version(),
            "venue_model": venue_model(),
            "snapshot_id": snapshot_id(ws),
            "criteria_version": criteria_version(),
            "plan_version": 1,
            "construct": construct,
            "objective": {"id": objective_id, "value": 0.0, "se": 0.0},
            "gates": gates
            or [
                {
                    "id": "embargoed_window",
                    "stage": "cert",
                    "params": {"min_fraction": 0.0},
                    "evidence": {},
                    "pass": verdict == "pass",
                }
            ],
            "n_trials": 1,
            "verdict": verdict,
            "created_at": datetime.now(tz=UTC),
        }
    )
    certificate.write(ws, store, made, source)
    return made


def certified_filter(
    ws: Workspace,
    store: StateStore,
    *,
    source: bytes = ALLOWING,
    gates: list[dict[str, Any]] | None = None,
) -> Certificate:
    """A passing certificate for a filter attached to the demo sleeve."""
    return a_certificate(
        ws,
        store,
        FILTER_ID,
        source,
        construct=FILTER_CONSTRUCT,
        objective_id="marginal_wf_sharpe",
        gates=gates,
        doc=FILTER_DOCUMENT,
    )


@pytest.fixture
def sleeve(ws: Workspace, store: StateStore) -> Certificate:
    """The certified demo sleeve, ready to compose."""
    return certified_sleeve(ws, store)


def pinned(ws: Workspace, store: StateStore, hyp_id: str = HYP_ID) -> Hypothesis:
    """A hypothesis as the registry holds it, which is the copy composition reads."""
    registration = cast(Registration, registration_of(ws, store, hyp_id))
    sha = registration.hypothesis_sha
    assert sha is not None
    return parse_yaml(Hypothesis, store.get_blob(sha).decode("utf-8"), HYPOTHESIS_FILE)
