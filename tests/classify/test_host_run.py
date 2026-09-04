"""The host-alone run a relative objective is differenced against."""

from __future__ import annotations

import pytest

from kanso.classify import get, run_key
from kanso.classify.construct import HostRef
from kanso.errors import PreconditionError
from tests.classify.conftest import strategy

RELATIVE = ("filter", "overlay", "exit")


def host(version: int = 1) -> HostRef:
    return HostRef("demo_sleeve", version, strategy().versions[0].sleeve)


class Runs:
    """A stand-in for the runner's host-alone backtest, counting what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[HostRef] = []

    def __call__(self, ref: HostRef) -> str:
        self.calls.append(ref)
        return f"run of {ref.strategy_id}@{ref.version}"


@pytest.mark.parametrize("construct_id", RELATIVE)
def test_the_host_run_is_computed_once_per_snapshot_and_host_version(construct_id: str) -> None:
    construct = get(construct_id)
    compute, cache = Runs(), {}
    first = construct.host_run(host(), "snap1", compute, cache)
    again = construct.host_run(host(), "snap1", compute, cache)
    assert first == again == "run of demo_sleeve@1"
    assert len(compute.calls) == 1


def test_a_new_host_version_or_a_new_snapshot_is_a_new_run() -> None:
    construct = get("filter")
    compute, cache = Runs(), {}
    construct.host_run(host(1), "snap1", compute, cache)
    construct.host_run(host(2), "snap1", compute, cache)
    construct.host_run(host(1), "snap2", compute, cache)
    assert len(compute.calls) == 3
    assert sorted(cache) == ["snap1:demo_sleeve@1", "snap1:demo_sleeve@2", "snap2:demo_sleeve@1"]


def test_without_a_cache_every_call_computes() -> None:
    compute = Runs()
    assert get("exit").host_run(host(), "snap1", compute) == "run of demo_sleeve@1"
    get("exit").host_run(host(), "snap1", compute)
    assert len(compute.calls) == 2


def test_the_key_names_the_data_and_the_host_version() -> None:
    assert run_key(host(3), "snap9") == "snap9:demo_sleeve@3"


def test_an_absolute_objective_has_no_host_run_to_difference_against() -> None:
    with pytest.raises(PreconditionError, match="no host run to difference against"):
        get("sleeve").host_run(host(), "snap1", Runs())


@pytest.mark.parametrize("construct_id", ["alpha", "execution", "allocation"])
def test_a_non_runnable_construct_has_no_host_run(construct_id: str) -> None:
    with pytest.raises(PreconditionError, match="classifiable but not runnable"):
        get(construct_id).host_run(host(), "snap1", Runs())
