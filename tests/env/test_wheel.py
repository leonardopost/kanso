"""The wheel rule: a tag's minimum OS at most the host's, its architecture equal."""

from __future__ import annotations

import importlib.metadata

import pytest

from kanso.env import host, wheel

MACOS_WHEEL = (
    "Wheel-Version: 1.0\nGenerator: poetry-core 2.3.1\nTag: cp312-cp312-macosx_26_0_arm64\n"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (MACOS_WHEEL, ["macosx_26_0_arm64"]),
        (
            "Tag: cp39-abi3-macosx_10_9_x86_64.macosx_11_0_arm64\n",
            ["macosx_10_9_x86_64", "macosx_11_0_arm64"],
        ),
        ("Tag: py3-none-any\n", ["any"]),
        ("Tag: cp312-cp312-linux_x86_64\nTag: cp312-cp312-linux_x86_64\n", ["linux_x86_64"]),
        ("Wheel-Version: 1.0\n", []),
        ("Tag: broken\n", []),
        ("", []),
    ],
)
def test_platform_tags(text: str, expected: list[str]) -> None:
    assert wheel.platform_tags(text) == expected


@pytest.mark.parametrize(
    ("tag", "os_name", "arch", "os_version", "expected"),
    [
        # This host: a macOS 26 wheel on macOS 27 arm64 is the correct wheel.
        ("macosx_26_0_arm64", "macos", "arm64", (27, 0), True),
        ("macosx_26_0_arm64", "macos", "arm64", (26, 0), True),
        ("macosx_26_0_arm64", "macos", "arm64", (26, 4), True),
        ("macosx_11_0_arm64", "macos", "arm64", (26, 0), True),
        ("macosx_26_0_arm64", "macos", "arm64", (25, 4), False),
        ("macosx_27_0_arm64", "macos", "arm64", (26, 9), False),
        ("macosx_26_0_arm64", "macos", "x86_64", (27, 0), False),
        ("macosx_10_9_x86_64", "macos", "x86_64", (26, 0), True),
        ("macosx_11_0_universal2", "macos", "arm64", (26, 0), True),
        ("macosx_11_0_universal2", "macos", "x86_64", (26, 0), True),
        ("macosx_10_9_intel", "macos", "x86_64", (26, 0), True),
        ("macosx_10_9_ppc64", "macos", "x86_64", (26, 0), False),
        ("macosx_26_0_arm64", "linux", "arm64", (2, 35), False),
        ("macosx_arm64", "macos", "arm64", (27, 0), False),
        ("macosx_x_y_arm64", "macos", "arm64", (27, 0), False),
        # Linux: manylinux carries a glibc floor, and the host's glibc must clear it.
        ("manylinux_2_35_x86_64", "linux", "x86_64", (2, 39), True),
        ("manylinux_2_35_x86_64", "linux", "x86_64", (2, 35), True),
        ("manylinux_2_35_x86_64", "linux", "x86_64", (2, 31), False),
        ("manylinux_2_35_x86_64", "linux", "arm64", (2, 39), False),
        ("manylinux_2_35_aarch64", "linux", "arm64", (2, 39), True),
        ("manylinux2014_x86_64", "linux", "x86_64", (2, 17), True),
        ("manylinux2014_x86_64", "linux", "x86_64", (2, 12), False),
        ("manylinux2010_x86_64", "linux", "x86_64", (2, 35), True),
        ("manylinux1_x86_64", "linux", "x86_64", (2, 35), True),
        ("manylinux_2_x86_64", "linux", "x86_64", (2, 39), False),
        ("manylinux", "linux", "x86_64", (2, 39), False),
        ("manylinux_x_y_x86_64", "linux", "x86_64", (2, 39), False),
        ("linux_x86_64", "linux", "x86_64", (2, 39), True),
        ("linux_aarch64", "linux", "x86_64", (2, 39), False),
        ("manylinux_2_35_x86_64", "macos", "x86_64", (26, 0), False),
        # musl wheels do not fit a host that reports a glibc version.
        ("musllinux_1_2_x86_64", "linux", "x86_64", (2, 39), False),
        ("musllinux_1_2_x86_64", "linux", "x86_64", None, True),
        ("musllinux_1_2_x86_64", "linux", "arm64", None, False),
        # A version the host cannot report waives the version half of the rule.
        ("macosx_26_0_arm64", "macos", "arm64", None, True),
        ("manylinux_2_35_x86_64", "linux", "x86_64", None, True),
        # Anything else.
        ("any", "macos", "arm64", (27, 0), True),
        ("any", "linux", "x86_64", None, True),
        ("win_amd64", "macos", "arm64", (27, 0), False),
        ("", "macos", "arm64", (27, 0), False),
    ],
)
def test_tag_fits(
    tag: str,
    os_name: str,
    arch: str,
    os_version: tuple[int, int] | None,
    expected: bool,
) -> None:
    assert wheel.tag_fits(tag, os_name=os_name, arch=arch, os_version=os_version) is expected


