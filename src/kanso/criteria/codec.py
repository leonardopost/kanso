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
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from kanso.criteria.run import CardRun, Fill, FundingPayment, Held, Trade

__all__ = ["decode_run", "encode_run"]


def encode_run(run: CardRun) -> dict[str, Any]:
    """One measured window as JSON-shaped data, losing nothing a gate reads."""
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
    }


def decode_run(payload: Mapping[str, Any]) -> CardRun:
    """One measured window back from its stored form; one stored before funding was booked
    reads as a run that paid none, which is what it measured."""
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
    )


def _encode_held(item: Held) -> dict[str, Any]:
    return {
        "ts_ns": item.ts_ns,
        "instrument_id": item.instrument_id,
        "qty": item.qty,
        "notional": item.notional,
    }


def _decode_held(payload: Mapping[str, Any]) -> Held:
    return Held(
        ts_ns=int(payload["ts_ns"]),
        instrument_id=str(payload["instrument_id"]),
        qty=float(payload["qty"]),
        notional=float(payload["notional"]),
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
    return {
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


def _decode_fill(payload: Mapping[str, Any]) -> Fill:
    """A recorded fill; one recorded before fills said whether they rested reads as a
    taker's, which is what it was charged as, and one recorded before the multiplier was
    kept reads as a multiplier of one, a share's; a run struck on a multiplied instrument
    before the multiplier was kept is re-run before a cost model is re-applied to it. One
    recorded before the increment and the limit were kept reads as an increment of zero and
    no limit, which no `slippage_ticks` charges. One recorded before fills said whether the
    venue struck them at the touch reads as not touched, which is how it was charged: a
    model re-applied to it charges the half-spread it paid."""
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
