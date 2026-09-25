"""The gate toolbox: the tests a card, a certificate, a paper stage or a live stage runs.

A gate carries no default and no threshold. Which gates run and with which values is a
runtime decision — the classifier's for the card stage, the planner's for everything after
— taken inside the ranges the library declares. What lives here is only the arithmetic of
each test and the honesty rule that goes with it: a gate that cannot reach its context
passes and records why it judged nothing, so an absent deployed book or an unset threshold
never manufactures a failure, and a certificate's verdict is the conjunction of the gates
that actually ran.

The card-stage gates judge a research card. The certification gates judge the pinned
strategy over the certification window, several of them against the research window as
well: an out-of-sample result is only meaningful beside the in-sample one it is supposed
to survive. Two of them are deliberately cheap in a different way — `cost_stress`
recomputes from the fills the runner already recorded instead of running the engine again,
which is possible only because costs are applied once, in the extraction, and never inside
the simulated venue.

A gate whose test is more than arithmetic on the run in hand lives in a module of its own
beside this one, because it needs machinery the rest do not: `param_plateau` re-runs the
subject once per perturbation and so reads the re-run the certification runner hands it,
`parity_replay` compares two code paths' order intents, which is a replay's record rather
than a run's, and the four paper and live gates — `paper_forward`, `live_drift`,
`daily_loss_kill` and `fill_quality_drift` — read the stage record the monitor assembles,
because a run says nothing about the deployment that produced it.

Nothing is declared here and left unimplemented: every gate in the library resolves.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from hashlib import sha256
from math import e, fsum, inf, sqrt
from statistics import NormalDist, median
from typing import ClassVar, Final

import numpy as np

from kanso.criteria.context import (
    Gate,
    GateContext,
    count,
    number,
    skipped,
    text,
    verdict,
)
from kanso.criteria.integrity import check as check_integrity
from kanso.criteria.objectives import REGISTRY, SHARPE_FAMILY, Objective, benchmark_level
from kanso.criteria.quantities import (
    correlation,
    drawdown_pct,
    mean,
    moments,
    periods_per_year,
    stdev,
    variance,
    years,
)
from kanso.criteria.run import BPS, NS_PER_SECOND, CardRun, Fill, Held, Trade, day_of
from kanso.schemas import GateResult, parse_duration

PERCENT: Final = 100.0
"""A share of capital is reported the way a risk limit states one."""

NS_PER_DAY: Final = 86_400 * NS_PER_SECOND

EULER_MASCHERONI: Final = 0.5772156649015329
"""The constant in the expected maximum of a sample of Sharpe ratios."""

BLOCK: Final = 1000
"""Bootstrap replications resampled at a time, so a large `n` stays in memory."""

NO_OBJECTIVE = "the hypothesis carries no objective to measure"
NO_RESEARCH_RUN = "no research-window run was supplied to compare against"


def _objective(ctx: GateContext) -> Objective | None:
    ref = ctx.hyp.objective
    return None if ref is None else REGISTRY.get(ref.id)


def _metric(
    objective: Objective,
    run: CardRun,
    ctx: GateContext,
    host: CardRun | None,
    benchmark: CardRun | None,
) -> float:
    return objective.compute(run, ctx.research_folds, host, benchmark)[0]


class _StrategyIntegrity:
    """The static half of the anti-cheat boundary: imports, identifiers and scope."""

    id: ClassVar[str] = "strategy_integrity"

    def evaluate(self, ctx: GateContext) -> GateResult:
        if ctx.lane_dir is None:
            return skipped(self.id, "no lane directory was supplied, so nothing was inspected")
        problems = check_integrity(
            ctx.lane_dir, ctx.pinned, sized=ctx.hyp.sizing is not None, construct=ctx.construct
        )
        return verdict(
            self.id,
            not problems,
            {"n_problems": len(problems), "problems": problems[:20]},
        )


class _MinTrades:
    """Enough closed trades overall, and at least one in every fold."""

    id: ClassVar[str] = "min_trades"

    def evaluate(self, ctx: GateContext) -> GateResult:
        minimum = count(ctx, "min")
        if minimum is None:
            return skipped(self.id, "no minimum was chosen, so no count was required")
        per_fold = [len(fold.trades) for fold in ctx.run.folds(ctx.research_folds)]
        total = len(ctx.run.trades)
        return verdict(
            self.id,
            total >= minimum and all(n >= 1 for n in per_fold),
            {"n_trades": total, "min": minimum, "trades_per_fold": per_fold},
        )


class _MaxHold:
    """No position held longer than the hypothesis allows, in calendar days or trading days.

    A thesis that says "switches are less than a month apart" is a sentence a model reads;
    this is the same sentence as a refusal, in whichever unit the operator states it:
    `days` counts calendar days, `trading_days` counts the sessions a position is held
    across — the run's period ends it spans, which at a daily return period are one per
    day that traded, so a weekend or a holiday inside a hold adds nothing. Either limit or
    both may be set, and a position over either is refused.

    A closed position is timed from its entry fill to its exit fill — exact instants, so a
    clock change counts for what it is and a reversal in one instrument is two positions,
    as the engine records it. A position still open when the window closes is the one
    `Trade.closed_ns` cannot see and the one a strategy that stops switching leaves
    behind: it is timed from its first fill to the window's last period end.

    An attached construct's fills are not separable from its host's, so it is timed on
    what it added to its host at each period end (`_own`): every stretch of consecutive
    period ends holding the same sign is one position, its calendar length whole periods
    rounded from the span and its trading length the ends it spans. That reading sees only
    period ends, so it is a floor on the hold, never a ceiling, and the yaml says so.
    """

    id: ClassVar[str] = "max_hold"

    def evaluate(self, ctx: GateContext) -> GateResult:
        days, trading = count(ctx, "days"), count(ctx, "trading_days")
        if days is None and trading is None:
            return skipped(self.id, "no limit was chosen, so no position was timed")
        if trading is not None and not _counts_sessions(ctx.run):
            return skipped(
                self.id,
                "trading days are the period ends of a daily return period, and this run's "
                f"period is {ctx.run.period!r} over {len(ctx.run.period_ends_ns)} period ends",
            )
        if ctx.host_run is None:
            measured, held = "fills", _held_from_fills(ctx.run)
        else:
            measured, held = "period_ends", _held_from_marks(ctx.run, _own(ctx))
        if not held:
            return skipped(self.id, "no position was opened, so nothing was timed")
        over = [
            (calendar, sessions)
            for calendar, sessions in held
            if (days is not None and calendar > days)
            or (trading is not None and sessions > trading)
        ]
        return verdict(
            self.id,
            not over,
            {
                "days": days,
                "trading_days": trading,
                "measured_on": measured,
                "longest_days": round(max(calendar for calendar, _ in held), 4),
                "longest_trading_days": max(sessions for _, sessions in held),
                "n_positions": len(held),
                "n_over": len(over),
            },
        )


def _counts_sessions(run: CardRun) -> bool:
    """Whether a period end is a trading day: a daily return period, with ends to count."""
    return bool(run.period_ends_ns) and parse_duration(run.period, "period") == timedelta(days=1)


def _held_from_fills(run: CardRun) -> list[tuple[float, int]]:
    """Every position's calendar and trading days, fill to fill, an open one to the close."""
    spans = [(trade.opened_ns, trade.closed_ns) for trade in run.trades]
    ends = run.period_ends_ns
    if ends:
        closes = ends[-1]
        last_closed: dict[str, int] = {}
        for trade in run.trades:
            last_closed[trade.instrument_id] = max(
                last_closed.get(trade.instrument_id, 0), trade.closed_ns
            )
        still_open = {item.instrument_id for item in run.held if item.ts_ns >= closes}
        for instrument in sorted(still_open):
            opened = [
                fill.ts_ns
                for fill in run.fills
                if fill.instrument_id == instrument and fill.ts_ns > last_closed.get(instrument, -1)
            ]
            if opened:
                spans.append((min(opened), closes))
    return [
        ((closed - opened) / NS_PER_DAY, _sessions(ends, opened, closed))
        for opened, closed in spans
    ]


