"""The generated implementation: the one directory every stage loads a version from.

`impl/<version>/` holds a verbatim copy of each certified source — the sleeve's bytes and
each attached construct's — beside a manifest naming the class in each file, the
configuration class it takes and the values that configuration is built with. Nothing is
rewritten on the way in: the bytes in this directory are byte-for-byte the bytes that were
certified, and their sha256 is recorded, so a version is checked against the certificate
that produced it without the state store.

**The check is made where the version is used.** Both ways out of this directory — loading
an implementation into an engine and reading its sources for a replay — hash every file
they are about to run and refuse the version when a digest is not the one its manifest
records. The directory is generated and long-lived, so the edit that matters happens long
after it is written; refusing there is what makes the code on a stage the code that was
certified rather than the code that happens to be on disk.

**One directory, every stage.** A backtest, a replay and a live node all load from here, so
they cannot drift apart. A file is named after the module it defines and that module name
carries a digest of its own bytes, which buys three things at once: the directory is on the
import path and the manifest's `module:Class` pairs resolve the way the engine resolves
every other component; two versions sharing a certified sleeve share one module rather than
loading the same code twice; and two workspaces holding different code can never collide on
a module name in one process, which a name built from the strategy id alone would.

The configuration is stored as plain data, so the manifest is legible and a node needs
nothing but this directory to build the components. It is applied to the configuration
class directly rather than through the engine's own JSON decoder, because `venue_model` is
a free-form mapping and that decoder refuses one; the sequence fields a configuration
declares as tuples are restored as tuples on the way in, since YAML has only lists.

Importing from the directory leaves a `__pycache__` in it, which is a transient artefact
of the interpreter rather than part of the version, in the same way a lane directory's is.
The files that make up a version are the manifest and the sources it names.

Engine facts this module relies on (nautilus_trader 1.231.0): a component is named to the
engine as the string `"module:Class"` and resolved with `importlib.import_module` followed
by `getattr`, which is what `ImportableStrategyConfig` and `ImportableActorConfig` carry;
`StrategyConfig` and `ActorConfig` are msgspec structs whose `__init__` assigns fields
without conversion, so a list handed to a tuple field stays a list unless it is converted.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module, invalidate_caches
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, get_origin, get_type_hints

from pydantic import Field

from kanso.certify.certificate import source_file
from kanso.errors import ValidationError
from kanso.schemas import (
    CatalogueId,
    FreeForm,
    HypId,
    Hypothesis,
    KansoModel,
    NonEmpty,
    Sha256,
    StrategyVersion,
    Versioned,
    load_yaml,
    write_yaml,
)
from kanso.strategy.files import impl_dir

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Mapping

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "MANIFEST_FILE",
    "MODULE_PREFIX",
    "SLEEVE",
    "Built",
    "Component",
    "ImplManifest",
    "Loaded",
    "generate",
    "load",
    "manifest_file",
    "module_name",
    "read_manifest",
    "sources",
]

MANIFEST_FILE: Final = "manifest.yaml"
"""What names the classes in the directory, and the configuration each is built with."""

MODULE_PREFIX: Final = "kanso_impl"
"""Every generated module starts with it, so nothing here can shadow an installed one."""

DIGEST_CHARS: Final = 12
"""How much of a source's digest goes into its module name."""

SLEEVE: Final = "sleeve"
"""The construct a sleeve component records, and the slot its file is named for."""

SLEEVE_ENTRY: Final = "Strategy"
MODIFIER_ENTRY: Final = "Modifier"
"""The class each kind of certified source must define; the templates spell both."""

_ADDED: set[str] = set()
"""Implementation directories already on the import path, so one is added once."""


