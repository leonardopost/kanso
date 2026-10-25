"""The certification plan and the certificate.

The plan is what an agent decided would count as proof for one hypothesis: which gates,
at which stage, with which values, and which gates it deliberately left out. The framework
holds only the structural invariants — a plan reaches all three stages and names no gate
twice — because which gates and which thresholds is the planner's decision. Whether each
id exists in the toolbox and each value sits inside its range is checked where the toolbox
lives.

The certificate is the immutable record of running that plan. Its verdict is not an
opinion: it passes exactly when every gate that was actually evaluated passed. Its file
name carries the strategy prefix, the trial count, the plan version and the engine
version, so re-certifying the same code under a new engine writes a new file beside the
old one instead of contradicting it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from kanso.schemas.base import (
    CatalogueId,
    FreeForm,
    HypId,
    KansoModel,
    NonEmpty,
    Params,
    Sha256,
    Versioned,
)
from kanso.schemas.duration import parse_duration
from kanso.schemas.hypothesis import ConstructRef
from kanso.schemas.venue import VenueModel

PlanStage = Literal["cert", "paper", "live"]
GateStage = Literal["card", "cert", "paper", "live"]
Verdict = Literal["pass", "fail"]
PLAN_STAGES: tuple[PlanStage, ...] = ("cert", "paper", "live")


class Span(KansoModel):
    """A closed span of instants, as data availability reports it."""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        return self


class DataAvailability(KansoModel):
    """What the pinned snapshot actually holds, as the planner was told it."""

    types: list[CatalogueId] = Field(default_factory=list)
    spans: dict[str, Span] = Field(default_factory=dict)


class PlanInputs(KansoModel):
    """The closed set of facts the planner saw; a replan re-runs on exactly these."""

    hypothesis_sha: Sha256
    construct_: ConstructRef = Field(alias="construct")
    data_availability: DataAvailability
    n_trials: int = Field(ge=0)

    @property
    def construct(self) -> ConstructRef:  # type: ignore[override]
        """The classification the plan was made for. Aliased: `construct` is taken."""
        return self.construct_


class PlannedGate(KansoModel):
    """One gate the plan includes, with the values the planner chose inside its ranges."""

    id: CatalogueId
    stage: PlanStage
    params: Params = Field(default_factory=dict)
    rationale: str = Field(max_length=200)


class ExcludedGate(KansoModel):
    """One gate the planner considered and left out, and why."""

    id: CatalogueId
    reason: str = Field(max_length=200)


class CertificationPlan(Versioned):
    """What counts as proof for one hypothesis, pinned until a replan supersedes it."""

    hyp_id: HypId
    plan_version: int = Field(ge=1)
    planned_at: datetime
    planned_by: NonEmpty
    inputs: PlanInputs
    gates: list[PlannedGate] = Field(min_length=1)
    excluded: list[ExcludedGate] = Field(default_factory=list)

    def stage_gates(self, stage: PlanStage) -> list[PlannedGate]:
        """The gates of one stage, in plan order."""
        return [g for g in self.gates if g.stage == stage]

    def paper_window_s(self, horizon: str) -> float | None:
        """How long this plan requires a version's paper window to be, in seconds.

        The longer of a paper gate's own minimum and its multiple of the hypothesis's
        horizon, over the paper gates that state one — that is the earliest a paper gate
        can judge a version, and so the window the plan asks its evidence to be measured
        over. `None` when no paper gate states either, which is a plan holding the paper
        stage to no window at all.
        """
        floors: list[float] = []
        for gate in self.stage_gates("paper"):
            floor = 0.0
            duration = gate.params.get("min_duration")
            if isinstance(duration, str):
                floor = max(floor, parse_duration(duration, "min_duration").total_seconds())
            multiple = gate.params.get("horizon_mult")
            if isinstance(multiple, int | float) and not isinstance(multiple, bool):
                horizon_s = parse_duration(horizon, "horizon").total_seconds()
                floor = max(floor, float(multiple) * horizon_s)
            if floor > 0:
                floors.append(floor)
        return max(floors) if floors else None

    @model_validator(mode="after")
    def _invariants(self) -> CertificationPlan:
        ids = [g.id for g in self.gates]
        if len(set(ids)) != len(ids):
            raise ValueError("gates: names a gate twice")
        for stage in PLAN_STAGES:
            if not any(g.stage == stage for g in self.gates):
                raise ValueError(f"gates: no {stage} gate, and a plan must reach every stage")
        clash = sorted(set(ids) & {e.id for e in self.excluded})
        if clash:
            raise ValueError(f"excluded: {', '.join(clash)} is both included and excluded")
        return self


class EvaluatedGate(KansoModel):
    """One gate as it ran: the values used, the evidence and the verdict."""

    id: CatalogueId
    stage: GateStage
    params: Params = Field(default_factory=dict)
    evidence: FreeForm = Field(default_factory=dict)
    passed: bool = Field(alias="pass")
    skipped: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _skipped_passes(self) -> EvaluatedGate:
        if self.skipped is not None and not self.passed:
            raise ValueError("pass: a skipped gate passes, since it judged nothing")
        return self


class ObjectiveResult(KansoModel):
    """The objective as certification measured it: over the certification window cut into
    the workspace's `[research] folds` equal calendar folds, `value` the mean of the folds and
    `se` the error of that mean read off the window's per-period or per-trade series — a
    card's arithmetic on the embargoed window.

    A mean of folds and not of sessions: every fold weighs the same however many periods it
    holds, and one that holds none scores zero, so over a window of a few sessions `value`
    is not the mean per session of the same run.
    """

    id: CatalogueId
    value: float
    se: float = Field(ge=0)


class Power(KansoModel):
    """What a certification window can resolve, stated from research's own dispersion.

    `research` and `research_se` are the objective and its error over the research window;
    `standard_error` is the error the certification window is expected to carry,
    `research_se` scaled by the root of the ratio of the two windows' calendar lengths, since
    periods and trades arrive at research's rate. `resolves` is the true value a one-sided
    five-percent test on that window detects four times in five, `(z₀.₉₅ + z₀.₈₀) ×
    standard_error`. `bar` is what `embargoed_window` holds the certification estimate to —
    above zero and at least `min_fraction` of `research` — and `pass_at_research` and
    `pass_at_zero` the probability the estimate clears it if the truth is the research
    estimate, and if it is nothing. The three are `None` when the plan sets no
    `min_fraction`, which leaves the gate nothing to judge.
    """

    research: float
    research_se: float = Field(ge=0)
    standard_error: float = Field(ge=0)
    resolves: float = Field(ge=0)
    bar: float | None = None
    pass_at_research: float | None = Field(default=None, ge=0, le=1)
    pass_at_zero: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _a_bar_with_both_probabilities(self) -> Power:
        stated = {self.bar is None, self.pass_at_research is None, self.pass_at_zero is None}
        if len(stated) != 1:
            raise ValueError("bar: states both pass probabilities, or neither does")
        return self


NET_T_TOLERANCE = 1e-9
"""How far a recorded `net_t` may sit from its value over its error, relatively: floats
written and read back are the same floats, so this only absorbs the division's rounding."""


