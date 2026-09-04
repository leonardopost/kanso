"""`allocation`: capital or risk allocation across sleeves. Classification only."""

from __future__ import annotations

from typing import Final

from kanso.classify.construct import Construct, Seam


class Allocation(Seam):
    id = "allocation"
    needs_host = "portfolio"
    seam = (
        "portfolio-level capital and risk allocation across the deployed sleeves, measured "
        "relative to the allocation currently in force"
    )


CONSTRUCT: Final[Construct] = Allocation()