def _sessions(ends: Sequence[int], opened: int, closed: int) -> int:
    """The period ends a position was held across: after its entry, up to its exit."""
    return bisect_right(ends, closed) - bisect_right(ends, opened)


def _held_from_marks(run: CardRun, marked: Sequence[Held]) -> list[tuple[float, int]]:
    """Every stretch of consecutive period ends held with one sign, and the ends it spans."""
    ends = run.period_ends_ns
    if not ends or not marked:
        return []
    period_ns = parse_duration(run.period, "period").total_seconds() * NS_PER_SECOND
    stretches: dict[tuple[str, int], set[int]] = {}
    for item in marked:
        position = min(bisect_left(ends, item.ts_ns), len(ends) - 1)
        sign = 1 if item.qty > 0 else -1
        stretches.setdefault((item.instrument_id, sign), set()).add(position)
    held: list[tuple[float, int]] = []
    for positions in stretches.values():
        ordered = sorted(positions)
        first = previous = ordered[0]
        for index in ordered[1:]:
            if index != previous + 1:
                held.append(_stretch(ends, first, previous, period_ns))
                first = index
            previous = index
        held.append(_stretch(ends, first, previous, period_ns))
    return held


def _stretch(ends: Sequence[int], first: int, last: int, period_ns: float) -> tuple[float, int]:
    """A stretch's calendar days in whole periods, and the period ends it spans."""
    whole = round((ends[last] - ends[first]) / period_ns) + 1
    return whole * period_ns / NS_PER_DAY, last - first + 1


