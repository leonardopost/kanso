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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from math import e, fsum, inf, sqrt
from statistics import NormalDist, median
from typing import ClassVar, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from kanso.criteria.digest import edge_per_trade as edge_per_trade_of
from kanso.criteria.integrity import check as check_integrity
from kanso.criteria.objectives import REGISTRY, SHARPE_FAMILY, Objective, benchmark_level
from kanso.criteria.quantities import (
    correlation,
    drawdown_pct,
    edge_series,
    mean,
    periods_per_year,
    stdev,
    student_t_quantile,
    variance,
    years,
)
from kanso.criteria.run import BPS, NS_PER_SECOND, CardRun, Fill, Held, Trade, day_of
from kanso.errors import PreconditionError, ValidationError
from kanso.schemas import GateResult, parse_duration
from kanso.schemas.screen import hours_minutes

PERCENT: Final = 100.0
"""A share of capital is reported the way a risk limit states one."""

NS_PER_DAY: Final = 86_400 * NS_PER_SECOND

MINUTES_PER_DAY: Final = 1440

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
            ctx.lane_dir,
            ctx.pinned,
            sized=ctx.hyp.sizing is not None,
            construct=ctx.construct,
            depth=ctx.hyp.depth is not None,
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
        gathered[key] = (qty + signed, notional + fill.notional)
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
    """Most research folds positive: an edge that recurs rather than arriving once.

    The gate once also required the certification window's estimate to exceed the worst
    research fold, and that clause is gone because it measured nothing. Four research folds
    and a certification window of a fold's length are five draws of one quantity, so the
    certification one is the lowest of the five a fifth of the time whatever the edge is —
    `1 − 1/(K+1)`, 0.80 at four folds — and the research folds of a selected candidate are
    the ones selection inflated, which takes it to about 0.42 after fifty trials and 0.24
    after five hundred: simulated in October 2026 on the layout of an operator's 153
    certificates, every one of which the planner had given this gate. A clause whose pass
    rate does not move with the quantity it tests has no power against anything; it only
    refuses sound candidates at random, and more certification data made it refuse more
    often, not less. It is not kept as a parameter whose absence means off, because a
    planner offered it could only choose to add those refusals. Whether the certification
    window holds the edge is `embargoed_window`'s question, asked against the research
    estimate rather than its worst fold. What is left reads the research window alone: how
    many of its folds the objective was positive in.
    """

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
        positive = sum(1 for value in folds if value > 0)
        return verdict(
            self.id,
            positive >= required,
            {
                "objective": objective.id,
                "folds": list(folds),
                "positive_folds": positive,
                "min_positive_folds": required,
            },
        )


class _DeflatedSharpe:
    """The research Sharpe, deflated by how many trials went into selecting it.

    The estimate deflated is the one selection acted on — the research-window metric the
    keep rule compared. The expected maximum it is measured against is built from the
    trials' own metrics: their count is how many candidates the maximum was taken over
    and their spread is the distribution it was taken from, so both must describe the
    same set or the bar is measured against a search that never happened. That set is
    `trial_metrics`, which excludes a crash, a card that placed no order and a card scored
    under another objective — the first two are edits that failed rather than candidates the
    selection could have chosen, the third a number in other units — and includes one
    refused as redundant, which is a candidate the selection ranked and declined. The
    estimate's own error is the objective's, the one its card carried: Mertens' variance of a
    Sharpe for skewed and fat-tailed returns, fold by fold, widened for serial correlation,
    and Memmel's paired one for a Sharpe differenced against a host or a hold. The estimate,
    the error and the spread are all taken back out of annualised units first, so the
    deflation is done in the units the return series was actually sampled in.
    """

    id: ClassVar[str] = "deflated_sharpe"

    def evaluate(self, ctx: GateContext) -> GateResult:
        floor = number(ctx, "min_dsr")
        if floor is None:
            return skipped(self.id, "no minimum was chosen, so no deflation was required")
        found = _deflatable(self.id, ctx, SHARPE_FAMILY, "a Sharpe")
        if isinstance(found, GateResult):
            return found
        research, metrics, (estimate, error) = found
        if len(research.returns) < 3:
            return skipped(self.id, "the research return series is too short to give an error")
        scale = sqrt(periods_per_year(research))
        expected = expected_maximum(variance(metrics) / periods_per_year(research), len(metrics))
        if error <= 0:
            return skipped(self.id, "the research return series does not vary")
        dsr = NormalDist().cdf((estimate / scale - expected) / (error / scale))
        return verdict(
            self.id,
            dsr >= floor,
            {
                "dsr": dsr,
                "min_dsr": floor,
                "sharpe": estimate / scale,
                "standard_error": error / scale,
                "expected_maximum": expected,
                "trials": len(metrics),
            },
        )


