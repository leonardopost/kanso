"""`filter`: a conditioning rule that gates a host sleeve's entries.

The modifier's `Decision.allow` is what the host's `before_entry` hook consults, so a
filter never changes the host's signal — it only withholds entries the rule excludes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final

from kanso.classify.construct import Attached, Construct


class Filter(Attached):
    id = "filter"
    params: ClassVar[Mapping[str, tuple[str, ...]]] = {"scope": ("time", "instrument")}
    consults = {"allow": "before_entry"}


CONSTRUCT: Final[Construct] = Filter()
