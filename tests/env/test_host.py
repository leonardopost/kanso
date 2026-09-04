"""Host probes: parsers, both host families, and the fallbacks that keep them total."""

from __future__ import annotations

import platform
from collections.abc import Callable
from pathlib import Path

import pytest

from kanso.env import host

from .conftest import (
    CPUINFO_X86,
    MEMINFO,
    OS_RELEASE,
    PMSET_AC,
    PMSET_BATTERY,
    write_power_supplies,
)


def test_run_returns_none_for_a_missing_command() -> None:
    assert host.run(["kanso-no-such-binary", "--version"]) is None


def test_run_returns_none_for_a_failing_command() -> None:
    assert host.run(["sh", "-c", "exit 3"]) is None


def test_run_returns_stripped_stdout() -> None:
    assert host.run(["sh", "-c", "printf ' hello \\n'"]) == "hello"


def test_read_file_returns_none_for_a_missing_path(tmp_path: Path) -> None:
    assert host.read_file(tmp_path / "absent") is None


def test_read_file_returns_none_for_a_directory(tmp_path: Path) -> None:
    assert host.read_file(tmp_path) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("7", 7), (" 7 \n", 7), ("seven", None), ("", None), (None, None)],
)
def test_to_int(text: str | None, expected: int | None) -> None:
    assert host.to_int(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("26.1.2", (26, 1)),
        ("26", (26, 0)),
        ("2.35", (2, 35)),
        (" 27.0 ", (27, 0)),
        ("26.x", (26, 0)),
        ("", None),
        ("sonoma", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int] | None) -> None:
    assert host.parse_version(text) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("arm64", "arm64"),
        ("aarch64", "arm64"),
        ("ARM64", "arm64"),
        ("x86_64", "x86_64"),
        ("AMD64", "x86_64"),
        ("riscv64", "riscv64"),
        ("", "unknown"),
    ],
)
def test_normalise_arch(name: str, expected: str) -> None:
    assert host.normalise_arch(name) == expected


def test_arch_matches_this_host() -> None:
    assert host.arch() == host.normalise_arch(platform.machine())


@pytest.mark.parametrize(
    ("byte_count", "expected"),
    [
        (64 * host.GIB, 64),
        (17179869184, 16),
        (33615241216, 31),
        (0, host.FALLBACK_MEM_GB),
        (-1, host.FALLBACK_MEM_GB),
        (None, host.FALLBACK_MEM_GB),
        (1024, 1),
    ],
)
def test_gib(byte_count: int | None, expected: int) -> None:
    assert host.gib(byte_count) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [(PMSET_AC, True), (PMSET_BATTERY, False), ("no idea", None), ("", None)],
)
def test_parse_pmset(text: str, expected: bool | None) -> None:
    assert host.parse_pmset(text) == expected


def test_parse_meminfo() -> None:
    assert host.parse_meminfo(MEMINFO) == 32827384 * 1024


@pytest.mark.parametrize("text", ["", "MemFree: 1 kB\n", "MemTotal:\n", "MemTotal: lots kB\n"])
def test_parse_meminfo_without_a_total(text: str) -> None:
    assert host.parse_meminfo(text) is None


def test_parse_cpuinfo_counts_processors_and_prefers_the_brand_string() -> None:
    count, chip = host.parse_cpuinfo(CPUINFO_X86)
    assert count == 4
    assert chip == "Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz"


def test_parse_cpuinfo_falls_back_to_a_lower_ranked_key() -> None:
    text = "processor\t: 0\nHardware\t: BCM2835\nmodel\t\t: 3\n"
    assert host.parse_cpuinfo(text) == (1, "BCM2835")


def test_parse_cpuinfo_on_an_empty_file() -> None:
    assert host.parse_cpuinfo("") == (None, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (OS_RELEASE, "22.04"),
        ("VERSION_ID=39\n", "39"),
        ("VERSION_ID='24.04'\n", "24.04"),
        ("VERSION_ID=\n", None),
        ('NAME="Arch Linux"\n', None),
        ("", None),
    ],
)
def test_parse_os_release(text: str, expected: str | None) -> None:
    assert host.parse_os_release(text) == expected


def test_power_supplies_reports_ac_when_mains_is_online(tmp_path: Path) -> None:
    write_power_supplies(tmp_path, [("BAT0", "Battery", None), ("ADP1", "Mains", "1")])
    assert host.power_supplies(tmp_path) is True


def test_power_supplies_reports_battery_when_mains_is_offline(tmp_path: Path) -> None:
    write_power_supplies(tmp_path, [("ADP1", "Mains", "0"), ("BAT0", "Battery", None)])
    assert host.power_supplies(tmp_path) is False


def test_power_supplies_reports_battery_without_a_mains_supply(tmp_path: Path) -> None:
    write_power_supplies(tmp_path, [("BAT0", "Battery", None)])
    assert host.power_supplies(tmp_path) is False


def test_power_supplies_reports_nothing_without_any_supply(tmp_path: Path) -> None:
    assert host.power_supplies(tmp_path) is None


def test_power_supplies_reports_nothing_without_the_directory(tmp_path: Path) -> None:
    assert host.power_supplies(tmp_path / "absent") is None


