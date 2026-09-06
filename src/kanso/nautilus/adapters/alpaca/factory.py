"""How the engine builds this broker's clients, and what it must be told to build one.

A node knows an execution client by an id in `portfolio.yaml` and by nothing else. This
module is the join between that id and a running client: the engine asks a factory for a
client by name, and the factory resolves the workspace, the account's credentials, the
host that account may address and the one shared rate-limited connection, and hands back
a client already paired with the account it names.

**The account is chosen by the id, not by a flag.** `alpaca_paper` and `alpaca` are two
separate accounts with two separate pairs of credential variables and two separate hosts,
and the id decides all three. There is no switch anywhere in this adapter that turns a
paper client into a real one; a stage that trades real money says so by naming the client
that declares `capital: real`, which the core will accept only on the live stage and only
behind a recorded, named approval.

**Credentials are resolved here, at the moment the client is opened, and nowhere else.**
The configuration carries the workspace's path and the client's id — never a value — so
it is safe to print, to record and to commit, which matters because a node configuration
is written into a session. The workspace is re-opened from its path because an engine
configuration must survive being serialised, and a `Workspace` is not.

**One connection for the whole adapter.** The execution client, the live data client and
the tradability overlay share the transport this broker builds for a workspace, because
the published rate limit belongs to the account rather than to any one caller: three
connections would be three times the limit.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`LiveExecClientFactory.create(loop, name, config, msgbus, cache, clock)` and
`LiveDataClientFactory.create(...)` are static methods, and `TradingNode`'s builder
registers a factory by name, looks the name up from the `exec_clients` and `data_clients`
configuration keys, and checks the factory is a subclass of the base before calling it.
`LiveExecClientConfig` and `LiveDataClientConfig` are frozen msgspec structs, so an
adapter's own configuration is a subclass carrying only serialisable fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig
from nautilus_trader.live.factories import LiveExecClientFactory

from kanso.errors import PreconditionError
from kanso.nautilus.adapters.alpaca import BROKER
from kanso.nautilus.adapters.alpaca.config import account
from kanso.nautilus.adapters.alpaca.execution import AlpacaExecutionClient, sender
from kanso.workspace import find

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import asyncio

    from nautilus_trader.live.execution_client import LiveExecutionClient

__all__ = [
    "EXEC_CLIENT_FACTORY",
    "AlpacaDataClientConfig",
    "AlpacaExecClientConfig",
    "AlpacaLiveExecClientFactory",
]


class AlpacaExecClientConfig(LiveExecClientConfig, frozen=True):
    """What the engine must be told to build one of this broker's execution clients.

    `client_id` is the kanso client id — which account is traded, which variables are
    read and which host is addressed, all three at once. `workspace` is where the
    workspace holding those variables lives. Neither is a credential, and no field here
    ever holds one: this object reaches a node's configuration and a recorded session.
    """

    client_id: str = ""
    workspace: str = "."


class AlpacaDataClientConfig(LiveDataClientConfig, frozen=True):
    """The same two facts for a live market data client of this broker."""

    client_id: str = ""
    workspace: str = "."


class AlpacaLiveExecClientFactory(LiveExecClientFactory):
    """Builds one execution client for the account the configuration names."""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveExecClientConfig,
        msgbus: Any,
        cache: Any,
        clock: Any,
    ) -> LiveExecutionClient:
        """One execution client, paired with the account its id names.

        The id comes from the configuration when it states one and from the name the node
        registered the client under otherwise, so a stage naming `alpaca_paper` opens the
        paper account without restating it. An id this broker does not provide is refused
        by name before any credential is read.
        """
        if not isinstance(config, AlpacaExecClientConfig):
            raise PreconditionError(
                f"alpaca: {type(config).__name__} is not this adapter's execution client "
                "configuration",
                remedy=f"configure the {name!r} client with an AlpacaExecClientConfig",
            )
        client_id = account(config.client_id or name).client_id
        workspace = find(Path(config.workspace))
        return AlpacaExecutionClient(
            loop,
            client_id=client_id,
            credentials=BROKER.open(workspace, client_id),
            send=sender(BROKER.transport(workspace)),
            host=BROKER.config(workspace).host(client_id),
            instrument_provider=BROKER.provider(workspace),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )


EXEC_CLIENT_FACTORY: Final = AlpacaLiveExecClientFactory
"""The name the adapter's registry reaches this factory through. The market data factory
lives beside its client in `data`, under the same convention."""
