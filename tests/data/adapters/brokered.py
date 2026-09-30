"""A broker package that exposes a data adapter, made for the length of one test.

A broker's package may carry that party's public-history loaders and reference provider,
exposed as a module-level `ADAPTER` beside its `BROKER`. No shipped broker does yet, so the
registry's discovery of one is tested against a package written here: a directory in the
test's own temporary tree, appended to `kanso.nautilus.adapters.__path__` and entered in
`sys.modules`, both undone when the test ends. Nothing under `src/` is written.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

BROKERS = "kanso.nautilus.adapters"

SOURCE = """
class Capabilities:
    def names(self):
        return ("bars",)

    def payload(self):
        return {{"datasets": ["bars"]}}


class Provider:
    id = "{adapter_id}"


class Loader:
    id = "{adapter_id}_bars"


class Adapter:
    id = "{adapter_id}"
    kind = "{kind}"
    capabilities = Capabilities()
    credentials = ()

    def client(self, ws):
        raise RuntimeError("not opened by this test")

    def configured(self, ws):
        return True

    def credential_origins(self, ws):
        return {{}}

    def quota(self, ws):
        return "5/s"

    def loaders(self, ws):
        return {{"{adapter_id}_bars": Loader}}

    def provider(self, ws):
        return Provider()

    def survey(self, ws):
        raise RuntimeError("not probed by this test")


ADAPTER = Adapter()
"""


def expose(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    name: str,
    adapter_id: str,
    kind: str = "data",
) -> ModuleType:
    """Make `name` a broker package exposing an `ADAPTER` with `adapter_id`, until teardown."""
    directory = root / name
    directory.mkdir(parents=True)
    init = directory / "__init__.py"
    init.write_text(SOURCE.format(adapter_id=adapter_id, kind=kind), encoding="utf-8")
    package = importlib.import_module(BROKERS)
    monkeypatch.setattr(package, "__path__", [*package.__path__, str(root)])
    dotted = f"{BROKERS}.{name}"
    spec = importlib.util.spec_from_file_location(dotted, init)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, dotted, module)
    return module
