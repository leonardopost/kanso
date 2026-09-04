"""`kanso data adapters`: what is registered here, and the plain statement that no vendor is.

D14 is the property under test: the core knows no vendor, so a workspace with every vendor
credential unset still has a complete answer to "what can this fetch?" — the package's own
loaders and the manual instrument provider, none of which takes a credential or opens a
socket. `--check` therefore has nothing to reach and says so rather than pretending.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import at, payload


def test_the_built_in_loaders_and_the_manual_provider_are_what_is_registered(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "data", "adapters", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    by_id = {item["id"]: item for item in document["adapters"]}
    assert set(by_id) == {"synthetic", "csv_parquet", "manual"}
    assert by_id["synthetic"]["kind"] == "data"
    assert by_id["manual"]["kind"] == "reference"
    assert all(item["credentials"] == [] for item in document["adapters"])
    assert all(item["credentials_resolve"] for item in document["adapters"])
    assert all(item["quota"] is None for item in document["adapters"])


def test_it_says_plainly_that_no_vendor_is_configured(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "data", "adapters")

    assert result.exit_code == Exit.OK
    assert "no vendor adapter is configured" in result.stdout
    assert "synthetic · data · builtin · no credential" in result.stdout


def test_check_makes_no_network_call_because_nothing_is_configured(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "data", "adapters", "--check", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["checked"] is True
    assert any("made no network call" in note for note in document["notes"])


def test_a_configured_adapter_nothing_provides_is_named(runner: CliRunner, workspace: Path) -> None:
    config = workspace / "kanso.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[adapters.massive]\nkind = "data"\n',
        encoding="utf-8",
    )

    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    assert any("massive" in note for note in document["notes"])


def test_an_extension_s_loader_is_registered_beside_the_built_in_ones(
    runner: CliRunner, workspace: Path
) -> None:
    """The registry has one entry point, identical for a package loader and an operator's."""
    package = workspace / "kanso_ext" / "mine"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "PROVIDES = {'loaders': ('mine',)}\n\n\n"
        "class Mine:\n"
        "    id = 'mine'\n\n"
        "    def discover(self, spec):\n        return []\n\n"
        "    def load(self, ref, window):\n        return []\n\n"
        "    def load_arrow(self, ref, window):\n        return None\n\n"
        "    def manifest(self, ref):\n        return None\n\n\n"
        "LOADERS = {'mine': Mine()}\n",
        encoding="utf-8",
    )

    document = payload(at(runner, workspace, "data", "adapters", "--json"))

    by_id = {item["id"]: item for item in document["adapters"]}
    assert by_id["mine"]["provider"] == "extension"
