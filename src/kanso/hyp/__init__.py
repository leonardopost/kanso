"""Hypotheses: scaffolding one, deciding whether it is admissible, and registering it.

A hypothesis is a file the operator writes and kanso pins. `scaffold` renders the three
files a run is scoped to; `validate` decides whether the file is admissible, which is the
whole of what a workspace can check about one; `add` registers or re-pins it, `show`
reports what is registered, `retire` ends its life and `resume` undoes that. Status, pins
and the hypothesis's best card live in the state store, never in the file, so the file's
bytes stay stable enough to be the content address every later record refers to.
"""

from __future__ import annotations

from kanso.hyp.registry import (
    Registration,
    Status,
    active_run,
    add,
    host_resolution,
    host_sizing,
    hypothesis_of,
    pin,
    refuse_active_run,
    resume,
    retire,
    set_status,
    show,
)
from kanso.hyp.scaffold import (
    HYPOTHESES,
    HYPOTHESIS_FILE,
    PROGRAM_FILE,
    STRATEGY_FILE,
    check_id,
    hypothesis_dir,
    hypothesis_file,
    scaffold,
    stub,
)
from kanso.hyp.validate import read_source, validate, venue_models

__all__ = [
    "HYPOTHESES",
    "HYPOTHESIS_FILE",
    "PROGRAM_FILE",
    "STRATEGY_FILE",
    "Registration",
    "Status",
    "active_run",
    "add",
    "check_id",
    "host_resolution",
    "host_sizing",
    "hypothesis_dir",
    "hypothesis_file",
    "hypothesis_of",
    "pin",
    "read_source",
    "refuse_active_run",
    "resume",
    "retire",
    "scaffold",
    "set_status",
    "show",
    "stub",
    "validate",
    "venue_models",
]
