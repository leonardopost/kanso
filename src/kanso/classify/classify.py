"""Classification: deciding what a hypothesis is, once, and writing the answer into it.

A hypothesis states an idea; a classification says what that idea *is* in
portfolio-construction terms, what scalar measures it and what every card of it must
satisfy. It is decided once, it directs every run that follows, and it is the cheapest
place in the system to spend the most — so it takes one call to the best model available,
after everything that can be worked out without a model already has been.

**The deterministic half comes first.** Which constructs can attach to what this workspace
has certified, how a candidate's universe, horizon and resolution relate to each certified
strategy's, and which objectives the toolbox's own predicate admits in each objective mode
are all computed before the call. The predicate bounds the objective and chooses nothing
within it: the model names one of the admitted ids — the wire holds it to the set, the
check to the construct's mode — with the keep rule's two parameters. It used to be the
lowest priority the predicate admitted, which put every hypothesis held under a day on the
per-trade edge whatever it said its target was, and under that objective a strategy making
two trades a day at ten basis points replaces one making forty at 1.2: five basis points a
day of a quarter of the capital kept over twelve.

**An objective the file already names is the operator's.** A hypothesis whose target is a
return on capital states the contribution in its own file, and one `classify` used to
replace it with the per-trade edge before the first card. Now an objective written in the
file stands, keep rule and all, whenever the construct chosen is measured in its mode and
its parameters are inside the toolbox's ranges; the model is shown it and asked to name it.
Otherwise the model's objective is written, and the replacement is returned with its reason
and recorded as an `objective_replaced` event, so `kanso classify` reports it rather than
leaving a reader to diff the file. The same holds for an objective a previous classification
wrote, which is the operator's once it is in the file: clearing the key is how a model is
asked to choose again.

**The answer is checked before it is believed.** A construct that is not in the catalogue,
a host that is not certified, an objective the construct's mode does not admit, a parameter
outside its declared range, a constraint that is not a card-stage gate, a missing required
gate — the toolbox's `required` ones, and those whose `required_for` names the objective
written, each with every parameter set — and a gate whose parameters mean nothing apart
named with some of them: each is a complaint handed back to the same model, and the router's
ladder decides how many attempts that is worth. Nothing half-valid reaches the file: the
classification is spliced into the hypothesis, the whole document is validated as a
workspace file would be, and only then is anything written.

**The prompt states the idea and never its results.** No card metric, no certificate and
no strategy source is assembled into it, here or anywhere the inputs come from, because a
classification conditioned on results is a classification fitted to them.

**A construct this version cannot run is still what the hypothesis is.** It is recorded
like any other and refused when a run is begun, naming the seam that would make it
runnable. What it does not get is a stub: the file kanso knows how to write is not the
file that construct would need.

**Another construct is another question**, so a classification that changes it clears
the hypothesis's `best`. That is the registry's rule, applied by the re-pin this step
ends with, and the same rule a hand-edited `hyp add` meets — including the edit that
strips the classification to make the hypothesis classifiable again.

The stub it does write replaces the hypothesis's `strategy.py` only while that file is
still one kanso rendered — compared by content address against every stub it could have
written for this hypothesis — so a classification never overwrites work.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, cast

import yaml

from kanso.classify.construct import PORTFOLIO, Catalogue, catalogue
from kanso.classify.features import CARD_STAGE, MODES, NO_HOST, applicable_ids, features
from kanso.criteria import catalogue as toolbox
from kanso.criteria import check_params, required_card_gates
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import (
    STRATEGY_FILE,
    Registration,
    hypothesis_dir,
    hypothesis_file,
    pin,
    read_source,
    set_status,
    show,
    stub,
    validate,
)
from kanso.hyp.registry import CLASSIFIED, DRAFT
from kanso.models import CallInputs, route
from kanso.models.tasks import classify_schema
from kanso.research.lanes import DEFAULT_LANE
from kanso.schemas import (
    ConstraintRef,
    ConstructRef,
    Hypothesis,
    ObjectiveParams,
    ObjectiveRef,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["Classified", "StatedObjective", "classify"]

TASK: Final = "classify"
"""The task class this step calls; the router owns its tier, effort and output cap."""

CLASSIFICATION: Final = ("construct", "objective", "constraints")
"""The three keys classification owns in `hypothesis.yaml`; it writes all three or none."""

CLASSIFIABLE: Final = frozenset({DRAFT, CLASSIFIED})
"""A hypothesis is classified before it is researched, and re-classified until it is."""

OBJECTIVE: Final = "objective"
"""The one of the three keys an operator may state for classification to keep."""

OBJECTIVE_REPLACED: Final = "objective_replaced"
"""The event kind recording an objective the file named and the classification did not keep."""

_KEY: Final = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")
"""A top-level key of a YAML document: no indentation, no leading marker."""


@dataclass(frozen=True)
class StatedObjective:
    """An objective the file named before it was classified, and what became of it.

    `id` is the id as the file wrote it, `None` when the key held no id at all. `reason` says
    why it was not kept, and is `None` exactly when it was.
    """

    id: str | None
    kept: bool
    reason: str | None = None


@dataclass(frozen=True)
class Classification:
    """What one accepted answer says the hypothesis is."""

    construct: ConstructRef
    objective: ObjectiveRef
    constraints: tuple[ConstraintRef, ...]
    stated: StatedObjective | None = None


@dataclass(frozen=True)
class Classified:
    """What `classify` wrote: the hypothesis as it now reads, and what it did with an
    objective the file already named — `None` when it named none."""

    hyp: Hypothesis
    stated: StatedObjective | None


@dataclass(frozen=True)
class _Stated:
    """The file's own objective block, read before anything is stripped from it."""

    id: str | None
    ref: ObjectiveRef | None
    problem: str | None


