"""`execution`: how a host's orders are worked. Classification only."""

from __future__ import annotations

from typing import Final

from kanso.classify.construct import Construct, Seam


class Execution(Seam):
    id = "execution"
    seam = (
        "NautilusTrader execution algorithms driving the host's orders, scored by the net "
        "P&L delta that fill quality produces"
    )


CONSTRUCT: Final[Construct] = Execution()