class Component(KansoModel):
    """One certified source in an implementation, and the classes it exposes.

    `path` and `config_path` are the `"module:Class"` pairs the engine resolves, and
    `config` is the configuration that class is built with, as plain data.
    """

    hyp_id: HypId
    strategy_sha: Sha256
    construct_: CatalogueId = Field(alias="construct")
    source: NonEmpty
    path: NonEmpty
    config_path: NonEmpty
    config: FreeForm = Field(default_factory=dict)

    @property
    def construct(self) -> CatalogueId:  # type: ignore[override]
        """What this source is, in construction terms. Aliased: `construct` is taken."""
        return self.construct_

    @property
    def module(self) -> str:
        """The module both paths name."""
        return self.path.split(":", 1)[0]


class ImplManifest(Versioned):
    """`impl/<version>/manifest.yaml`: what one strategy version is made of and runs."""

    strategy_id: HypId
    version: int = Field(ge=1)
    sleeve: Component
    attached: list[Component] = Field(default_factory=list)
    created_at: datetime

    @property
    def components(self) -> tuple[Component, ...]:
        """The sleeve first, then each attached construct in the order it was composed."""
        return (self.sleeve, *self.attached)


@dataclass(frozen=True)
class Built:
    """One component as the class it is and the configuration object it takes."""

    construct: str
    hyp_id: str
    cls: Any
    config: Any


@dataclass(frozen=True)
class Loaded:
    """One implementation, imported and configured, ready to be put into an engine."""

    manifest: ImplManifest
    directory: Path
    sleeve: Built
    attached: tuple[Built, ...] = ()

    def strategy(self) -> Any:
        """A fresh instance of the sleeve, configured as the manifest says."""
        return self.sleeve.cls(config=self.sleeve.config)

    def actors(self) -> tuple[Any, ...]:
        """Fresh instances of the attached constructs, in composition order."""
        return tuple(built.cls(config=built.config) for built in self.attached)


def manifest_file(ws: Workspace, strategy_id: str, version: int) -> Path:
    """Where one version's manifest lives."""
    return impl_dir(ws, strategy_id, version) / MANIFEST_FILE


def read_manifest(ws: Workspace, strategy_id: str, version: int) -> ImplManifest:
    """One version's manifest, refusing a version whose implementation was never written."""
    path = manifest_file(ws, strategy_id, version)
    if not path.is_file():
        raise ValidationError(
            f"{path} is missing, so version {version} of {strategy_id} has no implementation "
            "to run",
            remedy="compose the version again, which regenerates its implementation",
        )
    return load_yaml(ImplManifest, path)


def module_name(slot: str, hyp_id: str, source: bytes) -> str:
    """The module one certified source is written as: its slot, its origin and its digest."""
    return f"{MODULE_PREFIX}_{slot}_{hyp_id}_{sha256(source).hexdigest()[:DIGEST_CHARS]}"


def sources(
    ws: Workspace, manifest: ImplManifest
) -> tuple[bytes, tuple[tuple[str, bytes, dict[str, Any]], ...]]:
    """The bytes this implementation holds: the sleeve's, then each construct's.

    Shaped as the runner asks for them, so measuring an implementation runs the files in
    the directory rather than the blobs they were copied from. Every file is checked
    against the digest the manifest records, so what is measured is what was certified.
    """
    sleeve = _verified(ws, manifest, manifest.sleeve)
    attached = tuple(
        (component.construct, _verified(ws, manifest, component), _params(component.config))
        for component in manifest.attached
    )
    return sleeve, attached


def generate(
    ws: Workspace,
    store: StateStore,
    strategy_id: str,
    version: StrategyVersion,
    hyp: Hypothesis,
    capital: float,
    created_at: datetime | None = None,
) -> ImplManifest:
    """Write one version's implementation and the manifest that names its classes.

    The sources are copied from the blobs the certificates recorded, so what runs is what
    was certified. Every one of them is imported before the manifest is written: a
    manifest exists only for a directory that loads, and the configuration class each
    entry point declares is discovered from the loaded module rather than assumed.
    """
    directory = impl_dir(ws, strategy_id, version.version)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    _add_to_path(directory)

    sleeve = _write_source(
        directory,
        SLEEVE,
        version.sleeve.hyp_id,
        store.get_blob(version.sleeve.strategy_sha),
        version.sleeve.strategy_sha,
        _sleeve_config(hyp, capital, version),
    )
    attached = [
        _write_source(
            directory,
            ref.construct,
            ref.hyp_id,
            store.get_blob(ref.strategy_sha),
            ref.strategy_sha,
            _modifier_config(sleeve.path, ref.hyp_id, ref.params),
        )
        for ref in version.attached
    ]
    manifest = ImplManifest(
        strategy_id=strategy_id,
        version=version.version,
        sleeve=sleeve,
        attached=attached,
        created_at=created_at or datetime.now(tz=UTC),
    )
    write_yaml(manifest, manifest_file(ws, strategy_id, version.version))
    return manifest