class _PositionSize:
    """What a position was worth while it was held, as a share of the capital.

    The one thing a hypothesis could not say. `risk_limits` are three ceilings — a
    position may not exceed `max_position_pct`, the book may not exceed `max_leverage` —
    and a strategy that holds a tenth of what its operator asked for satisfies every one
    of them. This gate carries a floor as well, so "a position is worth about this much"
    becomes a refusal rather than a sentence in a brief no code reads.

    Measured on `run.held`: quantity times that period's mark, per instrument, per period
    end, over the capital the window opened at, which a `CardRun` guarantees is above
    zero. Neither notional a run already carried says this. `Fill.notional` is traded value
    struck at one price, so it says nothing about what was held afterwards, and a strategy
    that tops up in three orders looks like three small positions. `Trade.notional` is
    `peak_qty x avg_open`, an opening cost basis: it is biased upward by exactly the
    strategy that rebalances toward a target as the price falls — the behaviour an
    operator asking for a fixed size most likely wants — and it is blind to the drift of a
    position entered once and left alone, which is the behaviour they least want. A gate
    built on either would refuse the compliant strategy and pass the drifting one.

    Every held period is judged, not a median: a size instruction is broken by one period
    that breaks it, and a strategy that sizes correctly has none. An instrument at zero is
    not a position and is not judged, so a strategy that is flat is out of scope here —
    whether it should be deployed at all is a different question and belongs to a
    different gate.
    """

    id: ClassVar[str] = "position_size"

    def evaluate(self, ctx: GateContext) -> GateResult:
        low, high = number(ctx, "min_pct"), number(ctx, "max_pct")
        if low is None and high is None:
            return skipped(self.id, "no band was chosen, so no size was required")
        floor = 0.0 if low is None else low
        ceiling = inf if high is None else high
        if floor > ceiling:
            return verdict(
                self.id,
                False,
                {
                    "refused": "min_pct is above max_pct, so no position can satisfy it",
                    "min_pct": floor,
                    "max_pct": high,
                },
            )
        if ctx.hyp.sizing is not None:
            return self._on_entry_fills(ctx, floor, ceiling, high)
        marked = ctx.run.held if ctx.host_run is None else _own(ctx)
        if not marked:
            return skipped(
                self.id, "no instrument was held at any period end, so nothing was sized"
            )
        shares = [held.notional / ctx.run.capital * PERCENT for held in marked]
        outside = [share for share in shares if not floor <= share <= ceiling]
        return verdict(
            self.id,
            not outside,
            {
                "min_pct": floor,
                "max_pct": high,
                "n_held": len(shares),
                "n_outside": len(outside),
                "smallest_pct": min(shares),
                "largest_pct": max(shares),
                "median_pct": median(shares),
            },
        )

    def _on_entry_fills(
        self, ctx: GateContext, floor: float, ceiling: float, high: float | None
    ) -> GateResult:
        """Under a sizing rule: every entry, as a share of the budget it was sized to.

        A period-end mark is the wrong basis here — a leveraged leg drifts through any
        band between two closes — so the judgement is on what the harness actually
        placed. Fills are gathered per order first, because the venue fills a market
        order past a quarter of the bar's volume as two events; for an attached construct
        the host's orders are subtracted by identity and the remainder are the candidate's.
        """
        budget = ctx.hyp.sizing.budget  # type: ignore[union-attr]
        orders = _orders(ctx.run.fills)
        if ctx.host_run is not None:
            hosts = _orders(ctx.host_run.fills)
            orders = [order for order in orders if order not in hosts]
        entries = _entries(orders)
        if not entries:
            return skipped(self.id, "no entry was filled, so nothing was sized")
        shares = [notional / budget * PERCENT for notional in entries]
        outside = [share for share in shares if not floor <= share <= ceiling]
        return verdict(
            self.id,
            not outside,
            {
                "basis": "entry_fills",
                "budget": budget,
                "min_pct": floor,
                "max_pct": high,
                "n_entries": len(shares),
                "n_outside": len(outside),
                "smallest_pct": min(shares),
                "largest_pct": max(shares),
                "median_pct": median(shares),
            },
        )


def _orders(fills: Sequence[Fill]) -> list[tuple[int, str, str, float, float]]:
    """Fills gathered per order — one instant, one instrument, one side — as
    `(ts_ns, instrument_id, side, signed qty, notional)`, in time order."""
    gathered: dict[tuple[int, str, str], tuple[float, float]] = {}
    for fill in fills:
        key = (fill.ts_ns, fill.instrument_id, fill.side)
        qty, notional = gathered.get(key, (0.0, 0.0))
        signed = -fill.qty if fill.side == "SELL" else fill.qty
        gathered[key] = (qty + signed, notional + abs(fill.qty) * fill.px)
    return [
        (ts, instrument_id, side, round(qty, 9), round(notional, 6))
        for (ts, instrument_id, side), (qty, notional) in sorted(gathered.items())
    ]


def _entries(orders: Sequence[tuple[int, str, str, float, float]]) -> list[float]:
    """The notional of every order that opened or grew a position, by a running count."""
    position: dict[str, float] = {}
    entries: list[float] = []
    for _, instrument_id, _, qty, notional in orders:
        before = position.get(instrument_id, 0.0)
        after = before + qty
        position[instrument_id] = after
        if abs(after) > abs(before):
            entries.append(notional)
    return entries


def _own(ctx: GateContext) -> tuple[Held, ...]:
    """What the candidate added to its host, marked at the same prices.

    A modifier's run is the host and the modifier together — the runner reads every
    position in the cache — so judging `run.held` would judge the host's sizing as much as
    the candidate's. The host is measured over the same window at the same period ends, so
    the quantity it held is subtracted and the remainder re-marked at the price the pair
    was already marked at. A period where the modifier changed nothing holds nothing of
    its own and is not judged.
    """
    host = ctx.host_run
    if host is None:  # pragma: no cover - the caller checks this first
        return ctx.run.held
    theirs = {(item.ts_ns, item.instrument_id): item.qty for item in host.held}
    mine: list[Held] = []
    for item in ctx.run.held:
        qty = item.qty - theirs.get((item.ts_ns, item.instrument_id), 0.0)
        if not qty:
            continue
        price = abs(item.notional / item.qty)
        mine.append(replace(item, qty=qty, notional=abs(qty) * price))
    return tuple(mine)


