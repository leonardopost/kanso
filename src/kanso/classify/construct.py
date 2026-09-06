"""The construct catalogue: what a hypothesis *is* in portfolio-construction terms.

The taxonomy is the domain, not a menu of what this version happens to implement, so every
construct practice uses is in the catalogue and classifiable. A construct declares what it
attaches to (`needs_host`), whether its objective is measured on its own or against its
host (`objective_mode`), and whether this version can run it (`runnable`). A construct that
is classifiable but not runnable is accepted by classification and refused only when a run
is begun, naming the seam that would make it runnable — the refusal is late on purpose, so
the taxonomy stays honest about what an idea is even where the loop cannot yet test it.

Each catalogue item is a YAML file beside this module; its `impl` names a module exposing
`CONSTRUCT`, and the loader refuses an implementation whose declarations disagree with the
item, so the file the classifier reads and the object the runner uses cannot drift apart. A
workspace extension adds constructs by exposing the same item shape as a `CONSTRUCTS`
sequence; an extension id that is also a built-in id is reported and the built-in wins,
since an operator package must not silently redefine what a sleeve is.

Three things a construct owes the rest of the system:

* `harness` — everything the runner needs to execute one card: the class it loads from
  `strategy.py`, the base that class must subclass, the host it attaches to, and the
  mapping from each `Decision` field the construct sets to the host hook that consults it;
* `compose` — the strategy version certification produces: a sleeve is a new strategy at
  version 1, an attached construct is its host's version n+1 with itself appended. What the
  version was certified under and what composition measured are supplied by the caller,
  which is the only part of a version this module does not decide;
* `host_run` — the host-alone run a relative objective is differenced against, computed
  once per run and cached by data snapshot and host version, since the same host over the
  same data yields the same run for every card of that run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from importlib import import_module, resources
from typing import ClassVar, Final, Protocol, TypeVar, cast

from kanso import ext
from kanso.errors import KansoError, PreconditionError, ValidationError
from kanso.schemas import (
    AttachedRef,
    ConstructItem,
    ConstructRef,
    Expectation,
    Hypothesis,
    Params,
    Pins,
    SleeveRef,
    StrategyFile,
    StrategyVersion,
    parse_yaml,
)
from kanso.workspace import Workspace

PORTFOLIO: Final = "portfolio"
"""The host a construct names when it attaches to the book rather than to one sleeve."""

SLEEVE_ENTRY: Final = "Strategy"
MODIFIER_ENTRY: Final = "Modifier"
SLEEVE_BASE: Final = "kanso.nautilus.strategy.KansoStrategy"
MODIFIER_BASE: Final = "kanso.nautilus.strategy.KansoModifier"
SLEEVE_TEMPLATE: Final = "strategy_sleeve.py"
MODIFIER_TEMPLATE: Final = "strategy_modifier.py"

PACKAGE: Final = "kanso"
"""The source recorded on a construct this package ships."""

CONSTRUCTS_PACKAGE: Final = "kanso.classify.constructs"
CONSTRUCT_ATTR: Final = "CONSTRUCT"
"""What an implementation module exposes: the single instance of its construct."""

CONSTRUCTS_ATTR: Final = "CONSTRUCTS"
"""What an extension module exposes: a sequence of catalogue items in the item shape."""

KIND: Final = "constructs"
"""This catalogue's key in an extension's `PROVIDES` table."""

T = TypeVar("T")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class HostRef:
    """The certified host version a relative construct attaches to.

    A host is pinned by version, not by "latest": every card of a run differences against
    the same host, and the generated implementation the runner loads lives at `impl`.
    """

    strategy_id: str
    version: int
    sleeve: SleeveRef
    attached: tuple[AttachedRef, ...] = ()

    @property
    def impl(self) -> str:
        """The workspace-relative directory holding this version's generated impl."""
        return f"strategies/{self.strategy_id}/impl/{self.version}"

    @classmethod
    def of(cls, strategy: StrategyFile, version: int | None = None) -> HostRef:
        """The reference to one version of `strategy` (*default* its latest)."""
        chosen = strategy.latest() if version is None else _version(strategy, version)
        return cls(strategy.id, chosen.version, chosen.sleeve, tuple(chosen.attached))