def load(ws: Workspace, strategy_id: str, version: int) -> Loaded:
    """Import one version's implementation and configure every component of it.

    Every file is hashed before anything is imported, so a version whose bytes are not the
    bytes it was certified with is named rather than run.
    """
    manifest = read_manifest(ws, strategy_id, version)
    directory = impl_dir(ws, strategy_id, version)
    for component in manifest.components:
        _verified(ws, manifest, component)
    _add_to_path(directory)
    return Loaded(
        manifest=manifest,
        directory=directory,
        sleeve=_build(manifest.sleeve),
        attached=tuple(_build(component) for component in manifest.attached),
    )


# --- checking one against its certificate --------------------------------------


def _verified(ws: Workspace, manifest: ImplManifest, component: Component) -> bytes:
    """One component's file, refused unless its bytes are the bytes that were certified.

    The manifest records every source's sha256 and the module name carries a prefix of the
    same digest, so the check is one hash of a file that was about to be read anyway. The
    refusal names the file, both digests and the certified source to restore it from: the
    bytes are also on disk beside the certificate that admitted them, under the sha they
    hash to.
    """
    path = impl_dir(ws, manifest.strategy_id, manifest.version) / component.source
    version = f"{manifest.strategy_id}@{manifest.version}"
    certified = source_file(ws, component.hyp_id, component.strategy_sha)
    remedy = (
        f"restore the file from {certified}, which holds the certified bytes; to run code "
        "of your own, research and certify it"
    )
    if not path.is_file():
        raise ValidationError(
            f"{path} is missing, so {version} has no {component.construct} to run",
            remedy=remedy,
        )
    source = path.read_bytes()
    digest = sha256(source).hexdigest()
    if digest != component.strategy_sha:
        raise ValidationError(
            f"{path} hashes to {digest[:7]} and {version} was certified with "
            f"{component.strategy_sha[:7]}, so this file is not the {component.construct} "
            "that was certified",
            remedy=remedy,
        )
    return source


# --- writing one source -------------------------------------------------------


def _write_source(
    directory: Path,
    slot: str,
    hyp_id: str,
    source: bytes,
    strategy_sha: str,
    config: dict[str, Any],
) -> Component:
    """Copy one certified source in, import it, and describe the classes it defines."""
    module = module_name(slot, hyp_id, source)
    (directory / f"{module}.py").write_bytes(source)
    invalidate_caches()
    entrypoint = SLEEVE_ENTRY if slot == SLEEVE else MODIFIER_ENTRY
    loaded = _import(module)
    cls = _entry(loaded, entrypoint, slot)
    return Component(
        hyp_id=hyp_id,
        strategy_sha=strategy_sha,
        construct=slot,
        source=f"{module}.py",
        path=f"{module}:{entrypoint}",
        config_path=f"{module}:{_named(loaded, cls.config_cls, entrypoint)}",
        config=config,
    )


def _sleeve_config(hyp: Hypothesis, capital: float, version: StrategyVersion) -> dict[str, Any]:
    """What the hypothesis and the pinned venue model inject into the sleeve.

    Exactly the fields the runner injects when it builds a card, so a version deployed to
    a stage is configured as the card that earned it was, with the version's own `config`
    layered on top for the fields an operator or a later construct set.
    """
    return {
        "hyp_id": hyp.id,
        "universe": list(hyp.universe),
        "resolution": hyp.resolution,
        "data_requirements": list(hyp.data_requirements),
        "capital": capital,
        "max_position_pct": hyp.risk_limits.max_position_pct,
        "max_drawdown_pct": hyp.risk_limits.max_drawdown_pct,
        "max_leverage": hyp.risk_limits.max_leverage,
        "venue_model": version.pins.venue_model.model_dump(mode="json"),
        **dict(version.config),
    }


