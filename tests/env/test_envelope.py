"""Detection end to end: the file, the workspace inputs, and the failure modes."""

from __future__ import annotations

import importlib.metadata
import platform
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from kanso.env import Detected, Envelope, Plan, detect, plan_for, read, write
from kanso.env import envelope as module
from kanso.errors import Exit, ValidationError

from .conftest import FakeConfig, FakeEnv, FakeWorkspace

PORTFOLIO_LIVE = """\
schema: 1
stages:
  paper: {exec: sandbox, data: replay, strategies: []}
  live:  {exec: sandbox, data: replay, strategies: [{id: mr, version: 1}]}
"""

PORTFOLIO_EMPTY = """\
schema: 1
stages:
  paper: {exec: sandbox, data: replay, strategies: [{id: mr, version: 1}]}
  live:  {exec: sandbox, data: replay, strategies: []}
"""


def test_detect_without_a_workspace_describes_this_host() -> None:
    env = detect()
    assert env.schema_ == 1
    assert env.detected.python == platform.python_version()
    assert env.detected.nautilus_version == importlib.metadata.version("nautilus_trader")
    assert env.detected.os in {"macos", "linux", platform.system().lower()}
    assert env.detected.cores_total >= 1
    assert env.detected.disk_free_gb >= 0
    assert env.plan.lanes >= 1


def test_this_host_has_a_compatible_engine_wheel() -> None:
    assert detect().detected.nautilus_wheel_ok is True


def test_detected_at_is_an_offset_aware_timestamp() -> None:
    stamp = datetime.fromisoformat(detect().detected_at)
    assert stamp.tzinfo is not None


def test_detect_matches_the_pure_plan(workspace: FakeWorkspace) -> None:
    env = detect(workspace)
    assert env.plan == plan_for(env.detected)


def test_detect_reads_the_env_overrides(tmp_path: Path) -> None:
    ws = FakeWorkspace(
        root=tmp_path,
        config=FakeConfig(env=FakeEnv(reserved_cores=0, reserved_mem_gb=0, cores_per_lane=1)),
    )
    env = detect(ws)
    assert (env.plan.reserved_cores, env.plan.reserved_mem_gb) == (0, 0)
    assert env.plan.cores_per_lane == 1
    assert env.plan == plan_for(env.detected, reserved_cores=0, reserved_mem_gb=0, cores_per_lane=1)


def test_detect_reads_the_live_stage_and_the_recorded_baselines(tmp_path: Path) -> None:
    (tmp_path / "portfolio.yaml").write_text(PORTFOLIO_LIVE)
    _write_runs(tmp_path / "state.db", [1.0, 4.0, 2.0])
    env = detect(FakeWorkspace(root=tmp_path))
    assert env.plan.live_colocated is True
    assert env.plan.reserved_cores == 2
    assert env.plan.reserved_mem_gb == 8
    assert env.plan.mem_per_lane_gb == 6.0
    assert env.plan == plan_for(env.detected, live_colocated=True, baseline_peak_mem_gb=4.0)


def test_detect_tolerates_a_workspace_whose_config_has_no_env_table(tmp_path: Path) -> None:
    class Bare:
        root = tmp_path

    env = detect(Bare())  # type: ignore[arg-type]
    assert env.plan == plan_for(env.detected)


# --- the file ----------------------------------------------------------------------


def test_write_then_read_round_trips(workspace: FakeWorkspace) -> None:
    env = detect(workspace)
    path = write(workspace, env)
    assert path == workspace.root / "envelope.yaml"
    assert read(workspace) == env


