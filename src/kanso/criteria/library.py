"""The shipped toolbox, as data: what can be measured, and what a valid plan looks like.

Every objective and every gate is a YAML file declaring what it is, when it is
informative, which parameters it takes and the range each may be chosen from. That file —
not this module — is the catalogue: the classifier reads the objectives and the card-stage
gates from it, the planner reads the whole toolbox from it, and both choose values inside
the declared ranges without the framework ever supplying a default. Adding a gate is
adding a file and an implementation; nothing here enumerates them.

Two structural facts are declared there too, and held here. A gate whose `together` is set
is named with every parameter it declares or not at all, because half a rule judges
nothing and a gate that judges nothing passes (`check_params`). A card gate whose
`required_for` names an objective is carried by every hypothesis scored on it, with all
its parameters set (`required_card_gates`): an objective struck on the book rewards
trading less once every trade loses, and the gate that refuses that is part of what the
objective means, not a choice about this hypothesis. A cert gate whose `required_for` names
it is carried by every certification plan for such a hypothesis, in the same way
(`required_cert_gates`): an absolute objective credits a drift held as if it were timed, and
`entry_timing` is what tells the two apart.

A cert gate whose file says `reads: research` claims that what it judges is the research
window's runs, the card measurements the store keeps and the trial counts, never the
certification window (`research_gates`): the three deflation gates, `walk_forward_consistency`
and `research_plateau`. Certification judges those first, on research data, and a stall whose
best one of them refuses reads no certification window (`kanso.certify.run`). The claim is the
file's, and `tests/criteria/test_library.py` holds every such gate to it by judging each on a
context whose certification slots hold an unrelated run.

A range bound may be written against the run's own scale rather than in absolute units —
`1 * folds`, `100 * horizon` — so an item is written once and sized to each hypothesis.
Resolving those expressions is what makes a plan checkable: `min_positive_folds` is
between one and the fold count this workspace actually uses, whatever that is.

Plan validation enforces the structural invariants and nothing else. Every gate exists,
runs at the stage the toolbox says it runs at, and carries values inside its ranges; every
gate the toolbox marks required is present and none of them is excluded; the plan reaches
all three stages; every cert gate `required_for` the hypothesis's objective is present, with
every parameter it declares set, and not excluded; and once the hypothesis's `n_trials` —
every card it has, the count a certificate is stamped with — and the `prior_trials` its
family declares tried outside kanso together reach `SEARCH_TRIALS`, the plan includes the
gate that
`deflates` its objective, with its threshold set — a result selected from a search is
priced for the search, and that is a structural fact about selection, not a judgement
about this hypothesis. Which of the remaining gates to include, and with which values —
that gate's threshold among them — is the planner's decision and is not second-guessed
here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from kanso.schemas.screen import hours_minutes

NO_IMPLEMENTATION: Final = "this version of kanso has no implementation for it"
"""Why a declared-but-unbuilt gate is refused, in the words the planner is retried with."""

COST_SCENARIO: Final = "cost_scenario"
"""The gate that re-prices a run's recorded fills under a schedule its parameters state."""

LIBRARY: Final = Path(__file__).resolve().parent / "library"
"""One YAML file per toolbox item, named by its id."""

SEARCH_TRIALS: Final = 2
"""From how many draws a plan must correct for the search — `n_trials`, the cards, and the
`prior_trials` the family declares tried outside kanso, which every caller adds: two draws are
already a choice of one over another, and the maximum of two is above the mean of either."""

KIND_WORDS: Final = {
    "hours": "span of clock times on one day, HH:MM-HH:MM",
    "zone": "time zone this host knows",
}
"""How a refusal names a parameter type whose id alone would not say what to write."""


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


def deflation_gate(objective_id: str) -> str | None:
    """The gate that corrects a search scored on this objective, as the toolbox declares it.

    Read from the gates' `deflates` lists, which together claim every objective exactly
    once; `None` for an id no gate claims.
    """
    return next(
        (
            item.id
            for item in _catalogue().values()
            if item.kind == "gate" and objective_id in (item.deflates or ())
        ),
        None,
    )


