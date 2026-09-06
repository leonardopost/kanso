"""`kanso ext show`: what an extension declares, and what each registry did with it.

The command's whole job is the second half. Discovery already reports what loaded, and
`doctor` already grades it; what nothing else answers is why an id an extension declares is
handed out by nothing, which is the state an operator meets as "my loader is not there".
So every test here writes a real extension into a real workspace and drives the command.

Every extension written here is named `house_*`. An extension is imported by its bare name,
and a name another test in the same session has already imported is refused as taken — so a
name shared with any other test file, anywhere in the suite, would make these pass alone and
fail in a full run. The prefix is the reservation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.ext import KINDS

from .conftest import at, payload, run

pytestmark = pytest.mark.usefixtures("_leave_the_interpreter_as_found")

LOADER = '''from typing import ClassVar


class Loader:
    """A loader that serves nothing; being registered is the whole of what is asserted."""

    id: ClassVar[str] = "{id}"

    def discover(self, spec):
        return []

    def load(self, ref, window):
        return []

    def load_arrow(self, ref, window):
        return None

    def manifest(self, ref):
        raise NotImplementedError


LOADERS = {{"{id}": Loader()}}
PROVIDES = {{"loaders": ["{id}"]}}
'''

CONSTRUCT = """from typing import Final

from kanso.classify.construct import Attached, Construct


class Sized(Attached):
    id = "{id}"
    consults = {{"scale": "size"}}


CONSTRUCT: Final[Construct] = Sized()
"""

CATALOGUE = """from kanso.schemas import ConstructItem

PROVIDES = {{"constructs": ["{id}"]}}

CONSTRUCTS = [
    ConstructItem(
        id="{id}",
        description="An overlay scaling a host sleeve's orders by a house rule.",
        needs_host="sleeve",
        objective_mode="relative",
        runnable=True,
        impl="{module}.inner",
    )
]
"""

CLIENT = """from kanso.schemas import ExecutionClientSpec

PROVIDES = {{"exec_clients": ["{id}"]}}

EXEC_CLIENTS = [ExecutionClientSpec(id="{id}", capital="simulated", clock="replay")]
"""

TYPE = """from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass

from kanso.data.types import register_custom_type


@customdataclass
class HouseSignal(Data):
    value: float = 0.0


register_custom_type("{id}", HouseSignal)

PROVIDES = {{"data_types": ["{id}"]}}
"""


def module(root: Path, name: str, body: str) -> Path:
    """One single-file extension in the workspace's default extensions directory."""
    directory = root / "kanso_ext"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def package(root: Path, name: str, body: str, **files: str) -> Path:
    """One package extension, with the named submodules beside its `__init__`."""
    directory = root / "kanso_ext" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "__init__.py").write_text(body, encoding="utf-8")
    for stem, source in files.items():
        (directory / f"{stem}.py").write_text(source, encoding="utf-8")
    return directory


def provisions(document: dict[str, object], name: str) -> dict[tuple[str, str], dict[str, str]]:
    """One extension's provisions from the `--json` object, keyed by kind and id."""
    extensions = document["extensions"]
    assert isinstance(extensions, list)
    (found,) = [one for one in extensions if one["name"] == name]
    return {(item["kind"], item["id"]): item for item in found["provides"]}


def test_a_workspace_with_no_extensions_says_so(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "ext", "show")

    assert result.exit_code == Exit.OK
    assert "kanso_ext" in result.stdout
    assert "extensions none" in result.stdout
    # Nothing loaded, so nothing is counted four ways at the reader.
    assert "loaded ·" not in result.stdout


def test_no_extensions_as_json_is_still_the_whole_object(
    runner: CliRunner, workspace: Path
) -> None:
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert document["paths"] == ["kanso_ext"]
    assert document["extensions"] == []
    assert document["notes"] == []
    assert document["counts"] == {
        "extensions": 0,
        "loaded": 0,
        "registered": 0,
        "shadowed": 0,
        "absent": 0,
    }


def test_a_declared_loader_the_module_exposes_is_registered(
    runner: CliRunner, workspace: Path
) -> None:
    module(workspace, "house_data", LOADER.format(id="house_bars"))

    result = at(runner, workspace, "ext", "show")

    assert result.exit_code == Exit.OK
    assert "house_data" in result.stdout
    assert "loaders      house_bars          registered" in result.stdout
    assert "1/1 loaded · 1 registered · 0 shadowed · 0 absent" in result.stdout


