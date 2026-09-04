"""The router: the one path a model call takes, and the ladder it climbs.

Every model call kanso makes goes through `route`, and `route` is the only place a client
is constructed. That is what makes the ladder a property of the system rather than a habit
of four call sites, and it is what makes the ledger complete: an attempt that is not routed
is an attempt that is not recorded, and there is no way to make one.

**The ladder.** A task class is routed to a tier, an effort and an output cap.

1. The routed tier's first-listed model answers, at that effort and cap.
2. If the answer does not satisfy the task class's schema — or the calling step's own
   check — the same model is asked again, once, with the complaints appended to the user
   turn and the system turn untouched, so the provider cache still hits.
3. If it still does not, the next adjacent tier's first-listed model is asked once, at the
   same effort. Adjacent, never skipping: `cheap` escalates to `mid`, `mid` to `frontier`,
   and `frontier` to nothing. A class routed to the top tier therefore makes exactly two
   attempts.
4. If it still does not, the calling step fails.

The escalation carries the original user turn rather than the complaints. The retry exists
to correct a model that nearly answered; the escalation exists because the model was the
wrong size for the question, and priming a better model with a worse one's mistakes
narrows it to the same wrong ground.

**Every attempt is ledgered, the failed ones included.** A rejected answer was generated
and billed. A ledger holding only the answers that were used would under-report exactly
the calls that cost the most.

**No prompt carries a credential.** Before each attempt leaves, the assembled turns are
checked against every credential value the workspace can resolve; a prompt in which one
appears is refused by name and never sent. Nothing here reads a credential to put it in a
prompt — the check is against a mistake upstream, in the facts a calling step assembled.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kanso import creds
from kanso.errors import PreconditionError, ValidationError
from kanso.models.anthropic import AnthropicClient
from kanso.models.call import Answer, Call, CallInputs, Client
from kanso.models.jsonschema import validate
from kanso.models.ledger import LedgerEntry, ledger
from kanso.models.mock import MockClient
from kanso.models.openai_compat import OpenAICompatClient
from kanso.models.register import covered, escalation, first_for_tier, read_register, route_for
from kanso.models.tasks import build
from kanso.schemas.models import TASK_CLASSES, ModelsFile, ModelSpec, TaskClass, Tier
from kanso.state import StateStore
from kanso.workspace import Workspace

__all__ = ["ModelCheck", "check", "client_for", "route"]

MIN_SECRET_LENGTH: Final = 8
"""Shorter values are not treated as credentials: a two-character variable would match
half the prompts ever written, and refusing those would be a denial of service, not a
protection."""

CHECK_TASK: Final = "check"
"""The task class a register check is ledgered under. Not one of the four call sites."""

CHECK_MAX_OUTPUT: Final = 64

CHECK_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ModelCheck:
    """What one minimal call to one configured model proved."""

    id: str
    provider: str
    protocol: str
    tiers: tuple[Tier, ...]
    ok: bool
    latency_ms: float
    detail: str


def client_for(root: Path, spec: ModelSpec) -> Client:
    """The client that speaks a model's protocol.

    Every provider specific in this package is reached from here and from nowhere else:
    the core knows a protocol name and a register entry, and what those mean is a file in
    this package.
    """
    if spec.protocol == "anthropic":
        return AnthropicClient(root)
    if spec.protocol == "openai_compat":
        return OpenAICompatClient(root)
    return MockClient(root)


def route(
    ws: Workspace,
    store: StateStore,
    task_class: str,
    call_inputs: CallInputs,
    *,
    lane: str = "op",
) -> Answer:
    """Make one task class's call, climbing the ladder until an answer is usable."""
    task = _task(task_class)
    register = read_register(ws)
    covered(register)
    resolved = route_for(register, task)
    call = build(task, resolved, call_inputs)
    secrets = _secrets(ws, register)
    tried: list[str] = []

    def attempt(tier: Tier, this: Call) -> tuple[Answer, list[str]]:
        spec = first_for_tier(register, tier)
        tried.append(f"{spec.id} on {tier}")
        answer = _attempt(ws, store, spec, this, lane, secrets)
        return answer, _complaints(answer, this, call_inputs.check)

    answer, complaints = attempt(resolved.tier, call)
    if not complaints:
        return answer

    answer, complaints = attempt(resolved.tier, call.retrying(complaints))
    if not complaints:
        return answer

    higher = escalation(resolved.tier)
    if higher is not None:
        answer, complaints = attempt(higher, call.at(higher))
        if not complaints:
            return answer

    raise PreconditionError(
        f"{task}: no model answered in a usable shape in {len(tried)} attempts "
        f"({', '.join(tried)})",
        remedy="the last answer was rejected because " + "; ".join(complaints[:5]),
    )


