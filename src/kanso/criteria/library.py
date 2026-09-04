"""The shipped toolbox, as data: what can be measured, and what a valid plan looks like.

Every objective and every gate is a YAML file declaring what it is, when it is
informative, which parameters it takes and the range each may be chosen from. That file —
not this module — is the catalogue: the classifier reads the objectives and the card-stage
gates from it, the planner reads the whole toolbox from it, and both choose values inside
the declared ranges without the framework ever supplying a default. Adding a gate is
adding a file and an implementation; nothing here enumerates them.

A range bound may be written against the run's own scale rather than in absolute units —
`1 * folds`, `100 * horizon` — so an item is written once and sized to each hypothesis.
Resolving those expressions is what makes a plan checkable: `min_positive_folds` is
between one and the fold count this workspace actually uses, whatever that is.

Plan validation enforces the structural invariants and nothing else. Every gate exists,
runs at the stage the toolbox says it runs at, and carries values inside its ranges; every
gate the toolbox marks required is present and none of them is excluded; and the plan
reaches all three stages. Which of the remaining gates to include, and with which values,
is the planner's decision and is not second-guessed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from kanso import __version__
from kanso.criteria.context import Gate
from kanso.criteria.gates import PENDING
from kanso.criteria.objectives import Objective, applicable, history_days, resolution_seconds
from kanso.errors import ValidationError
from kanso.schemas import (
    CertificationPlan,
    CriteriaItem,
    Hypothesis,
    ParamValue,
    RangeBound,
    is_duration,
    parse_bound,
    parse_duration,
    parse_yaml,
)
from kanso.schemas.certification import PLAN_STAGES

NO_IMPLEMENTATION: Final = "this version of kanso has no implementation for it"
"""Why a declared-but-unbuilt gate is refused, in the words the planner is retried with."""

LIBRARY: Final = Path(__file__).resolve().parent / "library"
"""One YAML file per toolbox item, named by its id."""


@cache
def _catalogue() -> Mapping[str, CriteriaItem]:
    items: dict[str, CriteriaItem] = {}
    for path in sorted(LIBRARY.glob("*.yaml")):
        item = parse_yaml(CriteriaItem, path.read_text(encoding="utf-8"), path.name)
        if item.id != path.stem:
            raise ValidationError(f"{path.name}: declares id {item.id!r}, so it is misfiled")
        items[item.id] = item
    return items


def catalogue() -> dict[str, CriteriaItem]:
    """Every shipped objective and gate, by id, in id order."""
    return dict(_catalogue())


@cache
def criteria_version() -> str:
    """The package version and a digest of the toolbox, so a run records what judged it."""
    digest = sha256()
    for path in sorted(LIBRARY.glob("*.yaml")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"{__version__}+{digest.hexdigest()[:12]}"


def _resolve(item: CriteriaItem) -> Any:
    module, _, attribute = item.impl.rpartition(".")
    try:
        found = getattr(import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        raise ValidationError(
            f"{item.id}: its implementation {item.impl} is missing: {exc}"
        ) from None
    if getattr(found, "id", None) != item.id:
        raise ValidationError(f"{item.id}: {item.impl} implements a different item")
    return found


@cache
def _objectives() -> Mapping[str, Objective]:
    return {item.id: _resolve(item) for item in _catalogue().values() if item.kind == "objective"}


@cache
def _gates() -> Mapping[str, Gate]:
    return {
        item.id: _resolve(item)
        for item in _catalogue().values()
        if item.kind == "gate" and item.id not in PENDING
    }


def plannable() -> dict[str, CriteriaItem]:
    """Every gate a plan written by this version may name, by id.

    A cert-stage gate the library declares but this version cannot run is absent: the
    plan's cert gates are run by certification itself, so planning one with no
    implementation would pin a proof that can never be produced. Paper and live gates are
    run by the stages rather than by certification, so they are offered whether or not
    their implementations exist yet.
    """
    runnable = _gates()
    return {
        item.id: item
        for item in _catalogue().values()
        if item.kind == "gate"
        and item.stage in PLAN_STAGES
        and (item.stage != "cert" or item.id in runnable)
    }


def pending_required() -> frozenset[str]:
    """Required gates this version cannot run, which a plan is therefore not held to.

    Empty is the only state a release may ship in: a required gate is a structural
    invariant, and one that never runs is a promise the certificate does not keep. It is
    non-empty only between the milestone that declares a gate and the one that implements
    it.
    """
    return frozenset(
        item.id
        for item in _catalogue().values()
        if item.kind == "gate" and item.required and item.id in PENDING
    )


def objectives() -> dict[str, Objective]:
    """The implemented objectives, by id."""
    return dict(_objectives())


def gates() -> dict[str, Gate]:
    """The implemented gates, by id; the ones a later milestone fills are absent."""
    return dict(_gates())


def applicable_objectives(hyp: Hypothesis, mode: str) -> list[tuple[int, str]]:
    """Every objective this hypothesis admits as `(priority, id)`; the lowest wins."""
    return applicable(list(_catalogue().values()), hyp, mode)


def resolve_bound(bound: RangeBound, hyp: Hypothesis, folds: int) -> float:
    """A declared bound as a number on this hypothesis's own scale."""
    coefficient, attribute = parse_bound(bound)
    match attribute:
        case None | "seconds":
            return coefficient
        case "horizon":
            return coefficient * parse_duration(hyp.horizon, "horizon").total_seconds()
        case "resolution":
            return coefficient * resolution_seconds(hyp.resolution)
        case "history_days":
            return coefficient * history_days(hyp)
        case _:
            return coefficient * folds


