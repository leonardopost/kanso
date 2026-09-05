"""The Massive data adapter: what it offers, what it needs, and how it is opened.

This package is the whole of what kanso knows about Massive. Nothing outside it names the
vendor, nothing in the framework requires it, and every command works with all three of
its variables unset — the adapter is enabled by the presence of its credentials, never by
installation.

**What is declared and what is probed.** `capabilities` declares the *offer*: the classes
this adapter can serve, the datasets it can serve for each, the grain the source gates
entitlement at, and that a bulk transport exists. It does not declare entitlement or a
history floor, because neither is a property of the adapter: both belong to the operator's
plan on the day they are asked, and both are measured by `entitlement`. Declaring them
would put a stale constant where a measurement belongs, and the one wrong answer this
adapter must never give — "you are not entitled" when the truth is "that is older than
the source holds" — is exactly what such a constant produces.

**Three credentials, resolved independently.** The REST key and the object store's access
key and secret are three separate names under the standard scheme, each resolved at the
moment of use. That two of them happen to carry the same value under today's plan is a
coincidence of the vendor's provisioning and is nowhere relied on: an operator whose plan
issues distinct keys, or who rotates one of them, must not have to discover that kanso
assumed otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from kanso import creds
from kanso.data.adapters.massive.client import (
    MassiveClient,
    MassiveConfig,
    Signal,
    Transport,
    pyo3_transport,
)
from kanso.data.adapters.massive.entitlement import REFERENCE

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from datetime import date

    from kanso.data.instruments import InstrumentProvider
    from kanso.data.loader import Loader
    from kanso.data.registry import Survey
    from kanso.workspace import Workspace

__all__ = [
    "ACCESS_KEY_ID",
    "ADAPTER",
    "API_KEY",
    "CAPABILITIES",
    "CREDENTIALS",
    "ID",
    "KIND",
    "Capabilities",
    "Check",
    "ClassCapability",
    "MassiveAdapter",
    "MassiveClient",
    "MassiveConfig",
    "SECRET_KEY",
]

ID: Final = "massive"
"""The id this adapter is registered and configured under: `[adapters.massive]`."""

KIND: Final = "data"
"""It serves data and reference definitions; it executes nothing."""

API_KEY: Final = creds.standard_name(ID)
ACCESS_KEY_ID: Final = creds.standard_name(ID, "ACCESS_KEY_ID")
SECRET_KEY: Final = creds.standard_name(ID, "SECRET_KEY")
CREDENTIALS: Final[tuple[str, ...]] = (API_KEY, ACCESS_KEY_ID, SECRET_KEY)
"""The three variable names, derived from the standard scheme rather than spelled out, so
the adapter's id and the names an operator sets cannot drift apart."""

CHECK_TICKER: Final = "AAPL"
"""The key `check` looks up: a listed US equity, which every plan can resolve, so a
refusal is about the credential rather than about the ticker."""


@dataclass(frozen=True, slots=True)
class ClassCapability:
    """What this adapter offers for one asset class, entitlement aside.

    `datasets` is what it can ask for, not what the plan grants: a class whose ticks are
    excluded and whose aggregates are included is one class with two answers, and only a
    probe knows which is which. `grain` is where those answers may be reused — per
    endpoint for a class the source gates as a whole, per ticker for one it gates by the
    feed behind each ticker.
    """

    asset_class: str
    datasets: tuple[str, ...]
    grain: str

    def payload(self) -> dict[str, object]:
        return {
            "asset_class": self.asset_class,
            "datasets": list(self.datasets),
            "grain": self.grain,
            "entitlement": "probed",
            "history_floor": "probed",
        }


@dataclass(frozen=True, slots=True)
class Capabilities:
    """The adapter's offer: classes, datasets, grains and whether bulk history exists."""

    classes: tuple[ClassCapability, ...]
    bulk: bool = True

    def datasets(self) -> tuple[str, ...]:
        """Every dataset offered for any class, once each, in a stable order."""
        return tuple(sorted({name for item in self.classes for name in item.datasets}))

    def names(self) -> tuple[str, ...]:
        """The flat capability list `data adapters` prints beside the adapter's id."""
        return (*self.datasets(), *(("bulk",) if self.bulk else ()))

    def payload(self) -> dict[str, object]:
        return {
            "classes": [item.payload() for item in self.classes],
            "datasets": list(self.datasets()),
            "bulk": self.bulk,
            "entitlement": "probed per ticker, cached at the grain the source gates on",
            "history_floor": "probed per series, on the day it is used",
        }


CAPABILITIES: Final = Capabilities(
    classes=(
        ClassCapability(
            "stocks",
            ("reference", "bars", "trades", "quotes", "corporate_actions", "financials", "filings"),
            "endpoint",
        ),
        ClassCapability("options", ("reference", "bars", "trades", "quotes"), "endpoint"),
        ClassCapability("futures", ("reference", "bars"), "endpoint"),
        ClassCapability("forex", ("reference", "bars", "quotes"), "endpoint"),
        ClassCapability("indices", ("reference", "bars"), "ticker"),
    )
)
"""What this adapter can ask for. Indices are the class whose entitlement is decided per
ticker, because the source gates them by the feed behind each ticker rather than by class."""