def test_a_registered_loader_is_the_one_a_load_would_find(
    runner: CliRunner, workspace: Path
) -> None:
    """What the command claims and what the next command finds are the same registry."""
    module(workspace, "house_data", LOADER.format(id="house_bars"))
    spec = workspace / "house.yaml"
    spec.write_text(yaml.safe_dump({"loader": "house_bars"}), encoding="utf-8")

    document = payload(at(runner, workspace, "ext", "show", "--json"))
    loaded = at(runner, workspace, "data", "load", "--loader", "house_bars", "--spec", spec)

    assert provisions(document, "house_data")[("loaders", "house_bars")]["state"] == "registered"
    # The loader was found: the spec it discovers nothing from is what fails, not the id.
    assert "is not a known loader" not in loaded.stdout + loaded.stderr


def test_a_declared_id_no_table_exposes_is_absent_with_the_reason(
    runner: CliRunner, workspace: Path
) -> None:
    module(workspace, "house_forgetful", 'PROVIDES = {"loaders": ["house_bars"]}\n')

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    assert "LOADERS table yields no loader under that id" in result.stdout
    item = provisions(document, "house_forgetful")[("loaders", "house_bars")]
    assert item["state"] == "absent"
    assert "LOADERS" in item["note"]


def test_a_declared_adapter_no_table_exposes_is_absent(runner: CliRunner, workspace: Path) -> None:
    module(workspace, "house_vendorless", 'PROVIDES = {"adapters": ["house"]}\n')

    document = payload(at(runner, workspace, "ext", "show", "--json"))

    item = provisions(document, "house_vendorless")[("adapters", "house")]
    assert item["state"] == "absent"
    assert "ADAPTERS" in item["note"]


def test_a_declared_construct_no_table_exposes_is_absent(
    runner: CliRunner, workspace: Path
) -> None:
    module(workspace, "house_bare", 'PROVIDES = {"constructs": ["house_overlay"]}\n')

    document = payload(at(runner, workspace, "ext", "show", "--json"))

    item = provisions(document, "house_bare")[("constructs", "house_overlay")]
    assert item["state"] == "absent"
    assert "CONSTRUCTS" in item["note"]


def test_a_declared_execution_client_no_table_exposes_is_absent(
    runner: CliRunner, workspace: Path
) -> None:
    module(workspace, "house_clientless", 'PROVIDES = {"exec_clients": ["house_exec"]}\n')

    document = payload(at(runner, workspace, "ext", "show", "--json"))

    item = provisions(document, "house_clientless")[("exec_clients", "house_exec")]
    assert item["state"] == "absent"
    assert "EXEC_CLIENTS" in item["note"]


def test_a_declared_data_type_nothing_registered_is_absent(
    runner: CliRunner, workspace: Path
) -> None:
    """A different id from the one the registering test uses, deliberately.

    `register_custom_type` writes a registry that lives for the process, so a type another
    test in this session registered is registered here too however this file is ordered.
    Two tests of one id would pass or fail on the order the suite happened to choose.
    """
    module(workspace, "house_typeless", 'PROVIDES = {"data_types": ["absent_signal"]}\n')

    document = payload(at(runner, workspace, "ext", "show", "--json"))

    item = provisions(document, "house_typeless")[("data_types", "absent_signal")]
    assert item["state"] == "absent"
    assert "register_custom_type" in item["note"]


def test_a_declared_gate_or_objective_is_refused_and_names_the_page(
    runner: CliRunner, workspace: Path
) -> None:
    """The two kinds no registry reads, refused at the declaration rather than listed.

    A plan is drawn from, and judged by, the criteria library in the package, and nothing
    that builds it takes a workspace. So a collected gate would read as `doctor` green and
    the extension loaded, and be refused a command later by `hyp validate` as an id the
    toolbox does not hold. Here the extension imported and the declaration did not read,
    which is the state this command exists to distinguish.
    """
    module(workspace, "house_rules", 'PROVIDES = {"gates": ["g"], "objectives": ["o"]}\n')

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    assert provisions(document, "house_rules") == {}
    (found,) = document["extensions"]
    assert found["loaded"] is True
    assert "which a workspace cannot provide" in str(found["error"])
    assert "docs/extensions.md" in str(found["error"])
    assert "gates, objectives" in result.stdout
    assert document["counts"] == {
        "extensions": 1,
        "loaded": 1,
        "registered": 0,
        "shadowed": 0,
        "absent": 0,
    }


