"""The OKX broker: what it offers, what it declares, and the public reference it reads.

This package is the whole of what kanso knows about this exchange: two execution clients,
one live data client id, six credential names, the `[adapters.okx]` table, the venue model
of perpetual swaps on `OKX`, and — as `ADAPTER`, beside the `BROKER` — the exchange's public
reference, which resolves a listed swap into an instrument with no credential at all
(`reference.py`), and loads its public history — bars, trade prints and realised funding —
into the catalog the same way (`history.py`). It builds no execution client yet; those
arrive in a later change and build on what is declared here. Nothing outside this package
names the exchange, its venue code, its hosts or its instrument grammar, and every command
works with all six variables unset — the broker is enabled by its credentials, never by
installation, and the public reference by its table.

**Two execution clients, because there are two accounts.** `okx_demo` declares
`capital: broker_paper` and `okx` declares `capital: real`, and both declare `clock: wall`.
Those declarations are the safety property: a real-capital client may be configured only
on the live stage and reaches a version only through `promote --live --as NAME`, and a
wall-clock client fills against current prices, so a stage node that is a bounded replay of
the catalog refuses both at deploy (`docs/backlog.md`, the long-running stage node) exactly
as it refuses every other broker's.

**Six credential names, resolved independently.** A key, a secret and a passphrase per
account, each derived by `kanso.creds.standard_name` from the client id it opens. The
package knows those spellings and no others.

**The engine never receives a `None` credential.** Measured on the installed engine
(`facts.py`, re-checked by `kanso doctor`): the engine's OKX HTTP client and its
credentialed stream read `OKX_API_KEY`, `OKX_API_SECRET` and `OKX_API_PASSPHRASE` from the
process environment for any credential they are handed as `None`. That is a fallback to a
name kanso does not own, and a fallback is how a key exported for one tool ends up trading
through another. So the client factory this package will gain follows one rule: it resolves
all three of the client's own `KANSO_OKX_*` names, refuses when any is unset, passes every
one of them to the engine explicitly, and never calls a `from_env` constructor, which reads
those same three variables and, for a stream, its url from `OKX_WS_URL`.

**The engine never chooses the stream's url either.** Also measured, and re-checked by
`doctor`: `OKXWebSocketClient.with_credentials` handed `url=None` connects to
`wss://ws.okx.com:8443/ws/v5/public`, the global host's public stream, whatever the
credentials and whatever the region. So the factory passes the private url
`get_okx_ws_url_private` gives for the account's environment (`DEMO` for `okx_demo`) and
the region `[adapters.okx]` declares, and the business url `derive_okx_ws_url` derives
from it, exactly as the engine's own execution client does, and never leaves either to
the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from kanso import creds
from kanso.nautilus.adapters.okx import facts
from kanso.nautilus.adapters.okx.config import (
    CLIENTS,
    DEMO,
    ID,
    LIVE,
    LIVE_CLIENT,
    OkxConfig,
    Region,
    credential_names,
    table,
)
from kanso.nautilus.adapters.okx.reference import ADAPTER
from kanso.nautilus.adapters.okx.venue import declaration

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.nautilus.adapters import EngineClaim
    from kanso.schemas import ExecutionClientSpec, VenueDeclaration
    from kanso.workspace import Workspace

__all__ = [
    "ADAPTER",
    "BROKER",
    "CREDENTIALS",
    "DATA_CLIENTS",
    "EXEC_CLIENTS",
    "ID",
    "KIND",
    "OkxBroker",
    "OkxConfig",
    "Region",
]

KIND: Final = "execution"
"""It will execute orders and serve the live feed that goes with them."""

EXEC_CLIENTS: Final[tuple[ExecutionClientSpec, ...]] = (DEMO, LIVE)
"""The two execution clients, demo first."""

DATA_CLIENTS: Final[tuple[str, ...]] = (LIVE_CLIENT,)
"""The live data client id: the exchange's market data is public and one feed serves both
accounts, so one id offers it."""

CREDENTIALS: Final[dict[str, tuple[str, str, str]]] = {
    client: credential_names(client) for client in CLIENTS
}
"""Per client id, the key, secret and passphrase variable names, derived from the standard
scheme rather than spelled out."""


@dataclass(frozen=True, slots=True)
class OkxBroker:
    """The registry's entry point for this broker: identity, declarations, credentials.

    A credential is resolved at the moment of use and none is returned: `credential_origins`
    says where a variable resolved from and never what it holds, which is what `doctor` and
    `portfolio clients` print.
    """

    id: str = ID
    kind: str = KIND
    exec_clients: tuple[ExecutionClientSpec, ...] = EXEC_CLIENTS
    data_clients: tuple[str, ...] = DATA_CLIENTS
    engine_facts: tuple[EngineClaim, ...] = facts.CLAIMS

    def config(self, ws: Workspace) -> OkxConfig:
        """The `[adapters.okx]` table, validated by this adapter's own model."""
        return table(ws)

    def credentials(self, client_id: str) -> tuple[str, ...]:
        """The variable names one client resolves, refusing an id this broker has not got."""
        return credential_names(client_id)

    def credential_origins(self, ws: Workspace, client_id: str) -> dict[str, str | None]:
        """Per variable, where it resolved from — `.env`, `environment` — or `None`."""
        return {name: creds.origin(name, ws.root) for name in self.credentials(client_id)}

    def configured(self, ws: Workspace, client_id: str) -> bool:
        """Whether all three of one client's variables resolve; the exchange signs with all."""
        return all(value is not None for value in self.credential_origins(ws, client_id).values())

    def venue_declaration(self, venue: str) -> VenueDeclaration | None:
        """What this broker declares about a venue it serves, or `None` for one it does not."""
        return declaration(venue)


BROKER: Final = OkxBroker()
"""The registered instance. Building one costs nothing and touches no credential."""