def classify(
    ws: Workspace, store: StateStore, hyp_id: str, *, lane: str = DEFAULT_LANE
) -> Classified:
    """Classify one registered hypothesis and write the answer into its file.

    Returns the hypothesis as it now reads and what became of an objective the file named.
    `lane` is the lane the call's spend is attributed to.
    """
    registration = cast("Registration", show(ws, store, hyp_id))
    _classifiable(registration)
    path = hypothesis_file(ws, hyp_id)
    text = _text(path)
    stated = _stated(text)
    hyp = validate(ws, path, _without(text, CLASSIFICATION).encode("utf-8"))

    computed = features(ws, store, hyp)
    accepted: list[Classification] = []
    constructs = catalogue(ws)
    folds = ws.config.research.folds

    def check(data: Mapping[str, object]) -> Sequence[str]:
        answered, complaints = _read(
            data,
            hyp=hyp,
            constructs=constructs,
            attachable=computed["attachable"],
            objectives=computed["objectives"],
            folds=folds,
            stated=stated,
        )
        if answered is not None and not complaints:
            accepted.append(answered)
        return complaints

    route(ws, store, TASK, _inputs(hyp, computed, check, stated), lane=lane)
    chosen = accepted[-1]

    document = _written(text, chosen)
    updated = validate(ws, path, document.encode("utf-8"))
    _replace(path, document)
    pin(store, updated, document.encode("utf-8"))
    set_status(store, hyp_id, CLASSIFIED)
    if chosen.stated is not None and not chosen.stated.kept:
        store.event(
            OBJECTIVE_REPLACED,
            hyp_id,
            {
                "stated": chosen.stated.id,
                "objective": chosen.objective.id,
                "reason": chosen.stated.reason,
            },
        )
    _render_stub(ws, updated, chosen, computed["certified"])
    return Classified(hyp=updated, stated=chosen.stated)


