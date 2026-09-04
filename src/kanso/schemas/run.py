"""The run record and the card: what research writes into the state store.

A run is one lane working one hypothesis against one pinned snapshot; a card is one
evaluated `strategy.py` inside it. Neither is a workspace file. `results.tsv` is rendered
from cards, and its row is defined here so that every renderer produces the same bytes.

A crashed card carries a zero metric by definition: nothing was measured, and letting a
crash report anything else would let a timeout beat a working strategy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, model_validator

from kanso.schemas.base import (
    CatalogueId,
    FreeForm,
    HypId,
    KansoModel,
    NonEmpty,
    Sha256,
)
from kanso.schemas.venue import VenueModel

CardStatus = Literal["keep", "discard", "crash"]

RunTag = Annotated[str, StringConstraints(pattern=r"^[0-9]{8}-[0-9]+$")]
"""`<yyyymmdd>-<n>`: the nth run started on that date."""

LaneName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,16}$")]

RESULTS_COLUMNS: Final = (
    "sha7",
    "metric",
    "metric_se",
    "n_trials",
    "n_trades",
    "wall_s",
    "peak_mem_gb",
    "status",
    "desc",
)
RESULTS_HEADER: Final = "\t".join(RESULTS_COLUMNS)


class GateResult(KansoModel):
    """One gate's verdict. A gate lacking its context passes and says why it was skipped."""

    id: CatalogueId
    passed: bool = Field(alias="pass")
    evidence: FreeForm = Field(default_factory=dict)
    skipped: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _skipped_passes(self) -> GateResult:
        if self.skipped is not None and not self.passed:
            raise ValueError("pass: a skipped gate passes, since it judged nothing")
        return self


class RunRecord(KansoModel):
    """One run of one hypothesis in one lane, against one pinned snapshot."""

    run_id: NonEmpty
    hyp_id: HypId
    tag: RunTag
    lane: LaneName
    dir: NonEmpty
    base_sha: Sha256
    hypothesis_sha: Sha256
    program_sha: Sha256
    snapshot_id: NonEmpty
    criteria_version: NonEmpty
    host_version: int | None = Field(default=None, ge=1)
    card_budget_s: float = Field(gt=0)
    baseline_wall_s: float = Field(ge=0)
    baseline_peak_mem_gb: float = Field(ge=0)
    best_sha: Sha256 | None = None
    best_metric: float | None = None
    started_at: datetime
    ended_at: datetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RunRecord:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError(f"ended_at: {self.ended_at} is before started_at {self.started_at}")
        if (self.best_sha is None) != (self.best_metric is None):
            raise ValueError("best_metric: is set exactly when best_sha is")
        return self


class Card(KansoModel):
    """One evaluated `strategy.py`: the trial that `n_trials` counts."""

    run_id: NonEmpty
    lane: LaneName
    strategy_sha: Sha256
    metric: float
    metric_se: float = Field(ge=0)
    n_trials: int = Field(ge=1)
    n_trades: int = Field(ge=0)
    wall_s: float = Field(ge=0)
    peak_mem_gb: float = Field(ge=0)
    status: CardStatus
    desc: str = Field(max_length=120)
    aligned: bool = True
    gate_results: list[GateResult] = Field(default_factory=list)
    crash_tail: str | None = None
    venue_model: VenueModel
    created_at: datetime

    @property
    def sha7(self) -> str:
        """The seven-character prefix a certificate file and a results row carry."""
        return self.strategy_sha[:7]

    def row(self) -> str:
        """The `results.tsv` line for this card."""
        return "\t".join(
            (
                self.sha7,
                f"{self.metric:.6f}",
                f"{self.metric_se:.6f}",
                str(self.n_trials),
                str(self.n_trades),
                f"{self.wall_s:.3f}",
                f"{self.peak_mem_gb:.3f}",
                self.status,
                self.desc,
            )
        )

    @model_validator(mode="after")
    def _consistent(self) -> Card:
        if any(c in self.desc for c in "\t\r\n"):
            raise ValueError("desc: a results row is tab separated, so no tabs or newlines")
        if self.status == "crash":
            if self.metric != 0:
                raise ValueError("metric: a crashed card measured nothing and reports 0")
        elif self.crash_tail is not None:
            raise ValueError(f"crash_tail: only a crashed card carries one, not a {self.status}")
        return self
