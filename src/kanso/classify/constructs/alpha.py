"""`alpha`: a return forecast that does not trade by itself. Classification only."""

from __future__ import annotations

from typing import Final

from kanso.classify.construct import Construct, Seam


class Alpha(Seam):
    id = "alpha"
    needs_host = "sleeve"
    objective_mode = "absolute"
    seam = (
        "the canonical wrapper that turns a forecast into a measurable strategy, and the "
        "rule that combines forecasts inside an alpha-combining sleeve"
    )


CONSTRUCT: Final[Construct] = Alpha()
