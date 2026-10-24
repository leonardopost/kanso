"""The run record and the card: what research writes into the state store.

A run is one lane working one hypothesis against one pinned snapshot; a card is one
evaluated `strategy.py` inside it. Neither is a workspace file. `results.tsv` is rendered
from cards, and its row is defined here so that every renderer produces the same bytes.

A crashed card carries a zero metric by definition: nothing was measured, and letting a
crash report anything else would let a timeout beat a working strategy. A `redundant` card
is the opposite: it ran, it was measured, and what it measured was a book already judged
under the run's pins and, to within the hypothesis's noise floor, that book's own number,
so it carries its real number and may never be a keep.

A card's `tags` are the proposer's own account of what the change was, drawn from the
vocabulary `TAGS` fixes. The vocabulary is the package's rather than the model's because
the tags are read back as a coverage table keyed by them, and a table keyed by whatever
each answer happened to say would never have two rows in the same cell.

A card that ran carries a `digest`: what its trades did, beside the one number it is scored
on. The metric alone cannot say how it moved — a per-trade edge that rose because the worst
half of the clips were dropped and one that rose because every clip earned more read the
same, and a contribution that fell because the book traded less reads like one whose trades
got worse — and the proposer, the operator and `results.tsv` were each shown that number and
nothing else.

A card that ran also keeps what its metric was computed from: `measured_sha` names the blob
holding its objective and folds, the run it measured, the host and benchmark runs the
objective read beside it and the criteria version that scored it
(`kanso.research.records.Measurement`). The number is one reading of that run, and the run
is what any other reading needs — a prefix of the window, one week, one fold — so it is kept
rather than reconstructed by running the bytes again.
"""

from __future__ import annotations

from datetime import datetime
from math import isclose
from typing import Annotated, Final, Literal, get_args

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

CardStatus = Literal["keep", "discard", "crash", "redundant"]

Tag = Literal[
    "signal_trend",
    "signal_mean_reversion",
    "signal_breakout",
    "signal_volatility",
    "signal_volume",
    "signal_relative",
    "horizon_shorter",
    "horizon_longer",
    "filter_regime",
    "filter_time",
    "filter_volatility",
    "filter_liquidity",
    "exit_stop",
    "exit_target",
    "exit_time",
    "exit_signal",
    "sizing_fixed",
    "sizing_volatility",
    "sizing_conviction",
    "parameter_only",
    "refactor",
]
TAGS: Final[tuple[str, ...]] = get_args(Tag)
"""The vocabulary a proposal describes itself in: what the change reads (`signal_*`), how
long it holds (`horizon_*`), what it withholds on (`filter_*`), what closes it (`exit_*`),
how it sizes (`sizing_*`), and the two that say a change moved no structure. A card may
carry several; every card carries at least one."""

RunTag = Annotated[str, StringConstraints(pattern=r"^[0-9]{8}-[0-9]+$")]
"""`<yyyymmdd>-<n>`: the nth run started on that date."""

LaneName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,16}$")]

DIGEST_COLUMNS: Final = (
    "trades_per_session",
    "hit_rate",
    "median_hold_s",
    "maker_share",
    "fill_ratio",
    "gross_bps_per_trade",
    "cost_bps_per_trade",
    "net_bps_per_trade",
    "net_bps_per_trade_se",
    "gross_bps_per_session",
    "cost_bps_per_session",
    "net_bps_per_session",
)
"""The digest's numbers a `results.tsv` row carries after `desc`: appended rather than
inserted, so a reader that takes the first nine columns by position reads what it always
read."""

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
    *DIGEST_COLUMNS,
)
RESULTS_HEADER: Final = "\t".join(RESULTS_COLUMNS)