def _typed(kind: str, value: ParamValue) -> bool:
    match kind:
        case "bool":
            return isinstance(value, bool)
        case "int":
            return isinstance(value, int) and not isinstance(value, bool)
        case "float":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "duration":
            return isinstance(value, str) and is_duration(value)
        case _:
            return isinstance(value, str)


def _measured(kind: str, value: ParamValue) -> float:
    if kind == "duration":
        return parse_duration(str(value), "value").total_seconds()
    return float(value)


def check_params(
    item: CriteriaItem, params: Mapping[str, ParamValue], hyp: Hypothesis, folds: int
) -> list[str]:
    """Every way these chosen values depart from what the item declares."""
    problems: list[str] = []
    for name, value in params.items():
        kind = item.params.get(name)
        if kind is None:
            problems.append(f"{name}: {item.id} declares no such parameter")
            continue
        if not _typed(kind, value):
            problems.append(f"{name}: {value!r} is not a {kind}")
            continue
        bounds = item.ranges.get(name)
        if bounds is None:
            continue
        low, high = (resolve_bound(b, hyp, folds) for b in bounds)
        measured = _measured(kind, value)
        if not low <= measured <= high:
            problems.append(f"{name}: {value!r} is outside the range [{low:g}, {high:g}]")
    return problems


def plan_complaints(
    included: Sequence[Any],
    excluded: Sequence[Any],
    hyp: Hypothesis,
    folds: int,
) -> list[str]:
    """Every way a plan departs from the toolbox and the structural invariants.

    All of them at once, because the planner is allowed one retry: a plan corrected one
    complaint at a time would cost a call per mistake.

    A gate this version has no implementation for is not part of the toolbox a plan may
    draw on. It is neither offered to the planner, nor accepted from one, nor required of
    a plan even where the catalogue marks it a structural invariant, nor the planner's to
    exclude. Requiring a gate that cannot run would make every plan impossible; accepting
    one would let a certificate claim a test that never executed.
    """
    items = _catalogue()
    offered = plannable()
    complaints: list[str] = []
    named: list[str] = []
    for gate in included:
        item = items.get(gate.id)
        if item is None or item.kind != "gate":
            complaints.append(f"gates.{gate.id}: is not a gate in the toolbox")
            continue
        if gate.id not in offered:
            complaints.append(f"gates.{gate.id}: {NO_IMPLEMENTATION}, so a plan cannot name it")
            continue
        if item.stage != gate.stage:
            complaints.append(f"gates.{gate.id}: runs at the {item.stage} stage, not {gate.stage}")
        if gate.id in named:
            complaints.append(f"gates.{gate.id}: is named twice")
        else:
            named.append(gate.id)
        # Parameters are checked even when the stage is wrong: a plan gets one retry, so
        # a gate with two faults must come back with both of them.
        complaints.extend(
            f"gates.{gate.id}.{problem}" for problem in check_params(item, gate.params, hyp, folds)
        )
    complaints.extend(
        f"gates: no {stage} gate, and a plan reaches every stage"
        for stage in PLAN_STAGES
        if not any(gate.stage == stage for gate in included)
    )
    complaints.extend(
        f"gates: {item.id} is a structural invariant and every plan includes it"
        for item in offered.values()
        if item.required and item.id not in named
    )
    for left in excluded:
        item = items.get(left.id)
        if item is None or item.kind != "gate":
            complaints.append(f"excluded.{left.id}: is not a gate in the toolbox")
        elif left.id not in offered:
            complaints.append(
                f"excluded.{left.id}: {NO_IMPLEMENTATION}, so it was never yours to exclude"
            )
        elif item.required:
            complaints.append(f"excluded.{left.id}: is required and cannot be left out")
        elif left.id in named:
            complaints.append(f"excluded.{left.id}: is both included and excluded")
    return complaints


def validate_plan(plan: CertificationPlan | Mapping[str, Any], hyp: Hypothesis, folds: int) -> None:
    """Refuse a certification plan that breaks a structural invariant of the toolbox.

    The raising form of `plan_complaints`, which is the one rule set: two validators that
    had to agree would eventually not.
    """
    model = plan if isinstance(plan, CertificationPlan) else CertificationPlan.model_validate(plan)
    problems = plan_complaints(model.gates, model.excluded, hyp, folds)
    if problems:
        raise ValidationError("; ".join(problems))
