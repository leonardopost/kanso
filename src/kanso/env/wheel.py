"""Compatibility of the installed NautilusTrader wheel with this host.

The rule: the platform tag's minimum OS version must be at most the host's, and the
tag's architecture must equal the host's. A newer host running an older tag is
correct — equality is never required, so a `macosx_26_0_arm64` wheel is the right
wheel for an arm64 host running macOS 27.

nautilus_trader 1.231.0 publishes a compiled wheel per host family and no pure-Python
fallback, so an incompatible tag means the package would have to be built from source.
A distribution installed from source or in editable mode carries no platform tag at
all; there is then nothing to compare, and the answer is compatible.

Where a host cannot report the version half of the comparison — an unknown libc, a
`sw_vers` that does not answer — only the architecture is compared, and the detail
string says so. The check never raises: it returns a verdict and one line of evidence.
"""

from __future__ import annotations

import importlib.metadata
import platform

from kanso.env import host

DISTRIBUTION = "nautilus_trader"

# macOS tag architecture -> the host architectures it serves.
_MACOS_ARCHES: dict[str, frozenset[str]] = {
    "arm64": frozenset({"arm64"}),
    "x86_64": frozenset({"x86_64"}),
    "universal2": frozenset({"arm64", "x86_64"}),
    "intel": frozenset({"x86_64"}),
}

# The glibc version each pre-PEP-600 manylinux alias stands for.
_LEGACY_MANYLINUX: dict[str, tuple[int, int]] = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}


def platform_tags(wheel_text: str) -> list[str]:
    """The platform components of every `Tag:` line of a `WHEEL` file, in order.

    A tag is `<python>-<abi>-<platform>`, and a compressed tag set joins several
    platforms with dots; both are expanded here, and duplicates are dropped.
    """
    found: list[str] = []
    for line in wheel_text.splitlines():
        key, sep, value = line.partition(":")
        if not sep or key.strip().lower() != "tag":
            continue
        fields = value.strip().split("-")
        if len(fields) < 3:
            continue
        for name in "-".join(fields[2:]).split("."):
            if name and name not in found:
                found.append(name)
    return found


def tag_fits(
    tag: str,
    *,
    os_name: str,
    arch: str,
    os_version: tuple[int, int] | None,
) -> bool:
    """Does one wheel platform tag fit a host?

    `os_version` is the host's macOS product version on macOS and its glibc version on
    Linux. `None` means the host reported none, and the version half of the rule is
    waived: only the architecture is compared.
    """
    if tag == "any":
        return True
    if tag.startswith("macosx_"):
        return _macos_fits(tag, os_name=os_name, arch=arch, os_version=os_version)
    if tag.startswith(("manylinux", "musllinux", "linux_")):
        return _linux_fits(tag, os_name=os_name, arch=arch, os_version=os_version)
    return False


def _macos_fits(
    tag: str,
    *,
    os_name: str,
    arch: str,
    os_version: tuple[int, int] | None,
) -> bool:
    if os_name != "macos":
        return False
    fields = tag.removeprefix("macosx_").split("_")
    if len(fields) < 3:
        return False
    minimum = host.parse_version(f"{fields[0]}.{fields[1]}")
    if minimum is None:
        return False
    if arch not in _MACOS_ARCHES.get("_".join(fields[2:]), frozenset()):
        return False
    return os_version is None or minimum <= os_version


def _linux_fits(
    tag: str,
    *,
    os_name: str,
    arch: str,
    os_version: tuple[int, int] | None,
) -> bool:
    if os_name != "linux":
        return False
    if tag.startswith("linux_"):
        # An untagged native build: portable to nothing, but it is what is installed.
        return host.normalise_arch(tag.removeprefix("linux_")) == arch
    for alias, alias_floor in _LEGACY_MANYLINUX.items():
        if tag.startswith(f"{alias}_"):
            tag_arch = tag.removeprefix(f"{alias}_")
            return host.normalise_arch(tag_arch) == arch and (
                os_version is None or alias_floor <= os_version
            )
    if tag.startswith("musllinux_"):
        # `os_version` is a glibc version; a host that reports one is not a musl host.
        return os_version is None and host.normalise_arch(tag.split("_", 3)[-1]) == arch
    # `manylinux_<major>_<minor>_<arch>`: the last field keeps its own underscores.
    fields = tag.removeprefix("manylinux_").split("_", 2)
    if len(fields) < 3:
        return False
    floor = host.parse_version(f"{fields[0]}.{fields[1]}")
    if floor is None:
        return False
    return host.normalise_arch(fields[2]) == arch and (os_version is None or floor <= os_version)


def _host_version(os_name: str) -> tuple[tuple[int, int] | None, str]:
    """The version half of the comparison, and how to describe the host in one line."""
    arch = host.arch()
    if os_name == "macos":
        reported = host.macos_version()
        version = host.parse_version(reported) if reported else None
        return version, f"macOS {reported or host.UNKNOWN} {arch}"
    if os_name == "linux":
        version = host.glibc_version()
        libc = f"glibc {version[0]}.{version[1]}" if version else "an unknown libc"
        return version, f"Linux with {libc} {arch}"
    return None, f"{os_name} {platform.release()} {arch}"


def wheel_ok() -> tuple[bool, str]:
    """Is the installed NautilusTrader wheel compatible with this host?

    Returns the verdict and one line of evidence naming the wheel tag and the host, so
    an operator can act on a mismatch without guessing which half of it failed.
    """
    try:
        distribution = importlib.metadata.distribution(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return False, f"{DISTRIBUTION} is not installed"
    version = distribution.version
    try:
        metadata = distribution.read_text("WHEEL")
    except OSError:
        metadata = None
    tags = platform_tags(metadata or "")
    if not tags:
        return True, (
            f"{DISTRIBUTION} {version} carries no wheel platform tag "
            "(installed from source or in editable mode); nothing to compare"
        )
    os_name = host.os_name()
    os_version, described = _host_version(os_name)
    waived = " (architecture only: the host reports no comparable version)"
    note = waived if os_version is None else ""
    for tag in tags:
        if tag_fits(tag, os_name=os_name, arch=host.arch(), os_version=os_version):
            return True, f"{DISTRIBUTION} {version} wheel {tag} fits {described}{note}"
    return False, (f"{DISTRIBUTION} {version} wheel {'/'.join(tags)} does not fit {described}")
