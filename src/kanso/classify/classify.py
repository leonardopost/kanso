"""Classification: deciding what a hypothesis is, once, and writing the answer into it.

A hypothesis states an idea; a classification says what that idea *is* in
portfolio-construction terms, what scalar measures it and what every card of it must
satisfy. It is decided once, it directs every run that follows, and it is the cheapest
place in the system to spend the most — so it takes one call to the best model available,
after everything that can be worked out without a model already has been.

**The deterministic half comes first.** Which constructs can attach to what this workspace
has certified, how a candidate's universe, horizon and resolution relate to each certified
strategy's, and which objective the toolbox's own predicate selects for each objective
mode are all computed before the call. The objective is therefore never the model's to
choose: it follows from the hypothesis and the construct, and only the keep rule's two
parameters are asked for.

**The answer is checked before it is believed.** A construct that is not in the catalogue,
a host that is not certified, a parameter outside its declared range, a constraint that is
not a card-stage gate, a missing required gate — each is a complaint handed back to the
same model, and the router's ladder decides how many attempts that is worth. Nothing
half-valid reaches the file: the classification is spliced into the hypothesis, the whole
document is validated as a workspace file would be, and only then is anything written.

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
from kanso.classify.features import CARD_STAGE, NO_HOST, features
from kanso.criteria import catalogue as toolbox
from kanso.criteria import check_params
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

__all__ = ["classify"]

TASK: Final = "classify"
"""The task class this step calls; the router owns its tier, effort and output cap."""

CLASSIFICATION: Final = ("construct", "objective", "constraints")
"""The three keys classification owns in `hypothesis.yaml`; it writes all three or none."""

CLASSIFIABLE: Final = frozenset({DRAFT, CLASSIFIED})
"""A hypothesis is classified before it is researched, and re-classified until it is."""

_KEY: Final = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")
"""A top-level key of a YAML document: no indentation, no leading marker."""


@dataclass(frozen=True)
class Classification:
    """What one accepted answer says the hypothesis is."""

    construct: ConstructRef
    objective: ObjectiveRef
    constraints: tuple[ConstraintRef, ...]


def classify(
    ws: Workspace, store: StateStore, hyp_id: str, *, lane: str = DEFAULT_LANE
) -> Hypothesis:
    """Classify one registered hypothesis and write the answer into its file.

    Returns the hypothesis as it now reads. `lane` is the lane the call's spend is
    attributed to.
    """
    registration = cast("Registration", show(ws, store, hyp_id))
    _classifiable(registration)
    path = hypothesis_file(ws, hyp_id)
    text = _text(path)
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
        )
        if answered is not None and not complaints:
            accepted.append(answered)
        return complaints

    route(ws, store, TASK, _inputs(hyp, computed, check), lane=lane)
    chosen = accepted[-1]

    document = _written(text, chosen)
    updated = validate(ws, path, document.encode("utf-8"))
    _replace(path, document)
    pin(store, updated, document.encode("utf-8"))
    set_status(store, hyp_id, CLASSIFIED)
    _render_stub(ws, updated, chosen, computed["certified"])
    return updated


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


def _inputs(hyp: Hypothesis, computed: Mapping[str, Any], check: Check) -> CallInputs:
    """The call's two halves: the idea and the catalogues, then the state of the book.

    The hypothesis and the catalogues do not move while one hypothesis is classified, so
    they are the cached prefix; what this workspace has certified is the half that does.
    """
    return CallInputs(
        subject=hyp.id,
        stable={
            "hypothesis": _document(hyp),
            "constructs": computed["constructs"],
            "objectives": computed["objectives"],
            "card_gates": computed["card_gates"],
        },
        dynamic={
            "certified_strategies": computed["certified"],
            "attachable": computed["attachable"],
        },
        check=check,
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
) -> tuple[Classification | None, list[str]]:
    """One answer, read as a classification, and everything wrong with it.

    The three references are built first, so a value the data model cannot hold is one
    complaint rather than an exception; the catalogue, the applicability and the ranges
    are checked on the objects that survived.
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
        parameters = ObjectiveParams.model_validate(_object(data.get("objective_params")))
        constraints = tuple(
            ConstraintRef.model_validate(_object(entry))
            for entry in _sequence(data.get("constraints"))
        )
    except ValidationError as exc:
        return None, [f"the answer is not a classification: {exc.message}"]

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
    selected = objectives[mode]["selected"]
    if selected is None:
        complaints.append(
            f"construct.id: nothing in the toolbox measures a {construct.id} on this "
            f"hypothesis, since no {mode} objective applies to it"
        )
        return None, complaints
    complaints.extend(
        f"objective.params.{problem}"
        for problem in check_params(toolbox()[selected], parameters.model_dump(), hyp, folds)
    )
    if complaints:
        return None, complaints
    return Classification(
        construct=construct,
        objective=ObjectiveRef(id=str(selected), params=parameters),
        constraints=constraints,
    ), []


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
