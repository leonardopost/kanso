"""Where a broker's specifics live, and the only place they may.

One package per broker, each holding everything that knows the broker's hosts, its order
grammar, its wire formats and the venues it serves. Nothing outside such a package names
a broker: the rest of kanso reaches every one of them through the execution client ids
they declare and through the adapter this module discovers, and works with none of them
configured. That isolation is a tested property rather than a convention — the suite,
`kanso doctor` and the demo are all green with every broker credential unset.

**Adapters are discovered, never named.** Each subpackage of this one that exposes a
module-level `BROKER` satisfying `BrokerAdapter` is registered under that adapter's own
id. The directory is the whole declaration, so adding a broker adds a directory and edits
no list, and no module in the core spells a broker's name.

**Discovery costs no credential and opens no socket.** An adapter is enabled by the
presence of its credentials, never by installation, so a workspace with every broker
variable unset discovers exactly the same adapters and simply finds none of them
configured. The engine-facing pieces — the execution client, the live data client, the
tradability overlay — are reached through methods that import their module when called,
so listing what a workspace can deploy to builds nothing and needs nothing.

**What an adapter declares here is what the core is allowed to know.** Two facts per
execution client: how its capital is funded and which clock it runs on. Those are what
forbid real money off the live stage and a historical replay feeding a broker that fills
against current prices, and they are declarations rather than behaviour precisely so the
refusals can be made before anything connects.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import ExecutionClientSpec, VenueDeclaration
    from kanso.workspace import Workspace

PACKAGE = "kanso.nautilus.adapters"
"""Where the packaged broker adapters live. A directory of subpackages is the whole
declaration: this module names the directory, and no module in kanso names what is in it."""

BROKER_ATTR = "BROKER"
"""The module attribute a packaged broker adapter exposes itself under."""

__all__ = [
    "BROKER_ATTR",
    "PACKAGE",
    "BrokerAdapter",
    "broker_of",
    "exec_clients",
    "packaged",
    "venue_declaration",
]


@runtime_checkable
class BrokerAdapter(Protocol):
    """One broker, behind the only interface the core reaches a broker through.

    Every method that touches a workspace takes it as an argument, because a credential
    is resolved at the moment of use and never held: two workspaces on one host may hold
    two different accounts, and an adapter that cached one would trade the other's money.
    """

    id: str
    kind: str
    exec_clients: tuple[ExecutionClientSpec, ...]
    """What this broker offers as execution clients, each declaring its funding and clock."""

    data_clients: tuple[str, ...]
    """The live data client ids this broker offers, or `()` when it offers none."""

    def credentials(self, client_id: str) -> tuple[str, ...]:
        """The variable names one of this broker's clients resolves, never their values."""
        ...

    def credential_origins(self, ws: Workspace, client_id: str) -> dict[str, str | None]:
        """Per variable, where it resolves from — never what it holds."""
        ...

    def configured(self, ws: Workspace, client_id: str) -> bool:
        """Whether this workspace holds what that client needs to be opened at all."""
        ...

    def venue_declaration(self, venue: str) -> VenueDeclaration | None:
        """What this broker declares about a venue it serves, or `None` for one it does not."""
        ...


def packaged() -> dict[str, BrokerAdapter]:
    """Every packaged broker adapter, read from the directory rather than from a list.

    Each module or package directly under this one that exposes a `BROKER` satisfying the
    protocol is registered under that adapter's own id, and anything else is passed over
    in silence, because a shared helper living beside the adapters is not an error.
    Registering by the adapter's own id rather than by the directory's name means an id
    and its home cannot silently disagree.
    """
    package = importlib.import_module(PACKAGE)
    found: dict[str, BrokerAdapter] = {}
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        module = importlib.import_module(f"{PACKAGE}.{info.name}")
        broker = getattr(module, BROKER_ATTR, None)
        if isinstance(broker, BrokerAdapter):
            found[broker.id] = broker
    return found


def exec_clients() -> dict[str, ExecutionClientSpec]:
    """Every execution client the packaged brokers declare, by id.

    The first adapter to claim an id keeps it, adapters being visited in id order, so the
    table is the same on every host and a clash is resolved the same way twice.
    """
    found: dict[str, ExecutionClientSpec] = {}
    for _, broker in sorted(packaged().items()):
        for spec in broker.exec_clients:
            found.setdefault(spec.id, spec)
    return found


def broker_of(client_id: str) -> BrokerAdapter | None:
    """The broker declaring `client_id` as one of its execution clients, or `None`."""
    for _, broker in sorted(packaged().items()):
        if any(spec.id == client_id for spec in broker.exec_clients):
            return broker
    return None


def venue_declaration(broker_id: str | None, venue: str) -> VenueDeclaration | None:
    """What `broker_id` declares about `venue`, or `None` when nothing declares anything.

    `None` for a broker no adapter provides rather than a refusal: a workspace naming a
    broker it has no adapter for still resolves its venues from the shipped defaults, and
    a venue model that fell back is recorded as having done so.
    """
    if broker_id is None:
        return None
    broker = packaged().get(broker_id)
    return None if broker is None else broker.venue_declaration(venue)