def test_glibc_version_is_a_version_on_linux_and_nothing_on_macos() -> None:
    version = host.glibc_version()
    if host.os_name() == "linux":
        assert version is None or version >= (2, 0)
    else:
        assert version is None


def test_glibc_version_reads_the_c_library_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host.os, "confstr", lambda _name: "glibc 2.39")
    assert host.glibc_version() == (2, 39)


def test_glibc_version_falls_back_to_the_platform_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host.os, "confstr", lambda _name: None)
    monkeypatch.setattr(host.platform, "libc_ver", lambda: ("glibc", "2.35"))
    assert host.glibc_version() == (2, 35)


def test_glibc_version_of_a_host_that_does_not_use_glibc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host.os, "confstr", lambda _name: "musl 1.2.5")
    monkeypatch.setattr(host.platform, "libc_ver", lambda: ("", ""))
    assert host.glibc_version() is None


# --- whole-host detection ----------------------------------------------------------


def test_macos_facts_on_a_faked_apple_silicon_host(fake_macos: Callable[..., None]) -> None:
    fake_macos()
    facts = host.macos_facts()
    assert facts.os == "macos"
    assert facts.os_version == "26.0"
    assert facts.chip == "Apple M4 Max"
    assert (facts.cores_perf, facts.cores_eff, facts.cores_total) == (12, 4, 16)
    assert facts.mem_gb == 64
    assert facts.on_ac_power is True


def test_macos_facts_on_a_homogeneous_mac(fake_macos: Callable[..., None]) -> None:
    fake_macos(
        sysctl={"hw.logicalcpu": "8", "hw.memsize": str(32 * host.GIB)},
        product_version="26.4",
        pmset=None,
    )
    facts = host.macos_facts()
    assert (facts.cores_perf, facts.cores_eff, facts.cores_total) == (8, 0, 8)
    assert facts.chip == host.UNKNOWN
    assert facts.on_ac_power is True


def test_macos_facts_sums_perf_levels_when_the_total_is_missing(
    fake_macos: Callable[..., None],
) -> None:
    fake_macos(
        sysctl={"hw.perflevel0.logicalcpu": "10", "hw.perflevel1.logicalcpu": "4"},
        product_version=None,
    )
    facts = host.macos_facts()
    assert facts.cores_total == 14
    assert facts.mem_gb == host.FALLBACK_MEM_GB
    assert facts.os_version in {host.UNKNOWN, platform.mac_ver()[0]}


def test_macos_facts_reads_battery_power(fake_macos: Callable[..., None]) -> None:
    fake_macos(pmset=PMSET_BATTERY)
    assert host.macos_facts().on_ac_power is False


def test_linux_facts_on_a_faked_ubuntu_host(fake_linux: Callable[..., None]) -> None:
    fake_linux()
    facts = host.linux_facts()
    assert facts.os == "linux"
    assert facts.os_version == "22.04"
    assert facts.chip == "Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz"
    assert (facts.cores_perf, facts.cores_eff, facts.cores_total) == (4, 0, 4)
    assert facts.mem_gb == 31
    assert facts.on_ac_power is True


def test_linux_facts_on_a_laptop_running_on_battery(fake_linux: Callable[..., None]) -> None:
    fake_linux(on_ac_power=False)
    assert host.linux_facts().on_ac_power is False


def test_linux_facts_falls_back_when_proc_is_empty(fake_linux: Callable[..., None]) -> None:
    fake_linux(files={}, on_ac_power=None)
    facts = host.linux_facts()
    assert facts.cores_total >= 1
    assert facts.mem_gb == host.FALLBACK_MEM_GB
    assert facts.os_version != ""
    assert facts.on_ac_power is True


def test_neither_host_family_raises_on_the_other() -> None:
    for facts in (host.macos_facts(), host.linux_facts(), host.other_facts()):
        assert facts.cores_total >= 1
        assert facts.mem_gb >= 1
        assert facts.arch == host.arch()


@pytest.mark.parametrize(
    ("platform_name", "expected"),
    [("darwin", "macos"), ("linux", "linux"), ("linux2", "linux")],
)
def test_os_name(platform_name: str, expected: str) -> None:
    assert host.os_name(platform_name) == expected


def test_os_name_of_an_unsupported_platform_is_that_platform() -> None:
    # Answered on its own terms rather than from the running system, so the result does
    # not change with the host the tests happen to run on.
    assert host.os_name("plan9") == "plan9"


def test_os_name_of_the_real_host_falls_back_to_the_system_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host.sys, "platform", "sunos5")

    assert host.os_name() == platform.system().lower()


def test_facts_dispatches_on_the_platform(
    fake_macos: Callable[..., None], fake_linux: Callable[..., None]
) -> None:
    fake_macos()
    fake_linux()
    assert host.facts("darwin").mem_gb == 64
    assert host.facts("linux").mem_gb == 31
    # Neither family: Python alone, so neither faked memory size shows through.
    assert host.facts("plan9").mem_gb == host.FALLBACK_MEM_GB


def test_facts_describes_this_host() -> None:
    facts = host.facts()
    assert facts.os == host.os_name()
    assert facts.cores_total >= 1
    assert facts.cores_perf + facts.cores_eff >= 1
    assert facts.mem_gb >= 1
