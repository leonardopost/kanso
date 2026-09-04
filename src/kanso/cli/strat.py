"""`kanso strat`: the versions a certificate composes into, and the end of one's life.

A strategy is not written, it is composed: `compose` turns a hypothesis's passing
certificate into a version — a new strategy at version 1 for a sleeve, the host's next
version for anything attached to one — and generates the implementation every stage loads.
Certification already does this by itself, so the command is the hand-driven form of an
automatic act and repeating it returns the version that exists rather than making a second.

`show` reads: with no argument it lists every composed strategy and where its versions
stand, with `STRATEGY` it prints that strategy, with `STRATEGY@V` that one version.
`retire` ends a version — it leaves whatever stages hold it, and the stages whose kill
switch is off are restarted so the running nodes match the file.

`STRATEGY[@V]` is the notation everything downstream shares, so the parser lives here and
`promote`, `demote` and `replay` import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import typer

from kanso import strategy as strategies
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.errors import ValidationError
from kanso.portfolio import retire as retire_version
from kanso.strategy import compose as compose_version

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import StrategyFile, StrategyVersion
    from kanso.workspace import Workspace

app = typer.Typer(help="Composed strategies and their versions.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
TargetArgument = Annotated[
    str, typer.Argument(metavar="STRATEGY[@V]", help="A strategy id, optionally a version.")
]

STATE, LABEL = 12, 18
"""Widths of the state and name columns of the human listings."""


def target(text: str) -> tuple[str, int | None]:
    """Split `STRATEGY[@V]` into the id and the version, refusing anything else.

    A missing version means "whichever version this command's own rule picks", which is
    the latest for a read and the deployed one for a move; a version that is not a
    positive number is a typo rather than a default.
    """
    name, marker, chosen = text.partition("@")
    if not name.strip():
        raise ValidationError(
            f"strategy: {text!r} names no strategy",
            remedy="pass a strategy id, optionally with @VERSION",
        )
    if not marker:
        return name.strip(), None
    if not chosen.isdigit() or int(chosen) < 1:
        raise ValidationError(
            f"strategy: {text!r} names version {chosen!r}, which is not a version number",
            remedy=f"pass {name.strip()}@1 or higher, or leave the version out",
        )
    return name.strip(), int(chosen)


@app.command("compose")
def compose_command(
    ctx: typer.Context,
    hyp_id: Annotated[str, typer.Argument(metavar="ID", help="The certified hypothesis id.")],
    as_json: JsonOption = False,
) -> None:
    """Compose this hypothesis's passing certificate into a strategy version."""
    emit(as_json or global_json(ctx), lambda: _compose(open_workspace(ctx), hyp_id))


