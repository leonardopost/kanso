"""Workspace extension discovery.

A workspace may carry packages of its own that provide constructs, loaders, adapters,
execution clients and custom data types through the same interfaces the shipped ones use.
They live under the directories the configuration lists (`kanso_ext` by default), and they
are found at startup: every package or single-module file directly under such a directory
is imported, and what it declares is collected.

An extension is operator code, so importing one is expected to fail sometimes. A failure
is recorded on the extension and never raised: a broken extension degrades the workspace
to the extensions that do load, and `doctor` shows what went wrong.

An extension declares what it registers in a module-level `PROVIDES` table, mapping a
kind to the ids it claims. Discovery reads that declaration and compares it against the
built-in ids its caller supplies. It does not resolve a clash: shadowing is reported, and
which definition wins is not decided here.

A kind a registry cannot read is refused here rather than collected, so that a declaration
which could never take effect is a message at the declaration instead of a silence at the
command that wanted it. Gates and objectives are the two: certification plans from, and
judges by, the library in the package, and no workspace path reaches it.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

KINDS = (
    "constructs",
    "loaders",
    "adapters",
    "data_types",
    "exec_clients",
)
"""The kinds of thing an extension may declare in `PROVIDES`.

Each is a kind some registry reads from an extension. `exec_clients` is declared like any
other, and shadowing one that ships is worth reporting for the same reason: a packaged id
wins, so an extension declaring it would be registered nowhere and its author would have
no way to find out.
"""

REFUSED = ("gates", "objectives")
"""The kinds a workspace may not provide, refused where they are declared.

A plan is drawn from, and judged by, the criteria library in the package: no function
that builds the toolbox takes a workspace, so an extension's gate or objective would be
registered nowhere while `doctor` read green and the extension read loaded. Refusing the
declaration is what turns that silence into a sentence at the file the author wrote.
"""

REFUSAL = (
    "PROVIDES declares {kinds}, which a workspace cannot provide: the toolbox a plan is "
    "drawn from and judged by is the package's own library, so a gate or an objective is "
    "written in the package (docs/extensions.md)"
)
"""What an author is told about a refused kind, and where the two files that make one are
described."""


@dataclass(frozen=True)
class Extension:
    """One imported (or unimportable) workspace extension."""

    name: str
    path: Path
    module: object | None = None
    provides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.module is not None and self.error is None


def discover(workspace: Path, paths: list[str]) -> list[Extension]:
    """Import every extension under each configured path, in configuration order.

    A path that does not exist is not an error: the default one is absent from most
    workspaces. Within a path, extensions are visited in name order, so the result is
    the same on every host.
    """
    found: list[Extension] = []
    for entry in paths:
        directory = workspace / entry
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            name = _module_name(child)
            if name is not None:
                found.append(_load(name, child, directory))
    return found


def shadows(
    extensions: Iterable[Extension], builtins: Mapping[str, Iterable[str]]
) -> list[tuple[str, str, str]]:
    """Every `(extension, kind, id)` where a declared id is also a built-in one."""
    known = {kind: set(ids) for kind, ids in builtins.items()}
    return sorted(
        (ext.name, kind, item)
        for ext in extensions
        for kind, ids in ext.provides.items()
        for item in ids
        if item in known.get(kind, ())
    )


def _module_name(child: Path) -> str | None:
    """The importable name of a child of an extensions directory, if it is one."""
    if child.is_dir() and (child / "__init__.py").is_file():
        name = child.name
    elif child.is_file() and child.suffix == ".py":
        name = child.stem
    else:
        return None
    return name if name.isidentifier() and not name.startswith("_") else None


def _load(name: str, path: Path, directory: Path) -> Extension:
    try:
        module = _import(name, directory)
    except Exception as exc:  # operator code: any failure is a report, never a stop
        return Extension(name=name, path=path, error=f"{type(exc).__name__}: {exc}")
    origin = getattr(module, "__file__", None)
    if origin is None or not Path(origin).resolve().is_relative_to(directory.resolve()):
        return Extension(
            name=name,
            path=path,
            error=f"the name '{name}' was already taken by {origin or 'a namespace package'}",
        )
    provides, error = _read_provides(module)
    return Extension(name=name, path=path, module=module, provides=provides, error=error)


def _import(name: str, directory: Path) -> object:
    root = str(directory)
    sys.path.insert(0, root)
    try:
        importlib.invalidate_caches()
        return importlib.import_module(name)
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(root)


def _read_provides(module: object) -> tuple[dict[str, tuple[str, ...]], str | None]:
    """One module's declaration, and the one line describing everything wrong with it.

    A refused kind is reported before an unusable one, because the first has a repair the
    author can act on and the second is a typo. Both are reported together when both are
    there: an author fixing a declaration should see the whole of it once.
    """
    declared = getattr(module, "PROVIDES", None)
    if declared is None:
        return {}, None
    if not isinstance(declared, Mapping):
        return {}, "PROVIDES is not a table of kind to ids"
    provides: dict[str, tuple[str, ...]] = {}
    refused: list[str] = []
    unknown: list[str] = []
    for kind, ids in declared.items():
        if kind in REFUSED:
            refused.append(str(kind))
        elif kind not in KINDS or isinstance(ids, str) or not isinstance(ids, Iterable):
            unknown.append(str(kind))
        else:
            provides[str(kind)] = tuple(str(i) for i in ids)
    problems: list[str] = []
    if refused:
        problems.append(REFUSAL.format(kinds=", ".join(sorted(refused))))
    if unknown:
        problems.append(f"PROVIDES has unusable kinds: {', '.join(sorted(unknown))}")
    return provides, "; ".join(problems) or None
