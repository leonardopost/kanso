"""The deterministic half of classification: what is computed before anything is asked.

Classification is one model call, and what that call is shown is decided here. Three
kinds of fact go into it, and nothing else does.

**What the workspace already holds.** Every certified strategy is a possible host, so
each one is described by what it trades — its sleeve hypothesis's mechanism, universe,
horizon and resolution, read from the bytes that hypothesis was registered under rather
than from whatever its file says today — together with how that overlaps the hypothesis
being classified. A construct that attaches to a sleeve can be chosen only where a
certified sleeve exists and one that attaches to the book only where the book holds
something, so which constructs can attach is computed rather than left to be inferred
from the catalogue.

**What can be measured.** The objective is not the model's choice: it follows from the
hypothesis and the construct's objective mode through the toolbox's own applicability
predicate. The table is therefore computed for both modes, and what is shown is the
objective each mode selects and the ranges its keep-rule parameters may be chosen from.
The card-stage gates are shown the same way, with every range resolved onto this
hypothesis's own scale so a bound written against its horizon or its fold count arrives
as a number. A bound that is unbounded arrives as `None`.

**Nothing a result would say.** No card metric, no certificate, no strategy source and
nothing measured of a certified strategy is computed here, because a classification
conditioned on results is a classification fitted to them. What a certified strategy
contributes is what it trades, never how well it traded.

A certified strategy whose sleeve hypothesis this workspace no longer holds is reported
with its trading facts absent rather than dropped: it is still a host a construct may
attach to, and saying so with empty facts is honest where inventing them would not be.
"""

from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING, Any, Final

from kanso.classify.construct import PORTFOLIO, catalogue
from kanso.criteria import applicable_objectives, resolve_bound
from kanso.criteria import catalogue as toolbox
from kanso.schemas import (
    CriteriaItem,
    Hypothesis,
    StrategyFile,
    is_duration,
    load_yaml,
    parse_duration,
    parse_yaml,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Sequence

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "attachable",
    "card_gates",
    "certified",
    "construct_catalogue",
    "features",
    "objectives",
]

STRATEGIES: Final = "strategies"
STRATEGY_FILE: Final = "strategy.yaml"
"""Where a composed strategy lives; its presence is what makes it a possible host."""

CARD_STAGE: Final = "card"
"""The one stage a hypothesis's own constraints run at."""

NO_HOST: Final = "none"
MODES: Final = ("absolute", "relative")
"""The two objective modes, so the table is total whatever construct is chosen."""


def features(ws: Workspace, store: StateStore, hyp: Hypothesis) -> dict[str, Any]:
    """Everything classification works out for itself, before it asks anything.

    The mechanism, horizon, resolution and universe are repeated here because they are
    the facts every other entry is computed against: an overlap is an overlap with this
    universe, and an objective applies to this horizon.
    """
    folds = ws.config.research.folds
    specs = certified(ws, store, hyp)
    return {
        "mechanism": hyp.mechanism,
        "horizon": hyp.horizon,
        "resolution": hyp.resolution,
        "universe": list(hyp.universe),
        "certified": specs,
        "attachable": attachable(ws, specs),
        "constructs": construct_catalogue(ws),
        "objectives": objectives(hyp, folds),
        "card_gates": card_gates(hyp, folds),
    }


def certified(ws: Workspace, store: StateStore, hyp: Hypothesis) -> list[dict[str, Any]]:
    """Every composed strategy of this workspace, and how it relates to `hyp`."""
    return [_spec(store, strategy, hyp) for strategy in _strategies(ws)]