def _version(strategy: StrategyFile, version: int) -> StrategyVersion:
    found = next((v for v in strategy.versions if v.version == version), None)
    if found is None:
        raise PreconditionError(
            f"host: {strategy.id} has no version {version}; it has 1..{strategy.latest().version}"
        )
    return found


@dataclass(frozen=True)
class Harness:
    """What the runner needs to execute one card of one construct.

    `entrypoint` is the class the lane's `strategy.py` must define and `base` the class it
    must subclass; `consults` maps each `Decision` field this construct sets to the host
    hook that reads it, which is empty for a construct that is a strategy of its own.
    """

    construct: str
    hyp: Hypothesis
    objective_mode: str
    entrypoint: str
    base: str
    template: str
    host: HostRef | None = None
    consults: Mapping[str, str] = field(default_factory=dict)

    @property
    def hyp_id(self) -> str:
        return self.hyp.id

    @property
    def relative(self) -> bool:
        """Whether the objective is measured against the host rather than on its own."""
        return self.objective_mode == "relative"

    @property
    def attached(self) -> bool:
        """Whether this card runs on a host rather than as a strategy of its own."""
        return self.host is not None

    def entry(self, module: object) -> type:
        """The class a loaded `strategy.py` must expose for this construct.

        Structural only: the runner owns the import and the subclass check, and this is
        what it verifies about the module it loaded before instantiating anything.
        """
        found = getattr(module, self.entrypoint, None)
        if not isinstance(found, type):
            raise ValidationError(
                f"strategy.py: defines no class {self.entrypoint}; a {self.construct} card "
                f"runs {self.entrypoint}, a subclass of {self.base}"
            )
        if getattr(found, "config_cls", None) is None:
            raise ValidationError(
                f"strategy.py: {self.entrypoint} declares no config_cls; the numeric fields "
                "of that config are the parameters certification perturbs"
            )
        tag = getattr(found, "construct", None)
        if self.attached and tag != self.construct:
            raise ValidationError(
                f"strategy.py: {self.entrypoint}.construct is {tag!r}, not {self.construct!r}"
            )
        return found


def run_key(host: HostRef, snapshot_id: str) -> str:
    """The cache key of a host-alone run: the data it saw and the host version it ran."""
    return f"{snapshot_id}:{host.strategy_id}@{host.version}"


class Construct(Protocol):
    """What every construct implements, shipped or provided by a workspace extension."""

    id: ClassVar[str]
    needs_host: ClassVar[str]
    objective_mode: ClassVar[str]
    runnable: ClassVar[bool]
    params: ClassVar[Mapping[str, tuple[str, ...]]]

    def check_params(self, params: Params | None) -> None:
        """Refuse a parameter this construct does not declare, or a value outside its set.

        Part of the protocol because it is what validation asks a construct before a run
        exists: `hyp validate` and the harness must refuse the same classification in the
        same words, and `Base` is where both get the implementation.
        """
        ...

    def harness(
        self, hyp: Hypothesis, host: StrategyFile | None = None, *, version: int | None = None
    ) -> Harness: ...

    def compose(
        self,
        strategy: StrategyFile | None,
        hyp_id: str,
        strategy_sha: str,
        params: Params | None = None,
        *,
        pins: Pins,
        expectation: Expectation,
        created_at: datetime | None = None,
    ) -> StrategyVersion: ...

    def host_run(
        self,
        host: HostRef,
        snapshot_id: str,
        compute: Callable[[HostRef], T],
        cache: MutableMapping[str, T] | None = None,
    ) -> T: ...


