"""The catalogue: the whole taxonomy, its implementations, and what extensions add to it."""

from __future__ import annotations

import pytest

from kanso.classify import catalogue, constructs, get
from kanso.classify.construct import Entry, Shadow, implementation
from kanso.errors import ValidationError
from kanso.schemas import ConstructItem
from kanso.workspace import Workspace
from tests.classify.conftest import extension, item, package

TAXONOMY = {
    # id: (needs_host, objective_mode, runnable)
    "sleeve": ("none", "absolute", True),
    "alpha": ("sleeve", "absolute", False),
    "filter": ("sleeve", "relative", True),
    "overlay": ("sleeve", "relative", True),
    "exit": ("sleeve", "relative", True),
    "execution": ("sleeve", "relative", False),
    "allocation": ("portfolio", "relative", False),
}


def test_every_construct_of_the_taxonomy_is_representable_and_loadable() -> None:
    entries = catalogue().entries
    assert set(entries) == set(TAXONOMY)
    for construct_id, (needs_host, mode, runnable) in TAXONOMY.items():
        entry = entries[construct_id]
        assert (entry.item.needs_host, entry.item.objective_mode) == (needs_host, mode)
        assert entry.item.runnable is runnable
        assert entry.item.description.strip()
        assert entry.source == "kanso"


def test_the_implementation_declares_what_the_item_declares() -> None:
    for construct_id, construct in constructs().items():
        assert construct.id == construct_id
        needs_host, mode, runnable = TAXONOMY[construct_id]
        assert (construct.needs_host, construct.objective_mode) == (needs_host, mode)
        assert construct.runnable is runnable


def test_only_the_filter_declares_parameters() -> None:
    assert catalogue().entries["filter"].item.params == {"scope": ["time", "instrument"]}
    assert constructs()["filter"].params == {"scope": ("time", "instrument")}
    assert all(not c.params for name, c in constructs().items() if name != "filter")


def test_the_items_are_ordered_and_are_what_classification_reads() -> None:
    items = catalogue().items
    assert [entry.id for entry in items] == sorted(TAXONOMY)
    assert all(isinstance(entry, ConstructItem) for entry in items)


def test_an_unknown_construct_is_refused_by_name() -> None:
    with pytest.raises(ValidationError, match="'strangle' is not in the catalogue"):
        get("strangle")


def test_an_implementation_module_must_expose_the_construct() -> None:
    with pytest.raises(ValidationError, match="exposes no CONSTRUCT"):
        implementation(ConstructItem.model_validate(item("sleeveish", "kanso.errors")))


def test_an_implementation_disagreeing_with_its_item_is_refused() -> None:
    stated = item("sleeve", "kanso.classify.constructs.sleeve", runnable=False)
    with pytest.raises(ValidationError, match="disagrees with the catalogue item on"):
        implementation(ConstructItem.model_validate(stated))


def test_a_workspace_with_no_extensions_holds_the_built_ins(ws: Workspace) -> None:
    found = catalogue(ws)
    assert set(found.entries) == set(TAXONOMY)
    assert (found.shadowed, found.errors) == ((), ())


def declares(name: str, construct_id: str, declare: bool = True) -> str:
    """An extension's `__init__.py`: what it provides and the item that describes it."""
    provides = f"PROVIDES = {{'constructs': ['{construct_id}']}}\n" if declare else ""
    return (
        f'"""An extension providing the {construct_id} construct."""\n'
        + provides
        + f"CONSTRUCTS = [{item(construct_id, f'{name}.constructs')!r}]\n"
    )


IMPL = '''
"""The implementation the item names."""
from kanso.classify.construct import Attached


class Extra(Attached):
    id = "{id}"
    consults = {{"scale": "size"}}


CONSTRUCT = Extra()
'''


def test_an_extension_construct_joins_the_catalogue(ws: Workspace) -> None:
    package(ws, "cx_extra", declares("cx_extra", "hedge_ratio"), IMPL.format(id="hedge_ratio"))
    found = catalogue(ws)
    assert found.errors == ()
    assert found.entries["hedge_ratio"].source == "cx_extra"
    assert found.get("hedge_ratio").id == "hedge_ratio"
    assert [entry.id for entry in found.items] == sorted([*TAXONOMY, "hedge_ratio"])


def test_an_extension_shadowing_a_built_in_id_is_reported_and_the_built_in_wins(
    ws: Workspace,
) -> None:
    package(ws, "cx_shadow", declares("cx_shadow", "filter"), IMPL.format(id="filter"))
    found = catalogue(ws)
    assert found.shadowed == (Shadow("cx_shadow", "filter"),)
    assert found.entries["filter"].source == "kanso"
    assert str(found.shadowed[0]) == (
        "extension 'cx_shadow' declares the construct 'filter', which is built in; "
        "the built-in one is used"
    )


def test_a_shadow_is_reported_even_when_the_extension_declares_nothing(ws: Workspace) -> None:
    body = declares("cx_quiet", "overlay", declare=False)
    package(ws, "cx_quiet", body, IMPL.format(id="overlay"))
    assert catalogue(ws).shadowed == (Shadow("cx_quiet", "overlay"),)


def test_two_extensions_claiming_one_id_is_an_error(ws: Workspace) -> None:
    impl = IMPL.format(id="hedge_ratio")
    package(ws, "cx_one", declares("cx_one", "hedge_ratio"), impl)
    package(ws, "cx_two", declares("cx_two", "hedge_ratio"), impl)
    found = catalogue(ws)
    assert found.entries["hedge_ratio"].source == "cx_one"
    assert found.errors == ("cx_two: the construct 'hedge_ratio' is already provided by 'cx_one'",)


def test_an_extension_declaring_no_constructs_is_ignored(ws: Workspace) -> None:
    extension(ws, "cx_silent", "PROVIDES = {'loaders': ['nothing']}\n")
    found = catalogue(ws)
    assert (found.errors, set(found.entries)) == ((), set(TAXONOMY))


@pytest.mark.parametrize(
    ("name", "declared"), [("cx_str", "'not a sequence'"), ("cx_map", "{'id': 'x'}")]
)
def test_a_malformed_constructs_table_is_reported(ws: Workspace, name: str, declared: str) -> None:
    extension(ws, name, f"CONSTRUCTS = {declared}\n")
    assert catalogue(ws).errors == (f"{name}: CONSTRUCTS is not a sequence of catalogue items",)


def test_a_malformed_item_is_reported_not_raised(ws: Workspace) -> None:
    extension(ws, "cx_item", "CONSTRUCTS = [{'id': 'nope'}]\n")
    assert "description" in catalogue(ws).errors[0]


def test_an_unimportable_impl_is_reported_not_raised(ws: Workspace) -> None:
    extension(ws, "cx_gone", f"CONSTRUCTS = [{item('ghost', 'nowhere.at_all')!r}]\n")
    assert catalogue(ws).errors == (
        "cx_gone: ghost: nowhere.at_all cannot be imported: No module named 'nowhere'",
    )


def test_a_broken_extension_leaves_the_catalogue_standing(ws: Workspace) -> None:
    extension(ws, "cx_broken", "raise RuntimeError('boom')\n")
    assert set(catalogue(ws).entries) == set(TAXONOMY)


def test_an_entry_pairs_the_item_with_its_implementation() -> None:
    entry = catalogue().entries["sleeve"]
    assert isinstance(entry, Entry)
    assert entry.construct is get("sleeve")
