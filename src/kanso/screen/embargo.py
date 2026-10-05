"""The embargo, as a screen keeps it: a screen never reads data a hypothesis certifies on.

A certification window is the data that judges a strategy, so the search that chose the
strategy may not have read it — and a screen is part of that search: it chooses which ideas
get a lane. So a free screen's window is refused when it meets, for any registered
hypothesis that holds one of the screen's instruments, the span from that hypothesis's
certification start less its embargo to its certification end. Retired hypotheses count,
because `kanso hyp resume` brings one back. A bound screen reads its own hypothesis's
research window, which the hypothesis's own windows already keep clear of its certification.

The refusal is made before any point is read, from the pinned hypotheses alone.

**The embargo binds the other way too.** Data a screen read helped choose an idea, so it may
not later judge one: `kanso hyp validate`, and so `kanso hyp add`, refuses a hypothesis whose
certification window, with the embargo before it, meets the window any recorded screen read
for one of its instruments (`refuse_screened`). Which order the two arrive in does not matter:
a screen of data a registered hypothesis certifies on is refused at `screen run`, and a
hypothesis certifying on data a recorded screen read is refused at `hyp validate`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import hypothesis_of, show
from kanso.hyp.registry import Registration
from kanso.schemas import embargo_days
from kanso.schemas.screen import Screen
from kanso.screen.records import screened

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.schemas import Hypothesis
    from kanso.state import StateStore
    from kanso.workspace import Workspace


def refuse_certification_data(
    ws: Workspace, store: StateStore, screen: Screen, window: tuple[date, date]
) -> None:
    """Refuse a free screen whose window meets a registered certification span."""
    if screen.hyp is not None:
        return
    instruments = {leg.instrument for leg in screen.legs.values()}
    registrations = show(ws, store)
    assert isinstance(registrations, list)
    for registration in registrations:
        _refuse_one(ws, store, registration, instruments, window)


def _refuse_one(
    ws: Workspace,
    store: StateStore,
    registration: Registration,
    instruments: set[str],
    window: tuple[date, date],
) -> None:
    hypothesis = hypothesis_of(ws, store, registration.hyp_id)
    shared = sorted(instruments.intersection(hypothesis.universe))
    if not shared:
        return
    certification = hypothesis.windows.certification
    days = embargo_days(hypothesis.horizon)
    if clear(window, certification.start, certification.end, days):
        return
    latest = certification.start - timedelta(days=days)
    raise PreconditionError(
        f"window {window[0]}..{window[1]} meets the data {hypothesis.id} certifies on — "
        f"its certification {certification.start}..{certification.end} and the {days} "
        f"day(s) of embargo before it — on {', '.join(shared)}; a screen chooses ideas, and "
        f"the data that judges one may not have chosen it",
        remedy=f"end the window on or before {latest}, or begin it after {certification.end}",
    )


def clear(window: tuple[date, date], start: date, end: date, days: int) -> bool:
    """Whether data read over `window` may certify over `start..end` under `days` of embargo.

    kanso's own rule for a hypothesis's windows: certification starts no sooner than the
    embargo after the last day research read, so a window ending `days` before the start is
    clear; a window wholly after the certification end is clear too.
    """
    return window[1] + timedelta(days=days) <= start or window[0] > end


def refuse_screened(store: StateStore, hypothesis: Hypothesis) -> None:
    """Refuse a hypothesis that would certify on data a recorded screen read."""
    certification = hypothesis.windows.certification
    days = embargo_days(hypothesis.horizon)
    for read in screened(store):
        shared = sorted(set(read.instruments).intersection(hypothesis.universe))
        if not shared or clear(read.window, certification.start, certification.end, days):
            continue
        earliest = read.window[1] + timedelta(days=days)
        raise ValidationError(
            f"windows.certification: {certification.start}..{certification.end}, and the "
            f"{days} day(s) of embargo before it, meet the window {read.window[0]}.."
            f"{read.window[1]} screen {read.screen} read on {', '.join(shared)}; data a "
            f"screen read helped choose an idea, and may not judge one",
            remedy=f"start the certification window on or after {earliest}, or certify on "
            "data no screen has read",
        )
