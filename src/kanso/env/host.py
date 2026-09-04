"""Host-machine probes for envelope detection.

Every probe here is total. A source that is missing, unreadable, or belongs to the
other host family yields `None`, and the caller substitutes a documented conservative
value: detection on a macOS host never raises in the Linux readers, and detection on a
Linux host never raises in the macOS readers. Nothing here writes anything.

Sources
-------
macOS
    `sysctl -n` for `hw.perflevel0.logicalcpu`, `hw.perflevel1.logicalcpu`,
    `hw.logicalcpu`, `hw.memsize` and `machdep.cpu.brand_string`;
    `sw_vers -productVersion` for the OS version; `pmset -g batt` for the power source.
Linux
    `/proc/cpuinfo` for the logical core count and the chip name, `/proc/meminfo` for
    `MemTotal`, `/etc/os-release` for `VERSION_ID`, `/sys/class/power_supply/*` for the
    power source.

Fallbacks
---------
Core count falls back to `os.cpu_count()` and then to 1; memory falls back to 4 GiB,
the smallest host the lane plan is willing to assume; a version, architecture or chip
that no source reports becomes the literal `"unknown"`; and a host that reports no
power supply at all is read as mains, because a machine with no battery is a desktop,
a server or a container.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
UNKNOWN = "unknown"
FALLBACK_MEM_GB = 4
FALLBACK_CORES = 1
PROBE_TIMEOUT_S = 5.0

# Machine names that mean the same architecture. A name outside the table is reported
# verbatim, so an unfamiliar host is described rather than mislabelled.
_ARCHES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}

# /proc/cpuinfo keys that name the chip, best first. `model` alone is a numeric family
# id on x86 and must never win over `model name`.
_CHIP_KEYS = ("model name", "hardware", "cpu model", "model")


@dataclass(frozen=True)
class Facts:
    """What the host reports about itself, before Python and engine versions."""

    os: str
    os_version: str
    arch: str
    chip: str
    cores_perf: int
    cores_eff: int
    cores_total: int
    mem_gb: int
    on_ac_power: bool


def run(argv: Sequence[str]) -> str | None:
    """Stdout of a probe command, or `None` if it is absent, slow or unsuccessful."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def read_file(path: str | Path) -> str | None:
    """Text of a file, or `None` if it is absent or unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def to_int(text: str | None) -> int | None:
    """A decimal integer, or `None` if the text is absent or not one."""
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def parse_version(text: str) -> tuple[int, int] | None:
    """The leading `major.minor` of a dotted version: `"26.1.2"` becomes `(26, 1)`.

    A missing or non-numeric minor is 0; a non-numeric major means no version at all.
    """
    parts = text.strip().split(".")
    try:
        major = int(parts[0])
    except (IndexError, ValueError):
        return None
    minor = 0
    if len(parts) > 1:
        try:
            minor = int(parts[1])
        except ValueError:
            minor = 0
    return (major, minor)


def normalise_arch(name: str) -> str:
    """The canonical name of an architecture: `aarch64` and `arm64` are one."""
    lowered = name.strip().lower()
    return _ARCHES.get(lowered, lowered or UNKNOWN)


def arch() -> str:
    """This host's architecture."""
    return normalise_arch(platform.machine())


def gib(byte_count: int | None) -> int:
    """Bytes as whole GiB, to the nearest; an unreported size is 4 GiB."""
    if byte_count is None or byte_count <= 0:
        return FALLBACK_MEM_GB
    return max(1, round(byte_count / GIB))


# --- macOS ------------------------------------------------------------------------


def sysctl(name: str) -> str | None:
    """One `sysctl` value, or `None` where the key does not exist on this host."""
    return run(["sysctl", "-n", name])


def macos_version() -> str | None:
    """The macOS product version, e.g. `"26.1"`, preferring `sw_vers` over Python."""
    reported = run(["sw_vers", "-productVersion"])
    if reported:
        return reported
    return platform.mac_ver()[0] or None


def parse_pmset(text: str) -> bool | None:
    """The power source from `pmset -g batt`, whose first line names it."""
    lowered = text.lower()
    if "ac power" in lowered:
        return True
    if "battery power" in lowered:
        return False
    return None


def macos_facts() -> Facts:
    """Detect a macOS host. Safe to call anywhere: every probe may return nothing."""
    perf = to_int(sysctl("hw.perflevel0.logicalcpu"))
    eff = to_int(sysctl("hw.perflevel1.logicalcpu"))
    total = to_int(sysctl("hw.logicalcpu"))
    if total is None:
        total = (perf or 0) + (eff or 0) or os.cpu_count() or FALLBACK_CORES
    if perf is None:
        # A homogeneous Mac publishes no performance levels: every core is a fast one.
        perf, eff = total, 0
    battery = run(["pmset", "-g", "batt"])
    on_ac = parse_pmset(battery) if battery is not None else None
    return Facts(
        os="macos",
        os_version=macos_version() or UNKNOWN,
        arch=arch(),
        chip=sysctl("machdep.cpu.brand_string") or UNKNOWN,
        cores_perf=perf,
        cores_eff=eff or 0,
        cores_total=total,
        mem_gb=gib(to_int(sysctl("hw.memsize"))),
        on_ac_power=True if on_ac is None else on_ac,
    )


