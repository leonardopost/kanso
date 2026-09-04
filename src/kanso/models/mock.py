"""The `mock` protocol: scripted answers, no network, no cost.

This is the fixture the suite and the demo are built on, and the reason a workspace with
every provider credential unset still classifies, proposes, checks alignment and plans. It
reads a YAML file of answers keyed by task class and hands them out in order.

The cursor is per task class and per process, and it advances on **every** call, including
the router's retry and its escalation. That is deliberate: a script that rewound on a
retry could never express "this attempt fails and the next one succeeds", which is the one
behaviour a scripted fixture most needs to express. Running off the end wraps to the start,
so a script of three answers drives a run of thirty cards without the fixture growing.

A task class the script does not list answers `{}`. An empty object satisfies no task
class's schema, so an unlisted class exercises the whole ladder — retry, escalate, refuse
— without the script having to spell a malformed answer out.

The cursor lives for the life of the process. Lanes are separate processes, so two lanes
reading the same script each walk it from the start, independently.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Final

import yaml

from kanso.errors import PreconditionError, ValidationError
from kanso.models.call import Answer, Call
from kanso.models.ledger import cost_of
from kanso.models.register import script_path
from kanso.schemas.models import ModelSpec

__all__ = ["MockClient", "reset"]

CHARS_PER_TOKEN: Final = 4
"""The mock has no tokeniser; a fixed ratio keeps its ledger rows deterministic."""

_CURSORS: dict[tuple[str, str], int] = {}
"""`(script, task class) -> the number of calls made`, for the life of this process."""


def reset() -> None:
    """Forget every cursor. For a test that wants a script read from its start."""
    _CURSORS.clear()


class MockClient:
    """A client that answers from a file instead of from a wire."""

    protocol: ClassVar[str] = "mock"

    def __init__(self, root: Path) -> None:
        self.root = root

    def complete(self, spec: ModelSpec, call: Call) -> Answer:
        """The next scripted answer for this task class, advancing that class's cursor."""
        path = script_path(self.root, spec)
        answers = _script(path).get(call.task_class, [])
        key = (str(path), call.task_class)
        turn = _CURSORS.get(key, 0)
        _CURSORS[key] = turn + 1
        data: Mapping[str, object] = answers[turn % len(answers)] if answers else {}
        tokens_in = (len(call.system) + len(call.user)) // CHARS_PER_TOKEN
        tokens_out = len(json.dumps(data, default=str)) // CHARS_PER_TOKEN
        return Answer(
            data=data,
            model=spec.id,
            tier=call.tier,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost_of(tokens_in, tokens_out, spec.cost_in, spec.cost_out),
            cache_hit=None,
        )


def _script(path: Path) -> dict[str, list[Mapping[str, object]]]:
    """The script at `path`: a task class to a list of answers.

    Read on every call rather than cached, so an operator or a test may rewrite the file
    between calls and see the change; the cursor is unaffected, because it counts calls
    rather than positions.
    """
    if not path.is_file():
        raise PreconditionError(
            f"the mock script {path} does not exist",
            remedy="write the script, or point the model's `script` at one that exists",
        )
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationError(f"{path}: cannot be read as YAML: {exc}") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValidationError(f"{path}: expected a mapping of task class to answers")
    out: dict[str, list[Mapping[str, object]]] = {}
    for task, answers in loaded.items():
        if not isinstance(answers, list):
            raise ValidationError(f"{path}: {task}: expected a list of answers")
        for answer in answers:
            if not isinstance(answer, dict):
                raise ValidationError(f"{path}: {task}: every answer must be a mapping")
        out[str(task)] = list(answers)
    return out
