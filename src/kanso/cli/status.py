"""`kanso status`: the one screen an operator reads to know how the workbench is doing.

Six facts, chosen because each answers a question the others cannot. What the lanes are
doing says whether research is happening at all. Cards per hour says how fast, in the unit
the loop actually produces. The best metric per hypothesis says whether the speed is
buying anything. Today's spend says what that cost. Unread escalations say what is waiting
for a person. And a hypothesis whose baseline would not run is the one failure mode that
is silent everywhere else: it never reaches a lane, so it would otherwise sit in the queue
producing nothing without ever producing an error either.

The rate is measured over the trailing hour rather than averaged over a run, because the
question it answers is "is it working *now*" — an average over a day would still look
healthy an hour after every lane stopped. Spend is the UTC day, which is the day the
ledger's own timestamps are in.

Nothing here writes. `status` is safe to run against a workspace a daemon is working in,
and safe to run in a loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

import typer

from kanso import inbox, models
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.env import read as read_envelope
from kanso.research import daemon, loop, records, scheduler
from kanso.schemas import RunRecord
from kanso.state import StateStore
from kanso.workspace import Workspace

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]

WINDOW: Final = timedelta(hours=1)
"""The trailing window the card rate is measured over."""

TOP: Final = 5
"""How many escalations the human view lists before it stops and gives the count."""


def status_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Lanes, card rate, best metric per hypothesis, today's spend and what is waiting."""
    emit(as_json or global_json(ctx), lambda: _status(open_workspace(ctx)))


# -- command bodies ---------------------------------------------------------------


def _status(ws: Workspace) -> Report:
    with store(ws) as opened:
        pid = daemon.pid_of(ws)
        planned = _planned_lanes(ws)
        active = daemon.active_runs(opened)
        lanes = _lanes(planned, active, opened)
        today = datetime.now(tz=UTC).date()
        spent = models.spend(opened, day=today)
        unread = inbox.unread(opened)
        data: dict[str, Any] = {
            "workspace": str(ws.root),
            "daemon": {"running": pid is not None, "pid": pid},
            "lanes": lanes,
            "cards_per_hour": _cards_per_hour(opened),
            "hypotheses": _hypotheses(opened),
            "spend_today": {
                "day": today.isoformat(),
                "calls": spent.calls,
                "tokens_in": spent.tokens_in,
                "tokens_out": spent.tokens_out,
                "cost": round(spent.cost, 6),
                "by_lane": {name: round(cost, 6) for name, cost in spent.by_lane.items()},
            },
            "escalations": {
                "unread": len(unread),
                "entries": [entry.payload() for entry in unread],
            },
            "baseline_failed": _baseline_failed(opened),
            "queued": len(scheduler.queued(opened)),
        }
    return Report(data=data, lines=_lines(data))


def _planned_lanes(ws: Workspace) -> list[str]:
    """The lanes the envelope allows, or none when the workspace has no envelope yet."""
    if read_envelope(ws) is None:
        return []
    return list(daemon.lane_names(ws))


def _lanes(planned: list[str], active: list[RunRecord], opened: StateStore) -> list[dict[str, Any]]:
    """One row per lane: the planned ones, plus any lane holding a run of its own.

    The interactive lane is not in the envelope's plan and an operator's run is real
    research, so a lane with a run in it is reported whether or not the daemon would ever
    have started one there.
    """
    by_lane = {run.lane: run for run in active}
    names = list(dict.fromkeys([*planned, *sorted(by_lane)]))
    rows: list[dict[str, Any]] = []
    for name in names:
        run = by_lane.get(name)
        rows.append(
            {
                "lane": name,
                "planned": name in planned,
                "id": None if run is None else run.hyp_id,
                "run_id": None if run is None else run.run_id,
                "cards": 0 if run is None else _cards_in_run(opened, run.run_id),
                "best_sha": None if run is None else run.best_sha,
                "best_metric": None if run is None else run.best_metric,
            }
        )
    return rows


def _cards_in_run(opened: StateStore, run_id: str) -> int:
    row = opened.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE run_id = ?", (run_id,)
    ).fetchone()
    return int(row[0])