@app.command("show")
def show_command(
    ctx: typer.Context,
    strategy: Annotated[
        str | None,
        typer.Argument(metavar="STRATEGY[@V]", help="A strategy, or nothing to list them."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """List the composed strategies, or print one strategy or one of its versions."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx), strategy))


@app.command("retire")
def retire_command(
    ctx: typer.Context, strategy: TargetArgument, as_json: JsonOption = False
) -> None:
    """End a version: take it off every stage it is on and restart the stages that run."""
    emit(as_json or global_json(ctx), lambda: _retire(open_workspace(ctx), strategy))


# -- command bodies ---------------------------------------------------------------


def _compose(ws: Workspace, hyp_id: str) -> Report:
    with store(ws) as opened:
        version = compose_version(ws, opened, hyp_id)
        # A strategy is named after the hypothesis whose sleeve it was built from, so a
        # construct composed onto a host lands under the host's id without being told it.
        file = strategies.require(ws, version.sleeve.hyp_id)
    directory = strategies.impl_dir(ws, file.id, version.version)
    data: dict[str, Any] = {
        "strategy": file.id,
        **version.model_dump(mode="json", by_alias=True),
        "impl": str(directory),
        "path": str(strategies.strategy_file(ws, file.id)),
    }
    lines = (
        field("composed", f"{file.id}@{version.version} · {version.state}"),
        *_version_lines(version),
        field("impl", directory),
        field("written", strategies.strategy_file(ws, file.id)),
        field("next", "kanso portfolio show"),
    )
    return Report(data=data, lines=lines)


def _show(ws: Workspace, strategy: str | None) -> Report:
    if strategy is None:
        return _listing(ws)
    name, chosen = target(strategy)
    file = strategies.require(ws, name)
    if chosen is None:
        return _strategy(ws, file)
    if chosen > len(file.versions):
        raise ValidationError(
            f"strategy: {name} has no version {chosen}; it has 1..{len(file.versions)}"
        )
    return _version(ws, file, file.versions[chosen - 1])


def _retire(ws: Workspace, strategy: str) -> Report:
    name, chosen = target(strategy)
    with store(ws) as opened:
        done = retire_version(ws, opened, name, chosen)
    data: dict[str, Any] = {
        "strategy": done.strategy_id,
        "version": done.version,
        "was": done.was,
        "state": "retired",
        "left": list(done.stages),
        "redeployed": [one.stage for one in done.deployments],
        "halted": list(done.halted),
    }
    lines = (
        field("retired", f"{done.label} · was {done.was}"),
        field("left", ", ".join(done.stages) if done.stages else "no stage held it"),
        field(
            "restarted",
            (", ".join(one.stage for one in done.deployments) or "nothing to restart")
            + (f" · {', '.join(done.halted)} halted and left alone" if done.halted else ""),
        ),
    )
    return Report(data=data, lines=lines)


# -- rendering --------------------------------------------------------------------


def _listing(ws: Workspace) -> Report:
    """Every composed strategy, with one line per version."""
    held = strategies.strategies(ws)
    data: dict[str, Any] = {
        "strategies": [
            {
                "id": file.id,
                "versions": [
                    {"version": v.version, "state": v.state, "sleeve": v.sleeve.hyp_id}
                    for v in file.versions
                ],
            }
            for file in held
        ]
    }
    lines = [field("strategies", f"{len(held)} composed")]
    for file in held:
        for version in file.versions:
            lines.append(
                f"{version.state:<{STATE}}{f'{file.id}@{version.version}':<{LABEL}}"
                f"{_shape(version)}"
            )
    return Report(data=data, lines=tuple(lines))


def _strategy(ws: Workspace, file: StrategyFile) -> Report:
    """One strategy: every version it has, newest state first on its own line."""
    data: dict[str, Any] = {
        **file.model_dump(mode="json", by_alias=True),
        "path": str(strategies.strategy_file(ws, file.id)),
    }
    lines = [field("strategy", f"{file.id} · {len(file.versions)} version(s)")]
    for version in file.versions:
        lines.append(f"{version.state:<{STATE}}{f'@{version.version}':<{LABEL}}{_shape(version)}")
        lines.append(indent(_band(version), STATE + LABEL))
    lines.append(field("written", strategies.strategy_file(ws, file.id)))
    return Report(data=data, lines=tuple(lines))


def _version(ws: Workspace, file: StrategyFile, version: StrategyVersion) -> Report:
    """One version: what it is made of, what is expected of it and what it was pinned to."""
    directory = strategies.impl_dir(ws, file.id, version.version)
    data: dict[str, Any] = {
        "strategy": file.id,
        **version.model_dump(mode="json", by_alias=True),
        "impl": str(directory),
    }
    lines = (
        field("version", f"{file.id}@{version.version} · {version.state}"),
        *_version_lines(version),
        field("impl", directory),
    )
    return Report(data=data, lines=lines)


def _version_lines(version: StrategyVersion) -> tuple[str, ...]:
    """The three facts every view of a version prints: its shape, its band, its pins."""
    lines = [
        field("sleeve", f"{version.sleeve.hyp_id} · {version.sleeve.strategy_sha[:7]}"),
    ]
    lines += [
        indent(f"{ref.construct} {ref.hyp_id} · {ref.strategy_sha[:7]}") for ref in version.attached
    ]
    lines.append(field("expect", _band(version)))
    lines.append(
        field(
            "pins",
            f"engine {version.pins.nautilus_version} · plan {version.pins.plan_version} · "
            f"snapshot {version.pins.snapshot_id}",
        )
    )
    return tuple(lines)


def _shape(version: StrategyVersion) -> str:
    """What a version is made of, in one phrase."""
    attached = ", ".join(f"{ref.construct}:{ref.hyp_id}" for ref in version.attached)
    return f"sleeve {version.sleeve.hyp_id}" + (f" + {attached}" if attached else "")


def _band(version: StrategyVersion) -> str:
    """What composition measured of a version, and over which window."""
    expected = version.expectation
    low, high = expected.ci90
    return (
        f"{expected.objective_id} {expected.value:.6f} [{low:.6f}, {high:.6f}] · "
        f"mdd_p95 {expected.mdd_p95:.2f}% · {expected.window.start}..{expected.window.end}"
    )
