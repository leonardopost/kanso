"""A measured run as plain data: the one codec every stored `CardRun` goes through.

`encode_run` turns a run into JSON-shaped data and `decode_run` turns that back into the
same run, losing nothing a gate or an objective reads: every series, every trade with the
fills that made it, every fill with its cost, maker flag, multiplier, increment, limit and
whether the venue struck it at the touch, the holdings, the book series and the funding.
Two records keep runs this way — the window a stage realised (`kanso.portfolio.records`)
and what a research card was measured from (`kanso.research.records`) — and they keep them
through this one pair, so a field added to `CardRun` is added once and read back the same
by both.

A key added to the encoding is read with a default stating what a record written before it
measured, so every record already in a store still decodes: a fill recorded before fills
said whether they rested reads as a taker's, a run recorded before funding was booked as one
that paid none.

What a `period_target` did over the run (`period_stops`) is written only where the run has
it, so every other run is stored as it was before runs kept it, and a stored measurement of
the same run under the same pins is the same blob; a run stored without it decodes with none.
The splits the venue applied to what the run held (`adjustments`) are written the same way,
only where there are some: a run that held nothing through a split is stored as it was, and
a run stored before runs kept them decodes with none, even one that held a position through
a split, whose fill path cannot replay what was never kept
(`kanso.criteria.quantities.fill_path_drawdown_pct` says what it reads instead); an
adjustment writes where it fell among the fills of its instant (`fills_before`) only where
some came before it, and one written without it reads as before them all. A holding
says its mark was under zero (`Held.below_zero`) only where it was, so every other holding is
stored as it was, and one stored before holdings said so reads as at or above zero, which
the same fill path reads it as. The bases a `cap: room` rule sized each entry to (`sized`) are
written only where there are some, so a run sized without the cap, or unsized, is stored as it
was, and a run stored before runs kept them decodes with none.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from kanso.criteria.run import Adjustment, CardRun, Fill, FundingPayment, Held, Sized, Trade
from kanso.schemas import PeriodStops

__all__ = ["decode_run", "encode_run"]


def encode_run(run: CardRun) -> dict[str, Any]:
    """One measured window as JSON-shaped data, losing nothing a gate reads; what a period
    target did over it, and the splits applied to what it held, only where it has those."""
    stopped = (
        {}
        if run.period_stops is None
        else {"period_stops": run.period_stops.model_dump(mode="json")}
    )
    adjusted = (
        {"adjustments": [_encode_adjustment(item) for item in run.adjustments]}
        if run.adjustments
        else {}
    )
    based = {"sized": [_encode_sized(item) for item in run.sized]} if run.sized else {}
    return {
        "window": [run.window[0].isoformat(), run.window[1].isoformat()],
        "period": run.period,
        "period_ends_ns": list(run.period_ends_ns),
        "returns": list(run.returns),
        "equity": list(run.equity),
        "trades": [_encode_trade(trade) for trade in run.trades],
        "fills": [_encode_fill(fill) for fill in run.fills],
        "held": [_encode_held(item) for item in run.held],
        "cushion": list(run.cushion),
        "carry": list(run.carry),
        "worst_ratio": list(run.worst_ratio),
        "funding": [_encode_funding(payment) for payment in run.funding],
        "capital": run.capital,
        "currency": run.currency,
        "venue_model": dict(run.venue_model),
        **stopped,
        **adjusted,
        **based,
    }


def decode_run(payload: Mapping[str, Any]) -> CardRun:
    """One measured window back from its stored form; one stored before funding was booked
    reads as a run that paid none, which is what it measured, one stored without what a
    period target did as a run that had none, and one stored without the splits applied to
    what it held as a run that carries none."""
    stopped = payload.get("period_stops")
    opens, closes = payload["window"]
    return CardRun(
        window=(date.fromisoformat(opens), date.fromisoformat(closes)),
        period=str(payload["period"]),
        period_ends_ns=tuple(int(ts) for ts in payload["period_ends_ns"]),
        returns=tuple(float(value) for value in payload["returns"]),
        equity=tuple(float(value) for value in payload["equity"]),
        trades=tuple(_decode_trade(trade) for trade in payload["trades"]),
        fills=tuple(_decode_fill(fill) for fill in payload["fills"]),
        held=tuple(_decode_held(item) for item in payload.get("held", ())),
        cushion=tuple(float(value) for value in payload.get("cushion", ())),
        carry=tuple(float(value) for value in payload.get("carry", ())),
        worst_ratio=tuple(
            None if value is None else float(value) for value in payload.get("worst_ratio", ())
        ),
        funding=tuple(_decode_funding(item) for item in payload.get("funding", ())),
        capital=float(payload["capital"]),
        currency=str(payload["currency"]),
        venue_model=dict(payload["venue_model"]),
        period_stops=None if stopped is None else PeriodStops.model_validate(stopped),
        adjustments=tuple(_decode_adjustment(item) for item in payload.get("adjustments", ())),
        sized=tuple(_decode_sized(item) for item in payload.get("sized", ())),
    )


def _encode_sized(item: Sized) -> dict[str, Any]:
    clip = {"clip": True} if item.clip else {}
    return {
        "ts_ns": item.ts_ns,
        "instrument_id": item.instrument_id,
        "side": item.side,
        "base": item.base,
        **clip,
    }


def _decode_sized(payload: Mapping[str, Any]) -> Sized:
    return Sized(
        ts_ns=int(payload["ts_ns"]),
        instrument_id=str(payload["instrument_id"]),
        side=str(payload["side"]),
        base=float(payload["base"]),
        clip=bool(payload.get("clip", False)),
    )


def _encode_adjustment(item: Adjustment) -> dict[str, Any]:
    ordered = {"fills_before": item.fills_before} if item.fills_before else {}
    return {
        "ts_ns": item.ts_ns,
        "instrument_id": item.instrument_id,
        "qty": item.qty,
        "paid": item.paid,
        "ratio": item.ratio,
        **ordered,
    }


def _decode_adjustment(payload: Mapping[str, Any]) -> Adjustment:
    return Adjustment(
        ts_ns=int(payload["ts_ns"]),
        instrument_id=str(payload["instrument_id"]),
        qty=float(payload["qty"]),
        paid=float(payload["paid"]),
        ratio=None if payload.get("ratio") is None else float(payload["ratio"]),
        fills_before=int(payload.get("fills_before", 0)),
    )


def _encode_held(item: Held) -> dict[str, Any]:
    """A holding as stored; `below_zero` only where it is true, so every holding marked at or
    above zero is stored as it was before the field was kept."""
    stored: dict[str, Any] = {
        "ts_ns": item.ts_ns,
        "instrument_id": item.instrument_id,
        "qty": item.qty,
        "notional": item.notional,
    }
    if item.below_zero:
        stored["below_zero"] = True
    return stored


def _decode_held(payload: Mapping[str, Any]) -> Held:
    """A recorded holding; one recorded before holdings said whether their mark was under
    zero reads as at or above it."""
    return Held(
        ts_ns=int(payload["ts_ns"]),
        instrument_id=str(payload["instrument_id"]),
        qty=float(payload["qty"]),
        notional=float(payload["notional"]),
        below_zero=bool(payload.get("below_zero", False)),
    )


def _encode_funding(payment: FundingPayment) -> dict[str, Any]:
    return {
        "ts_ns": payment.ts_ns,
        "instrument_id": payment.instrument_id,
        "qty": payment.qty,
        "rate": payment.rate,
        "paid": payment.paid,
    }


def _decode_funding(payload: Mapping[str, Any]) -> FundingPayment:
    return FundingPayment(
        ts_ns=int(payload["ts_ns"]),
        instrument_id=str(payload["instrument_id"]),
        qty=float(payload["qty"]),
        rate=float(payload["rate"]),
        paid=float(payload["paid"]),
    )


def _encode_fill(fill: Fill) -> dict[str, Any]:
    """A fill as stored; `price_ts_ns` only where a dated spread set it, so a fill no dated
    spread charged is stored as it was before the field was kept."""
    stored: dict[str, Any] = {
        "ts_ns": fill.ts_ns,
        "instrument_id": fill.instrument_id,
        "side": fill.side,
        "qty": fill.qty,
        "px": fill.px,
        "cost": fill.cost,
        "maker": fill.maker,
        "multiplier": fill.multiplier,
        "tick": fill.tick,
        "limit": fill.limit,
        "touched": fill.touched,
    }
    if fill.price_ts_ns is not None:
        stored["price_ts_ns"] = fill.price_ts_ns
    return stored


def _decode_fill(payload: Mapping[str, Any]) -> Fill:
    """A recorded fill; one recorded before fills said whether they rested reads as a
    taker's, which is what it was charged as, and one recorded before the multiplier was
    kept reads as a multiplier of one, a share's; a run struck on a multiplied instrument
    before the multiplier was kept is re-run before a cost model is re-applied to it. One
    recorded before the increment and the limit were kept reads as an increment of zero and
    no limit, which no `slippage_ticks` charges. One recorded before fills said whether the
    venue struck them at the touch reads as not touched, which is how it was charged: a
    model re-applied to it charges the half-spread it paid. One recorded without the instant
    its price was struck at — every fill no dated spread charged — decodes with none, `None`,
    never its own `ts_ns`, and is encoded again as it was stored."""
    return Fill(
        ts_ns=int(payload["ts_ns"]),
        instrument_id=str(payload["instrument_id"]),
        side=str(payload["side"]),
        qty=float(payload["qty"]),
        px=float(payload["px"]),
        cost=float(payload["cost"]),
        maker=bool(payload.get("maker", False)),
        multiplier=float(payload.get("multiplier", 1.0)),
        tick=float(payload.get("tick", 0.0)),
        limit=None if payload.get("limit") is None else float(payload["limit"]),
        touched=bool(payload.get("touched", False)),
        price_ts_ns=None if payload.get("price_ts_ns") is None else int(payload["price_ts_ns"]),
    )


def _encode_trade(trade: Trade) -> dict[str, Any]:
    return {
        "opened_ns": trade.opened_ns,
        "closed_ns": trade.closed_ns,
        "instrument_id": trade.instrument_id,
        "qty": trade.qty,
        "avg_open": trade.avg_open,
        "avg_close": trade.avg_close,
        "pnl_net": trade.pnl_net,
        "cost": trade.cost,
        "fills": [_encode_fill(fill) for fill in trade.fills],
        "multiplier": trade.multiplier,
        "funding": trade.funding,
    }


def _decode_trade(payload: Mapping[str, Any]) -> Trade:
    """A recorded trade; one recorded before the multiplier was kept reads as one, and one
    recorded before funding was booked as a trade that paid none."""
    return Trade(
        opened_ns=int(payload["opened_ns"]),
        closed_ns=int(payload["closed_ns"]),
        instrument_id=str(payload["instrument_id"]),
        qty=float(payload["qty"]),
        avg_open=float(payload["avg_open"]),
        avg_close=float(payload["avg_close"]),
        pnl_net=float(payload["pnl_net"]),
        cost=float(payload["cost"]),
        fills=tuple(_decode_fill(fill) for fill in payload["fills"]),
        multiplier=float(payload.get("multiplier", 1.0)),
        funding=float(payload.get("funding", 0.0)),
    )
