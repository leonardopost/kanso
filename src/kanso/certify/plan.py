"""The certification plan: one call that decides what would count as proof.

A hypothesis states an idea and research produces a candidate; the plan says what that
candidate must survive before it may hold capital. It is one model call, it is made once
per hypothesis, and its answer is pinned — so this module is small and almost all of it
is about what the call may see and what its answer must satisfy.

**The planner is blind to the research it is judging.** What it is shown is the closed
set the plan then records: the hypothesis, the construct it was classified as, the gate
toolbox with each gate's `meaningful_when`, parameters and ranges, what data the
workspace holds for the universe, how many trials the search has spent, and the
structural invariants. No card metric, no certificate and no strategy source is assembled
here or reachable from anything that is — a plan written against the results it will
judge proves nothing, and the only way to guarantee that is to never put them in the
prompt.

**The answer is checked before it is believed.** A gate that is not in the toolbox, one
planned at a stage it does not run at, a value outside its declared range, a missing
required gate, a stage the plan never reaches — each is a complaint, and the complaints go
back to the same model through the router's own check hook. That is what puts a
semantically wrong plan on the same retry ladder as a malformed one instead of raising
past it. There is no default plan and no fallback: a workspace with no configured model
refuses the step rather than inventing what counts as proof.

**A gate this version cannot run is not offered and may not be planned.** The package
declares gates whose implementations arrive with the machinery they need. A cert-stage
gate in that state is left out of the toolbox the planner is shown and refused if it is
named anyway, because certification runs its cert gates now and a plan naming one that
cannot run would pin a proof that can never be produced. It is, for the same reason, not
required of a plan written by this version. Paper and live gates are run by the stages
rather than here, so they are offered whether or not their implementations exist yet.

**The plan is pinned and changes only by replanning.** It lives at
`certificates/<hyp>/plan.yaml`; reading it again returns it rather than paying for a
second opinion, and a pinned plan this version can no longer run is refused with the
replan that would fix it. `replan` re-runs the planner over the same closed set of inputs,
as they read now, and mints the next `plan_version`; the plan a certificate names is
therefore always recoverable.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from math import isfinite, sqrt
from typing import TYPE_CHECKING, Any, Final, cast

from kanso.criteria import plan_complaints, plannable, resolve_bound
from kanso.data.manifest import manifests
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import Registration, show
from kanso.models import CallInputs, route
from kanso.research import records
from kanso.research.lanes import DEFAULT_LANE
from kanso.schemas import (
    CertificationPlan,
    CriteriaItem,
    DataAvailability,
    ExcludedGate,
    Hypothesis,
    PlanInputs,
    PlannedGate,
    Span,
    load_yaml,
    parse_yaml,
    render_duration,
    write_yaml,
)
from kanso.schemas.certification import PLAN_STAGES

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "PLAN_FILE",
    "SAMPLING_FLOOR",
    "availability",
    "certificates_dir",
    "complaints_about",
    "paper_window_note",
    "plan",
    "plan_file",
    "plannable",
    "read_plan",
]

TASK: Final = "certify_plan"
"""The task class this step calls; the router owns its tier, effort and output cap."""

CERTIFICATES: Final = "certificates"
PLAN_FILE: Final = "plan.yaml"
"""Where a hypothesis's plan is pinned, beside the certificates it will judge."""

PLANNED: Final = "planned"
"""The event kind this module appends, under the hypothesis id as subject."""

DAY_S: Final = 86_400.0
"""Seconds in a day; the windows are dated, so a window is a whole number of these."""

SAMPLING_FLOOR: Final = 0.1
"""The share of the certification window a paper window is warned for falling under.

Both are estimates of the same objective and their scatter goes as the inverse square root
of the window, so at a tenth the paper result is drawn from a distribution more than three
times as wide as the one the ninety-percent band was drawn from — and a version behaving
exactly as certified lands outside that band more often than inside it. The line is drawn
on a continuum, so it is drawn generously: it warns about the plans that are certainly
mis-sized rather than about the borderline ones.
"""

CERT_STAGE: Final = "cert"
"""The one plan stage certification itself runs, and so the one that needs an implementation."""

NO_IMPLEMENTATION: Final = (
    "this version of kanso has no implementation for it and it is not in the toolbox you "
    "were given, so leave it out"
)

