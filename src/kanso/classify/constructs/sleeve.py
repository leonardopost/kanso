"""`sleeve`: a strategy of its own, measured absolutely, hosted by nothing.

The lane's `strategy.py` is the strategy class itself, and composition makes it version 1
of a new strategy in the portfolio.
"""

from __future__ import annotations

from typing import Final

from kanso.classify.construct import Construct, Sleeve


class SleeveConstruct(Sleeve):
    id = "sleeve"


CONSTRUCT: Final[Construct] = SleeveConstruct()