Bps = Annotated[float, Field(allow_inf_nan=False)]
Share = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Positive = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class CardDigest(KansoModel):
    """What a card's trades did: how often it traded, how it filled, and what each trade and
    each session earned before and after costs.

    Computed by `kanso.criteria.digest` from the run the card measured — its fills, its
    closed trades, its return series — and the orders it submitted, once, in the lane, and
    recorded with the card. Per trade is in basis points of the position opened, over the
    closed trades with a notional, as the per-trade objective reads them; per session is in
    basis points of the capital, over the UTC days the run struck a period end in. Gross is
    before fill costs and net after, so gross less cost is net in both; funding, which is a
    payment on what was held and not a fill cost, stays in gross. A number that has nothing
    to be read off — a rate with no trade, a share with no fill, the error of an edge whose
    trades closed in fewer than two periods, which an error read period by period reads as
    zero whatever they did — is `None`, never zero.
    """

    sessions: int = Field(ge=0)
    fills: int = Field(ge=0)
    trades_per_session: Positive | None = None
    hit_rate: Share | None = None
    median_hold_s: Positive | None = None
    maker_share: Share | None = None
    fill_ratio: Positive | None = None
    gross_bps_per_trade: Bps | None = None
    cost_bps_per_trade: Bps | None = None
    net_bps_per_trade: Bps | None = None
    net_bps_per_trade_se: Positive | None = None
    gross_bps_per_session: Bps | None = None
    cost_bps_per_session: Bps | None = None
    net_bps_per_session: Bps | None = None

    @model_validator(mode="after")
    def _consistent(self) -> CardDigest:
        per_trade = (
            self.hit_rate,
            self.median_hold_s,
            self.gross_bps_per_trade,
            self.cost_bps_per_trade,
            self.net_bps_per_trade,
        )
        if len({value is None for value in per_trade}) != 1:
            raise ValueError(
                "net_bps_per_trade: the per-trade numbers are read off the same closed trades, "
                "so they are all stated or all absent"
            )
        if self.net_bps_per_trade is None and self.net_bps_per_trade_se is not None:
            raise ValueError("net_bps_per_trade_se: an error is an edge's, and none is stated")
        per_session = (
            self.trades_per_session,
            self.gross_bps_per_session,
            self.cost_bps_per_session,
            self.net_bps_per_session,
        )
        if any((value is None) != (self.sessions == 0) for value in per_session):
            raise ValueError(
                "net_bps_per_session: the per-session numbers are stated exactly when the run "
                "measured a session"
            )
        if self.fills == 0 and self.maker_share is not None:
            raise ValueError("maker_share: a run with no fill made no share of them as a maker")
        for gross, cost, net in (
            (self.gross_bps_per_trade, self.cost_bps_per_trade, self.net_bps_per_trade),
            (self.gross_bps_per_session, self.cost_bps_per_session, self.net_bps_per_session),
        ):
            if gross is not None and cost is not None and net is not None:
                scale = max(1.0, abs(gross), abs(cost), abs(net))
                if not isclose(gross - cost, net, rel_tol=0.0, abs_tol=1e-9 * scale):
                    raise ValueError(f"gross: {gross} less the cost {cost} is not the net {net}")
        return self

    def cells(self) -> tuple[str, ...]:
        """The `results.tsv` cells of `DIGEST_COLUMNS`, an absent number an empty cell."""
        return tuple(_cell(getattr(self, name)) for name in DIGEST_COLUMNS)


def _cell(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


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
    tags: list[Tag] = Field(default_factory=list)
    gate_results: list[GateResult] = Field(default_factory=list)
    crash_tail: str | None = None
    venue_model: VenueModel
    created_at: datetime
    digest: CardDigest | None = None
    """What the card's trades did; `None` for a card that never ran to a result — a crash,
    one refused before or during its backtest — and for one recorded before cards kept it."""
    measured_sha: Sha256 | None = None
    """The blob holding what the card's metric was computed from — its objective and folds,
    its run, the host and benchmark runs the objective read, and the criteria version that
    scored it, under which the objective on them returns `metric` and `metric_se` exactly;
    `None` for a card that never ran to a result — a crash, one refused before or during its
    backtest — and for one recorded before cards kept it."""

    @property
    def sha7(self) -> str:
        """The seven-character prefix a certificate file and a results row carry."""
        return self.strategy_sha[:7]

    def row(self) -> str:
        """The `results.tsv` line for this card."""
        digest = ("",) * len(DIGEST_COLUMNS) if self.digest is None else self.digest.cells()
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
                *digest,
            )
        )

    @model_validator(mode="after")
    def _consistent(self) -> Card:
        if any(c in self.desc for c in "\t\r\n"):
            raise ValueError("desc: a results row is tab separated, so no tabs or newlines")
        if self.status == "crash":
            if self.metric != 0:
                raise ValueError("metric: a crashed card measured nothing and reports 0")
            if self.digest is not None:
                raise ValueError("digest: a crashed card measured nothing and digests nothing")
            if self.measured_sha is not None:
                raise ValueError("measured_sha: a crashed card measured nothing and keeps nothing")
        elif self.crash_tail is not None:
            raise ValueError(f"crash_tail: only a crashed card carries one, not a {self.status}")
        return self
