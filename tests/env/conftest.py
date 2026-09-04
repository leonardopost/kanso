"""Fixtures for the envelope slice: fake hosts and a stand-in workspace."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from kanso.env import host

CPUINFO_X86 = """\
processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 158
model name\t: Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz
stepping\t: 10

processor\t: 1
model\t\t: 158
model name\t: Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz

processor\t: 2
model\t\t: 158
model name\t: Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz

processor\t: 3
model\t\t: 158
model name\t: Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz
"""

MEMINFO = """\
MemTotal:       32827384 kB
MemFree:         1234567 kB
SwapTotal:             0 kB
"""

OS_RELEASE = """\
NAME="Ubuntu"
VERSION="22.04.4 LTS (Jammy Jellyfish)"
ID=ubuntu
VERSION_ID="22.04"
"""

PMSET_AC = """\
Now drawing from 'AC Power'
 -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true
"""

PMSET_BATTERY = """\
Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1)\t62%; discharging; 3:41 remaining present: true
"""

SYSCTL_M4_MAX = {
    "hw.perflevel0.logicalcpu": "12",
    "hw.perflevel1.logicalcpu": "4",
    "hw.logicalcpu": "16",
    "hw.memsize": str(64 * host.GIB),
    "machdep.cpu.brand_string": "Apple M4 Max",
}


@dataclass(frozen=True)
class FakeEnv:
    """Stands in for the `[env]` table of a parsed `kanso.toml`."""

    reserved_cores: int | None = None
    reserved_mem_gb: float | None = None
    cores_per_lane: int | None = None


@dataclass(frozen=True)
class FakeConfig:
    env: FakeEnv = FakeEnv()


@dataclass(frozen=True)
class FakeWorkspace:
    """The two attributes the envelope reads off a workspace."""

    root: Path
    config: FakeConfig = FakeConfig()


@pytest.fixture
def workspace(tmp_path: Path) -> FakeWorkspace:
    return FakeWorkspace(root=tmp_path)


@pytest.fixture
def fake_macos(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make the macOS probes answer from a table, so the path runs on any host."""

    def install(
        sysctl: Mapping[str, str] = SYSCTL_M4_MAX,
        product_version: str | None = "26.0",
        pmset: str | None = PMSET_AC,
    ) -> None:
        def run(argv: Sequence[str]) -> str | None:
            command = list(argv)
            if command[:2] == ["sysctl", "-n"]:
                return sysctl.get(command[2])
            if command == ["sw_vers", "-productVersion"]:
                return product_version
            if command == ["pmset", "-g", "batt"]:
                return pmset
            return None

        monkeypatch.setattr(host, "run", run)

    return install


@pytest.fixture
def fake_linux(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make the Linux probes answer from a table, so the path runs on any host."""

    def install(
        files: Mapping[str, str] | None = None,
        on_ac_power: bool | None = True,
    ) -> None:
        table = dict(
            files
            if files is not None
            else {
                "/proc/cpuinfo": CPUINFO_X86,
                "/proc/meminfo": MEMINFO,
                "/etc/os-release": OS_RELEASE,
            }
        )

        def read_file(path: str | Path) -> str | None:
            return table.get(str(path))

        monkeypatch.setattr(host, "read_file", read_file)
        monkeypatch.setattr(host, "power_supplies", lambda *_: on_ac_power)

    return install


def write_power_supplies(root: Path, entries: Iterable[tuple[str, str, str | None]]) -> Path:
    """Build a `/sys/class/power_supply` tree: `(name, type, online)` per supply."""
    for name, kind, online in entries:
        supply = root / name
        supply.mkdir(parents=True)
        (supply / "type").write_text(f"{kind}\n")
        if online is not None:
            (supply / "online").write_text(f"{online}\n")
    return root