def research_gates() -> frozenset[str]:
    """The cert gates judged on research data before the certification window is read, as the
    toolbox declares them with `reads: research`; no id is listed here."""
    return frozenset(
        item.id for item in _catalogue().values() if item.kind == "gate" and item.reads
    )


def required_card_gates(objective_id: str) -> tuple[str, ...]:
    """The card gates every hypothesis scored on this objective carries, with every parameter
    set, as the toolbox declares them in their `required_for` lists; in id order. A cert gate
    that guards the objective is a plan's to carry and never one of these."""
    return _guarding(objective_id, "card")


def required_cert_gates(objective_id: str) -> tuple[str, ...]:
    """The cert gates every certification plan for a hypothesis scored on this objective
    carries, with every parameter set, as the toolbox declares them in their `required_for`
    lists; in id order."""
    return _guarding(objective_id, "cert")


def _guarding(objective_id: str, stage: str) -> tuple[str, ...]:
    """The gates of one stage whose `required_for` names this objective, in id order."""
    return tuple(
        item.id
        for item in _catalogue().values()
        if item.kind == "gate" and item.stage == stage and objective_id in (item.required_for or ())
    )


def required_deflation(hyp: Hypothesis, n_trials: int) -> str | None:
    """The deflation gate a plan for this hypothesis must include, or `None` when it need
    include none: before the search's second draw — `n_trials` is the cards and the declared
    `prior_trials` together, as the caller counts them — or with no objective to deflate."""
    if hyp.objective is None or n_trials < SEARCH_TRIALS:
        return None
    return deflation_gate(hyp.objective.id)


def objectives() -> dict[str, Objective]:
    """The implemented objectives, by id."""
    return dict(_objectives())


def gates() -> dict[str, Gate]:
    """The implemented gates, by id; the ones a later milestone fills are absent."""
    return dict(_gates())


def applicable_objectives(hyp: Hypothesis, mode: str) -> list[tuple[int, str]]:
    """Every objective this hypothesis admits as `(priority, id)`, in the order listed.

    The set an objective is named from — by a classifier or by the operator — and never a
    choice within it: the priority orders the list and selects nothing."""
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
        case "hours":
            return isinstance(value, str) and _is_hours(value)
        case "zone":
            return isinstance(value, str) and _is_zone(value)
        case _:
            return isinstance(value, str)


def _is_hours(value: str) -> bool:
    try:
        hours_minutes(value)
    except ValidationError:
        return False
    return True


def _is_zone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _in_universe(kind: str, value: ParamValue, hyp: Hypothesis) -> bool:
    """An `instrument` names one of the hypothesis's own universe ids; any other kind may."""
    return kind != "instrument" or value in hyp.universe


def _measured(kind: str, value: ParamValue) -> float:
    if kind == "duration":
        return parse_duration(str(value), "value").total_seconds()
    return float(value)


_YOURS: Final = "; the values are yours, inside their ranges"
"""Said by `check_params` beside a parameter a `together` item lacks, to whoever chose the
values — the planner, the classifier, the operator's own `required_constraints` — and left
off what is said of a plan already pinned (`as_pinned`)."""


def as_pinned(problem: str) -> str:
    """A `check_params` complaint as said of a plan already pinned, without telling its reader
    the values are theirs: they are the planner's, and the way out is a replan. What the
    monitor's outcome and `cert plan`'s and `cert run`'s refusal of a pinned plan say."""
    return problem.removesuffix(_YOURS)