def _deflatable(
    gate: str, ctx: GateContext, family: frozenset[str], kind: str
) -> tuple[CardRun, list[float], tuple[float, float]] | GateResult:
    """What every deflation gate needs before it can judge — the research run, the trials and
    the research estimate with its error — or the skip that says which was missing.

    The estimate and its error are the objective's own over the research run, the numbers a
    card of these bytes carried, so a deflation never computes an error of its own.
    """
    objective = _objective(ctx)
    if objective is None:
        return skipped(gate, NO_OBJECTIVE)
    if objective.id not in family:
        return skipped(
            gate, f"the objective {objective.id} is not {kind}, so no card computed one to deflate"
        )
    research = ctx.research_run
    if research is None:
        return skipped(gate, NO_RESEARCH_RUN)
    metrics = list(ctx.trial_metrics)
    if len(metrics) < 2:
        return skipped(gate, "fewer than two trials, so the trial spread is unknown")
    try:
        measured = objective.compute(
            research, ctx.research_folds, ctx.host_research_run, ctx.benchmark_research_run
        )
    except PreconditionError as missing:
        return skipped(gate, missing.message[:200])
    return research, metrics, measured


def expected_maximum(trial_variance: float, n_trials: int) -> float:
    """The statistic a search of this width is expected to produce from pure noise: the
    expected maximum of `n_trials` draws from a normal of this variance (Bailey and Lopez de
    Prado's approximation), in the units the variance is in."""
    trials = max(n_trials, 2)
    normal = NormalDist()
    return sqrt(trial_variance) * (
        (1 - EULER_MASCHERONI) * normal.inv_cdf(1 - 1 / trials)
        + EULER_MASCHERONI * normal.inv_cdf(1 - 1 / (trials * e))
    )


MAD_TO_SIGMA: Final = 1.4826
"""The median absolute deviation of a normal sample, scaled to its standard deviation."""


def robust_variance(values: Sequence[float]) -> float:
    """The variance of a set read off its median absolute deviation, scaled to a normal's.

    The spread of a search's candidates is what the expected maximum is built from, and
    a few trials that sit far out — a rule that traded itself to ruin, a seed that fired
    once — hand a plain variance a width no candidate the selection weighed ever had:
    measured on a posting hypothesis, thirty-three trials whose kept candidates lay within
    thirty basis points of each other gave an expected maximum of a hundred and five. The
    median absolute deviation, scaled by 1.4826, is the same number as the standard
    deviation on a normal sample and is moved by no single trial. Falls back to the plain
    variance when the deviation is zero — most trials on one value — so a set that varies
    at all still has a spread.
    """
    centre = median(values)
    deviation = median(abs(value - centre) for value in values)
    if deviation == 0.0:
        return variance(values)
    return (MAD_TO_SIGMA * deviation) ** 2


CONTRIBUTION_FAMILY: Final = frozenset({"wf_contribution_bps", "marginal_wf_contribution_bps"})
"""The objectives that read a mean net return per period in basis points of the capital."""

EDGE_FAMILY: Final = frozenset({"net_edge_bps", "marginal_net_edge_bps"})
"""The objectives that read a mean net profit per closed trade in basis points."""


def _deflated_mean(
    gate: str,
    ctx: GateContext,
    family: frozenset[str],
    kind: str,
    units: tuple[str, str],
) -> GateResult:
    """A mean objective's research estimate, deflated by how many trials went into selecting
    it: the probability it exceeds the expected maximum of the search by its own error.

    `units` names the estimate's evidence key and what its error was read off — periods or
    trades — which is all that differs between a contribution and a per-trade edge.
    """
    floor = number(ctx, "min_probability")
    if floor is None:
        return skipped(gate, "no minimum was chosen, so no deflation was required")
    found = _deflatable(gate, ctx, family, kind)
    if isinstance(found, GateResult):
        return found
    research, metrics, (estimate, error) = found
    name, counted = units
    count_of = len(research.returns) if counted == "periods" else len(edge_series(research))
    if count_of < 3:
        return skipped(gate, f"the research window holds too few {counted} to give an error")
    if error <= 0:
        return skipped(gate, "the research estimate's error is zero, so nothing deflates it")
    spread = robust_variance(metrics)
    expected = expected_maximum(spread, len(metrics))
    probability = NormalDist().cdf((estimate - expected) / error)
    return verdict(
        gate,
        probability >= floor,
        {
            "probability": probability,
            "min_probability": floor,
            name: estimate,
            "standard_error_bps": error,
            "expected_maximum_bps": expected,
            "trial_spread_bps": sqrt(spread),
            "trials": len(metrics),
            counted: count_of,
        },
    )


