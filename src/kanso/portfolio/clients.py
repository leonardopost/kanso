"""Which execution clients a workspace has, and what each of them declares.

An execution client is named in `portfolio.yaml` by an id, and the id is all the file
carries. What matters about it is the pair of declarations behind the id: how its capital is
funded — simulated, a broker's paper account, or real money — and which clock it runs on.
Those two facts are what forbid real money off the live stage, what forbid a historical
replay feeding a broker matching against current prices, and what make a promotion the only
way a version reaches real capital.

kanso ships exactly one client of its own, the simulated `sandbox`. Every other one belongs
to a broker adapter: a packaged one is discovered from the adapter directory, and a
workspace's own declares itself in an `EXEC_CLIENTS` table on its extension module, in the
same way a construct or a gate is declared. The core therefore knows no broker — it names
none of them and asks each only what it declares — and a workspace with no adapter still has
a stage it can deploy to, which is what makes the suite, `doctor` and the demo green with
every broker credential unset.

**A packaged client is registered whether or not it is configured.** Discovery costs no
credential and opens no socket: an adapter is enabled by the presence of its variables,
never by installation, so listing what a stage may name is answerable in a workspace that
has set nothing. What the two declarations behind an id then forbid is decided before
anything connects, which is the point of their being declarations at all.

**What a configuration is refused for lives here too.** `refusals` runs the checks `deploy`
runs, on a stage as it stands, so `kanso portfolio clients` and `kanso doctor` report
exactly what the command would do rather than a description of it that can drift. One of
those checks is this module's own: a stage node in this version is a bounded replay into
kanso's simulated venue, so a `clock: wall` client — which fills against current prices and
needs a node that outlives the command — is refused rather than filled in simulation and
recorded as the broker's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from kanso import ext
from kanso.errors import KansoError, PreconditionError
from kanso.nautilus import adapters
from kanso.portfolio import files
from kanso.schemas import SANDBOX, ExecutionClientSpec, check_execution_client
from kanso.schemas.portfolio import STAGES
from kanso.schemas.venue import REPLAY_DATA_CLIENT

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = [
    "EXEC_CLIENTS_ATTR",
    "EXTENSION",
    "FRAMEWORK",
    "LIVE_ONLY",
    "REAL",
    "WALL",
    "Declared",
    "builtin",
    "check_runnable",
    "declared",
    "get",
    "refusals",
    "registry",
]

EXEC_CLIENTS_ATTR: Final = "EXEC_CLIENTS"
"""The module attribute an extension declares its execution clients in."""

FRAMEWORK: Final = "kanso"
"""What declares the simulated client: the framework, not an adapter."""

EXTENSION: Final = "extension"
"""What declares a client no packaged adapter provides: this workspace's own code."""

REAL: Final = "real"
"""The funding that may be configured on one stage only."""

LIVE_ONLY: Final = ("live",)
"""The stages a real-capital client may be configured on, and there is one."""

WALL: Final = "wall"
"""The clock declaration a stage node in this version cannot honour; see `check_runnable`."""


def builtin() -> dict[str, ExecutionClientSpec]:
    """Every client that ships: the simulated venue, then each packaged broker's.

    The simulated one first, so a broker declaring `sandbox` could not take the id out
    from under the one client every workspace is guaranteed. The brokers are read from
    the adapter directory rather than listed here, so the core still names none of them.
    """
    return {SANDBOX.id: SANDBOX} | {
        client_id: spec
        for client_id, spec in adapters.exec_clients().items()
        if client_id != SANDBOX.id
    }


def registry(ws: Workspace | None = None) -> dict[str, ExecutionClientSpec]:
    """Every execution client this workspace can name, by id.

    An extension whose declaration is unusable is skipped rather than raised: a broken
    adapter degrades a workspace to the clients that do load, and `deploy` then refuses the
    id that is missing with a message naming what is there.
    """
    found = builtin()
    if ws is None:
        return found
    for extension in ext.discover(ws.root, ws.config.extensions_paths):
        if not extension.ok:
            continue
        for spec in _declared(getattr(extension.module, EXEC_CLIENTS_ATTR, None)):
            found.setdefault(spec.id, spec)
    return found


def get(client_id: str, ws: Workspace | None = None) -> ExecutionClientSpec:
    """One execution client's declarations, refusing an id nothing provides."""
    known = registry(ws)
    spec = known.get(client_id)
    if spec is None:
        raise PreconditionError(
            f"exec: {client_id!r} is not an execution client of this workspace, which has "
            f"{', '.join(sorted(known))}",
            remedy="install the broker adapter that provides it, or use `sandbox`",
        )
    return spec


