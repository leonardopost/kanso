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

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory

from kanso.errors import PreconditionError
from kanso.nautilus.adapters.alpaca import BROKER
from kanso.nautilus.adapters.alpaca.config import PAPER_CLIENT, account
from kanso.nautilus.adapters.alpaca.execution import AlpacaExecutionClient, sender
from kanso.workspace import find

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import asyncio

    from nautilus_trader.live.execution_client import LiveExecutionClient

__all__ = [
    "DATA_CLIENT_FACTORY",
    "DATA_FACTORY_ATTR",
    "EXEC_CLIENT_FACTORY",
    "AlpacaDataClientConfig",
    "AlpacaExecClientConfig",
    "AlpacaLiveDataClientFactory",
    "AlpacaLiveExecClientFactory",
]

DATA_FACTORY_ATTR: Final = "DATA_CLIENT_FACTORY"
"""What this adapter's market data module exposes its own factory under."""

DATA_MODULE: Final = "data"
"""The module the live market data client lives in, imported when one is asked for."""


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


class AlpacaLiveDataClientFactory(LiveDataClientFactory):
    """Builds this broker's live market data client, which lives beside this module.

    A thin delegation rather than an import, so that listing what a workspace can deploy
    to — which reaches this module for the execution factory — never builds the market
    data client, and so that a fault in one of the two clients is not a fault in both.
    """

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveDataClientConfig,
        msgbus: Any,
        cache: Any,
        clock: Any,
    ) -> Any:
        """The market data client this broker's own data module builds."""
        return _data_factory().create(
            loop=loop,
            name=name,
            config=config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )


def _data_factory() -> Any:
    """This adapter's market data factory, imported when a client is asked for."""
    module = importlib.import_module(f"{__package__}.{DATA_MODULE}")
    found = getattr(module, DATA_FACTORY_ATTR, None)
    if found is None:
        raise PreconditionError(
            f"alpaca: this adapter's {DATA_MODULE} module declares no {DATA_FACTORY_ATTR}, "
            "so it offers no live market data client",
            remedy=(
                f"give the stage a data client another adapter provides, or pair "
                f"{PAPER_CLIENT!r} with the catalog replay client"
            ),
        )
    return found


EXEC_CLIENT_FACTORY: Final = AlpacaLiveExecClientFactory
DATA_CLIENT_FACTORY: Final = AlpacaLiveDataClientFactory
"""The two names the adapter's registry reaches these factories through."""