def test_wheel_ok_on_this_host() -> None:
    compatible, detail = wheel.wheel_ok()
    assert compatible is True
    assert importlib.metadata.version("nautilus_trader") in detail
    assert host.arch() in detail


def test_wheel_ok_names_the_tag_of_the_installed_wheel() -> None:
    _, detail = wheel.wheel_ok()
    metadata = importlib.metadata.distribution("nautilus_trader").read_text("WHEEL") or ""
    for tag in wheel.platform_tags(metadata):
        if tag in detail:
            return
    assert wheel.platform_tags(metadata) == [], detail


class _Distribution:
    """The two members of a distribution the wheel check reads."""

    def __init__(self, version: str, wheel_text: str | None) -> None:
        self.version = version
        self._wheel_text = wheel_text

    def read_text(self, name: str) -> str | None:
        return self._wheel_text if name == "WHEEL" else None


def _install(monkeypatch: pytest.MonkeyPatch, distribution: object | Exception) -> None:
    def factory(_name: str) -> object:
        if isinstance(distribution, Exception):
            raise distribution
        return distribution

    monkeypatch.setattr(wheel.importlib.metadata, "distribution", factory)


def test_wheel_ok_when_the_engine_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, importlib.metadata.PackageNotFoundError("nautilus_trader"))
    compatible, detail = wheel.wheel_ok()
    assert compatible is False
    assert "not installed" in detail


def test_wheel_ok_without_a_platform_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Distribution("1.231.0", "Wheel-Version: 1.0\n"))
    compatible, detail = wheel.wheel_ok()
    assert compatible is True
    assert "no wheel platform tag" in detail


def test_wheel_ok_without_wheel_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Distribution("1.231.0", None))
    assert wheel.wheel_ok()[0] is True


class _UnreadableDistribution:
    """A distribution whose metadata directory cannot be read."""

    version = "1.231.0"

    def read_text(self, name: str) -> str | None:
        raise OSError(f"{name} is unreadable")


def test_wheel_ok_when_the_metadata_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _UnreadableDistribution())
    compatible, detail = wheel.wheel_ok()
    assert compatible is True
    assert "no wheel platform tag" in detail


def test_wheel_ok_rejects_a_foreign_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Distribution("1.231.0", "Tag: cp312-cp312-win_amd64\n"))
    compatible, detail = wheel.wheel_ok()
    assert compatible is False
    assert "win_amd64" in detail
    assert "does not fit" in detail


def test_wheel_ok_accepts_a_pure_python_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Distribution("1.231.0", "Tag: py3-none-any\n"))
    assert wheel.wheel_ok()[0] is True


def test_wheel_ok_takes_any_fitting_tag_of_a_compressed_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Distribution("1.231.0", "Tag: cp312-cp312-win_amd64.macosx_1_0_universal2.linux_x86_64\n"),
    )
    compatible, detail = wheel.wheel_ok()
    assert compatible is True
    assert "fits" in detail


def test_wheel_ok_says_when_the_version_half_is_waived(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wheel.host, "os_name", lambda *_: "linux")
    monkeypatch.setattr(wheel.host, "glibc_version", lambda: None)
    monkeypatch.setattr(wheel.host, "arch", lambda: "x86_64")
    _install(monkeypatch, _Distribution("1.231.0", "Tag: cp312-cp312-manylinux_2_35_x86_64\n"))
    compatible, detail = wheel.wheel_ok()
    assert compatible is True
    assert "architecture only" in detail
    assert "an unknown libc" in detail


def test_wheel_ok_describes_an_unsupported_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wheel.host, "os_name", lambda *_: "windows")
    _install(monkeypatch, _Distribution("1.231.0", "Tag: cp312-cp312-macosx_26_0_arm64\n"))
    compatible, detail = wheel.wheel_ok()
    assert compatible is False
    assert "windows" in detail
