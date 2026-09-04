"""The kanso command line: the typer application, its global options and its commands.

Two options are global. `--workspace PATH` names the workspace a command acts on and
defaults to the current directory, from which discovery walks up to the nearest
`kanso.toml`. `--json` is accepted by the application and by every command, and turns the
whole output into one object: the result on success, the error envelope on failure, with
the exit code unchanged either way.

Exit codes are the contract with the operator agent: 0 success, 1 error, 2 precondition
failed, 3 validation failed, 4 approval missing. A command raises the kanso error that
carries its code and this layer renders it; `doctor` is the one command whose success
still carries a code, since a workspace that fails a check exits 2 with the diagnosis on
standard output rather than an error envelope.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Annotated

import typer

from kanso import __version__, env, skills_sync, workspace
from kanso.cli import align as align_commands
from kanso.cli import cert as cert_commands
from kanso.cli import classify as classify_commands
from kanso.cli import data as data_commands
from kanso.cli import doctor as diagnosis
from kanso.cli import hyp as hyp_commands
from kanso.cli import inbox as inbox_commands
from kanso.cli import models as models_commands
from kanso.cli import monitor as monitor_commands
from kanso.cli import portfolio as portfolio_commands
from kanso.cli import replay as replay_commands
from kanso.cli import research as research_commands
from kanso.cli import status as status_commands
from kanso.cli import strat as strat_commands
from kanso.cli.context import STATE_DB, global_json, open_workspace, workspace_option
from kanso.cli.render import Report, emit, field, indent
from kanso.env import envelope as envelope_module
from kanso.errors import Exit
from kanso.state import StateStore
from kanso.workspace import Workspace

STATUS, NAME = 5, 14
"""Widths of the grade and the name column of the human diagnosis."""

WorkspaceOption = Annotated[
    Path | None,
    typer.Option("--workspace", "-w", metavar="PATH", help="Workspace directory (default: cwd)."),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]

app = typer.Typer(
    name="kanso",
    help="A minimal, agent-first quantitative research workbench on NautilusTrader.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
skills_app = typer.Typer(help="The packaged operator skills.", no_args_is_help=True)
env_app = typer.Typer(help="The host envelope.", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
app.add_typer(env_app, name="env")
app.add_typer(data_commands.app, name="data")
app.add_typer(hyp_commands.app, name="hyp")
app.add_typer(research_commands.app, name="research")
app.add_typer(models_commands.app, name="models")
app.add_typer(align_commands.app, name="align")
app.add_typer(cert_commands.app, name="cert")
app.add_typer(inbox_commands.app, name="inbox")
app.add_typer(strat_commands.app, name="strat")
app.add_typer(portfolio_commands.app, name="portfolio")
app.add_typer(replay_commands.app, name="replay")
app.add_typer(monitor_commands.app, name="monitor")

# Four commands are registered rather than declared here: their bodies live beside the
# other command modules, and the application is where the command line is assembled.
app.command("classify")(classify_commands.classify_command)
app.command("status")(status_commands.status_command)
app.command("promote")(portfolio_commands.promote_command)
app.command("demote")(portfolio_commands.demote_command)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    workspace_path: WorkspaceOption = None,
    as_json: JsonOption = False,
    version: Annotated[
        bool, typer.Option("--version", help="Print the versions in play and exit.")
    ] = False,
) -> None:
    """Global options, carried to the command that runs next."""
    ctx.obj = workspace_path
    if version:
        emit(as_json, _version)
    if ctx.invoked_subcommand is None:
        ctx.fail("missing command")


@app.command()
def init(
    ctx: typer.Context,
    directory: Annotated[
        Path | None, typer.Argument(metavar="DIR", help="Where to scaffold (default: cwd).")
    ] = None,
    demo: Annotated[bool, typer.Option("--demo", help="Render the mock-only demo.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Scaffold a workspace, link the skills and detect the envelope."""
    emit(as_json or global_json(ctx), lambda: _init(_target(ctx, directory), demo))


