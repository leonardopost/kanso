"""What happens by itself when a certificate passes: the version, and the paper stage.

A hypothesis that survives its embargo does not wait for a person. The construct composes
the version and the paper stage takes it, because both acts are entirely determined by the
certificate — which construct, which host, which capital rule — and a research loop that
runs indefinitely cannot stop at every certificate to ask for a command that has only one
possible form. The one act that is *not* automatic is the next one: paper to live needs a
named operator, and nothing here goes near it.

**The aftermath never fails the certification.** The certificate is written, immutable, and
already true before any of this runs. A stage that is halted, an engine that has moved, a
catalog with no forward data or a portfolio with no capital left are all reasons a version
cannot trade yet, and none of them is a reason to lose the evidence: the refusal becomes a
`deploy_blocked` escalation naming the version and the way out, and the verdict stands.

**Composing twice is composing once.** Certification is automatic and so is this, so a
hypothesis certified again under a new plan or a new engine returns the version it already
has rather than a second copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kanso.errors import KansoError
from kanso.inbox import escalate
from kanso.portfolio import records
from kanso.portfolio.deploy import BLOCKED, PAPER, Deployment, deploy
from kanso.strategy import compose, strategy_id_of

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import Certificate, StrategyVersion
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["Adoption", "on_certified"]


@dataclass(frozen=True)
class Adoption:
    """What a passing certificate turned into: a version, a stage, or a reason for neither."""

    strategy_id: str
    version: StrategyVersion | None = None
    deployment: Deployment | None = None
    blocked: str | None = None

    @property
    def label(self) -> str:
        """How the composed version is named to an operator."""
        if self.version is None:
            return self.strategy_id
        return records.subject_of(self.strategy_id, self.version.version)

    @property
    def state(self) -> str | None:
        """Where the version stands once the stage has had it, when it was composed."""
        if self.version is None:
            return None
        if self.deployment is None:
            return self.version.state
        admitted = next(
            (
                one
                for one in self.deployment.admitted
                if one.strategy_id == self.strategy_id and one.version == self.version.version
            ),
            None,
        )
        return PAPER if admitted is not None else self.version.state

    @property
    def capital(self) -> float | None:
        """The money the paper stage gave it, when it took it."""
        if self.deployment is None or self.version is None:
            return None
        return next(
            (
                one.capital
                for one in self.deployment.admitted
                if one.strategy_id == self.strategy_id and one.version == self.version.version
            ),
            None,
        )

    def payload(self) -> dict[str, object]:
        """The adoption as one JSON object."""
        return {
            "strategy": self.strategy_id,
            "version": None if self.version is None else self.version.version,
            "state": self.state,
            "capital": self.capital,
            "blocked": self.blocked,
        }


def on_certified(ws: Workspace, store: StateStore, made: Certificate) -> Adoption:
    """Compose this passing certificate and put the version it makes on the paper stage."""
    strategy_id = strategy_id_of(made)
    try:
        version = compose(ws, store, made.hyp_id)
    except KansoError as error:
        return _blocked(ws, store, strategy_id, None, error)
    try:
        deployment = deploy(ws, store, PAPER)
    except KansoError as error:
        return _blocked(ws, store, strategy_id, version, error)
    return Adoption(strategy_id=strategy_id, version=version, deployment=deployment)


def _blocked(
    ws: Workspace,
    store: StateStore,
    strategy_id: str,
    version: StrategyVersion | None,
    error: KansoError,
) -> Adoption:
    """Record why the certificate could not reach the paper stage, and leave it standing."""
    adoption = Adoption(strategy_id=strategy_id, version=version, blocked=error.message)
    escalate(
        ws,
        store,
        BLOCKED,
        adoption.label,
        f"a passing certificate could not reach the paper stage: {error.message}",
        actions=str(error.remedy or ""),
    )
    return adoption