class _DeflatedContribution:
    """The research contribution, deflated by how many trials went into selecting it.

    The contribution's sampling error is the objective's own — the error of a mean of the
    per-period net returns in basis points of the capital over the research window, of the
    paired per-period differences against the host's for the marginal form, widened for
    serial correlation — and the expected maximum it is measured against is built from the
    trials' own metrics, in the same units: their count is how many candidates the selection
    took the maximum over and their spread is the distribution it took it from
    (`trial_metrics`, the same set `deflated_sharpe` reads), read robustly as 1.4826 times
    their median absolute deviation (`robust_variance`), so a few trials that blew up do not
    widen the search past any candidate the selection weighed. The probability reported is
    that of the estimate exceeding that maximum by its own error, and the gate passes when it
    clears the floor chosen for it. A t-statistic on the contribution deflated for the
    search, in short — the multiple-testing control a per-period objective has where a
    Sharpe has its deflated ratio.
    """

    id: ClassVar[str] = "deflated_contribution"

    def evaluate(self, ctx: GateContext) -> GateResult:
        return _deflated_mean(
            self.id, ctx, CONTRIBUTION_FAMILY, "a contribution", ("contribution_bps", "periods")
        )


class _DeflatedEdge:
    """The research per-trade edge, deflated by how many trials went into selecting it.

    The same test as `deflated_contribution`, on the sub-daily default: the estimate is the
    research edge in basis points per trade, its error the objective's own — the edge
    linearised period by period, so trades of one session that move together count as one
    session's worth, widened for serial correlation across periods, and for the marginal form
    the two runs' linearisations differenced period by period — and the bar the expected
    maximum of the search's trials scored under the same objective, from their count and
    their robust spread. Until it existed a run scored on a per-trade edge
    reached a certificate with nothing pricing the search behind it: both other deflations
    skip it, and the embargo's fraction of a selected maximum is only an implicit, partial
    correction, gone altogether when the research number is at or below zero.
    """

    id: ClassVar[str] = "deflated_edge"

    def evaluate(self, ctx: GateContext) -> GateResult:
        return _deflated_mean(self.id, ctx, EDGE_FAMILY, "a per-trade edge", ("edge_bps", "trades"))


class _MinEventDays:
    """Enough distinct sessions with a fill: a floor on the days an edge was seen on.

    A rule that fires on a regime or an event can put its whole sample into a handful of
    sessions — many fills, few days — and a per-trade count cannot tell. The sessions are the
    days any fill of the run falls on, so a rule active one day a month for two years has
    twenty-four however much it traded on each.
    """

    id: ClassVar[str] = "min_event_days"

    def evaluate(self, ctx: GateContext) -> GateResult:
        minimum = count(ctx, "min")
        if minimum is None:
            return skipped(self.id, "no minimum was chosen, so no count was required")
        days = {day_of(fill.ts_ns) for fill in ctx.run.fills}
        return verdict(self.id, len(days) >= minimum, {"event_days": len(days), "min": minimum})


def search_margin(trials: int) -> float:
    """How many of their own errors the best of `trials` cards whose trades earn nothing is
    expected to clear by chance: the expected maximum of that many standard normals, by the
    approximation the deflation gates use (`expected_maximum` at a spread of one error), and
    zero for one card, which is no search."""
    return expected_maximum(1.0, trials) if trials >= 2 else 0.0


def periods_margin(errors: float, periods: int) -> float:
    """`errors` of a normal error, restated as a multiple of a per-trade error read period by
    period off `periods` periods that closed a trade, at least two: the same chance of being
    cleared by a card whose trades earn nothing.

    Read that way the error is the per-period influences' over the window, which sum to zero
    and are zero outside the `p` periods that traded, so it is `√(N/(N − 1))` of the error of
    the mean of `p` period totals taken as `p` draws with no degree of freedom spent — low by
    `√((p − 1)/p)` against the one that spends it, the window's `N` periods being many more
    than `p` — and an edge over the corrected error is a Student's t of `p − 1` degrees of
    freedom, not a normal, wherever the period totals are normal: a Cauchy at two periods.
    So the margin is that t's quantile at the normal's chance of clearing `errors`, widened
    by `√(p/(p − 1))`: one error is 2.60 of them over two periods, 1.15 over eight and 1.004
    over 252; three are 333 over two, 4.84 over eight and 3.04 over 252. Zero is zero.
    """
    freedom = periods - 1
    return student_t_quantile(NormalDist().cdf(errors), freedom) * sqrt(periods / freedom)


