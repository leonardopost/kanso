"""The core knows no vendor: a source scan and an import-graph check that say so.

Adapter isolation is the property that lets kanso ship a vendor and stay usable without
one. It is cheap to state and expensive to lose: the first module outside an adapter
package that names a vendor is a second place its wire has to be changed, a second place a
credential could be read, and the end of "the suite, `doctor` and the demo are green with
every vendor credential unset".

Neither check names a vendor either. The vendors are read from the adapter directory, so
the day a second one lands it is scanned without anyone remembering to add it — which is
exactly the day this would otherwise start passing by accident.

Three directories are exempt by design. `templates/` is rendered into an operator's
workspace rather than imported, `skills/` is prose an agent reads, and the criteria library
is data; what any of them says about a vendor is documentation for a person, not a
dependency of the core.
"""

from __future__ import annotations

import re
from pathlib import Path

import kanso
from kanso.data import registry

PACKAGE = Path(kanso.__file__).resolve().parent
ADAPTERS = PACKAGE / "data" / "adapters"
EXEMPT = (PACKAGE / "templates", PACKAGE / "skills", PACKAGE / "criteria" / "library")

SCANNED = ("*.py", "*.yaml", "*.yml", "*.toml", "*.md")
"""Every shipped text file a vendor name could hide in."""


def vendors() -> tuple[str, ...]:
    """The vendor packages that ship, read from the directory rather than listed."""
    return tuple(
        sorted(
            path.name
            for path in ADAPTERS.iterdir()
            if path.is_dir() and (path / "__init__.py").is_file()
        )
    )


def outside() -> list[Path]:
    """Every shipped file that is neither an adapter's nor exempt."""
    found = [path for pattern in SCANNED for path in PACKAGE.rglob(pattern)]
    return sorted(
        path
        for path in found
        if not path.is_relative_to(ADAPTERS)
        and not any(path.is_relative_to(directory) for directory in EXEMPT)
    )


def test_there_is_at_least_one_vendor_to_scan_for() -> None:
    """A scan over an empty list of names passes vacuously, which would prove nothing."""
    assert vendors()
    assert set(vendors()) == set(registry.packaged())


def test_no_vendor_name_appears_outside_its_own_adapter_package() -> None:
    """The anti-leak scan: an endpoint, a field name or a symbology has exactly one home."""
    patterns = {name: re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE) for name in vendors()}
    leaked = [
        f"{path.relative_to(PACKAGE)}: {name}"
        for path in outside()
        for name, pattern in patterns.items()
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert leaked == []


def test_nothing_outside_an_adapter_package_imports_one() -> None:
    """The registry reaches an adapter by discovery, so no import direction is ever reversed."""
    imports = re.compile(r"(?:from|import)\s+kanso\.data\.adapters\.\w")
    leaked = [
        str(path.relative_to(PACKAGE))
        for path in outside()
        if path.suffix == ".py" and imports.search(path.read_text(encoding="utf-8"))
    ]
    assert leaked == []


def test_every_adapter_package_registers_itself_under_its_own_directory_name() -> None:
    """Discovery is by directory, so an id that disagreed with one would be unreachable."""
    assert sorted(registry.packaged()) == list(vendors())
    for adapter_id, adapter in registry.packaged().items():
        assert adapter.id == adapter_id
        assert adapter.kind in {"data", "reference", "exec"}
        assert adapter.credentials
