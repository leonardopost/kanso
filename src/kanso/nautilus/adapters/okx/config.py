"""Which account a credential opens, which host serves it, and what `[adapters.okx]` holds.

**Two accounts, two sets of names.** The exchange runs a demo-trading environment beside
the real one, and a key is created in one of them. The execution client `okx_demo` trades
the demo account and resolves `KANSO_OKX_DEMO_API_KEY`, `KANSO_OKX_DEMO_API_SECRET` and
`KANSO_OKX_DEMO_PASSPHRASE`; the real-capital client `okx` resolves `KANSO_OKX_API_KEY`,
`KANSO_OKX_API_SECRET` and `KANSO_OKX_PASSPHRASE`. All six are derived from the client id by
`kanso.creds.standard_name`, and they are the only spellings this package knows.

**The region is declared, never defaulted.** The exchange serves accounts from separate
regional hosts, and a key is accepted only by the host of the entity its account belongs to.
Measured on 2026-09-30 with a signed `GET /api/v5/account/config` from the operator's
machine: an Australian retail account's key was accepted by `us.okx.com` alone, and
`www.okx.com`, `my.okx.com` and `eea.okx.com` each answered "API key doesn't exist". The
engine's region `us` is the one that maps to `us.okx.com`, so an Australian account states
`region = "us"`. The engine defaults an unstated region to `global` (`www.okx.com`), which
for that account is a refusal that reads like a revoked key — so kanso gives the region no
default, and a client (a later change) refuses to open until the table states one. No
engine region maps to `app.okx.com` or `my.okx.com`; an account served only there has no
region to state, and `docs/backlog.md` records it.

**The quota is kanso's own requests', not the engine's clients'.** `rate_per_second` is the
single flat rate kanso's own public requests share — the reference provider's today, and the
public-history loaders' when they land. The engine's own HTTP client meters itself — its
compiled module carries a global bucket `okx:global` and one bucket per endpoint,
`okx:/api/v5/market/history-candles` among them — and takes no quota from its caller, so
this key governs nothing the engine sends. Five a second is a deliberately conservative
default rather than a measured ceiling: the instruments endpoint is asked one id per
request, and the history endpoints' own limits are measured when the loaders that call them
land.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`nautilus_pyo3.OKXRegion` has exactly three members, whose runtime names are `global`,
`eea` and `us` (`OKXRegion.variants()`), and `OKXRegion.from_str` parses any case of them.
`nautilus_pyo3.get_okx_http_base_url(region)` maps them to `https://www.okx.com`,
`https://eea.okx.com` and `https://us.okx.com`, and maps `None` to the first; the
`OKXDataClientConfig` and `OKXExecClientConfig` `region` fields default to `None`, and the
engine's factories read that as `OKXRegion.GLOBAL`. The websocket hosts follow the region
and the environment: `ws.okx.com`, `wseea.okx.com` and `wsus.okx.com` for `LIVE`, and
`wspap.okx.com`, `wseeapap.okx.com` and `wsuspap.okx.com` for `DEMO`
(`get_okx_ws_url_public`/`_private`, port 8443, path `/ws/v5/public` or `/ws/v5/private`).
Only the `us` REST host was measured against an account; the rest are the engine's.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import Field, field_validator

from kanso import creds
from kanso.errors import PreconditionError
from kanso.schemas import ExecutionClientSpec
from kanso.schemas.base import KansoModel

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = [
    "CLIENTS",
    "DEFAULT_RATE_PER_SECOND",
    "DEMO",
    "DEMO_CLIENT",
    "ID",
    "LIVE",
    "LIVE_CLIENT",
    "PASSPHRASE_PURPOSE",
    "REGION_HOSTS",
    "SECRET_PURPOSE",
    "OkxConfig",
    "Region",
    "credential_names",
    "spec",
    "table",
]

ID: Final = "okx"
"""The id this package is registered and configured under: `[adapters.okx]`. The broker and
the public reference answer to the same id, because they are one party with one table."""

DEMO_CLIENT: Final = "okx_demo"
LIVE_CLIENT: Final = "okx"
"""The two client ids. Each is the subject of its own credential names as well as the id a
stage names, so a variable and the account it opens cannot drift apart."""

DEMO: Final = ExecutionClientSpec(id=DEMO_CLIENT, capital="broker_paper", clock="wall")
"""The demo-trading account: the exchange's own order handling, simulated money."""