def check_params(
    item: CriteriaItem, params: Mapping[str, ParamValue], hyp: Hypothesis, folds: int
) -> list[str]:
    """Every way these chosen values depart from what the item declares.

    A value is checked for its declared type — an `hours` span and a `zone` for parsing as
    one — then, for an `instrument`, for naming one of the hypothesis's own universe ids,
    then for its range. The universe check lives here rather than in one caller because
    the classifier's answer, the operator's own `required_constraints` and a certification
    plan all pass through this function, and a leg the hypothesis does not trade is wrong
    from whichever of them it came. An item whose parameters mean nothing apart
    (`together`) is held here to setting every one, for the same reason: `edge_per_trade`
    given a `min_bps` and no `k_se` skipped every card it was asked to judge.
    """
    problems: list[str] = []
    if item.together:
        problems.extend(
            f"{name}: is not set, and {item.id} judges nothing without every one of "
            f"{', '.join(item.params)}{_YOURS}"
            for name in item.params
            if name not in params
        )
    for name, value in params.items():
        kind = item.params.get(name)
        if kind is None:
            problems.append(f"{name}: {item.id} declares no such parameter")
            continue
        if not _typed(kind, value):
            problems.append(f"{name}: {value!r} is not a {KIND_WORDS.get(kind, kind)}")
            continue
        if not _in_universe(kind, value, hyp):
            problems.append(f"{name}: {value!r} is not in the universe ({', '.join(hyp.universe)})")
            continue
        bounds = item.ranges.get(name)
        if bounds is None:
            continue
        outside = _outside(name, kind, value, bounds, hyp, folds)
        if outside is not None:
            problems.append(outside)
    return problems


def check_folds(
    item: CriteriaItem, params: Mapping[str, ParamValue], hyp: Hypothesis, folds: int
) -> list[str]:
    """What `check_params` refuses of these values under `folds` through a range one of whose
    bounds scales with `folds`, and nothing else it would.

    Of everything a file was validated against, `[research] folds` is the one the workspace
    can move under a pin it has made: the values, their types, the universe and every other
    bound are the file's, and a toolbox rule added since — `together`, say — is held to the
    file where it is registered, not where it was pinned before the rule. So this is what a
    pinned file is held to under the folds it is run at.
    """
    problems: list[str] = []
    for name, value in params.items():
        kind = item.params.get(name)
        bounds = item.ranges.get(name)
        if kind is None or bounds is None or not _typed(kind, value):
            continue
        if all(parse_bound(bound)[1] != "folds" for bound in bounds):
            continue
        outside = _outside(name, kind, value, bounds, hyp, folds)
        if outside is not None:
            problems.append(outside)
    return problems


def _outside(
    name: str,
    kind: str,
    value: ParamValue,
    bounds: tuple[RangeBound, RangeBound],
    hyp: Hypothesis,
    folds: int,
) -> str | None:
    """Why a typed value lies outside its declared range on this hypothesis's scale, or
    `None` where it lies inside."""
    low, high = (resolve_bound(b, hyp, folds) for b in bounds)
    measured = _measured(kind, value)
    if low <= measured <= high:
        return None
    return f"{name}: {value!r} is outside the range [{low:g}, {high:g}]"