def _classifiable(registration: Registration) -> None:
    """Refuse a hypothesis whose state makes a classification meaningless or unsafe."""
    if registration.active_run is not None:
        raise PreconditionError(
            f"{registration.hyp_id} has an active run ({registration.active_run}), so it "
            "cannot be classified",
            remedy=f"end the run with `kanso research end {registration.hyp_id}` first",
        )
    if registration.status not in CLASSIFIABLE:
        raise PreconditionError(
            f"{registration.hyp_id} is {registration.status}, and only a "
            f"{' or '.join(sorted(CLASSIFIABLE))} hypothesis is classified",
            remedy=(
                "edit construct, objective and constraints in hypothesis.yaml and run "
                "`kanso hyp add` to state the classification yourself"
            ),
        )


Check = Callable[[Mapping[str, object]], Sequence[str]]
"""What the router runs on an answer that already satisfies the task class's schema."""


def _inputs(
    hyp: Hypothesis, computed: Mapping[str, Any], check: Check, stated: _Stated | None
) -> CallInputs:
    """The call's two halves: the idea and the catalogues, then the state of the book.

    The hypothesis and the catalogues do not move while one hypothesis is classified, so
    they are the cached prefix; what this workspace has certified is the half that does. An
    objective the file names is part of the idea, and is shown beside it; the answer's
    `objective` is narrowed on the wire to the ids either mode admits.
    """
    stable: dict[str, Any] = {
        "hypothesis": _document(hyp),
        "constructs": computed["constructs"],
        "objectives": computed["objectives"],
        "card_gates": computed["card_gates"],
    }
    if stated is not None and stated.ref is not None:
        stable["objective_stated"] = stated.ref.model_dump(mode="json")
    return CallInputs(
        subject=hyp.id,
        stable=stable,
        dynamic={
            "certified_strategies": computed["certified"],
            "attachable": computed["attachable"],
        },
        check=check,
        answer_schema=classify_schema(applicable_ids(dict(computed["objectives"]))),
    )


def _document(hyp: Hypothesis) -> dict[str, Any]:
    """The hypothesis as the model reads it: the idea, without any classification."""
    return hyp.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude={"schema_", "construct_", "objective", "constraints"},
    )


def _read(
    data: Mapping[str, object],
    *,
    hyp: Hypothesis,
    constructs: Catalogue,
    attachable: Mapping[str, Sequence[str]],
    objectives: Mapping[str, Any],
    folds: int,
    stated: _Stated | None = None,
) -> tuple[Classification | None, list[str]]:
    """One answer, read as a classification, and everything wrong with it.

    The three references are built first, so a value the data model cannot hold is one
    complaint rather than an exception; the catalogue, the applicability and the ranges
    are checked on the objects that survived. The objective the answer names is checked
    against the set the construct's mode admits whether or not it is the one written: an
    objective the file states is kept over it when that mode admits it too
    (`_kept_objective`), and the gates the written objective requires are checked against
    that one, the operator's own `required_constraints` counting toward them.

    The keep rule is read on its own because it is the one part of the answer the wire
    does not constrain: a numeric range is a keyword the provider refuses, so
    `models/tasks.py` sends `min_delta` and `k_se` as bare numbers and this refusal is
    the only thing standing between a model's `k_se: 0` and the file. It earns the same
    `objective.params.<field>: <problem>` a value outside the toolbox's range earns,
    naming the field and the bound it missed. The bound was named before this too; what
    the retry gains is where in the answer to look, and one line per field.
    """
    proposal = _object(data.get("construct"))
    try:
        construct = ConstructRef.model_validate(
            {
                "id": proposal.get("id"),
                "host": proposal.get("host"),
                "params": _object(proposal.get("params")) or None,
                "rationale": data.get("rationale"),
            }
        )
        constraints = tuple(
            ConstraintRef.model_validate(_object(entry))
            for entry in _sequence(data.get("constraints"))
        )
    except ValidationError as exc:
        return None, [f"the answer is not a classification: {exc.message}"]
    try:
        parameters = ObjectiveParams.model_validate(_object(data.get("objective_params")))
    except ValidationError as exc:
        # `schemas.render_errors` renders one field per error and joins them with "; ",
        # so splitting there is one complaint per field rather than one for the pair.
        return None, [f"objective.params.{problem}" for problem in exc.message.split("; ")]

    entry = constructs.entries.get(construct.id)
    if entry is None:
        return None, [
            f"construct.id: {construct.id!r} is not in the catalogue, which holds "
            f"{', '.join(sorted(constructs.entries))}"
        ]
    complaints = [
        *_host_complaints(entry.item.needs_host, construct, attachable),
        *_construct_param_complaints(entry.item.params or {}, construct),
        *_constraint_complaints(constraints, hyp, folds),
    ]
    mode = entry.item.objective_mode
    admitted = applicable_ids(dict(objectives), mode)
    if not admitted:
        complaints.append(
            f"construct.id: nothing in the toolbox measures a {construct.id} on this "
            f"hypothesis, since no {mode} objective applies to it"
        )
        return None, complaints
    named = str(data.get("objective") or "")
    if named in admitted:
        complaints.extend(
            f"objective.params.{problem}"
            for problem in check_params(toolbox()[named], parameters.model_dump(), hyp, folds)
        )
    else:
        complaints.append(_objective_complaint(named, construct.id, mode, objectives))
    if complaints:
        return None, complaints
    answered = ObjectiveRef(id=named, params=parameters)
    written, note = _kept_objective(stated, answered, construct.id, mode, objectives, hyp, folds)
    complaints = _required_complaints(written.id, constraints, hyp)
    if complaints:
        return None, complaints
    return Classification(
        construct=construct, objective=written, constraints=constraints, stated=note
    ), []