class Base:
    """Shared machinery: the declarations, parameter checking and the classification read."""

    id: ClassVar[str]
    needs_host: ClassVar[str] = "sleeve"
    objective_mode: ClassVar[str] = "relative"
    runnable: ClassVar[bool] = True
    params: ClassVar[Mapping[str, tuple[str, ...]]] = {}

    def check_params(self, params: Params | None) -> None:
        """Refuse a parameter this construct does not declare, or a value outside its set."""
        for name, value in (params or {}).items():
            allowed = self.params.get(name)
            if allowed is None:
                takes = (
                    f"; it takes {', '.join(sorted(self.params))}"
                    if self.params
                    else "; it takes none"
                )
                raise ValidationError(
                    f"construct.params: {self.id} has no parameter {name!r}{takes}"
                )
            if value not in allowed:
                raise ValidationError(
                    f"construct.params.{name}: {value!r} is not one of {', '.join(allowed)}"
                )

    def classification(self, hyp: Hypothesis) -> ConstructRef:
        """This hypothesis's classification, checked to be this construct's."""
        ref = hyp.construct
        if ref is None:
            raise PreconditionError(
                f"{hyp.id} is not classified, so there is no construct to run it as",
                remedy=f"run `kanso classify {hyp.id}`",
            )
        if ref.id != self.id:
            raise ValidationError(
                f"construct.id: {hyp.id} is classified {ref.id!r}, not {self.id!r}"
            )
        self.check_params(ref.params)
        return ref


class Sleeve(Base):
    """A construct that is a strategy of its own: absolute objective, no host."""

    needs_host = "none"
    objective_mode = "absolute"

    def harness(
        self, hyp: Hypothesis, host: StrategyFile | None = None, *, version: int | None = None
    ) -> Harness:
        self.classification(hyp)
        if host is not None:
            raise ValidationError(
                f"host: a {self.id} is a strategy of its own and attaches to nothing, "
                f"but {host.id} was given as its host"
            )
        return Harness(
            construct=self.id,
            hyp=hyp,
            objective_mode=self.objective_mode,
            entrypoint=SLEEVE_ENTRY,
            base=SLEEVE_BASE,
            template=SLEEVE_TEMPLATE,
        )

    def compose(
        self,
        strategy: StrategyFile | None,
        hyp_id: str,
        strategy_sha: str,
        params: Params | None = None,
        *,
        pins: Pins,
        expectation: Expectation,
        created_at: datetime | None = None,
    ) -> StrategyVersion:
        self.check_params(params)
        if strategy is not None:
            raise PreconditionError(
                f"strategy: a {self.id} composes a new strategy at version 1, but {strategy.id} "
                f"already has {len(strategy.versions)}"
            )
        return StrategyVersion(
            version=1,
            sleeve=SleeveRef(hyp_id=hyp_id, strategy_sha=strategy_sha),
            attached=[],
            pins=pins,
            expectation=expectation,
            state="composed",
            created_at=created_at or _now(),
        )

    def host_run(
        self,
        host: HostRef,
        snapshot_id: str,
        compute: Callable[[HostRef], T],
        cache: MutableMapping[str, T] | None = None,
    ) -> T:
        raise PreconditionError(
            f"{self.id}: an absolute objective is measured on its own run, so there is no "
            "host run to difference against"
        )