_INSERT_PLAN: Final = """
INSERT INTO plans (hyp_id, plan_version, planned_at, planned_by, inputs, gates, excluded)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

Check = Callable[[Mapping[str, object]], Sequence[str]]
"""What the router runs on an answer that already satisfies the task class's schema."""

Answered = tuple[tuple[PlannedGate, ...], tuple[ExcludedGate, ...]]
"""One accepted answer: the gates the plan includes and the ones it leaves out."""


def certificates_dir(ws: Workspace, hyp_id: str) -> Path:
    """`certificates/<hyp>/`: the plan, the certificates and the certified sources."""
    return ws.path(CERTIFICATES, hyp_id)


def plan_file(ws: Workspace, hyp_id: str) -> Path:
    """Where this hypothesis's plan is pinned."""
    return certificates_dir(ws, hyp_id) / PLAN_FILE


def read_plan(ws: Workspace, hyp_id: str) -> CertificationPlan | None:
    """The pinned plan, or `None` when the hypothesis has none yet."""
    path = plan_file(ws, hyp_id)
    if not path.is_file():
        return None
    pinned = load_yaml(CertificationPlan, path)
    if pinned.hyp_id != hyp_id:
        raise ValidationError(
            f"{path}: is the plan of {pinned.hyp_id}, not of {hyp_id}",
            remedy=f"a plan lives under the hypothesis it plans; move it to {pinned.hyp_id}",
        )
    return pinned


def plan(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    replan: bool = False,
    lane: str = DEFAULT_LANE,
) -> CertificationPlan:
    """Plan one hypothesis's certification, or return the plan it already has.

    `replan` re-runs the planner and mints the next `plan_version`. `lane` is the lane
    the call's spend is attributed to.
    """
    registration = cast("Registration", show(ws, store, hyp_id))
    hyp = _pinned_hypothesis(store, registration)
    construct = hyp.construct
    if construct is None:
        raise PreconditionError(
            f"{hyp_id} is not classified, so there is no construct to plan a certification of",
            remedy=f"run `kanso classify {hyp_id}` first",
        )
    folds = ws.config.research.folds
    pinned = read_plan(ws, hyp_id)
    if pinned is not None and not replan:
        _refuse_unusable(pinned, hyp, folds)
        return pinned

    inputs = PlanInputs(
        hypothesis_sha=cast("str", registration.hypothesis_sha),
        construct=construct,
        data_availability=availability(ws, hyp),
        n_trials=records.n_trials(store, hyp_id),
    )
    accepted: list[Answered] = []

    def check(data: Mapping[str, object]) -> Sequence[str]:
        answered, complaints = _read(data, hyp=hyp, folds=folds)
        if answered is not None and not complaints:
            accepted.append(answered)
        return complaints

    answer = route(ws, store, TASK, _call_inputs(hyp, inputs, folds, check), lane=lane)
    included, excluded = accepted[-1]
    written = CertificationPlan(
        hyp_id=hyp_id,
        plan_version=_next_version(store, hyp_id, pinned),
        planned_at=datetime.now(tz=UTC),
        planned_by=answer.model,
        inputs=inputs,
        gates=list(included),
        excluded=list(excluded),
    )
    _pin(ws, store, written)
    return written


# -- what the planner is shown ------------------------------------------------


def availability(ws: Workspace, hyp: Hypothesis) -> DataAvailability:
    """What data the workspace holds for this hypothesis's universe, by type.

    Datasets of the same type are reported as one span, because what a plan needs from
    availability is how much history each kind of data reaches over — how long a paper
    window can be, how many folds a walk-forward has, whether a volume series exists at
    all. Spans are whole UTC days, each named by the instant the day opens.
    """
    universe = set(hyp.universe)
    spans: dict[str, tuple[date, date]] = {}
    for held in manifests(ws).values():
        if held.instrument not in universe:
            continue
        seen = spans.get(held.type)
        spans[held.type] = (
            held.span if seen is None else (min(seen[0], held.start), max(seen[1], held.end))
        )
    return DataAvailability(
        types=sorted(spans),
        spans={
            name: Span(start=_midnight(low), end=_midnight(high))
            for name, (low, high) in sorted(spans.items())
        },
    )


