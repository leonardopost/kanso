"""The generated implementation: what it holds, that it imports, and that it runs."""

from __future__ import annotations

from hashlib import sha256

import pytest

from kanso.certify.certificate import source_file
from kanso.criteria import drawdown_pct
from kanso.data.manifest import catalog_path
from kanso.errors import ValidationError
from kanso.nautilus import backtest
from kanso.nautilus.strategy import KansoModifier, KansoStrategy
from kanso.schemas import Certificate, load_yaml
from kanso.state import StateStore
from kanso.strategy import files, impl
from kanso.strategy.composition import compose
from kanso.workspace import Workspace
from tests.research.conftest import HYP_ID

from .conftest import ALLOWING, BLOCKING, FILTER_ID, VARYING, certified_filter, pinned


def test_the_directory_holds_the_certified_bytes_verbatim(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, version.version)

    directory = files.impl_dir(ws, HYP_ID, 1)
    copied = (directory / manifest.sleeve.source).read_bytes()

    assert copied == VARYING, "the implementation is the certified source, unrewritten"
    assert sha256(copied).hexdigest() == sleeve.strategy_sha
    assert manifest.sleeve.strategy_sha == sleeve.strategy_sha
    held = sorted(p.name for p in directory.iterdir() if p.suffix in (".py", ".yaml"))
    assert held == sorted([impl.MANIFEST_FILE, manifest.sleeve.source])


