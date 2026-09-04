"""Classification: what a hypothesis is, and the catalogue of what it could be.

The construct catalogue is this package's public interface. `Construct` is the contract an
implementation satisfies, shipped or provided by a workspace extension; `constructs`,
`catalogue` and `get` are how the rest of kanso reaches them.
"""

from __future__ import annotations

from kanso.classify.construct import (
    Attached,
    Base,
    Catalogue,
    Construct,
    Entry,
    Harness,
    HostRef,
    Seam,
    Shadow,
    Sleeve,
    catalogue,
    constructs,
    get,
    run_key,
)

__all__ = [
    "Attached",
    "Base",
    "Catalogue",
    "Construct",
    "Entry",
    "Harness",
    "HostRef",
    "Seam",
    "Shadow",
    "Sleeve",
    "catalogue",
    "constructs",
    "get",
    "run_key",
]