def _objective_complaint(
    named: str, construct: str, mode: str, objectives: Mapping[str, Any]
) -> str:
    """Why an answer's objective is not one its construct can be scored on.

    The wire holds the answer to the ids either mode admits, so one this mode does not
    admit is the other mode's.
    """
    other = next(one for one in MODES if one != mode)
    return (
        f"objective: {named} is {_a(other)} objective, and {_a(construct)} is measured "
        f"{mode}; name one of {', '.join(applicable_ids(dict(objectives), mode))}"
    )


def _a(word: str) -> str:
    """The word with its indefinite article: an absolute objective, a relative one."""
    return f"{'an' if word[:1] in 'aeiou' else 'a'} {word}"


def _kept_objective(
    stated: _Stated | None,
    answered: ObjectiveRef,
    construct: str,
    mode: str,
    objectives: Mapping[str, Any],
    hyp: Hypothesis,
    folds: int,
) -> tuple[ObjectiveRef, StatedObjective | None]:
    """The objective written, and what became of one the file stated.

    A stated objective stands, keep rule and all, when the construct's mode admits it and
    its parameters are inside the toolbox's ranges; the answer's is written otherwise, and
    the stated one is reported replaced, with the first of those that failed as the reason.
    """
    if stated is None:
        return answered, None
    reason = stated.problem
    ref = stated.ref
    if ref is not None and reason is None:
        reason = _inadmissible(ref, construct, mode, objectives, hyp, folds)
    if ref is not None and reason is None:
        return ref, StatedObjective(id=ref.id, kept=True)
    return answered, StatedObjective(id=stated.id, kept=False, reason=reason)


def _inadmissible(
    ref: ObjectiveRef,
    construct: str,
    mode: str,
    objectives: Mapping[str, Any],
    hyp: Hypothesis,
    folds: int,
) -> str | None:
    """Why a stated objective cannot be kept for this construct, or `None` when it can."""
    item = toolbox().get(ref.id)
    if item is None or item.kind != OBJECTIVE:
        return f"{ref.id} is not an objective in the toolbox"
    if ref.id not in applicable_ids(dict(objectives), mode):
        other = next(one for one in MODES if one != mode)
        if ref.id in applicable_ids(dict(objectives), other):
            return f"{ref.id} is {_a(other)} objective, and {_a(construct)} is measured {mode}"
        return f"{ref.id} does not apply to this hypothesis"
    problems = check_params(item, ref.params.model_dump(), hyp, folds)
    if problems:
        return f"its keep rule is outside the toolbox's ranges: {'; '.join(problems)}"
    return None


