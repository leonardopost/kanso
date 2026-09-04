"""Host envelope detection and the derived lane plan.

`detect()` describes the machine and works out how many research lanes fit on it;
`write()` and `read()` persist that answer as the workspace's `envelope.yaml`;
`wheel_ok()` answers whether the installed NautilusTrader wheel suits this host.

The `Detected`, `Plan` and `Envelope` models are the workspace data model's, re-exported
here so a caller needs one import to detect a host and hold the result.
"""

from __future__ import annotations

from kanso.env.envelope import FILENAME, detect, plan_for, read, write
from kanso.env.wheel import wheel_ok
from kanso.schemas.envelope import Detected, Envelope, Plan

__all__ = [
    "FILENAME",
    "Detected",
    "Envelope",
    "Plan",
    "detect",
    "plan_for",
    "read",
    "wheel_ok",
    "write",
]
