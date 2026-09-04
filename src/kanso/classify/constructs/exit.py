"""`exit`: exit logic added to or replacing a host sleeve's.

The modifier's `Decision.exit` is what the host's `before_exit` hook consults, so stops,
targets, time exits and trailing rules are tested against the host that would have held.
"""

from __future__ import annotations

from typing import Final

from kanso.classify.construct import Attached, Construct


class Exit(Attached):
    id = "exit"
    consults = {"exit": "before_exit"}


CONSTRUCT: Final[Construct] = Exit()
