"""The portfolio: which versions are on which stage, with what money, and running what.

A stage is a node. `deploy` admits what composition produced, funds it by the capital rule,
validates what the stage's execution client declares and restarts the node; `promote` and
`demote` move a version between the two stages, the first only under a named operator's
recorded approval, and `retire` ends a version's life; `show` reports the file, whether each
node is current and what every deployed version has realised. `on_certified` is the pair of
acts a passing certificate triggers by itself: compose, then take the paper stage.

Two rules govern everything here. A version reaches real capital only through an approval
recorded against that exact version, so editing `portfolio.yaml` by hand can never move
money. And a node flattens before every stop, so a stage always restarts flat and each
redeploy realises its window into the record the paper and live gates read.
"""

from __future__ import annotations

from kanso.portfolio.capital import assign, ceiling
from kanso.portfolio.clients import get as exec_client
from kanso.portfolio.clients import registry as exec_clients
from kanso.portfolio.deploy import (
    BLOCKED,
    LIVE,
    PAPER,
    RETIRED,
    Admitted,
    Deployment,
    clock_of,
    deploy,
)
from kanso.portfolio.files import PORTFOLIO_FILE, halt, portfolio_file, stage_of
from kanso.portfolio.files import read as read_portfolio
from kanso.portfolio.files import write as write_portfolio
from kanso.portfolio.lifecycle import Adoption, on_certified
from kanso.portfolio.promote import (
    PROMOTABLE,
    Demotion,
    Promotion,
    Retirement,
    demote,
    promote,
    retire,
    set_state,
)
from kanso.portfolio.records import (
    STAGE_RUN,
    Approval,
    StageResult,
    approvals,
    approve,
    approved,
    stage_results,
    subject_of,
)
from kanso.portfolio.show import Deployed, Report, StageReport, show

__all__ = [
    "BLOCKED",
    "LIVE",
    "PAPER",
    "PORTFOLIO_FILE",
    "PROMOTABLE",
    "RETIRED",
    "STAGE_RUN",
    "Admitted",
    "Adoption",
    "Approval",
    "Deployed",
    "Deployment",
    "Demotion",
    "Promotion",
    "Report",
    "Retirement",
    "StageReport",
    "StageResult",
    "approvals",
    "approve",
    "approved",
    "assign",
    "ceiling",
    "clock_of",
    "demote",
    "deploy",
    "exec_client",
    "exec_clients",
    "halt",
    "on_certified",
    "portfolio_file",
    "promote",
    "read_portfolio",
    "retire",
    "set_state",
    "stage_of",
    "stage_results",
    "show",
    "subject_of",
    "write_portfolio",
]