class _EdgePerTrade:
    """The card's trades make money on average, by more than their own error says is noise.

    The estimate is the card's mean net edge per closed trade in basis points of the
    position opened — `pnl_net / notional`, after every fill cost the runner charged, over
    the closed trades with a notional — and its error is the per-trade objective's own: the
    edge linearised period by period, so a session's clips that one regime moves together
    count as one session's worth, widened for serial correlation across periods. Both are
    read over the whole window as one fold (`kanso.criteria.digest.edge_per_trade`), the
    numbers a card's digest records as `net_bps_per_trade` and `net_bps_per_trade_se`, so
    what the proposer and the operator are shown is what was judged. The gate passes when
    `net − margin × se > min_bps`, strictly, over trades that closed in at least two of the
    window's periods, where `margin` is `periods_margin` of the larger of `k_se` and
    `search_margin(trials)` — the evidence's `normal_margin` — over the periods that closed
    a trade: on a hypothesis's first card, at `min_bps` zero and `k_se` zero, a card whose
    trades closed in two periods or more passes exactly when its mean trade made money.

    The margin is a chance, and the periods set its width. An error read period by period
    has the degrees of freedom of the periods that closed a trade, and on few of them it
    reads low and the edge over it has tails a normal does not: measured in October 2026 on
    cards whose trades earn nothing, held to `normal_margin` errors as if normal, those that
    traded on two sessions passed at `k_se` 1 0.31 of the time rather than 0.16, and 0.14
    rather than 0.0011 as the 500th card of a search. Restated on its degrees of freedom,
    `normal_margin` is cleared by such a card with the normal's chance `1 − Φ(normal_margin)`
    wherever each period's trades sum to a normal — measured 0.148 to 0.163 at `k_se` 1 on
    two to thirty sessions against 0.159, and at or below it on heavy-tailed trades four to a
    session — so the rates below hold on two sessions as on two hundred. Where the periods
    hold unequal numbers of heavy-tailed trades it runs a little above: up to 0.18 at `k_se`
    1 and 0.014 as the fiftieth card, on two to ten sessions.

    The margin grows with the search. A lane draws hundreds of cards, and one whose trades
    earn nothing still clears `k_se` errors' chance with probability `1 − Φ(k_se)` — at
    `k_se` 1, one card in six — so a fixed margin lets zero-edge cards through in proportion
    to the search's length. So `normal_margin` is never below the expected maximum of `trials`
    standard normals: 0 errors on one card, 0.52 at 2 trials, 2.28 at 50 and 3.05 at 500.
    `trials` is this card and every card of the hypothesis before it that ran to a result and
    traded, under any objective (`GateContext.trials`). Each card is held to the search as it
    stood when it ran, so the expected count of zero-edge cards that pass grows with the
    logarithm of the search rather than with its length: over 500 cards, about 3.3 at `k_se`
    1 rather than 79, 2.3 at `k_se` 2 rather than 11, and 0.67 at `k_se` 3 either way. That
    count is a sum of each card's chance, so it holds however the cards are correlated;
    correlated cards are fewer independent draws than their number, which makes the margin
    wider than their search, never narrower. Which gates run and their `min_bps` and `k_se`
    stay the agents' choice; the search sets only a floor under `k_se`.

    An error read period by period is zero whenever every trade closed in one period, however
    far apart the trades landed — each period's influence on the mean is zero — and has no
    degree of freedom to restate a margin on, so a card that traded once, or traded one
    session's clips, fails at any `k_se`, its error and margin stated as `None` and its
    periods in `periods_with_trades`. Trading less to pass therefore ends at two periods, where
    the margin is widest.

    It exists because an objective struck on the book rewards trading less once every trade
    loses: a contribution moves towards zero as the losing trades are dropped, and a card
    that halves its clips at a worse loss per clip scores better and is kept. As a card gate
    a failing card is not kept whatever it scored, so with it on a lane only a card whose
    trades pay on average can become the best. A card that closed no trade has no edge — its
    estimate is zero, in no period — and fails at any `min_bps`, which the range holds at
    zero or above: a card that did not trade did not show that a trade pays.
    """

    id: ClassVar[str] = "edge_per_trade"

    def evaluate(self, ctx: GateContext) -> GateResult:
        floor, k_se = number(ctx, "min_bps"), number(ctx, "k_se")
        if floor is None or k_se is None:
            return skipped(self.id, "no min_bps and k_se were chosen, so no edge was required")
        edge, se, periods = edge_per_trade_of(ctx.run)
        searched = search_margin(ctx.trials)
        normal = max(k_se, searched)
        margin = None if se is None else periods_margin(normal, periods)
        lower = None if se is None or margin is None else edge - margin * se
        return verdict(
            self.id,
            lower is not None and lower > floor,
            {
                "net_bps_per_trade": edge,
                "se": se,
                "k_se": k_se,
                "trials": ctx.trials,
                "expected_maximum": searched,
                "normal_margin": normal,
                "margin": margin,
                "lower": lower,
                "min_bps": floor,
                "n_trades": sum(1 for trade in ctx.run.trades if trade.notional > 0),
                "periods_with_trades": periods,
            },
        )


