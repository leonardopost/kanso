"""What makes a `hypothesis.yaml` admissible, checked in one pass over one file.

The file's own arithmetic — the duration grammar, the embargo between the research and
certification windows, their ordering and disjointness, the resolution and cost model
being answerable from the data the hypothesis asks for, and `strategy_integrity` among a
classified hypothesis's constraints — belongs to the model and is enforced when the
document is parsed. Everything else needs the workspace, and that is this module:

* the file lives at `hypotheses/<id>/`, so the id in the document and the directory that
  holds it are the same word. Directory names are unique, so this is what makes ids
  unique too, and it is what lets every later command find a hypothesis by its id alone;
* the id is not `portfolio`. The id grammar admits it, but a certified sleeve composes a
  strategy named after its hypothesis and `construct.host` spells the book itself as
  `portfolio`, so a sleeve of that name would make a strategy the host field could no
  longer tell from the book. The word is reserved here, where a hypothesis enters the
  workspace, rather than at the seam where the two meanings collide;
* every data requirement names a type the workspace knows — the three market-data types,
  or one an extension registered;
* every universe id resolves to an instrument as of the research window's first day. A
  manual entry answers on its own, a cached resolution answers when it was made as of
  that date, and anything else needs the configured reference adapter. An id that is
  unknown, ambiguous across venues, delisted before that date or listed after it fails,
  and every failing id is reported together. The question is asked and nothing is
  written — not the catalog's instrument store, not the cache: a validation that left
  something behind would not be one, and the store is written by `kanso data instruments
  resolve` alone;
* the venues those instruments trade on resolve to a complete cost model, and to one
  account currency. A spread taken from quotes needs quotes; a fixed spread needs its
  width; a universe spanning two account currencies would have a leg priced at a rate
  nothing in the workspace records, so it is refused;
* when classification has been written, its construct is in the catalogue, its host is
  present exactly when the construct needs one and names a certified strategy, its
  parameters are ones that construct declares with values inside the sets it declares
  them over, its objective applies to this hypothesis and its parameters are inside the
  toolbox's ranges, and every constraint is a card-stage gate whose parameters are inside
  theirs. The construct's own parameters are checked by asking the construct, which is the
  same call the runner makes when it builds the harness: a classification this command
  calls admissible is one the first card of a run does not reject.

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
from kanso.data import registry
from kanso.data.instruments import resolve_universe
from kanso.data.types import data_types
from kanso.errors import ValidationError
from kanso.hyp.scaffold import HYPOTHESES
from kanso.nautilus import adapters
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
    _check_id(hyp)
    _check_location(ws, path, hyp)
    _check_data_requirements(ws, hyp)
    instruments = resolve_universe(ws, hyp.universe, hyp.windows.research.start, record=False)
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

    The broker is named in `kanso.toml` and its declaration is asked of whichever adapter
    provides it, so this reads a broker's account type, currency and costs without naming
    one. A workspace configured for a broker no adapter here provides inherits nothing and
    falls back to the shipped defaults, which the resolved model records as its origin: a
    missing adapter must not silently change the numbers a card is measured with.
    """
    overrides = _venue_overrides(ws)
    quotes = QUOTE_TYPE in hyp.data_requirements
    broker = ws.config.research.broker
    models = {
        venue: resolve_venue_model(
            venue,
            broker=broker,
            declaration=adapters.venue_declaration(broker, venue),
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


def _check_id(hyp: Hypothesis) -> None:
    """`portfolio` is the book, so it is not a hypothesis.

    The id grammar admits the word and both meanings would be legitimate: a certified
    sleeve composes a strategy named after its hypothesis, and `construct.host` names the
    book itself with that same word. A workspace holding both has a `host: portfolio` that
    means two things, and the construct reads it as the book — so an overlay properly
    attached to the sleeve is refused for the portfolio it was never on. Reserving the
    word costs an operator one id and keeps `construct.host` unambiguous.
    """
    if hyp.id == PORTFOLIO:
        raise ValidationError(
            f"id: {PORTFOLIO!r} is reserved: it is how a construct attached to the book names "
            f"its host, and a certified sleeve of that name would compose a strategy nothing "
            f"could tell from the book",
            remedy=f"choose another id, and rename {HYPOTHESES}/{PORTFOLIO}/ to match",
        )


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

    Discovery imports the workspace's extensions, and asking each registered adapter for
    its loaders imports those, which is when either registers a custom type of its own. So
    a type a vendor adapter or an extension introduces is known to validation and not only
    once something has loaded with it. Neither costs a credential: an adapter hands out
    loader factories and none is built here.
    """
    extensions = ext.discover(ws.root, ws.config.extensions_paths)
    registry.adapter_loaders(ws, extensions)
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
    """The construct's objective mode, once it exists, its params fit and its host is right.

    The parameters are checked by asking the construct rather than against a copy of its
    declarations kept here, so this command and the harness the runner builds refuse the
    same classification in the same words: a parameter the construct does not declare, or a
    value outside its set, is refused where the file is judged rather than inside the first
    card of a run.
    """
    construct = construct_catalogue(ws).get(ref.id)
    construct.check_params(ref.params)
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
