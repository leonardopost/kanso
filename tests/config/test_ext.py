"""Extension discovery: what loads, what is skipped, and what is merely reported."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from kanso.ext import KINDS, REFUSAL, REFUSED, Extension, discover, shadows


@pytest.fixture(autouse=True)
def _leave_the_interpreter_as_found() -> Iterator[None]:
    modules = set(sys.modules)
    path = list(sys.path)
    yield
    for name in set(sys.modules) - modules:
        del sys.modules[name]
    sys.path[:] = path


def package(root: Path, name: str, body: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(body, encoding="utf-8")
    return directory


def module(root: Path, name: str, body: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_an_absent_extensions_directory_yields_nothing(tmp_path: Path) -> None:
    assert discover(tmp_path, ["kanso_ext"]) == []


def test_a_file_where_the_directory_should_be_yields_nothing(tmp_path: Path) -> None:
    (tmp_path / "kanso_ext").write_text("not a directory", encoding="utf-8")
    assert discover(tmp_path, ["kanso_ext"]) == []


def test_a_package_is_imported_and_its_declaration_collected(tmp_path: Path) -> None:
    path = package(
        tmp_path / "kanso_ext",
        "ext_alpha",
        'PROVIDES = {"loaders": ["my_loader"], "data_types": ("my_type",)}\nLOADED = True\n',
    )
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert found.name == "ext_alpha"
    assert found.path == path
    assert found.ok
    assert found.error is None
    assert found.provides == {"loaders": ("my_loader",), "data_types": ("my_type",)}
    assert getattr(found.module, "LOADED", False) is True


def test_a_single_file_extension_is_imported(tmp_path: Path) -> None:
    path = module(tmp_path / "kanso_ext", "ext_solo", 'PROVIDES = {"loaders": ["solo"]}\n')
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert (found.name, found.path, found.ok) == ("ext_solo", path, True)
    assert found.provides == {"loaders": ("solo",)}


def test_an_extension_without_a_declaration_provides_nothing(tmp_path: Path) -> None:
    package(tmp_path / "kanso_ext", "ext_quiet", "X = 1\n")
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert found.ok
    assert found.provides == {}


def test_a_package_may_import_its_own_submodules(tmp_path: Path) -> None:
    directory = package(tmp_path / "kanso_ext", "ext_deep", "from ext_deep.inner import PROVIDES\n")
    (directory / "inner.py").write_text('PROVIDES = {"adapters": ["deep"]}\n', encoding="utf-8")
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert found.ok
    assert found.provides == {"adapters": ("deep",)}


def test_a_broken_extension_is_reported_and_never_fatal(tmp_path: Path) -> None:
    root = tmp_path / "kanso_ext"
    package(root, "ext_bad", 'raise RuntimeError("no credentials for you")\n')
    package(root, "ext_good", 'PROVIDES = {"loaders": ["fine"]}\n')
    bad, good = discover(tmp_path, ["kanso_ext"])
    assert bad.name == "ext_bad"
    assert bad.module is None
    assert not bad.ok
    assert bad.error is not None
    assert "RuntimeError" in bad.error
    assert good.ok


def test_an_unparsable_extension_is_reported(tmp_path: Path) -> None:
    module(tmp_path / "kanso_ext", "ext_syntax", "def (\n")
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert not found.ok
    assert found.error is not None
    assert "SyntaxError" in found.error


def test_a_name_already_taken_by_an_imported_module_is_reported(tmp_path: Path) -> None:
    package(tmp_path / "kanso_ext", "json", "PROVIDES = {}\n")
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert not found.ok
    assert found.error is not None
    assert "already taken" in found.error


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("dir", "_private"),
        ("dir", "__pycache__"),
        ("dir", "plain"),
        ("file", "_hidden"),
    ],
)
def test_what_is_not_an_extension_is_skipped(tmp_path: Path, kind: str, name: str) -> None:
    root = tmp_path / "kanso_ext"
    root.mkdir()
    if kind == "file":
        (root / f"{name}.py").write_text("X = 1\n", encoding="utf-8")
    else:
        (root / name).mkdir()  # a directory with no __init__.py is not a package
    (root / "readme.txt").write_text("hello", encoding="utf-8")
    (root / "ext-with-dashes.py").write_text("X = 1\n", encoding="utf-8")
    assert [e.name for e in discover(tmp_path, ["kanso_ext"])] == []


def test_declarations_that_are_not_a_table_of_ids_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "kanso_ext"
    package(root, "ext_wrong", "PROVIDES = [1, 2, 3]\n")
    package(
        root, "ext_partly", 'PROVIDES = {"constructs": ["c"], "widgets": ["w"], "loaders": "l"}\n'
    )
    partly, wrong = discover(tmp_path, ["kanso_ext"])
    assert wrong.provides == {}
    assert wrong.error is not None
    assert "PROVIDES" in wrong.error
    assert partly.provides == {"constructs": ("c",)}
    assert partly.error is not None
    assert "loaders" in partly.error
    assert "widgets" in partly.error


def test_every_kind_is_declarable(tmp_path: Path) -> None:
    body = "PROVIDES = {" + ", ".join(f'"{k}": ["{k}_id"]' for k in KINDS) + "}\n"
    package(tmp_path / "kanso_ext", "ext_full", body)
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert found.error is None
    assert set(found.provides) == set(KINDS)


def test_a_gate_or_an_objective_is_refused_where_it_is_declared(tmp_path: Path) -> None:
    """No registry reads either kind, so collecting one would promise what it cannot keep.

    A workspace gate that is merely collected reads as `doctor` green and the extension
    loaded, and is then refused by `hyp validate` as an id the toolbox does not hold. The
    declaration is the one place that mismatch can be named while the author is still
    looking at the file, so it is refused here and the message says where one goes.
    """
    package(
        tmp_path / "kanso_ext",
        "ext_rules",
        'PROVIDES = {"gates": ["min_holding_period"], "objectives": ["net_edge_per_turn"]}\n',
    )
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert found.module is not None  # it imported; what it said about itself is refused
    assert not found.ok
    assert found.provides == {}
    assert found.error == REFUSAL.format(kinds="gates, objectives")
    assert "docs/extensions.md" in found.error


def test_a_refused_kind_leaves_the_kinds_beside_it_readable(tmp_path: Path) -> None:
    """Refusing one kind is not refusing the table: the rest is read as it always was."""
    package(
        tmp_path / "kanso_ext",
        "ext_mixed",
        'PROVIDES = {"gates": ["mine"], "loaders": ["house_bars"], "widgets": ["w"]}\n',
    )
    (found,) = discover(tmp_path, ["kanso_ext"])
    assert found.provides == {"loaders": ("house_bars",)}
    assert found.error is not None
    # Both problems in one line, the one with a repair first.
    assert found.error.startswith(REFUSAL.format(kinds="gates"))
    assert found.error.endswith("PROVIDES has unusable kinds: widgets")


def test_a_refused_kind_is_not_also_an_accepted_one() -> None:
    """The two tables are read in one pass, and a kind in both would make it order."""
    assert set(KINDS).isdisjoint(REFUSED)


def test_paths_are_visited_in_order_and_children_by_name(tmp_path: Path) -> None:
    package(tmp_path / "second", "ext_zulu", "")
    package(tmp_path / "first", "ext_bravo", "")
    package(tmp_path / "first", "ext_alfa", "")
    found = discover(tmp_path, ["first", "second"])
    assert [e.name for e in found] == ["ext_alfa", "ext_bravo", "ext_zulu"]


def test_shadowing_of_a_built_in_id_is_reported_not_resolved(tmp_path: Path) -> None:
    package(
        tmp_path / "kanso_ext",
        "ext_shadow",
        'PROVIDES = {"loaders": ["synthetic", "mine"], "data_types": ["bar"]}\n',
    )
    found = discover(tmp_path, ["kanso_ext"])
    builtins = {"loaders": ["synthetic", "file"], "data_types": ["bar"]}
    assert shadows(found, builtins) == [
        ("ext_shadow", "data_types", "bar"),
        ("ext_shadow", "loaders", "synthetic"),
    ]
    assert found[0].ok  # reported, and the extension still loaded


def test_nothing_shadows_when_the_registries_are_empty(tmp_path: Path) -> None:
    package(tmp_path / "kanso_ext", "ext_free", 'PROVIDES = {"loaders": ["mine"]}\n')
    assert shadows(discover(tmp_path, ["kanso_ext"]), {}) == []


def test_a_broken_extension_shadows_nothing() -> None:
    broken = Extension(name="ext_broken", path=Path("nowhere"), error="ImportError: boom")
    assert shadows([broken], {"loaders": ["g"]}) == []
