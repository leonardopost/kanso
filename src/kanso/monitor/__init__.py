"""Monitoring: the pass that watches every deployed version, and what it is allowed to do.

The research loop ends at a certificate. Everything after it — a version on paper, a version
live, a stage carrying money — is watched by one function run on a timer, and this package is
that function and the two things it reads.

`realised` joins the windows a stage has closed for one version into the single run its gates
are measured on: a node flattens before every stop, so a version's life on a stage is a
sequence of windows rather than one. `stage` is what the pass hands each gate — the clock, the
tenure, the day's book and the kind of money the stage's execution client spends — because a
run says nothing about the deployment that produced it. `run_once` is the pass itself: gates
in, promotions, demotions and halts out.

Nothing here decides a threshold. Which gates run on a stage, and with what values, was decided
once by the planner for the sleeve hypothesis, and this pass applies that decision to a stage
instead of to a backtest.
"""

from __future__ import annotations

from kanso.monitor.realised import Tenure, combined, tenure
from kanso.monitor.run import (
    DAILY_LOSS,
    DEMOTED,
    DEPLOY_BLOCKED,
    HALTED,
    PROMOTABLE,
    Exposure,
    Outcome,
    halt,
    mark,
    paper_window_s,
    run_once,
)
from kanso.monitor.stage import SIMULATED, UNKNOWN, StageRecord

__all__ = [
    "DAILY_LOSS",
    "DEMOTED",
    "DEPLOY_BLOCKED",
    "HALTED",
    "PROMOTABLE",
    "SIMULATED",
    "UNKNOWN",
    "Exposure",
    "Outcome",
    "StageRecord",
    "Tenure",
    "combined",
    "halt",
    "mark",
    "paper_window_s",
    "run_once",
    "tenure",
]
