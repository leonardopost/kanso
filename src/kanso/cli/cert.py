"""`kanso cert`: what would count as proof for a hypothesis, and whether it holds.

Three commands in one order. `plan` asks once what this hypothesis must survive and pins
the answer; `run` judges one card of the hypothesis against that plan, on the window
research never saw, and writes an immutable certificate; `show` prints the newest one.

**A failing verdict is not an error and does not exit like one.** The certificate is what
`cert run` produces, and a fail is evidence: it counts toward the failure run, its gates
are fed back into the next proposal, and the run that exhausts the allowance reaches the
operator through the inbox. So the command exits 0 and says `fail` on its first line. The
refusals that do carry exit 2 are refusals of the *act* rather than of the strategy —
certifying bytes already certified under this plan and this engine, and writing over a
certificate that exists — because a certificate is immutable.

`plan` is the one command here that spends a model call, and it spends exactly one per
hypothesis: reading a pinned plan is free, and `--replan` is the only way to pay again.

`plan` also warns, and warns rather than refuses. A paper gate judges the objective a stage
realises against the interval measured over the certification window, so a plan whose paper
window is a small fraction of that window asks for evidence too noisy to sit inside its own
band. The plan is the planner's and the windows are the operator's; the command says what
the pair costs and pins the plan either way.

`plan`, `run` and `show` each state the certification's power on a `power` line: the edge
the certification window resolves at eighty percent, and how likely `embargoed_window` is
to pass if the truth is the research estimate and if it is zero, from the research window's
own dispersion (`kanso.certify.power`). `plan` states it for the best card as it stands,
from the error that card recorded, and says so when there is nothing to state it from; a
certificate records the one its run measured, from the research window run again. The two
agree for a card this kanso measured, and can differ for one an earlier kanso did.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

import typer

from kanso.certify.certificate import certificate_file, source_file
from kanso.certify.plan import paper_window_note, plan_file, power_of
from kanso.certify.plan import plan as make_plan
from kanso.certify.run import certify
from kanso.certify.run import show as newest
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent, verdict
from kanso.hyp import Registration
from kanso.hyp import show as registration_of
from kanso.research.lanes import DEFAULT_LANE
from kanso.schemas import Certificate, PlannedGate, Power
from kanso.schemas.certification import PLAN_STAGES
from kanso.state import StateStore
from kanso.workspace import Workspace

app = typer.Typer(help="What would count as proof, and whether it holds.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
IdArgument = Annotated[str, typer.Argument(metavar="ID", help="The hypothesis id.")]

MARK, GATE = 6, 22
"""Widths of the stage-or-verdict column and the gate-id column of the gate lists."""

NO_BEST = (
    "nothing to state what the windows resolve from: no best card yet, or the best card's "
    "error is zero"
)
"""What `plan` says on its power line before research has kept anything, or when what it
kept carries no dispersion to work from."""

NEXT: dict[str, str] = {
    "certified": "kanso cert show {id}",
    "researching": "kanso research run {id}",
    "failed": "kanso inbox",
}
"""Where the verdict leaves the operator, by the status it moved the hypothesis to."""


@app.command("plan")
def plan_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    replan: Annotated[
        bool, typer.Option("--replan", help="Plan again, and mint the next plan version.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Decide what would count as proof for this hypothesis, and pin the answer."""
    emit(as_json or global_json(ctx), lambda: _plan(open_workspace(ctx), hyp_id, replan))


