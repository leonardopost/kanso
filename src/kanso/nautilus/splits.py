"""Splits: the schedule an instrument carries, and the arithmetic one changes.

A split is a bookkeeping change. A thousand shares at four dollars become a hundred at
forty; nothing was bought, nothing was sold and nothing was earned. A framework that does
not know this reads the price series as a ten-fold gain, and on a leveraged ETF — SOXS has
reverse-split ten times since 2010 — it teaches a research loop to buy an inverse fund the
week before one. So kanso applies the action instead of trading through it, and this module
holds the two halves of that: **when** a split happens, and **what** it changes. *Where* it
is applied is `kanso.nautilus.actions`: inside the simulated venue, one call before the
ex-date's first point is matched against anything.

**The schedule lives on the instrument definition, not in the data.** That is not the
design anyone would pick first — a split beside the bars, as an ordinary
`corporate_action` point, is the obvious shape and the one `data/types/corporate_action.py`
describes. It is closed, and closed for one reason: a dividend carries the day it was
announced, and a split carries only the day it takes effect. kanso cannot say when a split
became knowable, so a corporate-actions dataset that includes splits has no publication
rule, `snapshot._covers` refuses any snapshot relying on it, and it is right to — it cannot
rule out hindsight. A schedule on the definition has no such problem: a definition is a
dated fact the operator asserts, the store keeps one per date, and a restated ratio is a
later definition rather than an overwrite. It is also content-addressed for free, because
`engine_fields` is `to_dict` and `info` is one of its keys, so a schedule reaches
`definition_checksum`, `instruments_checksum` and the `snapshot_id` with nothing added.

**The schedule carries no cash.** `ratio` and an ex-date, and nothing else. A cash
dividend moves money, and money in kanso moves in exactly one place — the runner's
extraction, once per fill — so a definition that paid cash would be a second place costs
and credits are applied. A dividend also has an announcement date, which is what makes it
loadable as an ordinary `corporate_action` point with an honest `ts_init`. A `cash` key in
a schedule entry is therefore refused by name rather than ignored.

**What the engine permits, measured against nautilus_trader 1.231.0.**

* `Position.apply_adjustment(PositionAdjusted(...))` is the mechanism: a public `cpdef`
  that adds `quantity_change` to `signed_qty`, recomputes `quantity` and the side, and
  appends the event to the position's own adjustment ledger. It places no order, charges
  no commission and moves no account balance, and it **leaves `position.events` untouched**
  — which is the whole reason it is usable here, because the runner reads its fills off
  `position.events` and a split must not become a chargeable one.
* `PositionAdjustmentType` has two members, `COMMISSION` and `FUNDING`, and the method's
  own docstring is about crypto commissions and perpetual funding. There is no split
  member; the engine has no corporate-action concept at all, and `PositionAdjusted` is
  produced in one place, consumed nowhere, and never published to the bus. Using it for a
  split is **off-label**. It works, and this note is the record that it is off-label rather
  than blessed: an engine upgrade that gives `PositionAdjusted` a meaning of its own is a
  reason to re-measure this module.
* `Position.quantity`, `signed_qty`, `peak_qty`, `avg_px_open` and `realized_return` are
  `cdef readonly`, and `apply_adjustment` rescales none of `avg_px_open`, `peak_qty` or
  `realized_return`. Measured after an adjustment and a close: `peak=1005 avg_open=10.0
  ret=9.0`. So the engine's own cost basis stays in pre-split units for the life of the
  position, its `realized_pnl` is wrong by `(true_basis - avg_px_open) x closed_qty`, and
  every account balance derived from it is wrong by the same amount from the closing fill
  onwards. Measured under kanso's own venue and instruments: a position that lost 50
  reported a profit of 9,000 and a 100,000 account read 109,000, from the closing fill and
  not before it — nothing moves the balance *at* the ex-date, because the resync recomputes
  maintenance margin alone and kanso's resolved instruments carry a zero margin rate. None
  of it can be repaired from anywhere: the attribute is read-only. It is why `ledger` below
  exists: kanso reads neither `realized_pnl` nor `avg_px_open` nor `peak_qty`, and
  `criteria.integrity` denies a researched strategy the account and the cache, so no card
  can be sized off a number the engine cannot keep honest.
* The synthetic-fill route — a SELL and a BUY that rebase the basis — is forbidden and not
  merely discouraged: it drives `signed_qty` through zero, which trips the reopen branch of
  `Position.apply` and **clears `_events`, `_trade_ids` and `_adjustments`**, deleting the
  real entry fill from the record and fabricating two chargeable ones.
* A position snapshot is `pickle.loads(pickle.dumps(position))`, so it carries the
  adjustments it had when it was taken; the reopen branch then clears the live position's,
  so an adjustment belongs to exactly one of the two and is never counted twice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Final

from kanso.criteria.run import day_of, midnight_ns
from kanso.errors import PreconditionError, ValidationError

__all__ = [
    "FIELDS",
    "KEY",
    "Ledger",
    "Move",
    "Split",
    "apply_to",
    "ledger",
    "moves_of",
    "quantity_after",
    "schedule",
    "schedule_of",
    "unscheduled",
]

KEY: Final = "splits"
"""The key an instrument's `info` files its schedule under."""

