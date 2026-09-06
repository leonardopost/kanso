"""What the portfolio looks like now: the file, whether each node is current, and the P&L.

`portfolio show` answers three questions an operator has at once. What is configured — which
is the file, read back rather than described. Whether each stage's node is where it should
be — which for a replay-clocked stage means whether it has consumed everything the catalog
holds: a stage whose clock has fallen behind is a stage that was deployed and has not been
redeployed since new data arrived, and a halted stage is not up at all. And what each
deployed version has actually made, which is the sum of the windows its stage closed, since
a node flattens before every stop and each redeploy therefore realises one.

**The file is read against the record, never believed on its own.** `portfolio.yaml` is an
operator's file, and an entry added to a stage by hand funds nothing and approves nothing:
what a stage holds is what a deployment wrote into the store, which is the same answer
`deploy` acts on. An entry the record does not know is reported as the file's claim and
counted in nothing — not the stage's allocation, not its P&L, not whether its node is up —
because printing it beside a deployed version would say a node had run it.

Nothing here starts, stops or changes anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from kanso.criteria.run import day_of
from kanso.errors import PreconditionError
from kanso.portfolio import files, records
from kanso.portfolio.deploy import clock_of, served_to
from kanso.schemas.portfolio import STAGES

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.portfolio.records import StageResult
    from kanso.schemas import Portfolio
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["Deployed", "Report", "StageReport", "show"]


@dataclass(frozen=True)
class Deployed:
    """One entry of a stage's file: what it claims, whether the record knows it, and its P&L.

    `recorded` is the whole of the difference between an entry a deployment wrote and one
    an operator typed into `portfolio.yaml`: false says the store has no such version on
    this stage, so nothing was funded, nothing was approved and no node ran it.
    """

    strategy_id: str
    version: int
    capital: float
    joined_at: datetime
    results: tuple[StageResult, ...]
    recorded: bool

    @property
    def label(self) -> str:
        """How this version is named to an operator."""
        return records.subject_of(self.strategy_id, self.version)

    @property
    def pnl(self) -> float:
        """The realised and marked change in equity over every window this stage closed."""
        return sum(result.pnl for result in self.results)

    @property
    def windows(self) -> int:
        """How many windows this version has been measured over on this stage."""
        return len(self.results)


@dataclass(frozen=True)
class StageReport:
    """One stage: how it is configured, whether its node is current, and what is on it."""

    stage: str
    exec_id: str
    data: str
    speed: float
    capital: float
    kill_switch: bool
    clock_ns: int | None
    served_to: date | None
    strategies: tuple[Deployed, ...]

    @property
    def clock(self) -> date | None:
        """The day this stage's replay position stands on."""
        return None if self.clock_ns is None else day_of(self.clock_ns)

    @property
    def held(self) -> tuple[Deployed, ...]:
        """The entries the record knows: what this stage actually holds."""
        return tuple(one for one in self.strategies if one.recorded)

    @property
    def live(self) -> bool:
        """Whether this stage's node is up: unhalted, holding versions, and current.

        A stage fed by catalog replay is current when its clock has reached the last day
        the catalog serves for what it trades. A stage that has fallen behind is one that
        was deployed before the data it has not seen arrived, and the way to bring it up is
        to deploy it again. A stage whose only entries are the file's own is not up: no
        node was ever given them.
        """
        if self.kill_switch or not self.held:
            return False
        return (
            self.served_to is not None and self.clock is not None and self.clock >= self.served_to
        )

    @property
    def allocated(self) -> float:
        """The stage capital its deployed versions hold between them."""
        return sum(one.capital for one in self.held)

    @property
    def pnl(self) -> float:
        """What this stage has made over every window it has closed."""
        return sum(one.pnl for one in self.held)


@dataclass(frozen=True)
class Report:
    """The whole deployment surface: the file as it stands, and both stages."""

    portfolio: Portfolio
    stages: tuple[StageReport, ...]

    def stage(self, name: str) -> StageReport:
        """One stage's report by name."""
        found = next((one for one in self.stages if one.stage == name), None)
        if found is None:  # pragma: no cover - the two stages are the whole of it
            raise PreconditionError(f"stage: {name!r} is not a deployment stage")
        return found


def show(ws: Workspace, store: StateStore) -> Report:
    """The portfolio file, each stage's liveness, and every deployed version's realised P&L."""
    portfolio = files.read(ws)
    return Report(
        portfolio=portfolio,
        stages=tuple(_stage(ws, store, portfolio, name) for name in STAGES),
    )


def _stage(ws: Workspace, store: StateStore, portfolio: Portfolio, name: str) -> StageReport:
    """One stage of the report: its file entries, each read against what the record holds."""
    configured = files.stage_of(portfolio, name)
    recorded = records.staged(store, name)
    entries = tuple(
        Deployed(
            strategy_id=entry.id,
            version=entry.version,
            capital=entry.capital,
            joined_at=entry.joined_at,
            results=tuple(
                records.stage_results(
                    store, strategy_id=entry.id, version=entry.version, stage=name
                )
            ),
            recorded=recorded.get(entry.id) == entry.version,
        )
        for entry in configured.strategies
    )
    return StageReport(
        stage=name,
        exec_id=configured.exec,
        data=configured.data,
        speed=configured.speed,
        capital=configured.capital,
        kill_switch=configured.kill_switch,
        clock_ns=clock_of(store, name),
        served_to=served_to(ws, _universe(ws, [one for one in entries if one.recorded])),
        strategies=entries,
    )


def _universe(ws: Workspace, entries: Sequence[Deployed]) -> tuple[str, ...]:
    """Every instrument this stage's deployed versions trade, from their strategy files.

    Only the entries the record knows, because this is what the stage's node needs served
    and a file entry no node ran needs nothing; a hand-written one may also name a version
    that was never composed, and the last day the catalog serves is not the place to find
    that out.
    """
    from kanso import strategy as strategies

    names: set[str] = set()
    for entry in entries:
        file = strategies.read(ws, entry.strategy_id)
        if file is None:  # pragma: no cover - a recorded version has its strategy file
            continue
        version = file.versions[entry.version - 1]
        names.update(_hyp_universe(ws, version.sleeve.hyp_id))
    return tuple(sorted(names))


def _hyp_universe(ws: Workspace, hyp_id: str) -> tuple[str, ...]:
    """The universe a sleeve hypothesis declares, as its own file states it."""
    from kanso.hyp import HYPOTHESIS_FILE
    from kanso.schemas import Hypothesis, load_yaml

    path = ws.path("hypotheses", hyp_id, HYPOTHESIS_FILE)
    if not path.is_file():  # pragma: no cover - a deployed version has its hypothesis
        return ()
    return tuple(load_yaml(Hypothesis, path).universe)