def _required_complaints(
    objective: str, constraints: Sequence[ConstraintRef], hyp: Hypothesis
) -> list[str]:
    """The gates the objective written requires, each among the card gates.

    The operator's `required_constraints` count, as they do on every card. A gate that is
    `required_for` an objective declares `together`, so the one naming it was already held
    to every parameter by `_constraint_complaints`, or by validation for the operator's.
    """
    named = {ref.id for ref in (*constraints, *(hyp.required_constraints or ()))}
    return [
        f"constraints: {gate} is required of a hypothesis scored on {objective}, with "
        f"{', '.join(toolbox()[gate].params)} set; the values are yours, inside their ranges"
        for gate in required_card_gates(objective)
        if gate not in named
    ]


def _host_complaints(
    needs_host: str, construct: ConstructRef, attachable: Mapping[str, Sequence[str]]
) -> list[str]:
    """Whether the construct names the host it needs, and one it may have."""
    if needs_host == NO_HOST:
        if construct.host is None:
            return []
        return [
            f"construct.host: a {construct.id} is a strategy of its own and attaches to "
            f"nothing, but {construct.host!r} was named as its host"
        ]
    available = attachable.get(construct.id)
    if not available:
        return [
            f"construct.id: a {construct.id} attaches to a {needs_host}, and this workspace "
            "has no certified strategy for it to attach to; classify onto a construct that "
            "stands on its own"
        ]
    if construct.host is None:
        return [
            f"construct.host: a {construct.id} attaches to a {needs_host}, so it names one "
            f"of {', '.join(available)}"
        ]
    if construct.host not in available:
        return [
            f"construct.host: {construct.host!r} is not one of {', '.join(available)}, "
            f"which is what a {construct.id} may attach to here"
        ]
    return []


def _construct_param_complaints(declared: Mapping[str, Any], construct: ConstructRef) -> list[str]:
    """A construct takes the parameters its catalogue entry declares, and their values."""
    complaints: list[str] = []
    for name, value in (construct.params or {}).items():
        allowed = declared.get(name)
        if allowed is None:
            takes = ", ".join(sorted(declared)) or "none"
            complaints.append(
                f"construct.params: a {construct.id} has no parameter {name!r}; it takes {takes}"
            )
        elif value not in allowed:
            spelled = ", ".join(str(one) for one in allowed)
            complaints.append(f"construct.params.{name}: {value!r} is not one of {spelled}")
    return complaints


def _constraint_complaints(
    constraints: Sequence[ConstraintRef], hyp: Hypothesis, folds: int
) -> list[str]:
    """Every constraint is a card-stage gate, named once, inside its ranges."""
    items = toolbox()
    complaints: list[str] = []
    named: list[str] = []
    for constraint in constraints:
        item = items.get(constraint.id)
        if item is None or item.kind != "gate":
            complaints.append(f"constraints.{constraint.id}: is not a gate in the toolbox")
        elif item.stage != CARD_STAGE:
            complaints.append(
                f"constraints.{constraint.id}: runs at the {item.stage} stage, and a "
                f"hypothesis constrains only the {CARD_STAGE} stage"
            )
        elif constraint.id in named:
            complaints.append(f"constraints.{constraint.id}: is named twice")
        else:
            named.append(constraint.id)
            complaints.extend(
                f"constraints.{constraint.id}.{problem}"
                for problem in check_params(item, constraint.params, hyp, folds)
            )
    complaints.extend(
        f"constraints: {item.id} is required of every hypothesis and is missing"
        for item in items.values()
        if item.kind == "gate"
        and item.stage == CARD_STAGE
        and item.required
        and item.id not in named
    )
    return complaints


def _text(path: Path) -> str:
    """The hypothesis file as text, refused when it is not text."""
    source = read_source(path)
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path}: is not UTF-8 text: {exc}") from None