def _cards_per_hour(opened: StateStore) -> float:
    """Cards recorded in the trailing hour: the rate, measured rather than averaged."""
    since = (datetime.now(tz=UTC) - WINDOW).isoformat()
    row = opened.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE created_at >= ?", (since,)
    ).fetchone()
    return float(row[0])


def _hypotheses(opened: StateStore) -> list[dict[str, Any]]:
    """Every registered hypothesis with its status, its best metric and its card count."""
    rows = opened.connection.execute(
        "SELECT h.hyp_id, h.status, h.best_sha, h.best_metric,"
        " h.consecutive_cert_failures AS cert_failures,"
        " (SELECT COUNT(*) FROM cards c WHERE c.hyp_id = h.hyp_id) AS cards"
        " FROM hypotheses h ORDER BY h.hyp_id"
    ).fetchall()
    return [
        {
            "id": str(row["hyp_id"]),
            "status": str(row["status"]),
            "best_sha": None if row["best_sha"] is None else str(row["best_sha"]),
            "best_metric": None if row["best_metric"] is None else float(row["best_metric"]),
            "cards": int(row["cards"]),
            "cert_failures": int(row["cert_failures"]),
        }
        for row in rows
    ]


def _baseline_failed(opened: StateStore) -> list[dict[str, Any]]:
    """Hypotheses whose last attempt to begin a run died on the baseline.

    A later run that did begin clears the entry, so what is listed is what is still
    stuck: an id here has nothing running, nothing to run, and no error anywhere an
    operator would otherwise look.
    """
    failures: dict[str, dict[str, Any]] = {}
    for event in opened.events(kind=loop.BASELINE_FAILED):
        failures[event.subject] = {
            "id": event.subject,
            "at": event.ts,
            "reason": str(event.detail.get("reason", "")),
        }
    stuck: list[dict[str, Any]] = []
    for hyp_id, entry in sorted(failures.items()):
        runs = records.runs_of(opened, hyp_id)
        if any(run.started_at.isoformat() > str(entry["at"]) for run in runs):
            continue
        stuck.append(entry)
    return stuck


# -- the human view ---------------------------------------------------------------


def _lines(data: dict[str, Any]) -> tuple[str, ...]:
    daemon_state = data["daemon"]
    running = f"running (pid {daemon_state['pid']})" if daemon_state["running"] else "stopped"
    lines = [
        field("workspace", data["workspace"]),
        field("daemon", f"{running} · {data['queued']} queued"),
        field("lanes", _lane_summary(data["lanes"])),
    ]
    lines += [indent(_lane_line(row)) for row in data["lanes"]]
    lines.append(field("cards/h", f"{data['cards_per_hour']:.0f} in the last hour"))
    lines.append(field("best", f"{len(data['hypotheses'])} hypothesis(es)"))
    lines += [indent(_hypothesis_line(row)) for row in data["hypotheses"]]
    spend = data["spend_today"]
    lines.append(
        field("spend", f"${spend['cost']:.4f} today · {spend['calls']} call(s) · {spend['day']}")
    )
    escalations = data["escalations"]
    lines.append(field("inbox", f"{escalations['unread']} unread"))
    lines += [indent(_escalation_line(entry)) for entry in escalations["entries"][:TOP]]
    if data["baseline_failed"]:
        lines.append(
            field("baseline", ", ".join(str(row["id"]) for row in data["baseline_failed"]))
        )
    return tuple(lines)


def _lane_summary(rows: list[dict[str, Any]]) -> str:
    busy = sum(1 for row in rows if row["id"] is not None)
    return f"{busy}/{len(rows)} working" if rows else "none (run `kanso env detect`)"


def _lane_line(row: dict[str, Any]) -> str:
    if row["id"] is None:
        return f"{row['lane']:<5}idle"
    best = "no keep yet" if row["best_metric"] is None else f"best {row['best_metric']:.6f}"
    return f"{row['lane']:<5}{row['id']} · {row['cards']} card(s) · {best}"


def _hypothesis_line(row: dict[str, Any]) -> str:
    best = "no keep yet" if row["best_metric"] is None else f"{row['best_metric']:.6f}"
    return f"{row['id']:<20}{row['status']:<13}{best:<14}{row['cards']} card(s)"


def _escalation_line(entry: dict[str, Any]) -> str:
    return f"{entry['id']} {entry['kind']} {entry['subject']} — {entry['summary']}"