def test_the_written_file_is_generated_yaml_with_the_documented_keys(
    workspace: FakeWorkspace,
) -> None:
    path = write(workspace, detect(workspace))
    text = path.read_text()
    assert text.startswith("# envelope.yaml")
    document = yaml.safe_load(text)
    assert set(document) == {"schema", "detected", "plan", "detected_at"}
    assert list(document["detected"]) == [
        "os",
        "os_version",
        "arch",
        "chip",
        "cores_perf",
        "cores_eff",
        "cores_total",
        "mem_gb",
        "disk_free_gb",
        "on_ac_power",
        "python",
        "nautilus_version",
        "nautilus_wheel_ok",
    ]
    assert list(document["plan"]) == [
        "live_colocated",
        "reserved_cores",
        "reserved_mem_gb",
        "cores_per_lane",
        "mem_per_lane_gb",
        "lanes",
    ]


def test_write_leaves_no_temporary_behind(workspace: FakeWorkspace) -> None:
    write(workspace, detect(workspace))
    write(workspace, detect(workspace))
    assert [p.name for p in workspace.root.iterdir()] == ["envelope.yaml"]


def test_read_without_a_file_is_nothing(workspace: FakeWorkspace) -> None:
    assert read(workspace) is None


@pytest.mark.parametrize(
    "text",
    [
        "schema: 1\ndetected: [\n",
        "- one\n- two\n",
        "schema: 1\n",
        "schema: 1\ndetected: {}\nplan: {}\ndetected_at: now\n",
        "",
    ],
)
def test_read_refuses_a_broken_envelope(workspace: FakeWorkspace, text: str) -> None:
    (workspace.root / "envelope.yaml").write_text(text)
    with pytest.raises(ValidationError) as caught:
        read(workspace)
    assert caught.value.code is Exit.VALIDATION
    assert caught.value.remedy == "re-run `kanso env detect`"


def test_read_refuses_an_envelope_with_unknown_keys(workspace: FakeWorkspace) -> None:
    env = detect(workspace)
    document = env.model_dump(by_alias=True)
    document["surprise"] = True
    (workspace.root / "envelope.yaml").write_text(yaml.safe_dump(document))
    with pytest.raises(ValidationError):
        read(workspace)


def test_read_reports_an_unreadable_file(workspace: FakeWorkspace) -> None:
    (workspace.root / "envelope.yaml").mkdir()
    with pytest.raises(ValidationError, match="cannot be read"):
        read(workspace)


# --- workspace inputs --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (PORTFOLIO_LIVE, True),
        (PORTFOLIO_EMPTY, False),
        ("stages:\n  live: {}\n", False),
        ("stages: []\n", False),
        ("just a string\n", False),
        ("stages: {live: {strategies: [\n", False),
        ("", False),
    ],
)
def test_live_colocated(tmp_path: Path, text: str, expected: bool) -> None:
    path = tmp_path / "portfolio.yaml"
    path.write_text(text)
    assert module.live_colocated(path) is expected


def test_live_colocated_without_a_file(tmp_path: Path) -> None:
    assert module.live_colocated(tmp_path / "portfolio.yaml") is False
    assert module.live_colocated(None) is False


def _write_runs(db: Path, peaks: list[float | None]) -> None:
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE runs (run_id TEXT, baseline_peak_mem_gb REAL)")
    connection.executemany(
        "INSERT INTO runs VALUES (?, ?)",
        [(f"r{index}", peak) for index, peak in enumerate(peaks)],
    )
    connection.commit()
    connection.close()


def test_max_baseline_peak_reads_the_largest_recorded(tmp_path: Path) -> None:
    _write_runs(tmp_path / "state.db", [0.5, 3.25, 1.0])
    assert module.max_baseline_peak_mem_gb(tmp_path / "state.db") == 3.25


@pytest.mark.parametrize("peaks", [[], [None, None], [0.0], [-1.0]])
def test_max_baseline_peak_without_a_usable_value(
    tmp_path: Path, peaks: list[float | None]
) -> None:
    _write_runs(tmp_path / "state.db", peaks)
    assert module.max_baseline_peak_mem_gb(tmp_path / "state.db") is None


def test_max_baseline_peak_without_a_database(tmp_path: Path) -> None:
    assert module.max_baseline_peak_mem_gb(tmp_path / "state.db") is None
    assert module.max_baseline_peak_mem_gb(None) is None