def _stated(text: str) -> _Stated | None:
    """The objective the file names before classification, or `None` when it names none.

    Read from the file's own objective block alone, so the rest of the document — a
    construct whose host has gone, a constraint the toolbox has dropped — cannot stop it
    being read; a block that does not parse as an objective is stated and unusable, and is
    reported replaced with the reason.
    """
    block = _only(text, OBJECTIVE)
    try:
        found = yaml.safe_load(block) if block.strip() else None
    except yaml.YAMLError as exc:
        return _Stated(id=None, ref=None, problem=f"it does not parse: {exc}")
    value = found.get(OBJECTIVE) if isinstance(found, dict) else None
    if value is None:
        return None
    raw = value.get("id") if isinstance(value, dict) else None
    named = None if raw is None else str(raw)
    try:
        ref = ObjectiveRef.model_validate(value)
    except ValidationError as exc:
        return _Stated(id=named, ref=None, problem=f"it is not an objective: {exc.message}")
    return _Stated(id=ref.id, ref=ref, problem=None)


def _only(text: str, key: str) -> str:
    """The document's one top-level block under `key`, and nothing else."""
    kept: list[str] = []
    keeping = False
    for line in text.splitlines():
        found = _KEY.match(line)
        keeping = found.group(1) == key if found is not None else keeping
        if keeping:
            kept.append(line)
    return "".join(f"{line}\n" for line in kept)


def _without(text: str, keys: Sequence[str]) -> str:
    """The document with those top-level keys and their blocks removed.

    Everything else — comments, spacing, the order the operator chose — survives, because
    the file is theirs and classification owns three keys of it.
    """
    kept: list[str] = []
    dropping = False
    for line in text.splitlines():
        found = _KEY.match(line)
        dropping = found.group(1) in keys if found is not None else dropping
        if not dropping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "".join(f"{line}\n" for line in kept)


def _written(text: str, chosen: Classification) -> str:
    """The operator's file with the classification appended in place of the old one."""
    document = {
        "construct": chosen.construct.model_dump(mode="json", by_alias=True, exclude_none=True),
        "objective": chosen.objective.model_dump(mode="json", by_alias=True),
        "constraints": [
            constraint.model_dump(mode="json", by_alias=True) for constraint in chosen.constraints
        ],
    }
    block = yaml.safe_dump(
        document,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )
    return _without(text, CLASSIFICATION) + block


def _replace(path: Path, text: str) -> None:
    """Write the file in one step, so a reader never sees half a hypothesis."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _render_stub(
    ws: Workspace,
    hyp: Hypothesis,
    chosen: Classification,
    certified: Sequence[Mapping[str, Any]],
) -> bool:
    """Write the construct's stub, unless the file holds something kanso did not write.

    Reports whether it wrote. A construct this version cannot run gets no stub: the two
    templates kanso ships are a strategy and a modifier, and a construct that is neither
    would be misdescribed by both.
    """
    construct = catalogue(ws).entries[chosen.construct.id]
    if not construct.item.runnable:
        return False
    path = hypothesis_dir(ws, hyp.id) / STRATEGY_FILE
    wanted = stub(hyp.id, chosen.construct.id, chosen.construct.host)
    if path.is_file():
        current = _address(path.read_text(encoding="utf-8"))
        if current == _address(wanted) or current not in _renderable(ws, hyp.id, certified):
            return False
    _replace(path, wanted)
    return True


def _renderable(ws: Workspace, hyp_id: str, certified: Sequence[Mapping[str, Any]]) -> set[str]:
    """The content address of every stub kanso could have written for this hypothesis."""
    hosts = [str(spec["id"]) for spec in certified] + [PORTFOLIO]
    addresses = {_address(stub(hyp_id))}
    for entry in catalogue(ws).entries.values():
        if entry.item.needs_host == NO_HOST or not entry.item.runnable:
            continue
        addresses.update(_address(stub(hyp_id, entry.item.id, host)) for host in hosts)
    return addresses


def _address(text: str) -> str:
    """The content address of a file's text: the same currency the state store uses."""
    return sha256(text.encode("utf-8")).hexdigest()


def _object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []
