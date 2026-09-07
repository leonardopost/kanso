"""The wire encoding of a parameter map, for every test that scripts a model answer.

`classify` and `certify_plan` answer with their parameters as a list of `{name, value}`
pairs, because a provider constraining an answer to a schema refuses the free-form object
the map wants to be — `kanso.models.tasks.PARAM_PAIRS` records what each candidate shape
was measured to cost. A fixture states the mapping the model decided and encodes it here,
so a scripted answer reads as the decision rather than as the transport.

Nothing inland takes this shape: the router collapses the pairs before the answer reaches
a step, so a hypothesis document, a written plan and a gate context all still hold
mappings and the fixtures for those are mappings too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def pairs(params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """`params` as the list of pairs a model answers with; the empty map is an empty list."""
    return [{"name": name, "value": value} for name, value in (params or {}).items()]
