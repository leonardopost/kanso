"""`kanso ext show`: what this workspace's extensions declare, and what a registry took.

An extension is operator code a workspace imports at startup, so both of its failure modes
are quiet. It can fail to import, and the workspace degrades to the extensions that do
load. Or it can import cleanly and still declare an id that no registry ends up handing
out, which looks exactly the same from the outside: the loader, the construct or the
execution client is simply not there. `doctor` grades the first. This command answers both,
per declared id, because "my extension is missing" and "my loader is missing" have one
symptom and two repairs.

The state of a declared id is a fact about the registry for its kind and never a claim
about provenance: `registered` says the workspace's registry hands that id out, `shadowed`
says it hands out the packaged one of the same name, and `absent` says it hands out nothing
under it. Only the third needs a reason, and the reason is different for each kind — which
is the thing an operator is actually looking for and the thing a bare list of declarations
cannot say.

It exits 0 whatever it finds. Discovery is written so that a broken extension degrades a
workspace rather than stopping it, and `doctor` grades one `warn` on the same reasoning; a
second grader of one fact would eventually disagree with the first.

Nothing here opens a connection or resolves a credential. Every registry it asks is one
that answers from declarations — an adapter hands out loader *factories*, and none is
built — so this command answers in a workspace with no vendor or broker variable set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final

import typer

from kanso import ext
from kanso.classify.construct import Catalogue, catalogue
from kanso.cli.context import global_json, open_workspace
from kanso.cli.render import Report, emit, field, indent
from kanso.data.loader import loaders
from kanso.data.registry import adapter_loaders, adapters
from kanso.data.types import data_types
from kanso.portfolio import clients
from kanso.workspace import Workspace

app = typer.Typer(help="The workspace extensions.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]

NAME: Final = 16
"""Width of the extension column. An extension is named by its directory, which is an
importable Python name and so is longer than the two-column layout's label."""

KIND: Final = 13
ID: Final = 20
"""Widths of the kind and id columns of a provision line."""

REGISTERED: Final = "registered"
SHADOWED: Final = "shadowed"
ABSENT: Final = "absent"
STATES: Final = (REGISTERED, SHADOWED, ABSENT)
"""What a registry did with a declared id, and the order the summary counts them in."""

WHY: Final[Mapping[str, str]] = {
    "loaders": "the module's LOADERS table yields no loader under that id",
    "adapters": "the module's ADAPTERS table yields no adapter under that id",
    "constructs": "the module's CONSTRUCTS table yields no catalogue item under that id",
    "exec_clients": "the module's EXEC_CLIENTS table yields no client declaration under that id",
    "data_types": (
        "nothing registered it: a type is registered by calling "
        "kanso.data.types.register_custom_type while the module is being imported"
    ),
}
"""Why an id an imported extension declares is handed out by nothing, per kind."""

SKIPS_UNSOUND: Final = frozenset({"constructs", "exec_clients"})
"""The two registries that take nothing at all from an extension whose declaration did not
read, where the other three take what they can. So a `PROVIDES` kind that is unusable — or
one this version refuses — costs an extension its constructs and its execution clients and
leaves its loaders alone, and the reason those two are missing is the declaration rather
than the table they are in."""

UNSOUND: Final = "the declaration above did not read, and this registry skips such an extension"

UNBUILT: Final = "the construct catalogue could not be built at all; the note below says why"
"""Why every construct is absent when one implementation took the catalogue down with it."""


@dataclass(frozen=True, slots=True)
class Provision:
    """One id an extension declares, and what the registry for its kind did with it."""

    kind: str
    id: str
    state: str
    note: str | None = None

    def payload(self) -> dict[str, Any]:
        """The provision as one JSON object."""
        out: dict[str, Any] = {"kind": self.kind, "id": self.id, "state": self.state}
        if self.note is not None:
            out["note"] = self.note
        return out

    def line(self) -> str:
        """The provision as one human line: kind, id, verdict, and the reason if any."""
        tail = self.state if self.note is None else f"{self.state} · {self.note}"
        return f"{self.kind:<{KIND}}{self.id:<{ID}}{tail}"


