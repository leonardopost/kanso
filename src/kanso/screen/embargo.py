"""The embargo, as a screen keeps it: a screen never reads data a hypothesis certifies on.

A certification window is the data that judges a strategy, so the search that chose the
strategy may not have read it — and a screen is part of that search: it chooses which ideas
get a lane. So a free screen's window is refused when it meets, for any registered
hypothesis that holds one of the screen's instruments, the span from that hypothesis's
certification start less its embargo to its certification end. Retired hypotheses count,
because `kanso hyp resume` brings one back. A bound screen reads its own hypothesis's
research window, which the hypothesis's own windows already keep clear of its certification.

The refusal is made before any point is read, from the pinned hypotheses alone.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from kanso.errors import PreconditionError
from kanso.hyp import hypothesis_of, show
from kanso.hyp.registry import Registration
from kanso.schemas import embargo_days
from kanso.schemas.screen import Screen

if TYPE_CHECKING:  # pragma: no cover - annotations only
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
    opens = certification.start - timedelta(days=days)
    if window[0] > certification.end or window[1] < opens:
        return
    raise PreconditionError(
        f"window {window[0]}..{window[1]} meets the data {hypothesis.id} certifies on — "
        f"its certification {certification.start}..{certification.end} and the {days} "
        f"day(s) of embargo before it — on {', '.join(shared)}; a screen chooses ideas, and "
        f"the data that judges one may not have chosen it",
        remedy=f"end the window before {opens} or begin it after {certification.end}",
    )