class _TradingHours:
    """Every fill inside a stated daily session, and every position flat by its close.

    A venue that accepts no market order outside regular hours refuses what a backtest
    fills at 16:30 against an extended-hours quote, so a strategy that trades there earns a
    number no account could have earned. "Regular hours only" in a thesis is a sentence a
    model reads, and two constants in `strategy.py` are lines the loop may edit; this is
    the same instruction as a refusal, stated once in `hypothesis.yaml`.

    `session` is clock times on one day and `tz` the zone that clock is kept in, so
    daylight saving moves the session as the market moves it: `09:30-16:00` in
    `America/New_York` is 14:30 to 21:00 UTC in January and 13:30 to 20:00 in July. On
    each local date the session runs from the first instant the clock reads the opening
    time or later to the first it reads the closing time or later, and is half-open: a
    fill at the closing instant is outside it. So a time a clock change repeats is read
    at its first occurrence and one it skips at the change itself (`first_reading`).

    Two things are judged, both on exact instants. A fill is outside when it falls outside
    the session of its own local date. A position is every stretch an instrument's net
    quantity is away from zero, from the fill that opened it to the fill that closed it —
    a reversal in one fill does not close it — and one still open when the window closes is
    held to that close; it is held outside unless it opened and closed inside one session.
    Its fills alone cannot say that: bought at 15:59 and sold at 09:31 the next morning, it
    made two fills inside the session and was held all night.

    The run is judged whole. An attached construct's run is its host and itself trading
    together, and a venue refuses the host's order outside the session as it refuses the
    candidate's.

    The session is the same on every date: no calendar of early closes is on file, so a
    fill at 14:00 on a day the market closed at 13:00 is judged against the stated close.
    """

    id: ClassVar[str] = "trading_hours"

    def evaluate(self, ctx: GateContext) -> GateResult:
        span, zone = text(ctx, "session"), text(ctx, "tz")
        if span is None and zone is None:
            return skipped(self.id, "no session was chosen, so no fill was timed")
        session = _trading_session(span, zone)
        if isinstance(session, str):
            return verdict(self.id, False, {"refused": session, "session": span, "tz": zone})
        fills = sorted(ctx.run.fills, key=lambda item: item.ts_ns)
        if not fills:
            return skipped(self.id, "no fill was made, so nothing was timed")
        outside = [item for item in fills if not session.holds(item.ts_ns)]
        spells = _spells_held(fills, ctx.run.bounds[1])
        over = [spell for spell in spells if not session.holds_throughout(*spell[1:])]
        return verdict(
            self.id,
            not outside and not over,
            {
                "session": span,
                "tz": zone,
                "n_fills": len(fills),
                "n_fills_outside": len(outside),
                "earliest_fill_outside": session.fill(outside[0]) if outside else None,
                "latest_fill_outside": session.fill(outside[-1]) if outside else None,
                "n_positions": len(spells),
                "n_held_outside": len(over),
                "earliest_held_outside": session.spell(*over[0]) if over else None,
                "latest_held_outside": session.spell(*over[-1]) if over else None,
            },
        )