@app.command("show")
def show_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """List the extensions, what each declares, and which registry took each id."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx)))


def _show(ws: Workspace) -> Report:
    found = ext.discover(ws.root, ws.config.extensions_paths)
    constructs, failure = _constructs(ws)
    held, shipped = _held(ws, found, constructs), ext.shipped(ws)
    why = WHY if failure is None else {**WHY, "constructs": UNBUILT}
    # An extension's catalogue item that does not validate is recorded by the construct
    # catalogue and raised by nothing, so a malformed one is invisible until a
    # classification asks for a construct that is not there. This is where it surfaces.
    notes = [*constructs.errors, *([] if failure is None else [failure])]
    rows = [(one, _provisions(one, held, shipped, why)) for one in found]
    counts = {
        state: sum(1 for _, items in rows for item in items if item.state == state)
        for state in STATES
    }
    loaded = sum(1 for one, _ in rows if one.module is not None)
    data: dict[str, Any] = {
        "paths": list(ws.config.extensions_paths),
        "extensions": [
            {
                "name": one.name,
                "path": str(one.path),
                "loaded": one.module is not None,
                "error": one.error,
                "provides": [item.payload() for item in items],
            }
            for one, items in rows
        ],
        "counts": {"extensions": len(rows), "loaded": loaded, **counts},
        "notes": notes,
    }
    lines = [field("paths", ", ".join(ws.config.extensions_paths) or "none")]
    if not rows:
        # The ordinary state of a workspace: the default path exists in no scaffold, and
        # a summary counting nothing four ways says less than the word does.
        return Report(data=data, lines=(*lines, field("extensions", "none")))
    for one, items in rows:
        lines.append(f"{one.name:<{NAME}}{_state(one)} · {_where(ws, one.path)}")
        if one.error is not None:
            lines.append(indent(one.error, NAME))
        lines += [indent(item.line(), NAME) for item in items]
        if one.module is not None and not items:
            lines.append(indent("declares nothing", NAME))
    lines += [indent(note) for note in notes]
    summary = " · ".join(f"{counts[state]} {state}" for state in STATES)
    lines.append(f"{loaded}/{len(rows)} loaded · {summary}")
    return Report(data=data, lines=tuple(lines))


def _constructs(ws: Workspace) -> tuple[Catalogue, str | None]:
    """The construct catalogue, or an empty one and the failure that stopped it.

    Building it imports every declared implementation, and one that raises anything but a
    kanso error takes the whole catalogue with it — which is what `classify` and
    `hyp validate` would then do too, so reporting every construct absent is the truth
    rather than a degradation of it. This is the command an operator reaches for when
    something is wrong, so it is the one command that must not die of the same thing.
    """
    try:
        return catalogue(ws), None
    except Exception as exc:
        return Catalogue({}), f"the construct catalogue: {type(exc).__name__}: {exc}"


def _held(
    ws: Workspace, extensions: Sequence[ext.Extension], constructs: Catalogue
) -> dict[str, frozenset[str]]:
    """The ids each registry hands out here, by kind.

    Asked of the registries themselves rather than restated, so a registry that grows an
    id does not have to remember to grow this too, and so this cannot claim an id is
    registered that the command needing it would not find.
    """
    return {
        "loaders": frozenset({*loaders(extensions), *adapter_loaders(ws, extensions)}),
        "adapters": frozenset(adapters(extensions)),
        "constructs": frozenset(constructs.entries),
        "exec_clients": frozenset(clients.registry(ws)),
        "data_types": frozenset(data_types()),
    }


def _provisions(
    extension: ext.Extension,
    held: Mapping[str, frozenset[str]],
    shipped: Mapping[str, frozenset[str]],
    why: Mapping[str, str],
) -> list[Provision]:
    """Every id this extension declares, in kind then id order, with its verdict."""
    return [
        _provision(kind, item, held, shipped, why, extension.error)
        for kind in sorted(extension.provides)
        for item in sorted(extension.provides[kind])
    ]


def _provision(
    kind: str,
    item: str,
    held: Mapping[str, frozenset[str]],
    shipped: Mapping[str, frozenset[str]],
    why: Mapping[str, str],
    error: str | None,
) -> Provision:
    if item in shipped.get(kind, frozenset()):
        return Provision(kind, item, SHADOWED, "the packaged one is what this workspace uses")
    if item in held.get(kind, frozenset()):
        return Provision(kind, item, REGISTERED)
    if error is not None and kind in SKIPS_UNSOUND:
        return Provision(kind, item, ABSENT, UNSOUND)
    return Provision(kind, item, ABSENT, why[kind])


def _state(extension: ext.Extension) -> str:
    """Whether the module imported. A module that imported and declared badly still did."""
    return "loaded" if extension.module is not None else "failed"


def _where(ws: Workspace, path: Path) -> str:
    """Where the extension is, written the way `[extensions] paths` writes it.

    Relative to the workspace when it is under it, which it is for every configured path
    that is itself relative — and that is every workspace this scaffolds.
    """
    return str(path.relative_to(ws.root)) if path.is_relative_to(ws.root) else str(path)
