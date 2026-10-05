"""The session-sign-flip max-T null: deterministic, family-wise, and honest about thin cells."""

from __future__ import annotations

import numpy as np
import pytest

from kanso.screen import nulls


def test_the_seed_is_the_pins_and_nothing_else() -> None:
    first = nulls.seed("a" * 64, "b" * 64, 0)
    assert first == nulls.seed("a" * 64, "b" * 64, 0)
    assert len({first, nulls.seed("c" * 64, "b" * 64, 0), nulls.seed("a" * 64, "c" * 64, 0)}) == 3
    assert nulls.seed("a" * 64, "b" * 64, 1) != first


def test_the_same_pins_give_the_same_evidence_to_the_bit() -> None:
    values = np.random.default_rng(1).normal(size=(5, 40))
    one = nulls.evidence(values, 999, 7)
    two = nulls.evidence(values.copy(), 999, 7)
    for field in ("mean", "se", "t", "sessions", "p"):
        assert getattr(one, field).tobytes() == getattr(two, field).tobytes()


def test_mean_se_and_t_are_across_sessions() -> None:
    values = np.asarray([[1.0, 2.0, 3.0, np.nan]])
    found = nulls.evidence(values, 99, 1)
    assert found.sessions.tolist() == [3]
    assert found.mean.tolist() == pytest.approx([2.0])
    assert found.se.tolist() == pytest.approx([1.0 / np.sqrt(3.0)])
    assert found.t.tolist() == pytest.approx([2.0 * np.sqrt(3.0)])


def test_a_planted_effect_ranks_above_every_draw() -> None:
    rng = np.random.default_rng(2)
    values = rng.normal(size=(20, 60))
    values[3] += 1.0
    found = nulls.evidence(values, 999, 3)
    assert found.p[3] == pytest.approx(1 / 1000)
    assert all(0 < p <= 1 for p in found.p)


def test_with_no_effect_the_family_reads_significant_at_most_as_often_as_alpha() -> None:
    hits = 0
    trials = 200
    for trial in range(trials):
        values = np.random.default_rng(100 + trial).normal(size=(10, 30))
        hits += int(np.min(nulls.evidence(values, 199, trial).p) <= 0.05)
    # The family-wise rate is at most alpha in expectation; three standard errors of a
    # binomial at 0.05 over 200 trials is about 0.046.
    assert hits / trials <= 0.05 + 0.046


def test_a_cell_with_nothing_to_judge_by_reads_nothing() -> None:
    values = np.asarray([[np.nan, 0.4, np.nan], [0.2, 0.2, 0.2], [0.0, 0.0, 0.0]])
    found = nulls.evidence(values, 99, 1)
    assert found.t.tolist() == [0.0, 0.0, 0.0]
    assert found.p.tolist() == [1.0, 1.0, 1.0]
    assert found.se.tolist() == [0.0, 0.0, 0.0]


def test_a_measure_with_no_session_reads_nothing() -> None:
    found = nulls.evidence(np.zeros((2, 0)), 99, 1)
    assert found.sessions.tolist() == [0, 0]
    assert found.p.tolist() == [1.0, 1.0]