def check(ws: Workspace, store: StateStore) -> list[ModelCheck]:
    """One minimal call per configured model, in file order.

    Each call is a real call and is ledgered like any other, because it costs what a call
    costs. It asks for the smallest possible object at no thinking effort, and it judges
    only whether an answer came back: a model that answers something other than what was
    asked is reachable, which is what this command is for.
    """
    register = read_register(ws)
    results: list[ModelCheck] = []
    for spec in register.models:
        call = Call(
            task_class=CHECK_TASK,
            tier=spec.tiers[0],
            effort="none",
            max_output=CHECK_MAX_OUTPUT,
            system='Reply with the JSON object {"ok": true} and nothing else.',
            user="Reply now.",
            schema=CHECK_SCHEMA,
        )
        started = time.perf_counter()
        try:
            answer = client_for(ws.root, spec).complete(spec, call)
        except (PreconditionError, ValidationError) as exc:
            results.append(
                _result(spec, False, (time.perf_counter() - started) * 1000, exc.message)
            )
            continue
        ledger(
            store,
            LedgerEntry(
                task_class=CHECK_TASK,
                model=spec.id,
                tokens_in=answer.tokens_in,
                tokens_out=answer.tokens_out,
                cost=answer.cost,
                cache_hit=answer.cache_hit,
            ),
        )
        detail = f"answered {answer.tokens_out} tokens for ${answer.cost:.6f}"
        results.append(_result(spec, True, (time.perf_counter() - started) * 1000, detail))
    return results


def _result(spec: ModelSpec, ok: bool, latency_ms: float, detail: str) -> ModelCheck:
    return ModelCheck(
        id=spec.id,
        provider=spec.provider,
        protocol=spec.protocol,
        tiers=spec.tiers,
        ok=ok,
        latency_ms=latency_ms,
        detail=detail,
    )


def _task(task_class: str) -> TaskClass:
    if task_class not in TASK_CLASSES:
        raise ValidationError(
            f"{task_class!r} is not a task class",
            remedy=f"one of {', '.join(TASK_CLASSES)}",
        )
    return task_class


def _attempt(
    ws: Workspace,
    store: StateStore,
    spec: ModelSpec,
    call: Call,
    lane: str,
    secrets: Mapping[str, str],
) -> Answer:
    """One call to one model: guarded, made, and ledgered whatever it answered."""
    _no_credentials(call, secrets)
    answer = client_for(ws.root, spec).complete(spec, call)
    ledger(
        store,
        LedgerEntry(
            task_class=call.task_class,
            model=answer.model,
            tokens_in=answer.tokens_in,
            tokens_out=answer.tokens_out,
            cost=answer.cost,
            lane=lane,
            cache_hit=answer.cache_hit,
        ),
    )
    return answer


def _complaints(
    answer: Answer,
    call: Call,
    extra: Callable[[Mapping[str, object]], Sequence[str]] | None,
) -> list[str]:
    """Everything wrong with an answer: the schema first, then the caller's own check.

    The caller's check runs only on an answer that already satisfies the schema, so a
    step that reads a field is never handed one that is missing or of the wrong type. It
    is on the same ladder as the schema because a construct id that is not in the
    catalogue is as wrong as a missing field, and just as correctable by asking again.
    """
    found = validate(answer.data, call.schema)
    if found or extra is None:
        return found
    return list(extra(answer.data))


def _secrets(ws: Workspace, register: ModelsFile) -> dict[str, str]:
    """Every credential value this workspace can resolve, by variable name.

    The standard-named variables plus the ones the register renames, from the workspace
    `.env` and then the process environment — the same two places a credential is ever
    read from. Values are held for the length of one routing decision and are never
    written anywhere.
    """
    named = {spec.api_key_env for spec in register.models if spec.api_key_env}
    found: dict[str, str] = {}
    for source in (creds.read_env_file(ws.root), os.environ):
        for name, value in source.items():
            keep = name.startswith(creds.PREFIX) or name in named
            if keep and len(value) >= MIN_SECRET_LENGTH and name not in found:
                found[name] = value
    return found


def _no_credentials(call: Call, secrets: Mapping[str, str]) -> None:
    """Refuse a prompt in which a credential appears, naming the variable and not the value."""
    text = f"{call.system}\n{call.user}"
    leaked = sorted(name for name, value in secrets.items() if value in text)
    if leaked:
        raise ValidationError(
            f"the {call.task_class} prompt contains the value of {', '.join(leaked)}",
            remedy="a prompt states facts and never a credential; remove it from the inputs",
        )