class _LegEdge:
    """One leg's own closed spells earn their place, in every fold that closed one.

    A pair's edge can live in one leg while the other rides along — a hedge that pays its
    spread at every switch and returns nothing of its own — and a card's metric, struck on
    the book, cannot tell the two apart. A thesis that says each leg carries edge is a
    sentence a model reads; this is that sentence as a refusal on one named leg: within
    every research fold, the Sharpe of that leg's closed spells is at or above
    `min_sharpe`.

    A spell is one of the leg's closed positions, as the runner records a `Trade`, and its
    return is `pnl_net / notional` — net of the leg's own fill costs, in units of what the
    position opened at. It belongs to the fold its close falls in, as every trade does:
    the gate reads each fold's own `trades`, which `CardRun.between` cut at the exact
    instant the fold edge falls on, never the fold's day-rounded `bounds` — an edge inside
    a day would otherwise put that day's spells in two folds. A spell open across a fold
    edge is counted by the fold that closed it, and one still open when the window closes
    is counted by no fold, because a run carries no mark for a single leg
    (`docs/backlog.md`). The Sharpe is annualised by the spells the fold actually held per
    year, as `bootstrap` annualises the trades it resamples; a fold whose spells cannot
    vary — one spell, spells that all returned the same, or spells that returned the same
    but for the last bits of the arithmetic (`SPELL_SPREAD_FLOOR`) — scores zero, as every
    Sharpe in the toolbox does, so a leg that switched once in a fold clears only a floor
    at or below zero. A fold that closed no spell is not judged, and a run in which the leg
    never closed one is skipped rather than failed: the leg not trading is a different
    fault, and `min_trades` is where it is refused. A third skip — the leg closed spells,
    but none inside the window's folds — can arise only for a run whose trades were not
    cut to its window, which the runner's never are.

    An attached construct's trades are its host's too, so the closed positions the host's
    own run also closed — the same instrument, instants, quantity and prices — are
    subtracted first, by identity: a spell the candidate altered in any of those is judged
    whole, and one identical to the host's is not judged at all. The remainder are what
    the candidate's rule did to the leg.

    Every one of the three skips carries its reason in its evidence as well as in its skip,
    with whatever it did reach: a card records a gate's evidence, and a `leg_edge` that
    passed with an empty one told the operator that the leg was judged and cleared, which is
    the opposite of what happened.
    """

    id: ClassVar[str] = "leg_edge"

    def evaluate(self, ctx: GateContext) -> GateResult:
        leg, floor = text(ctx, "leg"), number(ctx, "min_sharpe")
        if leg is None or floor is None:
            return _unjudged("no leg or no floor was chosen, so no spell was judged", {})
        theirs = _host_keys(ctx)
        spells = _spells(ctx.run.trades, leg, theirs)
        chosen: dict[str, object] = {"leg": leg, "min_sharpe": floor}
        if not spells:
            return _unjudged(
                f"{leg} closed no spell of its own, so nothing was judged",
                {**chosen, "n_spells": 0},
            )
        judged: list[float | None] = []
        counted: list[int] = []
        for fold in ctx.run.folds(ctx.research_folds):
            inside = _spells(fold.trades, leg, theirs)
            counted.append(len(inside))
            judged.append(_spell_sharpe(inside, fold.window) if inside else None)
        measured = [value for value in judged if value is not None]
        if not measured:
            return _unjudged(
                f"{leg} closed no spell inside the window's folds, so nothing was judged",
                {**chosen, "folds": judged, "spells_per_fold": counted, "n_spells": len(spells)},
            )
        below = [value for value in measured if value < floor]
        return verdict(
            self.id,
            not below,
            {
                "leg": leg,
                "min_sharpe": floor,
                "folds": judged,
                "spells_per_fold": counted,
                "n_spells": len(spells),
                "n_below": len(below),
                "worst": min(measured),
            },
        )


def _unjudged(reason: str, evidence: Mapping[str, object]) -> GateResult:
    """A `leg_edge` skip whose evidence says what its skip says, and what it did reach.

    `skipped` records the reason in its own field and leaves the evidence empty, which is
    right for a gate read through `kanso research card`: the renderer prints the reason in
    the evidence's place. A card's stored gate row is read as evidence alone, though, and a
    `leg_edge` PASS with `{}` — one was recorded in a live workspace — says nothing at all.
    """
    return GateResult.model_validate(
        {
            "id": _LegEdge.id,
            "pass": True,
            "skipped": reason,
            "evidence": {"reason": reason, **evidence},
        }
    )


TradeKey = tuple[str, int, int, float, float, float]


def _spells(trades: Sequence[Trade], leg: str, theirs: frozenset[TradeKey]) -> list[Trade]:
    """`leg`'s closed positions among `trades` that opened something and are not the host's."""
    return [
        trade
        for trade in trades
        if trade.instrument_id == leg and trade.notional > 0 and _trade_key(trade) not in theirs
    ]