def _call_inputs(hyp: Hypothesis, inputs: PlanInputs, folds: int, check: Check) -> CallInputs:
    """The call's two halves: the idea and the toolbox, then what has moved since.

    The hypothesis, its construct, the toolbox and the invariants do not change while one
    hypothesis is planned, so they are the cached prefix; what the workspace holds and how
    many trials the search has spent are what a replan sees differently.
    """
    return CallInputs(
        subject=hyp.id,
        stable={
            "hypothesis": _document(hyp),
            "construct": inputs.construct.model_dump(mode="json", by_alias=True, exclude_none=True),
            "toolbox": _toolbox(hyp, folds),
            "invariants": _invariants(),
        },
        dynamic={
            "data_availability": inputs.data_availability.model_dump(mode="json"),
            "n_trials": inputs.n_trials,
        },
        check=check,
    )


def _document(hyp: Hypothesis) -> dict[str, Any]:
    """The hypothesis as the planner reads it: the idea and its windows, no results.

    The construct is excluded here because it is stated on its own beside it, and the
    file holds nothing a run measured — status, best card and pins live in the state
    store, never in these bytes.
    """
    return hyp.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude={"schema_", "construct_"}
    )


def _toolbox(hyp: Hypothesis, folds: int) -> list[dict[str, Any]]:
    """Every plannable gate: what it means, what it takes and what it may be given."""
    return [
        {
            "id": item.id,
            "stage": item.stage,
            "required": bool(item.required),
            "meaningful_when": item.meaningful_when,
            "params": dict(item.params),
            "ranges": _ranges(item, hyp, folds),
        }
        for item in plannable().values()
    ]


def _ranges(item: CriteriaItem, hyp: Hypothesis, folds: int) -> dict[str, dict[str, float | None]]:
    """Each declared range as two numbers on this hypothesis's own scale.

    An unbounded end arrives as `null`, because a prompt is JSON and JSON has no
    infinity: emitting one would make the whole call unparseable rather than generous.
    """
    return {
        name: {
            "min": _finite(resolve_bound(low, hyp, folds)),
            "max": _finite(resolve_bound(high, hyp, folds)),
        }
        for name, (low, high) in item.ranges.items()
    }


def _finite(value: float) -> float | None:
    return value if isfinite(value) else None


def _invariants() -> dict[str, Any]:
    """The structural invariants, as the facts they are, rather than as prose to infer."""
    offered = plannable()
    return {
        "required_gates": sorted(item.id for item in offered.values() if item.required),
        "stages": list(PLAN_STAGES),
        "rules": [
            "every gate included is one of the toolbox gates above, at the stage the "
            "toolbox gives it",
            "every parameter value lies inside that parameter's stated range",
            "every required gate is included, and none of them is excluded",
            "the plan includes at least one gate at each of the cert, paper and live stages",
            "no gate is named twice, and no gate is both included and excluded",
        ],
    }


# -- what the answer must satisfy ---------------------------------------------


def _read(
    data: Mapping[str, object], *, hyp: Hypothesis, folds: int
) -> tuple[Answered | None, list[str]]:
    """One answer, read as a plan, and everything wrong with it.

    The gates are built first, so a value the data model cannot hold is one complaint
    rather than an exception; the toolbox, the stages and the ranges are checked on the
    objects that survived.
    """
    try:
        included = tuple(
            PlannedGate.model_validate(_object(entry)) for entry in _sequence(data.get("gates"))
        )
        excluded = tuple(
            ExcludedGate.model_validate(_object(entry)) for entry in _sequence(data.get("excluded"))
        )
    except ValidationError as exc:
        return None, [f"the answer is not a plan: {exc.message}"]
    complaints = complaints_about(included, excluded, hyp, folds)
    if complaints:
        return None, complaints
    return (included, excluded), []


def complaints_about(
    included: Sequence[PlannedGate],
    excluded: Sequence[ExcludedGate],
    hyp: Hypothesis,
    folds: int,
) -> list[str]:
    """Every way a plan departs from the toolbox, collected rather than raised.

    The rules live in the toolbox, which is the one place that knows what a gate is and
    whether this version can run it. This is the planner's view of them: the router's
    retry needs the whole list at once, so a plan is corrected in one call rather than
    one mistake per call.
    """
    return plan_complaints(included, excluded, hyp, folds)


# -- the paper window a plan implies ------------------------------------------