def _modifier_config(
    sleeve_path: str, hyp_id: str, params: Mapping[str, Any] | None
) -> dict[str, Any]:
    """What an attached construct is configured with: its host, its origin and its params.

    The host is named by the sleeve's class, which is the name a modifier registers
    against and the name the sleeve answers to before the engine gives it an id of its own.
    """
    return {
        "host_strategy_id": sleeve_path.split(":", 1)[1],
        "hyp_id": hyp_id,
        **dict(params or {}),
    }


def _params(config: Mapping[str, Any]) -> dict[str, Any]:
    """A modifier's own parameters: its configuration without the two injected fields."""
    return {
        name: value for name, value in config.items() if name not in ("host_strategy_id", "hyp_id")
    }


# --- importing and configuring ------------------------------------------------


def _add_to_path(directory: Path) -> None:
    """Put an implementation directory on the import path, once."""
    entry = str(directory)
    if entry in _ADDED:
        return
    sys.path.append(entry)
    _ADDED.add(entry)
    invalidate_caches()


def _import(module: str) -> ModuleType:
    """One generated module, with an import failure named as the file that failed."""
    try:
        return import_module(module)
    except Exception as exc:
        raise ValidationError(
            f"{module}.py: the certified source does not import: {type(exc).__name__}: {exc}",
            remedy="certify a strategy that imports; the implementation runs these bytes",
        ) from None


def _entry(module: ModuleType, entrypoint: str, slot: str) -> Any:
    """The one class a certified source must define, checked against its base."""
    from kanso.nautilus.strategy import KansoModifier, KansoStrategy

    base = KansoStrategy if slot == SLEEVE else KansoModifier
    found = getattr(module, entrypoint, None)
    if not isinstance(found, type) or not issubclass(found, base):
        raise ValidationError(
            f"{module.__name__}.py: defines no class {entrypoint} subclassing "
            f"{base.__name__}; a {slot} is run by loading {entrypoint} from the file"
        )
    return found


def _named(module: ModuleType, config_cls: type, entrypoint: str) -> str:
    """The name a configuration class is reachable by in its own module.

    The engine resolves a configuration the way it resolves a component — a module and an
    attribute of it — so a class the module does not expose under a name of its own could
    not be loaded by a node, however well it works in process.
    """
    for name, value in vars(module).items():
        if value is config_cls:
            return name
    raise ValidationError(
        f"{module.__name__}.py: {entrypoint}.config_cls is "
        f"{config_cls.__qualname__}, which the module does not define under a name of its "
        "own; a configuration is loaded by import path, so it must be a module-level class"
    )


def _build(component: Component) -> Built:
    """One component as its class and a configuration object built from the manifest."""
    module = _import(component.module)
    cls = getattr(module, component.path.split(":", 1)[1])
    config_cls = getattr(module, component.config_path.split(":", 1)[1])
    return Built(
        construct=component.construct,
        hyp_id=component.hyp_id,
        cls=cls,
        config=config_cls(**_typed(config_cls, component.config)),
    )


def _typed(config_cls: type, config: Mapping[str, Any]) -> dict[str, Any]:
    """The stored configuration with its sequence fields restored to tuples.

    YAML has lists and a configuration declares tuples, and the engine's own decoder
    cannot be asked to do the conversion because a free-form mapping defeats it. Only the
    fields the class itself annotates as tuples are converted, so a field an author
    declared as a list stays one.
    """
    hints = get_type_hints(config_cls)
    return {
        name: tuple(value)
        if isinstance(value, list) and get_origin(hints.get(name)) is tuple
        else value
        for name, value in config.items()
    }