def _host_keys(ctx: GateContext) -> frozenset[TradeKey]:
    """The closed positions the host's own run closed, by identity; none without a host."""
    if ctx.host_run is None:
        return frozenset()
    return frozenset(_trade_key(trade) for trade in ctx.host_run.trades)


def _trade_key(trade: Trade) -> TradeKey:
    return (
        trade.instrument_id,
        trade.opened_ns,
        trade.closed_ns,
        round(trade.qty, 9),
        round(trade.avg_open, 9),
        round(trade.avg_close, 9),
    )


SPELL_SPREAD_FLOOR: Final = 1e-12
"""A spread of spell returns no wider than this is no spread: the fold cannot vary.

A spell's return is `pnl_net / notional`, and two spells that earned the same thing on the
same notional can still divide to figures a few units in the last place apart — a spread
around 1e-16 at a return of a few tenths. Dividing a mean by one of those is not a Sharpe.
Measured on a live card, a fold of four spells whose returns agreed to fifteen digits
scored -6113058453210397.0. This floor sits four orders of magnitude above that arithmetic
and six below the narrowest spread the suite asserts is real — a pair of returns 1e-6
apart — so it catches the one without reaching the other. It is an absolute spread, which
holds while returns are fractions of one: a pair this close at a return in the thousands
divides to a figure it would not catch, and `pnl_net / notional` is not that.
"""


def _spell_sharpe(spells: Sequence[Trade], window: tuple[date, date]) -> float:
    """Annualised Sharpe of per-spell returns; spells that cannot vary score zero.

    Cannot vary is one spell, spells that all returned the same, and spells that returned
    the same but for the last bits of the divisions that struck them. The bootstrap's
    `_sharpe_of` guards the same division at zero; this one guards it at
    `SPELL_SPREAD_FLOOR`, because a denormal spread is not variation either.
    """
    returns = [trade.pnl_net / trade.notional for trade in spells]
    dispersion = stdev(returns)
    if dispersion <= SPELL_SPREAD_FLOOR:
        return 0.0
    return mean(returns) / dispersion * sqrt(len(returns) / years(window))


class _MaxDrawdown:
    """Peak-to-trough equity inside the hypothesis's own drawdown limit."""

    id: ClassVar[str] = "max_drawdown"

    def evaluate(self, ctx: GateContext) -> GateResult:
        limit = ctx.hyp.risk_limits.max_drawdown_pct
        observed = drawdown_pct(ctx.run)
        return verdict(
            self.id,
            observed <= limit,
            {"max_drawdown_pct": observed, "limit_pct": limit},
        )


NO_FLOOR: Final = "the hypothesis declares no book.maintenance_pct, so there is no floor to hold"
NEVER_HELD: Final = (
    "no period end held a position valued at its period's adverse extreme, so no margin "
    "was ever called on"
)


def worst_margin(run: CardRun) -> tuple[float, int] | None:
    """The lowest maintenance ratio a run recorded, in percent, and the period end it fell at.

    `None` when no end held anything — a flat run, or one recorded without the series.
    """
    held = [
        (ratio * PERCENT, end)
        for ratio, end in zip(run.worst_ratio, run.period_ends_ns, strict=False)
        if ratio is not None
    ]
    return min(held, key=lambda item: item[0]) if held else None


class _MaintenanceMargin:
    """The book's equity over its gross stays above the floor the hypothesis declares.

    Each period's end-of-period holdings are valued at the period's adverse extreme — a
    long at its lowest low, a short at its highest high — so the ratio is a floor on what
    a margin desk would have read, like `max_hold`'s, not an intra-period measurement of
    what was held when. The floor is the operator's `book.maintenance_pct`, and the gate
    carries no parameter of its own, as `max_drawdown` carries none.
    """

    id: ClassVar[str] = "maintenance_margin"

    def evaluate(self, ctx: GateContext) -> GateResult:
        floor = None if ctx.hyp.book is None else ctx.hyp.book.maintenance_pct
        if floor is None:
            return skipped(self.id, NO_FLOOR)
        worst = worst_margin(ctx.run)
        if worst is None:
            return skipped(self.id, NEVER_HELD)
        observed, at_ns = worst
        return verdict(
            self.id,
            observed >= floor,
            {"worst_ratio_pct": observed, "floor_pct": floor, "at_ns": at_ns},
        )


class _EmbargoedWindow:
    """The objective survives the embargo: positive out of sample, and not a collapse."""

    id: ClassVar[str] = "embargoed_window"

    def evaluate(self, ctx: GateContext) -> GateResult:
        fraction = number(ctx, "min_fraction")
        if fraction is None:
            return skipped(self.id, "no minimum fraction was chosen, so no floor was required")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.research_run is None:
            return skipped(self.id, NO_RESEARCH_RUN)
        certified = _metric(objective, ctx.run, ctx, ctx.host_run, ctx.benchmark_run)
        researched = _metric(
            objective, ctx.research_run, ctx, ctx.host_research_run, ctx.benchmark_research_run
        )
        return verdict(
            self.id,
            certified > 0 and certified >= fraction * researched,
            {
                "objective": objective.id,
                "certification": certified,
                "research": researched,
                "min_fraction": fraction,
            },
        )