class NetPerTrade(KansoModel):
    """The certification window's mean net per trade, struck from the certification run.

    `net_per_trade_bp` is the mean net profit per closed trade in basis points of the
    position each opened — the run's per-trade edge over the window as one fold, with costs
    applied once by the runner, as `edge_per_trade` reads it. `net_se_session` is its error
    clustered by session: each session's trades move one session's influence on the mean,
    so a session's clips that move together count once. `net_t` is the value over that
    error. `sessions_with_trades` counts the sessions a closed trade fell on, by the day it
    closed, and `trades` the closed trades. The error and `net_t` are `None` when fewer than
    two sessions closed a trade, where the influences cannot vary, and `net_t` is `None` too
    when the error is zero.

    It is what a book ranks a member by (`kanso.nautilus.book`): read off the certificate,
    never computed again by running the certification window.
    """

    net_per_trade_bp: float
    net_se_session: float | None = Field(default=None, ge=0)
    net_t: float | None = None
    sessions_with_trades: int = Field(ge=1)
    trades: int = Field(ge=1)

    @model_validator(mode="after")
    def _the_t_is_the_value_over_its_error(self) -> NetPerTrade:
        if self.sessions_with_trades > self.trades:
            raise ValueError(
                f"sessions_with_trades: {self.sessions_with_trades} sessions cannot hold "
                f"{self.trades} trades"
            )
        if self.sessions_with_trades < 2 and self.net_se_session is not None:
            raise ValueError("net_se_session: one session closed every trade, so it has no error")
        se = self.net_se_session
        if se is None or se == 0.0:
            if self.net_t is not None:
                raise ValueError("net_t: there is no error to divide by")
            return self
        expected = self.net_per_trade_bp / se
        if self.net_t is None or abs(self.net_t - expected) > NET_T_TOLERANCE * max(
            1.0, abs(expected)
        ):
            raise ValueError(f"net_t: {self.net_t} is not the value over its error, {expected}")
        return self