# --- Linux ------------------------------------------------------------------------


def parse_meminfo(text: str) -> int | None:
    """`MemTotal` from `/proc/meminfo`, in bytes. The kernel reports it in kB."""
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            kilobytes = to_int(fields[1]) if len(fields) >= 2 else None
            return kilobytes * 1024 if kilobytes is not None else None
    return None


def parse_cpuinfo(text: str) -> tuple[int | None, str | None]:
    """`(logical core count, chip name)` from `/proc/cpuinfo`.

    The count is the number of `processor` records. The name is the first value of the
    best-ranked key present, so an x86 host reports its brand string rather than the
    numeric `model` family it also publishes.
    """
    count = 0
    seen: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "processor":
            count += 1
        elif value and key in _CHIP_KEYS and key not in seen:
            seen[key] = value
    chip = next((seen[key] for key in _CHIP_KEYS if key in seen), None)
    return (count or None, chip)


def parse_os_release(text: str) -> str | None:
    """`VERSION_ID` from `/etc/os-release`, with its quoting removed."""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "VERSION_ID":
            return value.strip().strip("\"'") or None
    return None


def power_supplies(root: str | Path = "/sys/class/power_supply") -> bool | None:
    """The power source from `/sys/class/power_supply`.

    A `Mains` supply reporting `online` is AC. A mains supply that is offline, or a
    battery with no mains supply beside it, is battery. A host with no power supply at
    all reports `None`: it has no battery to run down.
    """
    try:
        entries = sorted(Path(root).iterdir())
    except OSError:
        return None
    mains = False
    battery = False
    for entry in entries:
        kind = (read_file(entry / "type") or "").strip().lower()
        if kind == "mains":
            mains = True
            if (read_file(entry / "online") or "").strip() == "1":
                return True
        elif kind == "battery":
            battery = True
    return False if (mains or battery) else None


def glibc_version() -> tuple[int, int] | None:
    """The host's glibc version, or `None` on a host that does not use glibc."""
    raw: str | None = None
    try:
        raw = os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, OSError, ValueError):
        raw = None
    if raw:
        fields = raw.split()
        if len(fields) == 2 and fields[0] == "glibc":
            return parse_version(fields[1])
    name, version = platform.libc_ver()
    return parse_version(version) if name == "glibc" and version else None


def linux_facts() -> Facts:
    """Detect a Linux host. Safe to call anywhere: every source may be absent.

    Linux publishes no performance-class split of its cores, so every core counts as a
    performance core and `cores_eff` is 0. The lane plan uses `cores_total` regardless.
    """
    count, chip = parse_cpuinfo(read_file("/proc/cpuinfo") or "")
    total = count or os.cpu_count() or FALLBACK_CORES
    meminfo = read_file("/proc/meminfo")
    release = read_file("/etc/os-release")
    on_ac = power_supplies()
    return Facts(
        os="linux",
        os_version=(parse_os_release(release) if release else None)
        or platform.release()
        or UNKNOWN,
        arch=arch(),
        chip=chip or platform.processor() or UNKNOWN,
        cores_perf=total,
        cores_eff=0,
        cores_total=total,
        mem_gb=gib(parse_meminfo(meminfo) if meminfo else None),
        on_ac_power=True if on_ac is None else on_ac,
    )


def other_facts() -> Facts:
    """Detect a host of neither supported family, from Python alone."""
    total = os.cpu_count() or FALLBACK_CORES
    return Facts(
        os=os_name(),
        os_version=platform.release() or UNKNOWN,
        arch=arch(),
        chip=platform.processor() or UNKNOWN,
        cores_perf=total,
        cores_eff=0,
        cores_total=total,
        mem_gb=FALLBACK_MEM_GB,
        on_ac_power=True,
    )


def os_name(platform_name: str | None = None) -> str:
    """The host family kanso plans for: `macos`, `linux`, or the system's own name.

    Cheap enough to call before deciding which probes to run.
    """
    name = sys.platform if platform_name is None else platform_name
    if name == "darwin":
        return "macos"
    if name.startswith("linux"):
        return "linux"
    if platform_name is not None:
        # An explicit platform is answered on its own terms. Consulting the running
        # system here would tell a caller asking about another platform about this one,
        # which on Linux made every unknown platform look like Linux.
        return name.lower() or UNKNOWN
    # `sys.platform` is abbreviated on some systems, so name the real host from the
    # system itself.
    return platform.system().lower() or UNKNOWN


def facts(platform_name: str | None = None) -> Facts:
    """Detect this host, dispatching on the platform. Never raises."""
    family = os_name(platform_name)
    if family == "macos":
        return macos_facts()
    if family == "linux":
        return linux_facts()
    return other_facts()
