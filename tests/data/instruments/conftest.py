"""Fixtures for resolution: a real workspace, entries for each instrument class, a probe.

Everything here is vendor-free. The entries are `manual`, which is the path a workspace
with no credentials takes, and the only reference provider is a probe registered by a test,
so nothing reaches a network and the suite is green with every vendor variable unset.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from kanso.data.instruments import ResolveError
from kanso.schemas import InstrumentsFile, write_yaml
from kanso.workspace import Workspace, init

if TYPE_CHECKING:
    from collections.abc import Sequence

AS_OF = date(2024, 6, 3)
"""The date every fixture resolves as of; inside every dated entry's listing window."""

ACTIVATION = "2024-01-02"
EXPIRATION = "2024-12-20"

EQUITY: dict[str, Any] = {
    "nautilus_id": "AAPL.XNAS",
    "asset_class": "EQUITY",
    "manual": True,
    "corporate_actions": "adjust_all",
    "override": {"currency": "USD"},
}
"""An equity takes its tick and lot from the convention table, so it declares neither."""

INDEX: dict[str, Any] = {
    "nautilus_id": "SPX.XCBO",
    "asset_class": "INDEX",
    "manual": True,
    "corporate_actions": "none",
    "override": {"currency": "USD", "price_increment": "0.01", "size_increment": "1"},
}

CURRENCY_PAIR: dict[str, Any] = {
    "nautilus_id": "EUR/USD.SIM",
    "asset_class": "FX",
    "manual": True,
    "corporate_actions": "none",
    "override": {
        "base_currency": "EUR",
        "quote_currency": "USD",
        "price_increment": "0.00001",
        "size_increment": "1",
    },
}

OPTION: dict[str, Any] = {
    "nautilus_id": "AAPL240119C00150000.OPRA",
    "asset_class": "EQUITY",
    "manual": True,
    "corporate_actions": "none",
    "override": {
        "instrument_class": "option",
        "currency": "USD",
        "multiplier": "100",
        "underlying": "AAPL",
        "option_kind": "call",
        "strike_price": "150.00",
        "activation_ns": ACTIVATION,
        "expiration_ns": EXPIRATION,
    },
}

FUTURE: dict[str, Any] = {
    "nautilus_id": "ESZ4.XCME",
    "asset_class": "INDEX",
    "manual": True,
    "corporate_actions": "none",
    "override": {
        "instrument_class": "future",
        "currency": "USD",
        "price_increment": "0.25",
        "multiplier": "50",
        "lot_size": "1",
        "underlying": "ES",
        "activation_ns": ACTIVATION,
        "expiration_ns": EXPIRATION,
    },
}

CLASSES: dict[str, dict[str, Any]] = {
    "Equity": EQUITY,
    "IndexInstrument": INDEX,
    "CurrencyPair": CURRENCY_PAIR,
    "OptionContract": OPTION,
    "FuturesContract": FUTURE,
}
"""One entry per instrument class kanso resolves, keyed by the class it must build."""


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A scaffolded workspace, with the empty instruments file `init` writes."""
    return init(tmp_path / "ws")


def entries(**named: dict[str, Any]) -> InstrumentsFile:
    return InstrumentsFile(named)


def write(ws: Workspace, **named: dict[str, Any]) -> InstrumentsFile:
    """Replace the workspace's instruments file with these entries."""
    file = entries(**named)
    write_yaml(file, ws.path("instruments.yaml"))
    return file


def reading(ws: Workspace, reference: str) -> Workspace:
    """The same workspace, configured to resolve through `reference`."""
    return replace(ws, config=ws.config.model_copy(update={"data": _data(ws, reference)}))


def _data(ws: Workspace, reference: str) -> Any:
    return ws.config.data.model_copy(update={"reference": reference})


@dataclass
class Probe:
    """A reference provider that answers from a table and records what it was asked.

    It stands in for an adapter without being one: no credential, no network, no vendor.
    """

    id: ClassVar[str] = "probe"
    answers: dict[str, object] = field(default_factory=dict)
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def resolve(self, ids: Sequence[str], as_of: date) -> dict[str, object]:
        self.asked.append(tuple(ids))
        return {
            wanted: self.answers.get(wanted, ResolveError(wanted, "unknown: the probe knows none"))
            for wanted in ids
        }

    def sources(self, instrument_id: str) -> dict[str, str]:
        return {"probe": instrument_id.lower()}
