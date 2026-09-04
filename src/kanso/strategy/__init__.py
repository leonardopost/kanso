"""Strategies: what a certificate composes into, and the one implementation it runs.

A hypothesis that survives its embargo becomes a version of a strategy. The construct
decides the shape — a sleeve is a new strategy at version 1, an attached construct is its
host's next version — and composition writes it: `strategies/<id>/strategy.yaml` lists the
versions, `strategies/<id>/impl/<version>/` holds a verbatim copy of every certified
source beside a manifest naming the classes and the configuration each is built with.

That directory is the whole of what a stage loads. A backtest, a replay and a live node
resolve the same `module:Class` pairs out of the same files, so the thing that was
certified, the thing that is measured on paper and the thing that trades cannot be three
different programs.
"""

from __future__ import annotations

from kanso.strategy.composition import (
    COMPOSED,
    REPLICATIONS,
    compose,
    expectation,
    strategy_id_of,
)
from kanso.strategy.files import (
    IMPL,
    STRATEGIES,
    STRATEGY_FILE,
    impl_dir,
    read,
    require,
    strategies,
    strategy_dir,
    strategy_file,
)
from kanso.strategy.impl import (
    MANIFEST_FILE,
    Built,
    Component,
    ImplManifest,
    Loaded,
    load,
    manifest_file,
    read_manifest,
    sources,
)

__all__ = [
    "COMPOSED",
    "IMPL",
    "MANIFEST_FILE",
    "REPLICATIONS",
    "STRATEGIES",
    "STRATEGY_FILE",
    "Built",
    "Component",
    "ImplManifest",
    "Loaded",
    "compose",
    "expectation",
    "impl_dir",
    "load",
    "manifest_file",
    "read",
    "read_manifest",
    "require",
    "sources",
    "strategies",
    "strategy_dir",
    "strategy_file",
    "strategy_id_of",
]
