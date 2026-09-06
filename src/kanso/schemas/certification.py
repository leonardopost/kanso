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
    """The objective as certification measured it."""

    id: CatalogueId
    value: float
    se: float = Field(ge=0)


class Certificate(Versioned):
    """The immutable record of one plan run against one strategy under one engine."""

    hyp_id: HypId
    strategy_sha: Sha256
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

    @property
    def construct(self) -> ConstructRef:  # type: ignore[override]
        """What was certified, in construction terms. Aliased: `construct` is taken."""
        return self.construct_

    @property
    def sha7(self) -> str:
        """The strategy prefix the certificate and its source file are named by."""
        return self.strategy_sha[:7]

    def filename(self) -> str:
        """`<sha7>-<n_trials>-p<plan_version>-e<nautilus_version>.yaml`."""
        return f"{self.sha7}-{self.n_trials}-p{self.plan_version}-e{self.nautilus_version}.yaml"

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