LIVE: Final = ExecutionClientSpec(id=LIVE_CLIENT, capital="real", clock="wall")
"""The real account. `capital: real` confines it to the live stage and puts a recorded,
named approval between a certified version and this client."""

CLIENTS: Final[tuple[str, ...]] = (DEMO_CLIENT, LIVE_CLIENT)
"""Every client id this package serves, demo first, in the order they are reported."""

SECRET_PURPOSE: Final = "API_SECRET"
PASSPHRASE_PURPOSE: Final = "PASSPHRASE"
"""The second and third credentials' purposes under the standard scheme; the key takes the
default. The exchange signs every private request with all three."""

DEFAULT_RATE_PER_SECOND: Final = 5
"""The default quota of kanso's own public requests: conservative, not measured;
`rate_per_second` overrides it."""


class Region(StrEnum):
    """The engine's `OKXRegion`, by its runtime names; which regional host serves a key."""

    GLOBAL = "global"
    EEA = "eea"
    US = "us"


REGION_HOSTS: Final[dict[Region, str]] = {
    Region.GLOBAL: "https://www.okx.com",
    Region.EEA: "https://eea.okx.com",
    Region.US: "https://us.okx.com",
}
"""The REST host the engine maps each region to — read from the engine, and re-checked
against it by `kanso doctor` — so a region an operator states is the host a key reaches."""


def spec(client_id: str) -> ExecutionClientSpec:
    """The declaration of `client_id`, or a refusal naming the ids there are."""
    for one in (DEMO, LIVE):
        if one.id == client_id:
            return one
    raise PreconditionError(
        f"okx: {client_id!r} is not a client this broker provides; it provides "
        f"{', '.join(CLIENTS)}",
        remedy=f"name {DEMO_CLIENT} for the demo account or {LIVE_CLIENT} for the real one",
    )


def credential_names(client_id: str) -> tuple[str, str, str]:
    """The key, secret and passphrase variable names of one client, derived not spelled."""
    subject = spec(client_id).id
    return (
        creds.standard_name(subject),
        creds.standard_name(subject, SECRET_PURPOSE),
        creds.standard_name(subject, PASSPHRASE_PURPOSE),
    )


class OkxConfig(KansoModel):
    """The `[adapters.okx]` table: which regional host serves the account, and the quota.

    It holds no credential — those are resolved from the environment at the moment of use —
    so it is safe to print, record and commit, and an unknown key (an `api_key` pasted in,
    say) is refused rather than ignored.
    """

    region: Region | None = None
    rate_per_second: int = Field(default=DEFAULT_RATE_PER_SECOND, ge=1, le=1_000)

    @field_validator("region", mode="before")
    @classmethod
    def _engine_region(cls, value: object) -> object:
        """Any case of an engine region, as the engine parses one; anything else by name."""
        if value is None:
            return None
        text = str(value).strip().lower()
        if text not in {region.value for region in Region}:
            raise ValueError(
                f"{value!r} is not a region the engine serves; it serves "
                f"{', '.join(region.value for region in Region)}"
            )
        return text

    def require_region(self) -> Region:
        """The declared region, refusing to guess one.

        No default, because a key is accepted by one regional host only, and the engine's
        own default sends it to the global host — where an account served elsewhere is told
        its key does not exist.
        """
        if self.region is None:
            raise PreconditionError(
                "[adapters.okx] region: no region is declared, and there is no default; a key "
                "is accepted only by its own account's regional host "
                f"({', '.join(f'{r.value} = {host}' for r, host in REGION_HOSTS.items())})",
                remedy='add `region = "us"` (or "global", "eea") to the [adapters.okx] table '
                "in kanso.toml, naming the host that accepts the account's key",
            )
        return self.region


def table(ws: Workspace) -> OkxConfig:
    """The workspace's `[adapters.okx]` table, validated; its defaults when it is absent."""
    return OkxConfig.model_validate(ws.config.adapters.get(ID, {}))
