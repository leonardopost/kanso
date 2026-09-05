"""The loaders this adapter provides, one per data type it serves, and how they are opened.

Each implements `kanso.data.Loader` over the client and the entitlement probes in the
package above, so the catalog reaches vendor data through exactly the interface it reaches
a file or the generator through.

**Loaders are handed out as factories, not as instances.** A vendor loader is opened for
one workspace — that is where its credential, its quota and its cache come from — and
listing what this adapter can fetch must not build one. `kanso data adapters` prints these
ids in a workspace holding no credential at all; only `loader_for` calls a factory, and
only for the id a command actually named.

**Two transports, two loaders, and the operator chooses.** The request path serves any
class the plan entitles; the flat-file path serves the two aggregate resolutions the
object store lays out, for the classes it lays them out for. Nothing here picks between
them: their ids never collide, so a spec names the transport it wants and a command
fetches over the one it was given. The two agree about what a bar is, which is what makes
a history filled over one and extended over the other a single series.

Importing this module registers the custom types the fundamentals loaders yield, because a
type has to be registered before a hypothesis may require it and registering it costs no
credential and opens nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from kanso.data.adapters.massive.client import MassiveClient
from kanso.data.adapters.massive.corporate_actions import CorporateActionsLoader
from kanso.data.adapters.massive.financials import FinancialsLoader
from kanso.data.adapters.massive.loaders import bulk
from kanso.data.adapters.massive.loaders.bars import MassiveBarsLoader
from kanso.data.adapters.massive.loaders.quotes import MassiveQuotesLoader
from kanso.data.adapters.massive.loaders.trades import MassiveTradesLoader

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.data.loader import Loader
    from kanso.workspace import Workspace

__all__ = ["loaders"]


def loaders(ws: Workspace) -> dict[str, Callable[[], Loader]]:
    """Every loader this adapter provides for `ws`, as a factory per id.

    Nothing is built here and no credential is resolved here. The request loaders take the
    workspace and open one rate-limited connection when they are first used; the bulk
    loader resolves the object store's two keys, which are separate names from the REST
    one, at the moment it is built.
    """
    return {
        MassiveBarsLoader.id: lambda: MassiveBarsLoader(workspace=ws),
        MassiveTradesLoader.id: lambda: MassiveTradesLoader(workspace=ws),
        MassiveQuotesLoader.id: lambda: MassiveQuotesLoader(workspace=ws),
        bulk.BulkLoader.id: lambda: bulk.loader(ws),
        CorporateActionsLoader.id: lambda: CorporateActionsLoader(client=_client(ws)),
        FinancialsLoader.id: lambda: FinancialsLoader(client=_client(ws)),
    }


def _client(ws: Workspace) -> MassiveClient:
    """The authenticated connection the listing loaders read through.

    They hold a client rather than a workspace, so the credential is resolved here — once,
    where every other credential of this adapter is resolved — rather than inside a loader.
    """
    from kanso.data.adapters.massive import ADAPTER

    return ADAPTER.client(ws)
