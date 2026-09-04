"""The certification suite's workspace, which is the research suite's.

Certification runs real backtests, so its tests need a workspace with a catalog, a frozen
snapshot, resolved instruments and an envelope — exactly what the research suite already
builds from synthetic bars and nothing else. Re-exporting those fixtures keeps one
definition of the demo workspace in the tree; a module wanting a different one shadows
them by defining its own, as the planner's tests do.
"""

from __future__ import annotations

from tests.research.conftest import prepared, store, ws

__all__ = ["prepared", "store", "ws"]