@dataclass(frozen=True, slots=True)
class Check:
    """The result of one minimal authenticated request, for `data adapters --check`."""

    ok: bool
    path: str
    status: int
    signal: Signal
    detail: str

    def payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "path": self.path,
            "status": self.status,
            "signal": str(self.signal),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class MassiveAdapter:
    """The registry's entry point for Massive: identity, offer, credentials, a client.

    Everything here that touches a credential resolves it at the moment of use and
    returns none of it: `credential_origins` reports where a variable resolved from and
    never what it holds, which is what `doctor` prints.
    """

    id: str = ID
    kind: str = KIND
    capabilities: Capabilities = CAPABILITIES
    credentials: tuple[str, ...] = CREDENTIALS

    def config(self, ws: Workspace) -> MassiveConfig:
        """The `[adapters.massive]` table, validated by this adapter's own model."""
        return MassiveConfig.model_validate(ws.config.adapters.get(self.id, {}))

    def client(self, ws: Workspace, *, transport: Transport | None = None) -> MassiveClient:
        """A client for this workspace, or a precondition failure naming the variable.

        The REST key is the only credential a request path needs; the object store's two
        are resolved separately, by the code that signs for it. `transport` is how the
        suite replays frozen bodies: nothing else may pass one.
        """
        settings = self.config(ws)
        return MassiveClient(
            creds.require(API_KEY, ws.root),
            transport=transport
            or pyo3_transport(
                requests_per_second=settings.requests_per_second, timeout_s=settings.timeout_s
            ),
            base_url=settings.base_url,
            requests_per_second=settings.requests_per_second,
        )

    def quota(self, ws: Workspace) -> str:
        """The rate limit this workspace would enforce, reported without a credential.

        `data adapters` describes an adapter it cannot open, so the quota is read from
        the configuration rather than from a client.
        """
        return f"{self.config(ws).requests_per_second}/s"

    def object_store_keys(self, ws: Workspace) -> tuple[str, str]:
        """The bulk transport's access key id and secret, each resolved on its own.

        Never derived from the REST key, and never from each other: they are three names
        under the standard scheme and an operator may set them to three different values.
        """
        return creds.require(ACCESS_KEY_ID, ws.root), creds.require(SECRET_KEY, ws.root)

    def credential_origins(self, ws: Workspace) -> dict[str, str | None]:
        """Per variable, where it resolved from — `.env`, `environment` — or `None`."""
        return {name: creds.origin(name, ws.root) for name in self.credentials}

    def configured(self, ws: Workspace) -> bool:
        """True when the REST key resolves; the adapter needs no installation beyond it."""
        return creds.origin(API_KEY, ws.root) is not None

    def loaders(self, ws: Workspace) -> dict[str, Callable[[], Loader]]:
        """This adapter's loaders for `ws`, as a factory per id, building none of them.

        Imported here rather than at module scope because a loader imports this package
        for its capabilities and its client; deferring the import to the call keeps that
        one-way. Factories rather than instances because a loader is opened for one
        workspace, and listing what this adapter can fetch must open nothing.
        """
        from kanso.data.adapters.massive.loaders import loaders as table

        return table(ws)

    def provider(self, ws: Workspace) -> InstrumentProvider:
        """The instrument provider this adapter offers, for `[data] reference` to name."""
        from kanso.data.adapters.massive.reference import provider

        return provider(ws)

    def survey(
        self,
        ws: Workspace,
        *,
        transport: Transport | None = None,
        as_of: date | None = None,
    ) -> Survey:
        """Probe what this workspace's key actually reaches, dataset by dataset.

        The offer is declared and the reach is measured, and they are different questions:
        `capabilities` says what this adapter can ask for, and this says what came back
        when it asked. Costs one authenticated request per dataset and one more per
        measured floor, all on one quota.
        """
        from kanso.data.adapters.massive.survey import survey

        return survey(self, ws, transport=transport, as_of=as_of)

    def check(self, ws: Workspace, *, transport: Transport | None = None) -> Check:
        """One authenticated request: does this credential reach the vendor at all?

        A reference lookup of a listed equity, which every plan resolves, so the answer
        is about the credential. It says nothing about entitlement for any other class —
        that is `entitlement`'s to establish, per series and at the source's own grain.
        """
        path, params = REFERENCE.request(CHECK_TICKER)
        call = self.client(ws, transport=transport).call(path, params)
        detail = {
            Signal.ROWS: "the key authenticates and the vendor answered",
            Signal.NO_ROWS: "the key authenticates and the vendor knows no such key",
            Signal.REFUSED: "the vendor refused the key",
            Signal.BAD_REQUEST: "the vendor rejected the request shape",
            Signal.THROTTLED: "the vendor throttled the request",
            Signal.UNAVAILABLE: "the vendor did not answer",
            Signal.UNREADABLE: "the vendor's answer could not be read",
        }[call.signal]
        return Check(
            ok=call.signal is Signal.ROWS,
            path=call.path,
            status=call.status,
            signal=call.signal,
            detail=detail,
        )


ADAPTER: Final = MassiveAdapter()
"""The registered instance. Building one costs nothing and touches no credential."""