class _WalkForwardConsistency:
    """Most research folds positive, and the certification fold not the worst of them."""

    id: ClassVar[str] = "walk_forward_consistency"

    def evaluate(self, ctx: GateContext) -> GateResult:
        required = count(ctx, "min_positive_folds")
        if required is None:
            return skipped(self.id, "no fold count was chosen, so no consistency was required")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.research_run is None:
            return skipped(self.id, NO_RESEARCH_RUN)
        folds = objective.fold_values(
            ctx.research_run,
            ctx.research_folds,
            ctx.host_research_run,
            ctx.benchmark_research_run,
        )
        certified = _metric(objective, ctx.run, ctx, ctx.host_run, ctx.benchmark_run)
        positive = sum(1 for value in folds if value > 0)
        return verdict(
            self.id,
            positive >= required and certified > min(folds),
            {
                "objective": objective.id,
                "folds": list(folds),
                "positive_folds": positive,
                "min_positive_folds": required,
                "certification": certified,
            },
        )


class _DeflatedSharpe:
    """The research Sharpe, deflated by how many trials went into selecting it.

    The estimate deflated is the one selection acted on — the research-window metric the
    keep rule compared. The expected maximum it is measured against is built from the
    trials' own metrics: their count is how many candidates the maximum was taken over
    and their spread is the distribution it was taken from, so both must describe the
    same set or the bar is measured against a search that never happened. That set is
    `trial_metrics`, which excludes a crash and a card that placed no order — those are
    edits that failed rather than candidates the selection could have chosen — and includes
    one refused as redundant, which is a candidate the selection ranked and declined. The
    length,
    skewness and kurtosis of the research return series supply the sampling distribution.
    Both the estimate and the spread are taken back out of annualised units first, so the
    deflation is done in the units the return series was actually sampled in.
    """

    id: ClassVar[str] = "deflated_sharpe"

    def evaluate(self, ctx: GateContext) -> GateResult:
        floor = number(ctx, "min_dsr")
        if floor is None:
            return skipped(self.id, "no minimum was chosen, so no deflation was required")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if objective.id not in SHARPE_FAMILY:
            return skipped(
                self.id,
                f"the objective {objective.id} is not a Sharpe, so no card computed one to deflate",
            )
        if ctx.research_run is None:
            return skipped(self.id, NO_RESEARCH_RUN)
        metrics = list(ctx.trial_metrics)
        if len(metrics) < 2:
            return skipped(self.id, "fewer than two trials, so the trial spread is unknown")
        returns = ctx.research_run.returns
        shape = moments(returns)
        if len(returns) < 3 or shape is None:
            return skipped(self.id, "the research return series is too short or does not vary")
        scale = sqrt(periods_per_year(ctx.research_run))
        estimate = (
            _metric(
                objective, ctx.research_run, ctx, ctx.host_research_run, ctx.benchmark_research_run
            )
            / scale
        )
        expected = self._expected_maximum(
            variance(metrics) / periods_per_year(ctx.research_run), len(metrics)
        )
        skewness, kurtosis = shape
        denominator = 1 - skewness * estimate + (kurtosis - 1) / 4 * estimate**2
        if denominator <= 0:
            return skipped(self.id, "the sampling variance of the estimate is not positive")
        dsr = NormalDist().cdf((estimate - expected) * sqrt(len(returns) - 1) / sqrt(denominator))
        return verdict(
            self.id,
            dsr >= floor,
            {
                "dsr": dsr,
                "min_dsr": floor,
                "sharpe": estimate,
                "expected_maximum": expected,
                "trials": len(metrics),
                "skew": skewness,
                "kurtosis": kurtosis,
            },
        )

    @staticmethod
    def _expected_maximum(trial_variance: float, n_trials: int) -> float:
        """The Sharpe a search of this width is expected to produce from pure noise."""
        trials = max(n_trials, 2)
        normal = NormalDist()
        return sqrt(trial_variance) * (
            (1 - EULER_MASCHERONI) * normal.inv_cdf(1 - 1 / trials)
            + EULER_MASCHERONI * normal.inv_cdf(1 - 1 / (trials * e))
        )


def stressed(run: CardRun, multiplier: float) -> CardRun:
    """The same run with every recorded cost made worse by a multiple, re-applied to its own
    series.

    Costs are applied once, by the runner, in the extraction that produced this run, so
    stressing them is arithmetic on the recorded fills rather than another backtest. A charge
    is multiplied; a rebate — the negative charge a venue model's `maker_bps` can put on a
    fill that rested — is divided, so under a multiple of one or more every fill costs more
    or earns less, and no stress ever makes a rebate pay better. A fill is charged to the
    return period it falls in; a fill after the last period end changed no return and so
    changes none here. A book policy's `carry` is a rate on borrowed notional and not a fill
    cost, so it is left as recorded. Under a monthly reset the transfers stand as struck and
    the cushion as recorded, so the extra cost stays in the book across months where a real
    reset would have absorbed it at the next turn: a stressed run's returns are exact, and
    its equity and drawdown are the more conservative for it.
    """
    ends = run.period_ends_ns
    added = [0.0] * len(ends)
    for fill in run.fills:
        index = bisect_left(ends, fill.ts_ns)
        if index < len(ends):
            added[index] += _surcharge(fill.cost, multiplier)
    running = 0.0
    equity: list[float] = []
    for value, charge in zip(run.equity, added, strict=True):
        running += charge
        equity.append(value - running)
    return replace(
        run,
        returns=tuple(r - charge for r, charge in zip(run.returns, added, strict=True)),
        equity=tuple(equity),
        trades=tuple(_stressed_trade(trade, multiplier) for trade in run.trades),
        fills=tuple(replace(f, cost=_worsened(f.cost, multiplier)) for f in run.fills),
    )


