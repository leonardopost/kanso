"""Which execution clients a workspace has, and what each of them declares.

An execution client is named in `portfolio.yaml` by an id, and the id is all the file
carries. What matters about it is the pair of declarations behind the id: how its capital is
funded — simulated, a broker's paper account, or real money — and which clock it runs on.
Those two facts are what forbid real money off the live stage, what forbid a historical
replay feeding a broker matching against current prices, and what make a promotion the only
way a version reaches real capital.

kanso ships exactly one client, the simulated `sandbox`. Every other one belongs to a broker
adapter, which declares it in an `EXEC_CLIENTS` table on its extension module, in the same
way a construct or a gate is declared. The core therefore knows no broker, and a workspace
with no adapter installed still has a stage it can deploy to — which is what makes the suite,
`doctor` and the demo green with every vendor credential unset.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from kanso import ext
from kanso.errors import PreconditionError
from kanso.schemas import SANDBOX, ExecutionClientSpec

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace

__all__ = ["EXEC_CLIENTS_ATTR", "builtin", "get", "registry"]

EXEC_CLIENTS_ATTR: Final = "EXEC_CLIENTS"
"""The module attribute an extension declares its execution clients in."""


def builtin() -> dict[str, ExecutionClientSpec]:
    """The clients the framework itself provides: the simulated venue, and nothing else."""
    return {SANDBOX.id: SANDBOX}


def registry(ws: Workspace | None = None) -> dict[str, ExecutionClientSpec]:
    """Every execution client this workspace can name, by id.

    An extension whose declaration is unusable is skipped rather than raised: a broken
    adapter degrades a workspace to the clients that do load, and `deploy` then refuses the
    id that is missing with a message naming what is there.
    """
    found = builtin()
    if ws is None:
        return found
    for extension in ext.discover(ws.root, ws.config.extensions_paths):
        if not extension.ok:
            continue
        for spec in _declared(getattr(extension.module, EXEC_CLIENTS_ATTR, None)):
            found.setdefault(spec.id, spec)
    return found


def get(client_id: str, ws: Workspace | None = None) -> ExecutionClientSpec:
    """One execution client's declarations, refusing an id nothing provides."""
    known = registry(ws)
    spec = known.get(client_id)
    if spec is None:
        raise PreconditionError(
            f"exec: {client_id!r} is not an execution client of this workspace, which has "
            f"{', '.join(sorted(known))}",
            remedy="install the broker adapter that provides it, or use `sandbox`",
        )
    return spec


def _declared(value: object) -> tuple[ExecutionClientSpec, ...]:
    """The specifications an extension's table holds, ignoring anything else it says."""
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        return ()
    return tuple(spec for spec in value if isinstance(spec, ExecutionClientSpec))
