"""The lane formula, against hand-computed cases."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.env import Detected, Plan, plan_for


def detected(cores_total: int, mem_gb: int) -> Detected:
    """A host with only the two fields the formula reads varying."""
    return Detected(
        os="linux",
        os_version="22.04",
        arch="x86_64",
        chip="test",
        cores_perf=cores_total,
        cores_eff=0,
        cores_total=cores_total,
        mem_gb=mem_gb,
        disk_free_gb=100,
        on_ac_power=True,
        python="3.12.13",
        nautilus_version="1.231.0",
        nautilus_wheel_ok=True,
    )


def test_defaults_reserve_one_core_and_four_gigabytes() -> None:
    plan = plan_for(detected(16, 64))
    assert plan.live_colocated is False
    assert plan.reserved_cores == 1
    assert plan.reserved_mem_gb == 4
    assert plan.cores_per_lane == 2
    assert plan.mem_per_lane_gb == 4.0
    # min((16 - 1) // 2, (64 - 4) // 4) = min(7, 15) = 7
    assert plan.lanes == 7


def test_the_core_bound_branch() -> None:
    # min((16 - 1) // 2, (64 - 4) // 4) = min(7, 15): cores bind.
    assert plan_for(detected(16, 64)).lanes == 7


def test_the_memory_bound_branch() -> None:
    # min((64 - 1) // 2, (16 - 4) // 4) = min(31, 3): memory binds.
    assert plan_for(detected(64, 16)).lanes == 3


def test_a_host_too_small_for_the_formula_still_gets_one_lane() -> None:
    # min((2 - 1) // 2, (4 - 4) // 4) = min(0, 0) = 0, floored to one lane.
    assert plan_for(detected(2, 4)).lanes == 1


def test_a_host_smaller_than_the_reservation_still_gets_one_lane() -> None:
    # min((1 - 1) // 2, floor((2 - 4) / 4)) = min(0, -1) = -1, floored to one lane.
    assert plan_for(detected(1, 2)).lanes == 1


def test_colocating_the_live_stage_reserves_more() -> None:
    alone = plan_for(detected(16, 16))
    colocated = plan_for(detected(16, 16), live_colocated=True)
    assert (alone.reserved_cores, alone.reserved_mem_gb) == (1, 4)
    assert (colocated.reserved_cores, colocated.reserved_mem_gb) == (2, 8)
    # min((16 - 1) // 2, (16 - 4) // 4) = min(7, 3) against min(7, (16 - 8) // 4) = min(7, 2)
    assert alone.lanes == 3
    assert colocated.lanes == 2
    assert colocated.live_colocated is True


def test_a_measured_baseline_peak_widens_each_lane() -> None:
    plan = plan_for(detected(32, 64), baseline_peak_mem_gb=6.0)
    # 1.5 x 6 = 9 GB per lane; min((32 - 1) // 2, floor(60 / 9)) = min(15, 6) = 6
    assert plan.mem_per_lane_gb == 9.0
    assert plan.lanes == 6


def test_a_small_baseline_peak_keeps_the_four_gigabyte_floor() -> None:
    plan = plan_for(detected(32, 64), baseline_peak_mem_gb=2.0)
    assert plan.mem_per_lane_gb == 4.0
    assert plan.lanes == 15


def test_the_written_memory_per_lane_reproduces_the_lane_count() -> None:
    plan = plan_for(detected(64, 64), baseline_peak_mem_gb=2.9)
    assert plan.mem_per_lane_gb == 4.35
    # min((64 - 1) // 2, floor(60 / 4.35)) = min(31, 13) = 13
    assert plan.lanes == 13
    assert plan.lanes == max(1, min(31, math.floor(60 / plan.mem_per_lane_gb)))


def test_overrides_replace_every_default() -> None:
    plan = plan_for(
        detected(32, 64),
        reserved_cores=4,
        reserved_mem_gb=16,
        cores_per_lane=4,
    )
    assert (plan.reserved_cores, plan.reserved_mem_gb, plan.cores_per_lane) == (4, 16, 4)
    # min((32 - 4) // 4, (64 - 16) // 4) = min(7, 12) = 7
    assert plan.lanes == 7


def test_overrides_apply_to_a_colocated_host_too() -> None:
    plan = plan_for(detected(32, 64), live_colocated=True, reserved_cores=0)
    assert plan.reserved_cores == 0
    assert plan.reserved_mem_gb == 8


@pytest.mark.parametrize("cores_per_lane", [0, -1])
def test_a_lane_always_gets_at_least_one_core(cores_per_lane: int) -> None:
    plan = plan_for(detected(8, 64), cores_per_lane=cores_per_lane)
    assert plan.cores_per_lane == 1
    assert plan.lanes == 7


def test_a_negative_reservation_is_clamped_to_zero() -> None:
    plan = plan_for(detected(8, 16), reserved_cores=-5, reserved_mem_gb=-5)
    assert (plan.reserved_cores, plan.reserved_mem_gb) == (0, 0)
    assert plan.lanes == 4


@given(
    cores_total=st.integers(min_value=1, max_value=1024),
    mem_gb=st.integers(min_value=1, max_value=4096),
    live_colocated=st.booleans(),
    reserved_cores=st.one_of(st.none(), st.integers(min_value=-4, max_value=64)),
    reserved_mem_gb=st.one_of(st.none(), st.integers(min_value=-4, max_value=256)),
    cores_per_lane=st.one_of(st.none(), st.integers(min_value=-4, max_value=64)),
    baseline_peak_mem_gb=st.one_of(
        st.none(), st.floats(min_value=0.01, max_value=512, allow_nan=False)
    ),
)
def test_every_plan_is_usable(
    cores_total: int,
    mem_gb: int,
    live_colocated: bool,
    reserved_cores: int | None,
    reserved_mem_gb: int | None,
    cores_per_lane: int | None,
    baseline_peak_mem_gb: float | None,
) -> None:
    plan = plan_for(
        detected(cores_total, mem_gb),
        live_colocated=live_colocated,
        reserved_cores=reserved_cores,
        reserved_mem_gb=reserved_mem_gb,
        cores_per_lane=cores_per_lane,
        baseline_peak_mem_gb=baseline_peak_mem_gb,
    )
    assert isinstance(plan, Plan)
    assert plan.lanes >= 1
    assert plan.cores_per_lane >= 1
    assert plan.mem_per_lane_gb >= 4.0
    assert plan.reserved_cores >= 0
    assert plan.reserved_mem_gb >= 0
    assert plan.live_colocated is live_colocated


@given(
    cores_total=st.integers(min_value=1, max_value=1024),
    mem_gb=st.integers(min_value=1, max_value=4096),
)
def test_the_plan_never_exceeds_what_the_machine_holds(cores_total: int, mem_gb: int) -> None:
    host = detected(cores_total, mem_gb)
    plan = plan_for(host)
    fits_cores = plan.lanes * plan.cores_per_lane + plan.reserved_cores <= host.cores_total
    fits_memory = plan.lanes * plan.mem_per_lane_gb + plan.reserved_mem_gb <= host.mem_gb
    # Exactly one lane is the floor, and a machine may be too small even for that.
    assert (fits_cores and fits_memory) or plan.lanes == 1
