"""Where a vendor's specifics live, and the only place they may.

One package per vendor, each holding everything that knows the vendor's endpoints, its
symbology, its entitlement rules and its wire formats. Nothing outside such a package
names a vendor: the rest of kanso reaches every source through `kanso.data.Loader`,
`kanso.data.InstrumentProvider` and the adapter registry, and works with none of them
configured. That isolation is a tested property, not a convention — the suite, `kanso
doctor` and the demo are all green with every vendor credential unset.

This module deliberately imports nothing. An adapter is enabled by the presence of its
credentials, never by installation, so importing one here would make the package's import
graph depend on a vendor that may never be configured.
"""

from __future__ import annotations
