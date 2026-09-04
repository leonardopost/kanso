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
"""

from __future__ import annotations

from typing import Annotated, Any, cast

import typer

from kanso.certify.certificate import certificate_file, source_file
from kanso.certify.plan import plan as make_plan
from kanso.certify.plan import plan_file
from kanso.certify.run import certify
from kanso.certify.run import show as newest
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.hyp import Registration
from kanso.hyp import show as registration_of
from kanso.research.lanes import DEFAULT_LANE
from kanso.schemas import Certificate, EvaluatedGate, PlannedGate
from kanso.schemas.certification import PLAN_STAGES
from kanso.state import StateStore
from kanso.workspace import Workspace

app = typer.Typer(help="What would count as proof, and whether it holds.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
IdArgument = Annotated[str, typer.Argument(metavar="ID", help="The hypothesis id.")]

MARK, GATE = 6, 22
"""Widths of the stage-or-verdict column and the gate-id column of the gate lists."""

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
    path = plan_file(ws, hyp_id)
    data: dict[str, Any] = {**pinned.model_dump(mode="json", by_alias=True), "path": str(path)}
    counts = " · ".join(f"{stage} {len(pinned.stage_gates(stage))}" for stage in PLAN_STAGES)
    lines = [
        field("plan", f"{hyp_id} · version {pinned.plan_version} · by {pinned.planned_by}"),
        field("gates", f"{len(pinned.gates)} · {counts}"),
    ]
    lines += [indent(_planned(gate)) for gate in pinned.gates]
    lines.append(field("excluded", str(len(pinned.excluded))))
    lines += [indent(f"{'':<{MARK}}{item.id:<{GATE}}{item.reason}") for item in pinned.excluded]
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
    return [tally, *(indent(_evaluated(gate)) for gate in made.gates)]


def _evaluated(gate: EvaluatedGate) -> str:
    """One evaluated gate: pass, fail or skip, and the numbers it decided on."""
    mark = "skip" if gate.skipped is not None else ("pass" if gate.passed else "fail")
    return f"{mark:<{MARK}}{gate.id:<{GATE}}{_evidence(gate)}"


def _evidence(gate: EvaluatedGate) -> str:
    """What the gate saw, as one line, or why it saw nothing."""
    if gate.skipped is not None:
        return gate.skipped
    pairs = ", ".join(f"{key}={value}" for key, value in sorted(gate.evidence.items()))
    return pairs or "no evidence"


def _objective(made: Certificate) -> str:
    """The objective as certification measured it, on the certification window."""
    return f"{made.objective.id} {made.objective.value:.6f} ± {made.objective.se:.6f}"


def _pins(made: Certificate) -> str:
    """What the certificate is a claim under: the engine, the plan, the data, the search."""
    return (
        f"engine {made.nautilus_version} · plan {made.plan_version} · "
        f"snapshot {made.snapshot_id} · trial {made.n_trials}"
    )


def _status(ws: Workspace, opened: StateStore, hyp_id: str) -> str:
    """Where the verdict left the hypothesis; `show` answers with one registration here."""
    return cast("Registration", registration_of(ws, opened, hyp_id)).status
