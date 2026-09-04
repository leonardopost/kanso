"""What a gate is handed, and the small records that carry the context it cannot derive.

A gate is a pure evaluator: it reads this context, returns a verdict with its evidence and
writes nothing. Everything it needs therefore arrives here — the hypothesis it is judging,
the run over the window it is judging, and, for the gates that compare two windows or two
books, the second subject as well.

The optional members are the ones a stage may not have. A gate that finds its context
absent passes and says why it was skipped rather than failing, because a missing deployed
book, a missing volume series or a threshold the planner chose not to set is an absence of
evidence, not evidence of a fault. The single exception in spirit is `strategy_integrity`,
whose absence of a lane directory means the caller asked the wrong question; it still
reports a skip rather than a failure, and the runner always supplies one.

`window` is the span `run` covers, so the same gate reads a research window on a card, a
certification window during certification and a stage window in paper or live.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Protocol

from kanso.criteria.run import CardRun
from kanso.schemas import GateResult, Hypothesis, ParamValue


@dataclass(frozen=True)
class DeployedBook:
    """A book already carrying capital, as a return series a candidate is compared to."""

    id: str
    period_ends_ns: tuple[int, ...]
    returns: tuple[float, ...]


@dataclass(frozen=True)
class DatasetFacts:
    """One dataset of a pinned snapshot, as the availability gates need to see it.

    `min_lag_s` is the smallest publication delay actually observed in the data — the
    minimum of `ts_init - ts_event` — and `required_lag_s` is the minimum its data class
    is documented to have. A dataset whose publication is unknown carries no defensible
    lag at all.
    """

    dataset_id: str
    type: str
    publication: str
    min_lag_s: float
    required_lag_s: float


@dataclass(frozen=True)
class GateContext:
    """Everything one gate evaluation may read.

    `run` covers `window`, and `host_run` is the host's run over the same window for a
    relative construct. The rest is what individual gates need and cannot derive:

    * `research_run` (and `host_research_run`) is the research window, for the gates that
      compare a certification result against the estimate selection acted on;
    * `card_metrics` are the metrics of this hypothesis's non-crash cards, whose spread is
      how wide the search that produced the candidate was;
    * `lane_dir` and `pinned` are the lane directory and the sha256 of each blob the run
      pinned, which is what the scope rule is checked against;
    * `datasets` are the pinned snapshot's datasets with their observed and documented
      publication delays;
    * `daily_volume` maps an instrument id to its daily traded notional in the run's
      currency, oldest first, so a participation limit has something to be a share of.
    """

    hyp: Hypothesis
    construct: str
    stage: str
    params: Mapping[str, ParamValue]
    window: tuple[date, date]
    run: CardRun
    host_run: CardRun | None = None
    research_folds: int = 1
    n_trials: int = 1
    snapshot_id: str = ""
    strategy_sha: str = ""
    expectation: Mapping[str, object] | None = None
    deployed: Sequence[DeployedBook] = ()
    session: object | None = None
    research_run: CardRun | None = None
    host_research_run: CardRun | None = None
    card_metrics: Sequence[float] = ()
    lane_dir: Path | None = None
    pinned: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    datasets: Sequence[DatasetFacts] = ()
    daily_volume: Mapping[str, Sequence[float]] = field(
        default_factory=lambda: MappingProxyType({})
    )


class Gate(Protocol):
    """One test with a verdict and its evidence."""

    id: ClassVar[str]

    def evaluate(self, ctx: GateContext) -> GateResult:
        """Judge this context, or pass with a reason for judging nothing."""
        ...


def verdict(gate: str, ok: bool, evidence: Mapping[str, object]) -> GateResult:
    """A gate that judged, either way, with the numbers it judged on."""
    return GateResult.model_validate({"id": gate, "pass": ok, "evidence": dict(evidence)})


def skipped(gate: str, reason: str) -> GateResult:
    """A gate that lacked its context: it passes, and says what it did not have."""
    return GateResult.model_validate({"id": gate, "pass": True, "skipped": reason})


def number(ctx: GateContext, name: str) -> float | None:
    """A numeric parameter, or `None` when the planner did not choose one."""
    value = ctx.params.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def count(ctx: GateContext, name: str) -> int | None:
    """A whole-number parameter, or `None` when the planner did not choose one."""
    value = ctx.params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
