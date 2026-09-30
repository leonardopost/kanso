"""What this package relies on in the engine's own OKX adapter, each with its check.

`kanso.nautilus.facts` may not name a broker, so these claims live here and reach it through
the broker registry: `kanso doctor` re-checks them against the installed engine beside the
core's own, and a claim that stops holding is a broken binding like any other.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
**A credential handed as `None` is read from the ambient environment.** The engine's
`OKXDataClientConfig` and `OKXExecClientConfig` default `api_key`, `api_secret` and
`api_passphrase` to `None`, and the engine passes those straight on: the factories to
`nautilus_pyo3.OKXHttpClient`, the execution client to
`OKXWebSocketClient.with_credentials` for its private and business streams. Both then read
`OKX_API_KEY`, `OKX_API_SECRET` and `OKX_API_PASSPHRASE` from the process environment.
Measured by constructing each with `api_key=None` and those variables set to a marker:
`api_key` read back as the marker. The plain `OKXWebSocketClient(...)` constructor, which
the data client uses for its public stream, read back `None` — it does not fall back. Each
client also offers a `from_env()` constructor that reads nothing else. A value that is
passed wins over the environment. So the rule the
client factory (a later change) follows is: resolve all three of a client's own
`KANSO_OKX_*` names, refuse if any is missing, hand the engine every one of them explicitly,
and never call a `from_env` constructor — otherwise a variable exported for another tool
would open an account through kanso.

**The regions, their hosts and the venue** are recorded in `config.py` and `venue.py` and
checked below: the engine's `OKXRegion` members, the REST host
`get_okx_http_base_url` maps each to, and the venue code `OKX_VENUE`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from kanso.nautilus.adapters.okx.config import REGION_HOSTS, Region
from kanso.nautilus.adapters.okx.venue import VENUE

__all__ = ["AMBIENT", "CLAIMS"]

AMBIENT: Final[tuple[str, str, str]] = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")
"""The engine's own variable names, which kanso never sets for it and never reads."""

MARKER: Final = "kanso-engine-fact-probe"
"""What the probe puts in those variables: a string that is nobody's key."""


@contextmanager
def _marked() -> Iterator[None]:
    """The three ambient variables set to the marker, and put back exactly as they were."""
    held = {name: os.environ.get(name) for name in AMBIENT}
    try:
        for name in AMBIENT:
            os.environ[name] = MARKER
        yield
    finally:
        for name, value in held.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _engine_regions() -> Any:
    """The engine's `OKXRegion`, typed loosely: its stub omits `variants` and `from_str`."""
    from nautilus_trader.core import nautilus_pyo3

    return nautilus_pyo3.OKXRegion


def _check_regions() -> tuple[bool, str]:
    regions = _engine_regions()
    engine = sorted(str(name) for name in regions.variants())
    parsed = [str(regions.from_str(region.value.upper())) for region in Region]
    ours = [region.value for region in Region]
    holds = engine == sorted(ours) and parsed == ours
    return holds, f"OKXRegion.variants() = {engine}; from_str of the upper case = {parsed}"


def _check_hosts() -> tuple[bool, str]:
    from nautilus_trader.core import nautilus_pyo3

    regions = _engine_regions()
    found = {
        region.value: nautilus_pyo3.get_okx_http_base_url(regions.from_str(region.value))
        for region in Region
    }
    default = nautilus_pyo3.get_okx_http_base_url(None)
    expected = {region.value: host for region, host in REGION_HOSTS.items()}
    holds = found == expected and default == REGION_HOSTS[Region.GLOBAL]
    return holds, f"get_okx_http_base_url: {found}; None -> {default}"


def _check_ambient_credentials() -> tuple[bool, str]:
    from nautilus_trader.core import nautilus_pyo3

    with _marked():
        http = nautilus_pyo3.OKXHttpClient(api_key=None, api_secret=None, api_passphrase=None)
        stream = nautilus_pyo3.OKXWebSocketClient.with_credentials(
            url=None, api_key=None, api_secret=None, api_passphrase=None
        )
        passed = nautilus_pyo3.OKXHttpClient(
            api_key="passed", api_secret="passed", api_passphrase="passed"
        )
        read = (http.api_key == MARKER, stream.api_key == MARKER)
        kept = passed.api_key == "passed"
    return all(read) and kept, (
        f"api_key=None read the ambient OKX_API_KEY: OKXHttpClient {read[0]}, "
        f"OKXWebSocketClient.with_credentials {read[1]}; an explicit api_key was kept: {kept}"
    )


def _check_venue() -> tuple[bool, str]:
    from nautilus_trader.adapters.okx.constants import OKX_VENUE

    return OKX_VENUE.value == VENUE, f"OKX_VENUE = {OKX_VENUE.value!r}"


CLAIMS: Final = (
    ("the engine's OKX regions are exactly global, eea and us, in any case", _check_regions),
    (
        "the engine maps the OKX regions global, eea and us to www, eea and us.okx.com, "
        "and an unstated region to www.okx.com",
        _check_hosts,
    ),
    (
        "the engine's OKX HTTP client and credentialed stream read OKX_API_KEY from the "
        "environment when handed None, and keep a key they are handed",
        _check_ambient_credentials,
    ),
    ("the engine's OKX venue code is OKX", _check_venue),
)
"""The claims, in the shape `kanso.nautilus.facts` re-checks them in."""