def paper_window_note(ws: Workspace, store: StateStore, pinned: CertificationPlan) -> str | None:
    """Say so when this plan's paper window is too short to be judged by its own band.

    A paper gate compares the objective realised on the stage against the interval
    composition measured over the *certification* window, and the two are estimates of the
    same quantity at different precision: the shorter the paper window, the wider the
    result scatters around a band that does not widen with it. The scatter goes as the
    square root of the ratio, so a paper window a tenth of the certification window is
    roughly three times noisier than the band it must fall inside, and a version that is
    behaving exactly as certified reads as drifted more often than not.

    It is a note and not a refusal. The plan is the planner's, the windows are the
    operator's, and a short paper window is a defensible choice — it is only one made in
    the dark when nobody said what it costs. `None` when the plan asks for no paper window
    at all, which `paper_forward` reports for itself when it runs.
    """
    hyp = _pinned_hypothesis(store, cast("Registration", show(ws, store, pinned.hyp_id)))
    paper = pinned.paper_window_s(hyp.horizon)
    certification = _window_s(hyp)
    if paper is None or paper >= SAMPLING_FLOOR * certification:
        return None
    return (
        f"paper window {_rendered(paper)} against a {_rendered(certification)} certification "
        f"window · the paper objective is about {sqrt(certification / paper):.0f}x noisier than "
        "the band it is judged against, so a version behaving as certified can read as drifted "
        "· raise min_duration or horizon_mult, or narrow windows.certification"
    )


def _window_s(hyp: Hypothesis) -> float:
    """The certification window in seconds, counting both of the days it closes on."""
    window = hyp.windows.certification
    return ((window.end - window.start).days + 1) * DAY_S


def _rendered(seconds: float) -> str:
    """A span of seconds in the duration grammar a workspace file writes them in."""
    return render_duration(timedelta(seconds=round(seconds)))


def _refuse_unusable(pinned: CertificationPlan, hyp: Hypothesis, folds: int) -> None:
    """Refuse a pinned plan this version can no longer run, naming the way out."""
    problems = complaints_about(pinned.gates, pinned.excluded, hyp, folds)
    if problems:
        raise ValidationError(
            f"the plan pinned for {pinned.hyp_id} is not one this kanso can run: "
            + "; ".join(problems),
            remedy=f"run `kanso cert plan {pinned.hyp_id} --replan` to plan again",
        )


# -- pinning ------------------------------------------------------------------


def _pinned_hypothesis(store: StateStore, registration: Registration) -> Hypothesis:
    """The hypothesis as its pin holds it, which is what `hypothesis_sha` names.

    The workspace file may have been edited since; the plan records the bytes the planner
    was shown, so those bytes are what it is shown.
    """
    sha = registration.hypothesis_sha
    if sha is None or not store.has_blob(sha):
        raise PreconditionError(
            f"{registration.hyp_id} has no pinned hypothesis to plan against",
            remedy=f"run `kanso hyp add {registration.path}` to pin it",
        )
    return parse_yaml(Hypothesis, store.get_blob(sha).decode("utf-8"), f"{registration.hyp_id} pin")


def _next_version(store: StateStore, hyp_id: str, pinned: CertificationPlan | None) -> int:
    """One past the highest version this workspace has recorded or pinned."""
    row = store.connection.execute(
        "SELECT COALESCE(MAX(plan_version), 0) FROM plans WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return max(int(row[0]), 0 if pinned is None else pinned.plan_version) + 1


def _pin(ws: Workspace, store: StateStore, written: CertificationPlan) -> None:
    """Write the plan, record the version and say so, in that order."""
    directory = certificates_dir(ws, written.hyp_id)
    directory.mkdir(parents=True, exist_ok=True)
    write_yaml(written, directory / PLAN_FILE)
    store.connection.execute(
        _INSERT_PLAN,
        (
            written.hyp_id,
            written.plan_version,
            written.planned_at.isoformat(),
            written.planned_by,
            json.dumps(written.inputs.model_dump(mode="json", by_alias=True)),
            json.dumps([gate.model_dump(mode="json") for gate in written.gates]),
            json.dumps([left.model_dump(mode="json") for left in written.excluded]),
        ),
    )
    store.event(
        PLANNED,
        written.hyp_id,
        {
            "plan_version": written.plan_version,
            "planned_by": written.planned_by,
            "gates": [gate.id for gate in written.gates],
        },
    )


def _midnight(day: date) -> datetime:
    """The instant a UTC day opens."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []
