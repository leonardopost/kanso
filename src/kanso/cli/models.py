"""`kanso models`: the register, and whether the models on it answer.

`check` prints the register as the router reads it — which model serves which tier, which
task class is routed where, and at what thinking effort and output cap — and then makes
one minimal call to every configured model. The call is a real call: it is ledgered like
any other, because it costs what a call costs.

A tier with no model behind it is refused before any call: a register the router cannot
climb is broken however well the models it does list answer, and finding that out costs
nothing.

A model that does not answer is reported rather than raised: the point of the command is
to say which half of a register works, and a first entry that is misconfigured must not
hide the state of the rest. The command therefore exits 2 when any model failed and 0 when
they all answered, which is the same shape `doctor` uses for a workspace that is not well.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from kanso import models
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.errors import Exit
from kanso.workspace import Workspace

app = typer.Typer(help="The model register and the models on it.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]


@app.command("check")
def check_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Print the register and make one minimal call per model. Exits 2 on a failure."""
    emit(as_json or global_json(ctx), lambda: _check(open_workspace(ctx)))


# -- command bodies ---------------------------------------------------------------


def _check(ws: Workspace) -> Report:
    # A tier with no model behind it is a register no router could use, whatever the
    # models on it answer, so it is refused before a call is paid for.
    models.tiers_covered(ws)
    register = models.read_register(ws)
    with store(ws) as opened:
        results = models.check(ws, opened)
    failed = [result for result in results if not result.ok]
    routing = {task: route.model_dump(mode="json") for task, route in register.routes().items()}
    data: dict[str, Any] = {
        "ok": not failed,
        "register": str(ws.path(models.REGISTER_NAME)),
        "routing": routing,
        "models": [
            {
                "id": result.id,
                "provider": result.provider,
                "protocol": result.protocol,
                "tiers": list(result.tiers),
                "ok": result.ok,
                "latency_ms": round(result.latency_ms, 1),
                "detail": result.detail,
            }
            for result in results
        ],
    }
    lines = [field("register", ws.path(models.REGISTER_NAME))]
    lines += [
        indent(
            f"{task:<13}{route['tier']:<9}{route['effort']:<7}≤{route['max_output']} tokens",
        )
        for task, route in routing.items()
    ]
    for result in results:
        mark = "ok" if result.ok else "fail"
        lines.append(
            field(
                mark,
                f"{result.id} · {result.protocol} · {'/'.join(result.tiers)} · "
                f"{result.latency_ms:.0f}ms · {result.detail}",
            )
        )
    lines.append(field("models", f"{len(results) - len(failed)}/{len(results)} answered"))
    code = Exit.PRECONDITION if failed else Exit.OK
    return Report(data=data, lines=tuple(lines), code=code)