@app.command()
def doctor(
    ctx: typer.Context,
    report: Annotated[
        bool, typer.Option("--report", help="Redact paths, for pasting upstream.")
    ] = False,
    check_adapters: Annotated[
        bool, typer.Option("--check-adapters", help="Allow one request per adapter.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Diagnose the workspace, the install and the engine. Exits 2 when a check fails."""
    emit(as_json or global_json(ctx), lambda: _doctor(open_workspace(ctx), report, check_adapters))


@app.command()
def migrate(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Apply the pending state migrations."""
    emit(as_json or global_json(ctx), lambda: _migrate(open_workspace(ctx)))


@skills_app.command("sync")
def skills_sync_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Link the packaged skills into every configured target."""
    emit(as_json or global_json(ctx), lambda: _skills_sync(open_workspace(ctx)))


@env_app.command("detect")
def env_detect(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Detect the host, derive the lane plan and write `envelope.yaml`."""
    emit(as_json or global_json(ctx), lambda: _env_detect(open_workspace(ctx)))


# -- command bodies ---------------------------------------------------------------


def _version() -> Report:
    """The versions a bug report needs."""
    data: dict[str, object] = {
        "kanso": __version__,
        "python": platform.python_version(),
        "nautilus_trader": envelope_module.engine_version(),
    }
    return Report(data=data, lines=(f"kanso {__version__}",))


def _init(directory: Path, demo: bool) -> Report:
    ws = workspace.init(directory, demo=demo)
    links = skills_sync.sync(ws)
    envelope = env.detect(ws)
    path = env.write(ws, envelope)
    # The state store is created here rather than left to `migrate`, so a fresh
    # workspace is complete: its first `doctor` reports no pending migrations.
    with StateStore(ws.path(STATE_DB)) as store:
        applied = store.migrate()
    repository = workspace.in_git_repo(ws.root)
    data: dict[str, object] = {
        "workspace": str(ws.root),
        "demo": demo,
        "repository": str(repository) if repository else None,
        "skills": [str(link) for link, _ in links],
        "envelope": {"path": str(path), "lanes": envelope.plan.lanes},
        "migrations": applied,
        "notices": list(ws.notices),
    }
    detected = envelope.detected
    lines = [
        field("workspace", ws.root),
        field("skills", f"{len(links)} links · {len(ws.config.skills_targets)} target(s)"),
        field(
            "envelope",
            f"{envelope.plan.lanes} lane(s) · {detected.cores_total} cores · {detected.mem_gb} GB",
        ),
    ]
    if repository is not None:
        lines.append(field("repository", f"{repository} (kanso never runs git)"))
    lines += [indent(notice) for notice in ws.notices]
    lines.append(field("next", "kanso doctor"))
    return Report(data=data, lines=tuple(lines))


def _doctor(ws: Workspace, report: bool, check_adapters: bool) -> Report:
    checks = diagnosis.run(ws, check_adapters=check_adapters)
    root = str(ws.root)
    if report:
        redact = diagnosis.redactor(ws)
        checks = diagnosis.redacted(checks, redact)
        root = redact(root)
    counts = {
        status: sum(1 for check in checks if check.status == status)
        for status in ("ok", "warn", "fail")
    }
    failed = counts["fail"] > 0
    data: dict[str, object] = {
        "ok": not failed,
        "workspace": root,
        "counts": counts,
        "checks": [check.as_json() for check in checks],
    }
    lines: list[str] = [field("workspace", root)]
    for check in checks:
        lines.append(f"{check.status:<{STATUS}}{check.name:<{NAME}}{check.detail}")
        lines += [indent(item, STATUS + NAME) for item in check.items]
        if check.remedy:
            lines.append(indent(f"→ {check.remedy}", STATUS + NAME))
    summary = " · ".join(f"{count} {status}" for status, count in counts.items())
    lines.append(f"{len(checks)} checks · {summary}")
    return Report(data=data, lines=tuple(lines), code=Exit.PRECONDITION if failed else Exit.OK)


def _migrate(ws: Workspace) -> Report:
    with StateStore(ws.path(STATE_DB)) as store:
        applied = store.migrate()
        version = store.schema_version()
    data: dict[str, object] = {"applied": applied, "schema_version": version}
    lines = [
        field("applied", ", ".join(applied) if applied else "nothing pending"),
        field("schema", version),
    ]
    return Report(data=data, lines=tuple(lines))


def _skills_sync(ws: Workspace) -> Report:
    pairs = skills_sync.sync(ws)
    data: dict[str, object] = {
        "links": [{"link": str(link), "target": str(target)} for link, target in pairs]
    }
    per_directory: dict[Path, int] = {}
    for link, _ in pairs:
        per_directory[link.parent] = per_directory.get(link.parent, 0) + 1
    lines = [field("linked", f"{len(pairs)} skills")]
    lines += [indent(f"{directory}: {count}") for directory, count in per_directory.items()]
    return Report(data=data, lines=tuple(lines))


def _env_detect(ws: Workspace) -> Report:
    envelope = env.detect(ws)
    path = env.write(ws, envelope)
    detected, plan = envelope.detected, envelope.plan
    data: dict[str, object] = {
        "path": str(path),
        "envelope": envelope.model_dump(mode="json", by_alias=True),
    }
    lines = (
        field(
            "host",
            f"{detected.os} {detected.os_version} {detected.arch} · {detected.chip} · "
            f"{detected.cores_total} cores ({detected.cores_perf}P/{detected.cores_eff}E) · "
            f"{detected.mem_gb} GB · {'AC' if detected.on_ac_power else 'battery'}",
        ),
        field(
            "engine",
            f"nautilus_trader {detected.nautilus_version} · "
            f"wheel {'ok' if detected.nautilus_wheel_ok else 'unfit for this host'}",
        ),
        field(
            "plan",
            f"{plan.lanes} lane(s) · {plan.cores_per_lane} cores/lane · "
            f"{plan.mem_per_lane_gb} GB/lane · reserved {plan.reserved_cores} core(s), "
            f"{plan.reserved_mem_gb} GB",
        ),
        field("written", path),
    )
    return Report(data=data, lines=lines)


# -- context ----------------------------------------------------------------------


def _target(ctx: typer.Context, directory: Path | None) -> Path:
    """Where `init` scaffolds: its argument, else `--workspace`, else the cwd."""
    if directory is not None:
        return directory
    return workspace_option(ctx) or Path.cwd()