class Attached(Base):
    """A construct layered on a certified sleeve: relative objective, one host.

    `consults` is the whole of what makes these differ: each names the `Decision` field the
    modifier sets and the host hook that reads it.
    """

    consults: ClassVar[Mapping[str, str]]
    portfolio_seam: ClassVar[str | None] = None

    def harness(
        self, hyp: Hypothesis, host: StrategyFile | None = None, *, version: int | None = None
    ) -> Harness:
        ref = self.classification(hyp)
        if ref.host == PORTFOLIO:
            if self.portfolio_seam is None:
                raise ValidationError(
                    f"construct.host: {self.id} attaches to a sleeve, not to the portfolio"
                )
            raise PreconditionError(
                f"{self.id} on the portfolio is classifiable but not runnable in this version; "
                f"the seam is {self.portfolio_seam}",
                remedy=f"attach the {self.id} to a certified sleeve, or implement the seam",
            )
        if host is None:
            raise PreconditionError(
                f"host: {self.id} attaches to a sleeve, so a certified host strategy is needed",
                remedy="certify the host sleeve first, then run this hypothesis against it",
            )
        if ref.host is not None and ref.host != host.id:
            raise ValidationError(
                f"host: {hyp.id} is classified onto {ref.host!r}, not {host.id!r}"
            )
        return Harness(
            construct=self.id,
            hyp=hyp,
            objective_mode=self.objective_mode,
            entrypoint=MODIFIER_ENTRY,
            base=MODIFIER_BASE,
            template=MODIFIER_TEMPLATE,
            host=HostRef.of(host, version),
            consults=self.consults,
        )

    def compose(
        self,
        strategy: StrategyFile | None,
        hyp_id: str,
        strategy_sha: str,
        params: Params | None = None,
        *,
        pins: Pins,
        expectation: Expectation,
        created_at: datetime | None = None,
    ) -> StrategyVersion:
        self.check_params(params)
        if strategy is None:
            raise PreconditionError(
                f"strategy: a {self.id} composes onto its host's latest version, and no host "
                "strategy was given"
            )
        latest = strategy.latest()
        return StrategyVersion(
            version=latest.version + 1,
            sleeve=latest.sleeve,
            attached=[
                *latest.attached,
                AttachedRef(
                    hyp_id=hyp_id,
                    strategy_sha=strategy_sha,
                    construct=self.id,
                    params=params,
                ),
            ],
            config=latest.config,
            pins=pins,
            expectation=expectation,
            state="composed",
            created_at=created_at or _now(),
        )

    def host_run(
        self,
        host: HostRef,
        snapshot_id: str,
        compute: Callable[[HostRef], T],
        cache: MutableMapping[str, T] | None = None,
    ) -> T:
        """The host-alone run, computed once per `(snapshot, host version)` in `cache`."""
        if cache is None:
            return compute(host)
        key = run_key(host, snapshot_id)
        if key not in cache:
            cache[key] = compute(host)
        return cache[key]


class Seam(Base):
    """A construct the taxonomy names and this version cannot run.

    Classification accepts it; every attempt to run it is refused here, naming what would
    make it runnable, so an operator reads the seam rather than a missing implementation.
    """

    runnable = False
    seam: ClassVar[str]

    def harness(
        self, hyp: Hypothesis, host: StrategyFile | None = None, *, version: int | None = None
    ) -> Harness:
        raise self.refusal()

    def compose(
        self,
        strategy: StrategyFile | None,
        hyp_id: str,
        strategy_sha: str,
        params: Params | None = None,
        *,
        pins: Pins,
        expectation: Expectation,
        created_at: datetime | None = None,
    ) -> StrategyVersion:
        raise self.refusal()

    def host_run(
        self,
        host: HostRef,
        snapshot_id: str,
        compute: Callable[[HostRef], T],
        cache: MutableMapping[str, T] | None = None,
    ) -> T:
        raise self.refusal()

    def refusal(self) -> PreconditionError:
        """The one refusal every entry point of a non-runnable construct raises."""
        return PreconditionError(
            f"{self.id} is classifiable but not runnable in this version; the seam is {self.seam}",
            remedy=f"classify onto a runnable construct, or implement the {self.id} seam",
        )


@dataclass(frozen=True)
class Entry:
    """One catalogue entry: the item the classifier reads and the object the runner uses."""

    item: ConstructItem
    construct: Construct
    source: str = PACKAGE


@dataclass(frozen=True)
class Shadow:
    """An extension claiming an id this package already defines. The built-in wins."""

    extension: str
    id: str

    def __str__(self) -> str:
        return (
            f"extension {self.extension!r} declares the construct {self.id!r}, which is "
            "built in; the built-in one is used"
        )