@app.command("run")
def run_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    sha: Annotated[
        str | None, typer.Option("--sha", metavar="S", help="A card's sha (default: `best`).")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Run the plan's certification gates on the embargoed window and write the certificate."""
    emit(as_json or global_json(ctx), lambda: _run(open_workspace(ctx), hyp_id, sha))


@app.command("show")
def show_command(ctx: typer.Context, hyp_id: IdArgument, as_json: JsonOption = False) -> None:
    """Print this hypothesis's newest certificate: the verdict, then the gates."""
    emit(as_json or global_json(ctx), lambda: _show(open_workspace(ctx), hyp_id))


# -- command bodies ---------------------------------------------------------------


def _plan(ws: Workspace, hyp_id: str, replan: bool) -> Report:
    with store(ws) as opened:
        pinned = make_plan(ws, opened, hyp_id, replan=replan, lane=DEFAULT_LANE)
        note = paper_window_note(ws, opened, pinned)
        stated = power_of(opened, pinned)
    warnings = [] if note is None else [note]
    path = plan_file(ws, hyp_id)
    data: dict[str, Any] = {
        **pinned.model_dump(mode="json", by_alias=True),
        "path": str(path),
        "warnings": warnings,
        "power": None if stated is None else stated.model_dump(mode="json"),
    }
    counts = " · ".join(f"{stage} {len(pinned.stage_gates(stage))}" for stage in PLAN_STAGES)
    lines = [
        field("plan", f"{hyp_id} · version {pinned.plan_version} · by {pinned.planned_by}"),
        field("gates", f"{len(pinned.gates)} · {counts}"),
    ]
    lines += [indent(_planned(gate)) for gate in pinned.gates]
    lines.append(field("excluded", str(len(pinned.excluded))))
    lines += [indent(f"{'':<{MARK}}{item.id:<{GATE}}{item.reason}") for item in pinned.excluded]
    lines.append(field("power", NO_BEST if stated is None else _power(stated)))
    if note is not None:
        lines.append(field("warning", note))
    lines += [field("pinned", path), field("next", f"kanso cert run {hyp_id}")]
    return Report(data=data, lines=tuple(lines))


def _run(ws: Workspace, hyp_id: str, sha: str | None) -> Report:
    with store(ws) as opened:
        made = certify(ws, opened, hyp_id, sha=sha, lane=DEFAULT_LANE)
        status = _status(ws, opened, hyp_id)
    path = certificate_file(ws, made)
    source = source_file(ws, hyp_id, made.strategy_sha)
    data: dict[str, Any] = {
        **made.model_dump(mode="json", by_alias=True),
        "path": str(path),
        "source": str(source),
        "status": status,
    }
    lines = [
        field("verdict", f"{hyp_id} · {made.sha7} · {made.verdict}"),
        *_gate_lines(made),
        field("objective", _objective(made)),
        *_power_line(made),
        field("pins", _pins(made)),
        field("written", path),
        field("source", source),
        field("next", NEXT.get(status, "kanso hyp show {id}").format(id=hyp_id)),
    ]
    return Report(data=data, lines=tuple(lines))


def _show(ws: Workspace, hyp_id: str) -> Report:
    with store(ws) as opened:
        found = newest(ws, opened, hyp_id)
    if found is None:
        empty: dict[str, Any] = {"id": hyp_id, "certificate": None}
        return Report(
            data=empty,
            lines=(
                field("verdict", f"{hyp_id} · no certificate yet"),
                field("next", f"kanso cert run {hyp_id}"),
            ),
        )
    path = certificate_file(ws, found)
    data: dict[str, Any] = {**found.model_dump(mode="json", by_alias=True), "path": str(path)}
    lines = (
        field("verdict", f"{hyp_id} · {found.sha7} · {found.verdict}"),
        *_gate_lines(found),
        field("objective", _objective(found)),
        *_power_line(found),
        field("pins", _pins(found)),
        field("written", path),
    )
    return Report(data=data, lines=lines)


# -- rendering --------------------------------------------------------------------


def _planned(gate: PlannedGate) -> str:
    """One planned gate: where it runs, what it is, at what values, and why."""
    return f"{gate.stage:<{MARK}}{gate.id:<{GATE}}{_params(gate)} — {gate.rationale}"


def _params(gate: PlannedGate) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(gate.params.items())) or "no params"


def _gate_lines(made: Certificate) -> list[str]:
    """The tally of what judged what, then one line per gate."""
    judged = [gate for gate in made.gates if gate.skipped is None]
    passed = sum(1 for gate in judged if gate.passed)
    tally = field(
        "gates",
        f"{len(judged)} judged · {passed} pass · {len(judged) - passed} fail · "
        f"{len(made.gates) - len(judged)} skipped",
    )
    return [tally, *(indent(verdict(gate, MARK, GATE)) for gate in made.gates)]


def _objective(made: Certificate) -> str:
    """The objective as certification measured it, on the certification window."""
    return f"{made.objective.id} {made.objective.value:.6f} ± {made.objective.se:.6f}"


def _power_line(made: Certificate) -> list[str]:
    """The power a certificate states, when it states one."""
    return [] if made.power is None else [field("power", _power(made.power))]


def _power(stated: Power) -> str:
    """What the certification window resolves, and the window gate's chances either side."""
    line = (
        f"resolves {stated.resolves:.4g} at 80% · se {stated.standard_error:.4g} on the "
        f"certification window from research's {stated.research:.4g} ± {stated.research_se:.4g}"
    )
    if stated.pass_at_research is None or stated.pass_at_zero is None:
        return f"{line} · embargoed_window sets no min_fraction, so no pass probability"
    return (
        f"{line} · embargoed_window passes {stated.pass_at_research:.2f} at the research "
        f"estimate, {stated.pass_at_zero:.2f} at zero"
    )


def _pins(made: Certificate) -> str:
    """What the certificate is a claim under: the engine, the plan, the data, the search."""
    return (
        f"engine {made.nautilus_version} · plan {made.plan_version} · "
        f"snapshot {made.snapshot_id} · trial {made.n_trials}"
    )


def _status(ws: Workspace, opened: StateStore, hyp_id: str) -> str:
    """Where the verdict left the hypothesis; `show` answers with one registration here."""
    return cast("Registration", registration_of(ws, opened, hyp_id)).status