def _positive(value: object) -> bool:
    """Whether a parameter is a number above zero."""
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def plan_complaints(
    included: Sequence[Any],
    excluded: Sequence[Any],
    hyp: Hypothesis,
    folds: int,
    n_trials: int,
) -> list[str]:
    """Every way a plan departs from the toolbox and the structural invariants.

    All of them at once, because the planner is allowed one retry: a plan corrected one
    complaint at a time would cost a call per mistake.

    `n_trials` is the hypothesis's count of cards, every one of every run — the count a
    certificate is stamped with — and the `prior_trials` its family declares tried outside
    kanso, added by the caller. From `SEARCH_TRIALS` on, the gate that `deflates` the
    hypothesis's objective is held to more than a required gate is — present, not excluded,
    and with every parameter it declares set — because the result a certificate judges is
    then the best of a search, nothing else in a plan prices one, and a deflation gate with
    no threshold skips, which passes. The threshold's value stays the planner's. The gate
    itself takes its expected maximum over every card that ran and traded and every setting
    the family declares, and its spread from the narrower set of `trial_metrics`;
    certification refuses a subject behind two such draws — one card behind a declared grid
    among them — with fewer than two of these (`kanso.certify.run.unpriced`), since that
    spread cannot be read, and the gate skips only where the search has fewer than two draws
    at all: then nothing was chosen over anything.

    A cert gate the toolbox declares `required_for` the hypothesis's objective is held as the
    search correction is, from the first plan on: present, not excluded, and with every
    parameter it declares set, since it is part of what that objective means.

    A `cost_scenario` on a hypothesis charged a dated spread states a `fixed_bps` above zero. A
    scenario's parameters are numbers and cannot carry a table, and one that leaves the width
    out, or states it at zero, charges no spread, so it would re-price every fill of a run that
    paid its table's at none.

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
    correction = required_deflation(hyp, n_trials)
    searched = (
        f"is the search correction for {hyp.objective.id}, and a plan includes it once "
        "n_trials, every card of the hypothesis, and the prior_trials its family declares "
        f"reach {SEARCH_TRIALS} together (they are {n_trials})"
        if correction is not None and hyp.objective is not None
        else ""
    )
    if correction in offered and correction not in named:
        complaints.append(f"gates: {correction} {searched}")
    elif correction in offered:
        chosen = next(gate.params for gate in included if gate.id == correction)
        complaints.extend(
            f"gates.{correction}.{name}: is not set, and the search correction judges nothing "
            "without its threshold; the value is yours, inside its range"
            for name in offered[correction].params
            if name not in chosen
        )
    # A guarding gate declares `together`, so `check_params` has already held one that is
    # named to every parameter it declares; what is left is that it is named at all.
    guarded = () if hyp.objective is None else required_cert_gates(hyp.objective.id)
    guards = "" if hyp.objective is None else f"is required for {hyp.objective.id}, its objective"
    complaints.extend(
        f"gates: {gate_id} {guards}, and every plan for it includes the gate with every "
        "parameter set; the values are yours, inside their ranges"
        for gate_id in guarded
        if gate_id in offered and gate_id not in named
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
        elif left.id == correction:
            complaints.append(f"excluded.{left.id}: {searched}, so it cannot be left out")
        elif left.id in guarded:
            complaints.append(f"excluded.{left.id}: {guards}, so it cannot be left out")
        elif left.id in named:
            complaints.append(f"excluded.{left.id}: is both included and excluded")
    if hyp.costs is not None and hyp.costs.spread == "dated":
        complaints.extend(
            f"gates.{gate.id}.fixed_bps: is {gate.params.get('fixed_bps', 'not set')}, and "
            f"{hyp.id} is charged a dated spread, which a scenario cannot state: one without a "
            "fixed_bps above zero re-prices every fill at no spread at all; the width is yours, "
            "above zero and inside its range"
            for gate in included
            if gate.id == COST_SCENARIO and not _positive(gate.params.get("fixed_bps"))
        )
    return complaints


def stage_shortfall(stage: str, included: Sequence[Any], hyp: Hypothesis, folds: int) -> list[str]:
    """How a plan's gates fall short of the ones the toolbox requires at one stage: each such
    gate it lacks, and each it names without every parameter it declares or outside a range.

    The part of `plan_complaints` a stage holds a pinned plan to. The monitor reads a plan
    nothing has checked against this toolbox since it was pinned, and judges a deployed
    version by its own stage's gates alone, so a complaint about another stage — a search
    correction the lineage has since outgrown — is not one that should stop it. What it says
    is read by whoever reads the monitor's outcome, so it leaves out what `check_params` tells
    the planner about whose values they are.
    """
    named = {gate.id: gate for gate in included if gate.stage == stage}
    shortfall: list[str] = []
    for item in plannable().values():
        if item.stage != stage or not item.required:
            continue
        gate = named.get(item.id)
        if gate is None:
            shortfall.append(f"lacks {item.id}")
            continue
        shortfall.extend(
            f"names {item.id}, but {as_pinned(problem)}"
            for problem in check_params(item, gate.params, hyp, folds)
        )
    return shortfall


def validate_plan(plan: CertificationPlan | Mapping[str, Any], hyp: Hypothesis, folds: int) -> None:
    """Refuse a certification plan that breaks a structural invariant of the toolbox.

    The raising form of `plan_complaints`, which is the one rule set: two validators that
    had to agree would eventually not. The search it is checked against is the one the plan
    records it was made behind, `inputs.n_trials` and `inputs.prior_trials` together.
    """
    model = plan if isinstance(plan, CertificationPlan) else CertificationPlan.model_validate(plan)
    searched = model.inputs.n_trials + model.inputs.prior_trials
    problems = plan_complaints(model.gates, model.excluded, hyp, folds, searched)
    if problems:
        raise ValidationError("; ".join(problems))
