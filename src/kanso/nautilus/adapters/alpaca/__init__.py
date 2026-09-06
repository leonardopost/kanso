"""The Alpaca execution and live market data adapter: what it offers and how it is opened.

This package is the whole of what kanso knows about this broker. Nothing outside it names
the broker — the one exemption being the workspace template, which renders it as an
operator's default and which the core deliberately gives no default of its own — and every
command works with all four of its variables unset. The adapter is enabled by the presence
of its credentials, never by installation.

**Two execution clients, because there are two accounts.** `alpaca_paper` declares
`capital: broker_paper` and `alpaca` declares `capital: real`, and both declare
`clock: wall`. Those declarations are the safety property, not a description of one: a
client declaring real capital may be configured only on the live stage and only reaches a
version through a recorded, named approval, and a wall-clock client is refused a
historical replay feed and any speed but real time — a broker fills against current
prices, so a fill against replayed history would bear no relation to the data that
triggered it. Replay therefore executes against the simulated venue whatever a stage is
configured with.

**Two data clients, for the same reason.** The market data host serves both environments,
but a key belongs to one account, so the live feed is offered under both client ids and
each reads its own account's credentials. A paper stage pairs `exec: alpaca_paper` with
`data: alpaca_paper` and never needs a real account's key to exist.

**Four credential names, resolved independently.** Each account has a key id and a secret
under the standard scheme, named after the client id they belong to. The adapter knows
those four spellings and no others: it never reads, mentions or falls back to whatever a
particular machine happens to export, because a fallback is how a key intended for one
tool ends up trading through another. No value reaches a log, an error, a repr, a
manifest, a session or a commit from anywhere in this package.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from kanso import creds
from kanso.nautilus.adapters.alpaca.config import (
    CLIENTS,
    LIVE,
    PAPER,
    AlpacaConfig,
    Credentials,
    Feed,
    Transport,
    account,
    credential_names,
    pyo3_transport,
    resolve,
)
from kanso.nautilus.adapters.alpaca.venue import VENUES, declaration

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import ExecutionClientSpec, VenueDeclaration
    from kanso.workspace import Workspace

__all__ = [
    "BROKER",
    "CREDENTIALS",
    "DATA_CLIENTS",
    "EXEC_CLIENTS",
    "ID",
    "KIND",
    "AlpacaBroker",
    "AlpacaConfig",
    "Feed",
]

ID: Final = "alpaca"
"""The id this adapter is registered and configured under: `[adapters.alpaca]`."""

KIND: Final = "execution"
"""It executes orders and serves the live feed that goes with them; it loads no history."""

EXEC_CLIENTS: Final[tuple[ExecutionClientSpec, ...]] = (PAPER.spec, LIVE.spec)
"""The two execution clients, paper first. Their `capital` and `clock` declarations are
what the core reasons about, and the only thing about a broker it is allowed to know."""

DATA_CLIENTS: Final[tuple[str, ...]] = CLIENTS
"""The live data client ids, one per account, because a feed is opened with an account's
own key even though both accounts read the same market data host."""

CREDENTIALS: Final[dict[str, tuple[str, str]]] = {
    client: credential_names(client) for client in CLIENTS
}
"""Per client id, the key and secret variable names, derived from the standard scheme
rather than spelled out, so an id and the variables an operator sets cannot drift apart."""


def _engine_module(name: str) -> Any:
    """One of this package's engine-facing modules, imported when it is asked for.

    By name rather than by an import statement, because each of those modules imports this
    package for its declarations: deferring the import to the call keeps that one-way, and
    keeps listing what a workspace can deploy to from building an engine client at all.
    """
    return importlib.import_module(f"{__name__}.{name}")


@dataclass(frozen=True, slots=True)
class AlpacaBroker:
    """The registry's entry point for this broker: identity, declarations, credentials.

    Everything that touches a credential resolves it at the moment of use and returns none
    of it: `credential_origins` reports where a variable resolved from and never what it
    holds, which is what `doctor` prints.
    """

    id: str = ID
    kind: str = KIND
    exec_clients: tuple[ExecutionClientSpec, ...] = EXEC_CLIENTS
    data_clients: tuple[str, ...] = DATA_CLIENTS

    def config(self, ws: Workspace) -> AlpacaConfig:
        """The `[adapters.alpaca]` table, validated by this adapter's own model."""
        return AlpacaConfig.model_validate(ws.config.adapters.get(self.id, {}))

    def credentials(self, client_id: str) -> tuple[str, ...]:
        """The variable names one client resolves, refusing an id this broker has not got."""
        return credential_names(account(client_id).client_id)

    def credential_origins(self, ws: Workspace, client_id: str) -> dict[str, str | None]:
        """Per variable, where it resolved from — `.env`, `environment` — or `None`."""
        return {name: creds.origin(name, ws.root) for name in self.credentials(client_id)}

    def configured(self, ws: Workspace, client_id: str) -> bool:
        """Whether both of one client's variables resolve. An account needs both halves."""
        return all(value is not None for value in self.credential_origins(ws, client_id).values())

    def open(self, ws: Workspace, client_id: str) -> Credentials:
        """One client's credentials, refusing a key that belongs to the other account."""
        return resolve(ws.root, client_id)

    def venue_declaration(self, venue: str) -> VenueDeclaration | None:
        """What this broker declares about a venue it serves, or `None` for one it does not."""
        return declaration(venue)

    def transport(self, ws: Workspace, *, factory: Any = None) -> Transport:
        """One rate-limited connection for this workspace, shared by everything here.

        Built once and handed to the execution client, the data client and the tradability
        overlay alike, because the quota belongs to the account rather than to any caller:
        three connections would be three times the published limit. `factory` is how the
        suite drives the plumbing without a socket; nothing else passes one.
        """
        settings = self.config(ws)
        return pyo3_transport(
            requests_per_minute=settings.requests_per_minute,
            timeout_s=settings.timeout_s,
            factory=factory,
        )

    def quota(self, ws: Workspace) -> str:
        """The rate limit this workspace would enforce, reported without a credential."""
        return self.config(ws).quota

    def describe(self, ws: Workspace) -> dict[str, object]:
        """The whole offer as one JSON object, carrying no credential and no value.

        What an operator needs to see before deploying: which client trades which money on
        which clock, which variables each of them reads and whether they resolve, which
        venues the broker declares a model for, and which tape the data client would open.
        """
        return {
            "adapter": self.id,
            "kind": self.kind,
            "exec_clients": [
                {
                    "id": spec.id,
                    "capital": spec.capital,
                    "clock": spec.clock,
                    "credentials": list(self.credentials(spec.id)),
                    "configured": self.configured(ws, spec.id),
                }
                for spec in self.exec_clients
            ],
            "data_clients": list(self.data_clients),
            "venues": sorted(VENUES),
            "feed": self.config(ws).feed,
            "quota": self.quota(ws),
        }

    def exec_client_factory(self) -> Any:
        """The engine factory that builds this broker's execution client.

        Imported when it is asked for rather than at module scope: listing what a workspace
        can deploy to must not pull in the client, and the client imports this package for
        its declarations, so deferring the import keeps that one-way.
        """
        return _engine_module("factory").EXEC_CLIENT_FACTORY

    def data_client_factory(self) -> Any:
        """The engine factory that builds this broker's live market data client."""
        return _engine_module("data").DATA_CLIENT_FACTORY

    def provider(self, ws: Workspace) -> Any:
        """The instrument provider this broker offers for the assets it trades."""
        return _engine_module("provider").provider(ws)

    def tradability(self, ws: Workspace, client_id: str | None = None) -> Any:
        """The overlay that says what this broker will actually let a strategy do.

        Read through one account's own key, because a stage trades through one account
        and a permission is a fact about that account: an overlay opened on the other
        one would describe a book nobody is trading. `None` takes this workspace's
        default account, which is what a listing that belongs to no stage wants.
        """
        return _engine_module("tradability").overlay(ws, client_id=client_id)


BROKER: Final = AlpacaBroker()
"""The registered instance. Building one costs nothing and touches no credential."""
