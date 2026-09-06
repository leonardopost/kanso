"""The core knows no broker: a source scan and an import-graph check that say so.

The same property the data adapters are held to, on the other side of the boundary. A
broker adapter is where every host, order grammar, wire format and venue map lives, and the
first module outside one that names a broker is a second place its wire has to be changed, a
second place a credential could be read, and the end of "the suite, `doctor` and the demo are
green with every broker credential unset".

Neither check names a broker either. The names are read from the adapter directory, so the
day a second one lands it is scanned without anyone remembering to add it — which is exactly
the day this would otherwise start passing by accident.

Three directories are exempt, the same three the vendor scan exempts. `templates/` is
rendered into an operator's workspace rather than imported, and is the one place a broker is
deliberately named, since `[research] broker` has to default to something while the core
deliberately defaults it to nothing; `skills/` is prose an agent reads; and the criteria
library is data. What any of them says about a broker is documentation for a person, not a
dependency of the core. The data adapters are *not* exempt: a vendor package that named a
broker would be the same leak in the other direction.
"""

from __future__ import annotations

import re
from pathlib import Path

import kanso
from kanso.nautilus import adapters
from kanso.portfolio import clients

PACKAGE = Path(kanso.__file__).resolve().parent
BROKERS = PACKAGE / "nautilus" / "adapters"
EXEMPT = (
    PACKAGE / "templates",
    PACKAGE / "skills",
    PACKAGE / "criteria" / "library",
)

SCANNED = ("*.py", "*.yaml", "*.yml", "*.toml", "*.md")
"""Every shipped text file a broker name could hide in."""


def brokers() -> tuple[str, ...]:
    """The broker packages that ship, read from the directory rather than listed."""
    return tuple(
        sorted(
            path.name
            for path in BROKERS.iterdir()
            if path.is_dir() and (path / "__init__.py").is_file()
        )
    )


def outside() -> list[Path]:
    """Every shipped file that is neither a broker adapter's nor exempt."""
    found = [path for pattern in SCANNED for path in PACKAGE.rglob(pattern)]
    return sorted(
        path
        for path in found
        if not path.is_relative_to(BROKERS)
        and not any(path.is_relative_to(directory) for directory in EXEMPT)
    )


def test_there_is_at_least_one_broker_to_scan_for() -> None:
    """A scan over an empty list of names passes vacuously, which would prove nothing."""
    assert brokers()
    assert set(brokers()) == set(adapters.packaged())


def test_no_broker_name_appears_outside_its_own_adapter_package() -> None:
    """An endpoint, a field name or an order grammar has exactly one home."""
    patterns = {name: re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE) for name in brokers()}
    leaked = [
        f"{path.relative_to(PACKAGE)}: {name}"
        for path in outside()
        for name, pattern in patterns.items()
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert leaked == []


def test_the_template_is_the_one_place_a_broker_is_named() -> None:
    """The exemption, asserted rather than assumed.

    A workspace inherits its venue model from a broker, so the rendered `kanso.toml` names
    one as the operator's default. The core gives `broker` no default of its own, which is
    what keeps a workspace that names a broker nobody provides on the shipped defaults
    instead of on a refusal.
    """
    rendered = (PACKAGE / "templates" / "kanso.toml").read_text(encoding="utf-8")

    assert any(f'broker = "{name}"' in rendered for name in brokers())
    assert kanso.config.ResearchConfig().broker is None


def test_nothing_outside_a_broker_package_imports_one() -> None:
    """The registry reaches an adapter by discovery, so no import direction is ever reversed."""
    imports = re.compile(r"(?:from|import)\s+kanso\.nautilus\.adapters\.\w")
    leaked = [
        str(path.relative_to(PACKAGE))
        for path in outside()
        if path.suffix == ".py" and imports.search(path.read_text(encoding="utf-8"))
    ]
    assert leaked == []


def test_every_broker_package_registers_itself_under_its_own_directory_name() -> None:
    """Discovery is by directory, so an id that disagreed with one would be unreachable."""
    assert sorted(adapters.packaged()) == list(brokers())
    for broker_id, broker in adapters.packaged().items():
        assert broker.id == broker_id
        assert broker.kind == "execution"
        assert broker.exec_clients


def test_every_broker_client_declares_what_the_core_is_allowed_to_know() -> None:
    """Two declarations per client, and the credential names each account needs.

    The core reasons about a broker through exactly these: how the capital is funded, which
    clock it runs on, and which variables would open it. Everything else about a broker is
    behind the adapter, and reading these costs no credential and opens no socket.
    """
    for one in clients.declared():
        assert one.capital in {"simulated", "broker_paper", "real"}
        assert one.clock in {"replay", "wall"}
        if one.source in adapters.packaged():
            assert one.credentials, f"{one.id} names no variable to open an account with"
            assert all(name.startswith("KANSO_") for name in one.credentials)
