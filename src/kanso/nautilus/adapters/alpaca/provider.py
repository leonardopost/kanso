"""The instruments a stage trades here, which are kanso's own and never the broker's.

The engine asks a live client for instrument definitions, and the obvious thing to do is
to fetch them from the broker. This provider deliberately does not. An instrument in kanso
is a dated definition built from the regulator's tick and lot schedule and written to the
catalog when a universe is resolved, and it is that definition a hypothesis was certified
against: its price precision decides what a price rounds to, its lot size decides what a
size rounds to, and a stage that traded a different one would fill at prices the card
never measured. So the definitions served here are read out of the workspace's own
instrument store, and the broker is never asked what an instrument is.

**What the broker does say is a different question, and a different module answers it.**
Whether this broker will let a strategy trade an instrument at all — active, tradable,
shortable, easy to borrow — comes from its asset row, and that is the tradability
overlay's business. A definition and a permission are not the same fact, and keeping them
apart is what stops a broker's opinion from silently re-keying an instrument.

**A venue this broker does not serve yields nothing.** The store holds every instrument a
workspace has ever resolved, including the synthetic venues the demo and the suite use.
Serving one of those through a broker that cannot route to it would be a definition with
nowhere to send an order, so it is left out, and the data client refuses a subscription
for it by name rather than quietly never delivering a bar.

**The newest resolution wins.** The store is keyed by content address, so one instrument
may hold several definitions — one per date it was resolved as of. The one with the latest
initialisation instant is the current definition of that instrument, and the content
address breaks a tie so two hosts reading one store answer identically.

Nothing here opens a socket or resolves a credential: the whole of it is a read of a local
catalog, which is also why an unconfigured workspace can list what it would trade.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`LiveMarketDataClient.__init__` checks its `instrument_provider` against the engine's own
`nautilus_trader.common.providers.InstrumentProvider`, so a provider handed to a live
client must subclass that class rather than merely satisfy kanso's synchronous provider
protocol, which is a different interface for a different question. The base class holds
its instruments in a dictionary keyed by `InstrumentId`, exposes `add`, `add_bulk`,
`find`, `list_all` and `get_all`, and drives loading through the `load_all_async` and
`load_ids_async` coroutines — the two this class implements. `Instrument.id` is an
`InstrumentId` and `ts_init` is the nanosecond instant the definition was initialised at,
which for a kanso definition is midnight of the date it was resolved as of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.instruments import definition_checksum, read_store
from kanso.nautilus.adapters.alpaca.venue import serves

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Iterable, Sequence

    from kanso.workspace import Workspace

__all__ = ["AlpacaInstrumentProvider", "provider", "served"]


def served(definitions: Iterable[Any]) -> dict[InstrumentId, Any]:
    """The current definition of every instrument this broker can route an order for.

    Everything else the store holds is passed over: a definition on a venue the broker
    does not trade has nowhere to send an order, and one that is not an engine instrument
    at all is not this provider's business. Where an instrument holds several definitions
    the latest initialisation instant wins, with the content address breaking a tie so the
    same store answers the same way on every host.
    """
    newest: dict[InstrumentId, Any] = {}
    ranking: dict[InstrumentId, tuple[int, str]] = {}
    for definition in definitions:
        found = getattr(definition, "id", None)
        if not isinstance(found, InstrumentId) or not serves(found.venue.value):
            continue
        rank = (int(definition.ts_init), definition_checksum(definition))
        if found not in ranking or rank > ranking[found]:
            newest[found] = definition
            ranking[found] = rank
    return newest


class AlpacaInstrumentProvider(InstrumentProvider):
    """The workspace's own instrument definitions, as the engine's provider interface.

    The workspace is held rather than the definitions, so the store is read at the moment
    of use: a universe resolved between one connection and the next is visible to the
    second without rebuilding the provider, and a provider built for a workspace that has
    resolved nothing yet is empty rather than wrong.

    Loading everything on initialisation is the default, and is stated rather than left to
    the engine's own default of loading nothing: a caller that asks a provider to
    initialise and is handed an empty one has to find that out from a warning in a log. The
    whole of what is loaded is a local store read, so there is nothing to be careful of.
    """

    def __init__(self, ws: Workspace, config: Any = None) -> None:
        super().__init__(config=config or InstrumentProviderConfig(load_all=True))
        self._ws = ws

    @property
    def workspace(self) -> Workspace:
        """The workspace whose store this provider serves."""
        return self._ws

    def available(self) -> dict[InstrumentId, Any]:
        """Every instrument this broker could route for, read from the store now."""
        return served(read_store(self._ws).values())

    def find(self, instrument_id: InstrumentId) -> Any:
        """One definition, loading it from the store if this provider has not got it yet.

        The base class answers only out of what has been loaded, which would make a lookup
        before the first load report an instrument the workspace holds as one it has not
        got. Read once and then held: a definition is dated, and the date does not move
        under a running stage, so re-reading the store per bar would be a parquet read per
        point to answer a question with one answer.
        """
        found = super().find(instrument_id)
        if found is None:
            found = self.available().get(instrument_id)
            if found is not None:
                self.add(found)
        return found

    def unserved(self, instrument_ids: Sequence[InstrumentId]) -> tuple[InstrumentId, ...]:
        """Those of `instrument_ids` this broker has no definition it can route for.

        What a caller needs to refuse a subscription by name: an instrument that is not
        here is one whose venue this broker does not trade, or one the workspace has never
        resolved, and both are worth saying out loud rather than never delivering a bar for.
        """
        held = self.available()
        return tuple(found for found in instrument_ids if found not in held)

    async def load_all_async(self, filters: dict[Any, Any] | None = None) -> None:
        """Load every instrument this broker can route for. No filter is offered.

        A filter here would be a vendor's query language, and there is no vendor in this
        path: the store is local, the whole of it is cheap to read, and a caller that wants
        a subset asks for it by id.
        """
        self.add_bulk(list(self.available().values()))

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict[Any, Any] | None = None,
    ) -> None:
        """Load exactly the instruments named, and pass over the ones not served.

        Overridden rather than inherited: the base class loads everything and then filters,
        which for a store of a thousand definitions is a thousand reads to answer one id.
        An id with no definition is left out, and `unserved` is how a caller learns which.
        """
        held = self.available()
        self.add_bulk([held[found] for found in instrument_ids if found in held])


def provider(ws: Workspace) -> AlpacaInstrumentProvider:
    """This broker's instrument provider for one workspace.

    Built without reading the store, resolving a credential or opening a socket, so
    listing what a workspace can deploy to costs nothing.
    """
    return AlpacaInstrumentProvider(ws)
