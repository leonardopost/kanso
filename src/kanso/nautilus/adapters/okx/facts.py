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
`OKXWebSocketClient.with_credentials`, which it calls twice with explicit `url`s — the
region's private stream, and the business stream `derive_okx_ws_url` derives from it. Both
constructors then read `OKX_API_KEY`, `OKX_API_SECRET` and `OKX_API_PASSPHRASE` from the
process environment. Measured by constructing each with `api_key=None` in a child process
whose environment held those variables set to a marker: `api_key` read back as the marker.
The plain `OKXWebSocketClient(...)` constructor, which the data client uses for its public
stream, read back `None` — it does not fall back. Each client also offers a `from_env()`
constructor, which reads the same three variables and, for the stream, its url from
`OKX_WS_URL`. A value that is passed wins over the environment. So the rule the client
factory (a later change) follows is: resolve all three of a client's own `KANSO_OKX_*`
names, refuse if any is missing, hand the engine every one of them explicitly, and never
call a `from_env` constructor — otherwise a variable exported for another tool would open
an account through kanso.

**The probe never touches the operator's environment.** An operator who trades OKX through
another tool has those three variables exported, and the check that the engine reads them
must neither read nor overwrite them. So it runs in a child process started with an
explicit environment — `PROBE_ENV`'s ambient names and the three variables set to a marker
— and reads back booleans. The parent's `OKX_*` values are never copied into it, and the
parent's environment is never written.

**A credentialed stream handed no url opens the public one.**
`OKXWebSocketClient.with_credentials(url=None, ...)` connects to
`wss://ws.okx.com:8443/ws/v5/public`, the global host's public stream, whatever the
credentials, and — measured with `OKX_WS_URL` set to another url — does not read that
variable either. A client factory that relied on the default would sign in to a stream
that serves no orders, on a host that may not serve the account, so it passes the private
url of the declared region, and the business url derived from it, explicitly, as the
engine's own execution client does.

**The regions, their hosts and the venue** are recorded in `config.py` and `venue.py` and
checked below: the engine's `OKXRegion` members, the REST host
`get_okx_http_base_url` maps each to, and the venue code `OKX_VENUE`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, Final

from kanso.nautilus.adapters.okx.config import REGION_HOSTS, Region
from kanso.nautilus.adapters.okx.venue import VENUE

__all__ = ["AMBIENT", "CLAIMS", "PUBLIC_STREAM", "probe_env"]

AMBIENT: Final[tuple[str, str, str]] = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")
"""The engine's own variable names, which kanso never sets for it and never reads."""

MARKER: Final = "kanso-engine-fact-probe"
"""What the probe's child holds in those variables: a string that is nobody's key."""

PROBE_ENV: Final = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
"""The only ambient variables the probe's child inherits; none of them is a credential."""

PUBLIC_STREAM: Final = "wss://ws.okx.com:8443/ws/v5/public"
"""Where a credentialed stream handed no url connects: the global host's public stream."""

_PROBE: Final = f"""
import json
from nautilus_trader.core import nautilus_pyo3 as engine
http = engine.OKXHttpClient(api_key=None, api_secret=None, api_passphrase=None)
stream = engine.OKXWebSocketClient.with_credentials(
    url=None, api_key=None, api_secret=None, api_passphrase=None
)
passed = engine.OKXHttpClient(api_key="passed", api_secret="passed", api_passphrase="passed")
print(json.dumps([
    http.api_key == {MARKER!r},
    stream.api_key == {MARKER!r},
    passed.api_key == "passed",
    stream.url,
]))
"""
"""Run in the child: construct the three clients and print three booleans and the stream's
url — never a credential."""


def probe_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child's whole environment: `PROBE_ENV` from `environ`, and the marker in `AMBIENT`.

    Nothing else survives, so whatever the parent holds under `AMBIENT` is replaced rather
    than passed on.
    """
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in PROBE_ENV if name in source}
    env.update(dict.fromkeys(AMBIENT, MARKER))
    return env


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


def _probe() -> tuple[list[Any] | None, str]:
    """The child's four answers, or `None` and why it failed."""
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=probe_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if done.returncode:
        return None, f"the probe exited {done.returncode}: {done.stderr.strip()[-500:]}"
    return list(json.loads(done.stdout)), ""


def _check_ambient_credentials() -> tuple[bool, str]:
    answers, failure = _probe()
    if answers is None:
        return False, failure
    http, stream, kept = (bool(one) for one in answers[:3])
    return http and stream and kept, (
        f"api_key=None read the ambient OKX_API_KEY: OKXHttpClient {http}, "
        f"OKXWebSocketClient.with_credentials {stream}; an explicit api_key was kept: {kept}"
    )


def _check_public_stream() -> tuple[bool, str]:
    answers, failure = _probe()
    if answers is None:
        return False, failure
    url = answers[3]
    return url == PUBLIC_STREAM, f"with_credentials(url=None).url = {url!r}"


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
    (
        "the engine's credentialed OKX stream handed no url connects to the global public "
        "stream, so a client must pass its region's private url",
        _check_public_stream,
    ),
    ("the engine's OKX venue code is OKX", _check_venue),
)
"""The claims, in the shape `kanso.nautilus.facts` re-checks them in."""
