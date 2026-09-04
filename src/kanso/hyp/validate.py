"""What makes a `hypothesis.yaml` admissible, checked in one pass over one file.

The file's own arithmetic — the duration grammar, the embargo between the research and
certification windows, their ordering and disjointness, the resolution and cost model
being answerable from the data the hypothesis asks for, and `strategy_integrity` among a
classified hypothesis's constraints — belongs to the model and is enforced when the
document is parsed. Everything else needs the workspace, and that is this module:

* the file lives at `hypotheses/<id>/`, so the id in the document and the directory that
  holds it are the same word. Directory names are unique, so this is what makes ids
  unique too, and it is what lets every later command find a hypothesis by its id alone;
* every data requirement names a type the workspace knows — the three market-data types,
  or one an extension registered;
* every universe id resolves to an instrument as of the research window's first day. A
  manual entry answers on its own, a cached resolution answers when it was made as of
  that date, and anything else needs the configured reference adapter. An id that is
  unknown, ambiguous across venues, delisted before that date or listed after it fails,
  and every failing id is reported together;
* the venues those instruments trade on resolve to a complete cost model, and to one
  account currency. A spread taken from quotes needs quotes; a fixed spread needs its
  width; a universe spanning two account currencies would have a leg priced at a rate
  nothing in the workspace records, so it is refused;
* when classification has been written, its construct is in the catalogue, its host is
  present exactly when the construct needs one and names a certified strategy, its
  objective applies to this hypothesis and its parameters are inside the toolbox's
  ranges, and every constraint is a card-stage gate whose parameters are inside theirs.

A validation failure names the field and the reason, and where several are independent
they are reported together rather than one per attempt.

NautilusTrader facts this module relies on (nautilus_trader 1.231.0): an `Instrument`
carries its venue on `id.venue`, whose `value` is the venue code a venue model is
resolved for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from kanso import ext
from kanso.classify.construct import PORTFOLIO
from kanso.classify.construct import catalogue as construct_catalogue
from kanso.criteria import applicable_objectives, check_params
from kanso.criteria import catalogue as criteria_catalogue
from kanso.data.instruments import resolve_universe
from kanso.data.types import data_types
from kanso.errors import ValidationError
from kanso.hyp.scaffold import HYPOTHESES
from kanso.schemas import (
    ConstraintRef,
    ConstructRef,
    Hypothesis,
    ObjectiveRef,
    Portfolio,
    StrategyFile,
    VenueModel,
    load_yaml,
    parse_yaml,
    resolve_venue_model,
    single_currency,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.workspace import Workspace

CARD_STAGE: Final = "card"
"""The stage a hypothesis's own constraints run at; the rest are planned at certification."""

QUOTE_TYPE: Final = "quote"
"""The data requirement a spread read from quotes needs."""

STRATEGIES: Final = "strategies"
STRATEGY_FILE: Final = "strategy.yaml"
PORTFOLIO_FILE: Final = "portfolio.yaml"

CLASSIFICATION: Final = ("construct", "objective", "constraints")
"""The three fields classification writes; they are written and validated together."""