def _trading_session(span: str | None, zone: str | None) -> TradingSession | str:
    """The session these two parameters state, or why they state none."""
    if span is None or zone is None:
        return "a session is clock times in a zone, and only one of session and tz was chosen"
    try:
        opens, closes = hours_minutes(span)
    except ValidationError:
        return f"session: {span!r} is not HH:MM-HH:MM on one day"
    try:
        return TradingSession(opens, closes, ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return f"tz: {zone!r} is not a time zone this host knows"


@dataclass(frozen=True)
class TradingSession:
    """A daily span of clock times in a zone, read date by date as instants.

    `opens` and `closes` are minutes after local midnight, `closes` at most a whole day.
    Each date's instants are worked out once and kept, since a window's fills fall on a
    few hundred dates however many there are.
    """

    opens: int
    closes: int
    zone: ZoneInfo
    _dates: dict[date, tuple[int, int]] = field(default_factory=dict, compare=False, repr=False)

    def local(self, ts_ns: int) -> datetime:
        """The instant on this session's clock, to the microsecond."""
        seconds, rest = divmod(ts_ns, NS_PER_SECOND)
        return datetime.fromtimestamp(seconds, self.zone).replace(microsecond=rest // 1000)

    def bounds(self, day: date) -> tuple[int, int]:
        """A local date's session as the half-open span of instants `[opens, closes)`."""
        found = self._dates.get(day)
        if found is None:
            found = (
                first_reading(day, self.opens, self.zone),
                first_reading(day, self.closes, self.zone),
            )
            self._dates[day] = found
        return found

    def holds(self, ts_ns: int) -> bool:
        """Whether an instant falls inside the session of its own local date."""
        opens, closes = self.bounds(self.local(ts_ns).date())
        return opens <= ts_ns < closes

    def holds_throughout(self, opened: int, closed: int) -> bool:
        """Whether a position held over `[opened, closed)` stayed inside one session."""
        opens, closes = self.bounds(self.local(opened).date())
        return opens <= opened and closed <= closes

    def fill(self, item: Fill) -> dict[str, object]:
        """A fill as the evidence names it: what, which way, and when on both clocks."""
        return {
            "instrument": item.instrument_id,
            "side": item.side,
            "ts_ns": item.ts_ns,
            "local": self.local(item.ts_ns).isoformat(),
        }

    def spell(self, instrument: str, opened: int, closed: int) -> dict[str, object]:
        """A position held outside, with the close of the session it opened in."""
        _, closes = self.bounds(self.local(opened).date())
        return {
            "instrument": instrument,
            "opened": self.local(opened).isoformat(),
            "closed": self.local(closed).isoformat(),
            "session_closed": self.local(closes).isoformat(),
        }


def first_reading(day: date, minute: int, zone: ZoneInfo) -> int:
    """The first instant on `day` that `zone`'s clock reads `minute` past midnight or later.

    In nanoseconds since the epoch; a minute of a whole day is the next day's first instant.
    A reading a clock change repeats is the earlier of its two instants, which is what fold
    zero resolves it to. A reading a change skips has no instant at all, and the first
    instant past it is the change: fold one resolves it on the offset after the change, to
    an instant before it, and fold zero on the offset before, to one after, so the change
    lies between the two and is found by bisection on whole seconds, which is the grain
    every transition in the zone database is stamped at.
    """
    day += timedelta(days=minute // MINUTES_PER_DAY)
    hour, rest = divmod(minute % MINUTES_PER_DAY, 60)
    wall = datetime.combine(day, time(hour, rest), tzinfo=zone)
    after, before = int(wall.timestamp()), int(wall.replace(fold=1).timestamp())
    if before >= after:
        return after * NS_PER_SECOND
    offset = _offset(before, zone)
    while after - before > 1:
        middle = (before + after) // 2
        if _offset(middle, zone) == offset:
            before = middle
        else:
            after = middle
    return after * NS_PER_SECOND


def _offset(seconds: int, zone: ZoneInfo) -> timedelta | None:
    """The zone's offset from UTC at a whole second since the epoch."""
    return datetime.fromtimestamp(seconds, zone).utcoffset()


def _spells_held(fills: Sequence[Fill], closes: int) -> list[tuple[str, int, int]]:
    """Every stretch an instrument's net quantity was away from zero, in opening order.

    `(instrument, opened, closed)` from the fill that took the quantity off zero to the
    one that brought it back, or to `closes` when none did. Quantities are summed rounded,
    as `_orders` gathers them, so a fractional lot sold in pieces comes back to zero.
    """
    position: dict[str, float] = {}
    opened: dict[str, int] = {}
    spells: list[tuple[str, int, int]] = []
    for item in fills:
        before = position.get(item.instrument_id, 0.0)
        signed = -item.qty if item.side == "SELL" else item.qty
        after = round(before + signed, 9)
        position[item.instrument_id] = after
        if not before and after:
            opened[item.instrument_id] = item.ts_ns
        elif before and not after:
            spells.append((item.instrument_id, opened.pop(item.instrument_id), item.ts_ns))
    spells.extend((instrument, ts_ns, closes) for instrument, ts_ns in opened.items())
    return sorted(spells, key=lambda spell: spell[1])


SCENARIO_KEYS: Final = (
    "commission_bps",
    "commission_per_share",
    "slippage_bps",
    "slippage_ticks",
    "fixed_bps",
    "maker_bps",
    "maker_per_share",
    "sell_fee_bps",
    "sell_fee_per_share",
)
"""The cost model a scenario states, key for key with the charges of `costs:` in
`hypothesis.yaml`."""

TAKER_KEYS: Final = (
    "commission_bps",
    "commission_per_share",
    "slippage_bps",
    "slippage_ticks",
    "fixed_bps",
)
"""The scenario keys that say what a fill that took liquidity pays. A scenario is a cost model
only when it states one of them: the maker's keys charge a resting fill alone and the sell-side
fees a sale alone, and a scenario of those only would charge every other fill nothing."""


def repriced(run: CardRun, scenario: Mapping[str, float | None]) -> CardRun:
    """The same run with every recorded fill charged under another cost model.

    Costs are applied once, by the runner, in the extraction that produced this run, so
    re-pricing them is arithmetic on the recorded fills rather than another backtest: each
    fill's notional — quantity, price and contract multiplier — its quantity and its
    liquidity side are on record, and the scenario's rates are put through the same per-fill
    arithmetic the runner used, so the runner's own scenario reproduces the runner's own
    cost on any instrument. The difference between what a
    fill now costs and what it cost is charged to the return period it falls in, exactly as
    a cost multiple is; the carry, the transfers and the cushion stand as recorded, for the
    reasons `stressed` gives. A key the scenario leaves out is zero, and a scenario that
    states neither `maker_bps` nor `maker_per_share` charges a maker's fill what any fill pays.
    `slippage_ticks` is charged on each fill's recorded increment and capped by its recorded
    limit, as the runner charged it; a fill recorded before either was kept pays none of it.
    """
    from kanso.nautilus.costs import fill_cost, tick_slip

    half_spread = (scenario.get("fixed_bps") or 0.0) / 2.0 / BPS  # a rate, as the runner hands it
    maker_bps = scenario.get("maker_bps")
    maker_per_share = scenario.get("maker_per_share")
    ticks = scenario.get("slippage_ticks") or 0.0

    def recost(fill: Fill) -> float:
        return fill_cost(
            fill.notional,
            fill.qty,
            scenario.get("commission_bps") or 0.0,
            scenario.get("slippage_bps") or 0.0,
            0.0 if fill.touched else half_spread,
            maker_bps,
            scenario.get("commission_per_share") or 0.0,
            maker=fill.maker,
            sell=fill.side == "SELL",
            sell_fee_bps=scenario.get("sell_fee_bps") or 0.0,
            sell_fee_per_share=scenario.get("sell_fee_per_share") or 0.0,
            maker_per_share=maker_per_share,
            slip=tick_slip(ticks, fill.tick, fill.px, fill.limit, sell=fill.side == "SELL"),
            multiplier=fill.multiplier,
        )

    ends = run.period_ends_ns
    added = [0.0] * len(ends)
    for fill in run.fills:
        index = bisect_left(ends, fill.ts_ns)
        if index < len(ends):
            added[index] += recost(fill) - fill.cost
    running = 0.0
    equity: list[float] = []
    for value, charge in zip(run.equity, added, strict=True):
        running += charge
    `fixed_bps` is charged where the runner charges a spread — on a fill not struck at the
    touch — and a fill the venue struck against a quote or a book, whose price carries the
    spread, pays none of it under any scenario; a wider market on such a name is stated as
    `slippage_bps`, which every taker's fill pays on its notional.
        equity.append(value - running)
    trades = []
    for trade in run.trades:
        fills = tuple(replace(f, cost=recost(f)) for f in trade.fills)
        cost = fsum(f.cost for f in fills)
        trades.append(
            replace(trade, pnl_net=trade.pnl_net - (cost - trade.cost), cost=cost, fills=fills)
        )
    return replace(
        run,
        returns=tuple(r - charge for r, charge in zip(run.returns, added, strict=True)),
        equity=tuple(equity),
        trades=tuple(trades),
        fills=tuple(replace(f, cost=recost(f)) for f in run.fills),
    )


class _CostScenario:
    """The edge under another cost model, re-applied to the recorded fills.

    A hypothesis is priced under one account's schedule; the same fills under another's —
    a per-share commission against a flat rate, a maker rebate against none, a wider fixed
    width — say whether the edge belongs to the idea or to the schedule it was measured on.
    The scenario is stated key for key as `costs:` is, and the objective is recomputed on
    the re-priced run; it passes when that clears `min_metric`, zero unless chosen. The host
    and benchmark runs the objective may difference against are not re-priced.
    """

    id: ClassVar[str] = "cost_scenario"

    def evaluate(self, ctx: GateContext) -> GateResult:
        scenario = {key: number(ctx, key) for key in SCENARIO_KEYS}
        if all(scenario[key] is None for key in TAKER_KEYS):
            return skipped(self.id, "no cost model was chosen, so nothing was re-priced")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        floor = number(ctx, "min_metric") or 0.0
        under = repriced(ctx.run, scenario)
        recorded = _metric(objective, ctx.run, ctx, ctx.host_run, ctx.benchmark_run)
        metric = _metric(objective, under, ctx, ctx.host_run, ctx.benchmark_run)
        return verdict(
            self.id,
            metric >= floor,
            {
                "objective": objective.id,
                "metric_recorded": recorded,
                "metric_scenario": metric,
                "min_metric": floor,
                "cost_recorded": fsum(f.cost for f in ctx.run.fills),
                "cost_scenario": fsum(f.cost for f in under.fills),
                "scenario": {key: value for key, value in scenario.items() if value is not None},
            },
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


CONTRIBUTION_BAND: Final = "contribution_bps"
"""The contribution family's band: the mean of resampled return periods, in bp of capital."""

EDGE_BAND: Final = "edge_bps"
"""A per-trade edge's band: the mean of resampled closed trades' edges, in bp of notional."""

SHARPE_BAND: Final = "trade_sharpe"
"""The Sharpe family's band: a Sharpe of resampled closed trades' P&L, annualised by the
trades the window held per year."""


def band_statistic(objective: Objective) -> str:
    """What `bootstrap` resamples for this objective, by the name its evidence records it
    under — which is what a band is reused by, so a band of one statistic is never read
    as another's."""
    if objective.statistic == CONTRIBUTION_BAND:
        return CONTRIBUTION_BAND
    return SHARPE_BAND if objective.id in SHARPE_FAMILY else EDGE_BAND


class _Bootstrap:
    """What the run could have looked like, and how deep it could have drawn.

    A replication resamples the run with replacement, keeping the population and losing the
    order, and rebuilds the equity path from the starting capital. What is resampled is
    what the objective is a mean of. A contribution — the mean return per period in basis
    points of the capital — resamples the window's **return periods**, so its band is in
    basis points of the capital per period, the unit its value and a paper stage's
    realised contribution are in; every other objective resamples the **closed trades**,
    and its statistic is a Sharpe of their P&L or their mean edge per trade. The evidence
    names which (`statistic`). The gate judges both of the numbers it records. The path
    gives the drawdown distribution, whose ninety-fifth percentile is held to the
    hypothesis's limit; the same draw gives the statistic, whose ninety-percent band must
    reach above zero — a band that lies at or below zero says the population carries no
    edge in whatever order it arrives, and a drawdown inside the limit does not make that a
    pass. For an objective measured against a benchmark the statistic is less what the
    benchmark scored on the same statistic over the same folds: a hold has no trades of its
    own to resample, and the band has to be one of differences.

    The band is composition's `ci90`. It is a confidence band of the certification
    window's statistic, not the band a paper window of another length is judged against:
    that one is struck from the value's error and the window's periods
    (`kanso.criteria.band`).
    """

    id: ClassVar[str] = "bootstrap"

    def evaluate(self, ctx: GateContext) -> GateResult:
        replications = count(ctx, "n")
        if replications is None:
            return skipped(self.id, "no replication count was chosen, so nothing was resampled")
        objective = _objective(ctx)
        if objective is None:
            return skipped(self.id, NO_OBJECTIVE)
        statistic = band_statistic(objective)
        if statistic == CONTRIBUTION_BAND:
            if len(ctx.run.returns) < 2:
                return skipped(
                    self.id, "fewer than two return periods, so there is nothing to resample"
                )
        elif len(ctx.run.trades) < 2:
            return skipped(self.id, "fewer than two closed trades, so there is nothing to resample")
        limit = ctx.hyp.risk_limits.max_drawdown_pct
        low, high, worst = self._resample(ctx, objective, statistic, replications)
        return verdict(
            self.id,
            worst <= limit and high > 0,
            {
                "objective": objective.id,
                "statistic": statistic,
                "objective_ci90": [low, high],
                "mdd_p95": worst,
                "limit_pct": limit,
                "n": replications,
            },
        )

    @staticmethod
    def _resample(
        ctx: GateContext, objective: Objective, statistic: str, replications: int
    ) -> tuple[float, float, float]:
        trades = ctx.run.trades
        capital = ctx.run.capital
        moves = (
            np.array(ctx.run.returns, dtype=float)
            if statistic == CONTRIBUTION_BAND
            else np.array([t.pnl_net for t in trades], dtype=float)
        )
        edge = np.array(
            [t.pnl_net / t.notional * BPS if t.notional > 0 else 0.0 for t in trades],
            dtype=float,
        )
        per_year = len(trades) / years(ctx.run.window)
        rng = np.random.default_rng(
            int.from_bytes(sha256(ctx.strategy_sha.encode()).digest()[:8], "big")
        )
        drawdowns: list[np.ndarray] = []
        statistics: list[np.ndarray] = []
        for start in range(0, replications, BLOCK):
            width = min(BLOCK, replications - start)
            draws = rng.integers(0, len(moves), size=(width, len(moves)))
            sample = moves[draws]
            path = capital + np.cumsum(sample, axis=1)
            peak = np.maximum(np.maximum.accumulate(path, axis=1), capital)
            drawdowns.append(((peak - path) / capital * 100.0).max(axis=1))
            if statistic == CONTRIBUTION_BAND:
                statistics.append(sample.mean(axis=1) / capital * BPS)
            elif statistic == SHARPE_BAND:
                statistics.append(_sharpe_of(sample, per_year))
            else:
                statistics.append(edge[draws].mean(axis=1))
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
deflated_contribution: Final[Gate] = _DeflatedContribution()
deflated_edge: Final[Gate] = _DeflatedEdge()
min_event_days: Final[Gate] = _MinEventDays()
edge_per_trade: Final[Gate] = _EdgePerTrade()
trading_hours: Final[Gate] = _TradingHours()
cost_stress: Final[Gate] = _CostStress()
cost_scenario: Final[Gate] = _CostScenario()
bootstrap: Final[Gate] = _Bootstrap()
book_correlation: Final[Gate] = _BookCorrelation()
publication_lag: Final[Gate] = _PublicationLag()
capacity_vs_adv: Final[Gate] = _CapacityVsAdv()

PENDING: Final[frozenset[str]] = frozenset()
"""Gates the library declares that this version cannot run. Empty, and a release ships so."""
