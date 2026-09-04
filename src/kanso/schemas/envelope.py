"""`envelope.yaml`: the detected machine and the concurrency plan derived from it.

The file is generated; the operator's only say over it is the `[env]` overrides that feed
the plan. It is a schema here so that the detector, `doctor` and the scheduler agree on
what a machine is, and so that a stale or hand-edited file is refused rather than believed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field, field_validator

from kanso.schemas.base import KansoModel, NonEmpty, Versioned


class Detected(KansoModel):
    """What the host reported: cores, memory, power, and the versions in play."""

    os: NonEmpty
    os_version: str
    arch: NonEmpty
    chip: str
    cores_perf: int = Field(ge=0)
    cores_eff: int = Field(ge=0)
    cores_total: int = Field(ge=1)
    mem_gb: float = Field(gt=0)
    disk_free_gb: float = Field(ge=0)
    on_ac_power: bool
    python: NonEmpty
    nautilus_version: NonEmpty
    nautilus_wheel_ok: bool


class Plan(KansoModel):
    """How many lanes the machine can run, and what is held back from them."""

    live_colocated: bool
    reserved_cores: int = Field(ge=0)
    reserved_mem_gb: float = Field(ge=0)
    cores_per_lane: int = Field(ge=1)
    mem_per_lane_gb: float = Field(gt=0)
    lanes: int = Field(ge=1)


class Envelope(Versioned):
    """The machine envelope: research refuses to begin without one."""

    detected: Detected
    plan: Plan
    detected_at: NonEmpty

    @field_validator("detected_at", mode="before")
    @classmethod
    def _as_written(cls, value: Any) -> Any:
        """A YAML loader turns an unquoted timestamp into a datetime; keep the text."""
        return value.isoformat() if isinstance(value, date | datetime) else value