def _worsened(cost: float, multiplier: float) -> float:
    """One recorded fill cost under a cost multiple: a charge grows, a rebate shrinks."""
    return cost * multiplier if cost >= 0.0 else cost / multiplier


def _surcharge(cost: float, multiplier: float) -> float:
    """What a cost multiple adds to one recorded fill cost, struck for a charge exactly as it
    always was: the charge times the multiple less one."""
    return cost * (multiplier - 1.0) if cost >= 0.0 else cost / multiplier - cost


def _stressed_trade(trade: Trade, multiplier: float) -> Trade:
    """A closed trade under a cost multiple, its fills' charges and rebates apart."""
    rebates = fsum(fill.cost for fill in trade.fills if fill.cost < 0.0)
    if rebates == 0.0:
        return replace(
            trade,
            pnl_net=trade.pnl_net - trade.cost * (multiplier - 1.0),
            cost=trade.cost * multiplier,
        )
    cost = (trade.cost - rebates) * multiplier + rebates / multiplier
    return replace(trade, pnl_net=trade.pnl_net - (cost - trade.cost), cost=cost)


class _CostStress:
    """The edge survives costs worse than the venue's, at two multiples of them."""

    id: ClassVar[str] = "cost_stress"

    def evaluate(self, ctx: GateContext) -> GateResult:
        first, second = number(ctx, "mult_a"), number(ctx, "mult_b")
        if first is None or second is None:
            return skipped(self.id, "no cost multiples were chosen, so nothing was stressed")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        at_first = _metric(
            objective, stressed(ctx.run, first), ctx, ctx.host_run, ctx.benchmark_run
        )
        at_second = _metric(
            objective, stressed(ctx.run, second), ctx, ctx.host_run, ctx.benchmark_run
        )
        return verdict(
            self.id,
            at_first > 0 and at_second >= 0,
            {
                "objective": objective.id,
                "mult_a": first,
                "mult_b": second,
                "metric_a": at_first,
                "metric_b": at_second,
            },
        )


class _Bootstrap:
    """What the trade sequence could have looked like, and how deep it could have drawn.

    A replication resamples the closed trades with replacement, keeping the population and
    losing the order, and rebuilds the equity path from the starting capital. The path
    gives the drawdown distribution, which is what the gate judges; the same draw gives
    the objective's own statistic, which is recorded as evidence and becomes a deployed
    version's expectation. The statistic is the run's objective family measured on the
    resampled trades — a Sharpe of the trade series, or its mean edge per trade — less,
    for an objective measured against a benchmark, what the benchmark scored on the same
    statistic over the same folds: a hold has no trades of its own to resample, and the
    band a paper stage judges a realised difference against has to be one of differences.
    """

    id: ClassVar[str] = "bootstrap"

    def evaluate(self, ctx: GateContext) -> GateResult:
        replications = count(ctx, "n")
        if replications is None:
            return skipped(self.id, "no replication count was chosen, so nothing was resampled")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        if len(ctx.run.trades) < 2:
            return skipped(self.id, "fewer than two closed trades, so there is nothing to resample")
        limit = ctx.hyp.risk_limits.max_drawdown_pct
        low, high, worst = self._resample(ctx, objective, replications)
        return verdict(
            self.id,
            worst <= limit,
            {
                "objective": objective.id,
                "objective_ci90": [low, high],
                "mdd_p95": worst,
                "limit_pct": limit,
                "n": replications,
            },
        )

    @staticmethod
    def _resample(
        ctx: GateContext, objective: Objective, replications: int
    ) -> tuple[float, float, float]:
        trades = ctx.run.trades
        pnl = np.array([t.pnl_net for t in trades], dtype=float)
        edge = np.array(
            [t.pnl_net / t.notional * BPS if t.notional > 0 else 0.0 for t in trades],
            dtype=float,
        )
        capital = ctx.run.capital
        per_year = len(trades) / years(ctx.run.window)
        rng = np.random.default_rng(
            int.from_bytes(sha256(ctx.strategy_sha.encode()).digest()[:8], "big")
        )
        drawdowns: list[np.ndarray] = []
        statistics: list[np.ndarray] = []
        for start in range(0, replications, BLOCK):
            width = min(BLOCK, replications - start)
            draws = rng.integers(0, len(trades), size=(width, len(trades)))
            sample = pnl[draws]
            path = capital + np.cumsum(sample, axis=1)
            peak = np.maximum(np.maximum.accumulate(path, axis=1), capital)
            drawdowns.append(((peak - path) / capital * 100.0).max(axis=1))
            statistics.append(
                _sharpe_of(sample, per_year)
                if objective.id in SHARPE_FAMILY
                else edge[draws].mean(axis=1)
            )
        spread = np.concatenate(statistics) - benchmark_level(
            objective, ctx.research_folds, ctx.benchmark_run
        )
        return (
            float(np.percentile(spread, 5)),
            float(np.percentile(spread, 95)),
            float(np.percentile(np.concatenate(drawdowns), 95)),
        )


