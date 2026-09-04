"""Deterministic, deadline-free property testing for the schema suite."""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "schemas",
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile("schemas")