def test_an_id_that_ships_is_reported_shadowed_for_every_kind(
    runner: CliRunner, workspace: Path
) -> None:
    """A packaged id wins every registry, so an extension claiming one is registered nowhere."""
    module(
        workspace,
        "house_greedy",
        "PROVIDES = {\n"
        '    "loaders": ["synthetic"],\n'
        '    "adapters": ["massive"],\n'
        '    "constructs": ["filter"],\n'
        '    "exec_clients": ["sandbox"],\n'
        '    "data_types": ["bar"],\n'
        "}\n",
    )

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    found = provisions(document, "house_greedy")
    assert {item["state"] for item in found.values()} == {"shadowed"}
    assert {kind for kind, _ in found} == set(KINDS)
    assert document["counts"]["shadowed"] == len(KINDS)
    assert "the packaged one is what this workspace uses" in result.stdout


def test_an_extension_that_did_not_import_is_reported_and_never_fatal(
    runner: CliRunner, workspace: Path
) -> None:
    module(workspace, "house_unset", 'raise RuntimeError("KANSO_HOUSE_API_KEY is unset")\n')
    module(workspace, "house_data", LOADER.format(id="house_bars"))

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    # A broken extension degrades a workspace; `doctor` grades it a warning and this
    # command must not grade the same fact harder than `doctor` does.
    assert result.exit_code == Exit.OK
    assert "failed · kanso_ext/house_unset.py" in result.stdout
    assert "RuntimeError: KANSO_HOUSE_API_KEY is unset" in result.stdout
    assert document["counts"] == {
        "extensions": 2,
        "loaded": 1,
        "registered": 1,
        "shadowed": 0,
        "absent": 0,
    }
    (broken,) = [one for one in document["extensions"] if one["name"] == "house_unset"]
    assert broken["loaded"] is False
    assert broken["provides"] == []


def test_an_extension_declaring_nothing_says_so(runner: CliRunner, workspace: Path) -> None:
    module(workspace, "house_quiet", "HELPER = 1\n")

    result = at(runner, workspace, "ext", "show")

    assert result.exit_code == Exit.OK
    assert "loaded · kanso_ext/house_quiet.py" in result.stdout
    assert "declares nothing" in result.stdout


def test_a_declaration_kanso_cannot_read_is_reported_beside_what_it_could(
    runner: CliRunner, workspace: Path
) -> None:
    module(
        workspace,
        "house_partly",
        'PROVIDES = {"widgets": ["w"], "loaders": "house_bars"}\n',
    )

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    (found,) = document["extensions"]
    # The module imported, so it is `loaded`; what it said about itself is unusable.
    assert found["loaded"] is True
    assert "unusable kinds: loaders, widgets" in str(found["error"])
    assert "PROVIDES has unusable kinds" in result.stdout


def test_an_unusable_declaration_kind_costs_the_kinds_that_skip_such_an_extension(
    runner: CliRunner, workspace: Path
) -> None:
    """Two registries take nothing from an extension whose declaration did not read.

    The construct catalogue and the execution client registry both skip an extension that
    is not `ok`, where the loader registry takes what it can. So one bad kind in `PROVIDES`
    silently costs an extension its constructs and its clients, and the reason is the
    declaration rather than the table — which is what this reports instead of blaming the
    table the operator would then go and check.
    """
    package(
        workspace,
        "house_typo",
        "from kanso.schemas import ExecutionClientSpec\n\n"
        "PROVIDES = {\n"
        '    "widgets": ["w"],\n'
        '    "loaders": ["house_bars"],\n'
        '    "exec_clients": ["house_exec"],\n'
        "}\n\n"
        'EXEC_CLIENTS = [ExecutionClientSpec(id="house_exec", capital="simulated", '
        'clock="replay")]\n\n'
        "from house_typo.loader import LOADERS  # noqa: E402\n",
        loader=LOADER.format(id="house_bars"),
    )

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    found = provisions(document, "house_typo")
    assert found[("loaders", "house_bars")]["state"] == "registered"
    client = found[("exec_clients", "house_exec")]
    assert client["state"] == "absent"
    assert client["note"] == (
        "the declaration above did not read, and this registry skips such an extension"
    )
    assert "PROVIDES has unusable kinds: widgets" in result.stdout


def test_a_construct_an_extension_provides_is_registered(
    runner: CliRunner, workspace: Path
) -> None:
    package(
        workspace,
        "house_constructs",
        CATALOGUE.format(id="house_overlay", module="house_constructs"),
        inner=CONSTRUCT.format(id="house_overlay"),
    )

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    assert "loaded · kanso_ext/house_constructs" in result.stdout
    item = provisions(document, "house_constructs")[("constructs", "house_overlay")]
    assert item["state"] == "registered"
    assert document["notes"] == []