FIELDS: Final = ("ex_date", "ratio")
"""Everything a schedule entry holds. `cash` is refused by name; see the module note."""

REASON: Final = "split"
"""What a `PositionAdjusted` this module raises says it is for."""


@dataclass(frozen=True, slots=True, order=True)
class Split:
    """One split: the day it takes effect, and the shares it leaves per share held.

    `ratio` follows `CorporateAction.ratio` — shares held after per share held before, so
    4.0 is a four-for-one split and 0.1 a one-for-ten reverse split. A ratio of one is not
    a split and is refused, because an entry that changes nothing is a mistake rather than
    a no-op.
    """

    ex_date: date
    ratio: float

    @property
    def effective_ns(self) -> int:
        """The instant the split takes effect: the UTC midnight opening its ex-date."""
        return midnight_ns(self.ex_date)


def schedule(info: Mapping[str, Any] | None, who: str) -> tuple[Split, ...]:
    """The splits an instrument's `info` declares, in ex-date order.

    `who` names the instrument in any refusal, because a schedule is read in three places
    — resolution, the sleeve and the runner — and the operator's next action is the same
    in all three: correct the entry in `instruments.yaml`.
    """
    if not info:
        return ()
    declared = info.get(KEY)
    if declared is None:
        return ()
    if not isinstance(declared, list | tuple):
        raise ValidationError(
            f"{who}: info.{KEY} is {type(declared).__name__}, and a split schedule is a list "
            f"of entries, each naming {' and '.join(FIELDS)}",
            remedy=f"write info.{KEY} as a list of `{{ex_date, ratio}}` entries",
        )
    found = tuple(sorted(_entry(item, who, index) for index, item in enumerate(declared)))
    days = [split.ex_date for split in found]
    duplicated = sorted({day for day in days if days.count(day) > 1})
    if duplicated:
        raise ValidationError(
            f"{who}: info.{KEY} declares {len(duplicated)} ex-date(s) twice "
            f"({', '.join(str(day) for day in duplicated)}); a day holds one split",
            remedy=(
                "state one entry per ex-date, multiplying the ratios if the issuer really "
                "split twice in a day"
            ),
        )
    return found


def schedule_of(instrument: Any) -> tuple[Split, ...]:
    """The splits this engine instrument carries, in ex-date order."""
    return schedule(getattr(instrument, "info", None), str(instrument.id))


def _entry(item: object, who: str, index: int) -> Split:
    """One schedule entry, validated field by field."""
    where = f"{who}: info.{KEY}[{index}]"
    if not isinstance(item, Mapping):
        raise ValidationError(
            f"{where} is {type(item).__name__}, and a split entry is a mapping naming "
            f"{' and '.join(FIELDS)}",
            remedy="write it as `{ex_date: 2026-07-15, ratio: 0.1}`",
        )
    unknown = sorted(key for key in item if key not in FIELDS)
    if unknown:
        raise ValidationError(
            f"{where} names {', '.join(str(key) for key in unknown)}, and a split entry holds "
            f"only {' and '.join(FIELDS)}; a schedule carries no cash, because money moves "
            f"once, in the runner's extraction",
            remedy=(
                "drop the key; load a cash event as a `corporate_action` point, which carries "
                "the day it was announced and so has an honest publication instant"
            ),
        )
    missing = [field for field in FIELDS if field not in item]
    if missing:
        raise ValidationError(
            f"{where} declares no {', '.join(missing)}",
            remedy="write it as `{ex_date: 2026-07-15, ratio: 0.1}`",
        )
    return Split(ex_date=_day(item["ex_date"], where), ratio=_ratio(item["ratio"], where))


def _day(value: object, where: str) -> date:
    """A schedule entry's ex-date, however YAML handed it over."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError(
            f"{where}: ex_date {value!r} is not a calendar day",
            remedy="write the ex-date as YYYY-MM-DD",
        ) from None


def _ratio(value: object, where: str) -> float:
    """A schedule entry's ratio: shares after per share before, and never one."""
    try:
        ratio = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError(
            f"{where}: ratio {value!r} is not a number",
            remedy="write the ratio as shares held after per share held before, e.g. 0.1",
        ) from None
    if ratio <= 0.0:
        raise ValidationError(
            f"{where}: ratio {ratio} is not a positive number of shares per share",
            remedy="write the ratio as shares held after per share held before, e.g. 0.1",
        )
    if ratio == 1.0:
        raise ValidationError(
            f"{where}: ratio 1.0 leaves the position exactly as it was, so it is not a split",
            remedy="remove the entry, or state the ratio the issuer actually declared",
        )
    return ratio


