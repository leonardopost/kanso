"""`kanso classify`: one call that says what a hypothesis is, written into its file.

The command is the operator's way of asking for the answer rather than stating it. Editing
`construct`, `objective` and `constraints` by hand and running `kanso hyp add` remains the
override path and needs no model at all; this is the same three keys, decided by the best
model the register has and checked against the catalogues before anything is written.

What it reports is what changed: the construct it chose and whether that construct can be
run by this version, the objective and its keep rule, the constraints every card will be
judged by, and whether a strategy stub was rendered. A stub is rendered only when the
hypothesis's `strategy.py` is still one kanso wrote, so the line is a fact about the file
rather than a promise about it.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from kanso.classify import catalogue
from kanso.classify.classify import classify
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.hyp import STRATEGY_FILE, hypothesis_dir
from kanso.research.lanes import DEFAULT_LANE
from kanso.schemas import Hypothesis
from kanso.workspace import Workspace

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]
IdArgument = Annotated[str, typer.Argument(metavar="ID", help="The hypothesis id.")]


def classify_command(
    ctx: typer.Context,
    hyp_id: IdArgument,
    as_json: JsonOption = False,
) -> None:
    """Decide the construct, the objective and the constraints, and write them into the file."""
    emit(as_json or global_json(ctx), lambda: _classify(open_workspace(ctx), hyp_id))


# -- command bodies ---------------------------------------------------------------


def _classify(ws: Workspace, hyp_id: str) -> Report:
    with store(ws) as opened:
        # Classification is always the operator's own act, so its spend is the
        # interactive lane's: no daemon lane ever classifies.
        hyp = classify(ws, opened, hyp_id, lane=DEFAULT_LANE)
    # Classification refuses an id the catalogue does not hold, so this always resolves.
    runnable = catalogue(ws).get(_construct(hyp)).runnable
    stub = hypothesis_dir(ws, hyp_id) / STRATEGY_FILE
    data: dict[str, Any] = {
        "id": hyp.id,
        "path": str(ws.path("hypotheses", hyp_id, "hypothesis.yaml")),
        "construct": None if hyp.construct is None else hyp.construct.model_dump(mode="json"),
        "runnable": runnable,
        "objective": None if hyp.objective is None else hyp.objective.model_dump(mode="json"),
        "constraints": [item.model_dump(mode="json") for item in hyp.constraints or []],
        "strategy": str(stub),
    }
    lines = [
        field("classified", hyp.id),
        field("construct", _construct_line(hyp, runnable)),
        field("objective", _objective_line(hyp)),
        field("gates", ", ".join(item.id for item in hyp.constraints or []) or "none"),
    ]
    if hyp.construct is not None and hyp.construct.rationale:
        lines.append(indent(hyp.construct.rationale))
    lines.append(
        field("next", f"kanso research begin {hyp.id}")
        if runnable
        else field("next", f"this build cannot run a {_construct(hyp)}; see `kanso ext show`")
    )
    return Report(data=data, lines=tuple(lines))


def _construct(hyp: Hypothesis) -> str:
    """The construct the classification chose; a classified hypothesis always has one."""
    return "" if hyp.construct is None else hyp.construct.id


def _construct_line(hyp: Hypothesis, runnable: bool) -> str:
    host = (
        "" if hyp.construct is None or hyp.construct.host is None else f" on {hyp.construct.host}"
    )
    return f"{_construct(hyp)}{host}{'' if runnable else ' (not runnable in this build)'}"


def _objective_line(hyp: Hypothesis) -> str:
    if hyp.objective is None:  # pragma: no cover - a classified hypothesis carries one
        return "unclassified"
    params = hyp.objective.params
    return f"{hyp.objective.id} · min_delta {params.min_delta} · k_se {params.k_se}"