@dataclass(frozen=True)
class Catalogue:
    """Every construct available to a workspace, and what was wrong with the rest."""

    entries: Mapping[str, Entry]
    shadowed: tuple[Shadow, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def items(self) -> list[ConstructItem]:
        """The declarations classification shows the model, in id order."""
        return [self.entries[key].item for key in sorted(self.entries)]

    def constructs(self) -> dict[str, Construct]:
        """The implementations, by id."""
        return {key: self.entries[key].construct for key in sorted(self.entries)}

    def get(self, construct_id: str) -> Construct:
        """One construct, refusing an id the catalogue does not hold."""
        entry = self.entries.get(construct_id)
        if entry is None:
            raise ValidationError(
                f"construct: {construct_id!r} is not in the catalogue, which holds "
                f"{', '.join(sorted(self.entries))}"
            )
        return entry.construct


@cache
def builtin() -> Mapping[str, Entry]:
    """The constructs this package ships, read from the YAML files beside this module."""
    entries: dict[str, Entry] = {}
    files = resources.files(CONSTRUCTS_PACKAGE)
    for file in sorted(files.iterdir(), key=lambda f: f.name):
        if not file.name.endswith(".yaml"):
            continue
        item = parse_yaml(
            ConstructItem, file.read_text(encoding="utf-8"), f"{CONSTRUCTS_PACKAGE}/{file.name}"
        )
        entries[item.id] = Entry(item, implementation(item))
    return entries


def implementation(item: ConstructItem) -> Construct:
    """The object named by an item's `impl`, checked to declare what the item declares."""
    try:
        module = import_module(item.impl)
    except ImportError as exc:
        raise ValidationError(f"{item.id}: {item.impl} cannot be imported: {exc}") from None
    found = getattr(module, CONSTRUCT_ATTR, None)
    if found is None:
        raise ValidationError(f"{item.id}: {item.impl} exposes no {CONSTRUCT_ATTR}")
    construct = cast(Construct, found)
    declared = {
        "id": construct.id,
        "needs_host": construct.needs_host,
        "objective_mode": construct.objective_mode,
        "runnable": construct.runnable,
        "params": {name: list(values) for name, values in construct.params.items()} or None,
    }
    stated = {
        "id": item.id,
        "needs_host": item.needs_host,
        "objective_mode": item.objective_mode,
        "runnable": item.runnable,
        "params": item.params,
    }
    disagree = sorted(key for key, value in stated.items() if declared[key] != value)
    if disagree:
        raise ValidationError(
            f"{item.id}: {item.impl} disagrees with the catalogue item on {', '.join(disagree)}"
        )
    return construct


def catalogue(ws: Workspace | None = None) -> Catalogue:
    """The built-in constructs, merged with those a workspace's extensions provide."""
    entries = dict(builtin())
    if ws is None:
        return Catalogue(entries)
    shadowed: list[Shadow] = []
    errors: list[str] = []
    extensions = ext.discover(ws.root, ws.config.extensions_paths)
    shadowed.extend(
        Shadow(name, item)
        for name, kind, item in ext.shadows(extensions, {KIND: tuple(entries)})
        if kind == KIND
    )
    for extension in extensions:
        if extension.ok:
            _merge(extension, entries, shadowed, errors)
    return Catalogue(entries, tuple(dict.fromkeys(shadowed)), tuple(errors))


def _merge(
    extension: ext.Extension,
    entries: dict[str, Entry],
    shadowed: list[Shadow],
    errors: list[str],
) -> None:
    """Fold one extension's declared items into the catalogue, reporting what it cannot."""
    declared = getattr(extension.module, CONSTRUCTS_ATTR, None)
    if declared is None:
        return
    if isinstance(declared, str | bytes) or not isinstance(declared, Sequence):
        errors.append(f"{extension.name}: {CONSTRUCTS_ATTR} is not a sequence of catalogue items")
        return
    for raw in declared:
        try:
            item = ConstructItem.model_validate(raw)
            held = entries.get(item.id)
            if held is not None and held.source == PACKAGE:
                shadowed.append(Shadow(extension.name, item.id))
            elif held is not None:
                errors.append(
                    f"{extension.name}: the construct {item.id!r} is already provided by "
                    f"{held.source!r}"
                )
            else:
                entries[item.id] = Entry(item, implementation(item), extension.name)
        except KansoError as exc:
            errors.append(f"{extension.name}: {exc}")


def constructs(ws: Workspace | None = None) -> dict[str, Construct]:
    """The construct catalogue as implementations, by id."""
    return catalogue(ws).constructs()


def get(construct_id: str, ws: Workspace | None = None) -> Construct:
    """One construct by id, refusing an id no catalogue entry holds."""
    return catalogue(ws).get(construct_id)
