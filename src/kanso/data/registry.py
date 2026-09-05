"""The adapter registry: the one way a vendor's package reaches the rest of kanso.

An **adapter** is a vendor's whole presence here: the datasets it offers, the credential
names it needs, the loaders that fetch through it and the provider that resolves its
instruments. It is the registry's only entry point, and it is the same entry point for an
adapter this package ships and one a workspace extension provides.

**Adapters are discovered, never named.** The packaged adapters are the subpackages of
`kanso.data.adapters`, each exposing a module-level `ADAPTER`; an extension declares its
own ids in `PROVIDES["adapters"]` and exposes them in an `ADAPTERS` mapping, exactly as it
declares loaders. Nothing in this module — and nothing anywhere outside a vendor's own
package — spells a vendor's name, which is what makes the isolation property something a
source scan can check rather than something a reviewer has to remember.

**Discovery costs no credential and opens no socket.** An adapter is enabled by the
presence of its credentials, never by installation, so a workspace with every vendor
variable unset discovers exactly the same adapters and simply finds none of them
configured. That is why loaders are handed out as *factories*: listing what an adapter can
fetch must not build a loader whose construction would demand a key, and `data adapters`
lists ids in a workspace that could not open one of them.

**What the adapter declares and what it probes are different things.** `capabilities` is
the offer — the classes and datasets the adapter can ask for, and the grain the source
gates entitlement at. Whether *this* key reaches them, and how far back, is not a property
of the adapter at all: it belongs to the operator's plan on the day it is asked. `survey`
is where that is measured, and `Reach` is one measured answer. Declaring entitlement or a
history floor as a constant is precisely how an operator gets told to buy a subscription
they already hold, so nothing here lets an adapter do it.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from datetime import date

    from kanso.data.instruments import InstrumentProvider
    from kanso.data.loader import Loader
    from kanso.ext import Extension
    from kanso.workspace import Workspace

PACKAGE = "kanso.data.adapters"
"""Where the packaged adapters live. A directory of subpackages is the whole declaration:
this module names the directory, and no module in kanso names what is in it."""

__all__ = [
    "Adapter",
    "Capabilities",
    "Reach",
    "Survey",
    "adapter_loaders",
    "adapters",
    "packaged",
    "provider_for",
]


@runtime_checkable
class Capabilities(Protocol):
    """What an adapter offers, in a form the command line can print without knowing it."""

    def names(self) -> tuple[str, ...]:
        """The flat capability list, one short word each."""
        ...

    def payload(self) -> dict[str, object]:
        """The whole offer as one JSON object."""
        ...


@dataclass(frozen=True, slots=True)
class Reach:
    """What one key reaches for one dataset of one asset class, as measured.

    `grain` is the grain the answer may be reused at — per endpoint where the source
    gates a class as a whole, per ticker where it gates each ticker behind its own feed.
    It is part of the answer rather than decoration: an answer at ticker grain says
    nothing whatever about the next ticker, and an operator reading a green line for one
    index must not conclude anything about another.

    `floor` is the earliest day the source serves for the series that was probed, measured
    on `probed_on`. It is `None` where no floor was asked for, and `None` is not zero: a
    dataset whose floor was not measured has an unknown floor, never an unlimited history.
    """

    asset_class: str
    dataset: str
    grain: str
    outcome: str
    detail: str
    ticker: str | None = None
    floor: date | None = None
    probed_on: date | None = None

    @property
    def ok(self) -> bool:
        """True when the source served what was asked of it."""
        return self.outcome == "ok"

    def payload(self) -> dict[str, Any]:
        """The reach as one JSON object, carrying no credential and no vendor prose."""
        return {
            "asset_class": self.asset_class,
            "dataset": self.dataset,
            "grain": self.grain,
            "ticker": self.ticker,
            "outcome": self.outcome,
            "detail": self.detail,
            "floor": self.floor.isoformat() if self.floor else None,
            "probed_on": self.probed_on.isoformat() if self.probed_on else None,
        }

    def line(self) -> str:
        """The reach as one terse line, for the plain rendering."""
        where = f"{self.asset_class} {self.dataset}"
        if self.ticker is not None:
            where += f" ({self.ticker}, per {self.grain})"
        tail = f" · from {self.floor}" if self.floor is not None else ""
        return f"{where} → {self.outcome}{tail}"


@dataclass(frozen=True, slots=True)
class Survey:
    """What one adapter's credentials actually reach, and what it cost to find out.

    `reachable` answers the first question — does this credential authenticate at all —
    and it is asked first because every later answer would otherwise be ambiguous: a key
    the vendor does not accept refuses everything, and reporting that as "your plan
    excludes these datasets" would be the wrong answer in the most expensive direction.

    `requests` is the number of requests the survey made, so an operator can see that a
    check is bounded and a test can assert it.
    """

    adapter: str
    reachable: bool
    detail: str
    requests: int
    reach: tuple[Reach, ...] = ()
    notes: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "reachable": self.reachable,
            "detail": self.detail,
            "requests": self.requests,
            "reach": [item.payload() for item in self.reach],
            "notes": list(self.notes),
        }


@runtime_checkable
class Adapter(Protocol):
    """One vendor, behind the only interface the core reaches a vendor through.

    Every method takes the workspace it acts for, because a credential is resolved at the
    moment of use and never held: two workspaces on one host may hold two different keys,
    and an adapter that cached one would serve the other's data into it.
    """

    id: str
    kind: str
    capabilities: Capabilities
    credentials: tuple[str, ...]

    def client(self, ws: Workspace) -> object:
        """The authenticated connection, or a precondition failure naming the variable."""
        ...

    def configured(self, ws: Workspace) -> bool:
        """Whether this workspace holds what the adapter needs to be opened at all."""
        ...

    def credential_origins(self, ws: Workspace) -> dict[str, str | None]:
        """Per variable, where it resolves from — never what it holds."""
        ...

    def quota(self, ws: Workspace) -> str:
        """The rate limit this workspace would enforce, reportable with no credential."""
        ...

    def loaders(self, ws: Workspace) -> dict[str, Callable[[], Loader]]:
        """This adapter's loaders as factories, so listing them opens nothing."""
        ...

    def provider(self, ws: Workspace) -> InstrumentProvider | None:
        """The instrument provider this adapter offers, or `None` when it offers none."""
        ...

    def survey(self, ws: Workspace) -> Survey:
        """Probe what this key reaches, at the grain the source gates on."""
        ...