def quantity_after(signed_qty: float, ratio: float, lot: float) -> float:
    """The signed quantity a position holds after a split, floored onto the lot size.

    A share count is not divisible: 1,005 shares through a one-for-ten reverse split is
    100 whole shares and a half-share the issuer pays out in cash. kanso holds no cash for
    it — the schedule carries none — so the residue is dropped and shows up in the equity
    curve as the small loss it is.
    """
    step = Decimal(str(lot))
    scaled = Decimal(repr(abs(signed_qty))) * Decimal(repr(ratio))
    units = (scaled / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return float(units) * (1.0 if signed_qty > 0 else -1.0)


def apply_to(position: Any, split: Split, lot: float, ts_ns: int) -> Any:
    """Adjust one open position for this split, and hand back the event it applied.

    The target quantity is computed here rather than left to the engine, which rounds a
    `quantity_change` to the instrument's size precision half-to-even: 1,005 shares through
    a one-for-ten reverse split would become 100 by rounding rather than by the flooring a
    fractional-share cash-out actually is.
    """
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import PositionAdjustmentType
    from nautilus_trader.model.events import PositionAdjusted

    target = quantity_after(position.signed_qty, split.ratio, lot)
    if target == 0.0:
        raise PreconditionError(
            f"split: {position.instrument_id} holds {position.quantity} on {split.ex_date}, "
            f"and a ratio of {split.ratio} leaves less than one lot of it; kanso applies a "
            f"split as a quantity change and holds no cash to pay a position out in lieu",
            remedy=(
                "raise risk_limits.max_position_pct so a position survives the split, or "
                f"drop {position.instrument_id} from a universe whose window spans "
                f"{split.ex_date}"
            ),
        )
    event = PositionAdjusted(
        trader_id=position.trader_id,
        strategy_id=position.strategy_id,
        instrument_id=position.instrument_id,
        position_id=position.id,
        account_id=position.account_id,
        adjustment_type=PositionAdjustmentType.COMMISSION,
        quantity_change=Decimal(repr(target - position.signed_qty)),
        pnl_change=None,
        reason=REASON,
        event_id=UUID4(),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )
    position.apply_adjustment(event)
    return event


def restating(schedule: Sequence[Split], printed_ns: int, through_ns: int) -> float:
    """What a price printed at `printed_ns` is divided by to be quoted in the shares held at
    `through_ns`: the product of the ratios of every split effective after the one and at or
    before the other. A one-for-ten reverse split between them makes it 0.1, so a price of
    twenty reads as the two hundred the restated share count trades at."""
    factor = 1.0
    for split in schedule:
        if printed_ns < split.effective_ns <= through_ns:
            factor *= split.ratio
    return factor


# --- what a position was, in the units it opened in ---------------------------


@dataclass(frozen=True, slots=True)
class Move:
    """One thing that happened to a position: a fill, or a split's quantity change.

    A fill carries a side and a price; an adjustment carries neither, and its `qty` is the
    signed change the engine applied. Both are flat tuples rather than engine objects, so
    the arithmetic below is a pure function anyone can call with numbers.
    """

    ts_ns: int
    qty: float
    px: float | None = None

    @property
    def is_fill(self) -> bool:
        """Whether this move was an execution rather than a corporate action."""
        return self.px is not None


@dataclass(frozen=True, slots=True)
class Ledger:
    """What a position's own moves make it, expressed in the units it opened in.

    A position that spans a split has lived in two share-count eras, and only one of them
    can be the units its size is quoted in. This is the opening era: `peak` is the largest
    position it held counted in the shares it first bought, `avg_open` is the price it
    actually paid, and `avg_close` is what it got back per opening share — which is a
    synthetic price after a split, and has to be, because the shares that closed the
    position are not the shares that opened it. `notional` therefore still equals what was
    put in, and `realized` is what came back out, both unmoved by the split itself.

    `basis` is the one number quoted in *current* units rather than opening ones: the
    average cost of a share the position still holds, rescaled by every adjustment along
    with the quantity, so `basis x quantity` is the book value at any point. It is what
    marks a position no price has been published for, in place of `Position.avg_px_open`,
    which a split leaves in the shares the position opened with.
    """

    peak: float
    avg_open: float
    avg_close: float
    realized: float
    basis: float


def ledger(moves: Iterable[Move], multiplier: float = 1.0) -> Ledger:
    """Replay a position's fills and adjustments and report what they made.

    Average-cost accounting, split-aware: a fill on the side that grows the position blends
    into the average basis, a fill on the other side realises against it, and an adjustment
    rescales both the running quantity and the basis so that the book value across a split
    is exactly what it was before one. With no adjustment anywhere the arithmetic reduces
    to the engine's own — `_calculate_avg_px` blended the same way over the same quantities
    — which is what lets one implementation serve a position that saw a split and one that
    did not.

    Commissions are not in `realized`, and are not missing from it: the simulated venue is
    cost-neutral, because kanso's resolved instruments carry a zero maker and taker rate,
    and cost is applied once per fill in the runner's extraction. Reading the fills rather
    than `Position.realized_pnl` is what keeps that true even for an instrument that
    arrived with a non-zero rate.
    """
    running = 0.0
    basis = 0.0
    factor = 1.0
    peak = 0.0
    realized = 0.0
    open_value = 0.0
    open_units = 0.0
    close_value = 0.0
    close_units = 0.0
    for move in sorted(moves, key=lambda item: (item.ts_ns, item.is_fill)):
        if not move.is_fill:
            moved = running + move.qty
            scale = 1.0 if running == 0.0 or moved == 0.0 else moved / running
            basis /= scale
            factor *= scale
            running = moved
        else:
            price = move.px or 0.0
            size = abs(move.qty)
            if running == 0.0 or (move.qty > 0.0) == (running > 0.0):
                basis = (basis * abs(running) + price * size) / (abs(running) + size)
                open_value += price * size
                open_units += size / factor
                running += move.qty
            else:
                shut = min(size, abs(running))
                realized += (1.0 if running > 0.0 else -1.0) * (price - basis) * shut * multiplier
                close_value += price * shut
                close_units += shut / factor
                running += move.qty
                flipped = size - shut
                if flipped > 0.0:
                    basis = price
                    open_value += price * flipped
                    open_units += flipped / factor
        peak = max(peak, abs(running) / factor)
    return Ledger(
        peak=peak,
        avg_open=0.0 if open_units == 0.0 else open_value / open_units,
        avg_close=0.0 if close_units == 0.0 else close_value / close_units,
        realized=realized,
        basis=basis,
    )


def moves_of(position: Any) -> tuple[Move, ...]:
    """One position's fills and split adjustments, as the flat moves `ledger` replays."""
    from nautilus_trader.model.enums import OrderSide

    made = [
        Move(
            ts_ns=int(fill.ts_event),
            qty=float(fill.last_qty) * (1.0 if fill.order_side == OrderSide.BUY else -1.0),
            px=float(fill.last_px),
        )
        for fill in position.events
    ]
    made.extend(
        Move(ts_ns=int(event.ts_event), qty=float(event.quantity_change))
        for event in position.adjustments
    )
    return tuple(made)


def unscheduled(
    instruments: Sequence[Any], points: Iterable[Any], window: tuple[date, date]
) -> None:
    """Refuse a run whose window holds a split the instrument's schedule does not.

    A `corporate_action` point of kind `split` is the one thing that tells a runner an
    ex-date exists without an operator having written it down. When the window holds one
    and the definition declares no split on that day — or declares a different ratio — the
    run would trade through the action and report the gap as return. That is the 905% this
    module exists to stop, so it is a refusal rather than a warning. A workspace that loads
    no corporate actions at all has told kanso nothing, and kanso invents nothing: the
    schedule is what makes a split-spanning window researchable.

    Two things are outside it. An action whose ex-date falls outside the window is another
    window's question, and a point for an instrument this run holds no definition for is
    another universe's — a market-wide series is filed under no instrument at all, and the
    runner already loads only what the universe names.

    How far this reaches depends on where the actions came from, and the two cases meet in
    the same place. A dataset whose points carry the instant each action was announced is
    covered by a snapshot like any other, and its splits arrive here. A source that serves
    only effective dates cannot say when a split became knowable, so its dataset declares
    no publication instant, `snapshot._covers` refuses every snapshot relying on it, and
    the run never starts — which is this refusal one step earlier and for the same reason.
    """
    from kanso.data.types import CorporateAction

    held = {str(item.id): schedule_of(item) for item in instruments}
    opens, closes = midnight_ns(window[0]), midnight_ns(window[1]) + 86_400_000_000_000
    problems: list[str] = []
    for point in points:
        action = getattr(point, "data", point)
        if not isinstance(action, CorporateAction) or action.kind != REASON:
            continue
        effective = int(action.ex_date_ns)
        name = str(action.instrument_id)
        if not opens <= effective < closes or name not in held:
            continue
        day = day_of(effective)
        scheduled = [split for split in held.get(name, ()) if split.ex_date == day]
        if not scheduled:
            problems.append(
                f"{name}: the window holds a split effective {day} at a ratio of "
                f"{action.ratio}, and its definition schedules none"
            )
        elif not _same_ratio(scheduled[0].ratio, action.ratio):
            problems.append(
                f"{name}: the window holds a split effective {day} at a ratio of "
                f"{action.ratio}, and its definition schedules {scheduled[0].ratio}"
            )
    if problems:
        raise PreconditionError(
            "; ".join(sorted(set(problems))),
            remedy=(
                "add the split to `info.splits` in this instrument's `override` in "
                "instruments.yaml, then re-resolve and re-snapshot"
            ),
        )


def _same_ratio(scheduled: float, reported: float) -> bool:
    """Whether two ratios are the same number, allowing for how each was written down."""
    return abs(scheduled - reported) <= 1e-9 * max(1.0, abs(scheduled), abs(reported))