def test_the_manifest_names_the_classes_by_import_path(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    component = manifest.sleeve

    assert component.path == f"{component.module}:Strategy"
    assert component.config_path == f"{component.module}:Config"
    assert component.source == f"{component.module}.py"
    assert component.module.startswith(impl.MODULE_PREFIX)
    assert component.construct == "sleeve"


def test_the_module_name_carries_a_digest_of_its_own_bytes(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)

    assert manifest.sleeve.module == impl.module_name("sleeve", HYP_ID, VARYING)
    assert impl.module_name("sleeve", HYP_ID, VARYING) != impl.module_name(
        "sleeve", HYP_ID, VARYING + b"\n"
    ), "two different sources never share a module"


def test_the_generated_implementation_imports_and_configures_its_components(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    certified_filter(ws, store)
    compose(ws, store, FILTER_ID)

    loaded = impl.load(ws, HYP_ID, 2)

    assert issubclass(loaded.sleeve.cls, KansoStrategy)
    assert loaded.sleeve.config.hyp_id == HYP_ID
    assert loaded.sleeve.config.universe == ("DEMO.XNAS",), "a list becomes a tuple again"
    assert loaded.sleeve.config.capital == 100_000.0
    assert loaded.sleeve.config.max_drawdown_pct == 40.0
    assert loaded.sleeve.config.notional == 2_000.0, "the author's own field survives"
    assert [built.construct for built in loaded.attached] == ["filter"]
    assert issubclass(loaded.attached[0].cls, KansoModifier)
    assert loaded.attached[0].config.host_strategy_id == "Strategy"
    assert loaded.attached[0].config.hyp_id == FILTER_ID
    assert loaded.attached[0].config.scope == "time"


def test_the_loaded_components_build_into_engine_objects(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    certified_filter(ws, store)
    compose(ws, store, FILTER_ID)

    loaded = impl.load(ws, HYP_ID, 2)
    strategy = loaded.strategy()
    actors = loaded.actors()

    assert isinstance(strategy, KansoStrategy)
    assert len(actors) == 1
    assert isinstance(actors[0], KansoModifier)
    assert actors[0].host_strategy_id == "Strategy"
    assert loaded.manifest.components == (loaded.manifest.sleeve, *loaded.manifest.attached)


def test_the_implementation_runs_and_reproduces_the_expectation(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    source, attached = impl.sources(ws, manifest)

    result = backtest.run(
        backtest.RunRequest(
            hyp=pinned(ws, store),
            strategy_source=source,
            window=(version.expectation.window.start, version.expectation.window.end),
            snapshot_id=version.pins.snapshot_id,
            venue_model=version.pins.venue_model.model_dump(),
            capital=100_000.0,
            modifiers=attached,
        ),
        catalog_path(ws),
    )

    assert not result.crashed
    assert len(result.run.trades) > 1, "the sleeve trades the certification window"
    assert drawdown_pct(result.run) >= 0


def test_an_attached_implementation_hands_the_runner_its_modifier(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    certified_filter(ws, store, source=BLOCKING)
    compose(ws, store, FILTER_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 2)

    source, attached = impl.sources(ws, manifest)

    assert source == VARYING
    assert [(construct, bytes_) for construct, bytes_, _ in attached] == [("filter", BLOCKING)]
    assert [params for _, _, params in attached] == [{"scope": "time"}]


def test_a_version_with_no_implementation_is_refused_rather_than_guessed(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)

    with pytest.raises(ValidationError, match="has no implementation"):
        impl.read_manifest(ws, HYP_ID, 2)


# -- the version is the certified bytes, or it is refused --------------------------


def test_an_edited_source_is_refused_rather_than_loaded(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    """What a stage loads is hashed first, so an edited file is named rather than run."""
    compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    edited = files.impl_dir(ws, HYP_ID, 1) / manifest.sleeve.source
    edited.write_bytes(VARYING + b"\nEDITED = True\n")

    with pytest.raises(ValidationError, match="is not the sleeve that was certified") as refused:
        impl.load(ws, HYP_ID, 1)

    assert str(edited) in refused.value.message
    assert sha256(edited.read_bytes()).hexdigest()[:7] in refused.value.message
    assert sleeve.strategy_sha[:7] in refused.value.message, "the digest it should hash to"
    assert str(source_file(ws, HYP_ID, sleeve.strategy_sha)) in str(refused.value.remedy)


def test_an_edited_source_is_refused_where_a_replay_reads_it(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    """A replay runs the directory rather than the blobs, so it is checked the same way."""
    compose(ws, store, HYP_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    (files.impl_dir(ws, HYP_ID, 1) / manifest.sleeve.source).write_bytes(VARYING + b"\n")

    with pytest.raises(ValidationError, match="hashes to"):
        impl.sources(ws, manifest)


def test_an_edited_construct_is_refused_by_the_name_of_its_own_file(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    """Every component is checked, so an edit to an attached construct is found too."""
    compose(ws, store, HYP_ID)
    certified_filter(ws, store)
    compose(ws, store, FILTER_ID)
    manifest = impl.read_manifest(ws, HYP_ID, 2)
    edited = files.impl_dir(ws, HYP_ID, 2) / manifest.attached[0].source
    edited.write_bytes(ALLOWING.replace(b'"time"', b'"instrument"'))

    with pytest.raises(ValidationError, match="is not the filter that was certified") as refused:
        impl.sources(ws, manifest)
    assert str(edited) in refused.value.message

    with pytest.raises(ValidationError, match=f"{HYP_ID}@2 was certified with"):
        impl.load(ws, HYP_ID, 2)


def test_a_deleted_source_is_refused_rather_than_run_from_the_interpreter_cache(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    """A module imported once in this process would otherwise load out of `sys.modules`."""
    compose(ws, store, HYP_ID)
    impl.load(ws, HYP_ID, 1)
    manifest = impl.read_manifest(ws, HYP_ID, 1)
    (files.impl_dir(ws, HYP_ID, 1) / manifest.sleeve.source).unlink()

    with pytest.raises(ValidationError, match=f"is missing, so {HYP_ID}@1 has no sleeve to run"):
        impl.load(ws, HYP_ID, 1)


def test_a_second_composition_replaces_the_directory_rather_than_adding_to_it(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)
    directory = files.impl_dir(ws, HYP_ID, 1)
    (directory / "left_over.py").write_bytes(b"# from an attempt that failed\n")

    impl.generate(ws, store, HYP_ID, version, pinned(ws, store), 100_000.0)

    assert not (directory / "left_over.py").exists()
    assert (directory / impl.MANIFEST_FILE).is_file()


def test_a_source_that_does_not_import_is_refused_by_name(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)
    broken = version.model_copy(
        update={
            "sleeve": version.sleeve.model_copy(
                update={
                    "strategy_sha": store.put_blob(
                        b"raise RuntimeError('this file cannot be imported')\n"
                    )
                }
            )
        }
    )

    with pytest.raises(ValidationError, match="does not import"):
        impl.generate(ws, store, HYP_ID, broken, pinned(ws, store), 100_000.0)


def test_a_source_defining_no_entrypoint_is_refused(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)
    empty = version.model_copy(
        update={
            "sleeve": version.sleeve.model_copy(
                update={"strategy_sha": store.put_blob(b"VALUE = 1\n")}
            )
        }
    )

    with pytest.raises(ValidationError, match="defines no class Strategy"):
        impl.generate(ws, store, HYP_ID, empty, pinned(ws, store), 100_000.0)


def test_a_configuration_the_module_does_not_name_is_refused(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    version = compose(ws, store, HYP_ID)
    nested = version.model_copy(
        update={
            "sleeve": version.sleeve.model_copy(
                update={"strategy_sha": store.put_blob(HIDDEN_CONFIG)}
            )
        }
    )

    with pytest.raises(ValidationError, match="under a name of its own"):
        impl.generate(ws, store, HYP_ID, nested, pinned(ws, store), 100_000.0)


def test_a_modifier_source_must_define_a_modifier(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    certified_filter(ws, store, source=ALLOWING)
    version = compose(ws, store, FILTER_ID)
    swapped = version.model_copy(
        update={
            "attached": [
                version.attached[0].model_copy(
                    update={"strategy_sha": store.put_blob(b"class Modifier:\n    pass\n")}
                )
            ]
        }
    )

    with pytest.raises(ValidationError, match="subclassing KansoModifier"):
        impl.generate(ws, store, HYP_ID, swapped, pinned(ws, store), 100_000.0)


HIDDEN_CONFIG = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


def _make():
    class Config(KansoConfig):
        pass

    return Config


class Strategy(KansoStrategy):
    """Hides its configuration inside a function, so no import path names it."""

    config_cls = _make()
'''


def test_the_manifest_round_trips_through_its_own_file(
    ws: Workspace, store: StateStore, sleeve: Certificate
) -> None:
    compose(ws, store, HYP_ID)
    written = impl.read_manifest(ws, HYP_ID, 1)

    reread = load_yaml(impl.ImplManifest, impl.manifest_file(ws, HYP_ID, 1))

    assert reread == written
    assert reread.strategy_id == HYP_ID
    assert reread.version == 1