def read_source(path: Path) -> bytes:
    """The file's bytes, which are what a registration is pinned by."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"{path}: cannot be read: {exc}") from None


def validate(ws: Workspace, path: Path, source: bytes | None = None) -> Hypothesis:
    """The hypothesis at `path`, refused unless everything above holds.

    `source` is the file's bytes when the caller has already read them, so a caller that
    pins those bytes validates exactly what it pins.
    """
    raw = read_source(path) if source is None else source
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path}: is not UTF-8 text: {exc}") from None
    hyp = parse_yaml(Hypothesis, text, str(path))
    _check_location(ws, path, hyp)
    _check_data_requirements(ws, hyp)
    instruments = resolve_universe(ws, hyp.universe, hyp.windows.research.start)
    venue_models(ws, hyp, instruments)
    _check_classification(ws, hyp)
    return hyp


def venue_models(
    ws: Workspace, hyp: Hypothesis, instruments: Mapping[str, Any]
) -> dict[str, VenueModel]:
    """The resolved trading model of every venue this universe trades on.

    Each venue inherits the configured broker's declaration, then the operator's
    `venues.<MIC>` override, then the hypothesis's own `costs`. A cost model that cannot
    be completed — a spread from quotes the hypothesis does not require, or a fixed
    spread with no width — and a universe spanning more than one account currency are
    both refused here, because both would put a number on a card that nothing in the
    workspace can account for.
    """
    overrides = _venue_overrides(ws)
    quotes = QUOTE_TYPE in hyp.data_requirements
    models = {
        venue: resolve_venue_model(
            venue,
            broker=ws.config.research.broker,
            override=overrides.get(venue),
            hypothesis_costs=hyp.costs,
            max_leverage=hyp.risk_limits.max_leverage,
            quotes_available=quotes,
        )
        for venue in sorted({_venue_of(held) for held in instruments.values()})
    }
    single_currency(models)
    return models


def _venue_of(instrument: Any) -> str:
    return str(instrument.id.venue.value)


def _venue_overrides(ws: Workspace) -> Mapping[str, Any]:
    path = ws.path(PORTFOLIO_FILE)
    if not path.is_file():
        return {}
    return load_yaml(Portfolio, path).venues or {}


def _check_location(ws: Workspace, path: Path, hyp: Hypothesis) -> None:
    """A file under `hypotheses/` sits in the directory its own id names."""
    root = ws.path(HYPOTHESES).resolve()
    directory = path.resolve().parent
    if directory.parent != root:
        return
    if directory.name != hyp.id:
        raise ValidationError(
            f"id: {hyp.id!r} is declared in {HYPOTHESES}/{directory.name}/, and a hypothesis "
            f"lives in the directory its id names",
            remedy=f"rename the directory to {hyp.id!r}, or set id to {directory.name!r}",
        )


def _check_data_requirements(ws: Workspace, hyp: Hypothesis) -> None:
    known = _known_types(ws)
    unknown = [required for required in hyp.data_requirements if required not in known]
    if unknown:
        raise ValidationError(
            f"data_requirements: {', '.join(unknown)} is not a data type this workspace knows; "
            f"it knows {', '.join(sorted(known))}",
            remedy="require one of those, or install the extension that registers the type",
        )


def _known_types(ws: Workspace) -> dict[str, type]:
    """Every type a `data_requirements` entry may name here.

    Discovery imports the workspace's extensions, which is when one that provides a
    custom type registers it, so an extension's type is known to validation and not only
    once a loader has run.
    """
    ext.discover(ws.root, ws.config.extensions_paths)
    return data_types()


def _check_classification(ws: Workspace, hyp: Hypothesis) -> None:
    """The three fields classification writes, when they are written."""
    written = (hyp.construct, hyp.objective, hyp.constraints)
    absent = [name for name, value in zip(CLASSIFICATION, written, strict=True) if value is None]
    if len(absent) == len(CLASSIFICATION):
        return
    construct, objective, constraints = written
    if construct is None or objective is None or constraints is None:
        raise ValidationError(
            "; ".join(
                f"{name}: missing; a classified hypothesis carries "
                f"{', '.join(CLASSIFICATION)} together"
                for name in absent
            ),
            remedy="write the missing field, or clear all three and classify again",
        )
    mode = _check_construct(ws, construct)
    _check_objective(ws, hyp, objective, mode)
    _check_constraints(ws, hyp, constraints)


def _check_construct(ws: Workspace, ref: ConstructRef) -> str:
    """The construct's objective mode, once it exists and its host is what it needs."""
    construct = construct_catalogue(ws).get(ref.id)
    needs = construct.needs_host
    if needs == "none":
        if ref.host is not None:
            raise ValidationError(
                f"construct.host: {ref.id} is a strategy of its own and attaches to nothing, "
                f"but names {ref.host!r}"
            )
    elif ref.host is None:
        raise ValidationError(
            f"construct.host: {ref.id} attaches to a {needs}, so it names the host it attaches to",
            remedy=f"set construct.host to a certified strategy, or classify onto a {PORTFOLIO}",
        )
    elif needs == PORTFOLIO:
        if ref.host != PORTFOLIO:
            raise ValidationError(
                f"construct.host: {ref.id} attaches to the book, so its host is {PORTFOLIO!r}, "
                f"not {ref.host!r}"
            )
    else:
        _check_host_strategy(ws, ref.id, ref.host)
    return str(construct.objective_mode)


def _check_host_strategy(ws: Workspace, construct_id: str, host: str) -> None:
    """The host names a composed strategy, which is what a certified hypothesis becomes."""
    path = ws.path(STRATEGIES, host, STRATEGY_FILE)
    if not path.is_file():
        raise ValidationError(
            f"construct.host: {host!r} is not a certified strategy of this workspace; "
            f"a {construct_id} attaches to one",
            remedy="certify and compose the host sleeve first, or name another host",
        )
    strategy = load_yaml(StrategyFile, path)
    if strategy.id != host:
        raise ValidationError(
            f"construct.host: {path} declares the strategy {strategy.id!r}, not {host!r}"
        )


def _check_objective(ws: Workspace, hyp: Hypothesis, ref: ObjectiveRef, mode: str) -> None:
    item = criteria_catalogue().get(ref.id)
    if item is None or item.kind != "objective":
        raise ValidationError(
            f"objective.id: {ref.id!r} is not an objective in the toolbox",
            remedy="run `kanso classify` to have one chosen, or name one the toolbox holds",
        )
    applicable = [found for _, found in applicable_objectives(hyp, mode)]
    if ref.id not in applicable:
        raise ValidationError(
            f"objective.id: {ref.id!r} does not apply to this hypothesis; the applicable "
            f"{mode} objectives are {', '.join(sorted(applicable))}"
        )
    problems = check_params(item, ref.params.model_dump(), hyp, ws.config.research.folds)
    if problems:
        raise ValidationError("; ".join(f"objective.params.{problem}" for problem in problems))


def _check_constraints(
    ws: Workspace, hyp: Hypothesis, constraints: Sequence[ConstraintRef]
) -> None:
    items = criteria_catalogue()
    folds = ws.config.research.folds
    problems: list[str] = []
    for constraint in constraints:
        item = items.get(constraint.id)
        if item is None or item.kind != "gate":
            problems.append(f"constraints.{constraint.id}: is not a gate in the toolbox")
        elif item.stage != CARD_STAGE:
            problems.append(
                f"constraints.{constraint.id}: runs at the {item.stage} stage, and a "
                f"hypothesis constrains only the {CARD_STAGE} stage"
            )
        else:
            problems.extend(
                f"constraints.{constraint.id}.{problem}"
                for problem in check_params(item, constraint.params, hyp, folds)
            )
    if problems:
        raise ValidationError("; ".join(problems))