def test_max_baseline_peak_before_the_migration_that_creates_runs(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    sqlite3.connect(db).close()
    assert module.max_baseline_peak_mem_gb(db) is None


def test_max_baseline_peak_of_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    db.write_text("not a database")
    assert module.max_baseline_peak_mem_gb(db) is None


def test_max_baseline_peak_ignores_a_non_numeric_column(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE runs (baseline_peak_mem_gb TEXT)")
    connection.execute("INSERT INTO runs VALUES ('lots')")
    connection.commit()
    connection.close()
    assert module.max_baseline_peak_mem_gb(db) is None


def test_disk_free_gb(tmp_path: Path) -> None:
    assert module.disk_free_gb(tmp_path) >= 0
    assert module.disk_free_gb(tmp_path / "absent") == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2, 2),
        (2.9, 2),
        (True, None),
        (False, None),
        (None, None),
        ("2", None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_a_whole_number_override(value: object, expected: int | None) -> None:
    assert module._as_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2, 2.0), (2.5, 2.5), (True, None), (None, None), ("2", None), (float("nan"), None)],
)
def test_a_fractional_override(value: object, expected: float | None) -> None:
    assert module._as_number(value) == expected


def test_a_fractional_memory_reservation_survives_into_the_plan(tmp_path: Path) -> None:
    ws = FakeWorkspace(root=tmp_path, config=FakeConfig(env=FakeEnv(reserved_mem_gb=2.5)))
    env = detect(ws)
    assert env.plan.reserved_mem_gb == 2.5
    assert env.plan == plan_for(env.detected, reserved_mem_gb=2.5)


# --- the file as a schema ----------------------------------------------------------


@given(
    cores_perf=st.integers(min_value=1, max_value=256),
    cores_eff=st.integers(min_value=0, max_value=256),
    mem_gb=st.floats(min_value=0.5, max_value=4096.0, allow_nan=False),
    disk_free_gb=st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False),
    on_ac_power=st.booleans(),
    wheel_ok=st.booleans(),
    text=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=40
    ),
    lanes=st.integers(min_value=1, max_value=128),
    mem_per_lane_gb=st.floats(min_value=4.0, max_value=512.0, allow_nan=False),
)
def test_any_envelope_survives_being_written_and_read_back(
    tmp_path_factory: pytest.TempPathFactory,
    cores_perf: int,
    cores_eff: int,
    mem_gb: float,
    disk_free_gb: float,
    on_ac_power: bool,
    wheel_ok: bool,
    text: str,
    lanes: int,
    mem_per_lane_gb: float,
) -> None:
    env = Envelope(
        detected=Detected(
            os=text,
            os_version=text,
            arch=text,
            chip=text,
            cores_perf=cores_perf,
            cores_eff=cores_eff,
            cores_total=cores_perf + cores_eff,
            mem_gb=mem_gb,
            disk_free_gb=disk_free_gb,
            on_ac_power=on_ac_power,
            python=text,
            nautilus_version=text,
            nautilus_wheel_ok=wheel_ok,
        ),
        plan=Plan(
            live_colocated=on_ac_power,
            reserved_cores=1,
            reserved_mem_gb=4.0,
            cores_per_lane=2,
            mem_per_lane_gb=mem_per_lane_gb,
            lanes=lanes,
        ),
        detected_at=text,
    )
    ws = FakeWorkspace(root=tmp_path_factory.mktemp("workspace"))
    write(ws, env)
    assert read(ws) == env


def test_the_schema_key_is_named_schema_in_the_file_and_schema_underscore_in_python() -> None:
    env = Envelope.model_validate(
        {
            "schema": 1,
            "detected": detect().detected.model_dump(),
            "plan": plan_for(detect().detected).model_dump(),
            "detected_at": "2026-09-04T15:00:00+10:00",
        }
    )
    assert env.schema_ == 1
    assert env.model_dump(by_alias=True)["schema"] == 1
