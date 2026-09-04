"""`overlay`: exposure modification layered on a host without touching its signal.

Two decisions reach the host: `Decision.scale` through its `size` hook, which resizes what
the signal already asked for, and `Decision.hedges` through its `hedges` hook, which adds
legs beside it. An overlay whose host is the portfolio rather than a sleeve is classifiable
and refused: allocating exposure across the book is a seam of its own.
"""

from __future__ import annotations

from typing import Final

from kanso.classify.construct import Attached, Construct


class Overlay(Attached):
    id = "overlay"
    consults = {"scale": "size", "hedges": "hedges"}
    portfolio_seam = (
        "an overlay hosted by the portfolio rather than by one sleeve, which needs a "
        "portfolio-level host run to be relative to"
    )


CONSTRUCT: Final[Construct] = Overlay()