class Certificate(Versioned):
    """The immutable record of one plan run against one strategy under one engine."""

    hyp_id: HypId
    strategy_sha: Sha256
    hypothesis_sha: Sha256 | None = None
    """The hypothesis file the card's run was pinned to; `None` on a certificate written
    before pins were recorded, which refuses a repeat under any pin."""
    nautilus_version: NonEmpty
    venue_model: VenueModel
    snapshot_id: NonEmpty
    criteria_version: NonEmpty
    plan_version: int = Field(ge=1)
    construct_: ConstructRef = Field(alias="construct")
    objective: ObjectiveResult
    gates: list[EvaluatedGate] = Field(min_length=1)
    n_trials: int = Field(ge=1)
    verdict: Verdict
    created_at: datetime
    power: Power | None = None
    """What the certification window could resolve, from the research window's dispersion;
    `None` on a certificate written before it was stated, struck on a snapshot nothing could
    be measured on, or measured on a research window whose error is zero — one trade, or one
    period — which has no dispersion to state it from."""
    net_per_trade: NetPerTrade | None = None
    """The certification window's mean net per trade and its session-clustered error,
    struck from the run the gates judged; `None` on a certificate written before it was
    recorded, on a snapshot nothing could be measured on, and on a window that closed no
    trade."""

    @property
    def construct(self) -> ConstructRef:  # type: ignore[override]
        """What was certified, in construction terms. Aliased: `construct` is taken."""
        return self.construct_

    @property
    def sha7(self) -> str:
        """The strategy prefix the certificate and its source file are named by."""
        return self.strategy_sha[:7]

    @property
    def hyp7(self) -> str:
        """The pinned hypothesis file's prefix the certificate is named by; empty with no pin."""
        return "" if self.hypothesis_sha is None else self.hypothesis_sha[:7]

    def filename(self) -> str:
        """`<sha7>-h<pin7>-<n_trials>-p<plan_version>-e<nautilus_version>.yaml`, the pin left
        out by a certificate written before pins were recorded."""
        pin = "" if self.hypothesis_sha is None else f"h{self.hyp7}-"
        return (
            f"{self.sha7}-{pin}{self.n_trials}-p{self.plan_version}-e{self.nautilus_version}.yaml"
        )

    def source_filename(self) -> str:
        """`<sha7>.py`: the certified bytes that travel beside the certificate."""
        return f"{self.sha7}.py"

    @model_validator(mode="after")
    def _verdict_follows_the_gates(self) -> Certificate:
        ids = [g.id for g in self.gates]
        if len(set(ids)) != len(ids):
            raise ValueError("gates: names a gate twice")
        evaluated = [g for g in self.gates if g.skipped is None]
        expected: Verdict = "pass" if all(g.passed for g in evaluated) else "fail"
        if self.verdict != expected:
            failed = ", ".join(g.id for g in evaluated if not g.passed) or "none"
            raise ValueError(
                f"verdict: {self.verdict!r} but the gates say {expected!r} (failing: {failed})"
            )
        return self