@dataclass(frozen=True, slots=True)
class Declared:
    """One execution client as an operator needs to see it, and never a credential value.

    `origins` says where each variable this client's account needs resolves from — the
    workspace `.env`, the process environment, or nowhere — and nothing here ever holds
    what one of them contains. That is the whole of what a listing may know about a
    secret, and it is why this answers at all in a workspace that has set none of them.
    """

    id: str
    capital: str
    clock: str
    source: str
    credentials: tuple[str, ...] = ()
    origins: dict[str, str | None] = field(default_factory=dict)

    @property
    def stages(self) -> tuple[str, ...]:
        """The stages this client may be configured on: real capital reaches one only."""
        return LIVE_ONLY if self.capital == REAL else STAGES

    @property
    def configured(self) -> bool:
        """Whether every variable this client needs resolves somewhere.

        A client needing none is configured trivially, which is what keeps the simulated
        venue deployable in a workspace with nothing set.
        """
        return all(self.origins.get(name) is not None for name in self.credentials)

    def payload(self) -> dict[str, Any]:
        """The client as one JSON object: declarations, names, origins, no value."""
        return {
            "id": self.id,
            "capital": self.capital,
            "clock": self.clock,
            "source": self.source,
            "stages": list(self.stages),
            "credentials": list(self.credentials),
            "credentials_resolve": self.configured,
            "credential_origins": {name: self.origins.get(name) for name in self.credentials},
        }


def declared(ws: Workspace | None = None) -> list[Declared]:
    """Every execution client this workspace can name, described without opening one.

    The declarations come from the registry and the credential *names* from whichever
    adapter provides the client; a client no adapter claims declares no variable, which
    is the ordinary case for the simulated venue and for a workspace's own extension.
    Nothing here resolves a value, builds a client or reaches a network.
    """
    found: list[Declared] = []
    for client_id, spec in sorted(registry(ws).items()):
        broker = adapters.broker_of(client_id)
        names: tuple[str, ...] = () if broker is None else tuple(broker.credentials(client_id))
        origins: dict[str, str | None] = {}
        if broker is not None and ws is not None:
            origins = broker.credential_origins(ws, client_id)
        found.append(
            Declared(
                id=client_id,
                capital=spec.capital,
                clock=spec.clock,
                source=_source(client_id, broker),
                credentials=names,
                origins=origins,
            )
        )
    return found


def _source(client_id: str, broker: adapters.BrokerAdapter | None) -> str:
    """What declares this client: the framework, a packaged adapter, or the workspace."""
    if broker is not None:
        return broker.id
    return FRAMEWORK if client_id == SANDBOX.id else EXTENSION


def _declared(value: object) -> tuple[ExecutionClientSpec, ...]:
    """The specifications an extension's table holds, ignoring anything else it says."""
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        return ()
    return tuple(spec for spec in value if isinstance(spec, ExecutionClientSpec))


def check_runnable(stage: str, spec: ExecutionClientSpec) -> None:
    """Refuse a wall-clock client, which this version's node cannot actually run.

    A stage node here is a bounded run: it releases whatever the catalog holds that the
    stage has not replayed, into kanso's own simulated venue, flattens and returns. A
    client declaring `clock: replay` is fed exactly that and is executed by exactly that.
    A client declaring `clock: wall` fills against current prices, so it needs a node that
    outlives the command that started it and a feed that is still arriving — and running
    one through this node would fill every order in simulation while the stage record, and
    the gates reading it, called the money the broker's. That is the one failure this
    milestone exists to make impossible, so it is refused rather than approximated.

    The refusal is the run's and never the configuration's: what a client declares is
    checked before this, and a real-capital client still fails first for the approval it
    is missing rather than for the node it would have run in.
    """
    if spec.clock != WALL:
        return
    raise PreconditionError(
        f"stages.{stage}.exec: {spec.id!r} runs on the wall clock and fills against current "
        "prices, and a stage node in this version is a bounded replay of the catalog into "
        "kanso's own simulated venue; running one through it would record a simulated fill "
        "as the broker's",
        remedy=f"set stages.{stage}.exec to {SANDBOX.id!r} and stages.{stage}.data to "
        f"{REPLAY_DATA_CLIENT!r} until a stage node outlives the command that starts it",
    )


def refusals(ws: Workspace) -> dict[str, str | None]:
    """Per stage, what `deploy` would refuse its execution client for, or `None`.

    Every refusal that is a fact about the configuration rather than about a version: an
    id nothing provides, a pairing the declarations forbid, and a client this version's
    node cannot run. They are the calls `deploy` itself makes, so a report of them cannot
    drift from what the command would do — which is the point of reporting them at all,
    since an operator would rather read a refusal than run into one.
    """
    found: dict[str, str | None] = {}
    portfolio = files.read(ws)
    for stage in STAGES:
        configured = files.stage_of(portfolio, stage)
        try:
            spec = get(configured.exec, ws)
            check_execution_client(
                stage,
                spec,
                data_client=configured.data,
                speed=configured.speed,
                approved=True,
            )
            check_runnable(stage, spec)
        except KansoError as refusal:
            found[stage] = refusal.message
        else:
            found[stage] = None
    return found