def test_a_catalogue_item_that_does_not_validate_surfaces_as_a_note(
    runner: CliRunner, workspace: Path
) -> None:
    """The construct catalogue records the failure and raises nothing; nothing else prints it."""
    module(
        workspace,
        "house_misfiled",
        'PROVIDES = {"constructs": ["house_overlay"]}\n'
        "CONSTRUCTS = [\n"
        '    {"id": "house_overlay", "description": "d", "needs_host": "sleeve",\n'
        '     "objective_mode": "relative", "runnable": True, "impl": "not a dotted path"}\n'
        "]\n",
    )

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    notes = document["notes"]
    assert isinstance(notes, list)
    assert len(notes) == 1
    assert notes[0].startswith("house_misfiled: impl:")
    assert "house_misfiled: impl:" in result.stdout
    item = provisions(document, "house_misfiled")[("constructs", "house_overlay")]
    assert item["state"] == "absent"


def test_an_implementation_that_raises_does_not_take_the_command_with_it(
    runner: CliRunner, workspace: Path
) -> None:
    """The one command an operator reaches for when something is wrong must survive it.

    Building the construct catalogue imports every declared implementation, and one that
    raises anything but a kanso error takes the whole catalogue down — `classify` and
    `hyp validate` would both fail here. That makes every construct genuinely absent, so
    saying so and naming the cause is the truth; exiting 1 with a bare `RuntimeError` and
    no attribution would be the worst answer this command could give.
    """
    package(
        workspace,
        "house_boom",
        CATALOGUE.format(id="boomer", module="house_boom"),
        inner='raise RuntimeError("the house model server is not running")\n',
    )

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    item = provisions(document, "house_boom")[("constructs", "boomer")]
    assert item["state"] == "absent"
    assert item["note"] == (
        "the construct catalogue could not be built at all; the note below says why"
    )
    assert document["notes"] == [
        "the construct catalogue: RuntimeError: the house model server is not running"
    ]
    assert "the house model server is not running" in result.stdout


def test_an_execution_client_an_extension_provides_is_registered(
    runner: CliRunner, workspace: Path
) -> None:
    module(workspace, "house_broker", CLIENT.format(id="house_exec"))

    document = payload(at(runner, workspace, "ext", "show", "--json"))
    listed = payload(at(runner, workspace, "portfolio", "clients", "--json"))

    assert provisions(document, "house_broker")[("exec_clients", "house_exec")]["state"] == (
        "registered"
    )
    # The same registry `portfolio clients` and `deploy` read, not a restatement of it.
    assert "house_exec" in {one["id"] for one in listed["clients"]}


def test_a_data_type_an_extension_registers_is_registered(
    runner: CliRunner, workspace: Path
) -> None:
    module(workspace, "house_types", TYPE.format(id="house_signal"))

    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert provisions(document, "house_types")[("data_types", "house_signal")]["state"] == (
        "registered"
    )


def test_an_extension_outside_the_workspace_is_shown_by_its_own_path(
    runner: CliRunner, workspace: Path, tmp_path: Path
) -> None:
    """A configured path may be absolute, and then there is nothing to write it relative to."""
    elsewhere = tmp_path / "shared_ext"
    elsewhere.mkdir()
    (elsewhere / "house_data.py").write_text(LOADER.format(id="house_bars"), encoding="utf-8")
    config = workspace / "kanso.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'paths = ["kanso_ext"]', f'paths = ["{elsewhere}"]'
        ),
        encoding="utf-8",
    )

    result = at(runner, workspace, "ext", "show")

    assert result.exit_code == Exit.OK
    assert str(elsewhere) in result.stdout
    assert f"loaded · {elsewhere / 'house_data.py'}" in result.stdout


def test_a_workspace_configuring_no_extensions_path_looks_for_none(
    runner: CliRunner, workspace: Path
) -> None:
    config = workspace / "kanso.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace('paths = ["kanso_ext"]', "paths = []"),
        encoding="utf-8",
    )
    module(workspace, "house_data", LOADER.format(id="house_bars"))

    result = at(runner, workspace, "ext", "show")
    document = payload(at(runner, workspace, "ext", "show", "--json"))

    assert result.exit_code == Exit.OK
    assert "paths      none" in result.stdout
    assert document["paths"] == []
    assert document["extensions"] == []


def test_the_command_is_reachable_from_the_application_help(runner: CliRunner) -> None:
    result = run(runner, "--help")

    assert result.exit_code == Exit.OK
    assert "ext" in result.stdout
