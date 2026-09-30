"""The data registry reaches a data adapter wherever its party's one package lives.

One package per outside party: a pure data vendor's under `data/adapters/`, a broker's under
`nautilus/adapters/`, and a broker's package may also carry that party's public-history
loaders and reference provider as an `ADAPTER`. `registry.adapters()` finds those as well —
after the packaged data adapters and before an extension's — while `registry.packaged()`
stays the data adapter directory and nothing else, which is what both isolation equalities
read.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from kanso.data import registry
from kanso.ext import Extension
from kanso.nautilus import adapters as brokers
from kanso.workspace import Workspace

from .brokered import expose

WS = cast("Workspace", object())
"""The stand-in adapter reads nothing from the workspace it is handed."""


def test_no_shipped_broker_exposes_a_data_adapter_yet() -> None:
    """Every adapter the registry reaches today is a packaged data adapter."""
    assert registry.adapters() == registry.packaged()


def test_a_broker_package_s_adapter_is_reached_and_is_not_packaged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shipped = set(brokers.packaged())
    module = expose(monkeypatch, tmp_path, "tidebroker", "tide")

    found = registry.adapters()

    assert found["tide"] is module.ADAPTER
    assert "tide" not in registry.packaged()
    assert set(found) == set(registry.packaged()) | {"tide"}
    assert set(brokers.packaged()) == shipped


def test_a_broker_side_adapter_s_provider_and_loaders_are_reached_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expose(monkeypatch, tmp_path, "tidebroker", "tide")

    provider = registry.provider_for(WS, "tide")

    assert provider is not None
    assert getattr(provider, "id", None) == "tide"
    assert "tide_bars" in registry.adapter_loaders(WS)


def test_a_packaged_data_adapter_wins_a_clash_with_a_broker_side_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    packaged_id, packaged_adapter = next(iter(sorted(registry.packaged().items())))
    expose(monkeypatch, tmp_path, "tidebroker", packaged_id)

    assert registry.adapters()[packaged_id] is packaged_adapter


def test_a_broker_side_adapter_wins_a_clash_with_an_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An extension may add to what ships and may not replace it, wherever it ships from."""
    module = expose(monkeypatch, tmp_path / "brokers", "tidebroker", "tide")
    impostor = type(module.ADAPTER)()
    extension = Extension(
        name="mine",
        path=tmp_path / "mine",
        module=type("Module", (), {"ADAPTERS": {"tide": impostor}})(),
        provides={"adapters": ("tide",)},
    )

    assert registry.adapters([extension])["tide"] is module.ADAPTER


def test_a_broker_module_without_an_adapter_is_passed_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broker package with only a `BROKER` contributes nothing to the data registry."""
    module = expose(monkeypatch, tmp_path, "tidebroker", "tide")
    monkeypatch.delattr(module, "ADAPTER")

    assert registry.adapters() == registry.packaged()
