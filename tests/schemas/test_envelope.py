"""`envelope.yaml`: the detected machine and its concurrency plan."""

from __future__ import annotations

from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import Envelope

BASE: dict[str, Any] = {
    "schema": 1,
    "detected": {
        "os": "macos",
        "os_version": "26.0",
        "arch": "arm64",
        "chip": "Apple M4 Max",
        "cores_perf": 12,
        "cores_eff": 4,
        "cores_total": 16,
        "mem_gb": 64,
        "disk_free_gb": 412,
        "on_ac_power": True,
        "python": "3.13.5",
        "nautilus_version": "1.231.0",
        "nautilus_wheel_ok": True,
    },
    "plan": {
        "live_colocated": False,
        "reserved_cores": 1,
        "reserved_mem_gb": 4,
        "cores_per_lane": 2,
        "mem_per_lane_gb": 4.0,
        "lanes": 7,
    },
    "detected_at": "2026-09-03T10:00:00+10:00",
}


def test_the_template_is_valid() -> None:
    envelope = Envelope.model_validate(BASE)
    assert envelope.plan.lanes == 7
    assert envelope.detected.cores_total == 16


def test_a_machine_with_no_split_cores_is_fine() -> None:
    detected = {**BASE["detected"], "cores_perf": 0, "cores_eff": 0, "chip": ""}
    assert Envelope.model_validate({**BASE, "detected": detected}).detected.chip == ""


def test_at_least_one_core_and_one_lane() -> None:
    with pytest.raises(ValidationError, match="cores_total"):
        Envelope.model_validate({**BASE, "detected": {**BASE["detected"], "cores_total": 0}})
    with pytest.raises(ValidationError, match="lanes"):
        Envelope.model_validate({**BASE, "plan": {**BASE["plan"], "lanes": 0}})


def test_the_file_is_generated_so_unknown_keys_are_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Envelope.model_validate({**BASE, "notes": "hand edited"})


def test_an_unquoted_timestamp_is_kept_as_text() -> None:
    from datetime import UTC, datetime

    envelope = Envelope.model_validate(
        {**BASE, "detected_at": datetime(2026, 9, 3, 10, tzinfo=UTC)}
    )
    assert envelope.detected_at == "2026-09-03T10:00:00+00:00"