def adapters(extensions: Sequence[Extension] = ()) -> dict[str, Adapter]:
    """Every adapter available here: the packaged ones, then the extensions' own.

    A packaged id wins a clash, on the same rule the loader registry follows: the built-in
    answer is the one the suite and the demo run against, so an extension may add to it
    and may not replace it.
    """
    found = packaged()
    for extension in extensions:
        for adapter_id, adapter in _extension_adapters(extension).items():
            found.setdefault(adapter_id, adapter)
    return found


def adapter_loaders(
    ws: Workspace, extensions: Sequence[Extension] = ()
) -> dict[str, Callable[[], Loader]]:
    """Every loader every registered adapter provides for `ws`, as factories.

    Nothing is built here. A loader that needs a credential resolves it when it is called
    for, so this is safe to run — and to print — in a workspace that has none.
    """
    out: dict[str, Callable[[], Loader]] = {}
    for _, adapter in sorted(adapters(extensions).items()):
        for loader_id, factory in adapter.loaders(ws).items():
            out.setdefault(loader_id, factory)
    return out


def provider_for(
    ws: Workspace, adapter_id: str, extensions: Sequence[Extension] = ()
) -> InstrumentProvider | None:
    """The instrument provider `adapter_id` offers, or `None` when nothing offers one."""
    adapter = adapters(extensions).get(adapter_id)
    return None if adapter is None else adapter.provider(ws)


def packaged() -> dict[str, Adapter]:
    """Every packaged adapter, read from the adapter directory rather than from a list.

    The directory is the declaration: each module or package directly under it that exposes
    a module-level `ADAPTER` satisfying the protocol is registered under that adapter's own
    id, and anything else is passed over in silence, because a shared helper living beside
    the adapters is not an error. Registering by the adapter's own id rather than by the
    directory's name means an id and its home cannot silently disagree.
    """
    package = importlib.import_module(PACKAGE)
    found: dict[str, Adapter] = {}
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        module = importlib.import_module(f"{PACKAGE}.{info.name}")
        adapter = getattr(module, "ADAPTER", None)
        if isinstance(adapter, Adapter):
            found[adapter.id] = adapter
    return found


def _extension_adapters(extension: Extension) -> dict[str, Adapter]:
    """The adapters an extension both declares and exposes; anything else is ignored."""
    declared = extension.provides.get("adapters", ())
    table = getattr(extension.module, "ADAPTERS", None)
    if not declared or not isinstance(table, Mapping):
        return {}
    return {
        adapter_id: adapter
        for adapter_id in declared
        if isinstance(adapter := table.get(adapter_id), Adapter)
    }