def _sharpe_of(sample: np.ndarray, per_year: float) -> np.ndarray:
    """Annualised Sharpe of each resampled row; a row that cannot vary scores zero."""
    dispersion = sample.std(axis=1, ddof=1)
    safe = np.where(dispersion > 0, dispersion, 1.0)
    return np.where(dispersion > 0, sample.mean(axis=1) / safe * sqrt(per_year), 0.0)


class _BookCorrelation:
    """The candidate's returns are not another deployed book's returns again."""

    id: ClassVar[str] = "book_correlation"

    def evaluate(self, ctx: GateContext) -> GateResult:
        ceiling = number(ctx, "max_corr")
        if ceiling is None:
            return skipped(self.id, "no ceiling was chosen, so no correlation was required")
        if not ctx.deployed:
            return skipped(self.id, "nothing is deployed to correlate against")
        own = dict(zip(ctx.run.period_ends_ns, ctx.run.returns, strict=True))
        found: dict[str, float] = {}
        for book in ctx.deployed:
            paired = [
                (own[ts], value)
                for ts, value in zip(book.period_ends_ns, book.returns, strict=True)
                if ts in own
            ]
            value = correlation([a for a, _ in paired], [b for _, b in paired])
            if value is not None:
                found[book.id] = value
        if not found:
            return skipped(self.id, "no deployed book overlaps this window enough to correlate")
        return verdict(
            self.id,
            max(found.values()) <= ceiling,
            {"correlations": found, "max_corr": ceiling},
        )


class _PublicationLag:
    """Every delayed dataset in the pinned snapshot was published late enough to be real."""

    id: ClassVar[str] = "publication_lag"

    def evaluate(self, ctx: GateContext) -> GateResult:
        tolerance = number(ctx, "tolerance_s")
        if tolerance is None:
            return skipped(self.id, "no tolerance was chosen, so no lag was required")
        if not ctx.datasets:
            return skipped(self.id, "the pinned snapshot's datasets were not supplied")
        unknown = [d.dataset_id for d in ctx.datasets if d.publication == "unknown"]
        early = [
            d.dataset_id
            for d in ctx.datasets
            if d.publication == "delayed" and d.min_lag_s + tolerance < d.required_lag_s
        ]
        return verdict(
            self.id,
            not unknown and not early,
            {
                "unknown": unknown,
                "published_too_early": early,
                "tolerance_s": tolerance,
                "n_datasets": len(ctx.datasets),
            },
        )


class _CapacityVsAdv:
    """A day's traded notional stays a small share of what the instrument actually trades."""

    id: ClassVar[str] = "capacity_vs_adv"

    def evaluate(self, ctx: GateContext) -> GateResult:
        participation, window = number(ctx, "participation"), count(ctx, "adv_days")
        if participation is None or window is None:
            return skipped(self.id, "no participation limit was chosen, so nothing was capped")
        if not ctx.daily_volume:
            return skipped(self.id, "no volume data, so there is no capacity to compare against")
        peaks = _peak_daily_notional(ctx)
        judged: dict[str, dict[str, float]] = {}
        for instrument, peak in sorted(peaks.items()):
            series = ctx.daily_volume.get(instrument)
            if not series:
                continue
            adv = mean(list(series)[-window:])
            judged[instrument] = {"peak_notional": peak, "adv": adv, "cap": participation * adv}
        if not judged:
            return skipped(self.id, "nothing traded here has a volume series to compare against")
        return verdict(
            self.id,
            all(v["peak_notional"] <= v["cap"] for v in judged.values()),
            {"instruments": judged, "participation": participation, "adv_days": window},
        )


def _peak_daily_notional(ctx: GateContext) -> dict[str, float]:
    """The busiest single day of traded notional, per instrument."""
    per_day: dict[tuple[str, date], float] = {}
    for fill in ctx.run.fills:
        key = (fill.instrument_id, day_of(fill.ts_ns))
        per_day[key] = per_day.get(key, 0.0) + fill.notional
    peaks: dict[str, float] = {}
    for (instrument, _), value in per_day.items():
        peaks[instrument] = max(peaks.get(instrument, 0.0), value)
    return peaks


strategy_integrity: Final[Gate] = _StrategyIntegrity()
min_trades: Final[Gate] = _MinTrades()
max_hold: Final[Gate] = _MaxHold()
position_size: Final[Gate] = _PositionSize()
leg_edge: Final[Gate] = _LegEdge()
max_drawdown: Final[Gate] = _MaxDrawdown()
maintenance_margin: Final[Gate] = _MaintenanceMargin()
embargoed_window: Final[Gate] = _EmbargoedWindow()
walk_forward_consistency: Final[Gate] = _WalkForwardConsistency()
deflated_sharpe: Final[Gate] = _DeflatedSharpe()
cost_stress: Final[Gate] = _CostStress()
bootstrap: Final[Gate] = _Bootstrap()
book_correlation: Final[Gate] = _BookCorrelation()
publication_lag: Final[Gate] = _PublicationLag()
capacity_vs_adv: Final[Gate] = _CapacityVsAdv()

PENDING: Final[frozenset[str]] = frozenset()
"""Gates the library declares that this version cannot run. Empty, and a release ships so."""
