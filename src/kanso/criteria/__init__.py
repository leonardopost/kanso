"""The criteria toolbox: what kanso can measure, and what counts as proof.

Two kinds of thing live here. An **objective** is the one scalar a run optimises, chosen
for a hypothesis by a deterministic predicate rather than by an agent, and computed
fold-wise so that every metric arrives with the standard error the keep rule must clear. A
**gate** is a test with a verdict and its evidence, carrying no default and no threshold:
which gates run, and with which values inside the declared ranges, is decided at runtime —
by the classifier for the card stage, by the planner for certification, paper and live.

Both are declared as YAML in `library/`, and that catalogue is what the agents read. The
framework's own opinions are only the structural invariants: the objective set is total,
so classification always has one to write; a plan names real gates at their real stages
with values inside their ranges, includes every required gate and reaches all three
stages; and a gate that cannot reach its context passes while saying what it did not have,
so a certificate's verdict is the conjunction of the tests that actually ran.

Everything is evaluated on a `CardRun`: one window's return series, equity curve, closed
trades and fills, extracted once by the runner with costs already applied, and cut into
contiguous equal calendar folds on demand.
"""

from __future__ import annotations

from kanso.criteria.context import (
    DatasetFacts,
    DeployedBook,
    Gate,
    GateContext,
)
from kanso.criteria.integrity import (
    ALLOWED_IMPORTS,
    DENIED_ATTRIBUTES,
    DENIED_BUILTINS,
    DENIED_IDENTIFIERS,
    SCOPED_FILES,
)
from kanso.criteria.library import (
    applicable_objectives,
    catalogue,
    check_params,
    criteria_version,
    gates,
    objectives,
    resolve_bound,
    validate_plan,
)
from kanso.criteria.objectives import SHARPE_FAMILY, Objective
from kanso.criteria.quantities import (
    drawdown_pct,
    edge_bps,
    periods_per_year,
    sharpe,
    standard_error,
)
from kanso.criteria.run import CardRun, Fill, Trade

__all__ = [
    "ALLOWED_IMPORTS",
    "DENIED_ATTRIBUTES",
    "DENIED_BUILTINS",
    "DENIED_IDENTIFIERS",
    "SCOPED_FILES",
    "SHARPE_FAMILY",
    "CardRun",
    "DatasetFacts",
    "DeployedBook",
    "Fill",
    "Gate",
    "GateContext",
    "Objective",
    "Trade",
    "applicable_objectives",
    "catalogue",
    "check_params",
    "criteria_version",
    "drawdown_pct",
    "edge_bps",
    "gates",
    "objectives",
    "periods_per_year",
    "resolve_bound",
    "sharpe",
    "standard_error",
    "validate_plan",
]