def attachable(ws: Workspace, specs: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """The constructs that can be chosen now, each with the hosts open to it.

    A construct needing no host maps to an empty list; one that needs a host it cannot
    have is absent, because nothing in this workspace could host it today.
    """
    hosts = [str(spec["id"]) for spec in specs]
    open_to: dict[str, list[str]] = {}
    for item in catalogue(ws).items:
        if item.needs_host == NO_HOST:
            open_to[item.id] = []
        elif hosts:
            open_to[item.id] = [PORTFOLIO] if item.needs_host == PORTFOLIO else list(hosts)
    return open_to


def construct_catalogue(ws: Workspace) -> list[dict[str, Any]]:
    """The taxonomy itself: what each construct is and what it attaches to."""
    return [
        {
            "id": item.id,
            "description": item.description,
            "needs_host": item.needs_host,
            "params": item.params or {},
            "runnable": item.runnable,
        }
        for item in catalogue(ws).items
    ]


def objectives(hyp: Hypothesis, folds: int) -> dict[str, Any]:
    """The applicability table, per objective mode: what applies and what wins."""
    items = toolbox()
    table: dict[str, Any] = {}
    for mode in MODES:
        applies = applicable_objectives(hyp, mode)
        table[mode] = {
            "selected": next((found for _, found in applies), None),
            "applicable": [
                _measurable(items[found], hyp, folds, {"priority": priority})
                for priority, found in applies
            ],
        }
    return table


def card_gates(hyp: Hypothesis, folds: int) -> list[dict[str, Any]]:
    """The gates a hypothesis may constrain every one of its cards with."""
    return [
        _measurable(item, hyp, folds, {"required": bool(item.required)})
        for item in toolbox().values()
        if item.kind == "gate" and item.stage == CARD_STAGE
    ]


def _measurable(
    item: CriteriaItem, hyp: Hypothesis, folds: int, extra: dict[str, Any]
) -> dict[str, Any]:
    """One toolbox item as the model reads it: what it means and what it may be given."""
    return {
        "id": item.id,
        **extra,
        "meaningful_when": item.meaningful_when,
        "params": dict(item.params),
        "ranges": _ranges(item, hyp, folds),
    }


def _ranges(item: CriteriaItem, hyp: Hypothesis, folds: int) -> dict[str, dict[str, float | None]]:
    """Each declared range as two numbers on this hypothesis's scale; `None` is open."""
    return {
        name: {
            "min": _finite(resolve_bound(low, hyp, folds)),
            "max": _finite(resolve_bound(high, hyp, folds)),
        }
        for name, (low, high) in item.ranges.items()
    }


def _finite(value: float) -> float | None:
    return value if isfinite(value) else None


def _strategies(ws: Workspace) -> list[StrategyFile]:
    """Every `strategies/<id>/strategy.yaml`, in id order."""
    root = ws.path(STRATEGIES)
    if not root.is_dir():
        return []
    paths = [directory / STRATEGY_FILE for directory in sorted(root.iterdir())]
    return [load_yaml(StrategyFile, path) for path in paths if path.is_file()]


def _spec(store: StateStore, strategy: StrategyFile, hyp: Hypothesis) -> dict[str, Any]:
    """One certified strategy: what it is made of, and what it trades."""
    latest = strategy.latest()
    spec: dict[str, Any] = {
        "id": strategy.id,
        "version": latest.version,
        "sleeve": latest.sleeve.hyp_id,
        "attached": [
            {"construct": ref.construct, "hypothesis": ref.hyp_id} for ref in latest.attached
        ],
        "mechanism": None,
        "universe": None,
        "horizon": None,
        "resolution": None,
        "universe_overlap": None,
        "horizon_match": None,
        "resolution_match": None,
        "mechanism_match": None,
    }
    sleeve = _sleeve(store, latest.sleeve.hyp_id)
    if sleeve is None:
        return spec
    return spec | {
        "mechanism": sleeve.mechanism,
        "universe": list(sleeve.universe),
        "horizon": sleeve.horizon,
        "resolution": sleeve.resolution,
        "universe_overlap": sorted(set(sleeve.universe) & set(hyp.universe)),
        "horizon_match": _same_duration(sleeve.horizon, hyp.horizon),
        "resolution_match": _same_resolution(sleeve.resolution, hyp.resolution),
        "mechanism_match": sleeve.mechanism == hyp.mechanism,
    }


def _sleeve(store: StateStore, hyp_id: str) -> Hypothesis | None:
    """The hypothesis a certified sleeve was registered as, from the bytes it pinned."""
    row = store.connection.execute(
        "SELECT hypothesis_sha FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    sha = None if row is None else row["hypothesis_sha"]
    if sha is None or not store.has_blob(str(sha)):
        return None
    return parse_yaml(Hypothesis, store.get_blob(str(sha)).decode("utf-8"), f"{hyp_id} pin")


def _same_duration(one: str, other: str) -> bool:
    """Two durations, compared by length rather than by spelling."""
    return parse_duration(one, "horizon") == parse_duration(other, "horizon")


def _same_resolution(one: str, other: str) -> bool:
    """Two resolutions: the same grain, or two spellings of the same bar size."""
    if is_duration(one) and is_duration(other):
        return _same_duration(one, other)
    return one == other
